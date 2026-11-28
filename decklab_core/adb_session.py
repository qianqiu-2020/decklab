"""Interactive USB terminal through the local adb server's shell v2 service."""
import re
import secrets
import select
import shlex
import socket
import struct
import time
from contextlib import nullcontext

from .platform_tools import adb_executable
from .board_cases import AdbTransportUnavailable, BOOT_ID, READ_IDENTITY, find_transport
from .config import SSH_LIMIT
from .session_base import SessionBase
from .terminal_backend import control_session


# Return only fixed observations. Journal lines can contain board-specific data.
ADBD_DIAGNOSTIC = r"""(
unit=$(systemctl show --no-pager -p LoadState -p ActiveState -p SubState adbd.service 2>/dev/null)
if printf '%s\n' "$unit" | grep -qx 'SubState=running' || pidof adbd >/dev/null 2>&1; then
    echo adbd=running
    printf 'boot_id='; cat /proc/sys/kernel/random/boot_id 2>/dev/null
    journalctl -b -u adbd.service -n 50 --no-pager -o cat 2>/dev/null | awk '
        /FUNCTIONFS_ENABLE|UsbFfs: online/ { state="enabled" }
        /UsbFfs: (connection terminated|offline)|FUNCTIONFS_(DISABLE|UNBIND)/ { state="closed" }
        END { if (state != "") print "usb_event=" state }
    '
elif printf '%s\n' "$unit" | grep -qx 'LoadState=loaded' &&
     printf '%s\n' "$unit" | grep -Eq '^ActiveState=(failed|inactive)$'; then
    echo adbd=not-running
fi
set -- /sys/class/udc/*/state
if [ "$#" -eq 1 ] && [ -r "$1" ]; then
    case "$(cat "$1" 2>/dev/null)" in
        'not attached') echo gadget=detached ;;
        configured) echo gadget=configured ;;
    esac
fi
)"""


def diagnose_over_serial(device, connection, check, on_stage):
    stage = on_stage or (lambda *args: None)
    check()
    stage('checking-adbd-over-serial')
    ok, output = device.console.run_idle_command(ADBD_DIAGNOSTIC, expected_connection=connection)
    check()
    if not ok:
        stage('adb-serial-evidence-unavailable')
        return '串口暂时无法自动取证。请结束占用串口的操作，并让终端停在已登录的 Shell 提示符后重试。'
    facts = set(output.splitlines())
    if 'adbd=not-running' in facts:
        stage('adbd-not-running')
        return '串口确认板端 adbd 服务未运行，请检查该服务状态。'
    if 'adbd=running' in facts:
        stage('adbd-running')
        if 'gadget=detached' in facts:
            stage('adb-gadget-detached')
            return '串口确认 adbd 正在运行，但板端 USB 设备控制器未连接，请检查调试线和端口。'
        if 'gadget=configured' in facts:
            stage('adb-gadget-configured')
        if 'usb_event=closed' in facts:
            boot = next((line[8:] for line in facts if line.startswith('boot_id=') and BOOT_ID.fullmatch(line[8:])), '')
            stage('adb-usb-transport-closed', boot) if boot else stage('adb-usb-transport-closed')
            return ('串口确认 adbd 正在运行。本次启动最近的 ADB USB 日志显示连接已关闭，'
                    '控制机尚未识别到当前板卡在线。可在串口执行 systemctl restart adbd 后重试。')
        return '串口确认 adbd 正在运行，暂未读到能解释当前故障的板端 USB 日志。'
    stage('adb-serial-evidence-unavailable')
    return '串口已响应，但没有读到可确认的 adbd 服务状态。'


