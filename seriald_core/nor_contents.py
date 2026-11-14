"""Bounded, offline content metadata for NOR partition readbacks and payloads.

Versions come from embedded fields, explicit component banners or fixed build
identity records, never names, file sizes or FIT build timestamps. FIT images
are expanded in-process only for the compression they declare, within a fixed
output budget. Environment values are deliberately opaque. This is descriptive
metadata, not firmware authenticity or board compatibility.
"""
from datetime import datetime, timezone
import hashlib
import lzma
import re
import struct
import uuid
import zlib


MAX_PARTITION_BYTES = 16 * 1024 ** 2
MAX_FIT_PROPERTIES = 8192
MAX_EXPANDED_BYTES = 64 * 1024 ** 2
MAX_EXPANDED_IMAGES = 64
_TEXT_LIMIT = 160
_VERSION_KEYS = frozenset(('version', 'firmware-version', 'fw-version', 'build-version'))
# Patterns are tried in order; the first that matches wins, so an SPL banner
# hides the plain U-Boot version string linked into the same binary.
_BANNERS = {
    'fsbl': (rb'U-Boot SPL [0-9][A-Za-z0-9_.+~-]{1,100}', rb'U-Boot [0-9][A-Za-z0-9_.+~-]{1,100}'),
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


class _Budget:
    def __init__(self):
        self.left = MAX_EXPANDED_BYTES

    def take(self, size):
        if size > self.left:
            raise ValueError('expanded firmware exceeds metadata budget')
        self.left -= size


def _lzo1x(src, limit):
    """Decompress one LZO1X block (classic bitstream) without native helpers."""
    out, ip, state, n = bytearray(), 0, 0, len(src)

    def need(count):
        if ip + count > n:
            raise ValueError('truncated LZO block')

    def match(position, length):
        if position < 0 or length <= 0 or len(out) + length > limit:
            raise ValueError('invalid LZO match')
        distance = len(out) - position
        if distance >= length:
            out.extend(out[position:position + length])
        else:
            chunk = bytes(out[position:])
            out.extend((chunk * (length // distance + 1))[:length])

    def literals(count):
        nonlocal ip
        need(count)
        if len(out) + count > limit:
            raise ValueError('LZO output exceeds declared size')
        out.extend(src[ip:ip + count])
        ip += count

    def extended(base):
        nonlocal ip
        value = 0
        need(1)
        while src[ip] == 0:
            value += 255
            ip += 1
            need(1)
        value += base + src[ip]
        ip += 1
        return value

    need(1)
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        literals(t)
        state = t if t < 4 else 4
    while True:
        need(1)
        t = src[ip]
        ip += 1
        if t < 16:
            if state == 0:
                literals((extended(15) if t == 0 else t) + 3)
                state = 4
                continue
            need(1)
            if state != 4:
                position, length = len(out) - 1 - (t >> 2) - (src[ip] << 2), 2
            else:
                position, length = len(out) - 0x801 - (t >> 2) - (src[ip] << 2), 3
            ip += 1
            nxt = t & 3
        elif t >= 64:
            need(1)
            position = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
            ip += 1
            length, nxt = (t >> 5) + 1, t & 3
        elif t >= 32:
            length = (t & 31) + 2
            if length == 2:
                length = extended(31) + 2
            need(2)
            value = src[ip] | src[ip + 1] << 8
            ip += 2
            position, nxt = len(out) - 1 - (value >> 2), value & 3
        else:
            length = (t & 7) + 2
            if length == 2:
                length = extended(7) + 2
            need(2)
            value = src[ip] | src[ip + 1] << 8
            ip += 2
            position = len(out) - ((t & 8) << 11) - (value >> 2)
            if position == len(out):
                return bytes(out)
            position -= 0x4000
            nxt = value & 3
        match(position, length)
        if nxt:
            literals(nxt)
        state = nxt


def _lzop(data, budget):
    """Expand an lzop container as produced by mkimage for compression=lzo."""
    if not data.startswith(b'\x89LZO\x00\r\n\x1a\n'):
        raise ValueError('invalid lzop magic')
    version, = struct.unpack_from('>H', data, 9)
    position = 13 + (2 if version >= 0x940 else 0)
    method = data[position]
    position += 1 + (1 if version >= 0x940 else 0)
    flags, = struct.unpack_from('>I', data, position)
    if method not in (1, 2, 3) or flags & 0x800:
        raise ValueError('unsupported lzop method')
    position += 4 + 8 + (4 if version >= 0x940 else 0)
    position += 1 + data[position] + 4
    out = bytearray()
    while True:
        dst, = struct.unpack_from('>I', data, position)
        position += 4
        if not dst:
            return bytes(out)
        src, = struct.unpack_from('>I', data, position)
        position += 4
        checks = []
        for flag in (0x1, 0x100):
            if flags & flag:
                checks.append((flag, struct.unpack_from('>I', data, position)[0]))
                position += 4
        if src < dst:
            position += 4 * bool(flags & 0x2) + 4 * bool(flags & 0x200)
        if src > dst or dst > 64 * 1024 ** 2 or position + src > len(data):
            raise ValueError('invalid lzop block')
        budget.take(dst)
        block = data[position:position + src]
        position += src
        if src < dst:
            block = _lzo1x(block, dst)
            if len(block) != dst:
                raise ValueError('lzop block size mismatch')
        for flag, value in checks:
            actual = zlib.adler32(block) if flag == 0x1 else zlib.crc32(block)
            if actual & 0xffffffff != value:
                raise ValueError('lzop block checksum mismatch')
        out.extend(block)


def _expand(raw, compression, budget):
    if compression == 'none':
        return raw
    if compression == 'gzip':
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = stream.decompress(raw, budget.left + 1)
    elif compression == 'lzma':
        out = lzma.LZMADecompressor(lzma.FORMAT_ALONE).decompress(raw, budget.left + 1)
    elif compression == 'lzo':
        return _lzop(raw, budget)
    else:
        return None
    budget.take(len(out))
    return out


def _fit_images(data, properties, total, budget):
    """Yield (node, bytes) for each /images/<name> payload in declared form."""
    images = {}
    for node, name, offset, size in properties:
        if node.startswith('/images/') and node.count('/') == 2:
            images.setdefault(node, {})[name] = (offset, size)
    for node, fields in list(images.items())[:MAX_EXPANDED_IMAGES]:
        compression = _text(data[slice(*_span(fields.get('compression')))]) or 'none'
        if 'data' in fields:
            raw = data[slice(*_span(fields['data']))]
        elif 'data-size' in fields and ('data-offset' in fields or 'data-position' in fields):
            size = _u32(data, fields['data-size'])
            if 'data-position' in fields:
                start = _u32(data, fields['data-position'])
            else:
                start = (total + 3 & ~3) + _u32(data, fields['data-offset'])
            if size is None or start is None or start + size > len(data):
                continue
            raw = data[start:start + size]
        else:
            continue
        try:
            expanded = _expand(raw, compression, budget)
        except (ValueError, lzma.LZMAError, zlib.error, struct.error, IndexError):
            continue
        if expanded:
            yield node, expanded


def _span(field):
    return (0, 0) if field is None else (field[0], field[0] + field[1])


def _u32(data, field):
    return struct.unpack_from('>I', data, field[0])[0] if field and field[1] == 4 else None


_LZMA_SECTION = uuid.UUID('ee4e5898-3914-4259-9d6e-dc7bd79403cf').bytes_le
_EDK2_VENDOR = 'SPACEMIT\0'.encode('utf-16le')
_EDK2_VERSION = re.compile(_EDK2_VENDOR + rb'([0-9]\x00(?:[0-9A-Za-z.+~_-]\x00){0,63})\x00\x00')


def _edk2_versions(region, budget):
    """Read PcdFirmwareVendor/VersionString from LZMA GUID-defined FV sections."""
    found = []
    for match in re.finditer(re.escape(_LZMA_SECTION), region):
        header = match.start() - 4
        if header < 0 or region[header + 3] != 0x02 or len(found) >= 8:
            continue
        size = int.from_bytes(region[header:header + 3], 'little')
        offset = int.from_bytes(region[match.end():match.end() + 2], 'little')
        if not 24 <= offset < size or header + size > len(region):
            continue
        try:
            out = lzma.LZMADecompressor(lzma.FORMAT_ALONE).decompress(
                region[header + offset:header + size], budget.left + 1)
            budget.take(len(out))
        except (ValueError, lzma.LZMAError):
            continue
        for item in _EDK2_VERSION.finditer(out):
            found.append('EDK2 ' + item.group(1).decode('utf-16le'))
    return found


_OPENSBI_PLATFORM = re.compile(rb'(?s)(.{4}).{4}Generic\x00{57}')
_OPENSBI_BUILD = re.compile(rb'\x00([A-Za-z0-9_.+~-]{1,100})\x00{1,16}\nOpenSBI %s\n')
_ESOS_IDENTITY = re.compile(rb'([a-z0-9_]{1,32}:[0-9a-f]{12})\x00')


def _opensbi_versions(region):
    """sbi_platform.opensbi_version plus the OPENSBI_VERSION_GIT banner argument."""
    release = None
    for match in _OPENSBI_PLATFORM.finditer(region):
        value, = struct.unpack('<I', match.group(1))
        if value >> 16 < 16 and value & 0xffff < 100:
            release = 'OpenSBI v%d.%d' % (value >> 16, value & 0xffff)
            break
    build = _OPENSBI_BUILD.search(region)
    if not release and not build:
        return []
    build = build and build.group(1).decode('ascii')
    # A full commit is kept as evidence; the display uses the usual 12 digits.
    short = build[:12] if build and re.fullmatch('[0-9a-f]{40}', build) else build
    value = ' '.join(item for item in (release or 'OpenSBI', short) if item)
    return [(value, {'build': build}) if build else value]


def _esos_versions(region):
    """ESOS struct version_id: verid[64] then little-endian release_version."""
    found = []
    for match in _ESOS_IDENTITY.finditer(region):
        start = match.start()
        if start % 0x100 or start + 68 > len(region) or region[match.end():start + 64].strip(b'\0'):
            continue
        release, = struct.unpack_from('<I', region, start + 64)
        found.append(match.group(1).decode('ascii') + (';version=%d' % release if release else ''))
    return found


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
        # Image payloads are expanded only by their declared compression;
        # unknown methods stay unsearched rather than guessed.
        if partition in _BANNERS:
            budget = _Budget()
            search_regions = [(node, region) for node, region in _fit_images(data, properties, total, budget)]
    for pattern in _BANNERS.get(partition, ()):
        for source, region in search_regions:
            for match in re.finditer(pattern, region):
                value = match.group().decode('ascii')
                item = {'value': value, 'source': 'embedded-banner', 'offset': match.start()}
                if source:
                    item['image'] = source
                versions.append(item)
                if len(versions) >= 8:
                    break
        if versions:
            break
    for source, region in search_regions:
        if partition == 'opensbi':
            values = _opensbi_versions(region)
        elif partition == 'esos':
            values = _esos_versions(region)
        elif partition == 'uboot' and not versions:
            values = _edk2_versions(region, budget if result['format'] == 'fit' else _Budget())
        else:
            values = []
        for value in values:
            value, extra = value if isinstance(value, tuple) else (value, {})
            item = dict(extra, value=value, source='build-identity')
            if source:
                item['image'] = source
            versions.append(item)
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
