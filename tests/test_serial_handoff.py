"""Collector handoff exercises real reader loops against fake host handles."""
import threading
import time
import unittest
from unittest import mock
from contextlib import ExitStack

from seriald_core.device import Device
from seriald_core.devices import Manager
from seriald_core.hardware import HardwareBook, run_command
from seriald_core.tasks import TaskCancelled
from seriald_core.windows_manager import WindowsManager


class HandoffTests(unittest.TestCase):
    def fixture(self, windows=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        manager = WindowsManager() if windows else Manager()
        device = Device('fixture', 'fake-port', manager)
        device.present = True
        manager.devices['fixture'] = device
        manager._matches = mock.Mock(return_value=True)
        device._say = mock.Mock()
        device.close_log = mock.Mock()
        opened, closed = [], []
        if windows:
            class Handle:
                in_waiting = 0
                is_open = True
                def read(self, size):
                    time.sleep(0.01)
                    return b''
                def close(self):
                    self.is_open = False
                    closed.append(self)
            def open_handle(*args, **kwargs):
                value = Handle()
                opened.append(value)
                return value
            stack.enter_context(mock.patch('seriald_core.windows_manager.serial.Serial', side_effect=open_handle))
        else:
            def open_handle(*args, **kwargs):
                value = 100 + len(opened)
                opened.append(value)
                return value
            stack.enter_context(mock.patch('seriald_core.devices.open_serial', side_effect=open_handle))
            stack.enter_context(mock.patch('seriald_core.devices.os.close', side_effect=closed.append))
            def select(*args):
                time.sleep(0.01)
                return [], [], []
            stack.enter_context(mock.patch('seriald_core.devices.select.select', side_effect=select))
        device.last_health_check = time.time()
        device.thread = threading.Thread(target=manager._capture, args=(device,))
        device.thread.start()
        def cleanup():
            device.stop.set()
            device.thread.join(2)
            self.assertFalse(device.thread.is_alive())
        self.addCleanup(cleanup)
        self.wait(lambda: len(opened) == 1)
        return manager, device, opened, closed

    def wait(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() > deadline:
                self.fail('fixture reader did not settle')
            time.sleep(0.01)

    def test_both_collectors_release_before_helper_and_resume_same_connection(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                manager, device, opened, closed = self.fixture(windows)
                connection, stop = device.connection_id, device.stop
                with manager.exclusive_serial(device, lambda: None):
                    self.assertEqual(len(closed), 1)
                    self.assertIsNone(device.fd)
                    self.assertFalse(device.thread.is_alive())
                    self.assertFalse(stop.is_set())
                    self.assertTrue(device.present)
                    device.close_log.assert_not_called()
                    manager._discover = mock.Mock(return_value={'fixture': (device.dev, device.identity)})
                    manager.scan()
                    self.assertEqual(len(opened), 1)
                self.wait(lambda: len(opened) == 2)
                self.assertEqual(device.connection_id, connection)
                self.assertIs(device.stop, stop)

    def test_cancelled_helper_resumes_after_cleanup(self):
        manager, device, opened, closed = self.fixture()
        process = mock.Mock(returncode=0)
        process.poll.return_value = None
        with self.assertRaises(TaskCancelled):
            with manager.exclusive_serial(device, lambda: None):
                with mock.patch('seriald_core.hardware.helper_process', return_value=process):
                    calls = iter([None, TaskCancelled('cancel')])
                    def check():
                        value = next(calls)
                        if value:
                            raise value
                    run_command(['fake'], 1, check)
        process.close.assert_called_once()
        self.wait(lambda: len(opened) == 2)

    def test_cleanup_failure_keeps_port_quarantined(self):
        manager, device, opened, closed = self.fixture()
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        process.close.side_effect = OSError('cleanup failed')
        with self.assertRaises(OSError):
            with manager.exclusive_serial(device, lambda: None) as permit:
                with mock.patch('seriald_core.hardware.helper_process', return_value=process):
                    run_command(['fake'], 1, lambda: None, lambda: permit.update(resume=False))
        self.assertTrue(device.capture_paused.is_set())
        self.assertIsNone(device.fd)
        self.assertEqual(len(opened), 1)
        manager._discover = mock.Mock(return_value={'fixture': (device.dev, device.identity)})
        manager.scan()
        self.assertEqual(len(opened), 1)

    def test_disconnect_during_handoff_does_not_reopen_old_reader(self):
        manager, device, opened, closed = self.fixture()
        with manager.exclusive_serial(device, lambda: None):
            manager._discover = mock.Mock(return_value={})
            manager.scan()
        self.assertTrue(device.stop.is_set())
        self.assertEqual(len(opened), 1)

    def test_changed_host_identity_does_not_resume_old_reader(self):
        manager, device, opened, closed = self.fixture()
        with manager.exclusive_serial(device, lambda: None):
            manager._matches.return_value = False
        self.assertEqual(len(opened), 1)
        self.assertFalse(device.capture_paused.is_set())

    def test_pause_interrupts_collector_open_backoff(self):
        device = Device('fixture', 'fake-port')
        thread = threading.Thread(target=Manager._capture_wait, args=(device, 60))
        thread.start()
        try:
            device.capture_paused.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertFalse(device.stop.is_set())
        finally:
            device.stop.set()
            thread.join(1)

    def test_cancel_before_helper_restores_collector(self):
        manager, device, opened, closed = self.fixture()
        count = 0
        def check():
            nonlocal count
            count += 1
            if count > 1:
                raise TaskCancelled('cancel before helper')
        with self.assertRaises(TaskCancelled):
            with manager.exclusive_serial(device, check):
                self.fail('cancelled handoff launched helper')
        self.wait(lambda: device.fd is not None)
        self.assertFalse(device.capture_paused.is_set())

    def test_explicit_boolean_configuration(self):
        import sys
        book = HardwareBook({'boards': {'SN': {'flash': {'argv': [sys.executable], 'exclusive_serial': True}}}})
        self.assertTrue(book.command('SN', 'flash', 'web').exclusive_serial)
        with self.assertRaises(ValueError):
            HardwareBook({'boards': {'SN': {'flash': {'argv': [sys.executable], 'exclusive_serial': 1}}}})
