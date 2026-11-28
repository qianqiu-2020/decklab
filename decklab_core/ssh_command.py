"""Open a noninteractive SSH channel to the board verified on the station's USB link."""
from contextlib import contextmanager
from ipaddress import ip_address
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time

from .board_cases import BOOT_ID, READ_IDENTITY, adb_command
from .session_base import ssh_address, valid_ssh_user, valid_ssh_password
from .tasks import TaskError


class LoginDenied(TaskError):
    pass


SSH_PORT = 22


def serial_ssh_identity(device, actor, client, connection):
    """Read public keys and boot ID from the leased console before network login."""
    command = (READ_IDENTITY + '\nif [ "$serial" = ' + shlex.quote(device.serial) +
               ' ]; then printf "BOOT=%s\\n" "$(cat /proc/sys/kernel/random/boot_id)"; '
               'cat /etc/ssh/ssh_host_*_key.pub; fi')
    with device.transactions.actor_scope(actor, client):
        ok, output = device._run_cmd(command, timeout=15, expected_connection=connection)
    boot = re.findall(r'^BOOT=([0-9a-f-]+)$', output.replace('\r', ''), re.M) if ok else []
    if len(boot) != 1 or not BOOT_ID.fullmatch(boot[0]):
        raise TaskError('无法经本板串口核实 SSH 主机身份和启动编号', 409)
    return {'boot_id': boot[0], 'host_keys': output}


@contextmanager
def command_channel(operation, endpoint, context):
    device = operation.device
    try:
        address = str(ip_address(ssh_address(device.probe_info)))
    except ValueError:
        raise TaskError('本板暂无可用的 SSH 地址', 409) from None
    login = operation.ssh_login
    saved = operation.manager.store.get_ssh_credentials(operation.serial)
    if saved:
        user, password = saved['ssh_user'], saved['ssh_password']
    elif login is not None:
        user, password = login['user'], login['password']
    else:
        profile = device.profile
        if device._needs_ssh_credentials(device.login_os_family(), profile):
            raise TaskError('请先在本板的 SSH 设置保存登录信息', 409)
        user, password = profile.ssh_user, profile.ssh_password
    if not valid_ssh_user(user) or not valid_ssh_password(password):
        raise TaskError('本板 SSH 登录信息无效', 409)
    if not shutil.which('sshpass') or not shutil.which('ssh'):
        raise TaskError('控制机缺少 OpenSSH 或 sshpass', 409)
    # Learn public host keys over the identity-checked wire before offering a
    # password to a network address that DHCP may have reassigned.
    if endpoint is None:
        key_text = operation.serial_host_keys
    else:
        probe = READ_IDENTITY + '\n[ "$serial" = ' + shlex.quote(operation.serial) + \
            ' ] && [ "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(operation.boot_id) + \
            ' ] && cat /etc/ssh/ssh_host_*_key.pub'
        try:
            response = adb_command(operation.executable, '-t', endpoint, 'shell', '-T', probe)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TaskError('无法通过已绑定的 USB 连接核对 SSH 主机公钥', 409) from error
        key_text = response.stdout if response.returncode == 0 else ''
    keys = [parts[:2] for line in key_text.splitlines() if len(parts := line.split()) >= 2
            and re.fullmatch(r'[a-zA-Z0-9@._-]+', parts[0])
            and re.fullmatch(r'[A-Za-z0-9+/=]+', parts[1])]
    if not keys:
        raise TaskError('未读到与本板身份匹配的 SSH 主机公钥', 409)
    with tempfile.TemporaryDirectory(prefix='decklab-ssh-') as tmp:
        known = Path(tmp, 'known_hosts')
        known.write_text(''.join('board ' + ' '.join(key) + '\n' for key in keys), encoding='ascii')
        base = ['ssh', '-p', str(SSH_PORT), '-F', '/dev/null', '-S', str(Path(tmp, 'socket')), '-o', 'HostKeyAlias=board',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UpdateHostKeys=no',
                '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1',
                '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR']
        destination = user + '@' + address
        context.check()
        with subprocess.Popen(['sshpass', '-e', *base, '-M', '-N', destination], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env={**os.environ, 'LC_ALL': 'C', 'SSHPASS': password}, start_new_session=True) as master:
            try:
                deadline = time.monotonic() + 7
                while not Path(tmp, 'socket').is_socket():
                    context.check()
                    status = master.poll()
                    if status is not None:
                        diagnostic = master.stderr.read(8192).lower()
                        if status == 5 or b'permission denied' in diagnostic:
                            if saved:
                                raise TaskError('板卡拒绝了 DeckLab 已保存的 SSH 账号，请在本板的 SSH 设置中重新连接并保存', 409)
                            raise LoginDenied('板卡拒绝了本次 SSH 登录，请检查账号及 sshd 的登录策略', 409)
                        raise TaskError('无法建立经主机公钥验证的 SSH 连接', 409)
                    if time.monotonic() >= deadline:
                        raise TaskError('本板 SSH 连接或认证失败，请核对 SSH 设置与服务状态', 409)
                    time.sleep(0.05)
                # Refuse a fresh network login if the authenticated master died.
                yield [*base, '-o', 'ProxyCommand=false', '-T', destination]
            finally:
                try:
                    os.killpg(master.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    master.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(master.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
