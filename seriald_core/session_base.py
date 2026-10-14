"""Shared SSH stream, lifecycle, logging and audit; adapters own only IO."""
import codecs
from contextlib import contextmanager, ExitStack
import os
import threading
import time

from .config import ANSI, DATE_FMT, SCROLLBACK
from .terminal_backend import SharedTerminal


class SessionBase:
    transport_errors = (OSError, RuntimeError)
    join_timeout = None

    def __init__(self, sid, ip, label, logdir, user=None, password=None):
        self.sid, self.ip, self.label = sid, ip, label
        self.user = user or os.environ.get('SERIALD_SSH_USER', 'root')
        self.password = password if password is not None else os.environ.get('SERIALD_SSH_PASSWORD', 'bianbu')
        self.actor, self.title, self.persistent, self.store = 'web', '', False, None
        self.buf, self.base = bytearray(), 0
        self.cv = threading.Condition()
        self.write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._log_lock = threading.RLock()
        self._stop, self._disposed = threading.Event(), threading.Event()
        self._thread, self._started_once, self.closed = None, False, False
        self.fd = None
        self.started, self.last_touch = time.time(), time.monotonic()
        self.cols, self.rows = 120, 32
        self.terminal = SharedTerminal('ssh', self.cols, self.rows)
        self.takeovers = set()
        self.transport_error = ''
        self._logdir, self._logday = logdir, time.strftime(DATE_FMT)
        self.logf, self.logpath, self.log_error = None, None, ''
        self._log_retry_after = 0.0
        self._log_decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._log_pending, self._log_finished = '', False
        self._open_log(logdir)

    def _open_log(self, logdir):
        try:
            os.makedirs(logdir, exist_ok=True)
            day = time.strftime(DATE_FMT)
            path = os.path.join(logdir, 'ssh-%s-%s.log' % (self.sid, day))
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath, self._logday, self.log_error = path, day, ''
        except OSError as error:
            self.logf = None
            self.log_error = str(error)
            self._log_retry_after = time.monotonic() + 5

    def _release_log_handle(self):
        if self.logf:
            try:
                self.logf.close()
            except OSError as error:
                self.log_error = str(error)
            self.logf = None

    def _log(self, data, final=False):
        with self._log_lock:
            if not self.logf and not self._log_finished and time.monotonic() >= self._log_retry_after:
                self._open_log(self._logdir)
            if self.logf and not self._log_finished and self._logday != time.strftime(DATE_FMT):
                self._release_log_handle()
                self._open_log(self._logdir)
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
                self.log_error = ''
            except OSError as error:
                self.log_error = str(error)
                self._release_log_handle()
                self._log_retry_after = time.monotonic() + 5

    def _close_log(self):
        self._log(b'', final=True)
        with self._log_lock:
            self._log_finished = True
            self._release_log_handle()

    def _append(self, data, live=True):
        chunk = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        self._log(chunk)
        with self.cv:
            self.buf += chunk
            if len(self.buf) > SCROLLBACK:
                drop = len(self.buf) - SCROLLBACK
                del self.buf[:drop]
                self.base += drop
            replies = self.terminal.feed(chunk, self.base + len(self.buf), live=live)
            self.cv.notify_all()
        for reply in replies:
            self.write(reply)

    def ensure(self):
        with self._lifecycle_lock:
            with self.cv:
                if self.closed or self._started_once:
                    return self.fd is not None and not self.closed
                self._started_once = True
            try:
                fd = self._open_transport()
                with self.cv:
                    self.fd = fd
                    self.last_touch = time.monotonic()
                self._append('\r\n*** 已建立 SSH 会话 %s@%s (%dx%d) ***\r\n'
                             % (self.user, self.ip, self.cols, self.rows), live=False)
                self._thread = threading.Thread(target=self._reader, args=(fd,), daemon=True)
                self._thread.start()
                return True
            except self.transport_errors as error:
                self.transport_error = str(error)
                self._append('\r\n*** 建立 SSH 会话失败: %s ***\r\n' % error, live=False)
                self._thread = None
                self._finish()
                return False

    def _reader(self, fd):
        try:
            while not self._stop.is_set():
                try:
                    data = self._receive(fd)
                except (BlockingIOError, InterruptedError, TimeoutError):
                    continue
                except self.transport_errors as error:
                    self.transport_error = str(error)
                    break
                if data is None:
                    continue
                if not data:
                    break
                self._append(data)
        finally:
            try:
                self._append('\r\n*** SSH 会话已结束 ***\r\n', live=False)
            except self.transport_errors as error:
                self.transport_error = str(error)
            finally:
                self._finish()

    def _finish(self):
        with self._finish_lock:
            if self._disposed.is_set():
                return
            self._stop.set()
            try:
                with self.write_lock:
                    with self.cv:
                        self.closed = True
                        fd, self.fd = self.fd, None
                        self.cv.notify_all()
                    self._dispose_transport(fd)
            except self.transport_errors as error:
                self.transport_error = str(error)
            finally:
                self._close_log()
                self.terminal.close()
                self._disposed.set()
                if self.store:
                    self.store.close_session(self.sid)

    def close(self):
        with self._lifecycle_lock:
            if self._disposed.is_set():
                return
            with self.cv:
                self.closed = True
                self.cv.notify_all()
            self._stop.set()
            self.append_marker('*** 会话已关闭 ***')
            try:
                self._interrupt_transport()
            except self.transport_errors as error:
                self.transport_error = str(error)
            if self._thread is not None:
                self._thread.join(timeout=self.join_timeout)
            self._finish()

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

    def write(self, text, actor=None, client_id=''):
        with self.write_lock:
            if actor is not None and not self.terminal.control(actor, client_id):
                return False
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.last_touch = time.monotonic()
                fd = self.fd
            try:
                self._send(fd, text.encode('utf-8', 'replace'))
                return True
            except self.transport_errors:
                return False

    def resize(self, cols, rows, actor=None, client_id=''):
        if cols < 20 or rows < 5 or cols > 500 or rows > 200:
            return False
        with self.write_lock:
            if actor is not None and not self.terminal.control(actor, client_id):
                return False
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                previous = self.cols, self.rows
                self.cols, self.rows = cols, rows
                self.last_touch = time.monotonic()
                fd = self.fd
                try:
                    # Publish the grid before a SIGWINCH-triggered repaint can
                    # enter the parser. Readers publish under this same lock.
                    self._resize(fd)
                    self.terminal.resize(cols, rows)
                    self.cv.notify_all()
                    return True
                except self.transport_errors:
                    self.cols, self.rows = previous
                    return False

    def is_idle(self, now, ttl):
        with self.cv:
            return self.closed or now - self.last_touch >= ttl

    def append_marker(self, text):
        self._append('\r\n%s\r\n' % text, live=False)

    def snapshot(self):
        with self.cv:
            return {'sid': self.sid, 'kind': 'ssh', 'ip': self.ip,
                    'actor': self.actor, 'title': self.title, 'persistent': self.persistent,
                    'log_error': self.log_error, 'transport_error': self.transport_error,
                    'alive': self.fd is not None and not self.closed,
                    'started': self.started, 'bytes': len(self.buf),
                    'terminal_state': self.terminal.metadata()}

    def takeover(self, actor, client_id):
        with self.write_lock:
            self.takeovers.add(actor)
            self.terminal.control(actor, client_id, takeover=True)
            with self.cv:
                self.cv.notify_all()

    def _open_transport(self):
        raise NotImplementedError

    def _receive(self, fd):
        raise NotImplementedError

    def _send(self, fd, data):
        raise NotImplementedError

    def _resize(self, fd):
        raise NotImplementedError

    def _dispose_transport(self, fd):
        raise NotImplementedError

    def _interrupt_transport(self):
        pass


class ArchiveRelocation:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


@contextmanager
def relocate_session_logs(sessions, destination):
    """Pause handles while archives move, preserving partial UTF-8 and lines.

    Caller marks commit immediately after its registry transaction succeeds.
    Before commit, any exception resumes the original directory. Afterwards,
    even cleanup failure resumes the committed destination.
    """
    sessions = sorted({id(s): s for s in sessions if isinstance(s, SessionBase)
                       and os.path.abspath(s._logdir) != os.path.abspath(destination)}.values(),
                      key=lambda session: (session.sid, id(session)))
    relocation = ArchiveRelocation()
    with ExitStack() as stack:
        for session in sessions:
            stack.enter_context(session._log_lock)
        originals = [(session, session._logdir) for session in sessions]
        for session, _ in originals:
            session._release_log_handle()
        try:
            yield relocation
        finally:
            for session, original in originals:
                session._logdir = destination if relocation.committed else original
                session._log_retry_after = 0
                if not session._log_finished:
                    session._open_log(session._logdir)
