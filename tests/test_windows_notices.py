"""Offline notice collection uses synthetic installed distributions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / 'tools/collect_windows_notices.py'
spec = importlib.util.spec_from_file_location('windows_notices', SCRIPT)
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.python = self.root / 'Python.txt'
        self.python.write_text('Python attribution fixture')
        self.destination = self.root / 'bundle' / 'third-party'

    def distribution(self, name, files, version='1.0'):
        return SimpleNamespace(metadata={'Name': name, 'License-Expression': 'MIT'}, version=version,
                               files=files, requires=['dependency>=1; platform_system == "Windows"'],
                               locate_file=lambda file: self.root / file)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding='utf-8')
        return name

    def test_versions_nested_notices_hashes_and_utf8_are_collected(self):
        files = [self.write('one.dist-info/licenses/LICENSE', '许可 fixture'),
                 self.write('one.dist-info/licenses/vendor/MIT.txt', 'vendor notice'),
                 self.write('one/code.py', 'do not copy code')]
        result = notices.inventory([self.distribution('One_Package', files)], self.destination, self.python)
        self.assertEqual(result['scope'], 'installed-build-environment-superset')
        row = result['distributions'][0]
        self.assertEqual((row['name'], row['version']), ('One_Package', '1.0'))
        self.assertEqual(len(row['licenses']), 2)
        self.assertEqual(row['requires_dist'], ['dependency>=1; platform_system == "Windows"'])
        for entry in row['licenses']:
            data = (self.destination / entry['file']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
        self.assertEqual(json.loads((self.destination / 'inventory.json').read_text(encoding='utf-8')), result)
        self.assertEqual((self.destination / 'Python-LICENSE.txt').read_bytes(), self.python.read_bytes())

    def test_exact_version_fallback_and_unknown_version_rejection(self):
        license_path = self.root / self.write('fallback/LICENSE.txt', 'upstream notice')
        fallback = {('serial', '1.0'): (license_path, 'https://example.invalid/v1.0/LICENSE')}
        result = notices.inventory([self.distribution('Serial', None)], self.destination, self.python, fallback)
        self.assertEqual(result['distributions'][0]['licenses'][0]['source'], fallback['serial', '1.0'][1])
        with self.assertRaisesRegex(ValueError, 'license files missing'):
            notices.inventory([self.distribution('Serial', None, '2.0')], self.root / 'other', self.python, fallback)
        self.assertFalse((self.root / 'other').exists())

    def test_missing_empty_or_unreadable_notice_fails_without_partial_publication(self):
        empty = self.write('empty/LICENSE', '')
        for files in ([], [empty], ['missing/LICENSE']):
            with self.subTest(files=files), self.assertRaises((ValueError, OSError)):
                notices.inventory([self.distribution('bad', files)], self.destination, self.python, {})
            self.assertFalse(self.destination.exists())
            self.assertEqual(list(self.destination.parent.glob('.notices-*')), [])

    def test_existing_destination_is_never_overwritten(self):
        self.destination.mkdir(parents=True)
        marker = self.destination / 'keep'
        marker.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            notices.inventory([], self.destination, self.python)
        self.assertEqual(marker.read_text(), 'keep')

    def test_duplicate_and_path_like_names_rejected(self):
        file = self.write('valid/LICENSE', 'notice')
        for distributions in ([self.distribution('../escape', [file])],
                              [self.distribution('a_b', [file]), self.distribution('a-b', [file])]):
            with self.assertRaises(ValueError):
                notices.inventory(distributions, self.destination, self.python)
            self.assertFalse(self.destination.exists())

    def test_license_recognition(self):
        for file in ('LICENSE.txt', 'COPYING', 'NOTICE', 'd.dist-info/licenses/MIT.txt', 'LICENSE.APACHE'):
            self.assertTrue(notices.license_file(file))
        for file in ('code.py', 'license_handler.py', 'licensed.py'):
            self.assertFalse(notices.license_file(file))

    def test_verifier_detects_missing_tampered_and_escaping_files(self):
        file = self.write('valid/LICENSE', 'notice')
        result = notices.inventory([self.distribution('Valid', [file])], self.destination, self.python)
        self.assertEqual(notices.verify(self.destination), result)
        target = self.destination / result['distributions'][0]['licenses'][0]['file']
        target.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            notices.verify(self.destination)
        target.unlink()
        with self.assertRaises(OSError):
            notices.verify(self.destination)
        entry = result['distributions'][0]['licenses'][0]
        for path in ('../outside', '/absolute', 'C:/outside', 'folder\\outside'):
            entry['file'] = path
            (self.destination / 'inventory.json').write_text(json.dumps(result))
            with self.subTest(path=path), self.assertRaises(ValueError):
                notices.verify(self.destination)

    def test_repository_fallback_is_pinned_to_actual_pyserial_version(self):
        path, source = notices.FALLBACKS['pyserial', '3.5']
        data = path.read_text()
        self.assertIn('Chris Liechti', data)
        self.assertIn('/v3.5/', source)

    def native_bundle(self, versions=('3.0.16', '4.0.3')):
        bundle = self.destination.parent
        for relative, version in zip(notices.OPENSSL_PROVIDERS, versions):
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture\0OpenSSL ' + version.encode() + b' 11 Feb 2025\0')
        return bundle

    def test_native_providers_copy_exact_licenses_and_verify_binary_hashes(self):
        bundle = self.native_bundle()
        result = notices.inventory([], self.destination, self.python, native_bundle=bundle)
        self.assertEqual([row['version'] for row in result['native_crypto_providers']], ['3.0.16', '4.0.3'])
        self.assertEqual(notices.verify(self.destination), result)
        row = result['native_crypto_providers'][0]
        (bundle / row['binary']).write_bytes(b'changed provider')
        with self.assertRaisesRegex(ValueError, 'native provider checksum'):
            notices.verify(self.destination)

    def test_native_unknown_missing_and_ambiguous_versions_fail_atomically(self):
        bundle = self.native_bundle()
        binary = bundle / notices.OPENSSL_PROVIDERS[0]
        for data in (b'OpenSSL 9.0.0 future', b'no version',
                     b'OpenSSL 3.0.16 old\0OpenSSL 4.0.3 new'):
            binary.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                notices.inventory([], self.destination, self.python, native_bundle=bundle)
            self.assertFalse(self.destination.exists())
        binary.unlink()
        with self.assertRaises(OSError):
            notices.inventory([], self.destination, self.python, native_bundle=bundle)
        self.assertFalse(self.destination.exists())

    def test_native_shared_version_uses_one_license_and_two_provider_records(self):
        bundle = self.native_bundle(('3.0.16', '3.0.16'))
        result = notices.inventory([], self.destination, self.python, native_bundle=bundle)
        self.assertEqual(len(result['native_crypto_providers']), 2)
        self.assertEqual(len(list((self.destination / 'native').rglob('LICENSE.txt'))), 1)
        self.assertEqual(notices.verify(self.destination), result)

    def test_native_pinned_license_tampering_fails_before_publication(self):
        bundle = self.native_bundle()
        with patch.object(notices, 'OPENSSL_LICENSE_HASH', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'pinned OpenSSL license checksum'):
                notices.inventory([], self.destination, self.python, native_bundle=bundle)
        self.assertFalse(self.destination.exists())

    def test_native_license_tampering_fails_verification(self):
        bundle = self.native_bundle()
        result = notices.inventory([], self.destination, self.python, native_bundle=bundle)
        (self.destination / result['native_crypto_providers'][0]['file']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'notice checksum'):
            notices.verify(self.destination)