def recover_transport(device, connection, serial, boot, resolve, check, stage):
    check()
    stage('restarting-adbd-over-serial')
    command = ('(\n' + READ_IDENTITY
               + '[ "$serial" = ' + shlex.quote(serial) + ' ] && [ "$(cat /proc/sys/kernel/random/boot_id)" = '
               + shlex.quote(boot) + ' ] && [ "$(id -u)" = 0 ] && systemctl is-active --quiet adbd.service '
               + '|| { echo recovery=not-issued; exit; }\n'
               + 'systemctl --no-block restart adbd.service && echo recovery=acknowledged\n)')
    ok, output = device.console.run_idle_command(command, expected_connection=connection)
    check()
    if 'recovery=not-issued' in output.splitlines():
        raise ConnectionError('板卡身份、启动编号、串口权限或 adbd 状态未通过核对，请重新诊断。')
    stage('adbd-restart-acknowledged' if ok and 'recovery=acknowledged' in output.splitlines()
          else 'adbd-restart-unconfirmed')
    stage('waiting-adb-recovery')
    deadline = time.monotonic() + 15
    while True:
        check()
        try:
            candidate = resolve()
            if candidate[2].get('boot_id') != boot:
                raise ConnectionError('板卡已经重新启动，请重新打开 ADB 标签页。')
            return candidate
        except AdbTransportUnavailable as error:
            if time.monotonic() >= deadline:
                raise ConnectionError('已等待原板卡重新上线，仍无匹配的 ADB 连接。请查看本次任务回执和串口。') from error
        time.sleep(0.5)


def read_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        try:
            chunk = sock.recv(length - len(data))
        except TimeoutError as error:
            raise ConnectionError('ADB 数据接收超时') from error
        if not chunk:
            raise ConnectionError('ADB USB 连接已断开')
        data.extend(chunk)
    return bytes(data)


def request(sock, service):
    payload = service.encode()
    sock.sendall(('%04x' % len(payload)).encode() + payload)
    status = read_exact(sock, 4)
    if status == b'FAIL':
        try:
            length = int(read_exact(sock, 4), 16)
        except ValueError as error:
            raise ConnectionError('ADB 返回了无效错误长度') from error
        raise ConnectionError(read_exact(sock, length).decode(errors='replace'))
    if status != b'OKAY':
        raise ConnectionError('ADB 未确认连接')


def packet(sock):
    channel, length = struct.unpack('<BI', read_exact(sock, 5))
    if length > 1024 * 1024:
        raise ConnectionError('ADB 终端数据长度无效')
    return channel, read_exact(sock, length)


def usb_endpoints():
    """Some host USB drivers omit the bus path in 'adb devices -l'."""
    with socket.create_connection(('127.0.0.1', 5037), timeout=3) as sock:
        try:
            request(sock, 'host:track-devices-proto-text')
        except ConnectionError:
            # Older servers can still select a single unambiguous USB transport.
            with socket.create_connection(('127.0.0.1', 5037), timeout=3) as legacy:
                request(legacy, 'host:tport:usb')
                endpoint, = struct.unpack('<Q', read_exact(legacy, 8))
                return [('USB', str(endpoint))]
        size = int(read_exact(sock, 4), 16)
        listing = read_exact(sock, size).decode()
    candidates = []
    for record in re.findall(r'^device \{\n(.*?)^\}', listing, re.M | re.S):
        fields = dict(re.findall(r'^  (state|connection_type|transport_id): (\w+)$', record, re.M))
        if (fields.get('connection_type') == 'USB' and fields.get('state') == 'DEVICE'
                and re.fullmatch(r'[1-9]\d*', fields.get('transport_id', ''))):
            candidates.append(('USB', fields['transport_id']))
    return candidates


