"""Public discrete task contracts use fake operations and private temporary state."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from decklab_core.devices import Manager
from decklab_core.storage import Store
from decklab_core.task_actions import TaskActions
from decklab_core.results import response


class DiscreteTaskContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.store.bind({'serial': 'SN1', 'serial_source': 'device-tree'}, 'usb', 'fake', 'board')
        self.store.bind({'serial': 'SN2'}, 'usb2', 'fake2', 'other')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('board')
        self.device.fd, self.device.present = 98765, True
        self.actions = TaskActions(self.manager)

    def payload(self, kind, request_id, **extra):
        return dict(kind=kind, request_id=request_id, key='board', serial='SN1',
                    connection_id=self.device.connection_id, client_id='page', **extra)

    def finish(self, result):
        self.assertEqual(result.status, 202, result.payload)
        task_id = result.payload['task']['id']
        with self.manager.tasks._lock:
            active = self.manager.tasks._active.get(task_id)
        if active:
            active[1].join(2)
            self.assertFalse(active[1].is_alive())
        return self.manager.tasks.store.get(task_id)

    def test_identify_and_ssh_request_replay_never_reexecutes_and_credentials_remain_private(self):
        for kind in ('identify', 'ssh-connect', 'ssh-enable'):
            request_id = 'contract-' + kind
            payload = self.payload(kind, request_id)
            if kind == 'ssh-connect':
                payload.update(ssh_user='user', ssh_password='private-credential')
            with mock.patch('decklab_core.task_actions.DeviceActions.execute',
                            return_value=response(200, {'ok': True, 'serial': 'SN1'})) as execute:
                first = self.finish(self.actions.start(payload, self.device, 'web'))
                # A page reload changes its client ID; querying/replaying the
                # original operation does not acquire ownership or send input.
                replay = self.actions.start(dict(payload, client_id='new-page'), self.device, 'web')
                self.assertEqual(replay.payload['task']['id'], first['id'])
                self.assertEqual(execute.call_count, 1)
                changed = dict(payload, ssh_password='different') if kind == 'ssh-connect' else dict(payload, title='changed')
                self.assertEqual(self.actions.start(changed, self.device, 'web').status, 409)
                self.assertEqual(first['request_id'], request_id)
                self.assertEqual(first['outcome'], 'acknowledged' if kind == 'ssh-enable' else 'observed')
                self.assertEqual(self.actions.query(actor='web', request_id=request_id).payload['task']['id'], first['id'])
                self.assertEqual(self.actions.query(actor='other', request_id=request_id).status, 404)
        self.assertNotIn('private-credential', '\n'.join(self.store.db.iterdump()))
        self.assertNotIn('different', '\n'.join(self.store.db.iterdump()))

    def test_file_requests_share_idempotency_and_only_verified_adapter_completion_advances_outcome(self):
        for kind in ('file-upload', 'file-download', 'file-copy'):
            payload = self.payload(kind, 'contract-' + kind, path='/tmp/file', artifact_id='artifact')
            if kind == 'file-copy':
                destination = self.manager.resolve('other')
                destination.fd, destination.present = 98764, True
                payload.update(to_key='other', to_serial='SN2', to_connection_id=destination.connection_id,
                               to_path='/tmp/destination')
            with mock.patch('decklab_core.task_actions.TransferFile') as file_operation, \
                    mock.patch('decklab_core.task_actions.TransferCopy') as copy_operation:
                operation = copy_operation.return_value if kind == 'file-copy' else file_operation.return_value
                operation.run.return_value = {'ok': True, 'size': 1, 'sha256': 'verified-by-transfer'}
                if kind == 'file-copy':
                    operation.snapshots = {destination: (destination.connection_id, destination.stop)}
                    operation.serials = {}
                first = self.finish(self.actions.start(payload, self.device, 'web'))
                repeated = self.actions.start(payload, self.device, 'web')
                self.assertEqual(repeated.payload['task']['id'], first['id'])
                operation.run.assert_called_once()
                self.assertEqual(first['outcome'], 'verified')
                self.assertEqual(self.actions.start(dict(payload, path='/tmp/changed'), self.device, 'web').status, 409)

    def test_replay_after_connection_change_returns_original_without_reentering_adapter(self):
        payload = self.payload('identify', 'stale-original-request')
        with mock.patch('decklab_core.task_actions.DeviceActions.execute',
                        return_value=response(200, {'ok': True, 'serial': 'SN1'})) as execute:
            first = self.finish(self.actions.start(payload, self.device, 'web'))
            self.device.connection_id = 'replacement'
            repeated = self.actions.start(payload, self.device, 'web')
            self.assertEqual(repeated.payload['task']['id'], first['id'])
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(self.actions.start(dict(payload, request_id='new-stale-request'), self.device, 'web').status, 409)


if __name__ == '__main__':
    unittest.main()
