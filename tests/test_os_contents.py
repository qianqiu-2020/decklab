import gzip
import io
import random
import struct
import unittest
import unittest.mock
from pathlib import Path

from seriald_core import os_contents
from seriald_core.os_contents import ImageError, image_content

# 16 MiB GPT disk built with sgdisk, mkfs.vfat and mke2fs -d: opensbi raw
# firmware, an ESP with a GRUB version string and grub.cfg, and an ext4 rootfs
# whose /etc/os-release is a relative symlink, /lib -> usr/lib, a gzip kernel
# with a Linux banner, extlinux.conf and a private os-release field.
DISK = gzip.decompress((Path(__file__).parent / 'fixtures' / 'os-disk.img.gz').read_bytes())
ROOTFS = DISK[12288 * 512:28672 * 512]


def inspect(data, **options):
    return image_content(io.BytesIO(data), len(data), **options)


def sparse(raw, block=4096):
    """Android sparse encoding with raw, fill and don't-care chunks."""
    chunks = []
    for offset in range(0, len(raw), block):
        piece = raw[offset:offset + block]
        if piece == bytes(block):
            chunks.append(struct.pack('<2H2I', 0xcac3, 0, 1, 12))
        elif piece == piece[:4] * (block // 4):
            chunks.append(struct.pack('<2H2I', 0xcac2, 0, 1, 16) + piece[:4])
        else:
            chunks.append(struct.pack('<2H2I', 0xcac1, 0, 1, 12 + block) + piece)
    header = struct.pack('<I4H4I', 0xed26ff3a, 1, 0, 28, 12, block, len(raw) // block, len(chunks), 0)
    return header + b''.join(chunks)


class OSContentsTests(unittest.TestCase):
    def test_gpt_disk_reports_os_kernel_bootloader_and_firmware(self):
        result = inspect(DISK, kind='disk')
        self.assertEqual(result['format'], 'gpt')
        self.assertEqual(result['version'], 'Bianbu 2.3.5 · 20260908194336')
        parts = {part['name']: part for part in result['partitions']}
        self.assertEqual(parts['opensbi']['content']['version'], 'OpenSBI v1.6')
        esp = parts['EFI System']
        self.assertEqual(esp['type'], 'EFI System Partition')
        self.assertEqual(esp['filesystem'], {'type': 'fat12', 'label': 'ESP', 'uuid': '2E6C-DB97'})
        self.assertEqual(esp['boot']['loader'], 'GRUB 2.12-9+deb13u2')
        self.assertIn('root UUID 11111111-2222-3333-4444-555555555555', esp['boot']['entries'])
        root = parts['rootfs']
        self.assertEqual(root['os']['build'], '20260908194336')
        self.assertEqual(root['kernels'][0]['release'], '6.6.63')
        self.assertEqual(root['kernel_modules'], ['6.6.63'])
        self.assertIn('/boot/vmlinuz-6.6.63', root['boot']['entries'])
        self.assertNotIn('do-not-show', str(result))

    def test_android_sparse_matches_the_expanded_filesystem(self):
        plain, wrapped = inspect(ROOTFS), inspect(sparse(ROOTFS))
        self.assertTrue(wrapped.pop('sparse'))
        self.assertEqual(wrapped.pop('expanded_size'), len(ROOTFS))
        plain.pop('size'), wrapped.pop('size')
        self.assertEqual(wrapped, plain)
        broken = bytearray(sparse(ROOTFS))
        broken[28:30] = b'\x00\x00'
        self.assertEqual(inspect(bytes(broken))['version_state'], 'invalid-format')

    def test_read_budget_and_compressed_images_are_explicit(self):
        with unittest.mock.patch.object(os_contents, 'MAX_READ_BYTES', 16 * 1024):
            with self.assertRaises(ImageError):
                inspect(DISK)
        for head in (b'\xfd7zXZ\x00', b'\x1f\x8b\x08\x00', b'\x28\xb5\x2f\xfd'):
            result = inspect(head + bytes(4096))
            self.assertEqual((result['format'], result['version_state']), ('compressed', 'unknown'))
        self.assertEqual(inspect(bytes(4096))['version_state'], 'unknown')

    def test_filesystem_without_version_files_is_not_called_unknown(self):
        esp = DISK[4096 * 512:12288 * 512]
        result = inspect(esp[:0x4000] + bytes(len(esp) - 0x4000))
        self.assertIn(result['version_state'], ('no-version-files', 'unknown'))

    def test_corrupt_metadata_never_raises_beyond_budget(self):
        rng = random.Random(7)
        for _ in range(150):
            data = bytearray(DISK)
            for _flip in range(40):
                position = rng.randrange(0, 64 * 1024) if rng.random() < .5 else rng.randrange(len(data))
                data[position] = rng.randrange(256)
            try:
                result = inspect(bytes(data))
            except ImageError as error:
                self.assertIn('budget', str(error))
                continue
            self.assertIn(result['version_state'], ('identified', 'unknown', 'no-version-files', 'invalid-format'))


if __name__ == '__main__':
    unittest.main()
