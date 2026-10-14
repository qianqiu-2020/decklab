"""Shared UART display geometry and idle-shell setup; no extra serial reader."""
import re

from .config import ANSI
from .transactions import TransactionError
from .terminal_backend import SharedTerminal


class SerialTerminalState:
    def __init__(self, device):
        self.device = device
        self.screen = SharedTerminal('serial')
        self.tail = b''
        self.geometry = None
        self.history = []
        self.authority = None
        self.attempted = False
        self.environment_pending = False
        self.input_pending = False

    def observe(self, data):
        joined = self.tail + data
        previous = len(self.tail)
        start = self.device.base + len(self.device.buf)
        pattern = rb'\x1b\[8;(\d{1,3});(\d{1,3})t|(?:^|[\r\n])(?:\[[^\r\n]{1,40}\]\s*)?Linux version \d|(?:^|[\r\n])\[\s*0\.000000\] |\x00sys:\s*0x[0-9a-f]+|(?:^|[\r\n])U-Boot(?: SPL)? \d{4}\.\d{2}'
        for match in re.finditer(pattern, joined):
            if match.end() <= previous:
                continue
            if match[1]:
                rows, cols = int(match[1]), int(match[2])
                if 2 <= rows <= 200 and 20 <= cols <= 500:
                    self.geometry = {'rows': rows, 'cols': cols}
                    self.history.append((start + match.end() - previous, self.geometry))
            else:
                self.geometry = None
                self.history.append((start + match.end() - previous, None))
                self.authority = None
                self.attempted = False
                self.environment_pending = False
        self.tail = joined[-256:]
        if self.prompt():
            self.input_pending = False

    def geometry_at(self, offset):
        geometry = None
        for position, value in self.history:
            if position > offset:
                break
            geometry = value
        return geometry

    def prune(self, base):
        # Retain the state immediately before the first captured byte, plus all
        # changes inside the buffer. Every viewer can replay the same timeline.
        prior = -1
        for index, (position, _) in enumerate(self.history):
            if position > base:
                break
            prior = index
        if prior > 0:
            self.history = self.history[prior:]

    def prompt(self):
        tail = ANSI.sub('', self.tail.decode('utf-8', 'replace')).split('\n')[-1].split('\r')[-1]
        return bool(re.fullmatch(r'(?:[^\s@]+@[^\s]+[#$]|\[[^\]\r\n]*@[^\]\r\n]+\][#$])\s*', tail))

    def configure(self, cols, rows, observed_off, automatic):
        d = self.device
        with d.transactions.begin('terminal-setup', 3) as lease:
            actor, client = lease.actor, (lease.actor, lease.client_id)
            with d.cv:
                if (d.profile.probe != 'linux' or observed_off != d.base + len(d.buf)
                        or self.input_pending or not self.prompt()):
                    raise TransactionError('not-idle', '等待新的空闲 Linux shell 提示符')
                owner = d.transactions.ownership()
                if owner and owner['actor'] == actor and owner.get('client_id', '') == lease.client_id:
                    self.authority = client
                if automatic and (self.authority not in (None, client)
                                  or self.attempted and self.geometry is None):
                    return {'ok': True, 'skipped': True, 'geometry': self.geometry}
                if not self.screen.control(actor, lease.client_id, takeover=not automatic):
                    return {'ok': True, 'skipped': True, 'geometry': self.geometry}
                target = {'cols': cols, 'rows': rows}
                if automatic and self.geometry == target and self.attempted and not self.environment_pending:
                    return {'ok': True, 'skipped': True, 'geometry': self.geometry}
                if not self.attempted or not automatic:
                    self.environment_pending = True
                # Only echo the longer command after stty's marker and a fresh
                # idle prompt. Bulk typeahead can echo it using the old width.
                setup_environment = (automatic and self.attempted
                                     and self.geometry == target and self.environment_pending)
                if setup_environment:
                    self.environment_pending = False
                self.authority = client
                self.attempted = True
                self.input_pending = True
                if self.geometry is None:
                    self.history.append((d.base + len(d.buf), {'cols': 80, 'rows': 24}))
                    self.screen.resize(80, 24)
                self.screen.control(actor, lease.client_id, takeover=True)
            # Keep baud/parity/raw modes untouched. TERM is changed only if the
            # board actually has the matching terminfo. No extra Enter/probe.
            flow = ' -ixon -ixoff' if d.profile.serial.flow_control != 'xonxoff' else ''
            newline = d.newline or '\r'
            resize = ("stty echoctl{flow} rows {rows} cols {cols} && "
                      "printf '\\033[8;{rows};{cols}t'").format(flow=flow, rows=rows, cols=cols)
            environment = ("if command -v infocmp >/dev/null 2>&1 && infocmp xterm-256color >/dev/null 2>&1; "
                           "then export TERM=xterm-256color; fi; "
                           "case ${LC_ALL:-${LC_CTYPE:-$LANG}} in *UTF-8*|*utf8*|*UTF8*) stty iutf8;; esac")
            command = environment if setup_environment else resize
            token = d.visibility.begin('terminal-setup', command, timeout=3)
            try:
                ok, error = d._write(command + newline)
            except Exception:
                d.visibility.finish(token)
                raise
            if not ok:
                d.visibility.finish(token)
            return {'ok': ok, 'error': error, 'pending': ok}

    def presentation(self, cols, rows):
        """Use the available width for boot text without typing into a board."""
        d = self.device
        actor, client = d.transactions.source_actor, d.transactions.client_id
        with d.cv:
            state = self.screen.metadata()
            if (state.get('phase') != 'text' or state.get('internal_output') or self.input_pending or self.geometry
                    or not self.screen.control(actor, client)):
                return {'ok': True, 'skipped': True}
            ok = self.screen.resize(cols, rows)
            d.cv.notify_all()
        return {'ok': ok, 'terminal_state': self.screen.metadata()}
