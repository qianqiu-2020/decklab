"""Run one identity-pinned ADB shell command as a durable board task."""
from contextlib import nullcontext
import os
import re
import secrets
import shlex
import subprocess
import threading
import time

from .platform_tools import adb_executable
from .api_contract import ADB_STDIN_MAX_BYTES, RESERVED_COMMAND_MAX_SECONDS
from .board_cases import READ_IDENTITY, find_transport
from .board_maintenance import maintenance_for
from .board_ownership import ownership_for
from .agent_grants import GrantRevoked
from .flash_store import validate_request
from .session_base import valid_ssh_user, valid_ssh_password
from .ssh_command import LoginDenied, command_channel, serial_ssh_identity
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


def remote_command(command, serial, boot_id, marker, *, stdin=False):
    # The ADB server can assign a new transport ID after a restart. Repeat
    # identity and boot checks in the shell that actually runs the command.
    return READ_IDENTITY + (
        'boot=$(cat /proc/sys/kernel/random/boot_id)\n'
        'if [ "$serial" != ' + shlex.quote(serial) + ' ] || [ "$boot" != ' + shlex.quote(boot_id) + ' ]; then\n'
        "    printf '\\036" + marker + ":rejected\\n'; exit 0\n"
        'fi\n'
        '/bin/sh -c ' + shlex.quote(command) + ('\n' if stdin else ' </dev/null\n')
        + "printf '\\036" + marker + ":%s\\n' \"$?\"\n")


