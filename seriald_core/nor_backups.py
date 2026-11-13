"""Publish complete NOR readbacks through the existing scoped artifact service."""
import os
from pathlib import Path
import re

from .nor_contents import MAX_PARTITION_BYTES, partition_content
from .archive_io import open_archive
from .tasks import TaskError


_SHA = re.compile(r'[a-f0-9]{64}\Z')


def publish_backup(artifacts, snapshot, workspace, task_id, attempt_id, serial='',
                   phase='before', check=lambda: None):
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
    expected = [('bootinfo', 0, 128 << 10), ('fsbl', 128 << 10, 512 << 10),
                ('env', 640 << 10, 64 << 10), ('esos', 704 << 10, 1024 << 10),
                ('opensbi', 1728 << 10, 384 << 10), ('uboot', 2112 << 10, 4096 << 10)]
    metadata = []
    with open_archive(source_path) as source:
        if os.fstat(source.fileno()).st_size != snapshot['size']:
            raise TaskError('whole NOR snapshot size changed', 502)
        for part, (name, offset, size) in zip(partitions, expected):
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
            metadata.append(dict(name=name, offset=offset, size=size, content=content))
        # Reserve and seal through the existing quota/owner/digest machinery.
        artifact = artifacts.create('download', 'nor-%s-%s.bin' % (task_id, phase),
                                    snapshot['size'], 'web', serial, task_id)
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
                artifacts.append(artifact['id'], offset, chunk, 'web', serial, task_id)
                offset += len(chunk)
            artifact = artifacts.seal(artifact['id'], 'web', serial, task_id,
                                      snapshot['sha256'], check)
        except BaseException:
            artifacts.fail(artifact['id'], task_id)
            raise
    return {'artifact': {key: artifact[key] for key in ('id', 'name', 'size', 'sha256', 'state')},
            'layout_digest': snapshot['layout_digest'], 'partitions': metadata,
            'phase': phase}
