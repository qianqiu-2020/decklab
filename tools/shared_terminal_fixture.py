"""Disposable local PTYs for browser/Vim checks; no discovery, SSH or hardware."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import tty

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def shell(directory, cols, rows):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    argv = [sys.executable, '-c', 'import fcntl,termios,os; fcntl.ioctl(0,termios.TIOCSCTTY,0); '
            'os.execlp("bash","bash","--noprofile","--norc")']
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, cwd=directory, env=dict(os.environ, TERM='xterm-256color',
            PS1='root@fixture:~# ', HISTFILE='/dev/null'))
    os.close(slave)
    os.set_blocking(master, False)
    return master, process


def ssh_child(directory):
    """A local stand-in for ssh: stdio and SIGWINCH forward to a remote PTY."""
    rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8))
    master, process = shell(directory, cols, rows)
    tty.setraw(0)
    def resize(*_):
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    signal.signal(signal.SIGWINCH, resize)
    try:
        while process.poll() is None:
            for fd in select.select([0, master], [], [], .1)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                target = master if fd == 0 else 1
                while data:
                    data = data[os.write(target, data):]
    finally:
        os.close(master)
        process.terminate()
        process.wait(timeout=3)


def main():
    with tempfile.TemporaryDirectory(prefix='decklab-shared-pty-') as directory:
        os.environ['SERIALD_BASE'] = directory
        os.environ['SERIALD_BY_PATH'] = str(Path(directory) / 'empty-ports')
        Path(directory, 'sample.txt').write_text(''.join('sample line %03d 中文\n' % n for n in range(80)))
        from seriald_core.device import Device
        from seriald_core.devices import Manager
        from seriald_core.storage import Store
        from seriald_core.sessions import SshSession
        from seriald_core.httpd import BoundedThreadingTCPServer
        from seriald_core.web import Handler
        from seriald_core.control_server import ControlHandler

        class LocalSession(SshSession):
            def __init__(self, *args):
                super().__init__(*args)
                # Exercise the real Linux adapter's sshpass/PTy chain. Replace
                # only its network SSH child with a local disposable shell.
                self.argv = ['sshpass', '-e', sys.executable, __file__, '--ssh-child', directory]

        class LocalSerial(Device):
            def _write(self, text):
                with self.cv:
                    self.terminal.input_pending = True
                with self.write_lock:
                    os.write(self.fd, text.encode())
                return True, ''

            def _protocol_reply(self, text, connection=None):
                if connection is not None and connection != self.connection_id:
                    return False
                return self._write_reply(text)

            def _write_reply(self, text):
                with self.write_lock:
                    if self.stop.is_set():
                        return False
                    os.write(self.fd, text.encode())
                    return True

            def reader(self):
                while not self.stop.is_set():
                    if not select.select([self.fd], [], [], .1)[0]:
                        continue
                    try:
                        data = os.read(self.fd, 65536)
                    except (OSError, ValueError):
                        break
                    if not data:
                        break
                    self.append(data)

        store = Store(Path(directory) / 'registry.sqlite3')
        record = store.bind({'serial': 'TEST-SHARED-PTY', 'serial_source': 'device-tree',
            'identity_status': 'confirmed', 'hostname': 'fixture', 'ok': True}, 'fixture-port', '/fake/tty', label='fixture')
        manager = Manager(store)
        board = manager.boards[record['serial']]
        device = LocalSerial('fixture-port', '/fake/tty', manager, board=board)
        device.fd, serial_process = shell(directory, 80, 24)
        device.present = True
        board.attach(device)
        manager.devices[device.by_path] = device
        device.thread = threading.Thread(target=device.reader, daemon=True)
        device.thread.start()
        ssh = LocalSession('ssh-fixture', '192.0.2.1', 'fixture', directory)
        ssh.ensure()
        device.ssh_sessions[ssh.sid] = ssh
        with BoundedThreadingTCPServer(('127.0.0.1', 0), Handler) as collector:
            collector.manager = manager
            ControlHandler.collector_socket = collector.server_address
            ControlHandler.auth_token = ''
            with BoundedThreadingTCPServer(('127.0.0.1', 0), ControlHandler) as control:
                thread = threading.Thread(target=collector.serve_forever, daemon=True)
                thread.start()
                signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=control.shutdown, daemon=True).start())
                print(json.dumps({'url': 'http://127.0.0.1:%s' % control.server_address[1],
                    'geometry_file': str(Path(directory) / 'vim-geometry.txt')}), flush=True)
                try:
                    control.serve_forever()
                finally:
                    manager.close()
                    os.close(device.fd)
                    serial_process.terminate()
                    serial_process.wait(timeout=3)
                    collector.shutdown()
                    thread.join(timeout=3)
                    store.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--ssh-child':
        ssh_child(sys.argv[2])
    else:
        main()
