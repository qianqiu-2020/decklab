"""Boot comparison uses saved logs only, never a live serial connection."""
import unittest

from seriald_core.boot_compare import MAX_BOOT_BYTES, compare_boots


class BootComparisonTests(unittest.TestCase):
    def test_timestamps_are_ignored_but_target_errors_are_highlighted(self):
        baseline = (b'2026-09-20 08:00:00  [    0.123] Linux started\n'
                    b'2026-09-20 08:00:01  [    1.123] mmc ready\n')
        target = (b'2026-09-21 09:00:00  [    0.456] Linux started\n'
                  b'2026-09-21 09:00:01  [    2.456] mmc timeout\n')
        result = compare_boots(baseline, target)
        self.assertEqual(result['counts']['changed'], 1)
        self.assertEqual(len(result['clues']), 1)
        self.assertIn('mmc timeout', result['clues'][0]['text'])
        self.assertEqual(result['rows'][0]['type'], 'equal')

    def test_identical_boots_have_no_diff(self):
        result = compare_boots(b'U-Boot\nready\n', b'U-Boot\nready\n')
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['counts'], {'added': 0, 'removed': 0, 'changed': 0})

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            compare_boots(b'x' * (MAX_BOOT_BYTES + 1), b'')


if __name__ == '__main__':
    unittest.main()
