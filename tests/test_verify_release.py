import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace

from tools.bundle_manifest import create
from tools.collect_windows_notices import inventory, OPENSSL_PROVIDERS
from tools.verify_release import checksums, unpack_portable, inspect_release, inspect_linux_runtime, ROOT


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_checksums_reject_changed_missing_duplicate_and_escaping_names(self):
        (self.root / 'file.zip').write_bytes(b'fixture')
        digest = hashlib.sha256(b'fixture').hexdigest()
        manifest = self.root / 'SHA256SUMS.txt'
        manifest.write_text(digest + '  file.zip\n')
        checksums(self.root, manifest.name, ['file.zip'])
        for text in (digest + '  ../file.zip\n', (digest + '  file.zip\n') * 2,
                     '0' * 64 + '  file.zip\n', digest + '  other.zip\n'):
            manifest.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                checksums(self.root, manifest.name, ['file.zip'])

    def test_build_directory_prefix_is_explicit_and_does_not_allow_other_paths(self):
        (self.root / 'fixture.deb').write_bytes(b'deb')
        digest = hashlib.sha256(b'deb').hexdigest()
        manifest = self.root / 'linux-SHA256SUMS.txt'
        manifest.write_text(digest + '  dist/fixture.deb\n')
        checksums(self.root, manifest.name, ['fixture.deb'], 'dist/')
        with self.assertRaises(ValueError):
            checksums(self.root, manifest.name, ['fixture.deb'])

    def test_windows_zip_separators_normalize_without_changing_bytes(self):
        archive = self.root / 'portable.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('seriald\\file.txt', b'unchanged\r\n')
        root = unpack_portable(archive, self.root / 'extracted')
        self.assertEqual((root / 'file.txt').read_bytes(), b'unchanged\r\n')

    def test_unsafe_archive_is_rejected_before_any_member_is_written(self):
        archive = self.root / 'portable.zip'
        for name in ('seriald/../outside', '/seriald/file', 'seriald/data/file',
                     'seriald/FILE.txt', 'seriald\\file.txt', 'C:/seriald/file'):
            with self.subTest(name=name):
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr('seriald/file.txt', b'first')
                    output.writestr(name, b'bad')
                destination = self.root / 'extracted'
                with self.assertRaises(ValueError):
                    unpack_portable(archive, destination)
                self.assertFalse(destination.exists())

    def fixture(self, version='1.5.0'):
        bundle = self.root / 'build' / 'seriald'
        bundle.mkdir(parents=True)
        (bundle / 'seriald.exe').write_bytes(b'MZ fixture host')
        (bundle / 'package-version.txt').write_text(version + '\n')
        python_license = self.root / 'Python-LICENSE.txt'
        python_license.write_text('fixture notice')
        for relative, provider_version in zip(OPENSSL_PROVIDERS, ('3.0.16', '4.0.3')):
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'MZ OpenSSL ' + provider_version.encode() + b' fixture')
        # Model the already published 1.5.0 bundle, before source-graph supplements.
        with patch('tools.collect_windows_notices.native_source_notices', return_value=[]):
            inventory([], bundle / 'third-party', python_license, native_bundle=bundle)
        create(bundle)
        archive = self.root / 'seriald-windows-portable.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for path in bundle.rglob('*'):
                if path.is_file():
                    output.write(path, 'seriald/' + path.relative_to(bundle).as_posix())
        deb = 'seriald_%s_all.deb' % version
        (self.root / deb).write_bytes(b'fixture deb')
        setup = self.root / ('seriald-%s-windows-x64-setup.exe' % version)
        setup.write_bytes(b'MZ fixture installer')
        setup.with_suffix('.exe.sha256').write_text(hashlib.sha256(setup.read_bytes()).hexdigest())
        names = (deb, setup.name, archive.name)
        lines = [hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name for name in names]
        (self.root / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n')
        (self.root / 'linux-SHA256SUMS.txt').write_text(lines[0] + '\n')

    def test_end_to_end_fixture_checks_metadata_bundle_notices_and_version(self):
        self.fixture()
        result = SimpleNamespace(stdout='Package: seriald\nVersion: 1.5.0\nArchitecture: all\n')
        with patch('tools.verify_release.subprocess.run', return_value=result):
            verified = inspect_release(self.root, '1.5.0')
        self.assertEqual(verified['version'], '1.5.0')
        result.stdout = 'Package: seriald\nVersion: 1.4.50\nArchitecture: all\n'
        with patch('tools.verify_release.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
                inspect_release(self.root, '1.5.0')

    def test_new_releases_require_reviewed_native_source_supplements(self):
        self.fixture('1.5.1')
        result = SimpleNamespace(stdout='Package: seriald\nVersion: 1.5.1\nArchitecture: all\n')
        with patch('tools.verify_release.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'native source catalog missing'):
                inspect_release(self.root, '1.5.1')

    def test_stable_aliases_are_required_and_must_match_even_with_valid_checksums(self):
        version = '1.7.0'
        self.fixture(version)
        result = SimpleNamespace(stdout='Package: seriald\nVersion: 1.7.0\nArchitecture: all\n')
        aliases = {'seriald_latest_all.deb': 'seriald_1.7.0_all.deb',
                   'seriald-windows-x64-setup.exe': 'seriald-1.7.0-windows-x64-setup.exe'}
        with self.assertRaisesRegex(ValueError, 'file set mismatch'):
            inspect_release(self.root, version)
        for alias, canonical in aliases.items():
            (self.root / alias).write_bytes((self.root / canonical).read_bytes())

        def update_manifest():
            names = ['seriald_1.7.0_all.deb', 'seriald-1.7.0-windows-x64-setup.exe',
                     'seriald-windows-portable.zip', *aliases]
            (self.root / 'SHA256SUMS.txt').write_text('\n'.join(
                hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name
                for name in names) + '\n')

        update_manifest()
        # The old notice fixture intentionally lacks the post-1.5.0 source
        # supplements. Reaching that independent gate proves aliases passed.
        with patch('tools.verify_release.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'native source catalog missing'):
                inspect_release(self.root, version)
        (self.root / 'seriald-windows-x64-setup.exe').write_bytes(b'MZ different installer')
        update_manifest()  # A checksum alone must not bless a mismatched alias.
        with self.assertRaisesRegex(ValueError, 'alias differs'):
            inspect_release(self.root, version)


    def linux_runtime(self, architecture='amd64'):
        runtime = self.root / ('runtime-' + architecture)
        runtime.mkdir()
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        header[18:20] = {'amd64': 62, 'arm64': 183}[architecture].to_bytes(2, 'little')
        executable = runtime / 'seriald-mcp-worker'
        executable.write_bytes(header)
        executable.chmod(0o755)
        notices = runtime / 'third-party/inventory.json'
        notices.parent.mkdir()
        library = runtime / '_internal/native-fixture.so'
        library.parent.mkdir()
        library.write_bytes(header)
        entries = []
        for name in ('sdk-license.txt', 'native-license.txt'):
            data = ('fixture ' + name).encode()
            (notices.parent / name).write_bytes(data)
            entries.append({'file': name, 'sha256': hashlib.sha256(data).hexdigest()})
        notices.write_text(json.dumps({'schema': 1, 'notices': [entries[1]],
            'distributions': [{'name': 'mcp', 'version': '2.3.0', 'licenses': [entries[0]]}],
            'native_providers': [{'binary': '_internal/native-fixture.so', 'kind': 'dpkg',
                'size': len(header), 'sha256': hashlib.sha256(header).hexdigest(),
                'notice': entries[1]['file']}]}))
        manifest = {'schema': 1, 'architecture': architecture, 'executable': executable.name,
                    'mcp_version': '2.3.0', 'notices': 'third-party/inventory.json',
                    'requirements_sha256': hashlib.sha256((ROOT / 'packaging/linux-mcp-requirements.lock').read_bytes()).hexdigest(),
                    'source_files': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                     for name in ('seriald_mcp.py', 'serialctl.py', 'tools/linux_mcp_entrypoint.py')},
                    'build_platform': {'os': 'ubuntu', 'version': '22.04', 'glibc': '2.35',
                                       'machine': {'amd64': 'x86_64', 'arm64': 'aarch64'}[architecture]}, 'files': {}}
        for path in runtime.rglob('*'):
            if path.is_file():
                manifest['files'][path.relative_to(runtime).as_posix()] = {
                    'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        (runtime / 'runtime-manifest.json').write_text(json.dumps(manifest))
        return runtime

    def release_arch_fixtures(self, version='1.7.1'):
        self.fixture(version)
        (self.root / ('seriald_%s_all.deb' % version)).unlink()
        debs = ['seriald_%s_%s.deb' % (version, architecture) for architecture in ('amd64', 'arm64')]
        for name in debs:
            (self.root / name).write_bytes(name.encode())
        aliases = {'seriald_latest_amd64.deb': debs[0], 'seriald_latest_arm64.deb': debs[1],
                   'seriald_latest_all.deb': debs[0],
                   'seriald-windows-x64-setup.exe': 'seriald-%s-windows-x64-setup.exe' % version}
        for alias, canonical in aliases.items():
            (self.root / alias).write_bytes((self.root / canonical).read_bytes())
        names = [*debs, 'seriald-%s-windows-x64-setup.exe' % version,
                 'seriald-windows-portable.zip', *aliases]
        self.write_checksums('SHA256SUMS.txt', names)
        self.write_checksums('linux-SHA256SUMS.txt', debs)
        return debs, aliases

    def write_checksums(self, manifest, names):
        (self.root / manifest).write_text(''.join(
            hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in names))

    def test_native_debian_runtime_checks_file_set_hash_baseline_and_elf_architecture(self):
        runtime = self.linux_runtime()
        inspect_linux_runtime(runtime, 'amd64')
        with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
            inspect_linux_runtime(runtime, 'arm64')
        executable = runtime / 'seriald-mcp-worker'
        saved = executable.read_bytes()
        executable.write_bytes(saved + b'modified')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            inspect_linux_runtime(runtime, 'amd64')
        executable.write_bytes(saved)
        extra = runtime / 'unexpected'
        extra.write_text('not listed')
        with self.assertRaisesRegex(ValueError, 'file set mismatch'):
            inspect_linux_runtime(runtime, 'amd64')
        extra.unlink()
        manifest_path = runtime / 'runtime-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['build_platform']['version'] = '24.04'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, '22.04 build baseline'):
            inspect_linux_runtime(runtime, 'amd64')
        manifest['build_platform']['version'] = '22.04'
        manifest['architecture'] = 'arm64'
        manifest['build_platform']['machine'] = 'aarch64'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'executable architecture mismatch'):
            inspect_linux_runtime(runtime, 'arm64')

    def test_new_release_requires_both_architectures_and_all_stable_aliases(self):
        debs, aliases = self.release_arch_fixtures()
        complete = (self.root / 'SHA256SUMS.txt').read_text()
        for missing in (*debs, *aliases):
            with self.subTest(missing=missing):
                (self.root / 'SHA256SUMS.txt').write_text(''.join(
                    row + '\n' for row in complete.splitlines() if not row.endswith('  ' + missing)))
                with self.assertRaisesRegex(ValueError, 'file set mismatch'):
                    inspect_release(self.root, '1.7.1')
        (self.root / 'SHA256SUMS.txt').write_text(complete)
        (self.root / 'seriald_latest_all.deb').write_bytes((self.root / debs[1]).read_bytes())
        names = [row.split('  ', 1)[1] for row in complete.splitlines()]
        self.write_checksums('SHA256SUMS.txt', names)
        with self.assertRaisesRegex(ValueError, 'alias differs'):
            inspect_release(self.root, '1.7.1')

    def test_new_release_inspects_native_runtime_for_each_debian_architecture(self):
        self.release_arch_fixtures()
        runtimes = {architecture: self.linux_runtime(architecture) for architecture in ('amd64', 'arm64')}
        def dpkg(command, **kwargs):
            architecture = 'arm64' if '_arm64.deb' in command[2] else 'amd64'
            if command[1] == '--field':
                return SimpleNamespace(stdout='Package: seriald\nVersion: 1.7.1\nArchitecture: %s\n' % architecture)
            self.assertEqual(command[1], '--extract')
            code = Path(command[3]) / 'usr/lib/seriald'
            shutil.copytree(runtimes[architecture], code / 'mcp-runtime')
            for name in ('seriald_mcp.py', 'serialctl.py'):
                shutil.copyfile(ROOT / name, code / name)
            return SimpleNamespace(returncode=0)
        # This fixture still models the old Windows notices. Reaching that gate
        # proves both independent Debian runtime and stable-alias checks passed.
        with patch('tools.verify_release.subprocess.run', side_effect=dpkg) as invoked:
            with self.assertRaisesRegex(ValueError, 'native source catalog missing'):
                inspect_release(self.root, '1.7.1')
        self.assertEqual(sum(call.args[0][1] == '--extract' for call in invoked.call_args_list), 2)


    def test_native_debian_notices_and_library_provenance_are_independently_checked(self):
        runtime = self.linux_runtime()
        manifest_path = runtime / 'runtime-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        notices = runtime / 'third-party/inventory.json'
        inventory = json.loads(notices.read_text())
        inventory['native_providers'][0]['sha256'] = '0' * 64
        notices.write_text(json.dumps(inventory))
        manifest['files']['third-party/inventory.json'] = {
            'sha256': hashlib.sha256(notices.read_bytes()).hexdigest(), 'size': notices.stat().st_size}
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'native provider checksum mismatch'):
            inspect_linux_runtime(runtime, 'amd64')

    def test_native_debian_rejects_wrong_architecture_in_a_shared_library(self):
        runtime = self.linux_runtime()
        library = runtime / '_internal/native-fixture.so'
        data = bytearray(library.read_bytes())
        data[18:20] = (183).to_bytes(2, 'little')
        library.write_bytes(data)
        manifest_path = runtime / 'runtime-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['_internal/native-fixture.so'] = {
            'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'native library architecture mismatch'):
            inspect_linux_runtime(runtime, 'amd64')
