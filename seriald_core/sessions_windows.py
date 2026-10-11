"""Paramiko SSH channels with the same logged byte-stream API as Linux PTYs."""
import paramiko

from .session_base import SessionBase


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


class SshSession(SessionBase):
    """Windows IO adapter: Paramiko client/channel and remote PTY resizing."""
    transport_errors = (OSError, RuntimeError, paramiko.SSHException)
    join_timeout = 3

    def __init__(self, sid, ip, label, logdir, user=None, password=None):
        super().__init__(sid, ip, label, logdir, user, password)
        self.client = None
        self._interrupted_channel = None

    def _open_transport(self):
        self.client = _connect(self.ip, self.user, self.password)
        channel = self.client.invoke_shell(term='xterm-256color', width=self.cols, height=self.rows)
        self.fd = channel  # Cleanup owns it even if setting the timeout fails.
        channel.settimeout(0.25)
        return channel

    def _receive(self, fd):
        return fd.recv(65536)

    def _send(self, fd, data):
        fd.sendall(data)

    def _resize(self, fd):
        fd.resize_pty(width=self.cols, height=self.rows)

    def _interrupt_transport(self):
        channel = self.fd
        if channel is not None:
            self._interrupted_channel = channel
            try:
                channel.close()
            except BaseException:
                self._interrupted_channel = None
                raise

    def _dispose_transport(self, fd):
        client, self.client = self.client, None
        try:
            if fd is not None and fd is not self._interrupted_channel:
                fd.close()
        finally:
            if client is not None:
                client.close()
