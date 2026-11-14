"""Read-only, bounded catalogs for the two administrator-selected repositories."""
from html.parser import HTMLParser
import json
import http.client
import re
import ssl
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

from .image_download import ImageConnection
from .tasks import TaskError

SOURCES = {
    'public': {'name': '公开镜像', 'browse': 'https://archive.spacemit.com/image/',
               'download': 'https://archive.spacemit.com/image/'},
    'internal': {'name': '内部镜像', 'browse': 'https://nexus.bianbu.xyz/service/rest/repository/browse/image/',
                 'download': 'https://nexus.bianbu.xyz/repository/image/'},
}
MAX_PAGE = 2 * 1024 ** 2
_CACHE, _LOCK = {}, threading.Lock()


def catalog_path(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or c in '\\?#' for c in value):
        raise TaskError('invalid image catalog path')
    # Paths are decoded once by parse_qs. Reject further encoded separators or
    # traversal instead of allowing a different interpretation at the repository.
    if unquote(value) != value or value.startswith('/') or any(p in ('.', '..', '') for p in value.rstrip('/').split('/') if value):
        raise TaskError('invalid image catalog path')
    if value and value.split('/')[0] not in ('k1', 'k3'):
        raise TaskError('catalog is restricted to K1 and K3 images')
    return value.rstrip('/') + '/' if value else ''


class _Index(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.entries, self.active, self.row, self.cell = [], None, None, None
        self.lines = raw.splitlines()
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        if tag == 'td': self.cell = []
        if tag == 'a':
            self.active = {'href': dict(attrs).get('href', ''), 'text': [], 'row': self.row,
                           'line': self.lines[self.getpos()[0] - 1]}

    def handle_data(self, data):
        if self.active is not None: self.active['text'].append(data)
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.active is not None:
            self.entries.append(self.active); self.active = None
        if tag == 'td' and self.cell is not None:
            if self.row is not None: self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        if tag == 'tr': self.row = None


def _format(name):
    lower = name.lower()
    if lower in ('flash-all.zip', 'flash_all.zip'):
        return ''
    if lower.endswith(('.tar.gz', '.tgz', '.tar', '.zip')): return 'package'
    if lower.endswith(('.img.gz', '.img', '.raw.gz', '.raw')): return 'disk-image'
    if lower.endswith(('.itb', '.ext4', '.bin')): return 'partition-file'
    return ''


def parse_index(source, path, raw):
    spec = SOURCES[source]
    browse = spec['browse'] + quote(path, safe='/')
    entries, seen = [], set()
    for row in _Index(raw).entries[:4096]:
        href = row['href']
        if not isinstance(href, str) or href in ('', '../', './'):
            continue
        target = urlsplit(urljoin(browse, href))
        if target.query or target.fragment or target.username or target.password:
            continue
        directory = target.path.endswith('/')
        base = urlsplit(spec['browse'] if directory else spec['download'])
        if target.scheme != 'https' or target.netloc != base.netloc or not target.path.startswith(base.path):
            continue
        relative = unquote(target.path[len(base.path):])
        try:
            checked = catalog_path(relative)
        except TaskError:
            continue
        relative = checked if directory else checked.rstrip('/')
        name = relative.rstrip('/').rsplit('/', 1)[-1]
        # Show direct children only; ignore parent links, scripts, hidden probes
        # and unrelated repository namespaces.
        if not relative.startswith(path) or '/' in relative[len(path):].rstrip('/') or name.startswith(('.', '_')):
            continue
        kind = 'directory' if directory else _format(name)
        if not kind or relative in seen:
            continue
        seen.add(relative)
        item = {'name': name, 'path': relative, 'kind': kind}
        if not directory:
            item['url'] = spec['download'] + quote(relative, safe='/')
            cells = row['row']
            if cells and len(cells) >= 3:
                item['modified'] = cells[1]
                item['size_label'] = cells[2]
                if re.fullmatch(r'[0-9]+', cells[2]): item['size'] = int(cells[2])
            else:
                tail = re.sub(r'<[^>]+>', '', row['line']).split()
                if len(tail) >= 4:
                    item['modified'], item['size_label'] = ' '.join(tail[-3:-1]), tail[-1]
            # Extension compatibility is not board/layout compatibility. Only a
            # downloaded file's existing preview can authorize its write scope.
            item['validation'] = 'preview-required'
        entries.append(item)
    return sorted(entries, key=lambda e: (e['kind'] != 'directory', e['name'].casefold()))[:1024]


def _fetch(url):
    parsed = urlsplit(url)
    connection = ImageConnection(parsed.hostname, 443, True)
    deadline = time.monotonic() + 15
    try:
        connection.request('GET', parsed.path, headers={'Accept': 'text/html', 'Accept-Encoding': 'identity',
                                                       'User-Agent': 'DeckLab-image-catalog/1'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError()
        chunks, count = [], 0
        while True:
            if time.monotonic() > deadline: raise TimeoutError()
            value = response.read1(min(65536, MAX_PAGE + 1 - count))
            if not value: break
            chunks.append(value); count += len(value)
            if count > MAX_PAGE: raise ValueError()
        return b''.join(chunks).decode('utf-8')
    finally:
        connection.close()


def catalog(source='public', path='', refresh=False):
    if not isinstance(source, str) or source not in SOURCES:
        raise TaskError('unknown image source')
    path = catalog_path(path)
    key = (source, path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < 60:
            return json.loads(json.dumps(cached[1]))
    try:
        entries = parse_index(source, path, _fetch(SOURCES[source]['browse'] + quote(path, safe='/')))
    except ssl.SSLCertVerificationError:
        raise TaskError('镜像源 HTTPS 证书不受信任，请由管理员在控制机配置组织 CA 或更新系统证书', 502) from None
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise TaskError('内部镜像源暂不可访问，请检查控制机内网连接' if source == 'internal'
                        else '公开镜像源暂不可访问，请检查控制机网络', 502) from None
    result = {'source': source, 'source_name': SOURCES[source]['name'], 'path': path,
              'entries': entries, 'checked_at': time.time(),
              'sources': [{'id': k, 'name': v['name']} for k, v in SOURCES.items()]}
    with _LOCK:
        if len(_CACHE) >= 128: _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic(), result)
    return json.loads(json.dumps(result))


def library(manager):
    with manager.store.lock:
        ids = [row[0] for row in manager.store.db.execute(
            "SELECT id FROM transfer_artifacts WHERE serial='' AND direction='upload' AND state='ready' ORDER BY updated DESC LIMIT 100")]
        active = [row[0] for row in manager.store.db.execute(
            "SELECT t.id FROM tasks t JOIN task_targets s ON s.task_id=t.id WHERE t.actor='web' AND t.kind='image-download' "
            "AND s.serial='' AND s.connection_id='image-downloads' AND t.state IN ('queued','running') ORDER BY t.created DESC LIMIT 10")]
    return {'artifacts': [{k: row[k] for k in ('id', 'name', 'size', 'sha256', 'updated')}
                          for row in (manager.artifacts.get(i) for i in ids)],
            'tasks': [manager.tasks.store.get(i) for i in active]}
