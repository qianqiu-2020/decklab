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

from decklab_core import control_server, web
from decklab_core.agent_grants import AgentGrantStore
from decklab_core.board_maintenance import maintenance_for
from decklab_core.devices import Manager
from decklab_core.flash_runner import FlashBook, FlashRunnerError, LocalFlashExecutor
from decklab_core.httpd import BoundedThreadingTCPServer
from decklab_core.storage import Store
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

    def discover(self, argv, probe=False, device=None, check=lambda: None, link_token=None):
        check()
        self.discoveries.append({'probe': probe, 'device': copy.deepcopy(device)})
        self.link_tokens = getattr(self, 'link_tokens', []) + ([link_token] if link_token else [])
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
             target_storage=None, inspection_scope=None, on_event=None):
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


class FakeNORExecutor(FakeBootROMExecutor):
    """Real HTTP/task coordination, fake USB; no field-board operations."""
    def __init__(self, root):
        super().__init__(root)
        self.candidates[0]['modes'] = ['partitions', 'full']
        self.mutate_review = False

    def plan(self, binding, artifact, mode, *args, **kwargs):
        result = super().plan(binding, artifact, mode, *args, **kwargs)
        self.assert_mode = mode
        result['plan'].update(confirm_after_prepare=True, write_layout=True,
            writes=[{'storage': 'active', 'partition': 'layout', 'kind': 'layout',
                     'target': 'mtd', 'size': 123, 'sha256': 'a' * 64},
                    {'storage': 'active', 'partition': 'fsbl', 'kind': 'partition',
                     'target': 'fsbl', 'size': 456, 'sha256': 'b' * 64}])
        if self.preparations:
            result['plan']['ready'] = True
        profile = result.setdefault('execution_profile', copy.deepcopy(binding.profile_data))
        profile['storages'] = [{'id': 'active', 'label': 'MTD (NOR firmware package)',
            'layout_source': 'spacemit-nor', 'size_getvar': 'mtd-size', 'sector_size': 512,
            'partitions': None, 'package_tables': ['partition_nor.json'], 'layout_target': 'mtd', 'sparse': False}]
        return result

    def check_plan(self, binding, plan, *args, **kwargs):
        result = super().check_plan(binding, plan, *args, **kwargs)
        result['plan']['erase_ranges'] = [{'storage': 'active', 'offset': 0, 'size': 8 * 1024 * 1024}]
        if self.mutate_review:
            result['plan']['writes'][1]['size'] += 1
        return result


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
                mock.patch('decklab_core.devices.open_serial', side_effect=AssertionError('unexpected UART open')):
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
    def test_download_on_another_usb_path_requires_a_unique_echo_from_the_expected_console(self):
        from decklab_core.flash_tasks import _discovery
        from decklab_core.tasks import TaskError

        serial = 'KNOWN-SERIAL-42'
        for echo, multiple, accepted in ((serial, False, True), (None, False, False),
                                         ('OTHER-BOARD-42', False, False), (serial, True, False)):
            with self.subTest(echo=echo, multiple=multiple), automatic_flashing([(serial, 'fixture-board')]) as fixture:
                candidate = usb_candidate(port=(3, 4, 2), serial=serial, strong=True)
                candidate['link_sent'] = True
                fixture.executor.candidates = [candidate]
                if multiple:
                    fixture.executor.candidates.append(usb_candidate(port=(3, 4, 3)))
                links = fixture.manager.console_links = mock.Mock()
                links.start.return_value = 'fixture-echo'
                links.finish.return_value = [SimpleNamespace(serial=echo, connection_id='console', by_path='uart')] if echo else []
                clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 16]), time=time.time)
                discovery = _discovery(fixture.manager)
                with mock.patch('decklab_core.flash_discovery.time', clock):
                    if accepted:
                        found = discovery.wait_for_download(serial, [[2, 3, 2]], set(), mock.Mock(),
                                                            actor='web', client_id='')
                        self.assertEqual(found['serial'], serial)
                    else:
                        with self.assertRaises(TaskError):
                            discovery.wait_for_download(serial, [[2, 3, 2]], set(), mock.Mock(),
                                                        actor='web', client_id='')
                visible, *_ = discovery.refresh()['targets']
                self.assertEqual(visible['serial'], serial if accepted else '')
                if multiple:
                    links.start.assert_not_called()
                self.assertFalse(fixture.executor.executions)

    def test_board_grant_reads_bootrom_eeprom_before_binding_download_target(self):
        from decklab_core.demo import DemoDevice
        from decklab_core.hardware import HardwareBook

        class IdentityExecutor(FakeBootROMExecutor):
            def nor_workspace(self, binding, check):
                return {'workspace': str(self.local.root), 'execution_profile': binding.profile_data}

            def execution_binding(self, binding, profile, workspace, check=None):
                return binding

            def prepare(self, binding, on_event=None, check=lambda: None, **identity):
                holds = maintenance_for(self.manager).holds('KNOWN-SERIAL-42')
                if len(holds) != 1 or holds[0]['task_id'] != identity['task_id']:
                    raise AssertionError('EEPROM reader must hold the requested board until it exits')
                result = super().prepare(binding, on_event, check, **identity)
                final = dict(result['final_device'], port_path=[2, 1])
                ready = usb_candidate(serial=self.eeprom, strong=True)
                ready.update(device=final, serial_source='eeprom')
                ready['identity']['eeprom-serial'] = self.eeprom
                self.candidates[0] = ready
                return dict(result, final_device=final, final_identity=ready['identity'])

        serial = 'KNOWN-SERIAL-42'
        for eeprom in ('ANOTHER-BOARD-42', serial):
            with self.subTest(eeprom=eeprom), automatic_flashing([(serial, 'fixture-board')]) as fixture:
                fixture.manager.flash_executor = fixture.executor = IdentityExecutor(
                    fixture.manager.artifacts.root.parent / 'flashing')
                fixture.executor.manager = fixture.manager
                fixture.executor.eeprom = eeprom
                bootrom = fixture.executor.candidates.pop()
                device = DemoDevice('fixture-uart', fixture.manager, fixture.store.get(serial))
                fixture.manager.boards[serial] = device.board
                fixture.manager.devices[device.by_path] = device
                device.board.attach(device)
                fixture.manager.hardware = HardwareBook({'boards': {serial: {'enter-download': {
                    'argv': [sys.executable, '-c', 'pass'], 'allow_agent': True,
                }}}})
                token, _ = fixture.grants.issue('device', serial)
                code, claimed = self.request(fixture, '/api/input-claim',
                    {'key': device.key, 'serial': serial, 'scope': 'board', 'ttl': 90}, token=token)
                self.assertEqual(code, 200, claimed)

                def control(argv, timeout, check, cleanup_failed, outcome=None):
                    check()
                    outcome('issued')
                    fixture.executor.candidates.append(bootrom)
                    outcome('acknowledged')
                    return {'exit_code': 0}

                data = {'key': device.key, 'serial': serial, 'connection_id': device.connection_id,
                        'kind': 'hardware-enter-download', 'request_id': 'prepare-bootrom-with-lease',
                        'resolve_flash_target': True, 'execution_lease': claimed['execution_lease']}
                with mock.patch('decklab_core.board_cases.adb_binding', return_value={'usb_port': '3-4'}), \
                        mock.patch('decklab_core.flash_discovery.download_ports', return_value=[[3, 4], [1, 4]]), \
                        mock.patch('decklab_core.hardware.run_command', side_effect=control):
                    code, accepted = self.request(fixture, '/api/task-start', data, token=token)
                    self.assertEqual(code, 202, accepted)
                    task = self.terminal(fixture, accepted['task']['id'])
                identity = fixture.manager.tasks.store.get(task['result']['identity_task_id'])
                self.assertEqual(len(fixture.executor.preparations), 1)
                self.assertTrue(fixture.executor.preparations[0]['identity_only'])
                self.assertFalse(fixture.executor.executions)
                self.assertEqual(task['result']['control_result'], 'acknowledged')
                if eeprom != serial:
                    self.assertEqual(task['state'], 'failed', task)
                    self.assertEqual(identity['state'], 'failed', identity)
                    self.assertIsNone(fixture.store.get(eeprom))
                    self.assertEqual(identity['flash']['write_result'], 'not-started')
                    self.assertNotIn('flash_target', task['result'])
                    continue
                self.assertEqual(task['state'], 'completed', task)
                self.assertEqual(identity['result']['write_result'], 'not-started')
                self.assertEqual(task['result']['flash_target']['serial'], serial)
                self.assertEqual(task['result']['flash_target']['target_generation'],
                                 identity['result']['target_generation'])
                download = fixture.manager.flash_discovery.get(task['result']['flash_target']['target_id'])
                self.assertEqual(download['document']['device']['port_path'], [2, 1])
                self.assertFalse(maintenance_for(fixture.manager).holds(serial))

    def test_board_grant_keeps_one_lease_through_image_download_usb_probe_plan_and_flash(self):
        from decklab_core.demo import DemoDevice
        from decklab_core.hardware import HardwareBook
        from test_image_download import DATA, fixture as image_server

        serial = 'KNOWN-SERIAL-42'
        with automatic_flashing([(serial, 'fixture-board')]) as fixture:
            device = DemoDevice('fixture-uart', fixture.manager, fixture.store.get(serial))
            fixture.manager.boards[serial] = device.board
            fixture.manager.devices[device.by_path] = device
            device.board.attach(device)
            fixture.manager.hardware = HardwareBook({'boards': {serial: {'enter-download': {
                'argv': [sys.executable, '-c', 'pass'], 'allow_agent': True,
            }}}})
            candidate = usb_candidate(serial=serial, strong=True)
            candidate['modes'].append('full')
            fixture.executor.candidates = []
            token, _ = fixture.grants.issue('device', serial)
            board = {'key': device.key, 'serial': serial}
            code, claimed = self.request(fixture, '/api/input-claim',
                dict(board, scope='board', ttl=90), token=token)
            self.assertEqual(code, 200, claimed)
            proof = claimed['execution_lease']
            with image_server() as (_, origin):
                code, imported = self.request(fixture, '/api/image-download', dict(board,
                    url=origin + '/image', name='system.img', sha256=hashlib.sha256(DATA).hexdigest(),
                    request_id='managed-image-download'), token=token)
                self.assertEqual(code, 202, imported)
                image = self.terminal(fixture, imported['task']['id'])
                self.assertEqual(image['state'], 'completed', image)
                artifact = image['result']['artifact']

            def control(argv, timeout, check, cleanup_failed, outcome=None):
                check()
                outcome('issued')
                fixture.executor.candidates.append(candidate)
                outcome('acknowledged')
                return {'exit_code': 0, 'duration_ms': 1}

            transition = dict(board, kind='hardware-enter-download', connection_id=device.connection_id,
                              request_id='prepare-leased-download', resolve_flash_target=True, execution_lease=proof)
            with mock.patch('decklab_core.board_cases.adb_binding', return_value={'usb_port': '1-4'}), \
                    mock.patch('decklab_core.hardware.run_command', side_effect=control) as sent:
                code, accepted = self.request(fixture, '/api/task-start', transition, token=token)
                self.assertEqual(code, 202, accepted)
                ready = self.terminal(fixture, accepted['task']['id'])
                self.assertEqual(ready['state'], 'completed', ready)
                code, original_task = self.request(fixture, '/api/task', token=token,
                    query={'request_id': transition['request_id']})
                self.assertEqual(code, 200, original_task)
                self.assertEqual(original_task['task']['id'], ready['id'])
                sent.assert_called_once()

            original = ready['result']['flash_target']
            self.assertEqual(original['serial'], serial)
            self.assertFalse(fixture.executor.executions)
            target = self.scope(original)
            code, _ = self.request(fixture, '/api/flash-target', target, token=token)
            self.assertEqual(code, 423)
            code, found = self.request(fixture, '/api/flash-target',
                dict(target, execution_lease=proof), token=token)
            self.assertEqual(code, 200, found)

            preview = dict(target, artifact_id=artifact['id'], mode='full', binding_revision=1, verify='required',
                           target_storage='emmc', request_id='managed-flash-preview')
            code, _ = self.request(fixture, '/api/flash-plan', preview, token=token)
            self.assertEqual(code, 423)
            preview['execution_lease'] = proof
            code, accepted = self.request(fixture, '/api/flash-plan', preview, token=token)
            self.assertEqual(code, 202, accepted)
            planned = self.terminal(fixture, accepted['task']['id'])
            self.assertEqual(planned['state'], 'completed', planned)
            self.assertEqual(planned['kind'], 'flash-preview')
            code, recovered = self.request(fixture, '/api/task', query={'request_id': preview['request_id']}, token=token)
            self.assertEqual(code, 200, recovered)
            self.assertEqual(recovered['task']['id'], planned['id'])
            self.assertEqual(self.request(fixture, '/api/flash-plan', preview, token=token)[1]['task']['id'], planned['id'])
            code, _ = self.request(fixture, '/api/flash-plan', dict(preview, target_storage='nvme'), token=token)
            self.assertEqual(code, 409)
            self.assertEqual(len(fixture.executor.plans), 1)
            self.assertFalse(fixture.executor.executions)
            plan = planned['result']['plan']
            self.assertEqual([write['storage'] for write in plan['writes']], ['emmc'])
            request = dict(target, kind='flash', plan_id=plan['plan_id'], binding_revision=1,
                           request_id='managed-flash-under-board-lease')
            code, _ = self.request(fixture, '/api/task-start', request, token=token)
            self.assertEqual(code, 423)
            request['execution_lease'] = proof
            code, accepted = self.request(fixture, '/api/task-start', request, token=token)
            self.assertEqual(code, 202, accepted)
            task_id = accepted['task']['id']
            self.assertEqual(self.terminal(fixture, task_id)['state'], 'completed')
            code, repeated = self.request(fixture, '/api/task-start', request, token=token)
            self.assertEqual(code, 202, repeated)
            self.assertEqual(repeated['task']['id'], task_id)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(self.request(fixture, '/api/input-release',
                dict(board, scope='board', execution_lease=proof), token=token)[0], 200)

    def test_board_lookup_reuses_current_probed_usb_target_without_static_configuration(self):
        serial = 'KNOWN-SERIAL-42'
        with automatic_flashing([(serial, 'fixture-board'), ('OTHER-SERIAL-42', 'other-board')]) as fixture:
            candidate = usb_candidate(serial=serial, strong=True)
            fixture.executor.candidates = [candidate]
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            query = {'key': 'fixture-board', 'serial': serial, 'resolve_target': '1'}
            token, _ = fixture.grants.issue('device', serial)
            other_token, _ = fixture.grants.issue('device', 'OTHER-SERIAL-42')
            fixture.executor.discoveries.clear()

            code, result = self.request(fixture, '/api/flash-capabilities', query=query, token=token)
            self.assertEqual(code, 200, result)
            self.assertFalse(result['supported'])
            self.assertEqual(result['automatic_target'], dict(self.scope(target), serial=serial))
            self.assertEqual([call['probe'] for call in fixture.executor.discoveries], [False])
            self.assertFalse(fixture.manager.flash_book.boards)
            self.assertFalse(fixture.executor.executions)
            code, _ = self.request(fixture, '/api/flash-capabilities', query=query, token=other_token)
            self.assertEqual(code, 404)

            fixture.executor.candidates.clear()
            code, result = self.request(fixture, '/api/flash-capabilities', query=query, token=token)
            self.assertEqual(code, 200, result)
            self.assertIsNone(result['automatic_target'])
            fixture.executor.candidates.append(candidate)
            code, result = self.request(fixture, '/api/flash-capabilities', query=query, token=token)
            self.assertEqual(code, 200, result)
            self.assertIsNone(result['automatic_target'])
            new_target = self.probe(fixture, self.discover(fixture)[0])['target']
            self.assertNotEqual(new_target['target_generation'], target['target_generation'])
            code, result = self.request(fixture, '/api/flash-capabilities', query=query, token=token)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['automatic_target'], dict(self.scope(new_target), serial=serial))
            fixture.store.mark_conflict(serial)
            code, result = self.request(fixture, '/api/flash-capabilities', query=query, token=token)
            self.assertEqual(code, 200, result)
            self.assertIsNone(result['automatic_target'])

    def test_loader_relocation_takes_over_endpoint_without_removing_historical_tasks(self):
        from decklab_core.flash_tasks import _discovery
        with automatic_flashing() as fixture:
            executor = fixture.executor
            executor.candidates = [usb_candidate(port=(2, 3, 3))]
            discovery = _discovery(fixture.manager)
            maintenance = maintenance_for(fixture.manager)
            old_target = discovery.refresh()['targets'][0]
            old = discovery.probe(old_target['target_id'], old_target['target_generation'])
            old_binding = discovery.binding(old['target_id'], old['target_generation'])

            def finished(context):
                maintenance.release(context.id, confirmed=True)
                return {'ok': True}

            first = fixture.manager.tasks.start_board('ram-boot', 'web', 'client', '', finished,
                request_id='historical-superspeed', input_digest='1' * 64, details={}, resources=old_binding.resources,
                maintenance=maintenance, target_id=old['target_id'], target_generation=old['target_generation'])
            self.assertEqual(self.terminal(fixture, first['id'])['state'], 'completed')
            executor.candidates = [usb_candidate(port=(3, 4, 3))]
            initial = discovery.refresh()['targets'][0]
            initial = discovery.probe(initial['target_id'], initial['target_generation'])
            binding = discovery.binding(initial['target_id'], initial['target_generation'])
            final = dict(old_binding.device, usb_address=31)

            def relocate(context):
                try:
                    executor.candidates[0]['device'] = final
                    generation = discovery.accept_prepared_target(binding.target_id, binding.target_generation,
                        final, context.id, 'fixture', board_serial='FIXTURE-SUPERSPEED-SERIAL')
                    return {'generation': generation}
                finally:
                    maintenance.release(context.id, confirmed=True)

            second = fixture.manager.tasks.start_board('ram-boot', 'web', 'client', '', relocate,
                request_id='current-superspeed', input_digest='2' * 64, details={'attempt_id': 'fixture'},
                resources=binding.resources, maintenance=maintenance,
                target_id=initial['target_id'], target_generation=initial['target_generation'])
            completed = self.terminal(fixture, second['id'])
            self.assertEqual(completed['state'], 'completed', completed)
            target = discovery.refresh()['targets'][0]
            self.assertEqual(target['target_id'], initial['target_id'])
            self.assertEqual(target['target_generation'], completed['result']['generation'])
            self.assertEqual(target['usb'], final)
            self.assertFalse(discovery.get(old['target_id'], require_present=False)['present'])
            self.assertEqual(fixture.manager.tasks.store.get(first['id'])['flash_targets'][0]['target_id'], old['target_id'])

    def test_first_usb_only_board_can_be_registered_after_a_verified_loader_transition(self):
        from decklab_core.flash_tasks import _discovery
        with automatic_flashing() as fixture:
            discovery = _discovery(fixture.manager)
            target = discovery.refresh()['targets'][0]
            target = discovery.probe(target['target_id'], target['target_generation'])
            binding = discovery.binding(target['target_id'], target['target_generation'])
            final = dict(binding.device, usb_address=binding.device['usb_address'] + 1)

            def run(context):
                fixture.executor.candidates[0]['device'] = final
                generation = discovery.accept_prepared_target(binding.target_id, binding.target_generation,
                                                              final, context.id, 'attempt', 'BOARD123456')
                self.assertIsNone(fixture.store.get('BOARD123456'))
                serial = discovery.accept_eeprom_identity(binding.target_id, generation,
                    {'serialno': 'BOARD123456', 'eeprom-serial': 'BOARD123456'}, context.id, 'attempt')
                return {'resolved_serial': serial}

            task = fixture.manager.tasks.start_board('ram-boot', 'web', 'client', '', run,
                request_id='first-ram-boot', input_digest='1' * 64, details={'attempt_id': 'attempt'},
                resources=binding.resources, maintenance=maintenance_for(fixture.manager),
                target_id=binding.target_id, target_generation=binding.target_generation)
            completed = self.terminal(fixture, task['id'])
            self.assertEqual(completed['state'], 'completed', completed)
            self.assertEqual(completed['result']['resolved_serial'], 'BOARD123456')
            self.assertEqual(fixture.store.get('BOARD123456')['serial'], 'BOARD123456')

    def test_debian_image_pair_with_another_chip_is_rejected_before_native_preview(self):
        with automatic_flashing() as fixture:
            candidate = fixture.executor.candidates[0]
            candidate.update(chip='k1', modes=['full', 'partitions'])
            candidate['profile']['chip_family'] = 'k1'
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            wrong = self.image(fixture, target, name='Debian-13-GNOME-K3-v1.0rc1-202611170510.tar.gz')
            good = self.image(fixture, target, name='Debian-13-Firmware-K1-v1.0rc1-202611172244.tar.gz')
            for first, second in [(wrong, good), (good, wrong)]:
                code, result = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                    artifact_id=first['id'], firmware_artifact_id=second['id'], mode='full'))
                self.assertEqual(code, 409, result)
                self.assertIn(wrong['name'], result['error'])
                self.assertIn('设备为 K1', result['error'])
            self.assertFalse(fixture.executor.plans)
            self.assertFalse(fixture.executor.executions)

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

    def image(self, fixture, target, data=b'fixture raw partition payload', name='rootfs.img', *, token=None):
        code, created = self.request(fixture, '/api/artifact-create',
            dict(self.scope(target), name=name, size=len(data)), token=token)
        self.assertEqual(code, 200, created)
        artifact = created['artifact']
        for route, fields in (
                ('/api/artifact-append', {'offset': 0, 'data_b64': base64.b64encode(data).decode()}),
                ('/api/artifact-seal', {})):
            code, result = self.request(fixture, route, dict(id=artifact['id'], **fields), token=token)
            self.assertEqual(code, 200, result)
            artifact = result['artifact']
        return artifact

    def test_async_preview_preserves_unidentified_usb_scope_in_task_history(self):
        with automatic_flashing() as fixture:
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target)
            code, result = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[{'storage':'emmc','partition':'rootfs','artifact_id':image['id']}], request_id='usb-preview-001'))
            self.assertEqual(code, 202, result)
            task_id = result['task']['id']
            current = self.terminal(fixture, task_id)
            self.assertEqual(current['state'], 'completed', current)
            self.assertEqual(current['flash_targets'], [{'target_id': target['target_id'], 'generation': target['target_generation']}])
            code, history = self.request(fixture, '/api/tasks', query={'target_id': target['target_id']})
            self.assertEqual(code, 200, history)
            self.assertIn(task_id, [row['id'] for row in history['tasks']])
            self.assertEqual(fixture.executor.executions, [])

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

    def test_loader_moved_to_the_usb3_address_keeps_the_target_and_holds_both_addresses(self):
        class MovingExecutor(FakeBootROMExecutor):
            def prepare(self, binding, on_event=None, check=lambda: None, **identity):
                result = super().prepare(binding, on_event, check, **identity)
                final = dict(result['final_device'], port_path=[2, 1])
                final.pop('usb_serial', None)  # SuperSpeed RAM U-Boot has none
                self.candidates[0].update(device=final)
                return dict(result, final_device=final,
                            final_identity={'eeprom-serial': 'TEST-K3-0001', 'serialno': 'TEST-K3-0001'})
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = MovingExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            original = '.'.join(map(str, target['usb']['port_path']))
            image = self.image(fixture, target, bootrom_package(), 'k3-package.zip')
            code, preview = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='partitions', selections=[], verify='if-supported', binding_revision=1))
            self.assertEqual(code, 200, preview)
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='moved-loader-request-001'))
            self.assertEqual(code, 202, started)
            task_id = started['task']['id']
            self.phase(fixture, task_id, 'awaiting-selection')
            current = fixture.manager.flash_discovery.get(target['target_id'])
            self.assertEqual(current['document']['device']['port_path'], [2, 1])
            self.assertTrue(current['present'])
            targets = [t for t in fixture.manager.flash_discovery.refresh()['targets'] if t['present']]
            self.assertEqual([t['target_id'] for t in targets], [target['target_id']])
            holds = {h['resource'] for h in fixture.manager.store.db.execute(
                'SELECT resource FROM flash_resource_holds WHERE task_id=?', (task_id,)).fetchall()}
            self.assertIn('flash-usb:local:2.1', holds)
            self.assertIn('flash-usb:local:' + original, holds)
            code, cancelled = self.request(fixture, '/api/task-cancel', {'id': task_id})
            self.terminal(fixture, task_id)
            self.assertFalse(fixture.manager.store.db.execute(
                'SELECT 1 FROM flash_resource_holds WHERE task_id=?', (task_id,)).fetchall())

    def test_nor_full_recovery_waits_for_new_preview_after_loader_and_never_replays(self):
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = FakeNORExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target, bootrom_package(), 'nor-fixture.zip')
            code, preview = self.request(fixture, '/api/flash-plan', dict(self.scope(target),
                artifact_id=image['id'], mode='full', selections=[], verify='if-supported'))
            self.assertEqual(code, 200, preview)
            code, started = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='flash',
                plan_id=preview['plan']['id'], binding_revision=1, request_id='nor-full-initial-request-001'))
            self.assertEqual(code, 202, started)
            task_id = started['task']['id']
            waiting = self.phase(fixture, task_id, 'awaiting-selection')
            self.assertEqual(waiting['flash']['confirmation_mode'], 'full')
            self.assertEqual(len(waiting['flash']['prepared_plan']['erase_ranges']), 1)
            self.assertEqual(len(fixture.executor.preparations), 1)
            self.assertEqual(fixture.executor.executions, [], 'loader completion must never start NOR writes')
            layout = waiting['flash']['inspection']['layout_digest']
            payload = dict(self.scope(target), id=task_id, layout_digest=layout, preview=True, selections=[])
            code, selected = self.request(fixture, '/api/flash-selection', dict(payload,
                selections=[{'storage': 'active', 'partition': 'fsbl', 'artifact_id': image['id']}]))
            self.assertEqual(code, 400, selected)
            fixture.executor.mutate_review = True
            code, selected = self.request(fixture, '/api/flash-selection', payload)
            self.assertEqual(code, 409, selected)
            fixture.executor.mutate_review = False
            code, selected = self.request(fixture, '/api/flash-selection', dict(payload, verify='none'))
            self.assertEqual(code, 200, selected)
            self.assertEqual(selected['plan']['verify'], 'none')
            self.assertEqual(fixture.executor.executions, [])
            commit = dict(self.scope(target), id=task_id, layout_digest=layout,
                plan_id=selected['plan']['id'], request_id='nor-full-final-confirmation-001')
            code, resumed = self.request(fixture, '/api/flash-selection', commit)
            self.assertEqual(code, 200, resumed)
            self.assertEqual(self.terminal(fixture, task_id)['state'], 'completed')
            code, repeated = self.request(fixture, '/api/flash-selection', commit)
            self.assertEqual(code, 200, repeated)
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(len(fixture.executor.preparations), 1)

    def test_prepare_for_review_stops_before_writing_and_accepts_the_prepared_plan(self):
        with automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = FakeNORExecutor(
                fixture.manager.artifacts.root.parent / 'flashing')
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            image = self.image(fixture, target, bootrom_package(), 'nor-review.zip')
            scope = self.scope(target)
            code, preview = self.request(fixture, '/api/flash-plan',
                dict(scope, artifact_id=image['id'], mode='full', verify='none'))
            self.assertEqual(code, 200, preview)
            start = dict(scope, kind='flash', plan_id=preview['plan']['id'], binding_revision=1,
                         request_id='prepare-only-for-full-review', prepare_for_review=True)
            code, started = self.request(fixture, '/api/task-start', start)
            self.assertEqual(code, 202, started)
            task_id = started['task']['id']
            waiting = self.phase(fixture, task_id, 'awaiting-selection')['flash']
            self.assertTrue(waiting['prepared_plan']['ready'])
            self.assertEqual(fixture.executor.executions, [])
            commit = dict(scope, id=task_id, layout_digest=waiting['inspection']['layout_digest'],
                plan_id=waiting['prepared_plan']['id'], request_id='confirm-prepared-plan-once')
            code, result = self.request(fixture, '/api/flash-selection', commit)
            self.assertEqual(code, 200, result)
            self.assertEqual(self.terminal(fixture, task_id)['state'], 'completed')
            self.assertEqual(len(fixture.executor.executions), 1)
            self.assertEqual(len(fixture.executor.preparations), 1)

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

    def test_all_device_grants_can_discover_and_prepare_automatic_targets(self):
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
                    self.assertEqual(code, 200, result)
            self.assertEqual(fixture.executor.executions, [])
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
