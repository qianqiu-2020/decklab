"""会话注册表并发回归:假 SSH、假目录,不打开串口或启动子进程。"""
import json
import subprocess
import threading
import time
import types
import unittest
from unittest import mock

from seriald_core import device as seriald
from seriald_core.transactions import TransactionError
from seriald_core.profiles import BoardProfile, ProfileBook
from seriald_core.storage import Store
from seriald_core.models import Board


class FakeSession:
    def __init__(self, sid, ip='192.0.2.1', label='test', logdir='/unused', **kwargs):
        self.sid = sid
        self.ip = ip
        self.cv = threading.Condition()
        self.closed = False
        self._stop = threading.Event()
        self.last_touch = time.monotonic()
        self.close_count = 0
        self.on_ensure = None
        self.on_close = None
        self.on_snapshot = None
        self.on_idle = None

    def ensure(self):
        return self.on_ensure() if self.on_ensure else True

    def close(self):
        if self.on_close:
            self.on_close()
        self.closed = True
        self._stop.set()
        self.close_count += 1

    def snapshot(self):
        if self.on_snapshot:
            self.on_snapshot()
        with self.cv:
            return {'sid': self.sid, 'alive': not self.closed}

    def is_idle(self, now, ttl):
        if self.on_idle:
            self.on_idle()
        with self.cv:
            return self.closed or now - self.last_touch >= ttl


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(seriald.Device, 'preload'),
            mock.patch.object(seriald.Device, 'stable_dir', return_value='/unused'),
            mock.patch.object(seriald, 'SshSession', side_effect=FakeSession),
            mock.patch.object(seriald, 'check_password_login', return_value=(True, False, '')),
            mock.patch.object(seriald.os, 'open', side_effect=AssertionError('hardware forbidden')),
            mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('SSH forbidden')),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.device = seriald.Device('test-path', '/dev/test-only')
        self.device.probe_info = {'ips': ['eth0 192.0.2.1/24']}

    def test_pending_reservations_enforce_cap_and_ids_remain_unique(self):
        device = self.device
        release = threading.Event()
        state = threading.Condition()
        results, errors, constructed = [], [], []
        start = threading.Barrier(10)

        def factory(*args, **kwargs):
            session = FakeSession(*args, **kwargs)
            with state:
                constructed.append(session)
                state.notify_all()
            session.on_ensure = lambda: release.wait(3)
            return session

        def connect():
            try:
                start.wait(3)
                result = device.ssh_connect()
                with state:
                    results.append(result)
                    state.notify_all()
            except BaseException as error:
                with state:
                    errors.append(error)
                    state.notify_all()

        threads = [threading.Thread(target=connect) for _ in range(9)]
        with mock.patch.object(seriald, 'SSH_LIMIT', 3), \
                mock.patch.object(seriald, 'SshSession', side_effect=factory):
            try:
                for thread in threads:
                    thread.start()
                start.wait(3)
                with state:
                    self.assertTrue(state.wait_for(lambda: len(constructed) == 3, 3))
                    self.assertTrue(state.wait_for(lambda: len(results) == 6 or errors, 3))
                self.assertEqual(errors, [])
                self.assertEqual(device.ssh_pending, 3)
                self.assertEqual(device.ssh_sessions, {})
                self.assertTrue(all(session is None and '上限' in error for session, error in results))
            finally:
                release.set()
                for thread in threads:
                    thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 9)
        self.assertEqual(len(device.ssh_sessions), 3)
        self.assertTrue(all(sid.startswith('ssh') for sid in device.ssh_sessions))
        self.assertEqual(device.ssh_pending, 0)
        self.assertEqual(len({session.sid for session in constructed}), 3)

    def test_failed_creation_releases_slot_and_disposes_session(self):
        device = self.device
        failed = FakeSession('ssh1')
        failed.on_ensure = lambda: False
        with mock.patch.object(seriald, 'SSH_LIMIT', 1), \
                mock.patch.object(seriald, 'SshSession', return_value=failed):
            session, error = device.ssh_connect()
        self.assertIsNone(session)
        self.assertIn('失败', error)
        self.assertEqual(failed.close_count, 1)
        self.assertEqual(device.ssh_pending, 0)
        self.assertEqual(device.ssh_sessions, {})
        with mock.patch.object(seriald, 'SSH_LIMIT', 1):
            session, error = device.ssh_connect()
        self.assertEqual(error, '')
        self.assertNotEqual(session.sid, 'ssh1')
        self.assertIs(device.get_session(session.sid), session)

    def test_default_password_falls_back_without_changing_board(self):
        with mock.patch.object(seriald, 'check_password_login',
                               side_effect=[(False, True, 'SSH 认证失败'), (True, False, '')]) as check, \
                mock.patch.object(seriald, 'SshSession', side_effect=FakeSession) as opening, \
                mock.patch.object(self.device, 'configure_root_ssh') as configure, \
                mock.patch.object(self.device, '_write') as write:
            session, error = self.device.ssh_connect()
        self.assertEqual(error, '')
        self.assertIsNotNone(session)
        self.assertEqual(check.call_args_list,
                         [mock.call('192.0.2.1', 'root', 'bianbu'),
                          mock.call('192.0.2.1', 'root', 'debian')])
        self.assertEqual(opening.call_args.kwargs, {'user': 'root', 'password': 'debian'})
        self.assertFalse(session.auto_configured)
        configure.assert_not_called()
        write.assert_not_called()

    def test_all_authentication_failures_offer_manual_configuration_without_serial_writes(self):
        with mock.patch.object(seriald, 'check_password_login',
                               return_value=(False, True, 'SSH 认证失败')) as check, \
                mock.patch.object(seriald, 'SshSession') as opening, \
                mock.patch.object(self.device, 'configure_root_ssh') as configure, \
                mock.patch.object(self.device, '_write') as write:
            with self.assertRaises(seriald.SSHAuthenticationError) as caught:
                self.device.ssh_connect()
        self.assertEqual(caught.exception.user, 'root')
        self.assertNotIn('bianbu', str(caught.exception))
        self.assertNotIn('debian', str(caught.exception))
        self.assertEqual(check.call_count, 2)
        opening.assert_not_called()
        configure.assert_not_called()
        write.assert_not_called()
        self.assertEqual(self.device.ssh_pending, 0)
        self.assertEqual(self.device.ssh_sessions, {})

    def test_network_failure_never_tries_another_password(self):
        with mock.patch.object(seriald, 'check_password_login',
                               return_value=(False, False, 'SSH 连接超时')) as check, \
                mock.patch.object(self.device, 'configure_root_ssh') as configure:
            session, error = self.device.ssh_connect()
            self.assertIsNone(session)
            self.assertEqual(error, 'SSH 连接超时')
        check.assert_called_once_with('192.0.2.1', 'root', 'bianbu')
        configure.assert_not_called()

    def test_explicit_user_or_password_is_tried_once(self):
        for credentials in ({'ssh_user': 'ubuntu', 'ssh_password': 'different'},
                            {'ssh_user': 'root'}, {'ssh_password': 'bianbu'}):
            with self.subTest(credentials=list(credentials)), \
                    mock.patch.object(seriald, 'check_password_login',
                                      return_value=(False, True, 'denied')) as check, \
                    mock.patch.object(self.device, 'configure_root_ssh') as configure:
                with self.assertRaises(seriald.SSHAuthenticationError):
                    self.device.ssh_connect(**credentials)
                self.assertEqual(check.call_count, 1)
                configure.assert_not_called()
                self.assertEqual(self.device.ssh_pending, 0)

    def test_profile_password_order_is_deduplicated_for_non_root_users_too(self):
        for configured, expected in (('private', ['private', 'bianbu', 'debian']),
                                     ('bianbu', ['bianbu', 'debian']),
                                     ('debian', ['debian', 'bianbu'])):
            with self.subTest(configured=configured), \
                    mock.patch.object(seriald.Device, 'profile', new_callable=mock.PropertyMock,
                                      return_value=BoardProfile(ssh_user='ubuntu', ssh_password=configured,
                                                               auto_configure_ssh=False)), \
                    mock.patch.object(seriald, 'check_password_login',
                                      return_value=(False, True, 'denied')) as check:
                with self.assertRaises(seriald.SSHAuthenticationError) as caught:
                    self.device.ssh_connect()
                self.assertEqual(caught.exception.user, 'ubuntu')
                self.assertEqual(check.call_args_list,
                                 [mock.call('192.0.2.1', 'ubuntu', value) for value in expected])
                self.assertEqual(self.device.ssh_pending, 0)

    def test_replug_during_authentication_stops_before_retrying_credentials(self):
        def deny_and_replug(*args):
            self.device.connection_id = 'replacement'
            return False, True, 'denied'
        with mock.patch.object(seriald, 'check_password_login', side_effect=deny_and_replug) as check:
            session, error = self.device.ssh_connect()
        self.assertIsNone(session)
        self.assertIn('重新枚举', error)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(self.device.ssh_pending, 0)

    def test_task_cancellation_during_authentication_prevents_further_attempts(self):
        cancelled = threading.Event()
        def deny_and_cancel(*args):
            cancelled.set()
            return False, True, 'denied'
        with self.device.transactions.task_scope(cancelled, self.device.connection_id, self.device.stop), \
                mock.patch.object(seriald, 'check_password_login', side_effect=deny_and_cancel) as check:
            with self.assertRaises(TransactionError) as caught:
                self.device.ssh_connect()
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(check.call_count, 1)
        self.assertEqual(self.device.ssh_pending, 0)

    def test_transport_error_does_not_expose_an_attempted_password(self):
        for failure in ((False, False, 'server echoed secret-value'), RuntimeError('secret-value')):
            with self.subTest(exception=isinstance(failure, Exception)):
                options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with mock.patch.object(seriald, 'check_password_login', **options):
                    session, error = self.device.ssh_connect(ssh_password='secret-value')
                self.assertIsNone(session)
                self.assertNotIn('secret-value', error)
                self.assertIn('<redacted>', error)

    def test_named_agent_reuses_live_persistent_session(self):
        device = self.device
        first, error = device.ssh_connect(actor='claude-code/debug', title='pwd')
        self.assertEqual(error, '')
        first.fd = 101  # FakeSession does not spawn a PTY itself.
        self.assertTrue(first.persistent)
        second, error = device.ssh_connect(actor='claude-code/debug', title='uname -a')
        self.assertEqual(error, '')
        self.assertIs(second, first)
        self.assertEqual(first.title, 'uname -a')
        self.assertEqual(len(device.ssh_sessions), 1)

    def test_persistent_agent_session_is_not_reaped_for_idle(self):
        device = self.device
        session, error = device.ssh_connect(actor='agent', title='long-lived')
        self.assertEqual(error, '')
        session.fd = 101
        session.last_touch = 0
        device.reap_sessions()
        self.assertIs(device.get_session(session.sid), session)
        self.assertEqual(session.close_count, 0)

    def test_reaper_claims_under_cv_then_disposes_outside_registry_lock(self):
        device = self.device
        fresh = FakeSession('ssh-fresh')
        expired = FakeSession('ssh-idle')
        ended = FakeSession('ssh-ended')
        fresh.last_touch = ended.last_touch = 999
        expired.last_touch = 900
        ended.closed = True
        device.ssh_sessions = {s.sid: s for s in (fresh, expired, ended)}
        checks = []

        for session in (expired, ended):
            session.on_idle = lambda s=session: self.assertTrue(s.cv._is_owned())

            def disposal(s=session):
                self.assertTrue(s.closed)
                self.assertTrue(s._stop.is_set())
                self.assertNotIn(s.sid, device.ssh_sessions)
                acquired = threading.Event()

                def lock_elsewhere():
                    with device.sessions_lock:
                        acquired.set()

                thread = threading.Thread(target=lock_elsewhere)
                thread.start()
                try:
                    self.assertTrue(acquired.wait(1), 'disposal must run outside registry lock')
                finally:
                    thread.join(1)
                checks.append(s.sid)

            session.on_close = disposal
        with mock.patch.object(seriald.time, 'monotonic', return_value=1000), \
                mock.patch.object(seriald, 'SSH_IDLE', 60):
            device.reap_sessions()
        self.assertEqual(checks, ['ssh-idle', 'ssh-ended'])
        self.assertEqual(device.ssh_sessions, {'ssh-fresh': fresh})
        self.assertEqual(fresh.close_count, 0)
        self.assertEqual(expired.close_count, 1)
        self.assertEqual(ended.close_count, 1)

    def test_concurrent_list_and_close_uses_snapshot_and_disposes_once(self):
        device = self.device
        first, second = FakeSession('ssh1'), FakeSession('ssh2')
        device.ssh_sessions = {'ssh1': first, 'ssh2': second}
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def delayed_snapshot():
            entered.set()
            if not release.wait(3):
                raise AssertionError('snapshot did not resume')

        def list_sessions():
            try:
                results.extend(device.list_sessions())
            except BaseException as error:
                errors.append(error)

        first.on_snapshot = delayed_snapshot
        thread = threading.Thread(target=list_sessions)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(device.close_session('ssh1'))
            self.assertTrue(device.close_session('ssh2'))
            self.assertFalse(device.close_session('ssh1'))
            self.assertIsNone(device.get_session('ssh2'))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{'sid': 'ssh1', 'alive': False}, {'sid': 'ssh2', 'alive': False}])
        self.assertEqual(device.list_sessions(), [])
        self.assertEqual(first.close_count, 1)
        self.assertEqual(second.close_count, 1)

    def test_probe_result_cannot_overwrite_reenumerated_device(self):
        device = self.device
        current = {'ips': ['eth0 192.0.2.2/24'], 'serial': 'new-board'}
        device.probe_info = current
        device.hostname = 'new-board'

        def finished_old_command(*args, **kwargs):
            # 模拟命令锁释放后,Manager 已把同一 by-path 交给新采集线程。
            device.stop = threading.Event()
            return True, ('HOST=old-board\nSERIAL=old-serial\nMODEL=old-model\n'
                          'IP=eth0 192.0.2.1/24\nSSHD=active\n')

        with mock.patch.object(device, '_run_cmd', side_effect=finished_old_command), \
                mock.patch.object(device, '_say') as say:
            result = device.run_probe()
        self.assertFalse(result['ok'])
        self.assertIn('重新枚举', result['error'])
        self.assertIs(device.probe_info, current)
        self.assertEqual(device.hostname, 'new-board')
        say.assert_not_called()

    def test_probe_result_cannot_publish_after_capture_stops(self):
        device = self.device
        current = device.probe_info

        def stopped_command(*args, **kwargs):
            device.stop.set()
            return True, 'HOST=old\nSERIAL=old\nMODEL=old\nSSHD=active\n'

        with mock.patch.object(device, '_run_cmd', side_effect=stopped_command), \
                mock.patch.object(device, '_say') as say:
            result = device.run_probe()
        self.assertFalse(result['ok'])
        self.assertIs(device.probe_info, current)
        say.assert_not_called()


