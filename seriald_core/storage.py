"""Persistent board identities and session history, independent of USB topology."""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote
from .schema import initialize


class IdentityError(ValueError):
    pass


class IdentityConfirmationRequired(IdentityError):
    def __init__(self, message, confirmation):
        super().__init__(message)
        self.confirmation = confirmation


def serial_component(serial):
    """Reversible encoding, with ordinary hardware serials unchanged."""
    if not isinstance(serial, str) or not serial.strip() or len(serial.encode()) > 100:
        raise IdentityError('设备没有有效的唯一序列号（1–100 字节）')
    serial = serial.strip()
    if any(ord(c) < 32 for c in serial):
        raise IdentityError('序列号含控制字符')
    value = quote(serial, safe='-_')
    return value.replace('.', '%2E') if value in ('.', '..') else value


CONFIRMED_IDENTITY_SOURCES = frozenset(
    ('eeprom-onie', 'device-tree', 'uboot-injected', 'verified-correction', 'verified-manual'))


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


def validate_conflict_reason(reason):
    if (not isinstance(reason, str) or not reason.strip()
            or len(reason.encode('utf-8')) > 2048
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in reason)):
        raise IdentityError('解除冲突须填写核实原因（1–2048 字节，不含控制字符）')
    return reason.strip()


def _private_registry_files(path):
    """Protect secrets before SQLite opens or creates its sidecar files.

    SQLite creates journals/WAL with the database's permissions. Existing
    sidecars from an older install need tightening as well. Windows access is
    governed by the runtime directory's inherited ACL, rather than POSIX modes.
    """
    if path == ':memory:' or os.name != 'posix':
        return
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            os.chmod(path + suffix, 0o600)
        except FileNotFoundError:
            pass


