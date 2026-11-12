"""Read-only USB discovery and explicit flash targets, separate from UART identity."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from types import SimpleNamespace

from .board_maintenance import maintenance_for
from .flash_runner import FlashBinding, BoardControl, SerialEndpoint
from .models import Board
from .tasks import TaskError


def executor_command():
    override = os.environ.get('SERIALD_FLASH_EXECUTOR')
    candidates = ([Path(override)] if override else [
        Path('/usr/libexec/seriald/decklab-flash'),
        Path(__file__).resolve().parents[1] / 'decklab-flash',
        Path(__file__).resolve().parents[1] / 'decklab-flash.exe'])
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return (str(candidate),)
    return ()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _diagnostic(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise TaskError('flashing discovery returned an invalid diagnostic', 502)
    return ''.join(char if char.isprintable() else ' ' for char in value).strip()[:512]


def _selector(value):
    if not isinstance(value, dict):
        raise TaskError('flashing discovery returned an invalid USB selector', 502)
    ports = value.get('port_path')
    if (not isinstance(ports, list) or not 2 <= len(ports) <= 16
            or any(type(port) is not int or not 1 <= port <= (65535 if index == 0 else 255)
                   for index, port in enumerate(ports))):
        raise TaskError('flashing discovery did not return a fixed physical USB path', 502)
    result = {'port_path': list(ports)}
    for key in ('vendor_id', 'product_id', 'usb_address'):
        if key in value:
            upper = 127 if key == 'usb_address' else 65535
            if type(value[key]) is not int or not 1 <= value[key] <= upper:
                raise TaskError('flashing discovery returned an invalid USB identifier', 502)
            result[key] = value[key]
    if value.get('usb_serial'):
        serial = value['usb_serial']
        if not isinstance(serial, str) or len(serial.encode()) > 128 or any(ord(c) < 32 for c in serial):
            raise TaskError('flashing discovery returned an invalid USB serial', 502)
        result['usb_serial'] = serial
    return result


def _profile(value):
    if not isinstance(value, dict) or len(_encoded(value).encode()) > 512 * 1024:
        raise TaskError('flashing discovery returned an invalid board profile', 502)
    # These profiles come from fixed executor templates. USB data cannot name
    # host loaders, executables, paths or a different attachment.
    def inspect(item):
        if isinstance(item, dict):
            if set(item) & {'path', 'argv', 'prepare', 'download_device', 'source_path', 'workspace'}:
                raise TaskError('automatic board profile contains host-controlled fields', 502)
            for child in item.values():
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)
    if value.get('id') not in ('auto-live-layout-v1', 'auto-spacemit-gpt-v1'):
        raise TaskError('automatic board profile is not a supported executor template', 502)
    if set(value) - {'id', 'identity', 'storages', 'chip_family', 'bootrom_identity', 'ram_loader_protocol'}:
        raise TaskError('automatic board profile contains unsupported capabilities', 502)
    if value.get('ram_loader_protocol') and (value['ram_loader_protocol'] != 'decklab-nor-v1'
            or value['id'] != 'auto-spacemit-gpt-v1' or value.get('chip_family') != 'k3'
            or value.get('bootrom_identity')):
        raise TaskError('unsupported live RAM loader capability', 502)
    if value.get('chip_family') not in (None, '', 'k1', 'k3'):
        raise TaskError('automatic board profile contains an unsupported chip family', 502)
    storages = value.get('storages')
    if not isinstance(storages, list) or len(storages) != 1:
        raise TaskError('automatic board profile must describe one checked storage', 502)
    for storage in storages:
        if not isinstance(storage, dict) or storage.get('select_oem') or storage.get('layout_target') or storage.get('verify_getvar'):
            raise TaskError('automatic board profile contains unsupported device commands', 502)
        storage_id = storage.get('id')
        if not isinstance(storage_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', storage_id):
            raise TaskError('automatic board profile contains an invalid storage', 502)
        if value['id'] == 'auto-live-layout-v1':
            expected = {'size_getvar': 'storage-size:' + storage_id,
                        'sector_size_getvar': 'logical-sector-size:' + storage_id,
                        'layout_getvar': 'partition-layout:' + storage_id}
            if any(storage.get(key) != item for key, item in expected.items()) or storage.get('layout_source'):
                raise TaskError('automatic live layout queries differ from the fixed template', 502)
            if storage.get('full_disk_target') not in (None, '', 'disk'):
                raise TaskError('automatic profile contains an unsupported whole-disk target', 502)
        elif (storage_id != 'active' or storage.get('layout_source') != 'spacemit-gpt'
              or storage.get('size_getvar') != '__decklab_gpt_capacity'
              or any(storage.get(key) for key in ('full_disk_target', 'sector_size_getvar', 'layout_getvar'))):
            raise TaskError('automatic GPT layout differs from the fixed executor template', 502)
    return value


def _strong_serial(candidate):
    serial = candidate.get('serial')
    identity = candidate.get('identity')
    if (candidate.get('strong_serial') is not True or candidate.get('serial_source') != 'fastboot'
            or not isinstance(identity, dict) or identity.get('serialno') != serial
            or not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{3,99}', serial)):
        return ''
    compact = serial.lower().replace('-', '').replace(':', '')
    if (compact in ('unknown', 'unknow', 'default', 'serial', 'serialno', '0123456789', '123456789',
                    '12345678', '1234567890', 'abcdef', 'none', 'null') or len(set(compact)) <= 1):
        return ''
    if compact in ('dfudevice', 'spacemit', 'fastboot', 'device', '123456789abcdef', '0123456789abcdef'):
        return ''
    return serial


class FlashDiscovery:
    def __init__(self, manager, executor, argv=None):
        self.manager, self.store, self.executor = manager, manager.store, executor
        self.argv = tuple(argv) if argv is not None else executor_command()
        self.lock = threading.RLock()

    def _rows(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute('SELECT * FROM flash_targets')]

    def _save(self, row):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO flash_targets VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,serial=excluded.serial,'
                'document=excluded.document,seen=excluded.seen,present=excluded.present',
                (row['id'], row['generation'], row.get('serial') or None,
                 _encoded(row['document']), row['seen'], int(row['present'])))

    def get(self, target_id, generation=None, require_present=True):
        if not isinstance(target_id, str) or not re.fullmatch(r'[0-9a-f]{24}', target_id):
            raise TaskError('USB flash target not found', 404)
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM flash_targets WHERE id=?', (target_id,)).fetchone()
        if row is None:
            raise TaskError('USB flash target not found', 404)
        result = dict(row)
        result['document'] = json.loads(result['document'])
        if (generation is not None and result['generation'] != generation
                or require_present and not result['present']):
            raise TaskError('USB flash connection changed; detect the device again', 409)
        return result

    def public(self, row):
        data = row['document']
        return {'id': row['id'], 'target_id': row['id'], 'generation': row['generation'],
                'target_generation': row['generation'], 'serial': row.get('serial') or '',
                'label': data.get('label', 'USB ' + '.'.join(map(str, data['device']['port_path']))),
                'usb': data['device'], 'mode': data.get('mode', 'unknown'),
                'supported': bool(data.get('supported')), 'reason': data.get('reason', ''),
                'diagnostic': data.get('diagnostic', ''),
                'present': bool(row['present']), 'identity': data.get('identity', {}),
                'chip': data.get('chip', '')}

    def refresh(self):
        with self.lock:
            try:
                return self._refresh()
            except (TaskError, ValueError, OSError) as error:
                retained = self._recoverable_targets()
                if retained:
                    return {'available': False, 'reason': 'USB discovery is unavailable; retained tasks can still be managed',
                            'targets': retained}
                raise

    def _recoverable_targets(self, exclude=()):
        """Outstanding executions stay reachable even after their cable vanishes."""
        with self.store.lock:
            rows = self.store.db.execute('SELECT DISTINCT f.* FROM flash_targets f '
                'JOIN task_flash_targets x ON x.target_id=f.id JOIN tasks t ON t.id=x.task_id '
                "WHERE t.state IN ('queued','running') OR EXISTS "
                '(SELECT 1 FROM flash_resource_holds h WHERE h.task_id=t.id) ORDER BY f.id').fetchall()
        result = []
        for value in rows:
            if value['id'] in exclude:
                continue
            row = dict(value)
            row['document'] = json.loads(row['document'])
            row['present'] = False
            target = self.public(row)
            target.update(supported=False,
                          reason='USB device is offline or unverified; an active or recovery task remains')
            result.append(target)
        return result

    def _refresh(self):
        if not self.argv:
            return {'available': False, 'reason': 'bundled flashing executor is unavailable',
                    'targets': self._recoverable_targets()}
        result = self.executor.discover(self.argv, probe=False)
        devices = result.get('devices')
        if not isinstance(devices, list) or len(devices) > 128:
            raise TaskError('flashing discovery returned an invalid device list', 502)
        candidates = []
        for candidate in devices:
            if not isinstance(candidate, dict):
                raise TaskError('flashing discovery returned an invalid device', 502)
            selector = _selector(candidate.get('device'))
            candidates.append((selector, candidate))
        ports = [tuple(selector['port_path']) for selector, _ in candidates]
        if len(ports) != len(set(ports)):
            raise TaskError('multiple flashing devices occupy the same physical selector', 409)
        with self.lock:
            existing = self._rows()
            seen = set()
            for selector, candidate in candidates:
                endpoint = '.'.join(map(str, selector['port_path']))
                old = next((row for row in existing if json.loads(row['document']).get('endpoint') == endpoint), None)
                fingerprint = hashlib.sha256(_encoded(selector).encode()).hexdigest()
                unchanged = old and old['present'] and json.loads(old['document']).get('fingerprint') == fingerprint
                document = json.loads(old['document']) if unchanged else {
                    'endpoint': endpoint, 'fingerprint': fingerprint, 'device': selector,
                    'mode': candidate.get('mode', 'unknown'), 'supported': False,
                    'reason': 'select this USB device to read its flashing capabilities'}
                row = {'id': old['id'] if old else secrets.token_hex(12),
                       'generation': old['generation'] if unchanged else secrets.token_hex(12),
                       'serial': old['serial'] if unchanged else None, 'document': document,
                       'seen': time.time(), 'present': True}
                self._save(row)
                seen.add(row['id'])
            with self.store.lock, self.store.db:
                for row in existing:
                    if row['id'] not in seen:
                        self.store.db.execute('UPDATE flash_targets SET present=0 WHERE id=?', (row['id'],))
            targets = [self.public(self.get(target_id)) for target_id in sorted(seen)]
            targets.extend(self._recoverable_targets(exclude=seen))
        self.manager.refresh_flash_presence()
        return {'available': True, 'targets': targets}

    def view(self, target_id, generation=None, require_present=True):
        row = self.get(target_id, generation, require_present)
        return SimpleNamespace(serial=row.get('serial') or '', target_id=row['id'],
                               target_generation=row['generation'], key='', row=row)

    def binding(self, target_id, generation=None):
        row = self.get(target_id, generation)
        data = row['document']
        if not data.get('supported') or not data.get('profile'):
            raise TaskError(data.get('reason') or 'USB device does not provide a supported flashing layout', 409)
        profile = _profile(data['profile'])
        return FlashBinding(row.get('serial') or '', 1, str(profile.get('id') or 'auto-detected'), profile,
            BoardControl(), SerialEndpoint('none'), 'auto-' + row['id'], self.argv, data['device'],
            target_id=row['id'], target_generation=row['generation'])

    def probe(self, target_id, generation):
        if not isinstance(generation, str) or not generation:
            raise TaskError('USB target generation is required')
        self.refresh()
        row = self.get(target_id, generation)
        data = row['document']
        resources = ['flash-usb:local:' + '.'.join(map(str, data['device']['port_path']))]
        maintenance = maintenance_for(self.manager)
        with self.lock, maintenance.inspect_target_scope(target_id, generation, row.get('serial') or '', resources):
            try:
                result = self.executor.discover(self.argv, probe=True, device=data['device'])
            except BaseException as error:
                if getattr(error, 'cleanup_confirmed', True) is False:
                    task = maintenance.retain_target_inspection_failure(target_id, generation,
                        row.get('serial') or '', resources, attempt_id=secrets.token_hex(12),
                        details={'target_id': target_id, 'target_generation': generation})
                    error.recovery_task_id = task['id']
                raise
            devices = result.get('devices')
            if not isinstance(devices, list) or len(devices) != 1:
                raise TaskError('selected USB flash target is missing or ambiguous', 409)
            candidate = devices[0]
            if not isinstance(candidate, dict) or not isinstance(candidate.get('identity', {}), dict):
                raise TaskError('flashing discovery returned invalid identity metadata', 502)
            if _selector(candidate.get('device')) != data['device']:
                raise TaskError('USB connection changed during capability detection', 409)
            serial = _strong_serial(candidate)
            if serial:
                duplicate = any(other['id'] != row['id'] and other['present'] and other['serial'] == serial
                                for other in self._rows())
                if duplicate or self.store.has_conflict(serial):
                    self.store.mark_conflict(serial)
                    for other in self._rows():
                        if other['serial'] == serial:
                            other['document'] = json.loads(other['document'])
                            other['document'].update(supported=False, reason='conflicting USB board identity')
                            self._save(other)
                    raise TaskError('conflicting USB board identity; resolve the duplicate before flashing', 409)
                record = self.store.get(serial)
                if record is None:
                    info = {'serial': serial, 'serial_source': 'fastboot', 'identity_status': 'confirmed',
                            'hostname': candidate.get('identity', {}).get('product', '')}
                    record = self.store.bind(info, '', '')
                with self.manager.lock:
                    if serial not in self.manager.boards:
                        self.manager.boards[serial] = Board(record)
            profile = _profile(candidate['profile']) if candidate.get('profile') is not None else None
            data.update(identity=candidate.get('identity') or {}, mode=candidate.get('mode', 'unknown'),
                        chip=candidate.get('chip', ''), profile=profile,
                        inspection=candidate.get('inspection'), modes=candidate.get('modes') or [],
                        supported=candidate.get('supported') is True and profile is not None,
                        reason=str(candidate.get('reason') or '')[:512],
                        diagnostic=_diagnostic(candidate.get('diagnostic')))
            data['preparable'] = bool(candidate.get('preparable') or candidate.get('can_prepare_from_image'))
            if 'usb_address' not in data['device']:
                data.update(supported=False, reason='USB connection generation is unavailable; reconnect and detect again')
            data['label'] = ((self.store.get(serial) or {}).get('label') if serial else None) or (
                str(data['identity'].get('product') or data.get('chip') or 'USB') + ' · ' + data['endpoint'])
            row.update(serial=serial or None, document=data, seen=time.time())
            self._save(row)
        self.manager.refresh_flash_presence()
        return self.public(row)

    def accept_prepared_target(self, target_id, initial_generation, final_device, task_id, attempt_id,
                               expected_serial=''):
        """Record one engine-verified mode transition owned by the active task.

        Ordinary discovery still changes generations on attachment changes;
        this method supplies the task with its explicitly approved final one.
        """
        selector = _selector(final_device)
        if 'usb_address' not in selector:
            raise TaskError('prepared USB connection generation is unavailable', 409)
        with self.lock:
            with self.store.lock:
                task = self.store.db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
                snapshot = self.store.db.execute('SELECT generation FROM task_flash_targets '
                    'WHERE task_id=? AND target_id=?', (task_id, target_id)).fetchone()
                details = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?',
                                                (task_id,)).fetchone()
                needed = ['flash-target:' + target_id,
                          'flash-usb:local:' + '.'.join(map(str, selector['port_path']))]
                holds = self.store.db.execute('SELECT resource,task_id,state FROM flash_resource_holds '
                    'WHERE resource IN (?,?)', needed).fetchall()
            if (task is None or task['state'] != 'running' or snapshot is None
                    or snapshot['generation'] != initial_generation or details is None
                    or json.loads(details['document']).get('attempt_id') != attempt_id
                    or len(holds) != len(needed) or any(value['task_id'] != task_id or value['state'] != 'active'
                                                     for value in holds)):
                raise TaskError('USB transition does not belong to the active flashing attempt', 409)
            self.refresh()
            row = self.get(target_id, require_present=False)
            data = row['document']
            if selector != data['device'] or not row['present']:
                raise TaskError('prepared USB target changed before its transition was recorded', 409)
            row.update(generation=secrets.token_hex(12), serial=expected_serial or None,
                       present=True, seen=time.time())
            data.update(device=selector, fingerprint=hashlib.sha256(_encoded(selector).encode()).hexdigest(),
                        mode='loader', supported=False, preparable=False,
                        reason='task prepared this loader; detect again after the task finishes')
            row['document'] = data
            self._save(row)
            return row['generation']
