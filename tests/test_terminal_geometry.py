"""Exercise Linux console echo/size behavior on an isolated PTY, never a board."""
import os
import struct
import subprocess
import unittest


@unittest.skipUnless(os.name == 'posix', 'Linux termios requires a POSIX PTY')
class TerminalGeometryTests(unittest.TestCase):
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
