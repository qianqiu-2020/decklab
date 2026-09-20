"""Clean collector handoff is short-lived, same-boot and one-shot."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core import handoff


class HandoffTests(unittest.TestCase):
    def test_handoff_is_same_boot_recent_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(handoff, '_boot_id', return_value='host-boot'):
            path = str(Path(temporary) / 'handoff.json')
            connection = {'by_path': 'usb-1', 'dev': '/dev/ttyUSB0',
                          'serial': 'SN001', 'identity': [1, 2, 3]}
            handoff.write(path, [connection])
            self.assertEqual(handoff.consume(path), [connection])
            self.assertEqual(handoff.consume(path), [])

    def test_stale_or_other_boot_handoff_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'handoff.json'
            data = {'version': 1, 'created': 0, 'boot_id': 'old', 'connections': []}
            path.write_text(json.dumps(data))
            with mock.patch.object(handoff, '_boot_id', return_value='new'):
                self.assertEqual(handoff.consume(str(path)), [])
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
