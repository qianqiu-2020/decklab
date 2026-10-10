"""Index daily serial logs into boot epochs without rewriting the archives."""
import hashlib
import os
import re
from datetime import datetime


STAMP = re.compile(br'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  ')
BOOT_MARKERS = (
    (re.compile(br'(?:^|\x00)sys:\s*0x[0-9a-f]+', re.I), 'Boot ROM'),
    (re.compile(br'^U-Boot SPL \d{4}\.\d{2}', re.I), 'U-Boot SPL'),
    (re.compile(br'^U-Boot \d{4}\.\d{2}', re.I), 'U-Boot'),
    (re.compile(br'^(?:<\d+>)?(?:\[\s*\d+(?:\.\d+)?\]\s*)?Linux version \d', re.I), 'Linux'),
)


def _marker(line):
    # Remove our host timestamp and terminal paint codes, but never search
    # arbitrary text: journalctl pages and shell commands can quote banners.
    line = STAMP.sub(b'', line, count=1)
    line = re.sub(br'\x1b\[[0-?]*[ -/]*[@-~]', b'', line).lstrip(b' \t\r\x00')
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


def boot_epochs(root, files, cache=None):
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
        stat = os.stat(path)
        size = stat.st_size
        snapshots.append((entry['name'], size, _archive_date(entry)))
        fingerprint = (root, stat.st_dev, stat.st_ino, size, stat.st_mtime_ns)
        cached = cache.get(entry['name']) if cache is not None else None
        if cached is not None and cached[0] == fingerprint:
            markers, file_last_stamp = cached[1:3]
        else:
            # Growing daily logs are indexed from the last complete line.
            # An incomplete trailing line is re-read once more bytes arrive.
            same_file = (cached is not None and len(cached) == 4
                         and cached[0][:3] == fingerprint[:3]
                         and cached[0][3] <= size)
            markers = list(cached[1]) if same_file else []
            file_last_stamp = cached[2] if same_file else ''
            resume = cached[3] if same_file else 0
            with open(path, 'rb') as source:
                source.seek(resume)
                while True:
                    offset = source.tell()
                    line = source.readline()
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        resume = offset
                        break
                    stamp = _timestamp(line)
                    if stamp:
                        file_last_stamp = stamp
                    kind = _marker(line)
                    if kind:
                        markers.append((offset, stamp, kind))
                    resume = source.tell()
            if cache is not None:
                cache[entry['name']] = (fingerprint, markers, file_last_stamp, resume)
        if file_last_stamp:
            last_stamp = file_last_stamp
        for offset, stamp, kind in markers:
            # Firmware banners and the first Linux banner form one boot.
            marker_time = _stamp_seconds(stamp)
            close_chain = (marker_time is not None and last_marker_time is not None
                           and 0 <= marker_time - last_marker_time <= (120 if kind == 'Linux' else 10))
            same_chain = ((last_marker_kind == 'Boot ROM' and kind in ('U-Boot SPL', 'U-Boot'))
                          or (last_marker_kind == 'U-Boot SPL' and kind == 'U-Boot')
                          or (last_marker_kind in ('Boot ROM', 'U-Boot SPL', 'U-Boot')
                              and kind == 'Linux'))
            if candidates and close_chain and same_chain:
                # Advance the phase even when it belongs to the same boot;
                # a later Linux-only restart must not chain to stale firmware.
                last_marker_kind, last_marker_time = kind, marker_time
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


ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def search_epochs(root, epochs, query, offset=0, limit=20):
    """Search complete boot ranges newest first, streaming lines with bounded results.

    Pagination limits the response, never the amount of history searched. Each
    result represents one matching line, with a short excerpt around the match.
    """
    needle = query.casefold()
    results = []
    skipped = 0
    for epoch in epochs:
        line_number = 0
        for part in epoch['parts']:
            fd = os.open(os.path.join(root, part['file']),
                         os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as source:
                source.seek(part['start'])
                remaining = part['end'] - part['start']
                while remaining > 0:
                    raw = source.readline(remaining)
                    if not raw:
                        break
                    remaining -= len(raw)
                    line_number += 1
                    text = ANSI.sub('', raw.decode('utf-8', 'replace')).rstrip('\r\n')
                    position = text.casefold().find(needle)
                    if position < 0:
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    if len(results) == limit:
                        return {'results': results, 'offset': offset,
                                'next_offset': offset + limit, 'more': True}
                    start = max(0, position - 80)
                    excerpt = text[start:start + 320]
                    results.append({'id': epoch['id'], 'number': epoch['number'],
                                    'started': epoch['started'], 'date': part.get('date', ''),
                                    'file': part['file'], 'line': line_number,
                                    'excerpt': ('…' if start else '') + excerpt
                                    + ('…' if start + 320 < len(text) else '')})
    return {'results': results, 'offset': offset, 'next_offset': None, 'more': False}
