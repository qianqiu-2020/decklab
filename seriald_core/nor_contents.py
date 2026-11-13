"""Bounded, offline content metadata for NOR partition readbacks and payloads.

Versions come from embedded fields or explicit component banners, never names,
file sizes or FIT build timestamps. Environment values are deliberately opaque.
This is descriptive metadata, not firmware authenticity or board compatibility.
"""
from datetime import datetime, timezone
import hashlib
import re
import struct


MAX_PARTITION_BYTES = 16 * 1024 ** 2
MAX_FIT_PROPERTIES = 8192
_TEXT_LIMIT = 160
_VERSION_KEYS = frozenset(('version', 'firmware-version', 'fw-version', 'build-version'))
_BANNERS = {
    'fsbl': (rb'U-Boot (?:SPL )?[0-9][A-Za-z0-9_.+~-]{1,100}',),
    'opensbi': (rb'OpenSBI v[0-9][A-Za-z0-9_.+~-]{1,100}',),
    'esos': (rb'ESOS (?:v|version[ :=]+)[0-9][A-Za-z0-9_.+~-]{1,100}',),
    'uboot': (rb'U-Boot [0-9][A-Za-z0-9_.+~-]{1,100}',
              rb'EDK II (?:v|version[ :=]+)[0-9][A-Za-z0-9_.+~-]{1,100}'),
}


def _text(raw):
    if not raw or len(raw) > _TEXT_LIMIT + 1:
        return None
    try:
        value = raw.rstrip(b'\0').decode('utf-8')
    except UnicodeError:
        return None
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _fit_properties(data):
    if len(data) < 40:
        raise ValueError('short FIT header')
    magic, total, structure, strings, reserve, version, compatible, boot, string_size, struct_size = struct.unpack_from('>10I', data)
    if (magic != 0xd00dfeed or version != 17 or compatible > version
            or total < 40 or total > len(data) or structure % 4 or structure < 40
            or strings < 40 or structure + struct_size > total or strings + string_size > total
            or struct_size < 8 or structure < strings + string_size and strings < structure + struct_size):
        raise ValueError('invalid FIT bounds')
    end, position, stack, properties = structure + struct_size, structure, [], []
    nodes = 0
    while position + 4 <= end:
        token, = struct.unpack_from('>I', data, position)
        position += 4
        if token == 1:
            stop = data.find(b'\0', position, min(end, position + 256))
            if stop < 0 or len(stack) >= 32 or nodes >= 2048 or nodes and not stack:
                raise ValueError('invalid FIT node')
            name = data[position:stop].decode('ascii', errors='strict')
            if '/' in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise ValueError('invalid FIT node name')
            stack.append(name)
            nodes += 1
            position = (stop + 4) & ~3
        elif token == 2:
            if not stack:
                raise ValueError('unbalanced FIT node')
            stack.pop()
        elif token == 3:
            if not stack or position + 8 > end or len(properties) >= MAX_FIT_PROPERTIES:
                raise ValueError('invalid FIT property')
            size, name_offset = struct.unpack_from('>2I', data, position)
            position += 8
            if name_offset >= string_size or size > end - position:
                raise ValueError('invalid FIT property bounds')
            name_end = data.find(b'\0', strings + name_offset, strings + string_size)
            if name_end < 0 or name_end - strings - name_offset > 128:
                raise ValueError('invalid FIT property name')
            name = data[strings + name_offset:name_end].decode('ascii', errors='strict')
            properties.append(('/'.join(stack), name, position, size))
            position = (position + size + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            if stack or not nodes:
                raise ValueError('unclosed FIT tree')
            return properties, total
        else:
            raise ValueError('invalid FIT token')
    raise ValueError('missing FIT end')


def partition_content(data, partition=''):
    """Describe exact bytes, returning explicit unknown/non-versioned states."""
    if not isinstance(data, bytes) or len(data) > MAX_PARTITION_BYTES:
        raise ValueError('partition content exceeds metadata limit')
    result = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              'format': 'raw', 'version': None, 'version_state': 'unknown', 'versions': []}
    if partition in ('env', 'bootinfo'):
        result.update(format='uboot-environment' if partition == 'env' else 'board-configuration',
                      version_state='not-applicable')
        return result
    if not data or data.count(0xff) == len(data):
        result.update(format='empty', version_state='empty')
        return result
    search_regions = [('', data)]
    versions = []
    if data.startswith(b'\xd0\x0d\xfe\xed'):
        result['format'] = 'fit'
        try:
            properties, total = _fit_properties(data)
        except (ValueError, UnicodeError, struct.error):
            result['version_state'] = 'invalid-format'
            return result
        result['image_bytes'] = total
        # Only explicit fields are displayed. Image/configuration descriptions
        # are not versions, and a FIT timestamp is only a build timestamp.
        for node, name, offset, size in properties:
            raw = data[offset:offset + size]
            if name == 'description' and not node:
                text = _text(raw)
                if text:
                    result['description'] = text
            elif name in _VERSION_KEYS and (not node or node.startswith('/images/') and node.count('/') == 2):
                text = _text(raw)
                if text:
                    versions.append({'value': text, 'source': 'fit-property', 'field': (node or '/') + ':' + name})
                elif size == 4:
                    number, = struct.unpack('>I', raw)
                    versions.append({'value': '0x%08x' % number, 'source': 'fit-property', 'field': (node or '/') + ':' + name})
            elif name == 'timestamp' and not node and size == 4:
                timestamp, = struct.unpack('>I', raw)
                if 946684800 <= timestamp <= 4102444800:
                    result['built_at'] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        # Raw uncompressed FIT payloads may also carry a component banner.
        # Compression is not guessed or shell-expanded; absent evidence stays
        # unknown. Limit matches to the partition's own component.
    for pattern in _BANNERS.get(partition, ()):
        for source, region in search_regions:
            for match in re.finditer(pattern, region):
                value = match.group().decode('ascii')
                versions.append({'value': value, 'source': 'embedded-banner', 'offset': match.start()})
                if len(versions) >= 8:
                    break
    unique = []
    for item in versions:
        if item['value'] not in {value['value'] for value in unique}:
            unique.append(item)
        if len(unique) == 8:
            break
    result['versions'] = unique
    if unique:
        result['version_state'] = 'identified' if len(unique) == 1 else 'multiple'
        result['version'] = ' / '.join(item['value'] for item in unique)
    return result
