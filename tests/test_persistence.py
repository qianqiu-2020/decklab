"""Identity, durable history, migration and online log access without hardware."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock

from seriald_core import device as device_module
from seriald_core import devices, web
from seriald_core.admin import import_legacy
from seriald_core.storage import IdentityError, Store, archive_component, serial_component


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(str(self.root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        patches = [mock.patch.object(devices, 'LOG_ROOT', str(self.root / 'logs')),
                   mock.patch.object(device_module, 'LOG_ROOT', str(self.root / 'logs'))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = devices.Manager(self.store)
        self.connections = []
        self.addCleanup(lambda: [d.close_log() for d in self.connections])

    def connection(self, port='usb-1'):
        d = devices.Device(port, '/fake/ttyUSB0', self.manager)
        d.present = True
        d.fd = 98765
        self.connections.append(d)
        self.manager.devices[port] = d
        return d

    def identify(self, d, serial='SN001', host='riscv-118'):
        self.manager.identify(d, {'serial': serial, 'hostname': host, 'ips': ['eth0 192.0.2.1/24']})

    def handler(self, path, body=None):
        h = web.Handler.__new__(web.Handler)
        h.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        h.headers = {'Content-Length': str(len(raw))}
        h.rfile, h.wfile = io.BytesIO(raw), io.BytesIO()
        h.send_response, h.send_header, h.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR', self.manager):
            h.do_POST() if body is not None else h.do_GET()
        return h.send_response.call_args.args[0], json.loads(h.wfile.getvalue())

    def test_label_changes_do_not_move_serial_archive(self):
        d = self.connection()
        d.append(b'before identification\n')
        pending = Path(d.stable_dir())
        self.identify(d)
        directory = Path(d.stable_dir())
        self.assertEqual(directory.name, 'SN001')
        self.assertEqual(directory.parent.name, 'device-tree')
        self.assertEqual(directory.parent.parent.name, '_provisional')
        self.assertFalse(pending.exists())
        self.assertTrue(any('before identification' in p.read_text() for p in directory.glob('*.log')))
        self.manager.rename(d, 'musepi2')
        d.append(b'after rename\n')
        self.assertEqual(d.key, 'musepi2')
        self.assertEqual(d.stable_dir(), str(directory))
        self.assertIsNone(self.manager.resolve('riscv-118'))
        self.assertEqual(self.store.get('SN001')['label'], 'musepi2')

    def test_matching_eeprom_identity_promotes_provisional_archive(self):
        d = self.connection()
        self.identify(d)
        d.append(b'provisional history\n')
        provisional = Path(d.stable_dir())
        self.manager.identify(d, {
            'serial': 'SN001', 'eeprom_serial': 'SN001', 'hostname': 'riscv-118', 'ips': []})
        canonical = Path(d.stable_dir())
        self.assertEqual(canonical, self.root / 'logs' / 'SN001')
        self.assertFalse(provisional.exists())
        self.assertEqual(d.record['info']['serial_source'], 'eeprom-onie')
        matching = [p for p in canonical.glob('*.log') if 'provisional history' in p.read_text()]
        self.assertEqual(len(matching), 1)

    def test_eeprom_promotion_after_reconnect_migrates_previous_archive(self):
        old = self.connection('usb-1')
        self.identify(old)
        old.append(b'history before provisioning\n')
        provisional = Path(old.stable_dir())
        old.close_log()
        old.present, old.fd = False, None
        new = self.connection('usb-9')
        self.manager.identify(new, {
            'serial': 'SN001', 'eeprom_serial': 'SN001', 'hostname': 'riscv-118', 'ips': []})
        canonical = Path(new.stable_dir())
        self.assertEqual(canonical, self.root / 'logs' / 'SN001')
        self.assertFalse(provisional.exists())
        self.assertTrue(any('history before provisioning' in p.read_text()
                            for p in canonical.glob('*.log')))
        self.assertEqual(len(self.store.boards()), 1)

    def test_changed_eeprom_serial_requires_explicit_archive_migration(self):
        old = self.connection('usb-1')
        fields = {'serial': '0123456789ABCDEF', 'soc_serial': '0059D968E583C49A',
                  'soc_family': 'spacemit socs', 'soc_id': 'K1-6370', 'hostname': 'milkv2'}
        self.manager.identify(old, fields)
        old.present, old.fd = False, None
        new = self.connection('usb-9')
        with self.assertRaisesRegex(IdentityError, 'correct-serial'):
            self.manager.identify(new, dict(fields, eeprom_serial='MVJ-K1-000001'))
        self.assertIsNone(self.store.get('MVJ-K1-000001'))
        self.assertIsNotNone(self.store.get('0059D968E583C49A'))
        self.assertIn('/_unprovisioned/', new.stable_dir())

    def test_eeprom_provisioning_promotes_boot_marker_on_same_usb_path(self):
        marker = 'boot-4e75e47e-c41f-4e9b-8f61-ccc2d2e2c1e9'
        old = self.connection('usb-1')
        self.manager.identify(old, {
            'serial': '0123456789ABCDEF', 'boot_marker': marker,
            'hostname': 'k1', 'ips': []})
        self.manager.rename(old, 'milkv')
        old.append(b'history before eeprom\n')
        provisional = Path(old.stable_dir())
        old.close_log()
        old.present, old.fd = False, None

        new = self.connection('usb-1')
        self.manager.identify(new, {
            'serial': 'MJM10004000001', 'eeprom_serial': 'MJM10004000001',
            'boot_marker': marker, 'hostname': 'k1', 'ips': []})

        self.assertEqual(new.serial, 'MJM10004000001')
        self.assertEqual(new.key, 'milkv')
        self.assertEqual(new.record['info']['identity_status'], 'confirmed')
        self.assertEqual(new.stable_dir(), str(self.root / 'logs' / 'MJM10004000001'))
        self.assertFalse(provisional.exists())
        self.assertIsNone(self.store.get(marker))
        self.assertTrue(any('history before eeprom' in path.read_text()
                            for path in Path(new.stable_dir()).glob('*.log')))
        self.assertIn('identity-promoted',
                      [event['action'] for event in self.store.events('MJM10004000001')])

    def test_eeprom_does_not_claim_boot_marker_archive_after_usb_move(self):
        marker = 'boot-4e75e47e-c41f-4e9b-8f61-ccc2d2e2c1e9'
        old = self.connection('usb-1')
        self.manager.identify(old, {
            'serial': '0123456789ABCDEF', 'boot_marker': marker,
            'hostname': 'k1', 'ips': []})
        old.present, old.fd = False, None
        new = self.connection('usb-9')
        with self.assertRaisesRegex(IdentityError, 'correct-serial'):
            self.manager.identify(new, {
                'serial': 'MJM10004000001', 'eeprom_serial': 'MJM10004000001',
                'boot_marker': marker, 'hostname': 'k1', 'ips': []})
        self.assertIsNone(self.store.get('MJM10004000001'))

    def test_moving_board_preserves_label_and_old_logs_after_fresh_identification(self):
        old = self.connection('usb-1')
        self.identify(old)
        old.append(b'old port\n')
        old.close_log()
        old.present, old.fd = False, None
        new = self.connection('usb-9')
        self.assertNotEqual(new.key, old.key)
        self.assertNotIn(b'old port', new.buf)
        self.identify(new, host='changed-hostname')
        self.assertEqual(new.key, 'riscv-118')
        self.assertEqual(new.stable_dir(), old.stable_dir())
        self.assertEqual(self.store.get('SN001')['last_path'], 'usb-9')
        self.assertEqual(len([d for d in self.manager.all_devices() if d.serial]), 1)

    def test_another_board_in_same_port_cannot_inherit_identity(self):
        old = self.connection()
        self.identify(old)
        old.present, old.fd = False, None
        new = self.connection()
        self.assertEqual(new.serial, '')
        self.assertEqual(new.snapshot()['state'], 'pending')
        self.identify(new, 'SN002', 'musepi2')
        self.assertNotEqual(old.stable_dir(), new.stable_dir())
        self.assertEqual(old.snapshot()['state'], 'offline')
        self.assertEqual({d.key for d in self.manager.all_devices()}, {'riscv-118', 'musepi2'})

    def test_unprovisioned_key_follows_usb_path_but_connections_keep_separate_logs(self):
        one = self.connection('pci-usb-port-1')
        one_key, one_dir = one.key, Path(one.stable_dir())
        one.present = False
        two = self.connection('pci-usb-port-1')
        self.assertEqual(two.key, one_key)
        self.assertNotEqual(Path(two.stable_dir()), one_dir)
        self.assertEqual(Path(two.stable_dir()).parent, one_dir.parent)
        three = self.connection('pci-usb-port-2')
        self.assertNotEqual(three.key, one_key)
        self.assertTrue(two.snapshot()['needs_provisioning'])

    def test_duplicate_serial_online_and_duplicate_label_are_rejected(self):
        one, two = self.connection('usb-1'), self.connection('usb-2')
        self.identify(one)
        with self.assertRaises(IdentityError):
            self.identify(two)
        self.assertEqual(two.serial, '')
        self.assertEqual(self.store.get('SN001')['last_path'], 'usb-1')
        self.identify(two, 'SN002', 'musepi2')
        with self.assertRaises(IdentityError):
            self.manager.rename(two, 'riscv-118')
        self.assertEqual(two.key, 'musepi2')
        with self.assertRaises(IdentityError):
            self.store.bind({'serial': 'SN003'}, '', '', 'riscv-118')
        self.assertIsNone(self.store.get('SN003'))

    def test_serial_path_is_reversible_collision_free_and_cannot_traverse(self):
        names = ['SN001', '../x', '/tmp/x', '%2Ftmp%2Fx', '..']
        encoded = [serial_component(s) for s in names]
        self.assertEqual(encoded[0], 'SN001')
        self.assertEqual(len(set(encoded)), len(names))
        self.assertTrue(all('/' not in x and x not in ('.', '..') for x in encoded))
        for value in ('', 'a\x00b', 'a' * 101):
            with self.assertRaises(IdentityError):
                serial_component(value)

    def test_archive_paths_separate_provisional_and_confirmed_identities(self):
        self.assertEqual(archive_component('SN001', 'eeprom-onie'), 'SN001')
        self.assertEqual(archive_component('SN001', 'verified-manual'), 'SN001')
        self.assertEqual(archive_component('SN001', 'soc-sysfs'),
                         '_provisional/soc-sysfs/SN001')
        self.assertEqual(archive_component('SN001', 'device-tree'),
                         '_provisional/device-tree/SN001')

    def test_failed_identity_is_visible_without_binding_or_merging_logs(self):
        one, two = self.connection('usb-1'), self.connection('usb-2')
        self.identify(one)
        frame = 'HOST=other\nSERIAL=SN001\nMODEL=board\nSSHD=active\n'
        with mock.patch.object(two, '_run_cmd', return_value=(True, frame)):
            result = two.run_probe()
        self.assertFalse(result['ok'])
        state = two.snapshot()
        self.assertEqual(state['serial'], '')
        self.assertEqual(state['reported_serial'], 'SN001')
        self.assertIn('另一个串口', state['identity_error'])
        self.assertIn('/_unprovisioned/', two.stable_dir())

    def test_duplicate_serial_stays_blocked_after_disconnect_and_restart(self):
        one, two = self.connection('usb-1'), self.connection('usb-2')
        self.identify(one)
        with self.assertRaises(IdentityError):
            self.identify(two)
        one.present, one.fd = False, None
        self.assertTrue(self.store.has_conflict('SN001'))
        with self.assertRaisesRegex(IdentityError, '不唯一'):
            self.identify(two)
        restored = devices.Manager(self.store)
        new = devices.Device('usb-3', '/fake/ttyUSB3', restored)
        with self.assertRaisesRegex(IdentityError, '不唯一'):
            restored.identify(new, {'serial': 'SN001', 'hostname': 'other'})
        self.assertEqual(new.serial, '')

    def test_distinct_efuse_ids_with_same_firmware_serial_have_separate_archives(self):
        self.store.mark_conflict('0123456789ABCDEF')
        one, two = self.connection('usb-1'), self.connection('usb-2')
        for d, uid, hostname in ((one, '0059D968E583C49A', 'milkv2'),
                                 (two, '0059D968E583C49B', 'milkv')):
            frame = ('HOST=%s\nSERIAL=0123456789ABCDEF\nMODEL=Jupiter\nSSHD=active\n'
                     'SOC_SERIAL=%s\nSOC_FAMILY=spacemit socs\nSOC_ID=K1-6370\n') % (hostname, uid)
            with mock.patch.object(d, '_run_cmd', return_value=(True, frame)):
                self.assertTrue(d.run_probe()['ok'])
            self.assertEqual(d.serial, uid)
            self.assertEqual(d.snapshot()['reported_serial'], '0123456789ABCDEF')
            self.assertEqual(Path(d.stable_dir()).name, uid)
        self.assertNotEqual(one.stable_dir(), two.stable_dir())

    def test_restart_loads_history_as_offline_without_opening_ports(self):
        d = self.connection()
        self.identify(d)
        d.append(b'previous boot\n')
        d.close_log()
        with mock.patch.object(devices, 'open_serial', side_effect=AssertionError('hardware')):
            restored = devices.Manager(self.store)
        history = restored.resolve('riscv-118')
        self.connections.append(history)
        self.assertEqual(history.serial, 'SN001')
        self.assertFalse(history.snapshot()['alive'])
        self.assertEqual(history.snapshot()['dev'], '')
        self.assertEqual(history.buf, b'')
        history.read(0)
        self.assertIn(b'previous boot', history.buf)
        self.assertEqual(history.console_tail, '')

    def test_clean_handoff_restores_only_the_same_kernel_tty(self):
        d = self.connection('usb-1')
        self.identify(d)
        d.identity = (1, 2, 3)
        handoff = self.manager.close()
        self.assertEqual(handoff[0]['serial'], 'SN001')

        def scan(manager, identity):
            stat_result = types.SimpleNamespace(st_dev=identity[0], st_ino=identity[1],
                                                st_rdev=identity[2],
                                                st_mode=stat.S_IFCHR | 0o600)
            listdir = os.listdir
            with mock.patch.object(devices.os, 'listdir',
                    side_effect=lambda path: ['usb-1'] if path == devices.BY_PATH else listdir(path)), \
                    mock.patch.object(devices.os.path, 'realpath', return_value='/fake/ttyUSB0'), \
                    mock.patch.object(devices.os, 'stat', return_value=stat_result), \
                    mock.patch.object(devices.threading, 'Thread'):
                manager.scan()
            return manager.devices['usb-1']

        restored = devices.Manager(self.store, handoff)
        same = scan(restored, (1, 2, 3))
        self.assertEqual(same.serial, 'SN001')
        changed = devices.Manager(self.store, handoff)
        replacement = scan(changed, (1, 22, 3))
        self.assertEqual(replacement.serial, '')
        self.assertEqual(replacement.snapshot()['state'], 'pending')

    def test_prepare_switch_detaches_old_board_and_arms_one_probe(self):
        d = self.connection('usb-1')
        self.identify(d)
        old_connection = d.connection_id
        old_dir = Path(d.stable_dir())
        d.append(b'old board\n')
        key = self.manager.prepare_switch(d, 'operator')
        self.assertTrue(key.startswith('unprovisioned-'))
        self.assertEqual(d.serial, '')
        self.assertNotEqual(d.connection_id, old_connection)
        self.assertEqual(self.manager.resolve('riscv-118').snapshot()['state'], 'offline')
        self.assertTrue(d.auto_identify)
        d.append(b'new board booting\n')
        self.assertNotEqual(Path(d.stable_dir()), old_dir)
        self.assertIn('/_unprovisioned/', d.stable_dir())
        self.assertEqual(self.store.events('SN001')[0]['action'], 'switch-armed')

        finished = threading.Event()
        with mock.patch.object(d, 'run_probe', side_effect=lambda: (finished.set() or {'ok': True})) as probe:
            d._maybe_auto_identify('k1 login:')
            self.assertTrue(finished.wait(2))
            d._maybe_auto_identify('root@k1:~#')
        probe.assert_called_once_with()
        self.assertFalse(d.auto_identify)
        self.assertNotIn('usb-1', self.manager.armed_paths)

    def test_marker_survives_restart_port_and_hardware_interface_changes(self):
        marker = 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6'
        old = self.connection('usb-1')
        old.append(b'pending history\n')
        self.manager.identify(old, {'serial': '', 'boot_marker': marker, 'hostname': 'milkv'})
        self.manager.rename(old, 'milkv')
        old.close_log()
        restored = devices.Manager(self.store)
        new = devices.Device('usb-9', '/fake/ttyUSB9', restored)
        self.connections.append(new)
        restored.identify(new, {'serial': 'new-firmware', 'soc_serial': 'new-chip', 'boot_marker': marker})
        self.assertEqual(new.serial, marker)
        self.assertEqual(new.key, 'milkv')
        self.assertEqual(new.stable_dir(), old.stable_dir())
        self.assertTrue(any('pending history' in p.read_text() for p in Path(new.stable_dir()).glob('*.log')))
        self.assertEqual(len(self.store.boards()), 1)

    def test_copied_marker_is_rejected_and_conflict_persists(self):
        marker = 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6'
        one, two = self.connection('usb-1'), self.connection('usb-2')
        fields = {'serial': '', 'boot_marker': marker}
        self.manager.identify(one, dict(fields))
        with self.assertRaises(IdentityError):
            self.manager.identify(two, dict(fields))
        self.assertEqual(two.serial, '')
        one.present = False
        with self.assertRaises(IdentityError):
            self.manager.identify(two, dict(fields))
        self.assertTrue(self.store.has_conflict(marker))

    def test_marker_cannot_redirect_another_known_hardware_identity(self):
        marker = 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6'
        one, two = self.connection('usb-1'), self.connection('usb-2')
        self.manager.identify(one, {'serial': '', 'boot_marker': marker})
        self.identify(two)
        with self.assertRaisesRegex(IdentityError, '已有硬件档案冲突'):
            self.manager.identify(two, {'serial': 'SN001', 'boot_marker': marker})
        self.assertEqual(two.serial, 'SN001')

    def test_marker_initialization_is_explicit_and_errors_never_bind(self):
        d = self.connection()
        with mock.patch.object(d, '_run_cmd', return_value=(True, 'BOOT_MARKER_ERROR=create-failed\n')) as run:
            self.assertFalse(d.run_probe(init_boot_marker=True)['ok'])
            self.assertIn('set -C', run.call_args.args[0])
        self.assertEqual(d.serial, '')
        with mock.patch.object(d, '_run_cmd', return_value=(False, 'no shell')) as run:
            self.assertFalse(d.run_probe()['ok'])
            self.assertNotIn('set -C', run.call_args.args[0])
        self.identify(d)
        with mock.patch.object(d, '_run_cmd') as run:
            self.assertFalse(d.run_probe(init_boot_marker=True)['ok'])
        run.assert_not_called()

    def test_http_marker_flag_requires_boolean_and_records_success_actor(self):
        d = self.connection()
        with mock.patch.object(d, 'run_probe') as run:
            status, _ = self.handler('/api/identify', {'key': d.key, 'init_boot_marker': 'false'})
        self.assertEqual(status, 400)
        run.assert_not_called()
        marker = 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6'
        frame = 'HOST=milkv\nSERIAL=\nMODEL=board\nSSHD=inactive\nBOOT_MARKER=' + marker + '\n'
        with mock.patch.object(d, '_run_cmd', return_value=(True, frame)):
            status, result = self.handler('/api/identify', {
                'key': d.key, 'init_boot_marker': True, 'actor': 'test-agent'})
        self.assertEqual(status, 200)
        self.assertEqual(result['serial'], marker)
        self.assertEqual(self.store.events(marker)[0]['actor'], 'test-agent')
        self.assertEqual(self.store.events(marker)[0]['action'], 'boot-marker-initialized')

    def test_expected_serial_blocks_reassigned_label(self):
        d = self.connection()
        self.identify(d)
        with mock.patch.object(d, 'send') as send:
            status, result = self.handler('/api/session-send', {
                'key': 'riscv-118', 'serial': 'different-board', 'data': 'do not send'})
        self.assertEqual(status, 404)
        send.assert_not_called()

    def test_shutdown_prevents_a_late_scan_from_starting_collectors(self):
        self.manager.close()
        with mock.patch.object(devices.os, 'listdir', side_effect=AssertionError('discovery after shutdown')):
            self.assertEqual(self.manager.scan(), {})

    def test_online_log_view_is_bounded_and_rejects_traversal_or_symlinks(self):
        d = self.connection()
        self.identify(d)
        directory = Path(d.stable_dir())
        (directory / 'serial-test.log').write_bytes(b'a' * 100000)
        (directory / 'escape.log').symlink_to('/etc/passwd')
        status, result = self.handler('/api/log?key=riscv-118&file=serial-test.log')
        self.assertEqual(status, 200)
        self.assertEqual(len(result['data']), 65536)
        self.assertEqual(result['start_off'], 100000 - 65536)
        status, result = self.handler('/api/log?key=riscv-118&file=serial-test.log&off=0')
        self.assertTrue(result['more'])
        for name in ('../registry.sqlite3', 'escape.log'):
            self.assertEqual(self.handler('/api/log?key=riscv-118&file=' + name)[0], 404)

    def test_completed_and_unknown_sessions_survive_close_and_restart(self):
        d = self.connection()
        self.identify(d)
        self.store.open_session('ssh1', d.serial, 'claude-code', 'uname -a', '192.0.2.1')
        self.store.session_result(d.serial, 'ssh1', True, 7)
        self.store.close_session('ssh1')
        self.store.open_session('ssh2', d.serial, 'agent', 'long command', '192.0.2.1')
        self.store.session_result(d.serial, 'ssh2', False, None)
        self.store.close_session('ssh2')
        self.store.open_session('ssh3', d.serial, 'web', '', '192.0.2.1')
        self.store.recover_sessions()
        status, history = self.handler('/api/activity?key=riscv-118')
        self.assertEqual(status, 200)
        by_id = {s['sid']: s for s in history['sessions']}
        self.assertEqual(by_id['ssh1']['actor'], 'claude-code')
        self.assertEqual(by_id['ssh1']['exit_code'], 7)
        self.assertEqual(by_id['ssh2']['status'], 'unknown')
        self.assertEqual(by_id['ssh3']['status'], 'interrupted')
        for completed, code in ((True, None), (False, 0), (True, -1)):
            with self.assertRaises(IdentityError):
                self.store.session_result(d.serial, 'ssh1', completed, code)

    def test_activity_pages_sessions_and_filters_per_keystroke_events(self):
        d = self.connection()
        self.identify(d)
        for index in range(13):
            sid = 'ssh%02d' % index
            self.store.open_session(sid, d.serial, 'agent', 'command %d' % index, '192.0.2.1')
            self.store.close_session(sid)
        for index in range(12):
            self.store.event(d.serial, 'web', 'identified', 'usb-%d' % index)
            self.store.event(d.serial, 'web', 'session-send', 'request: sid=serial')

        status, page = self.handler(
            '/api/activity?key=riscv-118&session_offset=10&event_offset=10&limit=10')
        self.assertEqual(status, 200)
        self.assertEqual(page['session_total'], 13)
        self.assertEqual(len(page['sessions']), 3)
        self.assertEqual(page['event_total'], 13)  # 12 explicit events + initial identification
        self.assertEqual(len(page['events']), 3)
        self.assertNotIn('session-send', {event['action'] for event in page['events']})

    def test_log_files_are_newest_first_across_serial_and_ssh_names(self):
        d = self.connection()
        self.identify(d)
        root = Path(d.stable_dir())
        files = [
            ('ssh-ssh001-20260918.log', 30),
            ('serial-20260920.log', 10),
            ('serial-old-serial-20260919.log', 20),
        ]
        for name, mtime in files:
            path = root / name
            path.write_text(name)
            path.touch()
            os.utime(path, (mtime, mtime))
        listed = d.log_files()
        self.assertEqual([entry['date'] for entry in listed],
                         ['2026-09-20', '2026-09-19', '2026-09-18'])
        self.assertEqual([entry['kind'] for entry in listed], ['serial', 'serial', 'ssh'])

    def test_import_is_explicit_dry_run_repeatable_and_preserves_sources(self):
        old = self.root / 'legacy' / 'logs' / 'usb-path'
        old.mkdir(parents=True)
        (old / 'serial-old.log').write_text('original log\n')
        base = self.root / 'new'
        mapping = [{'serial': 'SN001', 'label': 'riscv-118', 'log_dirs': ['usb-path']}]
        plan = import_legacy(self.root / 'legacy', base, mapping)
        self.assertEqual(plan['files'], 1)
        self.assertFalse(base.exists())
        import_legacy(self.root / 'legacy', base, mapping, True)
        import_legacy(self.root / 'legacy', base, mapping, True)
        self.assertEqual((old / 'serial-old.log').read_text(), 'original log\n')
        self.assertEqual(len(list((base / 'logs' / 'SN001').glob('*.log'))), 1)
        (old / 'serial-old.log').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            import_legacy(self.root / 'legacy', base, mapping, True)


if __name__ == '__main__':
    unittest.main()
