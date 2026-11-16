"""Bounded WebSocket IO using unmodified, pinned wsproto/h11 wheels.

Routes authenticate before accept(); this module knows no devices, credentials or
upstream addresses. One receiver and concurrent serialized senders are supported.
"""
from collections import deque
import base64
import hashlib
import importlib
import math
from pathlib import Path
import re
import select
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit


_WHEELS = (
    ('h11', '0.16.0', 'h11-0.16.0-py3-none-any.whl',
     '63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86'),
    ('wsproto', '1.3.2', 'wsproto-1.3.2-py3-none-any.whl',
     '61eea322cdf56e8cc904bd3ad7573359a242ba65688716b0710a5eb12beab584'),
)
_IMPORT_LOCK = threading.Lock()
_PROTOCOL = None


class WSError(RuntimeError):
    def __init__(self, message='WebSocket connection stopped', code=1006):
        super().__init__(message)
        self.code = code


class WSHandshakeError(WSError):
    def __init__(self, status=400):
        super().__init__('WebSocket handshake rejected', 1002)
        self.status = status


class WSTimeout(WSError):
    """A recv polling deadline elapsed; the connection remains usable."""
    def __init__(self):
        super().__init__('WebSocket receive deadline elapsed', 1001)


class WSClosed(WSError):
    pass


def _protocol_modules():
    """Import only the checked offline inputs, never silently use a system package."""
    global _PROTOCOL
    with _IMPORT_LOCK:
        if _PROTOCOL is not None:
            return _PROTOCOL
        directory = Path(__file__).resolve().parents[1] / 'vendor' / 'python'
        paths = []
        try:
            for name, version, filename, expected in _WHEELS:
                path = directory / filename
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError()
                paths.append(str(path))
                existing = sys.modules.get(name)
                if existing is not None and (getattr(existing, '__version__', None) != version
                        or not str(getattr(existing, '__file__', '')).replace('\\', '/').startswith(path.as_posix() + '/')):
                    raise ValueError()
            for path in reversed(paths):
                if path not in sys.path:
                    sys.path.insert(0, path)
            wsproto = importlib.import_module('wsproto')
            events = importlib.import_module('wsproto.events')
            _PROTOCOL = wsproto, events
            return _PROTOCOL
        except (ImportError, OSError, ValueError):
            raise WSError('Pinned WebSocket protocol runtime is unavailable') from None


