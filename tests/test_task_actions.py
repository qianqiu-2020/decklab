"""Task adapters and HTTP use fake boards and real in-memory task persistence."""
import io
import json
import threading
import unittest
from unittest import mock

from seriald_core.devices import Manager
from seriald_core.storage import Store
from seriald_core.task_actions import TaskActions
from seriald_core.transactions import TransactionError
from seriald_core import web


class TaskActionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.bind({'serial': 'SN1', 'serial_source': 'device-tree'}, 'usb', 'fake', 'board')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('board')
        self.device.fd, self.device.present = 98765, True
        self.actions = TaskActions(self.manager)

    def start(self, **kwargs):
        payload = dict(kind='identify', connection_id=self.device.connection_id,
                       client_id='page', key='board', serial='SN1', **kwargs)
        return self.actions.start(payload, self.device, 'web')

    def test_non_object_submission_does_not_create_a_task(self):
        for payload in (None, [], 'identify', 123):
            with self.subTest(payload=payload):
                result = self.actions.start(payload, self.device, 'web')
                self.assertEqual(result.status, 400)
                self.assertEqual(result.payload['code'], 'invalid-argument')
        self.assertEqual(self.manager.tasks.store.list(), [])

    def wait_task(self, task_id):
        with self.manager.tasks._lock:
            entry = self.manager.tasks._active.get(task_id)
        if entry:
            entry[1].join(2)
            self.assertFalse(entry[1].is_alive())

    def http(self, route, body=None, actor='web'):
        handler = web.Handler.__new__(web.Handler)
        raw = json.dumps(body).encode() if body is not None else b''
        handler.path, handler.headers = route, {'Content-Length': str(len(raw)), 'X-Seriald-Actor': actor}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR', self.manager):
            handler.do_GET() if body is None else handler.do_POST()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_http_start_and_query_result_keep_identity_and_exclude_probe_credentials(self):
        payload = {'kind': 'identify', 'key': 'board', 'serial': 'SN1',
                   'connection_id': self.device.connection_id, 'actor': 'web', 'client_id': 'page'}
        with mock.patch.object(self.device, 'run_probe', return_value={
                'ok': True, 'serial': 'SN1', 'hostname': 'fake', 'password': 'secret', 'EEPROM_HEX': 'raw'}):
            code, result = self.http('/api/task-start', payload)
            self.assertEqual(code, 202)
            self.wait_task(result['task']['id'])
            self.assertTrue(self.manager.tasks.close())
        task_id = result['task']['id']
        code, result = self.http('/api/task?id=' + task_id)
        self.assertEqual(code, 200)
        task = result['task']
        self.assertEqual(task['state'], 'completed')
        self.assertEqual(task['progress']['done'], 1)
        self.assertEqual(task['targets'][0]['serial'], 'SN1')
        self.assertNotIn('secret', json.dumps(task))
        self.assertNotIn('EEPROM_HEX', task['result'])

    def test_task_cancel_wakes_real_transaction_wait_and_preserves_client_scope(self):
        ready = threading.Event()
        scopes = []
        def probe():
            with self.device.transactions.begin('probe', 10) as lease:
                scopes.append((lease.actor, lease.client_id))
                ready.set()
                with self.device.cv:
                    self.device.cv.wait_for(lease.is_set, timeout=2)
                lease.check()
        with mock.patch.object(self.device, 'run_probe', side_effect=probe):
            result = self.start()
            self.assertTrue(ready.wait(2))
            task_id = result.payload['task']['id']
            code, cancelled = self.http('/api/task-cancel', {'id': task_id, 'actor': 'web'})
            self.assertEqual(code, 200)
            self.assertTrue(cancelled['task']['cancel_requested'])
            self.assertTrue(self.manager.tasks.close())
        self.assertEqual(scopes, [('web', 'page')])
        self.assertEqual(self.manager.tasks.store.get(task_id)['state'], 'cancelled')
        self.assertIsNone(self.device.transactions.snapshot())

    def test_generation_change_between_leases_cannot_write_replacement_connection(self):
        def probe():
            with self.device.transactions.begin('first', 1):
                pass
            self.device.connection_id = 'replacement'
            with self.device.transactions.begin('second', 1):
                self.fail('entered a lease on the replacement board')
        with mock.patch.object(self.device, 'run_probe', side_effect=probe):
            result = self.start()
            self.wait_task(result.payload['task']['id'])
            self.assertTrue(self.manager.tasks.close())
        task = self.manager.tasks.store.get(result.payload['task']['id'])
        self.assertEqual(task['state'], 'failed')
        self.assertIn('连接已变化', task['error'])

    def test_task_lookup_and_cancel_enforce_actor_and_all_target_scope(self):
        task_id = self.manager.tasks.store.create('identify', 'agent', '', [
            {'serial': 'SN1', 'connection_id': 'one'}, {'serial': 'SN2', 'connection_id': 'two'}])
        self.assertEqual(self.actions.query(task_id, 'other').status, 404)
        self.assertEqual(self.actions.query(task_id, 'agent', 'SN1').status, 404)
        self.assertEqual(self.actions.cancel(task_id, 'agent', 'SN1').status, 404)
        self.assertFalse(self.manager.tasks.store.get(task_id)['cancel_requested'])
        self.assertEqual(self.actions.query(actor='agent', serial='SN1').payload['tasks'], [])
        self.assertEqual(self.actions.query(task_id, 'web').status, 200)

    def test_invalid_task_kind_connection_and_owned_input_are_rejected_before_recording(self):
        for payload in ({'kind': []}, {'kind': 'send'}, {'kind': 'identify', 'connection_id': 'old'}):
            result = self.actions.start(payload, self.device, 'web')
            self.assertIn(result.status, (400, 409))
        with self.device.transactions.actor_scope('agent'):
            self.device.transactions.claim('agent')
        result = self.start()
        self.assertEqual(result.status, 423)
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_task_context_cancel_does_not_poison_later_non_task_transaction(self):
        event = threading.Event(); event.set()
        with self.device.transactions.task_scope(event, self.device.connection_id, self.device.stop):
            with self.assertRaises(TransactionError):
                with self.device.transactions.begin('cancelled', 1):
                    pass
        with self.device.transactions.begin('ordinary', 1) as lease:
            lease.check()

    def test_revocation_cancels_active_tasks_without_depending_on_history_page(self):
        ready = threading.Event()
        def probe():
            with self.device.transactions.begin('probe', 10) as lease:
                ready.set()
                with self.device.cv:
                    self.device.cv.wait_for(lease.is_set, timeout=2)
                lease.check()
        actor = 'shared-agent/test'
        with mock.patch.object(self.device, 'run_probe', side_effect=probe):
            result = self.actions.start({'kind': 'identify', 'connection_id': self.device.connection_id},
                                        self.device, actor)
            self.assertTrue(ready.wait(2))
            with mock.patch.object(self.manager.tasks.store, 'list', side_effect=AssertionError('history page')):
                code, _ = self.http('/api/agent-revoke', {'actor': actor})
            self.assertEqual(code, 200)
            self.wait_task(result.payload['task']['id'])
        self.assertEqual(self.manager.tasks.store.get(result.payload['task']['id'])['state'], 'cancelled')

    def test_cancelled_ssh_task_closes_new_session_but_keeps_reused_session(self):
        for reused in (False, True):
            ready = threading.Event()
            session = mock.Mock(sid='task-ssh', ip='192.0.2.1', persistent=True, auto_configured=False)
            if reused:
                self.device.ssh_sessions[session.sid] = session
            def connect(**kwargs):
                self.device.ssh_sessions[session.sid] = session
                # Network connect can finish after cancellation without a serial lease.
                cancellation = self.device.transactions._local.task[0]
                ready.set()
                with self.device.cv:
                    self.device.cv.wait_for(cancellation.is_set, timeout=2)
                return session, ''
            with mock.patch.object(self.device, 'ssh_connect', side_effect=connect):
                result = self.actions.start({'kind': 'ssh-connect', 'connection_id': self.device.connection_id},
                                            self.device, 'web')
                self.assertTrue(ready.wait(2))
                task_id = result.payload['task']['id']
                self.actions.cancel(task_id)
                self.wait_task(task_id)
            self.assertEqual(self.manager.tasks.store.get(task_id)['state'], 'completed' if reused else 'cancelled')
            self.assertEqual(session.close.call_count, 0 if reused else 1)

    def test_http_missing_task_id_and_bad_pagination_are_rejected(self):
        self.assertEqual(self.http('/api/task')[0], 400)
        self.assertEqual(self.http('/api/tasks?limit=0')[0], 400)
        self.assertEqual(self.http('/api/tasks?offset=bad')[0], 400)
