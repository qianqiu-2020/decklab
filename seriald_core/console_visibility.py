"""Keep internal UART commands private without changing the protocol byte stream."""
from collections import deque
import re
import secrets
import threading

from .config import ANSI, SCROLLBACK


PROMPT = re.compile(r'^(?:[^\s@]+@[^\s]+[#$]|\[[^\]\r\n]*@[^\]\r\n]+\][#$])\s*$')
SYSTEM_OUTPUT = re.compile(rb'\[\s*\d+\.\d{6}\]|Kernel panic|Oops:|BUG: |Linux version |U-Boot(?: SPL)? \d{4}\.\d{2}|\x00sys:')


def human_output(output, command):
    """Drop only known helper echoes/fields; preserve unexpected diagnostic lines."""
    expected = [line.strip() for line in command.splitlines() if line.strip()]
    text = ANSI.sub('', output).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    lines = []
    for line in text.splitlines():
        value = line.strip()
        if not value or PROMPT.fullmatch(value):
            continue
        if any(value.endswith(echo) for echo in expected):
            continue
        if re.fullmatch(r'__SD_[BE]_[0-9a-f]{24}__', value):
            continue
        field = re.fullmatch(r'SD_(DATA|RC|SIZE|SHA|GZIP|OK|ERROR)=(.*)', value)
        if field:
            if field[1] == 'ERROR' or field[1] == 'RC' and field[2] != '0':
                lines.append('[DeckLab] ' + value)
            continue
        lines.append(line)
    return '\n'.join(lines)


class ConsoleVisibility:
    """A checkpoint belongs to one connection; other UARTs and SSH keep running."""
    def __init__(self, device):
        self.device = device
        self.token = None
        self.timer = None
        self.screen = None
        self.connection = None
        self.kind = ''
        self.command = ''
        self.messages = deque()
        self.message_bytes = 0
        self.tail = b''

    @property
    def active(self):
        if (not self.token or self.connection != self.device.connection_id
                or self.screen is not self.device.terminal.screen or self.screen.closed):
            return False
        state = self.screen.metadata()
        return bool(state.get('managed') and state.get('internal_output'))

    def begin(self, kind, command='', timeout=None):
        d = self.device
        with d.log_lock, d.cv:
            if self.active:
                return None
            self.finish(self.token, discard=True)
            screen = d.terminal.screen
            if not screen.begin_internal():
                return None
            self.token = secrets.token_hex(12)
            self.screen, self.connection = screen, d.connection_id
            self.kind, self.command = kind, command
            self.start = d.base + len(d.buf)
            self.messages.clear()
            self.message_bytes = 0
            self.tail = b''
            if timeout:
                self.timer = threading.Timer(timeout, self.finish, args=(self.token,))
                self.timer.daemon = True
                self.timer.start()
            d.cv.notify_all()
            return self.token

    def report(self, output, command):
        d = self.device
        with d.log_lock, d.cv:
            if not self.active:
                return
            text = human_output(output, command)
            if not text:
                return
            d.log.text(text)
            self.messages.append(text)
            self.message_bytes += len(text.encode('utf-8'))
            while self.message_bytes > SCROLLBACK and len(self.messages) > 1:
                self.message_bytes -= len(self.messages.popleft().encode('utf-8'))

    def interrupt_for_system_output(self, data):
        if not self.active:
            return
        previous = self.tail
        joined = previous + data
        self.tail = joined[-128:]
        match = SYSTEM_OUTPUT.search(joined)
        if match:
            self.finish(self.token, discard=True)
            if match.start() < len(previous):
                # A kernel/boot header can straddle two collector reads. Its
                # first fragment was private; restore it before logging the rest.
                self.device.log.display(previous[match.start():].decode('utf-8', 'replace'))

    def finish(self, token, discard=False):
        if token is None:
            return
        d = self.device
        with d.log_lock, d.cv:
            if token != self.token:
                return
            if self.timer:
                self.timer.cancel()
                self.timer = None
            same = self.connection == d.connection_id and self.screen is d.terminal.screen
            if same and self.active and self.kind == 'terminal-setup' and not discard:
                self.report(d._since(self.start), self.command)
            prompt = self.screen.end_internal('\n'.join(self.messages), discard=discard or not same)
            if same and not self.screen.error:
                # Replace the uncommitted old prompt; decoder continuity remains raw.
                d.log.display('\r' + prompt)
            self.token = None
            self.messages.clear()
            self.message_bytes = 0
            d.cv.notify_all()
