"""Board-scoped flash coordination; reset, UART capture and writing stay separate."""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import secrets
import threading
import time

from .board_maintenance import maintenance_for
from .flash_store import FlashStore
from .results import response
from .tasks import TaskCancelled, TaskError
from .transactions import TransactionError


_INIT_LOCK = threading.Lock()


def _check_package_chip(binding, artifacts, preparations=None, actor='web'):
    """Catch an explicitly labelled Debian package for another SoC before USB inspection."""
    chip = (binding.profile_data or {}).get('chip_family')
    if chip not in ('k1', 'k3'):
        return
    for artifact in artifacts:
        name = artifact['name']
        job = preparations.latest(artifact['id'], actor) if preparations else None
        families = ((job or {}).get('result') or {}).get('preparation', {}).get('chip_families', [])
        if families:
            if chip not in families:
                raise TaskError(f'当前设备为 {chip.upper()}，镜像 {name} 的刷机配置支持 {", ".join(families).upper()}。请选择与设备匹配的镜像。', 409)
            continue
        declared = re.fullmatch(r'Debian-[0-9]+-[a-z0-9]+-(K[13])(?:-v[a-z0-9.]+)?(?:-[0-9]{8,14})?\.(?:tar\.gz|tgz|zip)', name, re.I)
        if declared and declared[1].lower() != chip:
            raise TaskError(f'当前设备为 {chip.upper()}，所选镜像 {name} 标注为 {declared[1].upper()}。请选择与设备匹配的镜像。', 409)


def _digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _services(manager):
    from .flash_runner import FlashBook, LocalFlashExecutor
    with _INIT_LOCK:
        if not hasattr(manager, 'flash_book'):
            manager.flash_book = FlashBook.from_env()
        if not hasattr(manager, 'flash_executor'):
            artifacts = getattr(manager, 'artifacts', None)
            if artifacts is None:
                raise TaskError('flash artifact service unavailable', 503)
            manager.flash_executor = LocalFlashExecutor(Path(artifacts.root).parent / 'flashing')
        if not hasattr(manager, '_flash_waiters'):
            manager._flash_waiters = {}
            manager._flash_waiters_lock = threading.RLock()
            manager._flash_plans_lock = threading.RLock()
            manager._flash_recovery_pins = {}
    return manager.flash_book, manager.flash_executor


def _public(document):
    from .flash_runner import public_plan
    return public_plan(document)


def _discovery(manager):
    from .flash_discovery import FlashDiscovery
    _, executor = _services(manager)
    with _INIT_LOCK:
        if not hasattr(manager, 'flash_discovery'):
            manager.flash_discovery = FlashDiscovery(manager, executor,
                getattr(manager, 'flash_discovery_argv', None))
    return manager.flash_discovery


def _binding_revision(binding):
    return binding.binding_revision


def _resources(binding):
    return list(binding.resources)


def _binding_digest(binding):
    return _digest(asdict(binding))


def _write_scope(plan):
    return [{key: value.get(key) for key in ('storage', 'partition', 'target', 'kind', 'sha256',
                                            'write_sha256', 'size', 'write_size', 'sector_size', 'offset')}
            for value in plan.get('writes', [])]


def _snapshot_inspection(record):
    """Use only the inspected NOR medium handled by the snapshot adapter."""
    inspection = record.get('inspection') or {}
    plan = record['executor_plan']
    if plan.get('image_type') == 'bundle':
        firmware = next((step['result'] for step in plan.get('steps', []) if step['label'] == 'Firmware'), {})
        inspection = firmware.get('inspection') or {}
    storages = inspection.get('storages') or []
    if (len(storages) == 1 and not storages[0].get('physical_device')
            and storages[0].get('capacity_source') == 'spacemit-ram-v1'
            and storages[0].get('verify') is True):
        return inspection
    return None


def _selection_layout_matches(record, selected, digest):
    plan = selected['executor_plan']
    if record.get('operation') == 'nor-workspace':
        if plan.get('image_type') == 'bundle':
            snapshot = _snapshot_inspection(selected) or {}
            if snapshot.get('layout_digest') == digest:
                return True
            before = record.get('inspection') or {}
            if (not selected['intent'].get('firmware_table') or before.get('layout_digest') != digest
                    or not (plan['steps'][0]['result'].get('execution_profile') or {}).get('nor_fill_tail')
                    or not snapshot or len(before.get('storages') or []) != 1):
                return False
            # The selected volatile NOR mapping may extend only the last uboot partition.
            old, new = before['storages'][0], snapshot['storages'][0]
            previous, current = old.get('partitions') or [], new.get('partitions') or []
            if not previous or not current or current[-1].get('name') != 'uboot':
                return False
            tail = current[-1]
            return (before.get('identity') == snapshot.get('identity')
                    and tail['offset'] + tail['size'] == new['size']
                    and old == dict(new, partitions=[*current[:-1], dict(tail, size=previous[-1]['size'])]))
        if (selected.get('execution_profile') or {}).get('system_storage'):
            return True
    return ((selected.get('inspection') or {}).get('layout_digest') or plan.get('layout_digest')) == digest


def _source_ids(intent):
    sources = {item['artifact_id'] for item in intent['selections']}
    sources.update(value for value in (intent['artifact_id'], intent.get('firmware_artifact_id')) if value)
    return sources


@contextmanager
def _release_when_stopped(manager, task_id):
    """Release a direct executor operation's board holds once it has ended.

    Holds stay in recovery when the executor reports that its process could
    not be confirmed stopped; a person must then confirm recovery.
    """
    maintenance = maintenance_for(manager)
    try:
        yield
    except BaseException as error:
        if getattr(error, 'cleanup_confirmed', True):
            maintenance.release(task_id, confirmed=True)
        else:
            maintenance.keep_recovery(task_id)
            FlashStore(manager.store).update_details(task_id, {'recovery_required': True})
        raise
    maintenance.release(task_id, confirmed=True)


