"""In-memory boards using the real registry, archive, transfer and HTTP layers.

The shell interpreter is deliberately closed: it never executes host commands,
opens hardware, contacts SSH hosts or maps board paths onto host paths.
"""
import base64
import gzip
import hashlib
import os
import re
import secrets
import shlex
import time
from datetime import datetime, timedelta

from .device import Device
from .devices import Manager
from .profiles import ProfileBook
from .config import SSH_LIMIT
from .storage import IdentityError


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class DemoDevice(Device):
    def __init__(self, key, manager, record):
        super().__init__(key, 'demo://' + key, manager, record)
        self.fd = object() if record else None
        self.present = bool(record)
        self.history_loaded = True
        self.newline = '\r'
        self.input_pending = ''
        self.input_cr = False
        self.files = {'/root/log.txt': (b'[    0.000000] Linux version 6.18.0-demo\n'
                                     b'[    1.234000] mmc0: initialized\n') * 512}

    def prompt(self):
        return 'root@%s:~# ' % self.hostname

    def command(self, command):
        if command.startswith('if command -v infocmp '):
            dimensions = re.search(r'stty echoctl(?: -ixon -ixoff)? rows (\d+) cols (\d+)', command)
            if dimensions:
                return '\x1b[8;%s;%st' % dimensions.groups(), 0
        try:
            words = shlex.split(command)
        except ValueError:
            return 'demo: incomplete quoting\n', 2
        if not words:
            return '', 0
        if words == ['uname', '-a']:
            return 'Linux %s 6.18.0-demo #1 SMP riscv64 GNU/Linux\n' % self.hostname, 0
        if words == ['pwd']:
            return '/root\n', 0
        if words[0] == 'echo':
            return ' '.join(words[1:]) + '\n', 0
        if words[0] == 'cat' and len(words) == 2:
            value = self.files.get(words[1])
            return ((value.decode('utf-8', 'replace'), 0) if value is not None
                    else ('cat: file not found\n', 1))
        if words[0] == 'ls':
            return '\n'.join(sorted(self.files)) + '\n', 0
        if words[0] == 'dmesg':
            return '[    0.000000] Linux version 6.18.0-demo\n[    1.234000] mmc0: initialized\n', 0
        if words == ['reboot']:
            return '\nU-Boot 2026.01 (DeckLab demo)\nLinux version 6.18.0-demo\nBoot completed\n', 0
        if words == ['true']:
            return '', 0
        if words == ['false']:
            return '', 1
        return ('DeckLab simulated shell. Commands: uname -a, pwd, echo, ls, cat, '
                'dmesg, reboot, true, false. Files live only in demo memory.\n'), 127

    def _write(self, text):
        with self.write_lock:
            return self._write_input(text)

    def _write_input(self, text):
        if self.stop.is_set() or not self.present or self.fd is None:
            return False, 'Demo board is offline'
        echo = []
        for char in text:
            if char == '\n' and self.input_cr:
                self.input_cr = False
                continue
            self.input_cr = char == '\r'
            if char in ('\x08', '\x7f'):
                if self.input_pending:
                    self.input_pending = self.input_pending[:-1]
                    echo.append('\b \b')
                continue
            if char == '\x03':
                self.input_pending = ''
                echo.append('^C\r\n' + self.prompt())
                continue
            if char not in ('\r', '\n'):
                if char >= ' ':
                    self.input_pending += char
                    echo.append(char)
                continue
            line, self.input_pending = self.input_pending, ''
            echo.append('\r\n')
            # serialctl's child-shell frame is interpreted, never executed.
            if '__SD_AGENT_B_' in line:
                try:
                    words = shlex.split(line)
                    nonce = words[3].rstrip(';')
                    command = words[words.index('-c') + 1]
                    output, status = self.command(command)
                    output = '\n__SD_AGENT_B_%s\n%s\n__SD_AGENT_E_%s:%d\n' % (
                        nonce, output, nonce, status)
                except (ValueError, IndexError):
                    output = 'demo: unsupported command frame\n'
            else:
                output, _ = self.command(line)
            echo.append((output + self.prompt()).replace('\n', '\r\n'))
        if echo:
            self.append(''.join(echo))
        return True, ''

    def run_probe(self, init_boot_marker=False):
        if not self.present:
            return {'ok': False, 'error': 'Demo board is offline'}
        return dict(self.probe_info, ok=True)

    def configure_root_ssh(self):
        return False, 'Demo does not change passwords or sshd configuration'

    def _run_cmd_locked(self, command, timeout, ensure_shell=True):
        """Emulate only the fixed transfer protocol's board-side responses."""
        state = self.file_transfer.state
        words = shlex.split(command)
        if command.startswith('umask 077;'):
            paths = [word.rstrip(';') for word in words if word.startswith('/')]
            target, temporary = paths[0], paths[-1].rstrip(')')
            if target in self.files and 'false; then' not in command:
                return True, 'SD_ERROR=exists\n'
            self.files[temporary] = b''
            return True, 'SD_OK=1\nSD_GZIP=1\n'
        if command.startswith('rm -f '):
            self.files.pop(words[2], None)
            return True, ''
        if 'base64 -d' in command and state:
            temp = state['temp']
            for encoded, compressed in re.findall(
                    r'printf %s ([A-Za-z0-9+/=]+) \| base64 -d( \| gzip -d)?', command):
                data = base64.b64decode(encoded, validate=True)
                self.files[temp] += gzip.decompress(data) if compressed else data
            return True, 'SD_RC=0\nSD_SIZE=%d\n' % len(self.files[temp])
        if 'SD_DATA=' in command and state:
            data = self.files.get(state['path'], b'')[state['offset']:state['offset'] + 10240]
            if state['compress']:
                data = gzip.compress(data, mtime=0)
            return True, 'SD_DATA=' + base64.b64encode(data).decode() + '\n'
        if 'SD_ERROR=checksum' in command and state:
            data = self.files[state['temp']]
            if len(data) != state['size'] or _digest(data) != state['hasher'].hexdigest():
                return True, 'SD_ERROR=checksum\n'
            if state['path'] in self.files and not state['overwrite']:
                return True, 'SD_ERROR=promote\n'
            self.files[state['path']] = self.files.pop(state['temp'])
            return True, 'SD_OK=1\n'
        if 'SD_SIZE=' in command and 'sha256sum' in command:
            match = re.search(r'\[ -f (.*?) \]', command)
            path = (shlex.split(match.group(1))[0] if match else
                    state['path'] if state else '')
            data = self.files.get(path)
            if data is None:
                return True, 'SD_ERROR=unreadable\n'
            return True, 'SD_SIZE=%d\nSD_SHA=%s\nSD_GZIP=1\n' % (len(data), _digest(data))
        return False, 'Unsupported demo protocol command'

    def ssh_connect(self, actor='web', title='', ssh_user=None, ssh_password=None):
        if not self.present:
            return None, 'Demo board is offline'
        self.reap_sessions()
        with self.sessions_lock:
            if actor != 'web':
                for session in self.ssh_sessions.values():
                    if session.actor == actor and not session.closed:
                        return session, ''
            if len(self.ssh_sessions) >= SSH_LIMIT:
                return None, 'Demo SSH session limit reached; close an existing session'
            session = DemoSession(self, actor, title)
            self.ssh_sessions[session.sid] = session
            return session, ''


