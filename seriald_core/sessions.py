"""Independent SSH PTYs, byte streams, and session logs."""
import codecs
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import threading
import time

from .config import ANSI, DATE_FMT, SCROLLBACK
from .transport import write_all

class SshSession:
    """一条 ssh 到板子的会话(pty)。

    和串口那条链路并存但互不干扰:串口走"单读者采集",这里走独立 pty,
    各读各的。输出按偏移给多读者取增量,同时落盘成可读日志 ——
    维护调试板子的核心诉求就是"操作过程要有记录"。
    """

    def __init__(self, sid, ip, label, logdir):
        self.sid = sid
        self.actor = 'web'
        self.title = ''
        # Agent sessions are deliberately kept alive and reused by actor.  The
        # Device owns the policy; this flag is exposed in snapshots so clients
        # can explain why the tab remains present.
        self.persistent = False
        self.store = None
        self.ip = ip
        self.label = label
        self.buf = bytearray()
        self.base = 0
        self.cv = threading.Condition()
        self.write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stop = threading.Event()
        self._disposed = threading.Event()
        self._thread = None
        self._started_once = False
        self.closed = False
        self.fd = None
        self.proc = None
        self.started = time.time()
        self.last_touch = time.monotonic()
        self.cols = 120                   # 默认尺寸;浏览器连上后会按实际宽度覆盖
        self.rows = 32
        self.logf = None
        self.logpath = None
        self._logdir = logdir
        self._logday = time.strftime(DATE_FMT)
        self._log_decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._log_pending = ''
        self._log_finished = False
        self.argv = ['sshpass', '-e', 'ssh', '-tt',
                     '-o', 'StrictHostKeyChecking=no',
                     '-o', 'UserKnownHostsFile=/dev/null',
                     '-o', 'GlobalKnownHostsFile=/dev/null',
                     '-o', 'ConnectTimeout=10',
                     '-o', 'ServerAliveInterval=30', os.environ.get('SERIALD_SSH_USER', 'root') + '@' + ip]
        self._open_log(logdir)

    def _open_log(self, logdir):
        try:
            day = time.strftime(DATE_FMT)
            path = os.path.join(logdir, 'ssh-%s-%s.log' % (self.sid, day))
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath = path
            self._logday = day
        except OSError:
            self.logf = None

    def _log(self, data, final=False):
        with self._log_lock:
            if self.logf and not self._log_finished and self._logday != time.strftime(DATE_FMT):
                # SSH 会话可能跨天,不能一直把后续输出写进连接当天的文件。
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None
                self._open_log(self._logdir)
            if not self.logf or self._log_finished:
                return
            # read() 可以把一个汉字或一行切开,逐块 decode 会写出乱码和伪换行。
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

    def _close_log(self):
        self._log(b'', final=True)
        with self._log_lock:
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None

    def ensure(self):
        with self._lifecycle_lock:
            with self.cv:
                # 旧 sid 曾在写入时偷偷重建连接;结束的会话只能由显式新建替代。
                if self.closed or self._started_once:
                    return self.fd is not None and not self.closed
                self._started_once = True
            master = slave = proc = None
            try:
                master, slave = pty.openpty()
                os.set_blocking(master, False)
                self._set_winsize(slave)  # SSH 初次请求远端 pty 时就使用浏览器尺寸。
                proc = subprocess.Popen(
                    self.argv, stdin=slave, stdout=slave, stderr=slave,
                    # 多线程进程里 preexec_fn 可能继承已锁住的锁,导致子进程死锁。
                    start_new_session=True, env=dict(os.environ, TERM='xterm-256color',
                                             SSHPASS=os.environ.get('SERIALD_SSH_PASSWORD', 'bianbu')),
                    close_fds=True)
                with self.cv:
                    self.fd, self.proc = master, proc
                    self.last_touch = time.monotonic()
                self._append('\r\n*** 已建立 SSH 会话 %s@%s (%dx%d) ***\r\n'
                             % (os.environ.get('SERIALD_SSH_USER', 'root'), self.ip, self.cols, self.rows))
                self._thread = threading.Thread(target=self._reader, args=(master, proc),
                                                daemon=True)
                self._thread.start()
            except (OSError, RuntimeError) as e:
                self._append('\r\n*** 建立 SSH 会话失败: %s ***\r\n' % e)
                self._thread = None
                self._finish(master, proc)
                return False
            finally:
                if slave is not None:
                    os.close(slave)
            return True

    def _set_winsize(self, fd):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def resize(self, cols, rows):
        """把浏览器终端尺寸同步到 pty。

        不同步的话远端以为自己是 80x24,宽度用不满,vi/htop 也会按错误列数画。
        SSH 收到 window-change 会转发给远端。
        """
        if cols < 20 or rows < 5 or cols > 500 or rows > 200:
            return False
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.cols, self.rows = cols, rows
                self.last_touch = time.monotonic()
                fd = self.fd
            self._set_winsize(fd)
        return True

    def _reader(self, fd, proc):
        try:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                    if not ready:
                        continue
                    data = os.read(fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                self._append(data)
        finally:
            try:
                self._append('\r\n*** SSH 会话已结束 ***\r\n')
            finally:
                self._finish(fd, proc)

    def _finish(self, fd, proc):
        self._stop.set()
        # fd 只由读线程回收;和写入互斥,否则 fd 号码复用后可能写进别的会话。
        with self.write_lock:
            with self.cv:
                self.closed = True
                self.fd = None
                self.cv.notify_all()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if proc is not None:
            # sshpass/ssh 属于同一进程组;只发 TERM 而不 wait 曾遗留子进程和僵尸。
            try:
                os.killpg(proc.pid, 15)
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
            else:
                # 组长可能已退出而 ssh 仍存活;回收整组,不只清理直接子进程。
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
        self._close_log()
        self._disposed.set()
        if self.store:
            self.store.close_session(self.sid)

    def _append(self, data):
        b = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        self._log(b)
        with self.cv:
            self.buf += b
            if len(self.buf) > SCROLLBACK:
                drop = len(self.buf) - SCROLLBACK
                del self.buf[:drop]
                self.base += drop
            self.cv.notify_all()

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
            return (self.base + len(self.buf), bytes(self.buf[start:]),
                    self.fd is not None and not self.closed)

    def write(self, text):
        # 慢远端会使写入等待可写;不能持有输出 cv,否则读者也一起停住。
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.last_touch = time.monotonic()
                fd = self.fd
            try:
                write_all(fd, text.encode('utf-8', 'replace'), cancel=self._stop)
            except OSError:
                return False
            return True

    def close(self):
        with self._lifecycle_lock:
            if self._disposed.is_set():
                return
            with self.cv:
                self.closed = True
                self.cv.notify_all()
            self._stop.set()
            self.append_marker('*** 会话已关闭 ***')
            if self._thread is not None:
                # select 最多等待 250ms;写入由 stop 中断,不能从这里抢关读者的 fd。
                self._thread.join()
            else:
                self._finish(self.fd, self.proc)

    def is_idle(self, now, ttl):
        with self.cv:
            return self.closed or now - self.last_touch >= ttl

    def append_marker(self, text):
        self._append('\r\n%s\r\n' % text)

    def snapshot(self):
        with self.cv:
            return {'sid': self.sid, 'kind': 'ssh', 'ip': self.ip,
                    'actor': self.actor, 'title': self.title,
                    'persistent': self.persistent,
                    'alive': self.fd is not None and not self.closed,
                    'started': self.started, 'bytes': len(self.buf)}
