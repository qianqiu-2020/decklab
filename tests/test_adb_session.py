"""Exercise the USB terminal through a local smart-socket peer."""
import re
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from seriald_core.adb_session import AdbSession, packet, read_exact, usb_endpoints


class AdbTerminalTests(unittest.TestCase):
    def test_server_without_paths_lists_only_online_usb_transports(self):
        local, peer = socket.socketpair()
        self.addCleanup(peer.close)
        records = ''.join('device {\n  state: %s\n  connection_type: %s\n  transport_id: %d\n}\n' % row
                          for row in [('DEVICE', 'USB', 3), ('DEVICE', 'USB', 7),
                                      ('DEVICE', 'SOCKET', 9), ('OFFLINE', 'USB', 13)]).encode()
        peer.sendall(b'OKAY' + ('%04x' % len(records)).encode() + records)
        with patch('socket.create_connection', return_value=local):
            self.assertEqual(usb_endpoints(), [('USB', '3'), ('USB', '7')])
        self.assertEqual(read_exact(peer, int(read_exact(peer, 4), 16)), b'host:track-devices-proto-text')

    def fixture(self, reject=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        board = SimpleNamespace(serial='fixture', label='Board', stable_dir=lambda: directory.name)
        session = AdbSession('adb1234', board, '2-1', '5', {'uid': '1000', 'boot_id': 'example-boot'})
        local, peer = socket.socketpair()
        peer.settimeout(3)
        self.addCleanup(peer.close)
        seen = []

        def read_service():
            return read_exact(peer, int(read_exact(peer, 4), 16)).decode()

        def server():
            seen.append(read_service())
            peer.sendall(b'OKAY')
            command = read_service()
            seen.append(command)
            peer.sendall(b'OKAY')
            if reject:
                peer.sendall(struct.pack('<BI', 3, 1) + bytes([125]))
                return
            marker = re.search(r"printf '%s' ([a-f0-9]{32})", command)[1].encode()
            for data in (marker[:4], marker[4:] + b'shell prompt'):
                peer.sendall(struct.pack('<BI', 1, len(data)) + data)

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        with patch('socket.create_connection', return_value=local):
            connected = session.ensure()
        self.addCleanup(session.close)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen[0], 'host:transport-id:5')
        self.assertIn('shell,v2,TERM=xterm-256color,pty:', seen[1])
        self.assertIn('example-boot', seen[1])
        self.assertIn('fixture', seen[1])
        self.assertEqual(connected, not reject)
        return session, peer

    def test_multiple_shells_input_resize_output_disconnect_and_archive(self):
        first, one = self.fixture()
        second, two = self.fixture()
        for peer in (one, two):
            self.assertEqual(packet(peer), (5, b'32x120,0x0\0'))
        self.assertTrue(first.write('pwd\n'))
        self.assertEqual(packet(one), (0, b'pwd\n'))
        self.assertTrue(second.resize(70, 20))
        self.assertEqual(packet(two), (5, b'20x70,0x0\0'))
        data = b'only-first\n'
        one.sendall(struct.pack('<BI', 1, len(data)) + data + struct.pack('<BI', 3, 1) + b'\0')
        first._thread.join(3)
        self.assertTrue(first.closed)
        self.assertIn(data, first.read(0)[1])
        self.assertIn('only-first', Path(first.logpath).read_text())
        self.assertNotIn(data, second.read(0)[1])
        self.assertTrue(second.snapshot()['alive'])
        self.assertEqual(second.snapshot()['kind'], 'adb')

    def test_identity_rejection_prevents_input(self):
        session, _ = self.fixture(reject=True)
        self.assertFalse(session.write('pwd\n'))
        self.assertTrue(session.closed)
