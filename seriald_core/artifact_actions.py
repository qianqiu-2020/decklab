"""Scoped staging operations; HTTP never receives or selects host filesystem paths."""
import base64
import binascii

from .results import response
from .tasks import TaskError


def body_limit(route):
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
                    self.artifacts.remove(artifact_id, actor, serial)
                    artifact = self.artifacts.get(artifact_id, actor, serial)
                else:
                    raise TaskError('unknown artifact action', 404)
            return response(200, {'ok': True, 'artifact': artifact})
        except (TaskError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})

    def download(self, artifact_id, actor='web', serial=None):
        row = self.artifacts.get(artifact_id, actor, serial)
        if row['direction'] != 'download' or row['state'] != 'available':
            raise TaskError('download artifact is unavailable', 409)
        return row, self.artifacts.open(artifact_id, actor, serial)
