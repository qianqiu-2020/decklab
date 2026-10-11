"""Single-console transaction leases: exclusion, deadline and generation guards."""
from contextlib import contextmanager
import math
import secrets
import threading
import time


class TransactionError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


class ConsoleLease:
    def __init__(self, device, kind, timeout, actor, require_open):
        self.device = device
        self.id = secrets.token_hex(12)
        self.kind, self.actor = kind, actor
        self.connection_id, self.stop = device.connection_id, device.stop
        self.started = time.time()
        self.start_clock = time.monotonic()
        self.deadline = self.start_clock + timeout
        self.require_open = require_open
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise TransactionError('cancelled', '控制台操作已取消')
        if self.device.connection_id != self.connection_id or self.device.stop is not self.stop:
            raise TransactionError('stale', '设备连接已变化，取消旧操作')
        if self.require_open and (self.stop.is_set() or self.device.fd is None):
            raise TransactionError('disconnected', '串口连接已停止')
        if time.monotonic() >= self.deadline:
            raise TransactionError('timeout', '控制台操作超时，可能已执行部分命令', 504)

    def is_set(self):
        try:
            self.check()
            return False
        except TransactionError:
            return True

    def snapshot(self):
        return {'id': self.id, 'kind': self.kind, 'actor': self.actor,
                'connection_id': self.connection_id, 'started': self.started,
                'remaining': max(0, self.deadline - time.monotonic()),
                'cancelled': self.cancelled.is_set()}


class SerialTransactions:
    def __init__(self, device, lock):
        self.device, self.lock = device, lock
        self._state_lock = threading.Lock()
        self._local = threading.local()
        self._active = None

    @property
    def current(self):
        return getattr(self._local, 'lease', None)

    @contextmanager
    def actor_scope(self, actor):
        previous = getattr(self._local, 'actor', 'service')
        self._local.actor = actor
        try:
            yield
        finally:
            self._local.actor = previous

    @contextmanager
    def begin(self, kind, timeout, actor=None, allow_transfer=False,
              expected_connection=None, require_open=True):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('transaction timeout must be finite and positive')
        if not self.lock.acquire(blocking=False):
            raise TransactionError('busy', '已有控制台操作在进行中，稍后重试')
        lease = None
        try:
            if expected_connection is not None and expected_connection != self.device.connection_id:
                raise TransactionError('stale', '设备连接已变化，取消旧操作')
            if not allow_transfer and self.device.file_transfer.active:
                raise TransactionError('transfer-busy', '串口文件传输中，请完成或取消后再操作')
            lease = ConsoleLease(self.device, kind, timeout,
                                 actor if actor is not None else getattr(self._local, 'actor', 'service'),
                                 require_open)
            lease.check()
            with self._state_lock:
                self._active = lease
            self._local.lease = lease
            yield lease
            lease.check()
        finally:
            self._local.lease = None
            if lease is not None:
                with self._state_lock:
                    if self._active is lease:
                        self._active = None
            self.lock.release()

    def snapshot(self):
        with self._state_lock:
            return self._active.snapshot() if self._active else None

    def cancel(self, transaction_id, actor='web'):
        with self._state_lock:
            lease = self._active
            if lease is None or lease.id != transaction_id:
                raise TransactionError('not-found', '控制台操作已结束或已被其他操作替代', 404)
            if actor != 'web' and actor != lease.actor:
                raise TransactionError('forbidden', '只能取消自己发起的控制台操作', 403)
            lease.cancelled.set()
        cv = getattr(self.device, 'cv', None)
        if cv is not None:
            with cv:
                cv.notify_all()


def transactions_for(device):
    coordinator = getattr(device, 'transactions', None)
    if not isinstance(coordinator, SerialTransactions):
        coordinator = SerialTransactions(device, device.operation_lock)
        device.transactions = coordinator
    return coordinator
