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
from .storage import IdentityError
from .tasks import TaskError


def executor_command():
    override = os.environ.get('DECKLAB_FLASH_EXECUTOR')
    candidates = ([Path(override)] if override else [
        Path('/usr/libexec/decklab/decklab-flash'),
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


def _link_speed(candidate):
    speed = candidate.get('usb_link_speed')
    return speed if type(speed) is int and 1 <= speed <= 6 else None


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
    if value.get('ram_loader_protocol') and (value['ram_loader_protocol'] != 'spacemit-ram-v1'
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
    if (candidate.get('strong_serial') is not True or candidate.get('serial_source') not in ('fastboot', 'eeprom')
            or not isinstance(identity, dict) or identity.get('serialno') != serial
            or not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{3,99}', serial)):
        return ''
    if candidate.get('serial_source') == 'eeprom' and identity.get('eeprom-serial') != serial:
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
        self.lock = manager.flash_discovery_lock if hasattr(manager, 'flash_discovery_lock') else threading.RLock()

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
                'usb_link_speed': _link_speed(data) if row['present'] else None,
                'supported': bool(data.get('supported')), 'reason': data.get('reason', ''),
                'diagnostic': data.get('diagnostic', ''),
                'present': bool(row['present']), 'identity': data.get('identity', {}),
                'chip': data.get('chip', ''), 'console_link': self._public_link(row)}

    def _public_link(self, row):
        link = row['document'].get('uart_link')
        if not link:
            return None
        result = {'state': link.get('state', ''), 'reason': link.get('reason', '')}
        for key in ('serial', 'usb_serial'):
            if link.get(key):
                result[key] = link[key]
                result[key.replace('serial', 'label')] = (self.store.get(link[key]) or {}).get('label', '')
        return result

    def switch_bindings(self, serial, connection_id):
        """Snapshot inactive associations before detaching a UART identity."""
        rows = []
        maintenance = maintenance_for(self.manager)
        if maintenance.holds(serial):
            raise IdentityError('板卡仍被刷机或恢复任务占用，请先结束原任务')
        for row in self._rows():
            row['document'] = json.loads(row['document'])
            link = row['document'].get('uart_link') or {}
            if row.get('serial') != serial and link.get('connection_id') != connection_id and link.get('serial') != serial:
                continue
            if maintenance.target_holds(row['id']):
                raise IdentityError('刷机 USB 仍被任务占用，请先结束原任务')
            rows.append(row)
        return rows

    def clear_switch_bindings(self, serial, rows):
        for row in rows:
            data = row['document']
            data.pop('uart_link', None)
            if row['serial'] in (None, '', serial):
                row['serial'], row['generation'] = None, secrets.token_hex(12)
                keys = ('endpoint', 'fingerprint', 'device', 'mode', 'superseded_by')
                row['document'] = {name: data[name] for name in keys if name in data}
                row['document'].update(supported=False, reason='select this USB device to read its flashing capabilities')
            self._save(row)

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
            by_endpoint = {}
            for row in existing:
                document = json.loads(row['document'])
                if not document.get('superseded_by'):
                    by_endpoint.setdefault(document.get('endpoint'), row)
            seen = set()
            for selector, candidate in candidates:
                endpoint = '.'.join(map(str, selector['port_path']))
                old = by_endpoint.get(endpoint)
                fingerprint = hashlib.sha256(_encoded(selector).encode()).hexdigest()
                unchanged = old and old['present'] and json.loads(old['document']).get('fingerprint') == fingerprint
                document = json.loads(old['document']) if unchanged else {
                    'endpoint': endpoint, 'fingerprint': fingerprint, 'device': selector,
                    'mode': candidate.get('mode', 'unknown'), 'supported': False,
                    'reason': 'select this USB device to read its flashing capabilities'}
                document['usb_link_speed'] = _link_speed(candidate)
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
        response = {'available': True, 'targets': targets}
        if os.name == 'nt' and not candidates:
            from .windows_flash_usb import empty_scan_reason
            response['reason'] = empty_scan_reason()
        return response

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
        links = getattr(self.manager, 'console_links', None)
        token = links.start() if links is not None else None
        echoed, pending_bind = [], None
        try:
            with self.lock, maintenance.inspect_target_scope(target_id, generation, row.get('serial') or '', resources):
                row, pending_bind = self._probe_locked(row, data, target_id, generation, resources,
                                                       maintenance, links, token)
        finally:
            if token:
                links.cancel(token)
        if pending_bind is not None:
            self._bind_console(row, *pending_bind)
        self.manager.refresh_flash_presence()
        return self.public(row)

    def _probe_locked(self, row, data, target_id, generation, resources, maintenance, links, token):
        pending_bind = None
        try:
            if token:
                result = self.executor.discover(self.argv, probe=True, device=data['device'], link_token=token)
            else:
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
                info = {'serial': serial, 'serial_source': candidate.get('serial_source', 'fastboot'), 'identity_status': 'confirmed',
                        'hostname': candidate.get('identity', {}).get('product', '')}
                record = self.store.bind(info, '', '')
            with self.manager.lock:
                if serial not in self.manager.boards:
                    self.manager.boards[serial] = Board(record)
        profile = _profile(candidate['profile']) if candidate.get('profile') is not None else None
        data.update(identity=candidate.get('identity') or {}, mode=candidate.get('mode', 'unknown'),
                    usb_link_speed=_link_speed(candidate),
                    chip=candidate.get('chip', ''), profile=profile,
                    inspection=candidate.get('inspection'), modes=candidate.get('modes') or [],
                    supported=candidate.get('supported') is True and profile is not None,
                    reason=str(candidate.get('reason') or '')[:512],
                    diagnostic=_diagnostic(candidate.get('diagnostic')))
        data['preparable'] = bool(candidate.get('preparable') or candidate.get('can_prepare_from_image'))
        if 'usb_address' not in data['device']:
            data.update(supported=False, reason='USB connection generation is unavailable; reconnect and detect again')
        echoed = links.finish(token) if token and candidate.get('link_sent') is True else []
        link, console = self._link_document(echoed, serial)
        if link is None:
            data.pop('uart_link', None)
        else:
            data['uart_link'] = link
            if link['state'] == 'pending-bind':
                pending_bind = (console, serial, link['connection_id'])
        named = serial or (link or {}).get('state') == 'linked' and link.get('serial')
        data['label'] = ((self.store.get(named) or {}).get('label') if named else None) or (
            str(data['identity'].get('product') or data.get('chip') or 'USB') + ' · ' + data['endpoint'])
        row.update(serial=serial or None, document=data, seen=time.time())
        self._save(row)
        return row, pending_bind

    @staticmethod
    def _link_document(echoed, usb_serial):
        """Classify one link nonce's console echoes; only a unique echo is evidence."""
        if not echoed:
            return None, None
        if len(echoed) > 1:
            return {'state': 'ambiguous', 'consoles': len(echoed), 'seen': time.time()}, None
        console = echoed[0]
        uart = console.serial or ''
        link = {'connection_id': console.connection_id, 'by_path': console.by_path,
                'serial': uart, 'seen': time.time()}
        if usb_serial and uart and uart != usb_serial:
            link.update(state='mismatch', usb_serial=usb_serial)
        elif usb_serial and not uart:
            link.update(state='pending-bind', serial=usb_serial)
        else:
            link['state'] = 'linked' if (uart or usb_serial) else 'paired'
            link['serial'] = uart or usb_serial
        return link, console

    def _bind_console(self, row, console, serial, connection_id):
        """Bind a linked, unidentified console after discovery locks are released."""
        try:
            self.manager.bind_linked_console(console, serial, connection_id)
            state, reason = 'linked', ''
        except (IdentityError, OSError) as error:
            state, reason = 'bind-failed', str(error)[:256]
        with self.lock:
            try:
                current = self.get(row['id'], row['generation'], require_present=False)
            except TaskError:
                return
            link = current['document'].get('uart_link') or {}
            if link.get('connection_id') != connection_id:
                return
            link.update(state=state, serial=serial)
            if reason:
                link['reason'] = reason
            else:
                link.pop('reason', None)
                current['document']['label'] = (self.store.get(serial) or {}).get('label') or current['document'].get('label')
            current['document']['uart_link'] = link
            self._save(current)
            row.update(document=current['document'])

    def _linked_console(self, link):
        connection_id = (link or {}).get('connection_id')
        if not connection_id:
            return None
        with self.manager.lock:
            return next((device for device in self.manager.devices.values()
                         if device.connection_id == connection_id and device.present), None)

    def accept_prepared_target(self, target_id, initial_generation, final_device, task_id, attempt_id,
                               expected_serial='', board_serial=''):
        """Record one engine-verified mode transition owned by the active task.

        Ordinary discovery still changes generations on attachment changes;
        this method supplies the task with its explicitly approved final one.
        """
        selector = _selector(final_device)
        if 'usb_address' not in selector:
            raise TaskError('prepared USB connection generation is unavailable', 409)
        with self.lock:
            original = self.get(target_id, require_present=False)['document']['device']
            # A SuperSpeed link moves the RAM U-Boot to the USB 3 side of the
            # same connector. The executor followed it only by the board serial
            # the FSBL stage reported; the task also holds the new address.
            relocated = selector['port_path'] != original['port_path']
            # SuperSpeed RAM U-Boot has no USB serial; the executor matched its
            # EEPROM serial with the one the FSBL stage reported.
            if relocated and (not board_serial or expected_serial and board_serial != expected_serial
                              or selector.get('usb_serial') and selector['usb_serial'] != board_serial):
                raise TaskError('prepared loader moved to another USB address without the board serial', 409)
            moved_resource = 'flash-usb:local:' + '.'.join(map(str, selector['port_path']))
            with self.store.lock:
                task = self.store.db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
                snapshot = self.store.db.execute('SELECT generation FROM task_flash_targets '
                    'WHERE task_id=? AND target_id=?', (task_id, target_id)).fetchone()
                details = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?',
                                                (task_id,)).fetchone()
                needed = ['flash-target:' + target_id,
                          'flash-usb:local:' + '.'.join(map(str, original['port_path']))]
                holds = self.store.db.execute('SELECT resource,task_id,state FROM flash_resource_holds '
                    'WHERE resource IN (?,?)', needed).fetchall()
                if (relocated and task is not None and task['state'] == 'running' and len(holds) == len(needed)
                        and all(value['task_id'] == task_id and value['state'] == 'active' for value in holds)):
                    with self.store.db:
                        taken = self.store.db.execute('SELECT task_id FROM flash_resource_holds WHERE resource=?',
                                                      (moved_resource,)).fetchone()
                        if taken is not None and taken['task_id'] != task_id:
                            raise TaskError('the USB address the loader moved to belongs to another task', 409)
                        if taken is None:
                            self.store.db.execute('INSERT INTO flash_resource_holds VALUES(?,?,?,?)',
                                                  (moved_resource, task_id, 'active', time.time()))
            if (task is None or task['state'] != 'running' or snapshot is None
                    or snapshot['generation'] != initial_generation or details is None
                    or json.loads(details['document']).get('attempt_id') != attempt_id
                    or len(holds) != len(needed) or any(value['task_id'] != task_id or value['state'] != 'active'
                                                     for value in holds)):
                raise TaskError('USB transition does not belong to the active flashing attempt', 409)
            # The engine-verified transition keeps the same board, so its console
            # pairing survives the loader's USB re-enumeration.
            link = self.get(target_id, require_present=False)['document'].get('uart_link')
            if relocated:
                # Keep this target: refresh matches rows by endpoint.
                row = self.get(target_id, require_present=False)
                endpoint = '.'.join(map(str, selector['port_path']))
                with self.store.lock, self.store.db:
                    used = {value['target_id'] for value in
                            self.store.db.execute('SELECT target_id FROM task_flash_targets').fetchall()}
                    for other in self._rows():
                        data = json.loads(other['document'])
                        if other['id'] == target_id or data.get('endpoint') != endpoint or data.get('superseded_by'):
                            continue
                        if maintenance_for(self.manager).target_holds(other['id']):
                            raise TaskError('the previous USB target is reserved by another task', 423)
                        if other['id'] in used:
                            # Keep historical task references, but remove the old
                            # target from future discovery at this physical path.
                            data['superseded_by'] = target_id
                            self.store.db.execute('UPDATE flash_targets SET present=0,document=? WHERE id=?',
                                                  (_encoded(data), other['id']))
                        else:
                            self.store.db.execute('DELETE FROM flash_targets WHERE id=?', (other['id'],))
                row['document'].update(endpoint=endpoint, device=selector,
                                       fingerprint=hashlib.sha256(_encoded(selector).encode()).hexdigest())
                self._save(row)
            self.refresh()
            row = self.get(target_id, require_present=False)
            data = row['document']
            if link:
                data['uart_link'] = link
            if selector != data['device'] or not row['present']:
                raise TaskError('prepared USB target changed before its transition was recorded', 409)
            # A fresh USB-only board has no registry row yet. Its verified
            # identity is registered by accept_eeprom_identity, once that
            # method has checked conflicts and the board lease.
            known_serial = expected_serial if expected_serial and self.store.get(expected_serial) else None
            row.update(generation=secrets.token_hex(12), serial=known_serial, present=True, seen=time.time())
            data.update(device=selector, fingerprint=hashlib.sha256(_encoded(selector).encode()).hexdigest(),
                        mode='loader', supported=False, preparable=False,
                        reason='task prepared this loader; detect again after the task finishes')
            row['document'] = data
            self._save(row)
            return row['generation']

    def accept_eeprom_identity(self, target_id, generation, identity, task_id, attempt_id):
        """Associate a checked RAM identity while extending the same board lease.

        Original request/artifact scopes stay immutable. The resolved serial is
        additional task visibility, never an inferred USB descriptor identity.
        """
        serial = identity.get('serialno') if isinstance(identity, dict) and identity.get('eeprom-serial') == identity.get('serialno') else ''
        if not serial:
            return ''
        if not _strong_serial({'serial': serial, 'serial_source': 'eeprom',
                'strong_serial': True, 'identity': identity}):
            raise TaskError('invalid EEPROM board identity', 409)
        maintenance = maintenance_for(self.manager)
        with maintenance._lock, self.lock:
            row = self.get(target_id, generation)
            with self.store.lock:
                task = self.store.db.execute('SELECT state,actor,client_id FROM tasks WHERE id=?', (task_id,)).fetchone()
                details = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?', (task_id,)).fetchone()
            holds = maintenance.target_holds(target_id)
            if (task is None or task['state'] != 'running' or details is None
                    or json.loads(details['document']).get('attempt_id') != attempt_id
                    or not holds or any(h['task_id'] != task_id or h['state'] != 'active' for h in holds)):
                raise TaskError('EEPROM identity does not belong to the active flash task', 409)
            if row.get('serial') and row['serial'] != serial:
                raise TaskError('EEPROM board identity differs from the selected board', 409)
            if self.store.has_conflict(serial) or any(other['id'] != target_id and other['present']
                    and other['serial'] == serial for other in self._rows()):
                self.store.mark_conflict(serial)
                raise TaskError('conflicting EEPROM board identity', 409)
            record = self.store.get(serial)
            if record is None:
                record = self.store.bind({'serial': serial, 'serial_source': 'eeprom',
                    'identity_status': 'confirmed', 'hostname': identity.get('product', '')}, '', '')
            with self.manager.lock:
                if serial not in self.manager.boards:
                    self.manager.boards[serial] = Board(record)
            board_holds = maintenance.holds(serial)
            if not board_holds:
                maintenance.reserve(serial, task_id, actor=task['actor'], client_id=task['client_id'])
            elif any(h['task_id'] != task_id or h['state'] != 'active' for h in board_holds):
                raise TaskError('identified board is reserved by another task', 423)
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO task_board_targets VALUES(?,?)', (task_id, serial))
            row['serial'] = serial
            row['document']['identity'] = dict(identity)
            row['document']['label'] = record.get('label') or serial
            link = row['document'].get('uart_link') or {}
            console = None
            if link.get('state') == 'paired':
                console = self._linked_console(link)
                if console is None:
                    row['document'].pop('uart_link', None)
                elif console.serial:
                    link.update(state='linked' if console.serial == serial else 'mismatch',
                                serial=console.serial, usb_serial=serial)
                    console = None
            elif link.get('state') == 'linked' and link.get('serial') != serial:
                link.update(state='mismatch', usb_serial=serial)
            self._save(row)
        if console is not None:
            # The RAM loader supplied the identity this paired console lacked.
            self._bind_console(row, console, serial, link['connection_id'])
        self.manager.refresh_flash_presence()
        return serial
