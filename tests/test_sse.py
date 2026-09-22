"""SSE output must traverse the control proxy without 64 KiB buffering."""
import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from seriald_core import control_server, device as device_module, devices, web
from seriald_core.httpd import BoundedThreadingTCPServer


class SseTests(unittest.TestCase):
    def test_live_output_and_last_event_id_resume_through_proxy(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(device_module, 'LOG_ROOT', root):
            manager = devices.Manager()
            board = device_module.Device('fake-port', 'COM7', manager)
            board.fd, board.present = object(), True
            manager.devices[board.by_path] = board
            with mock.patch.object(web, 'MGR', manager), \
                    BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                    BoundedThreadingTCPServer(('127.0.0.1', 0), control_server.ControlHandler) as control, \
                    mock.patch.object(control_server.ControlHandler, 'auth_token', ''), \
                    mock.patch.object(control_server.ControlHandler, 'collector_socket',
                                      collector.server_address):
                workers = [threading.Thread(target=server.serve_forever, daemon=True)
                           for server in (collector, control)]
                for worker in workers:
                    worker.start()
                try:
                    route = '/api/session-events?key=%s&sid=serial&off=0' % quote(board.key)
                    conn = http.client.HTTPConnection('127.0.0.1', control.server_address[1], timeout=3)
                    conn.request('GET', route)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.getheader('Content-Type').startswith('text/event-stream'))
                    started = time.monotonic()
                    board.append(b'hello')
                    first = self._event(response)
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertEqual(base64.b64decode(first['data_b64']), b'hello')
                    self.assertEqual(first['off'], 5)
                    conn.close()

                    board.append(b'world')
                    resumed = http.client.HTTPConnection('127.0.0.1', control.server_address[1], timeout=3)
                    resumed.request('GET', route, headers={'Last-Event-ID': '5'})
                    response = resumed.getresponse()
                    second = self._event(response)
                    self.assertEqual(base64.b64decode(second['data_b64']), b'world')
                    self.assertEqual(second['off'], 10)
                    resumed.close()
                finally:
                    board.fd = None
                    with board.cv:
                        board.cv.notify_all()
                    for server in (collector, control):
                        server.shutdown()
                    for worker in workers:
                        worker.join(timeout=3)
                    board.close_log()

    @staticmethod
    def _event(response):
        for _ in range(10):
            line = response.readline()
            if line.startswith(b'data: '):
                return json.loads(line[6:])
        raise AssertionError('SSE data event was not forwarded')


if __name__ == '__main__':
    unittest.main()
