"""Collect offline notices and exact native provenance for a Linux MCP worker.

Wheel bytes must match the reviewed hash lock. Every frozen shared object must
match a member of such a wheel or a file owned by the build image's dpkg database.
The Rust notices are a conservative source-lock coverage set, not a binary SBOM.
"""
import argparse
from email.parser import BytesParser
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent.parent
SOURCE_CATALOG = ROOT / 'packaging/third-party/native-source-notices'
SOURCE_CATALOG_SHA256 = 'a20d92f7de96c8ac7a30fa7f352e2444f0da9bbc4f90e11d63eeb76cd3ae0273'
ADDITIONAL_SOURCE_CATALOG = ROOT / 'packaging/third-party/linux-mcp-native-notices'
ADDITIONAL_SOURCE_CATALOG_SHA256 = '239c4ff44450693401c2f617d1e75c79079ed6ccb7b8fa3dbf9a02f58ebafec6'
NATIVE_VERSIONS = {'cryptography': '50.0.2', 'cffi': '2.1.1',
                   'pydantic-core': '2.50.4', 'rpds-py': '0.30.0'}
OPENSSL_LICENSE_SHA256 = '7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('invalid distribution name')
    return re.sub(r'[-_.]+', '-', name).lower()


def safe_path(base, relative):
    if not isinstance(relative, str):
        raise ValueError('invalid inventory path: ' + str(relative))
    path = PurePosixPath(relative)
    if (path.is_absolute() or '..' in path.parts
            or '\\' in relative or ':' in relative or not path.parts):
        raise ValueError('invalid inventory path: ' + str(relative))
    result = Path(base) / relative
    if result.is_symlink() or not result.resolve().is_relative_to(Path(base).resolve()):
        raise ValueError('inventory path escapes directory: ' + relative)
    return result


