"""Durable host-only image preparation, reusing artifact authorization and tasks."""
from contextlib import nullcontext
import hashlib
import json
import secrets
import threading
import time

from .flash_runner import ImagePreparationQuotaError, public_plan
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
        self._lock = threading.RLock()

    def latest(self, artifact_id, actor='web'):
        with self.manager.store.lock:
            row = self.manager.store.db.execute(
                "SELECT t.id FROM tasks t JOIN task_targets s ON s.task_id=t.id "
                "WHERE t.kind='image-prepare' AND t.actor=? AND s.connection_id=? "
                "ORDER BY t.created DESC LIMIT 1", (actor, 'image-prepare:' + artifact_id)).fetchone()
        if not row:
            return None
        task = self.manager.tasks.store.get(row[0])
        executor = getattr(self.manager, 'flash_executor', None)
        artifact = (task.get('result') or {}).get('artifact')
        if (task['state'] == 'completed' and artifact and artifact.get('sha256')
                and hasattr(executor, 'image_workspace') and not executor.image_workspace(artifact).is_dir()):
            return None
        return task

    def start(self, artifact_id, actor='web', serial=None, client_id='', request_id=None):
        # Serialize default request selection with enqueue; explicit request IDs
        # continue using the task manager's exact original-request semantics.
        with self._lock:
            return self._start(artifact_id, actor, serial, client_id, request_id)

    def _start(self, artifact_id, actor='web', serial=None, client_id='', request_id=None):
        from .flash_tasks import _services, FlashActions
        from .flash_discovery import executor_command
        _, executor = _services(self.manager)
        if not getattr(executor, 'supports_image_preparation', False):
            return None
        scope_serial = None if actor == 'web' else serial
        row = self.manager.artifacts.get(artifact_id, actor, scope_serial)
        if row['direction'] != 'upload' or row['state'] not in ('ready', 'claimed'):
            raise TaskError('image must be sealed before preparation', 409)
        identity = hashlib.sha256(json.dumps({'kind': 'image-prepare', 'artifact_id': artifact_id,
            'sha256': row['sha256'], 'serial': row['serial']}, sort_keys=True).encode()).hexdigest()
        if request_id is not None:
            previous = self.manager.tasks.store.find_request(actor, request_id, identity)
            if previous is not None:
                return self.manager.tasks.store.get(previous)
        argv = executor_command()
        if not argv:
            raise TaskError('image preparation executor is unavailable', 503)
        if row.get('task_id'):
            FlashActions(self.manager)._owned_task(row['task_id'], actor, scope_serial)
        source = dict(id=row['id'], name=row['name'], size=row['size'], sha256=row['sha256'],
                      path=str(self.manager.artifacts._path(artifact_id, '.bin')))
        if request_id is None:
            previous = self.latest(artifact_id, actor)
            if previous and (previous['state'] in ('queued', 'running')
                    or previous.get('flash', {}).get('recovery_required')):
                return previous
            valid = getattr(executor, 'prepared_artifact_unchanged', lambda source: True)
            summary = (previous or {}).get('result') or {}
            catalogue = summary.get('preparation', {})
            if (previous and previous['state'] == 'completed' and valid(source)
                    and (not catalogue.get('catalogue') or catalogue.get('layout_version') == 2)):
                return previous
            request_id = 'image-prepare-' + secrets.token_hex(12)
        from .board_maintenance import maintenance_for
        maintenance = maintenance_for(self.manager)
        if maintenance:
            maintenance.check_preparation()
        FlashActions(self.manager).expire()
        def prepare(context):
            context.store.update(context.id, result={'image': {'name': source['name']}})
            # This pin protects the source upload, not a USB endpoint or board.
            guard = getattr(executor, 'image_guard', lambda source, check: nullcontext())
            with guard(source, context.check):
                if maintenance:
                    maintenance.begin_image(context.id, row['sha256'],
                        str(executor.image_workspace(source)) if hasattr(executor, 'image_workspace') else '', artifact_id)
                reader, claimed, uncertain = None, False, False
                timing = None
                if maintenance:
                    from .flash_store import FlashStore
                    from .flash_progress import PreviewProgress
                    from .image_workspaces import public_directory_info
                    directory_info = public_directory_info(source['path'])
                    timing_source = dict(source, size=directory_info.get('directory_bytes') or source['size'])
                    timing = PreviewProgress(FlashStore(self.manager.store), context.id, timing_source, 'image-preparation:local',
                        source['name'].lower().endswith(('.gz', '.tgz', '.zip')),
                        phases=('checking-image-sha256', 'expanding-image', 'checking-payloads', 'reading-payload-metadata'))
                success = False
                try:
                    current = self.manager.artifacts.get(artifact_id, actor, scope_serial)
                    if current['state'] == 'ready':
                        self.manager.artifacts.claim(artifact_id, context.id, actor, scope_serial)
                        claimed = True
                    reader = self.manager.artifacts.open(artifact_id, actor, scope_serial,
                        context.id if claimed else current.get('task_id'))
                    last_progress = ['', 0.0]
                    def event(value):
                        context.check()
                        now = time.monotonic()
                        phase = value.get('phase', 'preparing-image')
                        if (phase == last_progress[0] and now - last_progress[1] < .2
                                and value.get('done', 0) != value.get('total', -1)):
                            return
                        if timing:
                            timing.event(value)
                        done, total = value.get('done', 0), value.get('total', 0)
                        if phase == last_progress[0]:
                            context.progress(done, total, phase)
                        else:
                            context.stage(phase, done=done, total=total)
                        last_progress[:] = [phase, now]
                    try:
                        result = executor.prepare_image(argv, source, event, context.check)
                    except ImagePreparationQuotaError as error:
                        context.check()
                        if not error.reclaim_bytes:
                            raise
                        context.stage('reclaiming-image-cache')
                        last_progress[0] = ''
                        if not FlashActions(self.manager).expire(reclaim_bytes=error.reclaim_bytes):
                            raise
                        result = executor.prepare_image(argv, source, event, context.check)
                    success = True
                    context.stage('image-preparation-ready')
                    return {'artifact': {k: row[k] for k in ('id', 'name', 'size', 'sha256', 'serial', 'state')},
                            'preparation': public_plan(result)}
                except BaseException as error:
                    if maintenance and getattr(error, 'cleanup_confirmed', True) is False:
                        uncertain = True
                        maintenance.retain_image_failure(context.id, {'workspace': getattr(error, 'workspace', '')})
                        if not hasattr(self.manager, '_flash_recovery_pins'):
                            self.manager._flash_recovery_pins = {}
                        self.manager._flash_recovery_pins[context.id] = reader
                        reader = None
                    raise
                finally:
                    if timing:
                        timing.finish(success)
                    if reader is not None:
                        reader.close()
                    if not uncertain:
                        if claimed:
                            self.manager.artifacts.finish_claim(artifact_id, context.id)
                        if maintenance:
                            from .flash_store import FlashStore
                            FlashStore(self.manager.store).update_details(context.id, {'cleanup_confirmed': True})
                            maintenance.release(context.id, confirmed=True)
        return self.manager.tasks.start('image-prepare', actor, client_id,
            [{'serial': row['serial'] or '', 'connection_id': 'image-prepare:' + artifact_id}], prepare,
            request_id=request_id, input_digest=identity)

    def request(self, payload, actor='web'):
        try:
            if not isinstance(payload, dict) or set(payload) - {'id', 'actor', 'client_id', 'serial', 'request_id', 'action', 'task_id', 'attempt_id', 'reason', 'confirm_stopped'}:
                raise TaskError('invalid image preparation request')
            if payload.get('action') == 'recover':
                from .board_maintenance import maintenance_for
                task = self.manager.tasks.store.get(payload.get('task_id'))
                if task['kind'] != 'image-prepare' or actor != 'web':
                    raise TaskError('only the human owner may recover image preparation', 403)
                if payload.get('confirm_stopped') is not True or not payload.get('reason', '').strip():
                    raise TaskError('confirm the original process has stopped and give a reason', 409)
                if task['id'] in self.manager.tasks._active:
                    raise TaskError('image preparation worker is still active', 409)
                from .flash_store import FlashStore
                details = FlashStore(self.manager.store).details(task['id'])
                if details.get('attempt_id') != payload.get('attempt_id'):
                    raise TaskError('image preparation recovery attempt changed', 409)
                pins = getattr(self.manager, '_flash_recovery_pins', {}).pop(task['id'], None)
                if pins:
                    pins.close()
                task = maintenance_for(self.manager).confirm_recovery(task['id'], actor,
                    payload.get('attempt_id'), payload.get('reason'), True, self.manager.artifacts)
                return response(200, {'ok': True, 'task': task})
            if payload.get('action') not in (None, 'prepare'):
                raise TaskError('invalid image preparation action')
            task = self.start(payload.get('id'), actor, payload.get('serial'), payload.get('client_id', ''), payload.get('request_id'))
            if task is None:
                raise TaskError('image preparation is not supported by this executor', 503)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def after_upload(self, row, actor='web', client_id=''):
        """Seal/import succeeds independently; a preparation error remains a task."""
        if not row['name'].lower().endswith(('.tar.gz', '.tgz', '.tar', '.zip', '.itb', '.bin', '.ext2', '.ext3', '.ext4', '.vfat', '.f2fs', '.squashfs', '.ubifs', '.img', '.dtb', '.gz', '.directory')):
            return None
        try:
            return self.start(row['id'], actor, None if actor == 'web' else row['serial'], client_id)
        except (TaskError, ValueError, OSError):
            # Keep the uploaded artifact; the explicit preparation endpoint lets
            # the user see/recover an unavailable executor or queue limit.
            return None
