"""采集器回归测试:设备/系统调用均为替身,绝不打开真实 tty。"""
import errno
import importlib.util
import os
import pathlib
import socketserver
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
from seriald_core import devices as sd
from seriald_core import device as device_module
from seriald_core import transport as tr
from seriald_core.web import BoundedThreadingServer


class SerialIoTests(unittest.TestCase):
    def test_failed_configuration_closes_opened_fd(self):
        identity = types.SimpleNamespace(st_dev=1, st_ino=2, st_rdev=3)
        with mock.patch.object(tr.os, 'open', return_value=98765), \
                mock.patch.object(tr.os, 'fstat', return_value=identity), \
                mock.patch.object(tr.os, 'close') as close, \
                mock.patch.object(tr.termios, 'tcgetattr', side_effect=OSError('gone')), \
                mock.patch.object(tr.termios, 'tcsetattr') as configure:
            with self.assertRaises(OSError):
                tr.open_serial('/fake/ttyUSB0')
        close.assert_called_once_with(98765)
        configure.assert_not_called()

    def test_replaced_inode_is_rejected_before_configuration(self):
        identity = types.SimpleNamespace(st_dev=1, st_ino=22, st_rdev=3)
        with mock.patch.object(tr.os, 'open', return_value=98765), \
                mock.patch.object(tr.os, 'fstat', return_value=identity), \
                mock.patch.object(tr.os, 'close') as close, \
                mock.patch.object(tr.termios, 'tcgetattr') as getattrs:
            with self.assertRaises(OSError) as raised:
                tr.open_serial('/fake/ttyUSB0', identity=(1, 2, 3))
        self.assertEqual(raised.exception.errno, errno.ENODEV)
        close.assert_called_once_with(98765)
        getattrs.assert_not_called()

    def test_short_write_and_retry_preserve_exact_bytes(self):
        replies = iter([InterruptedError(), 2, BlockingIOError(), 1, 3])
        received = bytearray()

        def write(fd, data):
            self.assertEqual(fd, 98765)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            received.extend(bytes(data[:reply]))
            return reply

        with mock.patch.object(tr.os, 'write', side_effect=write), \
                mock.patch.object(tr.select, 'select', return_value=([], [98765], [])):
            tr.write_all(98765, b'abcdef')
        self.assertEqual(received, b'abcdef')

    def test_cancel_prevents_remaining_bytes(self):
        cancel = threading.Event()

        def write(fd, data):
            cancel.set()
            return 2

        with mock.patch.object(tr.os, 'write', side_effect=write) as write_mock:
            with self.assertRaises(OSError) as raised:
                tr.write_all(98765, b'abcdef', cancel=cancel)
        self.assertEqual(raised.exception.errno, errno.ECANCELED)
        self.assertEqual(write_mock.call_count, 1)

    def test_no_progress_and_deadline_fail(self):
        with mock.patch.object(tr.os, 'write', return_value=0):
            with self.assertRaises(OSError) as raised:
                tr.write_all(98765, b'abc')
        self.assertEqual(raised.exception.errno, errno.EIO)
        with mock.patch.object(tr.os, 'write', side_effect=BlockingIOError()), \
                mock.patch.object(tr.select, 'select', return_value=([], [], [])), \
                mock.patch.object(tr.time, 'monotonic', side_effect=[0, 0, 6]):
            with self.assertRaises(OSError) as raised:
                tr.write_all(98765, b'abc', timeout=5)
        self.assertEqual(raised.exception.errno, errno.ETIMEDOUT)

    def test_write_side_opens_only_write_fd_and_never_configures(self):
        identity = types.SimpleNamespace(st_dev=1, st_ino=2, st_rdev=3)
        with mock.patch.object(tr.os, 'open', return_value=98765) as open_fd, \
                mock.patch.object(tr.os, 'fstat', return_value=identity), \
                mock.patch.object(tr.os, 'close') as close, \
                mock.patch.object(tr, 'write_all') as write_all, \
                mock.patch.object(tr.termios, 'tcgetattr') as getattrs, \
                mock.patch.object(tr.termios, 'tcsetattr') as configure:
            ok, error = tr.write_serial('/fake/ttyUSB0', '中文', identity=(1, 2, 3))
        self.assertTrue(ok, error)
        self.assertEqual(open_fd.call_args.args[1] & os.O_ACCMODE, os.O_WRONLY)
        self.assertEqual(write_all.call_args.args, (98765, '中文'.encode()))
        close.assert_called_once_with(98765)
        getattrs.assert_not_called()
        configure.assert_not_called()


class DeviceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.capture-test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.logs = os.path.join(self.temp.name, 'logs')
        self.patchers = [mock.patch.object(sd, 'LOG_ROOT', self.logs),
                         mock.patch.object(device_module, 'LOG_ROOT', self.logs)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.devices = []
        self.addCleanup(self.close_logs)

    def close_logs(self):
        for device in self.devices:
            if device.logf is not None:
                device.logf.close()

    def device(self, key='pci-0000:00:14.0-usb-0:4.2:1.0-port0'):
        device = sd.Device(key, '/fake/ttyUSB0')
        self.devices.append(device)
        return device


class DeviceTests(DeviceFixture):
    def test_empty_log_directory_initializes_all_state(self):
        device = self.device()
        self.assertIsNone(device.logpath)
        self.assertFalse(device.dead)
        self.assertFalse(device.stop.is_set())
        self.assertFalse(device.snapshot()['alive'])
        device.append(b'first output\r\n')
        self.assertIn(b'first output', device.buf)

    def test_stable_directory_distinguishes_interfaces_and_long_keys(self):
        prefix = 'pci-' + 'x' * 80
        keys = [prefix + ':1.0-port0', prefix + ':1.1-port0',
                'pci-0000:00:14.0-usb-0:4.2:1.0-port0',
                'pci-0000:00:14.0-usb-0:4.2:1.1-port0']
        paths = [self.device(key).stable_dir() for key in keys]
        self.assertEqual(len(set(paths)), len(keys))
        for path in paths:
            self.assertEqual(os.path.commonpath([path, self.logs]), self.logs)

    def test_log_open_failure_preserves_live_buffer(self):
        device = self.device()
        with mock.patch.object(device, 'open_log', side_effect=OSError('disk full')) as open_log:
            device.append(b'root@board:~# \r\n')
        open_log.assert_called_once()
        self.assertIn(b'root@board:~# ', device.buf)
        self.assertGreater(device.last_rx, 0)
        self.assertEqual(device.hostname, 'board')
        self.assertIn('disk full', device.snapshot()['log_error'])

    def test_utf8_and_lines_survive_arbitrary_read_boundaries(self):
        device = self.device()
        data = '板子输出正常\r\n'.encode()
        for byte in data:
            device.append(bytes([byte]))
        device.logf.flush()
        contents = pathlib.Path(device.logpath).read_text()
        self.assertIn('板子输出正常', contents)
        self.assertNotIn('\ufffd', contents)
        self.assertEqual(contents.count('板子输出正常'), 1)

    def test_file_transfer_echo_is_redacted_from_archive_only(self):
        device = self.device()
        payload = 'A' * 128
        raw = ('printf %s ' + payload + ' | base64 -d >> /tmp/file\n'
               'SD_DATA=' + payload + '\n').encode()
        device.append(raw)
        self.assertIn(payload.encode(), device.buf)
        archive = pathlib.Path(device.logpath).read_text()
        self.assertNotIn(payload, archive)
        self.assertIn('[base64 omitted]', archive)

    def test_status_markers_do_not_mask_live_prompt_or_claim_device_output(self):
        device = self.device()
        device.append(b'root@board:~# ')
        device.newline = '\r'
        byte_count, last_rx = device.bytes, device.last_rx
        device._say('状态说明')
        with mock.patch.object(device, '_write') as write:
            ok, _ = device.ensure_shell(device.now_off())
        self.assertTrue(ok)
        write.assert_not_called()
        self.assertEqual(device.bytes, byte_count)
        self.assertEqual(device.last_rx, last_rx)

    def test_local_marker_cannot_split_live_utf8_bytes(self):
        device = self.device()
        raw = '中文\n'.encode()
        device.append(raw[:1])
        device._say('操作状态')
        device.append(raw[1:])
        self.assertEqual(bytes(device.buf), raw)
        self.assertIn('中文', pathlib.Path(device.logpath).read_text())

    def test_preloaded_history_cannot_impersonate_current_prompt(self):
        device = self.device()
        path = pathlib.Path(device.stable_dir()) / 'serial-20260919.log'
        path.write_text('2026-09-19 00:00:00  root@old-board:~# \n')
        replacement = self.device()
        # Unidentified connections must not inherit any previous board's output.
        self.assertNotIn(b'root@old-board', replacement.buf)
        with mock.patch.object(replacement, '_write', return_value=(True, '')) as write, \
                mock.patch.object(replacement, '_wait', return_value=None):
            ok, _ = replacement.ensure_shell(replacement.now_off())
        self.assertFalse(ok)
        self.assertEqual(write.call_args_list, [mock.call('\r'), mock.call('\n')])

    def test_preloaded_history_uses_terminal_line_boundaries(self):
        record = {'serial': 'SN-HISTORY', 'label': 'history-board',
                  'info': {'serial': 'SN-HISTORY'}, 'last_path': 'old',
                  'last_dev': '/fake/ttyUSB0'}
        device = sd.Device('pci-history', '/fake/ttyUSB0', record=record)
        self.devices.append(device)
        path = pathlib.Path(device.stable_dir()) / 'serial-20260919.log'
        path.write_bytes(b'2026-09-19 00:00:00  first\n2026-09-19 00:00:01  second\n')
        device.preload()
        self.assertIn(b'first\r\n2026-09-19', bytes(device.buf))
        self.assertNotIn(b'first\n2026-09-19', bytes(device.buf))

    def test_log_rotates_when_date_changes(self):
        device = self.device()
        with mock.patch.object(sd.time, 'strftime', return_value='20260919'):
            device.append(b'first day\n')
        first_log = device.logf
        with mock.patch.object(sd.time, 'strftime', return_value='20260920'):
            device.append(b'next day\n')
        self.assertTrue(first_log.closed)
        self.assertTrue(device.logpath.endswith('serial-20260920.log'))

    def test_probe_gate_excludes_manual_console_writes(self):
        device = self.device()
        device.fd = 98765
        device.present = True
        device.identity = (1, 2, 3)
        entered = threading.Event()
        release = threading.Event()

        def hold_probe_gate():
            with device.operation_lock:
                entered.set()
                release.wait(2)

        holder = threading.Thread(target=hold_probe_gate)
        holder.start()
        try:
            self.assertTrue(entered.wait(1))
            with mock.patch.object(device_module, 'write_serial') as write:
                ok, _ = device.send('must not interleave')
            self.assertFalse(ok)
            write.assert_not_called()
        finally:
            release.set()
            holder.join(2)
            device.fd = None
        self.assertFalse(holder.is_alive())


class ManagerTests(DeviceFixture):
    # 复用纯磁盘 fixture,所有线程/tty 打开均替换为无副作用对象。
    def test_aliases_for_same_character_device_start_only_one_reader(self):
        manager = sd.Manager()
        identity = types.SimpleNamespace(st_dev=1, st_ino=2, st_rdev=3,
                                         st_mode=stat.S_IFCHR | 0o600)
        listdir = os.listdir

        def discover(path):
            return ['usb-z-alias', 'usb-a-anchor'] if path == sd.BY_PATH else listdir(path)

        with mock.patch.object(sd.os, 'listdir', side_effect=discover), \
                mock.patch.object(sd.os.path, 'realpath', return_value='/fake/ttyUSB0'), \
                mock.patch.object(sd.os, 'stat', return_value=identity), \
                mock.patch.object(sd.threading, 'Thread') as thread:
            manager.scan()
            manager.scan()
        self.devices.extend(manager.devices.values())
        self.assertEqual(len(manager.devices), 1)
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.return_value.start.call_count, 1)

    def test_reused_tty_waits_for_old_reader_to_finish(self):
        manager = sd.Manager()
        old = self.device('usb-old')
        old.present, old.identity = True, (1, 2, 3)
        old.thread = mock.Mock()
        old.thread.is_alive.return_value = True
        manager.devices[old.by_path] = old
        identity = types.SimpleNamespace(st_dev=1, st_ino=22, st_rdev=3,
                                         st_mode=stat.S_IFCHR | 0o600)
        listdir = os.listdir

        def discover(path):
            return ['usb-new'] if path == sd.BY_PATH else listdir(path)

        with mock.patch.object(sd.os, 'listdir', side_effect=discover), \
                mock.patch.object(sd.os.path, 'realpath', return_value='/fake/ttyUSB0'), \
                mock.patch.object(sd.os, 'stat', return_value=identity), \
                mock.patch.object(sd.threading, 'Thread') as thread:
            manager.scan()
            self.assertTrue(old.stop.is_set())
            self.assertFalse(old.present)
            thread.assert_not_called()
            old.thread.is_alive.return_value = False
            manager.scan()
            self.assertEqual(thread.call_count, 1)
        self.devices.extend(d for d in manager.devices.values() if d is not old)
        self.assertEqual(manager.devices['usb-new'].identity, (1, 22, 3))
        self.assertEqual(old.dev, '/fake/ttyUSB0')

    def test_reenumeration_with_same_anchor_retires_thread_before_replacement(self):
        manager = sd.Manager()
        old = self.device('usb-anchor')
        old.present, old.identity = True, (1, 2, 3)
        old.thread = mock.Mock()
        old.thread.is_alive.return_value = True
        old_thread = old.thread
        manager.devices[old.by_path] = old
        identity = types.SimpleNamespace(st_dev=1, st_ino=22, st_rdev=33,
                                         st_mode=stat.S_IFCHR | 0o600)
        listdir = os.listdir

        def discover(path):
            return ['usb-anchor'] if path == sd.BY_PATH else listdir(path)

        with mock.patch.object(sd.os, 'listdir', side_effect=discover), \
                mock.patch.object(sd.os.path, 'realpath', return_value='/fake/ttyUSB1'), \
                mock.patch.object(sd.os, 'stat', return_value=identity), \
                mock.patch.object(sd.threading, 'Thread') as thread:
            manager.scan()
            self.assertEqual(old.dev, '/fake/ttyUSB0')
            thread.assert_not_called()
            old_thread.is_alive.return_value = False
            manager.scan()
            self.assertEqual(thread.call_count, 1)
        replacement = manager.devices['usb-anchor']
        self.assertIsNot(replacement, old)
        self.assertEqual(replacement.dev, '/fake/ttyUSB1')
        self.assertEqual(replacement.identity, (1, 22, 33))
        self.assertTrue(old.stop.is_set())

    def test_eof_closes_reader_and_backs_off(self):
        manager = sd.Manager()
        device = self.device()
        device.present, device.identity = True, (1, 2, 3)
        with mock.patch.object(manager, '_matches', return_value=True), \
                mock.patch.object(sd, 'open_serial', return_value=98765) as open_reader, \
                mock.patch.object(sd.select, 'select', return_value=([98765], [], [])), \
                mock.patch.object(sd.os, 'read', return_value=b'') as read, \
                mock.patch.object(sd.os, 'close') as close, \
                mock.patch.object(device, '_say'), \
                mock.patch.object(device.stop, 'wait', side_effect=lambda timeout: device.stop.set()) as wait:
            manager._capture(device)
        self.assertEqual(open_reader.call_count, 1)
        self.assertEqual(read.call_count, 1)
        close.assert_called_once_with(98765)
        wait.assert_called_once_with(1.0)
        self.assertIsNone(device.fd)
        self.assertTrue(device.dead)

    def test_old_operation_finishes_before_reactivation_and_metadata_reset(self):
        manager = sd.Manager()
        device = self.device('usb-anchor')
        device.present, device.identity = True, (1, 2, 3)
        device.hostname = 'old-board'
        device.probe_info = {'ips': ['eth0 192.0.2.1/24']}
        device.thread = mock.Mock()
        device.thread.is_alive.return_value = False
        old_stop = device.stop
        manager.devices[device.by_path] = device
        identity = types.SimpleNamespace(st_dev=1, st_ino=22, st_rdev=33,
                                         st_mode=stat.S_IFCHR | 0o600)
        listdir = os.listdir

        def discover(path):
            return ['usb-anchor'] if path == sd.BY_PATH else listdir(path)

        with mock.patch.object(sd.os, 'listdir', side_effect=discover), \
                mock.patch.object(sd.os.path, 'realpath', return_value='/fake/ttyUSB1'), \
                mock.patch.object(sd.os, 'stat', return_value=identity), \
                mock.patch.object(sd.threading, 'Thread') as thread:
            device.operation_lock.acquire()
            try:
                manager.scan()
                thread.assert_not_called()
                self.assertIs(device.stop, old_stop)
                self.assertTrue(old_stop.is_set())
                self.assertFalse(device.present)
                self.assertEqual(device.identity, (1, 2, 3))
            finally:
                device.operation_lock.release()
            manager.scan()
        self.assertEqual(thread.call_count, 1)
        replacement = manager.devices['usb-anchor']
        self.assertIsNot(replacement, device)
        self.assertFalse(replacement.stop.is_set())
        self.assertEqual(replacement.identity, (1, 22, 33))
        self.assertEqual(replacement.hostname, '')
        self.assertEqual(replacement.probe_info, {})


