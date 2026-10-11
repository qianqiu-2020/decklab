"""SSH 生命周期只用内存和假 fd 验证,绝不接触真实串口或 SSH。"""
import errno
import io
import threading
import unittest
from unittest import mock

from seriald_core import sessions as seriald
from seriald_core import session_base

REAL_OPEN_LOG = seriald.SshSession._open_log


class MemoryLog(io.StringIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class SshSessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.patches = [
            mock.patch.object(seriald.SshSession, '_open_log'),
            mock.patch.object(seriald.os, 'close'),
            mock.patch.object(seriald.os, 'killpg'),
            mock.patch.object(seriald.pty, 'openpty', side_effect=AssertionError('real pty forbidden')),
            mock.patch.object(seriald.subprocess, 'Popen', side_effect=AssertionError('real SSH forbidden')),
            mock.patch.object(seriald.os, 'set_blocking'),
            mock.patch.object(seriald.fcntl, 'ioctl'),
            mock.patch.object(seriald.os, 'makedirs'),
        ]
        self.mocks = [p.start() for p in self.patches]

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for patch in reversed(self.patches):
            patch.stop()

    def session(self):
        session = seriald.SshSession('ssh1', '192.0.2.1', 'test', '/unused')
        session.logf = MemoryLog()
        self.sessions.append(session)
        return session

    def test_utf8_and_lines_survive_arbitrary_read_boundaries(self):
        session = self.session()
        log = session.logf
        for byte in '中文密码：\r\n尾行'.encode('utf-8'):
            session._append(bytes([byte]))
        session.close()
        self.assertIn('中文密码：\n', log.saved)
        self.assertIn('尾行\n', log.saved)
        self.assertNotIn('\ufffd', log.saved)
        self.assertTrue(log.closed)

    def test_log_write_failure_is_visible_and_does_not_stop_live_output(self):
        session = self.session()
        session.logf = mock.Mock()
        session.logf.write.side_effect = OSError('disk full')
        session._append(b'visible\n')
        self.assertEqual(bytes(session.buf), b'visible\n')
        self.assertIn('disk full', session.snapshot()['log_error'])
        self.assertIsNone(session.logf)

    def test_write_never_starts_or_restarts_session(self):
        session = self.session()
        with mock.patch.object(session, 'ensure', side_effect=AssertionError('implicit restart')):
            self.assertFalse(session.write('hello'))
            session.close()
            self.assertFalse(session.write('again'))
        self.assertFalse(session.ensure())
        self.mocks[3].assert_not_called()
        self.mocks[4].assert_not_called()

    def test_reflashed_board_host_key_does_not_use_persistent_known_hosts(self):
        session = self.session()
        self.assertIn('StrictHostKeyChecking=no', session.argv)
        self.assertIn('UserKnownHostsFile=/dev/null', session.argv)
        self.assertIn('GlobalKnownHostsFile=/dev/null', session.argv)

    def test_request_credentials_select_normal_ubuntu_user(self):
        session = seriald.SshSession('ssh1', '192.0.2.1', 'test', '/unused',
                                     user='ubuntu', password='board-secret')
        self.sessions.append(session)
        self.assertEqual(session.argv[-1], 'ubuntu@192.0.2.1')
        with mock.patch.object(seriald.pty, 'openpty', return_value=(101, 102)), \
                mock.patch.object(seriald.subprocess, 'Popen', return_value=mock.Mock(pid=123)) as spawn:
            spawn.return_value.wait.return_value = 0
            self.assertTrue(session.ensure())
            self.assertEqual(spawn.call_args.kwargs['env']['SSHPASS'], 'board-secret')

    def test_password_preflight_distinguishes_denial_from_network_error(self):
        with mock.patch.object(seriald.subprocess, 'run',
                               return_value=mock.Mock(returncode=5, stderr=b'Permission denied')) as run:
            self.assertEqual(seriald.check_password_login('192.0.2.1', 'root', 'bianbu'),
                             (False, True, 'SSH 认证失败'))
        self.assertEqual(run.call_args.kwargs['env']['SSHPASS'], 'bianbu')
        self.assertNotIn('bianbu', run.call_args.args[0])
        with mock.patch.object(seriald.subprocess, 'run',
                               return_value=mock.Mock(returncode=255, stderr=b'Connection refused')):
            self.assertEqual(seriald.check_password_login('192.0.2.1', 'root', 'bianbu'),
                             (False, False, 'Connection refused'))

    def test_long_session_rotates_log_at_date_boundary(self):
        session = self.session()
        old_log = session.logf
        new_log = MemoryLog()
        session._logday = '20260101'
        with mock.patch.object(session_base.time, 'strftime', return_value='20260102'), \
                mock.patch('builtins.open', return_value=new_log) as opened, \
                mock.patch.object(session, '_open_log',
                                  side_effect=lambda path: REAL_OPEN_LOG(session, path)):
            session._append('新一天\n')
            session.close()
        self.assertTrue(old_log.closed)
        self.assertTrue(new_log.closed)
        self.assertIn('新一天', new_log.saved)
        self.assertEqual(opened.call_args.args[0], '/unused/ssh-ssh1-20260102.log')

    def test_spawn_failure_closes_both_fds_and_log_once(self):
        session = self.session()
        log = session.logf
        with mock.patch.object(seriald.pty, 'openpty', return_value=(101, 102)), \
                mock.patch.object(seriald.subprocess, 'Popen', side_effect=OSError('no sshpass')):
            self.assertFalse(session.ensure())
        session.close()
        self.assertEqual(self.mocks[1].call_args_list, [mock.call(101), mock.call(102)])
        self.assertTrue(log.closed)
        self.assertTrue(session.closed)
        self.assertTrue(session._disposed.is_set())
        self.assertFalse(session.snapshot()['alive'])

    def test_reader_eof_reaps_process_and_closes_resources(self):
        session = self.session()
        log = session.logf
        proc = mock.Mock(pid=12345)
        proc.wait.return_value = 0
        with mock.patch.object(seriald.pty, 'openpty', return_value=(101, 102)), \
                mock.patch.object(seriald.subprocess, 'Popen', return_value=proc) as spawn, \
                mock.patch.object(seriald.select, 'select', return_value=([101], [], [])), \
                mock.patch.object(seriald.os, 'read', side_effect=[b'hello\n', b'']):
            self.assertTrue(session.ensure())
            session._thread.join(timeout=2)
            self.assertFalse(session._thread.is_alive())
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertNotIn('preexec_fn', spawn.call_args.kwargs)
            self.assertFalse(session.ensure())
            spawn.assert_called_once()
        self.assertEqual(sorted(c.args[0] for c in self.mocks[1].call_args_list), [101, 102])
        proc.wait.assert_called_once_with(timeout=1.0)
        self.assertTrue(log.closed)
        self.assertIn('hello', log.saved)
        self.assertFalse(session.read(0)[2])

    def test_close_escalates_and_waits_for_term_resistant_child(self):
        session = self.session()
        session.fd = 101
        proc = session.proc = mock.Mock(pid=12345)
        proc.wait.side_effect = [seriald.subprocess.TimeoutExpired('fake-ssh', 1), 0]
        session.close()
        session.close()
        self.assertEqual(self.mocks[2].call_args_list,
                         [mock.call(12345, 15), mock.call(12345, 9)])
        self.mocks[1].assert_called_once_with(101)
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertTrue(session._disposed.is_set())

    def test_ordered_writes_do_not_block_output_read_lock(self):
        session = self.session()
        session.fd = 101
        entered = threading.Event()
        release = threading.Event()
        written = []

        def fake_write(fd, data, **kwargs):
            if data == b'A':
                entered.set()
                self.assertTrue(release.wait(2))
            written.append(data)

        results = []
        with mock.patch.object(seriald, 'write_all', side_effect=fake_write):
            first = threading.Thread(target=lambda: results.append(session.write('A')))
            second = threading.Thread(target=lambda: results.append(session.write('B')))
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            acquired = session.cv.acquire(timeout=1)
            try:
                self.assertTrue(acquired, 'slow writes must not hold output cv')
                session._append(b'output\n')
                self.assertIn(b'output', session.read(0)[1])
                self.assertEqual(written, [])
            finally:
                if acquired:
                    session.cv.release()
                release.set()
                first.join(2)
                second.join(2)
        self.assertEqual(written, [b'A', b'B'])
        self.assertEqual(results, [True, True])

    def test_partial_writes_are_completed_in_order(self):
        session = self.session()
        session.fd = 101
        sent = []

        def short_write(fd, data):
            sent.append(bytes(data[:1]))
            return 1

        with mock.patch.object(seriald.os, 'write', side_effect=short_write):
            self.assertTrue(session.write('中文'))
        self.assertEqual(b''.join(sent), '中文'.encode('utf-8'))

    def test_close_cancels_writer_then_joins_reader_before_fd_reuse(self):
        session = self.session()
        session.fd = 101
        proc = session.proc = mock.Mock(pid=12345)
        entered = threading.Event()
        results = []

        def blocked_write(fd, data, cancel=None):
            entered.set()
            self.assertTrue(cancel.wait(2))
            raise OSError(errno.ECANCELED, 'closed')

        def idle_select(*args):
            session._stop.wait(0.01)
            return [], [], []

        with mock.patch.object(seriald, 'write_all', side_effect=blocked_write), \
                mock.patch.object(seriald.select, 'select', side_effect=idle_select):
            session._thread = threading.Thread(target=session._reader, args=(101,))
            session._thread.start()
            writer = threading.Thread(target=lambda: results.append(session.write('A')))
            writer.start()
            self.assertTrue(entered.wait(2))
            session.close()
            writer.join(2)
        self.assertFalse(session._thread.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(results, [False])
        self.mocks[1].assert_called_once_with(101)
        self.assertTrue(session._disposed.is_set())

    def test_idle_uses_monotonic_activity_and_closed_state(self):
        session = self.session()
        with mock.patch.object(session_base.time, 'monotonic', return_value=100):
            session.read(0)
        self.assertFalse(session.is_idle(109, 10))
        self.assertTrue(session.is_idle(110, 10))
        session.close()
        self.assertTrue(session.is_idle(100, 10))


    def test_linux_pty_eio_is_clean_eof(self):
        session = self.session()
        with mock.patch.object(seriald.select, 'select', return_value=([101], [], [])), \
                mock.patch.object(seriald.os, 'read', side_effect=OSError(errno.EIO, 'PTY EOF')):
            self.assertEqual(session._receive(101), b'')
        self.assertEqual(session.transport_error, '')


if __name__ == '__main__':
    unittest.main()
