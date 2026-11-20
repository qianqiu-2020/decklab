"""Measured flashing configurations ("刷写配置"), one per board connection.

`calibrate` measures USB, on-board expansion, disk write and host compression
rates on a board and derives a configuration (codec, level, run size,
background writes). Later flashes of the same USB port use it; a port without
its own configuration uses the newest one measured for the same chip.
A configuration changes speed only, never what is written.
"""
import json
import math
import os
from pathlib import Path
import threading
import time

_LOCK = threading.Lock()
_FILE = 'tuning.json'
_MAX_ENTRIES = 64
_TUNING_KEYS = {'codec', 'zstd_level', 'gzip_level', 'run_bytes', 'background',
                'usb_bps', 'gzip_bps', 'dlz_bps', 'write_bps'}


def _port(binding):
    device = getattr(binding, 'device', None) or {}
    port = device.get('port_path') if isinstance(device, dict) else None
    if not isinstance(port, (list, tuple)) or not port or not all(type(p) is int and 0 <= p < 256 for p in port):
        raise ValueError('flash configuration requires a USB port path')
    return '-'.join(str(p) for p in port)


def key(binding):
    chip = binding.profile_data.get('chip_family')
    if chip not in ('k1', 'k3'):
        raise ValueError('flash configuration requires a K1 or K3 target')
    return chip + '@' + _port(binding)


def _path(root):
    return Path(root) / _FILE


def _load(root):
    try:
        document = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return []
    entries = document.get('entries') if isinstance(document, dict) else None
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get('key'), str)] if isinstance(entries, list) else []


def _save(root, entries):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'entries': entries[-_MAX_ENTRIES:]}, indent=1, sort_keys=True))
    os.replace(temporary, path)


def valid_tuning(value):
    if not isinstance(value, dict) or set(value) - _TUNING_KEYS or value.get('codec') not in ('dlz', 'gzip', 'raw'):
        return False
    for name in ('usb_bps', 'gzip_bps', 'dlz_bps', 'write_bps'):
        rate = value.get(name, 0)
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0 or rate > 1e11:
            return False
    if not value.get('usb_bps') or type(value.get('background', False)) is not bool:
        return False
    return all(type(value.get(name, 0)) is int for name in ('zstd_level', 'gzip_level', 'run_bytes'))


def record(root, binding, result):
    """Store the executor's calibration result for this board connection."""
    tuning = result.get('tuning') if isinstance(result, dict) else None
    if not valid_tuning(tuning):
        raise ValueError('executor returned an invalid flash configuration')
    identity = result.get('identity') if isinstance(result.get('identity'), dict) else {}
    serial = identity.get('eeprom-serial') or identity.get('serialno') or ''
    entry = {'key': key(binding), 'chip_family': binding.profile_data.get('chip_family'),
             'port': _port(binding), 'board_serial': serial if isinstance(serial, str) else '',
             'measured_at': int(time.time()), 'system_disk': str(result.get('system_disk') or ''),
             'measured': result.get('measured') if isinstance(result.get('measured'), dict) else {},
             'codecs': result.get('codecs') if isinstance(result.get('codecs'), list) else [],
             'predicted': result.get('predicted') if isinstance(result.get('predicted'), dict) else {},
             'tuning': tuning}
    with _LOCK:
        entries = [e for e in _load(root) if e['key'] != entry['key']]
        entries.append(entry)
        _save(root, entries)
    return dict(entry, scope='port')


def lookup(root, binding):
    """The configuration a flash of this board uses, or None."""
    try:
        wanted = key(binding)
    except ValueError:
        return None
    with _LOCK:
        entries = _load(root)
    exact = [e for e in entries if e['key'] == wanted and valid_tuning(e.get('tuning'))]
    if exact:
        return dict(exact[-1], scope='port')
    chip = binding.profile_data.get('chip_family')
    same = [e for e in entries if e.get('chip_family') == chip and valid_tuning(e.get('tuning'))]
    if same:
        newest = max(same, key=lambda e: e.get('measured_at', 0))
        return dict(newest, scope='chip')
    return None


def forget(root, binding):
    """Remove this connection's own configuration."""
    wanted = key(binding)
    with _LOCK:
        entries = _load(root)
        kept = [e for e in entries if e['key'] != wanted]
        if len(kept) != len(entries):
            _save(root, kept)
    return len(kept) != len(entries)
