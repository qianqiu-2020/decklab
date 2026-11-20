"""Durable operation history with bounded workers and cooperative cancellation.

Only one TaskManager owns a collector's task execution. Restart never replays
commands: queued/running records become interrupted, keeping partial progress.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time

from .api_contract import OUTCOMES


_OUTCOME_FIELD = '_task_outcome'
DISCRETE_TASK_KINDS = ('identify', 'ssh-enable', 'ssh-connect', 'file-copy', 'file-upload',
                       'file-download', 'hardware-reset', 'hardware-power-on',
                       'hardware-power-off', 'hardware-enter-download', 'hardware-flash')


class TaskError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class TaskCancelled(Exception):
    pass


def _check_result_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace('-', '_') in (
                    'password', 'ssh_password', 'token', 'authorization', 'cookie',
                    'client_secret', 'private_key'):
                raise TaskError('task results must not contain credentials')
            _check_result_fields(item)
    elif isinstance(value, list):
        for item in value:
            _check_result_fields(item)


class TaskStore:
    def __init__(self, store):
        self.store = store

    def request_digest(self, document):
        """Compare private request inputs without storing credentials or bare password hashes.

        The random HMAC key is separate from SQLite and must accompany a private
        runtime backup. Losing it with existing discrete requests fails closed.
        """
        try:
            raw = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        except (ValueError, TypeError, UnicodeError):
            raise TaskError('invalid task request input') from None
        if len(raw) > 65536:
            raise TaskError('task request input too large', 413)
        with self.store.lock:
            key = getattr(self.store, '_task_request_hmac_key', None)
            if key is None:
                key = self._request_key()
                self.store._task_request_hmac_key = key
        return hmac.new(key, raw, hashlib.sha256).hexdigest()

    def _request_key(self):
        if self.store.path == ':memory:':
            return secrets.token_bytes(32)
        path = Path(self.store.path).with_name('.task-request-hmac.key')
        if not path.exists():
            placeholders = ','.join('?' for _ in DISCRETE_TASK_KINDS)
            if self.store.db.execute('SELECT 1 FROM flash_requests r JOIN tasks t ON t.id=r.task_id '
                                     'WHERE t.kind IN (' + placeholders + ') LIMIT 1',
                                     DISCRETE_TASK_KINDS).fetchone():
                raise TaskError('private task request key is missing; restore the original runtime key', 503)
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix='.task-request-key-', dir=path.parent)
                with os.fdopen(fd, 'wb') as target:
                    target.write(secrets.token_bytes(32))
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    # Publish a fully written owner-only key without replacing a
                    # concurrent initializer's key. A symlink is never followed.
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                if os.name == 'posix':
                    directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except OSError:
                raise TaskError('private task request key could not be initialized', 503) from None
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size != 32
                        or os.name == 'posix' and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
                    raise ValueError()
                key = source.read(33)
                if len(key) != 32:
                    raise ValueError()
                return key
        except (OSError, ValueError):
            raise TaskError('private task request key is unavailable or not owner-only', 503) from None

    def find_request(self, actor, request_id, input_digest=None):
        from .flash_store import validate_request
        validate_request(request_id, input_digest if input_digest is not None else '0' * 64)
        with self.store.lock:
            row = self.store.db.execute('SELECT input_digest,task_id FROM flash_requests '
                                        'WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
        if row is not None and input_digest is not None and not hmac.compare_digest(row['input_digest'], input_digest):
            raise TaskError('task request ID was already used with different input', 409)
        return row['task_id'] if row else None

    def create(self, kind, actor, client_id, targets, request_id=None, input_digest=None, flash_target=None):
        if not all(isinstance(value, str) and value and len(value) <= 80 for value in (kind, actor)):
            raise TaskError('invalid task kind or actor')
        if not isinstance(client_id, str) or len(client_id) > 80:
            raise TaskError('invalid task client')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise TaskError('tasks require 1–8 connection targets')
        normalized = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {'serial', 'connection_id'}:
                raise TaskError('invalid task target')
            serial, connection = target['serial'], target['connection_id']
            if (not isinstance(serial, str) or len(serial.encode()) > 100
                    or not isinstance(connection, str) or not connection or len(connection) > 80):
                raise TaskError('invalid task identity')
            normalized.add((serial, connection))
        task_id, now = secrets.token_hex(12), time.time()
        with self.store.lock, self.store.db:
            if request_id is not None:
                from .flash_store import validate_request
                validate_request(request_id, input_digest)
            if flash_target is not None:
                target_id, generation = flash_target
                row = self.store.db.execute('SELECT generation,present,serial FROM flash_targets WHERE id=?', (target_id,)).fetchone()
                if (len(normalized) != 1 or row is None or not row['present'] or row['generation'] != generation
                        or (row['serial'] or '') != next(iter(normalized))[0]):
                    raise TaskError('flash USB target generation changed', 409)
            self.store.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                  (task_id, kind, actor, client_id if actor == 'web' else '',
                                   'queued', '{}', json.dumps({_OUTCOME_FIELD: 'not-issued'}), '', now, now, 0))
            self.store.db.executemany('INSERT INTO task_targets VALUES (?,?,?)',
                                     [(task_id, serial, connection) for serial, connection in normalized])
            if flash_target is not None:
                self.store.db.execute('INSERT INTO task_flash_targets VALUES(?,?,?)', (task_id, *flash_target))
                if row['serial']:
                    self.store.db.execute('INSERT INTO task_board_targets VALUES(?,?)', (task_id, row['serial']))
            if request_id is not None:
                self.store.db.execute('INSERT INTO flash_requests VALUES (?,?,?,?)',
                                      (actor, request_id, input_digest, task_id))
        return task_id

    def create_board(self, kind, actor, client_id, serial, request_id, input_digest,
                     details, resources, connection_id=None, *, initial_state='queued',
                     resource_state='active', error='', target_id=None, target_generation=None):
        """Atomically bind one execution, idempotency key and durable resource holds."""
        from .flash_store import encode_document, validate_request
        validate_request(request_id, input_digest)
        if kind not in ('flash', 'flash-inspect') or not isinstance(actor, str) or not actor or len(actor) > 80:
            raise TaskError('invalid board task kind or actor')
        if not isinstance(client_id, str) or len(client_id) > 80:
            raise TaskError('invalid board task client')
        if not isinstance(serial, str) or len(serial.encode()) > 100 or not (serial or target_id):
            raise TaskError('invalid board task serial')
        if target_id is not None:
            if (not isinstance(target_id, str) or not target_id or len(target_id) > 80
                    or not isinstance(target_generation, str) or not target_generation or len(target_generation) > 80):
                raise TaskError('automatic flash targets require a valid target and USB generation')
        elif target_generation is not None:
            raise TaskError('USB generation requires a flash target')
        if not serial and connection_id is not None:
            raise TaskError('unidentified USB target cannot carry a UART connection')
        if connection_id is not None and (not isinstance(connection_id, str) or not connection_id
                                           or len(connection_id) > 80):
            raise TaskError('invalid board task connection snapshot')
        if ((initial_state, resource_state) not in (('queued', 'active'), ('failed', 'recovery'))
                or not isinstance(error, str) or len(error) > 2048):
            raise TaskError('invalid board task initial state')
        encoded = encode_document(details)
        task_id, now = secrets.token_hex(12), time.time()
        with self.store.lock, self.store.db:
            if serial and self.store.db.execute('SELECT serial FROM boards WHERE serial=?', (serial,)).fetchone() is None:
                raise TaskError('flash board is not registered', 404)
            if target_id:
                target = self.store.db.execute('SELECT generation,serial,present FROM flash_targets WHERE id=?',
                                               (target_id,)).fetchone()
                if (target is None or initial_state == 'queued' and (
                        target['generation'] != target_generation
                        or (target['serial'] or '') != serial or not target['present'])):
                    raise TaskError('flash USB target generation or identity changed', 409)
            self.store.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, kind, actor, client_id if actor == 'web' else '', initial_state, '{}',
                 json.dumps({_OUTCOME_FIELD: 'not-issued' if initial_state == 'queued' else 'unknown'}), error, now, now, 0))
            if serial:
                self.store.db.execute('INSERT INTO task_board_targets VALUES(?,?)', (task_id, serial))
            if target_id:
                self.store.db.execute('INSERT INTO task_flash_targets VALUES(?,?,?)',
                                      (task_id, target_id, target_generation))
            if connection_id is not None:
                self.store.db.execute('INSERT INTO task_targets VALUES(?,?,?)', (task_id, serial, connection_id))
            self.store.db.execute('INSERT INTO flash_requests VALUES(?,?,?,?)',
                                  (actor, request_id, input_digest, task_id))
            self.store.db.execute('INSERT INTO flash_details VALUES(?,?,0)', (task_id, encoded))
            self.store.db.executemany('INSERT INTO flash_resource_holds VALUES(?,?,?,?)',
                [(resource, task_id, resource_state, now) for resource in resources])
        return task_id

    def get(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{24}', task_id):
            raise TaskError('task not found', 404)
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise TaskError('task not found', 404)
            record = dict(row)
            request = self.store.db.execute('SELECT request_id FROM flash_requests WHERE task_id=?',
                                            (task_id,)).fetchone()
            record['request_id'] = request[0] if request else None
            record['targets'] = [dict(target) for target in self.store.db.execute(
                'SELECT t.serial,t.connection_id,c.serial AS resolved_serial FROM task_targets t '
                'LEFT JOIN connection_identities c ON c.connection_id=t.connection_id '
                'WHERE task_id=? ORDER BY t.serial,t.connection_id',
                (task_id,))]
            board_targets = [row[0] for row in self.store.db.execute(
                'SELECT serial FROM task_board_targets WHERE task_id=? ORDER BY serial', (task_id,))]
            if board_targets:
                record['board_targets'] = board_targets
            flash_targets = [dict(target) for target in self.store.db.execute(
                'SELECT target_id,generation FROM task_flash_targets WHERE task_id=? ORDER BY target_id', (task_id,))]
            if flash_targets:
                record['flash_targets'] = flash_targets
            detail = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?',
                                            (task_id,)).fetchone()
            if detail:
                from .flash_store import FlashStore
                record['flash'] = FlashStore(self.store).public_details(task_id)
        for target in record['targets']:
            if target['resolved_serial'] is None:
                del target['resolved_serial']
        record['progress'] = json.loads(record['progress'])
        record['result'] = json.loads(record['result']) if record['result'] is not None else None
        if isinstance(record['result'], dict):
            record['outcome'] = record['result'].pop(_OUTCOME_FIELD, 'unknown')
            if not record['result']:
                record['result'] = None
        else:
            record['outcome'] = 'not-issued' if record['state'] == 'queued' else 'unknown'
        record['cancel_requested'] = bool(record['cancel_requested'])
        return record

    def list(self, actor=None, serials=None, limit=20, offset=0, connection_id=None, target_serial=None,
             target_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise TaskError('invalid task pagination')
        if target_id is not None and (not isinstance(target_id, str)
                                      or not re.fullmatch(r'[0-9a-f]{24}', target_id)):
            raise TaskError('invalid flash target filter')
        clauses, arguments = [], []
        if actor is not None:
            clauses.append('actor=?')
            arguments.append(actor)
        # Resolved identity is separate from immutable submission snapshots.
        # Legacy one-target identify results remain readable without inventing a
        # connection mapping during upgrade; multi-target results never widen scope.
        effective = ("COALESCE((SELECT serial FROM connection_identities c WHERE c.connection_id=t.connection_id),"
                     "NULLIF(t.serial,''),CASE WHEN tasks.kind='identify' AND "
                     "(SELECT COUNT(*) FROM task_targets WHERE task_id=tasks.id)=1 "
                     "THEN json_extract(tasks.result,'$.serial') END,'')")
        if serials is not None:
            if not serials:
                return []
            placeholders = ','.join('?' for _ in serials)
            clauses.append('NOT EXISTS (SELECT 1 FROM task_targets t WHERE t.task_id=tasks.id AND '
                           + effective + ' NOT IN (' + placeholders + '))')
            arguments.extend(serials)
            # An unbound download attachment has no board grant, even if its
            # historical target lists are otherwise empty (all([]) is unsafe).
            clauses.append('NOT EXISTS (SELECT 1 FROM task_flash_targets f WHERE f.task_id=tasks.id '
                           'AND NOT EXISTS (SELECT 1 FROM task_board_targets b WHERE b.task_id=tasks.id))')
            clauses.append('NOT EXISTS (SELECT 1 FROM task_board_targets b WHERE b.task_id=tasks.id '
                           'AND b.serial NOT IN (' + placeholders + '))')
            arguments.extend(serials)
        if target_serial is not None:
            if not isinstance(target_serial, str) or not target_serial or len(target_serial.encode()) > 100:
                raise TaskError('invalid task target serial')
            clauses.append('(EXISTS (SELECT 1 FROM task_targets t WHERE t.task_id=tasks.id AND '
                           + effective + '=?) OR EXISTS (SELECT 1 FROM task_board_targets b '
                           'WHERE b.task_id=tasks.id AND b.serial=?))')
            arguments.extend((target_serial, target_serial))
        if connection_id is not None:
            if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 80:
                raise TaskError('invalid task connection')
            clauses.append('EXISTS (SELECT 1 FROM task_targets WHERE task_id=tasks.id AND connection_id=?)')
            arguments.append(connection_id)
        if target_id is not None:
            clauses.append('EXISTS (SELECT 1 FROM task_flash_targets f WHERE f.task_id=tasks.id AND target_id=?)')
            arguments.append(target_id)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.lock:
            ids = [row[0] for row in self.store.db.execute(
                'SELECT id FROM tasks' + where + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                (*arguments, limit, offset))]
            return [self.get(task_id) for task_id in ids]

    @staticmethod
    def visible_serial(task, serial):
        if task.get('board_targets'):
            return all(value == serial for value in task['board_targets'])
        if task.get('flash_targets'):
            return False
        targets = task['targets']
        if len(targets) == 1 and not targets[0]['serial'] and 'resolved_serial' not in targets[0]:
            return task['kind'] == 'identify' and (task.get('result') or {}).get('serial') == serial
        return all(target.get('resolved_serial', target['serial']) == serial for target in targets)

    def update(self, task_id, state=None, progress=None, result=None, error='', outcome=None):
        if state is not None and state not in ('running', 'completed', 'failed', 'cancelled'):
            raise TaskError('invalid task state')
        if progress is not None:
            if not isinstance(progress, dict):
                raise TaskError('invalid task progress')
            counts, labels = {'done', 'total'}, {'phase'}
            if progress.get('phase') == 'writing':
                counts.update(('partition_done', 'partition_total', 'bytes_per_second'))
                labels.update(('component', 'storage', 'partition'))
            if set(progress) - counts - labels:
                raise TaskError('invalid task progress')
            for name in counts:
                if name in progress and (type(progress[name]) is not int or progress[name] < 0):
                    raise TaskError('invalid task progress count')
            for name in labels:
                if name in progress and (not isinstance(progress[name], str) or len(progress[name]) > 160):
                    raise TaskError('invalid task progress label')
        if outcome is not None and outcome not in OUTCOMES:
            raise TaskError('invalid physical outcome')
        if isinstance(result, dict) and _OUTCOME_FIELD in result:
            raise TaskError('reserved task result field')
        encoded = json.dumps(result, allow_nan=False) if result is not None else None
        if encoded and len(encoded.encode()) > 65536:
            raise TaskError('task result too large')
        if encoded:
            _check_result_fields(json.loads(encoded))
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT result FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return False
            old = json.loads(row['result']) if row['result'] is not None else {}
            old_outcome = old.get(_OUTCOME_FIELD, 'unknown') if isinstance(old, dict) else 'unknown'
            if outcome is not None or encoded is not None:
                # Keep physical outcome independent of the adapter's business
                # result, including when failure metadata is written later.
                merged = json.loads(encoded) if encoded is not None else old
                if not isinstance(merged, dict):
                    raise TaskError('task result must be an object')
                merged[_OUTCOME_FIELD] = outcome if outcome is not None else old_outcome
                encoded = json.dumps(merged, allow_nan=False)
                if len(encoded.encode()) > 65536:
                    raise TaskError('task result too large')
            assignments, values = ['updated=?'], [time.time()]
            for field, value in (('state', state), ('progress', json.dumps(progress) if progress is not None else None),
                                 ('result', encoded)):
                if value is not None:
                    assignments.append(field + '=?'); values.append(value)
            if state in ('failed', 'cancelled'):
                assignments.append('error=?'); values.append(str(error)[:2048])
            return bool(self.store.db.execute(
                'UPDATE tasks SET ' + ','.join(assignments) + " WHERE id=? AND state IN ('queued','running')",
                (*values, task_id)).rowcount)

    def request_cancel(self, task_id):
        self.get(task_id)
        with self.store.lock, self.store.db:
            return bool(self.store.db.execute(
                "UPDATE tasks SET cancel_requested=1,updated=? WHERE id=? AND state IN ('queued','running')",
                (time.time(), task_id)).rowcount)

    def retain_cleanup_failure(self, task_id, serial):
        """Retain the existing board resource when process shutdown is unproven.

        No flash details or synthetic flash task are created. Human recovery may
        append a recovery_confirmation later; the failed cleanup fact remains.
        """
        if not isinstance(serial, str) or not serial or len(serial.encode()) > 100:
            raise TaskError('cleanup recovery requires a board serial')
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT result FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise TaskError('task not found', 404)
            result = json.loads(row['result']) if row['result'] else {}
            if not isinstance(result, dict):
                result = {}
            result.update(cleanup_confirmed=False, **{_OUTCOME_FIELD: 'unknown'})
            now = time.time()
            self.store.db.execute('UPDATE tasks SET result=?,updated=? WHERE id=?',
                                  (json.dumps(result), now, task_id))
            # Never steal a resource retained by another operation. Its hold
            # still blocks the board; this task's cleanup flag independently
            # prevents a false actor-stopped acknowledgement.
            self.store.db.execute('INSERT OR IGNORE INTO flash_resource_holds VALUES(?,?,?,?)',
                                  ('board:' + serial, task_id, 'recovery', now))

    def recover(self):
        with self.store.lock, self.store.db:
            for row in self.store.db.execute("SELECT id,result FROM tasks WHERE state IN ('queued','running')").fetchall():
                result = json.loads(row['result']) if row['result'] else {}
                if not isinstance(result, dict):
                    result = {}
                if result.get(_OUTCOME_FIELD) != 'not-issued':
                    result[_OUTCOME_FIELD] = 'unknown'
                self.store.db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps(result), row['id']))
            count = self.store.db.execute(
                "UPDATE tasks SET state='interrupted',updated=?,error=? WHERE state IN ('queued','running')",
                (time.time(), 'collector restarted; execution outcome may be partial; not automatically replayed')).rowcount
            self.store.db.execute("UPDATE flash_resource_holds SET state='recovery',updated=? "
                                  "WHERE task_id IN (SELECT id FROM tasks WHERE state='interrupted')", (time.time(),))
            return count


class TaskContext:
    def __init__(self, task_id, store, actor_check=None, tracks_outcome=False):
        self.id, self.store = task_id, store
        self.actor_check, self.tracks_outcome = actor_check, tracks_outcome
        self.cancelled = threading.Event()
        self._lock, self._hooks = threading.Lock(), []

    def check(self):
        if self.cancelled.is_set():
            raise TaskCancelled('task cancelled')
        if self.actor_check is not None:
            self.actor_check()

    def mark_outcome(self, outcome):
        self.store.update(self.id, outcome=outcome)

    def issued(self):
        """Persist before entering a transport: loss of receipt must remain unknown."""
        self.check()
        self.mark_outcome('issued')

    def retain_cleanup_failure(self, serial):
        self.store.retain_cleanup_failure(self.id, serial)

    def progress(self, done, total, phase='', **details):
        self.check()
        self.store.update(self.id, progress=dict(details, done=done, total=total, phase=phase))

    def on_cancel(self, hook):
        with self._lock:
            immediate = self.cancelled.is_set()
            if not immediate:
                self._hooks.append(hook)
        if immediate:
            hook()

    def cancel(self):
        with self._lock:
            self.cancelled.set()
            hooks, self._hooks = self._hooks, []
        for hook in hooks:
            try:
                hook()
            except Exception:
                # Cancellation remains requested even if a transport already closed.
                pass


class TaskManager:
    def __init__(self, store, max_active=4):
        if type(max_active) is not int or max_active < 1:
            raise TaskError('invalid worker limit')
        self.store, self.max_active = TaskStore(store), max_active
        self._lock, self._active = threading.RLock(), {}
        self._closing = False
        self.agent_grants = None
        self.store.recover()

    def _check_actor(self, actor):
        if self.agent_grants is not None:
            self.agent_grants.check_actor(actor)

    def _execution_runner(self, run, actor, client_id):
        """Hook for the existing execution coordinator to capture request context."""
        hook = getattr(self, 'execution_runner', None)
        return hook(run, actor, client_id) if hook is not None else run

    def start(self, kind, actor, client_id, targets, run, *, request_id=None, input_digest=None,
              tracks_outcome=False, flash_target=None):
        if not callable(run):
            raise TaskError('task runner must be callable')
        with self._lock:
            self._check_actor(actor)
            if request_id is not None:
                from .flash_store import FlashStore
                previous = FlashStore(self.store.store).find_request(actor, request_id, input_digest)
                if previous is not None:
                    return self.store.get(previous)
            if self._closing:
                raise TaskError('collector is stopping', 503)
            if len(self._active) >= self.max_active:
                raise TaskError('too many active tasks', 429)
            run = self._execution_runner(run, actor, client_id)
            task_id = self.store.create(kind, actor, client_id, targets, request_id, input_digest, flash_target)
            context = TaskContext(task_id, self.store, lambda: self._check_actor(actor), tracks_outcome)
            worker = threading.Thread(target=self._run, args=(context, run), daemon=True)
            self._active[task_id] = (context, worker)
            try:
                worker.start()
            except Exception:
                self._active.pop(task_id)
                self.store.update(task_id, state='failed', error='worker could not start')
                raise
        return self.store.get(task_id)

    def start_board(self, kind, actor, client_id, serial, run, *, request_id, input_digest,
                    details=None, resources=(), maintenance=None, connection_id=None,
                    target_id=None, target_generation=None):
        from .flash_store import FlashStore
        if not callable(run) or maintenance is None:
            raise TaskError('board task requires a runner and maintenance coordinator')
        flashes = FlashStore(self.store.store)
        with self._lock:
            self._check_actor(actor)
            previous = flashes.find_request(actor, request_id, input_digest)
            if previous is not None:
                return self.store.get(previous)
            if self._closing:
                raise TaskError('collector is stopping', 503)
            if len(self._active) >= self.max_active:
                raise TaskError('too many active tasks', 429)
            run = self._execution_runner(run, actor, client_id)
            admission = (maintenance.target_admission(target_id, target_generation, serial, resources, actor, client_id)
                         if target_id is not None else maintenance.admission(serial, resources, actor, client_id))
            with admission as selected:
                task_id = self.store.create_board(kind, actor, client_id, serial, request_id, input_digest,
                    details if details is not None else {}, selected, connection_id,
                    target_id=target_id, target_generation=target_generation)
            context = TaskContext(task_id, self.store, lambda: self._check_actor(actor))
            worker = threading.Thread(target=self._run_board, args=(context, run, maintenance), daemon=True)
            self._active[task_id] = (context, worker)
            try:
                worker.start()
            except Exception:
                self._active.pop(task_id)
                self.store.update(task_id, state='failed', error='worker could not start')
                maintenance.release(task_id, confirmed=True)
                raise
        return self.store.get(task_id)

    def _run_board(self, context, run, maintenance):
        entered = False
        context.defer_active_release = True
        def invoke(current):
            nonlocal entered
            entered = True
            return run(current)
        try:
            self._run(context, invoke)
        finally:
            # Cancellation before the worker enters the adapter has no external
            # side effects. Once entered, only the adapter can confirm cleanup.
            try:
                if not entered:
                    maintenance.release(context.id, confirmed=True)
            finally:
                with self._lock:
                    self._active.pop(context.id, None)

    def _run(self, context, run):
        try:
            context.check()
            self.store.update(context.id, state='running')
            if not context.tracks_outcome:
                context.issued()
            result = run(context)
            if self.store.get(context.id)['outcome'] == 'issued':
                context.mark_outcome('acknowledged')
            # Cancellation is a request, not rollback. A completed command can win a race.
            self.store.update(context.id, state='completed', result=result)
        except TaskCancelled as error:
            self.store.update(context.id, state='cancelled', error=str(error),
                              outcome=self._failed_outcome(context.id))
        except Exception as error:
            state = 'cancelled' if getattr(error, 'code', '') in ('cancelled', 'authorization-revoked') else 'failed'
            self.store.update(context.id, state=state, error=str(error),
                              outcome=self._failed_outcome(context.id))
        finally:
            if not getattr(context, 'defer_active_release', False):
                with self._lock:
                    self._active.pop(context.id, None)

    def _failed_outcome(self, task_id):
        return 'not-issued' if self.store.get(task_id)['outcome'] == 'not-issued' else 'unknown'

    def actor_stopped(self, actor):
        """Do not acknowledge revoke while any actor worker/cleanup remains active."""
        with self._lock:
            if any(self.store.get(task_id)['actor'] == actor for task_id in self._active):
                return False
            with self.store.store.lock:
                held = self.store.store.db.execute(
                    'SELECT 1 FROM flash_resource_holds h JOIN tasks t ON t.id=h.task_id '
                    "WHERE t.actor=? AND h.state IN ('active','recovery') LIMIT 1", (actor,)).fetchone()
                pending_cleanup = self.store.store.db.execute(
                    "SELECT 1 FROM tasks WHERE actor=? AND json_extract(result,'$.cleanup_confirmed')=0 "
                    "AND COALESCE(json_extract(result,'$.recovery_confirmation.confirmed_stopped'),0)!=1 LIMIT 1",
                    (actor,)).fetchone()
                return held is None and pending_cleanup is None

    def cancel(self, task_id, actor):
        record = self.store.get(task_id)
        if actor != 'web' and record['actor'] != actor:
            raise TaskError('cannot cancel another actor task', 403)
        with self._lock:
            requested = self.store.request_cancel(task_id)
            entry = self._active.get(task_id)
        if requested and entry:
            entry[0].cancel()
        return self.store.get(task_id)

    def cancel_actor(self, actor):
        with self._lock:
            ids = list(self._active)
        count = 0
        for task_id in ids:
            if self.store.get(task_id)['actor'] == actor:
                self.cancel(task_id, actor)
                count += 1
        return count

    def close(self, timeout=5):
        with self._lock:
            self._closing = True
            active = list(self._active.values())
        for context, _ in active:
            self.store.request_cancel(context.id)
            context.cancel()
        deadline = time.monotonic() + timeout
        for _, worker in active:
            worker.join(max(0, deadline - time.monotonic()))
        return not any(worker.is_alive() for _, worker in active)
