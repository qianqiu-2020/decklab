"""Preinstalled RAM-only FSBL/U-Boot; never a partition payload source."""
import hashlib
import json
import os
from pathlib import Path
import re

from .tasks import TaskError


def manifest_path():
    configured = os.environ.get('SERIALD_FLASH_LOADERS')
    if configured:
        return Path(configured)
    from .config import STATIC
    return Path(STATIC) / 'flash-loaders' / 'manifest.json'


def prepare_materials(binding, workspace, check):
    family = binding.profile_data.get('chip_family')
    path = manifest_path()
    if not path.is_file():
        raise TaskError('后端尚未安装预备刷机环境（FSBL / 临时 U-Boot），请安装包含 flash-loaders 的后端包', 503)
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise TaskError('invalid RAM loader manifest', 503)
    document = json.loads(raw)
    if document.get('version') != 1 or not isinstance(document.get('families'), dict):
        raise TaskError('invalid RAM loader manifest', 503)
    material = document['families'].get(family)
    if not isinstance(material, dict) or material.get('protocol') not in ('stock', 'decklab-nor-v1'):
        raise TaskError('预备刷机环境不支持当前芯片', 409)
    root = path.parent.resolve()
    output = dict(chip_family=family, protocol=material['protocol'])
    destination = Path(workspace) / 'ram-loaders'
    destination.mkdir(mode=0o700)
    for key in ('fsbl', 'uboot'):
        entry = material.get(key)
        if (not isinstance(entry, dict) or set(entry) != {'file', 'sha256'}
                or not isinstance(entry['file'], str) or not re.fullmatch('[0-9a-f]{64}', str(entry['sha256']))):
            raise TaskError('invalid RAM loader entry', 503)
        source = (root / entry['file']).resolve()
        if source == root or root not in source.parents or not source.is_file():
            raise TaskError('RAM loader is outside the installed materials directory', 503)
        target = destination / ('factory/FSBL.bin' if key == 'fsbl' else 'u-boot.itb')
        target.parent.mkdir(mode=0o700, exist_ok=True)
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
        output[key] = dict(path=str(target), sha256=digest.hexdigest(), size=size, name=key, id=key)
    return output
