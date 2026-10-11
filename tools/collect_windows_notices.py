"""Offline build-environment inventory and license collection for Windows bundles.

The environment inventory is a conservative superset, not a claim that every
installed distribution is frozen. Native library attribution still needs review.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = {('pyserial', '3.5'): (
    ROOT / 'packaging/third-party/pyserial-3.5/LICENSE.txt',
    'https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt')}


def normalized(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('invalid distribution name')
    return re.sub(r'[-_.]+', '-', name).lower()


def license_file(value):
    path = PurePosixPath(str(value).replace('\\', '/'))
    if path.suffix.lower() in ('.py', '.pyc', '.pyd', '.dll', '.exe', '.so'):
        return False
    return (any(part.lower() in ('licenses', 'licences') for part in path.parts)
            or bool(re.match(r'^(license|licence|copying|notice)([._-]|$)', path.name, re.I)))


def inventory(distributions, destination, python_license, fallbacks=None):
    """Build into a private directory; failed collection cannot publish a manifest."""
    fallbacks = FALLBACKS if fallbacks is None else fallbacks
    destination = Path(destination)
    if destination.exists():
        raise ValueError('notice destination already exists; use a fresh bundle')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.notices-', dir=destination.parent))
    rows, seen = [], set()
    try:
        for distribution in sorted(distributions, key=lambda item: normalized(item.metadata['Name'])):
            name = distribution.metadata['Name']
            key = normalized(name)
            version = distribution.version
            if key in seen:
                raise ValueError('duplicate installed distribution: ' + key)
            seen.add(key)
            sources = [(str(file), Path(distribution.locate_file(file)), 'installed-distribution')
                       for file in distribution.files or () if license_file(file)]
            if not sources and (key, version) in fallbacks:
                path, url = fallbacks[key, version]
                sources = [(Path(path).name, Path(path), url)]
            if not sources:
                raise ValueError('license files missing for %s %s' % (name, version))
            entries = []
            for index, (original, path, source) in enumerate(sorted(sources)):
                data = path.read_bytes()
                if not data.strip():
                    raise ValueError('empty license file for ' + name)
                target = temporary / key / ('%03d-%s' % (index, re.sub(r'[^A-Za-z0-9._-]', '_', path.name)))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                entries.append({'file': target.relative_to(temporary).as_posix(),
                                'sha256': hashlib.sha256(data).hexdigest(),
                                'original': original, 'source': source})
            rows.append({'name': name, 'version': version,
                         'license_expression': distribution.metadata.get('License-Expression'),
                         'license_metadata': distribution.metadata.get('License'),
                         'requires_dist': list(distribution.requires or ()), 'licenses': entries})
        data = Path(python_license).read_bytes()
        if not data.strip():
            raise ValueError('Python license file is empty')
        (temporary / 'Python-LICENSE.txt').write_bytes(data)
        manifest = {'schema': 1, 'scope': 'installed-build-environment-superset',
                    'python': {'version': sys.version.split()[0],
                               'license': 'Python-LICENSE.txt',
                               'sha256': hashlib.sha256(data).hexdigest()},
                    'distributions': rows}
        (temporary / 'inventory.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (temporary / 'README.txt').write_text(
            'These notices cover all installed Python distributions in the build environment.\n'
            'Some components are build tools and are not shipped as executable modules.\n'
            'inventory.json records exact versions and hashes of copied notices.\n'
            'This is not a complete native-library SBOM; inspect frozen binaries separately.\n', encoding='utf-8')
        temporary.rename(destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(destination):
    destination = Path(destination)
    manifest = json.loads((destination / 'inventory.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('scope') != 'installed-build-environment-superset':
        raise ValueError('unsupported notice inventory')
    entries = [{'file': manifest['python']['license'], 'sha256': manifest['python']['sha256']}]
    names = set()
    for row in manifest['distributions']:
        name = normalized(row['name'])
        if name in names or not row['licenses']:
            raise ValueError('duplicate distribution or missing notices')
        names.add(name)
        entries.extend(row['licenses'])
    files = set()
    for entry in entries:
        relative = entry['file']
        path = PurePosixPath(relative)
        if (not isinstance(relative, str) or path.is_absolute() or '\\' in relative
                or ':' in relative or '..' in path.parts or relative in files):
            raise ValueError('invalid notice path')
        files.add(relative)
        target = destination / relative
        if not target.resolve().is_relative_to(destination.resolve()) or target.is_symlink():
            raise ValueError('notice escapes bundle')
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('notice checksum mismatch: ' + relative)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--python-license', type=Path, default=Path(sys.base_prefix) / 'LICENSE.txt')
    args = parser.parse_args()
    try:
        manifest = (verify(args.destination) if args.verify else
                    inventory(metadata.distributions(), args.destination, args.python_license))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, 'Notice collection failed: %s\n' % error)
    print('Collected notices for %d installed distributions' % len(manifest['distributions']))


if __name__ == '__main__':
    main()
