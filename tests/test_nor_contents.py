import hashlib
import lzma
import struct
import unittest
import uuid

from seriald_core import nor_contents
from seriald_core.nor_contents import partition_content

# `lzop -9` of 256 filler bytes around a K3 U-Boot version string.
LZOP_UBOOT = bytes.fromhex(
    '894c5a4f000d0a1a0a104020a00940030903000001000081b46af7ece0000000000a62616e6e65722e62696e92a609b3'
    '00000229000000385fff13a4151300000020db0c00000c552d426f6f7420323032322e31302d67633234643934633763'
    '6120284f634103396f0336290020dfa00411000000000000')


def fit(properties):
    strings = b''
    tree = struct.pack('>I', 1) + b'\0' * 4
    for key, value in properties.items():
        offset = len(strings)
        strings += key.encode() + b'\0'
        tree += struct.pack('>3I', 3, len(value), offset) + value
        tree += b'\0' * (-len(value) % 4)
    tree += struct.pack('>2I', 2, 9)
    return (struct.pack('>10I', 0xd00dfeed, 40 + len(tree) + len(strings), 40,
                        40 + len(tree), 0, 17, 16, 0, len(strings), len(tree)) + tree + strings)


def fit_images(images, root=None):
    """FIT with root properties and /images/<name> nodes, as mkimage lays them out."""
    strings, offsets = b'', {}

    def prop(key, value):
        nonlocal strings
        if key not in offsets:
            offsets[key] = len(strings)
            strings += key.encode() + b'\0'
        return struct.pack('>3I', 3, len(value), offsets[key]) + value + b'\0' * (-len(value) % 4)

    def node(name):
        raw = name.encode() + b'\0'
        return struct.pack('>I', 1) + raw + b'\0' * (-len(raw) % 4)

    tree = node('') + b''.join(prop(k, v) for k, v in (root or {}).items()) + node('images')
    for name, properties in images.items():
        tree += node(name) + b''.join(prop(k, v) for k, v in properties.items()) + struct.pack('>I', 2)
    tree += struct.pack('>3I', 2, 2, 9)
    return (struct.pack('>10I', 0xd00dfeed, 40 + len(tree) + len(strings), 40,
                        40 + len(tree), 0, 17, 16, 0, len(strings), len(tree)) + tree + strings)


