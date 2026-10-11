"""Versioned SQLite upgrades with a committed pre-upgrade snapshot."""
import os
from pathlib import Path
import secrets
import sqlite3
import time


class SchemaError(RuntimeError):
    pass


def _registry_initial(db):
    # execute(), not executescript(): DDL must remain inside the upgrade transaction.
    for statement in (
        'CREATE TABLE IF NOT EXISTS boards (serial TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE, '
        'info TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, '
        'last_path TEXT NOT NULL, last_dev TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, '
        'serial TEXT NOT NULL REFERENCES boards(serial), actor TEXT NOT NULL, title TEXT NOT NULL, '
        'ip TEXT NOT NULL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, '
        'serial TEXT NOT NULL REFERENCES boards(serial), ts REAL NOT NULL, '
        'actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)',
        'CREATE INDEX IF NOT EXISTS session_board ON sessions(serial, started DESC)',
        'CREATE INDEX IF NOT EXISTS event_board ON events(serial, id DESC)',
    ):
        db.execute(statement)


def _registry_history(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
    for column in ('completed', 'exit_code'):
        if column not in columns:
            db.execute('ALTER TABLE sessions ADD COLUMN ' + column + ' INTEGER')
    db.execute('CREATE TABLE IF NOT EXISTS identity_conflicts '
               '(serial TEXT PRIMARY KEY, detected REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS commands (serial TEXT NOT NULL REFERENCES boards(serial), '
               'command TEXT NOT NULL, first_used REAL NOT NULL, last_used REAL NOT NULL, '
               'use_count INTEGER NOT NULL, PRIMARY KEY(serial, command))')
    db.execute('CREATE INDEX IF NOT EXISTS command_board '
               'ON commands(serial, use_count DESC, last_used DESC)')


def _grants_initial(db):
    db.execute('CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, '
               'actor TEXT NOT NULL, scope TEXT NOT NULL, serial TEXT NOT NULL, '
               'created INTEGER NOT NULL, expires INTEGER, revoked INTEGER)')


def _registry_tasks(db):
    db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, '
               'client_id TEXT NOT NULL, state TEXT NOT NULL, progress TEXT NOT NULL, result TEXT, '
               'error TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, '
               'cancel_requested INTEGER NOT NULL DEFAULT 0)')
    # Historical targets survive board identity corrections; they are not live bindings.
    db.execute('CREATE TABLE IF NOT EXISTS task_targets (task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, '
               'serial TEXT NOT NULL, connection_id TEXT NOT NULL, '
               'PRIMARY KEY(task_id, serial, connection_id))')
    db.execute('CREATE INDEX IF NOT EXISTS task_history ON tasks(created DESC, id DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS task_board ON task_targets(serial, task_id)')


def _registry_artifacts(db):
    db.execute('CREATE TABLE IF NOT EXISTS transfer_artifacts ('
               'id TEXT PRIMARY KEY, actor TEXT NOT NULL, serial TEXT NOT NULL, '
               'direction TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, '
               'received INTEGER NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL, '
               'task_id TEXT REFERENCES tasks(id), created REAL NOT NULL, updated REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS artifact_expiry ON transfer_artifacts(state,updated)')


def _registry_connections(db):
    db.execute('CREATE TABLE IF NOT EXISTS connection_identities ('
               'connection_id TEXT PRIMARY KEY, serial TEXT NOT NULL REFERENCES boards(serial), '
               'identified REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS connection_board ON connection_identities(serial)')


REGISTRY_MIGRATIONS = (_registry_initial, _registry_history, _registry_tasks, _registry_artifacts,
                       _registry_connections)
GRANT_MIGRATIONS = (_grants_initial,)
_SCHEMAS = {
    'registry': (0x444C5247, REGISTRY_MIGRATIONS, {
        'boards': 'serial label info first_seen last_seen last_path last_dev',
        'sessions': 'sid serial actor title ip started ended status completed exit_code',
        'events': 'id serial ts actor action detail',
        'identity_conflicts': 'serial detected',
        'commands': 'serial command first_used last_used use_count',
        'tasks': 'id kind actor client_id state progress result error created updated cancel_requested',
        'task_targets': 'task_id serial connection_id',
        'transfer_artifacts': 'id actor serial direction name size received sha256 state task_id created updated',
        'connection_identities': 'connection_id serial identified',
    }),
    'grants': (0x444C4147, GRANT_MIGRATIONS, {
        'grants': 'id token_hash actor scope serial created expires revoked',
    }),
}


def _backup(db_path, old_version, target_version):
    """A second reader sees committed WAL data while the upgrader holds its write lock."""
    source = Path(db_path).resolve()
    directory = source.parent / 'backups'
    directory.mkdir(exist_ok=True)
    name = '{}.v{}-to-v{}.{}.{}.sqlite3'.format(
        source.name, old_version, target_version, time.strftime('%Y%m%d-%H%M%S'), secrets.token_hex(6))
    final = directory / name
    temporary = directory / (name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            destination = sqlite3.connect(str(temporary))
            try:
                reader.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise SchemaError('pre-upgrade backup failed integrity check')
            finally:
                destination.close()
        finally:
            reader.close()
        with temporary.open('rb') as file:
            os.fsync(file.fileno())
        os.replace(temporary, final)
        return str(final)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(db, path, kind):
    """Adopt unversioned databases and migrate forward atomically; never downgrade."""
    application_id, migrations, expected = _SCHEMAS[kind]
    target = len(migrations)
    backup = None
    try:
        db.execute('BEGIN IMMEDIATE')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        owner = db.execute('PRAGMA application_id').fetchone()[0]
        if owner not in (0, application_id):
            raise SchemaError('database belongs to a different application')
        if version > target:
            raise SchemaError('database schema {} is newer than supported {}; refusing downgrade'.format(version, target))
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if version == 0 and tables and (not tables.issubset(expected) or
                ('boards' if kind == 'registry' else 'grants') not in tables):
            raise SchemaError('unrecognized unversioned database layout')
        if version < target and tables and path != ':memory:':
            backup = _backup(path, version, target)
        for index in range(version, target):
            migrations[index](db)
            db.execute('PRAGMA user_version={}'.format(index + 1))
        for table, required in expected.items():
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if not set(required.split()).issubset(columns):
                raise SchemaError('incomplete {} schema: {}'.format(kind, table))
        if version < target and db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise SchemaError('database contains invalid board references')
        db.execute('PRAGMA application_id={}'.format(application_id))
        db.commit()
        return backup
    except Exception as error:
        db.rollback()
        suffix = '; pre-upgrade backup: ' + backup if backup else ''
        raise SchemaError('{} database upgrade failed: {}{}'.format(kind, error, suffix)) from error
