"""Interactive USB terminal through the local adb server's shell v2 service."""
import re
import secrets
import select
import shlex
import shutil
import socket
import struct
import time
from contextlib import nullcontext

from .board_cases import READ_IDENTITY, find_transport
from .config import SSH_LIMIT
from .session_base import SessionBase
from .terminal_backend import control_session


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


def connect(manager, device, payload, actor):
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
    executable = shutil.which('adb')
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
        port, endpoint, facts = find_transport(executable, serial, registered, usb_endpoints=usb_endpoints)
        check()
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
