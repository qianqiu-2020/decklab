"""Real local WebSocket tunnels through control; never opens a desktop/device."""
import base64
from contextlib import contextmanager
import http.server
import json
import socket
import ssl
from pathlib import Path
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from seriald_core.agent_grants import AgentGrantStore
from seriald_core.backend_link import BackendLink, server_context
from seriald_core.control_server import ControlHandler
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.websocket_io import WSConnection, WSError, WSTimeout, _protocol_modules


PROTOCOL = 'decklab-desktop-v1'
OWNER = 'synthetic-desktop-owner'


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.received.append({'path': self.path, 'headers': dict(self.headers)})
        ws = None
        try:
            ws = WSConnection.accept(self, origins={self.headers['X-Seriald-Desktop-Origin']}, subprotocol=PROTOCOL)
            ws.send('ready')
            while True:
                try:
                    data = ws.recv(timeout=.5)
                except WSTimeout:
                    continue
                if data is None:
                    break
                ws.send(data)
        except (WSError, OSError):
            pass
        finally:
            if ws:
                ws.close()
            self.server.closed.set()

    def log_message(self, *_):
        pass


@contextmanager
def tunnel(tls=False):
    class Control(ControlHandler):
        auth_token, agent_tokens, collector_link = OWNER, {}, None
    class Upstream(BoundedThreadingTCPServer):
        def finish_request(self, request, address):
            if tls:
                with self.context.wrap_socket(request, server_side=True) as protected:
                    super().finish_request(protected, address)
            else:
                super().finish_request(request, address)
    grants = AgentGrantStore(':memory:')
    Control.grant_store = grants
    with Upstream(('127.0.0.1', 0), Echo) as backend, BoundedThreadingTCPServer(('127.0.0.1', 0), Control) as control:
        backend.received, backend.closed = [], threading.Event()
        Control.collector_socket = backend.server_address
        if tls:
            directory = Path(__file__).parent / 'fixtures/backend-tls'
            backend.context = server_context(str(directory / 'certificate.pem'), str(directory / 'public-test-key.pem'))
            Control.collector_link = BackendLink('https://127.0.0.1:%d' % backend.server_address[1],
                                                 'private-node-' + 'x' * 40, str(directory / 'certificate.pem'))
        workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, control)]
        for worker in workers:
            worker.start()
        try:
            yield control, backend, grants
        finally:
            for server in (control, backend):
                server.shutdown()
            for worker in workers:
                worker.join(3)
            grants.close()