class DemoSession(DemoDevice):
    def __init__(self, board, actor, title):
        self.sid = 'ssh' + secrets.token_hex(12)
        super().__init__(board.by_path, board.manager, board.record)
        self.bind_board(board.board)
        self.files = board.files
        self.actor, self.title = actor, title
        self.ip = '192.0.2.10'
        self.cols, self.rows = 80, 24
        self.closed = False
        self._stop = self.stop
        self.persistent = actor != 'web'
        self.takeovers = set()
        self.last_touch = time.monotonic()
        self.auto_configured = False
        self.manager.store.open_session(self.sid, self.serial, actor, title, self.ip)
        self.append('DeckLab simulated SSH session\r\n' + self.prompt())

    def open_log(self):
        path = os.path.join(self.stable_dir(), 'ssh-%s-%s.log' % (self.sid, time.strftime('%Y%m%d')))
        if self.logf and self.logpath == path:
            return
        if self.logf:
            self.logf.close()
        self.logf = open(path, 'a', encoding='utf-8', buffering=1)
        self.logpath = path

    def write(self, text):
        self.last_touch = time.monotonic()
        return self._write(text)[0]

    def resize(self, cols, rows):
        if self.closed or not 2 <= cols <= 500 or not 2 <= rows <= 500:
            return False
        self.cols, self.rows = cols, rows
        return True

    def is_idle(self, now, ttl):
        return not self.persistent and now - self.last_touch > ttl

    def append_marker(self, text):
        self.append(text + '\r\n')

    def close(self):
        if self.fd is None:
            return
        self.closed = True
        self.stop.set()
        self.fd = None
        self.close_log()
        self.manager.store.close_session(self.sid)
        with self.cv:
            self.cv.notify_all()

    def snapshot(self):
        return {'sid': self.sid, 'kind': 'ssh', 'actor': self.actor, 'title': self.title,
                'ip': self.ip, 'alive': not self.closed, 'started': self.started,
                'cols': self.cols, 'rows': self.rows, 'persistent': self.persistent,
                'takeovers': sorted(self.takeovers), 'auto_configured': False,
                'log_error': self.log_error}


