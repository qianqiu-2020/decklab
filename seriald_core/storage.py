"""Persistent board identities and session history, independent of USB topology."""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote


class IdentityError(ValueError):
    pass


def serial_component(serial):
    """Reversible encoding, with ordinary hardware serials unchanged."""
    if not isinstance(serial, str) or not serial.strip() or len(serial.encode()) > 100:
        raise IdentityError('设备没有有效的唯一序列号（1–100 字节）')
    serial = serial.strip()
    if any(ord(c) < 32 for c in serial):
        raise IdentityError('序列号含控制字符')
    value = quote(serial, safe='-_')
    return value.replace('.', '%2E') if value in ('.', '..') else value


CONFIRMED_IDENTITY_SOURCES = frozenset(('eeprom-onie', 'verified-correction', 'verified-manual'))


def archive_component(serial, source=''):
    """Return the archive path relative to logs for the identity confidence level."""
    component = serial_component(serial)
    if source in CONFIRMED_IDENTITY_SOURCES:
        return component
    safe_source = source if source in ('device-tree', 'soc-sysfs', 'boot-marker') else 'unknown'
    return os.path.join('_provisional', safe_source, component)


def validate_label(label):
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', label):
        raise IdentityError('标签须为 1–64 位字母、数字、点、下划线或连字符，首位为字母或数字')
    if label.startswith(('pending-', 'unprovisioned-')):
        raise IdentityError('pending- 和 unprovisioned- 前缀保留给待识别连接')
    return label


