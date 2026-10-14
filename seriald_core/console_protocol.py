"""Platform-independent console dialogue over a connection's existing byte stream.

The connection owns buffered bytes, condition, profile/newline and transactions.
This service only consumes snapshots and uses the connection's guarded writer;
it never opens a serial handle, reads an OS descriptor or binds board identity.
Connection callbacks remain the seam for simulated firmware and test transports.
"""
import re
import secrets
import shlex
import time

from .config import ANSI
from .protocol import RE_LOGIN, RE_PASSWD, RE_PROMPT
from .transactions import TransactionError


def root_ssh_config_command():
    """Build a root-only, password-free command for the serial shell."""
    script = (
        'set -e; cfg=/etc/ssh/sshd_config; '
        'tmp=$(mktemp /etc/ssh/.seriald-config.XXXXXX); '
        'backup=$(mktemp /etc/ssh/.seriald-backup.XXXXXX); '
        "trap 'rm -f \"$tmp\" \"$backup\"' EXIT; "
        'cp -p "$cfg" "$backup"; '
        'if [ "$(head -n 1 "$cfg")" = "PermitRootLogin yes" ] && '
        '[ "$(sed -n 2p "$cfg")" = "PasswordAuthentication yes" ]; '
        'then cp -p "$cfg" "$tmp"; '
        'else { printf "PermitRootLogin yes\nPasswordAuthentication yes\n"; cat "$cfg"; } > "$tmp"; '
        'chmod --reference="$cfg" "$tmp"; chown --reference="$cfg" "$tmp"; fi; '
        'sshd_bin=$(command -v sshd || printf /usr/sbin/sshd); '
        '"$sshd_bin" -t -f "$tmp" || { echo SSH_CONFIG_ERROR=invalid; exit 1; }; '
        'mv "$tmp" "$cfg"; '
        'if (systemctl restart ssh || systemctl restart sshd) && '
        '"$sshd_bin" -T -C user=root,host=localhost,addr=127.0.0.1 '
        '| grep -qx "permitrootlogin yes" && '
        '"$sshd_bin" -T -C user=root,host=localhost,addr=127.0.0.1 '
        '| grep -qx "passwordauthentication yes"; '
        'then echo SSH_CONFIG_OK=1; '
        'else mv "$backup" "$cfg"; '
        '(systemctl restart ssh || systemctl restart sshd) >/dev/null 2>&1 || :; '
        'echo SSH_CONFIG_ERROR=restart-or-policy; fi'
    )
    return 'if [ "$(id -u)" = 0 ]; then sh -c {s}; else sudo -n sh -c {s}; fi'.format(
        s=shlex.quote(script))


