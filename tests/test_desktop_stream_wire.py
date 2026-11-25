"""Bad live controllers are cleaned up over real local WebSocket IO, without hardware."""
from collections import deque
from contextlib import contextmanager
from http.client import parse_headers
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from decklab_core.desktop import DesktopBook, DesktopSessions
from decklab_core.desktop_stream import serve
from decklab_core.transactions import transactions_for
from decklab_core.websocket_io import _protocol_modules
import test_desktop_stream as fixtures


class TrackingScreen(fixtures.NativeScreen):
    def __init__(self):
        super().__init__()
        self.held, self.released = set(), []

    def live_input(self, event, timeout=4):
        result = super().live_input(event, timeout)
        if event['type'] == 'key':
            key = (event['keysym'], event['code'])
            self.held.add(key) if event['down'] else self.held.discard(key)
        return result

    def release_all(self, timeout=4):
        result = super().release_all(timeout)
        for keysym, code in sorted(self.held):
            self.released.append({'type': 'key', 'keysym': keysym, 'code': code, 'down': False})
        self.held.clear()
        return result


@contextmanager
def stream_peer():
    screen = TrackingScreen()
    manager = SimpleNamespace(store=mock.Mock(), boards={}, agent_grants=None)
    manager.store.has_conflict.return_value = False
    device = SimpleNamespace(serial='WIRE-SN', connection_id='wire-uart-a', manager=manager,
        stop=threading.Event(), operation_lock=threading.RLock(), file_transfer=SimpleNamespace(active=False),
        present=True, fd=1, cv=threading.Condition())
    manager.desktop = sessions = DesktopSessions(manager, DesktopBook({'boards': {'WIRE-SN': {
        'provider': 'vnc', 'secret_url': 'vnc+plain://private.invalid', 'allow_agent': True,
        'display': 'monitor', 'display_kind': 'virtual', 'scale': 1}}}), provider_factory=lambda _: screen)
    sid = sessions.open(device, 'web', 'wire-client')['session']['id']
    server, client = socket.socketpair()
    server.settimeout(3); client.settimeout(3)
    reader, writer = server.makefile('rb'), server.makefile('wb', buffering=0)
    failures, packets = [], deque()
    def handle():
        try:
            method, target, version = reader.readline().decode().strip().split(' ')
            headers = parse_headers(reader)
            headers['X-DeckLab-Desktop-Origin'] = 'http://desktop.test'
            handler = SimpleNamespace(manager=manager, command=method, path=target,
                request_version=version, headers=headers, connection=server, rfile=reader,
                wfile=writer, close_connection=False)
            serve(handler, device, sid, 'web', 'wire-client')
        except Exception as error:
            failures.append(error)
    worker = threading.Thread(target=handle, daemon=True)
    worker.start()
    protocol, events = _protocol_modules()
    peer = protocol.WSConnection(protocol.ConnectionType.CLIENT)
    client.sendall(peer.send(events.Request(host='desktop.test', target='/api/desktop/stream',
        subprotocols=['decklab-desktop-v1'], extra_headers=[(b'origin', b'http://desktop.test')])))
    def receive(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for index, packet in enumerate(packets):
                if predicate(packet):
                    del packets[index]
                    return packet
            data = client.recv(65536)
            if not data:
                break
            peer.receive_data(data)
            for event in peer.events():
                if isinstance(event, events.TextMessage):
                    packets.append(json.loads(event.data))
        raise AssertionError('expected desktop WebSocket packet not received')
    def send(packet):
        text = json.dumps(packet) if not isinstance(packet, str) else packet
        return peer.send(events.TextMessage(data=text))
    try:
        receive(lambda packet: packet.get('type') == 'ready')
        yield SimpleNamespace(screen=screen, manager=manager, sessions=sessions, sid=sid,
            device=device, client=client, worker=worker, failures=failures, receive=receive, send=send)
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        worker.join(3)
        reader.close(); writer.close(); server.close(); client.close()
        if worker.is_alive():
            raise AssertionError('desktop stream worker did not stop')


class DesktopStreamWireTests(unittest.TestCase):
    def test_bad_json_or_repeated_sequence_releases_held_key_and_fences_buffered_old_input(self):
        key = {'type': 'key', 'keysym': 65507, 'code': 'ControlLeft', 'down': True}
        for failure in ('bad-json', 'duplicate-sequence'):
            with self.subTest(failure=failure), stream_peer() as f:
                f.screen.frame()
                frame = f.receive(lambda packet: packet.get('type') == 'frame')
                observed = {'type': 'observed', 'frame_id': frame['frame_id'], 'sequence': frame['sequence']}
                f.client.sendall(f.send(observed))
                f.receive(lambda packet: packet.get('type') == 'observed')
                first = {'type': 'input', 'seq': 1, 'frame_id': frame['frame_id'], 'event': key}
                f.client.sendall(f.send(first))
                f.receive(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
                self.assertEqual(f.screen.held, {(65507, 'ControlLeft')})
                invalid = '{"type":' if failure == 'bad-json' else first
                # All bytes arrive before cleanup: a client cannot keep a bad
                # controller alive by queuing heartbeat/observation/old inputs.
                f.client.sendall(f.send(invalid) + f.send({'type': 'heartbeat'}) + f.send(observed)
                                 + f.send(dict(first, seq=2)))
                error = f.receive(lambda packet: packet.get('type') == 'error')
                self.assertEqual(error['code'], 'desktop-input-rejected')
                self.assertNotIn('unknown', error['code'])
                f.worker.join(3)
                self.assertFalse(f.worker.is_alive())
                self.assertEqual(f.failures, [])
                self.assertEqual(f.screen.inputs, [key])
                self.assertEqual(f.screen.released, [dict(key, down=False)])
                self.assertEqual(f.screen.held, set())
                self.assertIn('stop_live', f.screen.calls)
                self.assertIsNone(transactions_for(f.device).ownership())
                self.assertFalse(f.screen.closed, 'known rejection must retain the MCP native connection')
                self.assertIn(f.sid, f.sessions.sessions)
                self.assertNotIn('live_channel', f.sessions.sessions[f.sid])


if __name__ == '__main__':
    unittest.main()
