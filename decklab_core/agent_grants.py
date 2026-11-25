"""Persistent, revocable bearer grants for shared agent access."""
import hashlib
from contextlib import contextmanager
import os
import re
import secrets
import sqlite3
import threading
import time
from .compat import GRANT_PREFIXES
from .schema import initialize


GRANT_FIELDS = ('id,actor,scope,serial,created,expires,revoked,key,connection_id,'
                'revocation_generation,backend_confirmed_generation')


class GrantRevoked(RuntimeError):
    code, status = 'authorization-revoked', 403


def validate_revocation(actor, generation):
    if (not isinstance(actor, str) or not re.fullmatch(r'shared-agent/[0-9a-f]{16}', actor)
            or type(generation) is not int or not 1 <= generation <= 2 ** 63 - 1):
        raise ValueError('invalid agent revocation')


class AgentGrantStore:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            self.schema_backup = initialize(self.db, path, 'grants')
            self.db.execute('PRAGMA journal_mode=WAL')
        except Exception:
            self.db.close()
            raise

    def issue(self, scope, serial='', ttl=None, key='', connection_id=''):
        if scope not in ('all', 'device', 'connection') or (scope == 'device' and not serial):
            raise ValueError('invalid grant scope')
        if scope == 'connection' and (not isinstance(key, str) or not key or len(key) > 256
                or not isinstance(connection_id, str) or not connection_id or len(connection_id) > 80):
            raise ValueError('invalid connection grant target')
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise ValueError('invalid grant lifetime')
        grant_id = secrets.token_hex(8)
        token = 'decklab-ag-' + secrets.token_urlsafe(32)
        now = int(time.time())
        grant = {'id': grant_id, 'actor': 'shared-agent/' + grant_id,
                 'scope': scope, 'serial': serial if scope == 'device' else '',
                 'key': key if scope == 'connection' else '',
                 'connection_id': connection_id if scope == 'connection' else '',
                 'created': now, 'expires': now + ttl if ttl else None, 'revoked': None,
                 'revocation_generation': 0, 'backend_confirmed_generation': 0}
        with self.lock:
            self.db.execute('INSERT INTO grants '
                            '(id,token_hash,actor,scope,serial,created,expires,revoked,key,connection_id) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (grant_id, hashlib.sha256(token.encode()).hexdigest(),
                             grant['actor'], scope, grant['serial'], now, grant['expires'], None,
                             grant['key'], grant['connection_id']))
            self.db.commit()
        return token, grant

    def lookup(self, token):
        if not isinstance(token, str) or not token.startswith(GRANT_PREFIXES) or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT ' + GRANT_FIELDS + ' '
                                  'FROM grants WHERE token_hash=?', (digest,)).fetchone()
        if not row or row['revoked'] is not None or (row['expires'] is not None and row['expires'] <= time.time()):
            return None
        return dict(row)

    def list(self):
        with self.lock:
            rows = self.db.execute('SELECT ' + GRANT_FIELDS + ' '
                                   'FROM grants ORDER BY created DESC, id DESC LIMIT 200').fetchall()
        return [dict(row) for row in rows]

    def revoke(self, grant_id):
        """Compatibility helper; every first revocation also queues backend delivery."""
        result = self.revoke_request(grant_id, active_only=True)
        return result['actor'] if result else None

    def revoke_request(self, grant_id, *, active_only=False):
        """Commit token denial and its durable notification in one transaction."""
        with self.lock:
            row = self.db.execute('SELECT ' + GRANT_FIELDS + ' FROM grants WHERE id=?',
                                  (grant_id,)).fetchone()
            if not row or active_only and row['revoked'] is not None:
                return None
            if row['revoked'] is None:
                self.db.execute('UPDATE grants SET revoked=?,revocation_generation=revocation_generation+1 '
                                'WHERE id=? AND revoked IS NULL', (int(time.time()), grant_id))
            self.db.commit()
            return dict(self.db.execute('SELECT ' + GRANT_FIELDS + ' FROM grants WHERE id=?',
                                        (grant_id,)).fetchone())

    def pending_revocations(self):
        with self.lock:
            # Expiration must also stop work already admitted by the backend.
            self.db.execute('UPDATE grants SET revoked=?,revocation_generation=revocation_generation+1 '
                            'WHERE revoked IS NULL AND expires IS NOT NULL AND expires<=?',
                            (int(time.time()), time.time()))
            self.db.commit()
            rows = self.db.execute('SELECT ' + GRANT_FIELDS + ' FROM grants WHERE revoked IS NOT NULL '
                                   'AND backend_confirmed_generation<revocation_generation '
                                   'ORDER BY revoked,id').fetchall()
            return [dict(row) for row in rows]

    def confirm_revocation(self, grant_id, generation):
        if type(generation) is not int or generation <= 0:
            return False
        with self.lock, self.db:
            return bool(self.db.execute('UPDATE grants SET backend_confirmed_generation=? '
                'WHERE id=? AND revoked IS NOT NULL AND revocation_generation=? '
                'AND backend_confirmed_generation<?',
                (generation, grant_id, generation, generation)).rowcount)

    def block_actor(self, actor, generation):
        """Backend fence: persist before cancelling any live actor resources."""
        validate_revocation(actor, generation)
        with self.lock, self.db:
            self.db.execute('INSERT INTO actor_revocations VALUES(?,?,?) '
                            'ON CONFLICT(actor) DO UPDATE SET generation=MAX(generation,excluded.generation),'
                            'updated=excluded.updated', (actor, generation, time.time()))
            return self.db.execute('SELECT generation FROM actor_revocations WHERE actor=?',
                                   (actor,)).fetchone()[0]

    def check_actor(self, actor, generation=None):
        if not isinstance(actor, str) or not actor.startswith('shared-agent/'):
            return
        if generation is not None and (type(generation) is not int or generation != 0):
            raise GrantRevoked('Agent authorization generation is no longer current')
        with self.lock:
            blocked = self.db.execute('SELECT generation FROM actor_revocations WHERE actor=?',
                                      (actor,)).fetchone()
            # On a local collector the original grant and fence share this store.
            grant = self.db.execute('SELECT revoked,expires FROM grants WHERE actor=?', (actor,)).fetchone()
        if blocked or grant and (grant['revoked'] is not None
                                or grant['expires'] is not None and grant['expires'] <= time.time()):
            raise GrantRevoked('Agent authorization has been revoked or expired')

    def close(self):
        with self.lock:
            self.db.close()


