"""Bounded, read-only metadata for OS image payloads.

Recognises Android sparse wrappers, GPT/MBR disks, ext2/3/4 and FAT12/16/32,
then reads a few known files: os-release, distribution version files, kernel
banners, module directories, boot configuration and bootloader version strings.
Only metadata blocks and those files are read, under a fixed I/O budget; the
payload is never mounted, executed or decompressed beyond declared formats.
Results describe the image bytes; they do not prove the image will boot.
"""
import bisect
import lzma
import re
import struct
import uuid
import zlib

from . import nor_contents


MAX_READ_BYTES = 256 * 1024 ** 2
MAX_SMALL_FILE = 256 * 1024
MAX_KERNEL_BYTES = 96 * 1024 ** 2
MAX_DIRECTORY_BYTES = 4 * 1024 ** 2
MAX_PARTITIONS = 16
_FIRMWARE = ('fsbl', 'esos', 'opensbi', 'uboot')
_TEXT = re.compile(r'[^\x00-\x1f\x7f]{1,160}')
_OS_FIELDS = ('PRETTY_NAME', 'NAME', 'ID', 'VERSION_ID', 'VERSION', 'VERSION_CODENAME',
              'BUILD_ID', 'IMAGE_ID', 'IMAGE_VERSION', 'VARIANT', 'DEBIAN_VERSION_FULL')
_KERNEL_NAME = re.compile(r'(?:vmlinuz|vmlinux|Image)(?:-[0-9][A-Za-z0-9._+~-]{0,100})?(?:\.gz)?')
_BANNER = re.compile(rb'Linux version ([0-9][A-Za-z0-9._+~-]{0,100}) \(')
_RELEASE = re.compile(r'[0-9][A-Za-z0-9._+~-]{0,100}')
_ESP_TYPE = uuid.UUID('c12a7328-f81f-11d2-ba4b-00a0c93ec93b')
_GPT_TYPES = {
    _ESP_TYPE: 'EFI System Partition',
    uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4'): 'Linux filesystem',
    uuid.UUID('72ec70a6-cf74-40e6-bd49-4bda08e8f224'): 'Linux root (RISC-V 64)',
    uuid.UUID('bc13c2ff-59e6-4262-a352-b275fd6f7172'): 'Linux extended boot',
    uuid.UUID('0657fd6d-a4ab-43c4-84e5-0933c84b4f4f'): 'Linux swap',
    uuid.UUID('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'): 'Basic data',
}


class ImageError(ValueError):
    pass


class _Budget:
    def __init__(self, limit=None):
        self.left = MAX_READ_BYTES if limit is None else limit

    def take(self, size):
        if size > self.left:
            raise ImageError('image metadata read budget exhausted')
        self.left -= size


class _File:
    def __init__(self, source, size, budget):
        self.source, self.size, self.budget = source, size, budget

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ImageError('read outside image')
        self.budget.take(length)
        self.source.seek(offset)
        data = self.source.read(length)
        if len(data) != length:
            raise ImageError('short image read')
        return data


class _Slice:
    def __init__(self, parent, base, size):
        if base < 0 or size < 0 or base + size > parent.size:
            raise ImageError('partition outside image')
        self.parent, self.base, self.size = parent, base, size

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ImageError('read outside partition')
        return self.parent.read(self.base + offset, length)


