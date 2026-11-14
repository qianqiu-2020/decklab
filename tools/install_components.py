"""Offline, hash-pinned inputs for a complete standalone installation.

These inputs belong to the package builder, never the browser or target board.
No validation step runs an executor, installs a driver or downloads anything.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_flash_loaders import publish as publish_loaders
MACHINES = {'linux-amd64': 62, 'linux-arm64': 183, 'windows-amd64': 0x8664}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def regular(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('installation input must be a regular non-symlink file')
    return path


def executor_metadata(path, platform):
    if platform not in MACHINES:
        raise ValueError('unsupported installation platform')
    path = regular(path)
    if not 20 <= path.stat().st_size <= 64 * 1024 ** 2:
        raise ValueError('executor size is outside installation bounds')
    with path.open('rb') as source:
        header = source.read(64)
        if platform.startswith('linux-'):
            valid = len(header) >= 20 and header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20], 'little') == MACHINES[platform]
        else:
            valid = len(header) == 64 and header[:2] == b'MZ'
            if valid:
                offset = struct.unpack_from('<I', header, 60)[0]
                if not 64 <= offset <= 16 * 1024 * 1024:
                    valid = False
                else:
                    source.seek(offset)
                    pe = source.read(6)
                    valid = pe[:4] == b'PE\0\0' and int.from_bytes(pe[4:6], 'little') == MACHINES[platform]
    if not valid:
        raise ValueError('flashing executor architecture does not match ' + platform)
    return {'sha256': digest(path), 'size': path.stat().st_size, 'platform': platform}


def tree_inventory(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('installation component requires a regular directory')
    entries = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('installation components cannot contain symbolic links')
        if path.is_file():
            regular(path)
            entries[path.relative_to(directory).as_posix()] = {'sha256': digest(path), 'size': path.stat().st_size}
        elif not path.is_dir():
            raise ValueError('installation components cannot contain special files')
    if not entries or len(entries) > 1024 or sum(v['size'] for v in entries.values()) > 256 * 1024 ** 2:
        raise ValueError('installation component directory is empty or exceeds its bounds')
    return entries


def load(path, platform):
    path = regular(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError('installation component manifest exceeds size limit')
    value = json.loads(raw)
    expected = {'version', 'platform', 'executor', 'loaders', 'notices'} | ({'driver'} if platform.startswith('windows-') else set())
    if not isinstance(value, dict) or set(value) != expected or type(value.get('version')) is not int or value.get('version') != 1 or value.get('platform') != platform:
        raise ValueError('invalid complete-install component manifest')
    def member(name, directory=False):
        entry = value[name]
        if not isinstance(entry, dict) or set(entry) != ({'path', 'files'} if directory else {'path', 'sha256'}):
            raise ValueError('invalid installation component entry')
        rel = entry['path']
        if not isinstance(rel, str) or not rel or '\\' in rel or ':' in rel or '..' in PurePosixPath(rel).parts or PurePosixPath(rel).is_absolute():
            raise ValueError('installation component path must be relative')
        source = path.parent / rel
        if path.parent not in source.resolve().parents:
            raise ValueError('installation component is outside its input bundle')
        if directory:
            if tree_inventory(source) != entry['files']:
                raise ValueError('installation component file inventory changed: ' + name)
        elif digest(regular(source)) != entry['sha256']:
            raise ValueError('installation component checksum changed: ' + name)
        return source
    result = {'executor': member('executor'), 'loaders': member('loaders', True), 'notices': member('notices', True)}
    executor_metadata(result['executor'], platform)
    if platform.startswith('windows-'):
        result['driver'] = member('driver', True)
        if not all((result['driver'] / name).is_file() for name in ('spacemit_usb.inf', 'WinUSB/spacemit_amd64.cat', 'driverinstall.exe')):
            raise ValueError('complete Windows installation requires the signed USB driver package')
    return result


def default_manifest(platform):
    return ROOT / 'build/install-components' / platform / 'manifest.json'


def publish_windows(bundle, components):
    """Enrich an already frozen host before final inventory/installer creation."""
    bundle = Path(bundle)
    if (bundle / 'data').exists():
        raise ValueError('refusing to package a running installation')
    info = executor_metadata(components['executor'], 'windows-amd64')
    for name in ('notices', 'driver'):
        tree_inventory(components[name])
    if not all((components['driver'] / name).is_file() for name in ('spacemit_usb.inf', 'WinUSB/spacemit_amd64.cat', 'driverinstall.exe')):
        raise ValueError('complete Windows installation requires the signed USB driver package')
    shutil.copyfile(components['executor'], bundle / '_internal/decklab-flash.exe')
    publish_loaders(components['loaders'], bundle / '_internal/flash-loaders')
    shutil.copytree(components['notices'], bundle / 'flash-third-party')
    shutil.copytree(components['driver'], bundle / 'usb-driver')
    for name in ('Install-seriald-USB-Driver.cmd', 'Install-seriald-USB-Driver.ps1'):
        shutil.copyfile(ROOT / 'tools/windows-launchers' / name, bundle / name)
    info.update(profile='complete', flash_executor='_internal/decklab-flash.exe',
                ram_loaders='_internal/flash-loaders/manifest.json', usb_driver='usb-driver/spacemit_usb.inf',
                driver_files=tree_inventory(components['driver']),
                loader_families=list(json.loads((components['loaders'] / 'manifest.json').read_text())['families']))
    (bundle / 'installation.json').write_text(json.dumps(info, indent=2) + '\n')
    return info


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--platform', choices=tuple(MACHINES), required=True)
    parser.add_argument('--windows-bundle', type=Path)
    for name in ('executor', 'loaders', 'notices', 'driver'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    manual = {name: getattr(args, name) for name in ('executor', 'loaders', 'notices', 'driver')}
    if any(manual.values()):
        if args.manifest or not all(manual.values()) or args.platform != 'windows-amd64':
            parser.error('explicit Windows inputs require executor, loaders, notices and driver together')
        components = manual
    else:
        components = load(args.manifest or default_manifest(args.platform), args.platform)
    if args.windows_bundle:
        if args.platform != 'windows-amd64':
            parser.error('--windows-bundle requires windows-amd64')
        print(json.dumps(publish_windows(args.windows_bundle, components)))
    else:
        print(json.dumps({name: str(path) for name, path in components.items()}))
