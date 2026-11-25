"""Explicit software identity provisioning; ordinary probes only read the marker."""
import re
import shlex

MARKER_PATH = '/boot/decklab-id'
LEGACY_MARKER_PATH = '/boot/seriald-id'
UUID_PATTERN = '[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
MARKER_PATTERN = 'boot-' + UUID_PATTERN


def marker_serial(info):
    value = str(info.get('boot_marker') or '').strip()
    return value if re.fullmatch(MARKER_PATTERN, value) else ''


def read_command(path=MARKER_PATH):
    # Use the same bounded, symlink-rejecting read for both marker names.
    if path != MARKER_PATH:
        target = shlex.quote(str(path))
        return f"if [ -f {target} ] && [ ! -L {target} ]; then head -c 80 {target} 2>/dev/null | tr '\\r\\n' '  '; fi"
    return ("p=/boot/decklab-id;[ -e $p ]||[ -L $p ]||p=/boot/seriald-id;"
            "if [ -f $p ]&&[ ! -L $p ];then head -c80 $p 2>/dev/null|tr '\\r\\n' '  '; fi")


def init_command(path=MARKER_PATH, uuid_path='/proc/sys/kernel/random/uuid'):
    """Create once using exclusive redirection (also works on FAT /boot partitions).

    A crash can leave a partial file: reject it on retry instead of replacing an identity.
    Paths are configurable only for isolated tests, never through the HTTP API.
    """
    legacy = 'l=/boot/seriald-id;[ -e $p ]||[ -L $p ]||{ [ ! -e $l ]&&[ ! -L $l ];}||p=$l;' if path == MARKER_PATH else ''
    parts = [
        '(', 'p=' + shlex.quote(str(path)) + ';', legacy,
        'if [ -L "$p" ] || { [ -e "$p" ] && [ ! -f "$p" ]; }; then',
        'echo BOOT_MARKER_ERROR=not-a-regular-file; exit 1; fi;',
        'if [ ! -e "$p" ]; then',
        'u=$(cat ' + shlex.quote(str(uuid_path)) + ' 2>/dev/null);',
        'if [ "${#u}" -ne 36 ] || ! printf "%s\\n" "$u" | LC_ALL=C grep -Eq '
        + shlex.quote('^' + UUID_PATTERN + '$') + '; then',
        'echo BOOT_MARKER_ERROR=random-uuid-unavailable; exit 1; fi;',
        '(umask 022; set -C; printf "boot-%s\\n" "$u" > "$p") 2>/dev/null ||',
        '{ echo BOOT_MARKER_ERROR=create-failed; exit 1; };',
        'sync "$p" 2>/dev/null || { echo BOOT_MARKER_ERROR=sync-failed; exit 1; }; fi;',
        'v=$(head -c 80 "$p" 2>/dev/null);',
        'if [ "${#v}" -ne 41 ] || ! printf "%s\\n" "$v" | LC_ALL=C grep -Eq '
        + shlex.quote('^' + MARKER_PATTERN + '$') + '; then',
        'echo BOOT_MARKER_ERROR=invalid-existing-marker; exit 1; fi;',
        'echo BOOT_MARKER_STATUS=ready', ')',
    ]
    return ' '.join(parts)
