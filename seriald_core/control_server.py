"""Restartable TCP/static control plane that proxies API calls to the collector."""
import argparse
import base64
import binascii
import hmac
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from http.cookies import SimpleCookie
from urllib.parse import urlparse

from .config import AUTH_TOKEN, SOCKET, STATIC
from .httpd import BoundedThreadingTCPServer
from . import __version__


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class ControlHandler(http.server.SimpleHTTPRequestHandler):
    server_version = 'seriald-control/' + __version__
    collector_socket = SOCKET
    static_root = STATIC
    auth_token = AUTH_TOKEN
    agent_tokens = {}
    trust_loopback = False
    share_seconds = 24 * 60 * 60
    allowed_static = frozenset(('/static/app.js', '/static/file-transfer.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js', '/static/terminal-policy.js',
                                '/static/app.css', '/vendor/xterm.js',
                                '/vendor/xterm.css', '/vendor/addon-fit.js'))

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _owner_authorized(self):
        if not self.auth_token:
            return True
        if self.trust_loopback:
            try:
                if ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return True
            except (AttributeError, ValueError, IndexError):
                pass
        value = self.headers.get('Authorization', '')
        supplied = ''
        if value.startswith('Bearer '):
            supplied = value[7:]
        elif value.startswith('Basic '):
            try:
                decoded = base64.b64decode(value[6:], validate=True).decode('utf-8')
                user, supplied = decoded.split(':', 1)
                if user != 'seriald':
                    supplied = ''
            except (binascii.Error, UnicodeDecodeError, ValueError):
                supplied = ''
        return hmac.compare_digest(supplied, self.auth_token)

    def _authorized(self):
        if self._owner_authorized():
            return True
        if self._agent_actor():
            return True
        value = self.headers.get('Authorization', '')
        if value.startswith('Bearer ') and self._valid_share(value[7:]):
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            session = cookie.get('seriald_share')
        except Exception:
            session = None
        return bool(session and self._valid_share(session.value))

    def _agent_actor(self):
        value = self.headers.get('Authorization', '')
        if not self.auth_token or not value.startswith('Bearer '):
            return ''
        token = value[7:]
        for actor, expected in self.agent_tokens.items():
            if hmac.compare_digest(token, expected):
                return actor
        return ''

    def _new_share(self):
        body = '%d.%s' % (int(time.time()) + self.share_seconds, secrets.token_urlsafe(12))
        mac = hmac.new(self.auth_token.encode(), body.encode(), hashlib.sha256).hexdigest()
        return body + '.' + mac

    def _valid_share(self, token):
        if not self.auth_token or len(token) > 160:
            return False
        try:
            expiry, nonce, signature = token.split('.')
            if not expiry.isdigit() or not nonce or not signature or int(expiry) < time.time():
                return False
            body = expiry + '.' + nonce
            expected = hmac.new(self.auth_token.encode(), body.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (AttributeError, ValueError):
            return False

    def _accept_share(self, token):
        if not self._valid_share(token):
            return self._json(403, {'error': '分享链接无效或已过期'})
        self.send_response(303)
        self.send_header('Location', '/')
        self.send_header('Set-Cookie', 'seriald_share=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax' %
                         (token, max(0, int(token.split('.')[0]) - int(time.time()))))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _require_auth(self):
        if self._authorized():
            return False
        body = b'{"error":"authentication required"}'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="seriald", charset="UTF-8"')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return True

    def _proxy(self):
        connection = None
        headers_sent = False
        try:
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            length = int(self.headers.get('Content-Length') or 0)
            if length < 0 or length > 65536:
                return self._json(413, {'error': 'body must be at most 65536 bytes'})
            body = self.rfile.read(length) if length else None
            if length and len(body) != length:
                return self._json(400, {'error': 'incomplete body'})
            agent_actor = self._agent_actor()
            if agent_actor and self.command == 'POST':
                try:
                    payload = json.loads(body.decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError('JSON object required')
                except (UnicodeDecodeError, ValueError, AttributeError):
                    return self._json(400, {'error': 'bad json'})
                payload['actor'] = agent_actor
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                length = len(body)
                if length > 65536:
                    return self._json(413, {'error': 'body must be at most 65536 bytes'})
            headers = {'Content-Type': self.headers.get('Content-Type', 'application/json'),
                       'Content-Length': str(length)} if length else {}
            events = self.command == 'GET' and urlparse(self.path).path == '/api/session-events'
            if events and self.headers.get('Last-Event-ID'):
                headers['Last-Event-ID'] = self.headers['Last-Event-ID']
            # Serial identification may wait for a complete framed response.
            # SSH enable can install packages, while SSH connect may configure
            # root access via the serial console after an auth failure.
            # Keep the control-plane socket open for those operations; a 30s
            # proxy timeout otherwise closes the browser request while the
            # collector is still working, leaving the board identified but the UI
            # showing a spurious "Failed to fetch".
            slow = self.command == 'POST' and urlparse(self.path).path in (
                '/api/identify', '/api/ssh-enable', '/api/ssh-connect',
                '/api/file-transfer/start', '/api/file-transfer/chunk', '/api/file-transfer/batch',
                '/api/file-transfer/finish', '/api/file-transfer/abort')
            timeout = 300 if slow else 30
            connection = (http.client.HTTPConnection(*self.collector_socket, timeout=timeout)
                          if isinstance(self.collector_socket, tuple)
                          else UnixHTTPConnection(self.collector_socket, timeout=timeout))
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name in ('Content-Type', 'Cache-Control', 'Content-Disposition'):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            length = response.getheader('Content-Length')
            if length is not None:
                self.send_header('Content-Length', length)
            if events and response.status == 200:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            headers_sent = True
            if events and response.status == 200:
                # A normal read(65536) waits for a full buffer or EOF; SSE must
                # forward each line immediately, including idle heartbeats.
                while True:
                    line = response.readline(65536)
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                return
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except ValueError:
            if not headers_sent:
                self._json(400, {'error': 'bad content length'})
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            if not headers_sent:
                self._json(503, {'error': 'collector unavailable', 'detail': str(error)})
        finally:
            if connection is not None:
                connection.close()

    def do_GET(self):
        route = urlparse(self.path).path
        if route.startswith('/share/'):
            return self._accept_share(route[len('/share/'):])
        if self._require_auth():
            return
        if route == '/api/access':
            return self._json(200, {'share_enabled': bool(self.auth_token),
                                    'loopback_open': bool(self.trust_loopback)})
        if route == '/healthz':
            return self._json(200, {'ok': True, 'role': 'control', 'pid': os.getpid(),
                                    'version': __version__,
                                    'collector_socket': self.collector_socket})
        if route.startswith('/api/'):
            return self._proxy()
        if route in ('/', '/index.html'):
            self.path = '/index.html'
            return super().do_GET()
        if route in self.allowed_static:
            return super().do_GET()
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        if self._require_auth():
            return
        if urlparse(self.path).path == '/api/share-link':
            if not self.auth_token:
                return self._json(400, {'error': '当前服务未启用密码'})
            if not self._owner_authorized():
                return self._json(403, {'error': '只有主访问凭证可以创建分享链接'})
            return self._json(200, {'path': '/share/' + self._new_share(),
                                    'expires_in': self.share_seconds})
        if urlparse(self.path).path.startswith('/api/'):
            return self._proxy()
        return self._json(404, {'error': 'not found'})

    def translate_path(self, path):
        route = urlparse(path).path.lstrip('/')
        return os.path.join(self.static_root, *route.split('/'))

    def end_headers(self):
        if urlparse(self.path).path in ('/', '', '/index.html', '/static/app.js', '/static/file-transfer.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js',
                                        '/static/terminal-policy.js', '/static/app.css'):
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', nargs='?', type=int, default=int(os.environ.get('SERIALD_PORT', '8898')))
    parser.add_argument('bind', nargs='?', default=os.environ.get('SERIALD_BIND', '127.0.0.1'))
    args = parser.parse_args()
    ControlHandler.collector_socket = SOCKET
    ControlHandler.static_root = STATIC
    ControlHandler.auth_token = AUTH_TOKEN
    tokens_path = os.environ.get('SERIALD_AGENT_TOKENS_FILE', '')
    if tokens_path:
        with open(tokens_path, encoding='utf-8') as source:
            tokens = json.load(source)
        if (not AUTH_TOKEN or not isinstance(tokens, dict)
                or any(not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}', actor)
                       or actor == 'web'
                       or not isinstance(token, str) or len(token) < 32 or token == AUTH_TOKEN
                       for actor, token in tokens.items())
                or len(set(tokens.values())) != len(tokens)):
            parser.error('invalid SERIALD_AGENT_TOKENS_FILE or missing SERIALD_AUTH_TOKEN')
        ControlHandler.agent_tokens = tokens
    with BoundedThreadingTCPServer((args.bind, args.port), ControlHandler) as server:
        print('seriald control on http://%s:%d/' % (args.bind, args.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
