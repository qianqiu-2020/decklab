"""Read Debian GNOME desktop login facts over the verified private USB channel."""
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys


queried = []


def run(*args):
    if args[0] == 'busctl':
        queried.append('accounts')
    elif args[0] == 'loginctl':
        queried.append(args[1])
    elif args[0] == 'grdctl':
        queried.append('grdctl')
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=4,
                                   env=dict(os.environ, LC_ALL='C', SYSTEMD_COLORS='0'))


def inspect(stage_only=False):
    if 'ID=debian' not in Path('/etc/os-release').read_text().splitlines():
        return {}
    if not Path('/usr/bin/grdctl').is_file():
        return {}
    # AccountsService is the account source used by GDM. Require a live local
    # Initial Setup greeter as well: empty accounts alone do not identify a UI.
    no_users = run('busctl', 'get-property', 'org.freedesktop.Accounts', '/org/freedesktop/Accounts',
                   'org.freedesktop.Accounts', 'HasNoUsers').strip()
    seat = run('loginctl', 'show-seat', 'seat0', '-p', 'ActiveSession', '--value').strip()
    props = {}
    if seat:
        props = dict(line.split('=', 1) for line in run('loginctl', 'show-session', seat, '-p', 'Name',
                     '-p', 'Active', '-p', 'Class', '-p', 'Type').splitlines() if '=' in line)
    wizard = (props.get('Name') == 'gnome-initial-setup' and props.get('Class') == 'greeter'
              and props.get('Active') == 'yes' and props.get('Type') in ('wayland', 'x11'))
    if no_users not in ('b true', 'b false'):
        return {}
    stage = ('initial' if no_users == 'b true' else 'finishing') if wizard else (
        'no-user' if no_users == 'b true' else 'login')
    result = {'stage': stage}
    if stage_only or stage == 'no-user':
        return result
    try:
        # Only the RDP section may supply these fields; VNC has its own password.
        status = run('grdctl', '--system', 'status', '--show-credentials')
        section = status.split('RDP:\n', 1)[1].split('\nVNC:', 1)[0]
        fields = dict(line.strip().split(': ', 1) for line in section.splitlines() if ': ' in line)
        if wizard:
            certs = list(Path('/run/debian-initial-setup-rdp').glob('session.*/tls/tls.crt'))
            if len(certs) != 1:
                return result
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certs[0].read_text())).hexdigest()
            port = 3390  # Debian's private GNOME Initial Setup launcher
        else:
            if fields['Status'] != 'enabled':
                return result
            port = int(fields['Port'])
            pin = fields['TLS fingerprint'].replace(':', '').lower()
        result.update(port=port, pin=pin, username=fields['Username'], password=fields['Password'])
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return result


if __name__ == '__main__':
    try:
        report = inspect('--stage-only' in sys.argv[1:])
    except (OSError, ValueError, subprocess.SubprocessError):
        report = {}
    print(json.dumps(dict(report, queries=queried)))
