"""Persistent, revocable bearer grants for shared agent access."""
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from .schema import initialize


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
        token = 'seriald-ag-' + secrets.token_urlsafe(32)
        now = int(time.time())
        grant = {'id': grant_id, 'actor': 'shared-agent/' + grant_id,
                 'scope': scope, 'serial': serial if scope == 'device' else '',
                 'key': key if scope == 'connection' else '',
                 'connection_id': connection_id if scope == 'connection' else '',
                 'created': now, 'expires': now + ttl if ttl else None, 'revoked': None}
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
        if not isinstance(token, str) or not token.startswith('seriald-ag-') or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT id,actor,scope,serial,created,expires,revoked,key,connection_id '
                                  'FROM grants WHERE token_hash=?', (digest,)).fetchone()
        if not row or row['revoked'] is not None or (row['expires'] is not None and row['expires'] <= time.time()):
            return None
        return dict(row)

    def list(self):
        with self.lock:
            rows = self.db.execute('SELECT id,actor,scope,serial,created,expires,revoked,key,connection_id '
                                   'FROM grants ORDER BY created DESC, id DESC LIMIT 200').fetchall()
        return [dict(row) for row in rows]

    def revoke(self, grant_id):
        with self.lock:
            row = self.db.execute('SELECT actor FROM grants WHERE id=? AND revoked IS NULL',
                                  (grant_id,)).fetchone()
            if not row:
                return None
            self.db.execute('UPDATE grants SET revoked=? WHERE id=?', (int(time.time()), grant_id))
            self.db.commit()
        return row['actor']

    def close(self):
        with self.lock:
            self.db.close()