class AdbSession(SessionBase):
    kind = 'adb'

    def __init__(self, sid, device, port, endpoint, facts):
        super().__init__(sid, port, device.label, device.stable_dir(), user=facts.get('uid') or '?', password='')
        self.endpoint = endpoint
        self.serial = device.serial
        self.boot_id = facts['boot_id']

    def _open_transport(self):
        sock = socket.create_connection(('127.0.0.1', 5037), timeout=5)
        try:
            request(sock, 'host:transport-id:' + self.endpoint)
            marker = secrets.token_hex(16)
            command = (READ_IDENTITY + '[ "$serial" = ' + shlex.quote(self.serial)
                       + ' ] && [ "$(cat /proc/sys/kernel/random/boot_id)" = '
                       + shlex.quote(self.boot_id) + ' ] || exit 125\n'
                       + "printf '%s' " + marker + '\nexec /bin/sh -i')
            request(sock, 'shell,v2,TERM=xterm-256color,pty:' + command)
            deadline = time.monotonic() + 5
            data = b''
            while marker.encode() not in data:
                if time.monotonic() >= deadline or len(data) > 4096:
                    raise ConnectionError('ADB 终端身份确认超时')
                channel, chunk = packet(sock)
                if channel not in (1, 2):
                    raise ConnectionError('ADB 终端未确认板卡和启动编号')
                data += chunk
            self.initial = data.split(marker.encode(), 1)[1]
            self._resize(sock)
            return sock
        except BaseException:
            sock.close()
            raise

    def _receive(self, sock):
        if self.initial:
            data, self.initial = self.initial, b''
            return data
        if not select.select([sock], [], [], 0.25)[0]:
            return None
        channel, data = packet(sock)
        if channel == 3:
            return b''
        if channel in (1, 2):
            return data or None
        raise ConnectionError('ADB 返回了未知终端通道')

    def _send(self, sock, data):
        sock.sendall(struct.pack('<BI', 0, len(data)) + data)

    def _resize(self, sock):
        data = ('%dx%d,0x0\0' % (self.rows, self.cols)).encode()
        sock.sendall(struct.pack('<BI', 5, len(data)) + data)

    def _dispose_transport(self, sock):
        if sock is not None:
            sock.close()

    def _interrupt_transport(self):
        if self.fd is not None:
            self.fd.shutdown(socket.SHUT_RDWR)


def connect(manager, device, payload, actor, *, on_stage=None, recovery_boot=None):
    serial, connection, stop = device.serial, device.connection_id, device.stop
    registry = device._sessions

    def check():
        device.transactions.check_context()
        if (not serial or payload.get('serial') != serial or payload.get('connection_id') != connection
                or device.serial != serial or device.connection_id != connection or device.stop is not stop
                or stop.is_set() or not device.present or device._sessions is not registry
                or device.board is None or device.board.connection is not device
                or manager.store.has_conflict(serial)):
            raise ConnectionError('请重新确认当前板卡的串口与 USB 连接')

    check()
    executable = adb_executable()
    if not executable:
        raise ConnectionError('控制机缺少 adb')
    device.reap_sessions()
    with registry.lock:
        if len(registry.sessions) + registry.pending >= SSH_LIMIT:
            raise ConnectionError('终端会话数量已达上限，请关闭闲置会话')
        registry.pending += 1
    session = None
    try:
        registered = manager.hardware.board_tests.get(serial)
        stages = {'on_stage': on_stage} if on_stage is not None else {}
        def resolve(trace=True):
            return find_transport(executable, serial, registered, usb_endpoints=usb_endpoints,
                                  allow_relocation=True, **(stages if trace else {}))

        try:
            port, endpoint, facts = resolve()
        except AdbTransportUnavailable as error:
            if actor != 'web':
                raise
            if recovery_boot:
                port, endpoint, facts = recover_transport(device, connection, serial, recovery_boot, lambda: resolve(False),
                                                          check, on_stage or (lambda *args: None))
            else:
                diagnosis = diagnose_over_serial(device, connection, check, on_stage)
                raise ConnectionError(str(error).rstrip('。') + '。' + diagnosis) from error
        check()
        if on_stage is not None:
            on_stage('opening-adb-terminal', port)
        session = AdbSession('adb' + secrets.token_hex(12), device, port, endpoint, facts)
        session.actor, session.title = actor, 'USB ADB'
        session._board_device = device
        grants = session.agent_grants = getattr(manager, 'agent_grants', None)
        control_session(session, actor, device.transactions.client_id)
        if not session.ensure():
            raise ConnectionError(session.transport_error or '建立 ADB 终端失败')
        with device.operation_lock, grants.lock if grants else nullcontext(), registry.lock:
            check()
            if registered != manager.hardware.board_tests.get(serial):
                raise ConnectionError('板卡 USB 接线配置已变化')
            if grants:
                grants.check_actor(actor)
            session.store = manager.store
            manager.store.open_session(session.sid, serial, actor, session.title, port)
            if session.closed:
                manager.store.close_session(session.sid)
                raise ConnectionError('ADB 终端已断开')
            registry.sessions[session.sid] = session
        return session
    except BaseException:
        if session:
            session.close()
        raise
    finally:
        with registry.lock:
            registry.pending -= 1
