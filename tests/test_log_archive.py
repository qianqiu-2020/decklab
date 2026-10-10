"""Boot indexing keeps daily files intact while exposing logical boot epochs."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core.log_archive import boot_epochs, read_epoch, search_epochs


class LogArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def files(self):
        return [{'name': path.name, 'kind': 'serial',
                 'date': path.stem[-8:], 'mtime': path.stat().st_mtime}
                for path in sorted(self.root.glob('serial-*.log'))]

    def test_search_reads_full_history_and_cross_day_parts_with_pagination(self):
        (self.root / 'serial-20260920.log').write_bytes(
            b'2026-09-20 10:00:00  U-Boot 2024.10\n'
            + b'x' * 70000 + b'\n'
            + b'2026-09-20 23:59:00  PA\x1b[31mNIC old\x1b[0m\n')
        (self.root / 'serial-20260921.log').write_bytes(
            b'2026-09-21 00:01:00  panic across midnight\n'
            b'2026-09-21 10:00:00  U-Boot 2024.10\n'
            b'2026-09-21 10:00:02  panic new\n')
        epochs = boot_epochs(str(self.root), self.files())
        page = search_epochs(str(self.root), epochs, 'panic', limit=2)
        self.assertEqual([row['number'] for row in page['results']], [2, 1])
        self.assertTrue(page['more'])
        self.assertNotIn('\x1b', page['results'][1]['excerpt'])
        rest = search_epochs(str(self.root), epochs, 'panic', offset=page['next_offset'], limit=2)
        self.assertEqual(len(rest['results']), 1)
        self.assertEqual(rest['results'][0]['date'], '2026-09-21')
        self.assertEqual(rest['results'][0]['id'], page['results'][1]['id'])
        self.assertFalse(rest['more'])

    def test_search_is_literal_and_unicode_and_stays_in_boot_ranges(self):
        (self.root / 'serial-20260920.log').write_text(
            '启动失败 before collection marker\n'
            '2026-09-20 10:00:00  U-Boot 2024.10\n'
            '2026-09-20 10:00:01  启动失败 foo.bar\n')
        epochs = boot_epochs(str(self.root), self.files())
        self.assertEqual(len(search_epochs(str(self.root), epochs, '启动失败')['results']), 1)
        self.assertEqual(len(search_epochs(str(self.root), epochs, 'foo.bar')['results']), 1)
        self.assertFalse(search_epochs(str(self.root), epochs, 'foo.*')['results'])

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
        self.assertEqual(newest['dates'], ['2026-09-20', '2026-09-21'])
        self.assertEqual(oldest['dates'], ['2026-09-20'])
        self.assertFalse(oldest['cross_day'])
        self.assertIn(b'second boot continues', read_epoch(str(self.root), newest))
        self.assertNotIn(b'first boot', read_epoch(str(self.root), newest))
        self.assertIn(b'first boot', read_epoch(str(self.root), oldest))

    def test_full_uboot_banner_does_not_double_count_spl(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  U-Boot SPL 2022.10\n'
                         b'2026-09-21 08:00:01  U-Boot 2022.10\n')
        self.assertEqual(len(boot_epochs(str(self.root), self.files())), 1)

    def test_next_day_reboot_does_not_make_previous_boot_cross_day(self):
        (self.root / 'serial-20260920.log').write_bytes(
            b'2026-09-20 23:00:00  U-Boot 2024.10\n'
            b'2026-09-20 23:00:01  running\n')
        (self.root / 'serial-20260921.log').write_bytes(
            b'2026-09-21 08:00:00  U-Boot 2024.10\n')
        newest, oldest = boot_epochs(str(self.root), self.files())
        self.assertEqual(oldest['dates'], ['2026-09-20'])
        self.assertFalse(oldest['cross_day'])
        self.assertEqual(newest['dates'], ['2026-09-21'])

    def test_linux_only_boot_is_counted_without_spl_or_uboot(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  Linux version 6.18.0 board\n'
                         b'2026-09-21 09:00:00  Linux version 6.18.0 board\n')
        self.assertEqual([x['marker'] for x in boot_epochs(str(self.root), self.files())],
                         ['Linux', 'Linux'])

    def test_unchanged_archives_are_not_read_again(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  U-Boot 2024.10\n')
        cache = {}
        self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 1)
        with mock.patch('builtins.open', side_effect=AssertionError('archive rescanned')):
            self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 1)
        with path.open('ab') as output:
            output.write(b'2026-09-21 09:00:00  Linux version 6.18.0\n')
        self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 2)

    def test_growing_archive_reads_only_new_complete_lines(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  U-Boot 2024.10\n' + b'x' * 200000 + b'\n')
        cache = {}
        self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 1)
        previous_end = cache[path.name][3]
        with path.open('ab') as output:
            output.write(b'2026-09-21 09:00:00  Linux version 6.18.0\n')
        seeks = []
        real_open = open
        class TrackedFile:
            def __init__(self, filename):
                self.source = real_open(filename, 'rb')
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.source.close()
            def seek(self, offset):
                seeks.append(offset)
                return self.source.seek(offset)
            def tell(self):
                return self.source.tell()
            def readline(self):
                return self.source.readline()
        with mock.patch('seriald_core.log_archive.open', side_effect=lambda filename, mode: TrackedFile(filename), create=True):
            self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 2)
        self.assertEqual(seeks, [previous_end])
        self.assertGreater(cache[path.name][3], 200000)

    def test_incomplete_boot_banner_is_indexed_after_newline(self):
        path = self.root / 'serial-20260921.log'
        path.write_bytes(b'2026-09-21 08:00:00  U-Boot 2024.')
        cache = {}
        self.assertEqual(boot_epochs(str(self.root), self.files(), cache=cache), [])
        with path.open('ab') as output:
            output.write(b'10\n')
        self.assertEqual(len(boot_epochs(str(self.root), self.files(), cache=cache)), 1)


if __name__ == '__main__':
    unittest.main()
