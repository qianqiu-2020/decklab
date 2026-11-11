"""Own an optional MCP worker without importing its SDK into the control plane.

The worker listens only on an ephemeral loopback port. Public clients use the
control plane's /mcp route and their own revocable agent grant, never a shared
host credential. The worker reuses HTTP APIs and cannot open serial devices.
"""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .config import STATIC


def local_control_url(server):
    """Reach this listener locally even when it binds one specific LAN address."""
    host, port = server.server_address[:2]
    if host in ('0.0.0.0', '::', ''):
        host = '::1' if host == '::' else '127.0.0.1'
    return 'http://%s:%d' % (('[' + host + ']') if ':' in host else host, port)


class MCPHost:
    def __init__(self, control_url, startup_timeout=12):
        self.control_url = control_url
        self.startup_timeout = startup_timeout
        self.process = None
        self._target = None
        self._temporary = None
        self._lock = threading.RLock()
        # Reserve control workers for nested authenticated tool API calls.
        self.requests = threading.BoundedSemaphore(16)
        self._closed = False
        self._retry = False
        self._retry_at = 0
        self.error = '服务端 MCP 尚未启动'

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()

    def _command(self, ready):
        if getattr(sys, 'frozen', False):
            command = [sys.executable, 'mcp']
        else:
            python = os.environ.get('SERIALD_MCP_PYTHON') or sys.executable
            command = [python, str(Path(STATIC) / 'seriald_mcp.py')]
        return command + ['--url', self.control_url, '--transport', 'streamable-http',
                          '--host', '127.0.0.1', '--port', '0', '--ready-file', str(ready),
                          '--parent-pid', str(os.getpid())]

    def start(self):
        with self._lock:
            return self._start()

    def _start(self):
        if self._closed:
            return False
        if self.process is not None and self.process.poll() is None and self._target:
            return True
        self._stop_worker()
        if os.environ.get('SERIALD_MCP_ENABLED', '1') == '0':
            self.error = '服务端 MCP 已停用'
            self._retry = False
            return False
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix='decklab-mcp-worker-')
        except OSError:
            self.error = '服务端 MCP 组件不可用，请检查安装或 SERIALD_MCP_PYTHON'
            return False
        ready = Path(self._temporary.name) / 'ready.json'
        environment = dict(os.environ)
        # HTTP mode always forwards each caller's grant; no administrator token
        # is needed by this child, including on an open-access host.
        for name in ('SERIALD_TOKEN', 'SERIALD_AUTH_TOKEN', 'SERIALD_AGENT_TOKENS_FILE'):
            environment.pop(name, None)
        environment['SERIALD_ACTOR'] = 'decklab-mcp'
        options = {'env': environment, 'stdin': subprocess.DEVNULL,
                   'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            self.process = subprocess.Popen(self._command(ready), **options)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                if ready.exists():
                    document = json.loads(ready.read_text(encoding='utf-8'))
                    port = document.get('port')
                    if (document.get('host') != '127.0.0.1' or type(port) is not int
                            or not 1 <= port <= 65535):
                        raise ValueError('invalid MCP worker readiness')
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
                    try:
                        # Missing credentials must fail before any board/control
                        # API call. The control listener may not be serving yet.
                        connection.request('GET', '/mcp')
                        response = connection.getresponse()
                        response.read(1024)
                        if response.status == 401:
                            self._target = ('127.0.0.1', port)
                            self.error = ''
                            self._retry = True
                            return True
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        connection.close()
                time.sleep(.05)
        except (OSError, ValueError):
            pass
        self.error = '服务端 MCP 组件不可用，请检查安装或 SERIALD_MCP_PYTHON'
        self._stop_worker()
        return False

    @property
    def target(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self._target
            if self._target:
                self._target = None
                self.error = '服务端 MCP 工作进程已停止'
            if self._retry and not self._closed and time.monotonic() >= self._retry_at:
                self._retry_at = time.monotonic() + 10
                self._start()
            return self._target

    def snapshot(self):
        # Availability checks remain cheap and do not spawn a worker in a GET.
        with self._lock:
            available = bool(self._target and self.process is not None
                             and self.process.poll() is None and not self._closed)
            return {'mcp_enabled': available, 'mcp_url': '/mcp',
                    'mcp_error': '' if available else (self.error or '服务端 MCP 工作进程已停止')}

    def _stop_worker(self):
        self._target = None
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass  # The child can exit between poll() and terminate().
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=5)
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def close(self):
        with self._lock:
            self._closed = True
            self._retry = False
            self._stop_worker()
