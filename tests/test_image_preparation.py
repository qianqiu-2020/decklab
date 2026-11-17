"""Host-only preparation; all executor and HTTP fixtures are isolated from USB/UART."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from seriald_core.flash_runner import LocalFlashExecutor
from seriald_core.image_preparation import for_manager
from seriald_core.tasks import TaskCancelled
import test_flash_integration as integration


class ImagePreparationTests(unittest.TestCase):
    def test_shared_directory_and_metadata_survive_plan_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'image.bin'
            source.write_bytes(b'test filesystem payload')
            artifact = dict(id='first-upload', path=str(source), name='rootfs.ext4', size=source.stat().st_size,
                            sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            runner = LocalFlashExecutor(root / 'flashing')
            imports = []
            def native(binding, operation, request, **options):
                self.assertEqual(operation, 'prepare-image')
                workspace = Path(request['workspace'])
                payload = workspace / 'payload'
                if not payload.exists():
                    payload.mkdir()
                    (payload / 'image.raw').write_bytes(source.read_bytes())
                    imports.append(operation)
                file = dict(name='image.raw', size=artifact['size'], sha256=artifact['sha256'])
                result = dict(version=1, artifact_sha256=artifact['sha256'], artifact_size=artifact['size'],
                    source_path=str(source), source_modified_ns=source.stat().st_mtime_ns,
                    root_path=str(payload), files=[file], catalogue=[])
                (workspace / ('prepared-' + artifact['sha256'] + '.json')).write_text(json.dumps(result))
                return {'artifact_sha256': artifact['sha256'], 'receipt_path': str(workspace / ('prepared-' + artifact['sha256'] + '.json')), 'reused': len(imports) == 1}
            with mock.patch.object(runner, '_run', side_effect=native):
                first = runner.prepare_image(('fixed-executor',), artifact)
                with mock.patch.object(runner, '_os_payload_metadata', side_effect=AssertionError('metadata reparsed')):
                    second = runner.prepare_image(('fixed-executor',), dict(artifact, id='another-upload'))
            self.assertEqual(len(imports), 1)
            self.assertEqual(first['root_path'], second['root_path'])
            self.assertEqual(Path(first['root_path']).parent, root / 'flashing/images' / artifact['sha256'])
            self.assertFalse(runner.cleanup_plan({'workspace': str(runner.image_workspace(artifact))}))
            self.assertTrue(Path(first['root_path']).is_dir())
            self.assertTrue(runner.prepared_artifact_unchanged(artifact))
            source.write_bytes(b'changed')
            self.assertFalse(runner.prepared_artifact_unchanged(artifact))
            self.assertEqual(runner._cache_reserved, 0)

    def test_native_receipt_reference_cannot_select_a_foreign_file(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = LocalFlashExecutor(Path(temp))
            artifact = dict(sha256='a' * 64, size=1, path='/unopened/source', name='image.bin')
            with mock.patch.object(runner, '_run', return_value={'receipt_path': '/foreign/receipt.json'}), \
                    self.assertRaisesRegex(ValueError, 'escaped its image directory'):
                runner.prepare_image(('fake-executor',), artifact)

    def test_image_lock_wait_is_cancellable(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = LocalFlashExecutor(Path(temp))
            artifact = {'sha256': 'a' * 64}
            held, release = threading.Event(), threading.Event()
            def hold():
                with runner.image_lock(artifact):
                    held.set()
                    release.wait(3)
            worker = threading.Thread(target=hold)
            worker.start()
            self.assertTrue(held.wait(1))
            try:
                def cancel():
                    raise TaskCancelled('cancelled while waiting')
                with self.assertRaises(TaskCancelled), runner.image_guard(artifact, cancel):
                    self.fail('acquired busy image')
            finally:
                release.set()
                worker.join(2)

    def test_auto_preparation_is_a_host_task_and_duplicate_seal_reuses_original(self):
        with integration.protected_flashing() as fixture:
            fixture.executor.supports_image_preparation = True
            calls = []
            def prepare(argv, artifact, on_event, check):
                check()
                calls.append(artifact['sha256'])
                on_event({'phase': 'checking-payloads', 'done': 32, 'total': 32})
                return {'artifact_sha256': artifact['sha256'], 'root_path': '/private/image',
                        'files': [{'name': 'rootfs.ext4'}], 'catalogue': []}
            fixture.executor.prepare_image = prepare
            helper = integration.FlashHTTPIntegrationTests()
            artifact = helper.image(fixture)
            service = for_manager(fixture.manager)
            with mock.patch('seriald_core.flash_discovery.executor_command', return_value=('fixed-executor',)):
                first = service.after_upload(artifact)
                self.assertIsNotNone(first)
                final = helper.terminal(fixture, first['id'])
                second = service.after_upload(artifact)
            self.assertEqual(first['kind'], 'image-prepare')
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertNotIn('root_path', final['result']['preparation'])
            self.assertNotIn('files', final['result']['preparation'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(fixture.executor.preparations, [])

    def test_web_image_can_be_reused_by_another_board(self):
        with integration.protected_flashing() as fixture:
            helper = integration.FlashHTTPIntegrationTests()
            artifact = helper.image(fixture, serial='FLASH-A')
            code, result = helper.request(fixture, '/api/flash-plan', {'key': 'other-board', 'serial': 'FLASH-B',
                'artifact_id': artifact['id'], 'mode': 'full', 'verify': 'none'})
            self.assertEqual(code, 200, result)
            code, started = helper.request(fixture, '/api/task-start', {'kind': 'flash', 'key': 'other-board',
                'serial': 'FLASH-B', 'plan_id': result['plan']['id'], 'binding_revision': 7, 'request_id': 'cross-board-image-001'})
            self.assertEqual(code, 202, started)
            final = helper.terminal(fixture, started['task']['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(fixture.executor.executions[0][0]['artifact']['id'], artifact['id'])
            with self.assertRaisesRegex(ValueError, 'not found'):
                fixture.manager.artifacts.get(artifact['id'], 'unrelated-agent', 'FLASH-B')
