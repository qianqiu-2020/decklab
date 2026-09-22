"""Index daily serial logs into boot epochs without rewriting the archives."""
import hashlib
import os
import re
from datetime import datetime


STAMP = re.compile(br'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  ')
BOOT_MARKERS = (
    (re.compile(br'(?:^|\x00)sys:\s*0x[0-9a-f]+', re.I), 'Boot ROM'),
    (re.compile(br'\bU-Boot SPL \d{4}\.\d{2}', re.I), 'U-Boot SPL'),
    (re.compile(br'\bU-Boot \d{4}\.\d{2}', re.I), 'U-Boot'),
)


def _marker(line):
    for pattern, label in BOOT_MARKERS:
        if pattern.search(line):
            return label
    return ''


def _timestamp(line):
    match = STAMP.match(line)
    return match.group(1).decode('ascii') if match else ''


def _stamp_seconds(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').timestamp()
    except (TypeError, ValueError):
        return None


def _archive_date(entry):
    date = entry.get('date') or ''
    return '%s-%s-%s' % (date[:4], date[4:6], date[6:]) if re.fullmatch(r'\d{8}', date) else date


def boot_epochs(root, files):
    """Return boot ranges spanning one or more chronological daily files.

    Boot ROM followed immediately by U-Boot/SPL is one boot, rather than two.
    Ranges use byte offsets so the original logs remain the source of truth.
    """
    ordered = sorted((entry for entry in files if entry.get('kind') == 'serial'),
                     key=lambda entry: (_archive_date(entry), entry.get('mtime', 0),
                                        entry.get('name', '')))
    snapshots = []
    candidates = []
    last_stamp = ''
    last_marker_kind = ''
    last_marker_time = None
    for index, entry in enumerate(ordered):
        path = os.path.join(root, entry['name'])
        size = os.path.getsize(path)
        snapshots.append((entry['name'], size, _archive_date(entry)))
        with open(path, 'rb') as source:
            while True:
                offset = source.tell()
                line = source.readline()
                if not line:
                    break
                stamp = _timestamp(line)
                if stamp:
                    last_stamp = stamp
                kind = _marker(line)
                if not kind:
                    continue
                # Firmware commonly prints Boot ROM, SPL and U-Boot banners in
                # one second.  Count that chain once, but keep later resets.
                marker_time = _stamp_seconds(stamp)
                close_chain = (marker_time is not None and last_marker_time is not None
                               and 0 <= marker_time - last_marker_time <= 10)
                same_chain = ((last_marker_kind == 'Boot ROM' and kind in ('U-Boot SPL', 'U-Boot'))
                              or (last_marker_kind == 'U-Boot SPL' and kind == 'U-Boot'))
                if candidates and close_chain and same_chain:
                    continue
                candidates.append({'file_index': index, 'offset': offset,
                                   'started': stamp, 'marker': kind})
                last_marker_kind, last_marker_time = kind, marker_time

    epochs = []
    for number, start in enumerate(candidates):
        end = candidates[number + 1] if number + 1 < len(candidates) else None
        end_index = end['file_index'] if end else len(snapshots) - 1
        end_offset = end['offset'] if end else snapshots[end_index][1]
        parts = []
        for index in range(start['file_index'], end_index + 1):
            name, size, date = snapshots[index]
            begin = start['offset'] if index == start['file_index'] else 0
            finish = end_offset if index == end_index else size
            if finish > begin:
                parts.append({'file': name, 'date': date, 'start': begin, 'end': finish,
                              'size': finish - begin})
        dates = list(dict.fromkeys(part['date'] for part in parts))
        dates = [date for date in dates if date]
        identity = '%s:%d' % (snapshots[start['file_index']][0], start['offset'])
        epochs.append({
            'id': 'boot-' + hashlib.sha256(identity.encode()).hexdigest()[:16],
            'number': number + 1,
            'started': start['started'],
            'ended': (end['started'] if end else last_stamp),
            'marker': start['marker'],
            'files': len(parts),
            'dates': dates,
            'cross_day': len(dates) > 1,
            'size': sum(part['size'] for part in parts),
            'parts': parts,
        })
    return list(reversed(epochs))


def read_epoch(root, epoch):
    data = bytearray()
    for part in epoch['parts']:
        path = os.path.join(root, part['file'])
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            source.seek(part['start'])
            remaining = part['end'] - part['start']
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                data += chunk
                remaining -= len(chunk)
    return bytes(data)
