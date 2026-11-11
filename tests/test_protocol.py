"""控制台协议测试:只向替身写入,完整命令仅作 bash -n 语法检查。"""
import importlib.util
import pathlib
import re
import os
import select
import shlex
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
from seriald_core import device as sd
from seriald_core.protocol import SSH_STATE_CMD
from seriald_core.console_protocol import frame_command
from seriald_core.profiles import BoardProfile

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
                mock.patch.object(sd.secrets, 'token_hex', return_value=NONCE), \
                mock.patch('seriald_core.transactions.time.monotonic', return_value=100.0):
            # Framing is fully mocked IO; runner scheduling must not consume
            # its artificial 10 ms deadline. Real deadline tests are separate.
            result = self.device._run_cmd(command, timeout=0.01)
        self.assertFalse(self.device.probing)
        self.assertTrue(self.device.operation_lock.acquire(blocking=False))
        self.device.operation_lock.release()
        return result, write, wait_mock


class FramingTests(DeviceFixture):
    def test_transfer_frame_does_not_repeat_shell_probe(self):
        response = BEGIN + '\nSD_OK=1\n' + END + '\n'
        with mock.patch.object(self.device, 'ensure_shell') as shell, \
                mock.patch.object(self.device, '_write', return_value=(True, '')), \
                mock.patch.object(self.device, '_wait', return_value=True), \
                mock.patch.object(self.device, '_since', return_value=response), \
                mock.patch.object(sd.secrets, 'token_hex', return_value=NONCE):
            ok, output = self.device._run_cmd_locked('true', 2, ensure_shell=False)
        self.assertTrue(ok, output)
        self.assertEqual(output, 'SD_OK=1\n')
        shell.assert_not_called()

    def test_complete_matching_frame_extracts_only_its_body(self):
        response = ('command echo\r\n' + BEGIN + '\r\n' + BODY.replace('\n', '\r\n')
                    + END + '\r\nroot@board:# ')
        (ok, raw), write, wait = self.run_frame(response)
        self.assertTrue(ok, raw)
        self.assertEqual(raw, BODY)
        command = write.call_args.args[0]
        self.assertNotIn(BEGIN, command)
        self.assertNotIn(END, command)
        self.assertLessEqual(max(len(line.encode()) for line in command.splitlines()), 512)
        self.assertTrue(command.endswith(self.device.newline))
        self.assertEqual(wait.call_count, 1)

    def test_standalone_carriage_return_and_flow_control_around_markers(self):
        response = ('echo\r\n\x11\r' + BEGIN + '\r' + BODY.replace('\n', '\r\n')
                    + '\x13\r\n' + END + '\r\n')
        (ok, raw), _, _ = self.run_frame(response)
        self.assertTrue(ok, raw)
        self.assertEqual(raw, BODY + '\n')

    def test_echo_can_precede_nonce_markers_on_same_line(self):
        response = ('echoed-base64-fragment' + BEGIN + '\r\n' + BODY
                    + 'echo-fragment' + END + '\r\n')
        (ok, raw), _, _ = self.run_frame(response)
        self.assertTrue(ok, raw)
        self.assertEqual(raw, BODY + 'echo-fragment')

    def test_missing_begin_marker_rejects_frame(self):
        (ok, error), _, wait = self.run_frame(BODY + END + '\n')
        self.assertFalse(ok)
        self.assertIn('起始帧缺失', error)
        self.assertEqual(wait.call_count, 1)

    def test_partial_end_marker_and_missing_newline_do_not_complete_wait(self):
        for ending in (END[:-1], END, END + '\r', END + '-extra\n'):
            with self.subTest(ending=ending):
                (ok, error), _, _ = self.run_frame(BEGIN + '\n' + BODY + ending)
                self.assertFalse(ok)
                self.assertIn('超时', error)

    def test_wrong_nonce_and_partial_end_marker_do_not_complete_wait(self):
        wrong = END.replace(NONCE, 'f' * 24)
        for response in (BEGIN + '\n' + BODY + wrong + '\n',
                         BEGIN + '\n' + BODY + 'echo ' + END[:-1] + '\n'):
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

    @unittest.skipUnless(pathlib.Path('/bin/bash').is_file(), 'requires POSIX bash for syntax checks')
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
    def test_custom_output_has_optional_fields_but_rejects_duplicates(self):
        self.assertEqual(sd.parse_probe('firmware 1.0\n', strict=False)['raw'], 'firmware 1.0\n')
        self.assertEqual(sd.parse_probe('MODEL=RTOS\n', strict=False)['model'], 'RTOS')
        with self.assertRaisesRegex(ValueError, 'MODEL'):
            sd.parse_probe('MODEL=one\nMODEL=two\n', strict=False)

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


