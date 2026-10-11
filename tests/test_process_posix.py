"""Hardware helper cleanup uses local processes/files only, never board ports."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from seriald_core.hardware import PosixProcess


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process-group verification')
class PosixProcessTests(unittest.TestCase):
    def test_root_exit_still_cleans_child_before_close_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            child_pid = Path(directory) / 'child.pid'
            code = ('import subprocess,sys\nfrom pathlib import Path\n'
                    'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                    'Path(%r).write_text(str(child.pid))\n' % str(child_pid))
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            process = PosixProcess([sys.executable, '-c', code])
            closed = False
            try:
                self.assertEqual(process.process.wait(timeout=3), 0)
                stat = Path('/proc') / child_pid.read_text() / 'stat'
                self.assertNotEqual(stat.read_text().rpartition(')')[2].split()[0], 'Z')
                process.close()
                closed = True
                if stat.exists():
                    self.assertIn(stat.read_text().rpartition(')')[2].split()[0], ('Z', 'X'))
                self.assertIsNone(unrelated.poll(), 'cleanup must not stop other groups')
            finally:
                if not closed:
                    process.close()
                unrelated.kill()
                unrelated.wait(timeout=3)

    def process_fixture(self):
        process = PosixProcess.__new__(PosixProcess)
        process.process = mock.Mock(pid=123456)
        return process

    def test_group_stat_handles_command_spaces_parentheses_and_non_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            stat = Path(directory) / '42' / 'stat'
            stat.parent.mkdir()
            process = self.process_fixture()
            scandir = os.scandir
            for state, expected in (('S', True), ('D', True), ('Z', False), ('X', False)):
                stat.write_bytes(b'42 (helper \xff (worker) name) ' + state.encode()
                                 + b' 1 123456 123456 0')
                with mock.patch('seriald_core.hardware.os.scandir',
                                side_effect=lambda path: scandir(directory if path == '/proc' else path)), \
                        mock.patch('seriald_core.hardware.os.killpg'):
                    self.assertEqual(process._group_running(), expected)

    def test_zombie_leader_with_live_thread_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            leader = Path(directory) / '42'
            thread = leader / 'task' / '43'
            thread.mkdir(parents=True)
            (leader / 'stat').write_text('42 (leader) Z 1 123456 123456 0')
            (thread / 'stat').write_text('43 (worker) D 1 123456 123456 0')
            scandir = os.scandir
            with mock.patch('seriald_core.hardware.os.scandir',
                            side_effect=lambda path: scandir(directory if path == '/proc' else path)), \
                    mock.patch('seriald_core.hardware.os.killpg'):
                self.assertTrue(self.process_fixture()._group_running())

    def test_invisible_existing_group_is_not_treated_as_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = os.scandir(directory)
            with entries, mock.patch('seriald_core.hardware.os.scandir', return_value=entries), \
                    mock.patch('seriald_core.hardware.os.killpg'):
                self.assertTrue(self.process_fixture()._group_running())

    def test_cleanup_waits_for_remaining_members_after_root_wait(self):
        process = self.process_fixture()
        with mock.patch('seriald_core.hardware.os.killpg'), \
                mock.patch.object(process, '_group_running', side_effect=[True, True, False]) as members, \
                mock.patch('seriald_core.hardware.time.sleep') as sleep:
            process.close()
        process.process.wait.assert_called_once_with(timeout=3)
        self.assertEqual(members.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_unstopped_group_has_bounded_cleanup_failure(self):
        process = self.process_fixture()
        with mock.patch('seriald_core.hardware.os.killpg'), \
                mock.patch.object(process, '_group_running', return_value=True), \
                mock.patch('seriald_core.hardware.time.monotonic', side_effect=[0, 4]):
            with self.assertRaisesRegex(OSError, 'process group did not stop'):
                process.close()

    def test_root_wait_timeout_is_reported_as_cleanup_failure(self):
        process = self.process_fixture()
        process.process.wait.side_effect = subprocess.TimeoutExpired('helper', 3)
        with mock.patch('seriald_core.hardware.os.killpg'):
            with self.assertRaisesRegex(OSError, 'root process did not stop'):
                process.close()

    def test_unreadable_process_metadata_does_not_prove_cleanup(self):
        process = self.process_fixture()
        with mock.patch('seriald_core.hardware.os.killpg'), \
                mock.patch('seriald_core.hardware.os.scandir', side_effect=PermissionError('proc unavailable')):
            with self.assertRaises(PermissionError):
                process._group_running()


if __name__ == '__main__':
    unittest.main()
