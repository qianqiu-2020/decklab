"""Verify downloaded draft release packages without running their executables."""
import argparse
import hashlib
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


def inspect_release(directory, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('invalid package version')
    directory = Path(directory)
    deb = 'seriald_%s_all.deb' % version
    setup = 'seriald-%s-windows-x64-setup.exe' % version
    portable = 'seriald-windows-portable.zip'
    checksums(directory, 'SHA256SUMS.txt', (deb, setup, portable))
    # Older workflow output names the build directory; artifacts retain basenames.
    checksums(directory, 'linux-SHA256SUMS.txt', (deb,), source_prefix='dist/')
    digest = (directory / (setup + '.sha256')).read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('invalid installer checksum')
    if hashlib.sha256((directory / setup).read_bytes()).hexdigest() != digest.lower():
        raise ValueError('installer checksum mismatch')
    fields = subprocess.run(['dpkg-deb', '--field', str(directory / deb), 'Package', 'Version', 'Architecture'],
                            check=True, capture_output=True, text=True).stdout
    expected = {'Package': 'seriald', 'Version': version, 'Architecture': 'all'}
    actual = dict(line.split(': ', 1) for line in fields.splitlines())
    if actual != expected:
        raise ValueError('Debian package metadata mismatch')
    if (directory / setup).read_bytes()[:2] != b'MZ':
        raise ValueError('installer is not a Windows executable')
    with tempfile.TemporaryDirectory(prefix='decklab-release-inspection-') as temporary:
        bundle = unpack_portable(directory / portable, Path(temporary))
        manifest = verify_bundle(bundle)
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
        return {'version': version, 'bundle_files': len(manifest['files']),
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
