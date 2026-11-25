"""Share the active Bianbu Wayland seat through an existing USB shell."""
import configparser
import json
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


queried = []


def property_value(kind, identity, *properties):
    queried.append('show-' + kind)
    result = subprocess.check_output(
        ['loginctl', 'show-' + kind, identity, *['--property=' + key for key in properties]],
        text=True, timeout=4)
    return dict(line.split('=', 1) for line in result.splitlines() if '=' in line)


def lxqt_seat():
    seat = property_value('seat', 'seat0', 'ActiveSession').get('ActiveSession')
    if not seat:
        return None
    values = property_value('session', seat, 'Active', 'Remote', 'Type', 'Class', 'User', 'Name')
    if (values.get('Active') != 'yes' or values.get('Remote') != 'no'
            or values.get('Type') != 'wayland' or values.get('Class') not in ('greeter', 'user', 'user-early')):
        return None
    uid = int(values['User'])
    directory = Path('/run/user') / str(uid)
    if directory.stat().st_uid != uid or not stat.S_ISDIR(directory.lstat().st_mode):
        return None
    displays = [entry for entry in directory.glob('wayland-*') if entry.name[8:].isdigit()
                and stat.S_ISSOCK(entry.lstat().st_mode) and entry.lstat().st_uid == uid]
    if len(displays) != 1:
        return None
    return uid, displays[0], values['Class'], values.get('Name', '')


def serve_lxqt():
    if 'ID=bianbu' not in Path('/etc/os-release').read_text().splitlines():
        raise ValueError('system changed')
    seat = lxqt_seat()
    if not seat:
        raise ValueError('active Wayland seat unavailable')
    uid, display, _, name = seat
    try:
        account = pwd.getpwuid(uid)
    except KeyError:
        # Calamares removes its temporary account while the finish page is still open.
        if name != 'initer' or not Path('/usr/share/calamares/sddm.conf').is_file():
            raise
        account = None
    if os.geteuid() == 0:
        if account:
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
        else:
            os.setgroups([])
            os.setgid(pwd.getpwnam('nobody').pw_gid)
        os.setuid(uid)
    if os.geteuid() != uid:
        raise PermissionError('Wayland seat belongs to another user')
    env = dict(os.environ, XDG_RUNTIME_DIR=str(display.parent), WAYLAND_DISPLAY=display.name,
               HOME=account.pw_dir if account else str(display.parent), USER=name)
    def disconnected(*_):
        raise SystemExit()
    signal.signal(signal.SIGHUP, disconnected)
    signal.signal(signal.SIGTERM, disconnected)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='decklab-vnc-', dir=display.parent) as folder:
        address = folder + '/display'
        process = subprocess.Popen(['/usr/bin/wayvnc', '--unix-socket', '--config=/dev/null',
                                    '--socket=' + folder + '/control', address], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as screen:
                deadline = time.monotonic() + 5
                while True:
                    if process.poll() is not None:
                        raise OSError('Wayland screen capture unavailable')
                    try:
                        screen.connect(address)
                        break
                    except (ConnectionRefusedError, FileNotFoundError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('display socket unavailable')
                        time.sleep(0.05)
                while True:
                    ready, _, _ = select.select([0, screen], [], [])
                    if 0 in ready:
                        data = os.read(0, 65536)
                        if not data:
                            break
                        screen.sendall(data)
                    if screen in ready:
                        data = screen.recv(65536)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def report_lxqt():
    stage = 'lxqt-unavailable'
    try:
        seat = lxqt_seat() if Path('/usr/bin/wayvnc').is_file() else None
        if seat:
            stage = 'lxqt-greeter' if seat[2] == 'greeter' else 'lxqt-desktop'
            if seat[2] != 'greeter' and seat[3] == 'initer':
                conf = configparser.ConfigParser(interpolation=None, strict=False)
                conf.read('/etc/sddm.conf')
                if (conf.get('Autologin', 'User', fallback='') == 'initer'
                        and conf.get('Autologin', 'Session', fallback='') == 'bianbu-init'):
                    stage = 'lxqt-initial'
                elif Path('/usr/share/calamares/sddm.conf').is_file():
                    stage = 'lxqt-finishing'
    except (OSError, ValueError, KeyError, configparser.Error, subprocess.SubprocessError):
        pass
    print(json.dumps({'stage': stage, 'queries': queried}))
