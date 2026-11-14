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
import signal
import re
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import AUTH_TOKEN, BASE, SOCKET, STATIC, SETTINGS, LAUNCH_ENVIRONMENT, SHARE_SECONDS, read_ui_language
from .web_settings import SettingsError
from .settings_restart import configure_control, reexecute
from .agent_grants import AgentGrantStore
from .artifact_actions import body_limit
from .httpd import BoundedThreadingTCPServer
from . import __version__
from .results import response as api_response
from .request_json import load_object
from .mcp_host import MCPHost, local_control_url
from .backend_link import BackendLink


def configured_agent_tokens(path, primary_token):
    if not path:
        return {}
    with open(path, 'rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('agent token configuration exceeds 64 KiB')
    tokens = load_object(raw)
    if (tokens and not primary_token or any(
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}', actor) or actor == 'web'
            or not isinstance(token, str) or len(token) < 32 or token == primary_token
            for actor, token in tokens.items()) or len(set(tokens.values())) != len(tokens)):
        raise ValueError('invalid agent token configuration')
    return tokens


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def lan_hosts():
    """Suggest reachable IPv4 addresses without depending on platform commands."""
    hosts = set()
    try:
        hosts.update(item[4][0] for item in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM))
    except OSError:
        pass
    for target in ('10.0.0.1', '172.16.0.1', '192.168.0.1'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 80))
                hosts.add(probe.getsockname()[0])
        except OSError:
            pass
    def is_lan(host):
        address = ipaddress.ip_address(host)
        return any(address in network for network in (
            ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16')))
    return sorted((host for host in hosts if is_lan(host)),
                  key=lambda host: (not host.startswith('10.'), host))


def _mcp_authority(value):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value) or value.endswith(':'):
        raise ValueError('invalid public MCP authority')
    parsed = urlparse('//' + value)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('invalid public MCP authority')
    host, port = parsed.hostname.lower(), parsed.port
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
            raise ValueError('invalid public MCP hostname')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid public MCP port')
    return host, port


def public_mcp_host_allowed(server, authority):
    """Keep SDK DNS-rebinding protection at the public reverse-proxy boundary."""
    if server is None:
        return False
    try:
        requested = _mcp_authority(authority)
    except ValueError:
        return False
    allowed = getattr(server, '_mcp_public_hosts', None)
    if allowed is None:
        address = getattr(server, 'server_address', ('127.0.0.1', 8898))
        bind, port = address[:2]
        names = {'localhost', '127.0.0.1', '::1', socket.gethostname().lower(), socket.getfqdn().lower()}
        try:
            names.update(item[4][0].lower() for item in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_UNSPEC, socket.SOCK_STREAM))
        except OSError:
            pass
        if bind not in ('0.0.0.0', '::', ''):
            names.add(bind.lower())
        names.update(lan_hosts())
        allowed = {(name, port) for name in names if name}
        for value in os.environ.get('SERIALD_MCP_ALLOWED_HOSTS', '').split(','):
            value = value.strip()
            if not value:
                continue
            try:
                if value.endswith(':*'):
                    host, specified = _mcp_authority(value[:-2])
                    if specified is not None:
                        continue
                    allowed.add((host, '*'))
                else:
                    host, specified = _mcp_authority(value)
                    allowed.add((host, specified))
                    if specified in (80, 443):
                        allowed.add((host, None))
            except ValueError:
                continue  # Invalid configured names never broaden the policy.
        if port in (80, 443):
            allowed.update((name, None) for name in names if name)
        server._mcp_public_hosts = frozenset(allowed)
    return requested in allowed or (requested[0], '*') in allowed


