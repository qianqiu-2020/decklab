"""USB observations annotate a board without creating a live UART."""
import json
import tempfile
import time
import unittest

from seriald_core.devices import Manager
from seriald_core.storage import Store


class FlashPresenceTests(unittest.TestCase):
    def test_recent_usb_presence_expires_without_changing_serial_liveness(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(':memory:')
            self.addCleanup(store.close)
            store.bind({'serial': 'USB-BOARD-123', 'serial_source': 'fastboot'}, '', '', 'usb-board')
            manager = Manager(store, artifact_dir=directory)
            self.addCleanup(manager.close)
            with store.lock, store.db:
                store.db.execute('INSERT INTO flash_targets VALUES(?,?,?,?,?,?)',
                    ('a' * 24, 'b' * 24, 'USB-BOARD-123', json.dumps({'mode': 'loader'}), time.time(), 1))
            manager.refresh_flash_presence()
            device = manager.resolve('usb-board')
            snapshot = device.snapshot()
            self.assertTrue(snapshot['flash_connected'])
            self.assertEqual(snapshot['flash_mode'], 'loader')
            self.assertFalse(snapshot['alive'])
            self.assertEqual(snapshot['state'], 'offline')
            self.assertIsNone(device.fd)
            self.assertNotIn('a' * 24, json.dumps(snapshot))
            with store.lock, store.db:
                store.db.execute('UPDATE flash_targets SET seen=?', (time.time() - 30,))
            manager.refresh_flash_presence()
            snapshot = manager.resolve('usb-board').snapshot()
            self.assertFalse(snapshot['flash_connected'])
            self.assertGreater(snapshot['flash_last_seen'], 0)
            self.assertFalse(snapshot['alive'])
