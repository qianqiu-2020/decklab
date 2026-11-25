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

from decklab_core.flash_runner import LocalFlashExecutor
from decklab_core.image_preparation import for_manager
from decklab_core.tasks import TaskCancelled
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
                file = dict(name='image.raw', size=artifact['size'], sha256=artifact['sha256'],
                            modified_ns=(payload / 'image.raw').stat().st_mtime_ns)
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
            with mock.patch('decklab_core.flash_discovery.executor_command', return_value=('fixed-executor',)):
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

    def test_failed_preparation_retries_but_explicit_request_keeps_original_result(self):
        with integration.protected_flashing() as fixture:
            fixture.executor.supports_image_preparation = True
            fixture.executor.prepare_image = mock.Mock(side_effect=[ValueError('bad archive'), {'catalogue': []}])
            helper = integration.FlashHTTPIntegrationTests()
            artifact = helper.image(fixture)
            service = for_manager(fixture.manager)
            with mock.patch('decklab_core.flash_discovery.executor_command', return_value=('fixed-executor',)):
                first = service.start(artifact['id'], request_id='explicit-preparation-001')
                self.assertEqual(helper.terminal(fixture, first['id'])['state'], 'failed')
                self.assertEqual(service.start(artifact['id'], request_id='explicit-preparation-001')['id'], first['id'])
                second = service.start(artifact['id'])
                self.assertNotEqual(second['id'], first['id'])
                self.assertEqual(helper.terminal(fixture, second['id'])['state'], 'completed')
                fixture.executor.prepared_artifact_unchanged = lambda source: False
                fixture.executor.prepare_image.side_effect = None
                fixture.executor.prepare_image.return_value = {'catalogue': []}
                third = service.start(artifact['id'])
                self.assertNotEqual(third['id'], second['id'])
                self.assertEqual(helper.terminal(fixture, third['id'])['state'], 'completed')

    def test_unknown_preparation_keeps_original_task_cache_hold_and_source_until_recovery(self):
        from decklab_core.flash_runner import FlashRunnerError
        from decklab_core.board_maintenance import maintenance_for, CACHE_POOL_RESOURCE
        with integration.protected_flashing() as fixture:
            fixture.executor.supports_image_preparation = True
            error = FlashRunnerError('process cleanup not confirmed', cleanup_confirmed=False)
            error.workspace = '/private/managed/image'
            fixture.executor.prepare_image = mock.Mock(side_effect=error)
            helper = integration.FlashHTTPIntegrationTests()
            artifact = helper.image(fixture)
            service = for_manager(fixture.manager)
            with mock.patch('decklab_core.flash_discovery.executor_command', return_value=('fixed-executor',)):
                first = service.start(artifact['id'], request_id='unknown-preparation-001')
                final = helper.terminal(fixture, first['id'])
                self.assertEqual(final['state'], 'failed')
                self.assertTrue(final['flash']['recovery_required'])
                holds = maintenance_for(fixture.manager).holds()
                self.assertEqual([row['resource'] for row in holds], ['flash-image:' + artifact['sha256']])
                self.assertEqual(holds[0]['task_id'], first['id'])
                self.assertEqual(service.start(artifact['id'])['id'], first['id'])
                self.assertEqual(service.start(artifact['id'], request_id='unknown-preparation-001')['id'], first['id'])
                with self.assertRaisesRegex(ValueError, 'recovery'):
                    service.start(artifact['id'], request_id='unknown-preparation-new')
                result = service.request({'action':'recover', 'task_id':first['id'],
                    'attempt_id':final['flash']['attempt_id'], 'confirm_stopped':True, 'reason':'confirmed original test process stopped'})
                self.assertEqual(result.status, 200, result.payload)
                self.assertEqual(maintenance_for(fixture.manager).holds(), [])
                self.assertFalse(result.payload['task']['flash']['recovery_required'])
                self.assertEqual(fixture.manager.artifacts.get(artifact['id'])['state'], 'ready')

    def test_restart_retains_image_claim_and_requires_explicit_recovery(self):
        from decklab_core.board_maintenance import BoardMaintenance
        from decklab_core.tasks import TaskStore
        with integration.protected_flashing() as fixture:
            helper = integration.FlashHTTPIntegrationTests()
            artifact = helper.image(fixture)
            tasks = TaskStore(fixture.manager.store)
            task_id = tasks.create('image-prepare', 'web', '',
                [{'serial':'', 'connection_id':'image-prepare:' + artifact['id']}])
            maintenance = BoardMaintenance(fixture.manager)
            maintenance.begin_image(task_id, artifact['sha256'], '/private/image', artifact['id'])
            fixture.manager.artifacts.claim(artifact['id'], task_id)
            tasks.recover()
            restarted = BoardMaintenance(fixture.manager)
            fixture.manager.artifacts.recover()
            self.assertEqual(fixture.manager.artifacts.get(artifact['id'])['state'], 'claimed')
            current = tasks.get(task_id)
            self.assertEqual(current['state'], 'interrupted')
            self.assertTrue(current['flash']['recovery_required'])
            with self.assertRaisesRegex(ValueError, 'recovery'):
                restarted.check_preparation()
            recovered = restarted.confirm_recovery(task_id, 'web', current['flash']['attempt_id'],
                'confirmed original executor process stopped after restart', True, fixture.manager.artifacts)
            self.assertEqual(recovered['state'], 'interrupted')
            self.assertFalse(recovered['flash']['recovery_required'])
            restarted.check_preparation()

    def test_confirmed_cancel_discards_partial_extraction_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = LocalFlashExecutor(temp)
            artifact = dict(sha256='a' * 64, size=1, path='/unopened/source', name='image.bin')
            workspace = runner.image_workspace(artifact)
            def cancelled(*args, **kwargs):
                (workspace / 'partial').write_bytes(b'incomplete')
                raise TaskCancelled('cancel preparation')
            with mock.patch.object(runner, '_run', side_effect=cancelled):
                with self.assertRaises(TaskCancelled):
                    runner.prepare_image(('fake-executor',), artifact)
            self.assertFalse(workspace.exists())
            self.assertEqual(runner._cache_reserved, 0)

    def test_image_retention_keeps_fresh_plan_then_reclaims_idle_image(self):
        import os
        from decklab_core.flash_tasks import FlashActions
        with integration.protected_flashing() as fixture:
            runner = LocalFlashExecutor(Path(fixture.manager.artifacts.root).parent / 'flashing')
            fixture.manager.flash_executor = runner
            fixture.manager._flash_plans_lock = threading.RLock()
            image = runner.image_workspace({'sha256':'a' * 64})
            image.mkdir(parents=True)
            (image / 'old-image').write_bytes(b'expired')
            old = time.time() - fixture.manager.artifacts.policy.ready_seconds - 10
            os.utime(image, (old, old))
            document = json.dumps({'workspace':str(image)})
            with fixture.manager.store.lock, fixture.manager.store.db:
                fixture.manager.store.db.execute('INSERT INTO flash_plans VALUES(?,?,?,?,?,?,?)',
                    ('protected-image-plan','web','FLASH-A','b'*64,document,time.time(),None))
            FlashActions(fixture.manager).expire()
            self.assertTrue(image.exists())
            with fixture.manager.store.lock, fixture.manager.store.db:
                fixture.manager.store.db.execute('UPDATE flash_plans SET created=0')
            FlashActions(fixture.manager).expire()
            self.assertFalse(image.exists())

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
