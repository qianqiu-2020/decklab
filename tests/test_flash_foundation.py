"""Board-scoped durable flashing uses fake UARTs and temporary SQLite only."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest import mock

from seriald_core.board_maintenance import maintenance_for
from seriald_core.device import Device
from seriald_core.flash_store import FlashStore, MAX_EVENTS
from seriald_core.models import Board
from seriald_core.storage import Store, IdentityError
from seriald_core.tasks import TaskContext, TaskError, TaskManager, TaskStore
from seriald_core.transactions import TransactionError
from seriald_core import schema


class FlashFoundationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(str(Path(temporary.name) / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.records = {serial: self.store.bind({'serial': serial}, '', '') for serial in ('BOARD-A', 'BOARD-B')}
        self.manager = types.SimpleNamespace(store=self.store,
            boards={serial: Board(record) for serial, record in self.records.items()})
        self.maintenance = maintenance_for(self.manager)
        self.tasks = TaskManager(self.store, max_active=2)
        self.addCleanup(self.tasks.close)
        self.flashes = FlashStore(self.store)
        logs = mock.patch('seriald_core.device.LOG_ROOT', temporary.name)
        logs.start()
        self.addCleanup(logs.stop)

    def device(self, serial):
        device = Device('fake-' + serial, '/fake/tty', board=self.manager.boards[serial])
        device.manager = self.manager
        device.fd, device.present = 98765, True
        self.manager.boards[serial].attach(device)
        self.addCleanup(device.close_log)
        self.addCleanup(device.terminal.screen.close)
        return device

    def start(self, run, serial='BOARD-A', request='request-one', digest='a' * 64, **kw):
        return self.tasks.start_board('flash', 'web', 'client-one', serial, run,
            request_id=request, input_digest=digest, details={'plan_id': 'immutable-plan'},
            maintenance=self.maintenance, **kw)

    def controlled(self):
        entered, release = threading.Event(), threading.Event()
        def run(context):
            entered.set()
            release.wait(2)
            self.maintenance.release(context.id, confirmed=True)
            return {'write_result': 'completed', 'boot_result': 'not-requested'}
        return entered, release, run

    def test_offline_board_target_is_real_and_survives_without_fake_connection(self):
        entered, release, run = self.controlled()
        task = self.start(run)
        self.assertTrue(entered.wait(2))
        self.assertEqual(task['targets'], [])
        self.assertEqual(task['board_targets'], ['BOARD-A'])
        self.assertEqual(task['flash']['plan_id'], 'immutable-plan')
        history = self.tasks.store
        self.assertEqual([row['id'] for row in history.list(target_serial='BOARD-A')], [task['id']])
        self.assertEqual(history.list(serials=['BOARD-B']), [])
        self.assertTrue(history.visible_serial(history.get(task['id']), 'BOARD-A'))
        release.set()
        self.assertTrue(self.tasks.close())
        self.assertEqual(history.get(task['id'])['state'], 'completed')
        self.assertEqual(self.maintenance.holds(), [])

    def test_request_deduplicates_before_worker_capacity_and_rejects_changed_input(self):
        entered, release, run = self.controlled()
        self.tasks.max_active = 1
        task = self.start(run)
        self.assertTrue(entered.wait(2))
        repeated = self.start(mock.Mock(side_effect=AssertionError('must not replay')))
        self.assertEqual(task['id'], repeated['id'])
        with self.assertRaisesRegex(TaskError, 'different input'):
            self.start(run, digest='b' * 64)
        with self.assertRaisesRegex(TaskError, 'too many active'):
            self.start(run, serial='BOARD-B', request='request-two')
        release.set()

    def test_same_board_and_shared_endpoint_exclude_while_other_board_can_run(self):
        entered, release, run = self.controlled()
        self.start(run, resources=['usb:slot-one'])
        self.assertTrue(entered.wait(2))
        with self.assertRaises(TaskError):
            self.start(run, request='same-board')
        with self.assertRaises(TaskError):
            self.start(run, serial='BOARD-B', request='shared-usb', resources=['usb:slot-one'])
        second = self.start(run, serial='BOARD-B', request='other-board', resources=['usb:slot-two'])
        self.assertEqual(second['board_targets'], ['BOARD-B'])
        release.set()

    def test_maintenance_blocks_new_uart_generation_and_allows_passive_reads(self):
        old = self.device('BOARD-A')
        entered, release, run = self.controlled()
        task = self.start(run, connection_id=old.connection_id)
        self.assertTrue(entered.wait(2))
        self.manager.boards['BOARD-A'].detach(old)
        old.present = False
        new = self.device('BOARD-A')
        with self.assertRaisesRegex(TransactionError, '刷机'):
            new.transactions.check_owner('web', 'client-one')
        with mock.patch.object(new, '_write') as writer:
            self.assertFalse(new.send('must-not-send')[0])
            writer.assert_not_called()
        self.assertIsInstance(new.read(0, 0)[1], bytes)
        other = self.device('BOARD-B')
        with other.transactions.begin('safe-other-board', 1):
            pass
        self.assertEqual(self.tasks.store.get(task['id'])['targets'][0]['connection_id'], old.connection_id)
        release.set()

    def test_active_operation_and_other_clients_input_prevent_admission(self):
        device = self.device('BOARD-A')
        with device.transactions.begin('file-transfer', 1):
            with self.assertRaises(TaskError):
                self.start(lambda context: {})
        with device.transactions.actor_scope('web', 'another-client'):
            owner = device.transactions.claim('web')
        with self.assertRaisesRegex(TaskError, 'owned'):
            self.start(lambda context: {})
        with device.transactions.actor_scope('web', 'another-client'):
            device.transactions.release(owner['id'], 'web')
        with self.maintenance.inspect_scope('BOARD-A', resources=['usb:slot-one']):
            with self.assertRaises(TaskError):
                self.start(lambda context: {})
            with self.assertRaises(TransactionError):
                with device.transactions.begin('input', 1):
                    self.fail('inspection must exclude input')

    def test_cancel_before_adapter_entry_releases_never_started_holds(self):
        gated, proceed = threading.Event(), threading.Event()
        original = self.tasks._run
        def gated_run(context, run):
            gated.set()
            proceed.wait(2)
            original(context, run)
        runner = mock.Mock(side_effect=AssertionError('must not execute after pre-cancel'))
        with mock.patch.object(self.tasks, '_run', side_effect=gated_run):
            task = self.start(runner)
            self.assertTrue(gated.wait(2))
            self.tasks.cancel(task['id'], 'web')
            proceed.set()
            self.assertTrue(self.tasks.close())
        runner.assert_not_called()
        self.assertEqual(self.tasks.store.get(task['id'])['state'], 'cancelled')
        self.assertEqual(self.maintenance.holds(), [])

    def test_restart_preserves_recovery_hold_without_replaying_and_requires_confirmation(self):
        # Simulate a process that died after committing the task/hold and before
        # recording cleanup, without launching an actual external process.
        with self.maintenance.admission('BOARD-A', ['usb:one']) as resources:
            task_id = self.tasks.store.create_board('flash', 'web', '', 'BOARD-A', 'restart-one',
                'a' * 64, {'attempt_id': 'original-attempt'}, resources)
        replacement = TaskManager(self.store)
        self.addCleanup(replacement.close)
        self.assertEqual(replacement.store.get(task_id)['state'], 'interrupted')
        self.assertTrue(all(row['state'] == 'recovery' for row in self.maintenance.holds()))
        with self.assertRaises(TaskError):
            self.start(lambda context: {}, request='restart-two')
        self.assertFalse(self.maintenance.release(task_id))
        self.assertTrue(self.maintenance.holds())
        self.assertTrue(self.maintenance.release(task_id, confirmed=True))
        self.assertEqual(self.maintenance.holds(), [])

    def test_events_are_bounded_with_absolute_cursor_and_explicit_gap(self):
        with self.maintenance.admission('BOARD-A') as resources:
            task_id = self.tasks.store.create_board('flash', 'web', '', 'BOARD-A', 'events-one',
                'a' * 64, {}, resources)
        for number in range(MAX_EVENTS + 5):
            self.flashes.append_event(task_id, {'phase': 'writing', 'done': number})
        page = self.flashes.events(task_id)
        self.assertTrue(page['gap'])
        self.assertEqual(page['events'][0]['cursor'], 6)
        self.assertEqual(page['next_cursor'], 105)
        self.assertEqual(len(self.flashes.events(task_id, 5, MAX_EVENTS)['events']), MAX_EVENTS)
        self.assertFalse(self.flashes.events(task_id, 5)['gap'])
        with self.assertRaises(TaskError):
            self.flashes.append_event(task_id, {'password': 'must-not-persist'})

    def test_plan_scope_digest_and_identity_migration_with_completed_history(self):
        plan = self.flashes.put_plan({'actor': 'agent-one', 'serial': 'BOARD-A', 'binding_revision': 1})
        self.assertEqual(len(plan['digest']), 64)
        with self.assertRaises(TaskError):
            self.flashes.get_plan(plan['plan_id'], actor='agent-two')
        with self.assertRaises(TaskError):
            self.flashes.get_plan(plan['plan_id'], serial='BOARD-B')
        entered, release, run = self.controlled()
        task = self.start(run)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(IdentityError):
            self.store.correct_serial('BOARD-A', 'CORRECTED')
        self.assertIsNotNone(self.store.get('BOARD-A'))
        release.set()
        self.assertTrue(self.tasks.close())
        self.store.correct_serial('BOARD-A', 'CORRECTED')
        self.assertEqual(self.tasks.store.get(task['id'])['board_targets'], ['CORRECTED'])
        self.assertTrue(self.tasks.store.visible_serial(self.tasks.store.get(task['id']), 'CORRECTED'))
        # Original plan intent remains old identity and cannot silently become a
        # new executable plan following archive identity correction.
        self.assertEqual(self.flashes.get_plan(plan['plan_id'], serial='CORRECTED')['serial'], 'BOARD-A')
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_schema_seven_upgrade_preserves_private_credentials_and_old_backup(self):
        path = Path(self.store.path).with_name('schema-seven.sqlite3')
        previous = sqlite3.connect(path)
        try:
            for migrate in schema.REGISTRY_MIGRATIONS[:7]:
                migrate(previous)
            previous.execute('INSERT INTO boards VALUES(?,?,?,?,?,?,?)',
                ('LEGACY', 'legacy', '{}', 1, 2, '', ''))
            previous.execute('INSERT INTO ssh_credentials VALUES(?,?,?,?)',
                ('LEGACY', 'root', 'private-saved-value', 3))
            previous.execute('PRAGMA user_version=7')
            previous.commit()
        finally:
            previous.close()
        upgraded = Store(str(path))
        try:
            self.assertEqual(upgraded.get_ssh_credentials('LEGACY')['revision'], 3)
            self.assertEqual(upgraded.db.execute('SELECT COUNT(*) FROM flash_resource_holds').fetchone()[0], 0)
            with closing(sqlite3.connect(upgraded.schema_backup)) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 7)
                self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='flash_plans'").fetchone())
        finally:
            upgraded.close()


if __name__ == '__main__':
    unittest.main()