class CustomProbeTests(DeviceFixture):
    def test_board_switch_before_transaction_acquisition_never_sends_probe(self):
        original = self.device._run_cmd

        def switched(cmd, timeout=30, expected_connection=None):
            self.device.connection_id = 'replacement-before-lease'
            return original(cmd, timeout, expected_connection=expected_connection)

        with mock.patch.object(self.device, '_run_cmd', side_effect=switched), \
                mock.patch.object(self.device, '_write') as write:
            result = self.device.run_probe()
        self.assertFalse(result['ok'])
        self.assertIn('连接已变化', result['error'])
        write.assert_not_called()

    def test_board_switch_after_response_cannot_publish_old_identity(self):
        manager = mock.Mock()
        self.device.manager = manager

        def switched(*args, **kwargs):
            # prepare_switch rotates the attachment generation without
            # replacing the collector's stop event.
            self.device.connection_id = 'replacement-generation'
            return True, BODY

        with mock.patch.object(sd.Device, 'profile', new_callable=mock.PropertyMock,
                               return_value=BoardProfile()), \
                mock.patch.object(self.device, '_run_cmd', side_effect=switched):
            result = self.device.run_probe()
        self.assertFalse(result['ok'])
        self.assertIn('重新枚举', result['error'])
        self.assertEqual(self.device.probe_info, {})
        manager.identify.assert_not_called()

    @unittest.skipUnless(pathlib.Path('/bin/sh').is_file(), 'requires a local POSIX shell')
    def custom_probe(self, command):
        profile = BoardProfile(probe='custom', probe_command=command, auto_configure_ssh=False)
        manager = mock.Mock()
        self.device.manager = manager

        def run(cmd, timeout, expected_connection=None):
            result = subprocess.run(['/bin/sh', '-c', cmd], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return True, result.stdout

        with mock.patch.object(sd.Device, 'profile', new_callable=mock.PropertyMock, return_value=profile), \
                mock.patch.object(self.device, '_run_cmd', side_effect=run), \
                mock.patch.object(self.device, '_say'):
            result = self.device.run_probe()
        return result, manager

    @unittest.skipUnless(pathlib.Path('/bin/sh').is_file(), 'requires a local POSIX shell')
    def test_metadata_only_probe_preserves_output_without_binding_identity(self):
        result, manager = self.custom_probe("printf 'MODEL=RTOS\\nversion 1.2\\n'")
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['identified'])
        self.assertTrue(result['needs_provisioning'])
        self.assertEqual(result['model'], 'RTOS')
        self.assertIn('version 1.2', self.device.probe_info['raw'])
        manager.identify.assert_not_called()

    @unittest.skipUnless(pathlib.Path('/bin/sh').is_file(), 'requires a local POSIX shell')
    def test_custom_exit_exec_and_syntax_error_are_confined_and_reported(self):
        for command, code in (('exit 7', 7), ('if', 2), ('set -e; false', 1)):
            with self.subTest(command=command):
                result, manager = self.custom_probe(command)
                self.assertFalse(result['ok'], result)
                self.assertEqual(result['exit_code'], code)
                manager.identify.assert_not_called()
        result, manager = self.custom_probe("exec printf 'MODEL=Firmware\\n'")
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['model'], 'Firmware')
        manager.identify.assert_not_called()


