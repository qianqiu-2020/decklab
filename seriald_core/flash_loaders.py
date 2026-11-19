"""Preinstalled RAM-only FSBL/U-Boot; never a partition payload source."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

from .tasks import TaskError

# USB ports whose quick-boot FSBL did not reach its download stage. The next
# attempt there stages the fallback FSBL, which trains DDR again (about 6 s
# slower) instead of trusting training data saved on the board.
_FULL_TRAINING = {}
_FULL_TRAINING_LOCK = threading.Lock()
_FULL_TRAINING_SECONDS = 24 * 3600


def _port(binding):
    device = getattr(binding, 'device', None) or {}
    port = device.get('port_path') if isinstance(device, dict) else None
    return tuple(port) if isinstance(port, (list, tuple)) and port else None


def record_fsbl_timeout(binding):
    """Use the fallback FSBL on this USB port for the next attempts."""
    port = _port(binding)
    if port is not None:
        with _FULL_TRAINING_LOCK:
            _FULL_TRAINING[port] = time.monotonic()


def _full_training(binding):
    port = _port(binding)
    with _FULL_TRAINING_LOCK:
        marked = _FULL_TRAINING.get(port)
        if marked is not None and time.monotonic() - marked > _FULL_TRAINING_SECONDS:
            del _FULL_TRAINING[port]
            marked = None
    return marked is not None


def manifest_path():
    configured = os.environ.get('SERIALD_FLASH_LOADERS')
    if configured:
        return Path(configured)
    from .config import STATIC
    return Path(STATIC) / 'flash-loaders' / 'manifest.json'


def _manifest():
    path = manifest_path()
    if not path.is_file():
        raise TaskError('后端尚未安装预备刷机环境（FSBL / 临时 U-Boot），请安装包含 flash-loaders 的后端包', 503)
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise TaskError('invalid RAM loader manifest', 503)
    document = json.loads(raw)
    if document.get('version') != 1 or not isinstance(document.get('families'), dict):
        raise TaskError('invalid RAM loader manifest', 503)
    return path, raw, document['families']


def installed_for(family):
    try:
        return family in _manifest()[2]
    except (OSError, ValueError, TaskError):
        return False


def prepare_materials(binding, workspace, check):
    family = binding.profile_data.get('chip_family')
    path, raw, families = _manifest()
    material = families.get(family)
    if not isinstance(material, dict) or material.get('protocol') not in ('stock', 'spacemit-ram-v1'):
        raise TaskError('预备刷机环境不支持当前芯片', 409)
    root = path.parent.resolve()
    output = dict(chip_family=family, protocol=material['protocol'])
    fallback = 'fsbl_fallback' in material and _full_training(binding)
    destination = Path(workspace) / 'ram-loaders'
    if Path(workspace).parent.name == 'images':
        destination = destination / hashlib.sha256(raw).hexdigest() / family
    if fallback:
        destination = destination / 'full-training'
        output['fsbl_variant'] = 'full-training'
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key in ('fsbl', 'uboot'):
        entry = material.get('fsbl_fallback' if key == 'fsbl' and fallback else key)
        if (not isinstance(entry, dict) or set(entry) != {'file', 'sha256'}
                or not isinstance(entry['file'], str) or not re.fullmatch('[0-9a-f]{64}', str(entry['sha256']))):
            raise TaskError('invalid RAM loader entry', 503)
        source = (root / entry['file']).resolve()
        if source == root or root not in source.parents or not source.is_file():
            raise TaskError('RAM loader is outside the installed materials directory', 503)
        target = destination / ('factory/FSBL.bin' if key == 'fsbl' else 'u-boot.itb')
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise TaskError('RAM loader cache changed', 409)
            size = target.stat().st_size
            digest = hashlib.sha256()
            with target.open('rb') as cached:
                for chunk in iter(lambda: cached.read(1024 ** 2), b''):
                    check()
                    digest.update(chunk)
            if not 0 < size <= 16 * 1024 ** 2 or digest.hexdigest() != entry['sha256']:
                raise TaskError('RAM loader cache changed', 409)
            output[key] = dict(path=str(target), sha256=digest.hexdigest(), size=size, name=target.name, id=key)
            continue
        digest = hashlib.sha256()
        size = 0
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            for chunk in iter(lambda: incoming.read(1024 ** 2), b''):
                check()
                size += len(chunk)
                if size > 16 * 1024 ** 2:
                    raise TaskError('RAM loader exceeds size limit', 503)
                digest.update(chunk)
                outgoing.write(chunk)
        if not size or digest.hexdigest() != entry['sha256']:
            raise TaskError('RAM loader checksum changed', 409)
        output[key] = dict(path=str(target), sha256=digest.hexdigest(), size=size, name=target.name, id=key)
    return output