class NORContentsTests(unittest.TestCase):
    def test_actual_bytes_supply_hash_and_embedded_component_banner(self):
        data = b'\0U-Boot 2022.10-g92aae9846c\0' + b'\xff' * 23
        result = partition_content(data, 'fsbl')
        self.assertEqual(result['version'], 'U-Boot 2022.10-g92aae9846c')
        self.assertEqual(result['versions'][0]['offset'], 1)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertIsNone(partition_content(data, 'opensbi')['version'])

    def test_fit_timestamp_and_description_never_become_a_firmware_version(self):
        result = partition_content(fit({'description': b'OpenSBI fw_dynamic Firmware\0',
                                        'timestamp': struct.pack('>I', 1794590000)}), 'opensbi')
        self.assertEqual(result['format'], 'fit')
        self.assertEqual(result['description'], 'OpenSBI fw_dynamic Firmware')
        self.assertEqual(result['version_state'], 'unknown')
        self.assertIsNone(result['version'])
        self.assertTrue(result['built_at'].startswith('2026-'))

    def test_explicit_fit_text_and_numeric_version_keep_evidence(self):
        text = partition_content(fit({'firmware-version': b'1.6.1\0'}), 'opensbi')
        self.assertEqual(text['version'], '1.6.1')
        self.assertEqual(text['versions'][0]['field'], '/:firmware-version')
        numeric = partition_content(fit({'version': struct.pack('>I', 0x01020304)}), 'uboot')
        self.assertEqual(numeric['version'], '0x01020304')

    def test_environment_is_never_scanned_or_exposed_and_configuration_has_no_firmware_version(self):
        data = b'root_password=private\0version=9.8.7\0U-Boot 2025.01\0'
        for partition in ('env', 'bootinfo'):
            result = partition_content(data, partition)
            self.assertEqual(result['version_state'], 'not-applicable')
            self.assertIsNone(result['version'])
            self.assertNotIn('private', str(result))
            self.assertNotIn('2025.01', str(result))

    def test_malformed_fit_cannot_fall_back_to_an_embedded_banner(self):
        for data in (b'\xd0\x0d\xfe\xed', fit({'version': b'v1\0'})[:-1],
                     b'\xd0\x0d\xfe\xed' + b'U-Boot 2025.01\0' * 100):
            result = partition_content(data, 'uboot')
            self.assertEqual(result['version_state'], 'invalid-format')
            self.assertIsNone(result['version'])

    def test_fit_properties_are_bounded_and_control_characters_are_not_displayed(self):
        data = fit({'version': b'secret\ncontrol\0'})
        self.assertIsNone(partition_content(data, 'opensbi')['version'])
        data = bytearray(fit({'version': b'v1\0'}))
        data[52:56] = struct.pack('>I', 0xffffffff)
        self.assertEqual(partition_content(bytes(data), 'opensbi')['version_state'], 'invalid-format')
        with self.assertRaises(ValueError):
            partition_content(b'\0' * (16 * 1024 ** 2 + 1))

    def test_empty_and_conflicting_versions_remain_explicit(self):
        self.assertEqual(partition_content(b'\xff' * 1024, 'opensbi')['version_state'], 'empty')
        result = partition_content(b'U-Boot 2022.10\0U-Boot 2025.01\0', 'uboot')
        self.assertEqual(result['version_state'], 'multiple')
        self.assertEqual(len(result['versions']), 2)

    def test_spl_banner_hides_the_plain_u_boot_string_in_the_same_fsbl(self):
        data = b'\0U-Boot 2022.10-gc24d94c7ca\0U-Boot SPL 2022.10-gc24d94c7ca (Oct 29 2026)\0'
        result = partition_content(data, 'fsbl')
        self.assertEqual(result['version_state'], 'identified')
        self.assertEqual(result['version'], 'U-Boot SPL 2022.10-gc24d94c7ca')

    def test_lzo_fit_payload_is_expanded_by_declared_compression_only(self):
        image = {'description': b'U-Boot\0', 'compression': b'lzo\0', 'data': LZOP_UBOOT}
        result = partition_content(fit_images({'uboot': image}), 'uboot')
        self.assertEqual(result['version'], 'U-Boot 2022.10-gc24d94c7ca')
        self.assertEqual(result['versions'][0]['image'], '/images/uboot')
        unsupported = dict(image, compression=b'lz4\0')
        self.assertEqual(partition_content(fit_images({'uboot': unsupported}), 'uboot')['version_state'], 'unknown')
        corrupt = dict(image, data=LZOP_UBOOT[:60] + b'\0' * 4 + LZOP_UBOOT[64:])
        self.assertEqual(partition_content(fit_images({'uboot': corrupt}), 'uboot')['version_state'], 'unknown')
        self.assertEqual(nor_contents._lzop(LZOP_UBOOT, nor_contents._Budget())[256:282], b'U-Boot 2022.10-gc24d94c7ca')

    def test_expansion_budget_stops_oversized_payloads(self):
        budget = nor_contents._Budget()
        budget.left = 100
        with self.assertRaises(ValueError):
            nor_contents._lzop(LZOP_UBOOT, budget)

    def test_opensbi_platform_version_and_build_banner_argument(self):
        commit = b'513a1e8d633be0c2d301037e0a370f2f7c7641f7'
        platform = struct.pack('<2I', 0x10006, 1) + b'Generic'.ljust(64, b'\0')
        body = b'\x13' * 32 + b'\0' + commit + b'\0' * 8 + b'\nOpenSBI %s\n\0' + b'\0' * 7 + platform
        result = partition_content(fit_images({'opensbi': {'compression': b'none\0', 'data': body}}), 'opensbi')
        self.assertEqual(result['version'], 'OpenSBI v1.6 513a1e8d633b')
        self.assertEqual(result['versions'][0]['build'], commit.decode())
        self.assertEqual(result['version_state'], 'identified')
        self.assertEqual(partition_content(platform, 'opensbi')['version'], 'OpenSBI v1.6')

    def test_esos_version_id_records_report_each_core_with_release(self):
        def core(name, release):
            return name.ljust(64, b'\0') + struct.pack('<I', release) + b'\0' * (0x100 - 68)
        images = {'rcpu0-fw': {'compression': b'none\0', 'data': b'\x13' * 0x200 + core(b'os0_rcpu:241e5e1953f7', 5)},
                  'rcpu1-fw': {'compression': b'none\0', 'data': b'\x13' * 0x100 + core(b'os1_rcpu:241e5e1953f7', 5)}}
        result = partition_content(fit_images(images), 'esos')
        self.assertEqual(result['version'], 'os0_rcpu:241e5e1953f7;version=5 / os1_rcpu:241e5e1953f7;version=5')
        legacy = partition_content(fit_images({'rcpu0-fw': {'data': core(b'k3_core0:4c4f04f20ed4', 0)}}), 'esos')
        self.assertEqual(legacy['version'], 'k3_core0:4c4f04f20ed4')
        unaligned = partition_content(fit_images({'rcpu0-fw': {'data': b'\x13' + core(b'os0_rcpu:241e5e1953f7', 5)}}), 'esos')
        self.assertEqual(unaligned['version_state'], 'unknown')

    def test_edk2_firmware_version_pcd_inside_lzma_section(self):
        pcd = 'SPACEMIT\0' + '0.0.36\0' + 'Timeout\0'
        packed = lzma.compress(b'\0' * 64 + pcd.encode('utf-16le'), format=lzma.FORMAT_ALONE)
        guid = uuid.UUID('ee4e5898-3914-4259-9d6e-dc7bd79403cf').bytes_le
        section = (24 + len(packed)).to_bytes(3, 'little') + b'\x02' + guid + struct.pack('<2H', 24, 1) + packed
        fd = b'\0' * 64 + section + b'\xff' * 64
        result = partition_content(fit_images({'uefi': {'compression': b'none\0', 'data': fd}}), 'uboot')
        self.assertEqual(result['version'], 'EDK2 0.0.36')
        self.assertEqual(partition_content(fd, 'uboot')['version'], 'EDK2 0.0.36')
        self.assertEqual(partition_content(fd.replace(guid, b'\0' * 16), 'uboot')['version_state'], 'unknown')
