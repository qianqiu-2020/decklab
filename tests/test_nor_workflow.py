"""Authenticated HTTP + SQLite with simulated NOR bytes; no hardware access."""
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace

from seriald_core.tasks import TaskError
from seriald_core.flash_runner import FlashRunnerError
import test_flash_integration as flash_fixture

FakeFlashExecutor = flash_fixture.FakeFlashExecutor
protected_flashing = flash_fixture.protected_flashing


class SnapshotExecutor(FakeFlashExecutor):
    supports_nor_snapshot = True

    def __init__(self, workspace):
        super().__init__()
        self.workspace = str(workspace)
        self.snapshot_failure = False
        self.verify_failure = False
        self.snapshots = []
        self.data = b'\xff' * (8 << 20)
        self.parts = [('bootinfo', 0, 128 << 10), ('fsbl', 128 << 10, 512 << 10),
                      ('env', 640 << 10, 64 << 10), ('esos', 704 << 10, 1024 << 10),
                      ('opensbi', 1728 << 10, 384 << 10), ('uboot', 2112 << 10, 4096 << 10)]

    def inspect(self, binding, check=lambda: None):
        check()
        return {'mode': 'download', 'layout_digest': '1' * 64, 'storages': [
            {'id': 'active', 'size': 8 << 20, 'capacity_source': 'decklab-nor-v1',
             'capacity_verified': True, 'verify': True, 'partitions': [
                 {'name': name, 'offset': offset, 'size': size} for name, offset, size in self.parts]}]}

    def raw_snapshot(self, identity, phase):
        path = Path(self.workspace) / ('nor-%s-%s-%s.bin' %
            (identity['task_id'], identity['attempt_id'], phase))
        path.write_bytes(self.data)
        return {'path': str(path), 'size': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest(),
                'layout_digest': '1' * 64, 'partitions': [
                    {'name': name, 'offset': offset, 'size': size,
                     'sha256': hashlib.sha256(self.data[offset:offset + size]).hexdigest()}
                    for name, offset, size in self.parts]}

    def snapshot(self, binding, workspace, on_event=None, check=lambda: None, **identity):
        check()
        self.snapshots.append(identity)
        if self.snapshot_failure:
            raise TaskError('simulated NOR read failure', 502)
        if on_event:
            on_event({'event': 'progress', 'phase': 'reading-nor', 'done': len(self.data), 'total': len(self.data)})
        return {'ok': True, 'snapshot': self.raw_snapshot(identity, 'before'), 'inspection': self.inspect(binding)}

    def execute(self, binding, plan, on_event=None, check=lambda: None, **identity):
        baseline = identity.get('nor_baseline')
        if not baseline or Path(baseline['path']).read_bytes() != self.data:
            raise AssertionError('backup must be published and pinned before a write')
        result = super().execute(binding, plan, on_event, check, **identity)
        result['nor_after'] = self.raw_snapshot(identity, 'after')
        result['verified_bytes'] = len(self.data)
        if self.verify_failure:
            error = FlashRunnerError('simulated verification failure', outcome={
                'write_result': 'completed', 'verify_result': 'failed'}, code='verify-failed')
            error.nor_snapshots = {'nor_after': result['nor_after']}
            raise error
        return result