class ControlHandler(http.server.SimpleHTTPRequestHandler):
    server_version = 'seriald-control/' + __version__
    collector_socket = SOCKET
    collector_link = None
    static_root = STATIC
    auth_token = AUTH_TOKEN
    agent_tokens = {}
    grant_store = None
    trust_loopback = False
    share_seconds = SHARE_SECONDS
    default_language = ''
    settings = None
    allowed_static = frozenset(('/static/i18n.js', '/static/app.js', '/static/settings.js', '/static/file-transfer.js', '/static/flashing.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js', '/static/terminal-policy.js', '/static/serial-progress.js', '/static/terminal-navigation.js', '/static/terminal-view.js', '/static/archives.js', '/static/devices.js', '/static/tasks.js', '/static/board-control.js',
                                '/static/app.css', '/static/decklab.svg', '/static/decklab.png', '/static/icons.svg', '/static/icons-LICENSE.txt', '/vendor/xterm.js',
                                '/vendor/xterm.css', '/vendor/addon-fit.js'))
    public_agent_files = frozenset(('/serialctl.py', '/seriald_mcp.py', '/requirements-mcp.txt',
                                   '/pyproject.toml', '/skills/seriald-board/SKILL.md'))
    agent_denied_posts = frozenset(('/api/label', '/api/switch-mode', '/api/rescue',
                                    '/api/ssh-enable', '/api/session-takeover', '/api/probe-settings', '/api/flash-recovery'))

    def guess_type(self, path):
        route = urlparse(self.path).path
        if route == '/skills/seriald-board/SKILL.md':
            return 'text/markdown; charset=utf-8'
        if route in ('/serialctl.py', '/seriald_mcp.py'):
            return 'text/x-python; charset=utf-8'
        if route in ('/requirements-mcp.txt', '/pyproject.toml'):
            return 'text/plain; charset=utf-8'
        return super().guess_type(path)

    def _json(self, code, payload):
        payload = api_response(code, payload).payload
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _owner_authorized(self):
        value = self.headers.get('Authorization', '')
        if value.startswith('Bearer seriald-ag-'):
            return False
        if not self.auth_token:
            return True
        if self.trust_loopback and not value:
            try:
                if ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return True
            except (AttributeError, ValueError, IndexError):
                pass
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
        return hmac.compare_digest(supplied.encode('utf-8'), self.auth_token.encode('utf-8'))

    def _authorized(self):
        if self.headers.get('Authorization', '').startswith('Bearer seriald-ag-'):
            return bool(self._agent_grant())
        if self._owner_authorized():
            return True
        if self._agent_actor():
            return True
        if self._agent_grant():
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

    def _agent_share_authorized(self):
        """Human browser collaborators may delegate board access to agents."""
        if self.headers.get('Authorization', '').startswith('Bearer seriald-ag-'):
            return False
        if self._owner_authorized():
            return True
        value = self.headers.get('Authorization', '')
        if value.startswith('Bearer '):
            return self._valid_share(value[7:])
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
            if hmac.compare_digest(token.encode('utf-8'), expected.encode('utf-8')):
                return actor
        return ''

    def _agent_grant(self):
        authorization = self.headers.get('Authorization', '')
        if not self.grant_store or not authorization.startswith('Bearer seriald-ag-'):
            return None
        return self.grant_store.lookup(authorization[7:])

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
        body = b'{"error":"authentication required","code":"unauthorized"}'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="seriald", charset="UTF-8"')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
        return True

    def _small_json_body(self, limit=4096):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('transfer encoding unsupported')
        length = int(self.headers.get('Content-Length') or 0)
        if length < 0 or length > limit:
            raise ValueError('request body exceeds limit')
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError('incomplete body')
        return load_object(body)

    def _settings_request(self, write=False):
        if not self._owner_authorized():
            return self._json(403, {'error': '只有主访问凭证可以管理系统配置'})
        if dict(parse_qsl(urlparse(self.path).query)).get('target') == 'backend':
            if self.collector_link is None:
                return self._json(409, {'error': '当前没有配置远程后端'})
            return self._proxy('/api/backend-settings')
        if self.settings is None:
            return self._json(503, {'error': '当前运行方式未启用系统配置'})
        with self.settings.guard:
            return self._settings_request_locked(write)

    def _settings_request_locked(self, write=False):
        try:
            if not write:
                if self.collector_link:
                    from .remote_settings import node_snapshot
                    result = node_snapshot(self.settings, 'web')
                    result['remote_backend'] = True
                else:
                    result = self.settings.snapshot()
                result['active_revision'] = getattr(self.server, 'settings_revision', None)
                result['restart_available'] = callable(getattr(self.server, 'activate_settings', None))
                result['activation'] = getattr(self.server, 'settings_activation', {'state': 'idle'})
                return self._json(200, result)
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise SettingsError('需要 application/json 请求')
            payload = self._small_json_body(2 * 1024 ** 2)
            action = payload.get('action', 'preview')
            if (getattr(self.server, 'settings_activation', {}).get('state') == 'applying'
                    and action != 'export'):
                raise SettingsError('正在应用配置，请稍候', 409)
            if action == 'export':
                if type(payload.get('include_secrets', False)) is not bool:
                    raise SettingsError('include_secrets 必须为布尔值')
                if self.collector_link:
                    from .remote_settings import node_snapshot
                    return self._json(200, node_snapshot(self.settings, 'web', payload.get('include_secrets', False)))
                return self._json(200, self.settings.snapshot(payload.get('include_secrets', False)))
            if action == 'activate':
                if payload.get('revision') != self.settings.snapshot()['revision']:
                    raise SettingsError('配置已改变，请重新加载', 409)
                activate = getattr(self.server, 'activate_settings', None)
                if not callable(activate):
                    raise SettingsError('当前运行方式需要重启服务后生效', 409)
                result = activate(payload['revision'])
                try:
                    self._json(202, result)
                    self.wfile.flush()
                finally:
                    reply = getattr(self.server, 'activation_reply', None)
                    if reply is not None:
                        reply.set()
                return
            if action not in ('preview', 'save'):
                raise SettingsError('未知配置操作')
            if self.collector_link:
                from .remote_settings import validate_scope
                validate_scope(self.settings, 'web', payload.get('configuration'))
            return self._json(200, self.settings.save(payload.get('configuration'),
                              payload.get('revision'), preview=action == 'preview'))
        except (ValueError, OSError, UnicodeError) as error:
            # Validators may contain user-supplied strings. Keep passwords and
            # helper arguments out of diagnostics at this owner API boundary.
            message = str(error) if isinstance(error, SettingsError) else '配置校验或读写失败，请检查字段和配置文档'
            return self._json(getattr(error, 'status', 400), {'error': message})

    def _collector_connection(self, timeout=30):
        if self.collector_link is not None:
            return self.collector_link.connection(timeout)
        return (http.client.HTTPConnection(*self.collector_socket, timeout=timeout)
                if isinstance(self.collector_socket, tuple)
                else UnixHTTPConnection(self.collector_socket, timeout=timeout))

    def _close_revoked_sessions(self, actor):
        body = json.dumps({'actor': actor}).encode()
        try:
            connection = self._collector_connection(5)
            try:
                connection.request('POST', '/api/agent-revoke', body=body,
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                response.read()
                return response.status == 200
            finally:
                connection.close()
        except (OSError, http.client.HTTPException):
            # The grant is already revoked; collector restart also closes sessions.
            return False

    def _live_connection(self, key, connection_id):
        """Validate the human-selected attachment without opening a serial device."""
        connection = self._collector_connection(5)
        try:
            connection.request('GET', '/api/devices')
            result = connection.getresponse()
            data = result.read(4 * 1024 * 1024 + 1)
            if result.status != 200 or len(data) > 4 * 1024 * 1024:
                raise ValueError('collector device list unavailable')
            payload = json.loads(data)
            if not isinstance(payload, dict) or not isinstance(payload.get('devices'), list):
                raise ValueError('collector returned invalid device list')
            return any(item.get('key') == key and item.get('connection_id') == connection_id
                       and item.get('alive') is True for item in payload['devices'] if isinstance(item, dict))
        finally:
            connection.close()

    def _mcp_status(self):
        host = getattr(getattr(self, 'server', None), 'mcp_host', None)
        if host is None:
            return {'mcp_enabled': False, 'mcp_url': '/mcp', 'mcp_error': '服务端 MCP 尚未启用'}
        return host.snapshot()

    def _mcp_proxy(self):
        """Forward MCP unchanged; the ordinary API actor rewrite is inapplicable.

        Even open-access and trusted-loopback hosts require an explicit current
        agent grant here. The worker and every tool API call validate that same
        credential again; no browser cookie or owner authority is forwarded.
        """
        if not self._agent_grant():
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Bearer')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            body = b'{"error":"a current agent grant is required","code":"unauthorized"}'
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        parsed = urlparse(self.path)
        if parsed.query or parsed.fragment:
            return self._json(400, {'error': 'MCP credentials belong in the Authorization header'})
        server = getattr(self, 'server', None)
        if not public_mcp_host_allowed(server, self.headers.get('Host', '')):
            return self._json(403, {'error': 'MCP Host is not an allowed public service address'})
        origin = self.headers.get('Origin')
        if origin:
            try:
                source = urlparse(origin)
            except ValueError:
                return self._json(403, {'error': 'invalid MCP Origin'})
            if (source.scheme not in ('http', 'https') or source.username or source.password
                    or source.netloc.lower() != self.headers.get('Host', '').lower()
                    or source.path not in ('', '/') or source.query or source.fragment):
                return self._json(403, {'error': 'MCP Origin does not match the public service'})
        host = getattr(getattr(self, 'server', None), 'mcp_host', None)
        target = host.target if host is not None else None
        if not target:
            return self._json(503, {'error': self._mcp_status()['mcp_error'], 'code': 'unavailable'})
        connection = None
        headers_sent = False
        slots = getattr(host, 'requests', None)
        if slots is not None and not slots.acquire(blocking=False):
            return self._json(429, {'error': 'too many concurrent MCP requests', 'code': 'busy'})
        try:
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            length = int(self.headers.get('Content-Length') or 0)
            if length < 0 or length > 65536:
                return self._json(413, {'error': 'MCP request body must be at most 65536 bytes'})
            body = self.rfile.read(length) if length else None
            if length and len(body) != length:
                return self._json(400, {'error': 'incomplete body'})
            headers = {name: self.headers[name] for name in (
                'Authorization', 'Content-Type', 'Accept', 'MCP-Protocol-Version',
                'Mcp-Session-Id', 'Last-Event-ID') if self.headers.get(name) is not None}
            if length:
                headers['Content-Length'] = str(length)
            connection = http.client.HTTPConnection(*target, timeout=3620)
            connection.request(self.command, '/mcp', body=body, headers=headers)
            result = connection.getresponse()
            # This worker uses stateless JSON responses, never an unbounded SSE
            # stream. Read before sending headers so oversized responses fail
            # atomically instead of exposing an apparently successful prefix.
            data = result.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                return self._json(502, {'error': 'MCP response exceeded the service limit'})
            self.send_response(result.status)
            for name in ('Content-Type', 'WWW-Authenticate', 'Allow', 'Mcp-Session-Id',
                         'MCP-Protocol-Version'):
                value = result.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            headers_sent = True
            self.wfile.write(data)
        except ValueError:
            return self._json(400, {'error': 'bad content length'})
        except (OSError, http.client.HTTPException, TimeoutError):
            if not headers_sent:
                return self._json(503, {'error': 'MCP worker unavailable', 'code': 'unavailable'})
        finally:
            if connection is not None:
                connection.close()
            if slots is not None:
                slots.release()

    def _proxy(self, target_override=None):
        connection = None
        headers_sent = False
        try:
            route = urlparse(self.path).path
            if route in ('/api/backend-settings', '/api/settings-health', '/api/settings-activate'):
                return self._json(404, {'error': 'not found'})
            grant = self._agent_grant()
            if self.headers.get('Authorization', '').startswith('Bearer seriald-ag-') and grant is None:
                return self._json(401, {'error': 'Agent 授权已撤销或过期', 'code': 'unauthorized'})
            agent_actor = grant['actor'] if grant else self._agent_actor()
            if agent_actor and route == '/api/image-download':
                return self._json(403, {'error': '镜像 URL 下载需要网页操作者发起'})
            if agent_actor and route in ('/api/flash-discover', '/api/flash-target'):
                return self._json(403, {'error': '只有网页操作者可以发现和选择新的刷机设备'})
            if agent_actor and route == '/api/probe-settings':
                return self._json(403, {'error': '只有网页操作者可以管理探测命令'})
            if grant and self.command == 'POST' and route in self.agent_denied_posts:
                return self._json(403, {'error': 'Agent 分享令牌不能执行平台管理操作'})
            if grant and grant['scope'] != 'all' and route in ('/api/health', '/api/diagnostics'):
                return self._json(403, {'error': '此令牌仅允许访问指定设备'})
            if grant and grant['scope'] == 'connection' and route == '/api/boot-compare':
                return self._json(403, {'error': '当前连接授权不允许比较其他设备的启动档案'})
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            length = int(self.headers.get('Content-Length') or 0)
            limit = body_limit(route)
            if length < 0 or length > limit:
                return self._json(413, {'error': 'body must be at most %d bytes' % limit})
            body = self.rfile.read(length) if length else None
            if length and len(body) != length:
                return self._json(400, {'error': 'incomplete body'})
            target_path = target_override or self.path
            if self.command == 'POST' and agent_actor:
                try:
                    payload = load_object(body or b'')
                except (UnicodeDecodeError, ValueError, AttributeError):
                    return self._json(400, {'error': 'bad json'})
                if ('association' in payload and (route == '/api/identify'
                        or route == '/api/task-start')):
                    return self._json(403, {'error': 'Agent 令牌不能确认关联历史档案'})
                if payload.get('target_id'):
                    return self._json(403, {'error': '自动发现的刷机目标只允许网页操作者访问'})
                if grant and grant['scope'] == 'device':
                    if not payload.get('key'):
                        return self._json(403, {'error': '此令牌需要指定设备 key'})
                    payload['serial'] = grant['serial']
                if grant and grant['scope'] == 'connection':
                    if route in ('/api/flash-plan', '/api/flash-selection') or (route == '/api/task-start' and payload.get('kind') in ('flash', 'nor-backup')):
                        return self._json(403, {'error': '刷机需要已登记板卡范围的授权'})
                    if not payload.get('key') and route not in ('/api/task-cancel',
                            '/api/artifact-append', '/api/artifact-seal', '/api/artifact-delete'):
                        return self._json(403, {'error': '此令牌需要指定设备 key'})
                    payload.pop('serial', None)
                    payload['connection_id'] = grant['connection_id']
                if (grant and grant['scope'] != 'all' and route == '/api/task-start'
                        and payload.get('kind') == 'file-copy'):
                    return self._json(403, {'error': '板间复制需要所有设备范围的令牌'})
                if grant and route == '/api/identify' and payload.get('init_boot_marker'):
                    return self._json(403, {'error': 'Agent 分享令牌不能创建板端身份标记'})
                if grant and route == '/api/task-start' and (
                        payload.get('kind') == 'ssh-enable' or payload.get('init_boot_marker')):
                    return self._json(403, {'error': 'Agent 分享令牌不能执行此平台管理任务'})
                payload['actor'] = agent_actor
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                length = len(body)
                if length > limit:
                    return self._json(413, {'error': 'body must be at most %d bytes' % limit})
            elif grant and grant['scope'] != 'all' and route not in ('/api/devices', '/api/capabilities'):
                parsed = urlparse(self.path)
                query = parse_qsl(parsed.query, keep_blank_values=True)
                if route == '/api/boot-compare':
                    left = [value for name, value in query if name == 'left_serial']
                    right = [value for name, value in query if name == 'right_serial']
                    if left != [grant['serial']] or right != [grant['serial']]:
                        return self._json(403, {'error': '此令牌只能比较同一授权设备的启动日志'})
                else:
                    if (not any(name == 'key' and value for name, value in query)
                            and not (grant['scope'] == 'connection' and route in (
                                '/api/tasks', '/api/task', '/api/artifact', '/api/artifact-download'))):
                        return self._json(403, {'error': '此令牌需要指定设备 key'})
                    query = [(name, value) for name, value in query if name not in ('serial', 'connection_id')]
                    if grant['scope'] == 'device':
                        query.append(('serial', grant['serial']))
                    else:
                        query.append(('connection_id', grant['connection_id']))
                    target_path = urlunparse(parsed._replace(query=urlencode(query)))
            headers = {'Content-Type': self.headers.get('Content-Type', 'application/json'),
                       'Content-Length': str(length)} if length else {}
            if agent_actor or route in ('/api/tasks', '/api/task', '/api/artifact', '/api/artifact-download', '/api/hardware',
                                       '/api/flash-capabilities', '/api/flash-events'):
                headers['X-Seriald-Actor'] = agent_actor or 'web'
            if grant and grant['scope'] == 'connection':
                headers['X-Seriald-Connection'] = grant['connection_id']
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
                '/api/file-transfer/finish', '/api/file-transfer/abort', '/api/artifact-seal', '/api/flash-plan',
                '/api/flash-selection', '/api/flash-target')
            timeout = 300 if slow else 30
            connection = self._collector_connection(timeout)
            connection.request(self.command, target_path, body=body, headers=headers)
            response = connection.getresponse()
            if self.collector_link and response.status == 401:
                return self._json(503, {'error': '后端节点认证失败，请检查节点间访问密钥', 'code': 'backend-unavailable'})
            if grant and grant['scope'] != 'all' and route == '/api/devices' and response.status == 200:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    return self._json(413, {'error': 'device list too large'})
                try:
                    devices_payload = json.loads(data)
                except ValueError:
                    return self._json(502, {'error': 'collector returned invalid device list'})
                devices_payload['devices'] = [item for item in devices_payload.get('devices', [])
                    if (item.get('serial') == grant['serial'] if grant['scope'] == 'device' else
                        item.get('connection_id') == grant['connection_id'] and item.get('alive') is True)]
                # Connection grants reveal their current attachment, not unrelated host storage/path data.
                if grant['scope'] == 'connection':
                    devices_payload = {'devices': devices_payload['devices'],
                                       'server_time': devices_payload.get('server_time')}
                return self._json(200, devices_payload)
            self.send_response(response.status)
            for name in ('Content-Type', 'Cache-Control', 'Content-Disposition', 'X-Checksum-SHA256'):
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
                    if grant and not self._agent_grant():
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                return
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                if route == '/api/artifact-download' and grant and not self._agent_grant():
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
        except ValueError:
            if not headers_sent:
                self._json(400, {'error': 'bad content length'})
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            if not headers_sent:
                self._json(503, {'error': 'collector unavailable', 'detail': 'backend connection failed' if self.collector_link else str(error)})
        finally:
            if connection is not None:
                connection.close()

    def do_GET(self):
        route = urlparse(self.path).path
        if route == '/mcp':
            return self._mcp_proxy()
        if route.startswith('/share/'):
            return self._accept_share(route[len('/share/'):])
        if route in self.public_agent_files:
            return super().do_GET()
        if self._require_auth():
            return
        if route == '/api/settings':
            return self._settings_request()
        if route == '/api/device-preferences':
            if not self._owner_authorized():
                return self._json(403, {'error': '只有主访问凭证可以管理设备偏好'})
            return self._proxy()
        if route == '/api/settings-health':
            return self._json(404, {'error': 'not found'})
        if route == '/api/access':
            return self._json(200, {'share_enabled': bool(self.auth_token and self._owner_authorized()),
                                    'settings_enabled': bool(self.settings and self._owner_authorized()),
                                    'agent_share_enabled': bool(self._agent_share_authorized() and self.grant_store),
                                    'open_access': not bool(self.auth_token),
                                    'loopback_open': bool(self.trust_loopback),
                                    'default_language': self.default_language,
                                    'lan_hosts': lan_hosts(), **self._mcp_status()})
        if route == '/api/agent-grants':
            if not self._agent_share_authorized() or not self.grant_store:
                return self._json(403, {'error': '需要有效的网页访问凭证才能管理 Agent 授权'})
            return self._json(200, {'grants': self.grant_store.list()})
        if route == '/healthz':
            return self._json(200, {'ok': True, 'role': 'control', 'pid': os.getpid(),
                                    'version': __version__,
                                    'collector_socket': self.collector_socket if self.collector_link is None else None,
                                    'backend_transport': 'https' if self.collector_link else 'local'})
        if route.startswith('/api/'):
            return self._proxy()
        if route in ('/', '/index.html'):
            self.path = '/index.html'
            return super().do_GET()
        if route in self.allowed_static:
            return super().do_GET()
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        if urlparse(self.path).path == '/mcp':
            return self._mcp_proxy()
        if self._require_auth():
            return
        route = urlparse(self.path).path
        if route == '/api/settings':
            return self._settings_request(write=True)
        if route == '/api/device-preferences':
            if not self._owner_authorized():
                return self._json(403, {'error': '只有主访问凭证可以管理设备偏好'})
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self._json(400, {'error': '需要 application/json 请求'})
            return self._proxy()
        if route == '/api/settings-activate':
            return self._json(404, {'error': 'not found'})
        if route in ('/api/agent-share', '/api/agent-revoke'):
            if not self._agent_share_authorized() or not self.grant_store:
                return self._json(403, {'error': '需要有效的网页访问凭证才能管理 Agent 授权'})
            try:
                payload = self._small_json_body()
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {'error': '无效的授权请求'})
            if route == '/api/agent-share':
                scope, serial = payload.get('scope'), payload.get('serial', '')
                if scope not in ('all', 'device', 'connection') or not isinstance(serial, str) or len(serial) > 128 \
                        or (scope == 'device' and not serial):
                    return self._json(400, {'error': '请选择全部设备、设备序列号或当前连接'})
                key, connection_id = payload.get('key', ''), payload.get('connection_id', '')
                if scope == 'connection':
                    if (not isinstance(key, str) or not key or len(key) > 256
                            or not isinstance(connection_id, str) or not connection_id or len(connection_id) > 80):
                        return self._json(400, {'error': '当前连接需要有效的 key 和 connection_id'})
                    try:
                        if not self._live_connection(key, connection_id):
                            return self._json(409, {'error': '设备连接已变化或未在线，请刷新后重新授权'})
                    except (OSError, http.client.HTTPException, ValueError):
                        return self._json(503, {'error': '无法核对当前设备连接'})
                token, grant = self.grant_store.issue(scope, serial, key=key, connection_id=connection_id)
                return self._json(200, {'token': token, 'grant': grant})
            grant_id = payload.get('id')
            if not isinstance(grant_id, str) or not re.fullmatch(r'[0-9a-f]{16}', grant_id):
                return self._json(400, {'error': '无效的授权 ID'})
            actor = self.grant_store.revoke(grant_id)
            if not actor:
                return self._json(404, {'error': '授权不存在或已撤销'})
            sessions_closed = self._close_revoked_sessions(actor)
            return self._json(200, {'ok': True, 'id': grant_id,
                                    'sessions_closed': sessions_closed})
        if route == '/api/share-link':
            if not self.auth_token:
                return self._json(400, {'error': '当前服务未启用密码'})
            if not self._owner_authorized():
                return self._json(403, {'error': '只有主访问凭证可以创建分享链接'})
            return self._json(200, {'path': '/share/' + self._new_share(),
                                    'expires_in': self.share_seconds})
        if route.startswith('/api/'):
            return self._proxy()
        return self._json(404, {'error': 'not found'})

    def do_DELETE(self):
        if urlparse(self.path).path == '/mcp':
            return self._mcp_proxy()
        return self._json(404, {'error': 'not found'})

    def translate_path(self, path):
        route = urlparse(path).path.lstrip('/')
        if route == 'skills/seriald-board/SKILL.md':
            language = dict(parse_qsl(urlparse(path).query)).get('lang', 'en')
            if language == 'zh-CN':
                route = 'skills/seriald-board/SKILL.zh-CN.md'
        return os.path.join(self.static_root, *route.split('/'))

    def end_headers(self):
        if urlparse(self.path).path in ('/', '', '/index.html', '/static/i18n.js', '/static/app.js', '/static/settings.js', '/static/file-transfer.js', '/static/flashing.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js',
                                        '/static/terminal-policy.js', '/static/serial-progress.js', '/static/terminal-navigation.js', '/static/terminal-view.js', '/static/archives.js', '/static/devices.js', '/static/tasks.js', '/static/board-control.js', '/static/app.css', '/static/decklab.svg', '/static/decklab.png', '/static/icons.svg'):
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    if SETTINGS.path.exists():
        SETTINGS.validate(SETTINGS.effective())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', nargs='?', type=int, default=int(os.environ.get('SERIALD_PORT', '8898')))
    parser.add_argument('bind', nargs='?', default=os.environ.get('SERIALD_BIND', '127.0.0.1'))
    parser.add_argument('--port', dest='named_port', type=int)
    parser.add_argument('--bind', dest='named_bind')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--require-backend', action='store_true', help='require a remote HTTPS backend; never use a local collector')
    args = parser.parse_args()
    args.port = args.named_port or args.port
    args.bind = args.named_bind or args.bind
    saved_environment = SETTINGS.read().get('environment', {})
    args.port = int(saved_environment.get('PORT', args.port))
    args.bind = saved_environment.get('BIND', args.bind)
    SETTINGS.environment.update({'SERIALD_PORT': str(args.port), 'SERIALD_BIND': args.bind})
    ControlHandler.collector_socket = SOCKET
    backend_url = os.environ.get('SERIALD_BACKEND_URL', '')
    if args.require_backend and not backend_url:
        parser.error('Web node requires SERIALD_BACKEND_URL')
    ControlHandler.collector_link = BackendLink(backend_url, os.environ.get('SERIALD_BACKEND_TOKEN', ''),
        os.environ.get('SERIALD_BACKEND_CA', '')) if backend_url else None
    ControlHandler.static_root = STATIC
    ControlHandler.auth_token = AUTH_TOKEN
    ControlHandler.settings = SETTINGS
    ControlHandler.default_language = read_ui_language(BASE)
    ControlHandler.grant_store = AgentGrantStore(os.path.join(BASE, 'agent-grants.sqlite3'))
    tokens_path = os.environ.get('SERIALD_AGENT_TOKENS_FILE', '')
    try:
        ControlHandler.agent_tokens = configured_agent_tokens(tokens_path, AUTH_TOKEN)
    except ValueError:
        parser.error('invalid SERIALD_AGENT_TOKENS_FILE or missing SERIALD_AUTH_TOKEN')
    try:
        with BoundedThreadingTCPServer((args.bind, args.port), ControlHandler) as server:
            if ControlHandler.collector_link is None:
                configure_control(server, SETTINGS, SOCKET,
                                  lambda target: UnixHTTPConnection(target, timeout=5))
            else:
                from .remote_settings import configure_remote_control
                configure_remote_control(server, SETTINGS)
            with MCPHost(local_control_url(server)) as mcp:
                server.mcp_host = mcp
                signal.signal(signal.SIGTERM,
                    lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
                print('seriald control on http://%s:%d/' % (args.bind, server.server_address[1]), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    finally:
        ControlHandler.grant_store.close()
    if server.restart_requested:
        reexecute(LAUNCH_ENVIRONMENT, role='web' if ControlHandler.collector_link else None)


if __name__ == '__main__':
    main()
