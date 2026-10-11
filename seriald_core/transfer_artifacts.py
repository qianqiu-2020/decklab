"""Persistent host-side transfer files; request names never select host paths.

One collector owns the spool. SQLite reserves declared sizes before files are
created; per-artifact locks serialize IO without holding the registry during
hashing. Task runners claim sealed uploads or write downloads before publishing.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time
import weakref

from .archive_io import open_archive
from .tasks import TaskError


@dataclass(frozen=True)
class ArtifactPolicy:
    max_files: int = 128
    max_file: int = 8 * 1024 ** 3
    max_total: int = 32 * 1024 ** 3
    reserve_free: int = 256 * 1024 ** 2
    idle_seconds: int = 3600
    ready_seconds: int = 7 * 86400

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value <= 2 ** 63 - 1 for value in vars(self).values()):
            raise ValueError('artifact limits must be nonnegative integers')
        if not self.max_files or not self.max_file or self.max_total < self.max_file or not self.idle_seconds or not self.ready_seconds:
            raise ValueError('invalid artifact limits')

    @classmethod
    def from_env(cls, environment=None):
        environment = os.environ if environment is None else environment
        keys = {'max_files': 'SERIALD_TRANSFER_MAX_FILES',
                'max_file': 'SERIALD_TRANSFER_MAX_FILE_BYTES',
                'max_total': 'SERIALD_TRANSFER_MAX_TOTAL_BYTES',
                'reserve_free': 'SERIALD_TRANSFER_MIN_FREE_BYTES',
                'idle_seconds': 'SERIALD_TRANSFER_IDLE_SECONDS',
                'ready_seconds': 'SERIALD_TRANSFER_RETENTION_SECONDS'}
        values = {}
        for field, key in keys.items():
            if key in environment:
                raw = environment[key]
                if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw):
                    raise ValueError(key + ' must be a nonnegative integer')
                values[field] = int(raw)
        return cls(**values)


class ArtifactReader:
    """Keep an active file out of expiry until its stream closes, including Windows."""
    def __init__(self, source, release):
        self.source, self.release = source, release
        self.lock, self.released = threading.Lock(), False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.source, name)

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        with self.lock:
            if self.released:
                return
            self.released = True
            try:
                self.source.close()
            finally:
                self.release()


class TransferArtifacts:
    live_states = ('receiving', 'sealing', 'ready', 'claimed', 'writing', 'available')

    def __init__(self, store, directory, policy=None):
        self.store, self.root = store, Path(directory).absolute()
        self.policy = policy or ArtifactPolicy.from_env()
        self._guard, self._locks = threading.Lock(), weakref.WeakValueDictionary()
        self._readers = {}

    @contextmanager
    def _lock(self, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(r'[0-9a-f]{24}', artifact_id):
            raise TaskError('artifact not found', 404)
        with self._guard:
            lock = self._locks.get(artifact_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[artifact_id] = lock
        with lock:
            yield

    def _path(self, artifact_id, suffix):
        return self.root / (artifact_id + suffix)

    def _directory(self):
        self.root.mkdir(mode=0o770, parents=True, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(self.root).st_mode):
            raise TaskError('artifact directory is not a regular directory', 503)

    def get(self, artifact_id, actor='web', serial=None):
        with self._lock(artifact_id), self.store.lock:
            row = self.store.db.execute('SELECT * FROM transfer_artifacts WHERE id=?', (artifact_id,)).fetchone()
            if row is None or (actor != 'web' and row['actor'] != actor) or (serial is not None and row['serial'] != serial):
                raise TaskError('artifact not found', 404)
            return dict(row)

    def create(self, direction, name, size, actor='web', serial='', task_id=None):
        if direction not in ('upload', 'download') or type(size) is not int or not 0 <= size <= self.policy.max_file:
            raise TaskError('invalid artifact direction or size', 413)
        if (not isinstance(name, str) or not name or len(name.encode()) > 255 or
                any(ord(char) < 32 or ord(char) == 127 or char in '/\\' for char in name)):
            raise TaskError('invalid artifact filename')
        if (not isinstance(actor, str) or not actor or len(actor) > 80 or
                not isinstance(serial, str) or len(serial.encode()) > 100):
            raise TaskError('invalid artifact owner')
        if direction == 'download' and not task_id:
            raise TaskError('download artifact requires a task')
        self._directory()
        artifact_id, now = secrets.token_hex(12), time.time()
        path = self._path(artifact_id, '.part')
        created_file = False
        try:
            with self.store.lock, self.store.db:
                placeholders = ','.join('?' for _ in self.live_states)
                count, used, remaining = self.store.db.execute(
                    'SELECT COUNT(*),COALESCE(SUM(size),0),COALESCE(SUM(size-received),0) FROM transfer_artifacts '
                    'WHERE state IN (' + placeholders + ')', self.live_states).fetchone()
                if count >= self.policy.max_files or used + size > self.policy.max_total:
                    raise TaskError('artifact quota exceeded', 413)
                if shutil.disk_usage(self.root).free < remaining + size + self.policy.reserve_free:
                    raise TaskError('insufficient free space for artifact', 503)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
                created_file = True
                os.close(fd)
                self.store.db.execute('INSERT INTO transfer_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (artifact_id, actor, serial, direction, name, size, 0, '',
                     'receiving' if direction == 'upload' else 'writing', task_id, now, now))
        except Exception:
            if created_file:
                path.unlink(missing_ok=True)
            raise
        return self.get(artifact_id)

    def append(self, artifact_id, offset, data, actor='web', serial=None, task_id=None):
        if type(offset) is not int or offset < 0 or not isinstance(data, bytes) or not 0 < len(data) <= 1024 ** 2:
            raise TaskError('invalid artifact chunk')
        with self._lock(artifact_id):
            row = self.get(artifact_id, actor, serial)
            if row['state'] not in ('receiving', 'writing') or row['task_id'] != task_id:
                raise TaskError('artifact is not writable', 409)
            if offset > row['received'] or offset + len(data) > row['size']:
                raise TaskError('artifact offset or size mismatch', 409)
            path = self._path(artifact_id, '.part')
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise TaskError('artifact file is not a private regular file', 409)
            fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'r+b') as output:
                after = os.fstat(output.fileno())
                if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                        not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or after.st_size != row['received']):
                    raise TaskError('artifact file changed', 409)
                output.seek(offset)
                if offset < row['received']:
                    if offset + len(data) > row['received'] or output.read(len(data)) != data:
                        raise TaskError('artifact retry differs from committed bytes', 409)
                    return row
                if shutil.disk_usage(self.root).free < len(data) + self.policy.reserve_free:
                    raise TaskError('insufficient free space for artifact', 503)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._update(artifact_id, received=offset + len(data), updated=time.time())
            return self.get(artifact_id, actor, serial)

    def _update(self, artifact_id, **fields):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE transfer_artifacts SET ' + ','.join(key + '=?' for key in fields)
                                  + ' WHERE id=?', (*fields.values(), artifact_id))

    def seal(self, artifact_id, actor='web', serial=None, task_id=None):
        with self._lock(artifact_id):
            row = self.get(artifact_id, actor, serial)
            if row['state'] in ('ready', 'available') and row['task_id'] == task_id:
                return row
            if (row['state'] not in ('receiving', 'writing') or row['task_id'] != task_id or
                    row['received'] != row['size']):
                raise TaskError('artifact is incomplete or already claimed', 409)
            if os.path.lexists(self._path(artifact_id, '.bin')):
                raise TaskError('sealed artifact path already exists', 409)
            self._update(artifact_id, state='sealing', updated=time.time())
            try:
                digest = hashlib.sha256()
                with open_archive(self._path(artifact_id, '.part')) as source:
                    info = os.fstat(source.fileno())
                    if info.st_size != row['size'] or info.st_nlink != 1:
                        raise TaskError('artifact file size changed', 409)
                    for chunk in iter(lambda: source.read(1024 ** 2), b''):
                        digest.update(chunk)
                os.link(self._path(artifact_id, '.part'), self._path(artifact_id, '.bin'))
                self._path(artifact_id, '.part').unlink()
                self._update(artifact_id, state='ready' if row['direction'] == 'upload' else 'available',
                             sha256=digest.hexdigest(), updated=time.time())
                return self.get(artifact_id, actor, serial)
            except Exception:
                self._update(artifact_id, state='failed', updated=time.time())
                self._unlink(artifact_id)
                raise

    def claim(self, artifact_id, task_id, actor='web', serial=None):
        with self._lock(artifact_id):
            row = self.get(artifact_id, actor, serial)
            if row['direction'] != 'upload' or row['state'] != 'ready' or not task_id:
                raise TaskError('upload artifact is not ready', 409)
            self._update(artifact_id, state='claimed', task_id=task_id, updated=time.time())
            return self.get(artifact_id, actor, serial)

    def open(self, artifact_id, actor='web', serial=None, task_id=None):
        with self._lock(artifact_id):
            row = self.get(artifact_id, actor, serial)
            if row['state'] not in ('ready', 'available', 'claimed') or (
                    row['state'] == 'claimed' and row['task_id'] != task_id):
                raise TaskError('artifact is unavailable', 409)
            source = open_archive(self._path(artifact_id, '.bin'))
            info = os.fstat(source.fileno())
            if info.st_size != row['size'] or info.st_nlink != 1:
                source.close()
                raise TaskError('artifact file size changed', 409)
            self._update(artifact_id, updated=time.time())
            with self._guard:
                self._readers[artifact_id] = self._readers.get(artifact_id, 0) + 1
            def release():
                with self._guard:
                    count = self._readers[artifact_id] - 1
                    if count:
                        self._readers[artifact_id] = count
                    else:
                        del self._readers[artifact_id]
            return ArtifactReader(source, release)

    def remove(self, artifact_id, actor='web', serial=None):
        with self._lock(artifact_id):
            row = self.get(artifact_id, actor, serial)
            with self._guard:
                reading = self._readers.get(artifact_id, 0)
            if reading or row['state'] in ('claimed', 'writing', 'sealing'):
                raise TaskError('artifact is owned by an active task', 409)
            self._update(artifact_id, state='deleted', updated=time.time())
            self._unlink(artifact_id)

    def finish_claim(self, artifact_id, task_id, consumed=False):
        with self._lock(artifact_id):
            row = self.get(artifact_id)
            with self._guard:
                reading = self._readers.get(artifact_id, 0)
            if reading or row['state'] != 'claimed' or row['task_id'] != task_id:
                raise TaskError('artifact task changed or reader is still active', 409)
            self._update(artifact_id, state='consumed' if consumed else 'ready',
                         task_id=task_id if consumed else None, updated=time.time())
            if consumed:
                self._unlink(artifact_id)

    def _unlink(self, artifact_id):
        for suffix in ('.part', '.bin'):
            self._path(artifact_id, suffix).unlink(missing_ok=True)

    def recover(self):
        """Run after task restart recovery; never replay board commands."""
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute('SELECT * FROM transfer_artifacts')]
        for row in rows:
            with self._lock(row['id']):
                state = row['state']
                if state == 'claimed':
                    with self.store.lock:
                        task = self.store.db.execute('SELECT state FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                    self.finish_claim(row['id'], row['task_id'], consumed=bool(task and task[0] == 'completed'))
                elif state in ('writing', 'sealing'):
                    self._update(row['id'], state='failed', updated=time.time())
                    self._unlink(row['id'])
                elif state in ('deleted', 'consumed', 'failed', 'expired'):
                    self._unlink(row['id'])
                if self.get(row['id'])['state'] in ('receiving', 'ready', 'available'):
                    current = self.get(row['id'])
                    suffix = '.part' if current['state'] == 'receiving' else '.bin'
                    try:
                        with open_archive(self._path(row['id'], suffix)) as file:
                            info = os.fstat(file.fileno())
                            valid = info.st_size == current['received'] and info.st_nlink == 1
                    except OSError:
                        valid = False
                    if not valid:
                        self._update(row['id'], state='failed', updated=time.time())
                        self._unlink(row['id'])
        self.expire()

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute(
                "SELECT * FROM transfer_artifacts WHERE state IN ('receiving','ready','available')")]
        for row in rows:
            with self._lock(row['id']):
                with self._guard:
                    if self._readers.get(row['id'], 0):
                        continue
                current = self.get(row['id'])
                ttl = self.policy.idle_seconds if current['state'] == 'receiving' else self.policy.ready_seconds
                if current['state'] not in ('receiving', 'ready', 'available') or current['updated'] + ttl > now:
                    continue
                self._update(row['id'], state='expired', updated=now)
                self._unlink(row['id'])
