"""Durable command evidence from the shared console transaction."""
import re
import secrets
import shlex
import time

from .adb_tasks import OUTPUT_BYTES, command_input, serial_window, titanx_context
from .api_contract import RESERVED_COMMAND_MAX_SECONDS
from .board_ownership import ownership_for
from .flash_store import validate_request
from .tasks import TaskError
from .transactions import TransactionError, transactions_for


def start(manager, device, payload, actor):
    if set(payload) - {'kind', 'key', 'serial', 'connection_id', 'actor', 'client_id',
                       'request_id', 'command', 'timeout', 'titanx', 'stdin_artifact'}:
        raise TaskError('invalid serial command parameters')
    if device is None or not device.serial or payload.get('serial') != device.serial:
        raise TaskError('串口命令需要已识别的板卡', 409)
    command, timeout = payload.get('command'), payload.get('timeout', 60)
    if (not isinstance(command, str) or not command.strip()
            or any(char != '\n' and (ord(char) < 32 or ord(char) == 127) for char in command)
            or len(command.encode('utf-8', 'surrogatepass')) > 2048
            or any(0xd800 <= ord(char) <= 0xdfff for char in command)
            or type(timeout) is not int or not 1 <= timeout <= RESERVED_COMMAND_MAX_SECONDS):
        raise TaskError('请填写不超过 2048 字节的命令，等待时限为 1 秒至 7 天')
    request_id = payload.get('request_id')
    serial, connection, stop = device.serial, device.connection_id, device.stop
    titanx = titanx_context(payload['titanx']) if 'titanx' in payload else None
    identity = {'kind': 'serial-command', 'serial': serial, 'connection_id': payload.get('connection_id'),
                'command': command, 'timeout': timeout, 'titanx': titanx}
    stdin = command_input(manager, device, payload, actor, identity)
    digest = manager.tasks.store.request_digest(identity)
    validate_request(request_id, digest)
    previous = manager.tasks.store.find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    if not device.present or stop.is_set() or payload.get('connection_id') != connection:
        raise TaskError('板卡串口连接已变化', 409)
    client = payload.get('client_id', '') if actor == 'web' else ''
    if not isinstance(client, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,80}', client):
        raise TaskError('invalid task client')
    ownership = ownership_for(manager)
    owner = ownership.current(serial) if ownership else None
    if not owner or owner['state'] != 'active':
        raise TaskError('串口命令需要当前板卡执行占用', 423)
    ownership.check(serial, actor, client)
    if stdin and stdin['state'] != 'ready':
        raise TaskError('串口输入文件未就绪', 409)
    transactions = transactions_for(device)

    def check_target():
        if (device.connection_id != connection or device.stop is not stop or stop.is_set()
                or device.serial != serial or device.board.connection is not device
                or manager.store.has_conflict(serial)):
            raise TaskError('板卡串口连接或身份已变化', 409)

    def run(context):
        receipt = {'command': command, 'state': 'checking', 'stdout': '', 'exit_code': None,
                   'reservation_id': owner['id']}
        if titanx:
            receipt['titanx'] = titanx
        result = {'serial_receipt': receipt}
        context.store.update(context.id, result=result)
        issued = False
        nonce = secrets.token_hex(12)
        marker = '__DECKLAB_SERIAL_RC_' + nonce + '__'
        # Keep the marker split in the echoed command. The child cannot change
        # the interactive shell's state or consume later terminal input.
        child = 'sh -c ' + shlex.quote(command)
        if stdin:
            from .transfer_tasks import TransferFile
            # Stage over the pinned UART using its chunked, checksummed file
            # protocol. Unlink the private file as soon as the shell opens it.
            path = '/tmp/.decklab-stdin-' + nonce
            receipt.update(stdin_size=stdin['size'], stdin_sha256=stdin['sha256'], stdin_path=path)
            if stdin.get('name') == 'titanx-case-script' and command.startswith("file='/tmp/shell-cases/"):
                with manager.artifacts.open(stdin['id'], actor, serial) as source:
                    receipt['script_preview'] = source.read(65536).decode('utf-8', 'replace')
            context.store.update(context.id, result=result)
            transfer = TransferFile(manager, device, actor, client, 'upload', path, stdin['id'],
                                    snapshot=(connection, stop, serial))
            context.check()
            check_target()
            transfer.run(context)
            child = '{ rm -f ' + path + ' && ' + child + '; } <' + path
        else:
            child += ' </dev/null'
        framed = child + "; printf '\\n%s%s:%s\\n' '__DECKLAB_SERIAL_RC_' '" + nonce + "__' \"$?\""

        def stage(phase):
            nonlocal issued
            if phase == 'sending-probe':
                context.check()
                receipt.update(state='running', started_at=time.time(), serial_window=serial_window(device))
                result['cleanup_confirmed'] = False
                context.store.update(context.id, result=result, outcome='issued')
                issued = True
            context.stage(phase)

        # The existing console lock spans login, write and response. Keep
        # uncertain operations protected before yielding the terminal lock.
        try:
            with transactions.actor_scope(actor, client), transactions.task_scope(context.cancelled, connection, stop):
                with transactions.begin('serial-command', timeout, expected_connection=connection):
                    device.probing = True
                    try:
                        context.check()
                        check_target()
                        ok, output = device.console.run_locked(framed, timeout, on_stage=stage)
                        if not ok:
                            raise TaskError(output, 502)
                        suffix = re.search(r'\n' + re.escape(marker) + r':([0-9]{1,3})\n', output)
                        if suffix is None or int(suffix[1]) > 255:
                            raise TaskError('串口未收到板端退出回执，请从原串口核对', 502)
                        data = output[:suffix.start()].encode('utf-8')
                        receipt.update(state='exited', exit_code=int(suffix[1]), finished_at=time.time(),
                                       end_off=device.now_off(), stdout=data[:OUTPUT_BYTES].decode('utf-8', 'replace'),
                                       stdout_truncated_bytes=max(0, len(data) - OUTPUT_BYTES))
                        result['cleanup_confirmed'] = True
                        context.store.update(context.id, result=result, outcome='observed')
                    finally:
                        device.probing = False
                        if issued and result.get('cleanup_confirmed') is not True:
                            receipt.update(state='unconfirmed', finished_at=time.time())
                            context.store.update(context.id, result=result)
                            context.retain_cleanup_failure(serial)
        except TransactionError:
            if result.get('cleanup_confirmed') is not True:
                raise
        finally:
            if stdin and not issued:
                # Only clean up this task's private input on its original board.
                # If the board has gone away, retain the path in the receipt.
                try:
                    check_target()
                    with transactions.actor_scope(actor, client):
                        device._run_cmd('rm -f ' + path, timeout=5, expected_connection=connection)
                except (TaskError, TransactionError, OSError):
                    pass
        return result

    return manager.tasks.start('serial-command', actor, client,
        [{'serial': serial, 'connection_id': connection}], run, request_id=request_id,
        input_digest=digest, tracks_outcome=True)
