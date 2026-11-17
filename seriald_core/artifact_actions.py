"""Scoped staging operations; HTTP never receives or selects host filesystem paths."""
import base64
import binascii

from .results import response
from .tasks import TaskError


def body_limit(route):
    if route in ('/api/settings', '/api/backend-settings', '/api/image-workspace'):
        return 2 * 1024 ** 2
    if route == '/api/device-preferences':
        return 1024 ** 2
    return 2 * 1024 ** 2 if route == '/api/artifact-append' else 65536


class ArtifactActions:
    def __init__(self, manager):
        self.manager = manager

    @property
    def artifacts(self):
        artifacts = getattr(self.manager, 'artifacts', None)
        if artifacts is None:
            raise TaskError('artifact service unavailable', 503)
        return artifacts

    def metadata(self, artifact_id, actor='web', serial=None):
        try:
            return response(200, {'artifact': self.artifacts.get(artifact_id, actor, serial)})
        except (TaskError, OSError) as error:
            return response(getattr(error, 'status', 503), {'error': str(error)})

    def execute(self, action, payload, actor='web', device=None):
        try:
            if not isinstance(payload, dict):
                raise TaskError('JSON object required')
            if not isinstance(actor, str) or not actor or len(actor) > 80:
                raise TaskError('invalid artifact actor')
            artifact_id, serial = payload.get('id'), payload.get('serial')
            if action == 'create':
                if device is None:
                    raise TaskError('no such device', 404)
                artifact = self.artifacts.create('upload', payload.get('name'), payload.get('size'),
                                                 actor, device.serial or '')
            else:
                # Download publication/cleanup belongs to the runner, never the client.
                artifact = self.artifacts.get(artifact_id, actor, serial)
                if artifact['direction'] != 'upload' and action != 'delete':
                    raise TaskError('artifact is managed by a download task', 409)
                if action == 'append':
                    try:
                        data = base64.b64decode(payload.get('data_b64'), validate=True)
                    except (ValueError, TypeError, binascii.Error):
                        raise TaskError('invalid artifact base64') from None
                    artifact = self.artifacts.append(artifact_id, payload.get('offset'), data, actor, serial)
                elif action == 'seal':
                    artifact = self.artifacts.seal(artifact_id, actor, serial)
                elif action == 'delete':
                    sha256 = artifact.get('sha256')
                    self.artifacts.remove(artifact_id, actor, serial)
                    if sha256:
                        with self.manager.store.lock:
                            referenced = self.manager.store.db.execute(
                                "SELECT 1 FROM transfer_artifacts WHERE sha256=? AND state IN ('ready','claimed') LIMIT 1", (sha256,)).fetchone()
                        if not referenced:
                            from .flash_tasks import FlashActions
                            FlashActions(self.manager).expire(image_sha256=sha256)
                    artifact = self.artifacts.get(artifact_id, actor, serial)
                else:
                    raise TaskError('unknown artifact action', 404)
            result = {'ok': True, 'artifact': artifact}
            if action == 'seal' and payload.get('prepare_image') is True:
                from .image_preparation import for_manager
                task = for_manager(self.manager).after_upload(artifact, actor, payload.get('client_id', ''))
                if task:
                    result['preparation_task'] = task
            return response(200, result)
        except (TaskError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def download(self, artifact_id, actor='web', serial=None):
        row = self.artifacts.get(artifact_id, actor, serial)
        if row['direction'] != 'download' or row['state'] != 'available':
            raise TaskError('download artifact is unavailable', 409)
        return row, self.artifacts.open(artifact_id, actor, serial)
