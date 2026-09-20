"""会话注册表并发回归:假 SSH、假目录,不打开串口或启动子进程。"""
import subprocess
import threading
import time
import unittest
from unittest import mock

from seriald_core import device as seriald


class FakeSession:
    def __init__(self, sid, ip='192.0.2.1', label='test', logdir='/unused'):
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

        def factory(*args):
            session = FakeSession(*args)
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


if __name__ == '__main__':
    unittest.main()
