"""Archive queries independent of HTTP parsing and global manager state."""
import base64
from dataclasses import dataclass
import os

from .results import response
from .archive_io import open_archive
from .config import LOG_ROOT
from .log_archive import read_epoch, search_epochs
from .boot_compare import MAX_BOOT_BYTES, compare_boots


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
            if offset < 0:
                raise ValueError
        except ValueError:
            return response(400, {'error': 'bad offset'})
        sid = 'serial' if action == 'serial' else params.get('sid', 'serial')
        stream = device if sid == 'serial' else device.get_session(sid)
        if not stream:
            return response(404, {'error': 'no such session'})
        offset, data, alive = stream.read(offset)
        result = {'off': offset, 'alive': alive,
                  'data_b64': base64.b64encode(data).decode('ascii'),
                  'data': data.decode('utf-8', 'replace')}
        if sid == 'serial':
            result['dev'] = device.dev
            result['terminal_geometry'] = getattr(getattr(device, 'terminal', None), 'geometry', None)
        else:
            result['ip'] = stream.ip
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

    def execute(self, action, values, device):
        params = parameters(values)
        if params is None:
            return response(400, {'error': 'invalid query parameters'})
        if action not in ('activity', 'commands', 'logs', 'boot-search', 'boot-log', 'log'):
            return response(404, {'error': 'not found'})
        try:
            return self._query(action, params, device)
        except OSError:
            return response(404, {'error': 'log unavailable'})

    def _query(self, action, params, d):
        if action == 'activity':
            try:
                legacy_offset = params.get('offset', '0')
                session_offset = int(params.get('session_offset', legacy_offset))
                event_offset = int(params.get('event_offset', '0'))
                limit = int(params.get('limit', '50'))
                if session_offset < 0 or event_offset < 0 or not 1 <= limit <= 50:
                    raise ValueError
            except ValueError:
                return response(400, {'error': 'bad offset'})
            history = self.manager.store.sessions(d.serial, session_offset, limit) if d.serial else []
            events = self.manager.store.visible_events(d.serial, event_offset, limit) if d.serial else []
            return response(200, {
                'sessions': history, 'session_offset': session_offset,
                'session_total': self.manager.store.session_count(d.serial) if d.serial else 0,
                'events': events, 'event_offset': event_offset,
                'event_total': self.manager.store.visible_event_count(d.serial) if d.serial else 0,
                # Keep old clients useful while they migrate to independent pagers.
                'next_offset': session_offset + len(history)})
        if action == 'commands':
            commands = self.manager.store.commands(d.serial, 20) if d.serial else []
            return response(200, {'commands': commands})
        files = d.log_files()
        if action == 'logs':
            archive = os.path.relpath(d.stable_dir(), self.log_root)
            boots = d.boot_epochs(files)
            boots = [{k: v for k, v in boot.items() if k != 'parts'} for boot in boots]
            return response(200, {'label': d.label, 'serial': d.serial,
                                    'archive': archive, 'files': files, 'boots': boots})
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
                result = self.search_epochs(d.stable_dir(), d.boot_epochs(files), query, offset, limit)
            except OSError:
                return response(404, {'error': '启动日志不可用，请刷新后重试'})
            result.update({'query': query, 'serial': d.serial, 'label': d.label})
            return response(200, result)
        if action == 'boot-log':
            boot_id = params.get('id', '')
            boot = next((item for item in d.boot_epochs(files) if item['id'] == boot_id), None)
            if not boot:
                return response(404, {'error': 'no such boot'})
            try:
                data = self.read_epoch(d.stable_dir(), boot)
            except OSError:
                return response(404, {'error': 'boot log unavailable'})
            return response(200, {'id': boot_id, 'data': data.decode('utf-8', 'replace'),
                'data_b64': base64.b64encode(data).decode('ascii'), 'start_off': 0,
                'off': len(data), 'size': len(data), 'more': False})
        name = params.get('file', '')
        if name not in {entry['name'] for entry in files}:
            return response(404, {'error': 'no such log'})
        path = os.path.join(d.stable_dir(), name)
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
