"""Board input ownership with fake connections and private temporary SQLite."""
import json
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
from unittest import mock

from decklab_core.actions import DeviceActions
from decklab_core.board_ownership import ownership_for
from decklab_core.device import Device
from decklab_core.devices import Manager
from decklab_core.storage import Store
from decklab_core.transactions import TransactionError


class BoardOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        logs = mock.patch('decklab_core.device.LOG_ROOT', directory.name)
        logs.start()
        self.addCleanup(logs.stop)
        self.path = str(Path(directory.name) / 'registry.sqlite3')
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        for serial in ('BOARD-A', 'BOARD-B'):
            self.store.bind({'serial': serial, 'serial_source': 'eeprom-onie'}, '', '', serial.lower())
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('board-a')
        self.device.fd, self.device.present = 98765, True
        self.owner = ownership_for(self.manager)
        self.actions = DeviceActions(self.manager)

    def claim(self, actor='platform', serial='BOARD-A'):
        return self.owner.claim(serial, actor)['execution_lease']

    def expire(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE board_input_owners SET expires=0')

    def test_default_shared_input_remains_and_board_proof_is_not_public(self):
        with mock.patch.object(self.device, '_write', return_value=(True, '')) as write:
            for actor in ('one', 'two'):
                self.assertEqual(self.actions.execute('send', {'data': 'x'}, self.device, actor).status, 200)
            proof = self.claim()
            public = self.owner.current('BOARD-A')
            self.assertNotIn(proof['token'], json.dumps(public))
            self.assertNotIn(proof['token'], str(self.store.db.execute('SELECT * FROM board_input_owners').fetchall()))
            self.assertEqual(self.actions.execute('send', {'data': 'x'}, self.device, 'platform').status, 423)
            self.assertEqual(self.actions.execute('send', {'data': 'x', 'execution_lease': proof}, self.device,
                                                  'platform').status, 200)
            denied = self.actions.execute('send', {'data': 'x', 'execution_lease': proof}, self.device, 'other')
            self.assertEqual(denied.status, 423)
            self.assertEqual(write.call_count, 3)

    def test_platform_tracker_allows_execution_claim_and_fences_second_plan(self):
        from decklab_core.board_maintenance import maintenance_for
        from decklab_core.tasks import TaskError

        maintenance = maintenance_for(self.manager)
        task_store = self.manager.tasks.store
        with maintenance.team_plan_admission('BOARD-A') as resources:
            task_id = task_store.create_board('team-plan', 'web', '', 'BOARD-A', 'plan-request',
                                              'a' * 64, {}, resources, self.device.connection_id)
        self.assertEqual(task_store.get(task_id)['state'], 'queued')
        with self.assertRaises(TaskError):
            with maintenance.team_plan_admission('BOARD-A'):
                self.fail('two plans reserved the same board')
        proof = self.claim()
        with self.assertRaises(TransactionError):
            self.claim('other')
        self.owner.release('BOARD-A', 'platform', '', proof)
        self.assertIsNone(self.owner.current('BOARD-A'))

    def test_concurrent_claims_have_one_winner(self):
        gate = threading.Barrier(3)
        results = []
        def run(actor):
            gate.wait()
            try:
                results.append(self.owner.claim('BOARD-A', actor))
            except TransactionError as error:
                results.append(error.status)
        threads = [threading.Thread(target=run, args=(actor,)) for actor in ('a', 'b')]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertIn(423, results)

    def test_board_ownership_survives_uart_reenumeration_and_other_board_is_free(self):
        proof = self.claim()
        old = self.device
        old.present = False
        new = Device('new-uart', '', manager=self.manager, board=old.board)
        self.addCleanup(new.close_log)
        self.addCleanup(new.terminal.screen.close)
        old.board.attach(new)
        with self.assertRaises(TransactionError):
            new.transactions.check_owner('other')
        with self.owner.scope('platform', proof=proof):
            new.transactions.check_owner('platform')
        self.assertEqual(self.owner.current('BOARD-A')['id'], proof['id'])
        self.owner.check('BOARD-B', 'other')

    def test_automatic_terminal_replies_do_not_bypass_board_ownership(self):
        from contextlib import nullcontext
        handle = mock.Mock()
        handle.write.return_value = len('\x1b[2;1R')
        with mock.patch('decklab_core.device.write_serial', return_value=(True, '')) as write:
            with mock.patch.object(self.device, 'fd', handle) if sys.platform == 'win32' else nullcontext():
                self.assertTrue(self.device._protocol_reply('\x1b[2;1R', self.device.connection_id))
                self.claim()
                self.assertFalse(self.device._protocol_reply('\x1b[2;1R', self.device.connection_id))
                self.assertEqual(handle.write.call_count if sys.platform == 'win32' else write.call_count, 1)

    def test_ssh_parser_replies_remain_passive_during_board_ownership(self):
        from decklab_core.session_base import SessionBase
        session = SessionBase('ssh-protocol', '127.0.0.1', 'fixture', str(Path(self.path).parent))
        self.addCleanup(session.close)
        session._board_device = self.device
        with mock.patch.object(session.terminal, 'feed', return_value=['\x1b[2;1R']), \
                mock.patch.object(session, 'write', return_value=True) as write:
            session._append(b'first response request')
            self.claim()
            session._append(b'second response request')
            self.assertEqual(write.call_count, 1)
            self.assertIn(b'second response request', bytes(session.buf))

    def test_expiry_fences_old_owner_and_requires_explicit_recovery(self):
        proof = self.claim()
        self.expire()
        with self.owner.scope('platform', proof=proof):
            with self.assertRaisesRegex(TransactionError, '恢复'):
                self.owner.check('BOARD-A')
        self.assertEqual(self.owner.current('BOARD-A')['state'], 'recovery')
        with self.assertRaises(TransactionError):
            self.owner.renew('BOARD-A', 'platform', '', proof)
        with self.assertRaises(TransactionError):
            self.owner.release('BOARD-A', 'platform', '', proof)
        self.owner.release('BOARD-A', 'platform', '', proof, stopped=True, reason='original operation checked')
        next_proof = self.claim('next')
        self.assertEqual(next_proof['generation'], proof['generation'] + 1)
        with self.owner.scope('platform', proof=proof):
            with self.assertRaises(TransactionError):
                self.owner.check('BOARD-A')

    def test_renew_and_release_check_secret_actor_and_web_client(self):
        response = self.owner.claim('BOARD-A', 'web', 'page-a')
        proof = response['execution_lease']
        for actor, client, token in [('other', '', proof), ('web', 'page-b', proof),
                ('web', 'page-a', dict(proof, token='x' * 43))]:
            with self.assertRaises(TransactionError):
                self.owner.renew('BOARD-A', actor, client, token)
        renewed = self.owner.renew('BOARD-A', 'web', 'page-a', proof, 600)
        self.assertGreater(renewed['input_owner']['expires'], response['input_owner']['expires'])
        self.owner.release('BOARD-A', 'web', 'page-a', proof)
        self.assertIsNone(self.owner.current('BOARD-A'))
        with self.owner.scope('web', 'page-a', proof):
            with self.assertRaises(TransactionError):
                self.owner.check('BOARD-A')

    def test_active_operation_blocks_release_and_human_recovery_fences_before_waiting(self):
        proof = self.claim()
        with self.owner.scope('platform', proof=proof):
            with self.owner.maintenance.operation('BOARD-A'):
                with self.assertRaises(TransactionError):
                    self.owner.release('BOARD-A', 'platform', '', proof)
                with self.assertRaises(TransactionError):
                    self.owner.recover('BOARD-A', 'web', proof['generation'], stopped=True, reason='take over')
                self.assertEqual(self.owner.current('BOARD-A')['state'], 'recovery')
                with self.assertRaises(TransactionError):
                    self.owner.check('BOARD-A')
        self.owner.recover('BOARD-A', 'web', proof['generation'], stopped=True, reason='stopped and checked')

    def test_background_worker_keeps_proof_and_expired_worker_cannot_write(self):
        proof = self.claim()
        entered, proceed = threading.Event(), threading.Event()
        checks = []
        def run(context):
            context.issued()
            checks.append('entered')
            entered.set()
            proceed.wait(2)
            context.check()
            checks.append('wrote')
            return {'ok': True}
        with self.owner.scope('platform', proof=proof):
            task = self.manager.tasks.start('identify', 'platform', '',
                [{'serial': 'BOARD-A', 'connection_id': self.device.connection_id}], run)
        self.assertTrue(entered.wait(2))
        self.expire()
        proceed.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.manager.tasks.store.get(task['id'])
            if result['state'] not in ('queued', 'running'):
                break
            time.sleep(.01)
        self.assertEqual(checks, ['entered'])
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(self.owner.current('BOARD-A')['state'], 'recovery')

    def test_restart_keeps_recovery_and_does_not_renew_old_proof(self):
        proof = self.claim()
        from decklab_core.board_ownership import BoardInputOwnership
        restarted = BoardInputOwnership(self.owner.maintenance)
        self.assertEqual(restarted.current('BOARD-A')['state'], 'recovery')
        with self.assertRaises(TransactionError):
            restarted.renew('BOARD-A', 'platform', '', proof)

    def test_atomic_claim_does_not_interrupt_existing_connection_owner(self):
        self.device.transactions.claim('other')
        with self.assertRaises(TransactionError):
            self.claim()
        self.assertIsNone(self.owner.current('BOARD-A'))

    def test_ownership_reads_do_not_commit_the_enclosing_task_transaction(self):
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            self.store.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                                  ('BOARD-A', time.time(), 'test', 'must-rollback', ''))
            self.owner.current('BOARD-A')
            self.owner.check('BOARD-A')
            self.assertTrue(self.store.db.in_transaction)
            self.store.db.rollback()
        self.assertFalse(self.store.db.execute("SELECT 1 FROM events WHERE action='must-rollback'").fetchone())

    def test_late_recovery_cannot_release_a_replacement_owner(self):
        from contextlib import contextmanager
        proof = self.claim()
        original = self.owner._transaction
        replacement, injected = {}, False
        @contextmanager
        def interleaved_transaction():
            nonlocal injected
            with original():
                yield
            if not injected:
                injected = True
                self.owner.release('BOARD-A', 'platform', '', proof, stopped=True, reason='checked by another request')
                replacement.update(self.claim('replacement'))
        with mock.patch.object(self.owner, '_transaction', side_effect=interleaved_transaction):
            with self.assertRaisesRegex(TransactionError, '已变化'):
                self.owner.recover('BOARD-A', 'web', proof['generation'], stopped=True, reason='late recovery')
        current = self.owner.current('BOARD-A')
        self.assertEqual((current['id'], current['state']), (replacement['id'], 'active'))

    def test_hardware_recovery_preserves_failure_and_only_releases_observed_task(self):
        tasks = self.manager.tasks.store
        task_id = tasks.create('hardware-reset', 'platform', '',
                               [{'serial': 'BOARD-A', 'connection_id': self.device.connection_id}])
        tasks.retain_cleanup_failure(task_id, 'BOARD-A')
        tasks.update(task_id, state='failed', error='helper cleanup was not confirmed', outcome='unknown')
        task = tasks.get(task_id)
        self.assertFalse(self.manager.tasks.actor_stopped('platform'))
        maintenance = self.owner.maintenance
        for actor, revision, stopped in [('platform', task['updated'], True),
                ('web', task['updated'] - 1, True), ('web', task['updated'], False)]:
            from decklab_core.tasks import TaskError
            with self.assertRaises(TaskError):
                maintenance.recover_operation(task_id, actor, revision, stopped, 'original process checked')
        result = maintenance.recover_operation(task_id, 'web', task['updated'], True, 'original process checked')
        self.assertEqual((result['state'], result['outcome']), ('failed', 'unknown'))
        self.assertIs(result['result']['cleanup_confirmed'], False)
        self.assertTrue(result['result']['recovery_confirmation']['confirmed_stopped'])
        self.assertEqual(maintenance.holds('BOARD-A'), [])
        self.assertTrue(self.manager.tasks.actor_stopped('platform'))
        repeated = maintenance.recover_operation(task_id, 'web', task['updated'], True, 'original process checked')
        self.assertEqual(result['updated'], repeated['updated'])


if __name__ == '__main__':
    unittest.main()
