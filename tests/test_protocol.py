"""控制台协议测试:只向替身写入,完整命令仅作 bash -n 语法检查。"""
import importlib.util
import pathlib
import re
import subprocess
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
from seriald_core import device as sd
from seriald_core.protocol import SSH_STATE_CMD

NONCE = '0123456789abcdef01234567'
BEGIN = '__SD_B_' + NONCE + '__'
END = '__SD_E_' + NONCE + '__'
BODY = ('HOST=board-test\nSERIAL=serial-123\nMODEL=mock board\n'
        'IP=eth0 192.0.2.10/24\nSSHD=active\n')


class DeviceFixture(unittest.TestCase):
    def setUp(self):
        # preload 也替换掉,不创建日志目录、不读取正在采集的真实缓冲。
        with mock.patch.object(sd.Device, 'preload'):
            self.device = sd.Device('mock-by-path', '/mock/ttyUSB0')
        self.device.fd = 98765
        self.device.present = True
        self.device.newline = '\r'         # 协议测试已模拟完成换行探测
        patcher = mock.patch.object(sd, 'write_serial',
                                    side_effect=AssertionError('unexpected hardware write'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_frame(self, response, command=sd.PROBE_CMD, write_result=(True, '')):
        def wait(base, pattern, timeout):
            return re.search(pattern, sd.ANSI.sub('', response))

        with mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_write', return_value=write_result) as write, \
                mock.patch.object(self.device, '_wait', side_effect=wait) as wait_mock, \
                mock.patch.object(self.device, '_since', return_value=response), \
                mock.patch.object(sd.secrets, 'token_hex', return_value=NONCE):
            result = self.device._run_cmd(command, timeout=0.01)
        self.assertFalse(self.device.probing)
        self.assertTrue(self.device.operation_lock.acquire(blocking=False))
        self.device.operation_lock.release()
        return result, write, wait_mock


class FramingTests(DeviceFixture):
    def test_complete_matching_frame_extracts_only_its_body(self):
        response = ('command echo\r\n' + BEGIN + '\r\n' + BODY.replace('\n', '\r\n')
                    + END + '\r\nroot@board:# ')
        (ok, raw), write, wait = self.run_frame(response)
        self.assertTrue(ok, raw)
        self.assertEqual(raw, BODY)
        command = write.call_args.args[0]
        self.assertNotIn(BEGIN, command)
        self.assertNotIn(END, command)
        self.assertEqual(command.count(NONCE), 2)
        self.assertTrue(command.endswith(self.device.newline))
        self.assertEqual(wait.call_count, 1)

    def test_missing_begin_marker_rejects_frame(self):
        (ok, error), _, wait = self.run_frame(BODY + END + '\n')
        self.assertFalse(ok)
        self.assertIn('帧不完整', error)
        self.assertEqual(wait.call_count, 1)

    def test_partial_end_marker_and_missing_newline_do_not_complete_wait(self):
        for ending in (END[:-1], END, END + '\r', END + '-extra\n'):
            with self.subTest(ending=ending):
                (ok, error), _, _ = self.run_frame(BEGIN + '\n' + BODY + ending)
                self.assertFalse(ok)
                self.assertIn('超时', error)

    def test_wrong_nonce_and_inline_end_marker_do_not_complete_wait(self):
        wrong = END.replace(NONCE, 'f' * 24)
        for response in (BEGIN + '\n' + BODY + wrong + '\n',
                         BEGIN + '\n' + BODY + 'echo ' + END + '\n'):
            with self.subTest(response=response):
                (ok, error), _, _ = self.run_frame(response)
                self.assertFalse(ok)
                self.assertIn('超时', error)

    def test_stale_frame_is_ignored_when_matching_frame_follows(self):
        stale = (BEGIN + '\nHOST=stale\n' + END + '\n').replace(NONCE, 'f' * 24)
        (ok, raw), _, _ = self.run_frame(stale + BEGIN + '\n' + BODY + END + '\n')
        self.assertTrue(ok, raw)
        self.assertEqual(raw, BODY)

    def test_literal_old_marker_in_field_value_survives_extraction_and_parse(self):
        body = BODY.replace('mock board', 'mock __SD_E__ board')
        (ok, raw), _, _ = self.run_frame(BEGIN + '\n' + body + END + '\n')
        self.assertTrue(ok, raw)
        self.assertEqual(sd.parse_probe(raw)['model'], 'mock __SD_E__ board')

    def test_write_failure_releases_transaction_without_waiting(self):
        (ok, error), _, wait = self.run_frame('', write_result=(False, 'mock write failed'))
        self.assertFalse(ok)
        self.assertEqual(error, 'mock write failed')
        wait.assert_not_called()

    def test_wait_exception_releases_transaction(self):
        with mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_write', return_value=(True, '')), \
                mock.patch.object(self.device, '_wait', side_effect=RuntimeError('mock failure')):
            with self.assertRaisesRegex(RuntimeError, 'mock failure'):
                self.device._run_cmd('echo mock')
        self.assertFalse(self.device.probing)
        self.assertTrue(self.device.operation_lock.acquire(blocking=False))
        self.device.operation_lock.release()

    def test_framed_probe_and_ssh_commands_have_valid_shell_syntax(self):
        for cmd in (sd.PROBE_CMD, sd.SSH_CMD):
            with self.subTest(command=cmd):
                _, write, _ = self.run_frame(BEGIN + '\n' + BODY + END + '\n', command=cmd)
                # -n 只解析,绝不能执行 PROBE_CMD/SSH_CMD 或向任何 tty 写命令。
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-n'],
                                        input=write.call_args.args[0].rstrip('\r\n') + '\n',
                                        text=True, capture_output=True, timeout=2, cwd=ROOT,
                                        env={'PATH': '', 'BASH_ENV': '/dev/null'})
                self.assertEqual(result.returncode, 0, result.stderr)


class ProbeParserTests(unittest.TestCase):
    def test_valid_fields_and_duplicate_ip_filtering(self):
        body = BODY.replace('SSHD=active', 'IP=other 192.0.2.10/24\nIP=lo 127.0.0.1/8\nSSHD=active')
        result = sd.parse_probe(body)
        self.assertEqual(result['hostname'], 'board-test')
        self.assertEqual(result['serial'], 'serial-123')
        self.assertEqual(result['sshd'], 'active')
        self.assertEqual(result['ips'], ['eth0 192.0.2.10/24'])

    def test_duplicate_and_missing_required_fields_are_rejected(self):
        for field in ('HOST', 'SERIAL', 'MODEL', 'SSHD'):
            for duplicate in (False, True):
                with self.subTest(field=field, duplicate=duplicate):
                    body = (BODY + field + '=duplicate\n' if duplicate else
                            '\n'.join(line for line in BODY.splitlines()
                                      if not line.startswith(field + '=')) + '\n')
                    with self.assertRaisesRegex(ValueError, field):
                        sd.parse_probe(body)

    def test_empty_optional_values_preserve_one_field_per_key(self):
        result = sd.parse_probe('HOST=board\nSERIAL=\nMODEL=\nSSHD=unsupported-systemd\n')
        self.assertEqual(result['serial'], '')
        self.assertEqual(result['model'], '')
        self.assertEqual(result['sshd'], 'unsupported-systemd')


class ShellLoginTests(DeviceFixture):
    def login(self, tail, replies, newline='\r', write_ok=True):
        self.device.console_tail = tail
        self.device.newline = newline
        pending = iter(replies)

        def wait(base, pattern, timeout):
            response = next(pending)
            if response is None:
                return None
            self.device.buf.extend(response.encode('utf-8'))
            self.device.console_tail += response
            return re.search(pattern, sd.ANSI.sub('', response))

        with mock.patch.object(self.device, '_write', return_value=(write_ok, 'mock write')) as write, \
                mock.patch.object(self.device, '_wait', side_effect=wait) as wait_mock, \
                mock.patch.object(self.device, '_say'):
            result = self.device.ensure_shell(0)
        self.assertEqual(list(pending), [], 'scripted responses were not consumed')
        return result, [call.args[0] for call in write.call_args_list], wait_mock

    def test_live_colored_prompt_with_known_newline_needs_no_input(self):
        (ok, _), writes, wait = self.login('\x1b[32mroot@board:~# \x1b[0m\x1b[?2004h', [])
        self.assertTrue(ok)
        self.assertEqual(writes, [])
        wait.assert_not_called()

    def test_passwordless_login_does_not_send_password_as_shell_command(self):
        (ok, _), writes, _ = self.login('board login: ', ['\r\nroot@board:~# \x1b[?2004h'])
        self.assertTrue(ok)
        self.assertEqual(writes, ['root\r'])

    def test_chinese_password_prompt_and_ansi_shell_prompt(self):
        (ok, _), writes, _ = self.login('用户名：', ['\x1b[31m密码：\x1b[0m',
                                                  '\x1b[32mroot@board:# \x1b[0m'])
        self.assertTrue(ok)
        self.assertEqual(writes, ['root\r', 'bianbu\r'])

    def test_login_rejection_does_not_send_default_password(self):
        for failure in ('Login incorrect', '登录错误', '认证失败', 'Authentication failure'):
            with self.subTest(failure=failure):
                (ok, _), writes, _ = self.login('board login: ', [failure])
                self.assertFalse(ok)
                self.assertEqual(writes, ['root\r'])

    def test_password_rejection_is_reported_as_failure(self):
        (ok, _), writes, _ = self.login('board login: ', ['Password: ', 'Login incorrect'])
        self.assertFalse(ok)
        self.assertEqual(writes, ['root\r', 'bianbu\r'])

    def test_failed_username_write_does_not_wait_for_password(self):
        (ok, _), writes, wait = self.login('board login: ', [], write_ok=False)
        self.assertFalse(ok)
        self.assertEqual(writes, ['root\r'])
        wait.assert_not_called()

    def test_known_lf_connection_uses_lf_for_login_and_password(self):
        (ok, _), writes, _ = self.login('board login: ', ['Password:', 'root@board:# '], newline='\n')
        self.assertTrue(ok)
        self.assertEqual(writes, ['root\n', 'bianbu\n'])

    def test_unknown_newline_requires_fresh_prompt_even_with_cached_shell_tail(self):
        (ok, _), writes, _ = self.login('root@board:# ', [None, 'root@board:# '], newline=None)
        self.assertTrue(ok)
        self.assertEqual(writes, ['\r', '\n'])
        self.assertEqual(self.device.newline, '\n')

    def test_lf_only_login_learns_newline_before_sending_username(self):
        (ok, _), writes, _ = self.login('boot messages', [None, 'board login: ',
                                                         'root@board:# '], newline=None)
        self.assertTrue(ok)
        self.assertEqual(writes, ['\r', '\n', 'root\n'])
        self.assertEqual(self.device.newline, '\n')

    def test_real_wait_strips_ansi_from_in_memory_capture_only(self):
        self.device.buf = bytearray(b'\x1b[32mroot@board:# \x1b[0m\x1b[?2004h')
        match = self.device._wait(0, sd.RE_PROMPT, timeout=0.1)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), '# ')


