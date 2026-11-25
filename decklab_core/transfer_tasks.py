"""Collector-owned serial copies: bounded buffering, overlap and task cancellation."""
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .actions import DeviceActions
from .file_transfer import _remote_path
from .tasks import TaskError
from .transactions import transactions_for


class TransferPort:
    """Apply identical actor, identity, generation and cancellation guards to all tasks."""
    def __init__(self, manager, device, context, actor, client_id, snapshot, cancellation=None):
        self.actions, self.device, self.context = DeviceActions(manager), device, context
        self.actor, self.client_id = actor, client_id
        self.connection, self.stop, self.serial = snapshot
        self.cancellation = cancellation if cancellation is not None else context.cancelled
        self.transactions = transactions_for(device)
        context.on_cancel(self.transactions._wake)

    def call(self, action, cleanup=False, **payload):
        device = self.device
        if (device.connection_id != self.connection or device.stop is not self.stop or self.stop.is_set()
                or (device.serial or '') != self.serial):
            raise TaskError('device connection or identity changed', 409)
        if not cleanup:
            self.context.check()
            if self.cancellation.is_set():
                raise TaskError('peer transfer failed')
        with self.transactions.task_scope(threading.Event() if cleanup else self.cancellation,
                                         self.connection, self.stop):
            result = self.actions.execute('file-transfer/' + action,
                dict(payload, connection_id=self.connection, client_id=self.client_id), device, self.actor)
        if result.status >= 400 or not result.payload.get('ok'):
            if not cleanup:
                self.context.check()
            raise TaskError(result.payload.get('error', 'transfer failed'), result.status)
        return result.payload

    def write(self, transfer, offset, data):
        if transfer.get('batch_size'):
            result = self.call('batch', id=transfer['id'], offset=offset,
                               data_b64=base64.b64encode(data).decode('ascii'))
            if result.get('offset') != offset + len(data):
                raise TaskError('destination transfer offset mismatch')
            return result
        sent = 0
        while sent < len(data):
            length = min(transfer['chunk_size'], len(data) - sent)
            while True:
                try:
                    result = self.call('chunk', id=transfer['id'], offset=offset + sent,
                        data_b64=base64.b64encode(data[sent:sent + length]).decode('ascii'))
                    break
                except TaskError as error:
                    if error.status != 413 or length == 1:
                        raise
                    length = max(1, length // 2)
            if result.get('offset') != offset + sent + length:
                raise TaskError('destination transfer offset mismatch')
            sent += length
        return result

    def cleanup(self, transfer):
        if transfer:
            try:
                self.call('abort', cleanup=True, id=transfer['id'])
            except (TaskError, OSError):
                pass
            finally:
                self.device.file_transfer.release_local(transfer['id'], self.connection)


class TransferCopy:
    def __init__(self, manager, source, destination, actor, client_id, path, target, overwrite):
        self.manager = manager
        self.source, self.destination = source, destination
        self.actor, self.client_id = actor, client_id
        self.path, self.target = _remote_path(path), _remote_path(target)
        self.overwrite = overwrite
        self.snapshots = {device: (device.connection_id, device.stop)
                          for device in (source, destination)}
        self.serials = {device: device.serial or '' for device in (source, destination)}

    def run(self, context):
        failed = threading.Event()
        class Cancellation:
            def is_set(self):
                return context.cancelled.is_set() or failed.is_set()
        cancellation = Cancellation()
        ports = {device: TransferPort(self.manager, device, context, self.actor, self.client_id,
                                      (*snapshot, self.serials[device]), cancellation)
                 for device, snapshot in self.snapshots.items()}
        def call(device, action, **payload):
            return ports[device].call(action, **payload)

        incoming = outgoing = None
        source_seconds = destination_seconds = 0.0
        started = time.monotonic()
        try:
            incoming = call(self.source, 'start', direction='download', path=self.path)
            outgoing = call(self.destination, 'start', direction='upload', path=self.target,
                            size=incoming['size'], overwrite=self.overwrite)
            size, batch_size = incoming['size'], outgoing.get('batch_size', incoming['chunk_size'])
            context.progress(0, size, 'copying')

            def read_batch(offset):
                data, elapsed = bytearray(), 0.0
                while offset + len(data) < size and len(data) < batch_size:
                    position, before = offset + len(data), time.monotonic()
                    chunk = call(self.source, 'chunk', id=incoming['id'], offset=position)
                    elapsed += time.monotonic() - before
                    part = base64.b64decode(chunk['data_b64'], validate=True)
                    if not part or chunk.get('offset') != position + len(part) or position + len(part) > size:
                        raise TaskError('source transfer offset mismatch')
                    data.extend(part)
                return bytes(data), elapsed

            with ThreadPoolExecutor(max_workers=1) as reader:
                try:
                    pending = reader.submit(read_batch, 0) if size else None
                    offset = 0
                    while offset < size:
                        data, elapsed = pending.result()
                        source_seconds += elapsed
                        next_offset = offset + len(data)
                        pending = reader.submit(read_batch, next_offset) if next_offset < size else None
                        before = time.monotonic()
                        sent = ports[self.destination].write(outgoing, offset, data)
                        destination_seconds += time.monotonic() - before
                        if sent.get('offset') != next_offset:
                            raise TaskError('destination transfer offset mismatch')
                        offset = next_offset
                        context.progress(offset, size, 'copying')
                except BaseException:
                    failed.set()
                    for device in self.snapshots:
                        transactions_for(device)._wake()
                    raise
            context.progress(size, size, 'verifying')
            expected_digest = incoming['sha256']
            verified = call(self.source, 'finish', id=incoming['id'])
            incoming = None
            if verified['sha256'] != expected_digest:
                raise TaskError('source file changed during transfer')
            result = call(self.destination, 'finish', id=outgoing['id'])
            outgoing = None
            if result['sha256'] != verified['sha256']:
                raise TaskError('destination SHA-256 mismatch')
            elapsed = time.monotonic() - started
            return dict(ok=True, path=result['path'], size=result['size'], sha256=result['sha256'],
                        source_device=self.source.key, destination_device=self.destination.key,
                        rate_diagnostics={
                            'source_kib_s': round(size / max(source_seconds, .001) / 1024, 2),
                            'destination_kib_s': round(size / max(destination_seconds, .001) / 1024, 2),
                            'effective_kib_s': round(size / max(elapsed, .001) / 1024, 2),
                            'total_s': round(elapsed, 3)})
        finally:
            ports[self.source].cleanup(incoming)
            ports[self.destination].cleanup(outgoing)


class TransferFile:
    """One board and one host artifact; staged content never enters task results."""
    def __init__(self, manager, device, actor, client_id, direction, path, artifact_id=None, overwrite=False, snapshot=None):
        self.manager, self.device = manager, device
        self.actor, self.client_id = actor, client_id
        self.direction, self.path = direction, _remote_path(path)
        self.artifact_id, self.overwrite = artifact_id, overwrite
        self.connection, self.stop, self.serial = snapshot or (device.connection_id, device.stop, device.serial or '')
        self.artifacts = manager.artifacts
        if self.artifacts is None:
            raise TaskError('artifact service unavailable', 503)
        if direction == 'upload':
            row = self.artifacts.get(artifact_id, actor, self.serial)
            if row['state'] != 'ready' or row['direction'] != 'upload':
                raise TaskError('upload artifact is not ready', 409)

    def run(self, context):
        device, artifacts = self.device, self.artifacts
        port = TransferPort(self.manager, device, context, self.actor, self.client_id,
                            (self.connection, self.stop, self.serial))
        call = port.call
        transfer, artifact, successful, corrupt = None, None, False, False

        try:
            if self.direction == 'upload':
                context.check()
                artifact = artifacts.claim(self.artifact_id, context.id, self.actor, self.serial)
                transfer = call('start', direction='upload', path=self.path,
                                size=artifact['size'], overwrite=self.overwrite)
                offset, digest = 0, hashlib.sha256()
                context.progress(0, artifact['size'], 'uploading')
                try:
                    source = artifacts.open(artifact['id'], self.actor, self.serial, context.id)
                except (OSError, TaskError):
                    corrupt = True
                    raise
                with source:
                    while offset < artifact['size']:
                        context.check()
                        length = min(transfer.get('batch_size', transfer['chunk_size']), artifact['size'] - offset)
                        data = source.read(length)
                        if len(data) != length:
                            corrupt = True
                            raise TaskError('staged upload size changed', 409)
                        digest.update(data)
                        written = port.write(transfer, offset, data)
                        if written.get('offset') != offset + len(data):
                            raise TaskError('upload offset mismatch')
                        offset += len(data)
                        context.progress(offset, artifact['size'], 'uploading')
                    if source.read(1) or digest.hexdigest() != artifact['sha256']:
                        corrupt = True
                        raise TaskError('staged upload checksum changed', 409)
                context.progress(offset, artifact['size'], 'verifying')
                result = call('finish', id=transfer['id'])
                transfer = None
                if result['sha256'] != artifact['sha256']:
                    raise TaskError('upload checksum mismatch', 502)
                successful = True
                return dict(ok=True, path=result['path'], size=result['size'], sha256=result['sha256'])

            transfer = call('start', direction='download', path=self.path)
            name = self.path.rsplit('/', 1)[-1].replace('\\', '_') or 'board-file'
            name = name.encode()[:255].decode('utf-8', errors='ignore')
            artifact = artifacts.create('download', name, transfer['size'], self.actor, self.serial, context.id)
            offset = 0
            context.progress(0, transfer['size'], 'downloading')
            while offset < transfer['size']:
                chunk = call('chunk', id=transfer['id'], offset=offset)
                data = base64.b64decode(chunk['data_b64'], validate=True)
                if not data or chunk.get('offset') != offset + len(data) or offset + len(data) > transfer['size']:
                    raise TaskError('download offset mismatch')
                artifacts.append(artifact['id'], offset, data, self.actor, self.serial, context.id)
                offset += len(data)
                context.progress(offset, transfer['size'], 'downloading')
            expected = transfer['sha256']
            context.progress(offset, transfer['size'], 'verifying')
            result = call('finish', id=transfer['id'])
            transfer = None
            if result['sha256'] != expected:
                raise TaskError('source checksum changed', 502)
            sealed = artifacts.seal(artifact['id'], self.actor, self.serial, context.id,
                                    expected_sha256=expected, check=context.check)
            successful = True
            return dict(ok=True, path=self.path, size=sealed['size'], sha256=sealed['sha256'],
                        artifact_id=sealed['id'], name=sealed['name'])
        finally:
            port.cleanup(transfer)
            if artifact:
                if self.direction == 'upload' and not corrupt:
                    artifacts.finish_claim(artifact['id'], context.id, consumed=successful)
                elif not successful:
                    artifacts.fail(artifact['id'], context.id)
