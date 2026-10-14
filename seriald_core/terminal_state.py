"""Shared UART display geometry and idle-shell setup; no extra serial reader."""
import re

from .config import ANSI
from .transactions import TransactionError


class SerialTerminalState:
    def __init__(self, device):
        self.device = device
        self.tail = b''
        self.geometry = None
        self.authority = None
        self.attempted = False
        self.input_pending = False

    def observe(self, data):
        joined = self.tail + data
        previous = len(self.tail)
        pattern = rb'\x1b\[8;(\d{1,3});(\d{1,3})t|(?:^|[\r\n])(?:\[[^\r\n]{1,40}\]\s*)?Linux version \d|\x00sys:\s*0x[0-9a-f]+|(?:^|[\r\n])U-Boot(?: SPL)? \d{4}\.\d{2}'
        for match in re.finditer(pattern, joined):
            if match.end() <= previous:
                continue
            if match[1]:
                rows, cols = int(match[1]), int(match[2])
                if 2 <= rows <= 200 and 20 <= cols <= 500:
                    self.geometry = {'rows': rows, 'cols': cols}
            else:
                self.geometry = None
                self.authority = None
                self.attempted = False
        self.tail = joined[-256:]
        if self.prompt():
            self.input_pending = False

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
                target = {'cols': cols, 'rows': rows}
                if self.geometry == target and self.attempted:
                    return {'ok': True, 'skipped': True, 'geometry': self.geometry}
                self.authority = client
                self.attempted = True
                self.input_pending = True
            # Keep baud/parity/raw modes untouched. TERM is changed only if the
            # board actually has the matching terminfo. No extra Enter/probe.
            flow = ' -ixon -ixoff' if d.profile.serial.flow_control != 'xonxoff' else ''
            command = ("if command -v infocmp >/dev/null 2>&1 && infocmp xterm-256color >/dev/null 2>&1; "
                       "then export TERM=xterm-256color; fi; "
                       "case ${{LC_ALL:-${{LC_CTYPE:-$LANG}}}} in *UTF-8*|*utf8*|*UTF8*) stty iutf8;; esac; "
                       "stty echoctl{flow} rows {rows} cols {cols} && "
                       "printf '\\033[8;{rows};{cols}t'").format(flow=flow, rows=rows, cols=cols)
            ok, error = d._write(command + (d.newline or '\r'))
            if ok:
                d.record_input('serial', actor, command + '\r')
            return {'ok': ok, 'error': error, 'pending': ok}