class SshStateTests(unittest.TestCase):
    def test_quiet_branches_emit_exactly_one_status_using_only_shell_mocks(self):
        # 所有可能接触系统的命令都被函数替换,PATH 为空;只有 echo/test 等 builtin。
        # 不运行 sshd、systemctl、apt-get,也不执行完整 SSH_CMD。
        for installed, ssh_status, sshd_status, expected in (
                (0, 0, 0, 'unsupported-systemd'),
                (1, 0, 3, 'active'),
                (1, 3, 0, 'active'),
                (1, 3, 3, 'inactive')):
            with self.subTest(installed=installed, ssh=ssh_status, sshd=sshd_status):
                prelude = '''
command() {
  if [ "$#" -ne 2 ] || [ "$1" != -v ] || [ "$2" != systemctl ]; then
    builtin echo unexpected-command >&2; return 99
  fi
  return %d
}
systemctl() {
  if [ "$#" -ne 3 ] || [ "$1" != is-active ] || [ "$2" != --quiet ]; then
    builtin echo unexpected-systemctl >&2; return 99
  fi
  case "$3" in
    ssh) return %d ;;
    sshd) return %d ;;
    *) builtin echo unexpected-unit >&2; return 99 ;;
  esac
}
''' % (0 if installed else 1, ssh_status, sshd_status)
                result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-s'],
                                        input=prelude + SSH_STATE_CMD + '\n',
                                        text=True, capture_output=True, timeout=2, cwd=ROOT,
                                        env={'PATH': '', 'BASH_ENV': '/dev/null'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'SSHD=' + expected + '\n')
                self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