def copy_notice(destination, relative, data, **fields):
    if not data.strip():
        raise ValueError('empty notice: ' + relative)
    target = safe_path(destination, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return dict(file=relative, sha256=digest(data), **fields)


def requirement_hashes(requirements):
    text = Path(requirements).read_text(encoding='utf-8').replace('\\\n', ' ')
    result = {}
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = re.match(r'^([A-Za-z0-9._-]+)==([^\s;]+)', line)
        hashes = set(re.findall(r'--hash(?:=|\s+)sha256:([a-f0-9]{64})(?=\s|$)', line))
        if not match or not hashes:
            raise ValueError('every build requirement must have a version and SHA256 hashes')
        key = (normalized(match.group(1)), match.group(2))
        if key in result:
            raise ValueError('duplicate build requirement')
        result[key] = hashes
    if not result:
        raise ValueError('empty build requirement lock')
    return result


def artifact_catalog(path=None):
    path = Path(path or ROOT / 'packaging/linux-mcp-wheels.json')
    return json.loads(path.read_text(encoding='utf-8'))


def wheel_index(wheelhouse, requirements, catalog=None):
    """Return native-byte hashes only from exact, reviewed wheel archives."""
    allowed = requirement_hashes(requirements)
    catalog = artifact_catalog() if catalog is None else catalog
    artifacts = catalog.get('wheels', [])
    if not artifacts:
        artifacts = [wheel for row in catalog.get('distributions', [])
                     for wheel in row.get('wheels', [])]
    if isinstance(artifacts, dict):
        artifacts = list(artifacts.values())
    official = {item['filename']: item for item in artifacts}
    native, wheels = {}, []
    for path in sorted(Path(wheelhouse).glob('*.whl')):
        if path.is_symlink():
            raise ValueError('wheel must be a regular file')
        data = path.read_bytes()
        checksum = digest(data)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            manifests = [name for name in names if name.endswith('.dist-info/METADATA')]
            if len(manifests) != 1:
                raise ValueError('wheel has ambiguous metadata: ' + path.name)
            info = BytesParser().parsebytes(archive.read(manifests[0]))
            name, version = normalized(info['Name']), info['Version']
            if checksum not in allowed.get((name, version), set()):
                raise ValueError('unreviewed wheel checksum: ' + path.name)
            provenance = official.get(path.name)
            if (not provenance or provenance['sha256'] != checksum
                    or not provenance['url'].startswith('https://files.pythonhosted.org/')):
                raise ValueError('wheel lacks pinned official provenance: ' + path.name)
            row = {'name': name, 'version': version, 'filename': path.name,
                   'sha256': checksum, 'source': provenance['url']}
            wheels.append(row)
            for member in names:
                if re.search(r'\.so(?:\.|$)', PurePosixPath(member).name):
                    if version != NATIVE_VERSIONS.get(name):
                        raise ValueError('unreviewed native distribution: ' + name + ' ' + version)
                    # Reading zip members does not extract potentially unsafe paths.
                    value = archive.read(member)
                    native.setdefault(digest(value), []).append(dict(row, member=member))
    if not wheels:
        raise ValueError('wheelhouse is empty')
    return native, wheels


def distribution_notices(distributions, destination):
    rows, seen = [], set()
    for dist in sorted(distributions, key=lambda item: normalized(item.metadata['Name'])):
        name = normalized(dist.metadata['Name'])
        if name in seen:
            raise ValueError('duplicate installed distribution: ' + name)
        seen.add(name)
        notices = []
        for number, file in enumerate(sorted(dist.files or (), key=str)):
            path = PurePosixPath(str(file).replace('\\', '/'))
            if (any(part.lower() in ('licenses', 'licences') for part in path.parts)
                    or re.match(r'^(license|licence|copying|notice)([._-]|$)', path.name, re.I)):
                if path.suffix.lower() in ('.py', '.pyc', '.pyd', '.dll', '.exe', '.so'):
                    continue
                original = Path(dist.locate_file(file))
                relative = 'python/%s/%03d-%s' % (name, number,
                    re.sub(r'[^A-Za-z0-9._-]', '_', path.name))
                notices.append(copy_notice(destination, relative, original.read_bytes(),
                                           original=str(file), source='installed-distribution'))
        if not notices:
            raise ValueError('license files missing for %s %s' % (name, dist.version))
        rows.append({'name': name, 'version': dist.version,
                     'license_expression': dist.metadata.get('License-Expression'),
                     'license_metadata': dist.metadata.get('License'), 'licenses': notices})
    return rows


def dpkg_provider(binary):
    """Find a byte-identical source file; filenames alone never establish identity."""
    result = subprocess.run(['dpkg-query', '-S', '*/' + binary.name], check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    checksum = digest(binary.read_bytes())
    for line in result.stdout.splitlines():
        if ': ' not in line:
            continue
        packages, original = line.split(': ', 1)
        source = Path(original)
        if not source.is_absolute() or not source.is_file() or digest(source.read_bytes()) != checksum:
            continue
        package = packages.split(', ')[0]
        version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', package], text=True)
        copyright_path = Path('/usr/share/doc') / package.split(':', 1)[0] / 'copyright'
        return {'package': package, 'version': version, 'source_file': original,
                'copyright_file': str(copyright_path), 'copyright': copyright_path.read_bytes()}
    raise ValueError('native provider has no reviewed wheel or identical dpkg file: ' + str(binary))


def native_providers(bundle, destination, wheel_native):
    rows, notices = [], {}
    for binary in sorted(Path(bundle).rglob('*')):
        if not binary.is_file() or not re.search(r'\.so(?:\.|$)', binary.name):
            continue
        relative = binary.relative_to(bundle).as_posix()
        safe_path(bundle, relative)
        data = binary.read_bytes()
        row = {'binary': relative, 'sha256': digest(data), 'size': len(data)}
        matches = wheel_native.get(row['sha256'], [])
        if matches:
            row.update(kind='pypi-wheel', wheel=matches[0],
                       rustc_commits=sorted({item.decode('ascii') for item in
                           re.findall(rb'/rustc/([0-9a-f]{40})/', data)}))
            if matches[0]['name'] == 'cryptography':
                versions = set(re.findall(rb'OpenSSL ([0-9]+\.[0-9]+\.[0-9]+)(?= )', data))
                if len(versions) != 1:
                    raise ValueError('missing or ambiguous embedded OpenSSL version')
                version = versions.pop().decode('ascii')
                if version not in ('3.0.16', '4.0.3'):
                    raise ValueError('unreviewed embedded OpenSSL version: ' + version)
                license_data = (ROOT / 'packaging/third-party' / ('openssl-' + version)
                                / 'LICENSE.txt').read_bytes()
                if digest(license_data) != OPENSSL_LICENSE_SHA256:
                    raise ValueError('pinned OpenSSL notice checksum mismatch')
                notice = copy_notice(destination, 'native/openssl-' + version + '/LICENSE.txt',
                    license_data, source='https://github.com/openssl/openssl/blob/openssl-%s/LICENSE.txt' % version)
                notices[notice['file']] = notice
                row['openssl_version'] = version
        else:
            provider = dpkg_provider(binary)
            notice = copy_notice(destination, 'debian/%s/copyright' %
                provider['package'].replace(':', '_'), provider.pop('copyright'),
                source=provider.pop('copyright_file'))
            notices[notice['file']] = notice
            row.update(kind='dpkg', dpkg=provider, notice=notice['file'])
        rows.append(row)
    if not rows:
        raise ValueError('frozen runtime has no native providers')
    return rows, list(notices.values())


def fetch_source(artifact, cache):
    path = safe_path(cache, artifact['filename'])
    if path.is_symlink():
        raise ValueError('source archive must be a regular file')
    if not path.exists():
        if not artifact['url'].startswith('https://files.pythonhosted.org/'):
            raise ValueError('source archive is not an official pinned PyPI artifact')
        failure = None
        for attempt in range(3):
            try:
                # A direct final attempt also works in build environments with
                # an intermittently failing configured HTTP proxy. Certificate
                # verification stays enabled in both cases.
                opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                          if attempt == 2 else urllib.request.build_opener())
                with opener.open(artifact['url'], timeout=30) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError('source archive exceeds the build download limit')
                break
            except OSError as error:
                failure = error
        else:
            raise OSError('could not download pinned source archive: ' + artifact['filename']) from failure
        if digest(data) != artifact['sha256']:
            raise ValueError('source archive download checksum mismatch')
        path.write_bytes(data)
    if digest(path.read_bytes()) != artifact['sha256']:
        raise ValueError('source archive checksum mismatch: ' + path.name)
    return path


def cargo_packages(data):
    """Cargo.lock's registry package identity fields need no TOML extension modules."""
    rows = []
    for block in data.decode('utf-8').split('[[package]]')[1:]:
        fields = dict(re.findall(r'^(name|version|source|checksum) = "([^"\n]+)"$', block, re.M))
        if fields.get('source', '').startswith('registry+'):
            if not all(key in fields for key in ('name', 'version', 'checksum')):
                raise ValueError('incomplete Cargo.lock registry package')
            rows.append(fields)
    if not rows:
        raise ValueError('Cargo.lock contains no registry package identities')
    return rows


def source_notices(destination, wheelhouse, catalog, providers):
    data = (SOURCE_CATALOG / 'inventory.json').read_bytes()
    if digest(data) != SOURCE_CATALOG_SHA256:
        raise ValueError('pinned native source catalog checksum mismatch')
    reviewed = json.loads(data)
    extra_data = (ADDITIONAL_SOURCE_CATALOG / 'inventory.json').read_bytes()
    if digest(extra_data) != ADDITIONAL_SOURCE_CATALOG_SHA256:
        raise ValueError('pinned additional Linux source catalog checksum mismatch')
    additional = json.loads(extra_data)
    crates = {(row['name'], row['version'], row['checksum']): row
              for row in reviewed['crates'] + additional.get('crates', [])}
    entries, source_locks = [], []
    artifacts = {normalized(row['name']): row for row in catalog.get('sources', [])}
    for name in ('cryptography', 'pydantic-core', 'rpds-py'):
        artifact = artifacts.get(name)
        if not artifact or artifact['version'] != NATIVE_VERSIONS[name]:
            raise ValueError('missing pinned source artifact: ' + name)
        archive_path = fetch_source(artifact, wheelhouse)
        with tarfile.open(archive_path) as archive:
            locks = [member for member in archive.getmembers() if member.name.endswith('/Cargo.lock')]
            if len(locks) != 1 or not locks[0].isfile():
                raise ValueError('source archive has ambiguous Cargo.lock: ' + name)
            lock = archive.extractfile(locks[0]).read()
        packages = cargo_packages(lock)
        missing = [(row['name'], row['version'], row['checksum']) for row in packages
                   if (row['name'], row['version'], row['checksum']) not in crates]
        if missing:
            raise ValueError('native source notices missing Cargo.lock crates for %s: %s' % (name, missing))
        entry = copy_notice(destination, 'source-locks/' + name + '/Cargo.lock', lock,
                            source=artifact['url'])
        entries.append(entry)
        source_locks.append(dict(artifact, cargo_lock_sha256=digest(lock),
                                 packages=packages, file=entry['file'],
                                 scope='complete-committed-source-lock-coverage-including-build-test-and-other-targets'))
    # The complete hash-pinned notice set is intentionally broader than Linux's
    # selected features. Windows provider paths remain provenance of that source
    # catalog only; actual Linux providers above are checked against wheel bytes.
    for relative, checksum in reviewed['files'].items():
        notice = safe_path(SOURCE_CATALOG, relative).read_bytes()
        if digest(notice) != checksum:
            raise ValueError('native source notice checksum mismatch: ' + relative)
        entries.append(copy_notice(destination, 'native-source/' + relative, notice,
                                   source='reviewed-upstream-source-notice-catalog'))
    entries.append(copy_notice(destination, 'native-source/upstream-inventory.json', data,
                               source='reviewed-source-catalog-with-original-platform-provenance'))
    commits = {commit for row in providers for commit in row.get('rustc_commits', [])}
    toolchains = {row['commit']: row for row in reviewed['rust_toolchains']}
    toolchains.update({row['commit']: row for row in additional.get('rust_toolchains', [])})
    for relative, checksum in additional.get('files', {}).items():
        notice = safe_path(ADDITIONAL_SOURCE_CATALOG, relative).read_bytes()
        if digest(notice) != checksum:
            raise ValueError('additional Linux source notice checksum mismatch: ' + relative)
        entries.append(copy_notice(destination, 'native-source/' + relative, notice,
                                   source='official-Linux-native-runtime-source-notices'))
    entries.append(copy_notice(destination, 'native-source/linux-additions-inventory.json', extra_data,
                               source='reviewed-Linux-source-notice-catalog'))
    if not commits or commits - toolchains.keys():
        raise ValueError('unreviewed embedded Rust compiler commits: ' + str(sorted(commits - toolchains.keys())))
    return entries, source_locks, [toolchains[commit] for commit in sorted(commits)]


def collect(bundle, destination, wheelhouse, requirements, distributions=None, catalog=None):
    bundle, destination = Path(bundle), Path(destination)
    if destination.exists():
        raise ValueError('notice destination already exists; use a fresh bundle')
    catalog = artifact_catalog() if catalog is None else catalog
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.linux-notices-', dir=destination.parent))
    try:
        native, wheels = wheel_index(wheelhouse, requirements, catalog)
        distributions = list(metadata.distributions()) if distributions is None else distributions
        locked = requirement_hashes(requirements)
        distributions = [dist for dist in distributions
                         if (normalized(dist.metadata['Name']), dist.version) in locked]
        installed = {(normalized(dist.metadata['Name']), dist.version) for dist in distributions}
        if installed != set(locked):
            raise ValueError('installed distributions do not cover the complete hash lock: '
                             + str(sorted(set(locked) - installed)))
        rows = distribution_notices(distributions, temporary)
        providers, system_notices = native_providers(bundle, temporary, native)
        source_entries, source_locks, toolchains = source_notices(temporary, wheelhouse, catalog, providers)
        manifest = {'schema': 1, 'scope': 'Linux-frozen-worker-with-build-environment-notice-superset',
                    'python_version': sys.version.split()[0], 'distributions': rows,
                    'wheel_artifacts': wheels, 'native_providers': providers,
                    'native_source_locks': source_locks, 'rust_toolchains': toolchains,
                    'notices': system_notices + source_entries,
                    'limitations': ['Rust source notices cover complete upstream source locks; selected wheel build features are not attested.',
                                    'Distribution notices include build tools and may exceed the frozen module set.']}
        (temporary / 'inventory.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        (temporary / 'README.txt').write_text(__doc__ + '\n', encoding='utf-8')
        verify(temporary, bundle)
        temporary.rename(destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(destination, bundle=None):
    destination = Path(destination)
    bundle = Path(bundle) if bundle is not None else destination.parent
    manifest = json.loads((destination / 'inventory.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or not manifest.get('native_providers'):
        raise ValueError('unsupported or empty Linux native notice inventory')
    entries = list(manifest['notices'])
    seen = set()
    for row in manifest['distributions']:
        name = normalized(row['name'])
        if name in seen or not row.get('licenses'):
            raise ValueError('duplicate distribution or missing notices')
        seen.add(name)
        entries.extend(row['licenses'])
    seen_files = set()
    for entry in entries:
        relative = entry['file']
        if relative in seen_files:
            raise ValueError('duplicate notice path')
        seen_files.add(relative)
        if digest(safe_path(destination, relative).read_bytes()) != entry['sha256']:
            raise ValueError('notice checksum mismatch: ' + relative)
    providers = set()
    for row in manifest['native_providers']:
        relative = row['binary']
        if relative in providers or not re.search(r'\.so(?:\.|$)', PurePosixPath(relative).name):
            raise ValueError('invalid or duplicate native provider')
        providers.add(relative)
        data = safe_path(bundle, relative).read_bytes()
        if digest(data) != row['sha256'] or len(data) != row['size']:
            raise ValueError('native provider checksum mismatch: ' + relative)
        if row['kind'] == 'pypi-wheel':
            if row['wheel'] not in manifest['wheel_artifacts'] and not any(
                    all(row['wheel'].get(key) == wheel.get(key) for key in
                        ('name', 'version', 'filename', 'sha256', 'source'))
                    for wheel in manifest['wheel_artifacts']):
                raise ValueError('native provider wheel provenance missing')
        elif row['kind'] != 'dpkg' or row['notice'] not in seen_files:
            raise ValueError('native provider notice missing')
    actual = {path.relative_to(bundle).as_posix() for path in bundle.rglob('*')
              if path.is_file() and re.search(r'\.so(?:\.|$)', path.name)}
    if providers != actual:
        raise ValueError('native provider inventory does not cover every frozen shared object')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--requirements', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        verify(args.destination, args.bundle)
    elif not all((args.bundle, args.wheelhouse, args.requirements)):
        parser.error('--bundle, --wheelhouse and --requirements are required')
    else:
        collect(args.bundle, args.destination, args.wheelhouse, args.requirements)


if __name__ == '__main__':
    main()