class _Sparse:
    """Android sparse image presented as its expanded byte range."""
    MAGIC = 0xed26ff3a

    def __init__(self, parent):
        header = parent.read(0, 28)
        magic, major, _minor, header_size, chunk_header, block, blocks, chunks, _crc = struct.unpack('<I4H4I', header)
        if (magic != self.MAGIC or major != 1 or header_size < 28 or chunk_header < 12
                or block % 4 or not 0 < block <= 1 << 20 or chunks > 1 << 18):
            raise ImageError('invalid sparse header')
        self.parent, self.size = parent, block * blocks
        self.starts, self.runs = [], []
        position, out = header_size, 0
        for _ in range(chunks):
            kind, _reserved, count, total = struct.unpack('<2H2I', parent.read(position, 12))
            body, length = position + chunk_header, count * block
            if kind == 0xcac1:
                if total - chunk_header != length:
                    raise ImageError('invalid sparse raw chunk')
                run = ('raw', body)
            elif kind == 0xcac2:
                run = ('fill', parent.read(body, 4))
            elif kind in (0xcac3, 0xcac4):
                run = ('zero', None)
                length = 0 if kind == 0xcac4 else length
            else:
                raise ImageError('invalid sparse chunk type')
            if length:
                self.starts.append(out)
                self.runs.append((out, length) + run)
            out += length
            position += total
        if out != self.size:
            raise ImageError('sparse chunks do not cover image')

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ImageError('read outside sparse image')
        out = bytearray()
        index = max(0, bisect.bisect_right(self.starts, offset) - 1)
        while length:
            start, size, kind, value = self.runs[index]
            skip = offset - start
            part = min(length, size - skip)
            if kind == 'raw':
                out += self.parent.read(value + skip, part)
            elif kind == 'fill':
                out += (value * ((skip % 4 + part) // 4 + 2))[skip % 4:skip % 4 + part]
            else:
                out += bytes(part)
            offset, length, index = offset + part, length - part, index + 1
        return bytes(out)


def _text(raw):
    value = raw.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()
    return value if value and _TEXT.fullmatch(value) else None


# ---------------------------------------------------------------- ext2/3/4

class _Ext:
    EXTENTS, INLINE = 0x80000, 0x10000000

    def __init__(self, dev):
        sb = dev.read(1024, 1024)
        if struct.unpack_from('<H', sb, 56)[0] != 0xef53:
            raise ImageError('not ext')
        self.dev = dev
        self.block = 1024 << struct.unpack_from('<I', sb, 24)[0]
        self.per_group = struct.unpack_from('<I', sb, 40)[0]
        self.first = struct.unpack_from('<I', sb, 20)[0]
        revision = struct.unpack_from('<I', sb, 76)[0]
        self.inode_size = struct.unpack_from('<H', sb, 88)[0] if revision else 128
        incompat = struct.unpack_from('<I', sb, 96)[0]
        self.desc = struct.unpack_from('<H', sb, 254)[0] if incompat & 0x80 else 32
        if (self.block > 65536 or not self.per_group or self.inode_size < 128 or incompat & 0x10
                or self.desc < 32 or self.desc > 1024):
            raise ImageError('unsupported ext layout')
        self.label = _text(sb[120:136])
        self.uuid = str(uuid.UUID(bytes=sb[104:120]))
        self.kind = 'ext4' if incompat & 0x2c0 else ('ext3' if struct.unpack_from('<I', sb, 92)[0] & 4 else 'ext2')
        self.groups = {}

    def _table(self, group):
        if group not in self.groups:
            raw = self.dev.read((self.first + 1) * self.block + group * self.desc, self.desc)
            low = struct.unpack_from('<I', raw, 8)[0]
            high = struct.unpack_from('<I', raw, 40)[0] if self.desc >= 64 else 0
            self.groups[group] = low | high << 32
        return self.groups[group]

    def inode(self, number):
        if number < 1:
            raise ImageError('invalid inode')
        group, index = divmod(number - 1, self.per_group)
        raw = self.dev.read(self._table(group) * self.block + index * self.inode_size, 128)
        mode, size_lo, flags, size_hi = (struct.unpack_from('<H', raw, 0)[0], struct.unpack_from('<I', raw, 4)[0],
                                         struct.unpack_from('<I', raw, 32)[0], struct.unpack_from('<I', raw, 108)[0])
        return {'mode': mode, 'size': size_lo | size_hi << 32, 'flags': flags, 'block': raw[40:100],
                'blocks': struct.unpack_from('<I', raw, 28)[0]}

    def _extents(self, node, depth=0):
        magic, entries, _max, level = struct.unpack_from('<4H', node, 0)
        if magic != 0xf30a or depth > 6 or entries > 340:
            raise ImageError('invalid extent tree')
        for index in range(entries):
            entry = node[12 + index * 12:24 + index * 12]
            if level:
                leaf = struct.unpack_from('<I', entry, 4)[0] | struct.unpack_from('<H', entry, 8)[0] << 32
                yield from self._extents(self.dev.read(leaf * self.block, self.block), depth + 1)
            else:
                logical, length, high, low = struct.unpack('<IHHI', entry)
                zero = length > 32768
                yield logical, length - 32768 if zero else length, None if zero else (low | high << 32)

    def _mapped(self, inode):
        blocks = struct.unpack('<15I', inode['block'])
        for logical, physical in enumerate(blocks[:12]):
            yield logical, 1, physical or None
        per = self.block // 4
        logical = 12
        for depth, pointer in enumerate(blocks[12:15], 1):
            yield from self._indirect(pointer, depth, logical, per)
            logical += per ** depth

    def _indirect(self, pointer, depth, logical, per):
        if not pointer:
            return
        table = struct.unpack('<%dI' % per, self.dev.read(pointer * self.block, self.block))
        for index, value in enumerate(table):
            start = logical + index * per ** (depth - 1)
            if depth == 1:
                yield start, 1, value or None
            elif value:
                yield from self._indirect(value, depth - 1, start, per)

    def data(self, inode, limit):
        size = inode['size']
        if size > limit:
            raise ImageError('file exceeds metadata limit')
        if inode['flags'] & self.INLINE:
            return inode['block'][:size] if size <= 60 else None
        out = bytearray(size)
        runs = self._extents(inode['block']) if inode['flags'] & self.EXTENTS else self._mapped(inode)
        for logical, length, physical in runs:
            start = logical * self.block
            if start >= size:
                if not inode['flags'] & self.EXTENTS:
                    break
                continue
            count = min(length * self.block, size - start)
            if physical:
                out[start:start + count] = self.dev.read(physical * self.block, count)
        return bytes(out)

    def entries(self, inode):
        raw = self.data(inode, MAX_DIRECTORY_BYTES) or b''
        position = 0
        while position + 8 <= len(raw):
            number, length, name_length, kind = struct.unpack_from('<IHBB', raw, position)
            if length < 8 or position + length > len(raw):
                break
            if number and name_length:
                name = raw[position + 8:position + 8 + name_length].decode('utf-8', 'replace')
                if name not in ('.', '..'):
                    yield name, number
            position += length

    def lookup(self, path, follow=True, depth=0):
        if depth > 8:
            raise ImageError('symlink loop')
        number, parts = 2, [part for part in path.split('/') if part]
        for index, part in enumerate(parts):
            node = self.inode(number)
            if node['mode'] & 0xf000 != 0x4000:
                return None
            number = dict(self.entries(node)).get(part)
            if number is None:
                return None
            node = self.inode(number)
            if node['mode'] & 0xf000 == 0xa000 and (follow or index < len(parts) - 1):
                target = self.link(node)
                if target is None:
                    return None
                base = '/'.join(parts[:index]) if not target.startswith('/') else ''
                return self.lookup(base + '/' + target + '/' + '/'.join(parts[index + 1:]), follow, depth + 1)
        return self.inode(number)

    def link(self, node):
        if node['size'] < 60 and not node['flags'] & (self.EXTENTS | self.INLINE) and not node['blocks']:
            raw = node['block'][:node['size']]
        else:
            raw = self.data(node, 4096)
        return raw.decode('utf-8', 'replace') if raw else None

    # Common filesystem interface used by the inspectors.
    def listdir(self, path):
        node = self.lookup(path)
        if not node or node['mode'] & 0xf000 != 0x4000:
            return []
        return [(name, self.inode(number)['mode'] & 0xf000 == 0x4000) for name, number in self.entries(node)][:512]

    def read(self, path, limit=MAX_SMALL_FILE):
        node = self.lookup(path)
        if not node or node['mode'] & 0xf000 != 0x8000 or node['size'] > limit:
            return None
        return self.data(node, limit)

    def size(self, path):
        node = self.lookup(path)
        return node['size'] if node and node['mode'] & 0xf000 == 0x8000 else None


# ---------------------------------------------------------------- FAT

class _Fat:
    def __init__(self, dev):
        boot = dev.read(0, 512)
        sector, cluster, reserved, fats, roots, total16, _media, fat16 = struct.unpack_from('<HBHBHHBH', boot, 11)
        total32, fat32, root_cluster = struct.unpack_from('<I', boot, 32)[0], struct.unpack_from('<I', boot, 36)[0], struct.unpack_from('<I', boot, 44)[0]
        if (boot[510:512] != b'\x55\xaa' or sector not in (512, 1024, 2048, 4096) or not cluster
                or cluster & (cluster - 1) or not reserved or fats not in (1, 2)):
            raise ImageError('not FAT')
        fat_sectors = fat16 or fat32
        total = total16 or total32
        root_sectors = (roots * 32 + sector - 1) // sector
        self.data_start = (reserved + fats * fat_sectors + root_sectors) * sector
        clusters = (total * sector - self.data_start) // (cluster * sector)
        if not fat_sectors or not total or total * sector > dev.size + sector or clusters < 1:
            raise ImageError('invalid FAT geometry')
        self.dev, self.cluster = dev, cluster * sector
        self.fat_start = reserved * sector
        self.bits = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        self.kind = 'fat%d' % self.bits
        label_at, serial_at = (71, 67) if self.bits == 32 else (43, 39)
        self.label = _text(boot[label_at:label_at + 11])
        self.uuid = '%04X-%04X' % divmod(struct.unpack_from('<I', boot, serial_at)[0], 0x10000)
        self.root = ('chain', root_cluster) if self.bits == 32 else ('fixed', (reserved + fats * fat_sectors) * sector, roots * 32)
        self.fat = {}

    def _next(self, cluster):
        offset = cluster * self.bits // 8
        page = offset // 4096
        if page not in self.fat:
            self.fat[page] = self.dev.read(self.fat_start + page * 4096, min(4096, self.dev.size - self.fat_start - page * 4096))
        raw = self.fat[page] + (self.fat.get(page + 1) or b'')
        if offset % 4096 + 4 > len(raw):
            self.fat[page + 1] = self.dev.read(self.fat_start + (page + 1) * 4096, 4096)
            raw = self.fat[page] + self.fat[page + 1]
        at = offset % 4096
        if self.bits == 32:
            return struct.unpack_from('<I', raw, at)[0] & 0x0fffffff
        if self.bits == 16:
            return struct.unpack_from('<H', raw, at)[0]
        value = struct.unpack_from('<H', raw, at)[0]
        return value >> 4 if cluster & 1 else value & 0xfff

    def _chain(self, first, limit):
        end = {12: 0xff8, 16: 0xfff8, 32: 0x0ffffff8}[self.bits]
        out, cluster = bytearray(), first
        while 2 <= cluster < end and len(out) < limit:
            out += self.dev.read(self.data_start + (cluster - 2) * self.cluster, self.cluster)
            cluster = self._next(cluster)
        return bytes(out[:limit])

    def _entries(self, where):
        raw = (self.dev.read(where[1], where[2]) if where[0] == 'fixed'
               else self._chain(where[1], MAX_DIRECTORY_BYTES))
        long_name = {}
        for position in range(0, len(raw) - 31, 32):
            entry = raw[position:position + 32]
            if entry[0] == 0:
                break
            if entry[0] == 0xe5:
                long_name = {}
                continue
            attributes = entry[11]
            if attributes == 0x0f:
                part = entry[1:11] + entry[14:26] + entry[28:32]
                long_name[entry[0] & 0x1f] = part.decode('utf-16le', 'replace').split('\0')[0].replace('￿', '')
                continue
            if attributes & 0x08:
                long_name = {}
                continue
            base, extension = entry[:8].decode('ascii', 'replace').rstrip(), entry[8:11].decode('ascii', 'replace').rstrip()
            base = base.lower() if entry[12] & 0x08 else base
            extension = extension.lower() if entry[12] & 0x10 else extension
            name = ''.join(long_name[key] for key in sorted(long_name)) or (base + ('.' + extension if extension else ''))
            long_name = {}
            if name in ('.', '..'):
                continue
            first = struct.unpack_from('<H', entry, 20)[0] << 16 | struct.unpack_from('<H', entry, 26)[0]
            yield name, bool(attributes & 0x10), first, struct.unpack_from('<I', entry, 28)[0]

    def _find(self, path):
        where, found = self.root, None
        for part in [part for part in path.split('/') if part]:
            if found and not found[1]:
                return None
            found = next((item for item in self._entries(where) if item[0].lower() == part.lower()), None)
            if not found:
                return None
            where = ('chain', found[2])
        return found or ('', True, None, 0)

    def listdir(self, path):
        found = self._find(path)
        if not found or not found[1]:
            return []
        where = self.root if found[2] is None else ('chain', found[2])
        return [(name, directory) for name, directory, _first, _size in self._entries(where)][:512]

    def read(self, path, limit=MAX_SMALL_FILE):
        found = self._find(path)
        if not found or found[1] or found[3] > limit:
            return None
        return self._chain(found[2], found[3]) if found[3] else b''

    def size(self, path):
        found = self._find(path)
        return found[3] if found and not found[1] else None


# ---------------------------------------------------------------- inspectors

def _os_release(raw):
    fields = {}
    for line in raw.decode('utf-8', 'replace').splitlines()[:200]:
        key, _, value = line.partition('=')
        if key.strip() in _OS_FIELDS:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            if _TEXT.fullmatch(value or '\0'):
                fields[key.strip().lower()] = value
    return fields


def _kernel_release(fs, path):
    """Linux banner from an Image, gzip Image or EFI zboot payload."""
    size = fs.size(path)
    if not size or size > MAX_KERNEL_BYTES:
        return None
    data = fs.read(path, MAX_KERNEL_BYTES)
    if data[:2] == b'MZ' and data[4:8] == b'zimg':
        start, length = struct.unpack_from('<II', data, 8)
        method = _text(data[24:32])
        payload = data[start:start + length]
        try:
            if method == 'gzip':
                data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(payload, MAX_KERNEL_BYTES)
            elif method in ('lzma', 'xz'):
                data = lzma.LZMADecompressor().decompress(payload, MAX_KERNEL_BYTES)
            else:
                return None
        except (zlib.error, lzma.LZMAError):
            return None
    elif data[:4] == b'\xd0\x0d\xfe\xed':
        # FIT-wrapped kernels (vendor U-Boot boot flows) expand only by the
        # compression each image declares.
        try:
            properties, total = nor_contents._fit_properties(data)
        except (ValueError, UnicodeError, struct.error):
            return None
        for _node, payload in nor_contents._fit_images(data, properties, total, nor_contents._Budget()):
            match = _BANNER.search(payload)
            if match:
                return match.group(1).decode('ascii')
        return None
    elif data[:2] == b'\x1f\x8b':
        try:
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, MAX_KERNEL_BYTES)
        except zlib.error:
            return None
    match = _BANNER.search(data)
    return match.group(1).decode('ascii') if match else None


def _bootloader(raw):
    match = re.search(rb'GNU GRUB  version %s\x00+([0-9][A-Za-z0-9._+~:-]{0,60})\x00', raw)
    if match:
        return 'GRUB ' + match.group(1).decode('ascii')
    match = re.search(rb'#### LoaderInfo: (systemd-boot [0-9][A-Za-z0-9._+~-]{0,60}) ####', raw)
    if match:
        return match.group(1).decode('ascii')
    match = re.search(rb'U-Boot (?:SPL )?[0-9][A-Za-z0-9._+~-]{1,60}', raw)
    return match.group().decode('ascii') if match else None


def _boot_entries(fs, path, raw):
    """Kernel references from extlinux, GRUB, BLS entries or U-Boot env text."""
    entries = []
    for line in raw.decode('utf-8', 'replace').splitlines()[:2000]:
        line = line.strip()
        match = (re.match(r'(?i)(?:linux|kernel)\s+(\S+)', line)
                 or re.match(r'knl_name=(\S+)', line)
                 or re.match(r"menuentry\s+['\"]([^'\"]{1,120})['\"]", line)
                 or re.match(r'search\s.*--fs-uuid\s+(?:--set=\S+\s+)?([0-9A-Fa-f-]{9,36})', line)
                 or re.match(r'search\.fs_uuid\s+([0-9A-Fa-f-]{9,36})', line)
                 or re.match(r'search\.fs_label\s+(\S{1,64})', line))
        if match and _TEXT.fullmatch(match.group(1)):
            value = match.group(1)
            if line.startswith('search.fs_label'):
                value = 'root label ' + value
            elif line.startswith('search'):
                value = 'root UUID ' + value
            if value not in entries:
                entries.append(value)
        if len(entries) >= 8:
            break
    return entries


_BOOT_CONFIGS = ('extlinux/extlinux.conf', 'boot/extlinux/extlinux.conf', 'boot/grub/grub.cfg',
                 'grub/grub.cfg', 'EFI/BOOT/grub.cfg', 'EFI/debian/grub.cfg', 'EFI/ubuntu/grub.cfg')


def _inspect_fs(fs):
    result = {'filesystem': {'type': fs.kind}}
    if fs.label:
        result['filesystem']['label'] = fs.label
    if fs.uuid:
        result['filesystem']['uuid'] = fs.uuid
    versions = []
    for path in ('etc/os-release', 'usr/lib/os-release'):
        raw = fs.read(path)
        if raw:
            fields = _os_release(raw)
            if fields:
                identifier = re.sub(r'[^a-z0-9_-]', '', fields.get('id', ''))
                extra = fs.read('etc/%s_version' % identifier, 256) if identifier else None
                extra = extra and _text(extra.strip())
                if extra and extra not in (fields.get('version_id'), fields.get('debian_version_full')):
                    fields['build'] = extra
                result['os'] = fields
                name = fields.get('pretty_name') or ' '.join(filter(None, (fields.get('name'), fields.get('version'))))
                detail = fields.get('debian_version_full') or fields.get('image_version') or fields.get('build_id') or fields.get('build')
                versions.append({'value': name + (' · ' + detail if detail and detail not in name else ''), 'source': 'os-release'})
                break
    kernels = []
    for directory in ('', 'boot'):
        names = sorted(name for name, is_dir in fs.listdir(directory) if not is_dir and _KERNEL_NAME.fullmatch(name))
        for name in names[-3:]:
            path = (directory + '/' + name).strip('/')
            release = _kernel_release(fs, path)
            if release and release not in [item['release'] for item in kernels]:
                kernels.append({'release': release, 'file': '/' + path, 'source': 'kernel-banner'})
    modules = sorted({name for directory in ('usr/lib/modules', 'lib/modules')
                      for name, is_dir in fs.listdir(directory) if is_dir and _RELEASE.fullmatch(name)})
    if modules:
        result['kernel_modules'] = modules[:8]
    if kernels:
        result['kernels'] = kernels
        if not versions:
            versions.extend({'value': 'Linux ' + item['release'], 'source': item['source']} for item in kernels)
    elif modules and not versions:
        versions.append({'value': 'Linux ' + modules[-1], 'source': 'module-directory'})
    boot = {}
    for path in _BOOT_CONFIGS:
        raw = fs.read(path, 1024 * 1024)
        if raw:
            entries = _boot_entries(fs, path, raw)
            if entries:
                boot.setdefault('entries', []).extend(item for item in entries if item not in boot.get('entries', []))
                boot.setdefault('configs', []).append('/' + path)
    for name, is_dir in fs.listdir(''):
        if not is_dir and re.fullmatch(r'env_[A-Za-z0-9_-]{1,40}\.txt', name):
            raw = fs.read(name, 64 * 1024)
            entries = _boot_entries(fs, name, raw or b'')
            if entries:
                boot.setdefault('entries', []).extend(entries)
                boot.setdefault('configs', []).append('/' + name)
    efi = []
    for vendor, is_dir in fs.listdir('EFI'):
        if not is_dir:
            continue
        for name, child_dir in fs.listdir('EFI/' + vendor):
            if not child_dir and name.lower().endswith('.efi'):
                efi.append('/EFI/%s/%s' % (vendor, name))
    if efi:
        boot['efi'] = efi[:8]
        for path in efi[:4]:
            raw = fs.read(path.lstrip('/'), 16 * 1024 ** 2)
            loader = raw and _bootloader(raw)
            if loader:
                boot['loader'] = loader
                if not versions:
                    versions.append({'value': loader, 'source': 'bootloader', 'file': path})
                break
    if boot:
        boot['entries'] = boot.get('entries', [])[:8]
        result['boot'] = boot
    result['versions'] = versions
    return result


_FAULTS = (ImageError, struct.error, ValueError, IndexError, KeyError, ZeroDivisionError,
           RecursionError, OverflowError, UnicodeError)


def _safe_inspect(fs):
    """Corrupt metadata degrades to an unreadable result; only budget exhaustion propagates."""
    try:
        return _inspect_fs(fs)
    except _FAULTS as error:
        if isinstance(error, ImageError) and 'budget' in str(error):
            raise
        return {'filesystem': {'type': fs.kind}, 'versions': [], 'unreadable': True}


def _filesystem(dev):
    for kind in (_Ext, _Fat):
        try:
            return kind(dev)
        except _FAULTS as error:
            if isinstance(error, ImageError) and 'budget' in str(error):
                raise
            continue
    return None


def _partitions(dev):
    """GPT (512 or 4096-byte sectors) or MBR primary partitions."""
    for sector in (512, 4096):
        if dev.size < sector * 3:
            continue
        header = dev.read(sector, 92)
        if header[:8] != b'EFI PART':
            continue
        table, count, entry_size = struct.unpack_from('<QII', header, 72)
        if count > 256 or entry_size < 128 or entry_size > 1024:
            raise ImageError('invalid GPT header')
        raw = dev.read(table * sector, count * entry_size)
        found = []
        for index in range(count):
            entry = raw[index * entry_size:(index + 1) * entry_size]
            kind = uuid.UUID(bytes_le=entry[:16])
            if kind.int == 0:
                continue
            first, last = struct.unpack_from('<QQ', entry, 32)
            name = entry[56:128].decode('utf-16le', 'replace').split('\0')[0]
            found.append({'index': index + 1, 'name': name if _TEXT.fullmatch(name or '\0') else '',
                          'type': _GPT_TYPES.get(kind, str(kind)), 'offset': first * sector,
                          'size': (last - first + 1) * sector})
        return 'gpt', found
    boot = dev.read(0, 512)
    if boot[510:512] != b'\x55\xaa':
        return None, []
    found = []
    for index in range(4):
        kind, start, count = boot[446 + index * 16 + 4], *struct.unpack_from('<II', boot, 446 + index * 16 + 8)
        if kind and count and kind != 0xee:
            found.append({'index': index + 1, 'name': '', 'type': '0x%02x' % kind,
                          'offset': start * 512, 'size': count * 512})
    return ('mbr', found) if found else (None, [])


def image_content(source, size, partition='', kind='partition'):
    """Describe an OS payload file; raises ImageError only for budget/IO faults."""
    budget = _Budget()
    dev = _File(source, size, budget)
    result = {'size': size, 'format': 'raw', 'version': None, 'version_state': 'unknown', 'versions': []}
    if size >= 28 and struct.unpack('<I', dev.read(0, 4))[0] == _Sparse.MAGIC:
        try:
            dev = _Sparse(dev)
        except _FAULTS as error:
            if isinstance(error, ImageError) and 'budget' in str(error):
                raise
            result.update(format='android-sparse', version_state='invalid-format')
            return result
        result['sparse'] = True
        result['expanded_size'] = dev.size
    head = dev.read(0, min(dev.size, 6))
    if head[:6] == b'\xfd7zXZ\x00' or head[:2] == b'\x1f\x8b' or head[:4] == b'\x28\xb5\x2f\xfd':
        result['format'] = 'compressed'
        return result
    fs = _filesystem(dev)
    if fs:
        result['format'] = fs.kind
        result.update(_safe_inspect(fs))
    else:
        try:
            table, parts = _partitions(dev)
        except _FAULTS as error:
            if isinstance(error, ImageError) and 'budget' in str(error):
                raise
            table, parts = None, []
            result['version_state'] = 'invalid-format'
        if table:
            result['format'] = table
            versions, inspected = [], []
            for part in parts[:MAX_PARTITIONS]:
                item = dict(part)
                if part['offset'] + part['size'] <= dev.size:
                    try:
                        piece = _Slice(dev, part['offset'], part['size'])
                        inner = _filesystem(piece)
                        if not inner and part['name'] in _FIRMWARE and part['size'] <= nor_contents.MAX_PARTITION_BYTES:
                            # Boot firmware stored on the same disk reuses NOR content rules.
                            content = nor_contents.partition_content(piece.read(0, part['size']), part['name'])
                            item['content'] = {key: content[key] for key in ('format', 'version', 'version_state', 'description', 'built_at') if key in content}
                            versions.extend(dict(value, source='firmware', partition=part['name']) for value in content['versions'][:1])
                        if inner:
                            item.update(_safe_inspect(inner))
                            versions.extend(item['versions'])
                    except _FAULTS as error:
                        if isinstance(error, ImageError) and 'budget' in str(error):
                            raise
                inspected.append(item)
            result['partitions'] = inspected
            result['versions'] = versions
    unique = []
    order = {'os-release': 0, 'kernel-banner': 1, 'module-directory': 2, 'bootloader': 3}
    for item in sorted(result['versions'], key=lambda value: order.get(value['source'], 9)):
        if item['value'] not in [value['value'] for value in unique]:
            unique.append(item)
    result['versions'] = unique[:8]
    if unique:
        result['version_state'] = 'identified'
        result['version'] = unique[0]['value']
    elif result['format'] in ('ext2', 'ext3', 'ext4', 'fat12', 'fat16', 'fat32', 'gpt', 'mbr'):
        result['version_state'] = 'no-version-files'
    return result