def _positive(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError('invalid WebSocket ' + name)
    return float(value)


class WSConnection:
    @classmethod
    def accept(cls, handler, *, origins, subprotocol, hosts=None, max_message=65536,
               max_send=12 * 1024 ** 2, idle_timeout=30, lifetime=3600,
               io_timeout=5, message_timeout=10):
        """Validate a parsed GET and send 101; invalid requests raise before writing.

        origins is the route's explicit set of exact allowed Origin values. The
        required subprotocol must have been offered by the browser. Extensions
        (including compression) are deliberately not negotiated.
        """
        if (not isinstance(origins, (set, frozenset, list, tuple)) or not origins
                or any(not isinstance(value, str) or not value or '\r' in value or '\n' in value for value in origins)
                or not isinstance(subprotocol, str)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", subprotocol)):
            raise ValueError('explicit WebSocket origins and subprotocol are required')
        for value in (max_message, max_send):
            if type(value) is not int or not 1 <= value <= 32 * 1024 ** 2:
                raise ValueError('invalid WebSocket message limit')
        limits = [_positive(value, name) for value, name in (
            (idle_timeout, 'idle timeout'), (lifetime, 'lifetime'),
            (io_timeout, 'IO timeout'), (message_timeout, 'message timeout'))]
        if hosts is None:
            hosts = {urlsplit(value).netloc for value in origins}
        if (not isinstance(hosts, (set, frozenset, list, tuple)) or not hosts
                or any(not isinstance(value, str) or not value or '\r' in value or '\n' in value for value in hosts)):
            raise ValueError('explicit WebSocket hosts are required')
        if handler.command != 'GET' or handler.request_version != 'HTTP/1.1':
            raise WSHandshakeError()
        pairs = list(handler.headers.raw_items())
        if len(pairs) > 64 or sum(len(key) + len(value) + 4 for key, value in pairs) > 32768:
            raise WSHandshakeError(431)
        for name in ('Host', 'Origin', 'Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Upgrade'):
            if len(handler.headers.get_all(name, [])) != 1:
                raise WSHandshakeError()
        if handler.headers['Origin'] not in origins or handler.headers['Host'] not in hosts:
            raise WSHandshakeError(403)
        if handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length', '0') != '0':
            raise WSHandshakeError()
        try:
            if len(base64.b64decode(handler.headers['Sec-WebSocket-Key'], validate=True)) != 16:
                raise ValueError()
            protocol_module, events = _protocol_modules()
            protocol = protocol_module.WSConnection(protocol_module.ConnectionType.SERVER)
            protocol.initiate_upgrade_connection(
                [(key.encode('ascii'), value.encode('latin-1')) for key, value in pairs], handler.path)
            requests = list(protocol.events())
            if (len(requests) != 1 or not isinstance(requests[0], events.Request)
                    or subprotocol not in requests[0].subprotocols):
                raise ValueError()
            response = protocol.send(events.AcceptConnection(subprotocol=subprotocol))
        except WSError:
            raise
        except Exception:
            # Do not propagate library diagnostics containing request headers.
            raise WSHandshakeError() from None
        stream = handler.connection
        try:
            stream.settimeout(limits[2])
            handler.wfile.write(response)
            handler.wfile.flush()
            stream.setblocking(False)
        except OSError:
            raise WSClosed() from None
        handler.close_connection = True
        return cls(stream, handler.rfile, protocol, events, max_message, max_send, *limits)

    def __init__(self, stream, reader, protocol, events, max_message, max_send,
                 idle_timeout, lifetime, io_timeout, message_timeout):
        self.stream, self.reader, self.protocol, self.events = stream, reader, protocol, events
        self.max_message, self.max_send = max_message, max_send
        self.idle_timeout, self.lifetime = idle_timeout, lifetime
        self.io_timeout, self.message_timeout = io_timeout, message_timeout
        self.started = self.last_received = self.last_pong = time.monotonic()
        self.closed, self.close_code = False, None
        self._state_lock, self._send_lock, self._recv_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self._pending, self._parts = deque(), []
        self._size, self._message_started, self._fragments = 0, None, 0

    def _wait(self, reading, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        try:
            readable, writable, _ = select.select([self.stream] if reading else [],
                                                 [] if reading else [self.stream], [], left)
            return bool(readable or writable)
        except (OSError, ValueError):
            raise WSClosed() from None

    def _write(self, data):
        pending, deadline = memoryview(data), time.monotonic() + self.io_timeout
        try:
            while pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                try:
                    count = self.stream.send(pending)
                    if count <= 0:
                        raise OSError()
                    pending = pending[count:]
                except ssl.SSLWantReadError:
                    if not self._wait(True, deadline):
                        raise TimeoutError()
                except (BlockingIOError, ssl.SSLWantWriteError):
                    if not self._wait(False, deadline):
                        raise TimeoutError()
        except (OSError, ValueError, TimeoutError):
            self._shutdown(1006)
            raise WSClosed() from None

    def _send_event(self, event):
        with self._send_lock:
            with self._state_lock:
                if self.closed:
                    raise WSClosed(code=self.close_code or 1006)
                try:
                    data = self.protocol.send(event)
                except Exception:
                    raise WSError('Invalid WebSocket send operation', 1002) from None
            self._write(data)

    def send(self, message):
        if not isinstance(message, (str, bytes)):
            raise ValueError('WebSocket messages require str or bytes')
        try:
            size = len(message.encode('utf-8')) if isinstance(message, str) else len(message)
        except UnicodeError:
            raise ValueError('invalid WebSocket text') from None
        if size > self.max_send:
            raise ValueError('outbound WebSocket message exceeds limit')
        self._check_deadlines()
        self._send_event(self.events.TextMessage(data=message) if isinstance(message, str)
                         else self.events.BytesMessage(data=message))

    def ping(self, payload=b''):
        if not isinstance(payload, bytes) or len(payload) > 125:
            raise ValueError('invalid WebSocket ping payload')
        self._check_deadlines()
        self._send_event(self.events.Ping(payload=payload))

    def _shutdown(self, code):
        with self._state_lock:
            self.closed, self.close_code = True, code
            self._pending.clear()
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self, code=1000):
        if self.closed:
            return
        try:
            self._send_event(self.events.CloseConnection(code=code))
        except WSError:
            pass
        finally:
            self._shutdown(code)

    def _check_deadlines(self):
        if self.closed:
            raise WSClosed(code=self.close_code or 1006)
        now = time.monotonic()
        if now - self.started >= self.lifetime or now - self.last_received >= self.idle_timeout:
            self.close(1001)
            raise WSClosed('WebSocket session deadline elapsed', 1001)
        if self._message_started is not None and now - self._message_started >= self.message_timeout:
            self.close(1008)
            raise WSError('WebSocket message deadline elapsed', 1008)

    def _read(self):
        # Read one byte first to distinguish nonblocking would-block (None)
        # from EOF (b''). read1 then drains prebuffered HTTP bytes or performs
        # at most one raw read, including over an SSLSocket.
        first = self.reader.read(1)
        if not first:
            return first
        try:
            read = getattr(self.reader, 'read1', self.reader.read)
            rest = read(16383)
        except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            rest = None
        return first + (rest or b'')

    def _consume(self, event):
        self._check_deadlines()
        if isinstance(event, self.events.Ping):
            self._send_event(event.response())
        elif isinstance(event, self.events.Pong):
            self.last_pong = time.monotonic()
        elif isinstance(event, self.events.CloseConnection):
            code = int(event.code)
            if code != 1006:
                try:
                    self._send_event(event.response())
                except WSError:
                    pass
            self._shutdown(code)
            if code in (1002, 1007, 1009):
                raise WSError('Invalid WebSocket peer message', code)
        elif isinstance(event, (self.events.TextMessage, self.events.BytesMessage)):
            if self._message_started is None:
                self._message_started = time.monotonic()
            self._size += len(event.data.encode('utf-8')) if isinstance(event.data, str) else len(event.data)
            self._fragments += 1
            if self._size > self.max_message or self._fragments > 4096:
                self.close(1009)
                raise WSError('Inbound WebSocket message exceeds limit', 1009)
            if event.data:
                self._parts.append(event.data)
            if event.message_finished:
                message = ''.join(self._parts) if isinstance(event.data, str) else b''.join(self._parts)
                with self._state_lock:
                    full = len(self._pending) >= 32
                    if not full and not self.closed:
                        self._pending.append(message)
                if full:
                    self.close(1009)
                    raise WSError('WebSocket peer queue exceeds limit', 1009)
                self._parts, self._size, self._message_started, self._fragments = [], 0, None, 0

    def recv(self, timeout=1):
        """Return one complete str/bytes message, None on close, or WSTimeout.

        Polling timeout does not discard fragments. No messages are resent or
        delivered again after a lost connection.
        """
        deadline = time.monotonic() + _positive(timeout, 'receive timeout')
        with self._recv_lock:
            while True:
                with self._state_lock:
                    if self.closed:
                        return None
                self._check_deadlines()
                with self._state_lock:
                    if self.closed:
                        return None
                    if self._pending:
                        return self._pending.popleft()
                try:
                    data = self._read()
                except ssl.SSLWantWriteError:
                    if not self._wait(False, deadline):
                        raise WSTimeout()
                    continue
                except (BlockingIOError, ssl.SSLWantReadError):
                    data = None
                except (OSError, ValueError):
                    self._shutdown(1006)
                    raise WSClosed() from None
                if data is None:
                    stop = min(deadline, self.started + self.lifetime,
                               self.last_received + self.idle_timeout)
                    if self._message_started is not None:
                        stop = min(stop, self._message_started + self.message_timeout)
                    if not self._wait(True, stop):
                        self._check_deadlines()
                        raise WSTimeout()
                    continue
                if not data:
                    self._shutdown(1006)
                    return None
                self.last_received = time.monotonic()
                try:
                    with self._state_lock:
                        self.protocol.receive_data(data)
                        events = list(self.protocol.events())
                    for event in events:
                        self._consume(event)
                except WSError:
                    raise
                except Exception:
                    self.close(1002)
                    raise WSError('Invalid WebSocket peer message', 1002) from None
                if time.monotonic() >= deadline and not self._pending and not self.closed:
                    raise WSTimeout()
