"""Verify downloaded draft release packages without running their executables."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.bundle_manifest import verify as verify_bundle
from tools.collect_windows_notices import verify as verify_notices
from tools.collect_linux_mcp_notices import verify as verify_linux_notices
from tools.install_components import executor_metadata, tree_inventory
from tools.package_flash_loaders import publish as inspect_loaders


def inspect_complete_install(root, platform):
    root = Path(root)
    windows = platform == 'windows-amd64'
    code = root if windows else root / 'usr/lib/seriald'
    info = json.loads((code / 'installation.json').read_text())
    if info.get('profile') != 'complete':
        raise ValueError('release requires a complete standalone installation')
    executor = root / ('_internal/decklab-flash.exe' if windows else 'usr/libexec/seriald/decklab-flash')
    actual = executor_metadata(executor, platform)
    pinned = info if windows else json.loads((root / 'usr/share/doc/seriald/flash-executor.json').read_text())
    if any(actual[key] != pinned.get(key) for key in ('sha256', 'size')):
        raise ValueError('installed flash executor checksum mismatch')
    loaders = root / ('_internal/flash-loaders' if windows else 'usr/lib/seriald/flash-loaders')
    with tempfile.TemporaryDirectory() as temporary:
        inspect_loaders(loaders, temporary)
    notices = root / ('flash-third-party' if windows else 'usr/share/doc/seriald/flash-third-party')
    tree_inventory(notices)
    if windows:
        driver = root / 'usb-driver'
        if tree_inventory(driver) != info.get('driver_files'):
            raise ValueError('installed USB driver inventory mismatch')
        for name in ('spacemit_usb.inf', 'WinUSB/spacemit_amd64.cat', 'driverinstall.exe'):
            if not (driver / name).is_file(): raise ValueError('installed USB driver is incomplete')
    return info


def checksums(directory, manifest, expected, source_prefix=''):
    rows = {}
    for line in (directory / manifest).read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *]([^\\:]+)', line)
        name = match[2].removeprefix(source_prefix) if match else ''
        if not match or '/' in name or name in rows or name in ('.', '..'):
            raise ValueError('invalid or duplicate checksum entry in ' + manifest)
        rows[name] = match[1].lower()
    if set(rows) != set(expected):
        raise ValueError('release checksum file set mismatch: ' + manifest)
    for name, digest in rows.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError('release checksum mismatch: ' + name)


def unpack_portable(archive_path, destination):
    """Normalize Windows ZIP separators; reject ambiguous paths before writing."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 4096 or sum(item.file_size for item in members) > 512 * 1024 * 1024:
            raise ValueError('portable archive exceeds inspection bounds')
        entries, seen = [], set()
        for member in members:
            name = member.filename.replace('\\', '/')
            relative = PurePosixPath(name)
            key = name.rstrip('/').casefold()
            if (relative.is_absolute() or ':' in name or '..' in relative.parts
                    or not relative.parts or relative.parts[0] != 'seriald'
                    or key in seen or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError('unsafe or duplicate portable archive path: ' + name)
            if len(relative.parts) > 1 and relative.parts[1].casefold() == 'data':
                raise ValueError('portable archive contains runtime data')
            seen.add(key)
            entries.append((member, relative, name.endswith('/')))
        for member, relative, directory in entries:
            target = destination / relative
            if directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    return destination / 'seriald'


def inspect_linux_runtime(runtime, architecture):
    """Independently check the complete embedded runtime without executing it."""
    manifest_path = runtime / 'runtime-manifest.json'
    if runtime.is_symlink() or manifest_path.is_symlink():
        raise ValueError('Debian MCP runtime and manifest must not be symbolic links')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('schema') != 1 or manifest.get('architecture') != architecture
            or manifest.get('mcp_version') != '2.3.0'
            or manifest.get('executable') != 'seriald-mcp-worker'
            or manifest.get('notices') != 'third-party/inventory.json'):
        raise ValueError('Debian MCP runtime metadata mismatch')
    platform = manifest.get('build_platform', {})
    if ((platform.get('os'), platform.get('version'), platform.get('glibc')) != ('ubuntu', '22.04', '2.35')
            or platform.get('machine') != {'amd64': 'x86_64', 'arm64': 'aarch64'}[architecture]):
        raise ValueError('Debian MCP runtime requires the Ubuntu 22.04 build baseline')
    if manifest.get('requirements_sha256') != hashlib.sha256(
            (ROOT / 'packaging/linux-mcp-requirements.lock').read_bytes()).hexdigest():
        raise ValueError('Debian MCP runtime requirements differ from the reviewed hash lock')
    sources = ('seriald_mcp.py', 'serialctl.py', 'tools/linux_mcp_entrypoint.py')
    if manifest.get('source_files') != {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                      for name in sources}:
        raise ValueError('Debian MCP runtime adapter differs from the checked-out release')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Debian MCP runtime file manifest missing')
    actual = set()
    for path in runtime.rglob('*'):
        if path.is_symlink():
            raise ValueError('Debian MCP runtime contains a symbolic link')
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(runtime).as_posix())
    if actual != set(files):
        raise ValueError('Debian MCP runtime file set mismatch')
    for name, row in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or not relative.parts or relative.as_posix() != name):
            raise ValueError('unsafe Debian MCP runtime manifest path')
        payload = (runtime / name).read_bytes()
        if (not isinstance(row, dict) or row.get('size') != len(payload)
                or row.get('sha256') != hashlib.sha256(payload).hexdigest()):
            raise ValueError('Debian MCP runtime checksum mismatch: ' + name)
    executable = runtime / 'seriald-mcp-worker'
    header = executable.read_bytes()[:20]
    if (len(header) != 20 or header[:5] != b'\x7fELF\x02' or header[5] != 1
            or int.from_bytes(header[18:20], 'little') != {'amd64': 62, 'arm64': 183}[architecture]
            or not executable.stat().st_mode & 0o111):
        raise ValueError('Debian MCP runtime executable architecture mismatch')
    for name in files:
        header = (runtime / name).read_bytes()[:20]
        if header[:4] == b'\x7fELF' and (len(header) != 20 or header[4:6] != b'\x02\x01'
                or int.from_bytes(header[18:20], 'little') != {'amd64': 62, 'arm64': 183}[architecture]):
            raise ValueError('Debian MCP native library architecture mismatch: ' + name)
    inventory = verify_linux_notices(runtime / 'third-party', bundle=runtime)
    distributions = inventory.get('distributions', [])
    sdk = [row for row in distributions if row.get('name', '').lower() == 'mcp']
    if len(sdk) != 1 or sdk[0].get('version') != '2.3.0':
        raise ValueError('Debian MCP SDK notice/version mismatch')
    if not inventory.get('native_providers'):
        raise ValueError('Debian MCP native dependency notices missing')
    return manifest