class ShortLineScriptTests(unittest.TestCase):
    @unittest.skipUnless(pathlib.Path('/bin/sh').is_file(), 'requires a local POSIX shell')
    def test_staging_preserves_long_scripts_quotes_unicode_and_newlines(self):
        payload = ('a\'\"$`\\中\n' * 500) + 'end'
        command = 'printf %s ' + shlex.quote(payload)
        script = frame_command(command, NONCE, '\n')
        self.assertLessEqual(max(len(line.encode()) for line in script.splitlines()), 512)
        self.assertNotIn(BEGIN, script)
        self.assertNotIn(END, script)
        shells = [['/bin/sh']]
        if shutil.which('busybox'):
            shells.append([shutil.which('busybox'), 'ash'])
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run(shell, input=script, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '\n' + BEGIN + '\n' + payload + '\n' + END + '\n')

    @unittest.skipUnless(pathlib.Path('/bin/sh').is_file(), 'requires a local POSIX shell')
    def test_large_transfer_script_staging_keeps_wire_overhead_bounded(self):
        payload = 'x' * (40 * 1024)
        script = frame_command('printf %s ' + shlex.quote(payload), NONCE, '\n')
        self.assertLess(len(script.encode()), 48 * 1024)
        self.assertLessEqual(max(len(line.encode()) for line in script.splitlines()), 512)
        result = subprocess.run(['/bin/sh'], input=script, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '\n' + BEGIN + '\n' + payload + '\n' + END + '\n')

    @unittest.skipUnless(os.name == 'posix' and shutil.which('busybox'), 'requires local BusyBox and PTY')
    def test_long_script_completes_in_busybox_interactive_line_editor(self):
        # The shell is an isolated test process attached to a new PTY, never
        # any collector-managed serial port. A >2 KiB interactive input line
        # is truncated by common Buildroot ash configurations.
        import pty
        master, slave = pty.openpty()
        process = subprocess.Popen([shutil.which('busybox'), 'ash', '-i'],
                                   stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()

        def receive(deadline, complete=None):
            while time.monotonic() < deadline:
                if complete and complete in output:
                    return
                ready, _, _ = select.select([master], [], [], 0.05)
                if not ready:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')

        try:
            receive(time.monotonic() + 0.1)
            payload = 'x' * 3000
            script = frame_command("printf 'PAYLOAD:%s:END\\n' '" + payload + "'", NONCE, '\n')
            os.write(master, script.encode())
            receive(time.monotonic() + 5, (END + '\r\n').encode())
            normalized = bytes(output).replace(b'\r\n', b'\n')
            self.assertIn(('\n' + BEGIN + '\nPAYLOAD:' + payload + ':END\n\n' + END + '\n').encode(), normalized)
        finally:
            process.kill()
            process.wait(timeout=3)
            os.close(master)


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

    def test_wait_wakes_when_collector_appends_response(self):
        def complete():
            time.sleep(0.02)
            with self.device.cv:
                self.device.buf.extend(b'__SD_E_done__\r\n')
                self.device.cv.notify_all()

        worker = threading.Thread(target=complete)
        worker.start()
        start = time.monotonic()
        try:
            found = self.device._wait(0, r'(?m)^__SD_E_done__\r?\n', timeout=1)
        finally:
            worker.join()
        self.assertIsNotNone(found)
        self.assertLess(time.monotonic() - start, 0.25)


class RootSshSetupTests(DeviceFixture):
    def test_custom_root_password_is_written_only_at_password_prompts(self):
        from seriald_core.profiles import BoardProfile
        profile = BoardProfile(ssh_password='custom-root-password')
        with mock.patch.object(sd.Device, 'profile', new_callable=mock.PropertyMock,
                               return_value=profile), \
                mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_write', return_value=(True, '')) as write, \
                mock.patch.object(self.device, '_wait', return_value=True), \
                mock.patch.object(self.device, '_run_cmd_locked',
                                  return_value=(True, 'SSH_CONFIG_OK=1\n')) as config, \
                mock.patch.object(self.device, '_say') as say:
            self.assertEqual(self.device.configure_root_ssh(), (True, ''))
        self.assertEqual([call.args[0] for call in write.call_args_list][1:],
                         ['custom-root-password\r', 'custom-root-password\r'])
        self.assertNotIn(profile.ssh_password, config.call_args.args[0])
        self.assertNotIn(profile.ssh_password, str(say.call_args_list))

    def test_non_root_or_empty_password_profile_never_starts_provisioning(self):
        from seriald_core.profiles import BoardProfile
        for profile in (BoardProfile(ssh_user='ubuntu'), BoardProfile(ssh_password='')):
            with self.subTest(user=profile.ssh_user), \
                    mock.patch.object(sd.Device, 'profile', new_callable=mock.PropertyMock,
                                      return_value=profile), \
                    mock.patch.object(self.device, '_write') as write, \
                    mock.patch.object(self.device, 'ensure_shell') as shell:
                self.assertFalse(self.device.configure_root_ssh()[0])
                shell.assert_not_called()
                write.assert_not_called()

    def test_passwd_warning_still_reaches_retype_and_config(self):
        responses = iter(['New password:',
                          'BAD PASSWORD: The password is shorter than 8 characters\nRetype new password:',
                          'passwd: password updated successfully', 'ubuntu@ubuntu:~$ '])

        def wait(base, pattern, timeout):
            return re.search(pattern, next(responses))

        with mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_write', return_value=(True, '')) as write, \
                mock.patch.object(self.device, '_wait', side_effect=wait), \
                mock.patch.object(self.device, '_run_cmd_locked',
                                  return_value=(True, 'SSH_CONFIG_OK=1\n')) as config, \
                mock.patch.object(self.device, '_say'):
            self.assertEqual(self.device.configure_root_ssh(), (True, ''))
        self.assertEqual([call.args[0] for call in write.call_args_list],
                         ['if [ "$(id -u)" = 0 ]; then passwd root; else sudo -n passwd root; fi\r',
                          'bianbu\r', 'bianbu\r'])
        command = config.call_args.args[0]
        self.assertNotIn('bianbu', command)
        self.assertIn('/etc/ssh/sshd_config', command)
        shell = shutil.which('sh')
        if shell:
            self.assertEqual(subprocess.run([shell, '-n', '-c', command]).returncode, 0)
        self.assertFalse(self.device.probing)

    def test_sudo_password_requirement_stops_before_changing_config(self):
        with mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_write', return_value=(True, '')) as write, \
                mock.patch.object(self.device, '_wait', return_value=None), \
                mock.patch.object(self.device, '_run_cmd_locked') as config, \
                mock.patch.object(self.device, '_say'):
            ok, error = self.device.configure_root_ssh()
        self.assertFalse(ok)
        self.assertIn('sudo', error)
        self.assertEqual(write.call_count, 1)
        config.assert_not_called()

class SshStateTests(unittest.TestCase):
    @unittest.skipUnless(pathlib.Path('/bin/bash').is_file(), 'requires POSIX bash builtins')
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
  if [ "$#" -ne 2 ] || [ "$1" != -v ]; then
    builtin echo unexpected-command >&2; return 99
  fi
  case "$2" in
    systemctl) return %d ;;
    timeout) return 0 ;;
    *) builtin echo unexpected-command >&2; return 99 ;;
  esac
}
timeout() { shift; "$@"; }
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
