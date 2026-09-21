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
from .log_archive import read_epoch
from . import __version__

MGR = Manager()
STARTED = time.time()

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'seriald-collector/' + __version__

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
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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

        if route == '/api/health':
            disk = shutil.disk_usage(LOG_ROOT)
            devices = MGR.all_devices()
            return self._json(200, {'ok': True, 'role': 'collector', 'pid': os.getpid(),
                                    'version': __version__,
                                    'started': STARTED, 'uptime': time.time() - STARTED,
                                    'devices': len(devices),
                                    'online': sum(bool(d.serial and d.present) for d in devices),
                                    'pending': sum(bool(not d.serial and d.present) for d in devices),
                                    'offline': sum(not d.present for d in devices),
                                    'disk_total': disk.total, 'disk_free': disk.free})

        if route == '/api/devices':
            devices = MGR.all_devices()
            devs = [d.snapshot() for d in devices]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'server_time': time.time()})

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

        if route == '/api/sessions':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            out = [{'sid': 'serial', 'kind': 'serial', 'dev': d.dev,
                    'alive': d.fd is not None}]
            out += d.list_sessions()
            return self._json(200, {'sessions': out, 'label': d.label})

        if route in ('/api/logs', '/api/log', '/api/boot-log', '/api/activity', '/api/commands'):
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
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
        q = {'key': [str(payload.get('key') or '')]}
        if payload.get('serial'):
            q['serial'] = [str(payload['serial'])]
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        actor = str(payload.get('actor') or 'web')[:80]
        if route == '/api/session-result':
            try:
                if not d.serial:
                    raise IdentityError('设备尚未识别')
                MGR.store.session_result(d.serial, str(payload.get('sid') or ''),
                                         payload.get('completed'), payload.get('exit_code'))
                return self._json(200, {'ok': True})
            except IdentityError as error:
                return self._json(400, {'ok': False, 'error': str(error)})

        if route == '/api/switch-mode':
            try:
                old_serial = d.serial
                key = MGR.prepare_switch(d, actor)
                return self._json(200, {'ok': True, 'device_key': key, 'serial': '',
                                        'old_serial': old_serial})
            except IdentityError as error:
                return self._json(409, {'ok': False, 'error': str(error)})

        if d.serial and route in ('/api/send', '/api/session-send', '/api/rescue', '/api/identify', '/api/ssh-enable'):
            MGR.store.event(d.serial, actor, route.rsplit('/', 1)[-1],
                            'request: sid=' + str(payload.get('sid') or 'serial'))

        if route == '/api/send':
            text = str(payload.get('data') or '')
            if not text:
                return self._json(400, {'error': 'data required'})
            ok, err = d.send(text)
            return self._json(200 if ok else 503, {'ok': ok, 'error': err, 'dev': d.dev})

        if route == '/api/probe':
            ok, err = d.send('\r')
            if ok:
                d.last_probe = time.time()
                d.probed += 1
                d._say('手动探测:已发送回车')
            return self._json(200, {'ok': ok, 'error': err})

        if route == '/api/rescue':
            # 手动自愈。mode: soft=只关 autosuspend(最轻), rebind=追加软拔插。
            # 分两步是为了能区分病因,见 rescue.sh 注释。
            mode = str(payload.get('mode') or 'soft')
            if mode not in ('soft', 'rebind'):
                return self._json(400, {'error': 'mode must be soft|rebind'})
            if not d.operation_lock.acquire(blocking=False):
                return self._json(409, {'ok': False, 'error': '已有控制台操作在进行中'})
            try:
                with d.write_lock:
                    if not d.present or not MGR._matches(d):
                        return self._json(409, {'ok': False, 'error': '设备已重新枚举,请刷新后重试'})
                    d.last_rescue = time.time()
                    rx_before = d.bytes
                    ok, note = try_rescue(d.dev, mode)
                d.rescue_note = note
                d.fail_count = 0
                d._say('手动自愈(%s)%s: %s' % (mode, '步骤完成' if ok else '失败', note))
            finally:
                d.operation_lock.release()
            # sysfs 写成功只能证明辅助步骤完成;不能据此宣布采集器已恢复。
            recovered = d.fd is not None and d.bytes > rx_before and MGR._matches(d)
            requires_setup = not ok and any(x in note.lower() for x in
                                           ('未安装', 'password', 'not allowed', 'sudo 需要密码'))
            hint = ''
            if requires_setup:
                hint = ('特权 recovery helper 未就绪。检查 %s、'
                        '/etc/sudoers.d/seriald-rescue 和 collector 日志；'
                        'Ubuntu 包会自动安装并仅向 seriald 服务账户放行该 helper。' % RESCUE_BIN)
            return self._json(200, {'ok': ok, 'recovered': recovered, 'requires_setup': requires_setup,
                                    'note': note, 'hint': hint, 'mode': mode})

        if route == '/api/ssh-connect':
            # 一键 SSH:起一条与串口并存的独立会话,输出单独落盘
            ssh_user = payload.get('ssh_user')
            ssh_password = payload.get('ssh_password')
            if ssh_user is not None and (not isinstance(ssh_user, str) or not ssh_user.strip()):
                return self._json(400, {'ok': False, 'error': 'ssh_user must be a non-empty string'})
            if ssh_password is not None and not isinstance(ssh_password, str):
                return self._json(400, {'ok': False, 'error': 'ssh_password must be a string'})
            s, err = d.ssh_connect(actor=actor, title=str(payload.get('title') or '')[:2048],
                                    ssh_user=ssh_user.strip() if isinstance(ssh_user, str) else None,
                                    ssh_password=ssh_password)
            if not s:
                return self._json(502, {'ok': False, 'error': err})
            return self._json(200, {'ok': True, 'sid': s.sid, 'ip': s.ip,
                                    'persistent': bool(getattr(s, 'persistent', False)),
                                    'auto_configured': bool(getattr(s, 'auto_configured', False)),
                                    'device_key': d.key, 'serial': d.serial})

        if route == '/api/session-send':
            sid = str(payload.get('sid') or 'serial')
            text = str(payload.get('data') or '')
            if not text:
                return self._json(400, {'error': 'data required'})
            if sid == 'serial':
                ok, err = d.send(text)
            else:
                s = d.get_session(sid)
                if not s:
                    return self._json(404, {'error': 'no such session'})
                ok, err = (s.write(text), '')
            if ok:
                d.record_input(sid, actor, text)
            return self._json(200 if ok else 503, {'ok': ok, 'error': err})

        if route == '/api/session-takeover':
            sid = str(payload.get('sid') or '')
            if not d.serial or not d.get_session(sid):
                return self._json(404, {'ok': False, 'error': 'no such active SSH session'})
            MGR.store.event(d.serial, actor, 'session-takeover', sid)
            return self._json(200, {'ok': True, 'sid': sid})

        if route == '/api/session-resize':
            sid = str(payload.get('sid') or '')
            try:
                cols = int(payload.get('cols') or 0)
                rows = int(payload.get('rows') or 0)
            except (TypeError, ValueError):
                return self._json(400, {'error': 'bad cols/rows'})
            s = d.get_session(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            return self._json(200, {'ok': s.resize(cols, rows), 'cols': s.cols, 'rows': s.rows})

        if route == '/api/session-close':
            sid = str(payload.get('sid') or '')
            if not d.close_session(sid):
                return self._json(404, {'error': 'no such session'})
            return self._json(200, {'ok': True})

        if route == '/api/identify':
            init_marker = payload.get('init_boot_marker', False)
            if type(init_marker) is not bool:
                return self._json(400, {'ok': False, 'error': 'init_boot_marker must be a boolean'})
            info = d.run_probe(init_boot_marker=True) if init_marker else d.run_probe()
            if init_marker and info.get('ok'):
                MGR.store.event(d.serial, actor, 'boot-marker-initialized', '/boot/seriald-id')
            return self._json(200 if info.get('ok') else 502, dict(info, device_key=d.key))

        if route == '/api/ssh-enable':
            r = d.run_ssh_enable()
            return self._json(200 if r.get('ok') else 502, r)

        if route == '/api/label':
            try:
                key = MGR.rename(d, payload.get('label'))
                return self._json(200, {'ok': True, 'label': key, 'device_key': key, 'serial': d.serial})
            except IdentityError as error:
                return self._json(409, {'ok': False, 'error': str(error)})

        return self._json(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        pass
