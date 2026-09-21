"""Exercise serial transfer shell commands against a local temporary directory."""
import base64
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from seriald_core.file_transfer import CHUNK_SIZE, SerialFileTransfer, TransferError
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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.device = ShellDevice()
        self.transfer = SerialFileTransfer(self.device)

    def upload(self, path, data, overwrite=False):
        state = self.transfer.start('upload', str(path), self.device.connection_id,
                                    size=len(data), overwrite=overwrite)
        for offset in range(0, len(data), CHUNK_SIZE):
            raw = data[offset:offset + CHUNK_SIZE]
            chunk = self.transfer.chunk(state['id'], self.device.connection_id, offset,
                                        base64.b64encode(raw).decode('ascii'))
            self.assertEqual(chunk['offset'], offset + len(raw))
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
            self.transfer.chunk(state['id'], self.device.connection_id, 0, 'AAAA')
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
            if action == 'finish':
                return self.transfer.finish(payload['id'], self.connection_id)
            if action == 'abort':
                return self.transfer.abort(payload['id'], self.connection_id)
        except TransferError as error:
            raise ClientError(str(error)) from error
        raise AssertionError(route)


class CliFileTransferTests(unittest.TestCase):
    def test_board_copy_overlaps_two_independent_serial_ports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = bytes(range(256)) * 24
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
                if route.endswith('/chunk') and payload['offset'] == 0:
                    self.assertTrue(next_read_started.wait(1), 'source prefetch did not overlap destination write')
                return original_target(route, **payload)
            source.post, target.post = source_post, target_post
            file_copy(source, target, str(source_path), str(target_path))
            self.assertEqual(target_path.read_bytes(), data)

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
