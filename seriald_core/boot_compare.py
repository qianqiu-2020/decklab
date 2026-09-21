"""Compare archived boot epochs while ignoring capture and kernel clock noise."""
import difflib
import re


MAX_BOOT_BYTES = 4 * 1024 * 1024
MAX_LINES = 12000
MAX_ROWS = 1500
STAMP = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  ')
KERNEL_TIME = re.compile(r'^\[\s*\d+(?:\.\d+)?\]\s*')
CLUE = re.compile(r'\b(?:error|failed?|panic|oops|timeout|timed out|segfault|I/O error|watchdog|resetting)\b', re.I)


def _lines(data):
    lines = data.decode('utf-8', 'replace').splitlines()
    if len(lines) > MAX_LINES:
        raise ValueError('boot log has too many lines to compare')
    display = [STAMP.sub('', line).rstrip('\r') for line in lines]
    normalized = [KERNEL_TIME.sub('', line).strip() for line in display]
    return display, normalized


def compare_boots(baseline, target):
    """Return changed lines with nearby context; target-only warnings are clues."""
    if len(baseline) > MAX_BOOT_BYTES or len(target) > MAX_BOOT_BYTES:
        raise ValueError('boot log exceeds 4 MiB comparison limit')
    left, a = _lines(baseline)
    right, b = _lines(target)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=True)
    groups = matcher.get_grouped_opcodes(2)
    rows = []
    counts = {'added': 0, 'removed': 0, 'changed': 0}
    clues = []
    truncated = False
    for group in groups:
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                pairs = [(i, j) for i, j in zip(range(i1, i2), range(j1, j2))]
            else:
                counts['changed' if tag == 'replace' else 'removed' if tag == 'delete' else 'added'] += max(i2-i1, j2-j1)
                width = max(i2-i1, j2-j1)
                pairs = [(i1+k if k < i2-i1 else None,
                          j1+k if k < j2-j1 else None) for k in range(width)]
                if tag in ('insert', 'replace'):
                    old_lines = set(a[i1:i2])
                    for j in range(j1, j2):
                        if CLUE.search(b[j]) and b[j] not in old_lines and len(clues) < 30:
                            clues.append({'line': j+1, 'text': right[j]})
            for i, j in pairs:
                if len(rows) < MAX_ROWS:
                    rows.append({'type': tag, 'left_no': i+1 if i is not None else None,
                                 'left': left[i] if i is not None else '',
                                 'right_no': j+1 if j is not None else None,
                                 'right': right[j] if j is not None else ''})
                else:
                    truncated = True
    return {'counts': counts, 'clues': clues, 'rows': rows, 'truncated': truncated,
            'left_lines': len(left), 'right_lines': len(right)}
