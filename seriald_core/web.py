"""Collector API served only on the local Unix socket."""
import base64
import http.server
import json
import os
import shutil
import sqlite3
import threading
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
from .flash_tasks import FlashActions
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
        expected_connection = self._read_guard[1]
        if not self._read_current():
            return self._json(404, {'error': 'device connection changed'})
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
            while d.key == expected_key and d.connection_id == expected_connection:
                owner = transactions_for(d).ownership() if sid == 'serial' else None
                terminal = screen_for(stream)
                state = terminal.metadata() if hasattr(terminal, 'metadata') else None
                # Ownership is independent of byte offsets, including on a quiet
                # board. Bound the wait; only changed state emits a data event.
                wait = 0 if owner != last_owner or state != last_terminal else 1
                new_off, data, alive = stream.read(off, wait=wait)
                if d.connection_id != expected_connection or d.key != expected_key:
                    break
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
                    if d.connection_id != expected_connection or d.key != expected_key:
                        break
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

    def _read_current(self):
        guard = getattr(self, '_read_guard', None)
        return guard is None or guard[0].connection_id == guard[1]

    def _json(self, code, payload):
        if not self._read_current():
            code, payload = 404, {'error': 'device connection changed', 'code': 'not-found'}
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
        generation = device.connection_id if device else None
        if device and q.get('serial') and q['serial'][0] != device.serial:
            return None
        expected = self.headers.get('X-Seriald-Connection') or (q.get('connection_id') or [''])[0]
        if device and expected and generation != expected:
            return None
        if device and self.headers.get('X-Seriald-Connection') and (
                not device.present or device.fd is None or device.stop.is_set()):
            return None
        if device and getattr(self, '_reading', False):
            self._read_guard = (device, generation)
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
            if not self._read_current():
                return self._json(404, {'error': 'device connection changed'})
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
            while self._read_current():
                chunk = source.read(65536)
                if not chunk or not self._read_current():
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        self.response_key = None
        self._reading, self._read_guard = True, None
        u = urlparse(self.path)
        route, q = u.path, parse_qs(u.query)

        if route == '/api/settings-health':
            return self._json(200, {'revision': getattr(self.server, 'settings_revision', None),
                                    'restarting': getattr(self.server, 'restart_requested', False)})

        if route == '/api/capabilities':
            result = capabilities()
            return self._json(result.status, result.payload)

        if self.manager is None:
            return self._json(503, {'error': 'collector manager unavailable'})

        if route in ('/api/image-catalog', '/api/image-library'):
            if self.headers.get('X-Seriald-Actor', 'web') != 'web':
                return self._json(403, {'error': 'image store requires a human operator'})
            try:
                from .image_catalog import catalog, library
                result = (library(self.manager) if route == '/api/image-library' else
                          catalog((q.get('source') or ['public'])[0], (q.get('path') or [''])[0],
                                  (q.get('refresh') or [''])[0] == '1'))
                return self._json(200, result)
            except (TaskError, OSError, ValueError) as error:
                return self._json(getattr(error, 'status', 503), {'error': str(error) if isinstance(error, TaskError) else '镜像商店暂不可用'})

        if route == '/api/device-preferences':
            try:
                from .device_preferences import snapshot
                return self._json(200, snapshot(self.manager))
            except (ValueError, sqlite3.Error, OSError):
                return self._json(503, {'error': '设备偏好读取失败'})

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
                                           (q.get('target_serial') or [None])[0],
                                           (q.get('target_id') or [None])[0])
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

        if route == '/api/flash-discover':
            result = FlashActions(self.manager).discover(self.headers.get('X-Seriald-Actor') or 'web')
            return self._json(result.status, result.payload)

        if route == '/api/flash-capabilities':
            target_id = (q.get('target_id') or [None])[0]
            device = self._dev(q) if not target_id else None
            # Flashing belongs to the registered board across UART generations.
            # Connection-scoped grants still fail _dev's explicit generation check.
            self._read_guard = None
            result = FlashActions(self.manager).capabilities(
                device, self.headers.get('X-Seriald-Actor') or 'web', target_id,
                (q.get('target_generation') or [None])[0])
            return self._json(result.status, result.payload)

        if route == '/api/flash-events':
            result = FlashActions(self.manager).events(
                (q.get('id') or [''])[0], (q.get('after') or ['0'])[0],
                self.headers.get('X-Seriald-Actor') or 'web',
                (q.get('serial') or [None])[0],
                self.headers.get('X-Seriald-Connection'), (q.get('target_id') or [None])[0])
            return self._json(result.status, result.payload)

        if route == '/api/operation':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = DeviceActions(self.manager).operation(device)
            return self._json(result.status, result.payload)

        if route == '/api/probe-settings':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = DeviceActions(self.manager).execute('probe-settings', {'read': True}, device,
                                                       self.headers.get('X-Seriald-Actor') or 'web')
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
        server = getattr(self, 'server', None)
        gate = getattr(server, 'configuration_gate', None)
        if gate is None:
            return self._do_POST()
        with gate:
            if getattr(server, 'restart_requested', False):
                return self._json(503, {'error': '采集器正在重启'})
            server.configuration_posts += 1
        try:
            return self._do_POST()
        finally:
            with gate:
                server.configuration_posts -= 1

    def _do_POST(self):
        if urlparse(self.path).path == '/api/settings-activate':
            try:
                from .web_settings import SettingsError
                activate = getattr(self.server, 'activate_settings', None)
                if not callable(activate):
                    raise SettingsError('当前运行方式不支持重启', 409)
                if self.headers.get('Transfer-Encoding'):
                    raise SettingsError('transfer encoding unsupported')
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 4096:
                    raise SettingsError('invalid body length')
                payload = load_object(self.rfile.read(length))
                result = activate(payload.get('revision'))
                try:
                    self._json(202, result)
                    self.wfile.flush()
                finally:
                    self.server.activation_reply.set()
                return
            except (ValueError, OSError) as error:
                return self._json(getattr(error, 'status', 400), {'error': str(error)})
        self.response_key = None
        self._reading, self._read_guard = False, None
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
        if route == '/api/device-preferences':
            try:
                from .device_preferences import apply
                from .web_settings import SettingsError
                if payload.get('action') not in ('preview', 'save'):
                    raise SettingsError('未知设备偏好操作')
                result = apply(self.manager, payload.get('configuration'), payload.get('revision'),
                               preview=payload['action'] == 'preview', client_id=payload.get('client_id', ''))
                return self._json(200, result)
            except (ValueError, TransactionError) as error:
                return self._json(getattr(error, 'status', 400), {'error': str(error)})
            except (sqlite3.Error, OSError):
                return self._json(503, {'error': '设备偏好保存失败，事务已回滚'})
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
                                            str(payload.get('actor') or 'web'), payload.get('serial'),
                                            self.headers.get('X-Seriald-Connection'))
            return self._json(result.status, result.payload)
        if route in ('/api/artifact-append', '/api/artifact-seal', '/api/artifact-delete'):
            result = ArtifactActions(self.manager).execute(route.removeprefix('/api/artifact-'), payload,
                                                 payload.get('actor') or 'web')
            return self._json(result.status, result.payload)
        actor = str(payload.get('actor') or 'web')[:80]
        if route == '/api/image-download':
            from .image_download import ImageDownloads
            device = None if payload.get('target_id') or not payload.get('key') else self._dev({'key': [str(payload.get('key') or '')],
                'serial': [str(payload.get('serial') or '')]})
            result = ImageDownloads(self.manager).start(payload, device, actor)
            return self._json(result.status, result.payload)
        if route == '/api/flash-target':
            result = FlashActions(self.manager).probe(payload, actor)
            return self._json(result.status, result.payload)
        if payload.get('target_id'):
            flash = FlashActions(self.manager)
            if route == '/api/artifact-create':
                result = flash.artifact_create(payload, actor)
            elif route == '/api/flash-plan':
                result = flash.plan(payload, None, actor)
            elif route == '/api/flash-selection':
                result = flash.selection(payload, None, actor)
            elif route == '/api/flash-recovery':
                result = flash.recovery(payload, None, actor)
            elif route == '/api/task-start' and payload.get('kind') in ('flash', 'nor-backup'):
                result = (flash.backup(payload, None, actor) if payload['kind'] == 'nor-backup'
                          else flash.start(payload, None, actor))
            else:
                return self._json(400, {'error': 'USB flash targets are not serial connections'})
            return self._json(result.status, result.payload)
        q = {'key': [str(payload.get('key') or '')]}
        if payload.get('serial'):
            q['serial'] = [str(payload['serial'])]
        if payload.get('connection_id'):
            q['connection_id'] = [str(payload['connection_id'])]
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        actor = str(payload.get('actor') or 'web')[:80]
        if route == '/api/artifact-create':
            result = ArtifactActions(self.manager).execute('create', payload, actor, d)
            return self._json(result.status, result.payload)
        if route == '/api/flash-plan':
            result = FlashActions(self.manager).plan(payload, d, actor)
            return self._json(result.status, result.payload)
        if route == '/api/flash-selection':
            result = FlashActions(self.manager).selection(payload, d, actor)
            return self._json(result.status, result.payload)
        if route == '/api/flash-recovery':
            result = FlashActions(self.manager).recovery(payload, d, actor)
            return self._json(result.status, result.payload)
        if route == '/api/task-start':
            result = TaskActions(self.manager).start(payload, d, actor)
            return self._json(result.status, result.payload)
        return self._device_post(route, payload, d, actor)

    def _device_post(self, route, payload, d, actor):
        actions = DeviceActions(self.manager, recover=try_rescue, recovery_binary=RESCUE_BIN)
        expected = self.headers.get('X-Seriald-Connection') or payload.get('connection_id')
        if expected:
            try:
                with transactions_for(d).task_scope(threading.Event(), expected, d.stop):
                    transactions_for(d).check_context()
                    result = actions.execute(route.removeprefix('/api/'), payload, d, actor)
            except TransactionError as error:
                return self._json(error.status, {'error': str(error), 'code': error.code})
        else:
            result = actions.execute(route.removeprefix('/api/'), payload, d, actor)
        return self._json(result.status, result.payload)

    def log_message(self, fmt, *args):
        pass
