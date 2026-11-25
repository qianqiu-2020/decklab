"""Real authenticated HTTP coordination with temporary images and a fake executor.

The fixture never scans USB, opens UART, invokes goflash or resets a board.
"""
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from decklab_core import control_server, web
from decklab_core.agent_grants import AgentGrantStore
from decklab_core.board_maintenance import maintenance_for
from decklab_core.devices import Manager
from decklab_core.device import Device
from decklab_core.flash_runner import FlashBook, FlashRunnerError, LocalFlashExecutor
from decklab_core.flash_store import FlashStore, MAX_EVENTS
from decklab_core.flash_tasks import FlashActions, _system_os
from decklab_core.httpd import BoundedThreadingTCPServer
from decklab_core.storage import Store
from decklab_core.tasks import TaskError
from decklab_core.transactions import transactions_for
from test_connection_access import api_request


class FlashedSystemEvidenceTests(unittest.TestCase):
    def test_system_hint_follows_written_payload_and_preserves_firmware_only_history(self):
        root = {'storage': 'active', 'kind': 'partition', 'partition': 'rootfs', 'sha256': '1' * 64}
        metadata = [dict(root, content={'os': {'id': 'debian'}})]
        self.assertEqual(_system_os({'writes': [root]}, metadata), 'debian')
        self.assertEqual(_system_os({'writes': [dict(root, sha256='2' * 64)]}, metadata), '')
        self.assertIsNone(_system_os({'writes': [
            {'storage': 'active', 'kind': 'partition', 'partition': 'uboot'}]}, metadata))


class FakeFlashExecutor:
    def __init__(self):
        self.plans, self.checks, self.executions, self.preparations = [], [], [], []
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False
        self.requires_prepare = False
        self.failure = None
        self.plan_failure = None
        self.image_failure = None
        self.workspace = None
        self.image_type = 'raw'
        self.image_started, self.image_release = threading.Event(), threading.Event()
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        self.block_image = self.block_inspection = False

    def inspect(self, binding, check=lambda: None):
        check()
        return {'mode': 'download', 'storages': [
            {'id': name, 'size': 1024 * 1024, 'partitions': [
                {'id': 'rootfs', 'size': 524288}]} for name in ('emmc', 'nvme')],
            'verification': True, 'layout_digest': '1' * 64}

    def plan(self, binding, artifact, mode, selections=None,
             verify='if-supported', check=lambda: None, target_storage=None, inspection_scope=None, on_event=None):
        check()
        self.plans.append(copy.deepcopy((artifact, mode, selections, verify, target_storage)))
        if on_event:
            on_event({'event': 'progress', 'phase': 'expanding-image', 'done': 8, 'total': 32})
        self.image_started.set()
        while self.block_image and not self.image_release.wait(.02):
            check()
        if self.image_failure:
            self.image_failure.stage = 'image-plan'
            raise self.image_failure
        writes = ([{'storage': item['storage'], 'partition': item['partition'],
                    'kind': 'partition', 'size': item['artifact']['size'],
                    'sha256': item['artifact']['sha256'], 'path': item['artifact']['path']}
                   for item in selections or []] if mode == 'partitions' else [
                       {'storage': target_storage or 'emmc', 'partition': 'disk', 'kind': 'disk',
                        'size': artifact['size'], 'sha256': artifact['sha256'], 'path': artifact['path']}])
        with inspection_scope() if inspection_scope else nullcontext():
            self.inspection_started.set()
            while self.block_inspection and not self.inspection_release.wait(.02):
                check()
            if self.plan_failure:
                self.plan_failure.stage = 'check-plan'
                raise self.plan_failure
            candidates = ([{'storage': storage, 'partition': 'rootfs', 'kind': 'partition',
                            'size': artifact['size'], 'sha256': artifact['sha256'], 'path': artifact['path']}
                           for storage in ('emmc', 'nvme')] if self.image_type == 'package' else [])
            return {'plan': {'schema': 1, 'mode': mode, 'ready': not self.requires_prepare,
                             'write_layout': mode == 'full', 'writes': writes,
                             'image_type': self.image_type, 'artifact': copy.deepcopy(artifact),
                             'layout_digest': '1' * 64, 'verify': verify},
                    'requires_prepare': self.requires_prepare, 'inspection': self.inspect(binding, check),
                    'workspace': self.workspace, 'payload_candidates': candidates}

    def check_plan(self, binding, plan, payload_candidates=None, check=lambda: None, selections=None):
        check()
        self.checks.append(copy.deepcopy((plan, payload_candidates, selections)))
        if self.plan_failure:
            self.plan_failure.stage = 'check-plan'
            raise self.plan_failure
        checked = copy.deepcopy(plan)
        checked['ready'] = not self.requires_prepare
        if selections is not None:
            requested = {(item['storage'], item['partition']) for item in selections}
            candidates = {(item['storage'], item['partition']): item for item in payload_candidates or []}
            if not requested.issubset(candidates):
                raise FlashRunnerError('selected target is absent from the fixed cached package')
            checked['writes'] = [copy.deepcopy(candidates[key]) for key in sorted(requested)]
        return {'plan': checked, 'requires_prepare': self.requires_prepare,
                'inspection': self.inspect(binding, check), 'payload_candidates': payload_candidates or []}

    def prepare(self, binding, on_event=None, check=lambda: None, **identity):
        check()
        self.preparations.append(identity)
        self.requires_prepare = False
        if on_event:
            on_event({'type': 'progress', 'phase': 'prepare', 'done': 0, 'total': 0})
        return {'ok': True}

    def execute(self, binding, plan, on_event=None, check=lambda: None, **identity):
        check()
        self.executions.append(copy.deepcopy((plan, identity)))
        self.started.set()
        if on_event:
            on_event({'type': 'progress', 'phase': 'writing', 'done': 0,
                      'total': sum(item['size'] for item in plan['writes'])})
        while self.block and not self.release.wait(.02):
            check()
        check()
        if self.failure:
            raise self.failure
        return {'ok': True, 'write_result': 'completed', 'verify_result': 'verified',
                'boot_result': 'not-requested',
                'written_bytes': sum(item['size'] for item in plan['writes'])}


