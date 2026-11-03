"""Identity selection and explicit archive correction, with no board access."""
from contextlib import redirect_stdout
import io
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from seriald_core.admin import confirm_serial, correct_serial, main, resolve_conflict
from seriald_core.identity import select_identity
from seriald_core.protocol import parse_onie_serial, parse_probe
from seriald_core.storage import IdentityError, Store


def info(serial='0123456789ABCDEF', soc='0059D968E583C49A'):
    return {'serial': serial, 'soc_serial': soc, 'soc_family': 'spacemit socs', 'soc_id': 'K1-6370'}


class IdentityTests(unittest.TestCase):
    @staticmethod
    def onie(serial=None):
        import binascii
        table = bytearray(b'TlvInfo\0\x01\0\0')
        if serial is not None:
            encoded = serial.encode()
            table += bytes((0x23, len(encoded))) + encoded
        table += b'\x21\x05board\xfe\x04'
        table[9:11] = (len(table) - 11 + 4).to_bytes(2, 'big')
        table += binascii.crc32(table).to_bytes(4, 'big')
        return bytes(table).hex()

    def test_upstream_eeprom_and_vendor_device_tree_choose_same_identity(self):
        serial = 'MVJ-K1-20260919-000001'
        upstream = select_identity({'serial': '0123456789ABCDEF', 'eeprom_serial': serial})
        vendor = select_identity({'serial': serial})
        self.assertEqual(upstream['serial'], vendor['serial'])
        self.assertEqual(upstream['serial_source'], 'eeprom-onie')
        self.assertEqual(vendor['serial_source'], 'device-tree')
        self.assertEqual(vendor['identity_status'], 'confirmed')

    def test_valid_raw_onie_extracts_serial_and_bad_crc_does_not(self):
        serial = 'MVJ-K1-000001'
        raw = self.onie(serial)
        self.assertEqual(parse_onie_serial(raw), (serial, True))
        damaged = raw[:-2] + ('00' if raw[-2:] != '00' else '01')
        self.assertEqual(parse_onie_serial(damaged), ('', False))
        self.assertEqual(parse_onie_serial(self.onie()), ('', True))

    def test_probe_uses_raw_onie_fallback_and_redacts_dump(self):
        raw = self.onie('MVJ-K1-000002')
        body = ('HOST=board\nSERIAL=0123456789ABCDEF\nEEPROM_SERIAL=\nEEPROM_HEX=%s\n'
                'MODEL=Jupiter\nUBOOT_VERSION=U-Boot 2024.10\nSSHD=active\n') % raw
        result = parse_probe(body)
        self.assertEqual(result['eeprom_serial'], 'MVJ-K1-000002')
        self.assertTrue(result['eeprom_present'])
        self.assertEqual(result['uboot_version'], 'U-Boot 2024.10')
        self.assertNotIn(raw, result['raw'])

    def test_eeprom_serial_precedes_a_different_soc_id(self):
        fields = info(soc='0059D968E583C49A')
        fields['eeprom_serial'] = 'MVJ-K1-000003'
        result = select_identity(fields)
        self.assertEqual(result['serial'], 'MVJ-K1-000003')
        self.assertEqual(result['serial_source'], 'eeprom-onie')

    def test_uboot_provenance_confirms_dt_serial_without_format_matching(self):
        fields = info('MJM10004000001', '0059D968E583CA22')
        fields['model'] = 'Milk-V Jupiter'
        fields['uboot_version'] = 'U-Boot 2024.10'
        result = select_identity(fields)
        self.assertEqual(result['serial'], 'MJM10004000001')
        self.assertEqual(result['serial_source'], 'uboot-injected')
        self.assertEqual(result['identity_status'], 'confirmed')
        arbitrary = dict(fields, serial='board-production-serial-42')
        self.assertEqual(select_identity(arbitrary)['serial_source'], 'uboot-injected')
        conflicted = select_identity(fields, conflicted=True)
        self.assertEqual(conflicted['serial_source'], 'soc-sysfs')
        self.assertEqual(conflicted['identity_status'], 'provisional')

    def test_dt_serial_without_uboot_provenance_is_confirmed(self):
        fields = info('MJM10004000001', '0059D968E583CA22')
        fields['model'] = 'Milk-V Jupiter'
        result = select_identity(fields)
        self.assertEqual(result['serial_source'], 'device-tree')
        self.assertEqual(result['identity_status'], 'confirmed')

    def test_k3_pico_dt_serial_is_the_onie_identity(self):
        result = select_identity({'serial': 'HW3MPK3321280183', 'model': 'SpacemiT K3 Pico ITX'})
        self.assertEqual(result['serial'], 'HW3MPK3321280183')
        self.assertEqual(result['serial_source'], 'device-tree')
        self.assertEqual(result['identity_status'], 'confirmed')

    def test_valid_board_serial_is_preserved(self):
        result = select_identity(info('5b9d4a0a6f5e25a8'))
        self.assertEqual(result['serial'], '5b9d4a0a6f5e25a8')
        self.assertEqual(result['serial_source'], 'device-tree')

    def test_placeholder_or_missing_board_serial_uses_hardware_id(self):
        for placeholder in ('0123456789ABCDEF', '0123456789abcdef', '', 'unknown', '0' * 16, 'F' * 16):
            with self.subTest(placeholder=placeholder):
                result = select_identity(info(placeholder, '0059d968e583c49a'))
                self.assertEqual(result['serial'], '0059D968E583C49A')
                self.assertEqual(result['reported_serial'], placeholder)
                self.assertEqual(result['serial_source'], 'soc-sysfs')

    def test_previously_conflicting_serial_requires_an_independent_hardware_id(self):
        result = select_identity(info('factory-duplicate'), conflicted=True)
        self.assertEqual(result['serial'], '0059D968E583C49A')
        with self.assertRaises(IdentityError):
            select_identity(info('0059D968E583C49A'), conflicted=True)

    def test_established_hardware_identity_survives_firmware_serial_changes(self):
        result = select_identity(info('new-firmware-serial'), known_soc=True)
        self.assertEqual(result['serial'], 'new-firmware-serial')
        self.assertEqual(result['reported_serial'], 'new-firmware-serial')
        self.assertEqual(select_identity(info('0059D968E583C49A'), known_soc=True)['serial'], '0059D968E583C49A')

    def test_invalid_efuse_values_never_become_directory_keys(self):
        for value in ('', '0' * 16, 'F' * 16, '007FFFFFFFFFFFFF', '0080000000000000',
                      '0123456789ABCDEF', '../oops', 'invalid', '0059D968E583C49A\nother'):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                select_identity(info(soc=value))

    def test_generic_soc_serials_do_not_use_spacemit_specific_bit_limits(self):
        result = select_identity({'serial': '', 'soc_serial': 'chip-unique-123'})
        self.assertEqual(result['serial'], 'chip-unique-123')

    def test_probe_preserves_hardware_fields_and_rejects_ambiguous_values(self):
        raw = ('HOST=board\nSERIAL=0123456789ABCDEF\nMODEL=Milk-V Jupiter\nSSHD=active\n'
               'SOC_SERIAL=0059D968E583C49A\nSOC_FAMILY=spacemit socs\nSOC_ID=K1-6370\n')
        self.assertEqual(select_identity(parse_probe(raw))['serial'], '0059D968E583C49A')
        for key in ('SOC_SERIAL', 'SOC_FAMILY', 'SOC_ID'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_probe(raw + key + '=duplicate\n')


class IdentityCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = Store(str(self.base / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.old, self.new = '0123456789ABCDEF', '0059D968E583C49A'
        self.store.bind({'serial': self.old}, 'usb-old', 'tty-old', 'milkv2')
        self.store.mark_conflict(self.old)
        self.store.open_session('ssh123', self.old, 'agent', 'uname -a', '192.0.2.1')
        self.store.close_session('ssh123')
        self.source = self.base / 'logs' / '_provisional' / 'unknown' / self.old / 'serial-test.log'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('verified board output\n')

    def test_explicit_correction_preserves_label_logs_sessions_and_conflict_record(self):
        plan = correct_serial(self.base, self.old, self.new)
        self.assertFalse(plan['applied'])
        self.assertIsNone(self.store.get(self.new))
        self.assertFalse((self.base / 'logs' / self.new).exists())
        correct_serial(self.base, self.old, self.new, True)
        board = self.store.get(self.new)
        self.assertEqual(board['label'], 'milkv2')
        self.assertEqual(board['info']['reported_serial'], self.old)
        self.assertIsNone(self.store.get(self.old))
        self.assertTrue(self.store.has_conflict(self.old))
        self.assertEqual(self.store.sessions(self.new)[0]['sid'], 'ssh123')
        self.assertEqual(self.store.events(self.new)[0]['action'], 'identity-corrected')
        self.assertEqual((self.base / 'logs' / self.new / self.source.name).read_text(),
                         'verified board output\n')
        self.assertFalse(self.source.parent.exists())
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_manual_confirmation_promotes_provisional_archive_without_changing_serial(self):
        serial = 'DT-VERIFIED-001'
        self.store.bind({'serial': serial, 'serial_source': 'soc-sysfs'}, '', '', 'musepi2')
        source = self.base / 'logs' / '_provisional' / 'soc-sysfs' / serial / 'serial-test.log'
        source.parent.mkdir(parents=True)
        source.write_text('board output\n')
        plan = confirm_serial(self.base, serial)
        self.assertFalse(plan['applied'])
        confirm_serial(self.base, serial, True)
        target = self.base / 'logs' / serial / source.name
        self.assertEqual(target.read_text(), 'board output\n')
        self.assertFalse(source.parent.exists())
        self.assertEqual(self.store.get(serial)['info']['serial_source'], 'verified-manual')
        self.assertEqual(self.store.events(serial)[0]['action'], 'identity-confirmed')

    def test_existing_destination_is_not_overwritten_or_merged(self):
        target = self.base / 'logs' / self.new / self.source.name
        target.parent.mkdir(parents=True)
        target.write_text('different device\n')
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            correct_serial(self.base, self.old, self.new, True)
        self.assertEqual(target.read_text(), 'different device\n')
        self.assertIsNotNone(self.store.get(self.old))
        self.assertIsNone(self.store.get(self.new))

    def test_destination_identity_conflict_rolls_back_registry(self):
        self.store.bind({'serial': self.new}, '', '', 'another-board')
        with self.assertRaises(IdentityError):
            self.store.correct_serial(self.old, self.new)
        self.assertEqual(self.store.get(self.old)['label'], 'milkv2')
        self.assertEqual(self.store.get(self.new)['label'], 'another-board')
        self.assertEqual(self.store.sessions(self.old)[0]['sid'], 'ssh123')


class ConflictResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / 'registry.sqlite3')
        self.addCleanup(self.store.close)
        self.serial = '7c2b63fd4b46'
        self.reason = '已核实两板编号不同，原档案属于保留编号的板子'
        self.store.bind({'serial': self.serial, 'serial_source': 'eeprom-onie'},
                        'COM5', 'COM5', 'debian')
        self.store.mark_conflict(self.serial)
        self.log = self.base / 'logs' / self.serial / 'serial-test.log'
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(b'original archive\n')

    def test_preview_is_read_only_and_apply_preserves_identity_archives_and_sessions(self):
        self.store.open_session('ssh123', self.serial, 'agent', 'uname', '192.0.2.1')
        self.store.close_session('ssh123')
        original = self.store.get(self.serial)
        sessions = self.store.sessions(self.serial)
        before = list(self.store.db.iterdump())
        with mock.patch('seriald_core.admin.Store', side_effect=AssertionError('preview wrote storage')):
            preview = resolve_conflict(self.base, self.serial, self.reason)
        self.assertFalse(preview['applied'])
        self.assertFalse(preview['resolved'])
        self.assertTrue(preview['conflicted'])
        self.assertEqual(preview['label'], 'debian')
        self.assertEqual(list(self.store.db.iterdump()), before)

        result = resolve_conflict(self.base, self.serial, self.reason, True)
        self.assertTrue(result['applied'])
        self.assertTrue(result['resolved'])
        self.assertFalse(self.store.has_conflict(self.serial))
        self.assertEqual(self.store.get(self.serial), original)
        self.assertEqual(self.store.sessions(self.serial), sessions)
        self.assertEqual(self.log.read_bytes(), b'original archive\n')
        event = self.store.events(self.serial)[0]
        self.assertEqual((event['actor'], event['action']), ('admin', 'identity-conflict-resolved'))
        self.assertEqual(json.loads(event['detail']),
                         {'reason': self.reason, 'detected_at': preview['detected_at']})
        again = resolve_conflict(self.base, self.serial, self.reason, True)
        self.assertFalse(again['resolved'])
        self.assertFalse(again['conflicted'])
        self.assertEqual(len(self.store.events(self.serial)), 1)

    def test_invalid_reason_serial_or_missing_archive_cannot_clear_conflict(self):
        for reason in ('', '  ', 'contains\nnewline', '\x00', '\x7f', '\x85', '核' * 683):
            for apply in (False, True):
                with self.subTest(reason=reason, apply=apply), self.assertRaises(ValueError):
                    resolve_conflict(self.base, self.serial, reason, apply)
        for serial in ('unknown', '0' * 16, 'unknown-board'):
            for apply in (False, True):
                with self.subTest(serial=serial, apply=apply), self.assertRaises(ValueError):
                    resolve_conflict(self.base, serial, self.reason, apply)
        self.assertTrue(self.store.has_conflict(self.serial))
        self.assertEqual(self.store.events(self.serial), [])

    def test_missing_database_is_not_created(self):
        missing_base = self.base / 'missing'
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaises(sqlite3.OperationalError):
                resolve_conflict(missing_base, self.serial, self.reason, apply)
        self.assertFalse(missing_base.exists())

    def test_failed_conflict_deletion_rolls_back_audit(self):
        with self.store.db:
            self.store.db.execute("CREATE TRIGGER prevent_resolution BEFORE DELETE ON identity_conflicts "
                                  "BEGIN SELECT RAISE(ABORT, 'resolution blocked'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'resolution blocked'):
            self.store.resolve_conflict(self.serial, self.reason)
        self.assertTrue(self.store.has_conflict(self.serial))
        self.assertEqual(self.store.events(self.serial), [])

    def test_cli_requires_reason_and_defaults_to_preview(self):
        args = ['seriald-admin', '--base', str(self.base), 'resolve-conflict',
                '--serial', self.serial]
        with mock.patch('sys.argv', args), redirect_stdout(io.StringIO()), \
                mock.patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        for apply in (False, True):
            argv = args + ['--reason', self.reason] + (['--apply'] if apply else [])
            with mock.patch('sys.argv', argv), redirect_stdout(io.StringIO()) as output:
                main()
            result = json.loads(output.getvalue())
            self.assertEqual(result['applied'], apply)
            self.assertEqual(self.store.has_conflict(self.serial), not apply)


if __name__ == '__main__':
    unittest.main()
