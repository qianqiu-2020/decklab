"""Short-lived collector handoff; never a durable source of board identity."""
import json
import os
import time


def _boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as source:
            return source.read().strip()
    except OSError:
        return ''


def write(path, connections):
    """Atomically publish connections observed immediately before a clean shutdown."""
    data = {'version': 1, 'created': time.time(), 'boot_id': _boot_id(),
            'connections': connections}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + '.tmp-%d' % os.getpid()
    try:
        with open(temporary, 'x', encoding='utf-8') as target:
            json.dump(data, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o660)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def consume(path, max_age=120):
    """Consume a recent same-boot handoff once; stale/crash state cannot be reused."""
    try:
        with open(path, encoding='utf-8') as source:
            data = json.load(source)
    except (OSError, ValueError, TypeError):
        data = None
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    now = time.time()
    if (not isinstance(data, dict) or data.get('version') != 1
            or not data.get('boot_id') or data.get('boot_id') != _boot_id()
            or not isinstance(data.get('created'), (int, float))
            or not 0 <= now - data['created'] <= max_age
            or not isinstance(data.get('connections'), list)):
        return []
    return data['connections']
