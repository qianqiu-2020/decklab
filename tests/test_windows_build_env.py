"""Packaging environment isolation, without freezing or accessing hardware."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.prepare_windows_build import prepare


class BuildEnvironmentTests(unittest.TestCase):
    def test_rebuild_removes_extra_packages_and_preserves_development(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            development = root / '.venv' / 'keep.txt'
            development.parent.mkdir()
            development.write_text('development environment', encoding='utf-8')
            environment = prepare(root)
            python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            site = Path(subprocess.check_output(
                [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
                text=True).strip())
            unwanted = site / 'decklab_unwanted_build_fixture.py'
            unwanted.write_text('VALUE = 42', encoding='utf-8')
            subprocess.run([str(python), '-I', '-c', 'import decklab_unwanted_build_fixture'], check=True)
            prepare(root)
            result = json.loads(subprocess.check_output([
                str(python), '-I', '-c',
                'import importlib.util,json,sys; print(json.dumps({'
                '"extra": importlib.util.find_spec("decklab_unwanted_build_fixture") is not None,'
                '"isolated": sys.prefix != sys.base_prefix}))'], text=True))
            self.assertEqual(result, {'extra': False, 'isolated': True})
            self.assertIn('include-system-site-packages = false',
                          (environment / 'pyvenv.cfg').read_text())
            self.assertEqual(development.read_text(), 'development environment')

    def test_rejects_redirected_build_directory_before_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'other'
            outside.mkdir()
            try:
                (root / 'build').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Creating directory symlinks is unavailable')
            with mock.patch('tools.prepare_windows_build.venv.EnvBuilder') as builder:
                with self.assertRaises(ValueError):
                    prepare(root)
                builder.assert_not_called()

    def test_rejects_redirected_environment_before_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            outside = root / 'other'
            outside.mkdir()
            try:
                (root / 'build' / 'windows-venv').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Creating directory symlinks is unavailable')
            with mock.patch('tools.prepare_windows_build.venv.EnvBuilder') as builder:
                with self.assertRaises(ValueError):
                    prepare(root)
                builder.assert_not_called()


if __name__ == '__main__':
    unittest.main()
