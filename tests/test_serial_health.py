"""Hourly scheduling and local-shell round trips; no real board or UART access."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from seriald_core.device import Device
from seriald_core import serial_health
from seriald_core.terminal_backend import node_binary


@unittest.skipUnless(node_binary(), 'requires the packaged terminal runtime')
class SerialHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        logs = mock.patch('seriald_core.device.LOG_ROOT', str(self.root / 'logs'))
        logs.start()
        self.addCleanup(logs.stop)
        self.device = Device('fake-port', '/fake/tty')
        self.device.fd, self.device.present, self.device.newline = 98765, True, '\r'
        self.addCleanup(self.device.terminal.screen.close)
        self.addCleanup(self.device.close_log)
        replies = mock.patch.object(self.device, '_protocol_reply', return_value=True)
        replies.start()
        self.addCleanup(replies.stop)
        self.device.append(b'root@fixture:~# ')

    def shell_write(self, text):
        result = subprocess.run(['/bin/sh', '-c', text.rstrip('\r')],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        self.device.append(text.encode().replace(b'\r', b'\r\n')
                           + result.stdout.replace(b'\n', b'\r\n') + b'root@fixture:~# ')
        return True, ''

    def probe(self):
        self.device.health._probe(self.device.connection_id)
        return self.device.health.snapshot()

    def test_first_probe_is_due_after_one_hour_and_only_one_worker_runs(self):
        health = self.device.health
        with mock.patch.object(serial_health, 'time', types.SimpleNamespace(
                monotonic=lambda: health.due - 1, time=lambda: 1000)), \
                mock.patch.object(serial_health.threading, 'Thread') as thread:
            health.tick()
            thread.assert_not_called()
        with mock.patch.object(serial_health, 'time', types.SimpleNamespace(
                monotonic=lambda: health.due, time=lambda: 1001)), \
                mock.patch.object(serial_health.threading, 'Thread') as thread:
            health.tick()
            health.tick()
            thread.assert_called_once()
            self.assertTrue(health.snapshot()['running'])

    @unittest.skipIf(os.name == 'nt', 'local POSIX shell integration')
    def test_success_uses_existing_framed_stream_without_visible_echo(self):
        with mock.patch.object(self.device, '_write', side_effect=self.shell_write) as write:
            state = self.probe()
        self.assertEqual(state['status'], 'ok')
        self.assertGreater(state['last_success'], 0)
        self.assertAlmostEqual(state['next_check'] - state['last_success'], 3600, delta=1)
        self.assertIn('; :; ', write.call_args.args[0])
        self.assertIn(b'__SD_B_', self.device.buf)
        self.assertIn(b'__SD_E_', self.device.buf)
        screen = base64.b64decode(self.device.terminal.screen.snapshot()['data']).decode()
        self.assertNotIn('printf', screen)
        self.assertNotIn('__SD_', screen)
        self.device.close_log()
        archive = '\n'.join(path.read_text() for path in Path(self.device.stable_dir()).glob('*.log'))
        self.assertNotIn('printf', archive)
        self.assertNotIn('__SD_', archive)
        self.assertEqual(self.device.snapshot()['serial_health']['status'], 'ok')

    def test_busy_firmware_application_and_owned_input_are_never_probed(self):
        with mock.patch.object(self.device, '_write') as write:
            self.device.terminal.input_pending = True
            self.assertEqual(self.probe()['status'], 'waiting')
            self.device.terminal.input_pending = False
            for phase in ('firmware', 'application'):
                with mock.patch.object(self.device.terminal.screen, 'metadata', return_value={'phase': phase}):
                    self.assertEqual(self.probe()['status'], 'waiting')
            with self.device.transactions.actor_scope('agent/other'):
                self.device.transactions.claim('agent/other')
            self.assertEqual(self.probe()['status'], 'waiting')
            self.assertEqual(self.device.health.last_attempt, 0)
            write.assert_not_called()

    def test_file_transfer_and_operation_exclusion_are_respected(self):
        with mock.patch.object(self.device, '_write') as write:
            self.device.operation_lock.acquire()
            try:
                self.assertEqual(self.probe()['status'], 'waiting')
            finally:
                self.device.operation_lock.release()
            self.device.file_transfer.state = {'id': 'fake-transfer'}
            self.assertEqual(self.probe()['status'], 'waiting')
            self.device.file_transfer.state = None
            write.assert_not_called()

    def test_timeout_is_visible_and_busy_retry_cannot_hide_failure(self):
        with mock.patch.object(self.device, '_run_cmd_locked', return_value=(False, '等待板子完整响应超时')):
            state = self.probe()
        self.assertEqual(state['status'], 'error')
        self.assertIn('超时', state['reason'])
        self.assertGreater(state['last_attempt'], 0)
        self.assertEqual(state['last_success'], 0)
        self.device.close_log()
        archive = '\n'.join(path.read_text() for path in Path(self.device.stable_dir()).glob('*.log'))
        self.assertIn('串口主动探测失败', archive)
        self.device.terminal.input_pending = True
        self.assertEqual(self.probe()['status'], 'error')
        self.device.terminal.input_pending = False
        with mock.patch.object(self.device, '_run_cmd_locked', return_value=(True, '')):
            self.assertEqual(self.probe()['status'], 'ok')

    def test_old_probe_cannot_publish_status_to_replacement_board(self):
        health = self.device.health

        def replace(*args, **kwargs):
            self.device.detach_identity()
            return True, ''

        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=replace):
            self.probe()
        self.assertEqual(health.snapshot()['status'], 'pending')
        self.assertEqual(health.snapshot()['last_success'], 0)
        self.assertFalse(health.snapshot()['running'])
        self.assertFalse(self.device.visibility.active)

    def test_waiting_for_board_does_not_block_scheduler_or_capture(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            started.set()
            release.wait(2)
            finished.set()
            return True, ''

        self.device.health.due = 0
        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=delayed):
            try:
                self.device.health.tick()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.is_set())
                self.device.append(b'\r\n[    1.000001] kernel output while probing\r\n')
                self.assertIn(b'kernel output while probing', self.device.buf)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                # Wait for the worker's finally section before fixture cleanup.
                with self.device.operation_lock:
                    pass
