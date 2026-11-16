"""Board-scoped graphical sessions; use existing grants, input leases and tasks."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import struct
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import quote

from .profiles import _unique_object
from .results import response
from .tasks import TaskError
from .transactions import TransactionError, transactions_for

MAX_IMAGE = 8 * 1024 ** 2
INPUT_FIELDS = {'click': {'x', 'y', 'button'}, 'scroll': {'x', 'y', 'steps'},
                'key': {'keys'}, 'text': {'text'}}


class DesktopBook:
    def __init__(self, document=None):
        document = document or {}
        if not isinstance(document, dict) or set(document) - {'boards'} or not isinstance(document.get('boards', {}), dict):
            raise ValueError('desktop configuration requires boards')
        self.boards = document.get('boards', {})
        for serial, config in self.boards.items():
            if not isinstance(serial, str) or not serial or len(serial) > 100 or not isinstance(config, dict):
                raise ValueError('invalid desktop board')
            if set(config) - {'provider', 'argv', 'allow_agent', 'display', 'display_kind', 'scale', 'reservation', 'secret_url', 'width', 'height', 'text_input', 'engine', 'certificate_sha256'}:
                raise ValueError('unknown desktop setting')
            if config.get('provider') not in ('helper', 'vnc', 'rdp'):
                raise ValueError('unsupported desktop provider')
            if 'engine' in config and (config['provider'] != 'rdp' or config['engine'] not in ('aardwolf', 'freerdp')):
                raise ValueError('RDP engine must be aardwolf or freerdp')
            if 'certificate_sha256' in config and (config.get('engine') != 'freerdp'
                    or not isinstance(config['certificate_sha256'], str)
                    or not re.fullmatch(r'[0-9a-fA-F]{64}', config['certificate_sha256'])):
                raise ValueError('FreeRDP certificate_sha256 requires a complete DER SHA256 fingerprint')
            if 'text_input' in config and (config['provider'] != 'rdp' or config['text_input'] not in ('unicode', 'clipboard')):
                raise ValueError('RDP text_input must be unicode or clipboard')
            if config.get('display_kind') not in ('physical', 'virtual', 'unknown'):
                raise ValueError('desktop display_kind must be physical, virtual or unknown')
            if not isinstance(config.get('display'), str) or not config['display'] or len(config['display']) > 128:
                raise ValueError('desktop display must identify the configured monitor')
            if type(config.get('allow_agent', False)) is not bool:
                raise ValueError('desktop allow_agent must be boolean')
            scale = config.get('scale', 1)
            if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or not .25 <= scale <= 8:
                raise ValueError('invalid desktop scale')
            for key in ('width', 'height'):
                if key in config and (type(config[key]) is not int or not 64 <= config[key] <= 4096):
                    raise ValueError('invalid desktop dimensions')
            if config['provider'] == 'helper':
                argv = config.get('argv')
                if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or not os.path.isabs(argv[0])
                        or any(not isinstance(arg, str) or '\x00' in arg or len(arg) > 4096 for arg in argv)):
                    raise ValueError('desktop helper requires a fixed absolute argv')
            elif not isinstance(config.get('secret_url'), str) or not config['secret_url'].startswith(config['provider'] + '+'):
                raise ValueError('desktop provider requires its server-side connection URL')
            reservation = config.get('reservation')
            if reservation is not None and (not isinstance(reservation, dict) or set(reservation) != {
                    'origin', 'credential_file', 'device_id', 'task', 'requester', 'lease_id'}
                    or any(not isinstance(v, str) or not v for v in reservation.values())
                    or not reservation['origin'].startswith(('http://', 'https://'))
                    or not os.path.isabs(reservation['credential_file'])):
                raise ValueError('reservation must reference the authoritative console and original owner/task/lease')

    @classmethod
    def from_env(cls):
        path = os.environ.get('SERIALD_DESKTOP')
        if not path:
            return cls()
        with open(path, encoding='utf-8') as source:
            return cls(json.load(source, object_pairs_hook=_unique_object))


def check_reservation(config, for_input=False, expected_activity=None, timeout=8):
    """Read the original reservation. Never acquire/release or trust client claims."""
    expected = config.get('reservation')
    if expected is None:
        return {'source': 'local', 'cross_platform_exclusive': False}
    try:
        credential = json.loads(Path(expected['credential_file']).read_text())
        if (credential.get('origin') != expected['origin']
                or credential.get('username') != expected['requester']):
            raise ValueError()
        auth = base64.b64encode((credential['username'] + ':' + credential['password']).encode()).decode()
        opener = build_opener(ProxyHandler({}))
        request = Request(expected['origin'].rstrip('/') + '/api/ops/v1/devices',
                          headers={'Authorization': 'Basic ' + auth})
        with opener.open(request, timeout=timeout) as incoming:
            raw = incoming.read(1024 ** 2 + 1)
        if len(raw) > 1024 ** 2:
            raise ValueError()
        rows = [row for row in json.loads(raw)['data'] if row['id'] == expected['device_id']]
        lease = rows[0]['lease'] if len(rows) == 1 else None
        if (not lease or lease['id'] != expected['lease_id'] or lease['state'] != 'reserved'
                or lease['task'] != expected['task'] or lease['requester'] != expected['requester']
                or 'desktop' not in lease['actions']):
            raise ValueError()
        if for_input and lease.get('activity') != 'idle':
            raise ValueError()
        if expected_activity is not None and lease.get('activity') not in (
                (expected_activity,) if isinstance(expected_activity, str) else expected_activity):
            raise ValueError()
        return {'source': 'console', 'device_id': expected['device_id'], 'task': lease['task'],
                'lease_id': lease['id'], 'cross_platform_exclusive': False}
    except Exception:
        raise TaskError('无法核实原控制台桌面占用，未执行图形操作', 423) from None


def reservation_activity(config, activity, timeout=8):
    """Update the original lease, never a second device pool or a new request."""
    expected = config.get('reservation')
    if expected is None:
        return
    try:
        credential = json.loads(Path(expected['credential_file']).read_text())
        if credential.get('origin') != expected['origin'] or credential.get('username') != expected['requester']:
            raise ValueError()
        auth = base64.b64encode((credential['username'] + ':' + credential['password']).encode()).decode()
        url = expected['origin'].rstrip('/') + '/api/ops/v1/devices/' + quote(expected['device_id'], safe='')
        url += '/reservations/' + quote(expected['lease_id'], safe='')
        request = Request(url, data=json.dumps({'action': activity, 'note': 'DeckLab desktop input: ' + activity}).encode(),
            headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as incoming:
            raw = incoming.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        lease = json.loads(raw)['data']
        if (lease['id'] != expected['lease_id'] or lease['task'] != expected['task']
                or lease['requester'] != expected['requester'] or lease['activity'] != activity):
            raise ValueError()
    except Exception:
        # Do not retry a lost mutation. Original running/unknown activity blocks
        # further input until the manager reconciles it in the original console.
        raise TaskError('原控制台操作状态未确认，请核对原占用；未重放输入', 423) from None


def checked_image(result):
    try:
        raw = base64.b64decode(result['png_base64'], validate=True)
        if not 33 <= len(raw) <= MAX_IMAGE or raw[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
            raise ValueError()
        width, height = struct.unpack('>II', raw[16:24])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 32 * 1024 ** 2:
            raise ValueError()
        if result.get('width', width) != width or result.get('height', height) != height:
            raise ValueError()
        return raw, width, height
    except (KeyError, ValueError, TypeError):
        raise TaskError('桌面截图不完整或尺寸不符', 502) from None


class DesktopSessions:
    def __init__(self, manager, book=None, provider_factory=None, settings=None):
        self.manager = manager
        self.book = book if book is not None else DesktopBook.from_env()
        self.lock, self.sessions = threading.RLock(), {}
        if provider_factory is None:
            from .desktop_providers import connect
            provider_factory = connect
        self.provider_factory = provider_factory
        from .desktop_setup import DesktopSetup
        if settings is None:
            from .config import SETTINGS
            settings = SETTINGS
        self.setup = DesktopSetup(self, settings)

    def config(self, device, actor):
        if not device or not device.serial:
            raise TaskError('图形操作需要已识别的板卡', 409)
        if self.manager.store.has_conflict(device.serial):
            raise TaskError('请先处理板卡身份冲突', 409)
        config = self.book.boards.get(device.serial)
        if not config or actor != 'web' and not config.get('allow_agent', False):
            raise TaskError('此设备未配置可用的图形接入', 404)
        return config

    def get(self, device, sid, actor, client_id=''):
        self.config(device, actor)
        with self.lock:
            session = self.sessions.get(sid)
        if (session is None or session['serial'] != device.serial or session['closed']
                or actor != 'web' and session['actor'] != actor):
            raise TaskError('desktop session not found', 404)
        return session

    def public(self, session):
        return {key: session[key] for key in ('id', 'serial', 'actor', 'client_id', 'display',
            'display_kind', 'scale', 'created', 'needs_observation')}

    def list(self, device, actor):
        try:
            config = self.config(device, actor)
        except TaskError as error:
            if error.status == 404:
                result = {'supported': False, 'reason': str(error), 'sessions': []}
                if actor == 'web':
                    result['setup'] = self.setup.public(device)
                return result
            raise
        with self.lock:
            sessions = [self.public(s) for s in self.sessions.values() if not s['closed']
                and s['serial'] == device.serial and (actor == 'web' or s['actor'] == actor)]
        result = {'supported': True, 'provider': config['provider'], 'display': config['display'],
                  'display_kind': config['display_kind'], 'sessions': sessions,
                  'reservation': 'console-check-required' if config.get('reservation') else 'local-only'}
        if actor == 'web':
            result['setup'] = self.setup.public(device)
        return result

    def open(self, device, actor, client_id):
        config = self.config(device, actor)
        check_reservation(config)
        with self.lock:
            if config != self.config(device, actor):
                raise TaskError('桌面连接设置已改变，请重新连接', 409)
            if len(self.sessions) >= 32:
                raise TaskError('桌面会话数已达上限，请关闭不用的连接', 429)
            for session in self.sessions.values():
                if session['serial'] == device.serial and not session['closed']:
                    if session['actor'] == actor and session['client_id'] == client_id:
                        return {'session': self.public(session)}
                    raise TaskError('该设备已有桌面会话，请协调原操作者关闭', 423)
            # Reserve before connecting; another request cannot create a competing session.
            session = dict(id='desktop-' + secrets.token_hex(12), serial=device.serial, actor=actor,
                client_id=client_id, display=config['display'], display_kind=config['display_kind'],
                scale=config.get('scale', 1), config=config, created=time.time(), needs_observation=True,
                closed=False, lock=threading.RLock(), provider=None, frames={}, mode=None, ownership=None)
            self.sessions[session['id']] = session
        try:
            with session['lock']:
                session['provider'] = self.provider_factory(config)
            return {'session': self.public(session)}
        except Exception as error:
            with self.lock:
                self.sessions.pop(session['id'], None)
            from .desktop_errors import connection_error
            failure = connection_error(config, 'connect', error)
            reported = TaskError(str(failure), 502)
            reported.code, reported.details = failure.code, failure.details
            raise reported from None

    def frame(self, device, sid, actor, client_id=''):
        session = self.get(device, sid, actor, client_id)
        with session['lock']:
            check_reservation(session['config'])
            result = session['provider'].screenshot()
            raw, width, height = checked_image(result)
            mode = self.mode(session, device, result, width, height)
            if mode != session['mode']:
                session['frames'].clear()
                session['mode'] = mode
            # Each caller may act only on its latest successful observation.
            for previous, observed in list(session['frames'].items()):
                if observed[1:3] == (actor, client_id):
                    del session['frames'][previous]
            frame_id = secrets.token_hex(12)
            session['frames'][frame_id] = (mode, actor, client_id, time.monotonic())
            while len(session['frames']) > 32:
                del session['frames'][next(iter(session['frames']))]
            session['needs_observation'] = False
            return {'session': self.public(session), 'frame': {'id': frame_id, 'display': session['display'],
                'display_kind': session['display_kind'], 'width': width, 'height': height,
                'scale': session['scale'], 'captured_at': time.time(),
                'source_updated_at': result.get('source_updated_at'), 'mime_type': 'image/png'},
                'png_base64': base64.b64encode(raw).decode()}

    @staticmethod
    def mode(session, device, result, width, height):
        # Ownership belongs to the existing Device/connection. A serial
        # attachment change invalidates observations even if the desktop did
        # not resize. Fresh input must claim the replacement Device as well.
        return (width, height, result.get('generation'), session['display'], session['scale'],
                id(device), device.connection_id)

    @staticmethod
    def release_input(session):
        if session['ownership']:
            device, owner = session['ownership']
            transactions = transactions_for(device)
            with transactions.actor_scope(owner['actor'], owner.get('client_id', '')):
                try:
                    transactions.release(owner['id'], owner['actor'])
                except TransactionError as error:
                    if error.code != 'not-found':
                        raise
            session['ownership'] = None

    def input(self, device, payload, actor, client_id):
        session = self.get(device, payload.get('sid'), actor, client_id)
        action = payload.get('action')
        fields = INPUT_FIELDS.get(action)
        if fields is None or set(payload) - (fields | {'action', 'sid', 'frame_id', 'request_id', 'key', 'serial', 'actor', 'client_id'}):
            raise TaskError('invalid desktop input')
        if action in ('click', 'scroll'):
            if any(type(payload.get(k)) is not int for k in ('x', 'y')):
                raise TaskError('desktop coordinates must be integers')
            if action == 'click' and payload.get('button') not in ('left', 'middle', 'right'):
                raise TaskError('invalid desktop button')
            if action == 'scroll' and (type(payload.get('steps')) is not int or not -20 <= payload['steps'] <= 20):
                raise TaskError('invalid desktop scroll steps')
        if action == 'key' and (not isinstance(payload.get('keys'), list) or not 1 <= len(payload['keys']) <= 4
                or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_+.-]{1,40}', k) for k in payload['keys'])):
            raise TaskError('invalid desktop keys')
        if action == 'text' and (not isinstance(payload.get('text'), str) or len(payload['text']) > 1024):
            raise TaskError('desktop text exceeds 1024 characters')
        # Do not persist typed text (which may be a password) or image bytes in task history.
        identity = dict(serial=device.serial, sid=session['id'], **{k: payload.get(k) for k in fields | {'action', 'frame_id'}})
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        from .flash_store import validate_request
        validate_request(payload.get('request_id'), digest)
        def run(context):
            with session['lock']:
                context.check()
                if session['closed']:
                    raise TaskError('desktop session closed', 409)
                check_reservation(session['config'], for_input=True)
                observed = session['frames'].get(payload.get('frame_id'))
                if (session['needs_observation'] or not observed or observed[1:3] != (actor, client_id)
                        or time.monotonic() - observed[3] > 600):
                    raise TaskError('请先重新截图观察当前桌面', 409)
                current = session['provider'].screenshot()
                _, width, height = checked_image(current)
                mode = self.mode(session, device, current, width, height)
                if mode != observed[0]:
                    session['frames'].clear()
                    session['needs_observation'] = True
                    raise TaskError('显示模式已变化，请重新截图', 409)
                if action in ('click', 'scroll') and not (0 <= payload['x'] < width and 0 <= payload['y'] < height):
                    raise TaskError('输入坐标超出已观察画面', 409)
                transactions = transactions_for(device)
                with transactions.actor_scope(actor, client_id):
                    transactions.check_owner(actor)
                    if session['ownership'] and session['ownership'][0] is not device:
                        self.release_input(session)
                    session['ownership'] = (device, transactions.claim(actor))
                    with transactions.begin('desktop-input', 30, actor, require_open=False) as lease:
                        context.check()
                        lease.check()
                        session['needs_observation'] = True
                        try:
                            reservation_activity(session['config'], 'running')
                            context.issued()
                            result = session['provider'].input(dict(action=action, **{k: payload[k] for k in fields}))
                            context.check()
                            lease.check()
                            if result.get('ok') is not True:
                                raise ValueError()
                            reservation_activity(session['config'], 'idle')
                        except Exception:
                            # Another observer must not revive pre-input frames
                            # by clearing the shared needs_observation flag.
                            session['frames'].clear()
                            try:
                                reservation_activity(session['config'], 'unknown')
                            except TaskError:
                                pass
                            raise TaskError('图形输入结果未知，请先截图核对，不要重放输入', 502) from None
                        session['needs_observation'] = False
                        context.mark_outcome('acknowledged')
                        return {'session_id': session['id'], 'action': action, 'input_result': 'sent',
                                'application_result': 'observe-screen'}
        task = self.manager.tasks.start('desktop-input', actor, client_id,
            [{'serial': device.serial, 'connection_id': session['id']}], run,
            request_id=payload.get('request_id'), input_digest=digest, tracks_outcome=True)
        return {'task': task}

    def close_session(self, session):
        with session['lock']:
            session['closed'] = True
            live = session.get('live_channel')
        if live is not None:
            # The live channel owns any held keys. Let it perform bounded UP
            # cleanup before the existing provider transport is closed.
            live.request_stop()
        with session['lock']:
            try:
                if session['provider'] is not None:
                    session['provider'].close()
            finally:
                self.release_input(session)
                with self.lock:
                    self.sessions.pop(session['id'], None)

    def close_actor(self, actor=None):
        with self.lock:
            rows = [s for s in self.sessions.values() if actor is None or s['actor'] == actor]
        for session in rows:
            self.close_session(session)
        return len(rows)

    def execute(self, route, device, actor, payload):
        try:
            client_id = payload.get('client_id', '') if actor == 'web' else ''
            if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,80}', client_id):
                raise TaskError('invalid desktop client')
            if route == 'sessions':
                result = self.list(device, actor)
            elif route == 'discover':
                endpoint = None
                if any(key in payload for key in ('host', 'port', 'provider')):
                    if actor != 'web':
                        raise TaskError('Agent 只能检测该设备已识别的 IP 和固定端口', 403)
                    from .desktop_setup import checked_host
                    port, provider = payload.get('port'), payload.get('provider')
                    if type(port) is not int or not 1 <= port <= 65535 or provider not in ('vnc', 'rdp'):
                        raise TaskError('请选择 VNC／RDP 并填写有效端口')
                    endpoint = (checked_host(payload.get('host')), port, provider)
                result = self.setup.discover(device, endpoint)
            elif route == 'configure':
                if actor != 'web':
                    raise TaskError('只有网页操作者可以配置桌面连接', 403)
                result = self.setup.configure(device, payload)
            elif route == 'open':
                result = self.open(device, actor, client_id)
            elif route == 'frame':
                result = self.frame(device, payload.get('sid'), actor, client_id)
            elif route == 'input':
                return response(202, self.input(device, payload, actor, client_id))
            elif route == 'close':
                session = self.get(device, payload.get('sid'), actor, client_id)
                if actor == 'web' and (session['actor'] != actor or session['client_id'] != client_id) \
                        and payload.get('takeover') is not True:
                    raise TaskError('其他页面或 Agent 的桌面连接需要人工明确断开', 403)
                self.close_session(session)
                result = {'ok': True}
            else:
                raise TaskError('unknown desktop route', 404)
            return response(200, result)
        except (TaskError, TransactionError) as error:
            result = {'error': str(error), 'code': getattr(error, 'code', 'desktop-error')}
            if isinstance(getattr(error, 'details', None), dict):
                result['details'] = error.details
            return response(error.status, result)
        except Exception:
            return response(502, {'error': '远程桌面不可用，请检查服务端连接；输入结果不明时先截图核对'})