class FlashActions:
    def __init__(self, manager):
        self.manager = manager
        self.store = FlashStore(manager.store) if manager.store is not None else None

    def _owned_task(self, task_id, actor, serial=None):
        from .task_actions import TaskActions
        if not task_id:
            raise TaskError('task not found', 404)
        visible = TaskActions(self.manager).query(task_id, actor, serial)
        if visible.status != 200:
            raise TaskError('task not found', visible.status)
        return visible.payload['task']

    def _device(self, device, payload, actor, require_present=True):
        if payload.get('target_id'):
            generation = payload.get('target_generation')
            if require_present and (not isinstance(generation, str) or not generation):
                raise TaskError('USB target generation is required')
            view = _discovery(self.manager).view(payload['target_id'],
                generation if require_present else None, require_present)
            if require_present and payload.get('serial') is not None and payload['serial'] != view.serial:
                raise TaskError('USB flash target board identity changed', 409)
            return view
        return device

    def discover(self, actor='web'):
        try:
            return response(200, _discovery(self.manager).refresh())
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def probe(self, payload, actor='web'):
        try:
            if (not isinstance(payload, dict) or set(payload) - {
                    'target_id', 'target_generation', 'actor', 'client_id', 'serial'}):
                raise TaskError('invalid USB capability detection request')
            self._device(None, payload, actor)
            discovery = _discovery(self.manager)
            target = discovery.probe(payload.get('target_id'), payload.get('target_generation'))
            self._device(None, dict(payload, target_generation=target['target_generation']), actor)
            capabilities = self.capabilities(None, actor, target['target_id'], target['target_generation'])
            if capabilities.status != 200:
                return capabilities
            return response(200, {'ok': True, 'target': target, 'capabilities': capabilities.payload})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            result = {'ok': False, 'error': str(error)}
            if getattr(error, 'recovery_task_id', None):
                result.update(task_id=error.recovery_task_id, recovery_required=True)
            return response(getattr(error, 'status', 503), result)

    def waiting_payload_serial(self, payload, actor='web'):
        """Validate host-only payload imports under the original prepared task."""
        task = self._owned_task(payload.get('task_id'), actor, payload.get('serial'))
        snapshot = next((value for value in task.get('flash_targets', [])
            if value['target_id'] == payload.get('target_id')
            and value['generation'] == payload.get('target_generation')), None)
        if task['state'] != 'running' or task['progress'].get('phase') != 'awaiting-selection' or not snapshot:
            raise TaskError('flash task is not waiting for a payload selection', 409)
        _services(self.manager)
        with self.manager._flash_waiters_lock:
            waiter = self.manager._flash_waiters.get(task['id'])
        if waiter is None:
            raise TaskError('flash selection is no longer available', 409)
        with waiter['lock']:
            waiter['operation'].check()
            if waiter.get('closed') or waiter.get('selected') is not None or time.time() >= waiter['deadline']:
                raise TaskError('flash selection is no longer available', 409)
            holds = maintenance_for(self.manager).target_holds(payload['target_id'])
            if not holds or any(row['task_id'] != task['id'] or row['state'] != 'active' for row in holds):
                raise TaskError('payload import does not belong to the active flash task', 409)
            serial = waiter['record']['serial']
            if payload.get('serial') is not None and payload['serial'] != serial:
                raise TaskError('flash payload board identity changed', 409)
            return serial

    def artifact_create(self, payload, actor='web'):
        try:
            if not isinstance(payload, dict):
                raise TaskError('JSON object required')
            if payload.get('task_id'):
                task = self._owned_task(payload['task_id'], actor, payload.get('serial'))
                snapshot = next((value for value in task.get('flash_targets', [])
                    if value['target_id'] == payload.get('target_id')
                    and value['generation'] == payload.get('target_generation')), None)
                if task['state'] != 'running' or task['progress'].get('phase') != 'awaiting-selection' or not snapshot:
                    raise TaskError('flash task is not waiting for a payload selection', 409)
                with self.manager._flash_waiters_lock:
                    waiter = self.manager._flash_waiters.get(task['id'])
                if waiter is None:
                    raise TaskError('flash selection is no longer available', 409)
                with waiter['lock']:
                    operation = waiter['operation']
                    operation.check()
                    if waiter.get('closed') or waiter.get('selected') is not None:
                        raise TaskError('flash selection is no longer available', 409)
                    holds = maintenance_for(self.manager).target_holds(payload['target_id'])
                    if not holds or any(value['task_id'] != task['id'] or value['state'] != 'active' for value in holds):
                        raise TaskError('payload upload does not belong to the active flash task', 409)
                    resource = 'flash-usb:local:' + '.'.join(map(str, operation.binding.device['port_path']))
                    with self.manager.store.lock:
                        usb_hold = self.manager.store.db.execute(
                            'SELECT task_id,state FROM flash_resource_holds WHERE resource=?', (resource,)).fetchone()
                    if usb_hold is None or usb_hold['task_id'] != task['id'] or usb_hold['state'] != 'active':
                        raise TaskError('flash task no longer owns its physical USB attachment', 409)
                    artifact = self.manager.artifacts.create('upload', payload.get('name'), payload.get('size'),
                                                             actor, waiter['record']['serial'])
                return response(200, {'ok': True, 'artifact': artifact})
            device = self._device(None, payload, actor)
            if device is None:
                raise TaskError('USB flash target is required')
            artifact = self.manager.artifacts.create('upload', payload.get('name'), payload.get('size'),
                                                     actor, device.serial)
            return response(200, {'ok': True, 'artifact': artifact})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def _target(self, device, actor, payload=None):
        if payload and payload.get('target_id'):
            device = self._device(device, payload, actor)
            if device.serial and self.manager.store.has_conflict(device.serial):
                raise TaskError('resolve the board identity conflict before flashing', 409)
            if payload.get('mode') and payload['mode'] not in device.row['document'].get('modes', []):
                raise TaskError('selected USB device does not provide that flashing mode', 409)
            return _discovery(self.manager).binding(device.target_id, device.target_generation), _services(self.manager)[1]
        if device is None or not device.serial or device.serial not in self.manager.boards:
            raise TaskError('flashing requires a registered board', 404)
        if payload is not None and payload.get('serial') != device.serial:
            raise TaskError('flash board identity changed', 409)
        if self.manager.store.has_conflict(device.serial):
            raise TaskError('resolve the board identity conflict before flashing', 409)
        if not isinstance(actor, str) or not actor or len(actor) > 80:
            raise TaskError('invalid flash actor')
        book, executor = _services(self.manager)
        binding = book.binding(device.serial, actor)
        return binding, executor

    def capabilities(self, device, actor='web', target_id=None, target_generation=None, serial=None):
        try:
            target = None
            if target_id is not None:
                discovery = _discovery(self.manager)
                row = discovery.get(target_id, target_generation)
                device = discovery.view(target_id, target_generation)
                if serial is not None and device.serial != serial:
                    raise TaskError('USB flash target board identity changed', 403)
                target = discovery.public(row)
                data = row['document']
                result = dict(target, board_serial=device.serial, binding_revision=1,
                    profile=(data.get('profile') or {}).get('id', ''),
                    control={'provider': 'manual', 'node': 'local'}, console={'provider': 'none'},
                    executor={'provider': 'goflash', 'node': 'local', 'endpoint': 'auto-' + target_id},
                    storages=(data.get('inspection') or {}).get('storages', []),
                    modes=data.get('modes') or ['partitions'], verification=['if-supported', 'required', 'none'],
                    can_upload=True, reset_required=False)
                result['preparable'] = data.get('preparable') is True
                result['can_prepare_from_image'] = result['preparable']
                result['readback_supported'] = bool(result['storages']) and all(
                    storage.get('verify') is True for storage in result['storages'])
                result['verification'] = {'required': result['readback_supported']}
                profile = data.get('profile') or {}
                if result['preparable']:
                    from .flash_loaders import installed_for
                    result['loader_sources'] = (['builtin'] if installed_for(profile.get('chip_family')) else []) + ['image']
                result['nor_workspace_supported'] = bool(
                    getattr(_services(self.manager)[1], 'supports_nor_snapshot', False)
                    and profile.get('id') == 'auto-spacemit-gpt-v1' and profile.get('chip_family') == 'k3'
                    and (profile.get('ram_loader_protocol') == 'spacemit-ram-v1' or result['preparable']))
                result['stream_flash_supported'] = bool(profile.get('id') == 'auto-spacemit-gpt-v1'
                    and profile.get('chip_family') in ('k1', 'k3')
                    and getattr(_services(self.manager)[1], 'supports_stream_flash', False))
                result['calibrate_supported'] = bool(result['stream_flash_supported'] and profile.get('chip_family') == 'k3'
                    and getattr(_services(self.manager)[1], 'supports_calibrate', False))
                if result['calibrate_supported']:
                    try:
                        configuration = _services(self.manager)[1].tuning(
                            SimpleNamespace(profile_data=profile, device=data['device']))
                    except (OSError, ValueError, KeyError):
                        configuration = None
                    result['flash_configuration'] = configuration
                result['storage_inventory_supported'] = bool(result['nor_workspace_supported']
                    and getattr(_services(self.manager)[1], 'supports_storage_metadata', False))
                if device.serial and self.manager.store.has_conflict(device.serial):
                    result.update(supported=False, reason='conflicting USB board identity')
            else:
                if device is None or not device.serial:
                    raise TaskError('flashing requires a registered board', 404)
                book, _ = _services(self.manager)
                result = dict(book.available(device.serial, actor))
            result.setdefault('board_serial', device.serial)
            result['reset_after_supported'] = bool(getattr(_services(self.manager)[1], 'supports_reset_after', False))
            result['skip_verification_supported'] = bool(getattr(_services(self.manager)[1], 'supports_skip_verification', False))
            result['image_preparation_supported'] = bool(getattr(_services(self.manager)[1], 'supports_image_preparation', False))
            result['artifacts'] = []
            artifacts = self.manager.artifacts
            with self.manager.store.lock:
                rows = self.manager.store.db.execute(
                    ("SELECT id FROM transfer_artifacts WHERE direction='upload' AND state IN ('ready','claimed') " if actor == 'web' else
                     "SELECT id FROM transfer_artifacts WHERE serial=? AND direction='upload' AND state='ready' ")
                    + ('' if actor == 'web' else 'AND actor=? ') + 'ORDER BY updated DESC LIMIT 100',
                    () if actor == 'web' else (device.serial, actor)).fetchall()
            for row in rows:
                metadata = artifacts.get(row['id'], actor, None if actor == 'web' else device.serial)
                item = {key:metadata[key] for key in ('id','name','size','sha256')}
                from .image_workspaces import public_directory_info
                item.update(public_directory_info(artifacts._path(metadata['id'], '.bin')))
                result['artifacts'].append(item)
            result['maintenance'] = (maintenance_for(self.manager).target_holds(target_id) if target_id
                                     else maintenance_for(self.manager).holds(device.serial))
            return response(200, result)
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    @staticmethod
    def _intent(payload):
        if type(payload.get('reset_after', False)) is not bool:
            raise TaskError('invalid reset after flash option')
        mode, verify = payload.get('mode', 'full'), payload.get('verify', 'if-supported')
        if mode not in ('full', 'partitions') or verify not in ('required', 'if-supported', 'none'):
            raise TaskError('invalid flash mode or verification policy')
        artifact_id, selections = payload.get('artifact_id'), payload.get('selections', [])
        target_storage = payload.get('target_storage', '')
        if (not isinstance(target_storage, str) or len(target_storage.encode()) > 128
                or mode != 'full' and target_storage and not re.fullmatch(r'(nvme|scsi|mmc|usb):[0-3]', target_storage)):
            raise TaskError('invalid full disk target storage')
        if not isinstance(artifact_id, str) or not re.fullmatch(r'[0-9a-f]{24}', artifact_id):
            raise TaskError('invalid flash artifact')
        if not isinstance(selections, list) or len(selections) > 128:
            raise TaskError('invalid flash selections')
        normalized, seen = [], set()
        for selection in selections:
            if (not isinstance(selection, dict) or set(selection) - {'storage', 'partition', 'artifact_id'}
                    or any(not isinstance(selection.get(key), str) or not selection[key]
                           or len(selection[key].encode()) > 128 for key in ('storage', 'partition'))):
                raise TaskError('invalid flash partition selection')
            source = selection.get('artifact_id', artifact_id)
            if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{24}', source):
                raise TaskError('invalid partition artifact')
            target = (selection['storage'], selection['partition'])
            if target in seen:
                raise TaskError('duplicate flash partition selection')
            seen.add(target)
            normalized.append(dict(storage=target[0], partition=target[1], artifact_id=source))
        if mode == 'full' and normalized:
            raise TaskError('full image mode does not accept partition selections')
        companion = payload.get('firmware_artifact_id')
        if companion is not None and (mode != 'full' or not isinstance(companion, str) or not re.fullmatch(r'[0-9a-f]{24}', companion) or companion == artifact_id):
            raise TaskError('invalid companion firmware image')
        result = {'artifact_id': artifact_id, 'mode': mode, 'selections': normalized, 'verify': verify,
                  'target_storage': target_storage}
        table = payload.get('firmware_table')
        if table is not None:
            if (mode != 'full' or companion or not isinstance(table, str)
                    or not re.fullmatch(r'partition_[a-zA-Z0-9_-]{1,80}\.json', table) or table == 'partition_flash.json'):
                raise TaskError('请选择 OS 包内的一张 NOR 分区表')
            result['firmware_table'] = table
        if companion:
            result['firmware_artifact_id'] = companion
        if 'loader_source' in payload:
            if payload['loader_source'] not in ('builtin', 'image'):
                raise TaskError('invalid flash loader source')
            result['loader_source'] = payload['loader_source']
        if payload.get('reset_after'):
            result['reset_after'] = True
        return result

    def _artifacts(self, intent, actor, serial, stack, task_id=None, check=lambda: None, on_event=None):
        output = {}
        for artifact_id in sorted(_source_ids(intent)):
            row = self.manager.artifacts.get(artifact_id, actor, None if actor == 'web' else serial)
            if row['direction'] != 'upload' or row['state'] not in ('ready', 'claimed'):
                raise TaskError('flash image is not sealed and ready', 409)
            source = stack.enter_context(self.manager.artifacts.open(artifact_id, actor, None if actor == 'web' else serial, task_id))
            if getattr(self.manager.flash_executor, 'supports_image_preparation', False):
                # The host preparation task verifies this source once; cached
                # previews validate its seal and members instead of rescanning.
                output[artifact_id] = dict(id=artifact_id, name=row['name'], size=row['size'], sha256=row['sha256'],
                    path=str(self.manager.artifacts._path(artifact_id, '.bin')))
                continue
            digest, done, last = hashlib.sha256(), 0, 0
            while True:
                check()
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                if on_event and (time.monotonic() - last >= .25 or done == row['size']):
                    on_event({'event': 'progress', 'phase': 'checking-upload', 'done': done, 'total': row['size']})
                    last = time.monotonic()
            if digest.hexdigest() != row['sha256']:
                raise TaskError('flash image checksum changed', 409)
            output[artifact_id] = dict(id=artifact_id, name=row['name'], size=row['size'],
                                       sha256=row['sha256'], path=str(self.manager.artifacts._path(artifact_id, '.bin')))
        return output

    def _make_plan(self, binding, executor, intent, actor, serial, check=lambda: None, task_id=None,
                   inspection_scope=None, prepared=None, previous_device=None, baseline_record=None, on_event=None):
        if intent.get('reset_after') and not getattr(executor, 'supports_reset_after', False):
            raise TaskError('刷机执行器不支持刷后自动复位')
        if prepared is not None and hasattr(executor, 'check_plan'):
            check()
            selections = None
            if intent['selections'] != prepared['intent']['selections']:
                selections = [dict(storage=value['storage'], partition=value['partition'])
                              for value in intent['selections']]
            arguments = {'previous_device': previous_device} if previous_device is not None else {}
            checked_binding = binding
            if prepared.get('execution_profile'):
                checked_binding = executor.execution_binding(binding, prepared['execution_profile'], prepared['workspace'], check)
            if prepared['executor_plan'].get('image_type') == 'bundle':
                from .flash_bundle import check_bundle
                result = check_bundle(executor, checked_binding, prepared['executor_plan'], check, previous_device)
            else:
                result = executor.check_plan(checked_binding, prepared['executor_plan'],
                    prepared.get('executor_candidates', []), check, selections=selections, **arguments)
            result['workspace'] = prepared.get('workspace')
            if binding.target_id:
                result['execution_device'] = dict(binding.device)
        else:
            with ExitStack() as stack:
                artifacts = self._artifacts(intent, actor, serial, stack, task_id, check, on_event)
                _check_package_chip(binding, artifacts.values(), getattr(self.manager, 'image_preparations', None), actor)
                selections = [dict(storage=value['storage'], partition=value['partition'],
                                   artifact=artifacts[value['artifact_id']]) for value in intent['selections']]
                check()
                arguments = {'target_storage': intent['target_storage']} if intent.get('target_storage') else {}
                if intent.get('loader_source'):
                    arguments['loader_source'] = intent['loader_source']
                if on_event is not None:
                    arguments['on_event'] = on_event
                if inspection_scope is not None:
                    arguments['inspection_scope'] = inspection_scope
                try:
                    if intent.get('firmware_artifact_id') or intent.get('firmware_table'):
                        from .flash_bundle import plan_bundle
                        result = plan_bundle(executor, binding, artifacts[intent['artifact_id']], artifacts[intent.get('firmware_artifact_id') or intent['artifact_id']], intent['mode'], intent['verify'], check, firmware_table=intent.get('firmware_table'), **arguments)
                    else:
                        result = executor.plan(binding, artifacts[intent['artifact_id']], intent['mode'], selections,
                                               intent['verify'], check, **arguments)
                except BaseException as error:
                    if getattr(error, 'cleanup_confirmed', True) is False:
                        # Keep source files pinned until a durable task owns them.
                        error.artifact_pins = stack.pop_all()
                        error.artifact_ids = sorted(artifacts)
                    raise
        try:
            return self._store_plan(binding, intent, actor, serial, result, task_id, prepared or baseline_record, check)
        except BaseException:
            if (prepared is None and isinstance(result, dict) and result.get('workspace')
                    and hasattr(executor, 'cleanup_plan')):
                try:
                    executor.cleanup_plan(result)
                except (TaskError, OSError):
                    pass
            raise

    def _store_plan(self, binding, intent, actor, serial, result, task_id, prepared=None, check=lambda: None):
        if not isinstance(result, dict):
            raise TaskError('invalid flash executor plan', 502)
        if type(result.get('requires_prepare', False)) is not bool:
            raise TaskError('invalid flash preparation requirement', 502)
        plan = result.get('plan', result)
        if not isinstance(plan, dict) or not isinstance(plan.get('writes', []), list):
            raise TaskError('invalid flash write plan', 502)
        if any(not isinstance(value, dict) for value in plan.get('writes', [])):
            raise TaskError('invalid flash write entry', 502)
        candidates = result.get('payload_candidates')
        if candidates is None:
            candidates = []
        if not isinstance(candidates, list):
            raise TaskError('invalid flash payload candidates', 502)
        if intent['mode'] == 'partitions' and (plan.get('write_layout') is not False
                or any(item.get('kind') in ('layout', 'disk') for item in plan.get('writes', []))):
            raise TaskError('partition plan attempted to change disk layout', 502)
        if intent['mode'] == 'partitions' and intent['selections']:
            expected = {(value['storage'], value['partition']) for value in intent['selections']}
            actual = [(value.get('storage'), value.get('partition')) for value in plan.get('writes', [])]
            if len(actual) != len(expected) or set(actual) != expected:
                raise TaskError('flash plan differs from selected partition scope', 502)
        source_workspaces = set((prepared or {}).get('source_workspaces') or [])
        source_workspaces.update(result.get('source_workspaces') or [])
        if (prepared or {}).get('workspace') and prepared['workspace'] != result.get('workspace'):
            source_workspaces.add(prepared['workspace'])
        metadata = result.get('payload_metadata') or []
        if not isinstance(metadata, list):
            raise TaskError('invalid flash payload metadata', 502)
        profile = result.get('execution_profile') or (prepared or {}).get('execution_profile') or {}
        backup = self.store.details(task_id).get('nor_before') if task_id else None
        storages = (result.get('inspection') or (prepared or {}).get('inspection') or {}).get('storages') or []
        if backup and not profile.get('system_storage') and not any(value.get('physical_device') for value in storages):
            from .nor_backups import compare_payloads
            metadata = compare_payloads(self.manager.artifacts, backup, metadata, actor, serial, check)
        return self.store.put_plan({
            'actor': actor, 'serial': serial, 'binding_revision': _binding_revision(binding),
            'target_id': binding.target_id or None, 'target_generation': binding.target_generation or None,
            'binding_digest': (prepared or {}).get('binding_digest') or _binding_digest(binding), 'intent': intent,
            'executor_plan': plan, 'requires_prepare': result.get('requires_prepare', False),
            'executor_candidates': candidates,
            'execution_profile': result.get('execution_profile') or (prepared or {}).get('execution_profile'),
            'execution_device': result.get('execution_device') or (prepared or {}).get('execution_device'),
            'source_workspaces': sorted(source_workspaces),
            'inspection': result.get('inspection'),
            'payload_metadata': metadata,
            'pending_checks': result.get('pending_checks', plan.get('pending_checks', [])),
            'payload_candidates': [dict(artifact_id=intent['artifact_id'],
                name=str(value.get('name') or (str(value['storage']) + '/' + str(value['partition'])
                    if value.get('storage') and value.get('partition') else plan.get('artifact', {}).get('name', intent['artifact_id']))),
                size=value.get('size', 0), sha256=value.get('sha256', ''),
                storage=value.get('storage', ''), partition=value.get('partition', ''))
                for value in candidates if isinstance(value, dict)],
            'created': time.time(),
            'selection_task_id': task_id, 'workspace': result.get('workspace')})

    @staticmethod
    def _preview(record):
        result = _public(record['executor_plan'])
        if record['intent'].get('firmware_table'):
            result['firmware_table'] = record['intent']['firmware_table']
        if record['intent'].get('firmware_artifact_id'):
            result['firmware_artifact_id'] = record['intent']['firmware_artifact_id']
        if record['intent'].get('loader_source'):
            result['loader_source'] = record['intent']['loader_source']
        if record.get('workspace') and record['executor_plan'].get('ready') and _snapshot_inspection(record):
            result['can_backup_nor'] = True
        result.setdefault('storage_layouts', _public((record.get('inspection') or {}).get('storages', [])))
        result.update(id=record['plan_id'], plan_id=record['plan_id'], digest=record['digest'],
                      binding_revision=record['binding_revision'],
                      target_id=record.get('target_id'), target_generation=record.get('target_generation'),
                      requires_prepare=record['requires_prepare'], pending_checks=record['pending_checks'],
                      payload_metadata=record.get('payload_metadata', []), reset_after=record['intent'].get('reset_after', False))
        return result

    def plan(self, payload, device, actor='web', check=None, on_event=None):
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'key', 'serial', 'actor', 'client_id', 'binding_revision',
                    'artifact_id', 'firmware_artifact_id', 'firmware_table', 'mode', 'selections', 'verify', 'target_storage', 'reset_after', 'loader_source',
                    'target_id', 'target_generation', 'request_id'}:
                raise TaskError('invalid flash plan request')
            device = self._device(device, payload, actor)
            binding, executor = self._target(device, actor, payload)
            if 'binding_revision' in payload and (type(payload['binding_revision']) is not int
                    or payload['binding_revision'] != _binding_revision(binding)):
                raise TaskError('flash binding changed; refresh capabilities', 409)
            intent = self._intent(payload)
            client = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            maintenance = maintenance_for(self.manager)
            if payload.get('request_id') is not None:
                if actor != 'web':
                    raise TaskError('asynchronous flash preview requires a Web operator', 403)
                from .flash_store import validate_request
                original = {key: value for key, value in payload.items() if key != 'request_id'}
                digest = _digest({'kind': 'flash-preview', 'input': original, 'serial': device.serial})
                validate_request(payload['request_id'], digest)
                def run(context):
                    from .flash_progress import PreviewProgress
                    source = self.manager.artifacts.get(intent['artifact_id'], actor, None if actor == 'web' else device.serial)
                    self.store.initialize_details(context.id, {
                        'preview_intent': intent,
                        'preview_source': {key: source[key] for key in ('id', 'name', 'size', 'sha256')}})
                    if intent.get('firmware_artifact_id'):
                        firmware = self.manager.artifacts.get(intent['firmware_artifact_id'], actor, None if actor == 'web' else device.serial)
                        self.store.update_details(context.id, {'preview_firmware':{key:firmware[key] for key in ('id','name','size','sha256')}})
                    timing = PreviewProgress(self.store, context.id, source, binding.endpoint,
                        source['name'].lower().endswith(('.gz', '.tgz', '.zip')))
                    def progress(value):
                        timing.event(value)
                        context.progress(value.get('done', 0), value.get('total', 0), value.get('phase', 'checking-image'))
                    success = False
                    try:
                        result = self.plan(original, device, actor, context.check, progress)
                        success = result.status == 200
                    finally:
                        timing.finish(success)
                    if result.status != 200:
                        context.store.update(context.id, result=result.payload)
                        raise TaskError(result.payload['error'], result.status)
                    context.mark_outcome('observed')
                    return result.payload
                target = (binding.target_id, binding.target_generation) if binding.target_id else None
                task = self.manager.tasks.start('flash-preview', actor, client,
                    [{'serial': device.serial or '', 'connection_id': getattr(device, 'connection_id', '') or 'flash-preview'}],
                    run, request_id=payload['request_id'], input_digest=digest, tracks_outcome=True, flash_target=target)
                return response(202, {'ok': True, 'task': task})
            def preparation_check():
                if check is not None:
                    check()
                maintenance.check_preparation()
            @contextmanager
            def inspect_scope():
                guard = (maintenance.inspect_target_scope(device.target_id, device.target_generation,
                    device.serial, _resources(binding), actor, client) if binding.target_id else
                    maintenance.inspect_scope(device.serial, _resources(binding), actor, client))
                with guard:
                    try:
                        yield
                    except BaseException as error:
                        if getattr(error, 'cleanup_confirmed', True) is False:
                            details = {'executor': binding.endpoint, 'binding_revision': _binding_revision(binding)}
                            task = (maintenance.retain_target_inspection_failure(binding.target_id,
                                binding.target_generation, device.serial, _resources(binding), actor, client,
                                attempt_id=secrets.token_hex(12), details=details) if binding.target_id else
                                maintenance.retain_inspection_failure(device.serial, _resources(binding), actor,
                                    client, attempt_id=secrets.token_hex(12), details=details))
                            error.recovery_task_id = task['id']
                        raise
            try:
                preparation_check()
                record = self._make_plan(binding, executor, intent, actor, device.serial,
                                         preparation_check, inspection_scope=inspect_scope, on_event=on_event)
            except BaseException as error:
                if getattr(error, 'cleanup_confirmed', True) is False:
                    task_id = getattr(error, 'recovery_task_id', None)
                    if task_id is None:
                        details = {'executor': binding.endpoint, 'binding_revision': _binding_revision(binding)}
                        task_id = (maintenance.retain_target_preparation_failure(binding.target_id,
                            binding.target_generation, device.serial, actor, client,
                            attempt_id=secrets.token_hex(12), details=details) if binding.target_id else
                            maintenance.retain_preparation_failure(device.serial, actor, client,
                                attempt_id=secrets.token_hex(12), details=details))['id']
                    self.store.update_details(task_id, {'workspace': getattr(error, 'workspace', None)})
                    self._retain_sources(error, task_id, actor, device.serial)
                    return response(getattr(error, 'status', 503), {
                        'ok': False, 'error': str(error), 'task_id': task_id, 'recovery_required': True})
                raise
            return response(200, {'ok': True, 'plan': self._preview(record),
                                  'requires_prepare': record['requires_prepare'],
                                  'inspection': _public(record.get('inspection') or {}),
                                  'pending_checks': record['pending_checks']})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def _retain_sources(self, error, task_id, actor, serial, claimed=None):
        claimed = [] if claimed is None else claimed
        pins = getattr(error, 'artifact_pins', None)
        try:
            for artifact_id in getattr(error, 'artifact_ids', []):
                if artifact_id not in claimed:
                    row = self.manager.artifacts.get(artifact_id, actor, None if actor == 'web' else serial)
                    if row['state'] != 'claimed' or row['task_id'] != task_id:
                        self.manager.artifacts.claim(artifact_id, task_id, actor, None if actor == 'web' else serial)
                    claimed.append(artifact_id)
        except BaseException:
            # A registry failure must not close the only remaining expiry pin.
            if pins:
                self.manager._flash_recovery_pins[task_id] = pins
            raise
        else:
            if pins:
                pins.close()

    def backup(self, payload, device, actor='web'):
        """Start one durable read/selection/write task, initially without images."""
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'kind', 'key', 'serial', 'actor', 'client_id', 'request_id',
                    'binding_revision', 'target_id', 'target_generation', 'read_system', 'temporary_snapshot'}:
                raise TaskError('NOR backup accepts only the fixed USB target')
            _services(self.manager)
            read_system = payload.get('read_system', False)
            if type(read_system) is not bool:
                raise TaskError('read_system must be boolean')
            identity = {key: payload.get(key) for key in ('serial', 'binding_revision', 'target_id', 'target_generation')}
            temporary = payload.get('temporary_snapshot', False)
            if type(temporary) is not bool:
                raise TaskError('temporary_snapshot must be boolean')
            if temporary:
                identity['temporary_snapshot'] = True
            if read_system:
                identity['read_system'] = True
            input_digest = _digest(dict(identity, operation='nor-workspace'))
            with self.manager._flash_plans_lock:
                previous = self.store.find_request(actor, payload.get('request_id'), input_digest)
                if previous:
                    return response(202, {'ok': True, 'task': self.manager.tasks.store.get(previous)})
                device = self._device(device, payload, actor)
                binding, executor = self._target(device, actor, payload)
                if read_system and not getattr(executor, 'supports_storage_metadata', False):
                    raise TaskError('system storage metadata requires an updated executor', 409)
                if (not binding.target_id or binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                        or binding.profile_data.get('chip_family') != 'k3'
                        or not getattr(executor, 'supports_nor_snapshot', False)):
                    raise TaskError('NOR backup requires a detected K3 USB target and updated executor', 409)
                if (type(payload.get('binding_revision')) is not int
                        or payload['binding_revision'] != binding.binding_revision):
                    raise TaskError('flash binding changed; refresh capabilities', 409)
                client = payload.get('client_id', '')
                if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                    raise TaskError('invalid flash client')
                record = self.store.put_plan({
                    'operation': 'nor-workspace', 'actor': actor, 'serial': device.serial,
                    'read_system': read_system,
                    'target_id': binding.target_id, 'target_generation': binding.target_generation,
                    'binding_revision': binding.binding_revision, 'binding_digest': _binding_digest(binding),
                    'intent': {'artifact_id': None, 'mode': 'partitions', 'verify': 'required',
                               'selections': [], 'target_storage': ''},
                    'executor_plan': {'version': 1, 'mode': 'partitions', 'image_type': 'nor-workspace',
                        'ready': False, 'write_layout': False, 'writes': [], 'verify': 'required'},
                    'requires_prepare': False, 'pending_checks': ['read-nor', 'partition-selection'],
                    'payload_candidates': [], 'executor_candidates': [], 'inspection': None,
                    'workspace': None, 'source_workspaces': [], 'created': time.time()})
                operation = FlashOperation(self, binding, executor, record, actor)
                task = self.manager.tasks.start_board('flash', actor, client, device.serial, operation.run,
                    request_id=payload.get('request_id'), input_digest=input_digest,
                    details={'operation': 'nor-workspace', 'temporary_snapshot': temporary, 'plan_id': record['plan_id'],
                        'plan_digest': record['digest'], 'binding_revision': binding.binding_revision,
                        'target_id': binding.target_id, 'target_generation': binding.target_generation,
                        'attempt_id': operation.attempt_id, 'executor': binding.endpoint,
                        'write_result': 'not-started', 'verify_result': 'not-started',
                        'boot_result': 'not-requested'}, resources=_resources(binding),
                    maintenance=maintenance_for(self.manager), target_id=binding.target_id,
                    target_generation=binding.target_generation)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def stream(self, payload, device, actor='web'):
        """Fast mode: stream an HTTP(S) K1/K3 system package straight to the disk.

        Download, decompression and writing overlap in the executor's memory;
        nothing is staged on the host disk. The executor writes the package
        GPT first and checks member digests against SHA256SUMS at the end.
        """
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'actor', 'client_id', 'request_id', 'target_id', 'target_generation', 'serial',
                    'url', 'sha256', 'system_storage', 'reset_after', 'memory_budget'}:
                raise TaskError('invalid fast flashing request')
            from .image_download import image_url
            parsed = image_url(payload.get('url'))
            if not parsed.path.lower().endswith(('.tar.gz', '.tgz', '.tar')):
                raise TaskError('fast flashing streams .tar.gz or .tar system packages')
            checksum = payload.get('sha256') or ''
            if checksum and not re.fullmatch('[a-f0-9]{64}', checksum):
                raise TaskError('invalid expected image SHA-256')
            storage = payload.get('system_storage') or 'auto'
            if storage not in ('auto', 'active') and not re.fullmatch(r'(nvme|scsi|mmc|usb):[0-3]', storage):
                raise TaskError('invalid system disk')
            reset_after = payload.get('reset_after', True)
            budget = payload.get('memory_budget', 1 << 30)
            if type(reset_after) is not bool or type(budget) is not int or not 128 << 20 <= budget <= 64 << 30:
                raise TaskError('invalid fast flashing options')
            client = payload.get('client_id', '')
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            _services(self.manager)  # initializes the shared plan lock on a fresh service
            identity = {key: payload.get(key) for key in ('target_id', 'target_generation', 'url', 'sha256')}
            identity.update(operation='stream', system_storage=storage, reset_after=reset_after, memory_budget=budget)
            input_digest = _digest(identity)
            with self.manager._flash_plans_lock:
                previous = self.store.find_request(actor, payload.get('request_id'), input_digest)
                if previous:
                    return response(202, {'ok': True, 'task': self.manager.tasks.store.get(previous)})
                if not payload.get('target_id'):
                    raise TaskError('fast flashing requires a detected USB target')
                device = self._device(device, payload, actor)
                binding, executor = self._target(device, actor, payload)
                family = binding.profile_data.get('chip_family')
                if (binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                        or family not in ('k1', 'k3')
                        or not getattr(executor, 'supports_stream_flash', False)):
                    raise TaskError('fast flashing requires a detected K1/K3 USB target and updated executor', 409)
                if family == 'k1' and storage not in ('active', 'auto'):
                    raise TaskError('K1 使用 Fastboot 当前系统盘')
                if family == 'k3' and storage == 'active':
                    raise TaskError('K3 需要选择实际系统盘或自动检测')
                url, store = payload['url'], self.store
                def run(context):
                    from .flash_loaders import prepare_materials
                    request = {'version': 1, 'device': binding.device, 'chip_family': family, 'source_url': url,
                               'system_storage': storage, 'reset_after': reset_after, 'memory_budget': budget}
                    if actor != 'web':
                        request['restrict_source'] = True
                    if checksum:
                        request['archive_sha256'] = checksum
                    if binding.profile_data.get('bootrom_identity'):
                        materials = prepare_materials(binding, executor.stream_loader_workspace(), context.check,
                                                      getattr(executor, 'root', None), url_stream=family == 'k3')
                        request['loader'] = {key: {name: materials[key][name] for name in ('id', 'name', 'path', 'size', 'sha256')}
                                             for key in ('fsbl', 'uboot')}
                        if materials.get('trained_for'):
                            request['loader']['trained_for'] = materials['trained_for']
                        if materials.get('usb_speed'):
                            request['loader']['usb_speed'] = materials['usb_speed']
                    started = {'writing': False}
                    def event(value):
                        store.append_event(context.id, value)
                        phase = value.get('phase', 'flash')
                        started['writing'] = started['writing'] or phase in ('writing-gpt', 'writing')
                        done, total = value.get('done', 0), value.get('total', 0)
                        if type(done) is int and type(total) is int and 0 <= done <= max(total, done):
                            context.progress(done, max(total, done), phase)
                    try:
                        with _release_when_stopped(self.manager, context.id):
                            result = executor.stream_flash(binding, request, event, context.check)
                    except BaseException:
                        store.update_details(context.id, {'write_result': 'partial-or-unknown' if started['writing'] else 'not-started'})
                        raise
                    store.update_details(context.id, {'write_result': result.get('write_result', 'completed'),
                        'verify_result': result.get('verify_result', 'not-requested'),
                        'boot_result': result.get('boot_result', 'not-requested')})
                    return result
                task = self.manager.tasks.start_board('flash', actor, client, device.serial, run,
                    request_id=payload.get('request_id'), input_digest=input_digest,
                    details={'operation': 'stream', 'source_url': url, 'system_storage': storage,
                        'target_id': binding.target_id, 'target_generation': binding.target_generation,
                        'executor': binding.endpoint, 'write_result': 'not-started',
                        'verify_result': 'not-started', 'boot_result': 'not-requested'},
                    resources=_resources(binding), maintenance=maintenance_for(self.manager),
                    target_id=binding.target_id, target_generation=binding.target_generation)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def calibrate(self, payload, device, actor='web'):
        """Measure a K3 board connection and keep its flashing configuration.

        Only RAM and an unchanged rewrite of one disk range are touched; the
        board's storage content is the same afterwards.
        """
        try:
            if set(payload) - {'key', 'serial', 'actor', 'client_id', 'request_id', 'target_id', 'target_generation',
                               'system_storage', 'measure_write', 'action'}:
                raise TaskError('unsupported calibration option')
            action = payload.get('action', 'measure')
            if action not in ('measure', 'forget'):
                raise TaskError('invalid calibration action')
            storage = payload.get('system_storage') or 'auto'
            if storage != 'auto' and not re.fullmatch(r'(nvme|scsi|mmc|usb):[0-3]', storage):
                raise TaskError('invalid system disk')
            measure_write = payload.get('measure_write', True)
            if type(measure_write) is not bool:
                raise TaskError('measure_write must be boolean')
            client = payload.get('client_id', '')
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            _services(self.manager)
            identity = {key: payload.get(key) for key in ('target_id', 'target_generation')}
            identity.update(operation='calibrate', system_storage=storage, measure_write=measure_write)
            input_digest = _digest(identity)
            with self.manager._flash_plans_lock:
                if action == 'measure':
                    previous = self.store.find_request(actor, payload.get('request_id'), input_digest)
                    if previous:
                        return response(202, {'ok': True, 'task': self.manager.tasks.store.get(previous)})
                if not payload.get('target_id'):
                    raise TaskError('calibration requires a detected USB target')
                device = self._device(device, payload, actor)
                binding, executor = self._target(device, actor, payload)
                if (binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                        or binding.profile_data.get('chip_family') != 'k3'
                        or not getattr(executor, 'supports_calibrate', False)):
                    raise TaskError('flash calibration requires a detected K3 USB target and updated executor', 409)
                if action == 'forget':
                    executor.forget_tuning(binding)
                    return response(200, {'ok': True, 'configuration': executor.tuning(binding)})
                store = self.store
                def run(context):
                    from .flash_loaders import prepare_materials
                    request = {'version': 1, 'device': binding.device, 'chip_family': 'k3',
                               'system_storage': storage, 'measure_write': measure_write}
                    if binding.profile_data.get('bootrom_identity'):
                        materials = prepare_materials(binding, executor.stream_loader_workspace(), context.check, getattr(executor, 'root', None))
                        request['loader'] = {key: {name: materials[key][name] for name in ('id', 'name', 'path', 'size', 'sha256')}
                                             for key in ('fsbl', 'uboot')}
                        if materials.get('trained_for'):
                            request['loader']['trained_for'] = materials['trained_for']
                        if materials.get('usb_speed'):
                            request['loader']['usb_speed'] = materials['usb_speed']
                    def event(value):
                        store.append_event(context.id, value)
                        done, total = value.get('done', 0), value.get('total', 0)
                        if type(done) is int and type(total) is int and 0 <= done <= max(total, done):
                            context.progress(done, max(total, done), value.get('phase', 'calibrating'))
                    with _release_when_stopped(self.manager, context.id):
                        result = executor.calibrate(binding, request, event, context.check)
                    store.update_details(context.id, {'configuration': result['configuration']})
                    return result
                task = self.manager.tasks.start_board('flash', actor, client, device.serial, run,
                    request_id=payload.get('request_id'), input_digest=input_digest,
                    details={'operation': 'calibrate', 'system_storage': storage, 'measure_write': measure_write,
                        'target_id': binding.target_id, 'target_generation': binding.target_generation,
                        'executor': binding.endpoint, 'write_result': 'not-started',
                        'verify_result': 'not-requested', 'boot_result': 'not-requested'},
                    resources=_resources(binding), maintenance=maintenance_for(self.manager),
                    target_id=binding.target_id, target_generation=binding.target_generation)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def start(self, payload, device, actor='web'):
        try:
            _services(self.manager)
            with self.manager._flash_plans_lock:
                return self._start(payload, device, actor)
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def _start(self, payload, device, actor='web'):
        try:
            if set(payload) - {'kind', 'key', 'serial', 'actor', 'client_id', 'plan_id',
                               'binding_revision', 'request_id', 'target_id', 'target_generation', 'retain_backup', 'temporary_snapshot', 'prepare_for_review'}:
                raise TaskError('flash tasks accept only a fixed plan and board binding')
            identity = {key: payload.get(key) for key in ('serial', 'plan_id', 'binding_revision')}
            for option in ('retain_backup', 'temporary_snapshot', 'prepare_for_review'):
                if type(payload.get(option, False)) is not bool:
                    raise TaskError(option + ' must be boolean')
                if payload.get(option):
                    identity[option] = True
            if payload.get('target_id'):
                identity.update(target_id=payload['target_id'], target_generation=payload.get('target_generation'))
            input_digest = _digest(identity)
            previous = self.store.find_request(actor, payload.get('request_id'), input_digest)
            if previous:
                return response(202, {'ok': True, 'task': self.manager.tasks.store.get(previous)})
            device = self._device(device, payload, actor)
            binding, executor = self._target(device, actor, payload)
            serial = device.serial
            record = self.store.get_plan(payload.get('plan_id'), actor, serial,
                                         target_id=binding.target_id or None)
            if (type(payload.get('binding_revision')) is not int
                    or payload['binding_revision'] != _binding_revision(binding)
                    or record['binding_revision'] != _binding_revision(binding)
                    or record['binding_digest'] != _binding_digest(binding)):
                raise TaskError('flash binding changed; create a new preview', 409)
            if time.time() - record['created'] > 3600:
                raise TaskError('flash plan expired; create a new preview', 409)
            if payload.get('prepare_for_review') and not (
                    record['requires_prepare'] and record['intent']['mode'] == 'full'
                    and record['executor_plan'].get('ready') is False
                    and record['executor_plan'].get('confirm_after_prepare') is True):
                raise TaskError('准备设备需要可在写入前暂停确认的计划，请重新预览', 409)
            if record['intent']['mode'] == 'full' and not record['executor_plan'].get('writes'):
                raise TaskError('full image preview must list all affected write ranges', 409)
            if payload.get('retain_backup') and (not getattr(executor, 'supports_nor_snapshot', False)
                    or not record.get('workspace') or not record['executor_plan'].get('ready') or not _snapshot_inspection(record)):
                raise TaskError('this plan cannot read a verified NOR snapshot', 409)
            client = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            operation = FlashOperation(self, binding, executor, record, actor)
            task = self.manager.tasks.start_board('flash', actor, client, serial, operation.run,
                request_id=payload.get('request_id'), input_digest=input_digest,
                details={'plan_id': record['plan_id'], 'plan_digest': record['digest'],
                         'retain_backup': payload.get('retain_backup', False),
                         'temporary_snapshot': payload.get('temporary_snapshot', False),
                         'workspace': record.get('workspace'),
                         'source_workspaces': record.get('source_workspaces', []),
                         'target_id': binding.target_id, 'target_generation': binding.target_generation,
                         'binding_revision': record['binding_revision'], 'executor': binding.endpoint,
                         'attempt_id': operation.attempt_id, 'write_result': 'not-started',
                         'verify_result': 'not-started', 'boot_result': 'not-requested',
                         'plan': self._preview(record)}, resources=_resources(binding),
                maintenance=maintenance_for(self.manager), target_id=binding.target_id or None,
                target_generation=binding.target_generation or None)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def events(self, task_id, after=0, actor='web', serial=None, connection_id=None, target_id=None):
        try:
            task = self.manager.tasks.store.get(task_id)
            if (task['kind'] not in ('flash', 'flash-inspect') or actor != 'web' and task['actor'] != actor
                    or serial is not None and not self.manager.tasks.store.visible_serial(task, serial)
                    or target_id is not None and not any(value['target_id'] == target_id
                                                        for value in task.get('flash_targets', []))
                    or connection_id is not None):
                raise TaskError('flash task not found', 404)
            try:
                cursor = int(after)
            except (ValueError, TypeError):
                raise TaskError('invalid flash event cursor') from None
            result = self.store.events(task_id, cursor)
            result.update(details=_public(self.store.details(task_id)), next_after=result['next_cursor'],
                          truncated_before=result['gap'])
            return response(200, result)
        except (TaskError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def recovery(self, payload, device, actor='web'):
        try:
            if (not isinstance(payload, dict) or set(payload) - {
                    'id', 'key', 'serial', 'actor', 'client_id', 'attempt_id', 'reason', 'confirm_stopped',
                    'target_id', 'target_generation'}):
                raise TaskError('invalid flash recovery confirmation')
            device = self._device(device, payload, actor, require_present=False)
            if device is None or not payload.get('target_id') and (not device.serial or payload.get('serial') != device.serial):
                raise TaskError('flash recovery requires the registered board', 409)
            task = self.manager.tasks.store.get(payload.get('id'))
            visible = (any(value['target_id'] == device.target_id for value in task.get('flash_targets', []))
                       if payload.get('target_id') else self.manager.tasks.store.visible_serial(task, device.serial))
            if not visible:
                raise TaskError('flash task not found', 404)
            details = self.store.details(task['id'])
            if (actor == 'web' and payload.get('confirm_stopped') is True
                    and payload.get('attempt_id') == details.get('attempt_id')
                    and task['state'] not in ('queued', 'running')):
                pins = getattr(self.manager, '_flash_recovery_pins', {}).pop(task['id'], None)
                if pins:
                    pins.close()
            result = maintenance_for(self.manager).confirm_recovery(task['id'], actor,
                payload.get('attempt_id'), payload.get('reason'), payload.get('confirm_stopped'),
                self.manager.artifacts)
            workspace = details.get('workspace')
            if workspace:
                from .flash_runner import LocalFlashExecutor
                executor = getattr(self.manager, 'flash_executor', None)
                if executor is None or not hasattr(executor, 'cleanup_plan'):
                    executor = LocalFlashExecutor(Path(self.manager.artifacts.root).parent / 'flashing')
                try:
                    executor.cleanup_plan(workspace)
                except (TaskError, OSError):
                    return response(200, {'ok': True, 'task': result, 'cache_cleanup_pending': True})
            return response(200, {'ok': True, 'task': result})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def expire(self, image_sha256=None):
        """Follow the collector's existing maintenance tick; keep active/recovery data."""
        executor = getattr(self.manager, 'flash_executor', None)
        if executor is None or not hasattr(executor, 'cleanup_plan'):
            return 0
        removed = 0
        with self.manager._flash_plans_lock, self.manager.store.lock:
            rows = self.manager.store.db.execute('SELECT id,document FROM flash_plans WHERE created<?',
                                                 (time.time() - 3600,)).fetchall()
            protected = self.manager.store.db.execute(
                "SELECT d.task_id,d.document FROM flash_details d JOIN tasks t ON t.id=d.task_id "
                "WHERE t.state IN ('queued','running') OR EXISTS "
                '(SELECT 1 FROM flash_resource_holds h WHERE h.task_id=d.task_id)').fetchall()
            task_ids = {row['task_id'] for row in protected}
            plan_ids, workspaces = set(), set()
            fresh = self.manager.store.db.execute('SELECT document FROM flash_plans WHERE created>=?',
                                                  (time.time() - 3600,)).fetchall()
            for row in fresh:
                document = json.loads(row['document'])
                workspaces.add(document.get('workspace'))
                workspaces.update(document.get('source_workspaces') or [])
            for row in protected:
                detail = json.loads(row['document'])
                plan_ids.add(detail.get('plan_id'))
                workspaces.add(detail.get('workspace'))
                workspaces.update(detail.get('source_workspaces') or [])
            for plan_id in plan_ids:
                row = self.manager.store.db.execute('SELECT document FROM flash_plans WHERE id=?',
                                                    (plan_id,)).fetchone()
                if row:
                    document = json.loads(row['document'])
                    workspaces.add(document.get('workspace'))
                    workspaces.update(document.get('source_workspaces') or [])
            for row in rows:
                document = json.loads(row['document'])
                if (row['id'] in plan_ids or document.get('selection_task_id') in task_ids
                        or document.get('workspace') and document['workspace'] in workspaces):
                    continue
                executor.cleanup_plan(document)
                # Retain immutable receipt metadata and digests, remove only expired payloads.
                removed += 1
            if hasattr(executor, 'cleanup_image'):
                # Same task/plan registry protects image data used by writers,
                # including unknown helpers retained for human recovery.
                pool = self.manager.store.db.execute("SELECT 1 FROM flash_resource_holds WHERE resource='flash-cache-pool:local' OR (resource LIKE 'flash-image:%' AND state='recovery')").fetchone()
                if not pool:
                    active_images = self.manager.store.db.execute(
                        "SELECT a.sha256 FROM tasks t JOIN task_targets s ON s.task_id=t.id "
                        "JOIN transfer_artifacts a ON s.connection_id='image-prepare:' || a.id "
                        "WHERE t.kind='image-prepare' AND t.state IN ('queued','running')").fetchall()
                    workspaces.update(str(executor.root / 'images' / value[0]) for value in active_images)
                    cutoff = time.time() - self.manager.artifacts.policy.ready_seconds
                    for image in (executor.root / 'images').glob('*'):
                        if str(image) in workspaces or not re.fullmatch(r'[0-9a-f]{64}', image.name):
                            continue
                        if image_sha256 != image.name and image.stat().st_mtime >= cutoff:
                            continue
                        removed += bool(executor.cleanup_image(image))
        return removed

    def boot_priority(self, payload, actor='web'):
        """One idempotent EEPROM write under the existing task's USB lease."""
        try:
            if not isinstance(payload, dict) or set(payload) - {'action', 'id', 'target_id', 'target_generation',
                    'serial', 'actor', 'client_id', 'key', 'request_id', 'expected', 'value'}:
                raise TaskError('invalid EEPROM priority request', 403)
            allowed = ('SSD', 'UFS', 'MMC', 'USB')
            if payload.get('value') not in allowed or payload.get('expected') not in ('-',) + allowed:
                raise TaskError('invalid EEPROM boot priority')
            request_id = payload.get('request_id')
            if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
                raise TaskError('invalid EEPROM request ID')
            task_id = payload.get('id')
            task = self._owned_task(task_id, actor, payload.get('serial'))
            self._device(None, payload, actor, require_present=False)
            if not any(t['target_id'] == payload.get('target_id') and t['generation'] == payload.get('target_generation')
                       for t in task.get('flash_targets', [])):
                raise TaskError('EEPROM task USB target changed', 409)
            _services(self.manager)
            with self.manager._flash_waiters_lock:
                waiter = self.manager._flash_waiters.get(task_id)
            identity = {key: payload[key] for key in ('request_id', 'expected', 'value')}
            previous = (self.store.details(task_id).get('boot_priority_requests') or {}).get(request_id)
            if previous and previous.get('request_id') == request_id:
                if any(previous.get(key) != value for key, value in identity.items()):
                    raise TaskError('EEPROM request ID reused with different input', 409)
                return response(200, {'ok': True, 'change': previous, 'task': task})
            if waiter is None:
                raise TaskError('read device storage before editing EEPROM', 409)
            with waiter['lock']:
                operation = waiter['operation']
                operation.check()
                if (waiter.get('closed') or waiter['selected'] is not None or time.time() >= waiter['deadline']
                        or waiter.get('failure') is not None):
                    raise TaskError('EEPROM selection is no longer available', 409)
                details = self.store.details(task_id)
                history = dict(details.get('boot_priority_requests') or {})
                if request_id in history:
                    previous = history[request_id]
                    if any(previous.get(key) != value for key, value in identity.items()):
                        raise TaskError('EEPROM request ID reused with different input', 409)
                    return response(200, {'ok': True, 'change': previous, 'task': self.manager.tasks.store.get(task_id)})
                inventory = details.get('storage_inventory') or {}
                if not inventory.get('boot_priority_editable') or not hasattr(operation.executor, 'set_boot_priority'):
                    raise TaskError('EEPROM editing requires updated RAM materials', 409)
                expected = 'eeprom:' + payload['expected'] if payload['expected'] != '-' else 'ram-policy'
                if inventory.get('priority_source') != expected:
                    raise TaskError('EEPROM priority changed; refresh before saving', 409)
                if len(history) >= 16:
                    raise TaskError('EEPROM edit limit reached; read device storage again', 409)
                previous = details.get('boot_priority_change')
                if previous and previous.get('state') != 'completed':
                    raise TaskError('previous EEPROM update outcome is unknown; read device storage again', 409)
                change = dict(identity, state='running', verify_result='not-started')
                history[request_id] = change
                self.store.update_details(task_id, {'boot_priority_change': change, 'boot_priority_requests': history})
                try:
                    result = operation.executor.set_boot_priority(operation.binding, payload['expected'], payload['value'], operation.check)
                    if (not isinstance(result, dict) or result.get('ok') is not True or result.get('verify_result') != 'verified'
                            or result.get('priority_source') != 'eeprom:' + payload['value']):
                        raise TaskError('EEPROM update readback was not confirmed', 502)
                except BaseException as error:
                    history[request_id] = dict(change, state='unknown', error=str(error)[:300])
                    self.store.update_details(task_id, {'boot_priority_change': history[request_id], 'boot_priority_requests': history})
                    waiter['failure'] = error
                    waiter['event'].set()
                    raise
                from .storage_metadata import default_storage_for_priority
                selected = default_storage_for_priority(inventory.get('storages', []), result['priority_source'])
                inventory = dict(inventory, priority_source=result['priority_source'],
                                 default_storage=selected, default_candidate=selected)
                # Match the read-back EEPROM choice, without claiming OS boot success.
                change = dict(change, state='completed', verify_result='verified', effective='next-boot')
                history[request_id] = change
                self.store.update_details(task_id, {'boot_priority_change': change, 'boot_priority_requests': history, 'storage_inventory': inventory})
                return response(200, {'ok': True, 'change': change, 'task': self.manager.tasks.store.get(task_id)})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def skip_verification(self, payload, actor='web'):
        try:
            if set(payload) - {'id', 'action', 'key', 'serial', 'actor', 'client_id', 'target_id', 'target_generation'}:
                raise TaskError('invalid skip verification request')
            with self.manager.store.lock:
                task = self._owned_task(payload.get('id'), actor, payload.get('serial'))
                if task['kind'] != 'flash':
                    raise TaskError('not a flash execution task', 409)
                if task.get('flash_targets') and not any(
                        row['target_id'] == payload.get('target_id') and row['generation'] == payload.get('target_generation')
                        for row in task['flash_targets']):
                    raise TaskError('flash verification USB target changed', 409)
                details = self.store.details(task['id'])
                if not details.get('verify_skip_requested'):
                    if (task['state'] != 'running' or task.get('cancel_requested')
                            or task.get('progress', {}).get('phase') not in ('verifying', 'verifying-system', 'verifying-nor')
                            or details.get('can_skip_verification') is not True):
                        raise TaskError('flash task is not in skippable readback verification', 409)
                    self.store.update_details(task['id'], {'verify_skip_requested': True,
                        'verify_skip_requested_by': actor, 'verify_skip_requested_at': time.time()})
                return response(200, {'ok': True, 'task': self.manager.tasks.store.get(task['id'])})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def selection(self, payload, device, actor='web'):
        if isinstance(payload, dict) and payload.get('action') == 'skip-verification':
            return self.skip_verification(payload, actor)
        if isinstance(payload, dict) and payload.get('action') == 'boot-priority':
            return self.boot_priority(payload, actor)
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'id', 'key', 'serial', 'actor', 'client_id', 'layout_digest', 'selections',
                    'preview', 'plan_id', 'request_id', 'target_id', 'target_generation',
                    'artifact_id', 'firmware_artifact_id', 'firmware_table', 'mode', 'target_storage', 'verify', 'reset_after', 'retain_backup'}:
                raise TaskError('invalid flash selection request')
            task_id = payload.get('id')
            historical_target = None
            if payload.get('target_id'):
                original = self._owned_task(task_id, actor, payload.get('serial'))
                historical_target = next((value for value in original.get('flash_targets', [])
                    if value['target_id'] == payload['target_id'] and value['generation'] == payload.get('target_generation')), None)
                if historical_target is None:
                    raise TaskError('flash selection USB target changed', 409)
                device = self._device(device, payload, actor, require_present=False)
            else:
                device = self._device(device, payload, actor)
            visible = self.events(task_id, 0, actor, device.serial if device.serial else None,
                                  target_id=getattr(device, 'target_id', None))
            if visible.status != 200:
                return visible
            details = self.store.details(task_id)
            retain = payload.get('retain_backup', False)
            if type(retain) is not bool or retain and payload.get('preview') is True:
                raise TaskError('retaining a backup requires a confirmed NOR snapshot')
            identity = {key: payload.get(key) for key in ('plan_id', 'request_id', 'layout_digest')}
            if retain:
                identity['retain_backup'] = True
            submitted = details.get('selection_request')
            if submitted and payload.get('preview') is not True:
                if submitted != identity:
                    raise TaskError('flash selection already submitted with different input', 409)
                return response(200, {'ok': True, 'task': self.manager.tasks.store.get(task_id)})
            with self.manager._flash_waiters_lock:
                waiter = self.manager._flash_waiters.get(task_id)
            if waiter is None:
                raise TaskError('flash task is not waiting for partition selection', 409)
            with waiter['lock']:
                if waiter.get('closed') or time.time() >= waiter['deadline'] or waiter['selected'] is not None:
                    raise TaskError('flash partition selection is no longer available', 409)
                operation, record = waiter['operation'], waiter['record']
                if historical_target:
                    device.serial = record['serial']
                    binding, executor = operation.binding, operation.executor
                else:
                    binding, executor = self._target(device, actor, payload)
                operation.check()
                actual = (record.get('inspection') or {}).get('layout_digest') or record['executor_plan'].get('layout_digest')
                if not actual or payload.get('layout_digest') != actual:
                    raise TaskError('flash target layout changed; refresh selection', 409)
                if payload.get('preview') is True:
                    intent = dict(record['intent'], selections=payload.get('selections', []))
                    for option in ('verify', 'reset_after'):
                        if option in payload:
                            intent[option] = payload[option]
                    if record.get('operation') != 'nor-workspace' and any(key in payload for key in ('artifact_id', 'mode', 'target_storage')):
                        raise TaskError('source import is only available in a storage reading workspace')
                    workspace_import = record.get('operation') == 'nor-workspace' and payload.get('artifact_id')
                    if ('firmware_artifact_id' in payload or 'firmware_table' in payload) and not workspace_import:
                        raise TaskError('companion firmware requires a full workspace image preview')
                    if workspace_import:
                        intent['firmware_table'] = payload.get('firmware_table')
                        intent['firmware_artifact_id'] = payload.get('firmware_artifact_id')
                        intent.update(artifact_id=payload['artifact_id'], mode=payload.get('mode', 'partitions'),
                                      verify=payload.get('verify', record['intent']['verify']), target_storage=payload.get('target_storage', ''))
                        intent = self._intent(intent)
                    if record.get('operation') == 'nor-workspace' and not workspace_import:
                        chosen = intent['selections']
                        if not isinstance(chosen, list) or not chosen or not isinstance(chosen[0], dict):
                            raise TaskError('select at least one target partition')
                        intent['artifact_id'] = chosen[0].get('artifact_id')
                        intent = self._intent(intent)
                        # One package can supply several selected partitions;
                        # raw overrides remain explicit per-partition artifacts.
                        # Choosing the package as the import anchor makes this
                        # independent of the visual order of partition rows.
                        for item in intent['selections']:
                            candidate = self.manager.artifacts.get(item.get('artifact_id'), actor, None if actor == 'web' else device.serial)
                            if candidate['name'].lower().endswith(('.tar.gz', '.tgz', '.tar', '.zip')):
                                intent['artifact_id'] = candidate['id']
                                break
                    intent = self._intent(intent)
                    full_review = record['intent']['mode'] == 'full' and record['executor_plan'].get('confirm_after_prepare') is True
                    if full_review and intent['selections']:
                        raise TaskError('firmware confirmation cannot change the full package scope')
                    if not full_review and not intent['selections'] and not workspace_import:
                        raise TaskError('select at least one target partition')
                    timing = None
                    try:
                        source_record = record
                        if workspace_import and payload.get('plan_id'):
                            source_record = self.store.get_plan(payload['plan_id'], actor, device.serial, target_id=binding.target_id or None)
                            if (source_record.get('selection_task_id') != task_id or source_record['binding_digest'] != record['binding_digest']
                                    or source_record['intent']['artifact_id'] != intent['artifact_id']
                                    or source_record['intent'].get('firmware_artifact_id') != intent.get('firmware_artifact_id')
                                    or source_record['intent'].get('firmware_table') != intent.get('firmware_table')
                                    or source_record['intent']['mode'] != intent['mode']
                                    or source_record['intent']['verify'] != intent['verify']
                                    or source_record['intent'].get('target_storage', '') != intent.get('target_storage', '')
                                    or time.time() - source_record['created'] > 600):
                                raise TaskError('source image preview changed or expired', 409)
                        cached = (source_record if source_record['executor_plan'].get('image_type') in ('package', 'payload', 'payload.gz', 'bundle')
                                  and source_record['intent']['verify'] == intent['verify']
                                  and source_record['intent']['artifact_id'] == intent['artifact_id']
                                  and source_record['intent'].get('firmware_artifact_id') == intent.get('firmware_artifact_id')
                                  and source_record['intent'].get('firmware_table') == intent.get('firmware_table')
                                  and all(value['artifact_id'] == source_record['intent']['artifact_id']
                                          for value in intent['selections']) else None)
                        from .flash_progress import PreviewProgress
                        source = self.manager.artifacts.get(intent['artifact_id'], actor, None if actor == 'web' else device.serial)
                        timing = PreviewProgress(self.store, task_id, source, binding.endpoint,
                            source['name'].lower().endswith(('.gz', '.tgz', '.zip')))
                        preview_succeeded = False
                        def preview_progress(value):
                            timing.event(value)
                        if not cached and source_record['executor_plan'].get('base_profile') is not None:
                            binding = replace(binding, profile_data=source_record['executor_plan']['base_profile'])
                        selected = self._make_plan(binding, executor, intent, actor, device.serial,
                                                   operation.check, task_id, prepared=cached,
                                                   baseline_record=record, on_event=preview_progress)
                        preview_succeeded = True
                    except BaseException as error:
                        if getattr(error, 'stage', None) in ('image-plan', 'check-plan'):
                            self.store.append_event(task_id, {'event': 'error', 'phase': 'preview-failed',
                                'stage': error.stage,
                                'message': '镜像预览失败（' + error.stage + '），未开始刷写；' + str(error)[:512]})
                        if getattr(error, 'cleanup_confirmed', True) is False:
                            waiter['failure'] = error
                            try:
                                self._retain_sources(error, task_id, actor, device.serial, waiter['claimed'])
                                self.store.update_details(task_id, {'workspace': getattr(error, 'workspace', None)})
                            finally:
                                waiter['event'].set()
                        raise
                    finally:
                        if timing is not None:
                            timing.finish(preview_succeeded)
                        else:
                            self.store.update_details(task_id, {'preview_progress': {'active': False}})
                    operation.check()
                    if full_review and (_write_scope(selected['executor_plan']) != _write_scope(record['executor_plan'])
                            or selected['executor_plan'].get('erase_ranges') != record['executor_plan'].get('erase_ranges')):
                        raise TaskError('firmware confirmation changed its reviewed scope', 409)
                    if selected['requires_prepare'] or not _selection_layout_matches(record, selected, actual):
                        raise TaskError('flash target layout changed during preview', 409)
                    return response(200, {'ok': True, 'plan': self._preview(selected),
                        'inspection': _public(selected.get('inspection') or {}),
                        'payload_candidates': _public(selected.get('payload_candidates') or [])})
                if not isinstance(payload.get('request_id'), str) or not re.fullmatch(
                        r'[A-Za-z0-9_-]{8,100}', payload['request_id']):
                    raise TaskError('invalid flash selection request ID')
                selected = self.store.get_plan(payload.get('plan_id'), actor, device.serial,
                                               target_id=binding.target_id or None)
                if (selected.get('selection_task_id') != task_id or selected['binding_digest'] != record['binding_digest']
                        or time.time() - selected['created'] > 600
                        or selected['executor_plan'].get('ready') is False
                        or not selected['executor_plan'].get('writes')):
                    raise TaskError('flash selection plan changed or expired', 409)
                if retain and not details.get('nor_before') and (not getattr(executor, 'supports_nor_snapshot', False)
                        or not selected.get('workspace') or not _snapshot_inspection(selected)):
                    raise TaskError('this plan cannot read a verified NOR snapshot', 409)
                if not _selection_layout_matches(record, selected, actual):
                    raise TaskError('flash selection layout differs from prepared target', 409)
                if record['intent']['mode'] == 'full' and (
                        {k: v for k, v in selected['intent'].items() if k not in ('verify', 'reset_after')}
                        != {k: v for k, v in record['intent'].items() if k not in ('verify', 'reset_after')}
                        or _write_scope(selected['executor_plan']) != _write_scope(record['executor_plan'])
                        or selected['executor_plan'].get('erase_ranges') != record['executor_plan'].get('erase_ranges')):
                    raise TaskError('firmware confirmation differs from the prepared scope', 409)
                added = []
                try:
                    for artifact_id in sorted(_source_ids(selected['intent'])):
                        if artifact_id not in waiter['claimed']:
                            self.manager.artifacts.claim(artifact_id, task_id, actor, None if actor == 'web' else device.serial)
                            added.append(artifact_id)
                    self.store.update_details(task_id, {'selection_request': identity})
                except BaseException:
                    for artifact_id in added:
                        self.manager.artifacts.finish_claim(artifact_id, task_id)
                    raise
                waiter['claimed'].extend(added)
                waiter['selected'] = selected
                waiter['event'].set()
            return response(200, {'ok': True, 'task': self.manager.tasks.store.get(task_id)})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})


