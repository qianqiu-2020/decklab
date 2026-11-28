"""Archive queries independent of HTTP parsing and global manager state."""
import base64
from dataclasses import dataclass
import os
import time

from .results import response
from .archive_io import open_archive
from .config import LOG_ROOT
from .log_archive import read_epoch, search_epochs
from .boot_compare import MAX_BOOT_BYTES, compare_boots
from .terminal_backend import terminal_payload


@dataclass(frozen=True)
class ArchiveDownload:
    path: str
    name: str



def parameters(values):
    if not isinstance(values, dict) or any(
            not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (str, int))
            for key, value in values.items()):
        return None
    return {key: str(value) for key, value in values.items()}


class SessionQueries:
    """Read cursors are caller-owned; this service never consumes another reader's output."""
    def execute(self, action, values, device):
        params = parameters(values)
        if params is None:
            return response(400, {'error': 'invalid query parameters'})
        if action == 'sessions':
            sessions = [{'sid': 'serial', 'kind': 'serial', 'dev': device.dev,
                         'alive': device.fd is not None}] + device.list_sessions()
            return response(200, {'sessions': sessions, 'label': device.label})
        if action not in ('serial', 'session'):
            return response(404, {'error': 'not found'})
        try:
            offset = int(params.get('off', '0') or 0)
            revision = int(params.get('terminal_rev', '0'))
            limit = int(params['max_bytes']) if action == 'serial' and 'max_bytes' in params else None
            if (offset < 0 or revision < 0 or
                    limit is not None and (not 1 <= limit <= 65536 or params.get('screen') == '1')):
                raise ValueError
        except ValueError:
            return response(400, {'error': 'bad offset'})
        sid = 'serial' if action == 'serial' else params.get('sid', 'serial')
        stream = device if sid == 'serial' else device.get_session(sid)
        if not stream:
            return response(404, {'error': 'no such session'})
        if action == 'serial' and params.get('wait') == '0':
            offset, data, alive = stream.read(offset, wait=0)
        else:
            offset, data, alive = stream.read(offset, wait=1) if params.get('screen') == '1' else stream.read(offset)
        more = limit is not None and len(data) > limit
        if more:
            offset -= len(data) - limit
            data = data[:limit]
        result = {'off': offset, 'alive': alive,
                  'data_b64': base64.b64encode(data).decode('ascii'),
                  'data': data.decode('utf-8', 'replace')}
        if limit is not None:
            result['more'] = more
        if sid == 'serial':
            result['dev'] = device.dev
            terminal = getattr(device, 'terminal', None)
            result['terminal_geometry'] = getattr(terminal, 'geometry', None)
            result['terminal_initial_geometry'] = terminal.geometry_at(offset - len(data)) if terminal else None
        else:
            result['ip'] = stream.ip
        terminal_payload(stream, result, int(params.get('off', '0') or 0),
                         screen=params.get('screen') == '1', revision=revision,
                         actor=params.get('actor', 'web'), client_id=params.get('client_id', ''))
        if action == 'serial':
            result['key'] = device.key
        else:
            result['sid'] = sid
        return response(200, result)


