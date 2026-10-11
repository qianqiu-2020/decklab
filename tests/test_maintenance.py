"""Retention and diagnostic redaction use temporary archives and fake sessions."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import shutil
import threading
import types
import unittest
from unittest import mock

from seriald_core.device import Device
from seriald_core.maintenance import MaintenancePolicy, LogMaintenance, disk_status, diagnostic_summary


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 12, 12).timestamp()
        self.device = Device('secret-usb', '/secret/tty')
        self.manager = types.SimpleNamespace(lock=threading.RLock(), all_devices=lambda: [self.device])
        self.manager.maintenance = LogMaintenance(MaintenancePolicy(retention_days=7))

    def old(self, name, days=30):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private board output\n')
        os.utime(path, (self.now - days * 86400, self.now - days * 86400))
        return path

    def test_disabled_policy_never_enumerates_or_deletes(self):
        maintenance = LogMaintenance(MaintenancePolicy())
        with mock.patch('seriald_core.maintenance.os.walk', side_effect=AssertionError('disabled retention')):
            self.assertEqual(maintenance.prune(self.manager, self.root)['removed'], 0)
            maintenance.run_due(self.manager, self.root)

    def test_old_daily_logs_removed_but_recent_unknown_and_current_files_kept(self):
        old = self.old('SN1/serial-20260901.log')
        ssh = self.old('_provisional/soc/SN2/ssh-sid-20260901.log')
        current = self.old('SN1/serial-20261012.log')
        recently_modified = self.old('SN1/serial-20260902.log', days=1)
        unknown = self.old('SN1/notes.log')
        invalid = self.old('SN1/serial-20269999.log')
        result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
        self.assertEqual(result['removed'], 2)
        self.assertFalse(old.exists())
        self.assertFalse(ssh.exists())
        for path in (current, recently_modified, unknown, invalid):
            self.assertTrue(path.exists(), path)

    def test_active_serial_and_ssh_handles_are_kept_even_with_old_dates(self):
        serial = self.old('SN1/serial-20260901.log')
        ssh = self.old('SN1/ssh-sid-20260901.log')
        self.device.logpath, self.device.logf = str(serial), serial.open('a')
        session = types.SimpleNamespace(sid='sid', _log_lock=threading.RLock(),
                                        logpath=str(ssh), logf=ssh.open('a'))
        self.device.ssh_sessions['sid'] = session
        try:
            result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
            self.assertEqual(result['removed'], 0)
            self.device.logf.write('still writable\n')
            session.logf.write('still writable\n')
        finally:
            self.device.logf.close()
            session.logf.close()
        self.assertTrue(serial.exists())
        self.assertTrue(ssh.exists())

    def test_links_and_outside_archives_are_never_followed(self):
        outside = Path(self.tmp.name).parent / (self.root.name + '-outside')
        outside.mkdir()
        self.addCleanup(lambda: shutil.rmtree(outside))
        target = outside / 'serial-20260901.log'
        target.write_text('outside')
        os.utime(target, (0, 0))
        (self.root / 'directory-link').symlink_to(outside, target_is_directory=True)
        (self.root / 'serial-20260901.log').symlink_to(target)
        self.assertEqual(self.manager.maintenance.prune(self.manager, self.root, now=self.now)['removed'], 0)
        self.assertEqual(target.read_text(), 'outside')

    def test_deletion_failure_is_reported_and_does_not_remove_history(self):
        path = self.old('serial-20260901.log')
        with mock.patch('seriald_core.maintenance.os.unlink', side_effect=PermissionError('denied')):
            result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(path.exists())
        self.assertEqual(self.manager.maintenance.snapshot()['errors'], 1)

    def test_cross_day_running_boot_keeps_its_old_parts(self):
        start = self.old('serial-20260901.log')
        old_boot = self.old('serial-20260801.log')
        self.device.present, self.device.fd = True, 98765
        with mock.patch.object(self.device, 'stable_dir', return_value=str(self.root)), \
                mock.patch.object(self.device, 'boot_epochs', return_value=[{'parts': [{'file': start.name}]}]):
            result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
        self.assertEqual(result['removed'], 1)
        self.assertTrue(start.exists())
        self.assertFalse(old_boot.exists())

    def test_replacement_during_scan_is_not_deleted(self):
        path = self.old('serial-20260901.log')
        original = os.lstat(path)
        replacement = types.SimpleNamespace(st_mode=original.st_mode, st_dev=original.st_dev,
                                           st_ino=original.st_ino + 1, st_mtime=original.st_mtime)
        with mock.patch('seriald_core.maintenance.os.lstat', side_effect=[original, replacement]):
            result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
        self.assertEqual(result['removed'], 0)
        self.assertTrue(path.exists())

    def test_unknown_active_archive_location_prevents_cleanup(self):
        path = self.old('serial-20260901.log')
        self.device.present, self.device.fd = True, 98765
        with mock.patch.object(self.device, 'stable_dir', side_effect=PermissionError('private path')):
            result = self.manager.maintenance.prune(self.manager, self.root, now=self.now)
        self.assertEqual(result, {'removed': 0, 'bytes': 0, 'errors': 1})
        self.assertTrue(path.exists())
        self.assertEqual(self.manager.maintenance.snapshot()['last_run'], self.now)

    def test_disk_thresholds_and_unavailable_disk_are_structured(self):
        policy = MaintenancePolicy(min_free_mb=1, min_free_percent=5)
        with mock.patch('seriald_core.maintenance.shutil.disk_usage', return_value=types.SimpleNamespace(total=100000000, free=3000000)):
            self.assertEqual(disk_status(self.root, policy)['reason'], 'low-space')
        with mock.patch('seriald_core.maintenance.shutil.disk_usage', side_effect=OSError('secret path')):
            result = disk_status(self.root, policy)
        self.assertFalse(result['available'])
        self.assertNotIn('secret', json.dumps(result))

    def test_diagnostic_export_uses_allowlist_not_raw_records_or_errors(self):
        self.device.hostname = 'secret-host'
        self.device.probe_info = {'password': 'secret-password', 'ips': ['secret-ip'],
                                  'model': 'secret-model', 'identity_status': 'confirmed'}
        self.device.log_error = 'secret-token /private/data'
        self.device.ssh_sessions['sid'] = types.SimpleNamespace(log_error='secret-ssh-password')
        result = diagnostic_summary(self.manager, str(self.root), self.now)
        encoded = json.dumps(result)
        for secret in ('secret', '/private/data', 'private board output', '/secret/tty'):
            self.assertNotIn(secret, encoded)
        self.assertFalse(result['boards'][0]['serial_logging_ok'])
        self.assertEqual(result['boards'][0]['ssh_log_errors'], 1)
        self.assertEqual(len(result['boards'][0]['id']), 16)

    def test_policy_rejects_invalid_values_before_running(self):
        for value in ('-1', 'nan', '1.5', '36501', ''):
            with mock.patch.dict(os.environ, {'SERIALD_LOG_RETENTION_DAYS': value}):
                with self.assertRaises(ValueError):
                    MaintenancePolicy.from_env()


if __name__ == '__main__':
    unittest.main()