def inspect_release(directory, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('invalid package version')
    directory = Path(directory)
    release_version = tuple(map(int, version.split('.')))
    architectures = ('amd64', 'arm64') if release_version >= (1, 7, 1) else ('all',)
    debs = {architecture: 'seriald_%s_%s.deb' % (version, architecture)
            for architecture in architectures}
    setup = 'seriald-%s-windows-x64-setup.exe' % version
    portable = 'seriald-windows-portable.zip'
    aliases = {}
    if release_version >= (1, 7, 0):
        aliases['seriald-windows-x64-setup.exe'] = setup
        aliases['seriald_latest_all.deb'] = debs.get('all', debs.get('amd64'))
    if release_version >= (1, 7, 1):
        aliases.update({'seriald_latest_%s.deb' % arch: name for arch, name in debs.items()})
    checksums(directory, 'SHA256SUMS.txt', (*debs.values(), setup, portable, *aliases))
    for alias, canonical in aliases.items():
        if hashlib.sha256((directory / alias).read_bytes()).digest() != hashlib.sha256(
                (directory / canonical).read_bytes()).digest():
            raise ValueError('stable download alias differs from versioned package: ' + alias)
    # Older workflow output names the build directory; artifacts retain basenames.
    checksums(directory, 'linux-SHA256SUMS.txt', debs.values(), source_prefix='dist/')
    digest = (directory / (setup + '.sha256')).read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('invalid installer checksum')
    if hashlib.sha256((directory / setup).read_bytes()).hexdigest() != digest.lower():
        raise ValueError('installer checksum mismatch')
    for architecture, deb in debs.items():
        fields = subprocess.run(['dpkg-deb', '--field', str(directory / deb), 'Package', 'Version', 'Architecture'],
                                check=True, capture_output=True, text=True).stdout
        expected = {'Package': 'seriald', 'Version': version, 'Architecture': architecture}
        actual = dict(line.split(': ', 1) for line in fields.splitlines())
        if actual != expected:
            raise ValueError('Debian package metadata mismatch: ' + deb)
        if release_version >= (1, 7, 1):
            with tempfile.TemporaryDirectory(prefix='decklab-debian-inspection-') as temporary:
                subprocess.run(['dpkg-deb', '--extract', str(directory / deb), temporary], check=True)
                code = Path(temporary) / 'usr/lib/seriald'
                if release_version >= (1, 10, 0):
                    inspect_complete_install(Path(temporary), 'linux-' + architecture)
                runtime = inspect_linux_runtime(code / 'mcp-runtime', architecture)
                for name in ('seriald_mcp.py', 'serialctl.py'):
                    if hashlib.sha256((code / name).read_bytes()).hexdigest() != runtime['source_files'][name]:
                        raise ValueError('Debian adapter and embedded MCP runtime differ: ' + name)
    if (directory / setup).read_bytes()[:2] != b'MZ':
        raise ValueError('installer is not a Windows executable')
    with tempfile.TemporaryDirectory(prefix='decklab-release-inspection-') as temporary:
        bundle = unpack_portable(directory / portable, Path(temporary))
        manifest = verify_bundle(bundle)
        if release_version >= (1, 10, 0):
            inspect_complete_install(bundle, 'windows-amd64')
        notices = verify_notices(bundle / 'third-party')
        if (bundle / 'package-version.txt').read_text(encoding='ascii').strip() != version:
            raise ValueError('portable package version mismatch')
        if (bundle / 'seriald.exe').read_bytes()[:2] != b'MZ':
            raise ValueError('portable host is not a Windows executable')
        if len(notices.get('native_crypto_providers', [])) != 2:
            raise ValueError('native crypto provider notices missing')
        if len(notices.get('runtime_source_notices', [])) != 3:
            raise ValueError('runtime source notices missing')
        if tuple(map(int, version.split('.'))) >= (1, 5, 1):
            if not notices.get('native_source_notices'):
                raise ValueError('reviewed native source catalog missing')
            sources = notices.get('corresponding_sources', [])
            paramiko = [row for row in notices['distributions'] if row['name'].lower() == 'paramiko']
            if len(sources) != 1 or len(paramiko) != 1 or sources[0]['installed_version'] != paramiko[0]['version']:
                raise ValueError('Paramiko corresponding source/version mismatch')
        return {'version': version, 'debian_architectures': list(architectures), 'bundle_files': len(manifest['files']),
                'distribution_notice_sets': len(notices['distributions'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    try:
        result = inspect_release(args.directory, args.version)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, 'Release verification failed: %s\n' % error)
    print('Release verified: %s; %s bundle files; %s distribution notice sets' %
          (result['version'], result['bundle_files'], result['distribution_notice_sets']))


if __name__ == '__main__':
    main()
