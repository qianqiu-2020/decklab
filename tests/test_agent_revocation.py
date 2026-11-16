"""Durable stop receipts with fake links/streams; never opens hardware."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from seriald_core.agent_grants import (AgentGrantStore, ActorRequests, GrantRevoked,
                                      actor_request, revoke_backend_actor)
from seriald_core.control_server import RevocationReconciler, deliver_revocation
from seriald_core.session_base import SessionBase
from seriald_core import schema
from seriald_core import device as device_module
from seriald_core.devices import Manager
from seriald_core.storage import Store


class ReplyConnection:
    def __init__(self, reply, status=200):
        self.reply, self.status = reply, status
        self.request_body = None

    def request(self, method, path, body=None, headers=None):
        self.request_body = json.loads(body)

    def getresponse(self):
        reply = self.reply(self.request_body) if callable(self.reply) else self.reply
        return SimpleNamespace(status=self.status, read=lambda _: json.dumps(reply).encode())

    def close(self):
        pass


class RevocationTests(unittest.TestCase):
    def store(self, path=':memory:'):
        store = AgentGrantStore(str(path))
        self.addCleanup(store.close)
        return store

    def test_revocation_and_pending_receipt_survive_restart_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'grants.sqlite3'
            with closing(AgentGrantStore(str(path))) as store:
                token, issued = store.issue('all')
                revoked = store.revoke_request(issued['id'])
                self.assertEqual(revoked['revocation_generation'], 1)
                self.assertEqual(store.revoke_request(issued['id']), revoked)
                self.assertIsNone(store.lookup(token))
            with closing(AgentGrantStore(str(path))) as reopened:
                self.assertEqual(reopened.pending_revocations(), [revoked])
                self.assertFalse(reopened.confirm_revocation(issued['id'], 2))
                self.assertTrue(reopened.confirm_revocation(issued['id'], 1))
                self.assertEqual(reopened.pending_revocations(), [])

    def test_backend_fence_survives_its_own_restart_and_never_revives_actor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backend-grants.sqlite3'
            actor = 'shared-agent/' + 'a' * 16
            with closing(AgentGrantStore(str(path))) as store:
                store.check_actor(actor, 0)
                store.block_actor(actor, 2)
                self.assertEqual(store.block_actor(actor, 1), 2)
            with closing(AgentGrantStore(str(path))) as reopened:
                for generation in (None, 0, 1, 2):
                    with self.subTest(generation=generation), self.assertRaises(GrantRevoked):
                        reopened.check_actor(actor, generation)
                reopened.check_actor('web')

    def test_version_two_revoked_grants_are_migrated_into_pending_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy-grants.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                schema._grants_initial(db)
                schema._grants_connections(db)
                db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('a' * 16, 'opaque-hash', 'shared-agent/' + 'a' * 16,
                     'all', '', 1, None, 2, '', ''))
                db.execute('PRAGMA user_version=2')
            with closing(AgentGrantStore(str(path))) as upgraded:
                pending = upgraded.pending_revocations()
                self.assertEqual(len(pending), 1)
                self.assertEqual((pending[0]['revocation_generation'], pending[0]['backend_confirmed_generation']), (1, 0))
                self.assertIsNotNone(upgraded.schema_backup)

    def test_current_unversioned_grants_keep_confirmation_and_backend_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'current-grants.sqlite3'
            with closing(AgentGrantStore(str(path))) as original:
                _, grant = original.issue('all')
                original.revoke_request(grant['id'])
                original.confirm_revocation(grant['id'], 1)
                original.block_actor(grant['actor'], 1)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('PRAGMA user_version=0')
            with closing(AgentGrantStore(str(path))) as adopted:
                self.assertEqual(adopted.pending_revocations(), [])
                self.assertEqual(adopted.list()[0]['backend_confirmed_generation'], 1)
                with self.assertRaises(GrantRevoked):
                    adopted.check_actor(grant['actor'])

    def test_only_exact_positive_stop_receipt_clears_pending(self):
        store = self.store()
        _, grant = store.issue('all')
        revoked = store.revoke_request(grant['id'])
        valid = {'actor': grant['actor'], 'revocation_generation': 1, 'sessions_closed': True}
        for changed in ({'actor': 'shared-agent/' + 'b' * 16}, {'revocation_generation': 2},
                        {'revocation_generation': True}, {'sessions_closed': False},
                        {'sessions_closed': 1}):
            with self.subTest(changed=changed):
                connection = ReplyConnection(dict(valid, **changed))
                self.assertFalse(deliver_revocation(store, lambda _: connection, revoked))
                self.assertEqual(len(store.pending_revocations()), 1)
        self.assertTrue(deliver_revocation(store, lambda _: ReplyConnection(valid), revoked))
        self.assertEqual(store.pending_revocations(), [])

    def test_control_restart_reconciles_original_actor_after_link_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control-grants.sqlite3'
            with closing(AgentGrantStore(str(path))) as store:
                _, grant = store.issue('all')
                store.revoke_request(grant['id'])
                offline = RevocationReconciler(store, mock.Mock(side_effect=OSError('offline')))
                self.assertFalse(offline.reconcile())
            with closing(AgentGrantStore(str(path))) as restarted, \
                    closing(AgentGrantStore(str(Path(directory) / 'backend-grants.sqlite3'))) as backend:
                manager = SimpleNamespace(agent_grants=backend, all_devices=lambda: [])
                seen = []
                def receive(payload):
                    seen.append(dict(payload))
                    return revoke_backend_actor(manager, payload['actor'], payload['revocation_generation'])
                online = RevocationReconciler(restarted, lambda _: ReplyConnection(receive))
                self.assertTrue(online.reconcile())
                self.assertEqual(seen, [{'actor': grant['actor'], 'revocation_generation': 1}])
                self.assertEqual(restarted.pending_revocations(), [])
                with self.assertRaises(GrantRevoked):
                    backend.check_actor(grant['actor'], 0)

    def test_backend_stop_waits_for_request_and_worker_cleanup(self):
        backend = self.store()
        actor = 'shared-agent/' + 'a' * 16
        tasks = SimpleNamespace(cancel_actor=mock.Mock(), actor_stopped=mock.Mock(return_value=False))
        manager = SimpleNamespace(agent_grants=backend, tasks=tasks, all_devices=lambda: [])
        manager.agent_requests = ActorRequests(backend)
        with self.assertRaises(GrantRevoked):
            with actor_request(manager, actor):
                first = revoke_backend_actor(manager, actor, 1)
                self.assertFalse(first['sessions_closed'])
                tasks.actor_stopped.return_value = True
                self.assertFalse(revoke_backend_actor(manager, actor, 1)['sessions_closed'])
        self.assertTrue(revoke_backend_actor(manager, actor, 1)['sessions_closed'])
        with self.assertRaises(GrantRevoked):
            with actor_request(manager, actor):
                self.fail('revoked actor re-entered backend request')

    def test_backend_cancels_unowned_transaction_and_retains_unfinished_session_cleanup(self):
        backend = self.store()
        actor = 'shared-agent/' + 'd' * 16
        state = {'active': {'id': 'transaction', 'actor': actor}}
        transactions = SimpleNamespace(snapshot=lambda: state['active'], ownership=lambda: None,
                                       cancel=mock.Mock())
        worker = mock.Mock()
        worker.is_alive.return_value = True
        session = SimpleNamespace(actor=actor, _thread=worker)
        device = SimpleNamespace(sessions_lock=threading.RLock(), ssh_sessions={'session': session}, serial='')
        device.close_session = lambda sid: device.ssh_sessions.pop(sid, None) is not None
        manager = SimpleNamespace(agent_grants=backend, all_devices=lambda: [device])
        with mock.patch('seriald_core.transactions.transactions_for', return_value=transactions):
            self.assertFalse(revoke_backend_actor(manager, actor, 1)['sessions_closed'])
            transactions.cancel.assert_called_once_with('transaction', actor)
            state['active'] = None
            # Removing the registry entry must not lose an unfinished thread.
            self.assertFalse(revoke_backend_actor(manager, actor, 1)['sessions_closed'])
            worker.is_alive.return_value = False
            self.assertTrue(revoke_backend_actor(manager, actor, 1)['sessions_closed'])

    def test_ssh_takeover_revocation_preserves_human_shell_and_blocks_stale_input(self):
        backend = self.store()
        actor = 'shared-agent/' + 'c' * 16
        session = SessionBase.__new__(SessionBase)
        session.actor, session.agent_grants = 'web', backend
        session.write_lock, session.cv = threading.Lock(), threading.Condition()
        session.takeovers = {actor}
        session.fd, session.closed = 42, False
        session.terminal = SimpleNamespace(lock=threading.RLock(), controller={'actor': actor, 'client_id': ''},
                                           control_revision=0, control=lambda *_: True)
        session._send = mock.Mock()
        backend.block_actor(actor, 1)
        session.revoke_actor(actor)
        self.assertEqual(session.takeovers, set())
        self.assertIsNone(session.terminal.controller)
        self.assertFalse(session.write('old command', actor))
        self.assertFalse(session.resize(120, 32, actor))
        self.assertFalse(session.takeover(actor, ''))
        session._send.assert_not_called()
        self.assertTrue(session.write('human command', 'web'))
        self.assertFalse(session.closed)

    def test_ssh_connection_finishing_after_revocation_cannot_register_a_session(self):
        backend = self.store()
        _, grant = backend.issue('all')
        store = Store(':memory:')
        self.addCleanup(store.close)
        record = store.bind({'serial': 'SN-REVOKE', 'serial_source': 'device-tree'}, 'fake', 'FAKE', 'board')
        manager = Manager(store)
        self.addCleanup(manager.close)
        manager.agent_grants = backend
        device = device_module.Device('fake', 'FAKE', manager, record=record)
        device.fd, device.present = 98765, True
        device.probe_info = {'ips': ['eth0 192.0.2.1/24']}
        session = mock.Mock()
        session.terminal.control.return_value = True
        def finish_connect():
            backend.block_actor(grant['actor'], 1)
            return True
        session.ensure.side_effect = finish_connect
        with mock.patch.object(device_module, 'check_password_login', return_value=(True, False, '')), \
                mock.patch.object(device_module, 'SshSession', return_value=session), \
                device.transactions.actor_scope(grant['actor']):
            try:
                connected, _ = device.ssh_connect(grant['actor'])
            except (GrantRevoked, RuntimeError) as error:
                self.assertEqual(getattr(error, 'code', None), 'authorization-revoked')
            else:
                self.assertIsNone(connected)
        self.assertEqual(device.ssh_sessions, {})
        session.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
