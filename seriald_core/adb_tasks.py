"""Run one identity-pinned ADB shell command as a durable board task."""
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

from .board_cases import READ_IDENTITY, find_transport
from .board_maintenance import maintenance_for
from .agent_grants import GrantRevoked
from .flash_store import validate_request
from .tasks import TaskCancelled, TaskError
from .transactions import TransactionError


OUTPUT_BYTES = 4096
ADB_ENV = {'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:5037'}


def serial_window(device):
    return {'connection_id': device.connection_id, 'start_off': device.now_off()}


class Output:
    def __init__(self):
        self.head = self.tail = b''
        self.size = 0
        self.lock = threading.Lock()

    def read(self, source):
        with source:
            while chunk := source.read1(8192):
                with self.lock:
                    self.head += chunk[:OUTPUT_BYTES - len(self.head)]
                    self.tail = (self.tail + chunk)[-128:]
                    self.size += len(chunk)

    def snapshot(self, frame=None):
        with self.lock:
            match = frame.search(self.tail) if frame else None
            size = self.size - len(match[0]) if match else self.size
            return self.head[:size].decode('utf-8', 'replace'), max(0, size - len(self.head)), match


def remote_command(command, serial, boot_id, marker):
    # The ADB server can assign a new transport ID after a restart. Repeat
    # identity and boot checks in the shell that actually runs the command.
    return READ_IDENTITY + (
        'boot=$(cat /proc/sys/kernel/random/boot_id)\n'
        'if [ "$serial" != ' + shlex.quote(serial) + ' ] || [ "$boot" != ' + shlex.quote(boot_id) + ' ]; then\n'
        "    printf '\\036" + marker + ":rejected\\n'; exit 0\n"
        'fi\n'
        '/bin/sh -c ' + shlex.quote(command) + ' </dev/null\n'
        "printf '\\036" + marker + ":%s\\n' \"$?\"\n")


