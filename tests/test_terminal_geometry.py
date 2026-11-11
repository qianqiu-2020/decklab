"""Exercise Linux console echo/size behavior on an isolated PTY, never a board."""
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import unittest
from unittest import mock


@unittest.skipUnless(os.name == 'posix', 'Linux termios requires a POSIX PTY')
class TerminalGeometryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('busybox'), 'requires a local BusyBox ash and stty')
    def test_busybox_path_prompt_configures_actual_local_pty_with_busybox_stty(self):
        import fcntl
        import pty
        import select
        import termios
        from seriald_core.actions import DeviceActions
        from seriald_core.device import Device

        busybox = shutil.which('busybox')
        with tempfile.TemporaryDirectory() as temporary:
            # This is the only external command visible to ash: successful
            # stty settings cannot accidentally come from the host GNU applet.
            applets = Path(temporary) / 'applets'
            applets.mkdir()
            (applets / 'stty').symlink_to(busybox)
            pid, fd = pty.fork()
            if pid == 0:
                os.environ.update(PS1='~ # ', PS2='> ', ENV='', PATH=str(applets), LANG='C.UTF-8',
                                  LC_ALL='C.UTF-8', TERM='dumb')
                os.execv(busybox, [busybox, 'ash', '-i'])
            device = None
            try:
                with mock.patch('seriald_core.device.LOG_ROOT', temporary), \
                        mock.patch.object(Device, '_protocol_reply', return_value=True):
                    device = Device('fixture-busybox-pty', '/fixture-only-local-pty', record={
                        'serial': 'LOCAL-BUSYBOX-PTY', 'label': 'busybox', 'info': {}})
                    device.fd, device.present = fd, True
                    actions = DeviceActions(mock.Mock())

                    def read_prompt():
                        output = bytearray()
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if select.select([fd], [], [], .1)[0]:
                                data = os.read(fd, 65536)
                                output.extend(data)
                                device.append(data)
                                if device.terminal.prompt():
                                    return bytes(output)
                        self.fail('BusyBox prompt did not return: ' + repr(bytes(output)))

                    def write(text):
                        data = text.encode('utf-8')
                        while data:
                            data = data[os.write(fd, data):]
                        return True, ''

                    def resize():
                        return actions.execute('session-resize', {
                            'sid': 'serial', 'cols': 140, 'rows': 42, 'automatic': True,
                            'observed_off': device.now_off(), 'client_id': 'fixture'}, device)

                    self.assertIn(b'~ # ', read_prompt())
                    with mock.patch.object(device, '_write', side_effect=write):
                        self.assertTrue(resize().payload['pending'])
                        self.assertIn(b'\x1b[8;42;140t', read_prompt())
                        dimensions = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8)
                        self.assertEqual(struct.unpack('HHHH', dimensions)[:2], (42, 140))
                        self.assertEqual(device.terminal.geometry, {'rows': 42, 'cols': 140})
                        self.assertEqual(device.terminal.screen.metadata()['geometry'], {'rows': 42, 'cols': 140})
                        self.assertTrue(resize().payload['pending'])
                        read_prompt()
                        write('stty -a\r')
                        settings = read_prompt()
                        self.assertRegex(settings, rb'(?:^|\s)echoctl(?:\s|$)')
                        self.assertRegex(settings, rb'(?:^|\s)iutf8(?:\s|$)')
                        self.assertTrue(resize().payload['skipped'])
            finally:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(fd)
                if device is not None:
                    device.fd = None
                    device.terminal.screen.close()
                    device.close_log()

    def test_echoctl_makes_arrow_echo_visible_without_cursor_commands(self):
        import fcntl
        import pty
        import select
        import termios
        master, slave = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] |= termios.ECHO | termios.ICANON
            attrs[3] &= ~termios.ECHOCTL
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            os.write(master, b'\x1b[A')
            self.assertTrue(select.select([master], [], [], 1)[0])
            self.assertEqual(os.read(master, 1024), b'\x1b[A')
            subprocess.run(['stty', 'echoctl', 'rows', '42', 'cols', '140'],
                           stdin=slave, check=True)
            os.write(master, b'\x1b[B')
            self.assertTrue(select.select([master], [], [], 1)[0])
            self.assertEqual(os.read(master, 1024), b'^[[B')
            dimensions = fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8)
            self.assertEqual(struct.unpack('HHHH', dimensions)[:2], (42, 140))
        finally:
            os.close(master)
            os.close(slave)
