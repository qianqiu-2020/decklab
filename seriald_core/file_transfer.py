"""Chunked, framed file transfer over the collector's existing serial reader."""
import base64
import binascii
import hashlib
import gzip
import re
import secrets
import shlex
import threading
import time
import zlib
from .transactions import transactions_for, TransactionError


CHUNK_SIZE = 10240  # Large reads; uploads split when their encoded shell line is too long.
BATCH_SIZE = 3 * CHUNK_SIZE  # Fits the 64 KiB HTTP body limit even without compression.
MAX_CONSOLE_LINE = 4095  # Typical canonical tty input limit, including framed command.
COMPRESS_RATIO = 0.80  # Save enough wire time to justify a gzip process per chunk.
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
    # Multiline batches echo later shell commands inside the response frame.
    # The last complete field is the command's actual result, after its echo.
    output = re.sub(r'[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]', '', output)
    output = output.replace('\r\n', '\n').replace('\r', '\n')
    matches = re.findall(r'(?m)^' + re.escape(name) + r'=([^\r\n]*)$', output)
    return matches[-1] if matches else None


class SerialFileTransfer:
    """One in-flight transfer per physical connection; commands share its reader."""

    def __init__(self, device):
        self.device = device
        self.transactions = transactions_for(device)
        self._opening_connection = None
        self._opening_actor = 'web'
        self._opening_client = ''
        self._opening_visibility = None
        self.lock = threading.RLock()
        self.state = None
        self._expiry_timer = None

    @property
    def active(self):
        with self.lock:
            return self.state is not None

    def _run(self, command, timeout=45, keep_private=False):
        device = self.device
        visibility = getattr(device, 'visibility', None)
        token = None
        ok = False
        try:
            state = self.state
            with self.transactions.begin('file-transfer', timeout,
                    actor=state['actor'] if state else self._opening_actor, allow_transfer=True,
                    client_id=state.get('client_id', '') if state else self._opening_client,
                    expected_connection=state['connection_id'] if state else self._opening_connection):
                device.probing = True
                try:
                    if visibility:
                        token = visibility.begin('file-transfer')
                    ok, output = device._run_cmd_locked(command, timeout, ensure_shell=state is None)
                    if visibility:
                        visibility.report(output, command)
                finally:
                    if visibility:
                        if keep_private and ok:
                            self._opening_visibility = token
                        else:
                            visibility.finish(token)
                    device.probing = False
        except TransactionError as error:
            raise TransferError(str(error), 502 if error.code == 'timeout' else error.status) from None
        if not ok:
            raise TransferError(output, 502)
        return output

    def _check_connection(self, connection_id):
        device = self.device
        if (not connection_id or connection_id != device.connection_id
                or not device.present or device.fd is None or device.stop.is_set()):
            raise TransferError('设备连接已变化，请重新选择设备', 409)

    def _schedule_expiry(self, transfer_id, delay=IDLE_SECONDS):
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
        timer = threading.Timer(delay, self._expire, args=(transfer_id,))
        timer.daemon = True
        self._expiry_timer = timer
        timer.start()

    def _release(self):
        """Release the reservation and its timer while the transfer lock is held."""
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        if self.state and getattr(self.device, 'visibility', None):
            self.device.visibility.finish(self.state.get('visibility_token'))
        self.state = None

    def release_local(self, transfer_id, connection_id):
        """Retire this reservation without issuing commands on a disconnected board."""
        with self.lock:
            state = self.state
            if not state or state['id'] != transfer_id or state['connection_id'] != connection_id:
                return False
            self._release()
            return True

    def _expire(self, transfer_id):
        with self.lock:
            state = self.state
            if not state or state['id'] != transfer_id:
                return
            remaining = IDLE_SECONDS - (time.monotonic() - state['last'])
            if remaining > 0:
                self._schedule_expiry(transfer_id, remaining)
                return
            self._cleanup(state)
            self._release()

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

    def _upload_prefix_matches(self, state, size, digest):
        """Resolve an ambiguous serial acknowledgement without replaying writes.

        The board may have completed a batch even when its beginning marker was
        lost from the UART output. Re-sending that batch would duplicate bytes.
        """
        temp = shlex.quote(state['temp'])
        command = ('if [ -f {0} ]; then s=$(wc -c < {0}); '
                   'h=$(sha256sum {0} | cut -d\x27 \x27 -f1); '
                   'printf "SD_SIZE=%s\\nSD_SHA=%s\\n" "$s" "$h"; fi').format(temp)
        try:
            output = self._run(command, timeout=120)
        except TransferError:
            return False
        return _field(output, 'SD_SIZE') == str(size) and _field(output, 'SD_SHA') == digest

    def _upload_promoted_matches(self, state, digest):
        """Check an uncertain final acknowledgement without repeating the rename."""
        target, temp = shlex.quote(state['path']), shlex.quote(state['temp'])
        command = ('if [ -f {dst} ] && [ ! -e {tmp} ] && [ ! -L {tmp} ]; then '
                   's=$(wc -c < {dst}); h=$(sha256sum {dst} | cut -d\x27 \x27 -f1); '
                   'printf "SD_SIZE=%s\\nSD_SHA=%s\\n" "$s" "$h"; fi').format(
                       dst=target, tmp=temp)
        try:
            output = self._run(command, timeout=120)
        except TransferError:
            return False
        return (_field(output, 'SD_SIZE') == str(state['size'])
                and _field(output, 'SD_SHA') == digest)

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
            self._opening_connection, self._opening_actor = connection_id, actor
            self._opening_client = self.transactions.client_id if actor == 'web' else ''
            if self.state:
                raise TransferError('此串口已有文件传输，请完成或取消后再试', 409)
            visibility = getattr(self.device, 'visibility', None)
            self._opening_visibility = None
            transfer_id = secrets.token_hex(12)
            state = {'id': transfer_id, 'direction': direction, 'path': path,
                     'connection_id': connection_id, 'actor': actor,
                     'client_id': self._opening_client,
                     'offset': 0, 'last': time.monotonic()}
            quoted = shlex.quote(path)
            if direction == 'upload':
                temp = path + '.seriald-' + transfer_id + '.part'
                command = ('umask 077; if [ -d {dst} ] || '
                           '{exists}; then printf "SD_ERROR=exists\\n"; '
                           'elif (set -C; : > {tmp}); then '
                           'if command -v gzip >/dev/null 2>&1; then g=1; else g=0; fi; '
                           'printf "SD_OK=1\\nSD_GZIP=%s\\n" "$g"; '
                           'else printf "SD_ERROR=create\\n"; fi').format(
                    dst=quoted,
                    exists=('false' if overwrite else '[ -e {0} ] || [ -L {0} ]'.format(quoted)),
                    tmp=shlex.quote(temp))
                output = self._run(command, keep_private=True)
                if _field(output, 'SD_OK') != '1':
                    raise TransferError('无法创建板端临时文件：' + (_field(output, 'SD_ERROR') or output[-200:]), 409)
                state.update(temp=temp, size=size, hasher=hashlib.sha256(), overwrite=bool(overwrite),
                             gzip=_field(output, 'SD_GZIP') == '1')
            else:
                command = ('if [ -f {0} ] && [ -r {0} ]; then '
                           's=$(wc -c < {0}); h=$(sha256sum {0} | cut -d\x27 \x27 -f1); '
                           'if command -v gzip >/dev/null 2>&1; then g=1; else g=0; fi; '
                           'printf "SD_SIZE=%s\\nSD_SHA=%s\\nSD_GZIP=%s\\n" "$s" "$h" "$g"; '
                           'else printf "SD_ERROR=unreadable\\n"; fi').format(quoted)
                output = self._run(command, timeout=120, keep_private=True)
                raw_size, digest = _field(output, 'SD_SIZE'), _field(output, 'SD_SHA')
                if not raw_size or not raw_size.isdigit() or not digest or not HEX_SHA256.fullmatch(digest):
                    raise TransferError('板端文件不可读，或缺少 wc/sha256sum：'
                                        + (_field(output, 'SD_ERROR') or output[-200:]), 409)
                state.update(size=int(raw_size), sha256=digest,
                             gzip=_field(output, 'SD_GZIP') == '1', compress=False)
            if self._opening_visibility:
                state['visibility_token'] = self._opening_visibility
            self.state = state
            if hasattr(self.device, '_say'):
                self.device._say('串口文件%s：%s · %s bytes' % (
                    '上传' if direction == 'upload' else '下载', path, state['size']))
            self._schedule_expiry(transfer_id)
            return {'id': transfer_id, 'direction': direction, 'path': path,
                    'size': state['size'], 'sha256': state.get('sha256'),
                    'chunk_size': CHUNK_SIZE, 'batch_size': BATCH_SIZE, 'offset': 0,
                    'gzip_available': state['gzip']}
        finally:
            token = self._opening_visibility
            if token and (not self.state or self.state.get('visibility_token') != token):
                visibility.finish(token)
            self._opening_visibility = None
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
                if not data or len(data) > expected:
                    raise TransferError('上传分块长度不匹配')
                packed = gzip.compress(data, compresslevel=6, mtime=0) if state['gzip'] else data
                compressed = len(packed) <= len(data) * COMPRESS_RATIO
                encoded = base64.b64encode(packed if compressed else data).decode('ascii')
                decode = 'base64 -d | gzip -d' if compressed else 'base64 -d'
                command = ('printf %s {0} | {2} >> {1}; r=$?; '
                           's=$(wc -c < {1}); printf "SD_RC=%s\\nSD_SIZE=%s\\n" "$r" "$s"').format(
                    shlex.quote(encoded), shlex.quote(state['temp']), decode)
                # _run_cmd_locked adds framing and the final shell newline.
                # Reject before sending so clients can retry a smaller raw slice.
                if len(command.encode('utf-8')) + 120 > MAX_CONSOLE_LINE:
                    raise TransferError('分块编码后超过串口终端行长，请缩小分块', 413)
                frame_error = ''
                try:
                    output = self._run(command)
                except TransferError as error:
                    if error.status != 502:
                        state['failed'] = True
                        raise
                    frame_error = str(error)
                    output = ''
                next_hash = state['hasher'].copy()
                next_hash.update(data)
                if (_field(output, 'SD_RC') != '0' or
                        _field(output, 'SD_SIZE') != str(offset + len(data))):
                    if not self._upload_prefix_matches(state, offset + len(data), next_hash.hexdigest()):
                        state['failed'] = True
                        raise TransferError('板端写入分块结果不确定，校验未通过' +
                                            ('：' + frame_error if frame_error else ''), 502)
                state['hasher'] = next_hash
            else:
                index = offset // CHUNK_SIZE
                encode = 'gzip -c | base64' if state['compress'] else 'base64'
                command = ('if [ -f {0} ]; then '
                           'd=$(dd if={0} bs={1} skip={2} count=1 2>/dev/null | {3} | tr -d "\\r\\n"); '
                           'printf "SD_DATA=%s\\n" "$d"; '
                           'else printf "SD_ERROR=missing\\n"; fi').format(
                    shlex.quote(state['path']), CHUNK_SIZE, index, encode)
                output = self._run(command)
                encoded = _field(output, 'SD_DATA')
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (TypeError, ValueError, binascii.Error):
                    raise TransferError('板端返回的分块损坏', 502) from None
                if state['compress']:
                    try:
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        data = decoder.decompress(data, CHUNK_SIZE + 1)
                        if len(data) > CHUNK_SIZE or not decoder.eof or decoder.unused_data:
                            raise zlib.error('invalid gzip chunk length or trailer')
                    except zlib.error:
                        raise TransferError('板端返回的 gzip 分块损坏', 502) from None
                if len(data) != expected:
                    raise TransferError('板端文件已变化，分块长度不匹配', 409)
                if state['gzip']:
                    packed = gzip.compress(data, compresslevel=6, mtime=0)
                    state['compress'] = len(packed) <= len(data) * COMPRESS_RATIO
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

    def batch(self, transfer_id, connection_id, offset, data_b64):
        """Write several console lines in one framed transaction and acknowledge once."""
        if not self.lock.acquire(blocking=False):
            raise TransferError('已有文件传输请求正在执行', 409)
        try:
            state = self._state(transfer_id, connection_id)
            if state['direction'] != 'upload':
                raise TransferError('批量写入只支持上传')
            if type(offset) is not int or offset != state['offset'] or offset >= state['size']:
                raise TransferError('分块偏移不匹配', 409)
            try:
                data = base64.b64decode(data_b64, validate=True)
            except (TypeError, ValueError, binascii.Error):
                raise TransferError('批量数据不是有效 base64') from None
            if not data or len(data) > BATCH_SIZE or offset + len(data) > state['size']:
                raise TransferError('批量数据长度无效', 413)
            temp = shlex.quote(state['temp'])
            lines = ['r=0;']
            pending = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
            while pending:
                part = pending.pop(0)
                packed = gzip.compress(part, compresslevel=6, mtime=0) if state['gzip'] else part
                compressed = len(packed) <= len(part) * COMPRESS_RATIO
                encoded = base64.b64encode(packed if compressed else part).decode('ascii')
                decode = 'base64 -d | gzip -d' if compressed else 'base64 -d'
                line = ('if [ "$r" = 0 ]; then printf %s {0} | {1} >> {2} || r=1; fi;'
                        .format(shlex.quote(encoded), decode, temp))
                if len(line.encode('utf-8')) + 120 > MAX_CONSOLE_LINE:
                    if len(part) == 1:
                        raise TransferError('分块编码后超过串口终端行长', 413)
                    half = len(part) // 2
                    pending[:0] = [part[:half], part[half:]]
                    continue
                lines.append(line)
            lines.append('s=$(wc -c < {0}); printf "SD_RC=%s\\nSD_SIZE=%s\\n" "$r" "$s"'.format(temp))
            frame_error = ''
            try:
                output = self._run('\n'.join(lines), timeout=90)
            except TransferError as error:
                if error.status != 502:
                    state['failed'] = True
                    raise
                frame_error = str(error)
                output = ''
            next_hash = state['hasher'].copy()
            next_hash.update(data)
            if _field(output, 'SD_RC') != '0' or _field(output, 'SD_SIZE') != str(offset + len(data)):
                if not self._upload_prefix_matches(state, offset + len(data), next_hash.hexdigest()):
                    state['failed'] = True
                    raise TransferError('板端批量写入结果不确定，校验未通过' +
                                        ('：' + frame_error if frame_error else ''), 502)
            state['hasher'] = next_hash
            state['offset'] += len(data)
            state['last'] = time.monotonic()
            return {'id': transfer_id, 'offset': state['offset'], 'size': state['size'],
                    'bytes': len(data)}
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
                frame_error = ''
                try:
                    output = self._run(command, timeout=120)
                except TransferError as error:
                    if error.status != 502:
                        raise
                    frame_error = str(error)
                    output = ''
                if _field(output, 'SD_OK') != '1':
                    if (_field(output, 'SD_ERROR') or
                            not self._upload_promoted_matches(state, digest)):
                        raise TransferError('板端校验或落盘失败：' +
                                            (_field(output, 'SD_ERROR') or frame_error or output[-200:]), 502)
            else:
                digest = state.get('hasher', hashlib.sha256()).hexdigest()
                if digest != state['sha256']:
                    raise TransferError('串口传输内容的 SHA-256 校验失败', 502)
                command = ('s=$(wc -c < {0}); h=$(sha256sum {0} | cut -d\x27 \x27 -f1); '
                           'printf "SD_SIZE=%s\\nSD_SHA=%s\\n" "$s" "$h"').format(path)
                output = self._run(command, timeout=120)
                if _field(output, 'SD_SIZE') != str(state['size']) or _field(output, 'SD_SHA') != digest:
                    raise TransferError('板端文件在传输期间发生变化', 409)
            self._release()
            if hasattr(self.device, '_say'):
                self.device._say('串口文件传输完成：%s · %s bytes · SHA-256 %s' % (
                    state['path'], state['size'], digest))
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
            self._release()
            if hasattr(self.device, '_say'):
                self.device._say('串口文件传输已取消：' + state['path'])
            return {'id': transfer_id, 'aborted': True}
        finally:
            self.lock.release()