class AdbCommand:
    def __init__(self, manager, device, executable, port, facts, registered, command, timeout):
        self.manager, self.device, self.executable = manager, device, executable
        self.serial, self.connection, self.stop = device.serial, device.connection_id, device.stop
        self.port, self.boot_id, self.registered = port, facts['boot_id'], registered
        self.command, self.timeout = command, timeout
        self.maintenance = maintenance_for(manager)

    def check_board(self):
        if (self.device.stop is not self.stop or self.stop.is_set() or not self.device.present
                or self.device.serial != self.serial or self.device.connection_id != self.connection
                or getattr(self.device.board, 'connection', None) is not self.device
                or self.manager.store.has_conflict(self.serial)):
            raise TaskError('板卡串口连接已变化，命令未执行', 409)
        if self.manager.hardware.board_tests.get(self.serial) != self.registered:
            raise TaskError('USB 调试接线配置已变化，命令未执行', 409)

    def run(self, context):
        confirmed = True
        receipt = {'command': self.command, 'state': 'checking', 'usb_port': self.port,
                   'stdout': '', 'stderr': '', 'exit_code': None}
        result = {'adb_receipt': receipt}

        def save(outcome=None):
            context.store.update(context.id, result=result, outcome=outcome)

        try:
            save()
            self.check_board()
            try:
                _, endpoint, facts = find_transport(self.executable, self.serial, {'usb_port': self.port})
            except (OSError, ConnectionError, subprocess.TimeoutExpired) as error:
                receipt['state'] = 'unavailable'
                save()
                raise TaskError('原 USB 调试连接不可用，命令未执行', 409) from error
            if facts['boot_id'] != self.boot_id:
                raise TaskError('板卡已重新启动，命令未执行', 409)
            self.check_board()
            marker = 'DECKLAB-ADB-' + secrets.token_hex(12)
            frame = re.compile(rb'\x1e' + marker.encode() + rb':(rejected|[0-9]{1,3})\r?\n\Z')
            receipt.update(state='running', started_at=time.time(), transport_id=endpoint,
                           serial_window=serial_window(self.device))
            result['cleanup_confirmed'] = False
            save()
            context.issued()
            try:
                process = subprocess.Popen([self.executable, '-t', endpoint, 'shell', '-T',
                    remote_command(self.command, self.serial, self.boot_id, marker)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                    env={**os.environ, **ADB_ENV})
            except OSError as error:
                receipt['state'] = 'unavailable'
                result['cleanup_confirmed'] = True
                save('not-issued')
                raise TaskError('控制机无法启动 ADB，命令未执行', 409) from error
            confirmed = False
            return self.wait(process, context, receipt, result, frame, save)
        except TaskError:
            if receipt['state'] == 'checking':
                receipt.update(state='rejected', finished_at=time.time())
                save()
            raise
        finally:
            self.maintenance.release(context.id, confirmed=confirmed or result.get('cleanup_confirmed') is True)

    def wait(self, process, context, receipt, result, frame, save):
        out, err = Output(), Output()
        readers = [threading.Thread(target=stream.read, args=(pipe,), daemon=True)
                   for stream, pipe in ((out, process.stdout), (err, process.stderr))]
        for reader in readers:
            reader.start()

        def capture():
            receipt['stdout'], receipt['stdout_truncated_bytes'], match = out.snapshot(frame)
            receipt['stderr'], receipt['stderr_truncated_bytes'], _ = err.snapshot()
            return match

        deadline = time.monotonic() + self.timeout
        reason = 'unconfirmed'
        interrupted = None
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'timed-out'
                    break
                if context.cancelled.wait(min(remaining, 0.5)):
                    reason = 'cancelled'
                    break
                try:
                    context.check()
                except (TaskCancelled, TaskError, GrantRevoked, TransactionError) as error:
                    reason, interrupted = 'cancelled', error
                    break
                capture()
                save()
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
            for reader in readers:
                reader.join(1)
        match = capture()
        receipt.update(adb_exit_code=process.returncode, finished_at=time.time())
        if match:
            result['cleanup_confirmed'] = True
            if match[1] == b'rejected':
                receipt['state'] = 'rejected'
                save('not-issued')
                raise TaskError('板端身份或启动编号已变化，命令未执行', 409)
            receipt.update(state='exited', exit_code=int(match[1]))
            save('observed')
            return result
        if reason == 'unconfirmed' and re.search(
                r'\b(?:closed|offline|disconnected|transport error|no devices)\b', receipt['stderr'], re.I):
            reason = 'disconnected'
        receipt['state'] = reason
        save()
        if reason == 'cancelled':
            raise TaskCancelled('ADB 已停止等待，板端结果未知') from interrupted
        raise TaskError('ADB 未收到退出回执，请从串口核对原命令', 504 if reason == 'timed-out' else 502)


def start(manager, device, payload, actor):
    if set(payload) - {'kind', 'key', 'serial', 'connection_id', 'actor', 'client_id',
                       'request_id', 'command', 'timeout', 'execution_lease'}:
        raise TaskError('invalid ADB command parameters')
    if device is None or not device.serial or payload.get('serial') != device.serial:
        raise TaskError('ADB 命令需要已识别的板卡', 409)
    command, timeout = payload.get('command'), payload.get('timeout', 60)
    if (not isinstance(command, str) or not command.strip() or '\0' in command
            or len(command.encode('utf-8', 'surrogatepass')) > 2048
            or any(0xd800 <= ord(char) <= 0xdfff for char in command)
            or type(timeout) is not int or not 1 <= timeout <= 3600):
        raise TaskError('请填写不超过 2048 字节的命令，等待时限为 1–3600 秒')
    request_id = payload.get('request_id')
    digest = manager.tasks.store.request_digest(
        {'kind': 'adb-command', 'serial': device.serial, 'connection_id': payload.get('connection_id'),
         'command': command, 'timeout': timeout})
    validate_request(request_id, digest)
    previous = manager.tasks.store.find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    if not device.present or device.stop.is_set() or payload.get('connection_id') != device.connection_id:
        raise TaskError('板卡串口连接已变化', 409)
    if manager.store.has_conflict(device.serial):
        raise TaskError('板卡身份存在冲突，无法执行 ADB 命令', 409)
    client = payload.get('client_id', '') if actor == 'web' else ''
    if not isinstance(client, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,80}', client):
        raise TaskError('invalid task client')
    executable = shutil.which('adb')
    if not executable:
        raise TaskError('控制机缺少 adb', 409)
    registered = manager.hardware.board_tests.get(device.serial)
    try:
        port, _, facts = find_transport(executable, device.serial, registered)
    except (OSError, ConnectionError, subprocess.TimeoutExpired) as error:
        raise TaskError('未找到与当前板卡匹配的在线 USB 调试连接', 409) from error
    operation = AdbCommand(manager, device, executable, port, facts, registered, command, timeout)
    if payload['connection_id'] != operation.connection:
        raise TaskError('板卡串口连接已变化', 409)
    operation.check_board()
    return manager.tasks.start_board('adb-command', actor, client, device.serial, operation.run,
        request_id=request_id, input_digest=digest, details={'attempt_id': request_id},
        resources=['board-test-usb:' + port], maintenance=operation.maintenance,
        connection_id=operation.connection, tracks_outcome=True)
