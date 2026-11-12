"""Board-scoped flash coordination; reset, UART capture and writing stay separate."""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time

from .board_maintenance import maintenance_for
from .flash_store import FlashStore
from .results import response
from .tasks import TaskCancelled, TaskError
from .transactions import TransactionError


_INIT_LOCK = threading.Lock()


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


class FlashActions:
    def __init__(self, manager):
        self.manager = manager
        self.store = FlashStore(manager.store) if manager.store is not None else None

    def _device(self, device, payload, actor, require_present=True):
        if payload.get('target_id'):
            if actor != 'web':
                raise TaskError('automatic USB flash targets require a human operator', 403)
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
            if actor != 'web':
                raise TaskError('USB device discovery requires a human operator', 403)
            return response(200, _discovery(self.manager).refresh())
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def probe(self, payload, actor='web'):
        try:
            if (not isinstance(payload, dict) or set(payload) - {
                    'target_id', 'target_generation', 'actor', 'client_id', 'serial'}):
                raise TaskError('invalid USB capability detection request')
            if actor != 'web':
                raise TaskError('USB capability detection requires a human operator', 403)
            discovery = _discovery(self.manager)
            target = discovery.probe(payload.get('target_id'), payload.get('target_generation'))
            capabilities = self.capabilities(None, actor, target['target_id'], target['target_generation'])
            if capabilities.status != 200:
                return capabilities
            return response(200, {'ok': True, 'target': target, 'capabilities': capabilities.payload})
        except (TaskError, TransactionError, ValueError, OSError) as error:
            result = {'ok': False, 'error': str(error)}
            if getattr(error, 'recovery_task_id', None):
                result.update(task_id=error.recovery_task_id, recovery_required=True)
            return response(getattr(error, 'status', 503), result)

    def artifact_create(self, payload, actor='web'):
        try:
            if not isinstance(payload, dict):
                raise TaskError('JSON object required')
            if payload.get('task_id'):
                if actor != 'web':
                    raise TaskError('automatic USB flash targets require a human operator', 403)
                task = self.manager.tasks.store.get(payload['task_id'])
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

    def capabilities(self, device, actor='web', target_id=None, target_generation=None):
        try:
            target = None
            if target_id is not None:
                if actor != 'web':
                    raise TaskError('automatic USB flash targets require a human operator', 403)
                discovery = _discovery(self.manager)
                row = discovery.get(target_id, target_generation)
                device = discovery.view(target_id, target_generation)
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
                if device.serial and self.manager.store.has_conflict(device.serial):
                    result.update(supported=False, reason='conflicting USB board identity')
            else:
                if device is None or not device.serial:
                    raise TaskError('flashing requires a registered board', 404)
                book, _ = _services(self.manager)
                result = dict(book.available(device.serial, actor))
            result.setdefault('board_serial', device.serial)
            result['artifacts'] = []
            artifacts = self.manager.artifacts
            with self.manager.store.lock:
                rows = self.manager.store.db.execute(
                    "SELECT id FROM transfer_artifacts WHERE serial=? AND direction='upload' AND state='ready' "
                    + ('' if actor == 'web' else 'AND actor=? ') + 'ORDER BY updated DESC LIMIT 100',
                    (device.serial,) if actor == 'web' else (device.serial, actor)).fetchall()
            for row in rows:
                metadata = artifacts.get(row['id'], actor, device.serial)
                result['artifacts'].append({key: metadata[key] for key in ('id', 'name', 'size', 'sha256')})
            result['maintenance'] = (maintenance_for(self.manager).target_holds(target_id) if target_id
                                     else maintenance_for(self.manager).holds(device.serial))
            return response(200, result)
        except (TaskError, TransactionError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    @staticmethod
    def _intent(payload):
        mode, verify = payload.get('mode', 'full'), payload.get('verify', 'if-supported')
        if mode not in ('full', 'partitions') or verify not in ('required', 'if-supported', 'none'):
            raise TaskError('invalid flash mode or verification policy')
        artifact_id, selections = payload.get('artifact_id'), payload.get('selections', [])
        target_storage = payload.get('target_storage', '')
        if (not isinstance(target_storage, str) or len(target_storage.encode()) > 128
                or mode != 'full' and target_storage):
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
        return {'artifact_id': artifact_id, 'mode': mode, 'selections': normalized, 'verify': verify,
                'target_storage': target_storage}

    def _artifacts(self, intent, actor, serial, stack, task_id=None, check=lambda: None):
        ids = {intent['artifact_id']} | {value['artifact_id'] for value in intent['selections']}
        output = {}
        for artifact_id in sorted(ids):
            row = self.manager.artifacts.get(artifact_id, actor, serial)
            if row['direction'] != 'upload' or row['state'] not in ('ready', 'claimed'):
                raise TaskError('flash image is not sealed and ready', 409)
            source = stack.enter_context(self.manager.artifacts.open(artifact_id, actor, serial, task_id))
            digest = hashlib.sha256()
            while True:
                check()
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != row['sha256']:
                raise TaskError('flash image checksum changed', 409)
            output[artifact_id] = dict(id=artifact_id, name=row['name'], size=row['size'],
                                       sha256=row['sha256'], path=str(self.manager.artifacts._path(artifact_id, '.bin')))
        return output

    def _make_plan(self, binding, executor, intent, actor, serial, check=lambda: None, task_id=None,
                   inspection_scope=None, prepared=None, previous_device=None, baseline_record=None):
        if prepared is not None and hasattr(executor, 'check_plan'):
            check()
            selections = None
            if intent['selections'] != prepared['intent']['selections']:
                selections = [dict(storage=value['storage'], partition=value['partition'])
                              for value in intent['selections']]
            arguments = {'previous_device': previous_device} if previous_device is not None else {}
            result = executor.check_plan(binding, prepared['executor_plan'],
                prepared.get('executor_candidates', []), check, selections=selections, **arguments)
            result['workspace'] = prepared.get('workspace')
            if binding.target_id:
                result['execution_device'] = dict(binding.device)
        else:
            with ExitStack() as stack:
                artifacts = self._artifacts(intent, actor, serial, stack, task_id, check)
                selections = [dict(storage=value['storage'], partition=value['partition'],
                                   artifact=artifacts[value['artifact_id']]) for value in intent['selections']]
                check()
                arguments = {'target_storage': intent['target_storage']} if intent.get('target_storage') else {}
                if inspection_scope is not None:
                    arguments['inspection_scope'] = inspection_scope
                try:
                    result = executor.plan(binding, artifacts[intent['artifact_id']], intent['mode'], selections,
                                           intent['verify'], check, **arguments)
                except BaseException as error:
                    if getattr(error, 'cleanup_confirmed', True) is False:
                        # Keep source files pinned until a durable task owns them.
                        error.artifact_pins = stack.pop_all()
                        error.artifact_ids = sorted(artifacts)
                    raise
        try:
            return self._store_plan(binding, intent, actor, serial, result, task_id, prepared or baseline_record)
        except BaseException:
            if (prepared is None and isinstance(result, dict) and result.get('workspace')
                    and hasattr(executor, 'cleanup_plan')):
                try:
                    executor.cleanup_plan(result)
                except (TaskError, OSError):
                    pass
            raise

    def _store_plan(self, binding, intent, actor, serial, result, task_id, prepared=None):
        if not isinstance(result, dict):
            raise TaskError('invalid flash executor plan', 502)
        if type(result.get('requires_prepare', False)) is not bool:
            raise TaskError('invalid flash preparation requirement', 502)
        plan = result.get('plan', result)
        if not isinstance(plan, dict) or not isinstance(plan.get('writes', []), list):
            raise TaskError('invalid flash write plan', 502)
        if any(not isinstance(value, dict) for value in plan.get('writes', [])):
            raise TaskError('invalid flash write entry', 502)
        if intent['mode'] == 'partitions' and (plan.get('write_layout') is not False
                or any(item.get('kind') in ('layout', 'disk') for item in plan.get('writes', []))):
            raise TaskError('partition plan attempted to change disk layout', 502)
        if intent['mode'] == 'partitions' and intent['selections']:
            expected = {(value['storage'], value['partition']) for value in intent['selections']}
            actual = [(value.get('storage'), value.get('partition')) for value in plan.get('writes', [])]
            if len(actual) != len(expected) or set(actual) != expected:
                raise TaskError('flash plan differs from selected partition scope', 502)
        source_workspaces = set((prepared or {}).get('source_workspaces') or [])
        if (prepared or {}).get('workspace') and prepared['workspace'] != result.get('workspace'):
            source_workspaces.add(prepared['workspace'])
        return self.store.put_plan({
            'actor': actor, 'serial': serial, 'binding_revision': _binding_revision(binding),
            'target_id': binding.target_id or None, 'target_generation': binding.target_generation or None,
            'binding_digest': (prepared or {}).get('binding_digest') or _binding_digest(binding), 'intent': intent,
            'executor_plan': plan, 'requires_prepare': result.get('requires_prepare', False),
            'executor_candidates': result.get('payload_candidates', []),
            'execution_profile': result.get('execution_profile') or (prepared or {}).get('execution_profile'),
            'execution_device': result.get('execution_device') or (prepared or {}).get('execution_device'),
            'source_workspaces': sorted(source_workspaces),
            'inspection': result.get('inspection'),
            'pending_checks': result.get('pending_checks', plan.get('pending_checks', [])),
            'payload_candidates': [dict(artifact_id=intent['artifact_id'],
                name=str(value.get('name') or (str(value['storage']) + '/' + str(value['partition'])
                    if value.get('storage') and value.get('partition') else plan.get('artifact', {}).get('name', intent['artifact_id']))),
                size=value.get('size', 0), sha256=value.get('sha256', ''),
                storage=value.get('storage', ''), partition=value.get('partition', ''))
                for value in result.get('payload_candidates', []) if isinstance(value, dict)],
            'created': time.time(),
            'selection_task_id': task_id, 'workspace': result.get('workspace')})

    @staticmethod
    def _preview(record):
        result = _public(record['executor_plan'])
        result.update(id=record['plan_id'], plan_id=record['plan_id'], digest=record['digest'],
                      binding_revision=record['binding_revision'],
                      target_id=record.get('target_id'), target_generation=record.get('target_generation'),
                      requires_prepare=record['requires_prepare'], pending_checks=record['pending_checks'])
        return result

    def plan(self, payload, device, actor='web'):
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'key', 'serial', 'actor', 'client_id', 'binding_revision',
                    'artifact_id', 'mode', 'selections', 'verify', 'target_storage',
                    'target_id', 'target_generation'}:
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
                maintenance.check_preparation()
                record = self._make_plan(binding, executor, intent, actor, device.serial,
                                         maintenance.check_preparation, inspection_scope=inspect_scope)
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
                    row = self.manager.artifacts.get(artifact_id, actor, serial)
                    if row['state'] != 'claimed' or row['task_id'] != task_id:
                        self.manager.artifacts.claim(artifact_id, task_id, actor, serial)
                    claimed.append(artifact_id)
        except BaseException:
            # A registry failure must not close the only remaining expiry pin.
            if pins:
                self.manager._flash_recovery_pins[task_id] = pins
            raise
        else:
            if pins:
                pins.close()

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
                               'binding_revision', 'request_id', 'target_id', 'target_generation'}:
                raise TaskError('flash tasks accept only a fixed plan and board binding')
            if payload.get('target_id') and actor != 'web':
                raise TaskError('automatic USB flash targets require a human operator', 403)
            identity = {key: payload.get(key) for key in ('serial', 'plan_id', 'binding_revision')}
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
            if record['intent']['mode'] == 'full' and not record['executor_plan'].get('writes'):
                raise TaskError('full image preview must list all affected write ranges', 409)
            client = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid flash client')
            operation = FlashOperation(self, binding, executor, record, actor)
            task = self.manager.tasks.start_board('flash', actor, client, serial, operation.run,
                request_id=payload.get('request_id'), input_digest=input_digest,
                details={'plan_id': record['plan_id'], 'plan_digest': record['digest'],
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

    def expire(self):
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
        return removed

    def selection(self, payload, device, actor='web'):
        try:
            if not isinstance(payload, dict) or set(payload) - {
                    'id', 'key', 'serial', 'actor', 'client_id', 'layout_digest', 'selections',
                    'preview', 'plan_id', 'request_id', 'target_id', 'target_generation'}:
                raise TaskError('invalid flash selection request')
            task_id = payload.get('id')
            historical_target = None
            if payload.get('target_id'):
                if actor != 'web':
                    raise TaskError('automatic USB flash targets require a human operator', 403)
                original = self.manager.tasks.store.get(task_id)
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
            submitted = details.get('selection_request')
            if submitted and payload.get('preview') is not True:
                identity = {key: payload.get(key) for key in ('plan_id', 'request_id', 'layout_digest')}
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
                    intent = self._intent(intent)
                    full_review = record['intent']['mode'] == 'full' and record['executor_plan'].get('confirm_after_prepare') is True
                    if full_review and intent['selections']:
                        raise TaskError('firmware confirmation cannot change the full package scope')
                    if not full_review and not intent['selections']:
                        raise TaskError('select at least one target partition')
                    try:
                        cached = (record if record['executor_plan'].get('image_type') in ('package', 'payload', 'payload.gz')
                                  and all(value['artifact_id'] == record['intent']['artifact_id']
                                          for value in intent['selections']) else None)
                        selected = self._make_plan(binding, executor, intent, actor, device.serial,
                                                   operation.check, task_id, prepared=cached,
                                                   baseline_record=record)
                    except BaseException as error:
                        if getattr(error, 'cleanup_confirmed', True) is False:
                            waiter['failure'] = error
                            try:
                                self._retain_sources(error, task_id, actor, device.serial, waiter['claimed'])
                                self.store.update_details(task_id, {'workspace': getattr(error, 'workspace', None)})
                            finally:
                                waiter['event'].set()
                        raise
                    operation.check()
                    current_layout = (selected.get('inspection') or {}).get('layout_digest') or selected['executor_plan'].get('layout_digest')
                    if full_review and (_write_scope(selected['executor_plan']) != _write_scope(record['executor_plan'])
                            or selected['executor_plan'].get('erase_ranges') != record['executor_plan'].get('erase_ranges')):
                        raise TaskError('firmware confirmation changed its reviewed scope', 409)
                    if selected['requires_prepare'] or current_layout != actual:
                        raise TaskError('flash target layout changed during preview', 409)
                    return response(200, {'ok': True, 'plan': self._preview(selected)})
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
                selected_layout = (selected.get('inspection') or {}).get('layout_digest') or selected['executor_plan'].get('layout_digest')
                if selected_layout != actual:
                    raise TaskError('flash selection layout differs from prepared target', 409)
                if record['intent']['mode'] == 'full' and (
                        selected['intent'] != record['intent']
                        or _write_scope(selected['executor_plan']) != _write_scope(record['executor_plan'])
                        or selected['executor_plan'].get('erase_ranges') != record['executor_plan'].get('erase_ranges')):
                    raise TaskError('firmware confirmation differs from the prepared scope', 409)
                added = []
                try:
                    for artifact_id in sorted({value['artifact_id'] for value in selected['intent']['selections']}):
                        if artifact_id not in waiter['claimed']:
                            self.manager.artifacts.claim(artifact_id, task_id, actor, device.serial)
                            added.append(artifact_id)
                    identity = {key: payload.get(key) for key in ('plan_id', 'request_id', 'layout_digest')}
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
        self.attempt_id = secrets.token_hex(12)

    def run(self, context):
        actions, record = self.actions, self.record
        store, maintenance = actions.store, maintenance_for(self.manager)
        claimed, cleanup_confirmed, write_started, outcome = [], True, False, {}
        submitted_full_review = False
        def check():
            context.check()
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
            if record['serial'] and self.manager.store.has_conflict(record['serial']):
                raise TaskError('board identity conflict was detected during flashing', 409)
            if _binding_digest(current) != record['binding_digest']:
                raise TaskError('flash binding changed during execution', 409)
        def event(value):
            value = _public(value)
            store.append_event(context.id, value)
            phase = value.get('phase', 'flash')
            done, total = value.get('done', 0), value.get('total', 0)
            if type(done) is int and type(total) is int and 0 <= done <= total:
                context.progress(done, total, phase)
        self.check = check
        try:
            check()
            for artifact_id in sorted({record['intent']['artifact_id']} | {
                    value['artifact_id'] for value in record['intent']['selections']}):
                self.manager.artifacts.claim(artifact_id, context.id, self.actor, record['serial'])
                claimed.append(artifact_id)
            if record['serial']:
                self.manager.store.event(record['serial'], self.actor, 'flash-start', context.id)
            if record['requires_prepare']:
                submitted_full_review = record['executor_plan'].get('confirm_after_prepare') is True
                if submitted_full_review and (record['intent']['mode'] != 'full'
                        or (self.binding.profile_data.get('storages') or [{}])[0].get('layout_source') != 'spacemit-nor'):
                    raise TaskError('post-prepare firmware confirmation requires the NOR template', 502)
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
                        context.id, self.attempt_id, record['serial'])
                    self.binding = replace(self.binding, device=final_device)
                    store.update_details(context.id, {'execution_device': final_device,
                                                       'allowed_final_generation': self.final_generation})
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
            if (record['intent']['mode'] == 'partitions' and not record['intent']['selections']
                    or submitted_full_review):
                record = self._wait_selection(context, record, event, check, claimed)
            plan = record['executor_plan']
            if plan.get('ready') is False or not plan.get('writes'):
                raise TaskError('flash plan is not ready to write', 409)
            store.update_details(context.id, {'plan_id': record['plan_id'], 'plan_digest': record['digest'],
                                             'workspace': record.get('workspace'),
                                             'source_workspaces': record.get('source_workspaces', []),
                                             'plan': actions._preview(record), 'write_result': 'running'})
            write_started = True
            result = self.executor.execute(self.binding, plan, event, check,
                                           task_id=context.id, attempt_id=self.attempt_id)
            if not isinstance(result, dict):
                raise TaskError('flash executor did not confirm success', 502)
            output = {key: result[key] for key in ('write_result', 'verify_result', 'boot_result',
                                                  'written_bytes', 'verified_bytes') if key in result}
            outcome.update(output)
            store.update_details(context.id, outcome)
            if result.get('ok') is not True:
                raise TaskError('flash executor did not confirm success', 502)
            if output.get('write_result') != 'completed':
                raise TaskError('flash executor did not confirm all planned writes', 502)
            if record['intent']['verify'] == 'required' and output.get('verify_result') != 'verified':
                raise TaskError('required flash verification did not pass', 502)
            output.setdefault('boot_result', 'not-requested')
            output.update(ok=True, plan_id=record['plan_id'], plan_digest=record['digest'])
            store.update_details(context.id, output)
            if record['serial']:
                self.manager.store.event(record['serial'], self.actor, 'flash-complete', context.id)
            return output
        except BaseException as error:
            cleanup_confirmed = getattr(error, 'cleanup_confirmed', True)
            if not cleanup_confirmed:
                actions._retain_sources(error, context.id, self.actor, record['serial'], claimed)
            reported = getattr(error, 'outcome', {})
            outcome.update({key: reported[key] for key in ('write_result', 'verify_result', 'boot_result',
                                                          'written_bytes', 'verified_bytes') if key in reported})
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
