import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tools.verify_release import checksums, inspect_release, inspect_linux_runtime, ROOT
from tools.build_linux_mcp import SOURCES


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

    def fixture(self):
        self.deb = 'decklab_1.19.0_amd64.deb'
        self.setup = 'decklab-1.19.0-windows-x64-setup.exe'
        self.receipt = 'windows-install-verification.json'
        self.aliases = {'decklab_latest_amd64.deb': self.deb,
                        'decklab-windows-x64-setup.exe': self.setup}
        (self.root / self.deb).write_bytes(b'fixture deb')
        (self.root / self.setup).write_bytes(b'MZ fixture installer')
        digest = hashlib.sha256((self.root / self.setup).read_bytes()).hexdigest()
        (self.root / (self.setup + '.sha256')).write_text(digest)
        (self.root / self.receipt).write_text(json.dumps({
            'schema': 1, 'version': '1.19.0', 'sha256': digest,
            'bundle_files': 26, 'distribution_notice_sets': 4,
        }))
        for alias, canonical in self.aliases.items():
            (self.root / alias).write_bytes((self.root / canonical).read_bytes())
        self.artifacts = [self.deb, self.setup, self.receipt, *self.aliases]
        self.write_checksums('SHA256SUMS.txt', self.artifacts)
        self.write_checksums('linux-SHA256SUMS.txt', [self.deb])

    def test_aliases_must_match_and_installer_receipt_is_bound_to_exact_file(self):
        self.fixture()
        (self.root / 'decklab_latest_amd64.deb').write_bytes(b'different deb')
        self.write_checksums('SHA256SUMS.txt', self.artifacts)
        with self.assertRaisesRegex(ValueError, 'alias differs'):
            inspect_release(self.root, '1.19.0')
        shutil.copyfile(self.root / self.deb, self.root / 'decklab_latest_amd64.deb')
        receipt = json.loads((self.root / self.receipt).read_text())
        receipt['sha256'] = '1' * 64
        (self.root / self.receipt).write_text(json.dumps(receipt))
        self.write_checksums('SHA256SUMS.txt', self.artifacts)
        with self.assertRaisesRegex(ValueError, 'does not match the exact installer'):
            inspect_release(self.root, '1.19.0')

    def linux_runtime(self, architecture='amd64'):
        runtime = self.root / ('runtime-' + architecture)
        runtime.mkdir()
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        header[18:20] = {'amd64': 62, 'arm64': 183}[architecture].to_bytes(2, 'little')
        executable = runtime / 'decklab-mcp-worker'
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
                                     for name in SOURCES},
                    'build_platform': {'os': 'ubuntu', 'version': '22.04', 'glibc': '2.35',
                                       'machine': {'amd64': 'x86_64', 'arm64': 'aarch64'}[architecture]}, 'files': {}}
        for path in runtime.rglob('*'):
            if path.is_file():
                manifest['files'][path.relative_to(runtime).as_posix()] = {
                    'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        (runtime / 'runtime-manifest.json').write_text(json.dumps(manifest))
        return runtime

    def write_checksums(self, manifest, names):
        (self.root / manifest).write_text(''.join(
            hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in names))

    def test_native_debian_runtime_checks_file_set_hash_baseline_and_elf_architecture(self):
        runtime = self.linux_runtime()
        inspect_linux_runtime(runtime, 'amd64')
        with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
            inspect_linux_runtime(runtime, 'arm64')
        executable = runtime / 'decklab-mcp-worker'
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

    def test_release_checks_installer_receipt_and_debian_runtime(self):
        self.fixture()
        runtime = self.linux_runtime()
        def dpkg(command, **kwargs):
            if command[1] == '--field':
                return SimpleNamespace(stdout='Package: decklab\nVersion: 1.19.0\nArchitecture: amd64\n')
            self.assertEqual(command[1], '--extract')
            code = Path(command[3]) / 'usr/lib/decklab'
            shutil.copytree(runtime, code / 'mcp-runtime')
            for name in ('decklab_mcp.py', 'decklabctl.py'):
                shutil.copyfile(ROOT / name, code / name)
            return SimpleNamespace(returncode=0)
        with patch('tools.verify_release.subprocess.run', side_effect=dpkg), \
             patch('tools.verify_release.inspect_complete_install'), \
             patch('tools.verify_release.inspect_desktop_runtime'), \
             patch('tools.verify_release.inspect_platform_tools') as platform_tools:
            result = inspect_release(self.root, '1.19.0')
        platform_tools.assert_called_once()
        self.assertEqual(result['debian_architectures'], ['amd64'])
        self.assertEqual(result['bundle_files'], 26)

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
