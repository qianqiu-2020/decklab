"""Real authenticated HTTPS across isolated Web/backend stores; no tty or USB I/O."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlencode

from seriald_core import control_server, device as device_module
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.backend_link import BackendConfigurationError, BackendLink, server_context
from seriald_core.backend_server import BackendServer
from seriald_core.devices import Manager
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.profiles import ProfileBook
from seriald_core.remote_settings import configure_remote_control, node_snapshot
from seriald_core.settings_restart import configure_collector
from seriald_core.storage import Store
from seriald_core.web_settings import WebSettings
from test_connection_access import api_request
from test_flash_discovery_integration import FakeDiscoveryExecutor
from seriald_core.flash_runner import FlashBook

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'backend-fixture-' + 'b' * 40


def certificates(folder=None):
    fixture = ROOT / 'tests/fixtures/backend-tls'
    return fixture / 'certificate.pem', fixture / 'public-test-key.pem'


@contextmanager
def split_nodes(certificate, key):
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        (base / 'backend').mkdir(); (base / 'web').mkdir()
        store = Store(base / 'backend/registry.sqlite3')
        grants = AgentGrantStore(base / 'web/agent-grants.sqlite3')
        manager = Manager(store, artifact_dir=base / 'backend/artifacts', profiles=ProfileBook({
            'default': 'raw', 'profiles': {'raw': {'probe': 'manual'}}}))
        manager.flash_book, manager.flash_executor = FlashBook(), FakeDiscoveryExecutor()
        manager.flash_discovery_argv = ('fake-never-executed',)
        backend_settings = WebSettings(base / 'backend/web-settings.json', {
            'SERIALD_BASE': str(base / 'backend'), 'SERIALD_BACKEND_CERT': str(certificate),
            'SERIALD_BACKEND_KEY': str(key), 'SERIALD_BACKEND_TOKEN': TOKEN})
        device = device_module.Device('fake-port', 'FAKE-COM4', manager)
        device.fd, device.present = 98765, True
        manager.devices[device.by_path] = device
        with BackendServer(('127.0.0.1', 0), server_context(str(certificate), str(key)), TOKEN) as backend:
            backend.manager, backend.host_settings = manager, backend_settings
            configure_collector(backend, backend_settings)
            url = 'https://127.0.0.1:%d' % backend.server_address[1]
            settings = WebSettings(base / 'web/web-settings.json', {'SERIALD_BASE': str(base / 'web'),
                'SERIALD_BACKEND_URL': url, 'SERIALD_BACKEND_TOKEN': TOKEN,
                'SERIALD_BACKEND_CA': str(certificate)})
            class Control(control_server.ControlHandler):
                collector_link = BackendLink(url, TOKEN, str(certificate))
                collector_socket = str(base / 'web/this-socket-must-not-exist')
                auth_token = 'fixture-web-owner'
                grant_store = grants
                agent_tokens = {}
                trust_loopback = False
            Control.settings = settings
            with BoundedThreadingTCPServer(('127.0.0.1', 0), Control) as control, \
                    mock.patch.object(device, 'send', return_value=(True, '')) as send, \
                    mock.patch.object(device_module, 'LOG_ROOT', str(base / 'backend/logs')):
                settings.environment.update(SERIALD_PORT=str(control.server_address[1]), SERIALD_BIND='127.0.0.1')
                configure_remote_control(control, settings)
                workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, control)]
                for worker in workers: worker.start()
                try:
                    yield SimpleNamespace(base=base, backend=backend, control=control, settings=settings,
                        backend_settings=backend_settings, device=device, send=send, grants=grants, manager=manager,
                        token=Control.auth_token, url='http://127.0.0.1:%d' % control.server_address[1],
                        backend_url=url, link=Control.collector_link)
                finally:
                    device.fd, device.present = None, False
                    with device.cv: device.cv.notify_all()
                    for server in (control, backend): server.shutdown()
                    for worker in workers: worker.join(timeout=3)
        manager.close(); grants.close(); store.close()


class SplitNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.certificate, cls.key = certificates(cls.folder.name)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_https_devices_commands_and_authorization_revocation_keep_backend_ownership(self):
        with split_nodes(self.certificate, self.key) as f:
            code, issued = api_request(f.url, '/api/agent-share', f.token,
                {'scope': 'connection', 'key': f.device.key, 'connection_id': f.device.connection_id})
            self.assertEqual(code, 200, issued)
            token = issued['token']
            f.device.append(b'backend-only-output\r\n')
            code, devices = api_request(f.url, '/api/devices', token)
            self.assertEqual(code, 200, devices)
            self.assertEqual(devices['devices'][0]['dev'], 'FAKE-COM4')
            code, data = api_request(f.url, '/api/session', token, query={'key': f.device.key, 'sid': 'serial', 'off': 0})
            self.assertEqual(code, 200, data); self.assertIn('backend-only-output', data['data'])
            code, result = api_request(f.url, '/api/session-send', token,
                {'key': f.device.key, 'sid': 'serial', 'data': 'help\r'})
            self.assertEqual(code, 200, result); f.send.assert_called_once_with('help\r')
            self.assertFalse((f.base / 'web/registry.sqlite3').exists())
            self.assertFalse((f.base / 'web/this-socket-must-not-exist').exists())
            self.assertEqual(api_request(f.url, '/api/settings?target=backend', token)[0], 403)
            self.assertEqual(api_request(f.url, '/api/backend-settings', token)[0], 404)
            self.assertEqual(api_request(f.url, '/api/settings-activate', token, {'revision': 'x'})[0], 404)
            code, result = api_request(f.url, '/api/agent-revoke', f.token, {'id': issued['grant']['id']})
            self.assertEqual(code, 200, result)
            self.assertEqual(api_request(f.url, '/api/devices', token)[0], 401)

    def test_backend_rejects_web_credentials_and_untrusted_or_wrong_host_certificates(self):
        with split_nodes(self.certificate, self.key) as f:
            for supplied in ('', 'fixture-web-owner', 'seriald-ag-fake'):
                connection = http.client.HTTPSConnection(*f.backend.server_address,
                    context=ssl.create_default_context(cafile=str(self.certificate)), timeout=3)
                try:
                    connection.request('GET', '/api/devices', headers={'Authorization': 'Bearer ' + supplied})
                    self.assertEqual(connection.getresponse().status, 401)
                finally: connection.close()
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection = BackendLink(f.backend_url, TOKEN).connection(3)
                try: connection.request('GET', '/api/devices')
                finally: connection.close()
            code, web_settings = api_request(f.url, '/api/settings', f.token)
            self.assertEqual(code, 200, web_settings)
            self.assertEqual(web_settings['node_role'], 'web')
            self.assertEqual(web_settings['configuration']['environment']['BACKEND_TOKEN'], {'$redacted': True})
            self.assertNotIn(TOKEN, json.dumps(web_settings))

    def test_settings_are_stored_on_the_selected_node_and_web_restart_never_restarts_backend(self):
        with split_nodes(self.certificate, self.key) as f:
            code, current = api_request(f.url, '/api/settings?target=backend', f.token)
            self.assertEqual(code, 200, current); self.assertEqual(current['node_role'], 'backend')
            code, saved = api_request(f.url, '/api/settings?target=backend', f.token,
                {'action': 'save', 'revision': current['revision'],
                 'configuration': {'version': 1, 'environment': {'BAUD': '9600'}}})
            self.assertEqual(code, 200, saved)
            self.assertEqual(f.backend_settings.read()['environment']['BAUD'], '9600')
            self.assertEqual(f.settings.read(), {'version': 1, 'environment': {}, 'documents': {}})
            _, current = api_request(f.url, '/api/settings', f.token)
            code, denied = api_request(f.url, '/api/settings', f.token,
                {'action': 'save', 'revision': current['revision'],
                 'configuration': {'version': 1, 'environment': {'BAUD': '19200'}}})
            self.assertEqual(code, 400, denied)
            code, saved = api_request(f.url, '/api/settings', f.token,
                {'action': 'save', 'revision': current['revision'],
                 'configuration': {'version': 1, 'environment': {'UI_LANGUAGE': 'zh-CN'}}})
            self.assertEqual(code, 200, saved)
            with mock.patch.object(f.control, 'shutdown') as shutdown:
                code, applying = api_request(f.url, '/api/settings', f.token,
                    {'action': 'activate', 'revision': saved['revision']})
                self.assertEqual(code, 202, applying)
                for _ in range(100):
                    if shutdown.called: break
                    threading.Event().wait(0.01)
                self.assertTrue(shutdown.called)
            self.assertFalse(f.backend.restart_requested)

    def test_upload_and_usb_discovery_use_backend_cache_and_cannot_be_delegated_to_connection_grant(self):
        with split_nodes(self.certificate, self.key) as f:
            code, listed = api_request(f.url, '/api/flash-discover', f.token)
            self.assertEqual(code, 200, listed)
            target = listed['targets'][0]
            code, result = api_request(f.url, '/api/flash-target', f.token,
                {'target_id': target['target_id'], 'target_generation': target['target_generation']})
            self.assertEqual(code, 200, result)
            code, created = api_request(f.url, '/api/artifact-create', f.token,
                {'target_id': target['target_id'], 'target_generation': target['target_generation'], 'name': 'image.bin', 'size': 3})
            self.assertEqual(code, 200, created)
            import base64
            artifact = created['artifact']['id']
            self.assertEqual(api_request(f.url, '/api/artifact-append', f.token,
                {'id': artifact, 'offset': 0, 'data_b64': base64.b64encode(b'abc').decode()})[0], 200)
            self.assertEqual(api_request(f.url, '/api/artifact-seal', f.token, {'id': artifact})[0], 200)
            self.assertTrue((f.base / 'backend/artifacts' / (artifact + '.bin')).exists())
            self.assertFalse((f.base / 'web/artifacts').exists())

    def test_sse_offsets_are_forwarded_immediately_and_revocation_stops_remote_access(self):
        with split_nodes(self.certificate, self.key) as f:
            _, issued = api_request(f.url, '/api/agent-share', f.token,
                {'scope':'connection','key':f.device.key,'connection_id':f.device.connection_id})
            f.device.append(b'one')
            connection = http.client.HTTPConnection(*f.control.server_address, timeout=3)
            route='/api/session-events?'+urlencode({'key':f.device.key,'sid':'serial','off':0})
            try:
                connection.request('GET',route,headers={'Authorization':'Bearer '+issued['token']})
                stream=connection.getresponse();self.assertEqual(stream.status,200)
                first=stream.readline();second=stream.readline()
                self.assertEqual(first,b'id: 3\n');self.assertIn(b'data:',second)
                f.grants.revoke(issued['grant']['id']);f.device.append(b'two')
                self.assertEqual(api_request(f.url,'/api/devices',issued['token'])[0],401)
            finally: connection.close()

    def test_remote_reset_runs_only_the_fixed_backend_helper(self):
        from seriald_core.hardware import HardwareBook
        with split_nodes(self.certificate,self.key) as f:
            record=f.manager.store.bind({'serial':'REMOTE-BOARD','serial_source':'device-tree'},f.device.by_path,f.device.dev,'remote-board')
            f.device.record=record;f.device.board.attach(f.device);f.manager.boards[f.device.serial]=f.device.board
            marker=f.base/'backend/reset-fixture.txt'
            f.manager.hardware=HardwareBook({'boards':{f.device.serial:{'reset':{
                'argv':[sys.executable,'-c','from pathlib import Path; Path('+repr(str(marker))+').write_text("fixture-reset")']}}}})
            code, started=api_request(f.url,'/api/task-start',f.token,{'kind':'hardware-reset','key':f.device.key,
                'serial':f.device.serial,'connection_id':f.device.connection_id})
            self.assertEqual(code,202,started)
            for _ in range(100):
                _,current=api_request(f.url,'/api/task',f.token,query={'id':started['task']['id'],'serial':f.device.serial})
                if current['task']['state'] not in ('queued','running'):break
                threading.Event().wait(.02)
            self.assertEqual(current['task']['state'],'completed',current)
            self.assertEqual(marker.read_text(),'fixture-reset')
            self.assertFalse((f.base/'web/reset-fixture.txt').exists())

    def test_flash_task_survives_web_reconnection_and_repeated_request_id_does_not_reflash(self):
        with split_nodes(self.certificate,self.key) as f:
            _,listed=api_request(f.url,'/api/flash-discover',f.token);target=listed['targets'][0]
            scope={key:target[key] for key in ('target_id','target_generation')}
            self.assertEqual(api_request(f.url,'/api/flash-target',f.token,scope)[0],200)
            _,created=api_request(f.url,'/api/artifact-create',f.token,dict(scope,name='fixture.img',size=3));artifact=created['artifact']['id']
            self.assertEqual(api_request(f.url,'/api/artifact-append',f.token,{'id':artifact,'offset':0,'data_b64':'YWJj'})[0],200)
            self.assertEqual(api_request(f.url,'/api/artifact-seal',f.token,{'id':artifact})[0],200)
            code,preview=api_request(f.url,'/api/flash-plan',f.token,dict(scope,mode='partitions',artifact_id=artifact,
                selections=[{'storage':'emmc','partition':'rootfs','artifact_id':artifact}],verify='if-supported'))
            self.assertEqual(code,200,preview)
            f.manager.flash_executor.block=True
            payload=dict(scope,kind='flash',plan_id=preview['plan']['id'],binding_revision=1,request_id='remote-single-flash-001')
            code,started=api_request(f.url,'/api/task-start',f.token,payload);self.assertEqual(code,202,started)
            self.assertTrue(f.manager.flash_executor.started.wait(3))
            # A different front door, with its own listener, reuses the same
            # remote backend. It creates no collector or registry copy.
            f.control.shutdown()
            handler=f.control.RequestHandlerClass
            with BoundedThreadingTCPServer(('127.0.0.1',0),handler) as fresh:
                worker=threading.Thread(target=fresh.serve_forever,daemon=True);worker.start()
                try:
                    code,repeated=api_request('http://127.0.0.1:%d'%fresh.server_address[1],'/api/task-start',f.token,payload)
                    self.assertEqual(code,202,repeated);self.assertEqual(repeated['task']['id'],started['task']['id'])
                    self.assertEqual(len(f.manager.flash_executor.executions),1)
                    f.manager.flash_executor.release.set()
                    for _ in range(100):
                        _,current=api_request('http://127.0.0.1:%d'%fresh.server_address[1],'/api/task',f.token,query=dict(scope,id=started['task']['id']))
                        if current['task']['state'] not in ('queued','running'):break
                        threading.Event().wait(.02)
                    self.assertEqual(current['task']['state'],'completed',current)
                    self.assertEqual(len(f.manager.flash_executor.executions),1)
                finally:fresh.shutdown();worker.join(timeout=3)


class BackendValidationTests(unittest.TestCase):
    def test_link_rejects_plaintext_redirect_credentials_and_invalid_header_tokens(self):
        for url in ('http://host:8899', 'https://user:secret@host', 'https://host/path',
                    'https://host?token=secret', 'https://host/#fragment', 'https://host:70000'):
            with self.subTest(url=url), self.assertRaises(BackendConfigurationError): BackendLink(url, TOKEN)
        for token in ('', 'short', 'x' * 32 + '\r\nInjected: true', '文' * 40):
            with self.subTest(token=repr(token)), self.assertRaises(BackendConfigurationError): BackendLink('https://localhost', token)
