"""The same observable session/log contracts on both SSH IO adapters."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import paramiko

from seriald_core.sessions_windows import SshSession as WindowsSession
from seriald_core.session_base import relocate_session_logs
from seriald_core.archive import stage_logs, finish_staged
from seriald_core.device import Device
from seriald_core.devices import Manager
from seriald_core.storage import Store, IdentityError
from seriald_core.config import SCROLLBACK

ADAPTERS = [WindowsSession]
if sys.platform != 'win32':
    from seriald_core.sessions import SshSession as LinuxSession
    ADAPTERS.append(LinuxSession)


class SessionContracts(unittest.TestCase):
    def session(self, adapter, directory):
        session = adapter('ssh-contract', '192.0.2.1', 'board', directory)
        # Real log files; fake transport hooks, never a PTY/process/network.
        for hook in ('_dispose_transport', '_interrupt_transport'):
            patcher = mock.patch.object(session, hook)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(session.close)
        return session

    def test_reader_eof_audits_once_and_never_restarts(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__module__), tempfile.TemporaryDirectory() as root:
                session = self.session(adapter, root)
                session.store = mock.Mock()
                with mock.patch.object(session, '_open_transport', return_value=object()) as opening, \
                        mock.patch.object(session, '_receive', side_effect=[b'hello\n', b'']):
                    self.assertTrue(session.ensure())
                    session._thread.join(timeout=3)
                    self.assertFalse(session._thread.is_alive())
                    self.assertFalse(session.ensure())
                    self.assertFalse(session.write('must not restart'))
                    session.close()
                    session.close()
                opening.assert_called_once()
                session._dispose_transport.assert_called_once()
                session.store.close_session.assert_called_once_with(session.sid)
                self.assertIn(b'hello\n', session.read(0)[1])
                self.assertFalse(session.snapshot()['alive'])
                self.assertIn('hello', Path(session.logpath).read_text())

    def test_relocation_preserves_split_utf8_partial_line_and_concurrent_output(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__module__), tempfile.TemporaryDirectory() as root:
                source, target = Path(root) / 'old', Path(root) / 'confirmed'
                session = self.session(adapter, str(source))
                session.fd = object()
                character = '汉'.encode()
                session._append(b'before\n' + '半'.encode() + character[:1])
                entered = threading.Event()

                def write():
                    entered.set()
                    session._append(character[1:] + b'after\n')

                with relocate_session_logs([session], str(target)) as relocation:
                    writer = threading.Thread(target=write)
                    writer.start()
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(session.read(0)[2], 'log migration must not close transport')
                    staged = stage_logs(str(source), str(target))
                    relocation.commit()
                    finish_staged(staged, [str(source)])
                writer.join(timeout=3)
                self.assertFalse(writer.is_alive())
                session.close()
                text = Path(session.logpath).read_text()
                self.assertIn('before', text)
                self.assertIn('半汉after', text)
                self.assertNotIn('\ufffd', text)
                self.assertEqual(Path(session.logpath).parent, target)
                self.assertFalse(source.exists())

    def test_uncommitted_relocation_resumes_original_log(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__module__), tempfile.TemporaryDirectory() as root:
                source, target = Path(root) / 'old', Path(root) / 'new'
                session = self.session(adapter, str(source))
                session._append('before\n')
                with self.assertRaisesRegex(RuntimeError, 'database failed'):
                    with relocate_session_logs([session], str(target)):
                        stage_logs(str(source), str(target))
                        raise RuntimeError('database failed')
                session._append('after rollback\n')
                session.close()
                self.assertEqual(Path(session.logpath).parent, source)
                self.assertIn('after rollback', Path(session.logpath).read_text())

    def test_shared_stream_has_independent_offsets_and_bounded_history(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__module__), tempfile.TemporaryDirectory() as root:
                session = self.session(adapter, root)
                session.fd = object()
                payload = b'x' * (SCROLLBACK + 73)
                session._append(payload)
                off, data, alive = session.read(0)
                self.assertTrue(alive)
                self.assertEqual(off, len(payload))
                self.assertEqual(data, payload[-SCROLLBACK:])
                self.assertEqual(session.read(off - 10, wait=0)[1], b'x' * 10)
                self.assertEqual(session.read(off, wait=0)[1], b'')
                self.assertEqual(session.read(0)[1], data)
                session.close()

    def test_unchanged_archive_does_not_reopen_its_handle(self):
        with tempfile.TemporaryDirectory() as root:
            session = self.session(WindowsSession, root)
            handle = session.logf
            with relocate_session_logs([session], root) as relocation:
                relocation.commit()
            self.assertIs(session.logf, handle)
            session.close()


class IdentitySessionMigration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        for module in ('seriald_core.device', 'seriald_core.devices'):
            patcher = mock.patch(module + '.LOG_ROOT', self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.serial = '0059D968E583C49A'
        record = self.store.bind({'serial': self.serial, 'hostname': 'board',
                                  'serial_source': 'soc-sysfs', 'identity_status': 'provisional'},
                                 'fake-usb', '/fake/tty')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.device = Device('fake-usb', '/fake/tty', self.manager,
                             board=self.manager.boards[self.serial])
        self.device.board.attach(self.device)
        self.device.present = True
        self.manager.devices['fake-usb'] = self.device
        self.session = WindowsSession('ssh-identity', '192.0.2.1', 'board', self.device.stable_dir())
        self.device.ssh_sessions[self.session.sid] = self.session
        self.session._append('before migration\n')
        self.fields = {'serial': self.serial, 'eeprom_serial': self.serial, 'hostname': 'board'}

    def test_identity_promotion_relocates_live_session_writer(self):
        original = self.session.logpath
        self.manager.identify(self.device, self.fields)
        self.session._append('after migration\n')
        self.assertNotEqual(original, self.session.logpath)
        self.assertFalse(Path(original).exists())
        self.assertEqual(self.session._logdir, self.device.stable_dir())
        text = Path(self.session.logpath).read_text()
        self.assertIn('before migration', text)
        self.assertIn('after migration', text)
        self.assertIs(self.device.get_session(self.session.sid), self.session)

    def test_database_failure_preserves_original_writer_and_identity(self):
        original = self.session.logpath
        with mock.patch.object(self.store, 'bind', side_effect=RuntimeError('database failed')):
            with self.assertRaises(RuntimeError):
                self.manager.identify(self.device, self.fields)
        self.session._append('after rollback\n')
        self.assertEqual(self.session.logpath, original)
        self.assertEqual(self.store.get(self.serial)['info']['identity_status'], 'provisional')
        self.assertIn('after rollback', Path(original).read_text())

    def test_cleanup_failure_keeps_committed_writer_at_destination(self):
        with mock.patch('seriald_core.devices.finish_staged', side_effect=OSError('cleanup failed')):
            with self.assertRaises(OSError):
                self.manager.identify(self.device, self.fields)
        self.session._append('after committed cleanup failure\n')
        self.assertEqual(self.store.get(self.serial)['info']['identity_status'], 'confirmed')
        self.assertEqual(Path(self.session.logpath).parent, Path(self.root) / self.serial)
        self.assertIn('after committed cleanup failure', Path(self.session.logpath).read_text())

    def test_pending_opener_blocks_migration_before_database_or_handle_changes(self):
        handle = self.session.logf
        self.device.ssh_pending = 1
        try:
            with self.assertRaises(IdentityError):
                self.manager.identify(self.device, self.fields)
            self.assertIs(self.session.logf, handle)
            self.assertFalse(handle.closed)
            self.assertEqual(self.store.get(self.serial)['info']['identity_status'], 'provisional')
        finally:
            self.device.ssh_pending = 0

    def test_boot_marker_promotion_moves_sessions_logs_and_persisted_history(self):
        marker = 'boot-11111111-2222-4333-8444-555555555555'
        self.store.bind({'serial': marker, 'hostname': 'old-board', 'serial_source': 'boot-marker',
                         'identity_status': 'provisional'}, 'fake-usb', '/fake/tty')
        manager = Manager(self.store)
        self.addCleanup(manager.close)
        old_board = manager.boards[marker]
        old_directory = Path(self.root) / old_board.archive_path
        session = WindowsSession('ssh-marker', '192.0.2.1', 'old-board', str(old_directory))
        session.store = self.store
        self.store.open_session(session.sid, marker, 'agent', 'diagnosis', session.ip)
        old_board.sessions.sessions[session.sid] = session
        session._append('before EEPROM provisioning\n')
        new_serial = 'ONIE-DEMO-00001'
        connection = Device('fake-usb', '/fake/tty', manager)
        connection.present = True
        manager.devices['fake-usb'] = connection
        manager.identify(connection, {'serial': new_serial, 'eeprom_serial': new_serial,
                                      'boot_marker': marker, 'hostname': 'new-board'})
        session._append('after EEPROM provisioning\n')
        self.assertNotIn(marker, manager.boards)
        self.assertIsNone(self.store.get(marker))
        self.assertIs(connection.get_session(session.sid), session)
        self.assertEqual(self.store.sessions(new_serial)[0]['sid'], session.sid)
        self.assertEqual(Path(session.logpath).parent, Path(self.root) / new_serial)
        text = Path(session.logpath).read_text()
        self.assertIn('before EEPROM provisioning', text)
        self.assertIn('after EEPROM provisioning', text)
        self.assertFalse(old_directory.exists())


class WindowsAdapterContracts(unittest.TestCase):
    def session(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = WindowsSession('ssh-windows', '192.0.2.1', 'board', temporary.name)
        session.store = mock.Mock()
        self.addCleanup(session.close)
        return session

    def test_shell_failure_closes_client_and_audits_once(self):
        session = self.session()
        client = mock.Mock()
        client.invoke_shell.side_effect = paramiko.SSHException('shell denied')
        with mock.patch('seriald_core.sessions_windows._connect', return_value=client) as connect:
            self.assertFalse(session.ensure())
            self.assertFalse(session.ensure())
            session.close()
        connect.assert_called_once()
        client.close.assert_called_once()
        session.store.close_session.assert_called_once()
        self.assertIn('shell denied', session.snapshot()['transport_error'])

    def test_timeout_setup_failure_closes_channel_and_client(self):
        session = self.session()
        client, channel = mock.Mock(), mock.Mock()
        client.invoke_shell.return_value = channel
        channel.settimeout.side_effect = OSError('channel gone')
        with mock.patch('seriald_core.sessions_windows._connect', return_value=client):
            self.assertFalse(session.ensure())
        channel.close.assert_called_once()
        client.close.assert_called_once()
        self.assertTrue(session._disposed.is_set())

    def test_close_interrupts_reader_and_resources_are_disposed_once(self):
        session = self.session()
        client, channel = mock.Mock(), mock.Mock()
        client.invoke_shell.return_value = channel
        entered, release = threading.Event(), threading.Event()

        def receive(count):
            entered.set()
            release.wait(5)
            return b''

        channel.recv.side_effect = receive
        channel.close.side_effect = release.set
        with mock.patch('seriald_core.sessions_windows._connect', return_value=client):
            self.assertTrue(session.ensure())
            try:
                self.assertTrue(entered.wait(2))
                session.close()
            finally:
                release.set()
                session._thread.join(timeout=3)
        self.assertFalse(session._thread.is_alive())
        channel.close.assert_called_once()
        client.close.assert_called_once()
        session.store.close_session.assert_called_once()

    def test_channel_cleanup_failure_still_closes_client_and_reports_error(self):
        session = self.session()
        client, channel = mock.Mock(), mock.Mock()
        session.client, session.fd = client, channel
        channel.close.side_effect = OSError('channel close failed')
        session._finish()
        self.assertTrue(session._disposed.is_set())
        client.close.assert_called_once()
        session.store.close_session.assert_called_once()
        self.assertIn('channel close failed', session.snapshot()['transport_error'])


if __name__ == '__main__':
    unittest.main()
