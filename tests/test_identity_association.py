"""Explicit association of a conflicted identity with its offline archive."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core import device as device_module
from seriald_core import devices
from seriald_core.storage import IdentityConfirmationRequired, IdentityError, Store
from seriald_core.task_actions import TaskActions


class IdentityAssociationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(str(self.root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        for module in (devices, device_module):
            patch = mock.patch.object(module, 'LOG_ROOT', str(self.root / 'logs'))
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = devices.Manager(self.store)
        self.addCleanup(self.manager.tasks.close)
        self.connections = []
        self.addCleanup(lambda: [device.close_log() for device in self.connections])

        self.original = self.connection('COM5')
        self.manager.identify(self.original, self.info())
        self.original.append(b'original board history\n')
        self.original.close_log()
        self.archive = Path(self.original.stable_dir())
        self.corrected = self.connection('COM6')
        with self.assertRaises(IdentityError):
            self.manager.identify(self.corrected, self.info())
        self.manager.identify(self.corrected, self.info('SN002', 'second-board'))
        self.corrected.append(b'corrected board history\n')
        self.corrected.close_log()
        self.corrected_record = self.store.get('SN002')
        self.original.present, self.original.fd = False, None
        self.pending = self.connection('COM5')
        self.reason = 'Verified original COM5 board; COM6 now reports SN002'

    @staticmethod
    def info(serial='SN001', hostname='debian'):
        return {'serial': serial, 'eeprom_serial': serial, 'hostname': hostname,
                'ips': ['eth0 192.0.2.1/24']}

    def connection(self, port):
        connection = devices.Device('usb-' + port, port, self.manager)
        connection.present, connection.fd = True, 98765
        self.connections.append(connection)
        self.manager.devices[connection.by_path] = connection
        return connection

    def confirmation(self, connection=None):
        connection = connection or self.pending
        with self.assertRaisesRegex(IdentityConfirmationRequired, '不唯一') as caught:
            self.manager.identify(connection, self.info())
        return dict(caught.exception.confirmation)

    def association(self, confirmation):
        return {'nonce': confirmation['nonce'], 'serial': confirmation['serial'],
                'reason': self.reason}

    def resolution_events(self):
        return [event for event in self.store.events('SN001')
                if event['action'] == 'identity-conflict-resolved']

    def assert_still_blocked(self):
        self.assertEqual(self.pending.serial, '')
        self.assertTrue(self.store.has_conflict('SN001'))
        self.assertEqual(self.store.get('SN002'), self.corrected_record)
        self.assertEqual(self.resolution_events(), [])

    def test_confirmation_preserves_archive_label_and_corrected_board(self):
        record = self.store.get('SN001')
        confirmation = self.confirmation()
        self.assertEqual(confirmation['serial'], 'SN001')
        self.assertEqual(confirmation['label'], 'debian')
        self.assertEqual(confirmation['hostname'], 'debian')
        self.assertEqual(confirmation['last_seen'], record['last_seen'])
        self.assertEqual(confirmation['last_dev'], 'COM5')
        self.assertEqual(confirmation['last_path'], 'usb-COM5')
        self.assertEqual(confirmation['connection_id'], self.pending.connection_id)
        self.assertTrue(confirmation['nonce'])
        self.assertGreater(confirmation['expires_at'], confirmation['detected_at'])
        self.assertEqual(self.store.get('SN001'), record)
        self.assert_still_blocked()

        self.pending.append(b'output before association\n')
        self.manager.identify(self.pending, self.info(),
                              association=self.association(confirmation), actor='web')
        self.assertEqual(self.pending.serial, 'SN001')
        self.assertEqual(self.pending.key, 'debian')
        self.assertEqual(Path(self.pending.stable_dir()), self.archive)
        history = ''.join(path.read_text() for path in self.archive.glob('*.log'))
        self.assertIn('original board history', history)
        self.assertIn('output before association', history)
        self.assertFalse(self.store.has_conflict('SN001'))
        self.assertEqual(self.store.get('SN002'), self.corrected_record)
        self.assertEqual(self.corrected.serial, 'SN002')
        self.assertEqual(len(self.store.boards()), 2)
        events = self.resolution_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['actor'], 'web')
        self.assertEqual(json.loads(events[0]['detail']), {
            'reason': self.reason, 'detected_at': confirmation['detected_at']})

    def test_confirmed_probe_reads_identity_again_before_associating(self):
        def frame(serial):
            return 'HOST=debian\nSERIAL=%s\nEEPROM_SERIAL=%s\nMODEL=board\nSSHD=active\n' % (serial, serial)

        with mock.patch.object(self.pending, '_run_cmd', side_effect=[
                (True, frame('SN001')), (True, frame('SN003')), (True, frame('SN001'))]) as probe:
            first = self.pending.run_probe()
            self.assertFalse(first['ok'])
            self.assertEqual(first['code'], 'identity-confirmation-required')
            association = self.association(first['identity_confirmation'])
            changed = self.pending.run_probe(association=association, actor='web')
            self.assertFalse(changed['ok'])
            self.assert_still_blocked()
            confirmed = self.pending.run_probe(association=association, actor='web')
            self.assertTrue(confirmed['ok'])
            self.assertEqual(confirmed['serial'], 'SN001')
            self.assertEqual(probe.call_count, 3)
        self.assertFalse(self.store.has_conflict('SN001'))

    def test_durable_identify_then_confirm_binds_the_original_archive(self):
        actions = TaskActions(self.manager)
        payload = {'kind': 'identify', 'key': self.pending.key, 'serial': '',
                   'connection_id': self.pending.connection_id, 'client_id': 'verification-page'}

        def submit(request):
            response = actions.start(request, self.pending, 'web')
            self.assertEqual(response.status, 202)
            task_id = response.payload['task']['id']
            with self.manager.tasks._lock:
                running = self.manager.tasks._active.get(task_id)
            if running:
                running[1].join(2)
                self.assertFalse(running[1].is_alive())
            return self.manager.tasks.store.get(task_id)

        frame = 'HOST=debian\nSERIAL=SN001\nEEPROM_SERIAL=SN001\nMODEL=board\nSSHD=active\n'
        with mock.patch.object(self.pending, '_run_cmd', return_value=(True, frame)) as probe:
            failed = submit(payload)
            self.assertEqual(failed['state'], 'failed')
            self.assertEqual(failed['result']['code'], 'identity-confirmation-required')
            self.assert_still_blocked()
            confirmed = submit(dict(payload, association=self.association(
                failed['result']['identity_confirmation'])))
            self.assertEqual(confirmed['state'], 'completed')
            self.assertEqual(confirmed['result']['serial'], 'SN001')
            self.assertEqual(confirmed['result']['device_key'], 'debian')
            self.assertEqual(probe.call_count, 2)
        self.assertFalse(self.store.has_conflict('SN001'))
        self.assertEqual(json.loads(self.resolution_events()[0]['detail'])['reason'], self.reason)

    def test_expired_confirmation_does_not_unlock_identity(self):
        with mock.patch.object(devices.time, 'time', return_value=1000):
            confirmation = self.confirmation()
        self.assertEqual(confirmation['expires_at'], 1600)
        with mock.patch.object(devices.time, 'time', return_value=confirmation['expires_at'] + 1):
            with self.assertRaises(IdentityError):
                self.manager.identify(self.pending, self.info(),
                                      association=self.association(confirmation), actor='web')
        self.assert_still_blocked()

    def test_confirmation_cannot_move_to_another_connection_generation(self):
        confirmation = self.confirmation()
        replacement = self.connection('COM5')
        with self.assertRaises(IdentityError):
            self.manager.identify(replacement, self.info(),
                                  association=self.association(confirmation), actor='web')
        self.assertEqual(replacement.serial, '')
        self.pending.connection_id = 'changed-generation'
        with self.assertRaises(IdentityError):
            self.manager.identify(self.pending, self.info(),
                                  association=self.association(confirmation), actor='web')
        self.assert_still_blocked()

    def test_changed_probe_serial_and_tampered_confirmation_are_rejected(self):
        for change in ('probe-serial', 'request-serial', 'nonce'):
            with self.subTest(change=change):
                confirmation = self.confirmation()
                association = self.association(confirmation)
                info = self.info()
                if change == 'probe-serial':
                    info = self.info('SN003')
                elif change == 'request-serial':
                    association['serial'] = 'SN003'
                else:
                    association['nonce'] = 'not-the-issued-nonce'
                with self.assertRaises(IdentityError):
                    self.manager.identify(self.pending, info, association=association, actor='web')
                self.assertIsNone(self.store.get('SN003'))
                self.assert_still_blocked()

    def test_changed_archive_or_conflict_requires_a_new_confirmation(self):
        for change in ('archive', 'conflict'):
            with self.subTest(change=change):
                confirmation = self.confirmation()
                if change == 'archive':
                    self.store.rename('SN001', 'debian-verified')
                else:
                    with self.store.db:
                        self.store.db.execute('UPDATE identity_conflicts SET detected=detected+1 '
                                              'WHERE serial=?', ('SN001',))
                with self.assertRaises(IdentityError):
                    self.manager.identify(self.pending, self.info(),
                                          association=self.association(confirmation), actor='web')
                self.assert_still_blocked()

    def test_another_online_connection_prevents_association(self):
        confirmation = self.confirmation()
        self.original.present, self.original.fd = True, 98765
        with self.assertRaises(IdentityError):
            self.manager.identify(self.pending, self.info(),
                                  association=self.association(confirmation), actor='web')
        self.assertEqual(self.manager.boards['SN001'].connection, self.original)
        self.assert_still_blocked()
        self.original.present, self.original.fd = False, None
        unbound_duplicate = self.connection('COM7')
        unbound_duplicate.reported_serial = 'SN001'
        with self.assertRaises(IdentityError):
            self.manager.identify(self.pending, self.info(),
                                  association=self.association(confirmation), actor='web')
        self.assert_still_blocked()

    def test_agent_cannot_confirm_a_web_association(self):
        confirmation = self.confirmation()
        with self.assertRaises(IdentityError):
            self.manager.identify(self.pending, self.info(),
                                  association=self.association(confirmation), actor='agent/diagnosis')
        self.assert_still_blocked()

    def test_failed_binding_preserves_conflict_and_avoids_resolution_audit(self):
        confirmation = self.confirmation()
        record = self.store.get('SN001')
        with mock.patch.object(self.store, '_bind_connection', side_effect=IdentityError('binding failed')):
            with self.assertRaisesRegex(IdentityError, 'binding failed'):
                self.manager.identify(self.pending, self.info(),
                                      association=self.association(confirmation), actor='web')
        self.assertEqual(self.store.get('SN001'), record)
        self.assert_still_blocked()


if __name__ == '__main__':
    unittest.main()