class Peer:
    def __init__(self, server, authorization=None, origin=None, cookie=None, query=None, early=None):
        module, self.events = _protocol_modules()
        self.peer = module.WSConnection(module.ConnectionType.CLIENT)
        self.socket = socket.create_connection(server.server_address, timeout=3)
        self.socket.settimeout(3)
        self.host = '%s:%d' % server.server_address
        self.origin = origin or 'http://' + self.host
        headers = [(b'origin', self.origin.encode())]
        if authorization:
            headers.append((b'authorization', authorization.encode()))
        if cookie:
            headers.append((b'cookie', cookie.encode()))
        target = '/api/desktop/stream?' + urlencode(query or {'key': 'board', 'serial': 'BOARD-A', 'sid': 'desktop-test', 'client_id': 'page'})
        request = self.peer.send(self.events.Request(host=self.host, target=target, subprotocols=[PROTOCOL], extra_headers=headers))
        if early is not None:
            from wsproto.connection import Connection
            request += Connection(module.ConnectionType.CLIENT).send(self.events.TextMessage(data=early))
        self.pending = []
        self.socket.sendall(request)

    def receive(self, kind):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for index, value in enumerate(self.pending):
                if isinstance(value, kind):
                    return self.pending.pop(index)
            self.peer.receive_data(self.socket.recv(65536))
            self.pending.extend(self.peer.events())
        raise AssertionError('expected WebSocket event not received')

    def send(self, text):
        self.socket.sendall(self.peer.send(self.events.TextMessage(data=text)))

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class DesktopProxyTests(unittest.TestCase):
    @mock.patch('seriald_core.control_server.public_mcp_host_allowed', return_value=False)
    def test_secondary_interface_matches_socket_and_browser_origin(self, _allowed):
        from seriald_core.desktop_proxy import public_origin
        handler = mock.Mock()
        handler.connection.getsockname.return_value = ('192.0.2.18', 8898)
        host = '192.0.2.18:8898'
        handler.headers = {'Host': host, 'Origin': 'http://' + host}
        self.assertEqual(public_origin(handler), 'http://' + host)
        for target, origin in [('192.0.2.19:8898', '192.0.2.19:8898'),
                               ('192.0.2.18:8897', '192.0.2.18:8897'),
                               ('desktop.example.test:8898', 'desktop.example.test:8898'),
                               (host, '192.0.2.19:8898')]:
            with self.subTest(target=target, origin=origin):
                handler.headers = {'Host': target, 'Origin': 'http://' + origin,
                                   'X-Forwarded-Host': host}
                with self.assertRaises(ValueError):
                    public_origin(handler)

    def owner(self):
        return 'Basic ' + base64.b64encode(('seriald:' + OWNER).encode()).decode()

    def test_basic_and_share_cookie_auth_with_prefetched_frames_in_both_directions(self):
        with tunnel() as (control, backend, grants):
            peer = Peer(control, authorization=self.owner(), early='prefetched input')
            try:
                self.assertEqual(peer.receive(peer.events.AcceptConnection).subprotocol, PROTOCOL)
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'ready')
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'prefetched input')
                peer.send('normal input')
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'normal input')
                self.assertNotIn('Authorization', backend.received[0]['headers'])
                self.assertNotIn('Cookie', backend.received[0]['headers'])
            finally:
                peer.close()
            self.assertTrue(backend.closed.wait(2))
            dummy = ControlHandler.__new__(ControlHandler)
            dummy.auth_token = OWNER
            share = dummy._new_share()
            cookie_peer = Peer(control, cookie='seriald_share=' + share)
            try:
                cookie_peer.receive(cookie_peer.events.AcceptConnection)
                self.assertEqual(cookie_peer.receive(cookie_peer.events.TextMessage).data, 'ready')
            finally:
                cookie_peer.close()

    def test_authenticated_tls_link_keeps_node_and_browser_credentials_separate(self):
        with tunnel(tls=True) as (control, backend, grants):
            peer = Peer(control, authorization=self.owner())
            try:
                peer.receive(peer.events.AcceptConnection)
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'ready')
                peer.send('TLS input')
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'TLS input')
                self.assertEqual(backend.received[0]['headers']['Authorization'], 'Bearer private-node-' + 'x' * 40)
            finally:
                peer.close()

    def test_grant_serial_is_pinned_and_revocation_closes_the_tunnel(self):
        with tunnel() as (control, backend, grants):
            token, grant = grants.issue('device', 'BOARD-A')
            peer = Peer(control, authorization='Bearer ' + token,
                        query={'key': 'forged', 'serial': 'BOARD-B', 'sid': 'desktop-test', 'client_id': 'x'})
            try:
                peer.receive(peer.events.AcceptConnection)
                self.assertEqual(peer.receive(peer.events.TextMessage).data, 'ready')
                self.assertIn('serial=BOARD-A', backend.received[0]['path'])
                self.assertNotIn('BOARD-B', backend.received[0]['path'])
                self.assertEqual(backend.received[0]['headers']['X-Seriald-Actor'], grant['actor'])
                grants.revoke(grant['id'])
                self.assertTrue(backend.closed.wait(3))
            finally:
                peer.close()

    def test_bad_origin_missing_auth_or_connection_only_grant_never_reaches_backend(self):
        with tunnel() as (control, backend, grants):
            token, _ = grants.issue('connection', key='board', connection_id='connection-one')
            for kwargs, status in [({'authorization': self.owner(), 'origin': 'https://other.invalid'}, 403),
                    ({}, 401), ({'authorization': 'Bearer ' + token}, 403)]:
                peer = Peer(control, **kwargs)
                try:
                    self.assertEqual(peer.receive(peer.events.RejectConnection).status_code, status)
                finally:
                    peer.close()
            self.assertEqual(backend.received, [])

    def test_partial_thread_start_failure_closes_both_sides_and_returns_capacity(self):
        original = threading.Thread.start
        def start(thread):
            if thread.name == 'desktop-tunnel-down':
                raise RuntimeError('simulated worker exhaustion')
            return original(thread)
        with tunnel() as (control, backend, grants), mock.patch.object(threading.Thread, 'start', start):
            peer = Peer(control, authorization=self.owner())
            try:
                peer.receive(peer.events.AcceptConnection)
                self.assertTrue(backend.closed.wait(3))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    reserved = []
                    for _ in range(4):
                        if control.desktop_stream_slots.acquire(blocking=False):
                            reserved.append(True)
                    for _ in reserved:
                        control.desktop_stream_slots.release()
                    if len(reserved) == 4:
                        break
                    time.sleep(.01)
                self.assertEqual(len(reserved), 4)
            finally:
                peer.close()


if __name__ == '__main__':
    unittest.main()