class NORWorkflowTests(unittest.TestCase):
    request = flash_fixture.FlashHTTPIntegrationTests.request
    image = flash_fixture.FlashHTTPIntegrationTests.image
    plan = flash_fixture.FlashHTTPIntegrationTests.plan
    start = flash_fixture.FlashHTTPIntegrationTests.start
    terminal = flash_fixture.FlashHTTPIntegrationTests.terminal

    def attach_executor(self, fixture, root):
        executor = SnapshotExecutor(root)
        fixture.manager.flash_executor = executor
        return executor

    def wait_selection(self, fixture, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(code, 200)
            task = result['task']
            if task['progress'].get('phase') == 'awaiting-selection':
                return task
            if task['state'] not in ('queued', 'running'):
                self.fail(str(task))
            time.sleep(.02)
        self.fail('simulated snapshot did not reach partition selection')

    def test_backup_is_published_before_partition_selection_and_survives_cancel(self):
        with protected_flashing() as fixture, tempfile.TemporaryDirectory() as workspace:
            executor = self.attach_executor(fixture, workspace)
            plan = self.plan(fixture, self.image(fixture), mode='partitions')
            code, result = self.start(fixture, plan)
            self.assertEqual(code, 202, result)
            task = self.wait_selection(fixture, result['task']['id'])
            backup = task['flash']['nor_before']
            self.assertEqual(backup['artifact']['size'], 8 << 20)
            self.assertEqual(len(backup['partitions']), 6)
            self.assertEqual(executor.executions, [])
            self.assertNotIn(workspace, str(task))
            code, _ = self.request(fixture, '/api/task-cancel',
                {'id': task['id'], 'key': 'offline-board', 'serial': 'FLASH-A'})
            self.assertEqual(code, 200)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'cancelled')
            self.assertEqual(fixture.manager.artifacts.get(backup['artifact']['id'])['state'], 'available')
            self.assertEqual(executor.executions, [])

    def test_backup_read_failure_prevents_all_writes(self):
        with protected_flashing() as fixture, tempfile.TemporaryDirectory() as workspace:
            executor = self.attach_executor(fixture, workspace)
            executor.snapshot_failure = True
            image = self.image(fixture)
            plan = self.plan(fixture, image, mode='partitions', selections=[
                {'storage': 'active', 'partition': 'opensbi', 'artifact_id': image['id']}])
            code, result = self.start(fixture, plan)
            self.assertEqual(code, 202)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['flash']['write_result'], 'not-started')
            self.assertEqual(executor.executions, [])

    def test_selected_write_pins_original_backup_and_publishes_after_image(self):
        with protected_flashing() as fixture, tempfile.TemporaryDirectory() as workspace:
            executor = self.attach_executor(fixture, workspace)
            image = self.image(fixture)
            plan = self.plan(fixture, image, mode='partitions', selections=[
                {'storage': 'active', 'partition': 'opensbi', 'artifact_id': image['id']}])
            code, result = self.start(fixture, plan)
            self.assertEqual(code, 202)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(task['flash']['nor_before']['artifact']['state'], 'available')
            self.assertEqual(task['flash']['nor_after']['artifact']['state'], 'available')
            self.assertEqual(task['flash']['verified_bytes'], 8 << 20)
            self.assertEqual(len(executor.executions), 1)
            self.assertEqual(executor.executions[0][1]['nor_baseline']['id'], task['flash']['nor_before']['artifact']['id'])
            self.assertNotIn(workspace, str(task))

    def test_after_publication_failure_does_not_lose_confirmed_write_or_verify_result(self):
        with protected_flashing() as fixture, tempfile.TemporaryDirectory() as workspace:
            executor = self.attach_executor(fixture, workspace)
            fixture.manager.artifacts.policy = replace(fixture.manager.artifacts.policy, max_files=2)
            image = self.image(fixture)
            plan = self.plan(fixture, image, mode='partitions', selections=[
                {'storage': 'active', 'partition': 'opensbi', 'artifact_id': image['id']}])
            code, result = self.start(fixture, plan)
            self.assertEqual(code, 202)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['flash']['write_result'], 'completed')
            self.assertEqual(task['flash']['verify_result'], 'verified')
            self.assertEqual(len(executor.executions), 1)

    def test_failed_verification_preserves_both_readbacks_and_never_replays_write(self):
        with protected_flashing() as fixture, tempfile.TemporaryDirectory() as workspace:
            executor = self.attach_executor(fixture, workspace)
            executor.verify_failure = True
            image = self.image(fixture)
            plan = self.plan(fixture, image, mode='partitions', selections=[
                {'storage': 'active', 'partition': 'opensbi', 'artifact_id': image['id']}])
            code, result = self.start(fixture, plan)
            self.assertEqual(code, 202)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['flash']['write_result'], 'completed')
            self.assertEqual(task['flash']['verify_result'], 'failed')
            self.assertEqual(task['flash']['nor_before']['artifact']['state'], 'available')
            self.assertEqual(task['flash']['nor_after']['artifact']['state'], 'available')
            self.assertEqual(len(executor.executions), 1)
