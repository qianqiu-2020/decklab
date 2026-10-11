"""Optional actor reservations coordinate fake serial writers and real waiters."""
import threading
import types
import io
import json
import unittest
import tempfile
from unittest import mock

from seriald_core.device import Device
from seriald_core.actions import DeviceActions
from seriald_core.transactions import TransactionError
from seriald_core import web


class InputOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch('seriald_core.device.LOG_ROOT', temporary.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = Device('fake-usb', '/fake/tty')
        self.addCleanup(self.device.close_log)
        self.device.fd, self.device.present = 98765, True
        self.transactions = self.device.transactions
        self.manager = types.SimpleNamespace(store=mock.Mock())
        self.actions = DeviceActions(self.manager)

    def test_default_shared_input_and_reserved_owner_input(self):
        with mock.patch.object(self.device, '_write', return_value=(True, '')) as write:
            for actor in ('one', 'two'):
                result = self.actions.execute('session-send', {'data': 'x'}, self.device, actor)
                self.assertEqual(result.status, 200)
            owner = self.transactions.claim('one')
            denied = self.actions.execute('session-send', {'data': 'x'}, self.device, 'two')
            self.assertEqual((denied.status, denied.payload['code']), (423, 'input-owned'))
            self.assertEqual(self.actions.execute('session-send', {'data': 'x'}, self.device, 'one').status, 200)
            self.assertEqual(write.call_count, 3)
            self.transactions.release(owner['id'], 'one')
            self.assertEqual(self.actions.execute('session-send', {'data': 'x'}, self.device, 'two').status, 200)

    def test_probe_transfer_and_direct_command_cannot_bypass_reservation(self):
        self.transactions.claim('owner')
        with mock.patch.object(self.device, '_run_cmd_locked') as command:
            for action in ('probe', 'identify', 'ssh-enable', 'rescue', 'file-transfer/start'):
                self.assertEqual(self.actions.execute(action, {}, self.device, 'other').status, 423)
            with self.transactions.actor_scope('other'):
                self.assertFalse(self.device._run_cmd('uname', 1)[0])
        command.assert_not_called()
        self.assertIsNone(self.transactions.snapshot())

    def test_human_takeover_cancels_waiter_but_keeps_collector_open(self):
        self.transactions.claim('agent')
        entered = threading.Event()
        errors = []

        def wait():
            try:
                with self.transactions.begin('identify', 10, actor='agent'):
                    entered.set()
                    self.device._wait(0, 'never', 10)
            except TransactionError as error:
                errors.append(error.code)

        worker = threading.Thread(target=wait)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            owner = self.transactions.claim('web', takeover=True)
            worker.join(timeout=0.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['cancelled'])
            self.assertEqual(owner['actor'], 'web')
            self.assertEqual(self.device.fd, 98765)
            self.assertFalse(self.device.stop.is_set())
            with self.transactions.begin('input', 1, actor='web'):
                pass
        finally:
            self.device.stop.set()
            with self.device.cv:
                self.device.cv.notify_all()
            worker.join(timeout=2)

    def test_only_human_can_takeover_and_stale_release_cannot_clear_new_owner(self):
        first = self.transactions.claim('one')
        with self.assertRaises(TransactionError) as denied:
            self.transactions.claim('two', takeover=True)
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(TransactionError):
            self.transactions.release(first['id'], 'two')
        second = self.transactions.claim('web', takeover=True)
        with self.assertRaises(TransactionError) as stale:
            self.transactions.release(first['id'], 'one')
        self.assertEqual(stale.exception.status, 404)
        self.assertEqual(self.transactions.ownership()['id'], second['id'])

    def test_connection_generation_invalidates_old_reservation(self):
        old = self.transactions.claim('one')
        self.device.connection_id = 'replacement'
        self.assertIsNone(self.transactions.ownership())
        with self.assertRaises(TransactionError):
            self.transactions.release(old['id'], 'one')
        with self.transactions.begin('input', 1, actor='two'):
            pass

    def test_repeat_claim_returns_same_id_during_owners_operation(self):
        owner = self.transactions.claim('agent')
        with self.transactions.begin('identify', 1, actor='agent'):
            self.assertEqual(self.transactions.claim('agent')['id'], owner['id'])

    def test_http_claim_and_agent_revocation_release_reservation(self):
        self.manager.resolve = lambda key: self.device
        self.manager.all_devices = lambda: [self.device]

        def post(action, payload):
            handler = web.Handler.__new__(web.Handler)
            body = json.dumps(payload).encode()
            handler.path, handler.headers = '/api/' + action, {'Content-Length': str(len(body))}
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
            with mock.patch.object(web, 'MGR', self.manager):
                handler.do_POST()
            return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

        status, result = post('input-claim', {'key': self.device.key, 'actor': 'shared-agent/test'})
        self.assertEqual(status, 200)
        self.assertEqual(result['input_owner']['actor'], 'shared-agent/test')
        status, _ = post('agent-revoke', {'actor': 'shared-agent/test'})
        self.assertEqual(status, 200)
        self.assertIsNone(self.transactions.ownership())
        self.transactions.claim('shared-agent/test')
        release = self.transactions.release

        def intervening_takeover(ownership_id, actor):
            self.transactions.claim('web', takeover=True)
            release(ownership_id, actor)

        with mock.patch.object(self.transactions, 'release', side_effect=intervening_takeover):
            status, _ = post('agent-revoke', {'actor': 'shared-agent/test'})
        self.assertEqual(status, 200)
        self.assertEqual(self.transactions.ownership()['actor'], 'web')

    def test_ssh_remains_writable_and_serial_output_readable_for_spectator(self):
        self.transactions.claim('serial-owner')
        session = mock.Mock(actor='ssh-owner', takeovers=set())
        self.device.ssh_sessions['ssh1'] = session
        session.write.return_value = True
        self.device.append(b'boot output\n')
        self.assertEqual(self.device.read(0)[1], b'boot output\n')
        with mock.patch.object(self.device, 'record_input'):
            result = self.actions.execute('session-send', {'sid': 'ssh1', 'data': 'uname\r'},
                                          self.device, 'ssh-owner')
        self.assertEqual(result.status, 200)
        session.write.assert_called_once()

    def test_two_claimants_cannot_both_become_owner(self):
        barrier = threading.Barrier(2)
        results = []

        def claim(actor):
            barrier.wait(timeout=2)
            try:
                results.append(self.transactions.claim(actor)['actor'])
            except TransactionError:
                results.append(None)

        threads = [threading.Thread(target=claim, args=(actor,)) for actor in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_two_human_clients_cannot_share_or_release_each_others_reservation(self):
        first = self.actions.execute('input-claim', {'client_id': 'browser-one'}, self.device, 'web')
        owner = first.payload['input_owner']
        repeat = self.actions.execute('input-claim', {'client_id': 'browser-one'}, self.device, 'web')
        self.assertEqual(repeat.payload['input_owner']['id'], owner['id'])
        with mock.patch.object(self.device, '_write', return_value=(True, '')) as write:
            denied = self.actions.execute('session-send', {'client_id': 'browser-two', 'data': 'x'}, self.device, 'web')
            self.assertEqual(denied.status, 423)
            self.assertEqual(self.actions.execute('session-send', {'client_id': 'browser-one', 'data': 'x'},
                                                  self.device, 'web').status, 200)
        write.assert_called_once()
        denied = self.actions.execute('input-release', {'client_id': 'browser-two', 'id': owner['id']}, self.device, 'web')
        self.assertEqual(denied.status, 403)
        self.assertEqual(self.transactions.ownership()['id'], owner['id'])

    def test_human_takeover_cancels_same_actor_different_client(self):
        with self.transactions.actor_scope('web', 'browser-one'):
            first = self.transactions.claim('web')
            with self.assertRaises(TransactionError) as cancelled:
                with self.transactions.begin('input', 1) as lease:
                    result = self.actions.execute('input-claim', {'client_id': 'browser-two', 'takeover': True},
                                                  self.device, 'web')
                    self.assertEqual(result.status, 200)
                    self.assertNotEqual(result.payload['input_owner']['id'], first['id'])
                    self.assertTrue(lease.cancelled.is_set())
        self.assertEqual(cancelled.exception.code, 'cancelled')

    def test_client_scope_is_restored_and_nonhuman_cannot_use_human_client_identity(self):
        with self.transactions.actor_scope('web', 'browser-one'):
            with self.transactions.actor_scope('web', 'browser-two'):
                self.assertEqual(self.transactions.client_id, 'browser-two')
            self.assertEqual(self.transactions.client_id, 'browser-one')
        self.assertEqual(self.transactions.client_id, '')
        result = self.actions.execute('input-claim', {'client_id': 'browser-one'}, self.device, 'shared-agent/test')
        self.assertEqual(result.payload['input_owner']['client_id'], '')
        self.assertEqual(result.payload['input_owner']['actor'], 'shared-agent/test')
        self.assertEqual(self.actions.execute('input-claim', {'client_id': []}, self.device, 'web').status, 400)

    def test_transfer_keeps_original_client_identity_for_background_commands(self):
        transfer = self.device.file_transfer
        self.actions.execute('input-claim', {'client_id': 'browser-one'}, self.device, 'web')
        seen = []

        def command(*args, **kwargs):
            seen.append(self.transactions.current.client_id)
            return True, 'SD_OK=1\nSD_GZIP=0\n'

        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=command), \
                mock.patch.object(transfer, '_schedule_expiry'):
            result = self.actions.execute('file-transfer/start', {
                'direction': 'upload', 'path': '/tmp/file', 'size': 0,
                'connection_id': self.device.connection_id, 'client_id': 'browser-one'}, self.device, 'web')
            self.assertEqual(result.status, 200)
            transfer._run('test-background-command')
            self.assertEqual(seen, ['browser-one', 'browser-one'])
            self.actions.execute('file-transfer/abort', {'id': result.payload['id'],
                'connection_id': self.device.connection_id, 'client_id': 'browser-one'}, self.device, 'web')


if __name__ == '__main__':
    unittest.main()
