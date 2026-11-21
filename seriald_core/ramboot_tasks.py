"""Prepare and boot an uploaded image while a single task owns the USB attachment."""
from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from .board_maintenance import CACHE_POOL_RESOURCE, maintenance_for
from .flash_discovery import _selector
from .flash_runner import stream_process
from .flash_tasks import _digest, _discovery, _resources, _services
from .ramboot_prepare import K1_MUSE_PI_PRO_MODEL, K3_PICO_MODEL
from .results import response
from .tasks import TaskError
from .transactions import TransactionError


TOOLS = ('debugfs', 'fdtget', 'fdtput', 'gzip', 'lsinitramfs', 'lz4', 'zstd')


@lru_cache(maxsize=8)
def executor_support(argv, identity):
    try:
        result = subprocess.run([*argv, '--capabilities'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=True)
        capabilities = json.loads(result.stdout)
        return frozenset(key for key in ('ram_boot', 'ram_boot_k1') if capabilities.get(key) is True)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return frozenset()


def availability(profile, actor, argv):
    family = profile.get('chip_family')
    if actor != 'web' or not sys.platform.startswith('linux') or family not in ('k1', 'k3'):
        return None
    if not profile.get('bootrom_identity') and (family == 'k1' or profile.get('ram_loader_protocol') != 'spacemit-ram-v1'):
        return None
    try:
        binary = Path(argv[0]).stat()
        identity = (binary.st_dev, binary.st_ino, binary.st_size, binary.st_mtime_ns)
    except (IndexError, OSError):
        return '控制机缺少内存启动执行器'
    capability = 'ram_boot_k1' if family == 'k1' else 'ram_boot'
    if capability not in executor_support(tuple(argv), identity):
        return '控制机需要支持内存启动的刷机执行器'
    from .flash_loaders import installed_for, k1_ram_boot_installed
    if family == 'k1' and not k1_ram_boot_installed():
        return '控制机缺少 K1 MUSE-Pi-Pro 内存启动组件'
    if family == 'k3' and profile.get('bootrom_identity') and not installed_for('k3'):
        return '控制机缺少 K3 临时加载器'
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        return '控制机缺少内存启动工具 ' + ', '.join(missing)
    return ''


def _prepare(args, context):
    process = stream_process([sys.executable, '-m', 'seriald_core.ramboot_prepare', *args])
    deadline = time.monotonic() + 3600
    try:
        while time.monotonic() < deadline:
            context.check()
            try:
                output, _ = process.process.communicate(timeout=.25)
            except subprocess.TimeoutExpired:
                continue
            try:
                receipt = json.loads(output)
            except (ValueError, UnicodeError):
                raise TaskError('控制机未返回内存启动准备结果', 502) from None
            if process.returncode:
                raise TaskError(str(receipt.get('error') or '内存启动镜像准备失败')[:2000], 422)
            return receipt
        raise TaskError('内存启动镜像准备超时', 504)
    finally:
        try:
            process.close()
        except OSError as error:
            failure = TaskError('无法确认镜像准备进程已停止', 503)
            failure.cleanup_confirmed = False
            raise failure from error
        finally:
            process.stdout.close()


def start(actions, payload, actor):
    manager = actions.manager
    try:
        if actor != 'web':
            raise TaskError('只有网页所有者可以启动内存镜像', 403)
        if set(payload) - {'kind', 'target_id', 'target_generation', 'binding_revision', 'serial',
                           'artifact_id', 'client_id', 'request_id', 'actor'}:
            raise TaskError('invalid RAM boot request')
        _services(manager)
        identity = {key: payload.get(key) for key in ('target_id', 'target_generation', 'serial',
                                                       'binding_revision', 'artifact_id')}
        input_digest = _digest(dict(identity, operation='ram-boot'))
        request_id = payload.get('request_id')
        with manager._flash_plans_lock:
            previous = actions.store.find_request(actor, request_id, input_digest)
            if previous:
                return response(202, {'ok': True, 'task': manager.tasks.store.get(previous)})
            if not isinstance(payload.get('target_id'), str) or not payload['target_id']:
                raise TaskError('内存启动需要检测到的 USB 下载目标', 409)
            device = actions._device(None, payload, actor)
            binding, executor = actions._target(device, actor, payload)
            reason = availability(binding.profile_data, actor, binding.argv)
            if reason is None:
                raise TaskError('此平台或加载器不支持内存启动', 409)
            if reason:
                raise TaskError(reason, 503)
            if type(payload.get('binding_revision')) is not int or payload['binding_revision'] != binding.binding_revision:
                raise TaskError('flash binding changed; refresh capabilities', 409)
            client = payload.get('client_id', '')
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            row = manager.artifacts.get(payload.get('artifact_id'), actor)
            preparing = (row['state'] == 'claimed' and row['task_id']
                         and manager.tasks.store.get(row['task_id'])['kind'] == 'image-prepare')
            if row['direction'] != 'upload' or not (row['state'] == 'ready' or preparing):
                raise TaskError('请选择已上传的原始系统镜像', 409)
            attempt_id = secrets.token_hex(12)
            maintenance = maintenance_for(manager)

            def run(context):
                return _run(actions, executor, binding, device.serial, row, context, attempt_id, maintenance)

            task = manager.tasks.start_board('ram-boot', actor, client, device.serial, run,
                request_id=request_id, input_digest=input_digest, maintenance=maintenance,
                resources=_resources(binding) + [CACHE_POOL_RESOURCE],
                target_id=binding.target_id, target_generation=binding.target_generation,
                details={'operation': 'ram-boot', 'attempt_id': attempt_id, 'artifact_id': row['id'],
                         'binding_revision': binding.binding_revision, 'target_id': binding.target_id,
                         'target_generation': binding.target_generation, 'write_result': 'not-started'})
        return response(202, {'ok': True, 'task': task})
    except (TaskError, TransactionError, OSError, ValueError) as error:
        return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})


