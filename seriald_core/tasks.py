"""Durable operation history with bounded workers and cooperative cancellation.

Only one TaskManager owns a collector's task execution. Restart never replays
commands: queued/running records become interrupted, keeping partial progress.
"""
import json
import secrets
import threading
import time


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

    def create(self, kind, actor, client_id, targets):
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
            self.store.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                  (task_id, kind, actor, client_id if actor == 'web' else '',
                                   'queued', '{}', None, '', now, now, 0))
            self.store.db.executemany('INSERT INTO task_targets VALUES (?,?,?)',
                                     [(task_id, serial, connection) for serial, connection in normalized])
        return task_id

    def get(self, task_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise TaskError('task not found', 404)
            record = dict(row)
            record['targets'] = [dict(target) for target in self.store.db.execute(
                'SELECT serial,connection_id FROM task_targets WHERE task_id=? ORDER BY serial,connection_id',
                (task_id,))]
        record['progress'] = json.loads(record['progress'])
        record['result'] = json.loads(record['result']) if record['result'] is not None else None
        record['cancel_requested'] = bool(record['cancel_requested'])
        return record

    def list(self, actor=None, serials=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise TaskError('invalid task pagination')
        clauses, arguments = [], []
        if actor is not None:
            clauses.append('actor=?')
            arguments.append(actor)
        if serials is not None:
            if not serials:
                return []
            placeholders = ','.join('?' for _ in serials)
            # A single-board grant must not see a two-board copy involving another board.
            clauses.append('NOT EXISTS (SELECT 1 FROM task_targets WHERE task_id=tasks.id '
                           'AND serial NOT IN (' + placeholders + '))')
            arguments.extend(serials)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.lock:
            ids = [row[0] for row in self.store.db.execute(
                'SELECT id FROM tasks' + where + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                (*arguments, limit, offset))]
            return [self.get(task_id) for task_id in ids]

    def update(self, task_id, state=None, progress=None, result=None, error=''):
        if state is not None and state not in ('running', 'completed', 'failed', 'cancelled'):
            raise TaskError('invalid task state')
        if progress is not None:
            if not isinstance(progress, dict) or set(progress) - {'done', 'total', 'phase'}:
                raise TaskError('invalid task progress')
            for name in ('done', 'total'):
                if name in progress and (type(progress[name]) is not int or progress[name] < 0):
                    raise TaskError('invalid task progress count')
            if 'phase' in progress and (not isinstance(progress['phase'], str) or len(progress['phase']) > 160):
                raise TaskError('invalid task phase')
        encoded = json.dumps(result, allow_nan=False) if result is not None else None
        if encoded and len(encoded.encode()) > 65536:
            raise TaskError('task result too large')
        if encoded:
            _check_result_fields(json.loads(encoded))
        assignments, values = ['updated=?'], [time.time()]
        for field, value in (('state', state), ('progress', json.dumps(progress) if progress is not None else None),
                             ('result', encoded)):
            if value is not None:
                assignments.append(field + '=?'); values.append(value)
        if state in ('failed', 'cancelled'):
            assignments.append('error=?'); values.append(str(error)[:2048])
        with self.store.lock, self.store.db:
            return bool(self.store.db.execute(
                'UPDATE tasks SET ' + ','.join(assignments) + " WHERE id=? AND state IN ('queued','running')",
                (*values, task_id)).rowcount)

    def request_cancel(self, task_id):
        self.get(task_id)
        with self.store.lock, self.store.db:
            return bool(self.store.db.execute(
                "UPDATE tasks SET cancel_requested=1,updated=? WHERE id=? AND state IN ('queued','running')",
                (time.time(), task_id)).rowcount)

    def recover(self):
        with self.store.lock, self.store.db:
            return self.store.db.execute(
                "UPDATE tasks SET state='interrupted',updated=?,error=? WHERE state IN ('queued','running')",
                (time.time(), 'collector restarted; execution outcome may be partial; not automatically replayed')).rowcount


class TaskContext:
    def __init__(self, task_id, store):
        self.id, self.store = task_id, store
        self.cancelled = threading.Event()
        self._lock, self._hooks = threading.Lock(), []

    def check(self):
        if self.cancelled.is_set():
            raise TaskCancelled('task cancelled')

    def progress(self, done, total, phase=''):
        self.check()
        self.store.update(self.id, progress={'done': done, 'total': total, 'phase': phase})

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
        self.store.recover()

    def start(self, kind, actor, client_id, targets, run):
        if not callable(run):
            raise TaskError('task runner must be callable')
        with self._lock:
            if self._closing:
                raise TaskError('collector is stopping', 503)
            if len(self._active) >= self.max_active:
                raise TaskError('too many active tasks', 429)
            task_id = self.store.create(kind, actor, client_id, targets)
            context = TaskContext(task_id, self.store)
            worker = threading.Thread(target=self._run, args=(context, run), daemon=True)
            self._active[task_id] = (context, worker)
            try:
                worker.start()
            except Exception:
                self._active.pop(task_id)
                self.store.update(task_id, state='failed', error='worker could not start')
                raise
        return self.store.get(task_id)

    def _run(self, context, run):
        try:
            context.check()
            self.store.update(context.id, state='running')
            result = run(context)
            # Cancellation is a request, not rollback. A completed command can win a race.
            self.store.update(context.id, state='completed', result=result)
        except TaskCancelled as error:
            self.store.update(context.id, state='cancelled', error=str(error))
        except Exception as error:
            self.store.update(context.id, state='failed', error=str(error))
        finally:
            with self._lock:
                self._active.pop(context.id, None)

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
