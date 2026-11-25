"""Adopt the previous Debian package's configuration, identity and data path."""
import grp
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
    if marker.exists() or not OLD_CONFIG.is_file():
        return
    source = OLD_CONFIG.read_text(encoding='utf-8')
    configured_base = str(OLD_DATA)
    for name, value in re.findall(r'(?m)^[\t ]*(\w+)=(.*)$', source):
        if name in ('SERIALD_BASE', 'DECKLAB_BASE'):
            configured_base = value
    adopt_default = configured_base.strip().strip('"\'') == str(OLD_DATA) and migrate_data(OLD_DATA, NEW_DATA)
    migrate_account()
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
