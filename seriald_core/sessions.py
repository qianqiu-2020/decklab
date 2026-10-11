"""Independent SSH PTYs, byte streams, and session logs."""
import fcntl
import errno
import os
import pty
import re
import select
import struct
import subprocess
import termios

from .session_base import SessionBase
from .transport import write_all


def check_password_login(ip, user, password):
    """Return (authenticated, denied, detail) without opening a persistent PTY."""
    argv = ['sshpass', '-e', 'ssh', '-T', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'ConnectTimeout=6', '-o', 'ConnectionAttempts=1',
            '-o', 'NumberOfPasswordPrompts=1', '-o', 'PubkeyAuthentication=no',
            '-o', 'PreferredAuthentications=password,keyboard-interactive',
            user + '@' + ip, 'true']
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=12, check=False,
                                env=dict(os.environ, SSHPASS=password))
    except subprocess.TimeoutExpired:
        return False, False, 'SSH 认证探测超时'
    except OSError as error:
        return False, False, 'SSH 认证探测失败: %s' % error
    if result.returncode == 0:
        return True, False, ''
    detail = result.stderr.decode('utf-8', 'replace')[-500:]
    denied = result.returncode == 5 or bool(re.search(r'Permission denied|Authentication failed', detail, re.I))
    return False, denied, 'SSH 认证失败' if denied else (detail.strip() or 'SSH 连接失败')

class SshSession(SessionBase):
    """Linux IO adapter: PTY, ssh subprocess group and termios resizing."""
    def __init__(self, sid, ip, label, logdir, user=None, password=None):
        super().__init__(sid, ip, label, logdir, user, password)
        self.proc = None
        self.argv = ['sshpass', '-e', 'ssh', '-tt',
                     '-o', 'StrictHostKeyChecking=no',
                     '-o', 'UserKnownHostsFile=/dev/null',
                     '-o', 'GlobalKnownHostsFile=/dev/null',
                     '-o', 'ConnectTimeout=10',
                     '-o', 'ServerAliveInterval=30', self.user + '@' + ip]

    def _open_transport(self):
        master, slave = pty.openpty()
        try:
            os.set_blocking(master, False)
            self._set_winsize(slave)
            self.proc = subprocess.Popen(
                self.argv, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, env=dict(os.environ, TERM='xterm-256color',
                                                 SSHPASS=self.password), close_fds=True)
            return master
        except BaseException:
            try:
                os.close(master)
            except OSError:
                pass
            raise
        finally:
            try:
                os.close(slave)
            except OSError:
                pass

    def _set_winsize(self, fd):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def _resize(self, fd):
        self._set_winsize(fd)

    def _receive(self, fd):
        ready, _, _ = select.select([fd], [], [], 0.25)
        if not ready:
            return None
        try:
            return os.read(fd, 65536)
        except OSError as error:
            if error.errno == errno.EIO:  # Linux PTYs report normal child EOF as EIO.
                return b''
            raise

    def _send(self, fd, data):
        write_all(fd, data, cancel=self._stop)

    def _dispose_transport(self, fd):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        proc, self.proc = self.proc, None
        if proc is None:
            return
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
            # The group leader may exit while its ssh child is still alive.
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
