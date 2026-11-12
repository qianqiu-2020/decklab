"""USB download targets are not invented boards or UART connections."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest

from seriald_core import schema
from seriald_core.board_maintenance import maintenance_for, CACHE_POOL_RESOURCE
from seriald_core.flash_store import FlashStore
from seriald_core.models import Board
from seriald_core.storage import Store
from seriald_core.tasks import TaskError, TaskManager


class FlashTargetFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(str(Path(self.temporary.name) / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        record = self.store.bind({'serial': 'REAL-BOARD'}, '', '')
        self.manager = types.SimpleNamespace(store=self.store, boards={'REAL-BOARD': Board(record)})
        self.maintenance = maintenance_for(self.manager)
        self.tasks = TaskManager(self.store)
        self.addCleanup(self.tasks.close)
        self.flashes = FlashStore(self.store)
        self.target('111111111111111111111111', 'generation-one')

    def target(self, target_id, generation, serial=None, present=True):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO flash_targets VALUES(?,?,?,?,?,?)',
                (target_id, generation, serial, '{}', time.time(), int(present)))

    def start(self, run, target_id='111111111111111111111111', generation='generation-one', serial='',
              request='request-original', digest='a' * 64, **kwargs):
        return self.tasks.start_board('flash', 'web', 'page', serial, run,
            request_id=request, input_digest=digest,
            details={'target_id': target_id, 'attempt_id': 'original-attempt'},
            resources=['flash-usb:local:1.4'], maintenance=self.maintenance,
            target_id=target_id, target_generation=generation, **kwargs)

    def test_unidentified_target_has_true_usb_snapshot_and_no_board_grant(self):
        def run(context):
            self.maintenance.release(context.id, confirmed=True)
            return {'write_result': 'completed'}
        task = self.start(run)
        self.assertTrue(self.tasks.close())
        task = self.tasks.store.get(task['id'])
        self.assertEqual(task['targets'], [])
        self.assertNotIn('board_targets', task)
        self.assertEqual(task['flash_targets'], [{'target_id': '111111111111111111111111', 'generation': 'generation-one'}])
        self.assertEqual(task['flash']['target_id'], '111111111111111111111111')
        self.assertFalse(self.tasks.store.visible_serial(task, 'REAL-BOARD'))
        self.assertEqual(self.tasks.store.list(serials=['REAL-BOARD']), [])
        self.assertEqual(self.tasks.store.list(connection_id='generation-one'), [])
        self.assertEqual([row['id'] for row in self.tasks.store.list(target_id='111111111111111111111111')], [task['id']])
        self.assertEqual(len(self.store.boards()), 1)

    def test_verified_board_and_usb_targets_both_snapshot_without_uart(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE flash_targets SET serial=? WHERE id=?', ('REAL-BOARD', '111111111111111111111111'))
        task = self.start(lambda context: self.maintenance.release(context.id, confirmed=True) or {},
                          serial='REAL-BOARD')
        self.assertTrue(self.tasks.close())
        self.assertEqual(task['board_targets'], ['REAL-BOARD'])
        self.assertEqual(task['targets'], [])
        self.assertTrue(self.tasks.store.visible_serial(task, 'REAL-BOARD'))
        self.assertEqual(len(self.tasks.store.list(serials=['REAL-BOARD'])), 1)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_target_plan_pins_generation_and_cannot_be_reused_for_other_target(self):
        document = {'actor': 'web', 'serial': '', 'target_id': '111111111111111111111111',
                    'target_generation': 'generation-one', 'binding_revision': 1}
        plan = self.flashes.put_plan(document)
        self.assertEqual(self.flashes.get_plan(plan['plan_id'], serial='', target_id='111111111111111111111111')['serial'], '')
        with self.assertRaises(TaskError):
            self.flashes.get_plan(plan['plan_id'], target_id='333333333333333333333333')
        with self.assertRaises(TaskError):
            self.flashes.put_plan(dict(document, actor='shared-agent/example'))
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE flash_targets SET generation=? WHERE id=?', ('replacement', '111111111111111111111111'))
        with self.assertRaisesRegex(TaskError, 'generation'):
            self.flashes.put_plan(document)
        self.assertEqual(self.store.db.execute('SELECT serial FROM flash_plans WHERE id=?',
                                              (plan['plan_id'],)).fetchone()[0], None)

    def test_only_running_owner_may_bridge_original_snapshot_to_checked_final_connection(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run(context):
            entered.set()
            release.wait(3)
            self.maintenance.release(context.id, confirmed=True)
            return {}
        task = self.start(run)
        self.assertTrue(entered.wait(2))
        final = {'port_path': [1, 4], 'usb_address': 8}
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE flash_targets SET generation=?,document=? WHERE id=?',
                ('final-generation', json.dumps({'device': final}), '111111111111111111111111'))
        self.flashes.update_details(task['id'], {'allowed_final_generation': 'final-generation',
                                                 'execution_device': final})
        document = {'actor': 'web', 'serial': '', 'target_id': '111111111111111111111111',
                    'target_generation': 'generation-one', 'selection_task_id': task['id'],
                    'execution_device': final}
        self.assertTrue(self.flashes.put_plan(document)['plan_id'])
        for changed in (dict(document, selection_task_id='0' * 24),
                        dict(document, target_generation='wrong-initial'),
                        dict(document, execution_device={'port_path': [1, 4], 'usb_address': 9})):
            with self.subTest(changed=changed), self.assertRaises(TaskError):
                self.flashes.put_plan(changed)
        self.tasks.store.update(task['id'], state='completed', result={})
        with self.assertRaises(TaskError):
            self.flashes.put_plan(document)
        release.set()

    def test_malformed_lookup_ids_and_target_filters_raise_domain_errors(self):
        malformed = (None, [], {}, 42, True, '', '0' * 23, '0' * 25, 'A' * 24, 'z' * 24)
        for value in malformed:
            for lookup in (self.tasks.store.get, self.flashes.get_plan):
                with self.subTest(value=value, lookup=lookup.__name__), self.assertRaises(TaskError) as error:
                    lookup(value)
                self.assertEqual(error.exception.status, 404)
            if value is not None:
                for serials in (None, []):
                    with self.subTest(value=value, serials=serials), self.assertRaises(TaskError) as error:
                        self.tasks.store.list(target_id=value, serials=serials)
                    self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.tasks.store.list(target_id='f' * 24), [])
        # Legacy tasks without an endpoint snapshot retain their previous behavior.
        self.assertTrue(self.tasks.store.visible_serial({'targets': [], 'kind': 'legacy'}, 'REAL-BOARD'))

    def test_stale_or_absent_target_creates_no_task_and_agents_are_rejected(self):
        for generation in ('stale', ''):
            with self.subTest(generation=generation), self.assertRaises(TaskError):
                self.start(lambda context: {}, generation=generation)
        with self.assertRaises(TaskError):
            self.tasks.start_board('flash', 'shared-agent/example', '', '', lambda context: {},
                request_id='agent-request', input_digest='a' * 64, maintenance=self.maintenance,
                target_id='111111111111111111111111', target_generation='generation-one')
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE flash_targets SET present=0 WHERE id=?', ('111111111111111111111111',))
        with self.assertRaises(TaskError):
            self.start(lambda context: {})
        self.assertEqual(self.tasks.store.list(), [])

    def test_request_is_idempotent_before_capacity_and_shared_usb_is_exclusive(self):
        self.target('222222222222222222222222', 'generation-two')
        entered, release = threading.Event(), threading.Event()
        def run(context):
            entered.set()
            release.wait(3)
            self.maintenance.release(context.id, confirmed=True)
            return {}
        task = self.start(run)
        self.assertTrue(entered.wait(2))
        self.tasks.max_active = 1
        repeated = self.start(lambda context: self.fail('duplicate must not execute'))
        self.assertEqual(task['id'], repeated['id'])
        with self.assertRaisesRegex(TaskError, 'different input'):
            self.start(run, digest='b' * 64)
        self.tasks.max_active = 2
        with self.assertRaises(TaskError):
            self.start(run, target_id='222222222222222222222222', generation='generation-two', request='second-request')
        # Unidentified USB maintenance does not block an unrelated UART board.
        with self.maintenance.operation('REAL-BOARD'):
            pass
        self.assertEqual([row['resource'] for row in self.maintenance.target_holds('111111111111111111111111')],
                         ['flash-target:111111111111111111111111'])
        self.assertEqual(self.maintenance.holds('REAL-BOARD'), [])
        release.set()

    def test_uncertain_inspection_retains_original_generation_after_usb_replacement(self):
        with self.maintenance.inspect_target_scope('111111111111111111111111', 'generation-one', resources=['flash-usb:local:1.4']):
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE flash_targets SET generation=?,present=0 WHERE id=?',
                                      ('replacement', '111111111111111111111111'))
            task = self.maintenance.retain_target_inspection_failure('111111111111111111111111', 'generation-one',
                resources=['flash-usb:local:1.4'], attempt_id='original-attempt')
        self.assertEqual(task['state'], 'failed')
        self.assertTrue(task['flash']['recovery_required'])
        self.assertEqual(task['flash_targets'][0]['generation'], 'generation-one')
        self.assertTrue(all(row['state'] == 'recovery' for row in self.maintenance.holds()))
        self.assertEqual(self.maintenance.holds('REAL-BOARD'), [])
        confirmed = self.maintenance.confirm_recovery(task['id'], 'web', 'original-attempt',
            'Verified the original helper has exited', confirm_stopped=True)
        self.assertFalse(confirmed['flash']['recovery_required'])
        self.assertEqual(confirmed['state'], 'failed')
        self.assertEqual(self.maintenance.holds(), [])

    def test_uncertain_import_reserves_cache_only_and_restart_does_not_replay(self):
        task = self.maintenance.retain_target_preparation_failure('111111111111111111111111', 'generation-one',
            attempt_id='original-attempt')
        self.assertEqual([row['resource'] for row in self.maintenance.holds()], [CACHE_POOL_RESOURCE])
        with self.assertRaises(TaskError):
            self.maintenance.check_preparation()
        with self.maintenance.target_admission('111111111111111111111111', 'generation-one'):
            pass
        with self.maintenance.operation('REAL-BOARD'):
            pass
        self.tasks.store.recover()
        self.assertEqual(self.tasks.store.get(task['id'])['state'], 'failed')

    def test_schema_nine_upgrade_keeps_old_plans_labels_and_private_credentials(self):
        path = Path(self.temporary.name) / 'registry-nine.sqlite3'
        with sqlite3.connect(path) as old:
            for migrate in schema.REGISTRY_MIGRATIONS[:9]:
                migrate(old)
            old.execute('INSERT INTO boards VALUES(?,?,?,?,?,?,?)', ('OLD', 'old', '{}', 1, 2, '', ''))
            old.execute('INSERT INTO attachment_labels VALUES(?,?)', ('usb-slot', 'pending-label'))
            old.execute('INSERT INTO ssh_credentials VALUES(?,?,?,?)', ('OLD', 'root', 'private-value', 3))
            old.execute('INSERT INTO flash_plans VALUES(?,?,?,?,?,?)',
                        ('444444444444444444444444', 'web', 'OLD', 'd' * 64, json.dumps({'actor': 'web', 'serial': 'OLD'}), 1))
            old.execute('PRAGMA user_version=9')
        upgraded = Store(str(path))
        try:
            plan = FlashStore(upgraded).get_plan('444444444444444444444444', serial='OLD')
            self.assertEqual(plan['digest'], 'd' * 64)
            self.assertEqual(upgraded.db.execute('SELECT label FROM attachment_labels').fetchone()[0], 'pending-label')
            self.assertEqual(upgraded.get_ssh_credentials('OLD')['revision'], 3)
            self.assertEqual(upgraded.db.execute('PRAGMA foreign_key_check').fetchall(), [])
            with sqlite3.connect(upgraded.schema_backup) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 9)
                self.assertEqual(backup.execute('SELECT COUNT(*) FROM flash_plans').fetchone()[0], 1)
        finally:
            upgraded.close()
