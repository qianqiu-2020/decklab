"""Persistent, revocable bearer grants for shared agent access."""
import hashlib
import os
import secrets
import sqlite3
import threading
import time


class AgentGrantStore:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS grants (
            id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
            actor TEXT NOT NULL, scope TEXT NOT NULL, serial TEXT NOT NULL,
            created INTEGER NOT NULL, expires INTEGER, revoked INTEGER)''')
        self.db.commit()

    def issue(self, scope, serial='', ttl=None):
        if scope not in ('all', 'device') or (scope == 'device' and not serial):
            raise ValueError('invalid grant scope')
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise ValueError('invalid grant lifetime')
        grant_id = secrets.token_hex(8)
        token = 'seriald-ag-' + secrets.token_urlsafe(32)
        now = int(time.time())
        grant = {'id': grant_id, 'actor': 'shared-agent/' + grant_id,
                 'scope': scope, 'serial': serial if scope == 'device' else '',
                 'created': now, 'expires': now + ttl if ttl else None, 'revoked': None}
        with self.lock:
            self.db.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,?)',
                            (grant_id, hashlib.sha256(token.encode()).hexdigest(),
                             grant['actor'], scope, grant['serial'], now, grant['expires'], None))
            self.db.commit()
        return token, grant

    def lookup(self, token):
        if not isinstance(token, str) or not token.startswith('seriald-ag-') or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT id,actor,scope,serial,created,expires,revoked '
                                  'FROM grants WHERE token_hash=?', (digest,)).fetchone()
        if not row or row['revoked'] is not None or (row['expires'] is not None and row['expires'] <= time.time()):
            return None
        return dict(row)

    def list(self):
        with self.lock:
            rows = self.db.execute('SELECT id,actor,scope,serial,created,expires,revoked '
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
