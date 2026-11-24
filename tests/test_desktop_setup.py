"""Real bounded handshake/HTTP setup checks; no UART, target login or desktop input."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit, urlunsplit

from seriald_core.desktop import DesktopBook, DesktopSessions
from seriald_core.desktop_setup import checked_host, hosts_for, probe, receive
from seriald_core.tasks import TaskError
from seriald_core.web_settings import WebSettings
from test_desktop import Screen
import test_flash_discovery_integration as fixture


@contextmanager
def peer(handler):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen()
    failures = []
    def serve():
        try:
            with listener.accept()[0] as stream:
                stream.settimeout(3)
                handler(stream)
        except Exception as error:
            failures.append(error)
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close(); worker.join(4)
        if worker.is_alive():
            raise AssertionError('probe peer did not close')
        if failures:
            raise failures[0]


class ProbeTests(unittest.TestCase):
    def test_only_known_unicast_board_interfaces_are_selected(self):
        device = SimpleNamespace(probe_info={'ips': ['lo 127.0.0.1/8', 'docker0 172.17.0.1/16',
            'vethX 10.0.1.1/24', 'end0 10.0.0.2/24', 'wifi0 10.0.0.2/24',
            'eth1 2001:db8::5/64', 'eth2 fe80::1/64', 'eth3 224.0.0.1/4', 'bad input', None]})
        self.assertEqual(hosts_for(device), ['10.0.0.2', '2001:db8::5'])
        for host in ('http://example', 'user@example', '0.0.0.0', 'a/../../b', 'a%eth0', '.host', 'a..b', 'a\n'):
            with self.subTest(host=host), self.assertRaises(TaskError):
                checked_host(host)
        self.assertEqual(checked_host('2001:db8::5'), '2001:db8::5')

    def test_vnc_is_identified_from_real_banner_without_sending_authentication(self):
        sent = []
        def serve(stream):
            stream.sendall(b'RFB 003.008\n'); sent.append(stream.recv(1))
        with peer(serve) as port:
            result = probe('127.0.0.1', port, 'vnc')
        self.assertEqual(result['protocol_version'], 'RFB 003.008')
        self.assertEqual(sent, [b''])
        with peer(lambda s: s.sendall(b'HTTP/1.0 200')) as port:
            self.assertIsNone(probe('127.0.0.1', port, 'vnc'))

    def test_rdp_tls_pin_is_observation_only_and_no_credentials_follow(self):
        certs = Path(__file__).parent / 'fixtures/backend-tls'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs / 'certificate.pem', certs / 'public-test-key.pem')
        received = []
        def serve(stream):
            received.append(receive(stream, 19))
            stream.sendall(bytes.fromhex('030000130ed000000000000200080002000000'))
            try:
                with context.wrap_socket(stream, server_side=True) as secured:
                    received.append(secured.recv(1))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError):
                # TLS 1.3 may write its final ticket after the probe has closed.
                # Windows can report the close while wrap_socket is returning.
                # The caller still asserts the exact certificate obtained by the
                # client; a failed client handshake cannot pass this test.
                received.append(b'')
        with peer(serve) as port:
            result = probe('127.0.0.1', port, 'rdp', timeout=2)
        expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert((certs / 'certificate.pem').read_text())).hexdigest()
        self.assertEqual(result['certificate_sha256'], expected)
        self.assertFalse(result['certificate_trusted'])
        self.assertEqual(received[-1], b'')
        self.assertEqual(received[0][-4:], b'\x0b\x00\x00\x00')

    def test_legacy_rdp_is_detected_but_never_entered(self):
        sent = []
        def serve(stream):
            receive(stream, 19)
            stream.sendall(bytes.fromhex('0300000b06d00000000000'))
            sent.append(stream.recv(1))
        with peer(serve) as port:
            result = probe('127.0.0.1', port, 'rdp')
        self.assertIn('TLS', result['error']); self.assertEqual(sent, [b''])


class SetupTests(unittest.TestCase):
    request = fixture.FlashDiscoveryHTTPIntegrationTests.request

    @contextmanager
    def setup(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f, tempfile.TemporaryDirectory() as directory:
            settings = WebSettings(Path(directory) / 'web-settings.json', {'SERIALD_BASE': directory})
            screen = Screen()
            f.manager.desktop = DesktopSessions(f.manager, provider_factory=lambda c: screen,
                book=DesktopBook(), settings=settings)
            device = f.manager.resolve('graphics')
            if device is None:
                device = f.manager.all_devices()[0]
            device.probe_info = dict(device.probe_info, ips=['eth0 10.0.0.2/24'])
            yield f, device, settings, screen

    def payload(self, settings, **changes):
        value = dict(revision=settings.snapshot()['revision'], provider='vnc', host='10.0.0.2',
                     port=5900, username='', password='private-pass', allow_agent=False)
        return dict(value, **changes)

    def test_owner_quick_open_uses_transient_credentials_and_board_stage(self):
        with self.setup() as (f, device, settings, screen):
            desktop = f.manager.desktop
            device.present = True
            device.board.attach(device)
            secret = secrets.token_hex(12)
            ephemeral = dict(provider='rdp', display='desktop', display_kind='unknown',
                             secret_url=urlunsplit(('rdp+ntlm-password', f'test-user:{secret}@board.example.test:3390', '', '', '')))
            body = dict(key=device.key, serial=device.serial)
            with mock.patch.object(desktop.setup, 'quick_config', return_value=(ephemeral, 'initial')):
                status, data = self.request(f, '/api/desktop/quick-open', body)
            self.assertEqual(status, 200, data)
            self.assertEqual(data['session']['login_stage'], 'initial')
            self.assertNotIn('secret_url', json.dumps(data))
            self.assertNotIn(secret, json.dumps(data))
            self.assertNotIn(device.serial, desktop.book.boards)
            self.assertIs(desktop.get(device, data['session']['id'], 'web')['config'], ephemeral)
            desktop.close_actor()
            self.assertFalse(desktop.list(device, 'web')['supported'])
            with mock.patch.object(desktop.setup, 'quick_config', return_value=(None, 'no-user')):
                status, data = self.request(f, '/api/desktop/quick-open', body)
            self.assertTrue(data['manual'])
            self.assertEqual(data['login_stage'], 'no-user')

    def test_web_can_set_up_an_unconfigured_board_without_restart_or_secret_response(self):
        with self.setup() as (f, device, settings, screen):
            q='?key=' + device.key + '&serial=' + device.serial
            status, data = self.request(f, '/api/desktop/sessions' + q)
            self.assertEqual(status, 200); self.assertFalse(data['supported'])
            self.assertEqual(data['setup']['hosts'], ['10.0.0.2'])
            payload = dict(self.payload(settings), key=device.key, serial=device.serial)
            status, data = self.request(f, '/api/desktop/configure', payload)
            self.assertEqual((status, data.get('state')), (200, 'configured'), data)
            self.assertNotIn('private-pass', json.dumps(data))
            self.assertTrue(data['setup']['connection']['password_saved'])
            status, data = self.request(f, '/api/desktop/open', {'key':device.key,'serial':device.serial})
            self.assertEqual(status, 200, data)
            self.assertIn('desktop-', data['session']['id'])
            saved = settings.read()['documents']['desktop']['boards'][device.serial]
            self.assertEqual(unquote(urlsplit(saved['secret_url']).password), 'private-pass')
            if os.name != 'nt':
                self.assertEqual(Path(settings.path).stat().st_mode & 0o777, 0o600)
            # A new native manager loads exactly the same original settings document.
            restarted = DesktopBook(settings.effective()['documents']['desktop'])
            self.assertEqual(restarted.boards[device.serial], saved)

    def test_setup_and_discovery_cannot_mutate_from_an_agent_grant(self):
        from seriald_mcp import Adapter, Settings
        from serialctl import ClientError
        from seriald_core.agent_grants import attach_agent_grants
        with self.setup() as (f, device, settings, _):
            attach_agent_grants(f.manager, f.grants)
            token, grant = f.grants.issue('device', serial=device.serial)
            from serialctl import Client
            client = Client(f.url, token=token)
            client.key, client.serial = device.key, device.serial
            with self.assertRaises(ClientError) as error:
                client.post('/api/desktop/configure', **self.payload(settings))
            self.assertEqual(error.exception.status, 403)
            adapter = Adapter(Settings(f.url, token))
            with mock.patch('seriald_core.desktop_setup.probe', return_value=None) as network:
                result = adapter.desktop(device.serial, 'discover')
                self.assertEqual(result['hosts'], ['10.0.0.2'])
                self.assertEqual({call.args[0] for call in network.call_args_list}, {'10.0.0.2'})
            with self.assertRaises(ClientError):
                adapter.desktop(device.serial, 'open')
            self.assertFalse(f.manager.desktop.sessions)
            self.assertFalse(settings.path.exists())

    def test_cache_and_identity_change_never_scan_a_subnet_or_reuse_old_result(self):
        with self.setup() as (f, device, settings, _):
            setup = f.manager.desktop.setup
            with mock.patch('seriald_core.desktop_setup.probe', side_effect=lambda host, port, provider:
                    {'host':host,'port':port,'provider':provider} if port==5900 else None) as call:
                first=setup.discover(device); self.assertEqual(len(first['candidates']),1)
                self.assertEqual(call.call_count,5)
                self.assertIn(mock.call('10.0.0.2', 3389, 'rdp'), call.call_args_list)
                self.assertIn(mock.call('10.0.0.2', 3390, 'rdp'), call.call_args_list)
                self.assertEqual(setup.discover(device),first); self.assertEqual(call.call_count,5)
                device.probe_info['ips']=['eth0 10.0.0.3/24']
                with self.assertRaises(TaskError) as error:
                    setup.configure(device, self.payload(settings,candidate_id=first['candidates'][0]['id']))
                self.assertEqual(error.exception.status,409)
                second=setup.discover(device)
                self.assertEqual(second['hosts'],['10.0.0.3']);self.assertEqual(call.call_count,10)

    def test_rdp_recheck_includes_both_default_ports_and_keeps_custom_ports_exact(self):
        with self.setup() as (f, device, settings, _):
            setup = f.manager.desktop.setup
            with mock.patch('seriald_core.desktop_setup.probe', side_effect=lambda host, port, provider:
                    {'host': host, 'port': port, 'provider': provider, 'certificate_sha256': 'a' * 64}) as network:
                for port in (3389, 3390):
                    network.reset_mock()
                    result = setup.discover(device, ('10.0.0.9', port, 'rdp'))
                    self.assertCountEqual(network.call_args_list, [mock.call('10.0.0.9', p, 'rdp') for p in (3389, 3390)])
                    self.assertEqual([item['port'] for item in result['candidates']], [3389, 3390])
                candidate = result['candidates'][1]
                setup.configure(device, self.payload(settings, provider='rdp', host='10.0.0.9', port=3390,
                    username='rdp', certificate_sha256='a' * 64, trust_certificate=True, candidate_id=candidate['id']))
                network.reset_mock()
                setup.discover(device, ('10.0.0.9', 3391, 'rdp'))
                network.assert_called_once_with('10.0.0.9', 3391, 'rdp')

    def test_new_certificate_requires_explicit_trust_and_password_is_not_reused_on_target_change(self):
        with self.setup() as (f, device, settings, _):
            setup=f.manager.desktop.setup
            payload=self.payload(settings,provider='rdp',port=3389,username='user',certificate_sha256='a'*64)
            with self.assertRaises(TaskError):setup.configure(device,payload)
            setup.configure(device,dict(payload,trust_certificate=True))
            same=self.payload(settings,provider='rdp',port=3389,username='user',certificate_sha256='a'*64,reuse_password=True)
            same.pop('password')
            setup.configure(device,same)
            changed=dict(same,revision=settings.snapshot()['revision'],host='10.0.0.3')
            with self.assertRaises(TaskError):setup.configure(device,changed)
            settings.save({'version':1,'environment':{'UI_LANGUAGE':'en'}},settings.snapshot()['revision'])
            with self.assertRaises(TaskError):setup.configure(device,same)

    def test_rdp_defaults_to_accepting_new_certificates_and_can_replace_an_old_pin(self):
        with self.setup() as (f, device, settings, _):
            setup = f.manager.desktop.setup
            observed = {'host':'10.0.0.2', 'port':3389, 'provider':'rdp', 'certificate_sha256':'a' * 64}
            with mock.patch('seriald_core.desktop_setup.probe', return_value=observed):
                candidate = setup.discover(device, ('10.0.0.2',3389,'rdp'))['candidates'][0]
            payload = dict(provider='rdp', host='10.0.0.2', port=3389, username='rdp')
            result = setup.configure(device, self.payload(settings, **payload, candidate_id=candidate['id']))
            self.assertEqual(result['setup']['connection']['certificate_policy'], 'accept')
            config = setup.desktop.book.boards[device.serial]
            self.assertNotIn('certificate_sha256', config)
            setup.configure(device, self.payload(settings, **payload, certificate_sha256='a' * 64, trust_certificate=True))
            result = setup.configure(device, self.payload(settings, **payload, certificate_policy='accept',
                                                           password=None, reuse_password=True))
            self.assertEqual(result['setup']['connection']['certificate_policy'], 'accept')
            self.assertTrue(result['setup']['connection']['password_saved'])
            self.assertEqual(f.manager.desktop.book.boards[device.serial]['engine'],'freerdp')

    def test_other_boards_configuration_and_active_session_ownership_are_preserved(self):
        with self.setup() as (f, device, settings, _):
            setup=f.manager.desktop.setup
            setup.configure(device,self.payload(settings))
            original=settings.read()
            f.manager.desktop.open(device,'web','browser-a')
            with self.assertRaises(TaskError) as error:
                setup.configure(device,self.payload(settings,host='10.0.0.4'))
            self.assertEqual(error.exception.status,423)
            self.assertEqual(settings.read(),original)

    def test_manual_probe_is_one_explicit_owner_target_and_agents_cannot_select_it(self):
        from serialctl import Client, ClientError
        from seriald_core.agent_grants import attach_agent_grants
        with self.setup() as (f, device, settings, _):
            with mock.patch('seriald_core.desktop_setup.probe', side_effect=lambda host, port, provider:
                    {'host':host,'port':port,'provider':provider}) as network:
                scope={'key':device.key,'serial':device.serial}
                status,data=self.request(f,'/api/desktop/discover',dict(scope,host='10.0.0.9',port=5905,provider='vnc'))
                self.assertEqual(status,200,data);network.assert_called_once_with('10.0.0.9',5905,'vnc')
                candidate=data['candidates'][0]
                f.manager.desktop.setup.configure(device,self.payload(settings,host='10.0.0.9',port=5905,candidate_id=candidate['id']))
                attach_agent_grants(f.manager,f.grants);token,_=f.grants.issue('device',serial=device.serial)
                client=Client(f.url,token=token);client.key,client.serial=device.key,device.serial
                with self.assertRaises(ClientError) as error:
                    client.post('/api/desktop/discover',key=device.key,host='10.0.0.8',port=5905,provider='vnc')
                self.assertEqual(error.exception.status,403)
                self.assertEqual(network.call_count,1)

    def test_private_config_preserves_other_boards_reservation_and_display_metadata(self):
        with self.setup() as (f, device, settings, _):
            other={'provider':'vnc','secret_url':'vnc+plain://:other-private@10.0.0.5:5900',
                   'display':'physical-A','display_kind':'physical'}
            lease={'origin':'http://console.example','credential_file':str(Path(settings.path).parent/'private-fixture.json'),
                   'device_id':'board-id','task':'TASK-1','requester':'owner','lease_id':'lease-1'}
            current=dict(other,display='monitor-B',reservation=lease,scale=1.5,width=1280,height=800)
            document={'boards':{device.serial:current,'OTHER-SN':other}}
            file=Path(settings.path).parent/'external-desktop.json';file.write_text(json.dumps(document))
            settings.environment['SERIALD_DESKTOP']=str(file)
            f.manager.desktop.book=DesktopBook(document)
            f.manager.desktop.setup.configure(device,self.payload(settings))
            saved=settings.read()['documents']['desktop']['boards']
            self.assertEqual(saved['OTHER-SN'],other)
            self.assertEqual(f.manager.desktop.book.boards['OTHER-SN'],other)
            self.assertEqual(saved[device.serial]['reservation'],lease)
            self.assertEqual({k:saved[device.serial][k] for k in ('display','scale','width','height')},
                             {'display':'monitor-B','scale':1.5,'width':1280,'height':800})
            self.assertNotIn('private',json.dumps(f.manager.desktop.setup.public(device)))

    def test_identity_change_during_discovery_discards_results(self):
        with self.setup() as (f, device, settings, _):
            def changed(*args):
                device.probe_info['ips']=['eth0 10.0.0.4/24']
                return None
            with mock.patch('seriald_core.desktop_setup.probe',side_effect=changed),self.assertRaises(TaskError) as error:
                f.manager.desktop.setup.discover(device)
            self.assertEqual(error.exception.status,409)
            self.assertFalse(f.manager.desktop.setup.cache)
            self.assertFalse(settings.path.exists())

    def test_open_rechecks_binding_after_configuration_changes(self):
        with self.setup() as (f, device, settings, screen):
            desktop=f.manager.desktop
            desktop.setup.configure(device,self.payload(settings))
            factory=mock.Mock(return_value=screen);desktop.provider_factory=factory
            def change(_):
                replacement=dict(desktop.book.boards[device.serial],secret_url='vnc+plain://new-target:5900')
                desktop.book=DesktopBook({'boards':{device.serial:replacement}})
            with mock.patch('seriald_core.desktop.check_reservation',side_effect=change),self.assertRaises(TaskError) as error:
                desktop.open(device,'web','browser')
            self.assertEqual(error.exception.status,409);factory.assert_not_called()
            self.assertFalse(desktop.sessions)


    def test_legacy_vnc_password_in_username_never_becomes_public_username(self):
        with self.setup() as (f, device, settings, _):
            config={'provider':'vnc','secret_url':'vnc+plain://legacy-private@10.0.0.2:5900',
                    'display':'vnc','display_kind':'unknown'}
            f.manager.desktop.book=DesktopBook({'boards':{device.serial:config}})
            public=f.manager.desktop.setup.public(device)
            self.assertEqual(public['connection']['username'],'')
            self.assertTrue(public['connection']['password_saved'])
            self.assertNotIn('legacy-private',json.dumps(public))
            payload=self.payload(settings,reuse_password=True);payload.pop('password')
            f.manager.desktop.setup.configure(device,payload)
            saved=settings.read()['documents']['desktop']['boards'][device.serial]
            self.assertEqual(unquote(urlsplit(saved['secret_url']).password),'legacy-private')


    def test_explicit_manual_recheck_is_fresh_while_automatic_results_are_cached(self):
        with self.setup() as (f, device, settings, _):
            endpoint=('10.0.0.2',5905,'vnc')
            with mock.patch('seriald_core.desktop_setup.probe',side_effect=[None,{'host':endpoint[0],'port':5905,'provider':'vnc'}]) as network:
                self.assertEqual(f.manager.desktop.setup.discover(device,endpoint)['candidates'],[])
                result=f.manager.desktop.setup.discover(device,endpoint)
                self.assertEqual(len(result['candidates']),1)
                self.assertEqual(network.call_count,2)
