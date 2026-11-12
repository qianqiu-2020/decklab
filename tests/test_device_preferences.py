"""Metadata imports never bind identities, move archives or execute console IO."""
from pathlib import Path
import io
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from seriald_core.device_preferences import apply, snapshot
from seriald_core.storage import Store
from seriald_core.devices import Manager
from seriald_core.device import Device
from seriald_core.board_maintenance import maintenance_for
from seriald_core.web_settings import SettingsError
from seriald_core.transactions import TransactionError, transactions_for
from seriald_core.web import Handler


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = Store(root / 'registry.sqlite3')
        self.addCleanup(self.store.close)
        for module in ('seriald_core.device', 'seriald_core.devices'):
            patch = mock.patch(module + '.LOG_ROOT', str(root / 'logs'))
            patch.start()
            self.addCleanup(patch.stop)
        self.store.bind({'serial': 'SERIAL-A', 'hostname': 'alpha'}, 'port-a', '/fake/a', connection_id='fixture-a')
        self.store.bind({'serial': 'SERIAL-B', 'hostname': 'beta'}, 'port-b', '/fake/b')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.pending = Device('pending-port', '/fake/c', self.manager)
        self.pending.present = True
        self.manager.devices[self.pending.by_path] = self.pending

    def handler(self, document):
        handler = Handler.__new__(Handler)
        handler.server = mock.Mock(manager=self.manager, spec=['manager'])
        handler.path = '/api/device-preferences'
        body = json.dumps(document).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        return handler

    def save(self, patch, preview=False, revision=None):
        return apply(self.manager, patch, revision or snapshot(self.manager)['revision'], preview, 'this-client')

    def test_swap_labels_and_import_pending_alias_probe_without_hardware_writes(self):
        old_serials = sorted(self.manager.boards)
        old_key = self.pending.key
        with mock.patch.object(self.pending, '_write') as write:
            result = self.save({'version': 1, 'boards': {'SERIAL-A': {'label': 'beta'}, 'SERIAL-B': {'label': 'alpha'}},
                'ports': {'pending-port': {'label': 'buildroot-left', 'probe': {'mode': 'manual', 'command': ''}}}})
        self.assertTrue(result['ok'])
        self.assertEqual(self.store.get('SERIAL-A')['label'], 'beta')
        self.assertEqual(self.store.get('SERIAL-B')['label'], 'alpha')
        self.assertEqual(self.pending.label, 'buildroot-left')
        self.assertEqual(self.pending.key, old_key)
        self.assertEqual(self.pending.serial, '')
        self.assertEqual(sorted(self.manager.boards), old_serials)
        self.assertEqual(self.pending.profile.probe, 'manual')
        self.assertTrue(self.pending.auto_identify_started)
        write.assert_not_called()
        self.assertEqual(len(self.store.events('SERIAL-A')), 1)

    def test_preview_unknown_identity_collision_and_malformed_probes_preserve_state(self):
        before = snapshot(self.manager)
        for patch in ({'version': 1, 'boards': {'UNKNOWN': {'label': 'new'}}},
                      {'version': 1, 'boards': {'SERIAL-A': {'label': 'beta'}}},
                      {'version': 1, 'ports': {'pending-port': {'probe': {'mode': 'custom', 'command': ''}}}},
                      {'version': 1, 'ports': {'pending-port': {'password': 'no'}}},
                      {'version': True}):
            with self.assertRaises(ValueError):
                self.save(patch)
            self.assertEqual(snapshot(self.manager), before)
        result = self.save({'version': 1, 'boards': {'SERIAL-A': {'label': 'renamed'}}}, preview=True)
        self.assertTrue(result['preview'])
        self.assertEqual(snapshot(self.manager), before)

    def test_connection_generation_and_intervening_metadata_changes_reject_stale_import(self):
        revision = snapshot(self.manager)['revision']
        self.pending.connection_id = 'replacement'
        with self.assertRaises(SettingsError):
            self.save({'version': 1, 'ports': {'pending-port': {'label': 'left'}}}, revision=revision)
        revision = snapshot(self.manager)['revision']
        self.store.rename('SERIAL-A', 'changed')
        with self.assertRaises(SettingsError):
            self.save({'version': 1, 'boards': {'SERIAL-A': {'label': 'newer'}}}, revision=revision)

    def test_all_changes_roll_back_when_audit_insert_fails(self):
        before = snapshot(self.manager)
        self.store.db.execute("CREATE TRIGGER fail_preferences BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save({'version': 1, 'boards': {'SERIAL-A': {'label': 'changed'}},
                       'ports': {'pending-port': {'label': 'left'}}})
        self.assertEqual(snapshot(self.manager), before)
        self.assertEqual(self.manager.boards['SERIAL-A'].record['label'], 'alpha')

    def test_reservation_or_active_operation_blocks_import(self):
        owner = transactions_for(self.pending)
        with owner.actor_scope('web', 'another-client'):
            owner.claim('web')
        before = snapshot(self.manager)
        with self.assertRaises(TransactionError):
            self.save({'version': 1, 'ports': {'pending-port': {'label': 'left'}}})
        self.assertEqual(snapshot(self.manager), before)

    def test_identity_conflicts_and_flash_recovery_holds_cannot_be_bypassed(self):
        patch = {'version': 1, 'boards': {'SERIAL-A': {'label': 'renamed'}}}
        with self.store.db:
            self.store.db.execute('INSERT INTO identity_conflicts VALUES(?,?)', ('SERIAL-A', 1))
        before = snapshot(self.manager)
        with self.assertRaises(SettingsError) as context:
            self.save(patch)
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(snapshot(self.manager), before)
        with self.store.db:
            self.store.db.execute('DELETE FROM identity_conflicts')
        maintenance = maintenance_for(self.manager)
        with maintenance.admission('SERIAL-A', ['flash:local:fixture']) as resources:
            task = self.manager.tasks.store.create_board('flash', 'web', '', 'SERIAL-A', 'request-fixture-original', 'a' * 64,
                {'recovery_required': True, 'write_result': 'unknown-or-partial'}, resources)
        self.manager.tasks.store.update(task, state='failed')
        maintenance.keep_recovery(task)
        before = snapshot(self.manager)
        with self.assertRaises(SettingsError) as context:
            self.save(patch)
        self.assertEqual(context.exception.status, 423)
        self.assertEqual(snapshot(self.manager), before)

    def test_omitted_preferences_remain_and_explicit_null_clears_port_override(self):
        self.store.rename_attachment('pending-port', 'left')
        self.store.set_probe_settings('pending-port', {'mode': 'manual', 'command': ''})
        self.save({'version': 1, 'ports': {'pending-port': {'label': None}}})
        document = snapshot(self.manager)['configuration']
        self.assertEqual(document['ports']['pending-port'], {'probe': {'mode': 'manual', 'command': ''}})
        self.assertEqual(self.pending.attachment_label, '')
        self.save({'version': 1, 'ports': {'pending-port': {'probe': None}}})
        self.assertNotIn('pending-port', snapshot(self.manager)['configuration']['ports'])

    def test_verified_ssh_credentials_are_neither_exported_nor_modified(self):
        self.assertTrue(self.store.save_ssh_credentials('SERIAL-A', 'root', 'fixture-password', 'fixture-a', 0))
        old = self.store.get_ssh_credentials('SERIAL-A')
        self.assertNotIn('fixture-password', json.dumps(snapshot(self.manager)))
        self.save({'version': 1, 'boards': {'SERIAL-A': {'label': 'renamed'}}})
        self.assertEqual(self.store.get_ssh_credentials('SERIAL-A'), old)

    def test_collector_api_validates_and_returns_committed_revision(self):
        before = snapshot(self.manager)
        handler = self.handler({'action': 'save', 'revision': before['revision'],
                                'configuration': {'version': 1, 'ports': {'pending-port': {'label': 'new'}}}})
        handler.do_POST()
        self.assertEqual(handler.send_response.call_args.args[0], 200)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload['revision'], snapshot(self.manager)['revision'])
        self.assertEqual(self.pending.label, 'new')
        handler = self.handler({})
        handler.do_GET()
        self.assertEqual(handler.send_response.call_args.args[0], 200)
        self.assertEqual(json.loads(handler.wfile.getvalue())['configuration']['ports']['pending-port']['label'], 'new')

    def test_collector_api_rolls_back_all_metadata_on_sql_failure(self):
        before = snapshot(self.manager)
        self.store.db.execute("CREATE TRIGGER fail_preferences BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fixture'); END")
        handler = self.handler({'action': 'save', 'revision': before['revision'],
                                'configuration': {'version': 1, 'boards': {'SERIAL-A': {'label': 'renamed'}}}})
        handler.do_POST()
        self.assertEqual(handler.send_response.call_args.args[0], 503)
        self.assertEqual(snapshot(self.manager), before)
