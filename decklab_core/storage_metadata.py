"""Bounded metadata reads from the task-owned RAM USB session, never a disk dump."""
import base64
import json
import re
from .os_contents import image_content, ImageError
from .tasks import TaskError

_DISK = re.compile(r'(nvme|scsi|mmc|usb):[0-3]\Z')
PAGE = 64 * 1024
MAX_READ = 32 * 1024 ** 2
MAX_DISK = 16 * 1024 ** 4


def default_storage_for_priority(storages, priority_source):
    """Match a verified EEPROM choice to one enumerated disk, never by order."""
    kind = {'eeprom:UFS': 'scsi', 'eeprom:SSD': 'nvme',
            'eeprom:MMC': 'mmc', 'eeprom:USB': 'usb'}.get(priority_source)
    matches = [disk['id'] for disk in storages
               if kind and disk['id'].startswith(kind + ':') and disk.get('size') != 0]
    return matches[0] if len(matches) == 1 else ''


class MetadataReader:
    def __init__(self, fetch, disk, offset, size, budget, check):
        self.fetch, self.disk = fetch, disk
        self.base, self.size, self.position = offset, size, 0
        self.budget, self.check, self.cache = budget, check, {}

    def seek(self, offset):
        if type(offset) is not int or not 0 <= offset <= self.size:
            raise ImageError('metadata seek outside partition')
        self.position = offset

    def read(self, length):
        if type(length) is not int or length < 0 or self.position + length > self.size:
            raise ImageError('metadata read outside partition')
        result = bytearray()
        while length:
            self.check()
            absolute = self.base + self.position
            page = absolute // PAGE * PAGE
            if page not in self.cache:
                count = min(PAGE, self.disk['size'] - page)
                if self.budget[0] < count:
                    raise ImageError('system metadata read budget exhausted')
                self.budget[0] -= count
                response = self.fetch({'storage_id': self.disk['id'], 'storage_size': self.disk['size'],
                                       'offset': page, 'size': count})
                if (response.get('storage_id') != self.disk['id'] or response.get('offset') != page
                        or response.get('size') != count or response.get('ok') is not True):
                    raise TaskError('system metadata read identity changed', 502)
                try:
                    data = base64.b64decode(response.get('data_b64', ''), validate=True)
                except (ValueError, TypeError) as error:
                    raise TaskError('invalid system metadata bytes', 502) from error
                if len(data) != count:
                    raise TaskError('short system metadata read', 502)
                self.cache[page] = data
            data = self.cache[page]
            start = absolute - page
            count = min(length, len(data) - start)
            if count <= 0:
                raise ImageError('empty system metadata extent')
            result.extend(data[start:start + count])
            self.position += count
            length -= count
        return bytes(result)


def read_inventory(document, fetch, check=lambda: None, progress=lambda done, total: None):
    if not isinstance(document, dict) or document.get('ok') is not True or not isinstance(document.get('storages'), list):
        raise TaskError('invalid live system storage inventory', 502)
    rows = document['storages']
    if len(rows) > 16:
        raise TaskError('too many system storages', 502)
    seen, disks, budget = set(), [], [MAX_READ]
    for disk in rows:
        check()
        if (not isinstance(disk, dict) or not _DISK.fullmatch(str(disk.get('id', '')))
                or disk['id'] in seen or type(disk.get('size')) is not int):
            raise TaskError('invalid system storage geometry', 502)
        seen.add(disk['id'])
        if (disk['size'] == 0 and disk.get('sector_size') == 0
                and disk.get('state') == 'layout-unavailable' and disk.get('capacity_verified') is False
                and disk.get('partitions') == []):
            disks.append({'id': disk['id'], 'size': 0, 'sector_size': 0, 'state': 'layout-unavailable',
                          'capacity_verified': False, 'error': str(disk.get('error', ''))[:500],
                          'partitions': [], 'read_only': True})
            continue
        if (not 512 <= disk['size'] <= MAX_DISK
                or disk.get('sector_size') not in (512, 4096) or disk['size'] % disk['sector_size']
                or not isinstance(disk.get('partitions'), list) or len(disk['partitions']) > 128
                or any(not isinstance(part, dict) or type(part.get('offset')) is not int
                       or type(part.get('size')) is not int for part in disk['partitions'])):
            raise TaskError('invalid system storage geometry', 502)
        parts = []
        previous_end = 0
        for part in sorted(disk['partitions'], key=lambda item: item.get('offset', -1)):
            if (type(part.get('offset')) is not int or type(part.get('size')) is not int
                    or part['offset'] < previous_end or part['size'] <= 0
                    or part['offset'] % disk['sector_size'] or part['size'] % disk['sector_size']
                    or part['offset'] + part['size'] > disk['size']
                    or not isinstance(part.get('name'), str) or len(part['name']) > 160):
                raise TaskError('invalid system storage partition', 502)
            previous_end = part['offset'] + part['size']
            if fetch is None:
                parts.append({key: part[key] for key in ('name', 'offset', 'size')})
                continue
            source = MetadataReader(fetch, disk, part['offset'], part['size'], budget, check)
            try:
                content = image_content(source, part['size'], partition=part['name'], metadata_only=True,
                                        read_budget=8 * 1024 ** 2)
            except ImageError as error:
                content = {'version_state': 'unknown', 'error': str(error),
                           'description': '已达到版本识别读取上限' if 'budget' in str(error) else '无法解析分区版本信息'}
            parts.append({'name': part['name'], 'offset': part['offset'], 'size': part['size'], 'content': content})
            progress(MAX_READ - budget[0], MAX_READ)
        disks.append({'id': disk['id'], 'size': disk['size'], 'sector_size': disk['sector_size'],
                      'capacity_verified': disk.get('capacity_verified') is True,
                      'state': disk.get('state', 'ready'), 'error': str(disk.get('error', ''))[:500],
                      'partitions': parts, 'read_only': True})
    selected = document.get('default_storage') or ''
    if not isinstance(selected, str):
        raise TaskError('invalid default storage identity', 502)
    if selected and not any(disk['id'] == selected and disk['size'] > 0 for disk in disks):
        raise TaskError('default storage identity changed', 502)
    result = {'storages': disks, 'default_storage': selected, 'default_candidate': document.get('default_candidate', ''),
            'priority_source': document.get('priority_source', ''), 'metadata_bytes_read': MAX_READ - budget[0],
            'read_only': True, 'full_backup': False,
            'boot_priority_editable': document.get('boot_priority_editable') is True}
    ec = document.get('ec')
    if isinstance(ec, dict) and ec.get('state') in ('ready', 'unavailable', 'not-applicable'):
        result['ec'] = {'state': ec['state']}
        if ec['state'] == 'ready':
            version = ec.get('version')
            if not isinstance(version, str) or not 0 < len(version) <= 64 or not version.isprintable() or ec.get('copy') not in ('RO', 'RW'):
                raise TaskError('invalid running EC version', 502)
            result['ec'].update(version=version, copy=ec['copy'])
    if len(json.dumps(result, ensure_ascii=False).encode()) > 48 * 1024:
        raise TaskError('system storage metadata exceeds display limit', 502)
    return result
