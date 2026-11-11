"""Human probe preferences over simulated connections; never sends to a board."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from seriald_core.actions import DeviceActions
from seriald_core.device import Device
from seriald_core.profiles import ProfileBook
from seriald_core.storage import Store


class ProbeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'registry.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.manager = types.SimpleNamespace(store=self.store, profiles=ProfileBook())
        self.device = Device('usb-one', '/fake/tty', manager=self.manager)
        self.device.present, self.device.fd = True, 98765
        self.actions = DeviceActions(self.manager)

    def save(self, **changes):
        payload = {'connection_id': self.device.connection_id, 'mode': 'custom',
                   'command': "printf 'HOST=buildroot\\nMODEL=custom\\n'"}
        payload.update(changes)
        return self.actions.execute('probe-settings', payload, self.device)

    def test_save_is_passive_persistent_and_specific_to_the_attachment(self):
        with mock.patch.object(self.device, '_write') as write:
            result = self.save()
        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload['mode'], 'custom')
        self.assertEqual(self.device.profile.probe, 'custom')
        self.assertFalse(self.device.profile.auto_configure_ssh)
        write.assert_not_called()
        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.get_probe_settings('usb-one')['mode'], 'custom')
            self.assertIsNone(reopened.get_probe_settings('usb-other'))
        finally:
            reopened.close()
        self.assertFalse(self.store.boards())

    def test_stale_connection_and_agent_cannot_change_or_read_configuration(self):
        self.assertEqual(self.save(connection_id='old-generation').status, 409)
        for payload in ({'read': True}, {'mode': 'custom', 'command': 'echo secret'}):
            result = self.actions.execute('probe-settings', payload, self.device, 'shared-agent/one')
            self.assertEqual(result.status, 403)
        self.assertIsNone(self.store.get_probe_settings('usb-one'))

    def test_busy_or_disconnected_connection_cannot_save(self):
        with self.device.transactions.begin('occupied', 5):
            self.assertEqual(self.save().status, 409)
        self.device.present = False
        self.assertEqual(self.save().status, 409)
        self.assertIsNone(self.store.get_probe_settings('usb-one'))

    def test_modes_validation_and_readback(self):
        for changes in ({'mode': 'unknown'}, {'command': ''}, {'command': 'a\x00b'},
                        {'command': 'a' * 16385}):
            with self.subTest(changes=repr(changes)[:80]):
                self.assertEqual(self.save(**changes).status, 400)
        self.assertEqual(self.save(mode='manual', command='').status, 200)
        self.assertEqual(self.device.profile.probe, 'manual')
        result = self.actions.execute('probe-settings', {'read': True}, self.device)
        self.assertEqual(result.payload['mode'], 'manual')
        self.assertIn('default_command', result.payload)
        self.assertEqual(self.save(mode='linux', command='').status, 200)
        self.assertEqual(self.device.profile.probe, 'linux')

    def test_store_rejects_unvalidated_settings_even_without_http(self):
        for port, settings in (('', {'mode': 'manual', 'command': ''}),
                               ('usb-one', {'mode': 'custom', 'command': ''}),
                               ('usb-one', {'mode': 'manual', 'command': '', 'extra': True})):
            with self.assertRaises(ValueError):
                self.store.set_probe_settings(port, settings)


if __name__ == '__main__':
    unittest.main()