def attach_agent_grants(manager, path):
    """The collector and HTTPS backend use the same authorization store format."""
    grants = path if isinstance(path, AgentGrantStore) else AgentGrantStore(path)
    manager.agent_grants = grants
    if getattr(getattr(manager, 'agent_requests', None), 'grants', None) is not grants:
        manager.agent_requests = ActorRequests(grants)
    tasks = getattr(manager, 'tasks', None)
    if tasks is not None:
        tasks.agent_grants = grants
    return grants


class ActorRequests:
    """In-flight synchronous requests are part of the backend stop receipt."""
    def __init__(self, grants):
        self.grants, self.lock, self.active = grants, threading.Lock(), {}

    @contextmanager
    def scope(self, actor):
        with self.grants.lock:
            self.grants.check_actor(actor)
            with self.lock:
                self.active[actor] = self.active.get(actor, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                count = self.active[actor] - 1
                if count:
                    self.active[actor] = count
                else:
                    self.active.pop(actor)
            self.grants.check_actor(actor)

    def actor_stopped(self, actor):
        with self.lock:
            return not self.active.get(actor, 0)


@contextmanager
def actor_request(manager, actor):
    requests = getattr(manager, 'agent_requests', None)
    if requests is None:
        grants = getattr(manager, 'agent_grants', None)
        if grants is not None:
            with grants.lock:
                requests = getattr(manager, 'agent_requests', None)
                if requests is None:
                    requests = manager.agent_requests = ActorRequests(grants)
    if requests is None:
        yield
    else:
        with requests.scope(actor):
            yield


def actor_requests_stopped(manager, actor):
    requests = getattr(manager, 'agent_requests', None)
    return requests is None or requests.actor_stopped(actor)


def revoke_backend_actor(manager, actor, generation):
    """Fence first; acknowledge only a positively observed stopped actor.

    Repeating this call is intentional: cancellation or a lost reply does not
    establish that an old request, worker, or transport finished cleanup.
    """
    from .transactions import transactions_for, TransactionError

    grants = manager.agent_grants
    current = grants.block_actor(actor, generation)
    from .board_ownership import ownership_for
    ownership = ownership_for(manager)
    if ownership is not None:
        ownership.fence_actor(actor)
    closed, stopped = 0, True
    tasks = getattr(manager, 'tasks', None)
    if tasks is not None:
        tasks.cancel_actor(actor)
        check = getattr(tasks, 'actor_stopped', None)
        stopped = bool(check and check(actor))
    desktop = getattr(manager, 'desktop', None)
    if desktop is not None:
        closed += desktop.close_actor(actor)
    for device in manager.all_devices():
        transactions = transactions_for(device)
        active, owner = transactions.snapshot(), transactions.ownership()
        if active and active['actor'] == actor:
            try:
                transactions.cancel(active['id'], actor)
            except TransactionError as error:
                if error.code != 'not-found':
                    raise
        if owner and owner['actor'] == actor:
            try:
                transactions.release(owner['id'], actor)
            except TransactionError as error:
                if error.code != 'not-found':
                    raise
        with device.sessions_lock:
            sessions = list(device.ssh_sessions.items())
        for sid, session in sessions:
            if session.actor == actor:
                with grants.lock:
                    pending = getattr(manager, '_revoking_sessions', None)
                    if pending is None:
                        pending = manager._revoking_sessions = {}
                    pending.setdefault(actor, {})[id(session)] = session
                closed += bool(device.close_session(sid))
            else:
                release = getattr(session, 'revoke_actor', None)
                if release:
                    release(actor)
        active = transactions.snapshot()
        if active and active['actor'] == actor:
            stopped = False
        with device.sessions_lock:
            if any(session.actor == actor for session in device.ssh_sessions.values()):
                stopped = False
        if (active or owner and owner['actor'] == actor) and device.serial:
            manager.store.event(device.serial, 'service', 'agent-revoke', actor)
    with grants.lock:
        pending = getattr(manager, '_revoking_sessions', {})
        sessions = pending.get(actor, {})
        alive = {key: session for key, session in sessions.items()
                 if getattr(session, '_thread', None) is not None and session._thread.is_alive()}
        if alive:
            pending[actor] = alive
            stopped = False
        else:
            pending.pop(actor, None)
    stopped = actor_requests_stopped(manager, actor) and stopped
    return {'ok': True, 'actor': actor, 'revocation_generation': current,
            'sessions_closed': stopped, 'closed_sessions': closed}
