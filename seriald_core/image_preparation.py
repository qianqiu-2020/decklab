"""Durable host-only image preparation, reusing artifact authorization and tasks."""
import hashlib
import json
import threading

from .flash_runner import public_plan
from .results import response
from .tasks import TaskError


_LOCK = threading.Lock()


def for_manager(manager):
    with _LOCK:
        if not hasattr(manager, 'image_preparations'):
            manager.image_preparations = ImagePreparations(manager)
    return manager.image_preparations


class ImagePreparations:
    def __init__(self, manager):
        self.manager = manager

    def start(self, artifact_id, actor='web', serial=None, client_id='', request_id=None):
        from .flash_tasks import _services, FlashActions
        from .flash_discovery import executor_command
        _, executor = _services(self.manager)
        if not getattr(executor, 'supports_image_preparation', False):
            return None
        scope_serial = None if actor == 'web' else serial
        row = self.manager.artifacts.get(artifact_id, actor, scope_serial)
        if row['direction'] != 'upload' or row['state'] not in ('ready', 'claimed'):
            raise TaskError('image must be sealed before preparation', 409)
        argv = executor_command()
        if not argv:
            raise TaskError('image preparation executor is unavailable', 503)
        if row.get('task_id'):
            FlashActions(self.manager)._owned_task(row['task_id'], actor, scope_serial)
        identity = hashlib.sha256(json.dumps({'kind': 'image-prepare', 'artifact_id': artifact_id,
            'sha256': row['sha256'], 'serial': row['serial']}, sort_keys=True).encode()).hexdigest()
        def prepare(context):
            # This pin protects the source upload, not a USB endpoint or board.
            with self.manager.artifacts.open(artifact_id, actor, scope_serial, row.get('task_id')):
                source = dict(id=row['id'], name=row['name'], size=row['size'], sha256=row['sha256'],
                              path=str(self.manager.artifacts._path(artifact_id, '.bin')))
                def event(value):
                    context.check()
                    context.progress(value.get('done', 0), value.get('total', 0), value.get('phase', 'preparing-image'))
                result = executor.prepare_image(argv, source, event, context.check)
                return {'artifact': {k: row[k] for k in ('id', 'name', 'size', 'sha256', 'serial', 'state')},
                        'preparation': public_plan(result)}
        return self.manager.tasks.start('image-prepare', actor, client_id,
            [{'serial': row['serial'] or '', 'connection_id': 'image-prepare:' + artifact_id}], prepare,
            request_id=request_id or 'image-prepare-' + artifact_id + '-v1', input_digest=identity)

    def request(self, payload, actor='web'):
        try:
            if not isinstance(payload, dict) or set(payload) - {'id', 'actor', 'client_id', 'serial', 'request_id'}:
                raise TaskError('invalid image preparation request')
            task = self.start(payload.get('id'), actor, payload.get('serial'), payload.get('client_id', ''), payload.get('request_id'))
            if task is None:
                raise TaskError('image preparation is not supported by this executor', 503)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def after_upload(self, row, actor='web', client_id=''):
        """Seal/import succeeds independently; a preparation error remains a task."""
        if not row['name'].lower().endswith(('.tar.gz', '.tgz', '.tar', '.zip', '.itb', '.bin', '.ext4', '.vfat', '.img', '.dtb', '.gz')):
            return None
        try:
            return self.start(row['id'], actor, None if actor == 'web' else row['serial'], client_id)
        except (TaskError, ValueError, OSError):
            # Keep the uploaded artifact; the explicit preparation endpoint lets
            # the user see/recover an unavailable executor or queue limit.
            return None
