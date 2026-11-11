"""Board-level maintenance exclusion independent of UART attachment generations."""
from contextlib import contextmanager
import hashlib
import re
import secrets
import threading
import time

from .tasks import TaskError
from .transactions import TransactionError


_CREATION_LOCK = threading.Lock()
CACHE_POOL_RESOURCE = 'flash-cache-pool:local'


def maintenance_for(manager):
    if manager is None:
        return None
    from .storage import Store
    if not isinstance(getattr(manager, 'store', None), Store):
        return None
    coordinator = getattr(manager, '_board_maintenance', None)
    if isinstance(coordinator, BoardMaintenance):
        return coordinator
    with _CREATION_LOCK:
        coordinator = getattr(manager, '_board_maintenance', None)
        if not isinstance(coordinator, BoardMaintenance):
            coordinator = BoardMaintenance(manager)
            manager._board_maintenance = coordinator
    return coordinator


class BoardMaintenance:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store
        self._lock = threading.RLock()
        self._active = {}
        self._local = threading.local()
        with self.store.lock, self.store.db:
            # Persistent holds survive restart. Never assume an external flasher
            # stopped merely because the central task is now interrupted.
            self.store.db.execute("UPDATE flash_resource_holds SET state='recovery',updated=? "
                                  "WHERE task_id IN (SELECT id FROM tasks WHERE state='interrupted')",
                                  (time.time(),))

    @staticmethod
    def resources(serial, resources=()):
        if not isinstance(serial, str) or not serial or len(serial.encode()) > 100:
            raise TaskError('maintenance requires a board serial')
        if (not isinstance(resources, (list, tuple)) or len(resources) > 32
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in resources)):
            raise TaskError('invalid maintenance resources')
        return sorted(set(['board:' + serial] + list(resources)))

    def holds(self, serial=None):
        with self.store.lock:
            if serial is None:
                rows = self.store.db.execute('SELECT * FROM flash_resource_holds ORDER BY resource').fetchall()
            else:
                rows = self.store.db.execute('SELECT * FROM flash_resource_holds WHERE resource=?',
                                             ('board:' + serial,)).fetchall()
        return [dict(row) for row in rows]

    def check(self, serial):
        if not serial:
            return
        if self.holds(serial):
            raise TransactionError('board-maintenance', '开发板正在刷机或等待恢复；仍可查看输出', 423)

    def _check_resources(self, resources):
        placeholders = ','.join('?' for _ in resources)
        with self.store.lock:
            row = self.store.db.execute('SELECT task_id,state FROM flash_resource_holds WHERE resource IN ('
                                        + placeholders + ') LIMIT 1', resources).fetchone()
        if row:
            raise TaskError('board or flash endpoint is reserved by task ' + row['task_id']
                            + ('; recovery must be confirmed' if row['state'] == 'recovery' else ''), 423)
        if any(self._active.get(resource, 0) for resource in resources):
            raise TaskError('board or flash endpoint has an active operation', 409)

    def _check_attachment(self, serial, actor, client_id):
        board = getattr(self.manager, 'boards', {}).get(serial)
        if board is None:
            raise TaskError('flash board is not registered', 404)
        device = board.connection
        if device is None:
            return
        transactions = getattr(device, 'transactions', None)
        if transactions is not None:
            owner = transactions.ownership()
            if owner and not transactions._same(owner, actor, client_id):
                raise TaskError('board input is owned by another client', 423)
            if transactions.snapshot() is not None:
                raise TaskError('board has an active console operation', 409)
        if getattr(getattr(device, 'file_transfer', None), 'active', False):
            raise TaskError('board has an active file transfer', 409)
        lock = getattr(device, 'operation_lock', None)
        if lock is not None:
            if not lock.acquire(blocking=False):
                raise TaskError('board has an active operation', 409)
            lock.release()
        if getattr(board.sessions, 'pending', 0):
            raise TaskError('board has an SSH connection in progress', 409)

    @contextmanager
    def admission(self, serial, resources=(), actor='web', client_id=''):
        """Hold while atomically registering a task and its durable holds."""
        selected = self.resources(serial, resources)
        with self._lock:
            self._check_resources(selected)
            self._check_attachment(serial, actor, client_id)
            yield selected

    @contextmanager
    def inspect_scope(self, serial, resources=(), actor='web', client_id=''):
        selected = self.resources(serial, resources)
        with self.admission(serial, resources, actor, client_id):
            inspections = getattr(self._local, 'inspections', None)
            if inspections is None:
                inspections = self._local.inspections = {}
            for resource in selected:
                self._active[resource] = self._active.get(resource, 0) + 1
                inspections[resource] = inspections.get(resource, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                for resource in selected:
                    inspected = inspections[resource] - 1
                    if inspected:
                        inspections[resource] = inspected
                    else:
                        del inspections[resource]
                    count = self._active[resource] - 1
                    if count:
                        self._active[resource] = count
                    else:
                        del self._active[resource]

    def retain_inspection_failure(self, serial, resources=(), actor='web', client_id='', *,
                                  attempt_id, details=None):
        """Convert this thread's failed inspection into an auditable recovery hold.

        Called inside inspect_scope before its temporary reservation exits. It
        records uncertainty; no helper is restarted and no new worker is queued.
        """
        from .flash_store import encode_document
        from .tasks import TaskStore
        if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', attempt_id):
            raise TaskError('invalid flash inspection attempt ID')
        selected = self.resources(serial, resources)
        document = dict(details or {}, attempt_id=attempt_id, recovery_required=True,
                        cleanup_confirmed=False, write_result='not-started',
                        verify_result='not-started', boot_result='not-requested',
                        phase='inspection-recovery')
        encoded = encode_document(document)
        with self._lock:
            inspections = getattr(self._local, 'inspections', {})
            if any(inspections.get(resource, 0) != self._active.get(resource, 0)
                   or not inspections.get(resource) for resource in selected):
                raise TaskError('inspection recovery must retain the current inspection reservation', 409)
            with self.store.lock:
                placeholders = ','.join('?' for _ in selected)
                if self.store.db.execute('SELECT 1 FROM flash_resource_holds WHERE resource IN ('
                                         + placeholders + ') LIMIT 1', selected).fetchone():
                    raise TaskError('inspection resource recovery was already registered', 409)
            tasks = TaskStore(self.store)
            task_id = tasks.create_board('flash-inspect', actor, client_id, serial,
                'inspect-' + secrets.token_hex(12), hashlib.sha256(encoded.encode()).hexdigest(),
                document, selected, initial_state='failed', resource_state='recovery',
                error='inspection process cleanup could not be confirmed; recovery required')
            return tasks.get(task_id)

    def check_preparation(self):
        """Unknown cache writers block new imports without reserving a board."""
        with self._lock, self.store.lock:
            row = self.store.db.execute('SELECT task_id FROM flash_resource_holds WHERE resource=?',
                                        (CACHE_POOL_RESOURCE,)).fetchone()
        if row is not None:
            raise TaskError('flash image cache is waiting for recovery of task ' + row['task_id'], 423)

    def retain_preparation_failure(self, serial, actor='web', client_id='', *, attempt_id, details=None):
        """Pin only the shared cache pool when an image-preparation writer is unknown.

        This host-only stage never holds UART/USB resources or the board. Its
        failed task still belongs to the real board for history and access scope.
        """
        from .flash_store import encode_document
        from .tasks import TaskStore
        if not isinstance(attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', attempt_id):
            raise TaskError('invalid flash preparation attempt ID')
        document = dict(details or {}, attempt_id=attempt_id, recovery_required=True,
                        cleanup_confirmed=False, write_result='not-started',
                        verify_result='not-started', boot_result='not-requested',
                        phase='preparation-recovery')
        encoded = encode_document(document)
        tasks = TaskStore(self.store)
        with self._lock, self.store.lock, self.store.db:
            # Serialize the uniqueness check and recovery task creation even
            # against another SQLite connection. A conflict creates no orphan.
            self.store.db.execute('BEGIN IMMEDIATE')
            self.check_preparation()
            task_id = tasks.create_board('flash-inspect', actor, client_id, serial,
                'prepare-' + secrets.token_hex(12), hashlib.sha256(encoded.encode()).hexdigest(),
                document, [CACHE_POOL_RESOURCE], initial_state='failed', resource_state='recovery',
                error='image preparation process cleanup could not be confirmed; recovery required')
        return tasks.get(task_id)

    @contextmanager
    def operation(self, serial):
        """Existing UART operations register before any active board IO."""
        if not serial:
            yield
            return
        resource = 'board:' + serial
        owned = getattr(self._local, 'owned', None)
        if owned is None:
            owned = self._local.owned = set()
        if resource in owned:
            yield
            return
        with self._lock:
            self.check(serial)
            if self._active.get(resource, 0):
                raise TransactionError('busy', '开发板检查进行中，请稍后重试')
            self._active[resource] = self._active.get(resource, 0) + 1
            owned.add(resource)
        try:
            yield
        finally:
            with self._lock:
                owned.remove(resource)
                count = self._active[resource] - 1
                if count:
                    self._active[resource] = count
                else:
                    del self._active[resource]

    def reserve(self, serial, task_id, resources=(), actor='web', client_id=''):
        with self.admission(serial, resources, actor, client_id) as selected:
            with self.store.lock, self.store.db:
                self.store.db.executemany('INSERT INTO flash_resource_holds VALUES(?,?,?,?)',
                    [(resource, task_id, 'active', time.time()) for resource in selected])

    def keep_recovery(self, task_id):
        with self._lock, self.store.lock, self.store.db:
            self.store.db.execute("UPDATE flash_resource_holds SET state='recovery',updated=? WHERE task_id=?",
                                  (time.time(), task_id))

    def release(self, task_id, confirmed=False):
        if confirmed is not True:
            self.keep_recovery(task_id)
            return False
        with self._lock, self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM flash_resource_holds WHERE task_id=?', (task_id,))
        return True

    def confirm_recovery(self, task_id, actor, expected_attempt_id, reason, confirm_stopped=False,
                         artifacts=None):
        """A human records an out-of-band process-stop check; never replay IO.

        The caller must authenticate actor as web. This action does not turn an
        interrupted/failed write into success or modify its terminal state.
        """
        from .flash_store import FlashStore, encode_document
        from .storage import validate_conflict_reason
        from .tasks import TaskStore
        if actor != 'web':
            raise TaskError('only a human operator can confirm flash recovery', 403)
        if confirm_stopped is not True:
            raise TaskError('confirm that the original flashing process has stopped', 409)
        try:
            reason = validate_conflict_reason(reason)
        except ValueError as error:
            raise TaskError(str(error)) from error
        with self._lock:
            tasks, flashes = TaskStore(self.store), FlashStore(self.store)
            task = tasks.get(task_id)
            details = flashes.details(task_id)
            if (task['kind'] not in ('flash', 'flash-inspect') or not isinstance(expected_attempt_id, str)
                    or not expected_attempt_id or details.get('attempt_id') != expected_attempt_id):
                raise TaskError('flash recovery execution attempt changed', 409)
            if task['state'] not in ('failed', 'cancelled', 'interrupted', 'completed'):
                raise TaskError('flash task is still active; cancel and confirm it stopped before recovery', 409)
            with self.store.lock:
                holds = self.store.db.execute('SELECT state FROM flash_resource_holds WHERE task_id=?',
                                              (task_id,)).fetchall()
                claimed = self.store.db.execute("SELECT id FROM transfer_artifacts WHERE task_id=? AND state='claimed'",
                                                (task_id,)).fetchall()
            if not holds:
                if details.get('recovery_confirmation', {}).get('attempt_id') == expected_attempt_id:
                    return tasks.get(task_id)
                raise TaskError('flash task has no recovery reservation', 409)
            if any(row['state'] != 'recovery' for row in holds):
                raise TaskError('flash resources have not entered recovery state', 409)
            if claimed and artifacts is None:
                raise TaskError('artifact cleanup service is required to confirm recovery', 503)
            # Hold every resource until all readers and claims can be released.
            # A failure here deliberately leaves the recovery holds untouched.
            for row in claimed:
                artifacts.finish_claim(row['id'], task_id)
            details.update({'recovery_required': False,
                    'cleanup_confirmed': True, 'recovery_confirmation': {
                        'attempt_id': expected_attempt_id, 'actor': actor, 'reason': reason,
                        'confirmed_at': time.time(), 'process_stopped_confirmed': True}})
            encoded = encode_document(details)
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE flash_details SET document=? WHERE task_id=?', (encoded, task_id))
                self.store.db.execute('DELETE FROM flash_resource_holds WHERE task_id=?', (task_id,))
                self.store.db.executemany('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                    [(serial, time.time(), actor, 'flash-recovery-confirmed', task_id + ': ' + reason)
                     for serial in task.get('board_targets', [])])
            return tasks.get(task_id)
