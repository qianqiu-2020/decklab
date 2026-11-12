"""Real authenticated HTTP for automatically discovered, simulated USB targets.

Every USB result and write is supplied by an in-process fixture. No serial
collector, USB library, reset helper or real flashing executable is started.
"""
from contextlib import contextmanager
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from seriald_core import control_server, web
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.board_maintenance import maintenance_for
from seriald_core.devices import Manager
from seriald_core.flash_runner import FlashBook, FlashRunnerError, LocalFlashExecutor
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.storage import Store
from test_connection_access import api_request
from test_flash_integration import FakeFlashExecutor


def usb_candidate(port=(1, 4), serial='', strong=False):
    return {
        'device': {'port_path': list(port), 'vendor_id': 0x361c,
                   'product_id': 0x1001, 'usb_address': 7},
        'mode': 'fastboot', 'chip': 'spacemit-k3',
        'identity': {'product': 'SpacemiT K3', 'serialno': serial},
        'serial': serial, 'strong_serial': strong, 'serial_source': 'fastboot',
        'supported': True, 'reason': '', 'modes': ['partitions'],
        'profile': {'id': 'auto-live-layout-v1', 'identity': {'product': 'SpacemiT K3'},
                    'storages': [{'id': 'emmc', 'sector_size': 512,
                                  'size_getvar': 'storage-size:emmc',
                                  'sector_size_getvar': 'logical-sector-size:emmc',
                                  'layout_getvar': 'partition-layout:emmc',
                                  'partitions': [], 'sparse': True}]},
        'inspection': {'mode': 'fastboot', 'layout_digest': '1' * 64,
                       'storages': [{'id': 'emmc', 'size': 1024 * 1024,
                                     'sector_size': 512, 'full_disk': False,
                                     'partitions': [{'name': 'rootfs', 'target': 'rootfs',
                                                     'offset': 4096, 'size': 524288}]}]},
    }


class FakeDiscoveryExecutor(FakeFlashExecutor):
    def __init__(self):
        super().__init__()
        self.candidates = [usb_candidate()]
        self.discoveries = []
        self.probe_failure = None

    def discover(self, argv, probe=False, device=None, check=lambda: None):
        check()
        self.discoveries.append({'probe': probe, 'device': copy.deepcopy(device)})
        if probe and self.probe_failure:
            raise self.probe_failure
        devices = self.candidates
        if probe and device is not None:
            devices = [value for value in devices if value['device']['port_path'] == device['port_path']]
        return {'devices': copy.deepcopy(devices)}


