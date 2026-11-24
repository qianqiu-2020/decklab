"""Authenticated, bounded WebSocket tunnel to the existing collector link.

Only the collector interprets desktop messages. This tunnel never receives
target credentials or opens a VNC/RDP connection.
"""
import http.client
import ipaddress
import socket
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_SLOTS_LOCK = threading.Lock()


def public_origin(handler):
    from .control_server import public_mcp_host_allowed
    host = handler.headers.get('Host', '')
    origin = handler.headers.get('Origin', '')
    try:
        parsed = urlparse(origin)
        if (parsed.scheme not in ('http', 'https') or parsed.username is not None
                or parsed.password is not None or parsed.netloc.lower() != host.lower()
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        if not public_mcp_host_allowed(handler.server, host):
            # A directly connected browser can use a secondary NIC or VPN IP.
            # Match the accepted socket's local address, independent of DNS and proxy headers.
            local_ip, local_port = handler.connection.getsockname()[:2]
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
            if ipaddress.ip_address(parsed.hostname) != ipaddress.ip_address(local_ip) or port != local_port:
                raise ValueError()
    except (ValueError, AttributeError, OSError, TypeError):
        raise ValueError('desktop Origin or Host does not match this service') from None
    return parsed.scheme + '://' + parsed.netloc


def _shutdown(connection):
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def proxy(handler):
    """Called only after the normal control authentication check."""
    connection, response, upstream = None, None, None
    upgraded = False
    done = threading.Event()
    workers = []
    with _SLOTS_LOCK:
        if not hasattr(handler.server, 'desktop_stream_slots'):
            handler.server.desktop_stream_slots = threading.BoundedSemaphore(4)
    slots = handler.server.desktop_stream_slots
    if not slots.acquire(blocking=False):
        return handler._json(429, {'error': 'too many live desktop connections', 'code': 'busy'})
    try:
        origin = public_origin(handler)
        if (handler.headers.get('Upgrade', '').lower() != 'websocket'
                or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length') not in (None, '0')):
            return handler._json(400, {'error': 'a WebSocket upgrade without a body is required'})
        grant = handler._agent_grant()
        if handler.headers.get('Authorization', '').startswith('Bearer seriald-ag-') and grant is None:
            return handler._json(401, {'error': 'Agent authorization expired or revoked', 'code': 'unauthorized'})
        if grant and grant['scope'] == 'connection':
            return handler._json(403, {'error': 'live desktop requires a registered board grant'})
        actor = grant['actor'] if grant else handler._agent_actor() or 'web'
        parsed = urlparse(handler.path)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        allowed = {'key', 'serial', 'sid', 'client_id'}
        if (any(name not in allowed for name, _ in query)
                or len(query) != len({name for name, _ in query})
                or not dict(query).get('key') or not dict(query).get('sid')):
            return handler._json(400, {'error': 'invalid desktop stream selector'})
        if grant and grant['scope'] == 'device':
            query = [(name, value) for name, value in query if name != 'serial'] + [('serial', grant['serial'])]
        target = urlunparse(parsed._replace(query=urlencode(query)))
        headers = {name: handler.headers[name] for name in (
            'Upgrade', 'Connection', 'Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol')
            if handler.headers.get(name) is not None}
        headers.update({'Host': handler.headers['Host'], 'Origin': origin,
                        'X-Seriald-Desktop-Origin': origin, 'X-Seriald-Actor': actor})
        if grant:
            headers['X-Seriald-Grant-Generation'] = str(grant.get('revocation_generation', 0))
        connection = handler._collector_connection(8)
        connection.request('GET', target, headers=headers)
        response = connection.getresponse()
        if response.status != 101:
            body = response.read(65537)
            if len(body) > 65536:
                return handler._json(502, {'error': 'desktop connection response exceeded limit'})
            handler.send_response(response.status)
            handler.send_header('Content-Type', 'application/json; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.wfile.write(body)
            return
        if (response.getheader('Upgrade', '').lower() != 'websocket'
                or response.getheader('Sec-WebSocket-Protocol') != 'decklab-desktop-v1'):
            return handler._json(502, {'error': 'backend did not confirm the desktop protocol'})
        upstream = connection.sock
        if upstream is None or response.fp is None:
            return handler._json(502, {'error': 'backend desktop transport unavailable'})
        handler.protocol_version = 'HTTP/1.1'
        handler.send_response(101)
        for name in ('Upgrade', 'Connection', 'Sec-WebSocket-Accept', 'Sec-WebSocket-Protocol'):
            value = response.getheader(name)
            if value:
                handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.flush()
        upgraded = True
        handler.close_connection = True
        # The buffered HTTP readers can already contain the first WS frame.
        # Dedicated directions preserve those bytes; neither direction parses
        # or replays input. A bounded send timeout prevents a slow viewer from
        # holding native resources indefinitely.
        handler.connection.settimeout(35)
        upstream.settimeout(35)

        def copy(source, destination):
            try:
                while not done.is_set():
                    block = source.read1(65536)
                    if not block:
                        break
                    destination.sendall(block)
            except (OSError, ValueError):
                pass
            finally:
                done.set()
                _shutdown(handler.connection)
                _shutdown(upstream)

        pending_workers = [threading.Thread(target=copy, args=(handler.rfile, upstream),
                                            name='desktop-tunnel-up', daemon=True),
                           threading.Thread(target=copy, args=(response.fp, handler.connection),
                                            name='desktop-tunnel-down', daemon=True)]
        for worker in pending_workers:
            worker.start()
            workers.append(worker)
        deadline = time.monotonic() + 24 * 3600
        while not done.wait(.5):
            if (time.monotonic() >= deadline or not handler._authorized()
                    or getattr(handler.server, 'restart_requested', False)):
                done.set()
        _shutdown(handler.connection)
        _shutdown(upstream)
    except ValueError as error:
        if not upgraded:
            handler._json(403, {'error': str(error), 'code': 'forbidden'})
    except (OSError, http.client.HTTPException, RuntimeError):
        if not upgraded:
            handler._json(503, {'error': 'desktop backend unavailable', 'code': 'unavailable'})
    finally:
        done.set()
        if upgraded:
            _shutdown(handler.connection)
            _shutdown(upstream)
        for worker in workers:
            worker.join(timeout=2)
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        slots.release()
