"""Process ownership guard, using private data directories and no devices."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from seriald_core.collector_lock import CollectorLock


class CollectorLockTests(unittest.TestCase):
    def test_second_local_or_network_collector_is_refused_until_the_owner_closes(self):
        with tempfile.TemporaryDirectory() as base:
            with CollectorLock(base):
                with self.assertRaises(RuntimeError):
                    with CollectorLock(base):
                        self.fail('two hardware owners entered')
                result = subprocess.run([sys.executable, '-c',
                    'from seriald_core.collector_lock import CollectorLock; '\
                    'guard=CollectorLock(__import__("sys").argv[1]); guard.__enter__()', base],
                    capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('another collector', result.stderr)
            with CollectorLock(base):
                pass

    def test_separate_backend_data_stores_do_not_share_a_process_lock(self):
        with tempfile.TemporaryDirectory() as base:
            with CollectorLock(Path(base) / 'a'), CollectorLock(Path(base) / 'b'):
                pass
