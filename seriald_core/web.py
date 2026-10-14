"""Collector API served only on the local Unix socket."""
import base64
import http.server
import json
import os
import shutil
import time
from urllib.parse import urlparse, parse_qs, quote

from .config import BY_PATH, LOG_ROOT, RESCUE_BIN
from .transport import try_rescue
from .log_archive import read_epoch, search_epochs
from .boot_compare import compare_boots
from .actions import DeviceActions, capabilities
from .queries import ArchiveQueries, ArchiveDownload, SessionQueries, open_archive
from . import __version__
from .results import response as api_response
from .transactions import transactions_for, TransactionError
from .maintenance import disk_status, diagnostic_summary
from .task_actions import TaskActions
from .artifact_actions import ArtifactActions, body_limit
from .tasks import TaskError
from .request_json import load_object
from .terminal_backend import screen_for, terminal_payload

MGR = None  # Direct-handler fixtures may inject a manager; production uses server.manager.
STARTED = time.time()

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'seriald-collector/' + __version__

    @property
    def manager(self):
        server = getattr(self, 'server', None)
        if server is not None and hasattr(server, 'manager'):
            return server.manager
        return MGR

    def _session_events(self, q):
        """Stream one session's byte offsets; reconnect resumes via Last-Event-ID."""
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})
        sid = (q.get('sid') or ['serial'])[0]
        stream = d if sid == 'serial' else d.get_session(sid)
        if not stream:
            return self._json(404, {'error': 'no such session'})
        try:
            off = int(self.headers.get('Last-Event-ID') or (q.get('off') or ['0'])[0])
            terminal_revision = int((q.get('terminal_rev') or ['0'])[0])
            if off < 0 or terminal_revision < 0:
                raise ValueError
        except ValueError:
            return self._json(400, {'error': 'bad offset'})
        expected_key = d.key
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.flush()
        last_owner = object()
        last_terminal = object()
        keepalive = time.monotonic()
        try:
            while d.key == expected_key:
                owner = transactions_for(d).ownership() if sid == 'serial' else None
                terminal = screen_for(stream)
                state = terminal.metadata() if hasattr(terminal, 'metadata') else None
                # Ownership is independent of byte offsets, including on a quiet
                # board. Bound the wait; only changed state emits a data event.
                wait = 0 if owner != last_owner or state != last_terminal else 1
                new_off, data, alive = stream.read(off, wait=wait)
                owner = transactions_for(d).ownership() if sid == 'serial' else None
                state = terminal.metadata() if hasattr(terminal, 'metadata') else None
                if data or not alive or owner != last_owner or state != last_terminal:
                    payload = {'sid': sid, 'key': d.key, 'off': new_off, 'alive': alive,
                               'data_b64': base64.b64encode(data).decode('ascii')}
                    if sid == 'serial':
                        payload['dev'] = d.dev
                        terminal = getattr(d, 'terminal', None)
                        payload['terminal_geometry'] = getattr(terminal, 'geometry', None)
                        payload['terminal_initial_geometry'] = terminal.geometry_at(new_off - len(data)) if terminal else None
                        payload['input_owner'] = owner
                        last_owner = owner
                    else:
                        payload['ip'] = stream.ip
                    terminal_payload(stream, payload, off, screen=(q.get('screen') or [''])[0] == '1',
                                     revision=terminal_revision, actor=self.headers.get('X-Seriald-Actor') or 'web',
                                     client_id=(q.get('client_id') or [''])[0])
                    new_off = payload['off']
                    last_terminal = state
                    terminal_revision = (last_terminal or {}).get('revision', 0)
                    last_owner = owner
                    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                    self.wfile.write(b'id: %d\ndata: ' % new_off + body + b'\n\n')
                    self.wfile.flush()
                    off = new_off
                    if not alive:
                        break
                else:
                    # read(0) intentionally returns immediately for snapshots;
                    # wait on the producer condition to avoid a busy SSE loop.
                    if off == 0:
                        with stream.cv:
                            if stream.fd is not None and not stream.buf:
                                stream.cv.wait(1 if sid == 'serial' else 10)
                    if time.monotonic() - keepalive >= 10:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        keepalive = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, code, payload):
        payload = api_response(code, payload).payload
        if getattr(self, 'response_key', None):
            payload = dict(payload, key=self.response_key)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _dev(self, q):
        key = (q.get('key') or [''])[0]
        self.response_key = key
        device = self.manager.resolve(key)
        if device and q.get('serial') and q['serial'][0] != device.serial:
            return None
        return device

    def _archives(self):
        return ArchiveQueries(self.manager, log_root=LOG_ROOT, reader=read_epoch,
                              searcher=search_epochs, comparator=compare_boots)

    def _download_log(self, path, name):
        """Stream one validated regular archive file without loading it into RAM."""
        try:
            source = open_archive(path)
        except OSError:
            return self._json(404, {'error': 'log unavailable'})
        with source:
            size = os.fstat(source.fileno()).st_size
            ascii_name = ''.join(char if 32 <= ord(char) < 127 and char not in '"\\'
                                 else '_' for char in name)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                             % (ascii_name, quote(name)))
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            shutil.copyfileobj(source, self.wfile, length=65536)

    def do_GET(self):
        self.response_key = None
        u = urlparse(self.path)
        route, q = u.path, parse_qs(u.query)

        if route == '/api/capabilities':
            result = capabilities()
            return self._json(result.status, result.payload)

        if self.manager is None:
            return self._json(503, {'error': 'collector manager unavailable'})

        if route == '/api/diagnostics':
            return self._json(200, diagnostic_summary(self.manager, LOG_ROOT, STARTED))

        if route in ('/api/artifact', '/api/artifact-download'):
            actions = ArtifactActions(self.manager)
            artifact_id = (q.get('id') or [''])[0]
            actor, serial = self.headers.get('X-Seriald-Actor', 'web'), (q.get('serial') or [None])[0]
            if route == '/api/artifact':
                result = actions.metadata(artifact_id, actor, serial)
                return self._json(result.status, result.payload)
            try:
                row, source = actions.download(artifact_id, actor, serial)
            except (TaskError, OSError) as error:
                return self._json(getattr(error, 'status', 503), {'error': str(error)})
            with source:
                name = row['name']
                fallback = ''.join(char if 32 <= ord(char) < 127 and char not in '"\\' else '_' for char in name)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                                 % (fallback, quote(name)))
                self.send_header('Content-Length', str(row['size']))
                self.send_header('X-Checksum-SHA256', row['sha256'])
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                shutil.copyfileobj(source, self.wfile, length=65536)
            return

        if route in ('/api/tasks', '/api/task'):
            try:
                limit = int((q.get('limit') or ['20'])[0])
                offset = int((q.get('offset') or ['0'])[0])
            except ValueError:
                return self._json(400, {'error': 'invalid task pagination'})
            task_id = (q.get('id') or [''])[0] if route == '/api/task' else None
            if route == '/api/task' and not task_id:
                return self._json(400, {'error': 'task id required'})
            result = TaskActions(self.manager).query(task_id, self.headers.get('X-Seriald-Actor', 'web'),
                                           (q.get('serial') or [None])[0], limit, offset,
                                           (q.get('connection_id') or [None])[0],
                                           (q.get('target_serial') or [None])[0])
            return self._json(result.status, result.payload)

        if route == '/api/health':
            storage = disk_status(LOG_ROOT, self.manager.maintenance.policy)
            devices = self.manager.all_devices()
            log_errors = []
            for device in devices:
                if isinstance(getattr(device, 'log_error', ''), str) and device.log_error:
                    log_errors.append({'key': device.key, 'kind': 'serial', 'error': device.log_error})
                with device.sessions_lock:
                    sessions = device.ssh_sessions
                    sessions = list(sessions.values()) if isinstance(sessions, dict) else []
                for session in sessions:
                    if getattr(session, 'log_error', ''):
                        log_errors.append({'key': device.key, 'kind': 'ssh',
                                           'sid': session.sid, 'error': session.log_error})
            return self._json(200, {'ok': True, 'role': 'collector', 'pid': os.getpid(),
                                    'version': __version__,
                                    'started': STARTED, 'uptime': time.time() - STARTED,
                                    'devices': len(devices),
                                    'online': sum(bool(d.serial and d.present) for d in devices),
                                    'pending': sum(bool(not d.serial and d.present) for d in devices),
                                    'offline': sum(not d.present for d in devices),
                                    'disk_total': storage.get('total', 0), 'disk_free': storage.get('free', 0),
                                    'storage': storage,
                                    'retention': self.manager.maintenance.snapshot(),
                                    'logging_ok': not log_errors, 'log_errors': log_errors})

        if route == '/api/devices':
            devices = self.manager.all_devices()
            devs = [d.snapshot() for d in devices]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'storage': disk_status(LOG_ROOT, self.manager.maintenance.policy),
                                    'server_time': time.time()})

        if route == '/api/hardware':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            actor = self.headers.get('X-Seriald-Actor') or 'web'
            return self._json(200, self.manager.hardware.available(device.serial or '', actor))

        if route == '/api/operation':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = DeviceActions(self.manager).operation(device)
            return self._json(result.status, result.payload)

        if route == '/api/boot-compare':
            result = self._archives().compare({key: values[0] for key, values in q.items()})
            return self._json(result.status, result.payload)

        if route in ('/api/serial', '/api/session', '/api/sessions'):
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            params = {key: values[0] for key, values in q.items()}
            params['actor'] = self.headers.get('X-Seriald-Actor') or 'web'
            result = SessionQueries().execute(route.removeprefix('/api/'), params, device)
            return self._json(result.status, result.payload)

        if route == '/api/session-events':
            return self._session_events(q)

        if route in ('/api/logs', '/api/log', '/api/boot-log', '/api/boot-search', '/api/activity', '/api/commands'):
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = self._archives().execute(route.removeprefix('/api/'),
                                               {key: values[0] for key, values in q.items()}, device)
            if isinstance(result, ArchiveDownload):
                return self._download_log(result.path, result.name)
            return self._json(result.status, result.payload)

        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        self.response_key = None
        route = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length') or 0)
            limit = body_limit(route)
            if n < 0 or n > limit:
                return self._json(413, {'error': 'body must be at most %d bytes' % limit})
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            raw = self.rfile.read(n)
            if len(raw) != n:
                return self._json(400, {'error': 'incomplete body'})
            payload = load_object(raw)
        except (ValueError, UnicodeDecodeError) as error:
            message = str(error)
            if message not in ('JSON object required', 'duplicate JSON field',
                               'JSON nesting too deep', 'non-finite JSON number'):
                message = 'bad json'
            return self._json(400, {'error': message})
        except TimeoutError:
            return self._json(408, {'error': 'request body timed out'})
        if self.manager is None:
            return self._json(503, {'error': 'collector manager unavailable'})
        if route == '/api/agent-revoke':
            actor = payload.get('actor')
            if not isinstance(actor, str) or not actor.startswith('shared-agent/'):
                return self._json(400, {'error': 'invalid agent actor'})
            closed = 0
            tasks = getattr(self.manager, 'tasks', None)
            if tasks is not None:
                tasks.cancel_actor(actor)
            for device in self.manager.all_devices():
                transactions = transactions_for(device)
                owner = transactions.ownership()
                if owner and owner['actor'] == actor:
                    try:
                        transactions.release(owner['id'], actor)
                    except TransactionError as error:
                        if error.code != 'not-found':
                            raise
                with device.sessions_lock:
                    ids = [sid for sid, session in device.ssh_sessions.items()
                           if session.actor == actor]
                for sid in ids:
                    closed += bool(device.close_session(sid))
                if (ids or owner and owner['actor'] == actor) and device.serial:
                    self.manager.store.event(device.serial, 'service', 'agent-revoke', actor)
            return self._json(200, {'ok': True, 'closed_sessions': closed})
        if route == '/api/task-cancel':
            result = TaskActions(self.manager).cancel(str(payload.get('id') or ''),
                                            str(payload.get('actor') or 'web'), payload.get('serial'))
            return self._json(result.status, result.payload)
        if route in ('/api/artifact-append', '/api/artifact-seal', '/api/artifact-delete'):
            result = ArtifactActions(self.manager).execute(route.removeprefix('/api/artifact-'), payload,
                                                 payload.get('actor') or 'web')
            return self._json(result.status, result.payload)
        q = {'key': [str(payload.get('key') or '')]}
        if payload.get('serial'):
            q['serial'] = [str(payload['serial'])]
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        actor = str(payload.get('actor') or 'web')[:80]
        if route == '/api/artifact-create':
            result = ArtifactActions(self.manager).execute('create', payload, actor, d)
            return self._json(result.status, result.payload)
        if route == '/api/task-start':
            result = TaskActions(self.manager).start(payload, d, actor)
            return self._json(result.status, result.payload)
        return self._device_post(route, payload, d, actor)

    def _device_post(self, route, payload, d, actor):
        result = DeviceActions(self.manager, recover=try_rescue, recovery_binary=RESCUE_BIN).execute(
            route.removeprefix('/api/'), payload, d, actor)
        return self._json(result.status, result.payload)

    def log_message(self, fmt, *args):
        pass