@contextmanager
def protected_flashing():
    with tempfile.TemporaryDirectory() as directory:
        store, grants = Store(str(Path(directory) / 'registry.sqlite3')), AgentGrantStore(':memory:')
        for serial, label in (('FLASH-A', 'offline-board'), ('FLASH-B', 'other-board')):
            store.bind({'serial': serial, 'serial_source': 'device-tree'}, '', '', label)
        manager = Manager(store, artifact_dir=str(Path(directory) / 'artifacts'))
        configuration = {'boards': {serial: {
            'binding_revision': 7, 'profile': 'fixture-board', 'profile_data': {},
            'console': {'provider': 'none'},
            'flash': {'argv': [sys.executable], 'endpoint': 'fixture-' + serial,
                      'device': {'port_path': [1, 4]}}}
            for serial in ('FLASH-A', 'FLASH-B')}}
        manager.flash_book, manager.flash_executor = FlashBook(configuration), FakeFlashExecutor()
        class Control(control_server.ControlHandler):
            auth_token = 'fixture-owner-token'
            grant_store = grants
            trust_loopback = False
            agent_tokens = {}
        with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                BoundedThreadingTCPServer(('127.0.0.1', 0), Control) as control, \
                mock.patch('decklab_core.devices.open_serial', side_effect=AssertionError('must not open UART')):
            collector.manager, Control.collector_socket = manager, collector.server_address
            workers = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (collector, control)]
            for worker in workers:
                worker.start()
            fixture = SimpleNamespace(url='http://127.0.0.1:%d' % control.server_address[1],
                token=Control.auth_token, manager=manager, executor=manager.flash_executor, grants=grants,
                configuration=configuration)
            try:
                yield fixture
            finally:
                fixture.executor.release.set()
                fixture.executor.image_release.set()
                fixture.executor.inspection_release.set()
                manager.close()
                for server in (collector, control):
                    server.shutdown()
                for worker in workers:
                    worker.join(timeout=3)
                grants.close()
                store.close()


@contextmanager
def fake_attachment(fixture):
    board = fixture.manager.boards['FLASH-A']
    attachment = Device('fixture-uart-no-reader', 'FAKE-UART-DO-NOT-OPEN', fixture.manager, board=board)
    attachment.present, attachment.fd = True, 98765
    board.attach(attachment)
    fixture.manager.devices[attachment.by_path] = attachment
    try:
        yield attachment
    finally:
        attachment.fd, attachment.present = None, False


