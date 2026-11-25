"""Display geometry from the same sealed package descriptors used by goflash."""
import hashlib
import json
from pathlib import Path
import re

from .archive_io import open_archive
from .tasks import TaskError


def table_bytes(root, file):
    path = root / file['name']
    if (not path.is_relative_to(root) or path.resolve(strict=True) != path or file['size'] > 1024 ** 2):
        raise TaskError('无效的镜像配置文件', 409)
    with open_archive(path) as stream:
        data = stream.read(1024 ** 2 + 1)
    if len(data) != file['size'] or hashlib.sha256(data).hexdigest() != file['sha256']:
        raise TaskError('分区表或刷机配置已变化，请重新准备镜像', 409)
    return data


def size_bytes(value):
    match = re.fullmatch(r'(\d+)([KMG]?)', str(value), re.ASCII)
    if match and len(match[1]) <= 18:
        return int(match[1]) * ({'':1, 'K':1024, 'M':1024**2, 'G':1024**3}[match[2]])
    return None


def describe(result):
    tables = result.get('catalogue') or []
    if not tables:
        return
    files = {f['name']: f for f in result['files']}
    root = Path(result['root_path'])
    recipe = files.get('fastboot.yaml')
    if recipe:
        try:
            # Official Debian fastboot.yaml is a JSON document. Other YAML
            # recipes keep the native executor's existing family validation.
            support = json.loads(table_bytes(root, recipe)).get('support', [])
            families = {'k1x':'k1', 'k1pro':'k1', 'k3':'k3'}
            if isinstance(support, list):
                result['chip_families'] = sorted({families[x] for x in support if isinstance(x, str) and x in families})
        except (ValueError, AttributeError):
            pass
    for table in tables:
        name = table['name']
        if name == 'partition_flash.json' or name not in files:
            continue
        descriptor = json.loads(table_bytes(root, files[name]))
        table['sha256'] = files[name]['sha256']
        for part, raw in zip(table['partitions'], descriptor['partitions']):
            if part['name'] != raw['name']:
                raise TaskError('镜像目录与分区表不一致', 409)
            part.update(offset=size_bytes(raw.get('offset')), size=size_bytes(raw.get('size')),
                        remaining=raw.get('size') == '-')
            payload = files.get(result.get('expanded_files', {}).get(part.get('payload'), part.get('payload')))
            if payload:
                part.update(payload_size=payload['size'], sha256=payload['sha256'])
    from .firmware_versions import describe_firmware
    describe_firmware(result, files)
    result['layout_version'] = 2
