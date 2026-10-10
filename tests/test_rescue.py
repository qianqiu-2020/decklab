"""只测拒绝路径,不把任何有效的 USB 设备名送到 sysfs 操作。"""
import pathlib
import subprocess
import unittest


HELPER = pathlib.Path(__file__).resolve().parents[1] / 'rescue.sh'


class RescueValidationTests(unittest.TestCase):
    def assert_rejected(self, args, message):
        result = subprocess.run(['bash', str(HELPER), *args],
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_invalid_device_names_are_rejected(self):
        for name in ('', '3-4/../../../../tmp/x', '3-4.', '3-4..2',
                     '3-4:1.0', '3-4\n', '3-a', '-3-4', '/3-4',
                     '3-4;echo pwn', '3-4$(echo pwn)'):
            with self.subTest(name=name):
                self.assert_rejected([name, 'rebind'], '设备名不合法')

    def test_argument_count_is_checked_before_sysfs_access(self):
        for args in ([], ['3-4', 'soft', 'extra']):
            with self.subTest(args=args):
                self.assert_rejected(args, '设备名不合法')

    def test_unknown_mode_is_rejected_before_sysfs_access(self):
        for mode in ('soft', 'invalid'):
            self.assert_rejected(['3-4', mode], '模式只能是 rebind')


if __name__ == '__main__':
    unittest.main()