class ConsoleProtocol:
    """Login, framed command completion and explicit SSH provisioning dialogue."""
    def __init__(self, connection):
        self.connection = connection

    def wait(self, base, pattern, timeout=20.0):
        device = self.connection
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        lease = device.transactions.current
        if lease:
            end = min(end, lease.deadline)
        while time.monotonic() < end:
            lease = device.transactions.current
            if lease:
                lease.check()
            if device.stop.is_set() or device.fd is None:
                return None
            with device.cv:
                observed = device.base + len(device.buf)
            m = rx.search(ANSI.sub('', device._since(base)))
            if m:
                return m
            # The sole collector reader notifies this condition for each append.
            # Only sleep if no bytes arrived after the search, avoiding a lost
            # wakeup and a fixed 300 ms delay on every transfer chunk.
            with device.cv:
                if device.base + len(device.buf) == observed:
                    if lease:
                        lease.check()
                    device.cv.wait(min(1.0, max(0, end - time.monotonic())))
        if lease:
            lease.check()
        return None

    def ensure_shell(self, base):
        """仅从现场输出判断登录状态,兼容 ANSI、免密码登录以及 CR/LF。"""
        device = self.connection
        if device.profile.probe != 'linux':
            return False, base
        with device.cv:
            tail = ANSI.sub('', device.console_tail).rstrip()[-120:]
        if device.newline is not None and re.search(RE_PROMPT, tail):
            return True, base
        at_login = bool(re.search(RE_LOGIN + r'\s*$', tail))
        if not at_login or device.newline is None:
            at_login = False
            # 有的板子只认 LF;先试本连接成功过的换行,只在没收到提示符时尝试另一种。
            configured = device.profile.line_ending
            preferred = configured or device.newline or '\r'
            endings = (configured,) if configured else (preferred, '\n' if preferred == '\r' else '\r')
            for newline in endings:
                base = device.now_off()
                if not device._write(newline)[0]:
                    return False, base
                match = device._wait(base, r'([#$]|' + RE_LOGIN + r')\s*$', 4)
                if match:
                    device.newline = newline
                    if re.search(RE_PROMPT, match.group(0)):
                        return True, device.now_off()
                    at_login = True
                    break
        if not at_login:
            return False, base
        device._say('停在登录提示符,使用配置的账户自动登录')
        base = device.now_off()
        if not device._write(device.profile.console_user + device.newline)[0]:
            return False, base
        failure = r'(?:[Ll]ogin incorrect|登录错误|认证失败|Authentication failure)'
        match = device._wait(base, RE_PASSWD + r'\s*$|' + RE_PROMPT + '|' + failure, 12)
        if not match or re.search(failure, match.group(0)):
            return False, base
        if re.search(RE_PROMPT, match.group(0)):
            return True, device.now_off()       # 免密码登录不能继续盲发默认密码
        base = device.now_off()
        if not device._write(device.profile.console_password + device.newline)[0]:
            return False, base
        match = device._wait(base, RE_PROMPT + '|' + failure, 20)
        return (bool(match and re.search(RE_PROMPT, match.group(0))), device.now_off())

    def run_command(self, cmd, timeout=30.0):
        """一个事务覆盖登录、完整发送和带 nonce 的响应,禁止其他写入口插队。"""
        device = self.connection
        if device.file_transfer.active:
            return False, '串口文件传输中，请完成或取消后再操作'
        try:
            with device.transactions.begin('console-command', timeout):
                device.probing = True
                try:
                    return device._run_cmd_locked(cmd, timeout)
                finally:
                    device.probing = False
        except TransactionError as error:
            return False, str(error)

    def run_locked(self, cmd, timeout, ensure_shell=True):
        """Run one framed console command while operation_lock is held.

        A transfer checks the shell once at start; every later chunk still
        requires its own complete nonce-framed response.
        """
        device = self.connection
        if device.fd is None or device.stop.is_set():
            return False, '设备未打开(串口没接上或已被拔掉)'
        if ensure_shell:
            ok, base = device.ensure_shell(device.now_off())
            if not ok:
                return False, '板子不在 shell 提示符,且自动登录失败(可能需要手动处理)'
        elif not device.newline:
            return False, '本连接尚未确定 shell 换行方式'
        nonce = secrets.token_hex(12)
        begin, end = '__SD_B_' + nonce + '__', '__SD_E_' + nonce + '__'
        # printf 参数拆开 marker,回显再怎么分块也不会成为完整标记行。
        framed = ("printf '\\n%s%s\\n' '__SD_B_' '%s__'; %s; "
                  "printf '\\n%s%s\\n' '__SD_E_' '%s__'")
        framed = framed % ('%s', '%s', nonce, cmd, '%s', '%s', nonce)
        base = device.now_off()
        ok, err = device._write(framed + device.newline)
        if not ok:
            return False, err
        # A long echoed upload line can interleave with printf output, leaving
        # echo characters directly before a marker. The random marker is split
        # in the command text, so an echo cannot contain it in full.
        if not device._wait(base, re.escape(end) + r'\r?\n', timeout):
            if device.visibility.active:
                device.visibility.report(device._since(base), framed)
            return False, '等待板子完整响应超时'
        # Firmware/shells can emit a standalone CR or XON/XOFF around a marker.
        # Normalize only this command's parsed copy; the shared terminal keeps
        # the exact received bytes for interactive display and archived logs.
        raw = ANSI.sub('', device._since(base))
        raw = re.sub(r'[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]', '', raw)
        raw = raw.replace('\r\n', '\n').replace('\r', '\n')
        match = re.search(re.escape(begin) + r'\n([\s\S]*?)'
                          + re.escape(end) + r'\n', raw)
        if not match:
            if device.visibility.active:
                device.visibility.report(device._since(base), framed)
            with device.cv:
                truncated = base < device.base
            return False, ('响应帧超出缓冲上限' if truncated else '响应起始帧缺失或行结束符异常')
        return True, match.group(1)

    def configure_root_ssh(self):
        """Use the already collected serial stream to set root's SSH password."""
        device = self.connection
        if not device.profile.auto_configure_ssh:
            return False, '当前设备配置未允许自动修改 root 密码或 sshd 配置'
        profile = device.profile
        if profile.ssh_user != 'root' or not profile.ssh_password:
            return False, 'SSH 自动配置需要配置非空的 root 密码'
        if device.file_transfer.active:
            return False, '串口文件传输中，请完成或取消后再配置 SSH'
        try:
            with device.transactions.begin('ssh-provision', 110):
                device.probing = True
                try:
                    if device.fd is None or device.stop.is_set():
                        return False, '串口未连接，无法自动配置 SSH'
                    ok, _ = device.ensure_shell(device.now_off())
                    if not ok:
                        return False, '串口未停在已登录的 shell，无法自动提权'
                    device._say('SSH 自动配置：通过串口修改 root 密码和 sshd 配置')
                    base = device.now_off()
                    ok, error = device._write(
                        'if [ "$(id -u)" = 0 ]; then passwd root; else sudo -n passwd root; fi'
                        + device.newline)
                    if not ok:
                        return False, error
                    first = r'(?i)(?:New (?:UNIX )?password|新的密码)[:：]\s*$'
                    second = r'(?i)(?:Retype new (?:UNIX )?password|Repeat new password|再次输入新的密码)[:：]\s*$'
                    if not device._wait(base, first, 15):
                        return False, '无法通过串口运行 passwd（sudo 可能需要普通用户密码）'
                    base = device.now_off()
                    ok, error = device._write(profile.ssh_password + device.newline)
                    if not ok:
                        return False, error
                    if not device._wait(base, second, 15):
                        return False, 'passwd 没有请求再次输入密码'
                    base = device.now_off()
                    ok, error = device._write(profile.ssh_password + device.newline)
                    if not ok:
                        return False, error
                    success = r'(?i)(?:password updated successfully|all authentication tokens updated successfully)'
                    if not device._wait(base, success, 20):
                        return False, 'passwd 未确认 root 密码更新成功'
                    # Wait for the normal shell prompt before sending the framed config
                    # command; passwd's success line can arrive before its child exits.
                    if not device._wait(base, RE_PROMPT, 5):
                        return False, '修改密码后未返回 shell 提示符'
                    ok, raw = device._run_cmd_locked(root_ssh_config_command(), 35)
                    if not ok or 'SSH_CONFIG_OK=1' not in raw:
                        return False, '更新 sshd 配置失败: ' + (raw[-300:] if ok else raw)
                    device._say('SSH 自动配置完成：root 密码已更新，sshd 已重启')
                    return True, ''
                finally:
                    device.probing = False
        except TransactionError as error:
            return False, str(error)
