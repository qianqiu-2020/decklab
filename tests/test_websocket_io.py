"""Real local WebSocket framing/HTTP tests; no desktop, devices or external sockets."""
from contextlib import contextmanager
import hashlib
from http.client import parse_headers
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

from seriald_core import websocket_io as module


@contextmanager
def connection(*, headers=None, prefetched=None, tls=False, **limits):
    server, client = socket.socketpair()
    if tls:
        root = Path(__file__).parent / 'fixtures' / 'backend-tls'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / 'certificate.pem', root / 'public-test-key.pem')
        peer_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        peer_context.check_hostname = False
        peer_context.verify_mode = ssl.CERT_NONE  # Isolated public test fixture only.
        pending = []
        def wrap_server():
            pending.append(context.wrap_socket(server, server_side=True))
        worker = threading.Thread(target=wrap_server)
        worker.start()
        client = peer_context.wrap_socket(client, server_hostname='localhost')
        worker.join(2)
        server = pending[0]
    server.settimeout(2); client.settimeout(2)
    reader, writer = server.makefile('rb'), server.makefile('wb', buffering=0)
    protocol, events = module._protocol_modules()
    peer = protocol.WSConnection(protocol.ConnectionType.CLIENT)
    request = peer.send(events.Request(host='desktop.test', target='/desktop/stream',
        subprotocols=['decklab-desktop-v1'], extra_headers=[(b'origin', b'http://desktop.test')]))
    if headers:
        for name, value in headers.items():
            lines = request.split(b'\r\n')
            lines = [line for line in lines if not line.lower().startswith(name.lower().encode() + b':')]
            if value is not None:
                lines.insert(-2, name.encode() + b': ' + value.encode())
            request = b'\r\n'.join(lines)
    trailing = b''
    if prefetched is not None:
        from wsproto.connection import Connection
        encoder = Connection(protocol.ConnectionType.CLIENT)
        trailing = encoder.send(events.TextMessage(data=prefetched))
    client.sendall(request + trailing)
    line = reader.readline().decode().strip().split(' ')
    handler = types.SimpleNamespace(command=line[0], path=line[1], request_version=line[2],
        headers=parse_headers(reader), connection=server, rfile=reader, wfile=writer, close_connection=False)
    accepted = None
    try:
        accepted = module.WSConnection.accept(handler, origins={'http://desktop.test'},
            subprotocol='decklab-desktop-v1', **limits)
        data = client.recv(65536)
        peer.receive_data(data)
        opened = list(peer.events())
        if not any(isinstance(value, events.AcceptConnection) for value in opened):
            raise AssertionError('server did not send a complete HTTP 101')
        yield accepted, client, peer, events, handler
    finally:
        if accepted is not None:
            accepted.close()
        reader.close(); writer.close(); server.close(); client.close()


def peer_events(client, peer):
    peer.receive_data(client.recv(1024 * 1024))
    return list(peer.events())


