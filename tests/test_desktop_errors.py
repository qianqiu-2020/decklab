"""Connection failures across native worker IPC, public HTTP and MCP; no boards."""
import errno
import io
import json
import subprocess
import sys
import unittest
from unittest import mock

from decklab_core.desktop import DesktopBook, DesktopSessions
from decklab_core.desktop_errors import DesktopConnectionError, connection_error, redact
from decklab_core import desktop_providers as providers
from decklabctl import ClientError
import test_desktop


CONFIG = {'provider': 'rdp', 'engine': 'freerdp', 'allow_agent': True,
          'secret_url': 'rdp+ntlm-password://tester:private%20password@configured-host:3389',
          'display': 'monitor', 'display_kind': 'virtual'}


class DesktopErrorTests(unittest.TestCase):
    def test_helper_arguments_are_redacted_without_replacing_the_diagnostic(self):
        config = {'provider': 'helper', 'argv': ['helper', '--password', 'helper-private']}
        self.assertEqual(redact('helper failed: helper-private', config), 'helper failed: [REDACTED]')

    def test_successful_worker_discards_startup_and_later_sensitive_output(self):
        real_popen = subprocess.Popen
        script = """import sys,json
r=json.loads(sys.stdin.readline())
print('startup diagnostic',file=sys.stderr,flush=True)
print(json.dumps({'type':'reply','id':r['id'],'ok':True,'capabilities':{}}),flush=True)
r=json.loads(sys.stdin.readline())
sys.stderr.write('clipboard-secret'*10000);sys.stderr.flush()
print(json.dumps({'type':'reply','id':r['id'],'ok':True}),flush=True)
"""
        def spawn(command, **kwargs):
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        with mock.patch.object(providers.subprocess, 'Popen', side_effect=spawn):
            worker = providers.DesktopWorker(CONFIG)
            try:
                self.assertTrue(worker.request({'action': 'test'})['ok'])
                self.assertFalse(worker.connecting)
                self.assertEqual(worker.connection_stderr, b'')
            finally:
                worker.close()

    def test_native_codes_distinguish_authentication_network_and_certificate(self):
        for reason, code in [('ERRCONNECT_WRONG_PASSWORD', 'desktop-authentication-failed'),
                ('ERRCONNECT_DNS_ERROR', 'desktop-dns-failed'),
                ('ERRCONNECT_CONNECT_TRANSPORT_FAILED', 'desktop-network-failed'),
                ('certificate: pinned SHA-256 mismatch', 'desktop-certificate-rejected')]:
            failure = connection_error(CONFIG, 'connect', RuntimeError('start failed'), {'code': 0x20015, 'reason': reason})
            self.assertEqual(failure.code, code)
            self.assertIn(reason, str(failure))
            self.assertIn('0x00020015', json.dumps(failure.details))

    def test_worker_connection_error_retains_redacted_stderr_and_structured_failure(self):
        real_popen = subprocess.Popen
        script = """import sys,json
r=json.loads(sys.stdin.readline())
print('missing library; password=private password; '+r['config']['secret_url'],file=sys.stderr,flush=True)
print(json.dumps({'type':'reply','id':r['id'],'ok':False,'error_details':{
 'code':'desktop-runtime-failed','stage':'runtime','provider':'rdp','engine':'freerdp',
 'endpoint':'configured-host:3389','reason':r['config']['secret_url'],'traceback':'loader failed'}}),flush=True)
"""
        def spawn(command, **kwargs):
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        with mock.patch.object(providers.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(DesktopConnectionError) as caught:
                providers.DesktopWorker(CONFIG)
        details = json.dumps(caught.exception.details)
        self.assertIn('missing library', details)
        for secret in ('private password', 'private%20password', CONFIG['secret_url']):
            self.assertNotIn(secret, details)

    def test_worker_main_sends_connection_exception_chain_without_credentials(self):
        request = json.dumps({'id': 1, 'config': dict(CONFIG, engine='aardwolf')}) + '\n'
        stream = io.TextIOWrapper(io.BytesIO(request.encode()))
        output = io.StringIO()
        with mock.patch.object(sys, 'stdin', stream), mock.patch.object(sys, 'stdout', output), \
                mock.patch.object(sys, 'stderr', io.StringIO()), \
                mock.patch.object(providers, 'RDPDesktop', side_effect=ConnectionRefusedError(errno.ECONNREFUSED, CONFIG['secret_url'])):
            providers.worker_main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['error_details']['code'], 'desktop-connection-refused')
        self.assertIn('ConnectionRefusedError', result['error_details']['traceback'])
        self.assertNotIn(CONFIG['secret_url'], output.getvalue())

    def test_public_http_and_mcp_preserve_full_chain_and_release_failed_session(self):
        with test_desktop.fixture.automatic_flashing(known=[('GRAPHICS-SN', 'graphics')]) as fixture:
            _, adapter, _ = test_desktop.setup(fixture)
            def fail(config):
                try:
                    raise ConnectionRefusedError(errno.ECONNREFUSED, 'refused ' + config['secret_url'])
                except OSError as cause:
                    raise RuntimeError('connection wrapper ' + 'x' * 5000) from cause
            fixture.manager.desktop = DesktopSessions(fixture.manager,
                DesktopBook({'boards': {'GRAPHICS-SN': CONFIG}}), provider_factory=fail)
            code, result = test_desktop.DesktopTests.request(self, fixture, '/api/desktop/open',
                {'key': 'graphics', 'serial': 'GRAPHICS-SN', 'client_id': 'error-test'})
            self.assertEqual(code, 502)
            self.assertEqual(result['code'], 'desktop-connection-refused')
            self.assertIn('ConnectionRefusedError', result['details']['traceback'])
            self.assertEqual(fixture.manager.desktop.sessions, {})
            with self.assertRaises(ClientError) as caught:
                adapter.desktop('graphics', 'open')
            body = str(caught.exception).split(': ', 1)[1]
            parsed = json.loads(body)
            self.assertIn('traceback', parsed['details'])
            for secret in ('private password', 'private%20password', CONFIG['secret_url']):
                self.assertNotIn(secret, body)
