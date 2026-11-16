"""Reference-client contract through real handlers and temporary SQLite; no sockets or boards."""
import copy
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import urlsplit

import serialctl as ctl
from seriald_core.devices import Manager
from seriald_core.storage import Store
from seriald_core import web
from seriald_core.results import response
from examples.platform_executor import run_attempt


class MemoryHTTP:
    """Replace only transport, keeping the public handler and task store real."""
    def __init__(self, manager):
        self.manager, self.calls = manager, []
        self.actor = 'platform-contract'
        self.lose_submission = False
        self.lose_lookup = False

    def __call__(self, request, timeout=None):
        parsed = urlsplit(request.full_url)
        body = request.data or b''
        self.calls.append((request.get_method(), parsed.path, parsed.query, json.loads(body) if body else None))
        if self.lose_lookup and parsed.path == '/api/task':
            raise TimeoutError('lookup unavailable')
        handler = web.Handler.__new__(web.Handler)
        handler.server = types.SimpleNamespace(manager=self.manager)
        handler.path = parsed.path + ('?' + parsed.query if parsed.query else '')
        # The authenticated control layer pins this actor in production. Tests
        # deliberately do not use an arbitrary POST actor as authentication.
        handler.headers = {'Content-Length': str(len(body)), 'Content-Type': 'application/json',
                           'X-Seriald-Actor': self.actor}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header, handler.end_headers = mock.Mock(), mock.Mock()
        if request.get_method() == 'POST':
            handler.do_POST()
        else:
            handler.do_GET()
        status = handler.send_response.call_args.args[0]
        data = handler.wfile.getvalue()
        if self.lose_submission and parsed.path == '/api/task-start':
            self.lose_submission = False
            raise TimeoutError('accepted response lost')
        if status >= 400:
            raise ctl.HTTPError(request.full_url, status, 'fixture', {}, io.BytesIO(data))
        return io.BytesIO(data)


class IntegrationClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(str(Path(temporary.name) / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.store.bind({'serial': 'BOARD-ONE', 'serial_source': 'eeprom-onie',
                         'identity_status': 'confirmed'}, 'fake-usb', 'fake-tty', 'board')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('board')
        self.device.fd, self.device.present = 98765, True
        self.client = ctl.Client('http://contract.invalid', token='fixture-token')
        self.client.actor = 'platform-contract'
        self.mapping = {'platform_device_id': 'test-room/slot-7', 'serial': 'BOARD-ONE',
                        'connection_id': self.device.connection_id}
        self.transport = MemoryHTTP(self.manager)
        patcher = mock.patch.object(ctl, 'urlopen', side_effect=self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self, task_id):
        with self.manager.tasks._lock:
            active = self.manager.tasks._active.get(task_id)
        if active:
            active[1].join(2)
            self.assertFalse(active[1].is_alive())
        return self.manager.tasks.store.get(task_id)

    def test_negotiation_uses_implemented_capability_contract_without_board_input(self):
        capability = self.client.negotiate('identify')
        self.assertEqual(capability['contract']['name'], 'decklab-execution')
        self.assertFalse(capability['scope']['cross_platform_exclusive'])
        self.assertEqual([entry[:2] for entry in self.transport.calls], [('GET', '/api/capabilities')])
        # A legacy/partial server is rejected before it can receive a POST.
        with mock.patch.object(self.client, 'request', return_value={'api_version': 1}), \
                self.assertRaises(ctl.ClientError) as failed:
            self.client.submit_task('identify', 'stable-attempt-001')
        self.assertEqual(failed.exception.code, 'unsupported-contract')

    def test_platform_mapping_rejects_reassigned_connection_without_falling_back_to_label(self):
        selected = self.client.select_mapping(self.mapping)
        self.assertEqual((selected['serial'], self.client.platform_device_id), ('BOARD-ONE', 'test-room/slot-7'))
        self.device.connection_id = 'new-physical-generation'
        with self.assertRaises(ctl.ClientError) as failed:
            self.client.select_mapping(self.mapping)
        self.assertEqual(failed.exception.code, 'stale-mapping')
        self.assertFalse(any(method == 'POST' for method, _, _, _ in self.transport.calls))

    def test_lost_submission_queries_original_request_and_never_reexecutes(self):
        self.client.select_mapping(self.mapping)
        self.transport.lose_submission = True
        with mock.patch('seriald_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'BOARD-ONE'})) as execute:
            result = self.client.submit_task('identify', 'stable-attempt-001')
            task = self.finish(result['task']['id'])
            self.assertEqual(task['request_id'], 'stable-attempt-001')
            self.assertEqual(execute.call_count, 1)
            receipt = ctl.TaskReceipt.from_response({'task': task}, 'stable-attempt-001')
            self.assertEqual(receipt.outcome, ctl.TaskOutcome.OBSERVED)
            self.assertEqual(sum(path == '/api/task-start' for _, path, _, _ in self.transport.calls), 1)
            lookups = [query for method, path, query, _ in self.transport.calls if path == '/api/task']
            self.assertTrue(any('request_id=stable-attempt-001' in query for query in lookups))

    def test_owner_lookup_preserves_original_post_actor_and_grant_lookup_ignores_override(self):
        self.client.select_mapping(self.mapping)
        self.transport.actor, self.transport.lose_submission = 'web', True
        with mock.patch('seriald_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'BOARD-ONE'})) as execute:
            result = self.client.submit_task('identify', 'owner-attempt-001')
            self.finish(result['task']['id'])
            self.assertEqual(result['task']['actor'], 'platform-contract')
            self.assertEqual(execute.call_count, 1)
        self.transport.actor = 'platform-contract'
        self.client.actor = 'cannot-override-the-grant'
        looked_up = self.client.task_by_request('owner-attempt-001')
        self.assertEqual(looked_up['task']['id'], result['task']['id'])

    def test_unavailable_reconciliation_reports_unknown_with_original_id_and_no_replay(self):
        self.client.select_mapping(self.mapping)
        self.transport.lose_submission, self.transport.lose_lookup = True, True
        with mock.patch('seriald_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'BOARD-ONE'})):
            with self.assertRaises(ctl.SubmissionUnconfirmed) as failed:
                self.client.submit_task('identify', 'stable-attempt-002')
        self.assertEqual((failed.exception.request_id, failed.exception.outcome), ('stable-attempt-002', 'unknown'))
        self.assertEqual(sum(path == '/api/task-start' for _, path, _, _ in self.transport.calls), 1)

    def test_same_request_deduplicates_and_changed_input_is_explicit_conflict(self):
        self.client.select_mapping(self.mapping)
        with mock.patch('seriald_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'BOARD-ONE'})) as execute:
            first = self.client.submit_task('identify', 'stable-attempt-003')
            self.finish(first['task']['id'])
            repeated = self.client.submit_task('identify', 'stable-attempt-003')
            self.assertEqual(first['task']['id'], repeated['task']['id'])
            with self.assertRaises(ctl.ClientError) as failed:
                self.client.submit_task('identify', 'stable-attempt-003', init_boot_marker=True)
            self.assertEqual(failed.exception.status, 409)
            self.assertEqual(execute.call_count, 1)

    def test_physical_outcome_does_not_follow_lifecycle_or_exit_code(self):
        base = {'id': 'task', 'request_id': 'stable-attempt-004', 'state': 'completed',
                'cancel_requested': True, 'result': {'exit_code': 0}}
        for outcome in ('acknowledged', 'unknown', 'a-future-outcome'):
            receipt = ctl.TaskReceipt.from_response({'task': dict(base, outcome=outcome)})
            self.assertEqual(receipt.outcome.value, 'acknowledged' if outcome == 'acknowledged' else 'unknown')
            self.assertTrue(receipt.cancel_requested)
            self.assertNotEqual(receipt.outcome, ctl.TaskOutcome.VERIFIED)

    def test_board_lease_secret_stays_in_memory_and_request_proof_reaches_handler(self):
        self.client.select_mapping(self.mapping)
        claimed = self.client.claim_board(60)
        self.assertNotIn('execution_lease', claimed)
        secret = self.client.execution_lease['token']
        self.assertNotIn(secret, json.dumps(claimed))
        owner = self.client.input_owner()
        self.assertEqual(owner['input_owner']['generation'], claimed['input_owner']['generation'])
        self.assertFalse(owner['cross_platform_exclusive'])
        self.assertNotIn(secret, json.dumps(owner))
        self.client.renew_board(60)
        with mock.patch('seriald_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'BOARD-ONE'})):
            result = self.client.submit_task('identify', 'leased-attempt-001')
            self.finish(result['task']['id'])
        posted = next(payload for method, path, _, payload in self.transport.calls if path == '/api/task-start')
        self.assertEqual(posted['execution_lease']['token'], secret)
        self.assertIsNone(self.client.release_board()['input_owner'])
        self.assertIsNone(self.client.execution_lease)

    def test_claim_response_loss_is_unknown_and_never_claims_twice(self):
        self.client.select_mapping(self.mapping)
        with mock.patch.object(self.client, 'negotiate', return_value={'execution_lease': {
                'supported': True, 'resource': 'board', 'scope': 'collector'}}), \
                mock.patch.object(self.client, 'post', side_effect=ctl.ClientError('response lost')) as post:
            with self.assertRaises(ctl.ClientError) as failed:
                self.client.claim_board()
        self.assertEqual((failed.exception.code, failed.exception.outcome), ('lease-unconfirmed', 'unknown'))
        post.assert_called_once_with('/api/input-claim', scope='board', ttl=300)
        self.assertIsNone(self.client.execution_lease)

    def test_release_response_loss_keeps_proof_without_claim_or_release_replay(self):
        proof = {'id': '1' * 32, 'generation': 1, 'token': 'private-proof-token' * 2}
        self.client.execution_lease = copy.deepcopy(proof)
        with mock.patch.object(self.client, 'post', side_effect=ctl.ClientError('response lost')) as post:
            with self.assertRaises(ctl.ClientError) as failed:
                self.client.release_board()
        self.assertEqual(failed.exception.outcome, 'unknown')
        self.assertEqual(self.client.execution_lease, proof)
        post.assert_called_once_with('/api/input-release', scope='board', execution_lease=proof)

    def test_unconfirmed_release_receipt_keeps_the_original_proof(self):
        proof = {'id': '1' * 32, 'generation': 1, 'token': 'private-proof-token' * 2}
        self.client.execution_lease = copy.deepcopy(proof)
        with mock.patch.object(self.client, 'post', return_value={'ok': True}):
            with self.assertRaises(ctl.ClientError) as failed:
                self.client.release_board()
        self.assertEqual(failed.exception.outcome, 'unknown')
        self.assertEqual(self.client.execution_lease, proof)

    def test_recovery_confirmation_requires_a_boolean_not_a_truthy_string_or_number(self):
        self.client.execution_lease = {'id': '1' * 32, 'generation': 1, 'token': 'private-proof-token' * 2}
        with mock.patch.object(self.client, 'post') as post:
            for confirmation in ('false', 'true', 1, 0, None):
                with self.subTest(confirmation=confirmation), self.assertRaises(ctl.ClientError):
                    self.client.release_board(process_stopped_confirmed=confirmation, reason='checked')
        post.assert_not_called()

    def test_revocation_receipt_preserves_pending_stop_and_omits_execution_proof(self):
        self.client.execution_lease = {'id': 'lease', 'generation': 1, 'token': 'do-not-forward'}
        pending = {'ok': True, 'id': 'grant', 'revocation_generation': 3,
                   'sessions_closed': False, 'revocation_pending': True}
        with mock.patch.object(ctl, 'urlopen', return_value=io.BytesIO(json.dumps(pending).encode())) as request:
            result = self.client.revoke_grant('grant')
        self.assertEqual(result, pending)
        self.assertNotIn('execution_lease', json.loads(request.call_args.args[0].data))
        self.assertFalse(result['sessions_closed'])  # ok is denial, not physical stop.


class ExecutorExampleTests(unittest.TestCase):
    def test_unknown_completed_task_does_not_release_the_execution_lease(self):
        client = mock.Mock()
        client.submit_task.return_value = {'task': {'id': 'original', 'state': 'completed',
            'request_id': 'stable-attempt-001', 'outcome': 'unknown'}}
        result = run_attempt(client, {'platform_device_id': 'slot'}, 'hardware-reset', 'stable-attempt-001')
        self.assertFalse(result['lease_released'])
        client.release_board.assert_not_called()
        client.submit_task.assert_called_once_with('hardware-reset', 'stable-attempt-001')

    def test_wait_expiry_does_not_cancel_release_or_resubmit(self):
        client = mock.Mock()
        client.claim_board.return_value = {'input_owner': {'id': 'owner'}}
        client.submit_task.return_value = {'task': {'id': 'original', 'state': 'running',
            'request_id': 'stable-attempt-001', 'outcome': 'issued'}}
        with mock.patch('examples.platform_executor.time.monotonic', side_effect=[0, 0, 2]):
            result = run_attempt(client, {'platform_device_id': 'slot'}, 'hardware-reset',
                                 'stable-attempt-001', wait_seconds=1)
        self.assertTrue(result['wait_expired'])
        client.release_board.assert_not_called()
        client.task_by_request.assert_not_called()
        client.submit_task.assert_called_once()


if __name__ == '__main__':
    unittest.main()
