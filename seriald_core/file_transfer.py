"""Chunked, framed file transfer over the collector's existing serial reader."""
import base64
import binascii
import hashlib
import re
import secrets
import shlex
import threading
import time


CHUNK_SIZE = 2048  # Encoded input stays below a typical 4096-byte tty line limit.
IDLE_SECONDS = 10 * 60
HEX_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class TransferError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _remote_path(value):
    if not isinstance(value, str) or not value.startswith('/') or len(value.encode('utf-8')) > 1024:
        raise TransferError('板端路径必须是长度不超过 1024 字节的绝对路径')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise TransferError('板端路径不能包含控制字符')
    return value


def _field(output, name):
    match = re.search(r'(?m)^' + re.escape(name) + r'=([^\r\n]*)$', output)
    return match.group(1) if match else None


class SerialFileTransfer:
    """One in-flight transfer per physical connection; commands share its reader."""

    def __init__(self, device):
        self.device = device
        self.lock = threading.RLock()
        self.state = None

    @property
    def active(self):
        with self.lock:
            return self.state is not None

    def _run(self, command, timeout=45):
        device = self.device
        if not device.operation_lock.acquire(blocking=False):
            raise TransferError('串口正在执行其他操作，请稍后重试', 409)
        device.probing = True
        try:
            ok, output = device._run_cmd_locked(command, timeout)
        finally:
            device.probing = False
            device.operation_lock.release()
        if not ok:
            raise TransferError(output, 502)
        return output

    def _check_connection(self, connection_id):
        device = self.device
        if (not connection_id or connection_id != device.connection_id
                or not device.present or device.fd is None or device.stop.is_set()):
            raise TransferError('设备连接已变化，请重新选择设备', 409)

    def _schedule_expiry(self, transfer_id):
        timer = threading.Timer(IDLE_SECONDS, self._expire, args=(transfer_id,))
        timer.daemon = True
        timer.start()

    def _expire(self, transfer_id):
        with self.lock:
            state = self.state
            if not state or state['id'] != transfer_id:
                return
            remaining = IDLE_SECONDS - (time.monotonic() - state['last'])
            if remaining > 0:
                timer = threading.Timer(remaining, self._expire, args=(transfer_id,))
                timer.daemon = True
                timer.start()
                return
            self._cleanup(state)
            self.state = None

    def _cleanup(self, state):
        if state['direction'] != 'upload' or not state.get('temp'):
            return
        device = self.device
        if device.stop.is_set() or device.fd is None or device.connection_id != state['connection_id']:
            return
        try:
            self._run('rm -f ' + shlex.quote(state['temp']), timeout=10)
        except TransferError:
            pass

    def start(self, direction, path, connection_id, actor='web', size=None, overwrite=False):
        path = _remote_path(path)
        if direction not in ('upload', 'download'):
            raise TransferError('direction 必须是 upload 或 download')
        if direction == 'upload' and (type(size) is not int or size < 0):
            raise TransferError('上传文件大小无效')
        if not self.lock.acquire(blocking=False):
            raise TransferError('已有文件传输请求正在执行', 409)
        try:
            self._check_connection(connection_id)
            if self.state:
                raise TransferError('此串口已有文件传输，请完成或取消后再试', 409)
            transfer_id = secrets.token_hex(12)
            state = {'id': transfer_id, 'direction': direction, 'path': path,
                     'connection_id': connection_id, 'actor': actor,
                     'offset': 0, 'last': time.monotonic()}
            quoted = shlex.quote(path)
            if direction == 'upload':
                temp = path + '.seriald-' + transfer_id + '.part'
                command = ('umask 077; if [ -d {dst} ] || '
                           '{exists}; then printf "SD_ERROR=exists\\n"; '
                           'elif (set -C; : > {tmp}); then printf "SD_OK=1\\n"; '
                           'else printf "SD_ERROR=create\\n"; fi').format(
                    dst=quoted,
                    exists=('false' if overwrite else '[ -e {0} ] || [ -L {0} ]'.format(quoted)),
                    tmp=shlex.quote(temp))
                output = self._run(command)
                if _field(output, 'SD_OK') != '1':
                    raise TransferError('无法创建板端临时文件：' + (_field(output, 'SD_ERROR') or output[-200:]), 409)
                state.update(temp=temp, size=size, hasher=hashlib.sha256(), overwrite=bool(overwrite))
            else:
                command = ('if [ -f {0} ] && [ -r {0} ]; then '
                           's=$(wc -c < {0}); h=$(sha256sum {0} | cut -d\x27 \x27 -f1); '
                           'printf "SD_SIZE=%s\\nSD_SHA=%s\\n" "$s" "$h"; '
                           'else printf "SD_ERROR=unreadable\\n"; fi').format(quoted)
                output = self._run(command, timeout=120)
                raw_size, digest = _field(output, 'SD_SIZE'), _field(output, 'SD_SHA')
                if not raw_size or not raw_size.isdigit() or not digest or not HEX_SHA256.fullmatch(digest):
                    raise TransferError('板端文件不可读，或缺少 wc/sha256sum：'
                                        + (_field(output, 'SD_ERROR') or output[-200:]), 409)
                state.update(size=int(raw_size), sha256=digest)
            self.state = state
            self._schedule_expiry(transfer_id)
            return {'id': transfer_id, 'direction': direction, 'path': path,
                    'size': state['size'], 'sha256': state.get('sha256'),
                    'chunk_size': CHUNK_SIZE, 'offset': 0}
        finally:
            self.lock.release()

    def _state(self, transfer_id, connection_id, allow_failed=False):
        self._check_connection(connection_id)
        state = self.state
        if not state or state['id'] != transfer_id or state['connection_id'] != connection_id:
            raise TransferError('文件传输已结束或设备连接已变化', 409)
        if state.get('failed') and not allow_failed:
            raise TransferError('上一个分块结果不确定，请取消后重新传输', 409)
        state['last'] = time.monotonic()
        return state

    def chunk(self, transfer_id, connection_id, offset, data_b64=None):
        if not self.lock.acquire(blocking=False):
            raise TransferError('已有文件传输请求正在执行', 409)
        try:
            state = self._state(transfer_id, connection_id)
            if type(offset) is not int or offset != state['offset'] or offset >= state['size']:
                raise TransferError('分块偏移不匹配', 409)
            expected = min(CHUNK_SIZE, state['size'] - offset)
            if state['direction'] == 'upload':
                try:
                    data = base64.b64decode(data_b64, validate=True)
                except (TypeError, ValueError, binascii.Error):
                    raise TransferError('分块不是有效 base64') from None
                if len(data) != expected:
                    raise TransferError('上传分块长度不匹配')
                encoded = base64.b64encode(data).decode('ascii')
                command = ('printf %s {0} | base64 -d >> {1}; r=$?; '
                           's=$(wc -c < {1}); printf "SD_RC=%s\\nSD_SIZE=%s\\n" "$r" "$s"').format(
                    shlex.quote(encoded), shlex.quote(state['temp']))
                try:
                    output = self._run(command)
                except TransferError:
                    state['failed'] = True
                    raise
                if _field(output, 'SD_RC') != '0' or _field(output, 'SD_SIZE') != str(offset + len(data)):
                    state['failed'] = True
                    raise TransferError('板端写入分块失败或长度不匹配', 502)
                state['hasher'].update(data)
            else:
                index = offset // CHUNK_SIZE
                command = ('if [ -f {0} ]; then '
                           'd=$(dd if={0} bs={1} skip={2} count=1 2>/dev/null | base64 | tr -d "\\r\\n"); '
                           'printf "SD_DATA=%s\\n" "$d"; '
                           'else printf "SD_ERROR=missing\\n"; fi').format(
                    shlex.quote(state['path']), CHUNK_SIZE, index)
                output = self._run(command)
                encoded = _field(output, 'SD_DATA')
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (TypeError, ValueError, binascii.Error):
                    raise TransferError('板端返回的分块损坏', 502) from None
                if len(data) != expected:
                    raise TransferError('板端文件已变化，分块长度不匹配', 409)
                state.setdefault('hasher', hashlib.sha256()).update(data)
            state['offset'] += len(data)
            state['last'] = time.monotonic()
            result = {'id': transfer_id, 'offset': state['offset'], 'size': state['size'],
                      'bytes': len(data)}
            if state['direction'] == 'download':
                result['data_b64'] = base64.b64encode(data).decode('ascii')
            return result
        finally:
            self.lock.release()

    def finish(self, transfer_id, connection_id):
        if not self.lock.acquire(blocking=False):
            raise TransferError('已有文件传输请求正在执行', 409)
        try:
            state = self._state(transfer_id, connection_id)
            if state['offset'] != state['size']:
                raise TransferError('文件尚未完整传输', 409)
            path = shlex.quote(state['path'])
            if state['direction'] == 'upload':
                temp = shlex.quote(state['temp'])
                digest = state.get('hasher', hashlib.sha256()).hexdigest()
                promote = ('mv {0} {1}' if state['overwrite'] else 'ln {0} {1} && rm {0}').format(temp, path)
                command = ('s=$(wc -c < {tmp}); h=$(sha256sum {tmp} | cut -d\x27 \x27 -f1); '
                           'if [ "$s" = {size} ] && [ "$h" = {digest} ]; then '
                           'if {promote}; then printf "SD_OK=1\\n"; '
                           'else printf "SD_ERROR=promote\\n"; fi; '
                           'else printf "SD_ERROR=checksum\\n"; fi').format(
                    tmp=temp, size=state['size'], digest=shlex.quote(digest), promote=promote)
                output = self._run(command, timeout=120)
                if _field(output, 'SD_OK') != '1':
                    raise TransferError('板端校验或落盘失败：' + (_field(output, 'SD_ERROR') or output[-200:]), 502)
            else:
                digest = state.get('hasher', hashlib.sha256()).hexdigest()
                if digest != state['sha256']:
                    raise TransferError('串口传输内容的 SHA-256 校验失败', 502)
                command = ('s=$(wc -c < {0}); h=$(sha256sum {0} | cut -d\x27 \x27 -f1); '
                           'printf "SD_SIZE=%s\\nSD_SHA=%s\\n" "$s" "$h"').format(path)
                output = self._run(command, timeout=120)
                if _field(output, 'SD_SIZE') != str(state['size']) or _field(output, 'SD_SHA') != digest:
                    raise TransferError('板端文件在传输期间发生变化', 409)
            self.state = None
            return {'id': transfer_id, 'path': state['path'], 'size': state['size'],
                    'sha256': digest, 'direction': state['direction']}
        finally:
            self.lock.release()

    def abort(self, transfer_id, connection_id):
        if not self.lock.acquire(blocking=False):
            raise TransferError('已有文件传输请求正在执行', 409)
        try:
            state = self._state(transfer_id, connection_id, allow_failed=True)
            self._cleanup(state)
            self.state = None
            return {'id': transfer_id, 'aborted': True}
        finally:
            self.lock.release()
