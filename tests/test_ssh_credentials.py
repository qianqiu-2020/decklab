"""Private SSH credentials use real temporary SQLite, never real boards."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core import schema, storage
from seriald_core.storage import Store


class SSHCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'registry.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.bind()

    def bind(self, serial='SN1', connection='connection-one', path='usb-one', **info):
        return self.store.bind(dict(serial=serial, hostname='test-board', **info),
                               path, '/fake/tty', connection_id=connection)

    def save(self, user='ubuntu', password='private-board-password', serial='SN1',
             connection='connection-one', revision=0):
        return self.store.save_ssh_credentials(serial, user, password, connection, revision)

    def test_verified_settings_survive_reopen_and_a_different_usb_port(self):
        password = ' spaces $`"秘密\t '
        self.assertTrue(self.save(password=password))
        self.store.close()
        self.store = Store(self.path)
        self.bind(connection='connection-two', path='usb-two')
        self.assertEqual(self.store.get_ssh_credentials('SN1'),
                         {'ssh_user': 'ubuntu', 'ssh_password': password, 'revision': 1})
        self.assertEqual(self.store.get_ssh_credentials_metadata('SN1'),
                         {'saved': True, 'user': 'ubuntu', 'revision': 1})
        self.assertTrue(self.save(user='debian', password='new-password',
                                  connection='connection-two', revision=1))
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_user'], 'debian')

    def test_reusing_a_port_for_a_different_board_does_not_reuse_credentials(self):
        self.assertTrue(self.save())
        self.bind('SN2', 'connection-two', 'usb-one')
        self.assertIsNone(self.store.get_ssh_credentials('SN2'))
        self.assertEqual(self.store.get_ssh_credentials_metadata('SN2'),
                         {'saved': False, 'user': '', 'revision': 0})
        self.assertFalse(self.save(serial='SN2'))
        self.assertFalse(self.save(connection='connection-two'))
        self.assertTrue(self.save(serial='SN2', connection='connection-two', password='second-board'))
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_password'], 'private-board-password')
        self.assertEqual(self.store.get_ssh_credentials('SN2')['ssh_password'], 'second-board')

    def test_rename_and_explicit_serial_correction_keep_the_same_boards_credentials(self):
        self.assertTrue(self.save())
        self.store.rename('SN1', 'renamed-board')
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_user'], 'ubuntu')
        self.store.correct_serial('SN1', 'SN-CORRECTED')
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))
        self.assertEqual(self.store.get_ssh_credentials('SN-CORRECTED'),
                         {'ssh_user': 'ubuntu', 'ssh_password': 'private-board-password', 'revision': 1})
        self.assertTrue(self.save(serial='SN-CORRECTED', password='corrected-password', revision=1))
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_verified_eeprom_promotion_carries_credentials_in_the_identity_transaction(self):
        self.bind(serial_source='boot-marker', identity_status='provisional')
        self.assertTrue(self.save())
        self.store.promote_eeprom_identity('SN1',
            {'serial': 'EEPROM-SN', 'serial_source': 'eeprom-onie', 'identity_status': 'confirmed'},
            'usb-one', '/fake/tty', connection_id='connection-one')
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))
        self.assertEqual(self.store.get_ssh_credentials('EEPROM-SN')['ssh_password'],
                         'private-board-password')
        self.assertTrue(self.save(serial='EEPROM-SN', password='promoted-password', revision=1))
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_conflicted_identities_hide_and_preserve_the_saved_value(self):
        self.assertTrue(self.save())
        self.store.mark_conflict('SN1')
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))
        self.assertEqual(self.store.get_ssh_credentials_metadata('SN1'),
                         {'saved': False, 'user': '', 'revision': 0})
        self.assertFalse(self.save(password='wrong-board', revision=1))
        self.store.resolve_conflict('SN1', 'Verified this board and existing archive ownership')
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_password'], 'private-board-password')

    def test_unknown_identity_and_unproven_connection_cannot_save(self):
        self.assertFalse(self.save(serial='missing'))
        self.assertFalse(self.save(connection='unknown-generation'))
        for serial, connection in (('', 'connection-one'), ('SN1', ''), (None, 'connection-one'),
                                   ('SN1', None), ('SN1', 'x' * 81)):
            with self.subTest(serial=serial, connection=connection):
                self.assertFalse(self.save(serial=serial, connection=connection))
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))

    def test_failed_compare_and_swap_keeps_newer_credentials(self):
        self.assertTrue(self.save())
        self.assertTrue(self.save(user='debian', password='newest-password', revision=1))
        self.assertFalse(self.save(password='late-password', revision=1))
        self.assertFalse(self.save(password='old-initial-password', revision=0))
        self.assertEqual(self.store.get_ssh_credentials('SN1'),
                         {'ssh_user': 'debian', 'ssh_password': 'newest-password', 'revision': 2})

    def test_two_writers_cannot_overwrite_the_same_revision(self):
        other = Store(self.path)
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)

        def save(store, password):
            barrier.wait(3)
            return store.save_ssh_credentials('SN1', 'ubuntu', password, 'connection-one', 0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(save, self.store, 'first-password')
            second = pool.submit(save, other, 'second-password')
            results = [first.result(5), second.result(5)]
        self.assertEqual(sorted(results), [False, True])
        expected = 'first-password' if results[0] else 'second-password'
        self.assertEqual(self.store.get_ssh_credentials('SN1'),
                         {'ssh_user': 'ubuntu', 'ssh_password': expected, 'revision': 1})

    def test_malformed_credentials_are_rejected_without_changing_the_saved_value(self):
        self.assertTrue(self.save())
        for user, password in (('', 'secret'), ('-oProxyCommand=bad', 'secret'),
                               ('a@host', 'secret'), ('with space', 'secret'),
                               ('ubuntu', 'secret\ncommand'), ('ubuntu', 'secret\x00command'),
                               ('ubuntu', None), ('ubuntu', 'x' * 4097)):
            with self.subTest(user=user, password_length=len(password) if password else None):
                with self.assertRaisesRegex(ValueError, '^invalid SSH credentials$'):
                    self.save(user=user, password=password, revision=1)
        for revision in (-1, True, '1', None):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(ValueError, '^invalid SSH credentials revision$'):
                    self.save(revision=revision)
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_password'], 'private-board-password')

    def test_board_records_and_public_metadata_never_include_passwords(self):
        self.assertTrue(self.save())
        outputs = [self.store.get('SN1'), self.store.resolve('test-board'), self.store.boards(),
                   self.store.get_ssh_credentials_metadata('SN1'),
                   self.store.get_ssh_credentials_metadata('missing'),
                   self.store.get_ssh_credentials_metadata(None)]
        public = json.dumps(outputs)
        self.assertNotIn('private-board-password', public)
        self.assertNotIn('ssh_password', public)
        self.assertEqual(self.store.events('SN1'), [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes; Windows inherits runtime directory ACLs')
    def test_new_database_is_private_before_sqlite_opens_it_even_with_a_permissive_umask(self):
        path = Path(self.temporary.name) / 'new-registry.sqlite3'
        connect = sqlite3.connect
        observed = []

        def opening(filename, *args, **kwargs):
            observed.append(stat.S_IMODE(Path(filename).stat().st_mode))
            return connect(filename, *args, **kwargs)

        old_umask = os.umask(0)
        try:
            with mock.patch.object(storage.sqlite3, 'connect', side_effect=opening):
                store = Store(path)
            try:
                store.bind({'serial': 'SN'}, 'usb', '/fake/tty', connection_id='generation')
                store.save_ssh_credentials('SN', 'ubuntu', 'secret', 'generation', 0)
                files = [Path(str(path) + suffix) for suffix in ('', '-wal', '-shm')]
                self.assertTrue(all(file.exists() for file in files))
                self.assertTrue(all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in files))
            finally:
                store.close()
        finally:
            os.umask(old_umask)
        self.assertEqual(observed, [0o600])

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes; Windows inherits runtime directory ACLs')
    def test_opening_an_existing_database_tightens_database_and_existing_sidecars(self):
        self.assertTrue(self.save())
        files = [Path(str(self.path) + suffix) for suffix in ('', '-wal', '-shm')]
        for file in files:
            self.assertTrue(file.exists())
            file.chmod(0o666)
        other = Store(self.path)
        try:
            self.assertTrue(all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in files))
            self.assertEqual(other.get_ssh_credentials('SN1')['ssh_password'], 'private-board-password')
        finally:
            other.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes; Windows inherits runtime directory ACLs')
    def test_future_schema_backup_with_credentials_stays_private(self):
        self.assertTrue(self.save())
        owner, migrations, expected = schema._SCHEMAS['registry']
        self.store.close()
        with mock.patch.dict(schema._SCHEMAS,
                             registry=(owner, migrations + (lambda db: None,), expected)):
            upgraded = Store(self.path)
        try:
            backup = Path(upgraded.schema_backup)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            connection = sqlite3.connect(backup)
            try:
                self.assertEqual(connection.execute('SELECT ssh_password FROM ssh_credentials').fetchone()[0],
                                 'private-board-password')
            finally:
                connection.close()
        finally:
            upgraded.close()


if __name__ == '__main__':
    unittest.main()
