import io
import contextlib
import threading
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from seriald_core import access_info
from seriald_core.access_info import links, read_environment


class AccessTests(unittest.TestCase):
    def test_access_response_exposes_running_web_version_without_credentials(self):
        from seriald_core import __version__
        from test_connection_access import protected_connection, api_request
        with protected_connection() as fixture:
            status, result = api_request(fixture.url, '/api/access', fixture.owner_token)
            self.assertEqual(status, 200)
            self.assertEqual(result['version'], __version__)
            self.assertNotIn(fixture.owner_token, json.dumps(result))

    def test_installed_environment_is_parsed_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'service.env'
            file.write_text('# comment\nSERIALD_PORT=1234\nSERIALD_AUTH_TOKEN="has space"\nexport WRONG=x\nSERIALD_VALUE=\'$(no-command)\'\n')
            env = read_environment(file)
            self.assertEqual(env['SERIALD_AUTH_TOKEN'], 'has space')
            self.assertEqual(env['SERIALD_VALUE'], '$(no-command)')
            self.assertNotIn('WRONG',env)

    def test_password_uses_existing_share_api_never_prints_owner_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            requests=[]
            class Opener:
                def open(self, request, timeout):
                    requests.append(request)
                    return io.BytesIO(json.dumps({'open_access':False,'lan_hosts':['10.0.0.3','::1','evil.test']} if request.full_url.endswith('/api/access') else {'path':'/share/signed','expires_in':86400}).encode())
            result = links({'SERIALD_BASE':directory,'SERIALD_BIND':'0.0.0.0','SERIALD_PORT':'1234','SERIALD_AUTH_TOKEN':'owner-secret'},Opener())
            self.assertEqual(result['urls'],['http://127.0.0.1:1234/share/signed','http://10.0.0.3:1234/share/signed'])
            self.assertNotIn('owner-secret',json.dumps(result))
            self.assertEqual(requests[1].get_method(),'POST')
            self.assertEqual(requests[0].headers['Authorization'],'Bearer owner-secret')

    def test_saved_configuration_and_ipv6_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'web-settings.json').write_text(json.dumps({'version':1,'environment':{'BIND':'::1','PORT':'1235'},'documents':{}}))
            class Opener:
                def open(self, request, timeout):
                    self.url=request.full_url
                    return io.BytesIO(b'{"open_access":true}')
            opener=Opener();result=links({'SERIALD_BASE':directory},opener)
            self.assertEqual(opener.url,'http://[::1]:1235/api/access')
            self.assertEqual(result['urls'],['http://[::1]:1235/'])

    def test_real_service_share_link_grants_browser_cookie_without_owner_password(self):
        from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler
        from http.cookiejar import CookieJar
        from urllib.parse import urlsplit
        from test_connection_access import protected_connection
        with protected_connection() as fixture, tempfile.TemporaryDirectory() as directory:
            port=str(urlsplit(fixture.url).port)
            result=links({'SERIALD_BASE':directory,'SERIALD_BIND':'127.0.0.1','SERIALD_PORT':port,'SERIALD_AUTH_TOKEN':fixture.owner_token})
            opener=build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            # Follow the existing share redirect and then access authenticated API.
            with opener.open(result['urls'][0],timeout=3) as response:
                self.assertEqual(response.status,200)
            with opener.open(fixture.url+'/api/access',timeout=3) as response:
                self.assertFalse(json.load(response)['open_access'])
            self.assertNotIn(fixture.owner_token,json.dumps(result))


class AccessStartupTests(unittest.TestCase):
    def test_installer_waits_for_late_web_listener_without_starting_any_service(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import socket
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"open_access":true}')
            def log_message(self, *args):
                pass
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        server = []
        ready = threading.Event()
        def late_start():
            ready.wait(.15)
            endpoint = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            server.append(endpoint)
            endpoint.serve_forever(poll_interval=.01)
        worker = threading.Thread(target=late_start)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'seriald.env'
                config.write_text('SERIALD_BASE=' + directory + '\nSERIALD_BIND=127.0.0.1\nSERIALD_PORT=' + str(port) + '\n')
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    access_info.main(['--config', str(config), '--wait', '2'])
                self.assertIn('http://127.0.0.1:' + str(port) + '/', output.getvalue())
                self.assertEqual(calls, ['/api/access'])
        finally:
            ready.set()
            if server:
                server[0].shutdown()
                server[0].server_close()
            worker.join(timeout=3)

    def test_connection_failure_is_not_reported_as_missing_credentials(self):
        errors = io.StringIO()
        with patch.object(access_info, 'read_environment', return_value={}), patch.object(
                access_info, 'links', side_effect=ConnectionRefusedError()), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as result:
                access_info.main(['--wait', '.01'])
        self.assertEqual(result.exception.code, 1)
        self.assertIn('服务未就绪', errors.getvalue())
        self.assertNotIn('凭证', errors.getvalue())
        self.assertNotIn('密码', errors.getvalue())

    def test_unreadable_configuration_is_distinct_from_a_stopped_service(self):
        errors = io.StringIO()
        with patch.object(access_info, 'read_environment', side_effect=PermissionError()), patch.object(
                access_info, 'links') as request, contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                access_info.main(['--wait', '20'])
        request.assert_not_called()
        self.assertIn('sudo seriald-access', errors.getvalue())

    def test_authentication_failure_is_not_retried_as_startup(self):
        errors = io.StringIO()
        with patch.object(access_info, 'read_environment', return_value={}), patch.object(
                access_info, 'links', side_effect=HTTPError('http://localhost', 401, '', {}, None)) as request, contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                access_info.main(['--wait', '20'])
        self.assertEqual(request.call_count, 1)
        self.assertIn('凭证与运行服务不一致', errors.getvalue())