class DemoManager(Manager):
    def __init__(self, store):
        super().__init__(store, profiles=ProfileBook({'profiles': {
            'demo': {'auto_configure_ssh': False}}, 'default': 'demo'}))
        for index, label in enumerate(('demo-k1', 'demo-k3', 'demo-offline')):
            info = {'serial': 'DEMO-%03d' % (index + 1), 'hostname': label,
                    'serial_source': 'verified-manual', 'identity_status': 'confirmed',
                    'ips': ['eth0 192.0.2.%d/24' % (10 + index)], 'sshd': 'active',
                    'model': 'Simulated development board'}
            record = store.bind(info, 'demo-usb-%d' % index, 'demo://%d' % index, label)
            device = DemoDevice('demo-usb-%d' % index, self, record)
            self.devices[device.by_path] = device
            device.board.attach(device)
            self.boards[device.serial] = device.board
            self._seed_logs(device, index)
            if index == 2:
                device.present = False
                device.fd = None
            else:
                device.append('DeckLab demo: no real hardware or shell execution\r\n' + device.prompt())

    @staticmethod
    def _seed_logs(device, index):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for delta, lines in ((-1, [('10:00:00', 'U-Boot 2026.01'),
                                  ('10:00:01', 'Linux version 6.17.0-demo'),
                                  ('10:00:03', 'Boot completed'),
                                  ('23:59:58', 'U-Boot 2026.01')]),
                             (0, [('00:00:01', 'Linux version 6.18.0-demo'),
                                  ('00:00:02', 'mmc0: timeout' if index == 1 else 'mmc0: initialized'),
                                  ('00:00:04', 'Boot completed')])):
            date = today + timedelta(days=delta)
            with open(os.path.join(device.stable_dir(), 'serial-%s.log' % date.strftime('%Y%m%d')),
                      'w', encoding='utf-8') as log:
                for clock, line in lines:
                    log.write('%s %s  %s\n' % (date.strftime('%Y-%m-%d'), clock, line))

    def scan(self):
        return {}  # No discovery, even if accidentally called by a background loop.

    def prepare_switch(self, device, actor='web'):
        raise IdentityError('Demo has fixed simulated boards; physical replacement is not emulated')

    def _matches(self, device):
        return False  # Hardware recovery is never permitted for simulated ports.

    def close(self):
        result = super().close()
        for device in self.all_devices():
            device.fd = None
        return result
