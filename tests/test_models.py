"""Persistent boards, replaceable connections and independent session ownership."""
import os
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.device import Device
from seriald_core.devices import Manager
from seriald_core.models import Board, SessionRegistry
from seriald_core.storage import IdentityError, Store


class ModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        for module in ('seriald_core.device', 'seriald_core.devices'):
            patcher = mock.patch(module + '.LOG_ROOT', os.path.join(self.root, 'logs'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.record = self.store.bind({'serial': 'MODEL-SN1', 'hostname': 'board-one',
                                       'serial_source': 'device-tree',
                                       'identity_status': 'confirmed', 'ips': ['eth0 192.0.2.1/24']},
                                      'usb-a', '/fake/tty0')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.board = self.manager.boards['MODEL-SN1']

    def connection(self, key='usb-a'):
        connection = Device(key, '/fake/tty', self.manager, board=self.board)
        self.board.attach(connection)
        connection.present = True
        self.manager.devices[key] = connection
        return connection

    def test_registry_holds_board_without_allocating_a_live_connection(self):
        self.assertIsInstance(self.board, Board)
        self.assertIsNone(self.board.connection)
        history = self.manager.resolve('board-one')
        self.assertIs(history.board, self.board)
        self.assertIsNone(history.fd)
        self.assertFalse(history.present)
        self.assertEqual(history.stable_dir(), os.path.join(self.root, 'logs', 'MODEL-SN1'))

    def test_board_and_session_catalog_survive_connection_replacement(self):
        original = self.connection()
        session = mock.Mock(sid='ssh1', closed=False)
        original.ssh_sessions['ssh1'] = session
        original.present = False
        original.stop.set()
        replacement = self.connection()
        self.assertIsNot(original, replacement)
        self.assertNotEqual(original.connection_id, replacement.connection_id)
        self.assertIs(replacement.board, self.board)
        self.assertIs(self.board.connection, replacement)
        self.assertIs(replacement.get_session('ssh1'), session)
        self.assertIs(original._sessions, replacement._sessions)
        self.assertIs(self.manager.resolve('board-one'), replacement)

    def test_two_online_connections_cannot_attach_to_one_board(self):
        original = self.connection()
        other = Device('usb-b', '/fake/other', self.manager)
        with self.assertRaises(IdentityError):
            self.board.attach(other)
        self.assertIs(self.board.connection, original)
        self.assertIsNone(other.board)

    def test_real_scan_reuses_board_without_copying_or_clearing_ssh_sessions(self):
        original = self.connection()
        original.identity = (1, 10, 20)
        original.stop.set()
        original.present = False
        session = mock.Mock(sid='ssh-kept')
        original.ssh_sessions[session.sid] = session
        with mock.patch.object(self.manager, '_discover',
                               return_value={'usb-a': ('/fake/tty-renumbered', (1, 11, 20))}), \
                mock.patch.object(self.manager, '_capture'):
            self.manager.scan()
        replacement = self.manager.resolve('board-one')
        self.assertIsNot(replacement, original)
        self.assertIs(self.manager.boards['MODEL-SN1'], self.board)
        self.assertIs(self.board.connection, replacement)
        self.assertEqual(replacement.dev, '/fake/tty-renumbered')
        self.assertIs(replacement.get_session(session.sid), session)
        self.assertIs(replacement._sessions, original._sessions)
        session.close.assert_not_called()

    def test_identifying_board_on_new_port_retains_identity_sessions_and_logs(self):
        original = self.connection()
        original.append('original-port-output\r\n')
        original.close_log()
        original.present = False
        session = mock.Mock(sid='ssh-kept')
        original.ssh_sessions[session.sid] = session
        replacement = Device('usb-b', '/fake/tty1', self.manager)
        replacement.present = True
        replacement.append('new-port-output\r\n')
        self.manager.devices['usb-b'] = replacement
        self.manager.identify(replacement, dict(self.record['info']))
        self.assertIs(self.manager.boards['MODEL-SN1'], self.board)
        self.assertIs(self.board.connection, replacement)
        self.assertIs(replacement.get_session(session.sid), session)
        self.assertEqual(replacement.record['last_path'], 'usb-b')
        text = ''
        for file in replacement.log_files():
            with open(os.path.join(replacement.stable_dir(), file['name'])) as source:
                text += source.read()
        self.assertIn('original-port-output', text)
        self.assertIn('new-port-output', text)

    def test_rename_updates_board_views_without_changing_archive_identity(self):
        history = self.manager.resolve('board-one')
        connection = self.connection()
        directory = connection.stable_dir()
        self.manager.rename(connection, 'renamed')
        self.assertEqual(history.key, 'renamed')
        self.assertIs(self.manager.resolve('renamed'), connection)
        self.assertIsNone(self.manager.resolve('board-one'))
        self.assertEqual(connection.stable_dir(), directory)
        with self.assertRaises(IdentityError):
            self.board.update(dict(self.board.record, serial='OTHER'))

    def test_preparing_swap_keeps_old_board_and_isolates_new_sessions(self):
        connection = self.connection()
        connection.fd = object()
        session = mock.Mock(sid='ssh1')
        connection.ssh_sessions['ssh1'] = session
        old_registry = self.board.sessions
        self.manager.prepare_switch(connection)
        session.close.assert_called_once()
        self.assertIsNone(connection.board)
        self.assertIsNone(self.board.connection)
        self.assertIsNot(connection._sessions, old_registry)
        history = self.manager.resolve('board-one')
        self.assertIs(history.board, self.board)
        self.assertFalse(history.present)
        self.assertFalse(history.ssh_sessions)
        connection.ssh_sessions['ssh-new'] = mock.Mock()
        self.assertFalse(history.ssh_sessions)

    def test_late_ssh_opener_releases_its_original_board_reservation(self):
        connection = self.connection()
        old_registry = connection._sessions
        entered, release = threading.Event(), threading.Event()
        session = mock.Mock()

        def ensure():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test synchronization timeout')
            return True

        session.ensure.side_effect = ensure
        result = []
        with mock.patch('seriald_core.device.SshSession', return_value=session), \
                mock.patch('seriald_core.device.check_password_login', return_value=(True, False, '')):
            worker = threading.Thread(target=lambda: result.append(connection.ssh_connect()))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(old_registry.pending, 1)
                self.manager.prepare_switch(connection)
                replacement_registry = connection._sessions
                self.assertEqual(replacement_registry.pending, 0)
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(result[0][0])
        session.close.assert_called_once()
        self.assertEqual(old_registry.pending, 0)
        self.assertEqual(replacement_registry.pending, 0)
        self.assertFalse(connection.ssh_sessions)

    def test_session_catalog_transfer_rejects_pending_and_collisions(self):
        source, destination = SessionRegistry(), SessionRegistry()
        source.sessions['ssh1'] = mock.Mock()
        source.pending = 1
        with self.assertRaises(IdentityError):
            source.transfer_to(destination)
        source.pending = 0
        destination.sessions['ssh1'] = mock.Mock()
        with self.assertRaises(IdentityError):
            source.transfer_to(destination)
        destination.sessions.clear()
        original = source.sessions['ssh1']
        source.transfer_to(destination)
        self.assertIs(destination.sessions['ssh1'], original)
        self.assertFalse(source.sessions)


if __name__ == '__main__':
    unittest.main()