class FlashOperation:
    def __init__(self, actions, binding, executor, record, actor):
        self.actions, self.manager = actions, actions.manager
        self.binding, self.initial_binding, self.executor, self.record, self.actor = binding, binding, executor, record, actor
        if record.get('execution_profile') is not None:
            self.binding = executor.execution_binding(binding, record['execution_profile'], record.get('workspace'))
        self.preparing, self.final_generation = False, None
        self.resolved_serial = record['serial']
        self.attempt_id = secrets.token_hex(12)

    def run(self, context):
        actions, record = self.actions, self.record
        store, maintenance = actions.store, maintenance_for(self.manager)
        claimed, cleanup_confirmed, write_started, outcome = [], True, False, {}
        write_progress = None
        submitted_full_review = False
        nor_baseline = None
        def check():
            context.check()
            from .board_ownership import ownership_for
            ownership = ownership_for(self.manager)
            if ownership is not None:
                ownership.check(self.resolved_serial or record.get('serial', ''), self.actor)
            if record.get('target_id'):
                if self.preparing:
                    current = self.initial_binding
                elif self.final_generation:
                    row = _discovery(self.manager).get(record['target_id'], self.final_generation)
                    if row['document']['device'] != self.binding.device:
                        raise TaskError('prepared USB target connection changed', 409)
                    current = self.initial_binding
                else:
                    current = _discovery(self.manager).binding(record['target_id'], record['target_generation'])
            else:
                book, _ = _services(self.manager)
                current = book.binding(record['serial'], self.actor)
            if self.resolved_serial and self.manager.store.has_conflict(self.resolved_serial):
                raise TaskError('board identity conflict was detected during flashing', 409)
            if _binding_digest(current) != record['binding_digest']:
                raise TaskError('flash binding changed during execution', 409)
        def event(value):
            value = _public(value)
            store.append_event(context.id, value)
            phase = value.get('phase', 'flash')
            done, total = value.get('done', 0), value.get('total', 0)
            if type(done) is int and type(total) is int and 0 <= done and (done <= total
                    or total == 0 and phase == 'reading-system-metadata'):
                progress = {}
                if write_progress is not None and phase == 'writing' and 'total' in value:
                    done, total, progress = write_progress.measure(value)
                context.progress(done, total, phase, **progress)
        self.check = check
        try:
            check()
            workspace_mode = record.get('operation') == 'nor-workspace'
            if workspace_mode:
                prepared_read = self.executor.nor_workspace(self.binding, check)
                self.binding = self.executor.execution_binding(self.binding, prepared_read['execution_profile'],
                                                               prepared_read['workspace'], check)
                record.update(prepared_read)
                record['requires_prepare'] = bool(self.binding.profile_data.get('bootrom_identity'))
                record = store.put_plan(record)
                store.update_details(context.id, {'workspace': record['workspace'], 'plan_id': record['plan_id']})
            for artifact_id in sorted(_source_ids(record['intent'])):
                self.manager.artifacts.claim(artifact_id, context.id, self.actor, None if self.actor == 'web' else record['serial'])
                claimed.append(artifact_id)
            if record['serial']:
                self.manager.store.event(record['serial'], self.actor, 'flash-start', context.id)
            if record['requires_prepare']:
                submitted_full_review = record['executor_plan'].get('confirm_after_prepare') is True
                if submitted_full_review and (record['intent']['mode'] != 'full'
                        or ((self.binding.profile_data.get('storages') or [{}])[0].get('layout_source') != 'spacemit-nor'
                            and not self.binding.profile_data.get('system_storage'))):
                    raise TaskError('post-prepare confirmation requires a checked NOR or system storage template', 502)
                context.progress(0, 0, 'prepare')
                previous_device = None
                if self.binding.target_id and not record.get('execution_profile'):
                    raise TaskError('automatic loader preparation requires a verified image execution profile', 409)
                self.preparing = bool(self.binding.target_id)
                try:
                    prepared = self.executor.prepare(self.binding, event, check,
                                                     task_id=context.id, attempt_id=self.attempt_id)
                finally:
                    self.preparing = False
                if self.binding.target_id:
                    from .flash_discovery import _selector
                    if not isinstance(prepared, dict):
                        raise TaskError('automatic loader preparation returned an invalid receipt', 502)
                    final_device = _selector(prepared.get('final_device'))
                    if record['serial'] and (prepared.get('final_identity') or {}).get('serialno') != record['serial']:
                        raise TaskError('prepared loader did not confirm the original board identity', 409)
                    previous_device = dict(self.binding.device)
                    self.final_generation = _discovery(self.manager).accept_prepared_target(
                        self.binding.target_id, self.binding.target_generation, final_device,
                        context.id, self.attempt_id, record['serial'],
                        (prepared.get('final_identity') or {}).get('eeprom-serial') or '')
                    self.binding = replace(self.binding, device=final_device)
                    identity = prepared.get('final_identity') or {}
                    self.resolved_serial = _discovery(self.manager).accept_eeprom_identity(
                        self.binding.target_id, self.final_generation, identity, context.id, self.attempt_id) or record['serial']
                    if identity.get('eeprom-serial') and identity.get('eeprom-serial') == identity.get('serialno'):
                        # The accepted serial belongs to this task and USB generation.
                        # Keep the profile sealed into the imported plan intact.
                        store.update_details(context.id, {'resolved_serial': self.resolved_serial, 'serial_source': 'eeprom', 'request_serial': record['serial']})
                    store.update_details(context.id, {'execution_device': final_device,
                                                       'allowed_final_generation': self.final_generation})
                if not workspace_mode:
                    submitted = record
                    record = actions._make_plan(self.binding, self.executor, record['intent'], self.actor,
                                               record['serial'], check, context.id, prepared=record,
                                               previous_device=previous_device)
                    if record['requires_prepare']:
                        raise TaskError('flash loader preparation did not provide a usable target', 502)
                    if submitted_full_review and record['executor_plan'].get('confirm_after_prepare') is not True:
                        raise TaskError('prepared firmware lost its mandatory review', 502)
                    if not submitted_full_review and (submitted['intent']['mode'] == 'full' or submitted['intent']['selections']) and (
                            _write_scope(submitted['executor_plan']) != _write_scope(record['executor_plan'])
                            or submitted['executor_plan'].get('write_layout') != record['executor_plan'].get('write_layout')):
                        raise TaskError('prepared write scope differs from preview; create a new preview', 409)
            snapshot = None
            if workspace_mode:
                snapshot = self.executor.snapshot(self.binding, record['workspace'], event, check,
                    task_id=context.id, attempt_id=self.attempt_id)
                if not isinstance(snapshot, dict) or snapshot.get('ok') is not True:
                    raise TaskError('whole NOR backup did not complete; storage was not written', 502)
                record['inspection'] = snapshot.get('inspection')
                if not isinstance(record['inspection'], dict):
                    raise TaskError('NOR read did not return a checked layout', 502)
                record['execution_device'] = dict(self.binding.device)
                record['selection_task_id'] = context.id
                record['executor_plan']['layout_digest'] = (record['inspection'] or {}).get('layout_digest')
                record['requires_prepare'] = False
                record['pending_checks'] = ['partition-selection']
                record = store.put_plan(record)
                store.update_details(context.id, {'plan_id': record['plan_id'], 'inspection': _public(record['inspection'])})
            inspected_nor = _snapshot_inspection(record)
            if (record['intent']['mode'] == 'partitions' and inspected_nor
                    and getattr(self.executor, 'supports_nor_snapshot', False)):
                nor_baseline = self._backup_before(context.id, record, inspected_nor, snapshot, event, check)
            if workspace_mode and record.get('read_system'):
                inventory = self.executor.storage_inventory(self.binding, check, event)
                store.update_details(context.id, {'storage_inventory': inventory})
            if workspace_mode and nor_baseline is None:
                raise TaskError('whole NOR backup is required before partition selection', 502)
            if (record['intent']['mode'] == 'partitions' and not record['intent']['selections']
                    or submitted_full_review):
                record = self._wait_selection(context, record, event, check, claimed)
                if (record.get('execution_profile') or {}).get('ram_loader_protocol') == 'spacemit-ram-v1' and record['execution_profile'] != self.binding.profile_data:
                    self.binding = self.executor.execution_binding(self.binding, record['execution_profile'], record['workspace'], check)
                if (record.get('execution_profile') or {}).get('system_storage'):
                    # The NOR backup remains retained, but is not a baseline for
                    # a different medium. The OS adapter rechecks its live GPT.
                    nor_baseline = None
            plan = record['executor_plan']
            if plan.get('ready') is False or not plan.get('writes'):
                raise TaskError('flash plan is not ready to write', 409)
            details = store.details(context.id)
            if not details.get('nor_before') and (details.get('retain_backup')
                    or (details.get('selection_request') or {}).get('retain_backup')):
                inspected_nor = _snapshot_inspection(record)
                if not inspected_nor:
                    raise TaskError('confirmed NOR backup scope is no longer available', 409)
                self._backup_before(context.id, record, inspected_nor, None, event, check)
            store.update_details(context.id, {'plan_id': record['plan_id'], 'plan_digest': record['digest'],
                                             'workspace': record.get('workspace'),
                                             'source_workspaces': record.get('source_workspaces', []),
                                             'plan': actions._preview(record), 'write_inspection': _public(record.get('inspection') or {}),
                                             'payload_candidates': _public(record.get('payload_candidates') or []), 'write_result': 'running'})
            context.progress(0, 0, 'checking-image')
            from .flash_progress import FlashWriteProgress
            write_progress = FlashWriteProgress(plan)
            write_started = True
            execution_args = dict(task_id=context.id, attempt_id=self.attempt_id)
            if getattr(self.executor, 'supports_skip_verification', False):
                store.update_details(context.id, {'can_skip_verification': True})
                execution_args['skip_verification'] = lambda: store.details(context.id).get('verify_skip_requested') is True
            if record['intent'].get('reset_after'):
                if not getattr(self.executor, 'supports_reset_after', False):
                    raise TaskError('刷机执行器不支持刷后自动复位')
                execution_args['reset_after'] = True
            if getattr(self.executor, 'supports_nor_snapshot', False):
                execution_args.update(workspace=record.get('workspace'), nor_baseline=nor_baseline)
            if plan.get('image_type') == 'bundle':
                from .flash_bundle import execute_bundle
                result = execute_bundle(self.executor, self.binding, plan, event, check, **execution_args)
            else:
                result = self.executor.execute(self.binding, plan, event, check, **execution_args)
            if not isinstance(result, dict):
                raise TaskError('flash executor did not confirm success', 502)
            output = {key: result[key] for key in ('write_result', 'verify_result', 'boot_result',
                                                  'written_bytes', 'verified_bytes', 'readback_bytes', 'verification_scope') if key in result}
            outcome.update(output)
            store.update_details(context.id, outcome)
            if isinstance(result, dict) and result.get('nor_after') is not None:
                from .nor_backups import publish_backup
                after = publish_backup(self.manager.artifacts, result['nor_after'], record.get('workspace'),
                                       context.id, self.attempt_id, record['serial'], phase='after', check=check, actor=self.actor,
                                       writes=plan.get('writes', []))
                store.update_details(context.id, {'nor_after': after})
            if result.get('ok') is not True:
                raise TaskError('flash executor did not confirm success', 502)
            if output.get('write_result') != 'completed':
                raise TaskError('flash executor did not confirm all planned writes', 502)
            skipped = output.get('verify_result') == 'skipped'
            if skipped and store.details(context.id).get('verify_skip_requested') is not True:
                outcome['verify_result'] = 'unavailable'
                raise TaskError('flash verification skipped without an operator request', 502)
            if record['intent']['verify'] == 'required' and output.get('verify_result') != 'verified' and not skipped:
                raise TaskError('required flash verification did not pass', 502)
            output.setdefault('boot_result', 'not-requested')
            output.update(ok=True, plan_id=record['plan_id'], plan_digest=record['digest'])
            store.update_details(context.id, output)
            context.mark_outcome('verified' if output.get('verify_result') == 'verified' else 'acknowledged')
            if record['serial']:
                self.manager.store.event(record['serial'], self.actor, 'flash-complete', context.id)
            return output
        except BaseException as error:
            cleanup_confirmed = getattr(error, 'cleanup_confirmed', True)
            if not cleanup_confirmed:
                actions._retain_sources(error, context.id, self.actor, record['serial'], claimed)
            reported = getattr(error, 'outcome', {})
            outcome.update({key: reported[key] for key in ('write_result', 'verify_result', 'boot_result',
                                                          'written_bytes', 'verified_bytes', 'readback_bytes', 'verification_scope') if key in reported})
            raw_after = getattr(error, 'nor_snapshots', {}).get('nor_after')
            if cleanup_confirmed and nor_baseline is not None and raw_after is not None:
                try:
                    from .nor_backups import publish_backup
                    after = publish_backup(self.manager.artifacts, raw_after, record.get('workspace'),
                        context.id, self.attempt_id, record['serial'], phase='after', check=check, actor=self.actor,
                        writes=(record.get('executor_plan') or {}).get('writes', []))
                    store.update_details(context.id, {'nor_after': after})
                except (TaskCancelled, TaskError, ValueError, OSError):
                    # Preserve the write/verify failure even when retention fails.
                    store.update_details(context.id, {'nor_after_unavailable': True})
            failure = {'write_result': 'unknown-or-partial' if write_started else 'not-started',
                       'verify_result': 'not-completed', 'boot_result': 'not-requested',
                       'recovery_required': not cleanup_confirmed}
            failure.update(outcome)
            if not cleanup_confirmed:
                failure['workspace'] = getattr(error, 'workspace', None)
            store.update_details(context.id, failure)
            if record['serial']:
                self.manager.store.event(record['serial'], self.actor, 'flash-stopped', context.id)
            raise
        finally:
            with self.manager._flash_waiters_lock:
                waiter = self.manager._flash_waiters.get(context.id)
            if waiter:
                with waiter['lock']:
                    waiter['closed'] = True
                    with self.manager._flash_waiters_lock:
                        self.manager._flash_waiters.pop(context.id, None)
            if cleanup_confirmed:
                try:
                    for artifact_id in claimed:
                        self.manager.artifacts.finish_claim(artifact_id, context.id)
                    maintenance.release(context.id, confirmed=True)
                except BaseException:
                    maintenance.keep_recovery(context.id)
                    store.update_details(context.id, {'recovery_required': True})
                    raise
            else:
                maintenance.keep_recovery(context.id)

    def _backup_before(self, task_id, record, inspection, snapshot, event, check):
        from .nor_backups import publish_backup
        workspace = record['workspace']
        snapshot = snapshot or self.executor.snapshot(self.binding, workspace, event, check,
            task_id=task_id, attempt_id=self.attempt_id)
        if not isinstance(snapshot, dict) or snapshot.get('ok') is not True:
            raise TaskError('whole NOR backup did not complete; storage was not written', 502)
        raw = snapshot.get('snapshot')
        if (not isinstance(raw, dict) or raw.get('size') != inspection['storages'][0].get('size')
                or raw.get('layout_digest') != inspection.get('layout_digest')):
            raise TaskError('NOR backup does not cover the checked live storage', 502)
        backup = publish_backup(self.manager.artifacts, raw, workspace, task_id, self.attempt_id,
                                record['serial'], check=check, actor=self.actor)
        self.actions.store.update_details(task_id, {'nor_before': backup})
        return dict(id=backup['artifact']['id'], path=raw['path'], size=raw['size'], sha256=raw['sha256'])

    def _wait_selection(self, context, record, event, check, claimed):
        waiter = {'lock': threading.RLock(), 'event': threading.Event(), 'record': record,
                  'operation': self, 'selected': None, 'deadline': time.time() + 600, 'claimed': claimed}
        with self.manager._flash_waiters_lock:
            self.manager._flash_waiters[context.id] = waiter
        self.actions.store.update_details(context.id, {
            'inspection': _public(record.get('inspection') or {}),
            'payload_candidates': _public(record.get('payload_candidates') or []),
            'selection_deadline': waiter['deadline'],
            'confirmation_mode': record['intent']['mode'],
            'prepared_plan': self.actions._preview(record) if record['intent']['mode'] == 'full' else None})
        context.progress(0, 0, 'awaiting-selection')
        context.on_cancel(waiter['event'].set)
        while True:
            check()
            if time.time() >= waiter['deadline']:
                raise TaskError('flash partition selection timed out; storage was not written', 408)
            waiter['event'].wait(0.2)
            with waiter['lock']:
                if waiter.get('failure') is not None:
                    raise waiter['failure']
                if waiter['selected'] is not None:
                    return waiter['selected']
