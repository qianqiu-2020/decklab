"""Persistent board scope for the existing input ownership and maintenance gate.

This is controller execution ownership, not a business device allocation pool.
Every writer must use this controller for the exclusion guarantee to apply.
"""
from contextlib import contextmanager
import hashlib
import secrets
import threading
import time

from .transactions import TransactionError


def ownership_for(manager):
    from .board_maintenance import maintenance_for
    maintenance = maintenance_for(manager)
    if maintenance is None:
        return None
    with maintenance._lock:
        service = getattr(maintenance, 'input_ownership', None)
        if service is None:
            service = maintenance.input_ownership = BoardInputOwnership(maintenance)
            tasks = getattr(manager, 'tasks', None)
            if tasks is not None:
                tasks.execution_runner = service.wrap_runner
        return service


class BoardInputOwnership:
    def __init__(self, maintenance):
        self.maintenance = maintenance
        self.manager, self.store = maintenance.manager, maintenance.store
        self.local = threading.local()
        with self._transaction():
            # A process restart is not proof that an external operation stopped.
            self.store.db.execute("UPDATE board_input_owners SET state='recovery',updated=? WHERE state='active'",
                                  (time.time(),))

    @contextmanager
    def _transaction(self):
        """Do not commit an enclosing maintenance/task transaction."""
        with self.store.lock:
            outer = self.store.db.in_transaction
            if not outer:
                self.store.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                if not outer:
                    self.store.db.rollback()
                raise
            else:
                if not outer:
                    self.store.db.commit()

    @staticmethod
    def _error(message='开发板执行输入已被占用；仍可查看输出', code='input-owned', status=423):
        return TransactionError(code, message, status)

    @staticmethod
    def _ttl(value):
        if type(value) is not int or not 30 <= value <= 3600:
            raise TransactionError('invalid-lease', 'ttl must be an integer from 30 to 3600', 400)
        return value

    @staticmethod
    def _proof(value):
        if (not isinstance(value, dict) or set(value) != {'id', 'generation', 'token'}
                or not isinstance(value['id'], str) or len(value['id']) != 32
                or type(value['generation']) is not int or value['generation'] < 1
                or not isinstance(value['token'], str) or not 32 <= len(value['token']) <= 128):
            raise TransactionError('invalid-lease', 'invalid execution_lease', 400)
        return dict(value)

    @contextmanager
    def scope(self, actor, client_id='', proof=None):
        previous = getattr(self.local, 'context', None)
        if proof is None and previous is not None:
            proof = previous[2]
        if proof is not None:
            proof = self._proof(proof)
        self.local.context = (actor, client_id if actor == 'web' else '', proof)
        try:
            yield
        finally:
            self.local.context = previous

    def wrap_runner(self, run, actor, client_id):
        captured = getattr(self.local, 'context', None)
        proof = captured[2] if captured else None
        def execute(context):
            with self.scope(actor, client_id, proof):
                original_check = context.check
                task = context.store.get(context.id)
                serials = set(task.get('board_targets', [])) | {
                    value.get('serial') for value in task.get('targets', []) if value.get('serial')}
                if task['kind'] in ('image-download', 'image-prepare'):
                    serials.clear()
                def check():
                    original_check()
                    for serial in serials:
                        self.check(serial, actor, client_id)
                context.check = check
                try:
                    check()
                    return run(context)
                finally:
                    context.check = original_check
        return execute

    def _row(self, serial):
        row = self.store.db.execute('SELECT * FROM board_input_owners WHERE serial=?', (serial,)).fetchone()
        if row is not None and row['state'] == 'active' and row['expires'] <= time.time():
            self.store.db.execute("UPDATE board_input_owners SET state='recovery',updated=? WHERE serial=?",
                                  (time.time(), serial))
            row = self.store.db.execute('SELECT * FROM board_input_owners WHERE serial=?', (serial,)).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _public(row):
        if row is None or row['state'] == 'released':
            return None
        return {key: row[key] for key in ('id', 'serial', 'actor', 'client_id', 'generation', 'state', 'expires')} | {
            'scope': 'board', 'execution_scope': 'collector'}

    def current(self, serial):
        with self.maintenance._lock, self._transaction():
            return self._public(self._row(serial))

    def _matches(self, row, actor, client_id, proof):
        return (proof is not None and row['id'] == proof['id'] and row['generation'] == proof['generation']
                and row['actor'] == actor and (actor != 'web' or row['client_id'] == client_id)
                and secrets.compare_digest(row['token_hash'], hashlib.sha256(proof['token'].encode()).hexdigest()))

    def check(self, serial, actor=None, client_id=None):
        if not serial:
            return
        context = getattr(self.local, 'context', None)
        proof = context[2] if context else None
        if actor is None:
            actor = context[0] if context else 'service'
        if client_id is None:
            client_id = context[1] if context else ''
        grants = getattr(self.manager, 'agent_grants', None)
        if grants is not None:
            from .agent_grants import GrantRevoked
            try:
                grants.check_actor(actor)
            except GrantRevoked as error:
                raise TransactionError(error.code, str(error), error.status) from None
        with self.maintenance._lock, self.store.lock:
            with self._transaction():
                row = self._row(serial)
            if row is None or row['state'] == 'released':
                if proof is not None:
                    raise self._error('执行占用已结束，拒绝旧请求', 'stale-lease')
                return
            if row['state'] != 'active':
                raise self._error('开发板执行占用等待恢复；须先核对原操作已停止', 'lease-recovery')
            if not self._matches(row, actor, client_id, proof):
                raise self._error()

    def _idle(self, serial):
        resource = 'board:' + serial
        if self.maintenance._active.get(resource, 0) or self.maintenance.holds(serial):
            raise self._error('开发板仍有操作或恢复占用', 'busy')
        active = self.store.db.execute(
            "SELECT 1 FROM tasks t WHERE t.state IN ('queued','running') AND ("
            'EXISTS(SELECT 1 FROM task_targets x WHERE x.task_id=t.id AND x.serial=?) OR '
            'EXISTS(SELECT 1 FROM task_board_targets x WHERE x.task_id=t.id AND x.serial=?)) LIMIT 1',
            (serial, serial)).fetchone()
        if active:
            raise self._error('开发板后台任务尚未结束', 'busy')

    def claim(self, serial, actor, client_id='', ttl=300):
        self._ttl(ttl)
        if serial not in getattr(self.manager, 'boards', {}):
            raise self._error('执行占用需要已登记的板卡身份', 'not-found', 404)
        token, lease_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.maintenance._lock, self._transaction():
            row = self._row(serial)
            if row is not None and row['state'] != 'released':
                raise self._error('已有板级执行占用；请查询原占用，不重复申请')
            self._idle(serial)
            from .tasks import TaskError
            try:
                self.maintenance._check_attachment(serial, actor, client_id)
            except TaskError as error:
                raise self._error(str(error), 'busy', error.status) from None
            now = time.time()
            generation = row['generation'] + 1 if row else 1
            self.store.db.execute('INSERT OR REPLACE INTO board_input_owners '
                '(serial,id,actor,client_id,generation,token_hash,state,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                (serial, lease_id, actor, client_id if actor == 'web' else '', generation,
                 hashlib.sha256(token.encode()).hexdigest(), 'active', now + ttl, now))
            result = self._public(self._row(serial))
            self.store.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                                  (serial, now, actor, 'input-claim-board', lease_id))
        return {'ok': True, 'input_owner': result,
                'execution_lease': {'id': lease_id, 'generation': generation, 'token': token}}

    def renew(self, serial, actor, client_id, proof, ttl=300):
        self._ttl(ttl)
        proof = self._proof(proof)
        with self.maintenance._lock, self._transaction():
            row = self._row(serial)
            if row is None or row['state'] != 'active' or not self._matches(row, actor, client_id, proof):
                raise self._error('执行占用已过期或被替换，未续期', 'stale-lease')
            now = time.time()
            self.store.db.execute('UPDATE board_input_owners SET expires=?,updated=? WHERE serial=?',
                                  (now + ttl, now, serial))
            return {'ok': True, 'input_owner': self._public(self._row(serial)), 'execution_lease': proof}

    def release(self, serial, actor, client_id, proof, *, stopped=False, reason=''):
        proof = self._proof(proof)
        with self.maintenance._lock, self._transaction():
            row = self._row(serial)
            if row is None or not self._matches(row, actor, client_id, proof):
                raise self._error('只能释放自己的当前执行占用', 'stale-lease')
            if row['state'] == 'released':
                return {'ok': True, 'input_owner': None}
            if row['state'] == 'recovery' and (stopped is not True or not isinstance(reason, str)
                    or not reason.strip() or len(reason.encode()) > 2048):
                raise self._error('请核对原操作停止并填写恢复原因', 'lease-recovery')
            self._idle(serial)
            self._release(serial, actor, row, reason)
            return {'ok': True, 'input_owner': None}

    def recover(self, serial, actor, expected_generation, *, stopped=False, reason=''):
        if actor != 'web':
            raise self._error('仅人工主访问身份可以恢复其他执行者的占用', 'forbidden', 403)
        if (type(expected_generation) is not int or stopped is not True or not isinstance(reason, str)
                or not reason.strip() or len(reason.encode()) > 2048):
            raise self._error('恢复需要原占用代次、停止确认与原因', 'invalid-lease', 400)
        with self.maintenance._lock, self._transaction():
            row = self._row(serial)
            if row is None or row['generation'] != expected_generation or row['state'] == 'released':
                raise self._error('原占用已变化，请重新观察', 'stale-lease')
            # Fence the old owner before asking it to stop. The caller retries
            # recovery after observing termination; no input is replayed here.
            self.store.db.execute("UPDATE board_input_owners SET state='recovery',updated=? WHERE serial=?",
                                  (time.time(), serial))
        with self.maintenance._lock, self._transaction():
            current = self._row(serial)
            if (current is None or current['id'] != row['id'] or current['generation'] != row['generation']
                    or current['state'] != 'recovery'):
                raise self._error('原占用已变化，请重新观察', 'stale-lease')
            self._idle(serial)
            self._release(serial, actor, current, reason)
        return {'ok': True, 'input_owner': None}

    def _release(self, serial, actor, row, reason):
        now = time.time()
        changed = self.store.db.execute("UPDATE board_input_owners SET state='released',updated=? "
            "WHERE serial=? AND id=? AND generation=? AND state!='released'",
            (now, serial, row['id'], row['generation'])).rowcount
        if changed != 1:
            raise self._error('原占用已变化，请重新观察', 'stale-lease')
        self.store.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                              (serial, now, actor, 'input-release-board', row['id'] + (': ' + reason if reason else '')))

    def fence_actor(self, actor):
        with self.maintenance._lock, self._transaction():
            self.store.db.execute("UPDATE board_input_owners SET state='recovery',updated=? "
                                  "WHERE actor=? AND state='active'", (time.time(), actor))