class WebSocketIOTests(unittest.TestCase):
    def test_offline_wheels_are_exact_upstream_inputs_with_notices(self):
        directory = Path(__file__).resolve().parents[1] / 'vendor' / 'python'
        manifest = json.loads((directory / 'websocket-sources.json').read_text())
        for row in manifest['packages']:
            self.assertEqual(hashlib.sha256((directory / row['filename']).read_bytes()).hexdigest(), row['sha256'])
            self.assertIn('Permission is hereby granted', (directory / row['license_file']).read_text())
        result = subprocess.run([sys.executable, '-S', '-c',
            'from seriald_core.websocket_io import _protocol_modules; '
            'p,e=_protocol_modules(); import h11; '
            'assert p.__version__=="1.3.2" and h11.__version__=="0.16.0"; '
            'assert ".whl/" in p.__file__.replace(chr(92),"/")'],
            cwd=directory.parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http101_subprotocol_and_buffered_first_frame(self):
        with connection(prefetched='frame buffered with HTTP headers') as (ws, client, peer, events, handler):
            self.assertTrue(handler.close_connection)
            self.assertEqual(ws.recv(), 'frame buffered with HTTP headers')
            ws.send('response')
            self.assertEqual([value.data for value in peer_events(client, peer)
                              if isinstance(value, events.TextMessage)], ['response'])

    def test_tls_buffered_first_frame_and_duplex_io(self):
        with connection(prefetched='TLS buffered frame', tls=True) as (ws, client, peer, events, _):
            self.assertEqual(ws.recv(), 'TLS buffered frame')
            client.sendall(peer.send(events.TextMessage(data='TLS second frame')))
            self.assertEqual(ws.recv(), 'TLS second frame')
            ws.send(b'TLS binary response')
            self.assertEqual([bytes(value.data) for value in peer_events(client, peer)
                              if isinstance(value, events.BytesMessage)], [b'TLS binary response'])

    def test_handshake_rejects_wrong_origin_host_key_version_and_subprotocol_without_101(self):
        cases = [({'Origin': 'http://foreign.test'}, 403), ({'Host': 'foreign.test'}, 403),
                 ({'Origin': None}, 400), ({'Sec-WebSocket-Key': 'private-secret'}, 400),
                 ({'Sec-WebSocket-Version': '12'}, 400), ({'Sec-WebSocket-Protocol': 'other'}, 400),
                 ({'Transfer-Encoding': 'chunked'}, 400), ({'Content-Length': '1'}, 400)]
        for headers, status in cases:
            with self.subTest(headers=headers), self.assertRaises(module.WSHandshakeError) as failure:
                with connection(headers=headers):
                    self.fail('invalid request upgraded')
            self.assertEqual(failure.exception.status, status)
            self.assertNotIn('private-secret', str(failure.exception))

    def test_fragmented_text_and_ping_are_reassembled_once(self):
        with connection() as (ws, client, peer, events, _):
            first = peer.send(events.TextMessage(data='你', message_finished=False))
            client.sendall(first[:3])
            with self.assertRaises(module.WSTimeout):
                ws.recv(.01)
            client.sendall(first[3:] + peer.send(events.Ping(payload=b'heartbeat'))
                           + peer.send(events.TextMessage(data='好', message_finished=True)))
            self.assertEqual(ws.recv(), '你好')
            replies = peer_events(client, peer)
            self.assertTrue(any(isinstance(value, events.Pong) and value.payload == b'heartbeat' for value in replies))
            with self.assertRaises(module.WSTimeout):
                ws.recv(.01)

    def test_unmasked_and_invalid_utf8_messages_are_rejected_by_wsproto(self):
        protocol, events = module._protocol_modules()
        from wsproto.connection import Connection
        wire = Connection(protocol.ConnectionType.SERVER).send(events.TextMessage(data='unmasked'))
        for data, code in ((wire, 1002), (b'\x81\x81\x00\x00\x00\x00\xff', 1007)):
            with self.subTest(code=code), connection() as (ws, client, peer, events, _):
                client.sendall(data)
                with self.assertRaises(module.WSError) as failure:
                    ws.recv()
                self.assertEqual(failure.exception.code, code)
                self.assertTrue(ws.closed)

    def test_fragmented_oversize_and_empty_fragment_flood_are_bounded(self):
        with connection(max_message=8) as (ws, client, peer, events, _):
            client.sendall(peer.send(events.BytesMessage(data=b'12345', message_finished=False))
                           + peer.send(events.BytesMessage(data=b'6789', message_finished=True)))
            with self.assertRaises(module.WSError) as failure:
                ws.recv()
            self.assertEqual(failure.exception.code, 1009)
        with connection() as (ws, client, peer, events, _):
            client.sendall(b''.join(peer.send(events.TextMessage(data='', message_finished=False)) for _ in range(4097)))
            with self.assertRaises(module.WSError) as failure:
                ws.recv()
            self.assertEqual(failure.exception.code, 1009)
            self.assertEqual(ws._parts, [])

    def test_outbound_binary_frame_limit_is_separate_from_input_limit(self):
        with connection(max_message=8, max_send=4096) as (ws, client, peer, events, _):
            ws.send(b'x' * 2048)
            values = peer_events(client, peer)
            self.assertEqual(sum(len(value.data) for value in values if isinstance(value, events.BytesMessage)), 2048)
            with self.assertRaises(ValueError):
                ws.send(b'x' * 4097)
            client.sendall(peer.send(events.TextMessage(data='input')))
            self.assertEqual(ws.recv(), 'input')

    def test_peer_close_reply_and_abrupt_eof_do_not_replay_pending_input(self):
        with connection() as (ws, client, peer, events, _):
            client.sendall(peer.send(events.TextMessage(data='queued')) + peer.send(events.CloseConnection(code=1000)))
            self.assertIsNone(ws.recv())
            self.assertEqual(ws.close_code, 1000)
            self.assertTrue(any(isinstance(value, events.CloseConnection) for value in peer_events(client, peer)))
        with connection() as (ws, client, peer, events, _):
            client.shutdown(socket.SHUT_WR)
            self.assertIsNone(ws.recv())
            self.assertEqual(ws.close_code, 1006)

    def test_poll_timeout_preserves_connection_and_total_idle_message_deadlines_close_it(self):
        with connection() as (ws, client, peer, events, _):
            with self.assertRaises(module.WSTimeout):
                ws.recv(.01)
            self.assertFalse(ws.closed)
        for option in ({'idle_timeout': .02}, {'lifetime': .02}):
            with self.subTest(option=option), connection(**option) as (ws, client, peer, events, _):
                with self.assertRaises(module.WSClosed):
                    ws.recv(.2)
                self.assertEqual(ws.close_code, 1001)
        with connection(message_timeout=.02) as (ws, client, peer, events, _):
            client.sendall(peer.send(events.TextMessage(data='partial', message_finished=False)))
            with self.assertRaises(module.WSError) as failure:
                ws.recv(.2)
            self.assertEqual(failure.exception.code, 1008)

    def test_select_returning_early_keeps_the_original_deadline(self):
        with connection() as (ws, client, peer, events, _):
            with mock.patch.object(module.time, 'monotonic', side_effect=[99, 99.99, 100]), \
                    mock.patch.object(module.select, 'select', return_value=([], [], [])) as wait:
                self.assertFalse(ws._wait(True, 100))
                self.assertEqual(wait.call_count, 2)
                self.assertEqual(wait.call_args_list[0].args[3], 1)
                self.assertAlmostEqual(wait.call_args_list[1].args[3], .01)
            self.assertFalse(ws.closed)

    def test_backpressured_socket_write_has_bounded_timeout_and_no_retry(self):
        with connection(max_send=16 * 1024 ** 2, io_timeout=.03) as (ws, client, peer, events, _):
            ws.stream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            # Windows loopback can accept one large send beyond SO_SNDBUF.
            # Establish actual backpressure before testing the write deadline.
            filled, fill_started = 0, time.monotonic()
            while filled < 16 * 1024 ** 2 and time.monotonic() - fill_started < 1:
                try:
                    filled += ws.stream.send(b'f' * 65536)
                except BlockingIOError:
                    break
            else:
                self.fail('socket fixture did not reach WouldBlock within its bounds')
            before = time.monotonic()
            with mock.patch.object(ws, '_write', wraps=ws._write) as write:
                with self.assertRaises(module.WSClosed):
                    ws.send(b'x' * (8 * 1024 ** 2))
                self.assertLess(time.monotonic() - before, 1)
                self.assertTrue(ws.closed)
                with self.assertRaises(module.WSClosed):
                    ws.send(b'not retried')
                self.assertEqual(write.call_count, 1, 'closed transport must not replay a write')

    def test_concurrent_senders_produce_complete_ordered_frames(self):
        with connection() as (ws, client, peer, events, _):
            failures = []
            def send(prefix):
                try:
                    for index in range(20):
                        ws.send(prefix + str(index))
                except Exception as error:
                    failures.append(error)
            workers = [threading.Thread(target=send, args=(prefix,)) for prefix in ('a', 'b')]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            received = []
            while len(received) < 40:
                received.extend(value.data for value in peer_events(client, peer) if isinstance(value, events.TextMessage))
            for prefix in ('a', 'b'):
                self.assertEqual([value for value in received if value.startswith(prefix)],
                                 [prefix + str(index) for index in range(20)])


if __name__ == '__main__':
    unittest.main()
