"""Durable tasks use real temporary SQLite and controlled workers, never boards."""
from pathlib import Path
import threading
import tempfile
import unittest

from seriald_core.storage import Store
from seriald_core.tasks import TaskContext, TaskError, TaskManager, TaskStore

TARGET = {'serial': 'SN-ONE', 'connection_id': 'connection-one'}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'registry.sqlite3')
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.tasks = TaskManager(self.store, max_active=1)
        self.addCleanup(self.tasks.close)

    def wait(self, event):
        self.assertTrue(event.wait(2), 'controlled worker did not respond')

    def test_progress_and_result_survive_reopening_registry(self):
        ready, release = threading.Event(), threading.Event()
        def run(context):
            context.progress(10, 20, 'writing')
            ready.set(); release.wait(2)
            return {'ok': True, 'size': 20, 'sha256': 'test-digest'}
        task = self.tasks.start('file-copy', 'agent', 'ignored-client', [TARGET], run)
        self.wait(ready)
        self.assertEqual(self.tasks.store.get(task['id'])['progress']['done'], 10)
        release.set()
        self.assertTrue(self.tasks.close())
        reopened = Store(self.path)
        try:
            record = TaskStore(reopened).get(task['id'])
            self.assertEqual(record['state'], 'completed')
            self.assertEqual(record['result']['size'], 20)
            self.assertEqual(record['client_id'], '')
        finally:
            reopened.close()

    def test_connection_identification_resolves_all_history_without_changing_snapshots(self):
        history = self.tasks.store
        wanted = history.create('file-download', 'agent', '', [{'serial': '', 'connection_id': 'pending'}])
        history.update(wanted, state='failed', error='before identification')
        for number in range(4):
            history.create('identify', 'web', '', [{'serial': '', 'connection_id': 'other-' + str(number)}])
        self.store.bind({'serial': 'SN-NEW'}, 'usb', 'tty', connection_id='pending')
        task = history.get(wanted)
        self.assertEqual(task['targets'], [{'serial': '', 'connection_id': 'pending', 'resolved_serial': 'SN-NEW'}])
        self.assertTrue(history.visible_serial(task, 'SN-NEW'))
        self.assertEqual([row['id'] for row in history.list(target_serial='SN-NEW', limit=1)], [wanted])
        self.assertEqual([row['id'] for row in history.list(serials=['SN-NEW'], actor='agent')], [wanted])
        self.assertEqual(history.list(serials=['SN-OTHER']), [])
        reopened = Store(self.path)
        try:
            self.assertEqual(TaskStore(reopened).get(wanted)['targets'], task['targets'])
        finally:
            reopened.close()

    def test_multi_board_pending_history_is_resolved_per_target_without_widening_scope(self):
        history = self.tasks.store
        task_id = history.create('file-copy', 'agent', '', [
            {'serial': '', 'connection_id': 'one'}, {'serial': '', 'connection_id': 'two'}])
        self.store.bind({'serial': 'SN-A'}, 'usb-one', 'tty', connection_id='one')
        self.store.bind({'serial': 'SN-B'}, 'usb-two', 'tty', connection_id='two')
        for serial in ('SN-A', 'SN-B'):
            self.assertEqual([row['id'] for row in history.list(target_serial=serial)], [task_id])
            self.assertEqual(history.list(serials=[serial]), [])
            self.assertFalse(history.visible_serial(history.get(task_id), serial))
        self.assertEqual([row['id'] for row in history.list(serials=['SN-A', 'SN-B'])], [task_id])

    def test_arbitrary_result_serial_cannot_replace_connection_proof(self):
        history = self.tasks.store
        task_id = history.create('file-download', 'agent', '', [{'serial': '', 'connection_id': 'pending'}])
        history.update(task_id, state='completed', result={'serial': 'UNPROVEN'})
        self.assertFalse(history.visible_serial(history.get(task_id), 'UNPROVEN'))
        self.assertEqual(history.list(serials=['UNPROVEN']), [])
        self.store.bind({'serial': 'PROVEN'}, 'usb', 'tty', connection_id='pending')
        self.assertTrue(history.visible_serial(history.get(task_id), 'PROVEN'))
        self.assertFalse(history.visible_serial(history.get(task_id), 'UNPROVEN'))
        self.assertEqual(history.list(target_serial='UNPROVEN'), [])

    def test_connection_cannot_be_reassigned_and_new_generation_does_not_steal_history(self):
        from seriald_core.storage import IdentityError
        task_id = self.tasks.store.create('file-upload', 'agent', '', [{'serial': '', 'connection_id': 'old'}])
        self.store.bind({'serial': 'SN-A'}, 'usb', 'tty', connection_id='old')
        with self.assertRaises(IdentityError):
            self.store.bind({'serial': 'SN-B'}, 'usb', 'tty', connection_id='old')
        self.assertIsNone(self.store.get('SN-B'))  # Board insert and mapping roll back together.
        self.store.bind({'serial': 'SN-B'}, 'usb', 'tty', connection_id='new')
        self.assertTrue(self.tasks.store.visible_serial(self.tasks.store.get(task_id), 'SN-A'))
        self.assertEqual(self.tasks.store.list(target_serial='SN-B'), [])

    def test_explicit_serial_correction_migrates_resolution_without_rewriting_task_snapshot(self):
        self.store.bind({'serial': 'SN-OLD'}, 'usb', 'tty')
        task_id = self.tasks.store.create('file-download', 'agent', '', [
            {'serial': 'SN-OLD', 'connection_id': 'legacy-known'}])
        self.store.correct_serial('SN-OLD', 'SN-NEW')
        task = self.tasks.store.get(task_id)
        self.assertEqual(task['targets'][0]['serial'], 'SN-OLD')
        self.assertEqual(task['targets'][0]['resolved_serial'], 'SN-NEW')
        self.assertTrue(self.tasks.store.visible_serial(task, 'SN-NEW'))
        self.assertEqual(self.tasks.store.list(target_serial='SN-OLD'), [])

    def test_eeprom_promotion_updates_resolved_history_and_artifact_identity(self):
        import time
        old = {'serial': 'MARKER', 'serial_source': 'boot-marker', 'identity_status': 'provisional'}
        self.store.bind(old, 'usb', 'tty', connection_id='connection')
        task_id = self.tasks.store.create('file-download', 'agent', '', [
            {'serial': 'MARKER', 'connection_id': 'connection'}])
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO transfer_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('artifact', 'agent', 'MARKER', 'download', 'file', 0, 0, '', 'failed', task_id, time.time(), time.time()))
        self.store.promote_eeprom_identity('MARKER', {'serial': 'EEPROM',
            'serial_source': 'eeprom-onie', 'identity_status': 'confirmed'}, 'usb', 'tty',
            connection_id='connection')
        task = self.tasks.store.get(task_id)
        self.assertEqual(task['targets'][0]['serial'], 'MARKER')
        self.assertEqual(task['targets'][0]['resolved_serial'], 'EEPROM')
        self.assertTrue(self.tasks.store.visible_serial(task, 'EEPROM'))
        self.assertEqual(self.store.db.execute('SELECT serial FROM transfer_artifacts').fetchone()[0], 'EEPROM')
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_restart_interrupts_unfinished_records_without_replaying(self):
        history = self.tasks.store
        task_id = history.create('identify', 'web', 'page-one', [TARGET])
        history.update(task_id, state='running', progress={'done': 1, 'total': 2})
        replacement = TaskManager(self.store)
        self.addCleanup(replacement.close)
        record = replacement.store.get(task_id)
        self.assertEqual(record['state'], 'interrupted')
        self.assertEqual(record['progress']['done'], 1)
        self.assertFalse(history.update(task_id, state='completed', result={'ok': True}))
        self.assertEqual(history.get(task_id)['state'], 'interrupted')

    def test_cancel_wakes_worker_and_cannot_overwrite_terminal_state(self):
        ready, wake = threading.Event(), threading.Event()
        calls = []
        def run(context):
            context.on_cancel(lambda: (calls.append('wake'), wake.set()))
            ready.set(); wake.wait(2); context.check()
        task = self.tasks.start('identify', 'agent', '', [TARGET], run)
        self.wait(ready)
        with self.assertRaises(TaskError) as error:
            self.tasks.cancel(task['id'], 'other-agent')
        self.assertEqual(error.exception.status, 403)
        self.tasks.cancel(task['id'], 'web')
        self.assertTrue(self.tasks.close())
        record = self.tasks.store.get(task['id'])
        self.assertEqual(record['state'], 'cancelled')
        self.assertTrue(record['cancel_requested'])
        self.assertEqual(calls, ['wake'])
        self.tasks.cancel(task['id'], 'agent')
        self.assertFalse(self.tasks.store.update(task['id'], progress={'done': 999}))

    def test_completion_can_win_cancel_race_without_claiming_rollback(self):
        ready, wake = threading.Event(), threading.Event()
        def run(context):
            context.on_cancel(wake.set)
            ready.set(); wake.wait(2)
            return {'ok': True}
        task = self.tasks.start('identify', 'agent', '', [TARGET], run)
        self.wait(ready)
        self.tasks.cancel(task['id'], 'agent')
        self.assertTrue(self.tasks.close())
        record = self.tasks.store.get(task['id'])
        self.assertEqual(record['state'], 'completed')
        self.assertTrue(record['cancel_requested'])
        self.assertTrue(record['result']['ok'])

    def test_worker_limit_and_close_refuse_new_commands(self):
        ready, wake = threading.Event(), threading.Event()
        def run(context):
            context.on_cancel(wake.set); ready.set(); wake.wait(2); context.check()
        self.tasks.start('identify', 'agent', '', [TARGET], run); self.wait(ready)
        with self.assertRaises(TaskError) as error:
            self.tasks.start('identify', 'agent', '', [TARGET], run)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(len(self.tasks.store.list()), 1)
        self.assertTrue(self.tasks.close())
        with self.assertRaises(TaskError) as error:
            self.tasks.start('identify', 'agent', '', [TARGET], run)
        self.assertEqual(error.exception.status, 503)

    def test_scoped_listing_excludes_copies_with_an_unshared_target(self):
        history = self.tasks.store
        own = history.create('identify', 'agent', '', [TARGET])
        history.create('file-copy', 'agent', '', [TARGET, {'serial': 'SN-TWO', 'connection_id': 'two'}])
        history.create('identify', 'other-agent', '', [TARGET])
        records = history.list(actor='agent', serials=['SN-ONE'])
        self.assertEqual([record['id'] for record in records], [own])
        self.assertEqual(history.list(serials=[]), [])
        self.assertEqual(len(history.list(limit=1)), 1)
        self.assertEqual(len(history.list(limit=1, offset=1)), 1)
        with self.assertRaises(TaskError):
            history.list(limit=0)

    def test_connection_filter_is_applied_before_pagination_and_preserves_serial_scope(self):
        history = self.tasks.store
        wanted = history.create('identify', 'web', '', [TARGET])
        for number in range(15):
            history.create('identify', 'web', '', [{'serial': 'SN-TWO', 'connection_id': str(number)}])
        records = history.list(connection_id=TARGET['connection_id'], limit=1)
        self.assertEqual([record['id'] for record in records], [wanted])
        self.assertEqual(history.list(connection_id=TARGET['connection_id'], serials=['SN-TWO']), [])
        with self.assertRaises(TaskError):
            history.list(connection_id='')

    def test_pending_identity_result_is_visible_under_confirmed_serial_without_rewriting_snapshot(self):
        history = self.tasks.store
        task_id = history.create('identify', 'web', '', [{'serial': '', 'connection_id': 'pending'}])
        history.update(task_id, state='completed', result={'serial': 'SN-NEW'})
        task = history.get(task_id)
        self.assertEqual(task['targets'][0]['serial'], '')
        self.assertTrue(history.visible_serial(task, 'SN-NEW'))
        self.assertFalse(history.visible_serial(task, 'SN-OTHER'))
        self.assertEqual([row['id'] for row in history.list(serials=['SN-NEW'])], [task_id])
        self.assertEqual(history.list(serials=['SN-OTHER']), [])
        # A two-target operation cannot use one result serial to widen access.
        copy_id = history.create('file-copy', 'web', '', [
            {'serial': '', 'connection_id': 'pending'}, TARGET])
        history.update(copy_id, state='completed', result={'serial': 'SN-NEW'})
        self.assertFalse(history.visible_serial(history.get(copy_id), 'SN-NEW'))
        self.assertEqual([row['id'] for row in history.list(serials=['SN-NEW'])], [task_id])

    def test_progress_and_results_are_validated_without_persisting_bad_values(self):
        history = self.tasks.store
        task_id = history.create('identify', 'web', 'page', [TARGET])
        for progress in ({'done': -1}, {'done': True}, {'phase': 'x' * 161}, {'password': 'secret'}):
            with self.assertRaises(TaskError):
                history.update(task_id, progress=progress)
        with self.assertRaises(ValueError):
            history.update(task_id, result={'rate': float('nan')})
        with self.assertRaises(TaskError):
            history.update(task_id, result={'output': 'x' * 65537})
        with self.assertRaises(TaskError):
            history.update(task_id, result={'nested': [{'ssh_password': 'secret'}]})
        self.assertEqual(history.get(task_id)['progress'], {})
        self.assertIsNone(history.get(task_id)['result'])
        with self.assertRaises(TaskError):
            history.create('identify', 'agent', '', [dict(TARGET, password='secret')])

    def test_cancel_hook_registered_after_request_runs_without_deadlock(self):
        context = TaskContext('test', self.tasks.store)
        context.cancel()
        called = []
        context.on_cancel(lambda: called.append('late'))
        context.cancel()
        self.assertEqual(called, ['late'])

    def test_worker_failure_is_durable_and_capacity_is_released(self):
        entered = threading.Event()
        def fail(context):
            entered.set()
            raise RuntimeError('transport closed')
        task = self.tasks.start('identify', 'web', '', [TARGET], fail)
        self.wait(entered)
        self.assertTrue(self.tasks.close())
        record = self.tasks.store.get(task['id'])
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['error'], 'transport closed')
        self.assertEqual(len(self.tasks._active), 0)
