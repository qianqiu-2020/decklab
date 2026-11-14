"""Board control providers, independent of UART and flashing transports.

Providers bind to administrator-configured targets. A mutating request is sent
once; an absent/invalid receipt is an unknown outcome, never a retry signal.
"""
from dataclasses import dataclass
import json
import os
import re
import ssl
import stat
import time
from typing import Protocol
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler

from .profiles import _unique_object
from .tasks import TaskError, TaskCancelled
from .transactions import TransactionError


CONTROL_ACTIONS = ('power-on', 'power-off', 'reset', 'enter-download')
MAX_RESPONSE = 1024 * 1024


class BoundControl(Protocol):
    def run(self, check, cleanup_failed=None): ...


class ControlAction(Protocol):
    provider: str
    timeout: int
    allow_agent: bool
    exclusive_serial: bool

    def bind(self, device) -> BoundControl: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _credentials(path):
    """Read only a bounded private file, without putting secrets in errors."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096
                    or (os.name == 'posix' and info.st_mode & 0o077)):
                raise ValueError()
            value = json.loads(source.read(4097).decode('utf-8-sig'), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {'username', 'password'}
                or any(not isinstance(v, str) or not v or len(v.encode()) > 2048 for v in value.values())):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise TaskError('BMC credentials require a private JSON file containing username and password', 503) from None


@dataclass(frozen=True)
class BmcAction:
    """SpacemiT BMC HTTP provider; credentials and tokens stay server-side."""
    origin: str
    board: str
    module_index: int
    usb_port: str
    credentials_file: str
    action: str
    timeout: int = 30
    allow_agent: bool = False
    ca_file: str = ''
    provider: str = 'bmc'
    exclusive_serial: bool = False

    def bind(self, device):
        # This provider never opens a serial device or depends on its COM/tty.
        return self

    def run(self, check, cleanup_failed=None):
        started = time.monotonic()
        deadline = started + self.timeout
        sent = False
        try:
            check()
            credentials = _credentials(self.credentials_file)
            tls = ssl.create_default_context(cafile=self.ca_file or None)
            opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=tls))

            def request(path, value=None, token=None):
                nonlocal sent
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                headers = {'Accept': 'application/json'}
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                body = None
                if value is not None:
                    headers['Content-Type'] = 'application/json'
                    body = json.dumps(value).encode()
                if path.endswith(('/reset', '/firmware-update')):
                    # Mark before sending: a network exception cannot prove the
                    # server did not apply the operation.
                    sent = True
                with opener.open(Request(self.origin + path, data=body, headers=headers),
                                 timeout=min(5, remaining)) as response:
                    if response.status != 200:
                        raise ValueError()
                    chunks, size = [], 0
                    while True:
                        check()
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise ValueError()
                    raw = b''.join(chunks)
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                check()
                result = json.loads(raw, object_pairs_hook=_unique_object)
                if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
                    raise ValueError()
                return result.get('data')

            login = request('/api/v1/login', credentials)
            token = login.get('access_token') if isinstance(login, dict) else None
            if not isinstance(token, str) or not token or len(token) > 16384 or any(c in token for c in '\r\n'):
                raise ValueError()
            board = quote(self.board, safe='')
            identity = request('/api/v1/devices/name/' + board, token=token)
            if (not isinstance(identity, dict) or identity.get('name') != self.board
                    or type(identity.get('module_index')) is not int
                    or identity['module_index'] != self.module_index
                    or identity.get('adb_port') != 'usb:' + self.usb_port):
                raise TaskError('BMC board mapping changed; no control request sent', 409)
            prefix = '/api/v1/openbmc/devices/' + board
            if self.action == 'enter-download':
                request(prefix + '/firmware-update', {}, token)
            else:
                reset_type = {'reset': 'ForceRestart', 'power-on': 'PowerOn', 'power-off': 'PowerOff'}[self.action]
                request(prefix + '/reset', {'reset_type': reset_type}, token)
            return {'provider': self.provider, 'control_result': 'acknowledged',
                    'duration_ms': round((time.monotonic() - started) * 1000)}
        except Exception as error:
            if sent:
                message = 'BMC control outcome unknown; inspect the original task and board before another action'
                if isinstance(error, TaskCancelled) or isinstance(error, TransactionError) and error.code == 'cancelled':
                    raise TaskCancelled(message) from None
                raise TaskError(message, error.status if isinstance(error, TaskError) else 502) from None
            if isinstance(error, (TaskCancelled, TransactionError)):
                raise
            if isinstance(error, TaskError):
                raise
            # Never include upstream response bodies, URLs, headers or tokens.
            raise TaskError('BMC control preflight failed; no control request sent', 502) from None


def bmc_actions(value):
    allowed = {'provider', 'url', 'board', 'module_index', 'usb_port', 'credentials_file',
               'actions', 'timeout', 'allow_agent', 'ca_file'}
    if not isinstance(value, dict) or set(value) - allowed or value.get('provider') != 'bmc':
        raise ValueError('invalid BMC control configuration')
    origin = value.get('url')
    try:
        parts = urlsplit(origin) if isinstance(origin, str) else None
        valid = (parts and parts.scheme in ('http', 'https') and parts.hostname
                 and parts.port != 0 and not parts.username and not parts.password
                 and parts.path in ('', '/') and not parts.query and not parts.fragment
                 and len(origin) <= 2048 and not any(c.isspace() or ord(c) < 32 for c in origin))
    except ValueError:
        valid = False
    board, module, usb = value.get('board'), value.get('module_index'), value.get('usb_port')
    credentials, ca = value.get('credentials_file'), value.get('ca_file', '')
    actions, timeout, agent = value.get('actions'), value.get('timeout', 30), value.get('allow_agent', False)
    if (not valid or not isinstance(board, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', board)
            or type(module) is not int or not 0 <= module <= 65535
            or not isinstance(usb, str) or not re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*', usb)
            or not isinstance(credentials, str) or not os.path.isabs(credentials) or '\x00' in credentials
            or not isinstance(ca, str) or ca and (not os.path.isabs(ca) or '\x00' in ca)
            or not isinstance(actions, list) or not actions or any(a not in CONTROL_ACTIONS for a in actions)
            or len(set(actions)) != len(actions)
            or type(timeout) is not int or not 1 <= timeout <= 3600 or type(agent) is not bool):
        raise ValueError('BMC control requires a fixed URL, board mapping, private credentials file and explicit actions')
    return {action: BmcAction(origin.rstrip('/'), board, module, usb, credentials, action, timeout, agent, ca)
            for action in actions}


# New integrations implement bind(device) -> run(check, cleanup_failed), keeping
# task ownership, authorization, cancellation and audit in HardwareOperation.
CONTROL_PROVIDERS = {'bmc': bmc_actions}


def control_actions(value):
    provider = value.get('provider') if isinstance(value, dict) else None
    factory = CONTROL_PROVIDERS.get(provider) if isinstance(provider, str) else None
    if factory is None:
        raise ValueError('unsupported board control provider')
    return factory(value)
