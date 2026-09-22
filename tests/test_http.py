"""HTTP 行为只调用 Handler 方法和内存流,不监听端口也不接触真实串口。"""
import base64
import importlib.util
import io
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
from seriald_core import web as sd
from seriald_core import control_server as control
from seriald_core.agent_grants import AgentGrantStore
from seriald_core import transport as tr


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.manager = sd.Manager()
        self.manager.store = mock.Mock()
        self.device = mock.Mock(key='board-key', serial='SN1', dev='/fake/ttyUSB0')
        self.device.file_transfer.active = False
        self.manager.boards['SN1'] = self.device
        patcher = mock.patch.object(sd, 'MGR', self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self, path, body=b'', headers=None):
        handler = sd.Handler.__new__(sd.Handler)
        handler.path = path
        handler.headers = headers if headers is not None else {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def result(self, handler):
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def control_handler(self, path):
        handler = control.ControlHandler.__new__(control.ControlHandler)
        handler.path = path
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.static_root = str(ROOT)
        return handler

    def test_boot_compare_reads_two_archived_epochs(self):
        boot = {'id': 'boot-one', 'size': 16, 'started': '2026-09-20 08:00:00',
                'parts': [{'file': 'serial.log', 'start': 0, 'end': 16}]}
        self.device.boot_epochs.return_value = [boot]
        self.device.stable_dir.return_value = '/fake/archive'
        path = ('/api/boot-compare?left_key=board-key&left_serial=SN1&left_id=boot-one'
                '&right_key=board-key&right_serial=SN1&right_id=boot-one')
        with mock.patch.object(sd, 'read_epoch', side_effect=[b'U-Boot\nready\n', b'U-Boot\nerror\n']) as reader:
            handler = self.handler(path)
            handler.do_GET()
        code, result = self.result(handler)
        self.assertEqual(code, 200)
        self.assertEqual(result['counts']['changed'], 1)
        self.assertEqual(len(result['clues']), 1)
        self.assertEqual(reader.call_count, 2)

    def test_boot_compare_rejects_stale_identity(self):
        handler = self.handler('/api/boot-compare?left_key=board-key&left_serial=OLD&left_id=x')
        handler.do_GET()
        self.assertEqual(self.result(handler)[0], 404)

    def test_boot_compare_can_use_an_offline_second_board(self):
        other = mock.Mock(key='other', serial='SN2', present=False)
        self.manager.boards['SN2'] = other
        boot = {'id': 'boot-one', 'size': 16, 'parts': []}
        self.device.boot_epochs.return_value = [boot]
        other.boot_epochs.return_value = [boot]
        with mock.patch.object(sd, 'read_epoch', side_effect=[b'U-Boot\nready\n', b'U-Boot\nfailed\n']):
            handler = self.handler('/api/boot-compare?left_key=board-key&left_serial=SN1&left_id=boot-one'
                                   '&right_key=other&right_serial=SN2&right_id=boot-one')
            handler.do_GET()
        code, result = self.result(handler)
        self.assertEqual(code, 200)
        self.assertEqual(result['right']['serial'], 'SN2')
        self.assertEqual(result['clues'][0]['text'], 'failed')

    def test_root_page_serves_exact_local_html_bytes(self):
        expected = (ROOT / 'index.html').read_bytes()
        handler = self.control_handler('/')
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), expected)
        headers = {call.args[0].lower(): call.args[1] for call in handler.send_header.call_args_list}
        self.assertTrue(headers['content-type'].startswith('text/html'))
        self.assertEqual(headers['content-length'], str(len(expected)))

    def test_index_html_follows_the_same_path_as_root(self):
        # 交给 SimpleHTTPRequestHandler 会漏掉 Cache-Control,浏览器整页启发式缓存,
        # 服务换了新页面用户还是旧的(踩过:新按钮在页面上不存在)。
        expected = (ROOT / 'index.html').read_bytes()
        handler = self.control_handler('/index.html')
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), expected)

    def test_page_responses_forbid_heuristic_caching(self):
        # end_headers 里的判断按 self.path 走,两个入口都必须带上 no-cache。
        for path in ('/', '', '/index.html'):
            handler = self.control_handler(path)
            # self.handler() 把 end_headers 换成了 Mock,这里要显式调类上的真实现。
            with mock.patch.object(control.http.server.BaseHTTPRequestHandler, 'end_headers') as parent:
                control.ControlHandler.end_headers(handler)
            sent = dict(call.args for call in handler.send_header.call_args_list)
            self.assertEqual(sent.get('Cache-Control'), 'no-cache, must-revalidate', path)
            parent.assert_called_once_with()

    def test_control_proxy_rejects_unbounded_or_chunked_bodies_locally(self):
        cases = [({'Content-Length': '65537'}, 413),
                 ({'Content-Length': 'bad'}, 400),
                 ({'Content-Length': '0', 'Transfer-Encoding': 'chunked'}, 400),
                 ({'Content-Length': '5'}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                handler = self.control_handler('/api/identify')
                handler.command = 'POST'
                handler.headers = headers
                handler._proxy()
                self.assertEqual(handler.send_response.call_args.args[0], expected)

    def test_optional_control_token_supports_browser_basic_and_cli_bearer(self):
        import base64 as b64
        for authorization in ('Bearer secret',
                              'Basic ' + b64.b64encode(b'seriald:secret').decode()):
            with self.subTest(authorization=authorization):
                handler = self.control_handler('/healthz')
                handler.auth_token = 'secret'
                handler.headers = {'Authorization': authorization}
                handler.do_GET()
                self.assertEqual(handler.send_response.call_args.args[0], 200)
        denied = self.control_handler('/healthz')
        denied.auth_token = 'secret'
        denied.do_GET()
        self.assertEqual(denied.send_response.call_args.args[0], 401)
        headers = dict(call.args for call in denied.send_header.call_args_list)
        self.assertIn('Basic realm="seriald"', headers['WWW-Authenticate'])

    def test_named_agent_token_overrides_untrusted_actor_before_proxy(self):
        token = 't' * 40
        handler = self.control_handler('/api/session-send')
        handler.auth_token = 'owner-secret'
        handler.agent_tokens = {'claude-code': token}
        handler.command = 'POST'
        body = json.dumps({'sid': 'ssh1', 'actor': 'web', 'data': 'pwd\r'}).encode()
        handler.headers = {'Authorization': 'Bearer ' + token,
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.getheader.return_value = None
        connection.getresponse.return_value.read.return_value = b''
        with mock.patch.object(control, 'UnixHTTPConnection', return_value=connection):
            handler.do_POST()
        forwarded = json.loads(connection.request.call_args.kwargs['body'])
        self.assertEqual(forwarded['actor'], 'claude-code')
        self.assertEqual(connection.request.call_args.kwargs['headers']['Content-Length'],
                         str(len(connection.request.call_args.kwargs['body'])))

    def test_scoped_agent_grant_filters_devices_and_pins_post_to_serial(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AgentGrantStore(str(pathlib.Path(folder) / 'grants.sqlite3'))
            self.addCleanup(store.close)
            token, grant = store.issue('device', 'SN1')
            headers = {'Authorization': 'Bearer ' + token}
            handler = self.control_handler('/api/devices')
            handler.auth_token, handler.grant_store, handler.headers = 'owner', store, headers
            handler.command = 'GET'
            reply = mock.Mock(status=200)
            reply.read.return_value = json.dumps({'devices': [
                {'key': 'A', 'serial': 'SN1'}, {'key': 'B', 'serial': 'SN2'}]}).encode()
            connection = mock.Mock()
            connection.getresponse.return_value = reply
            with mock.patch.object(control, 'UnixHTTPConnection', return_value=connection):
                handler.do_GET()
            self.assertEqual([item['key'] for item in self.result(handler)[1]['devices']], ['A'])

            body = json.dumps({'key': 'B', 'actor': 'web', 'data': 'pwd\r'}).encode()
            handler = self.control_handler('/api/session-send')
            handler.auth_token, handler.grant_store = 'owner', store
            handler.headers = dict(headers, **{'Content-Length': str(len(body))})
            handler.rfile, handler.command = io.BytesIO(body), 'POST'
            reply = mock.Mock(status=200)
            reply.getheader.return_value = None
            reply.read.return_value = b''
            connection.getresponse.return_value = reply
            with mock.patch.object(control, 'UnixHTTPConnection', return_value=connection):
                handler.do_POST()
            forwarded = json.loads(connection.request.call_args.kwargs['body'])
            self.assertEqual((forwarded['serial'], forwarded['actor']), ('SN1', grant['actor']))

            log = self.control_handler('/api/logs?key=A&serial=SN2')
            log.auth_token, log.grant_store, log.headers = 'owner', store, headers
            log.command = 'GET'
            with mock.patch.object(control, 'UnixHTTPConnection', return_value=connection):
                log.do_GET()
            forwarded_path = connection.request.call_args.args[1]
            self.assertIn('serial=SN1', forwarded_path)
            self.assertNotIn('SN2', forwarded_path)

            compare = self.control_handler('/api/boot-compare?left_serial=SN2&right_serial=SN1')
            compare.auth_token, compare.grant_store, compare.headers = 'owner', store, headers
            compare.command = 'GET'
            compare.do_GET()
            self.assertEqual(self.result(compare)[0], 403)

            denied = self.control_handler('/api/health')
            denied.auth_token, denied.grant_store, denied.headers = 'owner', store, headers
            denied.command = 'GET'
            denied.do_GET()
            self.assertEqual(self.result(denied)[0], 403)

    def test_revoked_agent_token_cannot_reuse_open_mode_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AgentGrantStore(str(pathlib.Path(folder) / 'grants.sqlite3'))
            self.addCleanup(store.close)
            token, grant = store.issue('all')
            handler = self.control_handler('/api/devices')
            handler.auth_token, handler.grant_store = '', store
            handler.headers = {'Authorization': 'Bearer ' + token}
            self.assertTrue(handler._authorized())
            store.revoke(grant['id'])
            self.assertFalse(handler._authorized())
            self.assertFalse(handler._owner_authorized())
            handler.auth_token = 'owner-secret'
            handler.trust_loopback = True
            handler.client_address = ('127.0.0.1', 12345)
            self.assertFalse(handler._authorized())

    def test_shared_agent_cannot_mint_or_revoke_grants_or_rename_board(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AgentGrantStore(str(pathlib.Path(folder) / 'grants.sqlite3'))
            self.addCleanup(store.close)
            token, _ = store.issue('all')
            headers = {'Authorization': 'Bearer ' + token}
            for route, body in (('/api/agent-share', {'scope': 'all'}),
                                ('/api/agent-revoke', {'id': '0123456789abcdef'}),
                                ('/api/label', {'key': 'board-key', 'label': 'new'})):
                with self.subTest(route=route):
                    handler = self.control_handler(route)
                    handler.auth_token, handler.grant_store = 'owner', store
                    raw = json.dumps(body).encode()
                    handler.headers = dict(headers, **{'Content-Length': str(len(raw))})
                    handler.rfile, handler.command = io.BytesIO(raw), 'POST'
                    handler.do_POST()
                    self.assertEqual(self.result(handler)[0], 403)

    def test_revoking_grant_closes_matching_live_ssh_sessions(self):
        actor = 'shared-agent/abcdef0123456789'
        matching = mock.Mock(actor=actor)
        other = mock.Mock(actor='another-agent')
        self.device.sessions_lock = threading.RLock()
        self.device.ssh_sessions = {'ssh1': matching, 'ssh2': other}
        self.device.close_session.return_value = True
        body = json.dumps({'actor': actor}).encode()
        handler = self.handler('/api/agent-revoke', body)
        handler.do_POST()
        self.assertEqual(self.result(handler)[1]['closed_sessions'], 1)
        self.device.close_session.assert_called_once_with('ssh1')

    def test_agent_skill_and_cli_are_self_hosted_without_login(self):
        for route in ('/serialctl.py', '/skills/seriald-board/SKILL.md'):
            with self.subTest(route=route):
                handler = self.control_handler(route)
                handler.auth_token = 'owner-secret'
                handler.do_GET()
                self.assertEqual(handler.send_response.call_args.args[0], 200)
                self.assertTrue(handler.wfile.getvalue())

    def test_protected_lan_allows_only_local_agents_without_token(self):
        local = self.control_handler('/healthz')
        local.auth_token = 'secret'
        local.trust_loopback = True
        local.client_address = ('127.0.0.1', 12345)
        local.do_GET()
        self.assertEqual(local.send_response.call_args.args[0], 200)

        remote = self.control_handler('/healthz')
        remote.auth_token = 'secret'
        remote.trust_loopback = True
        remote.client_address = ('192.0.2.10', 12345)
        remote.do_GET()
        self.assertEqual(remote.send_response.call_args.args[0], 401)

    def test_share_link_grants_expiring_browser_cookie_but_cannot_create_more_links(self):
        owner = self.control_handler('/api/share-link')
        owner.auth_token = 'secret'
        owner.headers = {'Authorization': 'Bearer secret'}
        owner.do_POST()
        self.assertEqual(owner.send_response.call_args.args[0], 200)
        path = json.loads(owner.wfile.getvalue())['path']

        guest = self.control_handler(path)
        guest.auth_token = 'secret'
        guest.do_GET()
        self.assertEqual(guest.send_response.call_args.args[0], 303)
        cookie = dict(call.args for call in guest.send_header.call_args_list)['Set-Cookie']
        self.assertIn('HttpOnly', cookie)

        page = self.control_handler('/healthz')
        page.auth_token = 'secret'
        page.headers = {'Cookie': cookie.split(';', 1)[0]}
        page.do_GET()
        self.assertEqual(page.send_response.call_args.args[0], 200)

        agent = self.control_handler('/healthz')
        agent.auth_token = 'secret'
        agent.headers = {'Authorization': 'Bearer ' + path.rsplit('/', 1)[-1]}
        agent.do_GET()
        self.assertEqual(agent.send_response.call_args.args[0], 200)

        denied = self.control_handler('/api/share-link')
        denied.auth_token = 'secret'
        denied.headers = page.headers
        denied.do_POST()
        self.assertEqual(denied.send_response.call_args.args[0], 403)

        with mock.patch.object(control.time, 'time', return_value=control.time.time() + 90000):
            expired = self.control_handler('/healthz')
            expired.auth_token = 'secret'
            expired.headers = page.headers
            expired.do_GET()
        self.assertEqual(expired.send_response.call_args.args[0], 401)

    def test_health_reports_running_version_and_device_states(self):
        devices = [mock.Mock(serial='SN1', present=True),
                   mock.Mock(serial='', present=True),
                   mock.Mock(serial='SN2', present=False)]
        handler = self.handler('/api/health')
        with mock.patch.object(self.manager, 'all_devices', return_value=devices), \
                mock.patch.object(sd.shutil, 'disk_usage',
                                  return_value=mock.Mock(total=1000, free=600)):
            handler.do_GET()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload['version'], __version__)
        self.assertEqual((payload['devices'], payload['online'], payload['pending'], payload['offline']),
                         (3, 1, 1, 1))
        self.assertTrue(payload['logging_ok'])

    def test_health_exposes_serial_and_ssh_log_failures(self):
        session = mock.Mock(sid='ssh1', log_error='ssh disk full')
        self.device.log_error = 'serial disk full'
        self.device.ssh_sessions = {'ssh1': session}
        handler = self.handler('/api/health')
        with mock.patch.object(self.manager, 'all_devices', return_value=[self.device]), \
                mock.patch.object(sd.shutil, 'disk_usage',
                                  return_value=mock.Mock(total=1000, free=0)):
            handler.do_GET()
        _, payload = self.result(handler)
        self.assertFalse(payload['logging_ok'])
        self.assertEqual([x['kind'] for x in payload['log_errors']], ['serial', 'ssh'])

    def test_logs_list_uses_serial_archive_without_rewriting_files(self):
        self.device.log_files.return_value = [{'name': 'serial-20260919.log', 'size': 123}]
        self.device.boot_epochs.return_value = []
        self.device.stable_dir.return_value = str(ROOT / 'logs' / 'SN1')
        self.device.label = 'riscv-118'
        handler = self.handler('/api/logs?key=board-key')
        with mock.patch.object(sd.os, 'replace') as replace:
            handler.do_GET()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload['serial'], 'SN1')
        self.assertEqual(payload['archive'], 'SN1')
        self.assertEqual(payload['files'][0]['size'], 123)
        replace.assert_not_called()

    def test_log_download_streams_original_bytes_with_attachment_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'serial-20260921.log'
            original = b'raw\x00serial\xfflog\n'
            path.write_bytes(original)
            self.device.log_files.return_value = [{'name': path.name, 'kind': 'serial'}]
            self.device.stable_dir.return_value = folder
            handler = self.handler('/api/log?key=board-key&file=' + path.name + '&download=1')
            handler.do_GET()
            self.assertEqual(handler.send_response.call_args.args[0], 200)
            self.assertEqual(handler.wfile.getvalue(), original)
            headers = dict(call.args for call in handler.send_header.call_args_list)
            self.assertIn('attachment', headers['Content-Disposition'])
            self.assertEqual(headers['Content-Length'], str(len(original)))

    def test_file_transfer_routes_pin_connection_and_do_not_audit_chunk_content(self):
        self.device.connection_id = 'attachment-1'
        self.device.file_transfer.start.return_value = {
            'id': 'abc', 'direction': 'upload', 'path': '/tmp/file',
            'size': 3, 'chunk_size': 2048, 'offset': 0}
        start = {'key': 'board-key', 'serial': 'SN1', 'connection_id': 'attachment-1',
                 'direction': 'upload', 'path': '/tmp/file', 'size': 3}
        handler = self.handler('/api/file-transfer/start', json.dumps(start).encode())
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 200)
        self.device.file_transfer.start.assert_called_once_with(
            'upload', '/tmp/file', 'attachment-1', actor='web', size=3, overwrite=False)
        self.manager.store.event.assert_called_once()
        self.manager.store.event.reset_mock()

        self.device.file_transfer.chunk.return_value = {'id': 'abc', 'offset': 3, 'bytes': 3}
        chunk = {'key': 'board-key', 'serial': 'SN1', 'connection_id': 'attachment-1',
                 'id': 'abc', 'offset': 0, 'data_b64': base64.b64encode(b'abc').decode()}
        handler = self.handler('/api/file-transfer/chunk', json.dumps(chunk).encode())
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 200)
        self.manager.store.event.assert_not_called()
        self.device.file_transfer.chunk.assert_called_once_with('abc', 'attachment-1', 0, 'YWJj')

    def test_human_takeover_of_agent_ssh_is_validated_and_audited(self):
        session = mock.Mock(actor='agent', takeovers=set())
        session.write.return_value = True
        self.device.get_session.return_value = session
        body = json.dumps({'key': 'board-key', 'serial': 'SN1', 'sid': 'ssh-agent',
                           'actor': 'web'}).encode()
        handler = self.handler('/api/session-takeover', body)
        handler.do_POST()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertIn('web', session.takeovers)
        self.manager.store.event.assert_called_once_with('SN1', 'web', 'session-takeover', 'ssh-agent')

    def test_agent_ssh_rejects_other_actor_until_explicit_takeover(self):
        session = mock.Mock(actor='agent', takeovers=set())
        session.write.return_value = True
        self.device.get_session.return_value = session
        base = {'key': 'board-key', 'serial': 'SN1', 'sid': 'ssh-agent', 'actor': 'web'}
        for route in ('/api/session-send', '/api/session-close', '/api/session-resize'):
            body = dict(base, data='pwd\r')
            if route == '/api/session-resize':
                body.update(cols=100, rows=30)
            handler = self.handler(route, json.dumps(body).encode())
            handler.do_POST()
            self.assertEqual(self.result(handler)[0], 403)
        session.write.assert_not_called()
        session.resize.assert_not_called()
        self.device.close_session.assert_not_called()

        takeover = self.handler('/api/session-takeover', json.dumps(base).encode())
        takeover.do_POST()
        self.assertEqual(self.result(takeover)[0], 200)
        send = self.handler('/api/session-send', json.dumps(dict(base, data='pwd\r')).encode())
        send.do_POST()
        self.assertEqual(self.result(send)[0], 200)
        session.write.assert_called_once_with('pwd\r')

    def test_invalid_offsets_are_rejected_without_reading_device(self):
        for route in ('serial', 'session'):
            for offset in ('-1', 'invalid', '1.5'):
                with self.subTest(route=route, offset=offset):
                    handler = self.handler('/api/%s?key=board-key&off=%s' % (route, offset))
                    handler.do_GET()
                    status, payload = self.result(handler)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload['key'], 'board-key')
        self.device.read.assert_not_called()
        self.device.get_session.assert_not_called()

    def test_json_body_must_be_object(self):
        for value in ([], None, True, 'text', 12):
            with self.subTest(value=value):
                handler = self.handler('/api/send', json.dumps(value).encode())
                handler.do_POST()
                status, payload = self.result(handler)
                self.assertEqual(status, 400)
                self.assertEqual(payload['error'], 'JSON object required')
        self.device.send.assert_not_called()

    def test_invalid_lengths_fail_before_body_read(self):
        for length, expected in (('-1', 413), ('65537', 413), ('invalid', 400)):
            with self.subTest(length=length):
                handler = self.handler('/api/send', headers={'Content-Length': length})
                handler.rfile = mock.Mock()
                handler.do_POST()
                status, _ = self.result(handler)
                self.assertEqual(status, expected)
                handler.rfile.read.assert_not_called()

    def test_request_timeout_has_explicit_response(self):
        handler = self.handler('/api/send', headers={'Content-Length': '20'})
        handler.rfile = mock.Mock()
        handler.rfile.read.side_effect = TimeoutError('slow body')
        handler.do_POST()
        status, payload = self.result(handler)
        self.assertEqual(status, 408)
        self.assertIn('timed out', payload['error'])
        self.device.send.assert_not_called()

    def test_incomplete_body_and_transfer_encoding_are_rejected(self):
        handler = self.handler('/api/send', b'{}', {'Content-Length': '20'})
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 400)
        handler = self.handler('/api/send', headers={'Content-Length': '20',
                                                    'Transfer-Encoding': 'chunked'})
        handler.rfile = mock.Mock()
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 400)
        handler.rfile.read.assert_not_called()

    def test_raw_serial_bytes_and_device_binding_are_preserved(self):
        # 故意停在汉字中间:JSON 文本可替换,但终端使用的 base64 必须逐字节不变。
        data = '中'.encode()[:2]
        self.device.read.return_value = (2, data, True)
        for path, sid in (('/api/serial?key=board-key&off=0', None),
                          ('/api/session?key=board-key&sid=serial&off=0', 'serial')):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_GET()
                status, payload = self.result(handler)
                self.assertEqual(status, 200)
                self.assertEqual(payload['key'], 'board-key')
                self.assertEqual(payload.get('sid'), sid)
                self.assertEqual(payload['off'], 2)
                self.assertEqual(base64.b64decode(payload['data_b64']), data)

    def test_ssh_response_is_bound_to_requested_device_and_session(self):
        other = mock.Mock(key='other-key', serial='SN2', dev='/fake/ttyUSB1')
        self.manager.boards['SN2'] = other
        session = mock.Mock(ip='192.0.2.20')
        session.read.return_value = (4, b'\x80\xe4\xb8\xad', True)
        other.get_session.return_value = session
        handler = self.handler('/api/session?key=other-key&sid=ssh1&off=0')
        handler.do_GET()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload['key'], 'other-key')
        self.assertEqual(payload['sid'], 'ssh1')
        self.assertEqual(payload['ip'], '192.0.2.20')
        self.assertEqual(base64.b64decode(payload['data_b64']), b'\x80\xe4\xb8\xad')
        self.device.get_session.assert_not_called()
        other.get_session.assert_called_once_with('ssh1')

    def test_action_response_keeps_original_device_binding(self):
        self.device.send.return_value = (True, '')
        handler = self.handler('/api/send', json.dumps({'key': 'board-key', 'data': 'x'}).encode())
        handler.do_POST()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload['key'], 'board-key')
        self.device.send.assert_called_once_with('x')

    def test_rescue_step_success_requires_new_capture_bytes_to_confirm_recovery(self):
        self.device.operation_lock = threading.Lock()
        self.device.write_lock = threading.Lock()
        self.device.present = True
        self.device.fd = 42
        for output in (False, True):
            with self.subTest(new_output=output):
                self.device.bytes = 100
                def rescue_step(*args):
                    if output:
                        self.device.bytes += 5
                    return True, 'mock helper completed'
                handler = self.handler('/api/rescue', json.dumps({
                    'key': 'board-key', 'mode': 'soft'}).encode())
                with mock.patch.object(sd, 'try_rescue', side_effect=rescue_step), \
                        mock.patch.object(self.manager, '_matches', return_value=True):
                    handler.do_POST()
                status, payload = self.result(handler)
                self.assertEqual(status, 200)
                self.assertTrue(payload['ok'])
                self.assertEqual(payload['recovered'], output)
                self.assertFalse(payload['requires_setup'])

    def test_busy_console_prevents_rescue_helper_invocation(self):
        self.device.operation_lock = threading.Lock()
        self.device.operation_lock.acquire()
        handler = self.handler('/api/rescue', json.dumps({'key': 'board-key', 'mode': 'rebind'}).encode())
        try:
            with mock.patch.object(sd, 'try_rescue') as rescue, \
                    mock.patch.object(self.manager, '_matches') as matches:
                handler.do_POST()
        finally:
            self.device.operation_lock.release()
        status, payload = self.result(handler)
        self.assertEqual(status, 409)
        self.assertEqual(payload['key'], 'board-key')
        self.assertFalse(payload['ok'])
        rescue.assert_not_called()
        matches.assert_not_called()


