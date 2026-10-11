"""Collector-owned serial copies: bounded buffering, overlap and task cancellation."""
import base64
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .actions import DeviceActions
from .file_transfer import _remote_path
from .tasks import TaskError
from .transactions import transactions_for


class TransferCopy:
    def __init__(self, manager, source, destination, actor, client_id, path, target, overwrite):
        self.actions = DeviceActions(manager)
        self.source, self.destination = source, destination
        self.actor, self.client_id = actor, client_id
        self.path, self.target = _remote_path(path), _remote_path(target)
        self.overwrite = overwrite
        self.snapshots = {device: (device.connection_id, device.stop)
                          for device in (source, destination)}

    def run(self, context):
        failed = threading.Event()
        class Cancellation:
            def is_set(self):
                return context.cancelled.is_set() or failed.is_set()
        cancellation = Cancellation()
        for device in self.snapshots:
            context.on_cancel(transactions_for(device)._wake)

        def call(device, action, cleanup=False, **payload):
            connection, stop = self.snapshots[device]
            # Cleanup may remove this operation's temporary file after cancellation,
            # but can never execute on a substituted connection.
            if device.connection_id != connection or device.stop is not stop or stop.is_set():
                raise TaskError('device connection changed', 409)
            if not cleanup:
                context.check()
                if failed.is_set():
                    raise TaskError('copy stopped after peer failure')
            transactions = transactions_for(device)
            with transactions.task_scope(threading.Event() if cleanup else cancellation, connection, stop):
                result = self.actions.execute('file-transfer/' + action,
                    dict(payload, connection_id=connection, client_id=self.client_id), device, self.actor)
            if result.status >= 400 or not result.payload.get('ok'):
                if not cleanup:
                    context.check()
                raise TaskError(result.payload.get('error', 'transfer failed'), result.status)
            return result.payload

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

            def write_batch(offset, data):
                if outgoing.get('batch_size'):
                    return call(self.destination, 'batch', id=outgoing['id'], offset=offset,
                                data_b64=base64.b64encode(data).decode('ascii'))
                sent = 0
                while sent < len(data):
                    length = min(outgoing['chunk_size'], len(data) - sent)
                    while True:
                        try:
                            result = call(self.destination, 'chunk', id=outgoing['id'], offset=offset + sent,
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
                        sent = write_batch(offset, data)
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
            for device, transfer in ((self.source, incoming), (self.destination, outgoing)):
                if transfer:
                    try:
                        call(device, 'abort', cleanup=True, id=transfer['id'])
                    except (TaskError, OSError):
                        pass
                    finally:
                        device.file_transfer.release_local(transfer['id'], self.snapshots[device][0])