class BoardCommand:
    def __init__(self, manager, device, executable, port, facts, registered, command, timeout, actor, stdin, titanx, kind, ssh_login, reservation_id):
        self.ssh_login = ssh_login
        self.reservation_id = reservation_id
        self.kind = kind
        self.transport = 'SSH' if kind == 'ssh-command' else 'ADB'
        self.manager, self.device, self.executable = manager, device, executable
        self.serial, self.connection, self.stop = device.serial, device.connection_id, device.stop
        self.port, self.boot_id, self.registered = port, facts['boot_id'], registered
        self.command, self.timeout = command, timeout
        self.serial_host_keys = facts.get('host_keys')
        self.actor, self.stdin, self.titanx = actor, stdin, titanx
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
        result = {self.transport.lower() + '_receipt': receipt}
        if self.reservation_id:
            receipt['reservation_id'] = self.reservation_id
        if self.titanx is not None:
            receipt['titanx'] = self.titanx
        claimed = False
        if self.stdin is not None:
            receipt.update(stdin_size=self.stdin['size'], stdin_sha256=self.stdin['sha256'])

        def save(outcome=None):
            context.store.update(context.id, result=result, outcome=outcome)

        try:
            save()
            self.check_board()
            endpoint = None
            receipt['identity_channel'] = 'uart' if self.serial_host_keys is not None else 'adb'
            if self.serial_host_keys is None:
                try:
                    _, endpoint, facts = find_transport(self.executable, self.serial, {'usb_port': self.port})
                except (OSError, ConnectionError, subprocess.TimeoutExpired) as error:
                    receipt['state'] = 'unavailable'
                    save()
                    raise TaskError('原 USB 调试连接不可用，命令未执行', 409) from error
                if facts['boot_id'] != self.boot_id:
                    raise TaskError('板卡已重新启动，命令未执行', 409)
            self.check_board()
            marker = 'DECKLAB-' + self.transport + '-' + secrets.token_hex(12)
            frame = re.compile(rb'\x1e' + marker.encode() + rb':(rejected|[0-9]{1,3})\r?\n\Z')
            receipt.update(started_at=time.time(), transport_id=endpoint,
                           serial_window=serial_window(self.device))
            source = nullcontext(subprocess.DEVNULL)
            if self.stdin is not None:
                artifact = self.manager.artifacts.claim(self.stdin['id'], context.id, self.actor, self.serial)
                claimed = True
                if (artifact['sha256'], artifact['size']) != (self.stdin['sha256'], self.stdin['size']):
                    raise TaskError('ADB 输入文件已变化', 409)
                source = self.manager.artifacts.open(artifact['id'], self.actor, self.serial, context.id)
            channel = nullcontext([self.executable, '-t', endpoint, 'shell', '-T'])
            if self.kind == 'ssh-command':
                channel = command_channel(self, endpoint, context)
            with source as input_stream, channel as argv:
                # Keep the uploaded source alongside the verified digest for case inspection.
                if (claimed and self.stdin.get('name') == 'titanx-case-script'
                        and self.command.startswith("file='/tmp/shell-cases/")):
                    receipt['script_preview'] = input_stream.read(65536).decode('utf-8', 'replace')
                    input_stream.seek(0)
                context.check()
                self.check_board()
                receipt['state'] = 'running'
                result['cleanup_confirmed'] = False
                save()
                context.issued()
                try:
                    process = subprocess.Popen([*argv,
                        remote_command(self.command, self.serial, self.boot_id, marker, stdin=claimed)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=input_stream,
                        env={**os.environ, **ADB_ENV})
                except OSError as error:
                    receipt['state'] = 'unavailable'
                    result['cleanup_confirmed'] = True
                    save('not-issued')
                    raise TaskError(f'控制机无法启动 {self.transport}，命令未执行', 409) from error
                confirmed = False
                return self.wait(process, context, receipt, result, frame, save)
        except TaskError as error:
            if receipt['state'] == 'checking':
                receipt.update(state='auth-failed' if isinstance(error, LoginDenied) else 'rejected', finished_at=time.time())
                save()
            raise
        finally:
            try:
                if claimed:
                    self.manager.artifacts.finish_claim(self.stdin['id'], context.id,
                        consumed=receipt['state'] == 'exited' and receipt['exit_code'] == 0)
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
        receipt.update(finished_at=time.time())
        receipt[self.transport.lower() + '_exit_code'] = process.returncode
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
            raise TaskCancelled(f'{self.transport} 已停止等待，板端结果未知') from interrupted
        raise TaskError(f'{self.transport} 未收到退出回执，请从串口核对原命令', 504 if reason == 'timed-out' else 502)


def titanx_context(value):
    fields = {'job_id', 'command_id', 'case_code', 'round'}
    if not isinstance(value, dict) or set(value) - fields:
        raise TaskError('无效的 TitanX 命令关联')
    for key in ('job_id', 'command_id', 'case_code'):
        if key == 'case_code' and key not in value:
            continue
        if not isinstance(value.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', value[key]):
            raise TaskError('无效的 TitanX 作业、命令或用例编号')
    if 'round' in value and (type(value['round']) is not int or not 1 <= value['round'] <= 1000000):
        raise TaskError('无效的 TitanX 轮次')
    return dict(value)


def command_input(manager, device, payload, actor, identity):
    if 'stdin_artifact' not in payload:
        return None
    artifact_id = payload['stdin_artifact']
    if not isinstance(artifact_id, str) or not artifact_id or manager.artifacts is None:
        raise TaskError('命令输入需要已暂存的文件')
    item = manager.artifacts.get(artifact_id, actor, device.serial)
    if item['direction'] != 'upload' or not item.get('sha256') or item['size'] > ADB_STDIN_MAX_BYTES:
        raise TaskError('命令输入需要已封存且不超过 1 MiB 的上传文件', 409)
    identity['stdin_sha256'] = item['sha256']
    return item


def start(manager, device, payload, actor):
    allowed = {'kind', 'key', 'serial', 'connection_id', 'actor', 'client_id',
               'request_id', 'command', 'timeout', 'execution_lease', 'stdin_artifact', 'titanx'}
    kind = payload.get('kind')
    login = None
    if kind == 'ssh-command':
        allowed.add('ssh_login')
        if 'ssh_login' in payload:
            login = payload['ssh_login']
            if (not isinstance(login, dict) or set(login) != {'user', 'password'}
                    or not valid_ssh_user(login['user']) or not valid_ssh_password(login['password'])):
                raise TaskError('SSH 作业登录参数无效')
    if set(payload) - allowed:
        raise TaskError('invalid board command parameters')
    if device is None or not device.serial or payload.get('serial') != device.serial:
        raise TaskError('ADB 命令需要已识别的板卡', 409)
    command, timeout = payload.get('command'), payload.get('timeout', 60)
    if (not isinstance(command, str) or not command.strip() or '\0' in command
            or len(command.encode('utf-8', 'surrogatepass')) > 2048
            or any(0xd800 <= ord(char) <= 0xdfff for char in command)
            or type(timeout) is not int or not 1 <= timeout <= RESERVED_COMMAND_MAX_SECONDS):
        raise TaskError('请填写不超过 2048 字节的命令，等待时限为 1 秒至 7 天，超过 1 小时需要板卡执行占用')
    request_id = payload.get('request_id')
    identity = {'kind': kind, 'serial': device.serial, 'connection_id': payload.get('connection_id'),
                'command': command, 'timeout': timeout}
    if login is not None:
        # Credentials authorize this attempt. Never persist the password or its digest.
        identity['ssh_user'] = login['user']
    titanx = titanx_context(payload['titanx']) if 'titanx' in payload else None
    if titanx is not None:
        identity['titanx'] = titanx
    stdin = command_input(manager, device, payload, actor, identity)
    digest = manager.tasks.store.request_digest(identity)
    validate_request(request_id, digest)
    previous = manager.tasks.store.find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    ownership = ownership_for(manager)
    owner = ownership.current(device.serial) if ownership else None
    if kind == 'ssh-command' or titanx is not None or timeout > 3600:
        if not owner or owner['actor'] != actor or owner['state'] != 'active':
            raise TaskError('此命令需要当前板卡执行占用', 423)
        ownership.check(device.serial, actor, payload.get('client_id', '') if actor == 'web' else '')
    if stdin is not None and stdin['state'] != 'ready':
        raise TaskError('ADB 输入文件未就绪', 409)
    if not device.present or device.stop.is_set() or payload.get('connection_id') != device.connection_id:
        raise TaskError('板卡串口连接已变化', 409)
    if manager.store.has_conflict(device.serial):
        raise TaskError('板卡身份存在冲突，无法执行 ADB 命令', 409)
    client = payload.get('client_id', '') if actor == 'web' else ''
    if not isinstance(client, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,80}', client):
        raise TaskError('invalid task client')
    executable = adb_executable()
    registered = manager.hardware.board_tests.get(device.serial)
    port = None
    try:
        if not executable:
            raise ConnectionError('控制机缺少 adb')
        port, _, facts = find_transport(executable, device.serial, registered, allow_relocation=True)
    except (ConnectionError, OSError, subprocess.TimeoutExpired) as error:
        if kind != 'ssh-command':
            raise TaskError(str(error) if isinstance(error, ConnectionError) else '控制机暂时无法读取 USB 调试连接', 409) from error
        facts = serial_ssh_identity(device, actor, client, payload['connection_id'])
    reservation = owner['id'] if owner and owner['actor'] == actor and owner['state'] == 'active' else None
    operation = BoardCommand(manager, device, executable, port, facts, registered, command, timeout,
                             actor, stdin, titanx, kind, login, reservation)
    if payload['connection_id'] != operation.connection:
        raise TaskError('板卡串口连接已变化', 409)
    operation.check_board()
    return manager.tasks.start_board(kind, actor, client, device.serial, operation.run,
        request_id=request_id, input_digest=digest, details={'attempt_id': request_id},
        resources=['board-test-usb:' + port] if port else [], maintenance=operation.maintenance,
        connection_id=operation.connection, tracks_outcome=True)
