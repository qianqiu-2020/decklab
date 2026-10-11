"""Transport-independent application results and stable error codes."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ActionResult:
    status: int
    payload: dict


def response(status, payload):
    if status >= 400 or payload.get('ok') is False:
        payload = dict(payload)
        payload.setdefault('code', {
            400: 'invalid-argument', 401: 'unauthorized', 403: 'forbidden',
            404: 'not-found', 408: 'timeout', 409: 'conflict', 413: 'too-large',
            500: 'internal-error', 502: 'remote-failure',
            503: 'unavailable', 504: 'timeout',
        }.get(status, 'operation-failed'))
    return ActionResult(status, payload)
