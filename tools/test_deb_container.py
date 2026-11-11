"""Install/upgrade/purge a Debian package in a disposable, hardware-free Ubuntu container.

No host mounts, devices, published ports or privileged mode. This tests real dpkg
scripts and installed entrypoints, not systemd activation or real serial adapters.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import uuid

from build_deb import build, __version__


CHECK = r'''
import importlib.util, json, os, pathlib, pwd, signal, socket, stat, subprocess, time
from urllib.request import urlopen

assert importlib.util.find_spec('mcp') is None, 'Target system Python must have no MCP SDK'
assert not os.environ.get('SERIALD_MCP_PYTHON'), 'No external MCP Python is permitted'
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1).close()
except OSError:
    pass
else:
    raise AssertionError('Installed acceptance must run after container network disconnection')
base = pathlib.Path('/data/seriald-data')
config = pathlib.Path('/etc/seriald/seriald.env')
account = pwd.getpwnam('seriald')
assert stat.S_IMODE(base.stat().st_mode) == 0o2770
assert base.stat().st_uid == account.pw_uid
assert config.stat().st_uid == 0
assert config.stat().st_gid == account.pw_gid
assert stat.S_IMODE(config.stat().st_mode) == 0o640
helper = pathlib.Path('/usr/libexec/seriald/rescue.sh')
assert helper.stat().st_uid == 0 and not helper.stat().st_mode & 0o022
subprocess.run(['visudo', '-c'], check=True)
original = config.read_bytes()
config.write_bytes(original + b'\n# installation preservation fixture\n')
sentinel = base / 'preserve.log'
sentinel.write_text('runtime data must survive')
os.chown(sentinel, account.pw_uid, account.pw_gid)
os.chmod(sentinel, 0o660)
assert config.read_bytes() == original + b'\n# installation preservation fixture\n'
assert sentinel.read_text() == 'runtime data must survive'
assert sentinel.stat().st_gid == account.pw_gid
assert stat.S_IMODE(sentinel.stat().st_mode) == 0o660
print('PASS: installed ownership, sudoers, modified conffile and runtime preservation', flush=True)

empty = pathlib.Path('/tmp/no-serial-devices')
empty.mkdir()
socket_dir = pathlib.Path('/run/seriald')
socket_dir.mkdir(exist_ok=True)
os.chown(socket_dir, account.pw_uid, account.pw_gid)
os.chmod(socket_dir, 0o750)
environment = dict(os.environ, SERIALD_BASE=str(base), SERIALD_STATIC='/usr/lib/seriald',
                   SERIALD_SOCKET='/run/seriald/collector.sock', SERIALD_BY_PATH=str(empty),
                   SERIALD_AUTH_TOKEN='', SERIALD_BIND='127.0.0.1', SERIALD_PORT='18898',
                   HOME=account.pw_dir, PATH='/usr/sbin:/usr/bin:/sbin:/bin')
for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'SERIALD_MCP_PYTHON'):
    environment.pop(name, None)
terminal_probe = r"""
import sys
sys.path.insert(0, '/usr/lib/seriald')
from seriald_core.terminal_backend import SharedTerminal
t = SharedTerminal('ssh', 120, 32)
assert t.feed(b'\x1b[32766;32766H\x1b[6n', 21) == ['\x1b[32;120R'], t.metadata()
snapshot = t.snapshot()
u = SharedTerminal('ssh')
assert u.restore(snapshot), u.metadata()
assert u.metadata()['geometry'] == {'cols': 120, 'rows': 32}
t.close()
u.close()
print('PASS: installed canonical terminal parser, replies and snapshots')
"""
subprocess.run(['sudo', '-u', 'seriald', '--preserve-env', 'python3', '-c', terminal_probe],
               env=environment, check=True)
workers = []
os.umask(0o007)  # Match the packaged systemd units for newly created runtime files.
def start(program):
    process = subprocess.Popen(['sudo', '-u', 'seriald', '--preserve-env', program],
                               env=environment, start_new_session=True)
    workers.append(process)
    return process
def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
def ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen('http://127.0.0.1:18898/api/devices', timeout=1) as response:
                assert json.load(response)['devices'] == []
            return
        except OSError:
            time.sleep(.1)
    raise AssertionError('Installed control/collector API did not start')
try:
    collector = start('/usr/bin/seriald-collector')
    control = start('/usr/bin/seriald-control')
    ready()
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        first_pid = json.load(response)['pid']
    installed_id = pathlib.Path('/usr/lib/seriald/.collector-build-id').read_bytes()
    previous = pathlib.Path('/tmp/seriald-previous.deb').is_file()
    if previous:
        with urlopen('http://127.0.0.1:18898/api/access', timeout=3) as response:
            assert not json.load(response)['mcp_enabled'], 'Legacy thin fixture unexpectedly supplies MCP'
    stop(control)
    subprocess.run(['dpkg', '--force-confold', '-i', '/tmp/seriald.deb'], check=True)
    assert pathlib.Path('/usr/lib/seriald/.collector-build-id').read_bytes() == installed_id
    assert config.read_bytes() == original + b'\n# installation preservation fixture\n'
    assert sentinel.read_text() == 'runtime data must survive'
    control = start('/usr/bin/seriald-control')
    ready()
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        assert json.load(response)['pid'] == first_pid
    subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:18898'],
                   env=environment, check=True, timeout=30)
    worker_users = []
    for process in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            command = (process / 'cmdline').read_bytes().split(b'\0')[0]
            if command == b'/usr/lib/seriald/mcp-runtime/seriald-mcp-worker':
                worker_users.append((process / 'status').read_text().split('Uid:', 1)[1].split()[0])
        except (OSError, IndexError):
            pass
    assert worker_users and set(worker_users) == {str(account.pw_uid)}, worker_users
    stop(control)
    control = start('/usr/bin/seriald-control')
    ready()
    subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:18898'],
                   env=environment, check=True, timeout=30)
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        assert json.load(response)['pid'] == first_pid
    assert importlib.util.find_spec('mcp') is None
    if previous:
        print('PASS: published 1.7.0 thin package upgrades to included MCP without replacing collector', flush=True)
    assert collector.poll() is None
    subprocess.run(['/usr/bin/serialctl', '--url', 'http://127.0.0.1:18898', 'devices'], check=True)
    subprocess.run(['/usr/bin/seriald-admin', '--help'], check=True, stdout=subprocess.DEVNULL)
    print('PASS: network-disabled service-account API/CLI/MCP and independent control restart', flush=True)
finally:
    for process in reversed(workers):
        stop(process)
subprocess.run(['dpkg', '--purge', 'seriald'], check=True)
assert sentinel.read_text() == 'runtime data must survive'
assert (base / 'registry.sqlite3').is_file()
assert not pathlib.Path('/usr/bin/seriald-collector').exists()
assert not pathlib.Path('/usr/lib/seriald/mcp-runtime').exists()
print('PASS: package purge preserves runtime data and removes program files', flush=True)
print('NOT COVERED: systemd activation, real UART, privileged USB recovery', flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', choices=('ubuntu:22.04', 'ubuntu:24.04'), default='ubuntu:24.04')
    parser.add_argument('--runtime', type=Path, help='prepared native MCP runtime directory')
    parser.add_argument('--package', type=Path, help='test this already-built package instead of building')
    parser.add_argument('--upgrade-from', type=Path, help='published 1.7.0 thin package to install first')
    args = parser.parse_args()
    for path in (args.package, args.upgrade_from):
        if path is not None and not path.is_file():
            parser.error('package must be an existing regular file: ' + str(path))
    name = 'decklab-deb-check-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='decklab-deb-check-') as directory:
        package = args.package.resolve() if args.package else build(Path(directory), __version__, runtime=args.runtime)
        check = Path(directory) / 'check.py'
        check.write_text(CHECK, encoding='utf-8')
        created = False
        try:
            subprocess.run(['docker', 'create', '--name', name, args.image, 'sleep', 'infinity'], check=True)
            created = True
            subprocess.run(['docker', 'start', name], check=True)
            subprocess.run(['docker', 'cp', str(package), name + ':/tmp/seriald.deb'], check=True)
            subprocess.run(['docker', 'cp', str(check), name + ':/tmp/check.py'], check=True)
            subprocess.run(['docker', 'cp', str(Path(__file__).with_name('installed_mcp_check.py')),
                            name + ':/tmp/installed_mcp_check.py'], check=True)
            initial = '/tmp/seriald.deb'
            if args.upgrade_from:
                subprocess.run(['docker', 'cp', str(args.upgrade_from.resolve()),
                                name + ':/tmp/seriald-previous.deb'], check=True)
                initial = '/tmp/seriald-previous.deb'
            subprocess.run(['docker', 'exec', name, 'apt-get', 'update', '-qq'], check=True, timeout=300)
            subprocess.run(['docker', 'exec', '-e', 'DEBIAN_FRONTEND=noninteractive', name,
                            'apt-get', 'install', '-y', initial], check=True, timeout=300)
            subprocess.run(['docker', 'network', 'disconnect', 'bridge', name], check=True)
            subprocess.run(['docker', 'exec', name, 'python3', '/tmp/check.py'], check=True, timeout=180)
            subprocess.run(['docker', 'inspect', '--format', '{{.Image}}', name], check=True)
        finally:
            if created:
                subprocess.run(['docker', 'rm', '-f', name], check=True)


if __name__ == '__main__':
    main()
