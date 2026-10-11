"""Real SQLite upgrades and WAL backups in temporary directories; no hardware."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from seriald_core import schema
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.storage import Store


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'registry.sqlite3'

    def legacy(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA journal_mode=WAL')
        schema._registry_initial(db)
        db.execute('INSERT INTO boards VALUES (?,?,?,?,?,?,?)',
                   ('SN1', 'board-one', json.dumps({'serial': 'SN1'}), 1, 2, 'usb-one', '/dev/ttyUSB1'))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                   ('ssh1', 'SN1', 'agent', 'uname', '192.0.2.1', 1, None, 'open'))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                   (1, 'SN1', 2, 'agent', 'identified', 'legacy'))
        db.commit()
        self.addCleanup(db.close)
        return db

    def test_new_and_memory_databases_are_versioned_without_backup(self):
        for path, kind, factory in ((str(self.path), 'registry', Store),
                                    (':memory:', 'registry', Store),
                                    (':memory:', 'grants', AgentGrantStore)):
            store = factory(path)
            try:
                self.assertIsNone(store.schema_backup)
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0],
                                 len(schema._SCHEMAS[kind][1]))
                self.assertEqual(store.db.execute('PRAGMA application_id').fetchone()[0],
                                 schema._SCHEMAS[kind][0])
            finally:
                store.close()
        self.assertFalse((self.path.parent / 'backups').exists())

    def test_unversioned_upgrade_preserves_history_and_backs_up_committed_wal(self):
        legacy = self.legacy()  # Keep the WAL writer open; copying only the main file is insufficient.
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        store = Store(str(self.path))
        try:
            self.assertEqual(store.resolve('board-one')['serial'], 'SN1')
            self.assertEqual(store.sessions('SN1')[0]['sid'], 'ssh1')
            self.assertIsNone(store.sessions('SN1')[0]['completed'])
            self.assertEqual(store.events('SN1')[0]['detail'], 'legacy')
            backup = sqlite3.connect(store.schema_backup)
            try:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(backup.execute('SELECT label FROM boards').fetchone()[0], 'board-one')
                self.assertNotIn('completed', {row[1] for row in backup.execute('PRAGMA table_info(sessions)')})
                self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                backup.close()
            self.assertEqual(legacy.execute('PRAGMA user_version').fetchone()[0], len(schema.REGISTRY_MIGRATIONS))
        finally:
            store.close()
        reopened = Store(str(self.path))
        try:
            self.assertIsNone(reopened.schema_backup)
            self.assertEqual(len(list((self.path.parent / 'backups').glob('*.sqlite3'))), 1)
        finally:
            reopened.close()

    def test_version_one_migrates_forward_without_recreating_boards(self):
        db = self.legacy()
        db.execute('PRAGMA user_version=1')
        db.commit()
        store = Store(str(self.path))
        try:
            self.assertEqual(store.get('SN1')['last_dev'], '/dev/ttyUSB1')
            store.record_command('SN1', 'uname -a')
            self.assertEqual(store.commands('SN1')[0]['command'], 'uname -a')
        finally:
            store.close()

    def test_version_two_adds_tasks_and_snapshot_preserves_old_schema(self):
        db = self.legacy()
        schema._registry_history(db)
        db.execute('PRAGMA user_version=2')
        db.commit()
        store = Store(str(self.path))
        try:
            self.assertEqual(store.get('SN1')['label'], 'board-one')
            self.assertEqual(store.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
            with sqlite3.connect(store.schema_backup) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone())
        finally:
            store.close()

    def test_failed_migration_rolls_back_ddl_data_and_version(self):
        db = self.legacy()
        owner, migrations, expected = schema._SCHEMAS['registry']

        def fail(connection):
            connection.execute('ALTER TABLE boards ADD COLUMN unwanted TEXT')
            connection.execute("UPDATE boards SET label='wrong'")
            raise RuntimeError('injected upgrade failure')

        with mock.patch.dict(schema._SCHEMAS, registry=(owner, (migrations[0], fail), expected)):
            with self.assertRaisesRegex(schema.SchemaError, 'pre-upgrade backup:'):
                Store(str(self.path))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT label FROM boards').fetchone()[0], 'board-one')
        self.assertNotIn('unwanted', {row[1] for row in db.execute('PRAGMA table_info(boards)')})
        recovered = Store(str(self.path))
        recovered.close()

    def test_backup_failure_prevents_any_schema_change(self):
        db = self.legacy()
        with mock.patch.object(schema, '_backup', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(schema.SchemaError, 'disk full'):
                Store(str(self.path))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertNotIn('completed', {row[1] for row in db.execute('PRAGMA table_info(sessions)')})

    def test_backup_publish_failure_removes_temporary_snapshot(self):
        db = self.legacy()
        with mock.patch.object(schema.os, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(schema.SchemaError, 'publish failed'):
                Store(str(self.path))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertEqual(list((self.path.parent / 'backups').iterdir()), [])

    def test_unrelated_unversioned_database_is_not_adopted(self):
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE unrelated (value TEXT)')
        db.commit()
        db.close()
        with self.assertRaisesRegex(schema.SchemaError, 'unrecognized unversioned'):
            Store(str(self.path))
        self.assertFalse((self.path.parent / 'backups').exists())

    def test_newer_schema_and_wrong_application_are_rejected_without_backup(self):
        db = self.legacy()
        for pragma, value, message in (('user_version', 999, 'refusing downgrade'),
                                       ('application_id', 12345, 'different application')):
            db.execute('PRAGMA user_version=0')
            db.execute('PRAGMA {}={}'.format(pragma, value))
            db.commit()
            with self.assertRaisesRegex(schema.SchemaError, message):
                Store(str(self.path))
            self.assertEqual(db.execute('PRAGMA ' + pragma).fetchone()[0], value)
        self.assertFalse((self.path.parent / 'backups').exists())

    def test_existing_current_unversioned_registry_is_adopted_without_losing_commands(self):
        store = Store(str(self.path))
        store.bind({'serial': 'SN1', 'hostname': 'board-one'}, 'usb', '/fake/tty')
        store.record_command('SN1', 'dmesg')
        store.db.execute('PRAGMA user_version=0')
        store.db.execute('PRAGMA application_id=0')
        store.db.commit()
        store.close()
        adopted = Store(str(self.path))
        try:
            self.assertEqual(adopted.commands('SN1')[0]['command'], 'dmesg')
            self.assertIsNotNone(adopted.schema_backup)
        finally:
            adopted.close()

    def test_grant_upgrade_preserves_token_hash_scope_and_revocation(self):
        db = sqlite3.connect(self.path)
        schema._grants_initial(db)
        token = 'seriald-ag-test'
        db.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,?)',
                   ('grant1', hashlib.sha256(token.encode()).hexdigest(), 'agent', 'device', 'SN1', 1, None, None))
        db.commit()
        db.close()
        store = AgentGrantStore(str(self.path))
        try:
            self.assertEqual(store.lookup(token)['serial'], 'SN1')
            self.assertNotIn(token.encode(), Path(store.schema_backup).read_bytes())
            self.assertEqual(store.revoke('grant1'), 'agent')
            self.assertIsNone(store.lookup(token))
        finally:
            store.close()

    def test_foreign_key_failure_rolls_back_adoption(self):
        db = self.legacy()
        db.execute("UPDATE sessions SET serial='missing'")
        db.commit()
        with self.assertRaisesRegex(schema.SchemaError, 'invalid board references'):
            Store(str(self.path))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)

    def test_concurrent_openers_perform_one_upgrade_and_one_backup(self):
        self.legacy()

        def open_store():
            store = Store(str(self.path))
            try:
                return store.schema_backup, store.get('SN1')['label']
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: open_store(), range(2)))
        self.assertEqual([label for _, label in results], ['board-one', 'board-one'])
        self.assertEqual(sum(backup is not None for backup, _ in results), 1)
        self.assertEqual(len(list((self.path.parent / 'backups').glob('*.sqlite3'))), 1)


if __name__ == '__main__':
    unittest.main()