class BoundedServerTests(unittest.TestCase):
    def server(self):
        with mock.patch.object(socketserver.ThreadingTCPServer, '__init__', return_value=None):
            return BoundedThreadingServer(('unused', 0), object)

    def test_connection_capacity_precedes_thread_creation(self):
        server = self.server()
        sockets = [mock.Mock() for _ in range(server.max_workers + 1)]
        with mock.patch.object(socketserver.ThreadingTCPServer, 'process_request') as spawn, \
                mock.patch.object(server, 'shutdown_request') as shutdown:
            for request in sockets:
                server.process_request(request, ('unused', 0))
        self.assertEqual(spawn.call_count, server.max_workers)
        shutdown.assert_called_once_with(sockets[-1])
        sockets[0].settimeout.assert_called_once_with(server.request_timeout)
        sockets[-1].settimeout.assert_not_called()

    def test_worker_exit_releases_capacity_even_after_failure(self):
        server = self.server()
        for _ in range(server.max_workers):
            self.assertTrue(server._workers.acquire(blocking=False))
        with mock.patch.object(socketserver.ThreadingTCPServer, 'process_request_thread',
                               side_effect=RuntimeError('handler failed')):
            with self.assertRaises(RuntimeError):
                server.process_request_thread(mock.Mock(), ('unused', 0))
        self.assertTrue(server._workers.acquire(blocking=False))
        self.assertFalse(server._workers.acquire(blocking=False))

    def test_thread_creation_failure_returns_slot_and_closes_socket(self):
        server = self.server()
        request = mock.Mock()
        with mock.patch.object(socketserver.ThreadingTCPServer, 'process_request',
                               side_effect=RuntimeError('thread failed')), \
                mock.patch.object(server, 'shutdown_request') as shutdown:
            with self.assertRaises(RuntimeError):
                server.process_request(request, ('unused', 0))
        shutdown.assert_called_once_with(request)
        for _ in range(server.max_workers):
            self.assertTrue(server._workers.acquire(blocking=False))
        self.assertFalse(server._workers.acquire(blocking=False))


if __name__ == '__main__':
    unittest.main()