class SavedSshCredentialTests(unittest.TestCase):
    """Verified credentials belong to a board, never a port or an old request."""
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.manager = types.SimpleNamespace(store=self.store, profiles=ProfileBook({
            'profiles': {'test': {'ssh_user': 'root', 'ssh_password': 'profile-secret'}},
            'default': 'test'}))
        patches = [
            mock.patch.object(seriald.Device, 'preload'),
            mock.patch.object(seriald.Device, 'stable_dir', return_value='/unused'),
            mock.patch.object(seriald, 'SshSession', side_effect=FakeSession),
            mock.patch.object(seriald, 'check_password_login', return_value=(True, False, '')),
            mock.patch.object(seriald.os, 'open', side_effect=AssertionError('hardware forbidden')),
            mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('SSH forbidden')),
        ]
        self.opening = patches[2].start()
        self.check = patches[3].start()
        self.addCleanup(patches[2].stop)
        self.addCleanup(patches[3].stop)
        for patch in patches[:2] + patches[4:]:
            patch.start()
            self.addCleanup(patch.stop)
        self.device = self.device_for('SN1')

    def device_for(self, serial, path='test-usb', ip='192.0.2.1'):
        info = {'serial': serial, 'serial_source': 'device-tree',
                'ips': ['eth0 ' + ip + '/24']}
        record = self.store.bind(info, path, '/fake/tty')
        device = seriald.Device(path, '/fake/tty', self.manager, record=record)
        device.present, device.fd = True, 98765
        self.store.bind(info, path, '/fake/tty', connection_id=device.connection_id)
        return device

    def remember(self, user='ubuntu', password='remembered-private-password'):
        self.assertTrue(self.store.save_ssh_credentials(
            'SN1', user, password, self.device.connection_id, 0))
        return self.store.get_ssh_credentials('SN1')

    def test_successful_manual_login_is_saved_and_reused_after_port_and_ip_change(self):
        session, error = self.device.ssh_connect(ssh_user='ubuntu', ssh_password='chosen-private-password')
        self.assertEqual(error, '')
        self.assertTrue(session.ssh_credentials_saved)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), {
            'ssh_user': 'ubuntu', 'ssh_password': 'chosen-private-password', 'revision': 1})
        replugged = self.device_for('SN1', path='other-usb-port', ip='192.0.2.99')
        self.check.reset_mock()
        with mock.patch.object(self.store, 'save_ssh_credentials', wraps=self.store.save_ssh_credentials) as save:
            reused, error = replugged.ssh_connect()
        self.assertEqual(error, '')
        self.check.assert_called_once_with('192.0.2.99', 'ubuntu', 'chosen-private-password')
        self.assertEqual(self.opening.call_args.kwargs,
                         {'user': 'ubuntu', 'password': 'chosen-private-password'})
        self.assertTrue(reused.ssh_credentials_saved)
        save.assert_not_called()
        self.assertEqual(self.store.get_ssh_credentials('SN1')['revision'], 1)

    def test_a_different_board_on_the_same_port_does_not_receive_old_password(self):
        remembered = self.remember()
        other = self.device_for('SN2')
        session, error = other.ssh_connect()
        self.assertEqual(error, '')
        self.check.assert_called_once_with('192.0.2.1', 'root', 'profile-secret')
        self.assertTrue(session.ssh_credentials_saved)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), remembered)
        self.assertEqual(self.store.get_ssh_credentials('SN2')['ssh_password'], 'profile-secret')

    def test_default_fallback_remembers_the_password_that_really_authenticated(self):
        self.check.side_effect = [(False, True, 'denied'), (False, True, 'denied'), (True, False, '')]
        session, error = self.device.ssh_connect()
        self.assertEqual(error, '')
        self.assertEqual(self.check.call_args_list, [
            mock.call('192.0.2.1', 'root', 'profile-secret'),
            mock.call('192.0.2.1', 'root', 'bianbu'),
            mock.call('192.0.2.1', 'root', 'debian')])
        self.assertTrue(session.ssh_credentials_saved)
        self.assertEqual(self.store.get_ssh_credentials('SN1')['ssh_password'], 'debian')

    def test_rejected_saved_password_does_not_try_defaults_or_replace_the_setting(self):
        remembered = self.remember()
        self.check.return_value = False, True, 'denied'
        with self.assertRaises(seriald.SSHAuthenticationError) as caught:
            self.device.ssh_connect()
        self.assertEqual(caught.exception.user, 'ubuntu')
        self.check.assert_called_once_with('192.0.2.1', 'ubuntu', remembered['ssh_password'])
        self.opening.assert_not_called()
        self.assertEqual(self.store.get_ssh_credentials('SN1'), remembered)
        self.assertEqual(self.device.ssh_pending, 0)

    def test_manual_replacement_is_saved_only_after_verified_connection(self):
        remembered = self.remember()
        self.check.return_value = False, True, 'denied'
        with self.assertRaises(seriald.SSHAuthenticationError):
            self.device.ssh_connect(ssh_user='debian', ssh_password='rejected-private-password')
        self.assertEqual(self.store.get_ssh_credentials('SN1'), remembered)
        self.check.return_value = True, False, ''
        failed = FakeSession('failed')
        failed.on_ensure = lambda: False
        self.opening.side_effect = None
        self.opening.return_value = failed
        session, error = self.device.ssh_connect(ssh_user='debian', ssh_password='not-opened-private-password')
        self.assertIsNone(session)
        self.assertIn('失败', error)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), remembered)
        self.opening.side_effect = FakeSession
        session, error = self.device.ssh_connect(ssh_user='debian', ssh_password='replacement-private-password')
        self.assertEqual(error, '')
        self.assertTrue(session.ssh_credentials_saved)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), {
            'ssh_user': 'debian', 'ssh_password': 'replacement-private-password', 'revision': 2})

    def test_cancelled_task_after_transport_open_never_remembers_credentials(self):
        cancelled = threading.Event()
        session = FakeSession('cancelled')
        session.on_ensure = lambda: cancelled.set() or True
        self.opening.side_effect = None
        self.opening.return_value = session
        with self.device.transactions.task_scope(cancelled, self.device.connection_id, self.device.stop):
            with self.assertRaises(TransactionError) as caught:
                self.device.ssh_connect(ssh_user='ubuntu', ssh_password='cancelled-private-password')
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertTrue(session.closed)
        self.assertEqual(self.device.ssh_sessions, {})
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))
        self.assertEqual(self.device.ssh_pending, 0)

    def test_replug_or_identity_change_after_transport_open_never_saves(self):
        for changed in ('connection', 'serial'):
            with self.subTest(changed=changed):
                device = self.device_for('SN1')
                session = FakeSession('stale-' + changed)
                def change():
                    if changed == 'connection':
                        device.connection_id = 'replacement-connection'
                    else:
                        other = self.store.bind({'serial': 'SN2'}, 'other', '/fake/tty')
                        device.board = Board(other)
                    return True
                session.on_ensure = change
                self.opening.side_effect = None
                self.opening.return_value = session
                result, error = device.ssh_connect(ssh_user='ubuntu', ssh_password='old-request-private-password')
                self.assertIsNone(result)
                self.assertIn('重新枚举', error)
                self.assertTrue(session.closed)
                self.assertEqual(device.ssh_sessions, {})
                self.assertIsNone(self.store.get_ssh_credentials('SN1'))
                self.assertIsNone(self.store.get_ssh_credentials('SN2'))
                self.assertEqual(device.ssh_pending, 0)

    def test_slow_manual_connection_cannot_replace_newer_saved_configuration(self):
        self.remember()
        session = FakeSession('slower')
        def newer_setting():
            self.assertTrue(self.store.save_ssh_credentials(
                'SN1', 'newer-user', 'newer-private-password', self.device.connection_id, 1))
            return True
        session.on_ensure = newer_setting
        self.opening.side_effect = None
        self.opening.return_value = session
        result, error = self.device.ssh_connect(ssh_user='older-user', ssh_password='older-private-password')
        self.assertEqual(error, '')
        self.assertIs(result, session)
        self.assertFalse(session.closed)
        self.assertFalse(session.ssh_credentials_saved)
        self.assertIn('配置或设备身份已变化', session.ssh_credentials_note)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), {
            'ssh_user': 'newer-user', 'ssh_password': 'newer-private-password', 'revision': 2})

    def test_saved_reconnection_does_not_block_a_newer_successful_manual_login(self):
        self.remember()
        reconnect = FakeSession('reconnect')
        replacement = FakeSession('replacement')
        def manual_replacement():
            self.opening.side_effect = None
            self.opening.return_value = replacement
            updated, error = self.device.ssh_connect(
                ssh_user='newer-user', ssh_password='newer-private-password')
            self.assertEqual(error, '')
            self.assertIs(updated, replacement)
            self.assertTrue(updated.ssh_credentials_saved)
            return True
        reconnect.on_ensure = manual_replacement
        self.opening.side_effect = None
        self.opening.return_value = reconnect
        with mock.patch.object(self.store, 'save_ssh_credentials', wraps=self.store.save_ssh_credentials) as save:
            connected, error = self.device.ssh_connect()
        self.assertEqual(error, '')
        self.assertIs(connected, reconnect)
        self.assertFalse(reconnect.closed)
        self.assertFalse(reconnect.ssh_credentials_saved)
        self.assertIn('配置或设备身份已变化', reconnect.ssh_credentials_note)
        save.assert_called_once_with('SN1', 'newer-user', 'newer-private-password',
                                     self.device.connection_id, 1)
        self.assertEqual(self.store.get_ssh_credentials('SN1'), {
            'ssh_user': 'newer-user', 'ssh_password': 'newer-private-password', 'revision': 2})

    def test_storage_failure_keeps_working_ssh_and_does_not_publish_raw_error(self):
        with mock.patch.object(self.store, 'save_ssh_credentials',
                               side_effect=RuntimeError('SQL contained chosen-private-password')):
            session, error = self.device.ssh_connect(ssh_user='ubuntu', ssh_password='chosen-private-password')
        self.assertEqual(error, '')
        self.assertFalse(session.closed)
        self.assertIs(self.device.get_session(session.sid), session)
        self.assertFalse(session.ssh_credentials_saved)
        self.assertEqual(session.ssh_credentials_note, 'SSH 已连接，但凭据保存失败，请检查服务端存储')
        self.assertNotIn('chosen-private-password', session.ssh_credentials_note)
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))

    def test_failed_read_stops_default_authentication_but_allows_explicit_connection(self):
        with mock.patch.object(self.store, 'get_ssh_credentials',
                               side_effect=RuntimeError('SQL contained stored-private-password')):
            session, error = self.device.ssh_connect()
            self.assertIsNone(session)
            self.assertEqual(error, '无法读取已保存 SSH 配置，请检查服务端存储')
            self.check.assert_not_called()
            session, error = self.device.ssh_connect(ssh_user='ubuntu', ssh_password='explicit-private-password')
        self.assertEqual(error, '')
        self.check.assert_called_once_with('192.0.2.1', 'ubuntu', 'explicit-private-password')
        self.assertFalse(session.ssh_credentials_saved)
        self.assertEqual(session.ssh_credentials_note, 'SSH 已连接，但服务端凭据存储不可用')
        self.assertIsNone(self.store.get_ssh_credentials('SN1'))

    def test_agent_can_reuse_or_override_without_changing_saved_credentials(self):
        remembered = self.remember()
        with mock.patch.object(self.store, 'save_ssh_credentials', wraps=self.store.save_ssh_credentials) as save:
            session, error = self.device.ssh_connect(actor='agent-one')
            self.assertEqual(error, '')
            self.assertTrue(session.ssh_credentials_saved)
            self.check.assert_called_once_with('192.0.2.1', 'ubuntu', remembered['ssh_password'])
            self.check.reset_mock()
            session, error = self.device.ssh_connect(actor='agent-two', ssh_user='temporary-user',
                                                      ssh_password='temporary-private-password')
            self.assertEqual(error, '')
            self.assertFalse(session.ssh_credentials_saved)
            self.check.assert_called_once_with('192.0.2.1', 'temporary-user', 'temporary-private-password')
        save.assert_not_called()
        self.assertEqual(self.store.get_ssh_credentials('SN1'), remembered)

    def test_conflicted_serial_does_not_use_or_replace_saved_credentials(self):
        remembered = self.remember()
        self.store.mark_conflict('SN1')
        session, error = self.device.ssh_connect()
        self.assertEqual(error, '')
        self.check.assert_called_once_with('192.0.2.1', 'root', 'profile-secret')
        self.assertFalse(session.ssh_credentials_saved)
        self.assertIn('配置或设备身份已变化', session.ssh_credentials_note)
        raw = self.store.db.execute('SELECT ssh_user,ssh_password,revision FROM ssh_credentials WHERE serial=?',
                                    ('SN1',)).fetchone()
        self.assertEqual(dict(raw), remembered)

    def test_unidentified_connection_never_persists_by_port_or_ip(self):
        device = seriald.Device('test-usb', '/fake/tty', self.manager)
        device.probe_info = {'ips': ['eth0 192.0.2.1/24']}
        with mock.patch.object(device, 'run_probe', return_value={'ok': True}), \
                mock.patch.object(self.store, 'save_ssh_credentials', wraps=self.store.save_ssh_credentials) as save:
            session, error = device.ssh_connect(ssh_user='ubuntu', ssh_password='temporary-private-password')
        self.assertEqual(error, '')
        self.assertFalse(session.ssh_credentials_saved)
        self.assertEqual(session.ssh_credentials_note, '设备尚未识别，SSH 凭据仅用于本次连接')
        save.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ssh_credentials').fetchone()[0], 0)

    def test_public_snapshot_contains_only_saved_state_and_username(self):
        self.remember()
        snapshot = self.device.snapshot()
        self.assertEqual(snapshot['ssh_settings'], {'saved': True, 'user': 'ubuntu'})
        self.assertNotIn('remembered-private-password', json.dumps(snapshot))
        self.assertNotIn('ssh_password', json.dumps(snapshot))
        self.assertNotIn('revision', snapshot['ssh_settings'])


if __name__ == '__main__':
    unittest.main()
