"""Restartable TCP/static control plane that proxies API calls to the collector."""
import argparse
import base64
import binascii
import hmac
import http.client
import http.server
import json
import os
import socket
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
    allowed_static = frozenset(('/static/app.js', '/static/file-transfer.js', '/static/boot-compare.js', '/static/ui-common.js', '/static/terminal-policy.js',
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

    def _authorized(self):
        if not self.auth_token:
            return True
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
        if self._require_auth():
            return
        route = urlparse(self.path).path
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
        if urlparse(self.path).path.startswith('/api/'):
            return self._proxy()
        return self._json(404, {'error': 'not found'})

    def translate_path(self, path):
        route = urlparse(path).path.lstrip('/')
        return os.path.join(self.static_root, *route.split('/'))

    def end_headers(self):
        if urlparse(self.path).path in ('/', '', '/index.html', '/static/app.js', '/static/file-transfer.js', '/static/boot-compare.js', '/static/ui-common.js',
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
    with BoundedThreadingTCPServer((args.bind, args.port), ControlHandler) as server:
        print('seriald control on http://%s:%d/' % (args.bind, args.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
