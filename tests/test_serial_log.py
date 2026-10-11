"""Archive writer can run without a Device, Manager, registry or transport."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core.serial_log import SerialLog


class SerialLogTests(unittest.TestCase):
    def test_status_does_not_split_decoder_and_close_flushes_partial_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SerialLog(lambda: directory)
            self.addCleanup(log.close)
            raw = '中文 partial'.encode()
            self.assertEqual(log.consume(raw[:1]), '')
            self.assertIsNone(log.consume(b'local status', received=False))
            self.assertEqual(log.consume(raw[1:]), '中文 partial')
            log.close()
            text = Path(log.logpath).read_text()
            self.assertIn('local status', text)
            self.assertIn('中文 partial', text)
            self.assertNotIn('\ufffd', text)

    def test_write_failure_preserves_decoded_output_and_next_write_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            log = SerialLog(lambda: directory)
            self.addCleanup(log.close)
            with mock.patch.object(log, 'opener', side_effect=OSError('disk full')):
                self.assertEqual(log.consume(b'first\n'), 'first\n')
            self.assertEqual(log.log_error, 'disk full')
            self.assertIsNone(log.logf)
            self.assertEqual(log.consume(b'second\n'), 'second\n')
            self.assertEqual(log.log_error, '')
            self.assertIn('second', Path(log.logpath).read_text())