class ArchiveQueries:
    def __init__(self, manager, log_root=LOG_ROOT, reader=None, searcher=None, comparator=None):
        self.manager, self.log_root = manager, log_root
        self.read_epoch = reader if reader is not None else read_epoch
        self.search_epochs = searcher if searcher is not None else search_epochs
        self.compare_boots = comparator if comparator is not None else compare_boots

    def execute(self, action, values, device, allow_connection_history=True, actor='web'):
        params = parameters(values)
        if params is None:
            return response(400, {'error': 'invalid query parameters'})
        if action not in ('activity', 'commands', 'logs', 'boot-search', 'boot-log', 'log'):
            return response(404, {'error': 'not found'})
        try:
            return self._query(action, params, device, allow_connection_history, actor)
        except OSError:
            return response(404, {'error': 'log unavailable'})

    def _query(self, action, params, d, allow_connection_history=True, actor='web'):
        if action == 'activity':
            if params.get('capture') == '1':
                saved = (self.manager.store.serial_capture_events(d.serial)
                         if d.serial and allow_connection_history else [])
                with d.capture_lock:
                    current = list(d.capture_events)
                events = {(event['at'], event['connection_id'], event['event']): event for event in saved + current}
                return response(200, {'capture_events': sorted(
                    events.values(), key=lambda event: event['at'], reverse=True)[:24]})
            try:
                legacy_offset = params.get('offset', '0')
                session_offset = int(params.get('session_offset', legacy_offset))
                event_offset = int(params.get('event_offset', '0'))
                limit = int(params.get('limit', '50'))
                if session_offset < 0 or event_offset < 0 or not 1 <= limit <= 50:
                    raise ValueError
            except ValueError:
                return response(400, {'error': 'bad offset'})
            kind = params.get('session_kind') or None
            if kind not in (None, 'adb', 'ssh'):
                return response(400, {'error': 'invalid session kind'})
            history = self.manager.store.sessions(d.serial, session_offset, limit, kind) if d.serial else []
            events = self.manager.store.visible_events(d.serial, event_offset, limit) if d.serial else []
            return response(200, {
                'sessions': history, 'session_offset': session_offset,
                'session_total': self.manager.store.session_count(d.serial, kind) if d.serial else 0,
                'events': events, 'event_offset': event_offset,
                'event_total': self.manager.store.visible_event_count(d.serial) if d.serial else 0,
                # Keep old clients useful while they migrate to independent pagers.
                'next_offset': session_offset + len(history)})
        if action == 'commands':
            commands = self.manager.store.commands(d.serial, 20) if d.serial else []
            return response(200, {'commands': commands})
        if action == 'logs' and params.get('task_window'):
            return self._task_window(d, params['task_window'], allow_connection_history, actor)
        connection = params.get('archive_connection', '')
        if connection and (d.serial or not allow_connection_history):
            return response(403, {'error': '此授权不能读取其他串口连接的归档'})
        connections = d.archive_connections() if not d.serial and allow_connection_history else []
        root = d.stable_dir()
        if connection:
            if not any(item['id'] == connection for item in connections):
                return response(404, {'error': 'no such archived connection'})
            root = os.path.join(os.path.dirname(root), connection)
            if os.path.realpath(root) != os.path.abspath(root):
                return response(404, {'error': 'log unavailable'})
            files = d.log_files(directory=root)
            boots = lambda: d.boot_epochs(files, directory=root)
        else:
            files = d.log_files()
            boots = lambda: d.boot_epochs(files)
        if action == 'logs':
            archive = os.path.relpath(root, self.log_root)
            epochs = [{k: v for k, v in boot.items() if k != 'parts'} for boot in boots()]
            return response(200, {'label': d.label, 'serial': d.serial,
                'archive': archive, 'files': files, 'boots': epochs,
                'connections': connections, 'archive_connection': connection or (d.connection_id if not d.serial else '')})
        if action == 'boot-search':
            query = params.get('q', '').strip()
            try:
                offset = int(params.get('offset', '0'))
                limit = int(params.get('limit', '20'))
                if not query or len(query) > 200 or offset < 0 or not 1 <= limit <= 50:
                    raise ValueError
            except ValueError:
                return response(400, {'error': '请输入 1–200 字符的关键词，分页参数必须有效'})
            try:
                result = self.search_epochs(root, boots(), query, offset, limit)
            except OSError:
                return response(404, {'error': '启动日志不可用，请刷新后重试'})
            result.update({'query': query, 'serial': d.serial, 'label': d.label})
            return response(200, result)
        if action == 'boot-log':
            boot_id = params.get('id', '')
            boot = next((item for item in boots() if item['id'] == boot_id), None)
            if not boot:
                return response(404, {'error': 'no such boot'})
            try:
                data = self.read_epoch(root, boot)
            except OSError:
                return response(404, {'error': 'boot log unavailable'})
            return response(200, {'id': boot_id, 'data': data.decode('utf-8', 'replace'),
                'data_b64': base64.b64encode(data).decode('ascii'), 'start_off': 0,
                'off': len(data), 'size': len(data), 'more': False})
        name = params.get('file', '')
        if name not in {entry['name'] for entry in files}:
            return response(404, {'error': 'no such log'})
        path = os.path.join(root, name)
        if params.get('download', '0') == '1':
            return ArchiveDownload(path, name)
        try:
            offset = int(params.get('off', '-1'))
            if offset < -1:
                raise ValueError
            full = params.get('full', '0') == '1'
            # Open only regular files inside the selected board's directory, no symlink traversal.
            with open_archive(path) as source:
                size = os.fstat(source.fileno()).st_size
                start = (0 if full else max(0, size - 65536)) if offset == -1 else min(offset, size)
                source.seek(start)
                data = source.read() if full else source.read(65536)
            return response(200, {'file': name, 'data': data.decode('utf-8', 'replace'),
                'data_b64': base64.b64encode(data).decode('ascii'), 'start_off': start,
                'off': start + len(data), 'size': size, 'more': start + len(data) < size})
        except ValueError:
            return response(400, {'error': 'bad offset'})
        except OSError:
            return response(404, {'error': 'log unavailable'})

    def _task_window(self, device, task_id, allow_connection_history, actor):
        from .tasks import TaskError

        tasks = getattr(self.manager, 'tasks', None)
        if not tasks or not device.serial or not allow_connection_history:
            return response(403, {'error': '当前授权不能读取任务时段的历史串口'})
        try:
            task = tasks.store.get(task_id)
        except TaskError as error:
            return response(error.status, {'error': str(error)})
        if (actor != 'web' and task['actor'] != actor) or not tasks.store.visible_serial(task, device.serial):
            return response(404, {'error': '该板卡不在此任务的目标中'})
        end = task['updated']
        start = max(task['created'], end - 300)
        return response(200, self.serial_interval(device, start, end))

    @staticmethod
    def serial_interval(device, start, end):
        start_text, end_text = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp)).encode()
                                for stamp in (start, end))
        output = bytearray()
        limited = False
        for entry in reversed(device.log_files()):
            if entry['kind'] != 'serial' or not start_text[:10].decode() <= entry['date'] <= end_text[:10].decode():
                continue
            with open_archive(os.path.join(device.stable_dir(), entry['name'])) as source:
                skip = max(0, os.fstat(source.fileno()).st_size - (16 << 20))
                if skip:
                    limited = True
                    source.seek(skip)
                    source.readline(16384)
                for line in source:
                    if len(line) >= 21 and line[19:21] == b'  ' and start_text <= line[:19] <= end_text:
                        output.extend(line)
                        if len(output) > (512 << 10):
                            limited = True
                            del output[:len(output) - (512 << 10)]
        if limited and output and not output[:4].isdigit():
            first_newline = output.find(b'\n')
            del output[:first_newline + 1]
        return {'data': output.decode('utf-8', 'replace'), 'from': start, 'to': end, 'limited': limited}

    def compare(self, values):
        params = parameters(values)
        if params is None:
            return response(400, {'error': 'invalid query parameters'})
        selected = []
        for side in ('left', 'right'):
            key = params.get(side + '_key', '')
            serial = params.get(side + '_serial', '')
            boot_id = params.get(side + '_id', '')
            device = self.manager.resolve(key) if key else None
            if not device or not serial or device.serial != serial:
                return response(404, {'error': '%s device unavailable' % side})
            try:
                boot = next((item for item in device.boot_epochs() if item['id'] == boot_id), None)
            except OSError:
                return response(404, {'error': 'boot log unavailable'})
            if not boot:
                return response(404, {'error': '%s boot unavailable' % side})
            if boot['size'] > MAX_BOOT_BYTES:
                return response(413, {'error': '%s boot exceeds 4 MiB' % side})
            selected.append((device, boot))
        try:
            data = [self.read_epoch(device.stable_dir(), boot) for device, boot in selected]
            comparison = self.compare_boots(*data)
        except OSError:
            return response(404, {'error': 'boot log unavailable'})
        except ValueError as error:
            return response(413, {'error': str(error)})
        comparison['left'] = {'key': selected[0][0].key, 'serial': selected[0][0].serial,
                              'boot': {k: v for k, v in selected[0][1].items() if k != 'parts'}}
        comparison['right'] = {'key': selected[1][0].key, 'serial': selected[1][0].serial,
                               'boot': {k: v for k, v in selected[1][1].items() if k != 'parts'}}
        return response(200, comparison)
