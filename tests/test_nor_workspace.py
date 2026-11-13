"""Image-free NOR workflow over real HTTP with simulated USB and NOR bytes."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core.flash_runner import LocalFlashExecutor
from seriald_core.tasks import TaskError
import test_flash_discovery_integration as usb_fixture
import test_nor_workflow as nor_fixture


class WorkspaceExecutor(nor_fixture.SnapshotExecutor):
    def __init__(self, root, bootrom=False):
        super().__init__(root)
        self.local = LocalFlashExecutor(Path(root) / 'flashing')
        self.candidates = [usb_fixture.usb_candidate()]
        profile = {'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3', 'identity': {},
            'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt',
                'size_getvar': '__decklab_gpt_capacity', 'sector_size': 512, 'sparse': True, 'partitions': []}]}
        if bootrom:
            profile['bootrom_identity'] = {'version-IC': 'K3', 'version-brom': '1.0'}
        else:
            profile['ram_loader_protocol'] = 'decklab-nor-v1'
        self.candidates[0].update(profile=profile, preparable=bootrom,
            mode='bootrom' if bootrom else 'fastboot', inspection=None if bootrom else self.inspect(None))
        self.discoveries, self.workspaces = [], []

    def discover(self, argv, probe=False, device=None, check=lambda: None):
        check()
        self.discoveries.append((probe, device))
        return {'devices': copy.deepcopy(self.candidates)}

    def nor_workspace(self, binding, check=lambda: None):
        cache = self.local._directory(self.local.root / 'cache')
        workspace = Path(tempfile.mkdtemp(prefix='plan-', dir=cache))
        self.workspace = str(workspace)
        profile = copy.deepcopy(binding.profile_data)
        if profile.get('bootrom_identity'):
            profile['ram_loader_protocol'] = 'decklab-nor-v1'
            profile['prepare'] = []
            for name in ('factory/FSBL.bin', 'u-boot.itb'):
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'simulated RAM loader, never sent to a real board')
                profile['prepare'].extend([{'kind': 'stage', 'path': str(path),
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, {'kind': 'continue'}])
        self.workspaces.append(str(workspace))
        return {'workspace': str(workspace), 'execution_profile': profile}

    def execution_binding(self, *args, **kwargs):
        return self.local.execution_binding(*args, **kwargs)

    def prepare(self, binding, on_event=None, check=lambda: None, **identity):
        check()
        self.preparations.append(identity)
        final = dict(self.candidates[0]['device'], usb_address=8)
        self.candidates[0]['device'] = final
        return {'final_device': final, 'final_identity': {}}


class NORWorkspaceTests(unittest.TestCase):
    request = usb_fixture.FlashDiscoveryHTTPIntegrationTests.request
    discover = usb_fixture.FlashDiscoveryHTTPIntegrationTests.discover
    probe = usb_fixture.FlashDiscoveryHTTPIntegrationTests.probe
    scope = staticmethod(usb_fixture.FlashDiscoveryHTTPIntegrationTests.scope)
    terminal = usb_fixture.FlashDiscoveryHTTPIntegrationTests.terminal
    phase = usb_fixture.FlashDiscoveryHTTPIntegrationTests.phase

    def setup_workspace(self, fixture, root, bootrom=False):
        executor = WorkspaceExecutor(root, bootrom)
        fixture.manager.flash_executor = executor
        target = self.discover(fixture)[0]
        result = self.probe(fixture, target)
        self.assertTrue(result['capabilities']['nor_workspace_supported'])
        return executor, result['target']

    def begin(self, fixture, target, request_id='read-nor-workspace-001'):
        body = dict(self.scope(target), kind='nor-backup', binding_revision=1, request_id=request_id)
        code, result = self.request(fixture, '/api/task-start', body)
        self.assertEqual(code, 202, result)
        return result['task'], body

    def upload(self, fixture, task, target, data=b'new opensbi fixture'):
        body = dict(self.scope(target), task_id=task['id'], name='opensbi.itb', size=len(data))
        code, result = self.request(fixture, '/api/artifact-create', body)
        self.assertEqual(code, 200, result)
        artifact_id = result['artifact']['id']
        import base64
        for route, values in [('/api/artifact-append', {'offset': 0, 'data_b64': base64.b64encode(data).decode()}),
                               ('/api/artifact-seal', {})]:
            code, result = self.request(fixture, route, dict(values, id=artifact_id))
            self.assertEqual(code, 200, result)
        return artifact_id

    def test_read_without_image_then_upload_and_select_in_same_task(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, body = self.begin(fixture, target)
            ready = self.phase(fixture, task['id'], 'awaiting-selection')
            self.assertEqual(ready['flash']['operation'], 'nor-workspace')
            self.assertEqual(ready['flash']['request_id'], body['request_id'])
            self.assertEqual(ready['flash']['nor_before']['artifact']['state'], 'available')
            self.assertEqual(executor.plans, [])
            self.assertEqual(executor.executions, [])
            self.assertEqual(executor.preparations, [])
            self.assertEqual(fixture.store.db.execute("SELECT COUNT(*) FROM transfer_artifacts WHERE direction='upload'").fetchone()[0], 0)
            artifact_id = self.upload(fixture, task, target)
            selection = dict(self.scope(target), id=task['id'], layout_digest='1' * 64, preview=True,
                selections=[{'storage': 'active', 'partition': 'opensbi', 'artifact_id': artifact_id}])
            code, result = self.request(fixture, '/api/flash-selection', selection)
            self.assertEqual(code, 200, result)
            self.assertFalse(result['plan']['write_layout'])
            self.assertEqual(result['plan']['verify'], 'required')
            self.assertEqual(executor.executions, [])
            code, result = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                id=task['id'], layout_digest='1' * 64, plan_id=result['plan']['id'], request_id='confirm-nor-write-001'))
            self.assertEqual(code, 200, result)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(final['flash']['verify_result'], 'verified')
            self.assertEqual(len(executor.snapshots), 1)
            self.assertEqual(len(executor.executions), 1)
            code, same = self.request(fixture, '/api/task-start', body)
            self.assertEqual(code, 202, same)
            self.assertEqual(same['task']['id'], task['id'])
            self.assertEqual(len(executor.workspaces), 1)

    def test_bootrom_loads_once_then_stops_before_any_storage_write(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root, True)
            task, body = self.begin(fixture, target)
            ready = self.phase(fixture, task['id'], 'awaiting-selection')
            self.assertEqual(len(executor.preparations), 1)
            self.assertEqual(len(executor.snapshots), 1)
            self.assertEqual(executor.executions, [])
            self.assertEqual(ready['flash_targets'][0]['generation'], target['target_generation'])
            artifact_id = self.upload(fixture, task, target)
            self.assertTrue(artifact_id)
            code, _ = self.request(fixture, '/api/task-cancel', dict(self.scope(target), id=task['id']))
            self.assertEqual(code, 200)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'cancelled')
            self.assertEqual(final['flash']['write_result'], 'not-started')
            self.assertEqual(executor.executions, [])

    def test_single_file_preview_accepts_executor_null_candidate_list(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, _ = self.begin(fixture, target)
            self.phase(fixture, task['id'], 'awaiting-selection')
            artifact_id = self.upload(fixture, task, target)
            original = executor.plan

            def plan(*args, **kwargs):
                result = original(*args, **kwargs)
                # Go serializes the absent package catalogue as JSON null for
                # an external single-file payload; it is still a valid plan.
                result['payload_candidates'] = None
                return result

            with mock.patch.object(executor, 'plan', side_effect=plan):
                code, result = self.request(fixture, '/api/flash-selection', dict(
                    self.scope(target), id=task['id'], layout_digest='1' * 64, preview=True,
                    selections=[{'storage': 'active', 'partition': 'opensbi', 'artifact_id': artifact_id}]))
            self.assertEqual(code, 200, result)
            self.assertEqual([write['partition'] for write in result['plan']['writes']], ['opensbi'])
            self.assertFalse(result['plan']['write_layout'])
            self.assertEqual(executor.executions, [])

    def test_read_failure_never_opens_partition_selection_or_writes(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            executor.snapshot_failure = True
            task, _ = self.begin(fixture, target)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'failed')
            self.assertEqual(final['flash']['write_result'], 'not-started')
            self.assertEqual(executor.executions, [])
            self.assertNotIn('nor_before', final['flash'])

    def test_url_import_after_bootrom_handoff_uses_original_task_scope(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root, True)
            task, _ = self.begin(fixture, target)
            self.phase(fixture, task['id'], 'awaiting-selection')
            def download(artifacts, context, url, serial, name=None, sha256=None):
                return {'fixture': True, 'serial': serial}
            with mock.patch('seriald_core.image_download.fetch_image', side_effect=download):
                body = dict(self.scope(target), task_id=task['id'], url='https://firmware.example/opensbi.itb',
                            request_id='nor-url-import-001')
                code, result = self.request(fixture, '/api/image-download', body)
                self.assertEqual(code, 202, result)
                imported = self.terminal(fixture, result['task']['id'])
                self.assertEqual(imported['state'], 'completed')
                wrong = dict(body, request_id='nor-url-import-002', target_generation='wrong')
                code, result = self.request(fixture, '/api/image-download', wrong)
                self.assertEqual(code, 409, result)
            code, _ = self.request(fixture, '/api/task-cancel', dict(self.scope(target), id=task['id']))
            self.assertEqual(code, 200)
            self.terminal(fixture, task['id'])
            code, result = self.request(fixture, '/api/image-download', dict(body, request_id='nor-url-import-003'))
            self.assertEqual(code, 409, result)
            self.assertEqual(executor.executions, [])

    def test_agent_and_injected_write_payload_cannot_start_a_read_workspace(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            body = dict(self.scope(target), kind='nor-backup', binding_revision=1, request_id='nor-denied-001')
            code, issued = self.request(fixture, '/api/agent-share', {'scope': 'all'})
            self.assertEqual(code, 200)
            code, result = self.request(fixture, '/api/task-start', body, token=issued['token'])
            self.assertEqual(code, 403, result)
            code, result = self.request(fixture, '/api/task-start', dict(body, selections=[{'partition': 'env'}]))
            self.assertEqual(code, 400, result)
            self.assertEqual(executor.workspaces, [])
            self.assertEqual(executor.executions, [])

    def test_short_backup_receipt_cannot_enter_selection(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            original = executor.snapshot
            def wrong(*args, **kwargs):
                result = original(*args, **kwargs)
                result['inspection']['storages'][0]['size'] = 16 << 20
                return result
            executor.snapshot = wrong
            task, _ = self.begin(fixture, target)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'failed')
            self.assertNotIn('nor_before', final['flash'])
            self.assertEqual(final['flash']['write_result'], 'not-started')
            self.assertEqual(executor.executions, [])

    def test_mixed_package_and_raw_payloads_do_not_depend_on_partition_row_order(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, _ = self.begin(fixture, target)
            self.phase(fixture, task['id'], 'awaiting-selection')
            raw = self.upload(fixture, task, target)
            package = fixture.manager.artifacts.create('upload', 'fixture-nor-package.tar.gz', 7, serial='')
            fixture.manager.artifacts.append(package['id'], 0, b'fixture')
            package = fixture.manager.artifacts.seal(package['id'])
            code, result = self.request(fixture, '/api/flash-selection', dict(self.scope(target), id=task['id'],
                layout_digest='1' * 64, preview=True, selections=[
                    {'storage': 'active', 'partition': 'fsbl', 'artifact_id': raw},
                    {'storage': 'active', 'partition': 'opensbi', 'artifact_id': package['id']}]))
            self.assertEqual(code, 200, result)
            self.assertEqual(executor.plans[-1][0]['id'], package['id'])
            self.assertEqual(executor.executions, [])
            self.request(fixture, '/api/task-cancel', dict(self.scope(target), id=task['id']))
            self.terminal(fixture, task['id'])
