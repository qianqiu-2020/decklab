"""Unknown flasher cleanup pins artifacts and requires an explicit human receipt."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import threading
import types
import unittest

from seriald_core.board_maintenance import maintenance_for
from seriald_core.flash_store import FlashStore
from seriald_core.models import Board
from seriald_core.storage import Store
from seriald_core.tasks import TaskError, TaskStore
from seriald_core.transfer_artifacts import ArtifactPolicy, TransferArtifacts


class FlashRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = Store(str(root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        record = self.store.bind({'serial': 'BOARD-A'}, '', '')
        self.manager = types.SimpleNamespace(store=self.store, boards={'BOARD-A': Board(record)})
        self.maintenance = maintenance_for(self.manager)
        self.tasks, self.flashes = TaskStore(self.store), FlashStore(self.store)
        self.spool = TransferArtifacts(self.store, root / 'spool',
            ArtifactPolicy(max_file=1024, max_total=2048, reserve_free=0, idle_seconds=1, ready_seconds=1))

    def reserved_task(self, state='failed'):
        with self.maintenance.admission('BOARD-A', ['flash:local:one']) as resources:
            task_id = self.tasks.create_board('flash', 'web', '', 'BOARD-A', 'request-original', 'a' * 64,
                {'attempt_id': 'attempt-original', 'recovery_required': True,
                 'write_result': 'unknown-or-partial'}, resources)
        if state != 'queued':
            self.tasks.update(task_id, state=state)
        self.maintenance.keep_recovery(task_id)
        return task_id

    def claimed_source(self, task_id):
        row = self.spool.create('upload', 'image.bin', 7, serial='BOARD-A')
        self.spool.append(row['id'], 0, b'payload', serial='BOARD-A')
        sealed = self.spool.seal(row['id'], serial='BOARD-A')
        self.spool.claim(row['id'], task_id, serial='BOARD-A')
        return sealed

    def confirm(self, task_id, **values):
        arguments = dict(actor='web', expected_attempt_id='attempt-original', reason='Checked original helper exited',
                         confirm_stopped=True, artifacts=self.spool)
        arguments.update(values)
        return self.maintenance.confirm_recovery(task_id, **arguments)

    def test_restart_does_not_release_claim_or_delete_image_while_execution_unknown(self):
        task_id = self.reserved_task('running')
        artifact = self.claimed_source(task_id)
        self.tasks.recover()
        self.spool.recover()
        self.spool.expire(artifact['updated'] + 100)
        self.assertEqual(self.tasks.get(task_id)['state'], 'interrupted')
        self.assertEqual(self.spool.get(artifact['id'])['state'], 'claimed')
        with self.spool.open(artifact['id'], task_id=task_id) as source:
            self.assertEqual(source.read(), b'payload')
        self.assertTrue(self.spool._path(artifact['id'], '.bin').exists())
        with self.assertRaises(TaskError):
            with self.maintenance.inspect_scope('BOARD-A', ['flash:local:one']):
                self.fail('unknown execution must keep exclusion')

    def test_periodic_expiry_preserves_task_attached_ready_source_with_recovery_hold(self):
        task_id = self.reserved_task()
        artifact = self.claimed_source(task_id)
        # A partial cleanup or older recovery may have returned a row to ready
        # while preserving its task reference. The durable hold still wins.
        self.spool._update(artifact['id'], state='ready')
        self.spool.expire(artifact['updated'] + 100)
        self.assertEqual(self.spool.get(artifact['id'])['state'], 'ready')
        self.assertTrue(self.spool._path(artifact['id'], '.bin').exists())
        with self.assertRaises(TaskError):
            self.spool.remove(artifact['id'])
        with self.assertRaises(TaskError):
            self.spool.fail(artifact['id'], task_id)
        self.assertTrue(self.spool._path(artifact['id'], '.bin').exists())

    def test_explicit_recovery_preserves_failed_write_result_and_is_idempotent(self):
        task_id = self.reserved_task()
        artifact = self.claimed_source(task_id)
        task = self.confirm(task_id)
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['flash']['write_result'], 'unknown-or-partial')
        self.assertFalse(task['flash']['recovery_required'])
        self.assertTrue(task['flash']['recovery_confirmation']['process_stopped_confirmed'])
        self.assertEqual(self.maintenance.holds(), [])
        self.assertEqual(self.spool.get(artifact['id'])['state'], 'ready')
        self.assertIsNone(self.spool.get(artifact['id'])['task_id'])
        self.assertEqual(self.confirm(task_id)['id'], task_id)
        self.assertEqual(len([event for event in self.store.events('BOARD-A')
                              if event['action'] == 'flash-recovery-confirmed']), 1)
        # Now ordinary retention can expire the no-longer-owned source.
        self.spool.expire(self.spool.get(artifact['id'])['updated'] + 100)
        self.assertEqual(self.spool.get(artifact['id'])['state'], 'expired')

    def test_recovery_rejects_agent_stale_attempt_missing_reason_and_live_task(self):
        task_id = self.reserved_task()
        for values in ({'actor': 'agent'}, {'expected_attempt_id': 'another-attempt'},
                       {'reason': ''}, {'confirm_stopped': False}):
            with self.subTest(values=values), self.assertRaises(TaskError):
                self.confirm(task_id, **values)
        self.assertTrue(self.maintenance.holds())
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE tasks SET state='running' WHERE id=?", (task_id,))
        with self.assertRaisesRegex(TaskError, 'still active'):
            self.confirm(task_id)

    def test_reader_cleanup_failure_keeps_every_recovery_resource(self):
        task_id = self.reserved_task()
        artifact = self.claimed_source(task_id)
        with self.spool.open(artifact['id'], task_id=task_id):
            with self.assertRaisesRegex(TaskError, 'reader is still active'):
                self.confirm(task_id)
            self.assertEqual(len(self.maintenance.holds()), 2)
            self.assertTrue(self.flashes.details(task_id)['recovery_required'])
        self.confirm(task_id)
        self.assertEqual(self.maintenance.holds(), [])

    def test_failed_inspection_retains_existing_scope_without_worker_or_fake_uart(self):
        with self.maintenance.inspect_scope('BOARD-A', ['flash:local:one']):
            task = self.maintenance.retain_inspection_failure('BOARD-A', ['flash:local:one'],
                attempt_id='inspect-attempt', details={'binding_revision': 1})
            self.assertEqual(task['kind'], 'flash-inspect')
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['targets'], [])
            self.assertEqual(task['board_targets'], ['BOARD-A'])
        self.assertTrue(all(row['state'] == 'recovery' for row in self.maintenance.holds()))
        with self.assertRaises(TaskError):
            self.maintenance.retain_inspection_failure('BOARD-A', attempt_id='outside-scope')
        recovered = self.confirm(task['id'], expected_attempt_id='inspect-attempt')
        self.assertEqual(recovered['flash']['write_result'], 'not-started')
        self.assertEqual(recovered['state'], 'failed')

    def test_inspection_recovery_keeps_workspace_private_in_public_task_details(self):
        private = '/private/flash/cache/plan-original'
        with self.maintenance.inspect_scope('BOARD-A', ['flash:local:one']):
            task = self.maintenance.retain_inspection_failure('BOARD-A', ['flash:local:one'],
                attempt_id='inspect-private', details={
                    'workspace': private, 'executor': {'cache_path': private + '/image.bin'},
                    'plan': {'files': [{'path': private + '/payload.bin'}],
                             'writes': [{'storage': 'emmc', 'partition': 'rootfs', 'size': 7}]}})
        self.assertNotIn(private, json.dumps(task))
        self.assertNotIn(private, json.dumps(self.tasks.list(target_serial='BOARD-A')))
        self.assertEqual(self.flashes.details(task['id'])['workspace'], private)
        self.assertEqual(self.flashes.public_details(task['id'])['plan']['writes'][0]['partition'], 'rootfs')
        self.assertTrue(task['flash']['recovery_required'])

    def test_unknown_preparation_pins_cache_pool_without_blocking_board_or_prepared_flash(self):
        task = self.maintenance.retain_preparation_failure('BOARD-A', attempt_id='prepare-original',
            details={'workspace': '/private/cache/original'})
        self.assertEqual(task['kind'], 'flash-inspect')
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(task['board_targets'], ['BOARD-A'])
        self.assertEqual(task['targets'], [])
        self.assertEqual([row['resource'] for row in self.maintenance.holds()], ['flash-cache-pool:local'])
        self.assertEqual(self.maintenance.holds('BOARD-A'), [])
        self.assertNotIn('/private/cache/original', json.dumps(task))
        with self.maintenance.operation('BOARD-A'):
            self.maintenance.check('BOARD-A')
        with self.maintenance.admission('BOARD-A', ['flash:local:one']):
            pass  # A previously prepared write can still acquire its real resources.
        with self.assertRaisesRegex(TaskError, task['id']):
            self.maintenance.check_preparation()
        artifact = self.claimed_source(task['id'])
        self.spool.recover()
        self.assertEqual(self.spool.get(artifact['id'])['state'], 'claimed')
        self.confirm(task['id'], expected_attempt_id='prepare-original')
        self.maintenance.check_preparation()
        self.assertEqual(self.maintenance.holds(), [])

    def test_preparation_failure_registration_is_atomic_across_database_connections(self):
        second_store = Store(self.store.path)
        self.addCleanup(second_store.close)
        second = maintenance_for(types.SimpleNamespace(store=second_store, boards=self.manager.boards))
        gate = threading.Barrier(2)
        def attempt(arguments):
            coordinator, attempt_id = arguments
            gate.wait(2)
            try:
                task = coordinator.retain_preparation_failure('BOARD-A', attempt_id=attempt_id)
                return task['id']
            except TaskError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(attempt, [(self.maintenance, 'prepare-one'), (second, 'prepare-two')]))
        self.assertEqual(sum(isinstance(value, str) for value in results), 1)
        self.assertIn(423, results)
        self.assertEqual(len(self.tasks.list(target_serial='BOARD-A')), 1)
        self.assertEqual(len(self.maintenance.holds()), 1)


if __name__ == '__main__':
    unittest.main()
