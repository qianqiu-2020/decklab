"""Publish complete NOR readbacks through the existing scoped artifact service."""
import hashlib
import os
from pathlib import Path
import re

from .nor_contents import MAX_PARTITION_BYTES, partition_content
from .archive_io import open_archive
from .tasks import TaskError


_SHA = re.compile(r'[a-f0-9]{64}\Z')
_PARTITIONS = [('bootinfo', 0, 128 << 10), ('fsbl', 128 << 10, 512 << 10),
               ('env', 640 << 10, 64 << 10), ('esos', 704 << 10, 1024 << 10),
               ('opensbi', 1728 << 10, 384 << 10), ('uboot', 2112 << 10, 4096 << 10)]


def compare_payloads(artifacts, backup, metadata, actor='web', serial='', check=lambda: None):
    """Compare exact payload extents in an existing sealed backup, without board IO."""
    extents = {name: (offset, size) for name, offset, size in _PARTITIONS}
    selected, digests = [], {}
    for index, item in enumerate(metadata):
        if not isinstance(item, dict) or item.get('storage') != 'active' or item.get('partition') not in extents:
            continue
        offset, capacity = extents[item['partition']]
        content = item.get('content')
        if not isinstance(content, dict):
            continue
        size, expected = content.get('size'), item.get('sha256')
        if (type(size) is not int or not 0 < size <= capacity or not isinstance(expected, str)
                or not _SHA.fullmatch(expected) or content.get('sha256') != expected):
            continue
        key = (offset, size)
        selected.append((index, key, expected))
        digests.setdefault(key, hashlib.sha256())
    if not selected:
        return metadata
    artifact = backup['artifact']
    row = artifacts.get(artifact['id'], actor, serial)
    if (row['direction'] != 'download' or row['state'] != 'available'
            or row['size'] != artifact['size'] or row['sha256'] != artifact['sha256']
            or not 8 * 1024 ** 2 <= row['size'] <= 256 * 1024 ** 2
            or [(part.get('name'), part.get('offset'), part.get('size'))
                for part in backup.get('partitions', [])] != _PARTITIONS):
        raise TaskError('NOR comparison backup identity or layout changed', 409)
    digest, position = hashlib.sha256(), 0
    with artifacts.open(artifact['id'], actor, serial) as source:
        before = os.fstat(source.fileno())
        while True:
            check()
            chunk = source.read(1024 ** 2)
            if not chunk:
                break
            digest.update(chunk)
            for (offset, size), prefix in digests.items():
                begin, end = max(position, offset), min(position + len(chunk), offset + size)
                if begin < end:
                    prefix.update(chunk[begin - position:end - position])
            position += len(chunk)
        after = os.fstat(source.fileno())
    if (position != row['size'] or digest.hexdigest() != row['sha256']
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise TaskError('NOR comparison backup checksum changed', 409)
    output = list(metadata)
    for index, (offset, size), expected in selected:
        actual = digests[(offset, size)].hexdigest()
        output[index] = dict(metadata[index], board_comparison={
            'size': size, 'sha256': actual, 'expected_sha256': expected, 'matches': actual == expected,
            'phase': 'before', 'backup_id': artifact['id'], 'backup_sha256': artifact['sha256']})
    return output


def publish_backup(artifacts, snapshot, workspace, task_id, attempt_id, serial='',
                   phase='before', check=lambda: None, actor='web', writes=()):
    if (phase not in ('before', 'after') or not isinstance(snapshot, dict)
            or type(snapshot.get('size')) is not int or not 8 * 1024 ** 2 <= snapshot['size'] <= 256 * 1024 ** 2
            or not isinstance(snapshot.get('sha256'), str) or not _SHA.fullmatch(snapshot['sha256'])
            or not isinstance(snapshot.get('layout_digest'), str) or not _SHA.fullmatch(snapshot['layout_digest'])):
        raise TaskError('invalid whole NOR snapshot receipt', 502)
    root = Path(workspace).resolve(strict=True)
    source_path = Path(snapshot.get('path', ''))
    # These are executor-created fixed filenames in this task's managed cache,
    # never paths chosen by the USB device or browser.
    allowed = {root / ('nor-%s-%s-%s.bin' % (task_id, attempt_id, suffix))
               for suffix in (('before', 'prewrite') if phase == 'before' else ('after',))}
    if not source_path.is_absolute() or source_path not in allowed or source_path.resolve(strict=True) != source_path:
        raise TaskError('NOR snapshot escaped its managed workspace', 502)
    partitions = snapshot.get('partitions')
    if not isinstance(partitions, list) or len(partitions) != 6:
        raise TaskError('NOR snapshot partition catalogue is incomplete', 502)
    metadata = []
    with open_archive(source_path) as source:
        if os.fstat(source.fileno()).st_size != snapshot['size']:
            raise TaskError('whole NOR snapshot size changed', 502)
        for part, (name, offset, size) in zip(partitions, _PARTITIONS):
            check()
            if (not isinstance(part, dict) or part.get('name') != name or part.get('offset') != offset
                    or part.get('size') != size or offset + size > snapshot['size']
                    or size > MAX_PARTITION_BYTES):
                raise TaskError('NOR snapshot partition extent changed', 502)
            source.seek(offset)
            data = source.read(size)
            if len(data) != size:
                raise TaskError('NOR snapshot partition is incomplete', 502)
            content = partition_content(data, name)
            if part.get('sha256') != content['sha256']:
                raise TaskError('NOR snapshot partition checksum changed', 502)
            content['sha256_scope'] = 'partition'
            # Compare only the confirmed payload length; trailing erase fill is
            # part of the partition digest, never guessed away or trimmed.
            matches = [value for value in writes if isinstance(value, dict)
                       and value.get('storage') == 'active' and value.get('partition') == name
                       and value.get('kind') == 'partition' and value.get('offset', offset) == offset
                       and type(value.get('size')) is int and 0 < value['size'] <= size
                       and isinstance(value.get('sha256'), str) and _SHA.fullmatch(value['sha256'])]
            if phase == 'after' and len(matches) == 1:
                payload = matches[0]
                digest = hashlib.sha256(data[:payload['size']]).hexdigest()
                content['payload_comparison'] = {'size': payload['size'], 'sha256': digest,
                    'expected_sha256': payload['sha256'], 'matches': digest == payload['sha256']}
            metadata.append(dict(name=name, offset=offset, size=size, content=content))
        # Reserve and seal through the existing quota/owner/digest machinery.
        artifact = artifacts.create('download', 'nor-%s-%s.bin' % (task_id, phase),
                                    snapshot['size'], actor, serial, task_id)
        try:
            if os.name == 'posix':
                os.chmod(artifacts._path(artifact['id'], '.part'), 0o600)
            source.seek(0)
            offset = 0
            while True:
                check()
                chunk = source.read(1024 ** 2)
                if not chunk:
                    break
                artifacts.append(artifact['id'], offset, chunk, actor, serial, task_id)
                offset += len(chunk)
            artifact = artifacts.seal(artifact['id'], actor, serial, task_id,
                                      snapshot['sha256'], check)
        except BaseException:
            artifacts.fail(artifact['id'], task_id)
            raise
    return {'artifact': {key: artifact[key] for key in ('id', 'name', 'size', 'sha256', 'state')},
            'layout_digest': snapshot['layout_digest'], 'partitions': metadata,
            'phase': phase}
