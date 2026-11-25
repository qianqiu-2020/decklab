"""Portable device metadata preferences, stored only in the existing registry."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import secrets
import time

from .profiles import validate_probe_settings
from .storage import validate_label
from .transactions import transactions_for, TransactionError
from .web_settings import SettingsError


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')


def snapshot(manager):
    store = manager.store
    with manager.lock, store.lock:
        document = {'version': 1, 'boards': {row['serial']: {'label': row['label']}
                    for row in store.db.execute('SELECT serial,label FROM boards')}, 'ports': {}}
        for row in store.db.execute('SELECT port,label FROM attachment_labels'):
            document['ports'].setdefault(row['port'], {})['label'] = row['label']
        for row in store.db.execute('SELECT port,document FROM probe_settings'):
            document['ports'].setdefault(row['port'], {})['probe'] = json.loads(row['document'])
        connections = {path: {'connection_id': device.connection_id, 'serial': device.serial}
                       for path, device in manager.devices.items() if device.present}
        conflicts = [row['serial'] for row in store.db.execute('SELECT serial FROM identity_conflicts ORDER BY serial')]
        revision = hashlib.sha256(_bytes([document, connections, conflicts])).hexdigest()
        return {'configuration': document, 'revision': revision, 'connections': connections}


def validate(current, patch):
    if (not isinstance(patch, dict) or type(patch.get('version')) is not int or patch['version'] != 1
            or set(patch) - {'version', 'boards', 'ports'}):
        raise SettingsError('设备偏好格式必须为 version 1')
    document = deepcopy(current)
    changes = []
    for group in ('boards', 'ports'):
        entries = patch.get(group, {})
        if not isinstance(entries, dict) or len(entries) > 4096:
            raise SettingsError('设备偏好集合无效')
        for key, value in entries.items():
            if (not isinstance(key, str) or not key or len(key.encode('utf-8')) > (100 if group == 'boards' else 2048)
                    or any(ord(char) < 32 for char in key) or not isinstance(value, dict)
                    or set(value) - ({'label'} if group == 'boards' else {'label', 'probe'})):
                raise SettingsError('设备偏好字段无效')
            if group == 'boards' and key not in current['boards']:
                raise SettingsError('配置包含尚未登记的板卡，请先识别，或移除该项')
            destination = document[group].setdefault(key, {})
            for field, setting in value.items():
                if setting is None and group == 'ports':
                    destination.pop(field, None)
                elif field == 'label':
                    destination[field] = validate_label(setting)
                else:
                    destination[field] = validate_probe_settings(setting)
            if not destination:
                document[group].pop(key, None)
            if current[group].get(key, {}) != document[group].get(key, {}):
                changes.append(group + ':' + key)
    labels = [value['label'] for value in document['boards'].values()]
    if len(set(labels)) != len(labels):
        raise SettingsError('板卡标签冲突，请为每块板设置不同标签')
    if len(_bytes(document)) > 1024 ** 2:
        raise SettingsError('设备偏好超过 1 MiB')
    return document, changes


def apply(manager, patch, revision, preview=True, client_id=''):
    store = manager.store
    with manager.lock, store.lock:
        before = snapshot(manager)
        if revision != before['revision']:
            raise SettingsError('设备偏好或连接已变化，请重新加载', 409)
        document, changes = validate(before['configuration'], patch)
        changed_boards = [key[7:] for key in changes if key.startswith('boards:')]
        changed_ports = [key[6:] for key in changes if key.startswith('ports:')]
        for serial in changed_boards:
            if store.db.execute('SELECT 1 FROM identity_conflicts WHERE serial=?', (serial,)).fetchone():
                raise SettingsError('存在身份冲突，请先核实档案归属', 409)
            if store.db.execute('SELECT 1 FROM flash_resource_holds WHERE resource=?', ('board:' + serial,)).fetchone():
                raise SettingsError('板卡处于刷机占用或恢复保留中', 423)
        if not preview:
            with ExitStack() as stack:
                devices = [device for device in manager.devices.values() if device.present
                           and (device.by_path in changed_ports or device.serial in changed_boards)]
                for device in devices:
                    if not device.operation_lock.acquire(blocking=False):
                        raise TransactionError('busy', '设备操作仍在进行中')
                    stack.callback(device.operation_lock.release)
                    transactions_for(device).check_owner('web', client_id)
                    if device.file_transfer.active:
                        raise TransactionError('transfer-busy', '文件传输仍在进行中')
                # No writes to configuration files and no new identity records.
                # One SQLite transaction covers labels, probes and audit events.
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    if snapshot(manager)['revision'] != revision:
                        raise SettingsError('设备偏好已被修改，请重新加载', 409)
                    # Temporary labels allow an explicitly reviewed label swap
                    # without violating boards.label's uniqueness constraint.
                    for serial in changed_boards:
                        store.db.execute('UPDATE boards SET label=? WHERE serial=?',
                                         ('settings-' + secrets.token_hex(16), serial))
                    for serial in changed_boards:
                        store.db.execute('UPDATE boards SET label=? WHERE serial=?',
                                         (document['boards'][serial]['label'], serial))
                    for port in changed_ports:
                        preference = document['ports'].get(port, {})
                        for table, field in (('attachment_labels', 'label'), ('probe_settings', 'probe')):
                            store.db.execute('DELETE FROM ' + table + ' WHERE port=?', (port,))
                            if field in preference:
                                value = preference[field] if field == 'label' else json.dumps(preference[field], ensure_ascii=False)
                                store.db.execute('INSERT INTO ' + table + ' VALUES(?,?)', (port, value))
                    audited = set(changed_boards) | {device.serial for device in devices
                                                     if device.serial and device.by_path in changed_ports}
                    for serial in audited:
                        store.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                                         (serial, time.time(), 'web', 'preferences-import', 'labels / port preferences'))
                for serial in changed_boards:
                    manager.boards[serial].update(store.get(serial))
                for device in devices:
                    if device.by_path in changed_ports:
                        device.attachment_label = document['ports'].get(device.by_path, {}).get('label', '')
                        device.auto_identify_started = True
        return {'ok': True, 'preview': preview, 'changes': changes,
                'revision': snapshot(manager)['revision'], 'configuration': document}