class FakeBootROMExecutor(FakeDiscoveryExecutor):
    """Only imported loader validation is real; all device operations stay fake."""
    def __init__(self, root):
        super().__init__()
        candidate = usb_candidate()
        candidate.update(mode='bootrom', preparable=True, can_prepare_from_image=True,
            inspection=None, reason='upload a supported K3 package to prepare the loader',
            profile={'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3',
                     'identity': {'product': 'SpacemiT K3'},
                     'bootrom_identity': {'version-brom': 'K3-BROM'},
                     'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt',
                                   'size_getvar': '__decklab_gpt_capacity',
                                   'sector_size': 512, 'partitions': [], 'sparse': True}]})
        self.candidates = [candidate]
        self.local = LocalFlashExecutor(root)
        self.prepare_failure = None

    def execution_binding(self, binding, profile, workspace, check=lambda: None):
        return self.local.execution_binding(binding, profile, workspace, check)

    def plan(self, binding, artifact, mode, selections=None, verify='if-supported', check=lambda: None,
             target_storage=None, inspection_scope=None):
        check()
        self.plans.append(copy.deepcopy((artifact, mode, selections, verify, target_storage)))
        if self.preparations:
            writes = [{'storage': value['storage'], 'partition': value['partition'], 'kind': 'partition',
                       'target': value['partition'], 'size': value['artifact']['size'],
                       'sha256': value['artifact']['sha256'], 'path': value['artifact']['path']}
                      for value in selections or []]
            result = self.check_plan(binding, {'schema': 1, 'mode': mode, 'ready': bool(writes),
                'writes': writes, 'write_layout': False, 'image_type': 'package',
                'artifact': copy.deepcopy(artifact), 'verify': verify,
                'profile_digest': hashlib.sha256(json.dumps(binding.profile_data, sort_keys=True).encode()).hexdigest()}, [], check)
            result['workspace'] = tempfile.mkdtemp(prefix='plan-', dir=self.local.root / 'cache')
            return result
        cache = self.local.root / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.mkdtemp(prefix='plan-', dir=cache)
        workspace = Path(self.workspace)
        with zipfile.ZipFile(artifact['path']) as source:
            for name in ('factory/FSBL.bin', 'u-boot.itb', 'rootfs.img'):
                destination = workspace / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read(name))
        profile = copy.deepcopy(binding.profile_data)
        profile['prepare'] = []
        for name in ('factory/FSBL.bin', 'u-boot.itb'):
            path = workspace / name
            profile['prepare'].extend([
                {'kind': 'stage', 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                {'kind': 'continue'}])
        payload = workspace / 'rootfs.img'
        candidate = {'storage': 'active', 'partition': 'rootfs', 'kind': 'partition',
                     'target': 'rootfs', 'size': payload.stat().st_size,
                     'sha256': hashlib.sha256(payload.read_bytes()).hexdigest(), 'path': str(payload)}
        return {'plan': {'schema': 1, 'mode': mode, 'ready': False, 'writes': [],
                         'write_layout': False, 'image_type': 'package', 'artifact': copy.deepcopy(artifact),
                         'layout_digest': '', 'verify': verify},
                'requires_prepare': True, 'pending_checks': ['loader', 'target-layout', 'partition-selection'],
                'inspection': None, 'payload_candidates': [candidate], 'workspace': self.workspace,
                'execution_profile': profile}

    def prepare(self, binding, on_event=None, check=lambda: None, **identity):
        check()
        self.preparations.append(copy.deepcopy(identity))
        if self.prepare_failure:
            raise self.prepare_failure
        if on_event:
            on_event({'event': 'phase', 'phase': 'prepare', 'done': 0, 'total': 0})
        final = copy.deepcopy(self.candidates[0]['device'])
        final['usb_address'] += 1
        self.candidates[0].update(device=final, mode='fastboot', preparable=False)
        return {'ok': True, 'final_device': final, 'final_identity': {'product': 'SpacemiT K3'}}

    def check_plan(self, binding, plan, payload_candidates=None, check=lambda: None,
                   selections=None, previous_device=None):
        check()
        self.checks.append(copy.deepcopy((plan, payload_candidates, selections, previous_device)))
        checked = copy.deepcopy(plan)
        if selections is not None:
            chosen = {(item['storage'], item['partition']) for item in selections}
            candidates = {(item['storage'], item['partition']): item for item in payload_candidates}
            if not chosen.issubset(candidates):
                raise FlashRunnerError('unknown cached partition')
            checked['writes'] = [copy.deepcopy(candidates[key]) for key in sorted(chosen)]
        checked.update(ready=bool(checked['writes']), layout_digest='2' * 64)
        return {'plan': checked, 'requires_prepare': False, 'payload_candidates': payload_candidates,
                'inspection': {'mode': 'fastboot', 'layout_digest': '2' * 64,
                    'storages': [{'id': 'active', 'size': 1024 * 1024, 'sector_size': 512,
                                  'partitions': [{'name': 'rootfs', 'target': 'rootfs',
                                                  'size': 524288, 'offset': 4096}]}]}}

    def execute(self, binding, plan, on_event=None, check=lambda: None, **identity):
        if plan.get('profile_digest') and plan['profile_digest'] != hashlib.sha256(
                json.dumps(binding.profile_data, sort_keys=True).encode()).hexdigest():
            raise FlashRunnerError('fixture execution profile digest changed')
        return super().execute(binding, plan, on_event, check, **identity)


def bootrom_package():
    destination = io.BytesIO()
    with zipfile.ZipFile(destination, 'w') as archive:
        archive.writestr('factory/FSBL.bin', b'fixture FSBL, never loaded into hardware')
        archive.writestr('u-boot.itb', b'fixture U-Boot, never loaded into hardware')
        archive.writestr('rootfs.img', b'fixture rootfs payload')
    return destination.getvalue()


@contextmanager
def automatic_flashing(known=None):
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'registry.sqlite3')
        grants = AgentGrantStore(':memory:')
        for serial, label in known or []:
            store.bind({'serial': serial, 'serial_source': 'device-tree'}, '', '', label)
        manager = Manager(store, artifact_dir=Path(directory) / 'artifacts', hardware=None)
        manager.flash_book = FlashBook()  # Normal discovery uses no board JSON.
        manager.flash_executor = FakeDiscoveryExecutor()
        manager.flash_discovery_argv = (sys.executable,)
        class Control(control_server.ControlHandler):
            auth_token = 'local-auto-fixture-owner'
            grant_store = grants
            trust_loopback = False
            agent_tokens = {}
        with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                BoundedThreadingTCPServer(('127.0.0.1', 0), Control) as control, \
                mock.patch('seriald_core.devices.open_serial', side_effect=AssertionError('unexpected UART open')):
            collector.manager = manager
            Control.collector_socket = collector.server_address
            workers = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (collector, control)]
            for worker in workers:
                worker.start()
            fixture = SimpleNamespace(manager=manager, store=store, grants=grants,
                executor=manager.flash_executor, token=Control.auth_token,
                url='http://127.0.0.1:%d' % control.server_address[1])
            try:
                yield fixture
            finally:
                fixture.executor.release.set()
                manager.close()
                for server in (collector, control):
                    server.shutdown()
                for worker in workers:
                    worker.join(3)
                grants.close()
                store.close()


