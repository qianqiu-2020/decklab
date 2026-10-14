"""Hourly UART round trips, only at a verified idle Linux shell prompt."""
import threading
import time

from .transactions import TransactionError


INTERVAL = 60 * 60
RETRY_BUSY = 60
PROBE_TIMEOUT = 8


class SerialHealth:
    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.connection = self.device.connection_id
            self.due = time.monotonic() + INTERVAL
            self.next_check = time.time() + INTERVAL
            self.running = False
            self.status = 'pending'
            self.reason = ''
            self.last_attempt = 0.0
            self.last_success = 0.0

    def snapshot(self):
        with self.lock:
            return {'status': self.status, 'reason': self.reason,
                    'running': self.running, 'last_attempt': self.last_attempt,
                    'last_success': self.last_success, 'next_check': self.next_check}

    def tick(self):
        """Called by the manager; waiting for a response never blocks capture."""
        d = self.device
        with self.lock:
            if (self.running or time.monotonic() < self.due or d.stop.is_set()
                    or not d.present or d.fd is None or d.capture_paused.is_set()):
                return
            self.running = True
            connection = self.connection
        thread = threading.Thread(target=self._probe, args=(connection,),
                                  name='serial-health-' + d.by_path, daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._complete(connection, 'waiting', '等待空闲控制台', RETRY_BUSY)

    def _idle(self):
        d = self.device
        with d.cv:
            screen = d.terminal.screen.metadata()
            return (d.profile.probe == 'linux' and d.newline
                    and not d.terminal.input_pending and d.terminal.prompt()
                    and not d.visibility.active and screen.get('phase') not in ('firmware', 'application'))

    def _complete(self, connection, status, reason, delay):
        d = self.device
        with self.lock:
            if connection != self.connection or connection != d.connection_id:
                return
            self.running = False
            self.due = time.monotonic() + delay
            self.next_check = time.time() + delay
            # Being busy cannot dismiss a previous failed round trip.
            if status != 'waiting' or self.status != 'error':
                self.status, self.reason = status, reason
            if status == 'ok':
                self.last_success = time.time()

    def _probe(self, connection):
        d = self.device
        token = None
        status, reason, delay = 'waiting', '等待空闲控制台', RETRY_BUSY
        try:
            if not self._idle():
                return
            with d.transactions.begin('health-check', PROBE_TIMEOUT + 2,
                                      actor='seriald/health', expected_connection=connection):
                # Input/ownership can change between scheduling and acquiring the lease.
                if not self._idle():
                    return
                with self.lock:
                    if connection != self.connection:
                        return
                    self.last_attempt = time.time()
                token = d.visibility.begin('health-check')
                d.probing = True
                try:
                    # The shell builtin has no side effects; nonce framing proves
                    # both send and receive through the existing single reader.
                    ok, output = d._run_cmd_locked(':', PROBE_TIMEOUT, ensure_shell=False)
                    d.visibility.report(output, ':')
                    status = 'ok' if ok else 'error'
                    reason = '' if ok else '串口主动探测失败：' + output
                    delay = INTERVAL
                finally:
                    d.probing = False
                    d.visibility.finish(token)
                    token = None
        except TransactionError as error:
            if error.code not in ('busy', 'transfer-busy', 'input-owned', 'stale', 'disconnected', 'cancelled'):
                status, reason, delay = 'error', '串口主动探测失败：' + str(error), INTERVAL
        except Exception as error:
            status, reason, delay = 'error', '串口主动探测失败：' + str(error), INTERVAL
        finally:
            d.visibility.finish(token)
            if (status == 'error' and connection == d.connection_id
                    and not d.stop.is_set() and d.present):
                d._say(reason)
            self._complete(connection, status, reason, delay)
