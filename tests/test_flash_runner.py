"""No USB or UART: configured flashing runs only temporary local fixtures."""
import json
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from seriald_core.flash_runner import (FlashBook, FlashRunnerError, LocalFlashExecutor,
                                       public_plan, safe_event)
from seriald_core.tasks import TaskCancelled, TaskError


def configuration(argv=None):
    return {'boards': {'board-a': {'binding_revision': 7, 'profile': 'test-board',
        'profile_data': {'id': 'test-board', 'identity': {'product': 'test-board'}},
        'control': {'provider': 'manual'}, 'console': {'provider': 'none'},
        'flash': {'endpoint': 'slot-4', 'argv': argv or [sys.executable],
                  'device': {'port_path': [1, 4], 'vendor_id': 0x1234}, 'timeout': 2}}}}


class FlashBookTests(unittest.TestCase):
    def test_default_disabled_and_agents_need_explicit_grant(self):
        self.assertFalse(FlashBook().available('board-a')['supported'])
        book = FlashBook(configuration())
        self.assertFalse(book.available('board-a', 'agent/alice')['supported'])
        with self.assertRaises(TaskError) as error:
            book.binding('board-a', 'agent/alice')
        self.assertEqual(error.exception.status, 403)
        public = book.available('board-a')
        self.assertEqual(public['binding_revision'], 7)
        self.assertEqual(public['control']['provider'], 'manual')
        self.assertNotIn('argv', json.dumps(public))
        self.assertNotIn('port_path', json.dumps(public))
        self.assertNotIn('profile_data', public)
        self.assertEqual(book.binding('board-a').resources, ['flash:local:slot-4', 'flash-usb:local:1.4'])

    def test_resources_include_final_download_and_intermediate_physical_usb_ports(self):
        config = configuration()
        config['boards']['board-a']['profile_data'].update(
            download_device={'port_path': [3, 2, 1]},
            prepare=[{'kind': 'continue', 'port_path': [1, 4]},
                     {'kind': 'continue', 'port_path': [2, 7]}])
        self.assertEqual(FlashBook(config).binding('board-a').resources,
            ['flash:local:slot-4', 'flash-usb:local:1.4', 'flash-usb:local:2.7', 'flash-usb:local:3.2.1'])

    def test_capability_reports_missing_or_nonexecutable_executor_without_host_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / 'private-executor')
            config = configuration([missing])
            with mock.patch('subprocess.Popen') as spawn:
                result = FlashBook(config).available('board-a')
            spawn.assert_not_called()
            self.assertFalse(result['supported'])
            self.assertIn('missing', result['reason'])
            self.assertNotIn(directory, json.dumps(result))
            executable = Path(missing)
            executable.write_text('fixture')
            with mock.patch('seriald_core.flash_runner.os.access', return_value=False):
                result = FlashBook(config).available('board-a')
            self.assertFalse(result['supported'])
            self.assertIn('not executable', result['reason'])
            self.assertNotIn(directory, json.dumps(result))
            config['boards']['board-a']['flash']['argv'] = [directory]
            self.assertFalse(FlashBook(config).available('board-a')['supported'])

    def test_rejects_ambiguous_paths_remote_nodes_commands_and_invalid_limits(self):
        mutations = [
            lambda v: v['flash'].update(argv=['relative/goflash']),
            lambda v: v['flash'].update(device={'usb_serial': 'x'}),
            lambda v: v['flash'].update(device={'port_path': [True, 1]}),
            lambda v: v['flash'].update(node='remote-node'),
            lambda v: v['flash'].update(timeout=True),
            lambda v: v['flash'].update(allow_agent=1),
            lambda v: v['flash'].update(max_cache_bytes=1),
            lambda v: v.update(binding_revision=0),
            lambda v: v.update(control={'provider': 'shell', 'argv': ['rm']}),
        ]
        for mutation in mutations:
            value = configuration()
            mutation(value['boards']['board-a'])
            with self.subTest(value=value), self.assertRaises(ValueError):
                FlashBook(value)

    def test_env_loading_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'flash.json'
            config.write_text('{"boards":{},"boards":{}}')
            with mock.patch.dict(os.environ, {'SERIALD_FLASH': str(config)}), self.assertRaises(ValueError):
                FlashBook.from_env()

    def test_preview_removes_all_host_path_fields_and_executor_configuration(self):
        value = {'digest': 'abc', 'mode': 'full', 'artifact': {'id': 'image', 'path': '/private/image'},
                 'profile': {'prepare': [{'path': '/loader'}]}, 'device': {'port_path': [1, 4]},
                 'writes': [{'storage': 'mmc', 'partition': 'rootfs', 'size': 123,
                             'path': '/private/cache/payload', 'cache_path': '/cache'}]}
        result = public_plan(value)
        self.assertNotIn('/private', json.dumps(result))
        self.assertNotIn('profile', result)
        self.assertEqual(result['writes'], [{'storage': 'mmc', 'partition': 'rootfs', 'size': 123}])


class FlashRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.helper = self.root / 'helper.py'
        self.runner = LocalFlashExecutor(self.root / 'workspace')

    def tearDown(self):
        self.temporary.cleanup()

    def binding(self, body, timeout=2):
        self.helper.write_text('import json,os,sys,time\nfrom pathlib import Path\n'
                               'request=json.loads(Path(sys.argv[-1]).read_text())\n' + body)
        config = configuration([sys.executable, str(self.helper)])
        config['boards']['board-a']['flash']['timeout'] = timeout
        return FlashBook(config).binding('board-a')

    def automatic_binding(self):
        profile = {'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3', 'identity': {},
                   'bootrom_identity': {'version-IC': 'K3', 'version-brom': '1.0'},
                   'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt',
                                 'size_getvar': '__decklab_gpt_capacity', 'sector_size': 512,
                                 'partitions': [], 'sparse': True}]}
        return replace(self.binding('pass'), profile='auto-spacemit-gpt-v1', profile_data=profile,
                       target_id='a' * 24, target_generation='b' * 24,
                       device={'port_path': [1, 4], 'usb_address': 3})

    def prepared_profile(self, binding, workspace):
        package = Path(workspace) / 'payloads' / 'image'
        (package / 'factory').mkdir(parents=True)
        fsbl, loader = package / 'factory' / 'FSBL.bin', package / 'u-boot.itb'
        fsbl.write_bytes(b'fixed-fsbl')
        loader.write_bytes(b'fixed-u-boot')
        profile = json.loads(json.dumps(binding.profile_data))
        profile['storages'][0]['package_tables'] = ['partition_universal.json']
        profile['prepare'] = [
            {'kind': 'stage', 'path': str(fsbl), 'sha256': hashlib.sha256(fsbl.read_bytes()).hexdigest()},
            {'kind': 'continue'},
            {'kind': 'stage', 'path': str(loader), 'sha256': hashlib.sha256(loader.read_bytes()).hexdigest()},
            {'kind': 'continue'}]
        return profile

    def test_imported_loader_profile_is_private_and_used_for_target_check(self):
        binding = self.automatic_binding()
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.zip'}
        derived = {}
        def run(current, operation, request, **options):
            if operation == 'image-plan':
                derived.update(self.prepared_profile(binding, request['workspace']))
                return {'plan': {'ready': False}, 'execution_profile': derived}
            self.assertEqual(operation, 'check-plan')
            self.assertEqual(current.profile_data, derived)
            self.assertEqual(request['profile'], derived)
            return {'plan': {'ready': False}, 'requires_prepare': True}
        with mock.patch.object(self.runner, '_run', side_effect=run):
            result = self.runner.plan(binding, artifact, 'partitions')
        self.assertEqual(result['execution_profile'], derived)
        self.assertNotIn('prepare', binding.profile_data, 'the source binding must remain fixed')
        self.assertNotIn('execution_profile', public_plan(result))
        self.assertNotIn(str(self.root), json.dumps(public_plan(result)))

    def test_imported_loaders_cannot_escape_cache_change_identity_or_expand_steps(self):
        binding = self.automatic_binding()
        workspace = self.runner._directory(self.root / 'workspace' / 'cache') / 'plan-abcdefgh'
        workspace.mkdir()
        original = self.prepared_profile(binding, workspace)
        accepted = self.runner.execution_binding(binding, original, workspace)
        self.assertEqual(accepted.profile_data, original)
        mutations = [
            lambda p: p['identity'].update(product='different-board'),
            lambda p: p['storages'][0].update(select_oem='erase all'),
            lambda p: p['storages'][0].update(package_tables=['different.json']),
            lambda p: p['prepare'][0].update(sha256='0' * 64),
            lambda p: p['prepare'][0].update(path='/private/factory/FSBL.bin'),
            lambda p: p['prepare'][1].update(port_path=[9, 9]),
            lambda p: p['prepare'].append({'kind': 'continue'}),
        ]
        for mutation in mutations:
            profile = json.loads(json.dumps(original))
            mutation(profile)
            with self.subTest(profile=profile), self.assertRaises(FlashRunnerError):
                self.runner.execution_binding(binding, profile, workspace)
        if os.name == 'posix':
            fsbl = Path(original['prepare'][0]['path'])
            target = fsbl.with_name('original.bin')
            fsbl.rename(target)
            fsbl.symlink_to(target)
            with self.assertRaisesRegex(FlashRunnerError, 'symbolic link'):
                self.runner.execution_binding(binding, original, workspace)

    def test_prepared_connection_transition_is_explicit_and_only_for_unready_plan(self):
        initial = self.automatic_binding()
        workspace = self.runner._directory(self.root / 'workspace' / 'cache') / 'plan-abcdefgh'
        workspace.mkdir()
        prepared = self.runner.execution_binding(initial, self.prepared_profile(initial, workspace), workspace)
        final = replace(prepared, device=dict(initial.device, usb_address=4))
        plan = {'artifact': {'id': 'image'}, 'mode': 'partitions', 'ready': False, 'selections': []}
        with mock.patch.object(self.runner, '_run', return_value={'plan': dict(plan, ready=True)}) as run:
            self.runner.check_plan(final, plan, previous_device=initial.device)
        self.assertEqual(run.call_args.args[2]['previous_device'], initial.device)
        self.assertEqual(run.call_args.args[2]['device']['usb_address'], 4)
        for invalid in (dict(plan, ready=True), dict(plan, ready=None)):
            with self.subTest(plan=invalid), self.assertRaises(TaskError):
                self.runner.check_plan(final, invalid, previous_device=initial.device)
        with self.assertRaises(TaskError):
            self.runner.check_plan(final, plan, previous_device={'port_path': [9, 9], 'usb_address': 3})
        with self.assertRaises(TaskError):
            self.runner.check_plan(initial, plan, previous_device=initial.device)

    def test_progress_and_result_with_private_request_and_no_stderr_exposure(self):
        binding = self.binding("print('password=host-secret',file=sys.stderr)\n"
            "print(json.dumps({'event':'progress','phase':'write','done':4,'total':4,'message':'host-secret'}))\n"
            "print(json.dumps({'event':'result','result':{'write_result':'completed','verify_result':'verified',"
            "'private_mode':os.stat(sys.argv[-1]).st_mode & 511}}))\n")
        events = []
        result = self.runner.execute(binding, {'verify': 'required'}, events.append, task_id='task', attempt_id='attempt')
        self.assertEqual(result['write_result'], 'completed')
        if os.name == 'posix':
            self.assertEqual(result['private_mode'], 0o600)
        self.assertEqual(events, [{'event': 'progress', 'phase': 'write', 'done': 4, 'total': 4}])
        self.assertEqual(list((self.root / 'workspace' / 'requests').iterdir()), [])

    def test_error_and_failed_result_keep_only_confirmed_domain_outcomes(self):
        report = {'write_result': 'completed', 'verify_result': 'failed', 'boot_result': 'not-requested',
                  'written_bytes': 4096, 'password': 'host-secret', 'argv': ['/private/helper'],
                  'confirmed_bytes': True, 'plan_digest': 'd' * 64}
        for kind in ('error', 'result'):
            value = dict(report)
            if kind == 'result':
                value['ok'] = False
            event = {'event': kind, 'result': value, 'code': 'verify-failed'}
            binding = self.binding('print(%r,flush=True)\nsys.exit(3)' % json.dumps(event))
            with self.subTest(kind=kind), self.assertRaises(FlashRunnerError) as error:
                self.runner.execute(binding, {'verify': 'required'})
            self.assertEqual(error.exception.outcome, {
                'write_result': 'completed', 'verify_result': 'failed', 'boot_result': 'not-requested',
                'written_bytes': 4096, 'plan_digest': 'd' * 64})
            self.assertNotIn('host-secret', str(error.exception))
            if kind == 'error':
                self.assertEqual(error.exception.code, 'verify-failed')
                self.assertIn('verify-failed', str(error.exception))

    def test_error_code_is_available_without_raw_helper_message(self):
        binding = self.binding("print(json.dumps({'event':'error','code':'unsupported-raw-image',"
                               "'message':'host-secret /private/image'}))")
        with self.assertRaises(FlashRunnerError) as error:
            self.runner.inspect(binding)
        self.assertEqual(error.exception.code, 'unsupported-raw-image')
        self.assertIn('unsupported-raw-image', str(error.exception))
        self.assertNotIn('host-secret', str(error.exception))
        self.assertNotIn('/private', str(error.exception))
        self.assertEqual(FlashRunnerError('failure', code='/private/secret').code, '')
        self.assertNotIn('/private', str(FlashRunnerError('failure', code='/private/secret')))

    def test_required_verification_failure_keeps_completed_write_evidence(self):
        binding = self.binding("print(json.dumps({'event':'result','result':{'write_result':'completed',"
                               "'verify_result':'unavailable','confirmed_bytes':8192}}))")
        with self.assertRaises(FlashRunnerError) as error:
            self.runner.execute(binding, {'verify': 'required'})
        self.assertEqual(error.exception.outcome['write_result'], 'completed')
        self.assertEqual(error.exception.outcome['verify_result'], 'unavailable')
        self.assertEqual(error.exception.outcome['written_bytes'], 8192)

    def test_failures_never_report_success_or_expose_raw_diagnostics(self):
        bodies = [
            "print(json.dumps({'event':'error','code':'write-failed','message':'password=host-secret'}))",
            "print(json.dumps({'event':'result','result':{'write_result':'completed'}}));sys.exit(2)",
            "print(json.dumps({'event':'result','result':{'write_result':'failed'}}))",
            "print(json.dumps({'event':'result','result':{'write_result':'completed','verify_result':'unavailable'}}))",
            "print('not-json host-secret')",
            "print(json.dumps({'event':'progress','done':8,'total':4}))",
            "print(json.dumps({'event':'result','result':{'write_result':'completed'}}));print(json.dumps({'event':'progress','done':1}))",
            "pass",
        ]
        for body in bodies:
            with self.subTest(body=body):
                binding = self.binding(body)
                with self.assertRaises(FlashRunnerError) as error:
                    self.runner.execute(binding, {'verify': 'required'})
                self.assertNotIn('host-secret', str(error.exception))
                self.assertTrue(error.exception.cleanup_confirmed)

    def test_plan_workspace_is_unique_and_retained_only_after_success(self):
        binding = self.binding("print(json.dumps({'event':'result','result':{'workspace':request['workspace'],"
                               "'mode':request['mode'],'budget':request['max_cache_bytes'],"
                               "'target':request.get('target_storage'),'plan':{'mode':request['mode']}}}))")
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.raw'}
        first = self.runner.plan(binding, artifact, 'full', target_storage='nvme')
        second = self.runner.plan(binding, artifact, 'full')
        self.assertNotEqual(first['workspace'], second['workspace'])
        self.assertTrue(Path(first['workspace']).is_dir())
        self.assertGreater(first['budget'], 0)
        self.assertEqual(first['target'], 'nvme')
        before = set((self.root / 'workspace' / 'cache').iterdir())
        binding = self.binding('sys.exit(1)')
        with self.assertRaises(FlashRunnerError):
            self.runner.plan(binding, artifact, 'partitions')
        self.assertEqual(set((self.root / 'workspace' / 'cache').iterdir()), before)
        self.assertTrue(self.runner.cleanup_plan(first))
        self.assertFalse(self.runner.cleanup_plan(first))
        self.assertTrue(Path(second['workspace']).exists())

    def test_image_preparation_precedes_short_usb_inspection_scope(self):
        binding = self.binding('pass')
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.raw'}
        events, held = [], False
        @contextmanager
        def scope():
            nonlocal held
            held = True
            events.append('enter')
            try:
                yield
            finally:
                events.append('leave')
                held = False
        def run(binding, operation, request, **options):
            events.append((operation, held))
            if operation == 'check-plan':
                self.assertEqual(request['plan'], {'digest': 'fixed'})
                self.assertEqual(request['payload_candidates'], [{'partition': 'rootfs'}])
            return {'plan': {'digest': 'fixed'}, 'payload_candidates': [{'partition': 'rootfs'}]}
        with mock.patch.object(self.runner, '_run', side_effect=run):
            self.runner.plan(binding, artifact, 'full', inspection_scope=scope)
        self.assertEqual(events, [('image-plan', False), 'enter', ('check-plan', True), 'leave'])

    def test_unknown_plan_process_identifies_which_resources_need_recovery(self):
        binding = self.binding('pass')
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.raw'}
        for failed in ('image-plan', 'check-plan'):
            def run(binding, operation, request, **options):
                if operation == failed:
                    raise FlashRunnerError('cleanup could not be confirmed', 503, False)
                return {'plan': {'digest': 'fixed'}, 'payload_candidates': []}
            with self.subTest(failed=failed), mock.patch.object(self.runner, '_run', side_effect=run), \
                    self.assertRaises(FlashRunnerError) as error:
                self.runner.plan(binding, artifact, 'full')
            self.assertEqual(error.exception.operation, failed)
            self.assertEqual(error.exception.stage, failed)
            self.assertTrue(Path(error.exception.workspace).exists())
            self.runner.cleanup_plan(error.exception.workspace)

    def test_cached_plan_rechecks_target_without_importing_or_reallocating_cache(self):
        binding = self.binding('pass')
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.raw'}
        selected = [{'storage': 'mmc', 'partition': 'rootfs'}]
        plan = {'artifact': artifact, 'mode': 'partitions', 'verify': 'required',
                'target_storage': 'mmc', 'selections': selected, 'digest': 'd' * 64}
        candidates = [{'storage': 'mmc', 'partition': 'rootfs', 'path': '/managed/cached/rootfs'}]
        result = {'plan': dict(plan, ready=True)}
        with mock.patch.object(self.runner, '_run', return_value=result) as run:
            self.assertIs(self.runner.check_plan(binding, plan, candidates), result)
        operation = run.call_args.args[1]
        request = run.call_args.args[2]
        self.assertEqual(operation, 'check-plan')
        self.assertEqual(request['artifact'], artifact)
        self.assertEqual(request['selections'], selected)
        self.assertEqual(request['payload_candidates'], candidates)
        self.assertEqual(request['target_storage'], 'mmc')
        self.assertEqual(request['verify'], 'required')
        self.assertNotIn('selection_update', request)
        self.assertFalse((self.root / 'workspace').exists())
        new_selection = [{'storage': 'mmc', 'partition': 'bootfs'}]
        with mock.patch.object(self.runner, '_run', return_value=result) as run:
            self.runner.check_plan(binding, plan, candidates, selections=new_selection)
        self.assertEqual(run.call_args.args[2]['selections'], new_selection)
        self.assertTrue(run.call_args.args[2]['selection_update'])
        self.assertEqual(plan['selections'], selected)

    def test_unconfirmed_plan_cleanup_retains_workspace_for_recovery(self):
        binding = self.binding('pass')
        artifact = {'id': 'image', 'sha256': 'a' * 64, 'size': 1, 'path': '/managed/image', 'name': 'image.raw'}
        failure = FlashRunnerError('cleanup could not be confirmed', 503, False)
        with mock.patch.object(self.runner, '_run', side_effect=failure), self.assertRaises(FlashRunnerError) as error:
            self.runner.plan(binding, artifact, 'full')
        workspace = Path(error.exception.workspace)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(workspace.parent, self.root / 'workspace' / 'cache')
        self.assertEqual(self.runner._cache_reserved, 0)
        # Explicit cleanup is only allowed after the coordinator has proved
        # there are no active processes or recovery references.
        self.assertTrue(self.runner.cleanup_plan(workspace))

    def test_workspace_cleanup_rejects_foreign_path_and_link(self):
        foreign = self.root / 'plan-abcdefgh'
        foreign.mkdir()
        with self.assertRaises(TaskError):
            self.runner.cleanup_plan(str(foreign))
        cache = self.root / 'workspace' / 'cache'
        cache.mkdir(parents=True)
        if os.name == 'posix':
            link = cache / 'plan-abcdefgh'
            link.symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(TaskError):
                self.runner.cleanup_plan(str(link))
            self.assertTrue(foreign.exists())

    def test_timeout_and_cancellation_kill_owned_process(self):
        for cancel in (False, True):
            marker = self.root / ('cancel.pid' if cancel else 'timeout.pid')
            binding = self.binding('Path(%r).write_text(str(os.getpid()))\ntime.sleep(30)' % str(marker), timeout=1)
            def check():
                if cancel and marker.exists():
                    raise TaskCancelled('cancelled')
            started = time.monotonic()
            with self.subTest(cancel=cancel), self.assertRaises(TaskCancelled if cancel else FlashRunnerError):
                self.runner.execute(binding, {}, check=check)
            self.assertLess(time.monotonic() - started, 5)
            if os.name == 'posix':
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(marker.read_text()), 0)

    def test_process_cleanup_failure_retains_recovery_requirement(self):
        binding = self.binding("print(json.dumps({'event':'result','result':{'write_result':'completed'}}))")
        from seriald_core.flash_runner import stream_process
        process = stream_process([sys.executable, str(self.helper), '--request', self._request_fixture()])
        original = process.close
        def close():
            original()
            raise OSError('cannot confirm cleanup')
        with mock.patch('seriald_core.flash_runner.stream_process', return_value=process), \
                mock.patch.object(process, 'close', side_effect=close), self.assertRaises(FlashRunnerError) as error:
            self.runner.execute(binding, {})
        self.assertFalse(error.exception.cleanup_confirmed)

    @unittest.skipUnless(os.name == 'posix', 'requires process groups and /proc')
    def test_success_cleans_lingering_descendants_and_closes_tree_once(self):
        marker = self.root / 'child.pid'
        child = 'import os,time;from pathlib import Path;Path(%r).write_text(str(os.getpid()));time.sleep(30)' % str(marker)
        binding = self.binding('import subprocess\nsubprocess.Popen([sys.executable,"-c",%r])\n'
            'while not Path(%r).exists():time.sleep(.01)\n'
            'print(json.dumps({"event":"result","result":{"write_result":"completed"}}),flush=True)' % (child, str(marker)))
        from seriald_core.flash_runner import stream_process
        closed = []
        def create(argv):
            process = stream_process(argv)
            original = process.close
            def close():
                closed.append(1)
                original()
            process.close = close
            return process
        started = time.monotonic()
        with mock.patch('seriald_core.flash_runner.stream_process', side_effect=create):
            self.runner.execute(binding, {})
        self.assertEqual(closed, [1])
        self.assertLess(time.monotonic() - started, 5)
        status = Path('/proc') / marker.read_text() / 'stat'
        if status.exists():
            self.assertIn(status.read_text().rpartition(')')[2].split()[0], ('Z', 'X'))

    def _request_fixture(self):
        path = self.root / 'request-fixture.json'
        path.write_text('{}')
        return str(path)

    def test_oversized_output_is_bounded_and_stopped(self):
        binding = self.binding("print('x' * 3000,flush=True);time.sleep(30)")
        with mock.patch('seriald_core.flash_runner.MAX_LINE', 1024), self.assertRaisesRegex(FlashRunnerError, 'limit'):
            self.runner.inspect(binding)


if __name__ == '__main__':
    unittest.main()
