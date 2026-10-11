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
