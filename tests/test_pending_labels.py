"""Attachment names never establish a board identity or select an archive."""
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from seriald_core.actions import DeviceActions
from seriald_core.device import Device
from seriald_core.devices import Manager
from seriald_core.storage import IdentityError, Store


class PendingLabelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'registry.sqlite3')
        self.addCleanup(self.store.close)
        for module in ('seriald_core.device', 'seriald_core.devices'):
            patcher = mock.patch(module + '.LOG_ROOT', str(self.root / 'logs'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)

    def pending(self, port='usb-stable-a', dev='/fake/ttyUSB0'):
        device = Device(port, dev, self.manager)
        device.present = True
        self.manager.devices[port] = device
        return device

    def test_pending_label_changes_display_only_and_keeps_logs_serial_and_key(self):
        pending = self.pending()
        key, directory = pending.key, pending.stable_dir()
        payload = {'key': key, 'serial': '', 'connection_id': pending.connection_id,
                   'label': 'buildroot-left'}
        result = DeviceActions(self.manager).execute('label', payload, pending)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload['label'], 'buildroot-left')
        self.assertEqual(result.payload['device_key'], key)
        self.assertEqual(result.payload['attachment_label'], 'buildroot-left')
        self.assertEqual(pending.key, key)
        self.assertEqual(pending.serial, '')
        self.assertEqual(pending.stable_dir(), directory)
        self.assertEqual(pending.snapshot()['label'], 'buildroot-left')
        self.assertEqual(self.store.boards(), [])
        self.assertIsNone(self.manager.resolve('buildroot-left'))

    def test_reconnect_restores_attachment_label_without_assigning_a_board(self):
        original = self.pending()
        self.manager.rename(original, 'uart-left', original.connection_id, '')
        original.present = False
        original.stop.set()
        replacement = self.pending(dev='/fake/ttyUSB9')
        self.assertEqual(replacement.label, 'uart-left')
        self.assertNotEqual(replacement.connection_id, original.connection_id)
        self.assertEqual(replacement.serial, '')
        self.assertEqual(self.pending('usb-stable-b').attachment_label, '')
        reopened = Store(self.root / 'registry.sqlite3')
        try:
            self.assertEqual(reopened.attachment_label('usb-stable-a'), 'uart-left')
            self.assertEqual(reopened.boards(), [])
        finally:
            reopened.close()

    def test_old_or_absent_generation_cannot_rename_a_replacement(self):
        original = self.pending()
        replacement = self.pending()
        for connection in (None, original.connection_id):
            with self.subTest(connection=connection), self.assertRaises(IdentityError):
                self.manager.rename(replacement, 'stale-label', connection, '')
        with self.assertRaises(IdentityError):
            self.manager.rename(original, 'stale-label', original.connection_id, '')
        with self.assertRaises(IdentityError):
            self.manager.rename(replacement, 'missing-serial-guard', replacement.connection_id)
        self.assertEqual(self.store.attachment_label(replacement.by_path), '')

    def test_new_board_inherits_annotation_and_existing_board_preserves_its_label(self):
        pending = self.pending()
        self.manager.rename(pending, 'left-board', pending.connection_id, '')
        self.manager.identify(pending, {'serial': 'NEW-SN', 'serial_source': 'device-tree',
                                       'hostname': 'debian'})
        self.assertEqual(pending.key, 'left-board')
        self.assertEqual(pending.serial, 'NEW-SN')
        pending.present = False
        pending.stop.set()
        replacement = self.pending()
        self.manager.rename(replacement, 'port-replacement-name', replacement.connection_id, '')
        self.manager.identify(replacement, {'serial': 'NEW-SN', 'serial_source': 'device-tree',
                                           'hostname': 'new-host'})
        self.assertEqual(replacement.key, 'left-board')
        self.assertEqual(replacement.serial, 'NEW-SN')
        self.assertEqual(self.store.get('NEW-SN')['label'], 'left-board')

    def test_label_collision_does_not_merge_or_block_a_new_identity(self):
        self.store.bind({'serial': 'OLD-SN'}, 'other-port', '/fake/old', label='same-name')
        pending = self.pending()
        self.manager.rename(pending, 'same-name', pending.connection_id, '')
        self.manager.identify(pending, {'serial': 'NEW-SN', 'hostname': 'debian'})
        self.assertEqual(pending.serial, 'NEW-SN')
        self.assertNotEqual(pending.key, 'same-name')
        self.assertEqual(self.store.resolve('same-name')['serial'], 'OLD-SN')

    def test_pending_request_cannot_rename_a_board_identified_during_the_request(self):
        pending = self.pending()
        self.manager.identify(pending, {'serial': 'NEW-SN', 'hostname': 'debian'})
        with self.assertRaisesRegex(IdentityError, '身份已变化'):
            self.manager.rename(pending, 'late-label', pending.connection_id, '')
        self.assertEqual(pending.key, 'debian')

    def test_invalid_label_does_not_persist_or_establish_identity(self):
        pending = self.pending()
        for value in ('', '../unsafe', 'pending-fake', 'contains space', '<script>'):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                self.manager.rename(pending, value, pending.connection_id, '')
        self.assertEqual(self.store.attachment_label(pending.by_path), '')
        self.assertEqual(pending.serial, '')


class PendingLabelMigrationTests(unittest.TestCase):
    def test_schema_eight_upgrade_keeps_existing_board_and_private_snapshot(self):
        from seriald_core.schema import _SCHEMAS, REGISTRY_MIGRATIONS
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'registry.sqlite3'
            db = sqlite3.connect(path)
            for migrate in REGISTRY_MIGRATIONS[:8]:
                migrate(db)
            db.execute('INSERT INTO boards VALUES(?,?,?,?,?,?,?)',
                       ('OLD-SN', 'old-board', '{}', 1, 2, 'old-port', '/fake/old'))
            db.execute('PRAGMA application_id=%d' % _SCHEMAS['registry'][0])
            db.execute('PRAGMA user_version=8')
            db.commit()
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.get('OLD-SN')['label'], 'old-board')
                self.assertEqual(store.attachment_label('old-port'), '')
                store.rename_attachment('old-port', 'port-label')
                self.assertEqual(store.attachment_label('old-port'), 'port-label')
                backup = Path(store.schema_backup)
                if os.name != 'nt':
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                with sqlite3.connect(backup) as prior:
                    self.assertEqual(prior.execute('PRAGMA user_version').fetchone()[0], 8)
                    self.assertIsNone(prior.execute("SELECT name FROM sqlite_master WHERE name='attachment_labels'").fetchone())
            finally:
                store.close()
