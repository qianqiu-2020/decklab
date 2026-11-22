"""MCP -> authenticated control -> collector tasks, using simulated USB only."""
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.request import Request, urlopen

import seriald_mcp
from serialctl import ClientError
from seriald_mcp import Adapter, FlashTarget, Settings
from seriald_core.board_maintenance import maintenance_for
from seriald_core.flash_runner import FlashRunnerError
from seriald_core.tasks import TaskError
import test_flash_discovery_integration as usb
import test_image_download as downloads
import test_nor_workspace as nor
from test_flash_stream import StreamExecutor


class MCPFlashTests(unittest.TestCase):
    request = usb.FlashDiscoveryHTTPIntegrationTests.request
    discover = usb.FlashDiscoveryHTTPIntegrationTests.discover
    probe = usb.FlashDiscoveryHTTPIntegrationTests.probe
    scope = staticmethod(usb.FlashDiscoveryHTTPIntegrationTests.scope)
    phase = usb.FlashDiscoveryHTTPIntegrationTests.phase
    terminal = usb.FlashDiscoveryHTTPIntegrationTests.terminal
    setup_workspace = nor.NORWorkspaceTests.setup_workspace

    def adapter(self, fixture, scope='all', serial=''):
        token, _ = fixture.grants.issue(scope, serial=serial)
        return Adapter(Settings(fixture.url, token)), token

    def upload(self, adapter, target, task_id=''):
        data = b'simulated partition replacement'
        row = adapter.create_flash_artifact(target, 'opensbi.itb', len(data), task_id)['artifact']
        adapter.append_artifact(row['id'], 0, base64.b64encode(data).decode())
        self.assertEqual(adapter.artifact(row['id'])['artifact']['received'], len(data))
        sealed = adapter.seal_artifact(row['id'])['artifact']
        self.assertEqual(sealed['sha256'], hashlib.sha256(data).hexdigest())
        return sealed['id']

    @unittest.skipUnless(seriald_mcp.MCPServer, 'optional MCP SDK not installed')
    def test_k1_k3_common_mcp_workflows(self):
        from mcp import Client, StdioServerParameters
        from seriald_core.ramboot_tasks import K1_MUSE_PI_PRO_MODEL, K3_PICO_MODEL

        async def exercise(fixture, chip, flow, token):
            params = StdioServerParameters(command=sys.executable, args=[str(Path(seriald_mcp.__file__))],
                env=dict(os.environ, SERIALD_URL=fixture.url, SERIALD_TOKEN=token))
            async with Client(params) as client:
                async def call(tool_name, **arguments):
                    result = await client.call_tool(tool_name, arguments)
                    self.assertFalse(result.is_error, str(result))
                    return result.structured_content

                listed = (await call('list_flash_targets'))['targets']
                found, = listed
                target = self.scope((await call('probe_flash_target', target=self.scope(found)))['target'])
                caps = await call('flash_capabilities', target=target)
                request = dict(target=target, request_id='mcp-regression-' + chip + '-' + flow)
                if flow != 'stream':
                    data = b'isolated test fixture'
                    artifact = (await call('create_flash_artifact', target=target,
                        name='test.tar.gz', size=len(data)))['artifact']['id']
                    await call('append_artifact', artifact_id=artifact, offset=0,
                               data_b64=base64.b64encode(data).decode())
                    await call('seal_artifact', artifact_id=artifact)
                if flow == 'normal':
                    storage = 'emmc' if chip == 'k1' else 'nvme:0'
                    plan = (await call('preview_flash', target=target, artifact_id=artifact, mode='full',
                        target_storage=storage, verify='required', binding_revision=caps['binding_revision']))['plan']
                    self.assertTrue(plan['write_layout'])
                    self.assertEqual({write['storage'] for write in plan['writes']}, {storage})
                    self.assertEqual(fixture.executor.executions, [])
                    tool = 'start_flash'
                    request.update(plan_id=plan['id'], binding_revision=caps['binding_revision'])
                elif flow == 'stream':
                    self.assertTrue(caps['stream_flash_supported'])
                    tool = 'stream_flash'
                    request.update(url=f'https://example.org/{chip}.tar.gz', sha256='a' * 64, reset_after=False,
                                   system_storage='active' if chip == 'k1' else 'nvme:0')
                else:
                    self.assertTrue(caps['ram_boot_supported'])
                    tool = 'boot_ram'
                    request.update(artifact_id=artifact, binding_revision=caps['binding_revision'])
                task_id = (await call(tool, **request))['task']['id']
                self.assertEqual((await call(tool, **request))['task']['id'], task_id)
                for _ in range(200):
                    task = (await call('get_task', task_id=task_id))['task']
                    if task['state'] not in ('queued', 'running'):
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(task['state'], 'completed', task)
                if flow == 'normal':
                    self.assertEqual(len(fixture.executor.executions), 1)
                    self.assertEqual(task['flash']['verify_result'], 'verified')
                elif flow == 'stream':
                    self.assertEqual(len(fixture.executor.streams), 1)
                    self.assertEqual(fixture.executor.streams[0]['system_storage'], request['system_storage'])
                    self.assertEqual(fixture.executor.streams[0]['archive_sha256'], request['sha256'])
                    self.assertEqual(task['flash']['verify_result'], 'package-digests')
                else:
                    self.assertEqual(task['result']['handoff'], 'acknowledged')
                    self.assertEqual(task['result']['write_result'], 'not-started')
                    self.assertEqual(fixture.executor.executions, [])
                    self.assertEqual(fixture.executor.ram_calls, ['identify', 'transfer'])

        for chip, model in (('k1', K1_MUSE_PI_PRO_MODEL), ('k3', K3_PICO_MODEL)):
            serial = 'FIXTURE-' + chip.upper()
            for flow in ('normal', 'stream', 'ram'):
                with self.subTest(chip=chip, flow=flow), usb.automatic_flashing([(serial, chip)]) as fixture, \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    if flow == 'stream':
                        fixture.manager.flash_executor = fixture.executor = StreamExecutor(directory)
                    executor = fixture.executor
                    candidate = executor.candidates[0]
                    candidate.update(chip=chip, serial=serial, serial_source='eeprom', strong_serial=True,
                                     modes=['full', 'partitions'])
                    candidate['identity'].update({'eeprom-serial': serial, 'serialno': serial})
                    candidate['profile']['chip_family'] = chip
                    if chip == 'k1':
                        candidate['profile'].pop('ram_loader_protocol', None)
                    if flow == 'ram':
                        candidate.update(mode='bootrom', preparable=True, inspection=None)
                        candidate.update(serial='', strong_serial=False, identity={'version-brom': '1.0'})
                        candidate['profile'].update(id='auto-spacemit-gpt-v1', bootrom_identity={'version-brom': '1.0'},
                            storages=[{'id': 'active', 'layout_source': 'spacemit-gpt', 'size_getvar': '__decklab_gpt_capacity',
                                       'sector_size': 512, 'sparse': True, 'partitions': []}])
                        executor.ram_boot_workspace = lambda *args: {'workspace': str(root),
                            'execution_profile': candidate['profile']}
                        executor.image_guard = lambda *_: nullcontext()
                        executor.execution_binding = lambda binding, *args: binding
                        final = dict(candidate['device'], usb_address=8)
                        def prepare(binding, on_event, check, **kwargs):
                            check()
                            identity = {'serialno': serial, 'eeprom-serial': serial}
                            candidate.update(device=final, mode='fastboot', serial=serial, strong_serial=True,
                                             identity=identity)
                            return {'final_device': final, 'final_identity': identity}
                        executor.prepare = prepare
                        executor.ram_calls = []
                        def ram_boot(binding, sn, workspace=None, files=None, *args, **kwargs):
                            self.assertEqual(sn, serial)
                            executor.ram_calls.append('identify' if files is None else 'transfer')
                            return {'serial': sn, 'model': model, 'ram_bytes': 16 << 30, 'handoff': 'acknowledged'}
                        executor.ram_boot = ram_boot
                        executor.cleanup_plan = lambda _: None
                    _, token = self.adapter(fixture)
                    with mock.patch('seriald_core.ramboot_tasks.availability', return_value=''), \
                         mock.patch('seriald_core.ramboot_tasks._prepare', return_value={'files': {'Image': {}}, 'test': True}):
                        asyncio.run(exercise(fixture, chip, flow, token))

    @unittest.skipUnless(seriald_mcp.MCPServer, 'optional MCP SDK not installed')
    def test_official_stdio_tools_full_and_partition_flashing_use_real_authorization(self):
        from mcp import Client, StdioServerParameters
        with usb.automatic_flashing() as fixture:
            fixture.executor.candidates[0]['modes'] = ['full', 'partitions']
            _, token = self.adapter(fixture)

            async def check():
                params = StdioServerParameters(command=sys.executable,
                    args=[str(Path(seriald_mcp.__file__))],
                    env=dict(os.environ, SERIALD_URL=fixture.url, SERIALD_TOKEN=token))
                async with Client(params) as client:
                    tools = (await client.list_tools()).tools
                    names = {tool.name for tool in tools}
                    self.assertTrue({'list_flash_targets', 'probe_flash_target', 'read_device_storage',
                        'preview_flash', 'start_flash', 'stream_flash', 'preview_flash_selection', 'confirm_flash_selection',
                        'set_boot_priority', 'get_flash_events', 'create_flash_artifact', 'append_artifact',
                        'seal_artifact', 'get_artifact', 'delete_artifact', 'list_images',
                        'list_downloaded_images', 'download_image'}.issubset(names))

                    async def call(tool_name, **arguments):
                        result = await client.call_tool(tool_name, arguments)
                        self.assertFalse(result.is_error, str(result))
                        return result.structured_content

                    found = (await call('list_flash_targets'))['targets'][0]
                    target = self.scope(found)
                    found = await call('probe_flash_target', target=target)
                    target = self.scope(found['target'])
                    caps = await call('flash_capabilities', target=target)
                    self.assertEqual(caps['binding_revision'], 1)
                    data = b'fixture image, never sent to hardware'
                    artifact = (await call('create_flash_artifact', target=target,
                        name='fixture.img', size=len(data)))['artifact']['id']
                    await call('append_artifact', artifact_id=artifact, offset=0,
                        data_b64=base64.b64encode(data).decode())
                    await call('seal_artifact', artifact_id=artifact)
                    for mode in ('partitions', 'full'):
                        selection = ([{'storage': 'emmc', 'partition': 'rootfs'}]
                                     if mode == 'partitions' else [])
                        before = len(fixture.executor.executions)
                        plan = (await call('preview_flash', target=target, artifact_id=artifact,
                            mode=mode, binding_revision=1, selections=selection, verify='if-supported'))['plan']
                        self.assertEqual(len(fixture.executor.executions), before)
                        self.assertEqual(plan['write_layout'], mode == 'full')
                        other, _ = self.adapter(fixture)
                        with self.assertRaises(ClientError):
                            other.start_flash(FlashTarget(**target), plan['id'], 1, 'cross-grant-write-001')
                        args = dict(target=target, plan_id=plan['id'], binding_revision=1,
                                    request_id='mcp-' + mode + '-001')
                        task = (await call('start_flash', **args))['task']
                        self.assertEqual((await call('start_flash', **args))['task']['id'], task['id'])
                        for _ in range(200):
                            task = (await call('get_task', task_id=task['id']))['task']
                            if task['state'] not in ('queued', 'running'):
                                break
                            await asyncio.sleep(.01)
                        self.assertEqual(task['state'], 'completed', task)
                        self.assertEqual(len(fixture.executor.executions), before + 1)
                        events = await call('get_flash_events', target=target, task_id=task['id'])
                        self.assertIn('next_after', events)
                    await call('delete_artifact', artifact_id=artifact)
                    self.assertEqual((await call('get_artifact', artifact_id=artifact))['artifact']['state'], 'deleted')
            asyncio.run(check())

    def test_storage_backup_eeprom_and_partition_selection_share_owned_task(self):
        with usb.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, found = self.setup_workspace(fixture, root, bootrom=True)
            executor.supports_storage_metadata = True
            executor.storage_inventory = lambda *args: {'storages': [{'id': 'nvme:0',
                'partitions': [{'name': 'rootfs', 'content': {'version': 'fixture-v1'}}]}],
                'default_storage': 'nvme:0', 'full_backup': False,
                'priority_source': 'eeprom:UFS', 'boot_priority_editable': True}
            changes = []
            def save(binding, expected, value, check):
                check()
                changes.append((expected, value))
                return {'ok': True, 'priority_source': 'eeprom:' + value, 'verify_result': 'verified'}
            executor.set_boot_priority = save
            prepare = executor.prepare
            def prepared(*args, **kwargs):
                result = prepare(*args, **kwargs)
                result['final_identity'] = {'serialno': 'MCP-EEPROM-001', 'eeprom-serial': 'MCP-EEPROM-001'}
                return result
            executor.prepare = prepared
            adapter, token = self.adapter(fixture)
            other, _ = self.adapter(fixture)
            target = FlashTarget(**self.scope(found))
            task = adapter.read_storage(target, 1, 'mcp-read-storage-001')['task']
            self.phase(fixture, task['id'], 'awaiting-selection')
            current = adapter.task('', task['id'])['task']
            self.assertEqual(current['board_targets'], ['MCP-EEPROM-001'])
            self.assertEqual(current['flash']['storage_inventory']['default_storage'], 'nvme:0')
            self.assertFalse(current['flash']['storage_inventory']['full_backup'])
            self.assertEqual(adapter.read_storage(target, 1, 'mcp-read-storage-001')['task']['id'], task['id'])
            artifact = current['flash']['nor_before']['artifact']['id']
            metadata = adapter.artifact(artifact)
            self.assertNotIn(token, metadata['download_url'])
            with urlopen(Request(metadata['download_url'], headers={'Authorization': 'Bearer ' + token})) as response:
                self.assertEqual(int(response.headers['Content-Length']), 8 << 20)
                self.assertEqual(len(response.read()), 8 << 20)
            for action in (
                    lambda: other.artifact(artifact),
                    lambda: other.task('', task['id']),
                    lambda: other.create_flash_artifact(target, 'bad.itb', 1, task['id']),
                    lambda: other.boot_priority(target, task['id'], 'UFS', 'SSD', 'mcp-priority-001')):
                with self.assertRaises(ClientError):
                    action()
            changed = adapter.boot_priority(target, task['id'], 'UFS', 'SSD', 'mcp-priority-001')
            self.assertEqual(changed['change']['verify_result'], 'verified')
            adapter.boot_priority(target, task['id'], 'UFS', 'SSD', 'mcp-priority-001')
            self.assertEqual(changes, [('UFS', 'SSD')])
            artifact_id = self.upload(adapter, target, task['id'])
            selections = [{'storage': 'active', 'partition': 'opensbi', 'artifact_id': artifact_id}]
            plan = adapter.select_flash(target, task['id'], '1' * 64, selections)['plan']
            self.assertFalse(plan['write_layout'])
            self.assertEqual(executor.executions, [])
            for _ in range(2):
                adapter.select_flash(target, task['id'], '1' * 64, plan_id=plan['id'],
                                     request_id='mcp-confirm-partition-001', preview=False)
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(final['flash']['verify_result'], 'verified')
            self.assertEqual(adapter.artifact(final['flash']['nor_after']['artifact']['id'])['artifact']['state'], 'available')
            self.assertEqual(len(executor.executions), 1)
            self.assertEqual(len(executor.preparations), 1)

    def test_unknown_eeprom_result_is_retained_and_same_request_never_rewrites(self):
        with usb.automatic_flashing() as fixture, tempfile.TemporaryDirectory() as root:
            executor, found = self.setup_workspace(fixture, root)
            executor.supports_storage_metadata = True
            executor.storage_inventory = lambda *args: {'storages': [], 'default_storage': '',
                'priority_source': 'eeprom:UFS', 'boot_priority_editable': True}
            writes = []
            def lost(binding, expected, value, check):
                writes.append((expected, value))
                raise TaskError('EEPROM write disconnected: unknown', 502)
            executor.set_boot_priority = lost
            adapter, _ = self.adapter(fixture)
            target = FlashTarget(**self.scope(found))
            task = adapter.read_storage(target, 1, 'mcp-unknown-read-001')['task']
            self.phase(fixture, task['id'], 'awaiting-selection')
            with self.assertRaises(ClientError):
                adapter.boot_priority(target, task['id'], 'UFS', 'SSD', 'mcp-unknown-priority-001')
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['flash']['boot_priority_change']['state'], 'unknown')
            self.assertEqual(adapter.boot_priority(target, task['id'], 'UFS', 'SSD',
                'mcp-unknown-priority-001')['change']['state'], 'unknown')
            with self.assertRaises(ClientError):
                adapter.boot_priority(target, task['id'], 'UFS', 'SSD', 'mcp-unknown-priority-002')
            self.assertEqual(writes, [('UFS', 'SSD')])
            self.assertEqual(executor.executions, [])

    def test_agent_preview_cleanup_failure_keeps_recovery_hold_and_owner(self):
        with usb.automatic_flashing() as fixture:
            adapter, token = self.adapter(fixture)
            found = adapter.probe_flash(FlashTarget(**self.scope(adapter.flash_targets()['targets'][0])))['target']
            target = FlashTarget(**self.scope(found))
            artifact = self.upload(adapter, target)
            fixture.executor.plan_failure = FlashRunnerError('unknown inspection process cleanup', 503, False)
            with self.assertRaises(ClientError):
                adapter.preview_flash(target, artifact, 'partitions', 1,
                    [{'storage': 'emmc', 'partition': 'rootfs'}], verify='if-supported')
            task = adapter.tasks('')['tasks'][0]
            self.assertEqual((task['kind'], task['state']), ('flash-inspect', 'failed'))
            self.assertTrue(task['flash']['recovery_required'])
            holds = maintenance_for(fixture.manager).target_holds(target.target_id)
            self.assertTrue(holds)
            self.assertTrue(all(hold['state'] == 'recovery' for hold in holds))
            code, _ = self.request(fixture, '/api/flash-recovery', dict(self.scope(found),
                id=task['id'], attempt_id=task['flash']['attempt_id'],
                reason='must not release through an agent', confirm_stopped=True), token=token)
            self.assertEqual(code, 403)
            other, _ = self.adapter(fixture)
            self.assertEqual(other.tasks('')['tasks'], [])

    def test_device_and_connection_grants_cannot_probe_guessed_or_different_usb_targets(self):
        with usb.automatic_flashing([('BOARD-A', 'a'), ('BOARD-B', 'b')]) as fixture:
            for serial in ('BOARD-A', 'BOARD-B'):
                candidate = usb.usb_candidate(port=(1, 4 if serial == 'BOARD-A' else 5), serial=serial, strong=True)
                candidate['serial_source'] = 'eeprom'
                candidate['identity']['eeprom-serial'] = serial
                if serial == 'BOARD-A':
                    fixture.executor.candidates[0] = candidate
                else:
                    fixture.executor.candidates.append(candidate)
            targets = [self.probe(fixture, row)['target'] for row in self.discover(fixture)]
            targets.sort(key=lambda row: row['serial'])
            adapter, token = self.adapter(fixture, 'device', 'BOARD-A')
            own = FlashTarget(**self.scope(targets[0]))
            different = FlashTarget(**self.scope(targets[1]))
            self.assertEqual(adapter.flash_capabilities(own)['serial'], 'BOARD-A')
            self.assertEqual(adapter.probe_flash(own)['target']['serial'], 'BOARD-A')
            artifact = self.upload(adapter, own)
            self.assertEqual(adapter.image_library()['artifacts'][0]['id'], artifact)
            plan = adapter.preview_flash(own, artifact, 'partitions', 1,
                [{'storage': 'emmc', 'partition': 'rootfs'}], verify='if-supported')['plan']
            task = adapter.start_flash(own, plan['id'], 1, 'mcp-scoped-flash-001')['task']
            self.terminal(fixture, task['id'])
            self.assertEqual(adapter.task('', task['id'])['task']['state'], 'completed')
            for action in (adapter.flash_targets, lambda: adapter.probe_flash(different),
                           lambda: adapter.flash_capabilities(different)):
                before = len(fixture.executor.discoveries)
                with self.assertRaises(ClientError):
                    action()
                self.assertEqual(len(fixture.executor.discoveries), before)
            unknown = dict(fixture.executor.candidates[0])
            unknown['serial'], unknown['strong_serial'] = '', False
            unknown['device'] = dict(unknown['device'], port_path=[1, 6])
            fixture.executor.candidates.append(unknown)
            unbound = FlashTarget(**self.scope(next(row for row in self.discover(fixture) if not row['serial'])))
            with self.assertRaises(ClientError):
                adapter.probe_flash(unbound)
            connection, _ = fixture.grants.issue('connection', key='unused', connection_id='current-connection')
            raw = Adapter(Settings(fixture.url, connection))
            for action in (raw.flash_targets, lambda: raw.probe_flash(own),
                           lambda: raw.flash_capabilities(own), raw.images, raw.image_library):
                with self.assertRaises(ClientError):
                    action()
            # Caller-supplied serial/actor cannot widen the server-owned grant.
            code, _ = self.request(fixture, '/api/flash-target', dict(self.scope(targets[1]),
                serial='BOARD-B', actor='web'), token=token)
            self.assertEqual(code, 409)
            grant = fixture.grants.lookup(token)
            fixture.grants.revoke(grant['id'])
            with self.assertRaises(ClientError) as revoked:
                adapter.flash_capabilities(own)
            self.assertEqual(revoked.exception.status, 401)

    def test_url_import_and_library_are_owned_and_request_id_deduplicated(self):
        with usb.automatic_flashing() as fixture, downloads.fixture() as (_, url):
            adapter, _ = self.adapter(fixture)
            other, _ = self.adapter(fixture)
            found = adapter.probe_flash(FlashTarget(**self.scope(adapter.flash_targets()['targets'][0])))['target']
            target = FlashTarget(**self.scope(found))
            with mock.patch('seriald_core.image_catalog.catalog', return_value={'entries': [{'name': 'fixture'}]}):
                self.assertEqual(adapter.images()['entries'][0]['name'], 'fixture')
            task = adapter.download_image(target, url + '/image', 'mcp-image-download-001', name='opensbi.itb')['task']
            final = self.terminal(fixture, task['id'])
            self.assertEqual(final['state'], 'completed', final)
            self.assertEqual(adapter.download_image(target, url + '/image', 'mcp-image-download-001',
                name='opensbi.itb')['task']['id'], task['id'])
            artifact = final['result']['artifact']
            self.assertEqual(adapter.image_library()['artifacts'][0]['id'], artifact['id'])
            self.assertEqual(other.image_library()['artifacts'], [])
            with self.assertRaises(ClientError):
                other.artifact(artifact['id'])
            with self.assertRaises(ClientError):
                adapter.download_image(target, url + '/other', 'mcp-image-download-001')
            adapter.delete_artifact(artifact['id'])
            self.assertEqual(adapter.image_library()['artifacts'], [])

    def test_target_and_chunk_validation_do_not_send_requests(self):
        adapter = Adapter(Settings('http://127.0.0.1:1'))
        for target in (FlashTarget(), FlashTarget('board', 'a' * 24, 'generation'),
                       FlashTarget(target_id='bad', target_generation='generation')):
            with self.assertRaises(ClientError):
                adapter.flash_capabilities(target)
        with self.assertRaises(ClientError):
            adapter.append_artifact('a' * 24, 0, 'A' * 60004)
