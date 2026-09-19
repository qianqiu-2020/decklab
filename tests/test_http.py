"""HTTP 行为只调用 Handler 方法和内存流,不监听端口也不接触真实串口。"""
import base64
import importlib.util
import io
import json
import pathlib
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('_seriald_http_test', ROOT / 'seriald.py')
sd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sd)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.manager = sd.Manager()
        self.device = mock.Mock(key='board-key', dev='/fake/ttyUSB0')
        self.manager.devices['board-key'] = self.device
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

    def test_root_page_serves_exact_local_html_bytes(self):
        expected = (ROOT / 'index.html').read_bytes()
        handler = self.handler('/')
        with mock.patch.object(sd, 'BASE', str(ROOT)):
            handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), expected)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertEqual(headers['Content-Length'], str(len(expected)))

    def test_index_html_follows_the_same_path_as_root(self):
        # 交给 SimpleHTTPRequestHandler 会漏掉 Cache-Control,浏览器整页启发式缓存,
        # 服务换了新页面用户还是旧的(踩过:新按钮在页面上不存在)。
        expected = (ROOT / 'index.html').read_bytes()
        handler = self.handler('/index.html')
        with mock.patch.object(sd, 'BASE', str(ROOT)):
            handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), expected)

    def test_page_responses_forbid_heuristic_caching(self):
        # end_headers 里的判断按 self.path 走,两个入口都必须带上 no-cache。
        for path in ('/', '', '/index.html'):
            handler = self.handler(path)
            # self.handler() 把 end_headers 换成了 Mock,这里要显式调类上的真实现。
            with mock.patch.object(sd.http.server.BaseHTTPRequestHandler, 'end_headers') as parent:
                sd.Handler.end_headers(handler)
            sent = dict(call.args for call in handler.send_header.call_args_list)
            self.assertEqual(sent.get('Cache-Control'), 'no-cache, must-revalidate', path)
            parent.assert_called_once_with()

    def test_logs_list_includes_hashed_and_legacy_files_without_rewriting(self):
        current = '/fake/logs/full-key-0123456789abcdef'
        legacy = '/fake/logs/old-prefix'
        self.device.stable_dir.return_value = current
        self.device.legacy_dir.return_value = legacy
        self.device.label = 'board-label'
        entries = {current: ['serial-20260919.log', 'ssh-20260919.log', 'ignore.txt'],
                   legacy: ['serial-20260918.log', 'ssh-20260918.log', 'labels.json']}
        handler = self.handler('/api/logs?key=board-key')
        # GET 只能枚举历史文件,禁止借迁移之名改写/搬走原日志。
        with mock.patch.object(sd.os.path, 'isdir', side_effect=lambda path: path == legacy), \
                mock.patch.object(sd.os, 'listdir', side_effect=lambda path: entries[path]), \
                mock.patch.object(sd.os.path, 'getsize', return_value=123), \
                mock.patch.object(sd, 'open', side_effect=AssertionError('must not open log'), create=True), \
                mock.patch.object(sd.os, 'replace') as replace, \
                mock.patch.object(sd.os, 'unlink') as unlink:
            handler.do_GET()
        status, payload = self.result(handler)
        self.assertEqual(status, 200)
        self.assertEqual(payload['key'], 'board-key')
        self.assertEqual(payload['dir'], 'full-key-0123456789abcdef')
        self.assertEqual({entry['path'] for entry in payload['files']}, {
            'logs/full-key-0123456789abcdef/serial-20260919.log',
            'logs/full-key-0123456789abcdef/ssh-20260919.log',
            'logs/old-prefix/serial-20260918.log',
            'logs/old-prefix/ssh-20260918.log',
        })
        self.assertEqual({entry['name'] for entry in payload['files']}, {
            'serial-20260919.log', 'ssh-20260919.log',
            '旧目录/serial-20260918.log', '旧目录/ssh-20260918.log',
        })
        self.assertTrue(all(entry['size'] == 123 for entry in payload['files']))
        replace.assert_not_called()
        unlink.assert_not_called()

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
        other = mock.Mock(key='other-key', dev='/fake/ttyUSB1')
        self.manager.devices['other-key'] = other
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

                with mock.patch.object(sd, 'usb_path_of', return_value='3-4.2'), \
                        mock.patch.object(sd.os, 'geteuid', return_value=0), \
                        mock.patch.object(sd, 'open', side_effect=open_file, create=True) as open_mock:
                    ok, note = sd.try_rescue('/fake/ttyUSB0', 'rebind')
                self.assertFalse(ok)
                self.assertIn('失败', note)
                self.assertEqual(open_mock.call_count, 3)

    def test_all_successful_root_steps_report_success(self):
        with mock.patch.object(sd, 'usb_path_of', return_value='3-4.2'), \
                mock.patch.object(sd.os, 'geteuid', return_value=0), \
                mock.patch.object(sd, 'open', mock.mock_open(), create=True) as open_mock:
            ok, _ = sd.try_rescue('/fake/ttyUSB0', 'rebind')
        self.assertTrue(ok)
        self.assertEqual(open_mock.call_count, 3)

    def test_invalid_root_path_is_rejected_before_sysfs_access(self):
        with mock.patch.object(sd, 'usb_path_of', return_value='3-4/../../evil'), \
                mock.patch.object(sd, 'open', mock.mock_open(), create=True) as open_mock:
            ok, _ = sd.try_rescue('/fake/ttyUSB0', 'soft')
        self.assertFalse(ok)
        open_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
