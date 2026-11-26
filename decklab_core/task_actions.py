"""Async operation adapters; all hardware work still uses DeviceActions."""
import re
from .actions import DeviceActions, validate_identify_request
from .results import response
from .tasks import TaskCancelled, TaskError
from .transactions import transactions_for, TransactionError
from .transfer_tasks import TransferCopy, TransferFile
from .file_transfer import TransferError
from .hardware import ACTIONS, HardwareOperation
from .flash_tasks import FlashActions


class TaskActions:
    kinds = frozenset(('identify', 'ssh-enable', 'ssh-connect', 'adb-connect', 'file-copy', 'file-upload', 'file-download', 'flash', 'nor-backup', 'board-test', 'adb-command')) | frozenset(
        'hardware-' + action for action in ACTIONS)

    def __init__(self, manager):
        self.manager = manager

    @property
    def tasks(self):
        tasks = getattr(self.manager, 'tasks', None)
        if tasks is None:
            raise TaskError('task service unavailable', 503)
        return tasks

    def start(self, payload, device, actor):
        try:
            if not isinstance(payload, dict):
                raise TaskError('JSON object required')
            kind = payload.get('kind')
            if not isinstance(kind, str) or kind not in self.kinds:
                raise TaskError('unsupported task kind')
            if not isinstance(actor, str) or not actor or len(actor) > 80:
                raise TaskError('invalid task actor')
            if kind == 'adb-command':
                from .adb_tasks import start
                self.tasks._check_actor(actor)
                return response(202, {'ok': True, 'task': start(self.manager, device, payload, actor)})
            if kind == 'board-test':
                if payload.get('source') == 'titanx':
                    from .team_run import start
                else:
                    from .board_cases import start
                if device is None or not device.serial or payload.get('serial') != device.serial:
                    raise TaskError('板卡用例需要已识别的设备', 404)
                if not device.present or payload.get('connection_id') != device.connection_id:
                    raise TaskError('板卡串口连接已变化', 409)
                return response(202, {'ok': True, 'task': start(self.manager, device, payload, actor)})
            if kind == 'nor-backup':
                return FlashActions(self.manager).backup(payload, device, actor)
            if kind == 'flash':
                return FlashActions(self.manager).start(payload, device, actor)
            self.tasks._check_actor(actor)
            request_id, input_digest = payload.get('request_id'), None
            if request_id is not None:
                from .flash_store import validate_request
                validate_request(request_id, '0' * 64)
                # Actor is pinned by authorization; mutable labels and browser
                # page IDs are routing/ownership metadata, not operation input.
                # All remaining inputs, including optional SSH credentials and
                # execution lease proofs, only enter a private HMAC in memory.
                document = {key: value for key, value in payload.items()
                            if key not in ('request_id', 'actor', 'client_id', 'key', 'to_key')}
                input_digest = self.tasks.store.request_digest(document)
                previous = self.tasks.store.find_request(actor, request_id, input_digest)
                if previous is not None:
                    return response(202, {'ok': True, 'task': self.tasks.store.get(previous)})
            submission = dict(request_id=request_id, input_digest=input_digest, tracks_outcome=True)
            if kind == 'identify':
                invalid = validate_identify_request(payload, actor)
                if invalid is not None:
                    return invalid
            elif 'association' in payload:
                raise TaskError('identity association requires an identify task')
            client = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid task client')
            connection, stop = device.connection_id, device.stop
            source_serial = device.serial or ''
            hardware = kind.startswith('hardware-')
            if (payload.get('connection_id') != connection or
                    (not hardware and (not device.present or stop.is_set()))):
                raise TaskError('device connection changed', 409)
            if hardware and not source_serial:
                raise TaskError('hardware operations require an identified board serial', 409)
            transactions = transactions_for(device)
            with transactions.actor_scope(actor, client):
                transactions.check_owner(actor)
            if kind.startswith('hardware-'):
                if set(payload) - {'kind', 'key', 'serial', 'connection_id', 'actor', 'client_id', 'request_id',
                                   'execution_lease', 'wiring_revision'}:
                    raise TaskError('hardware task accepts only a configured action and target')
                operation = HardwareOperation(self.manager, device, kind[9:], actor, client)
                if 'wiring_revision' in payload and payload['wiring_revision'] != (
                        operation.book.wiring(source_serial) or {}).get('revision', ''):
                    raise TaskError('接线已更新，请重新打开电源与启动并核对槽位', 409)
                task = self.tasks.start(kind, actor, client,
                    [{'serial': source_serial, 'connection_id': connection}], operation.run, **submission)
                return response(202, {'ok': True, 'task': task})
            if kind in ('file-upload', 'file-download'):
                operation = TransferFile(self.manager, device, actor, client,
                    'upload' if kind == 'file-upload' else 'download', payload.get('path'),
                    payload.get('artifact_id'), payload.get('overwrite') is True, (connection, stop, source_serial))
                task = self.tasks.start(kind, actor, client,
                    [{'serial': source_serial, 'connection_id': connection}],
                    self._file_runner(operation.run), **submission)
                return response(202, {'ok': True, 'task': task})
            if kind == 'file-copy':
                destination = self.manager.resolve(payload.get('to_key'))
                if (destination is None or destination is device or
                        payload.get('to_connection_id') != destination.connection_id or
                        payload.get('to_serial') != (destination.serial or '') or not destination.present or
                        destination.stop.is_set()):
                    raise TaskError('destination device connection changed or invalid', 409)
                destination_transactions = transactions_for(destination)
                with destination_transactions.actor_scope(actor, client):
                    destination_transactions.check_owner(actor)
                copy = TransferCopy(self.manager, device, destination, actor, client,
                                    payload.get('path'), payload.get('to_path'), payload.get('overwrite') is True)
                copy.snapshots[device] = (connection, stop)
                copy.serials[device] = source_serial
                copy.serials[destination] = payload['to_serial'] or ''
                copy.snapshots[destination] = (payload['to_connection_id'], copy.snapshots[destination][1])
                task = self.tasks.start(kind, actor, client, [
                    {'serial': source_serial, 'connection_id': connection},
                    {'serial': payload['to_serial'] or '', 'connection_id': payload['to_connection_id']}],
                    self._file_runner(copy.run), **submission)
                return response(202, {'ok': True, 'task': task})
            # Request credentials can be needed by SSH but never enter the task record.
            request = dict(payload)
            def run(context):
                context.on_cancel(transactions._wake)
                context.check()
                if device.connection_id != connection or device.stop is not stop or stop.is_set():
                    raise TaskError('device connection changed', 409)

                start_phase = {'identify': 'checking-probe-settings', 'ssh-connect': 'checking-session-target',
                               'adb-connect': 'checking-session-target'}.get(kind)
                if start_phase:
                    context.stage(start_phase, total=1)
                    action_options = {'on_stage': lambda phase, endpoint=None: context.stage(phase, endpoint, total=1)}
                else:
                    context.progress(0, 1, kind)
                    action_options = {}
                with device.sessions_lock:
                    existing_sessions = set(device.ssh_sessions)
                try:
                    with transactions.task_scope(context.cancelled, connection, stop):
                        context.issued()
                        result = DeviceActions(self.manager).execute(kind, request, device, actor, **action_options)
                except TransactionError:
                    context.check()
                    raise
                if result.status >= 400 or not result.payload.get('ok'):
                    context.check()
                    confirmation = result.payload.get('identity_confirmation')
                    if (kind == 'identify' and actor == 'web' and isinstance(confirmation, dict)
                            and result.payload.get('code') == 'identity-confirmation-required'):
                        # A failed probe can offer human review without claiming it
                        # identified a board. Keep only bounded identity metadata.
                        fields = ('nonce', 'serial', 'label', 'hostname', 'last_seen', 'last_dev',
                                  'last_path', 'detected_at', 'expires_at', 'connection_id')
                        self.tasks.store.update(context.id, result={
                            'code': 'identity-confirmation-required',
                            'identity_confirmation': {key: confirmation[key] for key in fields
                                                      if key in confirmation}})
                    elif (kind == 'ssh-connect'
                            and result.payload.get('code') == 'ssh-authentication-failed'):
                        # Failed authentication is actionable after a page refresh.
                        # Keep the account name, never request credentials or raw output.
                        user = result.payload.get('ssh_user')
                        self.tasks.store.update(context.id, result={
                            'code': 'ssh-authentication-failed',
                            'ssh_user': user if isinstance(user, str) and len(user) <= 128 else ''})
                    raise TaskError(result.payload.get('error', 'operation failed'), result.status)
                if kind in ('ssh-connect', 'adb-connect') and context.cancelled.is_set():
                    sid = result.payload.get('sid', '')
                    if sid not in existing_sessions:
                        device.close_session(sid)
                        raise TaskCancelled('terminal connection cancelled')
                # Only operation metadata; no raw probe dumps, credentials or file contents.
                output = {key: value for key, value in result.payload.items() if key in (
                    'ok', 'device_key', 'serial', 'hostname', 'ips', 'sshd', 'identity_status',
                    'serial_source', 'sid', 'ip', 'auto_configured', 'already_connected',
                    'ssh_credentials_saved', 'ssh_credentials_note')}
                # Preserve a completed result if cancellation races the final response.
                if start_phase:
                    context.stage('probe-finished' if kind == 'identify' else 'session-ready', done=1, total=1)
                else:
                    self.tasks.store.update(context.id, progress={'done': 1, 'total': 1, 'phase': kind})
                context.mark_outcome('observed' if kind in ('identify', 'ssh-connect', 'adb-connect') else 'acknowledged')
                return output
            task = self.tasks.start(kind, actor, client, [
                {'serial': source_serial, 'connection_id': connection}], run, **submission)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, TransferError, ValueError) as error:
            return response(getattr(error, 'status', 400), {'ok': False, 'error': str(error)})

    @staticmethod
    def _file_runner(run):
        def execute(context):
            context.issued()
            result = run(context)
            # Existing file adapters compare the source/destination SHA-256 and
            # complete the transfer before returning. This is file-content
            # evidence, not a board boot or whole-storage verification claim.
            context.mark_outcome('verified')
            return result
        return execute

    def query(self, task_id=None, actor='web', serial=None, limit=20, offset=0, connection_id=None, target_serial=None,
              target_id=None, request_id=None, flash_history=False):
        try:
            self.tasks._check_actor(actor)
            if request_id is not None:
                if task_id is not None:
                    raise TaskError('select a task ID or request ID, not both')
                task_id = self.tasks.store.find_request(actor, request_id)
                if task_id is None:
                    raise TaskError('task not found', 404)
            if task_id:
                task = self.tasks.store.get(task_id)
                if target_id is not None and not any(value['target_id'] == target_id
                                                     for value in task.get('flash_targets', [])):
                    raise TaskError('task not found', 404)
                if (actor != 'web' and task['actor'] != actor or serial is not None
                        and not self.tasks.store.visible_serial(task, serial) or connection_id is not None
                        and (not task['targets'] or any(target['connection_id'] != connection_id
                                                       for target in task['targets']))):
                    raise TaskError('task not found', 404)
                return response(200, {'task': task})
            tasks = self.tasks.store.list(actor=actor if actor != 'web' else None,
                                         serials=[serial] if serial is not None else None,
                                         limit=limit, offset=offset, connection_id=connection_id, target_serial=target_serial,
                                         target_id=target_id, flash_history=flash_history)
            return response(200, {'tasks': tasks})
        except TaskError as error:
            return response(error.status, {'error': str(error)})

    def cancel(self, task_id, actor='web', serial=None, connection_id=None):
        visible = self.query(task_id, actor, serial, connection_id=connection_id)
        if visible.status != 200:
            return visible
        try:
            task = self.tasks.cancel(task_id, actor)
            return response(200, {'ok': True, 'task': task})
        except TaskError as error:
            return response(error.status, {'error': str(error)})
