"""Linux notice collection never trusts a shared object's filename alone."""
import hashlib
import importlib.util
import json
from pathlib import Path
import io
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parent.parent / 'tools/collect_linux_mcp_notices.py'
SPEC = importlib.util.spec_from_file_location('linux_mcp_notices', SCRIPT)
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


class LinuxMCPNoticesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bundle = self.root / 'runtime'
        self.bundle.mkdir()
        self.wheelhouse = self.root / 'wheels'
        self.wheelhouse.mkdir()
        self.destination = self.bundle / 'third-party'
        self.lock = self.root / 'requirements.lock'
        self.native = b'fixture shared object'
        self.binary = self.bundle / '_internal/pydantic_core/native.so'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(self.native)
        wheel = self.wheelhouse / 'pydantic_core-2.50.4-cp310-cp310-manylinux2014_x86_64.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('pydantic_core-2.50.4.dist-info/METADATA',
                             'Name: pydantic_core\nVersion: 2.50.4\n')
            archive.writestr('pydantic_core/native.so', self.native)
        checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
        self.lock.write_text('pydantic-core==2.50.4 \\\n    --hash=sha256:' + checksum + '\n')
        self.catalog = {'wheels': [{'filename': wheel.name, 'sha256': checksum,
                                  'url': 'https://files.pythonhosted.org/example/' + wheel.name}]}
        self.license = self.root / 'license.txt'
        self.license.write_text('upstream license fixture')
        self.distribution = SimpleNamespace(metadata={'Name': 'pydantic_core'}, version='2.50.4',
            files=['pydantic_core.dist-info/LICENSE'], locate_file=lambda file: self.license)

    def collect(self):
        with patch.object(notices, 'source_notices', return_value=([], [], [])):
            return notices.collect(self.bundle, self.destination, self.wheelhouse, self.lock,
                                   [self.distribution], self.catalog)

    def test_wheel_bytes_and_actual_native_hash_are_recorded(self):
        result = self.collect()
        provider = result['native_providers'][0]
        self.assertEqual(provider['kind'], 'pypi-wheel')
        self.assertEqual(provider['wheel']['member'], 'pydantic_core/native.so')
        self.assertEqual(provider['sha256'], hashlib.sha256(self.native).hexdigest())
        self.assertEqual(notices.verify(self.destination, self.bundle), result)

    def test_tampered_wheel_is_rejected_before_notice_publication(self):
        wheel = next(self.wheelhouse.glob('*.whl'))
        with zipfile.ZipFile(wheel, 'a') as archive:
            archive.writestr('injected', 'changed')
        with self.assertRaisesRegex(ValueError, 'unreviewed wheel checksum'):
            self.collect()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.bundle.glob('.linux-notices-*')), [])

    def test_nonofficial_or_unpinned_wheel_provenance_is_rejected(self):
        self.catalog['wheels'][0]['url'] = 'https://example.invalid/wheel.whl'
        with self.assertRaisesRegex(ValueError, 'pinned official provenance'):
            self.collect()

    def test_modified_native_file_does_not_match_the_wheel(self):
        self.binary.write_bytes(self.native + b'changed')
        with patch.object(notices, 'dpkg_provider', side_effect=ValueError('no identical dpkg file')):
            with self.assertRaisesRegex(ValueError, 'no identical dpkg file'):
                self.collect()
        self.assertFalse(self.destination.exists())

    def test_missing_distribution_license_fails_atomically(self):
        self.distribution.files = []
        with self.assertRaisesRegex(ValueError, 'license files missing'):
            self.collect()
        self.assertFalse(self.destination.exists())

    def test_local_project_metadata_is_excluded_and_complete_lock_required(self):
        local = SimpleNamespace(metadata={'Name': 'decklab-mcp'}, version='0.1.0', files=[])
        with patch.object(notices, 'metadata') as metadata:
            metadata.distributions.return_value = [local, self.distribution]
            with patch.object(notices, 'source_notices', return_value=([], [], [])):
                result = notices.collect(self.bundle, self.destination, self.wheelhouse, self.lock,
                                         catalog=self.catalog)
        self.assertEqual([row['name'] for row in result['distributions']], ['pydantic-core'])
        other = self.root / 'other-notices'
        self.lock.write_text(self.lock.read_text() + 'missing==1.0 --hash=sha256:' + 'f' * 64 + '\n')
        with self.assertRaisesRegex(ValueError, 'complete hash lock'):
            notices.collect(self.bundle, other, self.wheelhouse, self.lock,
                            [self.distribution], self.catalog)
        self.assertFalse(other.exists())

    def test_verifier_detects_notice_and_native_tampering(self):
        result = self.collect()
        self.binary.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'native provider checksum mismatch'):
            notices.verify(self.destination, self.bundle)
        self.binary.write_bytes(self.native)
        license_path = self.destination / result['distributions'][0]['licenses'][0]['file']
        license_path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'notice checksum mismatch'):
            notices.verify(self.destination, self.bundle)

    def test_verifier_detects_extra_native_file_and_path_escape(self):
        result = self.collect()
        extra = self.bundle / 'unattributed.so.1'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'does not cover every'):
            notices.verify(self.destination, self.bundle)
        extra.unlink()
        result['native_providers'][0]['binary'] = '../outside.so'
        (self.destination / 'inventory.json').write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'invalid inventory path'):
            notices.verify(self.destination, self.bundle)

    def test_missing_hashes_and_duplicate_requirements_are_rejected(self):
        for value in ('pydantic-core==2.50.4\n', self.lock.read_text() * 2):
            self.lock.write_text(value)
            with self.assertRaises(ValueError):
                notices.requirement_hashes(self.lock)

    def test_cargo_lock_preserves_exact_registry_checksum(self):
        data = b'''version = 3
[[package]]
name = "local-provider"
version = "1.0"
[[package]]
name = "source-dependency"
version = "2.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "abc123"
dependencies = ["other"]
'''
        self.assertEqual(notices.cargo_packages(data), [{'name': 'source-dependency', 'version': '2.3',
            'source': 'registry+https://github.com/rust-lang/crates.io-index', 'checksum': 'abc123'}])
        with self.assertRaisesRegex(ValueError, 'incomplete Cargo.lock'):
            notices.cargo_packages(data.replace(b'checksum = "abc123"\n', b''))

    def test_source_archive_tampering_and_missing_crate_notices_are_rejected(self):
        source = self.wheelhouse / 'source.tar.gz'
        lock = (b'[[package]]\nname = "unreviewed"\nversion = "1.0"\n'
                b'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                b'checksum = "' + b'a' * 64 + b'"\n')
        with tarfile.open(source, 'w:gz') as archive:
            member = tarfile.TarInfo('source/Cargo.lock')
            member.size = len(lock)
            archive.addfile(member, io.BytesIO(lock))
        artifact = {'name': 'cryptography', 'version': '50.0.2', 'filename': source.name,
                    'url': 'https://files.pythonhosted.org/source.tar.gz',
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        wrong = dict(artifact, sha256='f' * 64)
        with self.assertRaisesRegex(ValueError, 'source archive checksum mismatch'):
            notices.fetch_source(wrong, self.wheelhouse)
        reviewed = self.root / 'reviewed'
        additional = self.root / 'additional'
        for path in (reviewed, additional):
            path.mkdir()
            (path / 'inventory.json').write_text(json.dumps({'crates': [], 'files': {}, 'rust_toolchains': []}))
        checksum = hashlib.sha256((reviewed / 'inventory.json').read_bytes()).hexdigest()
        with patch.object(notices, 'SOURCE_CATALOG', reviewed), \
                patch.object(notices, 'SOURCE_CATALOG_SHA256', checksum), \
                patch.object(notices, 'ADDITIONAL_SOURCE_CATALOG', additional), \
                patch.object(notices, 'ADDITIONAL_SOURCE_CATALOG_SHA256', checksum):
            with self.assertRaisesRegex(ValueError, 'missing Cargo.lock crates.*unreviewed'):
                notices.source_notices(self.root / 'output', self.wheelhouse,
                                       {'sources': [artifact]}, [])


if __name__ == '__main__':
    unittest.main()
