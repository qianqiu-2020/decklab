"""Continuous desktop lifecycle regressions with fake native IO; no network or boards."""
import base64
from email.message import Message
import importlib
import json
import queue
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from seriald_core.agent_grants import GrantRevoked
from seriald_core.desktop import DesktopBook, DesktopSessions
from seriald_core.tasks import TaskError
from seriald_core.transactions import transactions_for
from seriald_core.websocket_io import WSTimeout


def _png(width=20, height=12):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x00\xff\x00' * width) * height))
            + chunk(b'IEND', b''))


class FakeWS:
    def __init__(self):
        self.incoming = queue.Queue()
        self.condition = threading.Condition()
        self.messages, self.binary = [], []
        self.closed, self.close_code = False, None

    def send(self, message):
        if isinstance(message, bytes):
            self.binary.append(message)
            return
        value = json.loads(message) if isinstance(message, str) else message
        with self.condition:
            self.messages.append(value)
            self.condition.notify_all()

    def recv(self, timeout=1):
        if self.closed:
            return None
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise WSTimeout() from None

    def push(self, message):
        self.incoming.put(json.dumps(message) if message is not None else None)

    def close(self, code=1000):
        self.closed, self.close_code = True, code
        self.incoming.put(None)
        with self.condition:
            self.condition.notify_all()

    def wait(self, predicate, timeout=3, after=0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for message in self.messages[after:]:
                    if predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('expected desktop stream message; received ' + repr(self.messages))
                self.condition.wait(remaining)


class NativeScreen:
    def __init__(self):
        self.events = queue.Queue()
        self.condition = threading.Condition()
        self.inputs, self.calls = [], []
        self.held_keys, self.key_releases = set(), []
        self.produced_sequence, self.sequence_floors = 0, []
        self.closed = False
        self.fail_input = self.fail_release = self.fail_stop = False
        self.clipboard_text = 'private remote clipboard'

    def capabilities(self):
        return {'live': True, 'key': True, 'pointer': True,
                'clipboard': {'read': True, 'write': True, 'encoding': 'unicode'}}

    def start_live(self, timeout=4):
        self.called('start_live')
        self.sequence_floors.append(self.produced_sequence)
        return {'ok': True, 'capabilities': self.capabilities(), 'sequence_floor': self.produced_sequence}

    def next_event(self, timeout=1):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def frame(self, sequence=1, generation='display-a', width=20, height=12, reset=True):
        self.queue_frame({'type': 'frame', 'sequence': sequence, 'generation': generation,
            'width': width, 'height': height, 'x': 0, 'y': 0, 'rect_width': width,
            'rect_height': height, 'reset': reset, 'mime': 'image/png',
            'png_base64': base64.b64encode(_png(width, height)).decode(),
            'source_updated_at': time.time()})

    def queue_frame(self, event):
        self.produced_sequence = max(self.produced_sequence, event['sequence'])
        self.events.put(event)

    def live_input(self, event, timeout=4):
        with self.condition:
            self.inputs.append(dict(event))
            if event.get('type') == 'key':
                key = (event['keysym'], event['code'])
                if event['down']:
                    self.held_keys.add(key)
                else:
                    self.held_keys.discard(key)
            self.condition.notify_all()
        if self.fail_input:
            raise TimeoutError('native write receipt lost')
        return {'ok': True}

    def clipboard_read(self, timeout=4):
        self.called('clipboard_read')
        return {'ok': True, 'available': True, 'text': self.clipboard_text}

    def release_all(self, timeout=4):
        self.called('release_all')
        if self.fail_release:
            raise TimeoutError('native key release receipt lost')
        with self.condition:
            self.key_releases.extend({'type': 'key', 'keysym': keysym, 'code': code, 'down': False}
                                     for keysym, code in sorted(self.held_keys))
            self.held_keys.clear()
        return {'ok': True, 'release_confirmed': True}

    def stop_live(self, timeout=4):
        self.called('stop_live')
        if self.fail_stop:
            raise TimeoutError('native stop receipt lost')
        return {'ok': True, 'release_confirmed': True}

    def close(self):
        self.called('close')
        self.closed = True

    def called(self, name):
        with self.condition:
            self.calls.append(name)
            self.condition.notify_all()

    def wait_call(self, name, count=1):
        deadline = time.monotonic() + 3
        with self.condition:
            while self.calls.count(name) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('native ' + name + ' was not called')
                self.condition.wait(remaining)

    def wait_inputs(self, count):
        deadline = time.monotonic() + 3
        with self.condition:
            while len(self.inputs) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('native input was not delivered')
                self.condition.wait(remaining)


class DesktopStreamTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('seriald_core.desktop_stream')
        self.screen, self.ws = NativeScreen(), FakeWS()
        self.manager = SimpleNamespace(store=mock.Mock(), boards={}, agent_grants=None)
        self.manager.store.has_conflict.return_value = False
        self.device = SimpleNamespace(serial='STREAM-SN', connection_id='uart-generation-a',
            manager=self.manager, stop=threading.Event(), operation_lock=threading.RLock(),
            file_transfer=SimpleNamespace(active=False), present=True, fd=1,
            cv=threading.Condition())
        config = {'provider': 'vnc', 'secret_url': 'vnc+plain://private.invalid', 'allow_agent': True,
                  'display': 'monitor-a', 'display_kind': 'virtual', 'scale': 1}
        self.desktop = self.manager.desktop = DesktopSessions(self.manager,
            DesktopBook({'boards': {'STREAM-SN': config}}), provider_factory=lambda _: self.screen)
        self.actor, self.client = 'web', 'browser-a'
        self.sid = self.desktop.open(self.device, self.actor, self.client)['session']['id']
        headers = Message()
        headers['Host'] = 'desktop.test'
        headers['X-Seriald-Desktop-Origin'] = 'http://desktop.test'
        self.handler = SimpleNamespace(manager=self.manager, headers=headers,
            connection=SimpleNamespace(), command='GET', request_version='HTTP/1.1',
            path='/api/desktop/stream', close_connection=False, _json=mock.Mock(), send_error=mock.Mock())
        self.failures, self.thread = [], None
        self.accept = mock.patch.object(self.module.WSConnection, 'accept', return_value=self.ws)
        self.accept_mock = self.accept.start()
        self.addCleanup(self.accept.stop)
        self.addCleanup(self.finish)

    def start(self, actor=None, client=None):
        def serve():
            try:
                self.module.serve(self.handler, self.device, self.sid,
                                  actor or self.actor, self.client if client is None else client)
            except Exception as error:
                self.failures.append(error)
        self.thread = threading.Thread(target=serve, name='fake-desktop-stream', daemon=True)
        self.thread.start()
        selected_actor = actor or self.actor
        selected_client = self.client if client is None else client
        session = self.desktop.sessions[self.sid]
        if (session['actor'] == selected_actor
                and (selected_actor != 'web' or session['client_id'] == selected_client)):
            # Frames produced before start_live's watermark are intentionally
            # stale. Wait for ready so a test's first generated frame belongs
            # to the active epoch rather than racing the server's startup.
            self.ws.wait(lambda packet: packet.get('type') == 'ready')
        return self.ws

    def finish(self):
        if self.thread is not None:
            self.ws.push(None)
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive(), 'desktop stream did not stop')

    def observed_frame(self, sequence=1, generation='display-a', width=20, height=12):
        self.screen.frame(sequence, generation, width, height)
        frame = self.ws.wait(lambda value: value.get('type') == 'frame' and value.get('sequence') == sequence)
        self.ws.push({'type': 'observed', 'frame_id': frame['frame_id'], 'sequence': frame['sequence']})
        return frame

    @staticmethod
    def key():
        return {'type': 'key', 'keysym': 65507, 'code': 'ControlLeft', 'down': True}

    def input(self, frame, seq=1, event=None):
        self.ws.push({'type': 'input', 'seq': seq, 'frame_id': frame['frame_id'],
                      'event': event if event is not None else self.key()})

    def test_wrong_web_client_is_rejected_before_websocket_or_native_start(self):
        self.start(client='browser-b')
        self.finish()
        self.accept_mock.assert_not_called()
        self.assertNotIn('start_live', self.screen.calls)
        self.assertEqual(self.screen.inputs, [])
        self.assertTrue(any(getattr(error, 'status', None) in (403, 404, 409) for error in self.failures)
                        or self.handler._json.called or self.handler.send_error.called,
                        'rejection must be an HTTP/protocol error, not an unrelated crash')

    def test_ready_advertises_nonempty_capabilities_under_public_wire_name(self):
        self.start()
        ready = self.ws.wait(lambda packet: packet.get('type') == 'ready')
        self.assertEqual(ready['capabilities'], self.screen.capabilities())
        self.assertTrue(ready['capabilities']['live'])
        self.assertNotIn('caps', ready)

    def test_static_frame_observation_can_be_renewed_without_new_native_frame(self):
        self.start()
        frame = self.observed_frame()
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True)
        channel = self.desktop.sessions[self.sid]['live_channel']
        with channel.lock:
            channel.observed_at = time.monotonic() - 60
        self.ws.push({'type': 'heartbeat'})
        self.ws.wait(lambda packet: packet.get('type') == 'heartbeat' and packet.get('ok') is True)
        with channel.lock:
            self.assertGreater(time.monotonic() - channel.observed_at, 30,
                               'heartbeat alone must not imply the screen was observed')
        self.assertEqual(self.screen.inputs, [])
        after = len(self.ws.messages)
        self.ws.push({'type': 'observed', 'frame_id': frame['frame_id'], 'sequence': frame['sequence']})
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True, after=after)
        self.input(frame, seq=1)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertEqual(len([packet for packet in self.ws.messages if packet.get('type') == 'frame']), 1)

    def test_delta_sequence_gap_releases_and_requires_fresh_full_reset_observation(self):
        self.start()
        previous = self.observed_frame()
        self.input(previous, seq=1)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.screen.queue_frame({'type': 'frame', 'sequence': 3, 'generation': 'display-a',
            'width': 20, 'height': 12, 'x': 3, 'y': 2, 'rect_width': 5, 'rect_height': 4,
            'reset': False, 'mime': 'image/png',
            'png_base64': base64.b64encode(_png(5, 4)).decode(), 'source_updated_at': time.time()})
        self.ws.wait(lambda packet: packet.get('type') == 'reset' and packet.get('reason') == 'frame-gap')
        self.assertFalse(any(packet.get('type') == 'frame' and packet.get('sequence') == 3
                             for packet in self.ws.messages))
        self.assertIn('release_all', self.screen.calls)
        self.assertGreaterEqual(self.screen.calls.count('start_live'), 2)
        self.assertIsNone(transactions_for(self.device).ownership())
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertTrue(self.desktop.sessions[self.sid]['needs_observation'])
        self.screen.frame(sequence=4)
        current = self.ws.wait(lambda packet: packet.get('type') == 'frame' and packet.get('sequence') == 4)
        self.assertTrue(current['reset'])
        self.assertNotEqual(current['frame_id'], previous['frame_id'])
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertTrue(self.desktop.sessions[self.sid]['needs_observation'])
        after = len(self.ws.messages)
        self.ws.push({'type': 'observed', 'frame_id': current['frame_id'], 'sequence': current['sequence']})
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True, after=after)
        self.input(current, seq=2)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
        self.assertEqual(self.screen.inputs, [self.key(), self.key()])

    def test_clipboard_read_emits_independent_clipboard_packet_before_ack_including_unavailable(self):
        self.screen.clipboard_read = mock.Mock(side_effect=[
            {'ok': True, 'available': True, 'text': 'explicit clipboard text', 'encoding': 'unicode'},
            {'ok': True, 'available': False, 'encoding': 'unicode'}])
        self.start()
        frame = self.observed_frame()
        self.input(frame, seq=1, event={'type': 'clipboard-read'})
        clipboard = self.ws.wait(lambda packet: packet.get('type') == 'clipboard')
        ack = self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.assertEqual(clipboard['text'], 'explicit clipboard text')
        self.assertTrue(clipboard['available'])
        self.assertNotIn('clipboard', ack)
        self.assertLess(self.ws.messages.index(clipboard), self.ws.messages.index(ack))
        after = len(self.ws.messages)
        self.input(frame, seq=2, event={'type': 'clipboard-read'})
        unavailable = self.ws.wait(lambda packet: packet.get('type') == 'clipboard', after=after)
        ack = self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
        self.assertIs(unavailable['available'], False)
        self.assertNotIn('text', unavailable)
        self.assertLess(self.ws.messages.index(unavailable), self.ws.messages.index(ack))
        self.assertEqual(self.screen.clipboard_read.call_count, 2)
        self.assertEqual(self.screen.inputs, [])

    def test_unobserved_frame_cannot_deliver_native_input(self):
        self.start()
        self.screen.frame()
        frame = self.ws.wait(lambda value: value.get('type') == 'frame')
        self.input(frame)
        self.ws.wait(lambda value: value.get('type') == 'error')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'unobserved input rejection left controller active')
        self.assertEqual(self.screen.inputs, [])
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)

    def test_reused_sequence_with_changed_input_cannot_deliver_second_native_action(self):
        self.start()
        frame = self.observed_frame()
        self.input(frame)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.input(frame, event={'type': 'pointer', 'x': 1, 'y': 1, 'buttons': 1})
        self.ws.wait(lambda packet: packet.get('type') == 'error')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)
        self.assertIsNone(transactions_for(self.device).ownership())

    def assert_held_key_rejection_cleanup(self, frame):
        error = self.ws.wait(lambda packet: packet.get('type') == 'error')
        self.assertEqual(error['code'], 'desktop-input-rejected')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'rejected controller retained held keys')
        self.assertTrue(self.ws.closed)
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)
        self.assertEqual(self.screen.key_releases, [dict(self.key(), down=False)])
        self.assertEqual(self.screen.held_keys, set())
        self.assertIsNone(transactions_for(self.device).ownership())
        self.assertFalse(self.screen.closed, 'known rejection must not destroy a clean native session')
        self.assertIn(self.sid, self.desktop.sessions)
        self.assertNotIn('live_channel', self.desktop.sessions[self.sid])
        self.input(frame, seq=99, event={'type': 'pointer', 'x': 1, 'y': 1, 'buttons': 1})
        self.assertEqual(self.screen.inputs, [self.key()], 'old controller delivered input after rejection')

    def assert_empty_known_rejection_cleanup(self):
        error = self.ws.wait(lambda packet: packet.get('type') == 'error')
        self.assertEqual(error['code'], 'desktop-input-rejected')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'invalid client packet retained a controller')
        self.assertEqual(self.screen.inputs, [])
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)
        self.assertIsNone(transactions_for(self.device).ownership())
        self.assertFalse(self.screen.closed, 'known validation rejection was mislabeled as unknown execution')
        self.assertIn(self.sid, self.desktop.sessions)
        self.assertEqual(self.failures, [])

    def test_unhashable_event_type_is_known_rejection_without_native_input(self):
        self.start()
        self.input(self.observed_frame(), event={'type': []})
        self.assert_empty_known_rejection_cleanup()

    def test_zero_keysym_is_known_rejection_before_native_input(self):
        self.start()
        self.input(self.observed_frame(), event=dict(self.key(), keysym=0))
        self.assert_empty_known_rejection_cleanup()

    def test_scalar_input_execution_lease_is_known_rejection_without_native_input(self):
        self.start()
        frame = self.observed_frame()
        self.ws.push({'type': 'input', 'seq': 1, 'frame_id': frame['frame_id'],
                      'execution_lease': 5, 'event': self.key()})
        self.assert_empty_known_rejection_cleanup()

    def test_malformed_bind_execution_lease_is_known_rejection_without_native_input(self):
        self.start()
        self.observed_frame()
        self.ws.push({'type': 'bind', 'execution_lease': {'id': 'short', 'generation': False, 'token': 'short'}})
        self.assert_empty_known_rejection_cleanup()

    def test_old_mode_observed_is_ignored_but_old_mode_input_ends_controller(self):
        self.start()
        previous = self.observed_frame()
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True)
        self.screen.frame(2, 'display-b', width=24, height=12)
        current = self.ws.wait(lambda packet: packet.get('type') == 'frame' and packet.get('sequence') == 2)
        self.assertNotEqual(current['frame_id'], previous['frame_id'])
        channel = self.desktop.sessions[self.sid]['live_channel']
        with channel.lock:
            before = (channel.observed_at, channel.observed_sequence)
        after = len(self.ws.messages)
        self.ws.push({'type': 'observed', 'frame_id': previous['frame_id'], 'sequence': previous['sequence']})
        self.ws.push({'type': 'heartbeat'})
        self.ws.wait(lambda packet: packet.get('type') == 'heartbeat' and packet.get('ok') is True, after=after)
        with channel.lock:
            self.assertEqual((channel.observed_at, channel.observed_sequence), before)
        self.assertFalse(self.ws.closed)
        self.assertFalse(any(packet.get('type') == 'error' for packet in self.ws.messages[after:]))
        self.ws.push({'type': 'observed', 'frame_id': current['frame_id'], 'sequence': current['sequence']})
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('frame_id') == current['frame_id'])
        self.input(current)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.input(previous, seq=2)
        self.assert_held_key_rejection_cleanup(previous)

    def test_text_input_accepts_1024_unicode_characters_without_clipboard_access(self):
        self.start()
        text = '中' * 1024
        self.input(self.observed_frame(), event={'type': 'text', 'text': text})
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.assertEqual(self.screen.inputs, [{'type': 'text', 'text': text}])
        self.assertNotIn('clipboard_read', self.screen.calls)

    def test_nul_text_is_rejected_before_native_input(self):
        self.start()
        self.input(self.observed_frame(), event={'type': 'text', 'text': 'before\x00after'})
        self.assert_empty_known_rejection_cleanup()

    def test_held_key_then_bad_json_releases_native_key_and_ends_controller(self):
        self.start()
        frame = self.observed_frame()
        self.input(frame)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.ws.incoming.put('{invalid-json')
        self.assert_held_key_rejection_cleanup(frame)

    def test_held_key_then_duplicate_sequence_releases_native_key_and_ends_controller(self):
        self.start()
        frame = self.observed_frame()
        self.input(frame)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        self.input(frame)
        self.assert_held_key_rejection_cleanup(frame)

    def test_clipboard_text_is_only_returned_for_explicit_read(self):
        self.start()
        frame = self.observed_frame()
        self.input(frame, event={'type': 'pointer', 'x': 3, 'y': 4, 'buttons': 0})
        self.screen.wait_inputs(1)
        self.assertNotIn('clipboard_read', self.screen.calls)
        self.assertNotIn(self.screen.clipboard_text, json.dumps(self.ws.messages))
        self.input(frame, seq=2, event={'type': 'clipboard-read'})
        self.ws.wait(lambda value: self.screen.clipboard_text in json.dumps(value))
        self.assertEqual(self.screen.calls.count('clipboard_read'), 1)

    def test_lost_input_receipt_destroys_session_without_replaying_input(self):
        self.screen.fail_input = True
        self.start()
        self.input(self.observed_frame())
        self.finish()
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertTrue(self.screen.closed)
        self.assertNotIn(self.sid, self.desktop.sessions)

    def test_unconfirmed_key_release_destroys_session(self):
        self.start()
        frame = self.observed_frame()
        self.input(frame)
        self.screen.wait_inputs(1)
        self.screen.fail_release = True
        self.finish()
        self.assertTrue(self.screen.closed)
        self.assertNotIn(self.sid, self.desktop.sessions)

    def test_explicit_blur_release_all_uses_native_release_without_key_replay(self):
        self.start()
        self.input(self.observed_frame())
        self.screen.wait_inputs(1)
        self.ws.push({'type': 'release_all', 'seq': 2})
        self.screen.wait_call('release_all')
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertFalse(self.ws.closed)

    def test_release_ack_precedes_resync_frame_when_sender_is_already_waiting(self):
        self.start()
        self.observed_frame()
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True)
        channel = self.desktop.sessions[self.sid]['live_channel']
        frame_waiting, frame_sent, resync_queued = threading.Event(), threading.Event(), threading.Event()
        original_frame, original_start = channel._frame, self.screen.start_live
        original_lock = channel.operation_lock
        test = self

        def frame(event, *args, **kwargs):
            if event.get('sequence') == 2:
                frame_waiting.set()
            result = original_frame(event, *args, **kwargs)
            if event.get('sequence') == 2:
                frame_sent.set()
            return result

        def start_live(timeout=4):
            result = original_start(timeout=timeout)
            self.screen.frame(sequence=2)
            resync_queued.set()
            self.assertTrue(frame_waiting.wait(2), 'sender did not receive the queued resync frame')
            return result

        class OrderedLock:
            def __init__(self):
                self.waited = False

            def __enter__(self):
                original_lock.acquire()
                return self

            def __exit__(self, *args):
                original_lock.release()
                if (threading.current_thread() is test.thread and resync_queued.is_set()
                        and not self.waited):
                    self.waited = True
                    # Force the waiting sender to finish immediately after this
                    # critical section. An ACK sent outside it will then lose
                    # the race deterministically, without timing sleeps.
                    test.assertTrue(frame_sent.wait(2), 'resync sender did not finish after release')

        with original_lock:
            channel._frame = frame
            channel.operation_lock = OrderedLock()
            self.screen.start_live = start_live
        after = len(self.ws.messages)
        self.ws.push({'type': 'release_all', 'seq': 2})
        ack = self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2, after=after)
        fresh = self.ws.wait(lambda packet: packet.get('type') == 'frame' and packet.get('sequence') == 2, after=after)
        self.assertTrue(frame_waiting.is_set())
        self.assertTrue(ack['release_confirmed'])
        self.assertLess(self.ws.messages.index(ack), self.ws.messages.index(fresh),
                        'release ACK must precede the complete frame it requested')

    def test_resync_drops_delta_above_sequence_floor_until_complete_reset(self):
        self.start()
        previous = self.observed_frame()
        self.input(previous)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        channel = self.desktop.sessions[self.sid]['live_channel']
        processed = threading.Event()
        original_frame = channel._frame

        def frame(event, *args, **kwargs):
            try:
                return original_frame(event, *args, **kwargs)
            finally:
                if event.get('sequence') == 2:
                    processed.set()

        channel._frame = frame
        self.ws.push({'type': 'release_all', 'seq': 2})
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
        self.assertEqual(self.screen.sequence_floors[-1], 1)
        self.screen.frame(sequence=2, reset=False)
        self.assertTrue(processed.wait(2), 'sender did not inspect the queued delta')
        self.assertFalse(any(packet.get('type') == 'frame' and packet.get('sequence') == 2
                             for packet in self.ws.messages), 'delta escaped while waiting for a complete reset')
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertIsNone(transactions_for(self.device).ownership())
        current = self.observed_frame(sequence=3)
        self.assertNotEqual(current['frame_id'], previous['frame_id'])
        self.input(current, seq=3)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 3)
        self.assertEqual(self.screen.inputs, [self.key(), self.key()])
        self.assertEqual(self.screen.calls.count('start_live'), 2)

    def assert_resync_discards_previously_taken_frame(self, reset):
        self.start()
        previous = self.observed_frame()
        self.input(previous)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        channel = self.desktop.sessions[self.sid]['live_channel']
        waiting, allow_old, processed = threading.Event(), threading.Event(), threading.Event()
        original_lock, original_frame = channel.operation_lock, channel._frame

        class HoldSenderLock:
            def __enter__(self):
                if threading.current_thread() is channel.sender and not allow_old.is_set():
                    waiting.set()
                    if not allow_old.wait(3):
                        raise AssertionError('test did not release the waiting sender')
                original_lock.acquire()
                return self

            def __exit__(self, *args):
                original_lock.release()

        def frame(event, *args, **kwargs):
            try:
                return original_frame(event, *args, **kwargs)
            finally:
                if event.get('sequence') == 2:
                    processed.set()

        with original_lock:
            channel._frame = frame
            channel.operation_lock = HoldSenderLock()
        try:
            self.screen.frame(sequence=2, reset=reset)
            self.assertTrue(waiting.wait(2), 'sender did not take the old native frame')
            after = len(self.ws.messages)
            self.ws.push({'type': 'release_all', 'seq': 2})
            self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2, after=after)
            self.assertEqual(self.screen.sequence_floors[-1], 2)
            allow_old.set()
            self.assertTrue(processed.wait(2), 'sender did not finish the old native frame')
            self.assertFalse(any(packet.get('type') == 'frame' and packet.get('sequence') == 2
                                 for packet in self.ws.messages[after:]), 'pre-resync native frame escaped after ACK')
            self.assertEqual(self.screen.inputs, [self.key()])
            self.assertIsNone(transactions_for(self.device).ownership())
            current = self.observed_frame(sequence=3)
            self.assertNotEqual(current['frame_id'], previous['frame_id'])
            self.input(current, seq=3)
            self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 3)
            self.assertEqual(self.screen.inputs, [self.key(), self.key()])
        finally:
            allow_old.set()

    def test_resync_discards_old_delta_already_taken_by_sender_before_ack(self):
        self.assert_resync_discards_previously_taken_frame(reset=False)

    def test_resync_discards_old_complete_reset_already_taken_by_sender_before_ack(self):
        self.assert_resync_discards_previously_taken_frame(reset=True)

    def test_resync_accepts_unique_fresh_reset_returned_by_inflight_next_event(self):
        self.start()
        previous = self.observed_frame()
        self.ws.wait(lambda packet: packet.get('type') == 'observed' and packet.get('ok') is True)
        waiting, fresh_available = threading.Event(), threading.Event()
        original_next, original_start = self.screen.next_event, self.screen.start_live
        blocked = False

        def next_event(timeout=1):
            nonlocal blocked
            if not blocked:
                blocked = True
                waiting.set()
                if not fresh_available.wait(3):
                    raise AssertionError('resync did not produce the unique fresh reset')
            return original_next(timeout=timeout)

        def start_live(timeout=4):
            result = original_start(timeout=timeout)
            self.screen.frame(sequence=2)
            fresh_available.set()
            return result

        self.screen.next_event, self.screen.start_live = next_event, start_live
        self.screen.events.put(None)  # End any poll that began before the hook.
        try:
            self.assertTrue(waiting.wait(2), 'sender did not begin the cross-resync poll')
            self.ws.push({'type': 'release_all', 'seq': 2})
            self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
            current = self.ws.wait(lambda packet: packet.get('type') == 'frame' and packet.get('sequence') == 2)
            self.assertEqual(self.screen.sequence_floors[-1], 1)
            self.assertNotEqual(current['frame_id'], previous['frame_id'])
            self.assertEqual(self.screen.calls.count('start_live'), 2, 'fresh reset must not trigger another native start')
            self.ws.push({'type': 'observed', 'frame_id': current['frame_id'], 'sequence': current['sequence']})
            self.input(current, seq=3)
            self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 3)
            self.assertEqual(self.screen.inputs, [self.key()])
        finally:
            fresh_available.set()

    def test_negative_release_receipt_ends_stream_and_destroys_session(self):
        self.start()
        self.input(self.observed_frame())
        self.screen.wait_inputs(1)
        self.screen.release_all = mock.Mock(return_value={'ok': True, 'release_confirmed': False})
        self.ws.push({'type': 'release_all', 'seq': 2})
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'unconfirmed key release left the stream usable')
        self.assertTrue(self.screen.closed)
        self.assertNotIn(self.sid, self.desktop.sessions)

    def test_revocation_stops_live_input_and_releases_held_keys(self):
        class Grants:
            lock = threading.RLock()
            revoked = False

            def check_actor(self, actor):
                if self.revoked:
                    raise GrantRevoked('authorization revoked')

        self.actor, self.client = 'shared-agent/' + 'a' * 16, ''
        session = self.desktop.sessions[self.sid]
        session['actor'], session['client_id'] = self.actor, self.client
        self.manager.agent_grants = grants = Grants()
        self.start()
        frame = self.observed_frame()
        self.input(frame)
        self.screen.wait_inputs(1)
        grants.revoked = True
        self.ws.push({'type': 'heartbeat'})
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'revoked stream remained active')
        self.assertEqual(self.screen.inputs, [self.key()])
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)

    def test_unconfirmed_stop_destroys_session(self):
        self.start()
        self.observed_frame()
        self.screen.fail_stop = True
        self.finish()
        self.assertTrue(self.screen.closed)
        self.assertNotIn(self.sid, self.desktop.sessions)

    def test_negative_stop_receipt_destroys_session(self):
        self.start()
        self.observed_frame()
        self.screen.stop_live = mock.Mock(return_value={'ok': True, 'release_confirmed': False})
        self.finish()
        self.assertTrue(self.screen.closed)
        self.assertNotIn(self.sid, self.desktop.sessions)

    def test_lost_reservation_running_receipt_never_delivers_or_retries_input(self):
        self.desktop.sessions[self.sid]['config']['reservation'] = {'test_only': True}
        def activity(config, value, **kwargs):
            if value == 'running':
                raise TaskError('reservation mutation receipt lost', 502)
        with mock.patch.object(self.module, 'check_reservation', return_value={}), \
                mock.patch.object(self.module, 'reservation_activity', side_effect=activity) as mutate:
            try:
                self.start()
                self.input(self.observed_frame())
                self.thread.join(3)
                self.assertFalse(self.thread.is_alive(), 'uncertain reservation update left input enabled')
                self.assertEqual(self.screen.inputs, [])
                self.assertTrue(self.screen.closed)
                self.assertNotIn(self.sid, self.desktop.sessions)
                self.assertEqual([call.args[1] for call in mutate.call_args_list].count('running'), 1)
                self.assertNotIn('idle', [call.args[1] for call in mutate.call_args_list])
            finally:
                self.finish()

    def test_reservation_watch_failure_fences_existing_input_and_releases_keys(self):
        self.desktop.sessions[self.sid]['config']['reservation'] = {'test_only': True}
        lost = threading.Event()
        def check(config, **kwargs):
            if lost.is_set():
                raise TaskError('original reservation is unreachable', 423)
            return {}
        with mock.patch.object(self.module, 'check_reservation', side_effect=check), \
                mock.patch.object(self.module, 'reservation_activity') as mutate:
            try:
                self.start()
                self.input(self.observed_frame())
                self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
                lost.set()
                self.thread.join(3)
                self.assertFalse(self.thread.is_alive(), 'unreachable authority left stream active')
                self.assertEqual(self.screen.inputs, [self.key()])
                self.assertIn('release_all', self.screen.calls)
                self.assertIn('stop_live', self.screen.calls)
                self.assertTrue(self.screen.closed)
                activities = [call.args[1] for call in mutate.call_args_list]
                self.assertEqual(activities.count('running'), 1)
                self.assertNotIn('idle', activities)
            finally:
                self.finish()

    def test_blur_then_observe_reclaims_own_running_reservation_without_reissuing_mutation(self):
        self.desktop.sessions[self.sid]['config']['reservation'] = {'test_only': True}
        platform = {'activity': 'idle'}
        def check(config, for_input=False, expected_activity=None, **kwargs):
            if for_input and platform['activity'] != 'idle':
                raise TaskError('original reservation is not idle', 423)
            if expected_activity is not None:
                allowed = (expected_activity,) if isinstance(expected_activity, str) else expected_activity
                if platform['activity'] not in allowed:
                    raise TaskError('original reservation activity changed', 423)
            return {}
        def activity(config, value, **kwargs):
            platform['activity'] = value
        with mock.patch.object(self.module, 'check_reservation', side_effect=check), \
                mock.patch.object(self.module, 'reservation_activity', side_effect=activity) as mutate:
            try:
                self.start()
                self.input(self.observed_frame())
                self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
                self.ws.push({'type': 'release_all', 'seq': 2})
                self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
                self.input(self.observed_frame(sequence=2), seq=3)
                self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 3)
                self.assertEqual(self.screen.inputs, [self.key(), self.key()])
                self.assertEqual([call.args[1] for call in mutate.call_args_list].count('running'), 1)
            finally:
                self.finish()

    def test_uart_replacement_releases_old_owner_and_reclaims_after_fresh_observation(self):
        board = SimpleNamespace(connection=self.device, history=None)
        self.manager.boards[self.device.serial] = board
        self.start()
        previous = self.observed_frame()
        self.input(previous)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 1)
        replacement = SimpleNamespace(serial=self.device.serial, connection_id='uart-generation-b',
            manager=self.manager, stop=threading.Event(), operation_lock=threading.RLock(),
            file_transfer=SimpleNamespace(active=False), present=True, fd=1, cv=threading.Condition())
        board.connection = replacement
        self.ws.wait(lambda packet: packet.get('type') == 'reset' and packet.get('reason') == 'attachment-changed')
        self.screen.frame(sequence=2)
        current = self.ws.wait(lambda packet: packet.get('type') == 'frame' and packet.get('sequence') == 2)
        self.assertNotEqual(current['frame_id'], previous['frame_id'])
        self.assertIsNone(transactions_for(self.device).ownership())
        self.assertEqual(self.screen.inputs, [self.key()])
        self.ws.push({'type': 'observed', 'frame_id': current['frame_id'], 'sequence': current['sequence']})
        self.input(current, seq=2)
        self.ws.wait(lambda packet: packet.get('type') == 'ack' and packet.get('seq') == 2)
        self.assertEqual(self.screen.inputs, [self.key(), self.key()])
        self.assertEqual(transactions_for(replacement).ownership()['client_id'], self.client)

    def test_mode_change_invalidates_previously_observed_frame(self):
        self.start()
        previous = self.observed_frame()
        self.screen.frame(2, 'display-b', width=24, height=12)
        current = self.ws.wait(lambda value: value.get('type') == 'frame' and value.get('sequence') == 2)
        self.assertNotEqual(current['frame_id'], previous['frame_id'])
        self.input(previous)
        self.ws.wait(lambda value: value.get('type') == 'error')
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'old frame nonce rejection left controller active')
        self.assertEqual(self.screen.inputs, [])
        self.assertIn('release_all', self.screen.calls)
        self.assertIn('stop_live', self.screen.calls)


class NativeLiveTextValidationTests(unittest.TestCase):
    def test_1024_unicode_characters_are_accepted_at_native_boundary(self):
        from seriald_core.desktop_providers import validate_live_input
        validate_live_input({'type': 'text', 'text': '中' * 1024})

    def test_invalid_text_is_rejected_at_native_boundary(self):
        from seriald_core.desktop_providers import validate_live_input
        for value in ('x' * 1025, 'before\x00after', '\ud800', '\udfff', None, ['text']):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_live_input({'type': 'text', 'text': value})


if __name__ == '__main__':
    unittest.main()