class Store:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS boards (
                serial TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                info TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                last_path TEXT NOT NULL, last_dev TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY, serial TEXT NOT NULL REFERENCES boards(serial),
                actor TEXT NOT NULL, title TEXT NOT NULL, ip TEXT NOT NULL,
                started REAL NOT NULL, ended REAL, status TEXT NOT NULL,
                completed INTEGER, exit_code INTEGER
            );
            CREATE INDEX IF NOT EXISTS session_board ON sessions(serial, started DESC);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, serial TEXT NOT NULL REFERENCES boards(serial),
                ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_board ON events(serial, id DESC);
            CREATE TABLE IF NOT EXISTS identity_conflicts (
                serial TEXT PRIMARY KEY, detected REAL NOT NULL
            );
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    @staticmethod
    def _board(row):
        if row is None:
            return None
        out = dict(row)
        out['info'] = json.loads(out['info'])
        return out

    def boards(self):
        with self.lock:
            return [self._board(r) for r in self.db.execute('SELECT * FROM boards ORDER BY label')]

    def get(self, serial):
        with self.lock:
            return self._board(self.db.execute('SELECT * FROM boards WHERE serial=?', (serial,)).fetchone())

    def resolve(self, label):
        with self.lock:
            return self._board(self.db.execute('SELECT * FROM boards WHERE label=?', (label,)).fetchone())

    def bind(self, info, path, dev, label=None):
        serial = str(info.get('serial') or '').strip()
        serial_component(serial)
        explicit_label = label is not None
        with self.lock, self.db:
            old = self.get(serial)
            if old:
                label = old['label'] if label is None else validate_label(label)
            else:
                proposed = label or info.get('hostname') or ('board-' + serial_component(serial)[:32])
                try:
                    label = validate_label(proposed)
                except IdentityError:
                    if label is not None:
                        raise
                    label = 'board-' + hashlib.sha256(serial.encode()).hexdigest()[:12]
                if self.resolve(label):
                    if explicit_label:
                        raise IdentityError('标签已被其他设备使用')
                    label = 'board-' + hashlib.sha256(serial.encode()).hexdigest()[:12]
            now = time.time()
            try:
                self.db.execute('''INSERT INTO boards VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(serial) DO UPDATE SET label=excluded.label, info=excluded.info,
                    last_seen=excluded.last_seen, last_path=excluded.last_path, last_dev=excluded.last_dev''',
                    (serial, label, json.dumps(info, ensure_ascii=False),
                     old['first_seen'] if old else now, now, path, dev))
            except sqlite3.IntegrityError as e:
                raise IdentityError('标签已被其他设备使用') from e
            return self.get(serial)

    def rename(self, serial, label):
        label = validate_label(label)
        with self.lock, self.db:
            try:
                cursor = self.db.execute('UPDATE boards SET label=? WHERE serial=?', (label, serial))
            except sqlite3.IntegrityError as e:
                raise IdentityError('标签已被其他设备使用') from e
            if not cursor.rowcount:
                raise IdentityError('请先识别设备序列号')
        return self.get(serial)

    def touch(self, serial):
        with self.lock, self.db:
            now = time.time()
            self.db.execute('UPDATE boards SET last_seen=? WHERE serial=?', (now, serial))
            return now

    def correct_serial(self, old_serial, new_serial):
        """Explicit offline correction after verifying a board's real identity."""
        serial_component(new_serial)
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.get(old_serial)
            if old is None or self.get(new_serial) or self.has_conflict(new_serial):
                raise IdentityError('旧档案不存在，或目标序列号已存在/有冲突')
            info = dict(old['info'], serial=new_serial, serial_source='verified-correction',
                        identity_status='confirmed')
            info.setdefault('reported_serial', old_serial)
            self.db.execute('PRAGMA defer_foreign_keys=ON')
            self.db.execute('UPDATE boards SET serial=?, info=? WHERE serial=?',
                            (new_serial, json.dumps(info, ensure_ascii=False), old_serial))
            for table in ('sessions', 'events'):
                self.db.execute('UPDATE ' + table + ' SET serial=? WHERE serial=?', (new_serial, old_serial))
            self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                            (new_serial, time.time(), 'admin', 'identity-corrected', old_serial))
        return self.get(new_serial)

    def promote_eeprom_identity(self, old_serial, info, path, dev):
        """Replace a provisional identity with the EEPROM identity, preserving history."""
        new_serial = str(info.get('serial') or '').strip()
        serial_component(new_serial)
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.get(old_serial)
            if (old is None or self.get(new_serial) or self.has_conflict(new_serial)
                    or old['info'].get('identity_status') != 'provisional'
                    or info.get('serial_source') != 'eeprom-onie'
                    or info.get('identity_status') != 'confirmed'):
                raise IdentityError('候选档案或 EEPROM 正式身份不满足自动迁移条件')
            self.db.execute('PRAGMA defer_foreign_keys=ON')
            self.db.execute('''UPDATE boards SET serial=?, info=?, last_seen=?,
                last_path=?, last_dev=? WHERE serial=?''',
                (new_serial, json.dumps(info, ensure_ascii=False), time.time(), path, dev,
                 old_serial))
            for table in ('sessions', 'events'):
                self.db.execute('UPDATE ' + table + ' SET serial=? WHERE serial=?',
                                (new_serial, old_serial))
            self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                            (new_serial, time.time(), 'service', 'identity-promoted', old_serial))
        return self.get(new_serial)

    def confirm_serial(self, serial):
        """Mark a manually verified identity as canonical without changing its value."""
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.get(serial)
            if old is None or self.has_conflict(serial):
                raise IdentityError('档案不存在，或该序列号有冲突')
            if old['info'].get('serial_source') in CONFIRMED_IDENTITY_SOURCES:
                raise IdentityError('该序列号已经是正式身份')
            info = dict(old['info'], serial=serial, serial_source='verified-manual',
                        identity_status='confirmed')
            self.db.execute('UPDATE boards SET info=? WHERE serial=?',
                            (json.dumps(info, ensure_ascii=False), serial))
            self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                            (serial, time.time(), 'admin', 'identity-confirmed',
                             old['info'].get('serial_source', '')))
        return self.get(serial)

    def mark_conflict(self, serial):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO identity_conflicts VALUES(?,?)', (serial, time.time()))

    def has_conflict(self, serial):
        with self.lock:
            return self.db.execute('SELECT 1 FROM identity_conflicts WHERE serial=?', (serial,)).fetchone() is not None

    def open_session(self, sid, serial, actor, title, ip):
        with self.lock, self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,NULL,?,NULL,NULL)',
                            (sid, serial, actor[:80], title[:2048], ip, time.time(), 'active'))

    def close_session(self, sid):
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET ended=?, status=CASE WHEN completed=1 THEN 'completed' WHEN completed=0 THEN 'unknown' ELSE 'closed' END WHERE sid=?",
                            (time.time(), sid))

    def session_result(self, serial, sid, completed, exit_code):
        if type(completed) is not bool or (exit_code is not None and
                (type(exit_code) is not int or not 0 <= exit_code <= 255)):
            raise IdentityError('非法执行结果')
        if completed != (exit_code is not None):
            raise IdentityError('completed 与 exit_code 不一致')
        with self.lock, self.db:
            cur = self.db.execute('UPDATE sessions SET completed=?, exit_code=?, status=? WHERE serial=? AND sid=?',
                (completed, exit_code, 'completed' if completed else 'unknown', serial, sid))
            if not cur.rowcount:
                raise IdentityError('会话不存在')

    def recover_sessions(self):
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET status=CASE WHEN completed=1 THEN 'completed' ELSE 'interrupted' END, ended=? WHERE ended IS NULL", (time.time(),))

    def sessions(self, serial, offset=0, limit=50):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                'SELECT * FROM sessions WHERE serial=? ORDER BY started DESC, sid LIMIT ? OFFSET ?',
                (serial, limit, offset))]

    def session_count(self, serial):
        with self.lock:
            return self.db.execute(
                'SELECT COUNT(*) FROM sessions WHERE serial=?', (serial,)).fetchone()[0]

    def event(self, serial, actor, action, detail=''):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                            (serial, time.time(), actor[:80], action, detail[:2048]))

    def events(self, serial, limit=50):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                'SELECT * FROM events WHERE serial=? ORDER BY id DESC LIMIT ?', (serial, limit))]

    def visible_events(self, serial, offset=0, limit=50):
        """Page human-level operations; per-keystroke writes remain in the audit database."""
        with self.lock:
            return [dict(r) for r in self.db.execute('''
                SELECT * FROM events WHERE serial=? AND action<>'session-send'
                ORDER BY id DESC LIMIT ? OFFSET ?''', (serial, limit, offset))]

    def visible_event_count(self, serial):
        with self.lock:
            return self.db.execute('''SELECT COUNT(*) FROM events
                WHERE serial=? AND action<>'session-send' ''', (serial,)).fetchone()[0]
