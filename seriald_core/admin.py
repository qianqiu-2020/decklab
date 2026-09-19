"""Explicit, repeatable import of legacy path-based archives; never touches hardware."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from .archive import copy_atomic, digest, finish_staged
from .config import BASE
from .storage import Store, archive_component, serial_component, validate_label
from .identity import usable_serial


def _copy_log(source, target):
    copy_atomic(str(source), str(target))


def _archive_sources(base, serial, info):
    path = base / 'logs' / archive_component(serial, info.get('serial_source', ''))
    return [path] if path.is_dir() else []


def _plan_archive_move(sources, target):
    plan = []
    destinations = {}
    for source_dir in sources:
        if source_dir.is_symlink():
            raise ValueError('档案目录不可为符号链接')
        for source in sorted(source_dir.glob('*.log')):
            if source.is_symlink() or not source.is_file():
                continue
            destination = target / source.name
            previous = destinations.get(destination)
            if previous and digest(previous) != digest(source):
                raise ValueError('多个源目录含不同的同名日志: ' + source.name)
            destinations[destination] = source
            plan.append((source, destination))
    for source, destination in plan:
        if destination.is_symlink() or (destination.exists() and digest(source) != digest(destination)):
            raise ValueError('目标文件已存在且不同，拒绝覆盖: ' + str(destination))
    return plan


def _remove_migrated_sources(plan, target, source_dirs):
    target = target.resolve()
    staged = [(str(source), str(destination)) for source, destination in plan
              if source.parent.resolve() != target]
    sources = [str(directory) for directory in source_dirs if directory.resolve() != target]
    return finish_staged(staged, sources)


def correct_serial(base, old_serial, new_serial, apply=False):
    """Stage verified archives, correct identity, then remove the obsolete directory."""
    base = Path(base).resolve()
    new_component = serial_component(new_serial)
    if not usable_serial(new_serial) or old_serial == new_serial:
        raise ValueError('需要经过核实的新唯一序列号')
    database = base / 'registry.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as reader:
        old = reader.execute('SELECT label,info FROM boards WHERE serial=?', (old_serial,)).fetchone()
        if not old or reader.execute('SELECT 1 FROM boards WHERE serial=?', (new_serial,)).fetchone():
            raise ValueError('旧档案不存在，或目标序列号已存在')
        if reader.execute('SELECT 1 FROM identity_conflicts WHERE serial=?', (new_serial,)).fetchone():
            raise ValueError('目标序列号已确认不唯一')
    target = base / 'logs' / new_component
    if target.is_symlink():
        raise ValueError('档案目录不可为符号链接')
    old_info = json.loads(old[1])
    sources = _archive_sources(base, old_serial, old_info)
    plan = _plan_archive_move(sources, target)
    removed = 0
    if apply:
        for a, b in plan:
            _copy_log(a, b)
        store = Store(str(database))
        try:
            store.correct_serial(old_serial, new_serial)
        finally:
            store.close()
        removed = _remove_migrated_sources(plan, target, sources)
    return {'applied': apply, 'old_serial': old_serial, 'serial': new_serial,
            'label': old[0], 'files': len(plan), 'source_dirs_removed': removed}


def confirm_serial(base, serial, apply=False):
    """Promote a verified provisional identity and migrate its archive in place."""
    base = Path(base).resolve()
    database = base / 'registry.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as reader:
        row = reader.execute('SELECT label,info FROM boards WHERE serial=?', (serial,)).fetchone()
        conflict = reader.execute('SELECT 1 FROM identity_conflicts WHERE serial=?', (serial,)).fetchone()
    if not row or conflict:
        raise ValueError('档案不存在，或该序列号有冲突')
    info = json.loads(row[1])
    sources = _archive_sources(base, serial, info)
    target = base / 'logs' / serial_component(serial)
    if target.is_symlink():
        raise ValueError('档案目录不可为符号链接')
    plan = _plan_archive_move(sources, target)
    removed = 0
    if apply:
        for source, destination in plan:
            _copy_log(source, destination)
        store = Store(str(database))
        try:
            store.confirm_serial(serial)
        finally:
            store.close()
        removed = _remove_migrated_sources(plan, target, sources)
    return {'applied': apply, 'serial': serial, 'label': row[0], 'files': len(plan),
            'source_dirs_removed': removed}


def import_legacy(source, base, mapping, apply=False):
    source = Path(source).resolve()
    plan = []
    claimed = {}
    for entry in mapping:
        serial = entry['serial']
        component = serial_component(serial)
        validate_label(entry['label'])
        for directory in entry.get('log_dirs', []):
            root = (source / 'logs' / directory).resolve()
            if not root.is_relative_to(source / 'logs') or not root.is_dir():
                raise ValueError('日志目录不在源 logs 内: ' + directory)
            if root in claimed and claimed[root] != serial:
                raise ValueError('同一旧目录不能分配给两个设备')
            claimed[root] = serial
            for path in sorted(root.glob('*.log')):
                if path.is_symlink() or not path.is_file():
                    continue
                prefix = 'ssh' if path.name.startswith('ssh') else 'serial'
                name = prefix + '-legacy-' + hashlib.sha256(str(root).encode()).hexdigest()[:16] + '-' + path.name
                target = Path(base) / 'logs' / component / name
                plan.append((path, target))
    if apply:
        store = Store(str(Path(base) / 'registry.sqlite3'))
        try:
            for entry in mapping:
                store.bind({'serial': entry['serial'], 'hostname': entry.get('hostname', ''), 'ips': []},
                           entry.get('by_path', ''), entry.get('dev', ''), entry['label'])
            for source_file, target in plan:
                _copy_log(source_file, target)
        finally:
            store.close()
    return {'applied': apply, 'devices': len(mapping), 'files': len(plan),
            'copies': [{'source': str(a), 'target': str(b)} for a, b in plan]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=BASE)
    commands = parser.add_subparsers(dest='action', required=True)
    migrate = commands.add_parser('import-legacy')
    migrate.add_argument('--source', required=True)
    migrate.add_argument('--mapping', required=True, help='JSON array of serial/label/log_dirs mappings')
    migrate.add_argument('--apply', action='store_true', help='copy archives; default is dry-run')
    correct = commands.add_parser('correct-serial', help='offline correction of a verified board identity')
    correct.add_argument('--old', required=True)
    correct.add_argument('--new', required=True)
    correct.add_argument('--apply', action='store_true', help='move logs and update registry; stop service first')
    confirm = commands.add_parser('confirm-serial', help='promote a verified provisional identity')
    confirm.add_argument('--serial', required=True)
    confirm.add_argument('--apply', action='store_true', help='move logs and update registry; stop service first')
    args = parser.parse_args()
    if args.action == 'correct-serial':
        print(json.dumps(correct_serial(args.base, args.old, args.new, args.apply), ensure_ascii=False, indent=2))
        return
    if args.action == 'confirm-serial':
        print(json.dumps(confirm_serial(args.base, args.serial, args.apply), ensure_ascii=False, indent=2))
        return
    with open(args.mapping, encoding='utf-8') as source:
        mapping = json.load(source)
    print(json.dumps(import_legacy(args.source, args.base, mapping, args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
