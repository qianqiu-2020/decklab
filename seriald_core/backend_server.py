"""Hardware-owning backend for Windows and Ubuntu, served only over authenticated TLS."""
from contextlib import closing
import argparse
import hmac
import os
import signal
import ssl
import threading
from urllib.parse import urlsplit

from . import __version__, web
from .backend_link import server_context, validate_token
from .collector_lock import CollectorLock
from .config import BASE, HANDOFF, LOG_ROOT, SETTINGS, LAUNCH_ENVIRONMENT
from .handoff import consume as consume_handoff, write as write_handoff
from .httpd import BoundedThreadingTCPServer
from .profiles import ProfileBook
from .remote_settings import node_snapshot, validate_scope
from .request_json import load_object
from .settings_restart import configure_collector, reexecute
from .storage import Store
from .web_settings import SettingsError


class BackendHandler(web.Handler):
    def _authorized(self):
        values = self.headers.get_all('Authorization', [])
        expected = ('Bearer ' + self.server.backend_token).encode()
        if len(values) == 1 and hmac.compare_digest(values[0].encode(), expected):
            return True
        self.close_connection = True
        self._json(401, {'error': 'backend authentication required', 'code': 'unauthorized'})
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if urlsplit(self.path).path == '/api/backend-settings':
            return self._settings_request()
        return super().do_GET()

    def do_POST(self):
        if self._authorized():
            return super().do_POST()

    def _do_POST(self):
        if urlsplit(self.path).path == '/api/backend-settings':
            return self._settings_request(write=True)
        return super()._do_POST()

    def _settings_request(self, write=False):
        settings = self.server.host_settings
        try:
            with settings.guard:
                if not write:
                    result = node_snapshot(settings, 'backend')
                    result.update(active_revision=self.server.settings_revision, restart_available=True,
                                  activation={'state': 'restarting' if self.server.restart_requested else 'idle'})
                    return self._json(200, result)
                if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/json':
                    raise SettingsError('需要 application/json 请求')
                if self.headers.get('Transfer-Encoding'):
                    raise SettingsError('transfer encoding unsupported')
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 2 * 1024 ** 2:
                    raise SettingsError('invalid body length')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise SettingsError('incomplete body')
                payload = load_object(raw)
                action = payload.get('action', 'preview')
                if action == 'export':
                    if type(payload.get('include_secrets', False)) is not bool:
                        raise SettingsError('include_secrets 必须为布尔值')
                    return self._json(200, node_snapshot(settings, 'backend', payload.get('include_secrets', False)))
                if action == 'activate':
                    result = self.server.activate_settings(payload.get('revision'))
                    try:
                        self._json(202, result)
                        self.wfile.flush()
                    finally:
                        self.server.activation_reply.set()
                    return
                if action not in ('preview', 'save'):
                    raise SettingsError('未知配置操作')
                incoming = payload.get('configuration')
                validate_scope(settings, 'backend', incoming)
                return self._json(200, settings.save(incoming, payload.get('revision'), preview=action == 'preview'))
        except (ValueError, OSError, UnicodeError) as error:
            return self._json(getattr(error, 'status', 400),
                {'error': str(error) if isinstance(error, SettingsError) else '后端配置校验或读写失败'})


class BackendServer(BoundedThreadingTCPServer):
    def __init__(self, address, context, token):
        self.tls_context, self.backend_token = context, validate_token(token)
        super().__init__(address, BackendHandler)

    def finish_request(self, request, client_address):
        # TLS handshakes occur in bounded workers, never in the accept thread.
        try:
            with self.tls_context.wrap_socket(request, server_side=True) as protected:
                super().finish_request(protected, client_address)
        except (ssl.SSLError, ConnectionError, TimeoutError):
            pass


def manager_type():
    if os.name == 'nt':
        from .windows_manager import WindowsManager
        return WindowsManager
    from .devices import Manager
    return Manager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default=os.environ.get('SERIALD_BACKEND_BIND', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('SERIALD_BACKEND_PORT', '8899')))
    parser.add_argument('--demo', action='store_true', help='simulate boards; never open serial or USB devices')
    args = parser.parse_args()
    environment = SETTINGS.validate(SETTINGS.effective())['environment']
    saved = SETTINGS.read().get('environment', {})
    args.bind = saved.get('BACKEND_BIND', args.bind)
    args.port = int(saved.get('BACKEND_PORT', args.port))
    SETTINGS.environment.update(SERIALD_BACKEND_BIND=args.bind, SERIALD_BACKEND_PORT=str(args.port))
    if environment['BACKEND_URL']:
        parser.error('backend role must not configure a remote backend URL')
    token = validate_token(environment['BACKEND_TOKEN'])
    context = server_context(environment['BACKEND_CERT'], environment['BACKEND_KEY'])
    # Bind before discovering devices. An accidental second backend cannot take
    # hardware ownership from the active process.
    with BackendServer((args.bind, args.port), context, token) as server, CollectorLock(BASE):
        server.backend_address = (args.bind, server.server_address[1])
        Manager = manager_type()
        os.makedirs(LOG_ROOT, exist_ok=True)
        with closing(Store(os.path.join(BASE, 'registry.sqlite3'))) as store:
            store.recover_sessions()
            if args.demo:
                from .demo import DemoManager
                manager = DemoManager(store)
            else:
                manager = Manager(store, consume_handoff(HANDOFF), profiles=ProfileBook.from_env())
            server.manager, server.host_settings = manager, SETTINGS
            configure_collector(server, SETTINGS)
            def preflight(proposed):
                validate_token(proposed['BACKEND_TOKEN'])
                server_context(proposed['BACKEND_CERT'], proposed['BACKEND_KEY'])
                address = (proposed['BACKEND_BIND'], int(proposed['BACKEND_PORT']))
                if address != server.backend_address:
                    import socket
                    with socket.socket() as probe:
                        probe.bind(address)
            server.preflight_settings = preflight
            scanner = threading.Thread(target=manager.stopping.wait if args.demo else manager.loop, daemon=True)
            scanner.start()
            signal.signal(signal.SIGTERM,
                lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
            print('seriald hardware backend on https://%s:%d' % server.server_address, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                connections = manager.close()
                try:
                    write_handoff(HANDOFF, connections)
                except OSError:
                    pass
                scanner.join(timeout=6)
    if server.restart_requested:
        reexecute(LAUNCH_ENVIRONMENT, role='backend')


if __name__ == '__main__':
    main()
