"""Paramiko SSH channels with the same logged byte-stream API as Linux PTYs."""
import codecs
import os
import threading
import time

import paramiko

from .config import ANSI, DATE_FMT, SCROLLBACK


def _connect(ip, user, password):
    client = paramiko.SSHClient()
    # Board reflashing replaces host keys; do not persist a stale host key.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=6,
                       auth_timeout=6, banner_timeout=6, allow_agent=False,
                       look_for_keys=False)
        return client
    except BaseException:
        client.close()
        raise


def check_password_login(ip, user, password):
    try:
        client = _connect(ip, user, password)
        client.close()
        return True, False, ''
    except paramiko.AuthenticationException:
        return False, True, 'SSH 认证失败'
    except (OSError, paramiko.SSHException) as error:
        return False, False, 'SSH 连接失败: %s' % error


class SshSession:
    def __init__(self, sid, ip, label, logdir, user=None, password=None):
        self.sid, self.ip, self.label = sid, ip, label
        self.user = user or os.environ.get('SERIALD_SSH_USER', 'root')
        self.password = password if password is not None else os.environ.get('SERIALD_SSH_PASSWORD', 'bianbu')
        self.actor, self.title, self.persistent, self.store = 'web', '', False, None
        self.buf, self.base = bytearray(), 0
        self.cv, self.write_lock = threading.Condition(), threading.Lock()
        self._lifecycle_lock, self._log_lock = threading.Lock(), threading.Lock()
        self._finish_lock = threading.Lock()
        self._stop, self._disposed = threading.Event(), threading.Event()
        self._thread, self._started_once, self.closed = None, False, False
        self.fd, self.client = None, None  # fd is the Paramiko channel for API compatibility.
        self.started, self.last_touch = time.time(), time.monotonic()
        self.cols, self.rows = 120, 32
        self._logdir, self._logday = logdir, time.strftime(DATE_FMT)
        self.logf, self.logpath = None, None
        self._log_decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._log_pending, self._log_finished = '', False
        self._open_log()

    def _open_log(self):
        try:
            os.makedirs(self._logdir, exist_ok=True)
            self._logday = time.strftime(DATE_FMT)
            self.logpath = os.path.join(self._logdir, 'ssh-%s-%s.log' % (self.sid, self._logday))
            self.logf = open(self.logpath, 'a', encoding='utf-8', buffering=1)
        except OSError:
            self.logf = None

    def _log(self, data, final=False):
        with self._log_lock:
            if self.logf and not self._log_finished and self._logday != time.strftime(DATE_FMT):
                self.logf.close()
                self.logf = None
                self._open_log()
            if not self.logf or self._log_finished:
                return
            self._log_pending += self._log_decoder.decode(data, final=final)
            self._log_finished = final
            lines = self._log_pending.split('\n')
            self._log_pending = lines.pop()
            if final or len(self._log_pending) > 16384:
                lines.append(self._log_pending)
                self._log_pending = ''
            stamp = time.strftime('%Y-%m-%d %H:%M:%S')
            try:
                for raw in lines:
                    line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                    if line:
                        self.logf.write('%s  %s\n' % (stamp, line))
            except OSError:
                pass

    def _append(self, data):
        chunk = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        self._log(chunk)
        with self.cv:
            self.buf += chunk
            if len(self.buf) > SCROLLBACK:
                drop = len(self.buf) - SCROLLBACK
                del self.buf[:drop]
                self.base += drop
            self.cv.notify_all()

    def ensure(self):
        with self._lifecycle_lock:
            if self.closed or self._started_once:
                return self.fd is not None and not self.closed
            self._started_once = True
            try:
                client = _connect(self.ip, self.user, self.password)
                channel = client.invoke_shell(term='xterm-256color', width=self.cols,
                                              height=self.rows)
                channel.settimeout(0.25)
                with self.cv:
                    self.client, self.fd = client, channel
                    self.last_touch = time.monotonic()
                self._append('\r\n*** 已建立 SSH 会话 %s@%s (%dx%d) ***\r\n'
                             % (self.user, self.ip, self.cols, self.rows))
                self._thread = threading.Thread(target=self._reader, daemon=True)
                self._thread.start()
                return True
            except (OSError, paramiko.SSHException) as error:
                if 'client' in locals():
                    client.close()
                self._append('\r\n*** 建立 SSH 会话失败: %s ***\r\n' % error)
                self._finish()
                return False

    def _reader(self):
        try:
            while not self._stop.is_set() and self.fd is not None:
                try:
                    data = self.fd.recv(65536)
                except TimeoutError:
                    continue
                except (OSError, paramiko.SSHException):
                    break
                if not data:
                    break
                self._append(data)
        finally:
            self._append('\r\n*** SSH 会话已结束 ***\r\n')
            self._finish()

    def _finish(self):
        with self._finish_lock:
            if self._disposed.is_set():
                return
            self._stop.set()
            with self.write_lock:
                with self.cv:
                    self.closed = True
                    channel, self.fd = self.fd, None
                    client, self.client = self.client, None
                    self.cv.notify_all()
                if channel is not None:
                    channel.close()
                if client is not None:
                    client.close()
            self._log(b'', final=True)
            with self._log_lock:
                if self.logf:
                    self.logf.close()
                    self.logf = None
            self._disposed.set()
            if self.store:
                self.store.close_session(self.sid)

    def read(self, off, wait=15.0):
        if off <= 0:
            wait = 0
        deadline = time.monotonic() + wait
        with self.cv:
            self.last_touch = time.monotonic()
            while (not self.closed and self.fd is not None and off >= self.base + len(self.buf)
                   and time.monotonic() < deadline):
                self.cv.wait(1.0)
            start = max(off - self.base, 0) if off > self.base else 0
            return self.base + len(self.buf), bytes(self.buf[start:]), self.fd is not None and not self.closed

    def write(self, text):
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.last_touch = time.monotonic()
                channel = self.fd
            try:
                channel.sendall(text.encode('utf-8', 'replace'))
                return True
            except (OSError, paramiko.SSHException):
                return False

    def resize(self, cols, rows):
        if cols < 20 or rows < 5 or cols > 500 or rows > 200:
            return False
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.cols, self.rows = cols, rows
                self.last_touch = time.monotonic()
                channel = self.fd
            try:
                channel.resize_pty(width=cols, height=rows)
                return True
            except (OSError, paramiko.SSHException):
                return False

    def close(self):
        with self._lifecycle_lock:
            if self._disposed.is_set():
                return
            with self.cv:
                self.closed = True
                self.cv.notify_all()
            self._stop.set()
            self.append_marker('*** 会话已关闭 ***')
            # Closing the channel interrupts a blocked recv; _finish owns cleanup.
            if self.fd is not None:
                self.fd.close()
            if self._thread is not None:
                self._thread.join(timeout=3)
            self._finish()

    def is_idle(self, now, ttl):
        with self.cv:
            return self.closed or now - self.last_touch >= ttl

    def append_marker(self, message):
        self._append('\r\n%s\r\n' % message)

    def snapshot(self):
        with self.cv:
            return {'sid': self.sid, 'kind': 'ssh', 'ip': self.ip,
                    'actor': self.actor, 'title': self.title,
                    'persistent': self.persistent,
                    'alive': self.fd is not None and not self.closed,
                    'started': self.started, 'bytes': len(self.buf)}
