"""Opt-in archive retention and safe disk/diagnostic summaries."""
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
import re
import shutil
import stat
import sys
import threading
import time

from . import __version__


@dataclass(frozen=True)
class MaintenancePolicy:
    retention_days: int = 0
    min_free_mb: int = 1024
    min_free_percent: int = 5

    @classmethod
    def from_env(cls):
        values = []
        for name, default, maximum in (
                ('SERIALD_LOG_RETENTION_DAYS', 0, 36500),
                ('SERIALD_DISK_MIN_FREE_MB', 1024, 1048576),
                ('SERIALD_DISK_MIN_FREE_PERCENT', 5, 100)):
            raw = os.environ.get(name, str(default))
            if not re.fullmatch(r'\d+', raw) or not 0 <= int(raw) <= maximum:
                raise ValueError('%s must be an integer between 0 and %d' % (name, maximum))
            values.append(int(raw))
        return cls(*values)


def disk_status(root, policy):
    try:
        disk = shutil.disk_usage(root)
    except OSError:
        return {'available': False, 'warning': True, 'reason': 'unavailable'}
    percent = disk.free / disk.total * 100 if disk.total else 0
    warning = disk.free < policy.min_free_mb * 1024 * 1024 or percent < policy.min_free_percent
    return {'available': True, 'total': disk.total, 'free': disk.free,
            'free_percent': round(percent, 2), 'warning': warning,
            'reason': 'low-space' if warning else ''}


_DAILY_LOG = re.compile(r'^(?:serial(?:-[^/]*)?|ssh-[^/]+)-(\d{8})\.log$')


class LogMaintenance:
    def __init__(self, policy):
        self.policy = policy
        self._lock = threading.Lock()
        self.last_run = 0
        self.last_result = {'removed': 0, 'bytes': 0, 'errors': 0}

    def snapshot(self):
        with self._lock:
            return dict(self.last_result, last_run=self.last_run, retention_days=self.policy.retention_days)

    def run_due(self, manager, root):
        if not self.policy.retention_days or time.time() - self.last_run < 3600:
            return
        self.prune(manager, root)

    def prune(self, manager, root, now=None):
        """Enumerate outside writer locks; validate/protect each deletion inside them."""
        now = time.time() if now is None else now
        result = {'removed': 0, 'bytes': 0, 'errors': 0}
        if not self.policy.retention_days:
            return result
        cutoff = now - self.policy.retention_days * 86400
        cutoff_day = datetime.fromtimestamp(cutoff).date()
        protected_boot = set()
        unknown_boot_dirs = set()
        # A running boot can span many daily files. Preserve its indexed ranges,
        # rather than leaving only today's continuation after retention.
        with manager.lock:
            for device in manager.all_devices():
                if not device.present or device.fd is None:
                    continue
                try:
                    directory = os.path.abspath(device.stable_dir())
                except OSError:
                    # Without the active archive's location no deletion is safe.
                    result['errors'] += 1
                    with self._lock:
                        self.last_run, self.last_result = now, result
                    return result
                try:
                    boots = device.boot_epochs()
                    if boots:
                        protected_boot.update(os.path.join(directory, part['file']) for part in boots[0]['parts'])
                    else:
                        unknown_boot_dirs.add(directory)
                except OSError:
                    unknown_boot_dirs.add(directory)
                    result['errors'] += 1
        def walk_error(error):
            result['errors'] += 1
        for directory, subdirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            subdirs[:] = [name for name in subdirs if not os.path.islink(os.path.join(directory, name))]
            for name in names:
                match = _DAILY_LOG.fullmatch(name)
                if not match:
                    continue
                try:
                    day = datetime.strptime(match[1], '%Y%m%d').date()
                    if day >= cutoff_day:
                        continue
                    path = os.path.join(directory, name)
                    if (os.path.abspath(path) in protected_boot or name.startswith('serial-')
                            and os.path.abspath(directory) in unknown_boot_dirs):
                        continue
                    candidate = os.lstat(path)
                    if not stat.S_ISREG(candidate.st_mode) or candidate.st_mtime >= cutoff:
                        continue
                    # Identity promotion uses Manager -> registries -> device logs -> SSH logs.
                    # Keep the same order and hold locks only for one candidate, not directory walks.
                    with manager.lock, ExitStack() as locks:
                        devices = manager.all_devices()
                        registries = {id(d._sessions): d._sessions for d in devices}
                        for registry in sorted(registries.values(), key=id):
                            locks.enter_context(registry.lock)
                        for device in sorted(devices, key=id):
                            locks.enter_context(device.log_lock)
                        sessions = {id(s): s for d in devices for s in d.ssh_sessions.values()}
                        for session in sorted(sessions.values(), key=lambda s: (s.sid, id(s))):
                            session_lock = getattr(session, '_log_lock', None)
                            locks.enter_context(session_lock if session_lock is not None else session.log_lock)
                        protected = {os.path.abspath(item.logpath) for item in [*devices, *sessions.values()]
                                     if getattr(item, 'logf', None) is not None and getattr(item, 'logpath', None)}
                        current = os.lstat(path)
                        if (os.path.abspath(path) in protected or not stat.S_ISREG(current.st_mode)
                                or (current.st_dev, current.st_ino) != (candidate.st_dev, candidate.st_ino)
                                or current.st_mtime >= cutoff):
                            continue
                        os.unlink(path)
                        result['removed'] += 1
                        result['bytes'] += current.st_size
                except FileNotFoundError:
                    continue  # Renamed/promoted after enumeration.
                except ValueError:
                    continue  # Unknown/invalid dated filenames are never guessed.
                except OSError:
                    result['errors'] += 1
        with self._lock:
            self.last_run, self.last_result = now, result
        return result


def diagnostic_summary(manager, root, started):
    """Allowlist only: no raw exceptions, labels, addresses, paths, commands or credentials."""
    devices = manager.all_devices()
    boards = []
    for device in devices:
        identity = device.serial or device.key
        with device.sessions_lock:
            sessions = list(device.ssh_sessions.values())
        info = device.probe_info if isinstance(device.probe_info, dict) else {}
        status = info.get('identity_status')
        boards.append({
            'id': hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16],
            'online': bool(device.present), 'identified': bool(device.serial),
            'identity_status': status if status in ('confirmed', 'provisional') else 'unidentified',
            'serial_logging_ok': not bool(device.log_error),
            'ssh_sessions': len(sessions), 'ssh_log_errors': sum(bool(s.log_error) for s in sessions),
        })
    maintenance = manager.maintenance
    return {'format_version': 1, 'version': __version__, 'platform': sys.platform,
            'python': '%d.%d.%d' % sys.version_info[:3],
            'uptime': max(0, time.time() - started),
            'storage': disk_status(root, maintenance.policy),
            'retention': maintenance.snapshot(), 'boards': boards}
