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
from seriald_core.sessions_windows import SshSession
from seriald_core.terminal_backend import node_binary


class SseTests(unittest.TestCase):
    @unittest.skipUnless(node_binary(), 'requires the packaged Node terminal runtime')
    def test_shared_ssh_snapshot_and_quiet_takeover_are_forwarded_without_replaying_bytes(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(device_module, 'LOG_ROOT', root):
            manager = devices.Manager()
            board = device_module.Device('fake-port', 'COM7', manager)
            board.fd, board.present = object(), True
            manager.devices[board.by_path] = board
            session = SshSession('fixture-ssh', '192.0.2.1', 'fixture', root)
            session.fd = object()
            board.ssh_sessions[session.sid] = session
            with mock.patch.object(session, '_send'), mock.patch.object(session, '_dispose_transport'), \
                    mock.patch.object(session, '_interrupt_transport'), \
                    mock.patch.object(web, 'MGR', manager), \
                    BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                    BoundedThreadingTCPServer(('127.0.0.1', 0), control_server.ControlHandler) as control, \
                    mock.patch.object(control_server.ControlHandler, 'auth_token', ''), \
                    mock.patch.object(control_server.ControlHandler, 'collector_socket', collector.server_address):
                session.terminal.control('web', 'first')
                session._append(b'\x1b[?1049h\x1b[5;8Hvim')
                workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (collector, control)]
                for worker in workers:
                    worker.start()
                conn = http.client.HTTPConnection('127.0.0.1', control.server_address[1], timeout=3)
                try:
                    conn.request('GET', '/api/session-events?key=%s&sid=%s&screen=1&client_id=first' % (
                        quote(board.key), session.sid))
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    initial = self._event(response, include_empty=True)
                    self.assertEqual(initial['terminal_snapshot']['cursor']['buffer'], 'alternate')
                    self.assertTrue(initial['terminal_writable'])
                    started = time.monotonic()
                    session.takeover('web', 'second')
                    changed = self._event(response, include_empty=True)
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(changed['off'], initial['off'])
                    self.assertEqual(changed['data_b64'], '')
                    self.assertNotIn('terminal_snapshot', changed)
                    self.assertFalse(changed['terminal_writable'])
                    self.assertEqual(changed['terminal_state']['controller']['client_id'], 'second')
                finally:
                    conn.close()
                    session.close()
                    for server in (collector, control):
                        server.shutdown()
                    for worker in workers:
                        worker.join(3)

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
                    initial = self._event(response, include_empty=True)
                    self.assertIsNone(initial['input_owner'])
                    self.assertEqual(initial['data_b64'], '')
                    started = time.monotonic()
                    board.append(b'hello')
                    first = self._event(response)
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertEqual(base64.b64decode(first['data_b64']), b'hello')
                    self.assertEqual(first['off'], 5)
                    started = time.monotonic()
                    with board.transactions.actor_scope('web', 'peer-client'):
                        owner = board.transactions.claim('web')
                    reserved = self._event(response, include_empty=True)
                    self.assertEqual(reserved['off'], 5)
                    self.assertEqual(reserved['data_b64'], '')
                    self.assertEqual(reserved['input_owner'], owner)
                    self.assertLess(time.monotonic() - started, 2)
                    with board.transactions.actor_scope('web', 'peer-client'):
                        board.transactions.release(owner['id'], 'web')
                    released = self._event(response, include_empty=True)
                    self.assertIsNone(released['input_owner'])
                    self.assertEqual(released['off'], 5)
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
    def _event(response, include_empty=False):
        for _ in range(30):
            line = response.readline()
            if line.startswith(b'data: '):
                payload = json.loads(line[6:])
                if include_empty or payload.get('data_b64') or not payload.get('alive', True):
                    return payload
        raise AssertionError('SSE data event was not forwarded')


if __name__ == '__main__':
    unittest.main()
