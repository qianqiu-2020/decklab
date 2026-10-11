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
from .devices import Manager
from .storage import IdentityError
from .httpd import BoundedThreadingTCPServer as BoundedThreadingServer
from .log_archive import read_epoch, search_epochs
from .boot_compare import MAX_BOOT_BYTES, compare_boots
from .actions import DeviceActions, capabilities
from . import __version__

MGR = Manager()
STARTED = time.time()

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'seriald-collector/' + __version__

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
            if off < 0:
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
        try:
            while d.key == expected_key:
                new_off, data, alive = stream.read(off)
                if data or not alive:
                    payload = {'sid': sid, 'key': d.key, 'off': new_off, 'alive': alive,
                               'data_b64': base64.b64encode(data).decode('ascii')}
                    if sid == 'serial':
                        payload['dev'] = d.dev
                    else:
                        payload['ip'] = stream.ip
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
                                stream.cv.wait(10)
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, code, payload):
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
        device = MGR.resolve(key)
        if device and q.get('serial') and q['serial'][0] != device.serial:
            return None
        return device

    def _download_log(self, path, name):
        """Stream one validated regular archive file without loading it into RAM."""
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            source = os.fdopen(fd, 'rb')
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

        if route == '/api/health':
            disk = shutil.disk_usage(LOG_ROOT)
            devices = MGR.all_devices()
            log_errors = []
            for device in devices:
                if isinstance(getattr(device, 'log_error', ''), str) and device.log_error:
                    log_errors.append({'key': device.key, 'kind': 'serial', 'error': device.log_error})
                sessions = getattr(device, 'ssh_sessions', {})
                for session in (sessions.values() if isinstance(sessions, dict) else ()):
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
                                    'disk_total': disk.total, 'disk_free': disk.free,
                                    'logging_ok': not log_errors, 'log_errors': log_errors})

        if route == '/api/devices':
            devices = MGR.all_devices()
            devs = [d.snapshot() for d in devices]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'server_time': time.time()})

        if route == '/api/operation':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = DeviceActions(MGR).operation(device)
            return self._json(result.status, result.payload)

        if route == '/api/boot-compare':
            selected = []
            for side in ('left', 'right'):
                key = (q.get(side + '_key') or [''])[0]
                serial = (q.get(side + '_serial') or [''])[0]
                boot_id = (q.get(side + '_id') or [''])[0]
                device = MGR.resolve(key) if key else None
                if not device or not serial or device.serial != serial:
                    return self._json(404, {'error': '%s device unavailable' % side})
                boot = next((item for item in device.boot_epochs() if item['id'] == boot_id), None)
                if not boot:
                    return self._json(404, {'error': '%s boot unavailable' % side})
                if boot['size'] > MAX_BOOT_BYTES:
                    return self._json(413, {'error': '%s boot exceeds 4 MiB' % side})
                selected.append((device, boot))
            try:
                data = [read_epoch(device.stable_dir(), boot) for device, boot in selected]
                comparison = compare_boots(*data)
            except OSError:
                return self._json(404, {'error': 'boot log unavailable'})
            except ValueError as error:
                return self._json(413, {'error': str(error)})
            comparison['left'] = {'key': selected[0][0].key, 'serial': selected[0][0].serial,
                                  'boot': {k: v for k, v in selected[0][1].items() if k != 'parts'}}
            comparison['right'] = {'key': selected[1][0].key, 'serial': selected[1][0].serial,
                                   'boot': {k: v for k, v in selected[1][1].items() if k != 'parts'}}
            return self._json(200, comparison)

        if route == '/api/serial':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            try:
                off = int((q.get('off') or ['0'])[0] or 0)
                if off < 0:
                    raise ValueError
            except ValueError:
                return self._json(400, {'error': 'bad offset'})
            new_off, data, alive = d.read(off)
            return self._json(200, {'key': d.key, 'dev': d.dev, 'off': new_off,
                                    'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})

        if route == '/api/session':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            sid = (q.get('sid') or ['serial'])[0]
            try:
                off = int((q.get('off') or ['0'])[0] or 0)
                if off < 0:
                    raise ValueError
            except ValueError:
                return self._json(400, {'error': 'bad offset'})
            if sid == 'serial':
                new_off, data, alive = d.read(off)
                return self._json(200, {'sid': 'serial', 'dev': d.dev, 'off': new_off,
                                        'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})
            s = d.get_session(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            new_off, data, alive = s.read(off)
            return self._json(200, {'sid': sid, 'ip': s.ip, 'off': new_off,
                                    'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})

        if route == '/api/session-events':
            return self._session_events(q)

        if route == '/api/sessions':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            out = [{'sid': 'serial', 'kind': 'serial', 'dev': d.dev,
                    'alive': d.fd is not None}]
            out += d.list_sessions()
            return self._json(200, {'sessions': out, 'label': d.label})

        if route in ('/api/logs', '/api/log', '/api/boot-log', '/api/boot-search', '/api/activity', '/api/commands'):
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            if route == '/api/activity':
                try:
                    legacy_offset = (q.get('offset') or ['0'])[0]
                    session_offset = int((q.get('session_offset') or [legacy_offset])[0])
                    event_offset = int((q.get('event_offset') or ['0'])[0])
                    limit = int((q.get('limit') or ['50'])[0])
                    if session_offset < 0 or event_offset < 0 or not 1 <= limit <= 50:
                        raise ValueError
                except ValueError:
                    return self._json(400, {'error': 'bad offset'})
                history = MGR.store.sessions(d.serial, session_offset, limit) if d.serial else []
                events = MGR.store.visible_events(d.serial, event_offset, limit) if d.serial else []
                return self._json(200, {
                    'sessions': history, 'session_offset': session_offset,
                    'session_total': MGR.store.session_count(d.serial) if d.serial else 0,
                    'events': events, 'event_offset': event_offset,
                    'event_total': MGR.store.visible_event_count(d.serial) if d.serial else 0,
                    # Keep old clients useful while they migrate to independent pagers.
                    'next_offset': session_offset + len(history)})
            if route == '/api/commands':
                commands = MGR.store.commands(d.serial, 20) if d.serial else []
                return self._json(200, {'commands': commands})
            files = d.log_files()
            if route == '/api/logs':
                archive = os.path.relpath(d.stable_dir(), LOG_ROOT)
                boots = d.boot_epochs(files)
                for boot in boots:
                    boot.pop('parts', None)
                return self._json(200, {'label': d.label, 'serial': d.serial,
                                        'archive': archive, 'files': files, 'boots': boots})
            if route == '/api/boot-search':
                query = (q.get('q') or [''])[0].strip()
                try:
                    offset = int((q.get('offset') or ['0'])[0])
                    limit = int((q.get('limit') or ['20'])[0])
                    if not query or len(query) > 200 or offset < 0 or not 1 <= limit <= 50:
                        raise ValueError
                except ValueError:
                    return self._json(400, {'error': '请输入 1–200 字符的关键词，分页参数必须有效'})
                try:
                    result = search_epochs(d.stable_dir(), d.boot_epochs(files), query, offset, limit)
                except OSError:
                    return self._json(404, {'error': '启动日志不可用，请刷新后重试'})
                result.update({'query': query, 'serial': d.serial, 'label': d.label})
                return self._json(200, result)
            if route == '/api/boot-log':
                boot_id = (q.get('id') or [''])[0]
                boot = next((item for item in d.boot_epochs(files) if item['id'] == boot_id), None)
                if not boot:
                    return self._json(404, {'error': 'no such boot'})
                try:
                    data = read_epoch(d.stable_dir(), boot)
                except OSError:
                    return self._json(404, {'error': 'boot log unavailable'})
                return self._json(200, {'id': boot_id, 'data': data.decode('utf-8', 'replace'),
                    'data_b64': base64.b64encode(data).decode('ascii'), 'start_off': 0,
                    'off': len(data), 'size': len(data), 'more': False})
            name = (q.get('file') or [''])[0]
            if name not in {entry['name'] for entry in files}:
                return self._json(404, {'error': 'no such log'})
            path = os.path.join(d.stable_dir(), name)
            if (q.get('download') or ['0'])[0] == '1':
                return self._download_log(path, name)
            try:
                offset = int((q.get('off') or ['-1'])[0])
                if offset < -1:
                    raise ValueError
                full = (q.get('full') or ['0'])[0] == '1'
                # Open only regular files inside the selected board's directory, no symlink traversal.
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as source:
                    size = os.fstat(source.fileno()).st_size
                    start = (0 if full else max(0, size - 65536)) if offset == -1 else min(offset, size)
                    source.seek(start)
                    data = source.read() if full else source.read(65536)
                return self._json(200, {'file': name, 'data': data.decode('utf-8', 'replace'),
                    'data_b64': base64.b64encode(data).decode('ascii'), 'start_off': start,
                    'off': start + len(data), 'size': size, 'more': start + len(data) < size})
            except ValueError:
                return self._json(400, {'error': 'bad offset'})
            except OSError:
                return self._json(404, {'error': 'log unavailable'})

        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        self.response_key = None
        route = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0 or n > 65536:
                return self._json(413, {'error': 'body must be at most 65536 bytes'})
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            raw = self.rfile.read(n)
            if len(raw) != n:
                return self._json(400, {'error': 'incomplete body'})
            payload = json.loads(raw.decode('utf-8')) if n else {}
            if not isinstance(payload, dict):
                return self._json(400, {'error': 'JSON object required'})
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {'error': 'bad json'})
        except TimeoutError:
            return self._json(408, {'error': 'request body timed out'})
        if route == '/api/agent-revoke':
            actor = payload.get('actor')
            if not isinstance(actor, str) or not actor.startswith('shared-agent/'):
                return self._json(400, {'error': 'invalid agent actor'})
            closed = 0
            for device in MGR.all_devices():
                with device.sessions_lock:
                    ids = [sid for sid, session in device.ssh_sessions.items()
                           if session.actor == actor]
                for sid in ids:
                    closed += bool(device.close_session(sid))
                if ids and device.serial:
                    MGR.store.event(device.serial, 'service', 'agent-revoke', actor)
            return self._json(200, {'ok': True, 'closed_sessions': closed})
        q = {'key': [str(payload.get('key') or '')]}
        if payload.get('serial'):
            q['serial'] = [str(payload['serial'])]
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        actor = str(payload.get('actor') or 'web')[:80]
        return self._device_post(route, payload, d, actor)

    def _device_post(self, route, payload, d, actor):
        result = DeviceActions(MGR, recover=try_rescue, recovery_binary=RESCUE_BIN).execute(
            route.removeprefix('/api/'), payload, d, actor)
        return self._json(result.status, result.payload)

    def log_message(self, fmt, *args):
        pass
