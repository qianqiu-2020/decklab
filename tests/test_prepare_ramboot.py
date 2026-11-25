import gzip
import io
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import call, patch

from decklab_core.ramboot_prepare import (
    IMAGE_MAGIC, K1_BIANBU_JUNE_SHA256, K1_BIANBU_RCPU, K1_BIANBU_SHA256,
    K3_PICO_MODEL, PrepareError, choose_dtb, clear_k1_boot_properties,
    kernel_image, newc_entry, prepare_k1_bianbu_rcpu, ramboot_tool_path, select_partitions,
)


class PrepareRambootTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'private LZ4 tooling is Linux-only')
    def test_private_lz4_path_requires_both_initramfs_cli_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / 'decklab_core' / 'ramboot_prepare.py'
            tools = Path(tmp) / 'ramboot-tools'
            tools.mkdir()
            cli = tools / 'lz4'
            cli.write_text('#!/bin/sh\n')
            cli.chmod(0o755)
            with patch('decklab_core.ramboot_prepare.__file__', str(code)), patch.dict(
                    'os.environ', {'PATH': '/host-tools'}):
                self.assertEqual(ramboot_tool_path(), '/host-tools')
                (tools / 'lz4cat').symlink_to('lz4')
                self.assertEqual(ramboot_tool_path(), str(tools) + ':/host-tools')

    def test_june_bianbu_keeps_original_rcpu_enabled(self):
        with patch('decklab_core.ramboot_prepare.run',
                   side_effect=[b'spacemit,k1-x-rproc\n', b'okay\n']) as fdt:
            self.assertEqual(prepare_k1_bianbu_rcpu(Path('board.dtb'), K1_BIANBU_JUNE_SHA256), [])
            self.assertEqual(fdt.call_args_list, [
                call('fdtget', '-t', 's', 'board.dtb', K1_BIANBU_RCPU, 'compatible'),
                call('fdtget', '-t', 's', 'board.dtb', K1_BIANBU_RCPU, 'status'),
            ])

    def test_september_bianbu_limit_stays_bound_to_exact_image(self):
        with patch('decklab_core.ramboot_prepare.run', return_value=b'spacemit,k1-x-rproc\n') as fdt:
            with self.assertRaisesRegex(PrepareError, 'no verified'):
                prepare_k1_bianbu_rcpu(Path('board.dtb'), '0' * 64)
            fdt.assert_not_called()
            self.assertEqual(prepare_k1_bianbu_rcpu(Path('board.dtb'), K1_BIANBU_SHA256),
                             ['k1-bianbu-rcpu-disabled'])
            fdt.assert_called_with('fdtput', '-t', 's', 'board.dtb', K1_BIANBU_RCPU, 'status', 'disabled')

    def test_archive_partitions_must_be_unambiguous_regular_files(self):
        for name, entry_type in (('../rootfs.ext4', tarfile.REGTYPE),
                                 ('rootfs.ext4', tarfile.SYMTYPE)):
            member = tarfile.TarInfo(name)
            member.type = entry_type
            member.size = 1
            with self.subTest(name=name, entry_type=entry_type):
                with self.assertRaisesRegex(PrepareError, 'unsafe or ambiguous'):
                    select_partitions([member], tarfile.TarInfo.isfile,
                                      lambda value: value.name, lambda value: value.size)
        first = tarfile.TarInfo('one/rootfs.ext4')
        second = tarfile.TarInfo('two/rootfs.ext4')
        first.size = second.size = 1
        with self.assertRaisesRegex(PrepareError, 'unsafe or ambiguous'):
            select_partitions([first, second], tarfile.TarInfo.isfile,
                              lambda value: value.name, lambda value: value.size)

    def test_unified_kernel_extracts_only_embedded_riscv_image(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = (Path(directory) / name for name in ('vmlinux', 'Image'))
            image = bytearray(64)
            image[48:60] = IMAGE_MAGIC
            unified = bytearray(192)
            unified[:2] = b'MZ'
            struct.pack_into('<I', unified, 60, 64)
            unified[64:70] = b'PE\0\0\x64\x50'
            struct.pack_into('<H', unified, 70, 1)
            unified[88:96] = b'.linux\0\0'
            struct.pack_into('<IIII', unified, 96, len(image), 0, len(image), 128)
            unified[128:] = image
            source.write_bytes(unified)
            kernel_image(source, output)
            self.assertEqual(output.read_bytes(), bytes(image))
            struct.pack_into('<I', unified, 108, 129)
            source.write_bytes(unified)
            with self.assertRaisesRegex(PrepareError, 'section bounds'):
                kernel_image(source, output)

    def test_gzip_kernel_extracts_a_riscv_image(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = (Path(directory) / name for name in ('vmlinuz', 'Image'))
            image = bytearray(64)
            image[48:60] = IMAGE_MAGIC
            source.write_bytes(gzip.compress(image))
            kernel_image(source, output)
            self.assertEqual(output.read_bytes(), bytes(image))

    def test_pico_model_variant_requires_the_board_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'rootfs.ext4'
            board = b'board data'

            def extract(_image, name, destination):
                destination.write_bytes(board)

            with patch('decklab_core.ramboot_prepare.ext_list', return_value=[('pico.dtb', '100644', len(board))]), \
                    patch('decklab_core.ramboot_prepare.ext_dump', side_effect=extract), \
                    patch('decklab_core.ramboot_prepare.run') as fdt:
                for compat, expect in ((b'spacemit,k3-pico-itx spacemit,k3', True), (b'spacemit,k3', False)):
                    fdt.side_effect = lambda *args: b'SpacemiT K3 Pico ITX' if args[-1] == 'model' else compat
                    if expect:
                        self.assertEqual(choose_dtb(root, root, '6.18', K3_PICO_MODEL, Path(temp)), board)
                    else:
                        with self.assertRaisesRegex(PrepareError, 'found 0'):
                            choose_dtb(root, root, '6.18', K3_PICO_MODEL, Path(temp))

    def test_newc_payload_preserves_length_and_alignment(self):
        archive = io.BytesIO()
        payload = io.BytesIO(b'abcNEXT')
        newc_entry(archive, 13, 'ramroot/rootfs.0000', 0o100600, 3, payload)
        data = archive.getvalue()
        size = int(data[54:62], 16)
        name_size = int(data[94:102], 16)
        start = (110 + name_size + 3) & ~3
        self.assertEqual(size, 3)
        self.assertEqual(data[110:110 + name_size], b'ramroot/rootfs.0000\0')
        self.assertEqual(data[start:], b'abc\0')
        self.assertEqual(payload.read(), b'NEXT')

    def test_k1_direct_boot_drops_inherited_args_and_efi_pointers(self):
        with patch('decklab_core.ramboot_prepare.run', side_effect=[
                b'chosen\nsoc\n', b'bootargs\nstdout-path\nlinux,uefi-system-table\n', b'', b'']) as fdt:
            clear_k1_boot_properties(Path('board.dtb'))
        self.assertEqual(fdt.call_args_list, [
            call('fdtget', '-l', 'board.dtb', '/'),
            call('fdtget', '-p', 'board.dtb', '/chosen'),
            call('fdtput', '-d', 'board.dtb', '/chosen', 'bootargs'),
            call('fdtput', '-d', 'board.dtb', '/chosen', 'linux,uefi-system-table'),
        ])

    def test_k1_direct_boot_accepts_a_tree_without_chosen(self):
        with patch('decklab_core.ramboot_prepare.run', return_value=b'soc\n') as fdt:
            clear_k1_boot_properties(Path('board.dtb'))
        fdt.assert_called_once_with('fdtget', '-l', 'board.dtb', '/')
