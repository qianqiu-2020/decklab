"""Software fallback identities; shell commands run only against temporary files."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from seriald_core.boot_marker import init_command, marker_serial, read_command
from seriald_core.identity import select_identity
from seriald_core.protocol import PROBE_CMD, parse_probe
from seriald_core.storage import IdentityError

MARKER = 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6'


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'seriald-id'
        self.uuid = self.root / 'uuid'
        self.uuid.write_text(MARKER.removeprefix('boot-') + '\n')

    def initialize(self):
        return subprocess.run(['/bin/sh', '-c', init_command(self.path, self.uuid)],
                              text=True, capture_output=True, timeout=3)

    def test_creation_persists_and_repeat_does_not_replace_or_rewrite(self):
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.path.read_text(), MARKER + '\n')
        stat = self.path.stat()
        self.uuid.write_text('not a uuid\n')
        self.assertEqual(self.initialize().returncode, 0)
        self.assertEqual(self.path.read_text(), MARKER + '\n')
        self.assertEqual(self.path.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_existing_invalid_or_partial_file_is_preserved(self):
        for content in ('', 'copied-machine-id\n', MARKER + '\nextra\n', MARKER[:20]):
            with self.subTest(content=content):
                self.path.write_text(content)
                result = self.initialize()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('invalid-existing-marker', result.stdout)
                self.assertEqual(self.path.read_text(), content)

    def test_symlink_and_fifo_are_rejected_without_following_or_blocking(self):
        self.path.symlink_to(self.uuid)
        self.assertNotEqual(self.initialize().returncode, 0)
        self.assertEqual(self.uuid.read_text(), MARKER[5:] + '\n')
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertNotEqual(self.initialize().returncode, 0)
        result = subprocess.run(['/bin/sh', '-c', read_command(self.path)],
                                capture_output=True, timeout=3)
        self.assertEqual(result.stdout, b'')

    def test_missing_boot_directory_and_random_source_fail_without_marker(self):
        self.path = self.root / 'missing' / 'seriald-id'
        self.assertNotEqual(self.initialize().returncode, 0)
        self.assertFalse(self.path.exists())
        self.path = self.root / 'seriald-id'
        self.uuid.unlink()
        self.assertNotEqual(self.initialize().returncode, 0)
        self.assertFalse(self.path.exists())

    def test_reading_missing_file_does_not_create_it_and_reads_are_bounded(self):
        command = read_command(self.path)
        result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, timeout=3)
        self.assertEqual(result.stdout, b'')
        self.assertFalse(self.path.exists())
        self.path.write_text(MARKER + '\n')
        result = subprocess.run(['/bin/sh', '-c', command], text=True, capture_output=True, timeout=3)
        self.assertEqual(marker_serial({'boot_marker': result.stdout}), MARKER)
        self.path.write_text(MARKER + 'x' * 4096)
        result = subprocess.run(['/bin/sh', '-c', command], text=True, capture_output=True, timeout=3)
        self.assertEqual(len(result.stdout), 80)
        self.assertEqual(marker_serial({'boot_marker': result.stdout}), '')

    def test_framed_initialization_fits_canonical_input_and_has_valid_posix_syntax(self):
        command = init_command() + ' && { ' + PROBE_CMD + '; }'
        self.assertLess(len(command.encode()) + 200, 3500)
        # Parse only: never execute the real /boot command or hardware probe on this host.
        result = subprocess.run(['/bin/sh', '-n'], input=command, text=True,
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)


class MarkerIdentityTests(unittest.TestCase):
    def test_marker_is_last_resort_and_remembers_original_firmware_value(self):
        fields = {'serial': '0123456789ABCDEF', 'boot_marker': MARKER}
        result = select_identity(fields)
        self.assertEqual(result['serial'], MARKER)
        self.assertEqual(result['reported_serial'], fields['serial'])
        self.assertEqual(result['serial_source'], 'boot-marker')
        self.assertEqual(select_identity(dict(fields, serial='board-unique'))['serial'], 'board-unique')
        self.assertEqual(select_identity(dict(fields, soc_serial='chip-unique'))['serial'], 'chip-unique')

    def test_established_marker_survives_new_hardware_interface(self):
        result = select_identity({'serial': 'new-firmware', 'soc_serial': 'new-chip',
                                  'boot_marker': MARKER}, known_marker=True)
        self.assertEqual(result['serial'], MARKER)

    def test_conflicted_hardware_id_can_use_marker(self):
        result = select_identity({'serial': '', 'soc_serial': 'duplicate', 'boot_marker': MARKER},
                                 known_soc=True, soc_conflicted=True)
        self.assertEqual(result['serial'], MARKER)

    def test_invalid_marker_does_not_bind(self):
        for value in ('', '../other', 'boot-' + '0' * 36, MARKER + '\nother', 'x' * 4096):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                select_identity({'serial': '', 'boot_marker': value})

    def test_protocol_accepts_optional_marker_and_rejects_duplicate_fields(self):
        base = 'HOST=board\nSERIAL=\nMODEL=board\nSSHD=inactive\n'
        self.assertEqual(parse_probe(base)['boot_marker'], '')
        field = 'BOOT_MARKER=' + MARKER + '\n'
        self.assertEqual(select_identity(parse_probe(base + field))['serial'], MARKER)
        with self.assertRaisesRegex(ValueError, 'BOOT_MARKER'):
            parse_probe(base + field * 2)
