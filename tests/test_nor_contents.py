import hashlib
import struct
import unittest

from seriald_core.nor_contents import partition_content


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
