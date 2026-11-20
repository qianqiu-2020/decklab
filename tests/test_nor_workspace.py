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
            profile['ram_loader_protocol'] = 'spacemit-ram-v1'
        self.candidates[0].update(profile=profile, preparable=bootrom,
            mode='bootrom' if bootrom else 'fastboot', inspection=None if bootrom else self.inspect(None))
        self.discoveries, self.workspaces = [], []

    def discover(self, argv, probe=False, device=None, check=lambda: None, link_token=None):
        check()
        self.discoveries.append((probe, device))
        return {'devices': copy.deepcopy(self.candidates)}

    def nor_workspace(self, binding, check=lambda: None):
        cache = self.local._directory(self.local.root / 'cache')
        workspace = Path(tempfile.mkdtemp(prefix='plan-', dir=cache))
        self.workspace = str(workspace)
        profile = copy.deepcopy(binding.profile_data)
        if profile.get('bootrom_identity'):
            profile['ram_loader_protocol'] = 'spacemit-ram-v1'
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

    def test_os_catalogue_and_all_targets_reuse_read_task_without_nor_selections(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, _ = self.begin(fixture, target)
            ready = self.phase(fixture, task['id'], 'awaiting-selection')
            artifact = self.upload(fixture, ready, target, b'fixture OS package')
            original_plan = executor.plan
            imports, executed = [], []
            os_inspection = {'layout_digest': '2' * 64, 'storages': [{'id': 'active',
                'physical_device': 'scsi:0', 'size': 1 << 30, 'partitions': [
                    {'name': 'ESP', 'offset': 1 << 20, 'size': 256 << 20},
                    {'name': 'rootfs', 'offset': 257 << 20, 'size': 512 << 20}]}]}
            def plan(binding, image, mode, selections=None, verify='if-supported', check=lambda: None, **kwargs):
                result = original_plan(binding, image, mode, selections, verify, check, **kwargs)
                imports.append(image['id'])
                profile = copy.deepcopy(binding.profile_data)
                profile['system_storage'] = 'scsi:0'
                profile['storages'][0]['package_tables'] = ['partition_universal.json']
                profile['storages'][0]['sparse'] = True
                result.update(execution_profile=profile, inspection=os_inspection)
                result['plan'].update(image_type='package', ready=False, writes=[], selections=[], write_layout=False)
                result['payload_candidates'] = [dict(storage='active', partition=name, kind='partition',
                    size=image['size'], sha256=image['sha256'], path=image['path']) for name in ('ESP', 'rootfs')]
                return result
            executor.plan = plan
            original_check = executor.check_plan
            def checked(binding, *args, **kwargs):
                self.assertEqual(binding.profile_data['system_storage'], 'scsi:0')
                result = original_check(binding, *args, **kwargs)
                result['inspection'] = os_inspection
                result['plan']['layout_digest'] = '2' * 64
                return result
            executor.check_plan = checked
            def execute(binding, plan, on_event, check, **identity):
                self.assertEqual(binding.profile_data['system_storage'], 'scsi:0')
                self.assertIsNone(identity['nor_baseline'])
                self.assertFalse(plan['write_layout'])
                executed.append(plan)
                return {'ok': True, 'write_result': 'completed', 'verify_result': 'verified', 'boot_result': 'not-requested'}
            executor.execute = execute
            body = dict(self.scope(target), id=ready['id'], preview=True,
                layout_digest=ready['flash']['inspection']['layout_digest'], artifact_id=artifact,
                mode='partitions', selections=[], verify='if-supported')
            code, catalog = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, catalog)
            self.assertEqual(catalog['inspection']['storages'][0]['physical_device'], 'scsi:0')
            self.assertEqual(executed, [])
            body.update(plan_id=catalog['plan']['id'], selections=[dict(storage='active', partition=name, artifact_id=artifact) for name in ('ESP', 'rootfs')])
            code, reviewed = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, reviewed)
            self.assertEqual(len(imports), 1, 'reuse imported package, never unpack it again for selection')
            self.assertEqual(executed, [])
            code, submitted = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                id=ready['id'], layout_digest=body['layout_digest'], plan_id=reviewed['plan']['id'], request_id='confirm-os-package-001'))
            self.assertEqual(code, 200, submitted)
            final = self.terminal(fixture, ready['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(len(executed), 1)
            self.assertEqual({w['partition'] for w in executed[0]['writes']}, {'ESP', 'rootfs'})
            self.assertEqual(len(executor.snapshots), 1)
            self.assertEqual(executor.data, b'\xff' * (8 << 20))

    def test_read_workspace_can_confirm_manual_firmware_and_os_in_same_task(self):
        from test_flash_bundle import BundleHTTPTests
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, _ = self.begin(fixture, target)
            waiting = self.phase(fixture, task['id'], 'awaiting-selection')
            os_id = self.upload(fixture, waiting, target, b'os', 'os.zip')
            fw_id = self.upload(fixture, waiting, target, b'firmware', 'Firmware.zip')
            fixture.executor = executor
            BundleHTTPTests().setup(fixture)
            calls = []

            def execute(binding, plan, on_event, check, **options):
                image_id = plan['artifact']['id']
                self.assertEqual(bool(options.get('nor_baseline')), image_id == fw_id)
                for claimed_id in (fw_id, os_id):
                    self.assertEqual(fixture.manager.artifacts.get(claimed_id)['state'], 'claimed')
                calls.append(image_id)
                return {'ok': True, 'write_result': 'completed', 'verify_result': 'verified'}

            executor.execute = execute
            scope = dict(self.scope(target), id=task['id'], layout_digest=waiting['flash']['inspection']['layout_digest'])
            params = dict(scope, preview=True, mode='full', artifact_id=os_id, firmware_artifact_id=fw_id)
            code, preview = self.request(fixture, '/api/flash-selection', params)
            self.assertEqual(code, 200, preview)
            self.assertEqual([part['label'] for part in preview['plan']['components']], ['Firmware', 'OS'])
            code, stale = self.request(fixture, '/api/flash-selection',
                dict(params, firmware_artifact_id=None, plan_id=preview['plan']['id']))
            self.assertEqual(code, 409, stale)
            self.assertEqual(calls, [])
            code, submitted = self.request(fixture, '/api/flash-selection',
                dict(scope, plan_id=preview['plan']['id'], request_id='manual-companion-confirm-001'))
            self.assertEqual(code, 200, submitted)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(calls, [fw_id, os_id])

    def test_unified_read_keeps_nor_backup_and_system_metadata_in_one_task(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root, bootrom=True)
            executor.supports_storage_metadata = True
            calls = []
            def inventory(binding, check, on_event):
                check()
                self.assertEqual(len(executor.snapshots), 1, 'NOR must be backed up before metadata reads')
                on_event({'event': 'progress', 'phase': 'reading-system-metadata', 'done': 65536, 'total': 0})
                calls.append(binding)
                return {'storages': [{'id': 'nvme:0', 'size': 64 << 30, 'partitions': [
                    {'name': 'rootfs', 'size': 32 << 30, 'offset': 1 << 20,
                     'content': {'version': 'Bianbu 2.3.5', 'version_state': 'identified'}}]}],
                    'default_storage': 'nvme:0', 'priority_source': 'eeprom:SSD', 'full_backup': False}
            executor.storage_inventory = inventory
            body = dict(self.scope(target), kind='nor-backup', binding_revision=1,
                        request_id='unified-read-001', read_system=True)
            code, result = self.request(fixture, '/api/task-start', body)
            self.assertEqual(code, 202, result)
            ready = self.phase(fixture, result['task']['id'], 'awaiting-selection')
            self.assertEqual(ready['flash']['nor_before']['artifact']['size'], 8 << 20)
            self.assertEqual(ready['flash']['storage_inventory']['default_storage'], 'nvme:0')
            self.assertFalse(ready['flash']['storage_inventory']['full_backup'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(executor.preparations), 1)
            self.assertEqual(executor.executions, [])
            code, again = self.request(fixture, '/api/task-start', body)
            self.assertEqual(code, 202, again)
            self.assertEqual(again['task']['id'], ready['id'])
            self.assertEqual(len(executor.workspaces), 1)
            code, changed = self.request(fixture, '/api/task-start', dict(body, read_system=False))
            self.assertEqual(code, 409, changed)
            self.request(fixture, '/api/task-cancel', {'id': ready['id']})
            self.terminal(fixture, ready['id'])

    def test_system_disconnect_keeps_completed_nor_backup_without_retry_or_write(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            executor.supports_storage_metadata = True
            calls = []
            def inventory(*args):
                calls.append(True)
                raise TaskError('system storage USB disconnected')
            executor.storage_inventory = inventory
            body = dict(self.scope(target), kind='nor-backup', binding_revision=1,
                        request_id='unified-disconnected-001', read_system=True)
            code, result = self.request(fixture, '/api/task-start', body)
            self.assertEqual(code, 202, result)
            final = self.terminal(fixture, result['task']['id'])
            self.assertEqual(final['state'], 'failed')
            self.assertEqual(final['flash']['nor_before']['artifact']['state'], 'available')
            self.assertEqual(len(calls), 1)
            self.assertEqual(executor.executions, [])

    def test_unified_read_requires_explicit_executor_capability(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            code, result = self.request(fixture, '/api/task-start', dict(self.scope(target),
                kind='nor-backup', binding_revision=1, request_id='read-unsupported', read_system=True))
            self.assertEqual(code, 409, result)
            self.assertEqual(executor.workspaces, [])

    def test_ram_without_physical_eeprom_proof_keeps_temporary_identity(self):
        for identity in ({}, {'serialno': 'ENV-ONLY-SERIAL'}):
            with self.subTest(identity=identity), usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
                executor, target = self.setup_workspace(fixture, root, bootrom=True)
                prepare = executor.prepare
                def prepared(*args, **kwargs):
                    result = prepare(*args, **kwargs)
                    result['final_identity'] = identity
                    return result
                executor.prepare = prepared
                task, _ = self.begin(fixture, target)
                ready = self.phase(fixture, task['id'], 'awaiting-selection')
                self.assertNotEqual(ready['flash'].get('serial_source'), 'eeprom')
                self.assertFalse(fixture.manager.flash_discovery.get(target['id'])['serial'])
                self.assertEqual(executor.executions, [])

    def test_eeprom_identity_after_ram_prepare_reuses_offline_board_and_extends_lease(self):
        from seriald_core.board_maintenance import maintenance_for
        serial = 'BOARD-SAMPLE-01'
        with usb_fixture.automatic_flashing(known=[(serial, 'cloud-board')]) as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root, bootrom=True)
            prepare = executor.prepare
            def prepared(*args, **kwargs):
                result = prepare(*args, **kwargs)
                result['final_identity'] = {'serialno': serial, 'eeprom-serial': serial}
                return result
            executor.prepare = prepared
            task, _ = self.begin(fixture, target)
            ready = self.phase(fixture, task['id'], 'awaiting-selection')
            self.assertEqual(ready['flash']['resolved_serial'], serial)
            self.assertEqual(ready['board_targets'], [serial])
            self.assertEqual(fixture.store.get(serial)['label'], 'cloud-board')
            self.assertEqual(maintenance_for(fixture.manager).holds(serial)[0]['task_id'], task['id'])
            self.assertEqual(fixture.manager.flash_discovery.get(target['id'])['serial'], serial)
            self.assertEqual(executor.executions, [])
            # Original anonymous source scope stays usable after identity resolves.
            artifact = self.upload(fixture, task, target)
            code, result = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                id=task['id'], layout_digest='1' * 64, preview=True,
                selections=[{'storage': 'active', 'partition': 'opensbi', 'artifact_id': artifact}]))
            self.assertEqual(code, 200, result)
            self.request(fixture, '/api/task-cancel', {'id': task['id']})
            self.terminal(fixture, task['id'])
            self.assertEqual(maintenance_for(fixture.manager).holds(serial), [])

    def test_bootrom_paired_console_is_bound_when_ram_loader_reads_eeprom(self):
        from seriald_core import device as device_module, devices
        from seriald_core.console_link import LINK_PREFIX
        serial = 'BOARD-LINKED-01'
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root, \
                mock.patch.object(devices, 'LOG_ROOT', str(Path(root) / 'logs')), \
                mock.patch.object(device_module, 'LOG_ROOT', str(Path(root) / 'logs')):
            console = devices.Device('usb-COM9', 'COM9', fixture.manager)
            console.present, console.fd = True, 98765
            fixture.manager.devices[console.by_path] = console
            self.addCleanup(console.close_log)
            executor = WorkspaceExecutor(root, bootrom=True)
            discover = executor.discover
            def echoing(argv, probe=False, device=None, check=lambda: None, link_token=None):
                result = discover(argv, probe, device, check)
                if probe and link_token:
                    console.append(('fastboot_handle_command: ' + LINK_PREFIX + link_token + '\r\n').encode())
                    for candidate in result['devices']:
                        candidate['link_sent'] = True
                return result
            executor.discover = echoing
            fixture.manager.flash_executor = executor
            result = self.probe(fixture, self.discover(fixture)[0])
            target = result['target']
            self.assertEqual(target['console_link']['state'], 'paired')
            self.assertEqual(console.serial, '')
            prepare = executor.prepare
            def prepared(*args, **kwargs):
                result = prepare(*args, **kwargs)
                result['final_identity'] = {'serialno': serial, 'eeprom-serial': serial}
                return result
            executor.prepare = prepared
            task, _ = self.begin(fixture, target)
            self.phase(fixture, task['id'], 'awaiting-selection')
            self.assertEqual(console.serial, serial)
            self.assertEqual(fixture.store.get(serial)['last_path'], console.by_path)
            link = fixture.manager.flash_discovery.get(target['id'])['document']['uart_link']
            self.assertEqual((link['state'], link['serial']), ('linked', serial))
            self.request(fixture, '/api/task-cancel', {'id': task['id']})
            self.terminal(fixture, task['id'])

    def test_eeprom_conflict_after_prepare_stops_before_backup_or_flash(self):
        with usb_fixture.automatic_flashing(known=[('BOARD-SAMPLE-01', 'cloud-board')]) as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root, bootrom=True)
            fixture.store.mark_conflict('BOARD-SAMPLE-01')
            prepare = executor.prepare
            def prepared(*args, **kwargs):
                result = prepare(*args, **kwargs)
                result['final_identity'] = {'serialno': 'BOARD-SAMPLE-01', 'eeprom-serial': 'BOARD-SAMPLE-01'}
                return result
            executor.prepare = prepared
            task, _ = self.begin(fixture, target)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'failed')
            self.assertIn('conflicting EEPROM', final['error'])
            self.assertEqual(executor.snapshots, [])
            self.assertEqual(executor.executions, [])

    def test_eeprom_probe_associates_existing_board_only_when_field_matches(self):
        with usb_fixture.automatic_flashing(known=[('BOARD-SAMPLE-01', 'cloud-board')]) as fixture:
            candidate = usb_fixture.usb_candidate(serial='BOARD-SAMPLE-01', strong=True)
            candidate['serial_source'] = 'eeprom'
            candidate['identity']['eeprom-serial'] = candidate['serial']
            fixture.executor.candidates[0] = candidate
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            self.assertEqual(target['serial'], 'BOARD-SAMPLE-01')
            self.assertEqual(target['label'], 'cloud-board')
            candidate['identity']['serialno'] = 'DIFFERENT-SERIAL'
            target = self.probe(fixture, target)['target']
            self.assertEqual(target['serial'], '')

    def priority_workspace(self, fixture, root, broken=False):
        executor, target = self.setup_workspace(fixture, root)
        executor.supports_storage_metadata = True
        executor.storage_inventory = lambda *args: {'storages': [], 'default_storage': '',
            'priority_source': 'eeprom:UFS', 'boot_priority_editable': True}
        calls = []
        def save(binding, expected, value, check):
            check()
            calls.append((expected, value))
            if broken:
                raise TaskError('USB disconnected after EEPROM write; outcome unknown', 502)
            return {'ok': True, 'priority_source': 'eeprom:' + value, 'verify_result': 'verified'}
        executor.set_boot_priority = save
        code, result = self.request(fixture, '/api/task-start', dict(self.scope(target), kind='nor-backup',
            binding_revision=1, request_id='priority-read-001', read_system=True))
        self.assertEqual(code, 202, result)
        ready = self.phase(fixture, result['task']['id'], 'awaiting-selection')
        return executor, target, ready, calls

    def test_boot_priority_is_separate_verified_and_repeated_requests_never_rewrite(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target, ready, calls = self.priority_workspace(fixture, root)
            body = dict(self.scope(target), action='boot-priority', id=ready['id'],
                        expected='UFS', value='SSD', request_id='priority-save-001')
            code, result = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['change']['verify_result'], 'verified')
            self.assertEqual(result['task']['progress']['phase'], 'awaiting-selection')
            code, _ = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200)
            code, _ = self.request(fixture, '/api/flash-selection', dict(body, value='USB'))
            self.assertEqual(code, 409)
            second = dict(body, request_id='priority-save-002', expected='SSD', value='MMC')
            code, result = self.request(fixture, '/api/flash-selection', second)
            self.assertEqual(code, 200, result)
            code, _ = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200)
            self.assertEqual(calls, [('UFS', 'SSD'), ('SSD', 'MMC')])
            self.assertEqual(executor.executions, [])
            self.request(fixture, '/api/task-cancel', {'id': ready['id']})
            self.terminal(fixture, ready['id'])

    def test_verified_priority_edit_reassociates_unique_system_disk_without_changing_write_scope(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target, ready, calls = self.priority_workspace(fixture, root)
            inventory = dict(ready['flash']['storage_inventory'], storages=[
                {'id': 'nvme:0'}, {'id': 'scsi:0'}])
            from seriald_core.flash_store import FlashStore
            FlashStore(fixture.store).update_details(ready['id'], {'storage_inventory': inventory})
            body = dict(self.scope(target), action='boot-priority', id=ready['id'],
                        expected='UFS', value='SSD', request_id='priority-associate-001')
            code, result = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, result)
            current = result['task']['flash']['storage_inventory']
            self.assertEqual(current['default_storage'], 'nvme:0')
            self.assertEqual(current['default_candidate'], 'nvme:0')
            body.update(expected='SSD', value='UFS', request_id='priority-associate-002')
            code, result = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['task']['flash']['storage_inventory']['default_storage'], 'scsi:0')
            self.assertEqual(result['task']['flash'].get('selections'), ready['flash'].get('selections'))
            self.assertEqual(executor.executions, [])
            self.request(fixture, '/api/task-cancel', {'id': ready['id']})
            self.terminal(fixture, ready['id'])

    def test_concurrent_duplicate_priority_requests_share_one_write(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target, ready, calls = self.priority_workspace(fixture, root)
            original = executor.set_boot_priority
            entered, release, second_read = threading.Event(), threading.Event(), threading.Event()
            def save(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(*args)
            executor.set_boot_priority = save
            body = dict(self.scope(target), action='boot-priority', id=ready['id'],
                        expected='UFS', value='SSD', request_id='priority-concurrent-001')
            from seriald_core.flash_store import FlashStore
            details = FlashStore.details
            def observed(store, task_id):
                value = details(store, task_id)
                if task_id == ready['id'] and entered.is_set() and not release.is_set():
                    second_read.set()
                return value
            with mock.patch.object(FlashStore, 'details', observed), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.request, fixture, '/api/flash-selection', body)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.request, fixture, '/api/flash-selection', body)
                read = second_read.wait(5)
                release.set()
                self.assertTrue(read, 'second request must read the running receipt before the first write returns')
                for result in (first.result(), second.result()):
                    self.assertEqual(result[0], 200, result)
            self.assertEqual(calls, [('UFS', 'SSD')])
            self.request(fixture, '/api/task-cancel', {'id': ready['id']})
            self.terminal(fixture, ready['id'])

    def test_boot_priority_disconnect_keeps_unknown_receipt_and_does_not_retry(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target, ready, calls = self.priority_workspace(fixture, root, broken=True)
            body = dict(self.scope(target), action='boot-priority', id=ready['id'],
                        expected='UFS', value='SSD', request_id='priority-lost-001')
            code, _ = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 502)
            final = self.terminal(fixture, ready['id'])
            self.assertEqual(final['flash']['boot_priority_change']['state'], 'unknown')
            code, result = self.request(fixture, '/api/flash-selection', body)
            self.assertEqual(code, 200, result)
            self.assertEqual(result['change']['state'], 'unknown')
            self.assertEqual(len(calls), 1)
            self.assertEqual(executor.executions, [])

    def setup_workspace(self, fixture, root, bootrom=False):
        executor = WorkspaceExecutor(root, bootrom)
        fixture.manager.flash_executor = executor
        target = self.discover(fixture)[0]
        result = self.probe(fixture, target)
        self.assertTrue(result['capabilities']['nor_workspace_supported'])
        return executor, result['target']

    def begin(self, fixture, target, request_id='read-nor-workspace-001', **options):
        body = dict(self.scope(target), kind='nor-backup', binding_revision=1, request_id=request_id, **options)
        code, result = self.request(fixture, '/api/task-start', body)
        self.assertEqual(code, 202, result)
        return result['task'], body

    def upload(self, fixture, task, target, data=b'new opensbi fixture', name='opensbi.itb'):
        body = dict(self.scope(target), task_id=task['id'], name=name, size=len(data))
        code, result = self.request(fixture, '/api/artifact-create', body)
        self.assertEqual(code, 200, result)
        artifact_id = result['artifact']['id']
        import base64
        for route, values in [('/api/artifact-append', {'offset': 0, 'data_b64': base64.b64encode(data).decode()}),
                               ('/api/artifact-seal', {})]:
            code, result = self.request(fixture, route, dict(values, id=artifact_id))
            self.assertEqual(code, 200, result)
        return artifact_id

    def test_preview_compares_uploaded_file_with_existing_backup_without_reading_or_writing_again(self):
        from seriald_core.flash_store import FlashStore
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            task, _ = self.begin(fixture, target)
            ready = self.phase(fixture, task['id'], 'awaiting-selection')
            original = executor.plan
            def metadata(*args, **kwargs):
                result = original(*args, **kwargs)
                image = args[1]
                result['payload_metadata'] = [{'storage': 'active', 'partition': 'opensbi',
                    'sha256': image['sha256'], 'content': {'size': image['size'], 'sha256': image['sha256']}}]
                return result
            executor.plan = metadata
            for data, same in ((b'\xff' * 32, True), (b'changed payload', False)):
                artifact_id = self.upload(fixture, task, target, data)
                code, result = self.request(fixture, '/api/flash-selection', dict(self.scope(target),
                    id=task['id'], layout_digest='1' * 64, preview=True,
                    selections=[{'storage': 'active', 'partition': 'opensbi', 'artifact_id': artifact_id}]))
                self.assertEqual(code, 200, result)
                comparison = result['plan']['payload_metadata'][0]['board_comparison']
                self.assertEqual(comparison['matches'], same)
                self.assertEqual(comparison['size'], len(data))
                self.assertEqual(comparison['backup_id'], ready['flash']['nor_before']['artifact']['id'])
                saved = FlashStore(fixture.store).get_plan(result['plan']['id'])
                self.assertEqual(saved['payload_metadata'][0]['board_comparison'], comparison)
                self.assertFalse(result['plan']['write_layout'])
            self.assertEqual(len(executor.snapshots), 1)
            self.assertEqual(executor.executions, [])
            self.request(fixture, '/api/task-cancel', {'id': task['id']})

    def test_inventory_snapshot_retention_reuses_file_and_honors_active_download(self):
        for retain in (False, True):
            with self.subTest(retain=retain), usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
                executor, target = self.setup_workspace(fixture, root)
                task, _ = self.begin(fixture, target, temporary_snapshot=True)
                ready = self.phase(fixture, task['id'], 'awaiting-selection')
                artifacts = fixture.manager.artifacts
                file_id = ready['flash']['nor_before']['artifact']['id']
                artifacts.expire()
                for _ in range(2):
                    with artifacts.open(file_id) as stream:
                        self.assertEqual(stream.read(8), executor.data[:8])
                source = self.upload(fixture, task, target)
                scope = dict(self.scope(target), id=task['id'], layout_digest='1' * 64)
                code, result = self.request(fixture, '/api/flash-selection', dict(scope, preview=True,
                    selections=[dict(storage='active', partition='opensbi', artifact_id=source)]))
                self.assertEqual(code, 200, result)
                confirmation = dict(scope, plan_id=result['plan']['id'], request_id='confirm-retention', retain_backup=retain)
                with artifacts.open(file_id):
                    code, result = self.request(fixture, '/api/flash-selection', confirmation)
                    self.assertEqual(code, 200, result)
                    final = self.terminal(fixture, task['id'])
                    self.assertEqual(final['state'], 'completed', final)
                    artifacts.expire()
                    self.assertEqual(artifacts.get(file_id)['state'], 'available')
                artifacts.expire()
                self.assertEqual(artifacts.get(file_id)['state'], 'available' if retain else 'expired')
                self.assertEqual(artifacts.get(final['flash']['nor_after']['artifact']['id'])['state'], 'available')
                self.assertEqual(len(executor.snapshots), 1)
                self.assertEqual(self.request(fixture, '/api/flash-selection', confirmation)[0], 200)
                self.assertEqual(self.request(fixture, '/api/flash-selection', dict(confirmation, retain_backup=not retain))[0], 409)

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
            def download(artifacts, context, url, serial, name=None, sha256=None, actor='web'):
                row = artifacts.create('upload', name or 'opensbi.itb', 4, actor, serial)
                artifacts.append(row['id'], 0, b'test', actor, serial)
                return {'artifact': artifacts.seal(row['id'], actor, serial)}
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

    def test_injected_write_payload_cannot_start_a_read_workspace(self):
        with usb_fixture.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, target = self.setup_workspace(fixture, root)
            body = dict(self.scope(target), kind='nor-backup', binding_revision=1, request_id='nor-denied-001')
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
