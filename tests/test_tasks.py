"""Durable tasks use real temporary SQLite and controlled workers, never boards."""
from pathlib import Path
import hashlib
import json
import os
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

    def test_private_request_digest_deduplicates_before_capacity_and_survives_reopen(self):
        document = {'kind': 'ssh-connect', 'connection_id': 'one', 'ssh_password': 'low-entropy-secret'}
        digest = self.tasks.store.request_digest(document)
        ready, release = threading.Event(), threading.Event()
        calls = []
        def run(context):
            calls.append(context.id)
            ready.set(); release.wait(2)
            return {'ok': True}
        task = self.tasks.start('ssh-connect', 'agent', '', [TARGET], run,
                                request_id='ssh-request-001', input_digest=digest)
        self.wait(ready)
        same = self.tasks.start('ssh-connect', 'agent', '', [TARGET], run,
                                request_id='ssh-request-001', input_digest=digest)
        self.assertEqual(same['id'], task['id'])
        changed = self.tasks.store.request_digest(dict(document, ssh_password='different-secret'))
        with self.assertRaises(TaskError) as failure:
            self.tasks.start('ssh-connect', 'agent', '', [TARGET], run,
                             request_id='ssh-request-001', input_digest=changed)
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(calls, [task['id']])
        release.set(); self.assertTrue(self.tasks.close())
        reopened = Store(self.path)
        try:
            history = TaskStore(reopened)
            self.assertEqual(history.request_digest(document), digest)
            self.assertEqual(history.find_request('agent', 'ssh-request-001', digest), task['id'])
            self.assertIsNone(history.find_request('other', 'ssh-request-001'))
            self.assertEqual(history.get(task['id'])['request_id'], 'ssh-request-001')
            private = '\n'.join(reopened.db.iterdump())
            for secret in ('low-entropy-secret', 'different-secret', hashlib.sha256(b'low-entropy-secret').hexdigest()):
                self.assertNotIn(secret, private)
            self.assertNotIn('input_digest', json.dumps(history.get(task['id'])))
        finally:
            reopened.close()

    def test_missing_or_public_request_key_fails_closed_without_replacement(self):
        digest = self.tasks.store.request_digest({'ssh_password': 'private'})
        self.tasks.store.create('ssh-connect', 'agent', '', [TARGET], 'private-key-request', digest)
        key = Path(self.tmp.name) / '.task-request-hmac.key'
        original = key.read_bytes()
        if os.name == 'posix':
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            key.chmod(0o644)
            reopened = Store(self.path)
            try:
                with self.assertRaises(TaskError):
                    TaskStore(reopened).request_digest({'ssh_password': 'private'})
                self.assertEqual(key.read_bytes(), original)
            finally:
                reopened.close()
            key.chmod(0o600)
        key.unlink()
        reopened = Store(self.path)
        try:
            with self.assertRaises(TaskError) as failure:
                TaskStore(reopened).request_digest({'ssh_password': 'private'})
            self.assertEqual(failure.exception.status, 503)
            self.assertFalse(key.exists())
        finally:
            reopened.close()

    def test_concurrent_request_key_initialization_publishes_one_complete_key(self):
        barrier = threading.Barrier(2)
        values, failures = [], []
        def initialize():
            connection = Store(self.path)
            try:
                barrier.wait(2)
                values.append(TaskStore(connection).request_digest({'ssh_password': 'private'}))
            except Exception as error:
                failures.append(error)
            finally:
                connection.close()
        workers = [threading.Thread(target=initialize) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0], values[1])
        self.assertEqual(len((Path(self.tmp.name) / '.task-request-hmac.key').read_bytes()), 32)

    def test_outcome_is_independent_of_lifecycle_and_internal_metadata_is_hidden(self):
        def run(context):
            context.issued()
            context.mark_outcome('observed')
            return {'ok': True}
        task = self.tasks.start('identify', 'agent', '', [TARGET], run, tracks_outcome=True)
        self.assertTrue(self.tasks.close())
        record = self.tasks.store.get(task['id'])
        self.assertEqual((record['state'], record['outcome']), ('completed', 'observed'))
        self.assertEqual(record['result'], {'ok': True})
        self.assertNotIn('_task_outcome', json.dumps(record))
        with self.assertRaises(TaskError):
            self.tasks.store.update(task['id'], outcome='success')
        with self.assertRaises(TaskError):
            self.tasks.store.update(task['id'], result={'_task_outcome': 'verified'})

    def test_restart_keeps_unissued_but_marks_issued_outcome_unknown(self):
        waiting = self.tasks.store.create('identify', 'agent', '', [TARGET])
        issued = self.tasks.store.create('hardware-reset', 'agent', '', [TARGET])
        self.tasks.store.update(issued, state='running', outcome='issued')
        self.tasks.store.recover()
        self.assertEqual(self.tasks.store.get(waiting)['outcome'], 'not-issued')
        self.assertEqual(self.tasks.store.get(issued)['outcome'], 'unknown')
        self.assertEqual(self.tasks.store.get(issued)['state'], 'interrupted')

    def test_revoke_check_precedes_replay_and_actor_stopped_waits_for_cleanup(self):
        ready, cleanup, release = threading.Event(), threading.Event(), threading.Event()
        def run(context):
            context.issued()
            ready.set()
            try:
                context.cancelled.wait(2)
                context.check()
            finally:
                cleanup.set(); release.wait(2)
        digest = self.tasks.store.request_digest({'kind': 'identify'})
        task = self.tasks.start('identify', 'agent', '', [TARGET], run,
                                request_id='revoke-request-001', input_digest=digest, tracks_outcome=True)
        self.wait(ready)
        self.tasks.cancel_actor('agent'); self.wait(cleanup)
        self.assertFalse(self.tasks.actor_stopped('agent'))
        class Revoked:
            def check_actor(self, actor):
                raise TaskError('actor revoked', 403)
        self.tasks.agent_grants = Revoked()
        with self.assertRaises(TaskError) as failure:
            self.tasks.start('identify', 'agent', '', [TARGET], run,
                             request_id='revoke-request-001', input_digest=digest)
        self.assertEqual(failure.exception.status, 403)
        release.set(); self.assertTrue(self.tasks.close())
        self.assertTrue(self.tasks.actor_stopped('agent'))
        self.assertEqual(self.tasks.store.get(task['id'])['outcome'], 'unknown')

    def test_failed_cleanup_retains_board_and_prevents_false_stopped_receipt_after_worker_exit(self):
        task_id = self.tasks.store.create('hardware-reset', 'agent', '', [TARGET])
        self.tasks.store.update(task_id, state='running', outcome='issued')
        self.tasks.store.retain_cleanup_failure(task_id, TARGET['serial'])
        self.tasks.store.update(task_id, state='failed', error='process stop not confirmed')
        record = self.tasks.store.get(task_id)
        self.assertEqual(record['result'], {'cleanup_confirmed': False})
        self.assertEqual(record['outcome'], 'unknown')
        self.assertFalse(self.tasks.actor_stopped('agent'))
        self.assertEqual(self.store.db.execute('SELECT state FROM flash_resource_holds WHERE task_id=?',
                                              (task_id,)).fetchone()[0], 'recovery')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM flash_details WHERE task_id=?',
                                              (task_id,)).fetchone()[0], 0)
        # A hold disappearing alone is not proof that a failed cleanup ended.
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM flash_resource_holds WHERE task_id=?', (task_id,))
        self.assertFalse(self.tasks.actor_stopped('agent'))
        # The human recovery path adds evidence without rewriting old failure.
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE tasks SET result=json_set(result,'$.recovery_confirmation',json(?)) WHERE id=?",
                (json.dumps({'confirmed_stopped': True, 'actor': 'web'}), task_id))
        self.assertTrue(self.tasks.actor_stopped('agent'))
        self.assertFalse(self.tasks.store.get(task_id)['result']['cleanup_confirmed'])

    @unittest.skipUnless(os.name == 'posix', 'symlink and owner mode protection uses POSIX')
    def test_request_key_symlink_is_never_followed(self):
        target = Path(self.tmp.name) / 'not-a-task-key'
        target.write_bytes(b'x' * 32)
        target.chmod(0o600)
        (Path(self.tmp.name) / '.task-request-hmac.key').symlink_to(target)
        with self.assertRaises(TaskError):
            self.tasks.store.request_digest({'ssh_password': 'private'})
        self.assertEqual(target.read_bytes(), b'x' * 32)

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

    def test_flash_history_includes_offline_usb_and_filters_before_pagination(self):
        history = self.tasks.store
        wanted = history.create('flash', 'web', '', [{'serial':'', 'connection_id':'usb-session'}])
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO flash_targets(id,generation,document,seen,present) VALUES(?,?,'{}',0,0)",
                                  ('a' * 24, 'b' * 24))
            self.store.db.execute('INSERT INTO task_flash_targets VALUES(?,?,?)', (wanted, 'a' * 24, 'b' * 24))
        history.create('identify', 'web', '', [TARGET])
        self.assertEqual([t['id'] for t in history.list(flash_history=True, limit=1)], [wanted])
        self.assertEqual(history.list(flash_history=True, serials=['SN-ONE']), [])
        self.assertEqual(history.list(flash_history=True, actor='agent'), [])

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
        for progress in ({'done': -1}, {'done': True}, {'phase': 'x' * 161}, {'password': 'secret'},
                         {'phase': 'writing', 'bytes_per_second': -1}, {'partition': 'outside-writing'},
                         {'phase': 'writing', 'partition': ['invalid']}, {'phase': 'writing', 'password': 'secret'}):
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