class RootRescueTests(unittest.TestCase):
    def test_any_failed_step_makes_entire_root_rebind_fail(self):
        for failed_suffix in ('power/control', 'usb/unbind', 'usb/bind'):
            with self.subTest(step=failed_suffix):
                file_mock = mock.mock_open()

                def open_file(path, mode):
                    if path.endswith(failed_suffix):
                        raise OSError('simulated sysfs failure')
                    return file_mock(path, mode)

                with mock.patch.object(tr, 'usb_path_of', return_value='3-4.2'), \
                        mock.patch.object(tr.os, 'geteuid', return_value=0), \
                        mock.patch.object(tr, 'open', side_effect=open_file, create=True) as open_mock:
                    ok, note = tr.try_rescue('/fake/ttyUSB0', 'rebind')
                self.assertFalse(ok)
                self.assertIn('失败', note)
                self.assertEqual(open_mock.call_count, 3)

    def test_all_successful_root_steps_report_success(self):
        with mock.patch.object(tr, 'usb_path_of', return_value='3-4.2'), \
                mock.patch.object(tr.os, 'geteuid', return_value=0), \
                mock.patch.object(tr, 'open', mock.mock_open(), create=True) as open_mock:
            ok, _ = tr.try_rescue('/fake/ttyUSB0', 'rebind')
        self.assertTrue(ok)
        self.assertEqual(open_mock.call_count, 3)

    def test_invalid_root_path_is_rejected_before_sysfs_access(self):
        with mock.patch.object(tr, 'usb_path_of', return_value='3-4/../../evil'), \
                mock.patch.object(tr, 'open', mock.mock_open(), create=True) as open_mock:
            ok, _ = tr.try_rescue('/fake/ttyUSB0', 'soft')
        self.assertFalse(ok)
        open_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
