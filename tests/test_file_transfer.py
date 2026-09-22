"""Exercise serial transfer shell commands against a local temporary directory."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from seriald_core.file_transfer import (BATCH_SIZE, CHUNK_SIZE, MAX_CONSOLE_LINE,
                                        SerialFileTransfer, TransferError, _field)
from serialctl import ClientError, file_put, file_get, file_copy


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


class TransferClient:
    def __init__(self, key):
        self.key = key
        self.device = ShellDevice()
        self.device.connection_id = key + '-connection'
        self.connection_id = self.device.connection_id
        self.transfer = SerialFileTransfer(self.device)

    def post(self, route, **payload):
        action = route.rsplit('/', 1)[-1]
        try:
            if action == 'start':
                return self.transfer.start(payload['direction'], payload['path'], self.connection_id,
                                           size=payload.get('size'), overwrite=payload.get('overwrite', False))
            if action == 'chunk':
                return self.transfer.chunk(payload['id'], self.connection_id, payload['offset'],
                                           payload.get('data_b64'))
            if action == 'batch':
                return self.transfer.batch(payload['id'], self.connection_id, payload['offset'],
                                           payload.get('data_b64'))
            if action == 'finish':
                return self.transfer.finish(payload['id'], self.connection_id)
            if action == 'abort':
                return self.transfer.abort(payload['id'], self.connection_id)
        except TransferError as error:
            raise ClientError(str(error), status=error.status) from error
        raise AssertionError(route)


class CliFileTransferTests(unittest.TestCase):
    def test_incompressible_upload_splits_without_losing_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = os.urandom(CHUNK_SIZE + 3072)
            source_path, target_path = root / 'source.bin', root / 'target.bin'
            source_path.write_bytes(data)
            target = TransferClient('target')
            target.device.gzip_available = False
            file_put(target, str(source_path), str(target_path))
            self.assertEqual(target_path.read_bytes(), data)
            self.assertGreater(sum(command.count('base64 -d')
                                   for command in target.device.commands), 2)

    def test_board_copy_overlaps_two_independent_serial_ports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = bytes(range(256)) * 64
            source_path, target_path = root / 'source.bin', root / 'target.bin'
            source_path.write_bytes(data)
            source, target = TransferClient('source'), TransferClient('target')
            next_read_started = threading.Event()
            original_source, original_target = source.post, target.post
            source_chunks = 0
            def source_post(route, **payload):
                nonlocal source_chunks
                if route.endswith('/chunk'):
                    source_chunks += 1
                    if source_chunks == 2:
                        next_read_started.set()
                return original_source(route, **payload)
            def target_post(route, **payload):
                if route.endswith('/batch') and payload['offset'] == 0:
                    self.assertTrue(next_read_started.wait(1), 'source prefetch did not overlap destination write')
                return original_target(route, **payload)
            source.post, target.post = source_post, target_post
            result = file_copy(source, target, str(source_path), str(target_path))
            self.assertEqual(target_path.read_bytes(), data)
            rates = result['rate_diagnostics']
            self.assertGreater(rates['source_kib_s'], 0)
            self.assertGreater(rates['destination_kib_s'], 0)
            self.assertGreater(rates['effective_kib_s'], 0)

    def test_local_upload_download_and_board_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'local.bin'
            data = bytes(range(256)) * 19
            source.write_bytes(data)
            first, second = TransferClient('first'), TransferClient('second')
            first_path, second_path = root / 'first.bin', root / 'second.bin'
            file_put(first, str(source), str(first_path))
            self.assertEqual(first_path.read_bytes(), data)
            copy_result = file_copy(first, second, str(first_path), str(second_path))
            self.assertEqual(second_path.read_bytes(), data)
            self.assertEqual(copy_result['sha256'], hashlib.sha256(data).hexdigest())
            download = root / 'download.bin'
            file_get(second, str(second_path), str(download))
            self.assertEqual(download.read_bytes(), data)
            with self.assertRaisesRegex(ClientError, 'already exists'):
                file_get(second, str(second_path), str(download))
            self.assertFalse(list(root.glob('.seriald-*')))


if __name__ == '__main__':
    unittest.main()
