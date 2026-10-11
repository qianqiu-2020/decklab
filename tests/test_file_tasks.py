"""Background uploads/downloads exercise actual framing on fake serial boards."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.demo import DemoManager
from seriald_core.storage import Store
from seriald_core.task_actions import TaskActions
from seriald_core.artifact_actions import ArtifactActions, body_limit
from seriald_core.transfer_artifacts import ArtifactPolicy
from seriald_core import web


class FileTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = Store(str(self.path / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        with mock.patch('seriald_core.device.LOG_ROOT', str(self.path / 'logs')):
            self.manager = DemoManager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('demo-k1')
        self.artifacts, self.actions = self.manager.artifacts, TaskActions(self.manager)
        self.data = bytes(range(256)) * 500

    def stage(self, data=None, actor='web', serial=None):
        data = self.data if data is None else data
        row = self.artifacts.create('upload', 'test.bin', len(data), actor, serial or self.device.serial)
        if data:
            self.artifacts.append(row['id'], 0, data, actor)
        return self.artifacts.seal(row['id'], actor)

    def start(self, kind='file-download', actor='web', **payload):
        request = dict(kind=kind, connection_id=self.device.connection_id, path='/root/log.txt')
        request.update(payload)
        return self.actions.start(request, self.device, actor)

    def finish(self, response):
        self.assertEqual(response.status, 202, response.payload)
        task_id = response.payload['task']['id']
        with self.manager.tasks._lock:
            entry = self.manager.tasks._active.get(task_id)
        if entry:
            entry[1].join(3)
            self.assertFalse(entry[1].is_alive(), 'file task did not stop')
        return self.manager.tasks.store.get(task_id)

    def http(self, route, body=None, actor='web'):
        handler = web.Handler.__new__(web.Handler)
        raw = json.dumps(body).encode() if body is not None else b''
        handler.path = route
        handler.headers = {'Content-Length': str(len(raw)), 'X-Seriald-Actor': actor}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR', self.manager):
            handler.do_GET() if body is None else handler.do_POST()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue(), handler

    def test_upload_validates_and_consumes_host_file_after_remote_commit(self):
        row = self.stage()
        task = self.finish(self.start('file-upload', artifact_id=row['id'], path='/tmp/upload'))
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.device.files['/tmp/upload'], self.data)
        self.assertEqual(task['result']['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.artifacts.get(row['id'])['state'], 'consumed')
        self.assertEqual(list(self.artifacts.root.iterdir()), [])
        self.assertFalse(self.device.file_transfer.active)
        self.assertNotIn('data_b64', json.dumps(task))

    def test_corrupted_staging_never_promotes_or_overwrites_target(self):
        row = self.stage(b'correct')
        self.device.files['/tmp/upload'] = b'original'
        (self.artifacts.root / (row['id'] + '.bin')).write_bytes(b'changed')
        task = self.finish(self.start('file-upload', artifact_id=row['id'], path='/tmp/upload', overwrite=True))
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(self.device.files['/tmp/upload'], b'original')
        self.assertEqual(self.artifacts.get(row['id'])['state'], 'failed')
        self.assertFalse(any('.seriald-' in path for path in self.device.files))

    def test_download_result_is_durable_and_scoped_stream_matches_hash(self):
        task = self.finish(self.start(actor='agent-one'))
        self.assertEqual(task['state'], 'completed', task)
        artifact_id = task['result']['artifact_id']
        code, content, handler = self.http('/api/artifact-download?id=' + artifact_id, actor='agent-one')
        self.assertEqual(code, 200)
        self.assertEqual(content, self.device.files['/root/log.txt'])
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertEqual(headers['Content-Length'], str(len(content)))
        self.assertEqual(headers['X-Checksum-SHA256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.artifacts._readers, {})
        self.assertEqual(self.http('/api/artifact-download?id=' + artifact_id, actor='agent-two')[0], 404)
        self.assertEqual(self.http('/api/artifact-download?id=' + artifact_id + '&serial=other', actor='agent-one')[0], 404)
        self.assertEqual(self.http('/api/artifact-download?id=' + artifact_id)[0], 200)
        self.assertEqual(self.artifacts.get(artifact_id)['state'], 'available')

    def test_identity_rebinding_without_tty_change_cannot_target_the_replacement_board(self):
        row = self.stage()
        original = self.artifacts.claim
        def rebind(*args, **kwargs):
            claimed = original(*args, **kwargs)
            # Drop the previous identity as a real replacement does, while
            # deliberately preserving tty/generation to exercise the SN guard.
            self.device.record = None
            self.device.bind_board(self.manager.resolve('demo-k3').board)
            return claimed
        with mock.patch.object(self.artifacts, 'claim', side_effect=rebind), \
             mock.patch.object(self.device, '_run_cmd_locked') as commands:
            task = self.finish(self.start('file-upload', artifact_id=row['id']))
        self.assertEqual(task['state'], 'failed')
        commands.assert_not_called()
        self.assertEqual(self.artifacts.get(row['id'])['state'], 'ready')

    def test_failed_remote_upload_returns_original_stage_for_retry(self):
        row = self.stage()
        self.device.files['/tmp/upload'] = b'original'
        failed = self.finish(self.start('file-upload', artifact_id=row['id'], path='/tmp/upload'))
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.artifacts.get(row['id'])['state'], 'ready')
        done = self.finish(self.start('file-upload', artifact_id=row['id'], path='/tmp/upload', overwrite=True))
        self.assertEqual(done['state'], 'completed', done)

    def test_actor_and_board_scope_rejected_before_starting_upload(self):
        for row in (self.stage(actor='other-agent'), self.stage(actor='agent-one', serial='other-board')):
            response = self.start('file-upload', actor='agent-one', artifact_id=row['id'])
            self.assertEqual(response.status, 404)
        self.assertEqual(self.manager.tasks.store.list(), [])
        self.assertFalse(self.device.file_transfer.active)

    def test_cancel_download_wakes_serial_and_removes_partial_artifact(self):
        ready = threading.Event()
        original = self.device._run_cmd_locked
        def waiting(command, timeout, ensure_shell=True):
            if 'SD_DATA=' in command:
                ready.set()
                lease = self.device.transactions._local.lease
                with self.device.cv:
                    self.device.cv.wait_for(lease.is_set, timeout=2)
                lease.check()
            return original(command, timeout, ensure_shell)
        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=waiting):
            response = self.start()
            self.assertTrue(ready.wait(2))
            self.manager.tasks.cancel(response.payload['task']['id'], 'web')
            task = self.finish(response)
        self.assertEqual(task['state'], 'cancelled', task)
        self.assertFalse(self.device.file_transfer.active)
        self.assertEqual(list(self.artifacts.root.iterdir()), [])
        self.assertEqual(self.store.db.execute('SELECT state FROM transfer_artifacts').fetchone()[0], 'failed')

    def test_cancel_upload_retains_valid_stage_and_never_commits_destination(self):
        artifact = self.stage()
        entered = threading.Event()
        original = self.device._run_cmd_locked
        def waiting(command, timeout, ensure_shell=True):
            if 'base64 -d' in command:
                entered.set()
                lease = self.device.transactions._local.lease
                with self.device.cv:
                    self.device.cv.wait_for(lease.is_set, timeout=2)
                lease.check()
            return original(command, timeout, ensure_shell)
        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=waiting):
            response = self.start('file-upload', artifact_id=artifact['id'], path='/tmp/cancelled')
            self.assertTrue(entered.wait(2))
            self.manager.tasks.cancel(response.payload['task']['id'], 'web')
            task = self.finish(response)
        self.assertEqual(task['state'], 'cancelled', task)
        self.assertNotIn('/tmp/cancelled', self.device.files)
        self.assertFalse(any('.seriald-' in path for path in self.device.files))
        self.assertEqual(self.artifacts.get(artifact['id'])['state'], 'ready')
        self.assertEqual(self.artifacts._readers, {})
        with self.artifacts.open(artifact['id']) as source:
            self.assertEqual(source.read(), self.data)

    def test_cancel_during_host_verification_never_publishes_download(self):
        entered = threading.Event()
        original = self.artifacts.seal
        def waiting(*args, **kwargs):
            entered.set()
            with self.manager.tasks._lock:
                context = next(iter(self.manager.tasks._active.values()))[0]
            self.assertTrue(context.cancelled.wait(2))
            return original(*args, **kwargs)
        with mock.patch.object(self.artifacts, 'seal', side_effect=waiting):
            response = self.start()
            self.assertTrue(entered.wait(2))
            self.manager.tasks.cancel(response.payload['task']['id'], 'web')
            task = self.finish(response)
        self.assertEqual(task['state'], 'cancelled', task)
        self.assertEqual(list(self.artifacts.root.iterdir()), [])
        self.assertFalse(self.device.file_transfer.active)

    def test_zero_length_transfers_and_host_quota_failure_release_uart(self):
        row = self.stage(b'')
        self.assertEqual(self.finish(self.start('file-upload', artifact_id=row['id'], path='/tmp/empty'))['state'], 'completed')
        self.device.files['/empty'] = b''
        task = self.finish(self.start(path='/empty'))
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(task['result']['size'], 0)
        self.artifacts.policy = ArtifactPolicy(max_file=1, max_total=1, reserve_free=0)
        failed = self.finish(self.start())
        self.assertEqual(failed['state'], 'failed')
        self.assertFalse(self.device.file_transfer.active)

    def test_http_staging_allows_large_host_chunks_and_keeps_serial_free(self):
        code, raw, _ = self.http('/api/artifact-create', {'key': self.device.key,
            'name': '固件.bin', 'size': len(self.data), 'actor': 'web'})
        self.assertEqual(code, 200)
        artifact_id = json.loads(raw)['artifact']['id']
        code, _, _ = self.http('/api/artifact-append', {'id': artifact_id, 'offset': 0,
            'data_b64': base64.b64encode(self.data).decode()})
        self.assertEqual(code, 200)
        self.assertEqual(body_limit('/api/artifact-append'), 2 * 1024 ** 2)
        self.assertEqual(body_limit('/api/file-transfer/chunk'), 65536)
        self.assertFalse(self.device.file_transfer.active)
        code, raw, _ = self.http('/api/artifact-seal', {'id': artifact_id})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['artifact']['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.http('/api/artifact?id=' + artifact_id)[0], 200)
        self.assertEqual(self.http('/api/artifact-download?id=' + artifact_id)[0], 409)
        self.assertEqual(self.http('/api/artifact-delete', {'id': artifact_id})[0], 200)

    def test_download_cannot_be_published_with_changed_host_content(self):
        original = self.artifacts.seal
        def corrupt(artifact_id, *args, **kwargs):
            path = self.artifacts.root / (artifact_id + '.part')
            path.write_bytes(b'x' * path.stat().st_size)
            return original(artifact_id, *args, **kwargs)
        with mock.patch.object(self.artifacts, 'seal', side_effect=corrupt):
            task = self.finish(self.start())
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(list(self.artifacts.root.iterdir()), [])
        self.assertEqual(self.store.db.execute('SELECT state FROM transfer_artifacts').fetchone()[0], 'failed')
