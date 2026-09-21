"""Boot indexing keeps daily files intact while exposing logical boot epochs."""
from pathlib import Path
import tempfile
import unittest

from seriald_core.log_archive import boot_epochs, read_epoch


class LogArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def files(self):
        return [{'name': path.name, 'kind': 'serial',
                 'date': path.stem[-8:], 'mtime': path.stat().st_mtime}
                for path in sorted(self.root.glob('serial-*.log'))]

    def test_multiple_boots_per_day_and_one_boot_across_midnight(self):
        (self.root / 'serial-20260920.log').write_bytes(
            b'2026-09-20 10:00:00  U-Boot SPL 2022.10\n'
            b'2026-09-20 10:00:03  first boot\n'
            b'2026-09-20 23:59:59  \x00sys: 0x10000200\n')
        (self.root / 'serial-20260921.log').write_bytes(
            b'2026-09-21 00:00:00  U-Boot SPL 2022.10\n'
            b'2026-09-21 00:00:04  second boot continues\n')

        epochs = boot_epochs(str(self.root), self.files())
        self.assertEqual(len(epochs), 2)
        newest, oldest = epochs
        self.assertEqual((newest['number'], newest['files'], newest['cross_day']), (2, 2, True))
        self.assertIn(b'second boot continues', read_epoch(str(self.root), newest))
        self.assertNotIn(b'first boot', read_epoch(str(self.root), newest))
        self.assertIn(b'first boot', read_epoch(str(self.root), oldest))

    def test_full_uboot_banner_does_not_double_count_spl(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  U-Boot SPL 2022.10\n'
                         b'2026-09-21 08:00:01  U-Boot 2022.10\n')
        self.assertEqual(len(boot_epochs(str(self.root), self.files())), 1)


if __name__ == '__main__':
    unittest.main()
