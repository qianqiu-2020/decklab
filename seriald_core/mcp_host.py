"""Own an optional MCP worker without importing its SDK into the control plane.

The worker listens only on an ephemeral loopback port. Public clients use the
control plane's /mcp route and their own revocable agent grant, never a shared
host credential. The worker reuses HTTP APIs and cannot open serial devices.
"""
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .config import BASE


CODE_ROOT = Path(__file__).resolve().parents[1]
LOG_LIMIT = 64 * 1024
LOG_LINE_LIMIT = 8192


def _safe_log_text(value):
    """Worker output is untrusted; retain diagnostics without terminal escapes or credentials."""
    text = value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(char for char in text if char in '\r\n\t' or ord(char) >= 32 and ord(char) != 127)
    text = re.sub(r'(?i)(Bearer\s+)[^\s\"\'<>]+', r'\1<redacted>', text)
    text = re.sub(r'seriald-ag-[A-Za-z0-9_-]+', '<redacted-agent-token>', text)
    text = re.sub(r'(?i)(https?://)[^\s/@]+@', r'\1<redacted>@', text)
    return text


class _WorkerLog:
    """Drain one child pipe continuously into bounded, private diagnostic files."""
    def __init__(self, path):
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + '.1')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.tail = bytearray()
        self.file = self._open()
        self.thread = None
        self.pipe = None

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        flags |= getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('MCP diagnostic log must be a regular file')
            if hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(self.path, 0o600)
            if os.fstat(fd).st_size > LOG_LIMIT:
                os.ftruncate(fd, 0)
            return os.fdopen(fd, 'ab', buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def write(self, value):
        data = _safe_log_text(value).encode('utf-8')
        if not data:
            return
        if len(data) > LOG_LINE_LIMIT:
            data = b'[oversized worker log line omitted]\n'
        with self.lock:
            self.tail.extend(data)
            del self.tail[:-LOG_LINE_LIMIT]
            if self.file is None:
                return
            try:
                if self.file.tell() + len(data) > LOG_LIMIT:
                    self.file.close()
                    self.file = None
                    os.replace(self.path, self.backup)
                    self.file = self._open()
                self.file.write(data)
            except OSError:
                # A logging failure cannot wedge the worker's output pipe.
                if self.file is not None:
                    self.file.close()
                    self.file = None

    def attach(self, pipe):
        self.pipe = pipe
        self.thread = threading.Thread(target=self._drain, name='mcp-worker-log', daemon=True)
        self.thread.start()

    def _drain(self):
        pipe = self.pipe
        pending = bytearray()
        discard = False
        try:
            while True:
                chunk = pipe.read(LOG_LINE_LIMIT)
                if not chunk:
                    break
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, remainder = pending.partition(b'\n')
                    pending = bytearray(remainder)
                    if not discard:
                        self.write(bytes(line) + b'\n')
                    discard = False
                if len(pending) > LOG_LINE_LIMIT:
                    if not discard:
                        self.write('[oversized worker log line omitted]\n')
                    pending.clear()
                    discard = True
            if pending and not discard:
                self.write(bytes(pending))
        except (OSError, ValueError):
            pass

    def close(self):
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None
        with self.lock:
            if self.file is not None:
                self.file.close()
                self.file = None
            return bytes(self.tail).decode('utf-8', 'replace')


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
        self._log = None
        self._log_path = Path(BASE) / 'mcp-worker.log'
        self._last_log_tail = ''
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
            override = os.environ.get('SERIALD_MCP_PYTHON')
            bundled = CODE_ROOT / 'mcp-runtime' / 'seriald-mcp-worker'
            if not override and os.path.lexists(bundled):
                if not bundled.is_file() or not os.access(bundled, os.X_OK):
                    raise RuntimeError('内置 MCP 运行程序不可执行，请检查安装包完整性与文件权限')
                command = [str(bundled)]
            else:
                script = CODE_ROOT / 'seriald_mcp.py'
                if not script.is_file():
                    raise RuntimeError('MCP 入口脚本缺失，请检查安装包完整性')
                command = [override or sys.executable, str(script)]
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
        self._last_log_tail = ''
        try:
            self._log = _WorkerLog(self._log_path)
            self._log.write('\n[%s] MCP worker startup\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
        except OSError:
            self.error = '服务端 MCP 无法创建诊断日志，请检查运行数据目录权限：' + str(self._log_path)
            return False
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix='decklab-mcp-worker-')
        except OSError as error:
            return self._failure('服务端 MCP 无法创建启动目录，请检查临时目录权限', error)
        ready = Path(self._temporary.name) / 'ready.json'
        environment = dict(os.environ)
        # HTTP mode always forwards each caller's grant; no administrator token
        # is needed by this child, including on an open-access host.
        for name in ('SERIALD_TOKEN', 'SERIALD_AUTH_TOKEN', 'SERIALD_AGENT_TOKENS_FILE'):
            environment.pop(name, None)
        environment['SERIALD_ACTOR'] = 'decklab-mcp'
        options = {'env': environment, 'stdin': subprocess.DEVNULL,
                   'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT, 'bufsize': 0}
        if os.name == 'nt':
            options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            self.process = subprocess.Popen(self._command(ready), **options)
            self._log.attach(self.process.stdout)
            deadline = time.monotonic() + self.startup_timeout
            probe_status = None
            while time.monotonic() < deadline:
                code = self.process.poll()
                if code is not None:
                    self._stop_worker()
                    dependency = any(term in self._last_log_tail for term in (
                        'ImportError', 'ModuleNotFoundError', 'Install MCP support', 'MCP SDK import failed',
                        'error while loading shared libraries', 'DLL load failed'))
                    reason = '依赖加载失败' if dependency else '工作进程提前退出'
                    self.error = '服务端 MCP %s（退出码 %s）；详情见 %s' % (reason, code, self._log_path)
                    return False
                if ready.exists():
                    document = json.loads(ready.read_text(encoding='utf-8'))
                    if not isinstance(document, dict):
                        raise ValueError('invalid MCP worker readiness document')
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
                        probe_status = response.status
                        if response.status == 401:
                            self._log.write('MCP worker ready on a private loopback listener\n')
                            self._target = ('127.0.0.1', port)
                            self.error = ''
                            self._retry = True
                            return True
                    except (OSError, http.client.HTTPException):
                        pass
                    finally:
                        connection.close()
                time.sleep(.05)
        except FileNotFoundError as error:
            return self._failure('服务端 MCP 启动程序不存在，请检查安装路径', error)
        except PermissionError as error:
            return self._failure('服务端 MCP 启动程序权限不足', error)
        except RuntimeError as error:
            return self._failure(str(error), error)
        except (ValueError, UnicodeError) as error:
            return self._failure('服务端 MCP 返回的启动信息无效', error)
        except OSError as error:
            return self._failure('服务端 MCP 无法启动工作进程（系统错误 %s）' % error.errno, error)
        detail = '（启动检查 HTTP %s）' % probe_status if probe_status is not None else ''
        return self._failure('服务端 MCP 启动超时，等待 %s 秒%s' % (self.startup_timeout, detail))

    def _failure(self, message, error=None):
        if self._log is not None:
            self._log.write(message + '\n')
            if error is not None:
                self._log.write('%s: %s\n' % (type(error).__name__, error))
        self._stop_worker()
        self.error = message + '；详情见 ' + str(self._log_path)
        return False

    @property
    def target(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self._target
            if self._target:
                self._target = None
                self.error = '服务端 MCP 工作进程已停止；详情见 ' + str(self._log_path)
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
            if self._log is not None:
                self._log.write('MCP worker exited with code %s\n' % process.returncode)
        if self._log is not None:
            self._last_log_tail = self._log.close()
            self._log = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def close(self):
        with self._lock:
            self._closed = True
            self._retry = False
            self._stop_worker()
