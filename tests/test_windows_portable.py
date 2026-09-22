"""Windows transport contracts exercised with fake COM/SSH devices on any host."""
import tempfile
import threading
import types
import unittest
from unittest import mock
from urllib.request import urlopen
import json

import serial

from seriald_core import device as device_module
from seriald_core import control_server, handoff, web
from seriald_core import windows_manager
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.sessions_windows import SshSession, check_password_login
from seriald_core.storage import Store


def port(device='COM7', location='1-4.2', serial_number='adapter-1'):
    return types.SimpleNamespace(device=device, location=location, interface='UART',
                                 vid=0x10c4, pid=0xea60, serial_number=serial_number,
                                 hwid='USB VID:PID=10C4:EA60')


class WindowsPortableTests(unittest.TestCase):
    def test_clean_windows_handoff_allows_clock_rounding(self):
        with tempfile.TemporaryDirectory() as root:
            path = root + '/handoff.json'
            connections = [{'by_path': 'usb-1', 'dev': 'COM7', 'serial': 'SN1',
                            'identity': [1, 2, 3]}]
            with mock.patch.object(handoff, '_boot_id', side_effect=['windows:100', 'windows:104']), \
                    mock.patch.object(handoff.sys, 'platform', 'win32'):
                handoff.write(path, connections)
                self.assertEqual(handoff.consume(path), connections)

    def test_loopback_collector_serves_shared_control_api(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(windows_manager.list_ports, 'comports', return_value=[]):
            store = Store(root + '/registry.sqlite3')
            original = web.MGR
            web.MGR = windows_manager.WindowsManager(store)
            try:
                with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                        BoundedThreadingTCPServer(('127.0.0.1', 0), control_server.ControlHandler) as control:
                    with mock.patch.object(control_server.ControlHandler, 'collector_socket',
                                           collector.server_address), \
                            mock.patch.object(control_server.ControlHandler, 'auth_token', ''):
                        workers = [threading.Thread(target=server.serve_forever, daemon=True)
                                   for server in (collector, control)]
                        for worker in workers:
                            worker.start()
                        try:
                            url = 'http://127.0.0.1:%d' % control.server_address[1]
                            with urlopen(url + '/healthz') as response:
                                self.assertTrue(json.load(response)['ok'])
                            with urlopen(url + '/api/devices') as response:
                                self.assertEqual(json.load(response)['devices'], [])
                        finally:
                            for server in (collector, control):
                                server.shutdown()
                            for worker in workers:
                                worker.join(timeout=3)
            finally:
                web.MGR = original
                store.close()

    def test_usb_location_survives_com_renumbering(self):
        first, second = port(), port(device='COM12')
        self.assertEqual(windows_manager.port_key(first), windows_manager.port_key(second))
        self.assertEqual(windows_manager.port_identity(first), windows_manager.port_identity(second))
        with mock.patch.object(windows_manager.list_ports, 'comports', return_value=[first]):
            manager = windows_manager.WindowsManager()
            found = manager._discover()
            self.assertEqual(found['usb-1-4.2-UART'][0], 'COM7')
            device = device_module.Device('usb-1-4.2-UART', 'COM7', manager)
            device.identity = found[device.by_path][1]
            self.assertTrue(manager._matches(device))
        with mock.patch.object(windows_manager.list_ports, 'comports', return_value=[second]):
            self.assertFalse(manager._matches(device))

    def test_com_fallback_and_unique_adapter_identity(self):
        first = port(location='', serial_number='same')
        second = port(location='', serial_number='different')
        self.assertEqual(windows_manager.port_key(first), 'COM7')
        self.assertNotEqual(windows_manager.port_identity(first), windows_manager.port_identity(second))

    def test_ambiguous_multiport_adapter_uses_com_anchors(self):
        first, second = port('COM7'), port('COM12')
        with mock.patch.object(windows_manager.list_ports, 'comports', return_value=[first, second]):
            manager = windows_manager.WindowsManager()
            self.assertEqual(set(manager._discover()), {'COM7', 'COM12'})

    def test_com_capture_and_write_share_one_handle(self):
        with tempfile.TemporaryDirectory() as root:
            manager = windows_manager.WindowsManager()
            device = device_module.Device('usb-1-4.2-UART', 'COM7', manager)
            device.identity, device.present = (1, 2, 3), True
            handle = mock.Mock()
            handle.in_waiting = 5
            def read(_):
                device.stop.set()
                return b'hello'
            handle.read.side_effect = read
            handle.write.side_effect = lambda data: len(data)
            with mock.patch.object(device_module, 'LOG_ROOT', root), \
                    mock.patch.object(manager, '_matches', return_value=True), \
                    mock.patch.object(windows_manager.serial, 'Serial', return_value=handle):
                manager._capture(device)
            self.assertIn(b'hello', device.buf)
            self.assertIsNone(device.fd)
            handle.close.assert_called_once()

    def test_com_write_rejects_short_write(self):
        device = device_module.Device('usb-1', 'COM7')
        device.present = True
        device.fd = mock.Mock()
        device.fd.write.return_value = 1
        with mock.patch.object(device_module.sys, 'platform', 'win32'), \
                mock.patch.object(device_module, 'SerialException', serial.SerialException, create=True):
            self.assertFalse(device._write('hello')[0])

    def test_ssh_auth_failure_is_reported_as_denied(self):
        with mock.patch('seriald_core.sessions_windows._connect',
                        side_effect=__import__('paramiko').AuthenticationException()):
            self.assertEqual(check_password_login('127.0.0.1', 'root', 'wrong'),
                             (False, True, 'SSH 认证失败'))

    def test_ssh_session_byte_stream_and_log(self):
        with tempfile.TemporaryDirectory() as root:
            session = SshSession('ssh123', '127.0.0.1', 'board', root)
            session.fd = mock.Mock()
            session._append(b'root@board:~# echo ok\r\nok\r\n')
            off, data, alive = session.read(0)
            self.assertTrue(alive)
            self.assertIn(b'echo ok', data)
            self.assertEqual(off, len(data))
            self.assertTrue(session.write('ls\r'))
            session.fd.sendall.assert_called_once_with(b'ls\r')
            self.assertTrue(session.resize(100, 30))
            session.fd.resize_pty.assert_called_once_with(width=100, height=30)
            session.close()


if __name__ == '__main__':
    unittest.main()
