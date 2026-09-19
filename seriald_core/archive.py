"""Collision-safe archive staging used by live promotion and offline administration."""
import hashlib
import os
import secrets
import shutil


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            value.update(chunk)
    return value.digest()


def same_file(left, right):
    return os.path.getsize(left) == os.path.getsize(right) and digest(left) == digest(right)


def copy_atomic(source, target):
    """Copy one file atomically; identical existing content makes retries harmless."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        if os.path.islink(target) or not same_file(source, target):
            raise ValueError('目标文件已存在且不同，拒绝覆盖: ' + target)
        return
    temporary = target + '.tmp-' + secrets.token_hex(6)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stage_logs(source, target, prefix=''):
    """Stage regular log files without deleting the authoritative source."""
    if source == target or not os.path.isdir(source) or os.path.islink(source):
        return []
    os.makedirs(target, exist_ok=True)
    staged = []
    for name in sorted(os.listdir(source)):
        path = os.path.join(source, name)
        if not name.endswith('.log') or not os.path.isfile(path) or os.path.islink(path):
            continue
        destination = os.path.join(target, prefix + name)
        if os.path.exists(destination) and not same_file(path, destination):
            destination = os.path.join(
                target, prefix + hashlib.sha256(path.encode()).hexdigest()[:12] + '-' + name)
        copy_atomic(path, destination)
        staged.append((path, destination))
    return staged


def finish_staged(staged, sources):
    """Remove sources only after the caller commits its registry transaction."""
    for source, _ in staged:
        try:
            os.unlink(source)
        except FileNotFoundError:
            pass
    removed = 0
    for source in sorted(set(sources), key=lambda path: len(path.split(os.sep)), reverse=True):
        try:
            os.rmdir(source)
            removed += 1
        except OSError:
            pass
    return removed
