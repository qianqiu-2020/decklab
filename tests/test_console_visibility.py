"""Real parser and local shell; no managed ports or development boards."""
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from seriald_core.console_visibility import human_output
from seriald_core.device import Device
from seriald_core.terminal_backend import ENGINE, node_binary


@unittest.skipUnless(node_binary(), 'requires the packaged terminal runtime')
class ConsoleVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        logs = mock.patch('seriald_core.device.LOG_ROOT', str(self.root / 'logs'))
        logs.start()
        self.addCleanup(logs.stop)
        self.device = self.make_device('one')

    def make_device(self, key):
        device = Device(key, '/fake/tty')
        device.fd, device.present, device.newline = 98765, True, '\r'
        self.addCleanup(device.terminal.screen.close)
        self.addCleanup(device.close_log)
        replies = mock.patch.object(device, '_protocol_reply', return_value=True)
        replies.start()
        self.addCleanup(replies.stop)
        device.append(b'user output\r\nroot@fixture:~# ')
        return device

    def screen(self, device=None):
        snapshot = (device or self.device).terminal.screen.snapshot()
        return base64.b64decode(snapshot['data']).decode('utf-8')

    def shell_write(self, text):
        result = subprocess.run(['/bin/sh', '-c', text.rstrip('\r')],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        output = (result.stdout + result.stderr).replace(b'\n', b'\r\n')
        self.device.append(text.encode().replace(b'\r', b'\r\n') + output + b'root@fixture:~# ')
        return True, ''

    def archive(self):
        self.device.close_log()
        return '\n'.join(path.read_text() for path in Path(self.device.stable_dir()).glob('*.log'))

    def test_setup_echo_is_hidden_but_geometry_raw_capture_and_user_commands_remain(self):
        def write(text):
            geometry = b'\x1b[8;52;205t' if text.startswith('stty ') else b''
            self.device.append(text.encode() + b'\r\n' + geometry + b'\x1b[?2004hroot@fixture:~# ')
            return True, ''

        with mock.patch.object(self.device, '_write', side_effect=write), \
                mock.patch.object(self.device, 'record_input') as record:
            for _ in range(2):
                result = self.device.terminal.configure(205, 52, self.device.now_off(), True)
                self.assertTrue(result['ok'])
            record.assert_not_called()
        snapshot = self.device.terminal.screen.snapshot()
        self.assertEqual(snapshot['geometry'], {'cols': 205, 'rows': 52})
        self.assertTrue(snapshot['modes']['bracketedPasteMode'])
        self.assertIn(b'stty echoctl', self.device.buf)
        self.assertIn(b'infocmp', self.device.buf)
        self.assertNotIn('stty echoctl', self.screen())
        self.assertNotIn('infocmp', self.screen())
        self.device.append(b'stty rows 30 cols 100\r\nroot@fixture:~# ')
        self.assertIn('stty rows 30 cols 100', self.screen())
        archive = self.archive()
        self.assertNotIn('stty echoctl', archive)
        self.assertNotIn('infocmp', archive)
        self.assertIn('stty rows 30 cols 100', archive)
        self.assertIn('user output', archive)

    @unittest.skipIf(os.name == 'nt', 'local POSIX shell integration')
    def test_binary_upload_download_keep_raw_frames_and_clean_archives(self):
        data = bytes(range(256)) * 4 + b'\0\xff\n'
        path = self.root / 'payload.bin'
        transfer = self.device.file_transfer
        with mock.patch.object(self.device, '_write', side_effect=self.shell_write):
            upload = transfer.start('upload', str(path), self.device.connection_id, size=len(data))
            transfer.chunk(upload['id'], self.device.connection_id, 0, base64.b64encode(data).decode())
            self.assertTrue(self.device.visibility.active)
            self.assertNotIn('SD_SIZE=', self.screen())
            result = transfer.finish(upload['id'], self.device.connection_id)
            self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
            download = transfer.start('download', str(path), self.device.connection_id)
            result = transfer.chunk(download['id'], self.device.connection_id, 0)
            self.assertEqual(base64.b64decode(result['data_b64']), data)
            transfer.finish(download['id'], self.device.connection_id)
        self.assertEqual(path.read_bytes(), data)
        self.assertIn(b'__SD_B_', self.device.buf)
        self.assertIn(b'SD_DATA=', self.device.buf)
        self.assertFalse(self.device.visibility.active)
        self.assertNotIn('SD_DATA=', self.screen())
        archive = self.archive()
        self.assertNotIn('SD_DATA=', archive)
        self.assertNotIn('printf', archive)
        self.assertIn('串口文件传输完成', archive)
        self.assertIn(hashlib.sha256(data).hexdigest(), archive)

    def test_queries_are_answered_during_hidden_output_and_other_ports_keep_rendering(self):
        other = self.make_device('two')
        token = self.device.visibility.begin('file-transfer')
        self.device.append(b'private command\r\n\x1b[6n')
        self.device._protocol_reply.assert_called_with('\x1b[3;1R', self.device.connection_id)
        other.append(b'other port output\r\n')
        self.assertIn('other port output', self.screen(other))
        self.assertNotIn('private command', self.screen())
        self.device.visibility.finish(token)

    def test_user_command_cannot_be_hidden_by_an_unfinished_automatic_setup(self):
        token = self.device.visibility.begin('terminal-setup', 'stty rows 30 cols 100')
        with mock.patch.object(self.device, '_write', return_value=(True, '')) as write:
            ok, error = self.device.send('ls\r')
            self.assertFalse(ok)
            self.assertIn('终端配置', error)
            write.assert_not_called()
            self.device.visibility.finish(token)
            self.assertTrue(self.device.send('ls\r')[0])
            write.assert_called_once_with('ls\r')

    def test_completion_keeps_partial_prompt_and_fragmented_osc_prefix(self):
        token = self.device.visibility.begin('file-transfer')
        self.device.append(b'private echo\r\nroo')
        self.device.visibility.finish(token)
        self.device.append(b't@fixture:~# ')
        self.assertIn('root@fixture:~#', self.screen())
        token = self.device.visibility.begin('file-transfer')
        self.device.append(b'private echo\r\nroot@fixture:~# \x1b]3008;end=context')
        self.device.visibility.finish(token)
        self.device.append(b'\x1b\\ls\r\nready\r\n')
        self.assertNotIn('end=context', self.screen())
        self.assertIn('ready', self.screen())

    def test_kernel_output_interrupts_hiding_and_is_recorded(self):
        self.device.visibility.begin('file-transfer')
        self.device.append(b'private protocol\r\n')
        self.device.append(b'[    2.123456] real kernel failure\r\n')
        self.assertFalse(self.device.visibility.active)
        self.assertIn('real kernel failure', self.screen())
        self.assertIn('real kernel failure', self.archive())

    def test_failed_parser_does_not_drop_archive_output(self):
        self.device.visibility.begin('file-transfer')
        with mock.patch.object(ENGINE, 'call', side_effect=RuntimeError('worker unavailable')):
            self.device.append(b'preserved after parser failure\r\n')
        self.assertIn('preserved after parser failure', self.archive())

    def test_setup_errors_remain_visible_and_logged(self):
        def write(text):
            self.device.append(text.encode() + b'\r\nstty: unsupported setting\r\nroot@fixture:~# ')
            return True, ''
        with mock.patch.object(self.device, '_write', side_effect=write):
            self.device.terminal.configure(205, 52, self.device.now_off(), True)
        self.assertIn('unsupported setting', self.screen())
        self.assertIn('unsupported setting', self.archive())


class HumanOutputTests(unittest.TestCase):
    def test_only_registered_echoes_and_known_fields_are_removed(self):
        command = 'printf %s payload | base64 -d\ns=$(wc -c < file)'
        output = (command + '\r\nSD_DATA=AAAA\x13\nSD_RC=0\nSD_SIZE=3\n'
                  'SD_ERROR=checksum\nbase64: invalid input\nordinary stty command\n')
        cleaned = human_output(output, command)
        self.assertNotIn('printf', cleaned)
        self.assertNotIn('SD_DATA', cleaned)
        self.assertNotIn('SD_RC', cleaned)
        self.assertIn('SD_ERROR=checksum', cleaned)
        self.assertIn('base64: invalid input', cleaned)
        self.assertIn('ordinary stty command', cleaned)
