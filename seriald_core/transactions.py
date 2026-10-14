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
    def __init__(self, device, kind, timeout, actor, require_open, client_id=''):
        self.device = device
        self.id = secrets.token_hex(12)
        self.kind, self.actor = kind, actor
        self.client_id = client_id if actor == 'web' else ''
        self.connection_id, self.stop = device.connection_id, device.stop
        self.started = time.time()
        self.start_clock = time.monotonic()
        self.deadline = self.start_clock + timeout
        self.require_open = require_open
        self.cancelled = threading.Event()
        self.external_cancel = None

    def check(self):
        if self.cancelled.is_set() or self.external_cancel is not None and self.external_cancel.is_set():
            raise TransactionError('cancelled', '控制台操作已取消')
        if self.device.connection_id != self.connection_id or self.device.stop is not self.stop:
            raise TransactionError('stale', '设备连接已变化，取消旧操作')
        self.device.transactions.check_owner(self.actor, self.client_id)
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
        self._owner = None

    def _owner_locked(self):
        if self._owner and self._owner['connection_id'] != self.device.connection_id:
            self._owner = None
        return self._owner

    def ownership(self):
        with self._state_lock:
            owner = self._owner_locked()
            return dict(owner) if owner else None

    @property
    def client_id(self):
        return getattr(self._local, 'client_id', '')

    @property
    def source_actor(self):
        return getattr(self._local, 'actor', 'service')

    @staticmethod
    def _same(owner, actor, client_id):
        return owner['actor'] == actor and (actor != 'web' or owner.get('client_id', '') == client_id)

    def check_owner(self, actor, client_id=None):
        client_id = self.client_id if client_id is None else client_id
        with self._state_lock:
            owner = self._owner_locked()
            if owner and not self._same(owner, actor, client_id):
                raise TransactionError('input-owned', '串口输入由 %s 占用；仍可查看输出' % owner['actor'], 423)

    def claim(self, actor, takeover=False):
        client_id = self.client_id if actor == 'web' else ''
        if takeover and actor != 'web':
            raise TransactionError('forbidden', '仅人工可以接管串口输入', 403)
        with self._state_lock:
            owner = self._owner_locked()
            if owner and self._same(owner, actor, client_id):
                return dict(owner)
        if not takeover and not self.lock.acquire(blocking=False):
            raise TransactionError('busy', '控制台操作进行中，请结束后再占用')
        try:
            if not takeover and self.device.file_transfer.active:
                raise TransactionError('transfer-busy', '文件传输进行中，请结束后再占用')
            with self._state_lock:
                owner = self._owner_locked()
                if owner and self._same(owner, actor, client_id):
                    return dict(owner)
                if owner and not takeover:
                    raise TransactionError('input-owned', '串口输入已由 %s 占用' % owner['actor'], 423)
                if self._active and (self._active.actor != actor or self._active.client_id != client_id):
                    self._active.cancelled.set()
                self._owner = {'id': secrets.token_hex(12), 'actor': actor,
                               'client_id': client_id,
                               'connection_id': self.device.connection_id, 'since': time.time()}
                result = dict(self._owner)
            self._wake()
            return result
        finally:
            if not takeover:
                self.lock.release()

    def release(self, ownership_id, actor):
        with self._state_lock:
            owner = self._owner_locked()
            if not owner or owner['id'] != ownership_id:
                raise TransactionError('not-found', '输入占用已结束或已替换', 404)
            if not self._same(owner, actor, self.client_id):
                raise TransactionError('forbidden', '只能释放自己的输入占用', 403)
            self._owner = None
            if self._active and self._same(owner, self._active.actor, self._active.client_id):
                self._active.cancelled.set()
        self._wake()

    def _wake(self):
        cv = getattr(self.device, 'cv', None)
        if cv is not None:
            with cv:
                cv.notify_all()

    @property
    def current(self):
        return getattr(self._local, 'lease', None)

    @contextmanager
    def actor_scope(self, actor, client_id=''):
        previous = getattr(self._local, 'actor', 'service')
        previous_client = self.client_id
        self._local.actor = actor
        self._local.client_id = client_id if actor == 'web' else ''
        try:
            yield
        finally:
            self._local.actor = previous
            self._local.client_id = previous_client

    @contextmanager
    def task_scope(self, cancelled, connection_id, stop):
        previous = getattr(self._local, 'task', None)
        self._local.task = (cancelled, connection_id, stop)
        try:
            yield
        finally:
            self._local.task = previous

    @contextmanager
    def begin(self, kind, timeout, actor=None, allow_transfer=False,
              expected_connection=None, require_open=True, client_id=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('transaction timeout must be finite and positive')
        if not self.lock.acquire(blocking=False):
            raise TransactionError('busy', '已有控制台操作在进行中，稍后重试')
        lease = None
        try:
            task = getattr(self._local, 'task', None)
            if task is not None:
                if task[0].is_set():
                    raise TransactionError('cancelled', '控制台任务已取消')
                if self.device.connection_id != task[1] or self.device.stop is not task[2]:
                    raise TransactionError('stale', '设备连接已变化，取消旧任务')
            if expected_connection is not None and expected_connection != self.device.connection_id:
                raise TransactionError('stale', '设备连接已变化，取消旧操作')
            if not allow_transfer and self.device.file_transfer.active:
                raise TransactionError('transfer-busy', '串口文件传输中，请完成或取消后再操作')
            lease = ConsoleLease(self.device, kind, timeout,
                                 actor if actor is not None else getattr(self._local, 'actor', 'service'),
                                 require_open, self.client_id if client_id is None else client_id)
            lease.external_cancel = task[0] if task is not None else None
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
        self._wake()


def transactions_for(device):
    coordinator = getattr(device, 'transactions', None)
    if not isinstance(coordinator, SerialTransactions):
        coordinator = SerialTransactions(device, device.operation_lock)
        device.transactions = coordinator
    return coordinator
