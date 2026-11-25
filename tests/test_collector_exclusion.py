"""Linux cooperative reader exclusion on local PTYs, never tty/USB hardware."""
import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from decklab_core import transport
from decklab_core.collector_lock import CollectorLock
from decklab_core.profiles import SerialSettings


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux') and transport.fcntl is not None,
                     'requires Linux PTY and flock')
class CollectorExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='decklab-pty-exclusion-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.alias = self.base / 'serial-by-path'
        self.alias.symlink_to(self.path)
        self.readers = []
        self.addCleanup(self.close_descriptors)

    def close_descriptors(self):
        for fd in self.readers + [self.slave, self.master]:
            os.close(fd)
        self.readers.clear()

    def reader(self, path=None):
        fd = transport.open_serial(str(path or self.path), settings=SerialSettings(115200))
        self.readers.append(fd)
        return fd

    def test_alias_conflict_closes_loser_before_termios_or_flush(self):
        self.reader()
        before = transport.termios.tcgetattr(self.slave)
        opened, real_open = [], os.open
        def opening(*args):
            fd = real_open(*args)
            opened.append(fd)
            return fd
        with mock.patch.object(transport.os, 'open', side_effect=opening), \
                mock.patch.object(transport.termios, 'tcgetattr') as read_settings, \
                mock.patch.object(transport.termios, 'tcsetattr') as configure, \
                mock.patch.object(transport.termios, 'tcflush') as flush:
            with self.assertRaises(OSError) as error:
                transport.open_serial(str(self.alias), flush=True, settings=SerialSettings(9600))
        self.assertEqual(error.exception.errno, errno.EBUSY)
        read_settings.assert_not_called()
        configure.assert_not_called()
        flush.assert_not_called()
        self.assertEqual(transport.termios.tcgetattr(self.slave), before)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as closed:
            os.fstat(opened[0])
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_different_runtime_directories_and_processes_still_share_reader_lock(self):
        first_base, second_base = self.base / 'first', self.base / 'second'
        with CollectorLock(first_base):
            self.reader()
            environment = dict(os.environ, DECKLAB_BASE=str(second_base))
            child = subprocess.run([sys.executable, '-c', '''
import errno,os,sys
from decklab_core.collector_lock import CollectorLock
from decklab_core.profiles import SerialSettings
from decklab_core.transport import open_serial
with CollectorLock(os.environ['DECKLAB_BASE']):
    try:
        fd=open_serial(sys.argv[1], settings=SerialSettings(9600))
    except OSError as error:
        if error.errno != errno.EBUSY:
            raise
    else:
        os.close(fd)
        raise AssertionError('second runtime acquired an occupied PTY reader')
''', str(self.alias)], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue((first_base / '.collector-owner.lock').exists())
        self.assertTrue((second_base / '.collector-owner.lock').exists())

    def test_closing_reader_releases_lock_for_alias_reopen(self):
        original = self.reader()
        os.close(original)
        self.readers.remove(original)
        reopened = self.reader(self.alias)
        self.assertEqual(os.fstat(reopened).st_rdev, os.fstat(self.slave).st_rdev)

    def test_independent_write_only_descriptor_works_without_reader_lock_or_termios(self):
        self.reader()
        with mock.patch.object(transport.fcntl, 'flock', side_effect=AssertionError('writer took reader lock')), \
                mock.patch.object(transport.termios, 'tcgetattr', side_effect=AssertionError('writer read framing')), \
                mock.patch.object(transport.termios, 'tcsetattr', side_effect=AssertionError('writer changed framing')), \
                mock.patch.object(transport.os, 'open', wraps=os.open) as opening:
            ok, error = transport.write_serial(str(self.alias), 'cooperative writer\r')
        self.assertTrue(ok, error)
        self.assertEqual(opening.call_args.args[1] & os.O_ACCMODE, os.O_WRONLY)
        self.assertEqual(select.select([self.master], [], [], 1)[0], [self.master])
        self.assertEqual(os.read(self.master, 4096), b'cooperative writer\r')


if __name__ == '__main__':
    unittest.main()
