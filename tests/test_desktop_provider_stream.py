"""Native desktop stream boundaries; generated images and an isolated fake worker."""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import threading
import time
import unittest
from unittest import mock

from decklab_core import desktop_providers as providers


REAL_POPEN = subprocess.Popen


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'optional desktop image dependency not installed')
class LiveFramesTests(unittest.TestCase):
    def frames(self, capacity=16):
        frames = providers.LiveFrames(capacity=capacity)

        def close():
            frames.close()
            frames.thread.join(timeout=1)
            self.assertFalse(frames.thread.is_alive())

        self.addCleanup(close)
        return frames

    def image(self, event):
        from PIL import Image
        image = Image.open(io.BytesIO(base64.b64decode(event['png_base64'])))
        image.load()
        return image

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate(), 'frame encoder did not reach the expected state')

    def test_native_updates_emit_first_reset_then_dirty_rectangle_and_idle_stays_quiet(self):
        from PIL import Image
        frames = self.frames()
        frames.start()
        self.assertIsNone(frames.next_event(timeout=.02))
        image = Image.new('RGB', (8, 6), (10, 20, 30))
        frames.update(image)
        first = frames.next_event(timeout=2)
        self.assertEqual(first['type'], 'frame')
        self.assertTrue(first['reset'])
        self.assertEqual((first['x'], first['y'], first['rect_width'], first['rect_height']), (0, 0, 8, 6))
        self.assertEqual(self.image(first).getpixel((7, 5)), (10, 20, 30))

        changed = image.copy()
        changed.paste((200, 30, 40), (2, 1, 5, 3))
        frames.update(changed, [(2, 1, 3, 2)])
        dirty = frames.next_event(timeout=2)
        self.assertFalse(dirty['reset'])
        self.assertEqual(dirty['generation'], first['generation'])
        self.assertGreater(dirty['sequence'], first['sequence'])
        self.assertEqual((dirty['width'], dirty['height']), (8, 6))
        self.assertEqual((dirty['x'], dirty['y'], dirty['rect_width'], dirty['rect_height']), (2, 1, 3, 2))
        patch = self.image(dirty)
        self.assertEqual(patch.size, (3, 2))
        self.assertEqual(patch.getpixel((2, 1)), (200, 30, 40))
        self.assertIsNone(frames.next_event(timeout=.06), 'a static desktop must not emit repeated screenshots')

    def test_resize_changes_generation_and_emits_a_full_reset(self):
        from PIL import Image
        frames = self.frames()
        frames.update(Image.new('RGB', (4, 3), 'red'))
        frames.start()
        first = frames.next_event(timeout=2)
        frames.update(Image.new('RGB', (6, 2), 'blue'), [(1, 1, 1, 1)])
        resized = frames.next_event(timeout=2)
        self.assertTrue(resized['reset'])
        self.assertGreater(resized['generation'], first['generation'])
        self.assertEqual((resized['x'], resized['y'], resized['rect_width'], resized['rect_height']), (0, 0, 6, 2))
        self.assertEqual(self.image(resized).getpixel((5, 1)), (0, 0, 255))

    def test_start_watermark_rejects_old_queued_reset_and_inflight_encoder_epoch(self):
        from PIL import Image
        frames = self.frames()
        frames.update(Image.new('RGB', (8, 6), 'red'))
        self.assertEqual(frames.start(), 0)
        old = frames.next_event(timeout=2)
        frames.resync()
        self.wait_for(lambda: frames.events.qsize() == 1)
        queued_sequence = frames.sequence
        floor = frames.start()
        self.assertEqual(floor, queued_sequence)
        current = frames.next_event(timeout=2)
        self.assertTrue(current['reset'])
        self.assertGreater(current['sequence'], floor)
        self.assertLessEqual(old['sequence'], floor)

        entered, resume = threading.Event(), threading.Event()
        original_save = Image.Image.save
        def save(image, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not resume.wait(timeout=2):
                    raise RuntimeError('test encoder was not resumed')
            return original_save(image, *args, **kwargs)
        with mock.patch.object(Image.Image, 'save', save):
            frames.update(Image.new('RGB', (8, 6), 'green'))
            self.assertTrue(entered.wait(timeout=2))
            frames.update(Image.new('RGB', (8, 6), 'blue'))
            floor = frames.start()
            resume.set()
            current = frames.next_event(timeout=2)
        self.assertTrue(current['reset'])
        self.assertEqual(current['sequence'], floor + 1, 'the old encoder must not publish a sequence')
        self.assertEqual(self.image(current).getpixel((0, 0)), (0, 0, 255))
        self.assertIsNone(frames.next_event(timeout=.02))

    def test_slow_consumer_receives_coalesced_paints_without_releasing_drag(self):
        from PIL import Image
        frames = self.frames(capacity=1)
        image = Image.new('RGB', (8, 6), 'black')
        frames.update(image)
        frames.start()
        first = frames.next_event(timeout=2)
        image.putpixel((1, 1), (255, 0, 0))
        frames.update(image, [(1, 1, 1, 1)])
        self.wait_for(lambda: frames.events.qsize() == 1)
        encoded = threading.Event()
        save = Image.Image.save
        def record_encode(frame, *args, **kwargs):
            encoded.set()
            return save(frame, *args, **kwargs)
        with mock.patch.object(Image.Image, 'save', record_encode):
            image.putpixel((6, 4), (0, 255, 0))
            frames.update(image, [(6, 4, 1, 1)])
            image.putpixel((4, 2), (0, 0, 255))
            frames.update(image, [(4, 2, 1, 1)])
            self.assertFalse(encoded.wait(.1), 'a full queue must keep accumulating dirty pixels')
        queued = frames.next_event(timeout=2)
        coalesced = frames.next_event(timeout=2)
        self.assertEqual([queued['sequence'], coalesced['sequence']],
                         [first['sequence'] + 1, first['sequence'] + 2])
        for event in (queued, coalesced):
            self.assertFalse(event['reset'])
        snapshot = self.image(first)
        for event in (queued, coalesced):
            snapshot.paste(self.image(event), (event['x'], event['y']))
        self.assertEqual(snapshot.tobytes(), image.tobytes())
        self.assertIsNone(frames.next_event(timeout=.03))


FAKE_WORKER = r'''
import json
import os
import sys
import time

mode, record_path = MODE, RECORD_PATH
waiting = []

def emit(value):
    print(json.dumps(value), flush=True)

def reply(request, **values):
    emit(dict(values, type='reply', id=request['id'], ok=True))

def event(**values):
    emit({'type': 'event', 'event': values})

for line in sys.stdin:
    request = json.loads(line)
    with open(record_path, 'a') as record:
        record.write(json.dumps(request) + '\n')
    if 'config' in request:
        reply(request, capabilities={'live': True, 'pointer': True, 'key': True,
            'clipboard': {'read': True, 'write': True, 'encoding': 'utf-8'}})
        continue
    if mode == 'reorder':
        waiting.append(request)
        if len(waiting) == 2:
            event(type='clipboard', text='between-commands')
            reply(waiting[1], token=waiting[1]['token'], reply_order=0)
            emit({'type': 'reply', 'id': 999999, 'ok': True, 'token': 'unrelated'})
            event(type='frame', sequence=1, reset=True, marker='independent-frame')
            reply(waiting[0], token=waiting[0]['token'], reply_order=1)
    elif mode == 'overflow':
        if request['action'] == 'flood':
            for sequence in range(25):
                event(type='frame', sequence=sequence, reset=False, marker='discarded-patch')
            reply(request)
        elif request['action'] == 'resync_live':
            event(type='frame', sequence=25, reset=False, marker='still-not-a-reset')
            event(type='frame', sequence=26, reset=True, marker='resynchronized-frame')
            reply(request)
    elif mode == 'eof':
        os.close(sys.stdout.fileno())
        time.sleep(30)
    elif mode == 'malformed':
        print('not json', flush=True)
        time.sleep(30)
    elif mode == 'oversize':
        print('x' * 2048, flush=True)
        time.sleep(30)
    elif mode == 'timeout':
        time.sleep(30)
'''


class DesktopWorkerTests(unittest.TestCase):
    def worker(self, mode):
        temporary = tempfile.TemporaryDirectory(prefix='decklab-fake-desktop-')
        self.addCleanup(temporary.cleanup)
        record = Path(temporary.name) / 'requests.jsonl'
        source = 'MODE = ' + repr(mode) + '\nRECORD_PATH = ' + repr(str(record)) + '\n' + FAKE_WORKER
        processes = []

        def spawn(command, **kwargs):
            # Exercise the real pipe reader/writer and process lifecycle without
            # importing native clients or reaching a display or network.
            process = REAL_POPEN([sys.executable, '-u', '-c', source], **kwargs)
            processes.append(process)
            return process

        patcher = mock.patch.object(providers.subprocess, 'Popen', side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = providers.DesktopWorker({'provider': 'rdp', 'secret_url': 'fake-secret-only'})

        def close():
            worker.close()
            worker.reader.join(timeout=1)
            worker.writer.join(timeout=1)
            self.assertFalse(worker.reader.is_alive())
            self.assertFalse(worker.writer.is_alive())

        self.addCleanup(close)
        return worker, record, processes

    def records(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_command_ids_route_out_of_order_replies_and_events_independently(self):
        worker, record, processes = self.worker('reorder')
        capabilities = worker.capabilities()
        self.assertTrue(capabilities['live'])
        capabilities['clipboard']['read'] = False
        self.assertTrue(worker.capabilities()['clipboard']['read'])
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {token: executor.submit(worker.request, {'action': 'probe', 'token': token}, timeout=2)
                for token in ('first', 'second')}
            results = {token: future.result(timeout=3) for token, future in futures.items()}
        self.assertEqual({token: result['token'] for token, result in results.items()},
            {'first': 'first', 'second': 'second'})
        sent = self.records(record)
        commands = [request for request in sent if 'config' not in request]
        self.assertEqual(len({request['id'] for request in sent}), 3)
        self.assertEqual(results[commands[0]['token']]['reply_order'], 1)
        self.assertEqual(results[commands[1]['token']]['reply_order'], 0)
        self.assertEqual(worker.next_event(timeout=1), {'type': 'clipboard', 'text': 'between-commands'})
        self.assertEqual(worker.next_event(timeout=1)['marker'], 'independent-frame')
        self.assertIsNone(worker.next_event(timeout=.02))
        self.assertEqual(len(processes), 1)

    def test_invalid_worker_stream_or_unknown_timeout_kills_attachment_without_replay(self):
        for mode in ('eof', 'malformed', 'oversize', 'timeout'):
            with self.subTest(mode=mode):
                worker, record, processes = self.worker(mode)
                worker.process.kill = mock.Mock(wraps=worker.process.kill)
                # A smaller receive limit exercises the same bounded-read branch
                # without allocating a multi-megabyte fixture in this test.
                with mock.patch.object(providers, 'MAX_MESSAGE_BYTES', 1024):
                    with self.assertRaisesRegex(RuntimeError, 'outcome unknown') as error:
                        worker.request({'action': 'input', 'token': 'deliver-once'}, timeout=.05)
                self.assertNotIn('fake-secret-only', str(error.exception))
                self.assertTrue(worker.closed.is_set())
                self.assertIsNotNone(worker.process.poll())
                worker.process.kill.assert_called_once_with()
                self.assertTrue(worker.process.stdin.closed)
                self.assertTrue(worker.process.stdout.closed)
                with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                    worker.request({'action': 'input', 'token': 'deliver-once'}, timeout=.05)
                commands = [request for request in self.records(record) if 'config' not in request]
                self.assertEqual([(request['action'], request['token']) for request in commands],
                    [('input', 'deliver-once')])
                self.assertEqual(len(processes), 1, 'an uncertain command must not restart the worker')
                self.assertEqual(worker.next_event(timeout=.02)['type'], 'closed')

    def test_worker_event_overflow_requests_reset_and_drops_intermediate_patches(self):
        worker, record, _ = self.worker('overflow')
        worker.request({'action': 'flood'}, timeout=2)
        self.assertTrue(worker.needs_reset)
        frame = worker.next_event(timeout=1)
        self.assertEqual(frame['marker'], 'resynchronized-frame')
        self.assertTrue(frame['reset'])
        self.assertFalse(worker.needs_reset)
        self.assertIsNone(worker.next_event(timeout=.02))
        self.assertEqual([request.get('action') for request in self.records(record) if 'config' not in request],
            ['flood', 'resync_live'])

    def test_worker_main_dispatches_live_and_discrete_commands_with_separate_event_envelopes(self):
        event_written = threading.Event()
        instances = []

        class Output(io.StringIO):
            def write(self, value):
                result = super().write(value)
                if value.strip() and json.loads(value).get('type') == 'event':
                    event_written.set()
                return result

        class Provider:
            def __init__(self, config):
                self.calls, self.events, self.closed = [], queue.Queue(), False
                instances.append(self)
                print('native diagnostic stays outside the IPC stream')

            def capabilities(self):
                return {'live': True, 'clipboard': {'read': True, 'write': True}}

            def start_live(self):
                self.calls.append('start_live')
                self.events.put({'type': 'frame', 'reset': True, 'marker': 'native-update'})
                if not event_written.wait(timeout=1):
                    raise RuntimeError('fake frame was not emitted')
                return {'ok': True}

            def next_event(self, timeout=1):
                try:
                    return self.events.get(timeout=.01)
                except queue.Empty:
                    return None

            def screenshot(self):
                self.calls.append('screenshot')
                return {'png_base64': 'generated-frame'}

            def live_input(self, request):
                self.calls.append(('live_input', request))
                return {'ok': True}

            def clipboard_read(self):
                self.calls.append('clipboard_read')
                return {'ok': True, 'available': True, 'text': 'copied text'}

            def release_all(self):
                self.calls.append('release_all')
                return {'ok': True}

            def stop_live(self):
                self.calls.append('stop_live')
                return {'ok': True}

            def input(self, request):
                self.calls.append(('input', request['action']))
                return {'ok': True}

            def close(self):
                self.closed = True

        pointer = {'type': 'pointer', 'x': 3, 'y': 4, 'buttons': 1}
        messages = [
            {'config': {'provider': 'vnc', 'secret_url': 'synthetic-secret-must-not-be-returned'}},
            {'action': 'start_live'}, {'action': 'screenshot'},
            {'action': 'live_input', 'input': pointer}, {'action': 'clipboard_read'},
            {'action': 'release_all'}, {'action': 'stop_live'},
            {'action': 'click', 'x': 3, 'y': 4, 'button': 'left'},
        ]
        source = ''.join(json.dumps(dict(message, id=index)) + '\n'
            for index, message in enumerate(messages, 1))
        stdin = io.TextIOWrapper(io.BytesIO(source.encode()))
        self.addCleanup(stdin.close)
        output, diagnostics = Output(), io.StringIO()
        with mock.patch.object(providers, 'VNCDesktop', Provider), \
                mock.patch.object(sys, 'stdin', stdin), \
                mock.patch.object(sys, 'stdout', output), \
                mock.patch.object(sys, 'stderr', diagnostics):
            self.assertEqual(providers.worker_main(), 0)
            # The only fake native event is synchronized above. Let the empty
            # event reader observe shutdown before restoring global streams.
            for thread in threading.enumerate():
                if thread.name == 'desktop-worker-events':
                    thread.join(timeout=1)
                    self.assertFalse(thread.is_alive())
        envelopes = [json.loads(line) for line in output.getvalue().splitlines()]
        replies = [value for value in envelopes if value['type'] == 'reply']
        self.assertEqual([value['id'] for value in replies], list(range(1, 9)))
        self.assertTrue(all(value['ok'] for value in replies))
        self.assertTrue(replies[0]['capabilities']['live'])
        self.assertEqual(replies[2]['png_base64'], 'generated-frame')
        self.assertEqual(replies[4]['text'], 'copied text')
        self.assertEqual([value for value in envelopes if value['type'] == 'event'],
            [{'type': 'event', 'event': {'type': 'frame', 'reset': True, 'marker': 'native-update'}}])
        self.assertEqual(instances[0].calls, ['start_live', 'screenshot', ('live_input', pointer),
            'clipboard_read', 'release_all', 'stop_live', ('input', 'click')])
        self.assertTrue(instances[0].closed)
        self.assertIn('native diagnostic', diagnostics.getvalue())
        self.assertNotIn('native diagnostic', output.getvalue())
        self.assertNotIn('synthetic-secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