class FlashDiscoveryHTTPIntegrationTests(unittest.TestCase):
    def test_unavailable_target_keeps_diagnostic_without_granting_a_write_plan(self):
        with automatic_flashing() as fixture:
            fixture.executor.candidates[0].update(mode='unknown', identity={}, supported=False,
                reason='target-unavailable', diagnostic='flush read error: -1\n\x1b' + 'x' * 1024,
                profile=None, inspection=None, modes=[])
            target = self.discover(fixture)[0]
            result = self.probe(fixture, target)
            diagnostic = result['target']['diagnostic']
            self.assertEqual(result['capabilities']['diagnostic'], diagnostic)
            self.assertTrue(diagnostic.startswith('flush read error: -1'))
            self.assertEqual(len(diagnostic), 512)
            self.assertNotIn('\n', diagnostic)
            self.assertNotIn('\x1b', diagnostic)
            self.assertFalse(result['capabilities']['supported'])
            self.assertFalse(result['capabilities']['can_prepare_from_image'])
            self.assertEqual(fixture.store.boards(), [])
            image = self.image(fixture, target)
            code, error = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[], verify='if-supported'))
            self.assertEqual(code, 409, error)
            self.assertFalse(fixture.executor.plans)
            self.assertFalse(fixture.executor.executions)
            self.assertFalse(fixture.executor.preparations)

    def request(self, fixture, route, payload=None, query=None, token=None):
        return api_request(fixture.url, route, token or fixture.token, payload, query)

    def discover(self, fixture):
        code, result = self.request(fixture, '/api/flash-discover')
        self.assertEqual(code, 200, result)
        self.assertTrue(result['available'], result)
        return result['targets']

    @staticmethod
    def scope(target):
        return {'target_id': target['target_id'], 'target_generation': target['target_generation']}

    def probe(self, fixture, target):
        code, result = self.request(fixture, '/api/flash-target', self.scope(target))
        self.assertEqual(code, 200, result)
        return result

    def image(self, fixture, target, data=b'fixture raw partition payload', name='rootfs.img'):
        code, created = self.request(fixture, '/api/artifact-create',
            dict(self.scope(target), name=name, size=len(data)))
        self.assertEqual(code, 200, created)
        artifact = created['artifact']
        for route, fields in (
                ('/api/artifact-append', {'offset': 0, 'data_b64': base64.b64encode(data).decode()}),
                ('/api/artifact-seal', {})):
            code, result = self.request(fixture, route, dict(id=artifact['id'], **fields))
            self.assertEqual(code, 200, result)
            artifact = result['artifact']
        return artifact

    def plan(self, fixture, target, image):
        payload = dict(self.scope(target), artifact_id=image['id'], mode='partitions',
                       selections=[{'storage': 'emmc', 'partition': 'rootfs', 'artifact_id': image['id']}],
                       verify='if-supported', binding_revision=1)
        code, result = self.request(fixture, '/api/flash-plan', payload)
        self.assertEqual(code, 200, result)
        return result['plan']

    def terminal(self, fixture, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(code, 200, result)
            if result['task']['state'] not in ('queued', 'running'):
                return result['task']
            time.sleep(.02)
        self.fail('simulated USB flash task did not finish')

    def phase(self, fixture, task_id, phase):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(fixture, '/api/task', query={'id': task_id})
            self.assertEqual(code, 200, result)
            task = result['task']
            if task['progress'].get('phase') == phase:
                return task
            self.assertIn(task['state'], ('queued', 'running'), task)
            time.sleep(.02)
        self.fail('simulated task did not reach phase ' + phase)

    def test_bootrom_package_prepares_once_then_selects_with_original_task_generation(self):
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = FakeBootROMExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            probed = self.probe(fixture, self.discover(fixture)[0])
            target = probed['target']
            self.assertTrue(probed['capabilities']['can_prepare_from_image'])
            image = self.image(fixture, target, bootrom_package(), 'k3-package.zip')
            code, preview = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[], verify='if-supported', binding_revision=1))
            self.assertEqual(code, 200, preview)
            self.assertTrue(preview['requires_prepare'])
            self.assertEqual(fixture.executor.preparations, [])
            self.assertNotIn('execution_profile', json.dumps(preview))
            self.assertNotIn(fixture.executor.workspace, json.dumps(preview))
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='bootrom-prepare-request-001'))
            self.assertEqual(code, 202, started)
            task_id = started['task']['id']
            waiting = self.phase(fixture, task_id, 'awaiting-selection')
            self.assertEqual(waiting['flash_targets'][0]['generation'], target['target_generation'])
            current = fixture.manager.flash_discovery.get(target['target_id'])
            self.assertNotEqual(current['generation'], target['target_generation'])
            self.assertEqual(current['document']['device']['usb_address'], 8)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(len(fixture.executor.plans), 1, 'loader completion must reuse imported payloads')
            self.assertEqual(len(fixture.executor.executions), 0)
            self.assertNotIn(fixture.executor.workspace, json.dumps(waiting))
            layout = waiting['flash']['inspection']['layout_digest']
            code, selected = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                id=task_id, layout_digest=layout, preview=True,
                selections=[{'storage': 'active', 'partition': 'rootfs', 'artifact_id': image['id']}]))
            self.assertEqual(code, 200, selected)
            self.assertNotIn(fixture.executor.workspace, json.dumps(selected))
            code, resumed = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                id=task_id, layout_digest=layout, plan_id=selected['plan']['id'],
                request_id='bootrom-selection-request-001'))
            self.assertEqual(code, 200, resumed)
            self.assertEqual(resumed['task']['id'], task_id)
            task = self.terminal(fixture, task_id)
            self.assertEqual(task['state'], 'completed', task)
            # The global USB generation changed while preparing. A lost/retried
            # original submission still refers to this completed attempt.
            code, repeated = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='bootrom-prepare-request-001'))
            self.assertEqual(code, 202, repeated)
            self.assertEqual(repeated['task']['id'], task_id)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(len(fixture.executor.plans), 1)
            self.assertEqual(fixture.store.boards(), [])

    def test_bootrom_prepare_cleanup_unknown_does_not_write_and_keeps_image_and_target_holds(self):
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = FakeBootROMExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target, bootrom_package(), 'k3-package.zip')
            code, preview = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[], verify='if-supported', binding_revision=1))
            self.assertEqual(code, 200, preview)
            fixture.executor.prepare_failure = FlashRunnerError('fixture loader cleanup is unknown',
                                                               cleanup_confirmed=False)
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='bootrom-unknown-request-001'))
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'failed', task)
            self.assertEqual(task['flash']['write_result'], 'not-started')
            self.assertTrue(task['flash']['recovery_required'])
            self.assertEqual(len(fixture.executor.executions), 0)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'claimed')
            holds = maintenance_for(fixture.manager).target_holds(target['target_id'])
            self.assertTrue(holds)
            self.assertTrue(all(value['state'] == 'recovery' for value in holds))
            self.assertNotIn(fixture.executor.workspace, json.dumps(task))

    def test_bootrom_waiting_task_can_upload_a_new_payload_after_expected_usb_transition(self):
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = FakeBootROMExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target, bootrom_package(), 'k3-package.zip')
            code, preview = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[], verify='if-supported', binding_revision=1))
            self.assertEqual(code, 200, preview)
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='bootrom-extra-upload-001'))
            self.assertEqual(code, 202, started)
            task_id = started['task']['id']
            waiting = self.phase(fixture, task_id, 'awaiting-selection')
            payload = b'new independent rootfs bytes'
            body = dict(self.scope(target), name='replacement.img', size=len(payload))
            code, stale = self.request(fixture, '/api/artifact-create', body)
            self.assertEqual(code, 409, stale)
            code, created = self.request(fixture, '/api/artifact-create', dict(body, task_id=task_id))
            self.assertEqual(code, 200, created)
            artifact_id = created['artifact']['id']
            for route, fields in (('/api/artifact-append', {'offset': 0,
                    'data_b64': base64.b64encode(payload).decode()}), ('/api/artifact-seal', {})):
                code, response = self.request(fixture, route, dict(id=artifact_id, **fields))
                self.assertEqual(code, 200, response)
            layout = waiting['flash']['inspection']['layout_digest']
            code, selected = self.request(fixture, '/api/flash-selection', dict(self.scope(target), id=task_id,
                layout_digest=layout, preview=True,
                selections=[{'storage': 'active', 'partition': 'rootfs', 'artifact_id': artifact_id}]))
            self.assertEqual(code, 200, selected)
            self.assertEqual(selected['plan']['writes'][0]['sha256'], hashlib.sha256(payload).hexdigest())
            private_plan = fixture.manager.store.db.execute('SELECT document FROM flash_plans WHERE id=?',
                                                            (selected['plan']['id'],)).fetchone()
            private_plan = json.loads(private_plan['document'])
            self.assertIn(fixture.executor.workspace, private_plan['source_workspaces'])
            self.assertNotIn('source_workspaces', json.dumps(selected))
            code, resumed = self.request(fixture, '/api/flash-selection', dict(self.scope(target), id=task_id,
                layout_digest=layout, plan_id=selected['plan']['id'], request_id='bootrom-extra-selection-001'))
            self.assertEqual(code, 200, resumed)
            result = self.terminal(fixture, task_id)
            self.assertEqual(result['state'], 'completed', result)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(len(fixture.executor.executions[0][0]['writes']), 1)

    def test_discovery_before_uart_identification_creates_only_usb_targets_and_reads_on_selection(self):
        with automatic_flashing() as fixture:
            targets = self.discover(fixture)
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]['serial'], '')
            self.assertEqual(fixture.store.boards(), [])
            self.assertFalse(any(call['probe'] for call in fixture.executor.discoveries))
            selected = self.probe(fixture, targets[0])
            self.assertTrue(selected['capabilities']['supported'])
            self.assertEqual(selected['capabilities']['modes'], ['partitions'])
            self.assertEqual(selected['capabilities']['storages'][0]['partitions'][0]['name'], 'rootfs')
            self.assertEqual(fixture.store.boards(), [])
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(fixture.executor.preparations, [])

    def test_unknown_serial_target_upload_preview_and_task_have_no_invented_board_or_uart(self):
        with automatic_flashing() as fixture:
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            artifact = self.image(fixture, target)
            plan = self.plan(fixture, target, artifact)
            self.assertFalse(plan['write_layout'])
            self.assertEqual([(item['storage'], item['partition']) for item in plan['writes']], [('emmc', 'rootfs')])
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target),
                kind='flash', plan_id=plan['id'], binding_revision=1, request_id='auto-fixture-start-001'))
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(task['targets'], [])
            self.assertEqual(task.get('board_targets', []), [])
            self.assertEqual(task['flash_targets'][0]['target_id'], target['target_id'])
            self.assertEqual(fixture.store.boards(), [])
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertNotIn(str(fixture.manager.artifacts.root), str(task))
            code, events = self.request(fixture, '/api/flash-events', query={
                'id': task['id'], 'target_id': target['target_id'], 'after': 0})
            self.assertEqual(code, 200, events)
            self.assertEqual(events['details']['write_result'], 'completed')
            code, issued = self.request(fixture, '/api/agent-share', {'scope': 'all'})
            self.assertEqual(code, 200, issued)
            code, hidden = self.request(fixture, '/api/task', query={'id': task['id']}, token=issued['token'])
            self.assertEqual(code, 404, hidden)

    def test_duplicate_usb_flash_submission_recovers_the_same_task_without_reexecution(self):
        with automatic_flashing() as fixture:
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            plan = self.plan(fixture, target, self.image(fixture, target))
            payload = dict(self.scope(target), kind='flash', plan_id=plan['id'],
                           binding_revision=1, request_id='auto-duplicate-request-001')
            code, first = self.request(fixture, '/api/task-start', payload)
            self.assertEqual(code, 202, first)
            task = self.terminal(fixture, first['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            code, repeated = self.request(fixture, '/api/task-start', payload)
            self.assertEqual(code, 202, repeated)
            self.assertEqual(repeated['task']['id'], task['id'])
            self.assertEqual(len(fixture.executor.executions), 1)
            code, changed = self.request(fixture, '/api/task-start', dict(payload, binding_revision=2))
            self.assertEqual(code, 409, changed)

    def test_usb_target_history_and_events_keep_two_unidentified_devices_separate(self):
        with automatic_flashing() as fixture:
            fixture.executor.candidates.append(usb_candidate((1, 5)))
            targets = [self.probe(fixture, target)['target'] for target in self.discover(fixture)]
            tasks = []
            for index, target in enumerate(targets):
                plan = self.plan(fixture, target, self.image(fixture, target))
                code, started = self.request(fixture, '/api/task-start', dict(self.scope(target),
                    kind='flash', plan_id=plan['id'], binding_revision=1,
                    request_id='auto-separate-request-%03d' % index))
                self.assertEqual(code, 202, started)
                tasks.append(self.terminal(fixture, started['task']['id']))
            self.assertTrue(all(task['state'] == 'completed' for task in tasks), tasks)
            code, listed = self.request(fixture, '/api/tasks', query={'target_id': targets[0]['target_id']})
            self.assertEqual(code, 200, listed)
            self.assertEqual([task['id'] for task in listed['tasks']], [tasks[0]['id']])
            code, wrong = self.request(fixture, '/api/flash-events', query={
                'id': tasks[0]['id'], 'target_id': targets[1]['target_id']})
            self.assertEqual(code, 404, wrong)
            self.assertEqual(fixture.store.boards(), [])

    def test_unknown_target_cleanup_failure_keeps_its_resources_until_explicit_recovery(self):
        with automatic_flashing() as fixture:
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target)
            plan = self.plan(fixture, target, image)
            fixture.executor.failure = FlashRunnerError('simulated process cleanup is unknown',
                                                       cleanup_confirmed=False)
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target),
                kind='flash', plan_id=plan['id'], binding_revision=1,
                request_id='auto-recovery-request-001'))
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'failed', task)
            self.assertTrue(task['flash']['recovery_required'])
            holds = maintenance_for(fixture.manager).target_holds(target['target_id'])
            self.assertTrue(holds)
            self.assertTrue(all(hold['state'] == 'recovery' for hold in holds))
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'claimed')
            code, wrong = self.request(fixture, '/api/flash-recovery', dict(self.scope(target),
                id=task['id'], attempt_id='wrong-attempt', reason='checked original process', confirm_stopped=True))
            self.assertEqual(code, 409, wrong)
            code, recovered = self.request(fixture, '/api/flash-recovery', dict(self.scope(target),
                id=task['id'], attempt_id=task['flash']['attempt_id'],
                reason='fixture confirms original process exited', confirm_stopped=True))
            self.assertEqual(code, 200, recovered)
            self.assertEqual(recovered['task']['state'], 'failed')
            self.assertEqual(maintenance_for(fixture.manager).target_holds(target['target_id']), [])
            self.assertEqual(fixture.manager.artifacts.get(image['id'])['state'], 'ready')

    def test_disconnected_usb_target_cannot_start_a_new_flash_from_an_old_plan(self):
        with automatic_flashing() as fixture:
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            plan = self.plan(fixture, target, self.image(fixture, target))
            fixture.executor.candidates = []
            self.assertEqual(self.discover(fixture), [])
            code, result = self.request(fixture, '/api/task-start', dict(self.scope(target),
                kind='flash', plan_id=plan['id'], binding_revision=1,
                request_id='auto-disconnected-request-001'))
            self.assertEqual(code, 409, result)
            self.assertEqual(fixture.executor.executions, [])

    def test_only_reliable_serial_associates_an_existing_board_and_preserves_its_name(self):
        with automatic_flashing([('KNOWN-SERIAL-42', 'existing-label')]) as fixture:
            fixture.executor.candidates[0] = usb_candidate(serial='KNOWN-SERIAL-42', strong=True)
            selected = self.probe(fixture, self.discover(fixture)[0])
            self.assertEqual(selected['target']['serial'], 'KNOWN-SERIAL-42')
            self.assertEqual(selected['target']['label'], 'existing-label')
            self.assertEqual(len(fixture.store.boards()), 1)
            self.assertEqual(fixture.store.get('KNOWN-SERIAL-42')['label'], 'existing-label')
            self.assertIsNone(fixture.manager.boards['KNOWN-SERIAL-42'].connection)

    def test_unreliable_or_factory_serial_does_not_create_a_board(self):
        for serial, strong in (('NEW-SERIAL-42', False), ('unknown', True), ('00000000', True)):
            with self.subTest(serial=serial), automatic_flashing() as fixture:
                fixture.executor.candidates[0] = usb_candidate(serial=serial, strong=strong)
                selected = self.probe(fixture, self.discover(fixture)[0])
                self.assertEqual(selected['target']['serial'], '')
                self.assertEqual(fixture.store.boards(), [])

    def test_reliable_new_serial_registers_a_board_without_needing_uart_or_fixed_json(self):
        with automatic_flashing() as fixture:
            fixture.executor.candidates[0] = usb_candidate(serial='NEW-SERIAL-42', strong=True)
            selected = self.probe(fixture, self.discover(fixture)[0])
            self.assertEqual(selected['target']['serial'], 'NEW-SERIAL-42')
            self.assertEqual([board['serial'] for board in fixture.store.boards()], ['NEW-SERIAL-42'])
            self.assertIsNone(fixture.manager.boards['NEW-SERIAL-42'].connection)
            self.assertEqual(fixture.manager.flash_book.boards, {})
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(fixture.executor.preparations, [])

    def test_automatic_profile_cannot_supply_host_paths_or_arbitrary_device_commands(self):
        for forbidden, value in (('prepare', [{'kind': 'stage', 'path': '/host/private-loader'}]),
                                 ('argv', ['/bin/sh', '-c', 'arbitrary']),
                                 ('download_device', {'port_path': [99, 1]})):
            with self.subTest(field=forbidden), automatic_flashing() as fixture:
                fixture.executor.candidates[0]['profile'][forbidden] = value
                target = self.discover(fixture)[0]
                code, result = self.request(fixture, '/api/flash-target', self.scope(target))
                self.assertEqual(code, 502, result)
                self.assertEqual(fixture.executor.executions, [])
                self.assertEqual(fixture.executor.preparations, [])
                self.assertEqual(fixture.store.boards(), [])

    def test_changed_usb_generation_rejects_old_selection_without_probing_or_writing(self):
        with automatic_flashing() as fixture:
            target = self.discover(fixture)[0]
            fixture.executor.candidates[0]['device']['usb_address'] += 1
            refreshed = self.discover(fixture)[0]
            self.assertEqual(refreshed['target_id'], target['target_id'])
            self.assertNotEqual(refreshed['target_generation'], target['target_generation'])
            before = len([call for call in fixture.executor.discoveries if call['probe']])
            code, result = self.request(fixture, '/api/flash-target', self.scope(target))
            self.assertEqual(code, 409, result)
            self.assertEqual(len([call for call in fixture.executor.discoveries if call['probe']]), before)
            self.assertEqual(fixture.executor.executions, [])

    def test_discovery_and_automatic_targets_are_not_exposed_to_agent_grants(self):
        with automatic_flashing() as fixture:
            target = self.discover(fixture)[0]
            code, issued = self.request(fixture, '/api/agent-share', {'scope': 'all'})
            self.assertEqual(code, 200, issued)
            token = issued['token']
            for route, payload, query in (
                    ('/api/flash-discover', None, None),
                    ('/api/flash-target', self.scope(target), None),
                    ('/api/flash-capabilities', None, self.scope(target)),
                    ('/api/artifact-create', dict(self.scope(target), name='image.img', size=1), None)):
                with self.subTest(route=route):
                    code, result = self.request(fixture, route, payload, query, token)
                    self.assertEqual(code, 403, result)
            code, result = self.request(fixture, '/api/flash-discover', token='invalid-owner-token')
            self.assertEqual(code, 401, result)

    def test_ambiguous_usb_candidates_or_serial_conflicts_do_not_proceed(self):
        with automatic_flashing() as fixture:
            fixture.executor.candidates.append(usb_candidate())
            code, result = self.request(fixture, '/api/flash-discover')
            self.assertEqual(code, 409, result)
            self.assertFalse(any(call['probe'] for call in fixture.executor.discoveries))
        with automatic_flashing([('KNOWN-SERIAL-42', 'existing-label')]) as fixture:
            fixture.store.mark_conflict('KNOWN-SERIAL-42')
            fixture.executor.candidates[0] = usb_candidate(serial='KNOWN-SERIAL-42', strong=True)
            target = self.discover(fixture)[0]
            code, result = self.request(fixture, '/api/flash-target', self.scope(target))
            self.assertEqual(code, 409, result)
            self.assertEqual(fixture.executor.executions, [])
            self.assertEqual(fixture.store.get('KNOWN-SERIAL-42')['label'], 'existing-label')
