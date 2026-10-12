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
OPENSSL_LICENSE_HASH = '7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a'
OPENSSL_VERSIONS = ('3.0.16', '4.0.3')
OPENSSL_PROVIDERS = ('_internal/libcrypto-3.dll',
                     '_internal/cryptography/hazmat/bindings/_rust.pyd')
RUNTIME_SOURCE_NOTICES = (
    ('expat-COPYING', '122f2c27000472a201d337b9b31f7eb2b52d091b02857061a8880371612d9534',
     'https://github.com/python/cpython/blob/v3.12.10/Modules/expat/COPYING'),
    ('zlib-LICENSE', '845efc77857d485d91fb3e0b884aaa929368c717ae8186b66fe1ed2495753243',
     'https://github.com/python/cpython-source-deps/blob/zlib-1.3.1/LICENSE'),
    ('xz-COPYING', 'bcb02973ef6e87ea73d331b3a80df7748407f17efdb784b61b47e0e610d3bb5c',
     'https://github.com/python/cpython-source-deps/blob/xz-5.2.5/COPYING'))
NATIVE_SOURCE_CATALOG_HASH = '80c2da5cf616467448c7106ffaa811b7fee4d44ddc07ba259e2e9caeddef50dc'


def native_source_notices(bundle, destination):
    """Copy reviewed source-graph notices only for the observed frozen providers."""
    source = ROOT / 'packaging/third-party/native-source-notices'
    data = (source / 'inventory.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != NATIVE_SOURCE_CATALOG_HASH:
        raise ValueError('pinned native source catalog checksum mismatch')
    catalog = json.loads(data)
    for relative, checksum in catalog['providers'].items():
        binary = Path(bundle) / relative
        if (binary.is_symlink() or not binary.resolve().is_relative_to(Path(bundle).resolve())
                or hashlib.sha256(binary.read_bytes()).hexdigest() != checksum):
            raise ValueError('unreviewed native source provider: ' + relative)
    entries = []
    files = dict(catalog['files'], **{'inventory.json': NATIVE_SOURCE_CATALOG_HASH})
    for relative, checksum in files.items():
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
            raise ValueError('invalid native source notice path')
        notice = source / relative
        if notice.is_symlink() or not notice.resolve().is_relative_to(source.resolve()):
            raise ValueError('native source notice escapes catalog')
        notice_data = notice.read_bytes()
        if hashlib.sha256(notice_data).hexdigest() != checksum:
            raise ValueError('native source notice checksum mismatch: ' + relative)
        target = Path(destination) / 'native-source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(notice_data)
        entries.append({'file': 'native-source/' + relative, 'sha256': checksum})
    return entries


def runtime_source_notices(destination):
    """Conservative source notices, explicitly scoped to the reviewed baseline."""
    rows = []
    for name, checksum, url in RUNTIME_SOURCE_NOTICES:
        data = (ROOT / 'packaging/third-party/cpython-3.12.10' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError('pinned runtime source notice checksum mismatch: ' + name)
        relative = 'runtime-source/cpython-3.12.10/' + name
        target = Path(destination) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows.append({'file': relative, 'sha256': checksum, 'source': url,
                     'scope': 'CPython-3.12.10-source-baseline-not-binary-identification'})
    return rows


def native_crypto_notices(bundle, destination):
    """Attribute observed OpenSSL providers; this is not a complete native SBOM."""
    bundle, destination = Path(bundle), Path(destination)
    rows = []
    for relative in OPENSSL_PROVIDERS:
        binary = bundle / relative
        if binary.is_symlink() or not binary.resolve().is_relative_to(bundle.resolve()):
            raise ValueError('native provider escapes bundle')
        data = binary.read_bytes()
        versions = set(re.findall(rb'OpenSSL ([0-9]+\.[0-9]+\.[0-9]+)(?= )', data))
        if len(versions) != 1:
            raise ValueError('missing or ambiguous OpenSSL version: ' + relative)
        version = versions.pop().decode('ascii')
        if version not in OPENSSL_VERSIONS:
            raise ValueError('unreviewed OpenSSL version %s in %s' % (version, relative))
        license_data = (ROOT / 'packaging/third-party' / ('openssl-' + version) / 'LICENSE.txt').read_bytes()
        if hashlib.sha256(license_data).hexdigest() != OPENSSL_LICENSE_HASH:
            raise ValueError('pinned OpenSSL license checksum mismatch')
        target = destination / 'native' / ('openssl-' + version) / 'LICENSE.txt'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(license_data)
        rows.append({'component': 'OpenSSL', 'version': version,
                     'binary': relative, 'binary_sha256': hashlib.sha256(data).hexdigest(),
                     'file': target.relative_to(destination).as_posix(),
                     'sha256': OPENSSL_LICENSE_HASH,
                     'source': 'https://github.com/openssl/openssl/blob/openssl-%s/LICENSE.txt' % version})
    return rows


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


def inventory(distributions, destination, python_license, fallbacks=None, native_bundle=None):
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
        if native_bundle is not None:
            manifest['native_crypto_providers'] = native_crypto_notices(native_bundle, temporary)
            manifest['runtime_source_notices'] = runtime_source_notices(temporary)
            manifest['native_source_notices'] = native_source_notices(native_bundle, temporary)
        (temporary / 'inventory.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (temporary / 'README.txt').write_text(
            'These notices cover all installed Python distributions in the build environment.\n'
            'Some components are build tools and are not shipped as executable modules.\n'
            'inventory.json records exact versions and hashes of copied notices.\n'
            'native_crypto_providers, when present, links observed OpenSSL binaries to pinned licenses.\n'
            'runtime_source_notices covers a named CPython source baseline, not arbitrary runtime versions.\n'
            'native-source/inventory.json records a reviewed source-graph superset and matched provider hashes.\n'
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
    native_files = {}
    for row in manifest.get('native_crypto_providers', []):
        relative = row['binary']
        if relative not in OPENSSL_PROVIDERS:
            raise ValueError('invalid native provider path')
        binary = destination.parent / relative
        if binary.is_symlink() or not binary.resolve().is_relative_to(destination.parent.resolve()):
            raise ValueError('native provider escapes bundle')
        if hashlib.sha256(binary.read_bytes()).hexdigest() != row['binary_sha256']:
            raise ValueError('native provider checksum mismatch: ' + relative)
        previous = native_files.get(row['file'])
        if previous is not None and previous['sha256'] != row['sha256']:
            raise ValueError('conflicting native notices')
        native_files[row['file']] = row
    entries.extend(native_files.values())
    entries.extend(manifest.get('runtime_source_notices', []))
    entries.extend(manifest.get('native_source_notices', []))
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
    parser.add_argument('--bundle', type=Path, help='Inspect frozen OpenSSL providers and copy pinned licenses')
    args = parser.parse_args()
    try:
        manifest = (verify(args.destination) if args.verify else
                    inventory(metadata.distributions(), args.destination, args.python_license,
                              native_bundle=args.bundle))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, 'Notice collection failed: %s\n' % error)
    print('Collected notices for %d installed distributions' % len(manifest['distributions']))


if __name__ == '__main__':
    main()
