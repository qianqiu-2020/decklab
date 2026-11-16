"""Validate and copy a reviewed native desktop SDK without loading its libraries."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_linux_mcp import relative_path
from tools.install_components import digest, executor_metadata, regular, tree_inventory

MANIFEST = 'runtime-manifest.json'
WRAPPERS = {'linux-amd64': 'libdecklab-freerdp.so',
            'windows-amd64': 'decklab-freerdp.dll'}
RESOURCE_PATH = '_internal/desktop-runtime/freerdp'
SOURCES = ('native/freerdp/CMakeLists.txt', 'native/freerdp/decklab_rdp.c',
           'native/freerdp/decklab_rdp.h')
FIELDS = {'schema', 'platform', 'abi', 'wrapper', 'source_files', 'sources', 'notices', 'files'}


def read_json(path):
    raw = regular(path).read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError('desktop runtime JSON exceeds size limit')
    return json.loads(raw)


def reviewed_sources():
    reviewed = read_json(ROOT / 'packaging/third-party/freerdp-sources.json')
    if (not isinstance(reviewed, dict) or set(reviewed) != {'schema', 'sources'}
            or type(reviewed['schema']) is not int or reviewed['schema'] != 1
            or not isinstance(reviewed['sources'], dict)
            or set(reviewed['sources']) != {'freerdp', 'openssl', 'openh264'}):
        raise ValueError('invalid reviewed desktop sources')
    for component in reviewed['sources'].values():
        if (not isinstance(component, dict) or set(component) != {'version', 'url', 'sha256'}
                or not isinstance(component['version'], str) or not component['version']
                or not isinstance(component['url'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', str(component['sha256']))):
            raise ValueError('invalid reviewed desktop source')
        url = urlsplit(component['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('desktop sources require a public HTTPS URL')
    return reviewed['sources']


def validate(source, platform):
    """Check exact ABI sources, upstream inputs, native architecture and every byte."""
    if platform not in WRAPPERS:
        raise ValueError('unsupported desktop runtime platform')
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('desktop runtime must be a regular directory')
    manifest = read_json(source / MANIFEST)
    if (not isinstance(manifest, dict) or set(manifest) != FIELDS
            or type(manifest['schema']) is not int or manifest['schema'] != 1
            or type(manifest['abi']) is not int or manifest['abi'] != 1
            or manifest['platform'] != platform or manifest['wrapper'] != WRAPPERS[platform]):
        raise ValueError('desktop runtime schema, platform, ABI or wrapper mismatch')
    sources = {name: digest(regular(ROOT / name)) for name in SOURCES}
    if manifest['source_files'] != sources:
        raise ValueError('desktop wrapper source differs from this checkout; rebuild the SDK')
    if manifest['sources'] != reviewed_sources():
        raise ValueError('desktop upstream sources differ from the reviewed inputs')
    files = manifest['files']
    if not isinstance(files, dict) or not files or MANIFEST in files:
        raise ValueError('desktop runtime file inventory is missing or includes itself')
    for name, row in files.items():
        relative_path(name)
        if (not isinstance(row, dict) or set(row) != {'sha256', 'size'}
                or type(row['size']) is not int or row['size'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(row['sha256']))):
            raise ValueError('invalid desktop runtime file inventory')
    actual = tree_inventory(source)
    del actual[MANIFEST]
    if files != actual:
        raise ValueError('desktop runtime files are missing, unexpected or have changed checksums')
    if manifest['wrapper'] not in files:
        raise ValueError('desktop runtime wrapper is missing')
    for name in files:
        path = source / name
        with path.open('rb') as stream:
            magic = stream.read(4)
        library = name.endswith('.so') or '.so.' in name or name.lower().endswith('.dll')
        if name == manifest['wrapper'] or library or magic == b'\x7fELF' or magic[:2] == b'MZ':
            executor_metadata(path, platform)
    notices = manifest['notices']
    if not isinstance(notices, dict) or set(notices) != set(manifest['sources']):
        raise ValueError('desktop notices must identify every reviewed source')
    for names in notices.values():
        if (not isinstance(names, list) or not names
                or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names)):
            raise ValueError('desktop source notices require unique file paths')
        for name in names:
            relative_path(name)
            if not name.startswith('third-party/') or name not in files or files[name]['size'] == 0:
                raise ValueError('desktop source notice is missing from third-party inventory')
    return manifest


def publish(source, destination, platform):
    """Copy an exact validated SDK into a new package directory; never run it."""
    source, destination = Path(source), Path(destination)
    manifest = validate(source, platform)
    if destination.exists() or destination.is_symlink():
        raise ValueError('desktop runtime destination already exists')
    if source.resolve() in destination.resolve().parents:
        raise ValueError('desktop runtime destination cannot be inside its source')
    destination.mkdir(parents=True, mode=0o755)
    destination.chmod(0o755)
    for name in [*manifest['files'], MANIFEST]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        for parent in target.parents:
            if parent == destination:
                break
            parent.chmod(0o755)
        shutil.copyfile(source / name, target)
        target.chmod(0o644)
    if validate(destination, platform) != manifest:
        raise ValueError('desktop runtime changed while copying')
    return manifest


def notice_inventory(bundle, platform, destination=None):
    """Attribute the fixed SDK and copy its source notices into the package inventory."""
    source = Path(bundle) / RESOURCE_PATH
    if not source.exists() and not source.is_symlink():
        return None
    if source.is_symlink() or not source.resolve().is_relative_to(Path(bundle).resolve()):
        raise ValueError('desktop runtime escapes bundle')
    manifest = validate(source, platform)
    records, paths = [], {}
    for name, row in manifest['files'].items():
        if not name.startswith('third-party/'):
            continue
        relative = 'desktop-sdk/' + name
        if destination is not None:
            target = Path(destination) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if digest(source / name) != row['sha256']:
                raise ValueError('desktop runtime notice changed during collection')
            shutil.copyfile(source / name, target)
            if digest(target) != row['sha256']:
                raise ValueError('desktop runtime notice changed during collection')
        records.append({'file': relative, 'sha256': row['sha256']})
        paths[name] = relative
    providers = {}
    for name, row in manifest['files'].items():
        with (source / name).open('rb') as stream:
            magic = stream.read(4)
        if (name.lower().endswith('.dll') or name.endswith('.so') or '.so.' in name
                or magic == b'\x7fELF' or magic[:2] == b'MZ'):
            providers[RESOURCE_PATH + '/' + name] = row['sha256']
    if destination is not None and validate(source, platform) != manifest:
        raise ValueError('desktop runtime changed during notice collection')
    return {'path': RESOURCE_PATH, 'manifest_sha256': digest(source / MANIFEST),
            'sources': manifest['sources'], 'providers': providers,
            'notices': {name: [paths[path] for path in files]
                        for name, files in manifest['notices'].items()},
            'notice_files': records}


def prune_frozen_aliases(bundle, platform):
    """Keep the SDK closure in its fixed location, without PyInstaller duplicates."""
    bundle = Path(bundle)
    inventory = notice_inventory(bundle, platform)
    if inventory is None:
        return []
    aliases = set()
    for name, checksum in inventory['providers'].items():
        alias = bundle / '_internal' / Path(name).name
        if alias.exists() or alias.is_symlink():
            if not alias.resolve().is_relative_to(bundle.resolve()):
                raise ValueError('frozen desktop library alias escapes bundle')
            if digest(regular(alias)) != checksum:
                raise ValueError('frozen desktop library alias differs from reviewed SDK: ' + alias.name)
            aliases.add(alias)
    # Check every duplicate before removing any. Other libraries retain their
    # original provenance checks; an unexpected byte is never accepted by name.
    for alias in sorted(aliases):
        alias.unlink()
    return [path.relative_to(bundle).as_posix() for path in sorted(aliases)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--platform', required=True, choices=tuple(WRAPPERS))
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--prune-frozen-aliases', action='store_true',
                        help='source is a frozen bundle; remove only identical top-level SDK duplicates')
    args = parser.parse_args()
    if args.prune_frozen_aliases and args.destination:
        parser.error('--prune-frozen-aliases cannot copy an SDK')
    if args.prune_frozen_aliases:
        result = {'removed_aliases': prune_frozen_aliases(args.source, args.platform)}
    elif args.destination:
        result = publish(args.source, args.destination, args.platform)
    else:
        result = validate(args.source, args.platform)
    print(json.dumps(result, indent=2))
