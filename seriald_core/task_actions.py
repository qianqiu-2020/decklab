"""Async operation adapters; all hardware work still uses DeviceActions."""
import re
from .actions import DeviceActions
from .results import response
from .tasks import TaskCancelled, TaskError
from .transactions import transactions_for, TransactionError
from .transfer_tasks import TransferCopy
from .file_transfer import TransferError


class TaskActions:
    kinds = frozenset(('identify', 'ssh-enable', 'ssh-connect', 'file-copy'))

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
            kind = payload.get('kind')
            if not isinstance(kind, str) or kind not in self.kinds:
                raise TaskError('unsupported task kind')
            if not isinstance(actor, str) or not actor or len(actor) > 80:
                raise TaskError('invalid task actor')
            client = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client, str) or client and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):
                raise TaskError('invalid task client')
            connection, stop = device.connection_id, device.stop
            source_serial = device.serial or ''
            if payload.get('connection_id') != connection or not device.present or stop.is_set():
                raise TaskError('device connection changed', 409)
            transactions = transactions_for(device)
            with transactions.actor_scope(actor, client):
                transactions.check_owner(actor)
            if kind == 'file-copy':
                destination = self.manager.resolve(payload.get('to_key'))
                if (destination is None or destination is device or
                        payload.get('to_connection_id') != destination.connection_id or
                        payload.get('to_serial') != destination.serial or not destination.present or
                        destination.stop.is_set()):
                    raise TaskError('destination device connection changed or invalid', 409)
                destination_transactions = transactions_for(destination)
                with destination_transactions.actor_scope(actor, client):
                    destination_transactions.check_owner(actor)
                copy = TransferCopy(self.manager, device, destination, actor, client,
                                    payload.get('path'), payload.get('to_path'), payload.get('overwrite') is True)
                copy.snapshots[device] = (connection, stop)
                copy.snapshots[destination] = (payload['to_connection_id'], copy.snapshots[destination][1])
                task = self.tasks.start(kind, actor, client, [
                    {'serial': source_serial, 'connection_id': connection},
                    {'serial': payload['to_serial'] or '', 'connection_id': payload['to_connection_id']}], copy.run)
                return response(202, {'ok': True, 'task': task})
            # Request credentials can be needed by SSH but never enter the task record.
            request = dict(payload)
            def run(context):
                context.on_cancel(transactions._wake)
                context.check()
                if device.connection_id != connection or device.stop is not stop or stop.is_set():
                    raise TaskError('device connection changed', 409)
                context.progress(0, 1, kind)
                with device.sessions_lock:
                    existing_sessions = set(device.ssh_sessions)
                try:
                    with transactions.task_scope(context.cancelled, connection, stop):
                        result = DeviceActions(self.manager).execute(kind, request, device, actor)
                except TransactionError:
                    context.check()
                    raise
                if result.status >= 400 or not result.payload.get('ok'):
                    context.check()
                    raise TaskError(result.payload.get('error', 'operation failed'), result.status)
                if kind == 'ssh-connect' and context.cancelled.is_set():
                    sid = result.payload.get('sid', '')
                    if sid not in existing_sessions:
                        device.close_session(sid)
                        raise TaskCancelled('SSH connection cancelled')
                # Only operation metadata; no raw probe dumps, credentials or file contents.
                output = {key: value for key, value in result.payload.items() if key in (
                    'ok', 'device_key', 'serial', 'hostname', 'ips', 'sshd', 'identity_status',
                    'serial_source', 'sid', 'ip', 'auto_configured', 'already_connected')}
                # Preserve a completed result if cancellation races the final response.
                self.tasks.store.update(context.id, progress={'done': 1, 'total': 1, 'phase': kind})
                return output
            task = self.tasks.start(kind, actor, client, [
                {'serial': source_serial, 'connection_id': connection}], run)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, TransactionError, TransferError, ValueError) as error:
            return response(getattr(error, 'status', 400), {'ok': False, 'error': str(error)})

    def query(self, task_id=None, actor='web', serial=None, limit=20, offset=0, connection_id=None, target_serial=None):
        try:
            if task_id:
                task = self.tasks.store.get(task_id)
                if (actor != 'web' and task['actor'] != actor or serial is not None
                        and not self.tasks.store.visible_serial(task, serial)):
                    raise TaskError('task not found', 404)
                return response(200, {'task': task})
            tasks = self.tasks.store.list(actor=actor if actor != 'web' else None,
                                         serials=[serial] if serial is not None else None,
                                         limit=limit, offset=offset, connection_id=connection_id, target_serial=target_serial)
            return response(200, {'tasks': tasks})
        except TaskError as error:
            return response(error.status, {'error': str(error)})

    def cancel(self, task_id, actor='web', serial=None):
        visible = self.query(task_id, actor, serial)
        if visible.status != 200:
            return visible
        try:
            task = self.tasks.cancel(task_id, actor)
            return response(200, {'ok': True, 'task': task})
        except TaskError as error:
            return response(error.status, {'error': str(error)})
