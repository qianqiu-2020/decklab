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
from .boot_compare import compare_boots
from .actions import DeviceActions, capabilities
from .queries import ArchiveQueries, ArchiveDownload, SessionQueries, open_archive
from . import __version__
from .results import response as api_response
from .transactions import transactions_for, TransactionError
from .maintenance import disk_status, diagnostic_summary

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
        device = MGR.resolve(key)
        if device and q.get('serial') and q['serial'][0] != device.serial:
            return None
        return device

    def _archives(self):
        return ArchiveQueries(MGR, log_root=LOG_ROOT, reader=read_epoch,
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

        if route == '/api/diagnostics':
            return self._json(200, diagnostic_summary(MGR, LOG_ROOT, STARTED))

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
                                    'storage': disk_status(LOG_ROOT, MGR.maintenance.policy),
                                    'retention': MGR.maintenance.snapshot(),
                                    'logging_ok': not log_errors, 'log_errors': log_errors})

        if route == '/api/devices':
            devices = MGR.all_devices()
            devs = [d.snapshot() for d in devices]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'storage': disk_status(LOG_ROOT, MGR.maintenance.policy),
                                    'server_time': time.time()})

        if route == '/api/operation':
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = DeviceActions(MGR).operation(device)
            return self._json(result.status, result.payload)

        if route == '/api/boot-compare':
            result = self._archives().compare({key: values[0] for key, values in q.items()})
            return self._json(result.status, result.payload)

        if route in ('/api/serial', '/api/session', '/api/sessions'):
            device = self._dev(q)
            if not device:
                return self._json(404, {'error': 'no such device'})
            result = SessionQueries().execute(route.removeprefix('/api/'),
                                              {key: values[0] for key, values in q.items()}, device)
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
