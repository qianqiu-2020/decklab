"""Adopt the previous Debian package's configuration, identity and data path."""
import grp
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile


OLD_CONFIG = Path('/etc/seriald/seriald.env')
NEW_CONFIG = Path('/etc/decklab/decklab.env')
OLD_DATA = Path('/data/seriald-data')
NEW_DATA = Path('/data/decklab-data')
DEFAULT_PATHS = {
    'DECKLAB_BASE': (str(OLD_DATA), str(NEW_DATA)),
    'DECKLAB_STATIC': ('/usr/lib/seriald', '/usr/lib/decklab'),
    'DECKLAB_SOCKET': ('/run/seriald/collector.sock', '/run/decklab/collector.sock'),
    'DECKLAB_RESCUE_BIN': ('/usr/libexec/seriald/rescue.sh', '/usr/libexec/decklab/rescue.sh'),
}


def migrate_data(legacy, target):
    if legacy.is_symlink():
        if not legacy.exists() and legacy.resolve() == target:
            raise ValueError(f'Legacy data path points to a missing directory: {target}')
        return legacy.resolve() == target
    if not legacy.exists():
        return target.is_dir()
    if not legacy.is_dir():
        raise ValueError(f'Legacy data path is not a directory: {legacy}')
    if target.exists() or target.is_symlink():
        raise ValueError(f'Both data roots exist. Choose the active registry before upgrading: {legacy}, {target}')
    if legacy.is_mount():
        return False
    legacy.rename(target)
    try:
        legacy.symlink_to(target, target_is_directory=True)
    except OSError:
        target.rename(legacy)
        raise
    return True


def converted_config(text, adopt_default):
    assignments = set()
    result = []
    for line in text.splitlines():
        match = re.match(r'^(\s*)(?:SERIALD_|DECKLAB_)([A-Z0-9_]+)=(.*)$', line)
        if not match:
            result.append(line)
            continue
        indent, suffix, value = match.groups()
        name = 'DECKLAB_' + suffix
        assignments.add(name)
        old, new = DEFAULT_PATHS.get(name, (None, None))
        if value.strip().strip('"\'') == old and (name != 'DECKLAB_BASE' or adopt_default):
            value = new
        result.append(f'{indent}{name}={value}')
    if 'DECKLAB_BASE' not in assignments:
        result.append('DECKLAB_BASE=' + str(NEW_DATA if adopt_default else OLD_DATA))
    return '\n'.join(result) + '\n'


def configured_base(text, fallback):
    base = fallback
    for name, value in re.findall(r'(?m)^[\t ]*(\w+)=(.*)$', text):
        if name in ('SERIALD_BASE', 'DECKLAB_BASE'):
            base = value.strip().strip('"\'')
    return Path(base)


def migrate_saved_paths(base, adopt_default):
    path = base / 'web-settings.json'
    if not path.is_file() or path.is_symlink():
        return
    with path.open('rb') as source:
        metadata = os.fstat(source.fileno())
        if metadata.st_size > 2 * 1024 ** 2:
            return
        settings = json.load(source)
    values = settings.get('environment', {})
    changes = {}
    for name, (old, new) in DEFAULT_PATHS.items():
        key = name.removeprefix('DECKLAB_')
        if values.get(key) == old and (key != 'BASE' or adopt_default):
            changes[key] = new
    if not changes:
        return
    values.update(changes)
    with tempfile.NamedTemporaryFile(dir=base, mode='w', encoding='utf-8', delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(settings, output, ensure_ascii=False, indent=2)
            output.write('\n')
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(output.fileno(), metadata.st_mode & 0o777)
            output.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def migrate_account():
    try:
        previous = pwd.getpwnam('seriald')
    except KeyError:
        return
    try:
        current = pwd.getpwnam('decklab')
    except KeyError:
        current = None
    # Preserve ownership and permissions for mounts, credentials and snapshots.
    if current is not None and current.pw_uid != previous.pw_uid:
        raise ValueError('The legacy and new service accounts are both present with different IDs')
    if current is None:
        group = grp.getgrgid(previous.pw_gid)
        if group.gr_name == 'seriald':
            subprocess.run(['groupmod', '--new-name', 'decklab', 'seriald'], check=True)
        subprocess.run(['usermod', '--login', 'decklab', 'seriald'], check=True)


def main():
    marker = NEW_CONFIG.parent / '.migration-complete'
    if marker.exists():
        base = configured_base(NEW_CONFIG.read_text(encoding='utf-8'), NEW_DATA)
        migrate_saved_paths(base, OLD_DATA.is_symlink() and OLD_DATA.resolve() == NEW_DATA)
        return
    if not OLD_CONFIG.is_file():
        return
    source = OLD_CONFIG.read_text(encoding='utf-8')
    base = configured_base(source, OLD_DATA)
    adopt_default = base == OLD_DATA and migrate_data(OLD_DATA, NEW_DATA)
    migrate_account()
    migrate_saved_paths(NEW_DATA if adopt_default else base, adopt_default)
    # An interrupted upgrade may leave either the generated config or the
    # previous package's file. The migration is safe to run again in both cases.
    with tempfile.NamedTemporaryFile(dir=NEW_CONFIG.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(converted_config(source, adopt_default).encode('utf-8'))
            output.close()
            temporary.replace(NEW_CONFIG)
        finally:
            temporary.unlink(missing_ok=True)
    marker.touch(mode=0o600)


if __name__ == '__main__':
    main()
