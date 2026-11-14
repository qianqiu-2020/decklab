"""Backend-owned, bounded HTTP(S) image imports into the existing artifact spool."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import unquote, urljoin, urlsplit

from .results import response
from .tasks import TaskError, TaskCancelled


def image_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise TaskError('invalid image URL')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or '\\' in value):
            raise ValueError()
        parsed.hostname.encode('idna')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise TaskError('image URL must be HTTP(S), without credentials or fragment') from None
    return parsed


def _addresses(host, port):
    # Private LAN repositories are supported. Reject host-local and metadata
    # addresses; resolve once and connect to that exact address to prevent rebinding.
    output = []
    for family, kind, protocol, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(address[0].split('%')[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise TaskError('image URL resolves to a host-local or reserved address')
        output.append((family, kind, protocol, address))
    if not output:
        raise TaskError('image URL has no reachable address', 502)
    return output


class ImageConnection(http.client.HTTPConnection):
    def __init__(self, host, port, secure):
        super().__init__(host, port, timeout=15)
        self.secure = secure

    def connect(self):
        entries = _addresses(self.host, self.port)
        last = None
        for family, kind, protocol, address in entries:
            stream = socket.socket(family, kind, protocol)
            stream.settimeout(self.timeout)
            try:
                stream.connect(address)
                self.sock = (ssl.create_default_context().wrap_socket(stream, server_hostname=self.host)
                             if self.secure else stream)
                return
            except OSError as error:
                stream.close()
                last = error
        raise last


def fetch_image(artifacts, context, url, serial, name=None, sha256=None):
    """No retries: partial imports fail; a repeated request finds its original task."""
    original = image_url(url)
    if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256)):
        raise TaskError('invalid expected image SHA-256')
    filename = name or unquote(original.path.rsplit('/', 1)[-1]) or 'image.bin'
    artifact = None
    deadline = time.monotonic() + 3600
    try:
        for redirects in range(6):
            context.check()
            parsed = image_url(url)
            connection = ImageConnection(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                         parsed.scheme == 'https')
            context.on_cancel(connection.close)
            try:
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                connection.request('GET', path, headers={'Accept-Encoding': 'identity', 'User-Agent': 'DeckLab-image-import/1'})
                incoming = connection.getresponse()
                if incoming.status in (301, 302, 303, 307, 308):
                    if redirects == 5 or not incoming.getheader('Location'):
                        raise TaskError('image redirect limit exceeded', 502)
                    following = urljoin(url, incoming.getheader('Location'))
                    if parsed.scheme == 'https' and image_url(following).scheme != 'https':
                        raise TaskError('HTTPS image redirect must remain HTTPS', 502)
                    url = following
                    continue
                if incoming.status != 200:
                    raise TaskError('image server returned HTTP %d' % incoming.status, 502)
                if incoming.getheader('Content-Encoding', 'identity').lower() != 'identity':
                    raise TaskError('image server must provide original file bytes', 502)
                lengths = incoming.headers.get_all('Content-Length', [])
                if len(lengths) > 1 or lengths and not re.fullmatch('[0-9]+', lengths[0]):
                    raise TaskError('invalid image Content-Length', 502)
                length = int(lengths[0]) if lengths else None
                if length is not None and not 0 < length <= artifacts.policy.max_file:
                    raise TaskError('image exceeds the configured file size limit', 413)
                limit = length if length is not None else artifacts.policy.max_file
                artifact = artifacts.create('upload', filename, limit, 'web', serial, context.id)
                artifacts._update(artifact['id'], state='writing')
                received = 0
                context.progress(0, length or 0, 'image-download')
                while True:
                    context.check()
                    if time.monotonic() > deadline:
                        raise TaskError('image download exceeded one hour', 504)
                    chunk = incoming.read(min(1024 ** 2, limit - received + 1))
                    if not chunk:
                        break
                    if received + len(chunk) > limit:
                        raise TaskError('image exceeds the reserved size', 413)
                    artifacts.append(artifact['id'], received, chunk, task_id=context.id)
                    received += len(chunk)
                    context.progress(received, length or 0, 'image-download')
                if length is not None and received != length:
                    raise TaskError('image download was truncated', 502)
                context.progress(received, received, 'image-ready')
                result = artifacts.finish_import(artifact['id'], context.id, sha256, context.check)
                return {'artifact': {key: result[key] for key in ('id', 'name', 'size', 'sha256', 'serial', 'state')}}
            finally:
                connection.close()
    except BaseException as error:
        if artifact is not None:
            artifacts.fail(artifact['id'], context.id)
        if context.cancelled.is_set():
            raise TaskCancelled('image download cancelled') from None
        if isinstance(error, TaskError):
            raise
        # HTTP errors can include signed URLs or server-provided response text.
        raise TaskError('image download failed; check backend connectivity and TLS trust', 502) from None


class ImageDownloads:
    def __init__(self, manager):
        self.manager = manager

    def start(self, payload, device=None, actor='web'):
        try:
            if actor != 'web':
                raise TaskError('image URL imports require a human operator', 403)
            if not isinstance(payload, dict) or set(payload) - {
                    'url', 'name', 'sha256', 'request_id', 'client_id', 'actor', 'key', 'serial',
                    'target_id', 'target_generation', 'connection_id', 'task_id'}:
                raise TaskError('invalid image download request')
            image_url(payload.get('url'))
            if payload.get('task_id'):
                from .flash_tasks import FlashActions
                serial = FlashActions(self.manager).waiting_payload_serial(payload, actor)
            else:
                if payload.get('target_id'):
                    from .flash_tasks import FlashActions
                    device = FlashActions(self.manager)._device(None, payload, actor)
                if device is None and any(payload.get(key) for key in ('key', 'serial', 'connection_id')):
                    raise TaskError('image download target is required')
                serial = (device.serial or '') if device is not None else ''
            checksum = payload.get('sha256') or None
            if checksum is not None and (not isinstance(checksum, str) or not re.fullmatch('[a-f0-9]{64}', checksum)):
                raise TaskError('invalid expected image SHA-256')
            name = payload.get('name')
            if name is not None and (not isinstance(name, str) or not name or len(name.encode()) > 255
                                     or any(ord(c) < 32 or c in '/\\' for c in name)):
                raise TaskError('invalid image filename')
            digest = hashlib.sha256(json.dumps({'kind': 'image-download', 'url': payload['url'],
                'name': name, 'sha256': checksum, 'serial': serial}, sort_keys=True).encode()).hexdigest()
            from .flash_store import validate_request
            validate_request(payload.get('request_id'), digest)
            task = self.manager.tasks.start('image-download', actor, payload.get('client_id', ''),
                [{'serial': serial, 'connection_id': 'image-downloads'}],
                lambda context: fetch_image(self.manager.artifacts, context, payload['url'], serial, name, checksum),
                request_id=payload.get('request_id'), input_digest=digest)
            return response(202, {'ok': True, 'task': task})
        except (TaskError, ValueError, OSError) as error:
            return response(getattr(error, 'status', 503), {'ok': False, 'error': str(error)})