class Store:
    def __init__(self, path):
        path = os.fspath(path)
        self.path = path
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _private_registry_files(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            self.schema_backup = initialize(self.db, path, 'registry')
            self.db.execute('PRAGMA journal_mode=WAL')
            _private_registry_files(path)
        except Exception:
            self.db.close()
            raise

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

    def attachment_label(self, port):
        with self.lock:
            row = self.db.execute('SELECT label FROM attachment_labels WHERE port=?', (port,)).fetchone()
        return row['label'] if row else ''

    def rename_attachment(self, port, label):
        label = validate_label(label)
        if (not isinstance(port, str) or not port or len(port.encode()) > 2048
                or any(ord(char) < 32 for char in port)):
            raise IdentityError('当前串口没有有效的连接位置')
        with self.lock, self.db:
            self.db.execute('INSERT INTO attachment_labels VALUES(?,?) '
                            'ON CONFLICT(port) DO UPDATE SET label=excluded.label', (port, label))
        return label

    def get_ssh_credentials(self, serial):
        """Private connection input; never include this result in an API record."""
        if not isinstance(serial, str) or not serial:
            return None
        with self.lock:
            row = self.db.execute(
                'SELECT ssh_user,ssh_password,revision FROM ssh_credentials WHERE serial=? '
                'AND NOT EXISTS (SELECT 1 FROM identity_conflicts WHERE serial=?)',
                (serial, serial)).fetchone()
        return dict(row) if row else None

    def get_ssh_credentials_metadata(self, serial):
        """Public settings status deliberately selects no password column."""
        if not isinstance(serial, str) or not serial:
            return {'saved': False, 'user': '', 'revision': 0}
        with self.lock:
            row = self.db.execute(
                'SELECT ssh_user,revision FROM ssh_credentials WHERE serial=? '
                'AND NOT EXISTS (SELECT 1 FROM identity_conflicts WHERE serial=?)',
                (serial, serial)).fetchone()
        return ({'saved': True, 'user': row['ssh_user'], 'revision': row['revision']} if row
                else {'saved': False, 'user': '', 'revision': 0})

    def save_ssh_credentials(self, serial, ssh_user, ssh_password, connection_id, expected_revision):
        """Save verified credentials only for the proven board and unchanged settings.

        The connection caller still guards its live generation and cancellation
        under the device operation lock. This transaction checks its persistent
        identity proof and prevents a slower login overwriting newer settings.
        """
        from .session_base import valid_ssh_user, valid_ssh_password
        if not valid_ssh_user(ssh_user) or not valid_ssh_password(ssh_password):
            raise ValueError('invalid SSH credentials')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('invalid SSH credentials revision')
        if (not isinstance(serial, str) or not serial or not isinstance(connection_id, str)
                or not connection_id or len(connection_id) > 80):
            return False
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            owner = self.db.execute(
                'SELECT 1 FROM connection_identities JOIN boards USING(serial) '
                'WHERE connection_id=? AND serial=? '
                'AND NOT EXISTS (SELECT 1 FROM identity_conflicts WHERE serial=?)',
                (connection_id, serial, serial)).fetchone()
            if owner is None:
                return False
            row = self.db.execute('SELECT revision FROM ssh_credentials WHERE serial=?',
                                  (serial,)).fetchone()
            revision = row['revision'] if row else 0
            if revision != expected_revision:
                return False
            self.db.execute(
                'INSERT INTO ssh_credentials(serial,ssh_user,ssh_password,revision) VALUES (?,?,?,?) '
                'ON CONFLICT(serial) DO UPDATE SET ssh_user=excluded.ssh_user, '
                'ssh_password=excluded.ssh_password,revision=excluded.revision',
                (serial, ssh_user, ssh_password, revision + 1))
        return True

    def get_probe_settings(self, port):
        """Port preferences do not assert a persistent board identity."""
        with self.lock:
            row = self.db.execute('SELECT document FROM probe_settings WHERE port=?', (port,)).fetchone()
        return json.loads(row['document']) if row else None

    def set_probe_settings(self, port, document):
        from .profiles import validate_probe_settings
        if not isinstance(port, str) or not port or len(port) > 4096 or '\x00' in port:
            raise ValueError('invalid probe settings port')
        settings = validate_probe_settings(document)
        with self.lock, self.db:
            self.db.execute('INSERT INTO probe_settings(port,document) VALUES (?,?) '
                            'ON CONFLICT(port) DO UPDATE SET document=excluded.document',
                            (port, json.dumps(settings, ensure_ascii=False)))
        return settings

    def bind(self, info, path, dev, label=None, connection_id=None, conflict_resolution=None):
        serial = str(info.get('serial') or '').strip()
        serial_component(serial)
        explicit_label = label is not None
        with self.lock, self.db:
            if conflict_resolution is not None:
                self.db.execute('BEGIN IMMEDIATE')
                conflict = self.db.execute('SELECT detected FROM identity_conflicts WHERE serial=?',
                                           (serial,)).fetchone()
                if conflict is None or conflict['detected'] != conflict_resolution['detected_at']:
                    raise IdentityError('冲突记录已变化，请重新识别并确认')
                self._resolve_conflict(serial, conflict_resolution['reason'],
                                       conflict_resolution['actor'])
            old = self.get(serial)
            if old:
                label = old['label'] if label is None else validate_label(label)
            else:
                proposed = label or self.attachment_label(path) or info.get('hostname') or ('board-' + serial_component(serial)[:32])
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
            if connection_id is not None:
                self._bind_connection(connection_id, serial)
            return self.get(serial)

    def _bind_connection(self, connection_id, serial):
        """Called inside the identity transaction; a generation cannot change boards."""
        if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 80:
            raise IdentityError('invalid connection identity')
        current = self.db.execute('SELECT serial FROM connection_identities WHERE connection_id=?',
                                  (connection_id,)).fetchone()
        if current and current[0] != serial:
            raise IdentityError('connection already belongs to another board; prepare a board switch first')
        self.db.execute('INSERT OR IGNORE INTO connection_identities VALUES(?,?,?)',
                        (connection_id, serial, time.time()))
        # Only task-owned artifacts have a proven generation. Unsubmitted staging
        # has no connection proof and must never be guessed from its USB path.
        self.db.execute("UPDATE transfer_artifacts SET serial=? WHERE serial='' AND task_id IN "
                        '(SELECT task_id FROM task_targets WHERE connection_id=? GROUP BY task_id '
                        'HAVING (SELECT COUNT(*) FROM task_targets t WHERE t.task_id=task_targets.task_id)=1)',
                        (serial, connection_id))

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

    def _migrate_identity_references(self, old_serial, new_serial):
        """Migrate proven ownership inside the caller's identity transaction."""
        if self.db.execute('SELECT 1 FROM flash_resource_holds WHERE resource=?',
                           ('board:' + old_serial,)).fetchone():
            raise IdentityError('开发板刷机维护占用尚未释放，不能修改身份')
        self.db.execute('INSERT OR IGNORE INTO connection_identities '
                        'SELECT DISTINCT connection_id,serial,? FROM task_targets WHERE serial=?',
                        (time.time(), old_serial))
        for table in ('sessions', 'events', 'commands', 'connection_identities', 'transfer_artifacts'):
            self.db.execute('UPDATE ' + table + ' SET serial=? WHERE serial=?',
                            (new_serial, old_serial))

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
            self._migrate_identity_references(old_serial, new_serial)
            self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                            (new_serial, time.time(), 'admin', 'identity-corrected', old_serial))
        return self.get(new_serial)

    def promote_eeprom_identity(self, old_serial, info, path, dev, connection_id=None):
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
            self._migrate_identity_references(old_serial, new_serial)
            if connection_id is not None:
                self._bind_connection(connection_id, new_serial)
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

    def resolve_conflict(self, serial, reason):
        """Explicit offline recovery after verifying uniqueness and archive ownership."""
        serial_component(serial)
        reason = validate_conflict_reason(reason)
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return self._resolve_conflict(serial, reason, 'admin')

    def _resolve_conflict(self, serial, reason, actor):
        """Called only inside the caller's registry write transaction."""
        reason = validate_conflict_reason(reason)
        if self.get(serial) is None:
            raise IdentityError('档案不存在，无法核实冲突归属')
        conflict = self.db.execute('SELECT detected FROM identity_conflicts WHERE serial=?',
                                   (serial,)).fetchone()
        if conflict is None:
            return False
        detail = json.dumps({'reason': reason, 'detected_at': conflict['detected']},
                            ensure_ascii=False)
        self.db.execute('INSERT INTO events(serial,ts,actor,action,detail) VALUES(?,?,?,?,?)',
                        (serial, time.time(), actor, 'identity-conflict-resolved', detail))
        self.db.execute('DELETE FROM identity_conflicts WHERE serial=?', (serial,))
        return True

    def open_session(self, sid, serial, actor, title, ip):
        with self.lock, self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,NULL,?,NULL,NULL)',
                            (sid, serial, actor[:80], title[:2048], ip, time.time(), 'active'))

    def close_session(self, sid):
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET ended=?, status=CASE WHEN completed=1 THEN 'completed' WHEN completed=0 THEN 'unknown' ELSE 'closed' END WHERE sid=?",
                            (time.time(), sid))

    def session_result(self, serial, sid, completed, exit_code, actor=None):
        if type(completed) is not bool or (exit_code is not None and
                (type(exit_code) is not int or not 0 <= exit_code <= 255)):
            raise IdentityError('非法执行结果')
        if completed != (exit_code is not None):
            raise IdentityError('completed 与 exit_code 不一致')
        with self.lock, self.db:
            cur = self.db.execute('UPDATE sessions SET completed=?, exit_code=?, status=? WHERE serial=? AND sid=?'
                                  + (' AND actor=?' if actor is not None else ''),
                (completed, exit_code, 'completed' if completed else 'unknown', serial, sid)
                + ((actor,) if actor is not None else ()))
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

    def record_command(self, serial, command):
        command = str(command).strip()
        if (not command or len(command.encode('utf-8')) > 2048
                or any(ord(char) < 32 and char != '\t' for char in command)):
            return False
        now = time.time()
        with self.lock, self.db:
            self.db.execute('''INSERT INTO commands VALUES(?,?,?,?,1)
                ON CONFLICT(serial,command) DO UPDATE SET
                last_used=excluded.last_used, use_count=commands.use_count+1''',
                (serial, command, now, now))
        return True

    def commands(self, serial, limit=20):
        with self.lock:
            return [dict(row) for row in self.db.execute('''
                SELECT command, first_used, last_used, use_count FROM commands
                WHERE serial=? ORDER BY use_count DESC, last_used DESC LIMIT ?''',
                (serial, limit))]
