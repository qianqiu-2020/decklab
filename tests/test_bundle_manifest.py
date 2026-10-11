"""Complete package checks catch DLL/resource changes without native execution."""
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from tools.bundle_manifest import NAME, create, verify


class BundleManifestTests(unittest.TestCase):
    def fixture(self, root):
        root = Path(root)
        (root / '_internal').mkdir()
        (root / 'docs').mkdir()
        for name in ('seriald.exe', '_internal/crypto.dll', '_internal/module.pyd',
                     'docs/使用说明.md', 'Start-seriald.cmd'):
            (root / name).write_bytes(('fixture ' + name).encode())
        return root

    def test_complete_inventory_roundtrip_and_native_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            manifest = create(root)
            self.assertEqual(verify(root), manifest)
            self.assertEqual(len(manifest['files']), 5)
            self.assertEqual(manifest['files']['_internal/crypto.dll']['kind'], 'native')
            self.assertEqual(manifest['files']['docs/使用说明.md']['kind'], 'resource')
            self.assertNotIn(NAME, manifest['files'])

    def test_same_size_dll_or_resource_modification_is_rejected(self):
        for name in ('_internal/crypto.dll', 'Start-seriald.cmd'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                create(root)
                path = root / name
                data = path.read_bytes()
                path.write_bytes(b'X' + data[1:])
                with self.assertRaisesRegex(ValueError, 'content changed'):
                    verify(root)

    def test_added_and_missing_files_are_rejected(self):
        for change in ('add', 'remove'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                create(root)
                if change == 'add':
                    (root / '_internal/stale.dll').write_bytes(b'leftover')
                else:
                    (root / 'Start-seriald.cmd').unlink()
                with self.assertRaisesRegex(ValueError, 'file set changed'):
                    verify(root)

    def test_runtime_data_cannot_be_published_or_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            create(root)
            (root / 'data').mkdir()
            for operation in (create, verify):
                with self.assertRaisesRegex(ValueError, 'runtime data'):
                    operation(root)

    def test_redirected_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            try:
                (root / 'alias.dll').symlink_to(root / '_internal/crypto.dll')
            except OSError:
                self.skipTest('Symlink creation unavailable')
            with self.assertRaisesRegex(ValueError, 'redirected'):
                create(root)

    def test_invalid_manifest_and_outside_path_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            manifest = create(root)
            for bad in ({}, dict(manifest, files={}),
                        dict(manifest, files={'../outside': {'sha256': 'ignored'}})):
                (root / NAME).write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    verify(root)

    def test_build_cli_returns_failure_for_modified_bundle(self):
        tool = Path(__file__).resolve().parents[1] / 'tools/bundle_manifest.py'
        with tempfile.TemporaryDirectory(prefix='decklab bundle ') as directory:
            root = self.fixture(directory)
            subprocess.run([sys.executable, str(tool), str(root)], check=True, capture_output=True)
            subprocess.run([sys.executable, str(tool), str(root), '--verify'], check=True, capture_output=True)
            (root / '_internal/crypto.dll').write_bytes(b'modified')
            result = subprocess.run([sys.executable, str(tool), str(root), '--verify'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'content changed', result.stderr)
