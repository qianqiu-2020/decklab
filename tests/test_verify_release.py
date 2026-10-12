import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace

from tools.bundle_manifest import create
from tools.collect_windows_notices import inventory, OPENSSL_PROVIDERS
from tools.verify_release import checksums, unpack_portable, inspect_release


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