class FlashHTTPIntegrationTests(unittest.TestCase):
    def request(self, fixture, route, payload=None, query=None, token=None):
        return api_request(fixture.url, route, token or fixture.token, payload, query)

    def image(self, fixture, serial='FLASH-A', actor='web'):
        data = b'isolated-image-fixture'
        artifacts = fixture.manager.artifacts
        row = artifacts.create('upload', 'fixture.img', len(data), actor, serial)
        artifacts.append(row['id'], 0, data, actor, serial)
        return artifacts.seal(row['id'], actor, serial)

    def plan(self, fixture, image, mode='full', **values):
        body = {'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'],
                'mode': mode, 'verify': 'if-supported'}
        body.update(values)
        code, result = self.request(fixture, '/api/flash-plan', body)
        self.assertEqual(code, 200, result)
        return result['plan']

    def start(self, fixture, plan, request_id='fixture-request-001', **values):
        body = {'kind': 'flash', 'key': 'offline-board', 'serial': 'FLASH-A',
                'plan_id': plan['id'], 'binding_revision': 7, 'request_id': request_id}
        body.update(values)
        return self.request(fixture, '/api/task-start', body)

    def terminal(self, fixture, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(code, 200, result)
            if result['task']['state'] not in ('queued', 'running'):
                return result['task']
            time.sleep(.02)
        self.fail('fake flash task did not finish')

    def test_skip_readback_is_scoped_idempotent_and_does_not_cancel_required_flash(self):
        with protected_flashing() as fixture:
            fixture.executor.supports_skip_verification = True
            verifying = threading.Event()
            def execute(binding, plan, on_event, check, skip_verification, **identity):
                on_event({'event': 'progress', 'phase': 'writing', 'done': 16, 'total': plan['writes'][0]['size'],
                          'storage': plan['writes'][0]['storage'], 'partition': plan['writes'][0]['partition']})
                fixture.executor.started.set()
                while not fixture.executor.release.wait(.02):
                    check()
                on_event({'event': 'progress', 'phase': 'verifying-system', 'done': 1, 'total': 32})
                verifying.set()
                deadline = time.monotonic() + 5
                while not skip_verification():
                    check()
                    if time.monotonic() > deadline:
                        raise AssertionError('skip request did not reach the original execution')
                    time.sleep(.01)
                return {'ok': True, 'write_result': 'completed', 'verify_result': 'skipped', 'boot_result': 'not-requested'}
            fixture.executor.execute = execute
            plan = self.plan(fixture, self.image(fixture), verify='required')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            self.assertTrue(fixture.executor.started.wait(2))
            code, live = self.request(fixture, '/api/task', query={'id': started['task']['id']})
            self.assertEqual(code, 200)
            self.assertEqual(live['task']['progress']['partition_done'], 16)
            payload = {'id': started['task']['id'], 'action': 'skip-verification', 'key': 'offline-board', 'serial': 'FLASH-A'}
            code, _ = self.request(fixture, '/api/flash-selection', payload)
            self.assertEqual(code, 409, 'writing cannot skip integrity checks')
            fixture.executor.release.set()
            self.assertTrue(verifying.wait(2))
            code, _ = self.request(fixture, '/api/flash-selection', dict(payload, serial='FLASH-B'))
            self.assertNotEqual(code, 200)
            self.assertEqual(FlashActions(fixture.manager).skip_verification(payload, 'agent-unrelated').status, 404)
            code, result = self.request(fixture, '/api/flash-selection', payload)
            self.assertEqual(code, 200, result)
            self.assertTrue(result['task']['flash']['verify_skip_requested'])
            final = self.terminal(fixture, payload['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(final['result']['verify_result'], 'skipped')
            self.assertFalse(final['cancel_requested'])
            code, repeated = self.request(fixture, '/api/flash-selection', payload)
            self.assertEqual(code, 200, repeated)
            self.assertEqual(repeated['task']['id'], payload['id'])

    def test_cached_nor_catalogue_uses_its_sealed_execution_profile(self):
        from decklab_core.flash_tasks import FlashActions
        with protected_flashing() as fixture:
            actions = FlashActions(fixture.manager)
            binding = SimpleNamespace(target_id='')
            sealed = {'ram_loader_protocol':'spacemit-ram-v1', 'storages':[
                {'id':'active','package_tables':['partition_nor.json']}]}
            prepared = {'intent':{'selections':[]}, 'workspace':'/managed/plan',
                        'execution_profile':sealed, 'executor_plan':{'writes':[]}}
            checked = SimpleNamespace(profile_data=sealed)
            executor = SimpleNamespace(execution_binding=mock.Mock(return_value=checked),
                                       check_plan=mock.Mock(return_value={'plan':{'writes':[]}}))
            with mock.patch.object(actions, '_store_plan', return_value='stored'):
                result = actions._make_plan(binding, executor, {'selections':[]}, 'web', 'FLASH-A', prepared=prepared)
            self.assertEqual(result, 'stored')
            self.assertEqual(executor.execution_binding.call_args.args[:3], (binding, sealed, '/managed/plan'))
            self.assertIs(executor.check_plan.call_args.args[0], checked)

    def test_reset_and_optional_readback_survive_preview_and_are_not_replayed(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            code, refused = self.request(fixture, '/api/flash-plan', {
                'key':'offline-board', 'serial':'FLASH-A', 'artifact_id':image['id'],
                'mode':'full', 'reset_after':True})
            self.assertEqual(code, 400, refused)
            fixture.executor.supports_reset_after = True
            plan = self.plan(fixture, image, verify='none', reset_after=True)
            self.assertTrue(plan['reset_after'])
            self.assertEqual(plan['verify'], 'none')
            original = fixture.executor.execute
            resets = []
            def execute(*args, reset_after=False, **kwargs):
                resets.append(reset_after)
                result = original(*args, **kwargs)
                result.update(boot_result='reset-command-acknowledged', verify_result='not-requested')
                return result
            with mock.patch.object(fixture.executor, 'execute', side_effect=execute):
                code, started = self.start(fixture, plan)
                self.assertEqual(code, 202, started)
                task = self.terminal(fixture, started['task']['id'])
                self.assertEqual(task['state'], 'completed', task)
                code, repeated = self.start(fixture, plan)
                self.assertEqual(repeated['task']['id'], task['id'])
            self.assertEqual(resets, [True])
            self.assertEqual(task['result']['boot_result'], 'reset-command-acknowledged')

    def test_offline_board_full_flash_has_real_board_target_and_never_exposes_host_paths(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            code, capabilities = self.request(fixture, '/api/flash-capabilities',
                                              query={'key': 'offline-board', 'serial': 'FLASH-A'})
            self.assertEqual(code, 200, capabilities)
            self.assertTrue(capabilities['supported'])
            self.assertEqual(capabilities['artifacts'][0]['id'], image['id'])
            self.assertEqual(fixture.executor.executions, [])
            plan = self.plan(fixture, image)
            self.assertEqual(plan['writes'][0]['sha256'], hashlib.sha256(b'isolated-image-fixture').hexdigest())
            self.assertNotIn('path', json.dumps(plan))
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(task['board_targets'], ['FLASH-A'])
            self.assertEqual(task['targets'], [], 'offline flash must not invent a UART connection')
            self.assertEqual(task['result']['write_result'], 'completed')
            self.assertEqual(task['result']['boot_result'], 'not-requested')
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
            self.assertNotIn(str(fixture.manager.artifacts.root), json.dumps(task))

    def test_prepare_for_review_cannot_start_an_already_writable_plan(self):
        with protected_flashing() as fixture:
            plan = self.plan(fixture, self.image(fixture))
            code, _ = self.start(fixture, plan, prepare_for_review=True)
            self.assertEqual(code, 409)
            self.assertEqual(fixture.executor.executions, [])

    def test_partition_preview_selects_storage_and_does_not_implicitly_write_layout(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            selection = [{'storage': 'nvme', 'partition': 'rootfs', 'artifact_id': image['id']}]
            plan = self.plan(fixture, image, 'partitions', selections=selection)
            self.assertFalse(plan['write_layout'])
            self.assertEqual([(item['storage'], item['partition']) for item in plan['writes']], [('nvme', 'rootfs')])
            self.assertEqual(fixture.executor.executions, [], 'preview must not write')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            executed = fixture.executor.executions[0][0]
            self.assertFalse(executed['write_layout'])
            self.assertEqual([item['storage'] for item in executed['writes']], ['nvme'])

    def test_duplicate_submission_and_changed_request_input_are_checked_before_reexecution(self):
        with protected_flashing() as fixture:
            fixture.executor.block = True
            plan = self.plan(fixture, self.image(fixture))
            code, first = self.start(fixture, plan)
            self.assertEqual(code, 202, first)
            self.assertTrue(fixture.executor.started.wait(2))
            code, repeated = self.start(fixture, plan)
            self.assertEqual(code, 202, repeated)
            self.assertEqual(first['task']['id'], repeated['task']['id'])
            code, changed = self.start(fixture, plan, binding_revision=8)
            self.assertEqual(code, 409, changed)
            self.assertEqual(len(fixture.executor.executions), 1)
            fixture.executor.release.set()
            self.assertEqual(self.terminal(fixture, first['task']['id'])['state'], 'completed')
            code, completed = self.start(fixture, plan)
            self.assertEqual(code, 202, completed)
            self.assertEqual(completed['task']['id'], first['task']['id'])
            self.assertEqual(len(fixture.executor.executions), 1)

    def test_wrong_board_stale_revision_and_arbitrary_command_are_rejected_before_execute(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            plan = self.plan(fixture, image)
            for values in ({'serial': 'FLASH-B'}, {'binding_revision': 8}, {'argv': ['/bin/false']}):
                with self.subTest(values=values):
                    code, result = self.start(fixture, plan, **values)
                    self.assertIn(code, (400, 404, 409), result)
            self.assertEqual(fixture.executor.executions, [])

    def test_default_agent_denial_and_board_scope_use_real_control_proxy(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            agent_token, grant = fixture.grants.issue('device', 'FLASH-A')
            code, available = self.request(fixture, '/api/flash-capabilities',
                                           query={'key': 'offline-board'}, token=agent_token)
            self.assertEqual(code, 200, available)
            self.assertFalse(available['supported'])
            code, denied = self.request(fixture, '/api/flash-plan',
                {'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'},
                token=agent_token)
            self.assertEqual(code, 403, denied)
            plan = self.plan(fixture, image)
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            for route in ('/api/task', '/api/flash-events'):
                code, result = self.request(fixture, route,
                    query={'key': 'offline-board', 'id': task['id']}, token=agent_token)
                self.assertEqual(code, 404, result)
            other_token, _ = fixture.grants.issue('device', 'FLASH-B')
            code, result = self.request(fixture, '/api/flash-events',
                query={'key': 'other-board', 'id': task['id']}, token=other_token)
            self.assertEqual(code, 404, result)
            fixture.grants.revoke(grant['id'])
            self.assertEqual(self.request(fixture, '/api/flash-capabilities',
                query={'key': 'offline-board'}, token=agent_token)[0], 401)

    def test_event_retention_has_cursor_gap_and_does_not_restart_executor(self):
        with protected_flashing() as fixture:
            plan = self.plan(fixture, self.image(fixture))
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            flashes = FlashStore(fixture.manager.store)
            for index in range(MAX_EVENTS + 5):
                flashes.append_event(task['id'], {'type': 'progress', 'phase': 'fixture', 'done': index, 'total': 1000})
            code, packet = self.request(fixture, '/api/flash-events',
                query={'key': 'offline-board', 'id': task['id'], 'after': 0})
            self.assertEqual(code, 200, packet)
            self.assertTrue(packet.get('gap') or packet.get('truncated_before'))
            self.assertGreater(packet['events'][0].get('seq', packet['events'][0].get('cursor')), 1)
            self.assertEqual(len(fixture.executor.executions), 1)

    def test_unknown_process_cleanup_keeps_recovery_hold_and_prevents_new_task(self):
        with protected_flashing() as fixture:
            fixture.executor.failure = FlashRunnerError('cleanup cannot be confirmed', cleanup_confirmed=False)
            plan = self.plan(fixture, self.image(fixture))
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'failed')
            self.assertTrue(task['flash']['recovery_required'])
            holds = maintenance_for(fixture.manager).holds('FLASH-A')
            self.assertEqual(holds[0]['state'], 'recovery')
            code, blocked = self.start(fixture, plan, request_id='different-request-001')
            self.assertEqual(code, 423, blocked)
            self.assertEqual(len(fixture.executor.executions), 1)

    def test_loader_preparation_is_explicit_and_occurs_once_with_same_execution_identity(self):
        with protected_flashing() as fixture:
            fixture.executor.requires_prepare = True
            fixture.executor.workspace = str(Path(fixture.manager.artifacts.root).parent / 'prepared-cache')
            plan = self.plan(fixture, self.image(fixture))
            self.assertTrue(plan['requires_prepare'])
            self.assertEqual(fixture.executor.preparations, [], 'preview must not load a device')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(fixture.executor.preparations[0], fixture.executor.executions[0][1])
            self.assertEqual(len(fixture.executor.plans), 1, 'loader preparation must not import the image again')
            self.assertEqual(len(fixture.executor.checks), 1)
            checked_plan = fixture.executor.checks[0][0]
            self.assertEqual(checked_plan['artifact']['id'], fixture.executor.plans[0][0]['id'])
            self.assertEqual(checked_plan['writes'][0]['path'], fixture.executor.plans[0][0]['path'])
            final = FlashStore(fixture.manager.store).get_plan(task['flash']['plan_id'])
            original = FlashStore(fixture.manager.store).get_plan(plan['id'])
            self.assertEqual(final['workspace'], original['workspace'])

    def test_prepared_task_waits_for_final_partition_selection_and_rejects_changed_layout(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            plan = self.plan(fixture, image, 'partitions')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task_id, deadline = started['task']['id'], time.monotonic() + 3
            while time.monotonic() < deadline:
                task = fixture.manager.tasks.store.get(task_id)
                if task['progress'].get('phase') == 'awaiting-selection':
                    break
                time.sleep(.02)
            self.assertEqual(task['progress'].get('phase'), 'awaiting-selection', task)
            self.assertEqual(fixture.executor.executions, [])
            selection = {'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                         'layout_digest': 'wrong-layout', 'preview': True,
                         'selections': [{'storage': 'nvme', 'partition': 'rootfs', 'artifact_id': image['id']}]}
            code, rejected = self.request(fixture, '/api/flash-selection', selection)
            self.assertEqual(code, 409, rejected)
            selection['layout_digest'] = '1' * 64
            code, preview = self.request(fixture, '/api/flash-selection', selection)
            self.assertEqual(code, 200, preview)
            self.assertEqual(fixture.executor.executions, [], 'selection preview must remain read-only')
            commit = {'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                      'layout_digest': '1' * 64, 'plan_id': preview['plan']['id'],
                      'request_id': 'selection-fixture-001'}
            code, selected = self.request(fixture, '/api/flash-selection', commit)
            self.assertEqual(code, 200, selected)
            task = self.terminal(fixture, task_id)
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual([(item['storage'], item['partition'])
                             for item in fixture.executor.executions[0][0]['writes']], [('nvme', 'rootfs')])
            code, repeated = self.request(fixture, '/api/flash-selection', commit)
            self.assertEqual(code, 200, repeated)
            self.assertEqual(repeated['task']['id'], task_id)
            self.assertEqual(len(fixture.executor.executions), 1)

    def test_uart_replacement_does_not_cancel_usb_write_but_new_input_is_refused(self):
        with protected_flashing() as fixture:
            fixture.executor.block = True
            plan = self.plan(fixture, self.image(fixture))
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            self.assertTrue(fixture.executor.started.wait(2))
            board = fixture.manager.boards['FLASH-A']
            attachment = Device('fixture-new-uart', 'FAKE-UART-DO-NOT-OPEN', fixture.manager, board=board)
            attachment.present, attachment.fd = True, 98765
            board.attach(attachment)
            fixture.manager.devices[attachment.by_path] = attachment
            try:
                with mock.patch.object(attachment, '_write') as write:
                    code, denied = self.request(fixture, '/api/session-send', {
                        'key': 'offline-board', 'serial': 'FLASH-A', 'sid': 'serial', 'data': 'must-not-send\r'})
                    self.assertEqual(code, 423, denied)
                    write.assert_not_called()
                attachment.connection_id = 'new-generation-during-flash'
                fixture.executor.release.set()
                task = self.terminal(fixture, started['task']['id'])
                self.assertEqual(task['state'], 'completed', task)
                self.assertEqual(len(fixture.executor.executions), 1)
            finally:
                attachment.fd, attachment.present = None, False

    def test_cancel_waiting_selection_stops_without_writing_and_releases_confirmed_resources(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            plan = self.plan(fixture, image, 'partitions')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task_id, deadline = started['task']['id'], time.monotonic() + 3
            while time.monotonic() < deadline:
                task = fixture.manager.tasks.store.get(task_id)
                if task['progress'].get('phase') == 'awaiting-selection':
                    break
                time.sleep(.02)
            self.assertEqual(task['progress'].get('phase'), 'awaiting-selection', task)
            code, requested = self.request(fixture, '/api/task-cancel', {'id': task_id})
            self.assertEqual(code, 200, requested)
            task = self.terminal(fixture, task_id)
            self.assertEqual(task['state'], 'cancelled', task)
            self.assertTrue(task['cancel_requested'])
            self.assertEqual(task['flash']['write_result'], 'not-started')
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'ready')

    def test_required_verification_failure_keeps_confirmed_write_outcome(self):
        with protected_flashing() as fixture:
            outcome = {'write_result': 'completed', 'verify_result': 'failed',
                       'boot_result': 'not-requested', 'written_bytes': 22}
            fixture.executor.failure = FlashRunnerError('simulated readback mismatch', outcome=outcome)
            plan = self.plan(fixture, self.image(fixture), verify='required')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'failed', task)
            self.assertEqual(task['flash']['write_result'], 'completed')
            self.assertEqual(task['flash']['verify_result'], 'failed')
            self.assertEqual(task['flash']['written_bytes'], 22)
            self.assertEqual(task['flash']['boot_result'], 'not-requested')
            self.assertFalse(task['flash']['recovery_required'])
            self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
            self.assertEqual(len(fixture.executor.executions), 1)

    def test_full_disk_target_storage_survives_preview_and_fixed_plan_execution(self):
        with protected_flashing() as fixture:
            plan = self.plan(fixture, self.image(fixture), target_storage='nvme')
            self.assertEqual(fixture.executor.plans[0][-1], 'nvme')
            self.assertEqual([item['storage'] for item in plan['writes']], ['nvme'])
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual([item['storage'] for item in fixture.executor.executions[0][0]['writes']], ['nvme'])

    def test_preview_uncertain_cleanup_has_private_inspection_recovery_and_explicit_release(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            failure = FlashRunnerError('simulated inspection cleanup uncertainty', 503, False)
            failure.workspace = str(Path(fixture.manager.artifacts.root).parent / 'private-cache' / 'plan-a')
            fixture.executor.plan_failure = failure
            code, rejected = self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'})
            self.assertEqual(code, 503, rejected)
            self.assertTrue(rejected['recovery_required'])
            self.assertNotIn(failure.workspace, json.dumps(rejected))
            task_id = rejected['task_id']
            code, packet = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(code, 200, packet)
            task = packet['task']
            self.assertEqual((task['kind'], task['state']), ('flash-inspect', 'failed'))
            self.assertEqual(task['targets'], [])
            self.assertNotIn(failure.workspace, json.dumps(task))
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'claimed')
            code, deletion = self.request(fixture, '/api/artifact-delete', {
                'id': image['id'], 'serial': 'FLASH-A'})
            self.assertEqual(code, 409, deletion)
            attempt_id = task['flash']['attempt_id']
            body = {'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                    'attempt_id': attempt_id, 'reason': 'Original fixture helper is confirmed stopped',
                    'confirm_stopped': True}
            for mutation in ({'attempt_id': 'stale-attempt'}, {'confirm_stopped': False}, {'reason': ''}):
                code, denied = self.request(fixture, '/api/flash-recovery', dict(body, **mutation))
                self.assertIn(code, (400, 409), denied)
                self.assertTrue(maintenance_for(fixture.manager).holds('FLASH-A'))
            agent_token, _ = fixture.grants.issue('all')
            code, denied = self.request(fixture, '/api/flash-recovery', body, token=agent_token)
            self.assertEqual(code, 403, denied)
            code, released = self.request(fixture, '/api/flash-recovery', body)
            self.assertEqual(code, 200, released)
            self.assertEqual(released['task']['state'], 'failed')
            self.assertEqual(released['task']['flash']['write_result'], 'not-started')
            self.assertFalse(released['task']['flash']['recovery_required'])
            self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'ready')
            self.assertEqual(fixture.executor.executions, [])

    def test_selection_preview_uncertain_cleanup_wakes_original_worker_and_keeps_hold(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            plan = self.plan(fixture, image, 'partitions')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task_id, deadline = started['task']['id'], time.monotonic() + 3
            while time.monotonic() < deadline:
                task = fixture.manager.tasks.store.get(task_id)
                if task['progress'].get('phase') == 'awaiting-selection':
                    break
                time.sleep(.02)
            self.assertEqual(task['progress'].get('phase'), 'awaiting-selection', task)
            failure = FlashRunnerError('simulated selection cleanup uncertainty', 503, False)
            failure.workspace = str(Path(fixture.manager.artifacts.root).parent / 'private-selection')
            fixture.executor.plan_failure = failure
            code, rejected = self.request(fixture, '/api/flash-selection', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                'layout_digest': '1' * 64, 'preview': True,
                'selections': [{'storage': 'nvme', 'partition': 'rootfs', 'artifact_id': image['id']}]})
            self.assertEqual(code, 503, rejected)
            task = self.terminal(fixture, task_id)
            self.assertEqual(task['state'], 'failed', task)
            self.assertTrue(task['flash']['recovery_required'])
            self.assertEqual(task['flash']['write_result'], 'not-started')
            self.assertNotIn(failure.workspace, json.dumps(task))
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'claimed')
            self.assertTrue(all(item['state'] == 'recovery'
                                for item in maintenance_for(fixture.manager).holds('FLASH-A')))

    def test_expired_cache_cleanup_preserves_active_and_recovery_workspaces_and_receipts(self):
        with protected_flashing() as fixture:
            root = Path(fixture.manager.artifacts.root).parent / 'flashing'
            cache = root / 'cache'
            cache.mkdir(parents=True)
            workspaces = {name: cache / ('plan-' + suffix) for name, suffix in (
                ('active', 'aaaaaaaa'), ('recovery', 'bbbbbbbb'), ('unused', 'cccccccc'),
                ('fresh', 'dddddddd'))}
            for path in workspaces.values():
                path.mkdir()
                (path / 'payload.img').write_bytes(b'cache fixture')
            fixture.executor.cleanup_plan = LocalFlashExecutor(root).cleanup_plan
            fixture.executor.workspace, fixture.executor.block = str(workspaces['active']), True
            plan = self.plan(fixture, self.image(fixture))
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            self.assertTrue(fixture.executor.started.wait(2))
            flashes = FlashStore(fixture.manager.store)
            recovery_plan = flashes.put_plan({'actor': 'web', 'serial': 'FLASH-B',
                'workspace': str(workspaces['recovery']), 'created': time.time() - 7200})
            coordinator = maintenance_for(fixture.manager)
            with coordinator.admission('FLASH-B', ['flash:local:fixture-FLASH-B']) as resources:
                recovery_task = fixture.manager.tasks.store.create_board('flash', 'web', '', 'FLASH-B',
                    'recovery-cache-request', 'c' * 64,
                    {'plan_id': recovery_plan['plan_id'], 'workspace': str(workspaces['recovery']),
                     'attempt_id': 'recovery-cache-attempt', 'recovery_required': True}, resources)
            fixture.manager.tasks.store.update(recovery_task, state='failed', error='fixture cleanup uncertain')
            coordinator.keep_recovery(recovery_task)
            unused = flashes.put_plan({'actor': 'web', 'serial': 'FLASH-B',
                'workspace': str(workspaces['unused']), 'created': time.time() - 7200})
            # Old image plans can share the same workspace with later checked
            # plans. Any live reference must protect all of those cache paths.
            aliases = [flashes.put_plan({'actor': 'web', 'serial': 'FLASH-B',
                'workspace': str(workspaces[name]), 'created': time.time() - 7200})
                for name in ('active', 'recovery', 'fresh')]
            with fixture.manager.store.lock, fixture.manager.store.db:
                fixture.manager.store.db.execute('UPDATE flash_plans SET created=?', (time.time() - 7200,))
            fresh = flashes.put_plan({'actor': 'web', 'serial': 'FLASH-B',
                'workspace': str(workspaces['fresh']), 'created': time.time()})
            FlashActions(fixture.manager).expire()
            self.assertTrue(workspaces['active'].exists())
            self.assertTrue(workspaces['recovery'].exists())
            self.assertTrue(workspaces['fresh'].exists(), 'an expired alias must not delete an unexpired plan workspace')
            self.assertFalse(workspaces['unused'].exists())
            self.assertEqual(flashes.get_plan(unused['plan_id'])['digest'], unused['digest'])
            self.assertEqual(fixture.manager.tasks.store.get(recovery_task)['state'], 'failed')
            fixture.executor.release.set()
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            receipt_digest = task['result']['plan_digest']
            FlashActions(fixture.manager).expire()
            self.assertFalse(workspaces['active'].exists())
            self.assertTrue(workspaces['recovery'].exists())
            self.assertTrue(workspaces['fresh'].exists())
            self.assertEqual(fixture.manager.tasks.store.get(task['id'])['result']['plan_digest'], receipt_digest)
            with fixture.manager.store.lock, fixture.manager.store.db:
                fixture.manager.store.db.execute('UPDATE flash_plans SET created=? WHERE id=?',
                    (time.time() - 7200, fresh['plan_id']))
            FlashActions(fixture.manager).expire()
            self.assertFalse(workspaces['fresh'].exists())
            for receipt in aliases + [fresh]:
                self.assertEqual(flashes.get_plan(receipt['plan_id'])['digest'], receipt['digest'])

    def test_image_preparation_reclaims_only_idle_derived_cache_and_retries_original_upload(self):
        from decklab_core.image_workspaces import origin_path

        with protected_flashing() as fixture:
            root = Path(fixture.manager.artifacts.root).parent / 'flashing'
            executor = LocalFlashExecutor(root)
            fixture.manager.flash_executor = executor
            store = fixture.manager.artifacts
            artifact = store.create('upload', 'system.tar.gz', 4)
            store.append(artifact['id'], 0, b'pack')
            artifact = store.seal(artifact['id'])
            paths = {name: root / 'images' / (suffix * 64) for name, suffix in (
                ('old', 'a'), ('unused', 'b'), ('fresh-plan', 'c'), ('import-source', 'd'))}
            for index, (name, path) in enumerate(paths.items()):
                path.mkdir(parents=True)
                (path / 'payload').write_bytes(b'x' * {'old':6, 'unused':1, 'fresh-plan':2, 'import-source':2}[name])
                modified = time.time() - 300 + index
                os.utime(path, (modified, modified))
            workdir = root / 'workdirs'
            workdir.mkdir()
            (workdir / 'edited').write_bytes(b'x')
            FlashStore(fixture.manager.store).put_plan({'actor':'web', 'serial':'FLASH-B',
                'source_workspaces':[str(paths['fresh-plan'])], 'created':time.time()})
            directory = store.create('upload', 'import.directory', 1)
            store.append(directory['id'], 0, b'x')
            store.seal(directory['id'])
            origin_path(store._path(directory['id'], '.bin')).write_text(
                json.dumps({'directory':str(paths['import-source'])}))
            prepare = executor.prepare_image

            def run(binding, operation, request, **kwargs):
                self.assertEqual(operation, 'prepare-image')
                self.assertEqual(request['artifact']['id'], artifact['id'])
                self.assertGreaterEqual(request['max_cache_bytes'], 7)
                return {'artifact_sha256':artifact['sha256'], 'root_path':request['workspace'],
                        'files':[{'name':'fastboot.yaml', 'size':1}]}

            with mock.patch('decklab_core.flash_discovery.executor_command', return_value=('fixture',)), \
                    mock.patch.object(executor, '_run', side_effect=run) as executed, \
                    mock.patch.object(executor, 'prepare_image', side_effect=lambda *args: prepare(
                        *args, max_cache_bytes=14, max_expanded_bytes=3)) as attempted:
                code, started = self.request(fixture, '/api/image-prepare', {'id':artifact['id']})
                self.assertEqual(code, 202, started)
                result = self.terminal(fixture, started['task']['id'])
                self.assertEqual(result['state'], 'completed', result)
                self.assertEqual(attempted.call_count, 2)
                executed.assert_called_once()
            self.assertFalse(paths['old'].exists())
            self.assertTrue(all(paths[name].exists() for name in ('unused', 'fresh-plan', 'import-source')))
            self.assertTrue((workdir / 'edited').exists())
            with store.open(artifact['id']) as source:
                self.assertEqual(source.read(), b'pack')
            self.assertEqual(store.get(directory['id'])['state'], 'ready')

    def test_package_partition_selection_rechecks_cached_candidates_without_reimporting(self):
        with protected_flashing() as fixture:
            fixture.executor.image_type = 'package'
            fixture.executor.workspace = str(Path(fixture.manager.artifacts.root).parent / 'package-cache')
            image = self.image(fixture)
            plan = self.plan(fixture, image, 'partitions')
            code, started = self.start(fixture, plan)
            self.assertEqual(code, 202, started)
            task_id, deadline = started['task']['id'], time.monotonic() + 3
            while time.monotonic() < deadline:
                task = fixture.manager.tasks.store.get(task_id)
                if task['progress'].get('phase') == 'awaiting-selection':
                    break
                time.sleep(.02)
            self.assertEqual(task['progress'].get('phase'), 'awaiting-selection', task)
            body = {'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                    'layout_digest': '1' * 64, 'preview': True,
                    'selections': [{'storage': 'absent-disk', 'partition': 'rootfs', 'artifact_id': image['id']}]}
            code, rejected = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 502, rejected)
            self.assertEqual(fixture.executor.executions, [])
            body['selections'][0]['storage'] = 'nvme'
            code, preview = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, preview)
            self.assertEqual(len(fixture.executor.plans), 1, 'selection must not decompress or import the package again')
            self.assertEqual(len(fixture.executor.checks), 2)
            checked_plan, candidates, selected = fixture.executor.checks[-1]
            self.assertEqual(checked_plan['artifact']['id'], image['id'])
            self.assertEqual([(entry['storage'], entry['partition']) for entry in candidates],
                             [('emmc', 'rootfs'), ('nvme', 'rootfs')])
            self.assertEqual(selected, [{'storage': 'nvme', 'partition': 'rootfs'}])
            self.assertEqual([(entry['storage'], entry['partition']) for entry in preview['plan']['writes']],
                             [('nvme', 'rootfs')])
            self.assertFalse(preview['plan']['write_layout'])
            original = FlashStore(fixture.manager.store).get_plan(plan['id'])
            final = FlashStore(fixture.manager.store).get_plan(preview['plan']['id'])
            self.assertEqual(final['workspace'], original['workspace'])
            code, accepted = self.request(fixture, '/api/flash-selection', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'id': task_id,
                'layout_digest': '1' * 64, 'plan_id': preview['plan']['id'], 'request_id': 'package-selection-001'})
            self.assertEqual(code, 200, accepted)
            task = self.terminal(fixture, task_id)
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(len(fixture.executor.plans), 1)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual([(entry['storage'], entry['partition'])
                             for entry in fixture.executor.executions[0][0]['writes']], [('nvme', 'rootfs')])

    def test_async_preview_reports_progress_deduplicates_and_does_not_hold_uart_or_write(self):
        with protected_flashing() as fixture, fake_attachment(fixture) as attachment:
            image = self.image(fixture)
            fixture.executor.block_image = True
            body = {'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'],
                    'mode': 'full', 'request_id': 'async-preview-001'}
            code, result = self.request(fixture, '/api/flash-plan', body)
            self.assertEqual(code, 202, result)
            task_id = result['task']['id']
            self.assertTrue(fixture.executor.image_started.wait(2))
            code, repeated = self.request(fixture, '/api/flash-plan', body)
            self.assertEqual(code, 202, repeated)
            self.assertEqual(repeated['task']['id'], task_id)
            self.assertEqual(len(fixture.executor.plans), 1)
            code, conflict = self.request(fixture, '/api/flash-plan', dict(body, verify='none'))
            self.assertEqual(code, 409, conflict)
            code, current = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(current['task']['progress'], {'done': 8, 'total': 32, 'phase': 'expanding-image'})
            details = current['task']['flash']
            self.assertEqual(details['preview_source']['id'], image['id'])
            self.assertEqual(details['preview_intent']['mode'], 'full')
            self.assertEqual(len(details['preview_progress']['stages']), 6)
            self.assertTrue(details['preview_progress']['active'])
            self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
            fixture.executor.image_release.set()
            completed = self.terminal(fixture, task_id)
            self.assertEqual(completed['state'], 'completed', completed)
            self.assertTrue(completed['result']['plan']['id'])
            self.assertEqual(completed['flash']['preview_progress']['state'], 'completed')
            self.assertFalse(completed['flash']['preview_progress']['active'])
            self.assertNotIn('path', json.dumps(completed['result']))
            self.assertEqual(fixture.executor.executions, [])

    def test_async_preview_can_cancel_import_without_starting_layout_or_writes(self):
        with protected_flashing() as fixture:
            image = self.image(fixture)
            fixture.executor.block_image = True
            code, result = self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'],
                'mode': 'full', 'request_id': 'cancel-preview-001'})
            self.assertEqual(code, 202, result)
            self.assertTrue(fixture.executor.image_started.wait(2))
            task_id = result['task']['id']
            self.request(fixture, '/api/task-cancel', {'id': task_id})
            completed = self.terminal(fixture, task_id)
            self.assertEqual(completed['state'], 'cancelled', completed)
            self.assertFalse(completed['flash']['preview_progress']['active'])
            self.assertFalse(fixture.executor.inspection_started.is_set())
            self.assertEqual(fixture.executor.executions, [])

    def test_image_preparation_allows_uart_input_but_target_inspection_excludes_it(self):
        with protected_flashing() as fixture, fake_attachment(fixture) as attachment:
            image = self.image(fixture)
            fixture.executor.block_image = fixture.executor.block_inspection = True
            result = []
            worker = threading.Thread(target=lambda: result.append(self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'})))
            worker.start()
            try:
                self.assertTrue(fixture.executor.image_started.wait(2))
                self.assertFalse(fixture.executor.inspection_started.is_set())
                self.assertEqual(maintenance_for(fixture.manager).holds('FLASH-A'), [])
                with mock.patch.object(attachment, '_write', return_value=(True, '')) as write:
                    code, sent = self.request(fixture, '/api/session-send', {
                        'key': 'offline-board', 'serial': 'FLASH-A', 'sid': 'serial', 'data': 'fixture-input\r'})
                    self.assertEqual(code, 200, sent)
                    write.assert_called_once_with('fixture-input\r')
                fixture.executor.image_release.set()
                self.assertTrue(fixture.executor.inspection_started.wait(2))
                with mock.patch.object(attachment, '_write') as write:
                    code, denied = self.request(fixture, '/api/session-send', {
                        'key': 'offline-board', 'serial': 'FLASH-A', 'sid': 'serial', 'data': 'must-not-send\r'})
                    self.assertIn(code, (409, 423), denied)
                    write.assert_not_called()
            finally:
                fixture.executor.image_release.set()
                fixture.executor.inspection_release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0][0], 200, result)
            self.assertEqual(fixture.executor.executions, [])

    def test_image_preparation_does_not_interrupt_existing_console_task(self):
        with protected_flashing() as fixture, fake_attachment(fixture) as attachment:
            image = self.image(fixture)
            existing_started, existing_release = threading.Event(), threading.Event()
            def existing(context):
                with transactions_for(attachment).begin('existing-fixture-operation', 8):
                    existing_started.set()
                    while not existing_release.wait(.02):
                        context.check()
                return {'ok': True}
            task = fixture.manager.tasks.start('identify', 'web', '', [
                {'serial': 'FLASH-A', 'connection_id': attachment.connection_id}], existing)
            self.assertTrue(existing_started.wait(2))
            fixture.executor.block_image = True
            result = []
            worker = threading.Thread(target=lambda: result.append(self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'})))
            worker.start()
            try:
                self.assertTrue(fixture.executor.image_started.wait(2), 'host-only preparation must not acquire the board')
                self.assertEqual(fixture.manager.tasks.store.get(task['id'])['state'], 'running')
                fixture.executor.image_release.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result[0][0], 409, result)
                self.assertFalse(fixture.executor.inspection_started.is_set())
                self.assertEqual(fixture.manager.tasks.store.get(task['id'])['state'], 'running')
            finally:
                fixture.executor.image_release.set()
                existing_release.set()
                worker.join(3)
            self.assertEqual(self.terminal(fixture, task['id'])['state'], 'completed')
            self.assertEqual(fixture.executor.executions, [])

    def test_unknown_image_preparation_reserves_cache_only_and_blocks_new_preparation(self):
        with protected_flashing() as fixture, fake_attachment(fixture) as attachment:
            image = self.image(fixture)
            failure = FlashRunnerError('simulated host-only image preparation uncertainty', 503, False)
            failure.workspace = str(Path(fixture.manager.artifacts.root).parent / 'private-image-workspace')
            fixture.executor.image_failure = failure
            code, rejected = self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'})
            self.assertEqual(code, 503, rejected)
            task = self.terminal(fixture, rejected['task_id'])
            self.assertEqual(task['kind'], 'flash-inspect')
            self.assertEqual(task['flash']['phase'], 'preparation-recovery')
            coordinator = maintenance_for(fixture.manager)
            self.assertEqual(coordinator.holds('FLASH-A'), [], 'host-only uncertainty must not reserve UART or USB')
            self.assertEqual([row['resource'] for row in coordinator.holds()], ['flash-cache-pool:local'])
            with self.assertRaises(TaskError) as error:
                coordinator.check_preparation()
            self.assertEqual(error.exception.status, 423)
            with mock.patch.object(attachment, '_write', return_value=(True, '')) as write:
                code, sent = self.request(fixture, '/api/session-send', {
                    'key': 'offline-board', 'serial': 'FLASH-A', 'sid': 'serial', 'data': 'fixture-input\r'})
                self.assertEqual(code, 200, sent)
                write.assert_called_once()
            code, blocked = self.request(fixture, '/api/flash-plan', {
                'key': 'offline-board', 'serial': 'FLASH-A', 'artifact_id': image['id'], 'mode': 'full'})
            self.assertEqual(code, 423, blocked)
            self.assertEqual(len(fixture.executor.plans), 1)
            self.assertFalse(fixture.executor.inspection_started.is_set())
            self.assertEqual(fixture.executor.executions, [])
            self.assertNotIn(failure.workspace, json.dumps(task))


if __name__ == '__main__':
    unittest.main()
