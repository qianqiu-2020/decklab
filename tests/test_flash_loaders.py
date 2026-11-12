"""Preinstalled RAM-only loader provenance, managed copies and isolation."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from seriald_core.flash_loaders import prepare_materials
from seriald_core.flash_runner import FlashRunnerError, LocalFlashExecutor
from seriald_core.tasks import TaskError
from tools.package_flash_loaders import publish
from test_flash_runner import configuration
from seriald_core.flash_runner import FlashBook


class FlashLoaderTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows short-path normalization')
    def test_windows_short_runtime_paths_are_canonical_before_go_loader_validation(self):
        import ctypes
        from seriald_core.transfer_artifacts import TransferArtifacts
        from seriald_core.storage import Store
        with tempfile.TemporaryDirectory(prefix='decklab loader path ') as directory:
            buffer = ctypes.create_unicode_buffer(32768)
            count = ctypes.windll.kernel32.GetShortPathNameW(directory, buffer, len(buffer))
            self.assertTrue(count, 'cannot obtain Windows runtime path')
            short = Path(buffer.value)
            executor = LocalFlashExecutor(short / 'flashing')
            cache = executor._directory(short / 'flashing/cache')
            self.assertEqual(cache, cache.resolve())
            self.assertEqual(executor.root, (short / 'flashing').resolve())
            store = Store(short / 'registry.sqlite3')
            try:
                artifacts = TransferArtifacts(store, short / 'artifacts')
                self.assertEqual(artifacts.root, artifacts.root.resolve())
            finally:
                store.close()

    def bundle(self, root):
        root.mkdir()
        family = {'protocol': 'decklab-nor-v1'}
        for key in ('fsbl', 'uboot'):
            payload = ('RAM only ' + key).encode()
            (root / (key + '.bin')).write_bytes(payload)
            family[key] = {'file': key + '.bin', 'sha256': hashlib.sha256(payload).hexdigest()}
        (root / 'manifest.json').write_text(json.dumps({'version': 1, 'families': {'k3': family}}))
        (root / 'BUILD-INFO.txt').write_text('public offline fixture')
        (root / 'COPYING.txt').write_text('test fixture; no actual firmware')
        return root / 'manifest.json'

    def test_chip_specific_loaders_are_copied_and_checked_without_firmware_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.bundle(root / 'bundle')
            workspace = root / 'workspace'
            workspace.mkdir()
            with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(manifest)}):
                materials = prepare_materials(SimpleNamespace(profile_data={'chip_family': 'k3'}), workspace, lambda: None)
            self.assertEqual(materials['chip_family'], 'k3')
            self.assertEqual(Path(materials['fsbl']['path']), workspace / 'ram-loaders/factory/FSBL.bin')
            self.assertEqual(Path(materials['uboot']['path']).read_bytes(), b'RAM only uboot')
            self.assertEqual(set(materials), {'chip_family', 'protocol', 'fsbl', 'uboot'})
            publish(manifest.parent, root / 'installed')
            self.assertEqual((root / 'installed/uboot.bin').read_bytes(), b'RAM only uboot')

    def test_changed_loader_wrong_family_and_escaping_paths_are_rejected(self):
        for failure in ('checksum', 'family', 'path'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = self.bundle(root / 'bundle')
                family = 'k1' if failure == 'family' else 'k3'
                if failure == 'checksum':
                    (manifest.parent / 'uboot.bin').write_bytes(b'changed')
                if failure == 'path':
                    document = json.loads(manifest.read_text())
                    document['families']['k3']['uboot']['file'] = '../outside.bin'
                    (root / 'outside.bin').write_bytes(b'RAM only uboot')
                    manifest.write_text(json.dumps(document))
                workspace = root / 'workspace'
                workspace.mkdir()
                with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(manifest)}), self.assertRaises(TaskError):
                    prepare_materials(SimpleNamespace(profile_data={'chip_family': family}), workspace, lambda: None)

    def test_missing_materials_fail_explicitly_and_do_not_select_any_payload(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': directory + '/missing.json'}):
            with self.assertRaisesRegex(TaskError, '预备刷机环境'):
                prepare_materials(SimpleNamespace(profile_data={'chip_family': 'k3'}), directory, lambda: None)
