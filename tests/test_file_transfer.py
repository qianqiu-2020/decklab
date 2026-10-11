"""Exercise serial transfer shell commands against a local temporary directory."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.file_transfer import (BATCH_SIZE, CHUNK_SIZE, MAX_CONSOLE_LINE,
                                        SerialFileTransfer, TransferError, _field)


class ShellDevice:
    def __init__(self):
        self.connection_id = 'connection-1'
        self.present = True
        self.fd = 1
        self.stop = threading.Event()
        self.operation_lock = threading.Lock()
        self.probing = False
        self.commands = []
        self.shell_checks = []
        self.gzip_available = True

    def _run_cmd_locked(self, command, timeout, ensure_shell=True):
        self.commands.append(command)
        self.shell_checks.append(ensure_shell)
        if not self.gzip_available:
            command = command.replace('command -v gzip >/dev/null 2>&1', 'false')
        result = subprocess.run(['/bin/sh', '-c', command], capture_output=True,
                                text=True, timeout=timeout)
        return True, result.stdout + result.stderr


class SerialFileTransferTests(unittest.TestCase):
    def test_completed_and_aborted_transfers_release_expiry_timer(self):
        with mock.patch('seriald_core.file_transfer.threading.Timer') as timer_factory:
            timers = []

            def make_timer(*args, **kwargs):
                timer = mock.Mock()
                timers.append(timer)
                return timer

            timer_factory.side_effect = make_timer
            self.upload(self.root / 'complete', b'')
            timers[0].cancel.assert_called_once()
            self.assertIsNone(self.transfer._expiry_timer)
            state = self.transfer.start('upload', str(self.root / 'abort'),
                                        self.device.connection_id, size=0)
            timer = self.transfer._expiry_timer
            self.transfer.abort(state['id'], self.device.connection_id)
            timer.cancel.assert_called_once()
            self.assertIsNone(self.transfer._expiry_timer)
            self.assertFalse(self.transfer.active)

    def test_local_release_requires_original_transfer_and_connection_and_never_writes(self):
        state = self.transfer.start('upload', str(self.root / 'disconnect'),
                                    self.device.connection_id, size=0)
        timer, commands = self.transfer._expiry_timer, len(self.device.commands)
        self.device.connection_id = 'replacement'
        self.assertFalse(self.transfer.release_local('wrong-task', 'connection-1'))
        self.assertFalse(self.transfer.release_local(state['id'], 'replacement'))
        self.assertTrue(self.transfer.active)
        self.assertTrue(self.transfer.release_local(state['id'], 'connection-1'))
        self.assertFalse(self.transfer.active)
        self.assertIsNone(self.transfer._expiry_timer)
        self.assertEqual(len(self.device.commands), commands)
        timer.join(1)
        self.assertFalse(timer.is_alive())

    def test_idle_timer_reschedules_then_expires_and_releases_reservation(self):
        with mock.patch('seriald_core.file_transfer.threading.Timer') as factory:
            factory.side_effect = lambda *args, **kwargs: mock.Mock()
            state = self.transfer.start('upload', str(self.root / 'expire'),
                                        self.device.connection_id, size=0)
            original = self.transfer._expiry_timer
            self.transfer._expire(state['id'])
            replacement = self.transfer._expiry_timer
            original.cancel.assert_called_once()
            self.assertIsNot(original, replacement)
            self.transfer.state['last'] -= 601
            self.transfer._expire(state['id'])
            replacement.cancel.assert_called_once()
            self.assertIsNone(self.transfer._expiry_timer)
            self.assertFalse(self.transfer.active)
            self.assertFalse(list(self.root.glob('*.part')))

    def test_stale_expiry_callback_cannot_cancel_replacement_transfer(self):
        with mock.patch('seriald_core.file_transfer.threading.Timer') as factory:
            factory.side_effect = lambda *args, **kwargs: mock.Mock()
            first = self.transfer.start('upload', str(self.root / 'first'),
                                        self.device.connection_id, size=0)
            self.transfer.abort(first['id'], self.device.connection_id)
            second = self.transfer.start('upload', str(self.root / 'second'),
                                         self.device.connection_id, size=0)
            timer = self.transfer._expiry_timer
            self.transfer._expire(first['id'])
            self.assertEqual(self.transfer.state['id'], second['id'])
            self.assertIs(self.transfer._expiry_timer, timer)
            timer.cancel.assert_not_called()
            self.transfer.abort(second['id'], self.device.connection_id)

    def test_result_field_follows_echoed_batch_command(self):
        output = 'SD_RC=%s\nSD_SIZE=%s\n\x1b[?2004l\rSD_RC=0\r\nSD_SIZE=30720\x13\n'
        self.assertEqual(_field(output, 'SD_RC'), '0')
        self.assertEqual(_field(output, 'SD_SIZE'), '30720')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.device = ShellDevice()
        self.transfer = SerialFileTransfer(self.device)

    def upload(self, path, data, overwrite=False):
        state = self.transfer.start('upload', str(path), self.device.connection_id,
                                    size=len(data), overwrite=overwrite)
        offset = 0
        while offset < len(data):
            raw = data[offset:offset + CHUNK_SIZE]
            while True:
                try:
                    chunk = self.transfer.chunk(state['id'], self.device.connection_id, offset,
                                                base64.b64encode(raw).decode('ascii'))
                    break
                except TransferError as error:
                    if error.status != 413 or len(raw) == 1:
                        raise
                    raw = raw[:max(1, len(raw) // 2)]
            self.assertEqual(chunk['offset'], offset + len(raw))
            offset = chunk['offset']
        return self.transfer.finish(state['id'], self.device.connection_id)

    def test_binary_upload_is_atomic_and_uses_expected_hash(self):
        data = bytes(range(256)) * 20 + b'\x00\xff\n'
        target = self.root / "board's file.bin"
        result = self.upload(target, data)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertFalse(self.transfer.active)
        self.assertEqual(self.device.shell_checks, [True] + [False] * (len(self.device.shell_checks) - 1))

    def test_compressible_upload_and_download_keep_original_bytes(self):
        data = b'boot: device initialized\n' * 600
        target = self.root / 'boot.log'
        self.upload(target, data)
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue(any('| gzip -d >>' in command for command in self.device.commands))
        state = self.transfer.start('download', str(target), self.device.connection_id)
        received = bytearray()
        for offset in range(0, len(data), CHUNK_SIZE):
            part = self.transfer.chunk(state['id'], self.device.connection_id, offset)
            received.extend(base64.b64decode(part['data_b64']))
        self.assertEqual(self.transfer.finish(state['id'], self.device.connection_id)['sha256'],
                         hashlib.sha256(data).hexdigest())
        self.assertEqual(bytes(received), data)
        self.assertTrue(any('| gzip -c | base64' in command for command in self.device.commands))

    def test_missing_board_gzip_uses_plain_base64(self):
        self.device.gzip_available = False
        data = b'compress me\n' * 400
        target = self.root / 'plain.log'
        self.upload(target, data)
        self.assertFalse(any('| gzip -d >>' in command for command in self.device.commands))
        state = self.transfer.start('download', str(target), self.device.connection_id)
        for offset in range(0, len(data), CHUNK_SIZE):
            self.transfer.chunk(state['id'], self.device.connection_id, offset)
        self.transfer.finish(state['id'], self.device.connection_id)
        self.assertFalse(any('| gzip -c | base64' in command for command in self.device.commands))

    def test_upload_does_not_overwrite_without_explicit_flag(self):
        target = self.root / 'file.bin'
        target.write_bytes(b'old')
        with self.assertRaisesRegex(TransferError, 'exists'):
            self.transfer.start('upload', str(target), self.device.connection_id, size=3)
        self.assertEqual(target.read_bytes(), b'old')
        self.upload(target, b'new', overwrite=True)
        self.assertEqual(target.read_bytes(), b'new')

    def test_failed_upload_can_be_aborted_and_cleans_temporary_file(self):
        target = self.root / 'partial.bin'
        state = self.transfer.start('upload', str(target), self.device.connection_id, size=5)
        with self.assertRaisesRegex(TransferError, '偏移'):
            self.transfer.chunk(state['id'], self.device.connection_id, 1, 'AAAA')
        with self.assertRaisesRegex(TransferError, '长度'):
            self.transfer.chunk(state['id'], self.device.connection_id, 0,
                                base64.b64encode(b'123456').decode('ascii'))
        self.transfer.abort(state['id'], self.device.connection_id)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('*.part')))

    def test_download_checks_complete_hash_and_detects_source_change(self):
        target = self.root / 'download.bin'
        original = bytes(range(256)) * 10
        target.write_bytes(original)
        state = self.transfer.start('download', str(target), self.device.connection_id)
        received = bytearray()
        for offset in range(0, len(original), CHUNK_SIZE):
            chunk = self.transfer.chunk(state['id'], self.device.connection_id, offset)
            received.extend(base64.b64decode(chunk['data_b64'], validate=True))
        result = self.transfer.finish(state['id'], self.device.connection_id)
        self.assertEqual(bytes(received), original)
        self.assertEqual(result['sha256'], hashlib.sha256(original).hexdigest())

        state = self.transfer.start('download', str(target), self.device.connection_id)
        for offset in range(0, len(original), CHUNK_SIZE):
            self.transfer.chunk(state['id'], self.device.connection_id, offset)
        target.write_bytes(b'changed')
        with self.assertRaisesRegex(TransferError, '变化'):
            self.transfer.finish(state['id'], self.device.connection_id)
        self.transfer.abort(state['id'], self.device.connection_id)

    def test_empty_files_and_connection_pin(self):
        target = self.root / 'empty'
        self.upload(target, b'')
        state = self.transfer.start('download', str(target), self.device.connection_id)
        self.assertEqual(self.transfer.finish(state['id'], self.device.connection_id)['size'], 0)
        state = self.transfer.start('upload', str(self.root / 'later'), self.device.connection_id, size=0)
        self.device.connection_id = 'replacement'
        with self.assertRaisesRegex(TransferError, '变化'):
            self.transfer.finish(state['id'], 'connection-1')

    def test_path_and_payload_validation(self):
        for path in ('relative', '/tmp/new\nline'):
            with self.assertRaises(TransferError):
                self.transfer.start('download', path, self.device.connection_id)
        state = self.transfer.start('upload', str(self.root / 'new'), self.device.connection_id, size=2)
        with self.assertRaisesRegex(TransferError, 'base64'):
            self.transfer.chunk(state['id'], self.device.connection_id, 0, '!')
        self.transfer.abort(state['id'], self.device.connection_id)

    def test_batch_upload_compressible_and_incompressible(self):
        for name, data in [('log', b'boot: ready\n' * 2800),
                           ('binary', os.urandom(BATCH_SIZE + 100))]:
            target = self.root / name
            state = self.transfer.start('upload', str(target), self.device.connection_id,
                                        size=len(data))
            offset = 0
            while offset < len(data):
                raw = data[offset:offset + BATCH_SIZE]
                result = self.transfer.batch(state['id'], self.device.connection_id, offset,
                                             base64.b64encode(raw).decode('ascii'))
                offset = result['offset']
            self.transfer.finish(state['id'], self.device.connection_id)
            self.assertEqual(target.read_bytes(), data)
        self.assertTrue(any('\n' in command and 'SD_RC=' in command
                            for command in self.device.commands))
        for command in self.device.commands:
            if 'SD_RC=' in command and '\n' in command:
                self.assertLessEqual(max(len(line.encode()) + 120
                                         for line in command.split('\n')), MAX_CONSOLE_LINE)

    def test_batch_rejects_bad_payload_without_advancing(self):
        target = self.root / 'batch'
        state = self.transfer.start('upload', str(target), self.device.connection_id,
                                    size=BATCH_SIZE + 1)
        with self.assertRaises(TransferError):
            self.transfer.batch(state['id'], self.device.connection_id, 0, '!')
        with self.assertRaises(TransferError):
            self.transfer.batch(state['id'], self.device.connection_id, 0,
                                base64.b64encode(b'x' * (BATCH_SIZE + 1)).decode('ascii'))
        self.assertEqual(self.transfer.state['offset'], 0)
        self.transfer.abort(state['id'], self.device.connection_id)

    def test_batch_lost_ack_is_confirmed_by_remote_size_and_hash(self):
        data = os.urandom(BATCH_SIZE + 123)
        target = self.root / 'lost-ack.bin'
        original = self.device._run_cmd_locked
        lost = False

        def drop_one_ack(command, timeout, ensure_shell=True):
            nonlocal lost
            result = original(command, timeout, ensure_shell)
            if not lost and '\n' in command and 'SD_RC=' in command:
                lost = True
                return False, '响应起始帧缺失或行结束符异常'
            return result

        self.device._run_cmd_locked = drop_one_ack
        state = self.transfer.start('upload', str(target), self.device.connection_id,
                                    size=len(data))
        offset = 0
        while offset < len(data):
            part = data[offset:offset + BATCH_SIZE]
            result = self.transfer.batch(state['id'], self.device.connection_id, offset,
                                         base64.b64encode(part).decode('ascii'))
            offset = result['offset']
        result = self.transfer.finish(state['id'], self.device.connection_id)
        self.assertTrue(lost)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(any('SD_SHA=' in command for command in self.device.commands))

    def test_batch_unexecuted_command_does_not_advance_after_check(self):
        data = os.urandom(1000)
        target = self.root / 'unexecuted.bin'
        original = self.device._run_cmd_locked

        def drop_batch(command, timeout, ensure_shell=True):
            if '\n' in command and 'SD_RC=' in command:
                return False, '响应起始帧缺失或行结束符异常'
            return original(command, timeout, ensure_shell)

        self.device._run_cmd_locked = drop_batch
        state = self.transfer.start('upload', str(target), self.device.connection_id,
                                    size=len(data))
        with self.assertRaisesRegex(TransferError, '校验未通过'):
            self.transfer.batch(state['id'], self.device.connection_id, 0,
                                base64.b64encode(data).decode('ascii'))
        self.assertEqual(self.transfer.state['offset'], 0)
        self.assertTrue(self.transfer.state['failed'])
        self.transfer.abort(state['id'], self.device.connection_id)

    def test_final_ack_lost_after_rename_confirms_destination_without_repeating(self):
        data = os.urandom(2048)
        target = self.root / 'final-ack.bin'
        state = self.transfer.start('upload', str(target), self.device.connection_id,
                                    size=len(data))
        self.transfer.batch(state['id'], self.device.connection_id, 0,
                            base64.b64encode(data).decode('ascii'))
        original = self.device._run_cmd_locked
        lost = False

        def drop_final_ack(command, timeout, ensure_shell=True):
            nonlocal lost
            result = original(command, timeout, ensure_shell)
            if not lost and 'SD_OK=1' in command and 'sha256sum' in command:
                lost = True
                return False, '响应起始帧缺失'
            return result

        self.device._run_cmd_locked = drop_final_ack
        result = self.transfer.finish(state['id'], self.device.connection_id)
        self.assertTrue(lost)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(sum('SD_OK=1' in command and 'sha256sum' in command
                             for command in self.device.commands), 1)
        self.assertFalse(self.transfer.active)

    def test_final_ack_lost_before_rename_does_not_claim_success(self):
        data = b'unchanged'
        target = self.root / 'no-rename.bin'
        state = self.transfer.start('upload', str(target), self.device.connection_id,
                                    size=len(data))
        self.transfer.batch(state['id'], self.device.connection_id, 0,
                            base64.b64encode(data).decode('ascii'))
        original = self.device._run_cmd_locked

        def drop_final_command(command, timeout, ensure_shell=True):
            if 'SD_OK=1' in command and 'sha256sum' in command:
                return False, '响应起始帧缺失'
            return original(command, timeout, ensure_shell)

        self.device._run_cmd_locked = drop_final_command
        with self.assertRaisesRegex(TransferError, '响应起始帧缺失'):
            self.transfer.finish(state['id'], self.device.connection_id)
        self.assertFalse(target.exists())
        self.transfer.abort(state['id'], self.device.connection_id)


if __name__ == '__main__':
    unittest.main()
