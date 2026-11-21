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

from seriald_core.flash_loaders import K1_RAM_BOOT_PROTOCOL, k1_ram_boot_installed, prepare_materials
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
        family = {'protocol': 'spacemit-ram-v1'}
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
            self.assertEqual(set(materials), {'chip_family', 'protocol', 'fsbl', 'uboot', 'usb_speed'})
            self.assertEqual(materials['usb_speed'], 'high')  # no USB port known
            publish(manifest.parent, root / 'installed')
            self.assertEqual((root / 'installed/uboot.bin').read_bytes(), b'RAM only uboot')

    def test_url_stream_stages_vendor_uboot_separately_and_reuses_fsbl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.bundle(root / 'bundle')
            board = SimpleNamespace(profile_data={'chip_family': 'k3'})
            with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(manifest)}):
                with self.assertRaisesRegex(TaskError, '原版 U-Boot'):
                    prepare_materials(board, root / 'workspace', lambda: None, url_stream=True)
                payload = b'vendor U-Boot'
                (manifest.parent / 'url.itb').write_bytes(payload)
                document = json.loads(manifest.read_text())
                document['families']['k3']['stream_uboot'] = {
                    'file': 'url.itb', 'sha256': hashlib.sha256(payload).hexdigest()}
                manifest.write_text(json.dumps(document))
                regular = prepare_materials(board, root / 'workspace', lambda: None)
                stream = prepare_materials(board, root / 'workspace', lambda: None, url_stream=True)
                self.assertEqual(stream['fsbl']['sha256'], regular['fsbl']['sha256'])
                self.assertEqual(Path(stream['uboot']['path']).read_bytes(), payload)
                self.assertEqual(Path(regular['uboot']['path']).read_bytes(), b'RAM only uboot')
                self.assertEqual(stream['uboot']['name'], 'u-boot.itb')
                publish(manifest.parent, root / 'installed')
                self.assertEqual((root / 'installed/url.itb').read_bytes(), payload)

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

    def test_k1_ram_boot_material_is_separate_from_the_firmware_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.bundle(root / 'bundle')
            document = json.loads(manifest.read_text())
            stock = dict(document['families']['k3'], protocol='stock')
            ram_fit = b'verified temporary FIT fixture'
            (manifest.parent / 'ram.itb').write_bytes(ram_fit)
            stock['ram_boot'] = {'protocol': K1_RAM_BOOT_PROTOCOL, 'fsbl': stock['fsbl'],
                                 'uboot': {'file': 'ram.itb', 'sha256': hashlib.sha256(ram_fit).hexdigest()}}
            document['families']['k1'] = stock
            manifest.write_text(json.dumps(document))
            installed = root / 'installed'
            publish(manifest.parent, installed)
            binding = SimpleNamespace(profile_data={'chip_family': 'k1'})
            with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(installed / 'manifest.json')}):
                self.assertTrue(k1_ram_boot_installed())
                normal = prepare_materials(binding, root / 'stock', lambda: None)
                ram = prepare_materials(binding, root / 'ram', lambda: None, k1_ram_boot=True)
            self.assertEqual(normal['protocol'], 'stock')
            self.assertEqual(Path(normal['uboot']['path']).read_bytes(), b'RAM only uboot')
            self.assertEqual(ram['protocol'], K1_RAM_BOOT_PROTOCOL)
            self.assertEqual(Path(ram['uboot']['path']).read_bytes(), ram_fit)

    @staticmethod
    def training(chipid=0x1234, kind=1):
        import base64, struct, zlib
        data = bytearray(0x9800)
        struct.pack_into('<IIQ', data, 0, 0x54524444, 0, chipid)
        struct.pack_into('<II', data, 28, kind, 2)
        data[200:204] = b'TRND'
        struct.pack_into('<I', data, 4, zlib.crc32(bytes(data[8:])))
        return base64.b64encode(bytes(data)).decode()

    def test_training_result_is_kept_per_board_and_embedded_into_the_fsbl_for_that_port(self):
        import struct, zlib
        from seriald_core import flash_loaders
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'
            bundle.mkdir()
            image = bytearray(0x20000)
            struct.pack_into('<4sBBHQQII', image, 0xfe0, b'AIHD', 1, 0, 0, 0x1f000, 0, 0, 0)
            struct.pack_into('<I', image, 0xffc, zlib.crc32(bytes(image[0x1000:0x20000])))
            (bundle / 'fsbl.bin').write_bytes(bytes(image))
            (bundle / 'uboot.bin').write_bytes(b'RAM only uboot')
            fsbl_sha = hashlib.sha256(bytes(image)).hexdigest()
            family = {'protocol': 'spacemit-ram-v1',
                      'fsbl': {'file': 'fsbl.bin', 'sha256': fsbl_sha,
                               'training_slots': {'lpddr5': {'offset': 0x2000, 'size': 0x9800},
                                                  'lpddr4x': {'offset': 0xc000, 'size': 0x9800}}},
                      'uboot': {'file': 'uboot.bin', 'sha256': hashlib.sha256(b'RAM only uboot').hexdigest()}}
            manifest = bundle / 'manifest.json'
            manifest.write_text(json.dumps({'version': 1, 'families': {'k3': family}}))
            workspace, work = root / 'workspace', root / 'flashing'
            workspace.mkdir()
            board = SimpleNamespace(profile_data={'chip_family': 'k3'}, device={'port_path': [1, 8, 2]})
            other = SimpleNamespace(profile_data={'chip_family': 'k3'}, device={'port_path': [1, 8, 3]})
            with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(manifest)}):
                # No result yet: the plain FSBL trains DDR.
                plain = prepare_materials(board, workspace, lambda: None, work)
                self.assertNotIn('trained_for', plain)
                self.assertEqual(Path(plain['fsbl']['path']).read_bytes(), bytes(image))
                with self.assertRaises(ValueError):
                    flash_loaders.record_training(board, work, {'data': self.training()})  # no board serial
                with self.assertRaises(ValueError):
                    import base64
                    corrupt = bytearray(base64.b64decode(self.training()))
                    corrupt[300] ^= 1
                    flash_loaders.record_training(board, work, {'data': base64.b64encode(bytes(corrupt)).decode(),
                                                                'identity': {'eeprom-serial': 'SN1'}})
                flash_loaders.record_training(board, work, {'data': self.training(), 'identity': {'eeprom-serial': 'SN1'}})
                trained = prepare_materials(board, workspace, lambda: None, work)
                self.assertEqual(trained['trained_for'], 'SN1')
                self.assertEqual(set(trained), {'chip_family', 'protocol', 'fsbl', 'uboot', 'trained_for', 'usb_speed'})
                self.assertEqual(trained['usb_speed'], 'super')
                flash_loaders.record_usb_speed(board, work, 'high')
                self.assertEqual(prepare_materials(board, workspace, lambda: None, work)['usb_speed'], 'high')
                self.assertEqual(prepare_materials(other, workspace, lambda: None, work)['usb_speed'], 'super')
                staged = Path(trained['fsbl']['path']).read_bytes()
                self.assertEqual(Path(trained['fsbl']['path']).parent.parent, Path(trained['uboot']['path']).parent)
                self.assertEqual(staged[0x2000 + 200:0x2000 + 204], b'TRND')
                self.assertEqual(staged[:0x2000], bytes(image[:0x2000])[:0xffc] + staged[0xffc:0x1000] + bytes(image[0x1000:0x2000]))
                self.assertEqual(struct.unpack_from('<I', staged, 0xffc)[0], zlib.crc32(staged[0x1000:0x20000]))
                self.assertEqual(hashlib.sha256(staged).hexdigest(), trained['fsbl']['sha256'])
                # Another port has no result; the plain cache stays valid.
                self.assertNotIn('trained_for', prepare_materials(other, workspace, lambda: None, work))
                self.assertEqual(Path(plain['fsbl']['path']).read_bytes(), bytes(image))
                # A failed load with it discards it; the next load trains again.
                self.assertTrue(flash_loaders.discard_training(board, work))
                self.assertNotIn('trained_for', prepare_materials(board, workspace, lambda: None, work))
                self.assertFalse(flash_loaders.discard_training(board, work))
                # A result from another FSBL build is not embedded.
                flash_loaders.record_training(board, work, {'data': self.training(), 'identity': {'serialno': 'SN1'}})
                entries = json.loads((work / 'ddr-training.json').read_text())['entries']
                entries[0]['fsbl_sha256'] = '0' * 64
                (work / 'ddr-training.json').write_text(json.dumps({'version': 1, 'entries': entries}))
                self.assertNotIn('trained_for', prepare_materials(board, workspace, lambda: None, work))

    def test_missing_materials_fail_explicitly_and_do_not_select_any_payload(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': directory + '/missing.json'}):
            with self.assertRaisesRegex(TaskError, '预备刷机环境'):
                prepare_materials(SimpleNamespace(profile_data={'chip_family': 'k3'}), directory, lambda: None)