def _run(actions, executor, binding, serial, row, context, attempt_id, maintenance):
    manager = actions.manager
    discovery = _discovery(manager)
    k1 = binding.profile_data.get('chip_family') == 'k1'
    original_generation = generation = binding.target_generation
    loader_workspace = None
    claimed = False
    cleanup_confirmed = True

    def check():
        context.check()
        if serial:
            from .board_ownership import ownership_for
            ownership_for(manager).check(serial, 'web')
            if manager.store.has_conflict(serial):
                raise TaskError('RAM boot board identity is in conflict', 409)

    def check_loader():
        check()
        current = discovery.get(binding.target_id, generation)
        if current['document']['device'] != binding.device:
            raise TaskError('RAM boot USB attachment changed', 409)

    def event(value):
        phase = value.get('phase', '')
        if phase in ('preparing-loader', 'ramboot-transfer', 'ramboot-handoff'):
            actions.store.append_event(context.id, value)
            context.progress(value.get('done', 0), value.get('total', 0), phase)

    try:
        check_loader()
        with executor.image_guard(row, check_loader):
            manager.artifacts.claim(row['id'], context.id, 'web')
            claimed = True
        with manager.artifacts.open(row['id'], 'web', task_id=context.id):
            check_loader()
            if binding.profile_data.get('bootrom_identity'):
                context.progress(0, 0, 'preparing-loader')
                materials = executor.ram_boot_workspace(binding, check_loader)
                loader_workspace = materials['workspace']
                actions.store.update_details(context.id, {'workspace': loader_workspace})
                binding = executor.execution_binding(binding, materials['execution_profile'], loader_workspace, check_loader)
                prepared = executor.prepare(binding, event, check, task_id=context.id, attempt_id=attempt_id)
                final = _selector(prepared.get('final_device'))
                identity = prepared.get('final_identity') or {}
                verified = identity.get('serialno')
                if not verified or identity.get('eeprom-serial') != verified or serial and serial != verified:
                    raise TaskError('RAM boot loader did not confirm the board EEPROM serial', 409)
                generation = discovery.accept_prepared_target(binding.target_id, original_generation, final,
                                                               context.id, attempt_id, verified)
                serial = discovery.accept_eeprom_identity(binding.target_id, generation, identity, context.id, attempt_id)
                binding = replace(binding, device=final)
                actions.store.update_details(context.id, {'resolved_serial': serial, 'serial_source': 'eeprom'})
            if not serial:
                raise TaskError('RAM boot requires an identified board EEPROM serial', 409)
            context.progress(0, 0, 'ramboot-identify')
            board = executor.ram_boot(binding, serial, check=check_loader)
            model = K1_MUSE_PI_PRO_MODEL if k1 else K3_PICO_MODEL
            if board.get('serial') != serial or board.get('model') != model:
                raise TaskError(f'内存启动尚未支持加载器报告的板型 {board.get("model")!r}', 409)
            ram_bytes = board.get('ram_bytes')
            if type(ram_bytes) is not int or not 4 << 30 <= ram_bytes <= 128 << 30:
                raise TaskError('无法确认板卡可用内存容量', 409)
            context.progress(0, 0, 'ramboot-prepare')
            if loader_workspace is None:
                cache = executor._directory(executor.root / 'cache')
                loader_workspace = tempfile.mkdtemp(prefix='plan-', dir=cache)
                actions.store.update_details(context.id, {'workspace': loader_workspace})
            output = Path(loader_workspace) / 'ram-payload'
            path = manager.artifacts._path(row['id'], '.bin')
            receipt = _prepare([str(path), '--board-model', board['model'], '--ram-bytes', str(ram_bytes),
                                '--output', str(output), '--expected-sha256', row['sha256'], '--task-result'], context)
            receipt['image'] = row['name']
            actions.store.update_details(context.id, {'ram_boot': receipt})
            handoff_started = False

            def transfer_event(value):
                nonlocal handoff_started
                event(value)
                if value.get('phase') == 'ramboot-handoff':
                    handoff_started = True

            def transfer_check():
                if handoff_started:
                    check()
                else:
                    check_loader()

            handoff = executor.ram_boot(binding, serial, output, receipt['files'], transfer_event, transfer_check,
                                       image_sha256=row['sha256'])
            return {'ram_boot': receipt, 'handoff': handoff['handoff'], 'write_result': 'not-started'}
    except BaseException as error:
        cleanup_confirmed = getattr(error, 'cleanup_confirmed', True)
        raise
    finally:
        try:
            if cleanup_confirmed:
                if claimed:
                    manager.artifacts.finish_claim(row['id'], context.id)
                if loader_workspace is not None:
                    executor.cleanup_plan(loader_workspace)
            else:
                actions.store.update_details(context.id, {'recovery_required': True})
        finally:
            maintenance.release(context.id, confirmed=cleanup_confirmed)
