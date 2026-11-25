"""Preinstalled RAM-only FSBL/U-Boot; never a partition payload source."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .tasks import TaskError

# DDR training results, one per board. In USB download mode the K3 FSBL
# (vendor SPL) ignores training data saved on the board's boot storage, which
# may come from other firmware or another board: it trains DDR (about 5.6 s)
# and the executor reads the result from SRAM once the RAM loader runs. Later
# loads on the same USB port embed it into the FSBL image, where SPL uses it
# instead of training; the executor then requires the loader to report the
# same board (EEPROM serial) before anything else, and a mismatch or failed
# memory check discards the result so the next load trains again.
_TRAINING_FILE = 'ddr-training.json'
_TRAINING_LOCK = threading.Lock()
_TRAINING_SIZE = 0x9800
_TRAINING_MAGIC = 0x54524444
_TRAINING_TYPES = {0: 'lpddr4x', 1: 'lpddr5'}
_TRAINING_ENTRIES = 64
# Ports whose last load used an embedded result, so a failure there discards it.
_EMBEDDED = {}
K1_RAM_BOOT_PROTOCOL = 'spacemit-k1-ramboot-v1'
K3_RAM_BOOT_PROTOCOL = 'spacemit-k3-ramboot-v1'
RAM_BOOT_PROTOCOLS = {'k1': K1_RAM_BOOT_PROTOCOL, 'k3': K3_RAM_BOOT_PROTOCOL}


def _port(binding):
    device = getattr(binding, 'device', None) or {}
    port = device.get('port_path') if isinstance(device, dict) else None
    return tuple(port) if isinstance(port, (list, tuple)) and port else None


def _port_key(port):
    return '-'.join(str(p) for p in port)


def _training_entries(root):
    try:
        document = json.loads((Path(root) / _TRAINING_FILE).read_text())
    except (OSError, ValueError, TypeError):
        return []
    entries = document.get('entries') if isinstance(document, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _save_training(root, entries):
    path = Path(root) / _TRAINING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'entries': entries[-_TRAINING_ENTRIES:]}, sort_keys=True))
    os.replace(temporary, path)


def _training_data(value):
    """The raw result if it is a well-formed, chip-stamped training record."""
    import base64
    import struct
    import zlib
    try:
        data = base64.b64decode(value, validate=True) if isinstance(value, str) else b''
    except ValueError:
        return None
    if len(data) != _TRAINING_SIZE:
        return None
    magic, crc = struct.unpack_from('<II', data)
    kind = struct.unpack_from('<I', data, 28)[0]
    if magic != _TRAINING_MAGIC or crc != zlib.crc32(data[8:]) or kind not in _TRAINING_TYPES:
        return None
    return data


def _installed_fsbl_sha256():
    try:
        entry = _manifest()[2]['k3']['fsbl']
        return entry['sha256'] if isinstance(entry, dict) else None
    except (OSError, ValueError, KeyError, TypeError, TaskError):
        return None


def record_training(binding, root, result):
    """Keep the training result the DeckLab FSBL produced on this board."""
    port = _port(binding)
    if root is None or port is None or binding.profile_data.get('chip_family') != 'k3' or not isinstance(result, dict):
        raise ValueError('training result without a K3 USB port')
    data = _training_data(result.get('data'))
    if data is None:
        raise ValueError('malformed DDR training result')
    identity = result.get('identity') if isinstance(result.get('identity'), dict) else {}
    serial = identity.get('eeprom-serial') or identity.get('serialno') or ''
    if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', serial):
        raise ValueError('training result without a board serial')
    entry = {'port': _port_key(port), 'board_serial': serial,
             'fsbl_sha256': _installed_fsbl_sha256(), 'captured_at': int(time.time()), 'data': result['data']}
    with _TRAINING_LOCK:
        # One result per board and per port: the newest board on a port wins.
        entries = [e for e in _training_entries(root)
                   if e.get('board_serial') != serial and e.get('port') != entry['port']]
        _save_training(root, entries + [entry])
    return entry


def _training_for(binding, root, fsbl_sha256):
    port = _port(binding)
    if root is None or port is None:
        return None
    with _TRAINING_LOCK:
        entries = _training_entries(root)
    for entry in reversed(entries):
        if (entry.get('port') == _port_key(port) and entry.get('fsbl_sha256') == fsbl_sha256
                and isinstance(entry.get('board_serial'), str) and entry['board_serial']):
            data = _training_data(entry.get('data'))
            if data is not None:
                return entry, data
    return None


def discard_training(binding, root):
    """Forget the result used on this port; True if the last load used one."""
    port = _port(binding)
    if port is None:
        return False
    used = _EMBEDDED.pop(port, False)
    if root is not None:
        with _TRAINING_LOCK:
            entries = _training_entries(root)
            kept = [e for e in entries if e.get('port') != _port_key(port)]
            if len(kept) != len(entries):
                _save_training(root, kept)
    return used


def embed_training(image, slots, data):
    """FSBL image with a training result in the slot for its DDR type."""
    import struct
    import zlib
    kind = _TRAINING_TYPES[struct.unpack_from('<I', data, 28)[0]]
    slot = slots.get(kind) if isinstance(slots, dict) else None
    if (not isinstance(slot, dict) or type(slot.get('offset')) is not int or slot.get('size') != _TRAINING_SIZE
            or len(image) < 0x1000):
        raise ValueError('FSBL has no training slot for ' + kind)
    magic, _, _, _, size, _, _, _ = struct.unpack_from('<4sBBHQQII', image, 0xfe0)
    offset = slot['offset']
    if magic != b'AIHD' or offset < 0x1000 or offset + _TRAINING_SIZE > 0x1000 + size or 0x1000 + size > len(image):
        raise ValueError('FSBL image layout does not hold the training slot')
    patched = bytearray(image)
    patched[offset:offset + _TRAINING_SIZE] = data
    struct.pack_into('<I', patched, 0xffc, zlib.crc32(bytes(patched[0x1000:0x1000 + size])))
    return bytes(patched)


def manifest_path():
    configured = os.environ.get('DECKLAB_FLASH_LOADERS')
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


def k1_ram_boot_installed():
    return ram_boot_installed('k1')


def ram_boot_installed(family):
    try:
        material = _manifest()[2].get(family, {}).get('ram_boot', {})
        return material.get('protocol') == RAM_BOOT_PROTOCOLS[family]
    except (OSError, ValueError, AttributeError, TaskError):
        return False


def _loader_entry(entry, allowed=frozenset()):
    if (not isinstance(entry, dict) or not {'file', 'sha256'} <= set(entry) <= {'file', 'sha256'} | allowed
            or not isinstance(entry['file'], str) or not re.fullmatch('[0-9a-f]{64}', str(entry['sha256']))):
        raise TaskError('invalid RAM loader entry', 503)
    return entry


def _staged(target, check):
    if target.is_symlink() or not target.is_file():
        raise TaskError('RAM loader cache changed', 409)
    digest = hashlib.sha256()
    with target.open('rb') as cached:
        for chunk in iter(lambda: cached.read(1024 ** 2), b''):
            check()
            digest.update(chunk)
    return target.stat().st_size, digest.hexdigest()


def prepare_materials(binding, workspace, check, root=None, *, ram_boot=False):
    """Stage the RAM loaders; `root` is the flashing work directory that keeps
    per-board DDR training results."""
    family = binding.profile_data.get('chip_family')
    path, raw, families = _manifest()
    material = families.get(family)
    protocols = ('stock', 'spacemit-ram-v1')
    if ram_boot:
        material = material.get('ram_boot') if isinstance(material, dict) else None
        protocols = (RAM_BOOT_PROTOCOLS[family],)
    if not isinstance(material, dict) or material.get('protocol') not in protocols:
        raise TaskError('预备刷机环境不支持当前芯片', 409)
    bundle = path.parent.resolve()
    output = dict(chip_family=family, protocol=material['protocol'])
    destination = Path(workspace) / 'ram-loaders'
    if Path(workspace).parent.name == 'images':
        destination = destination / hashlib.sha256(raw).hexdigest() / family
    port = _port(binding)
    use_usb2 = False
    if family == 'k3' and not ram_boot and port:
        from .config import SETTINGS
        saved = SETTINGS.read().get('environment', {})
        ports = saved.get('FLASH_USB2_PORTS', os.environ.get('DECKLAB_FLASH_USB2_PORTS', ''))
        use_usb2 = '.'.join(map(str, port)) in {value.strip() for value in ports.split(',')}
    uboot_key = 'usb2_uboot' if use_usb2 else 'uboot'
    if use_usb2:
        if not material.get(uboot_key):
            raise TaskError('已选择 USB 2.0 兼容端口，请安装包含 K3 兼容加载器的组件包', 409)
        destination /= 'usb2'
    fsbl_entry = _loader_entry(material.get('fsbl'), frozenset({'training_slots'}))
    training = None
    if family == 'k3' and 'training_slots' in fsbl_entry:
        training = _training_for(binding, root, fsbl_entry['sha256'])
    if port is not None:
        _EMBEDDED[port] = training is not None
    embedded = None
    if training is not None:
        record, data = training
        source = (bundle / fsbl_entry['file']).resolve()
        if source == bundle or bundle not in source.parents or not source.is_file() or source.stat().st_size > 16 * 1024 ** 2:
            raise TaskError('RAM loader is outside the installed materials directory', 503)
        image = source.read_bytes()
        if hashlib.sha256(image).hexdigest() != fsbl_entry['sha256']:
            raise TaskError('RAM loader checksum changed', 409)
        try:
            embedded = embed_training(image, fsbl_entry['training_slots'], data)
        except ValueError as error:
            raise TaskError('RAM loader training slot is invalid: %s' % error, 503) from None
        # The executor needs factory/FSBL.bin next to u-boot.itb.
        destination = destination / ('trained-' + hashlib.sha256(embedded).hexdigest()[:16])
        # The executor requires the loader to report this board.
        output['trained_for'] = record['board_serial']
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key in ('fsbl', 'uboot'):
        entry = fsbl_entry if key == 'fsbl' else _loader_entry(material.get(uboot_key))
        expected = hashlib.sha256(embedded).hexdigest() if key == 'fsbl' and embedded is not None else entry['sha256']
        source = (bundle / entry['file']).resolve()
        if source == bundle or bundle not in source.parents or not source.is_file():
            raise TaskError('RAM loader is outside the installed materials directory', 503)
        target = destination / ('factory/FSBL.bin' if key == 'fsbl' else 'u-boot.itb')
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if not target.exists():
            digest = hashlib.sha256()
            size = 0
            # Concurrent boards may prepare the same cached pair. Publish only a
            # complete, verified copy, with a private temporary file per caller.
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as outgoing:
                temporary = Path(outgoing.name)
            try:
                incoming = io.BytesIO(embedded) if key == 'fsbl' and embedded is not None else source.open('rb')
                with incoming, temporary.open('wb') as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 ** 2), b''):
                        check()
                        size += len(chunk)
                        if size > 16 * 1024 ** 2:
                            raise TaskError('RAM loader exceeds size limit', 503)
                        digest.update(chunk)
                        outgoing.write(chunk)
                if not size or digest.hexdigest() != expected:
                    raise TaskError('RAM loader checksum changed', 409)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        size, digest = _staged(target, check)
        if not 0 < size <= 16 * 1024 ** 2 or digest != expected:
            raise TaskError('RAM loader cache changed', 409)
        output[key] = dict(path=str(target), sha256=digest, size=size, name=target.name, id=key)
    return output
