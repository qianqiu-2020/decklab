"""Install/upgrade/purge a Debian package in a disposable, hardware-free Ubuntu container.

No host mounts, devices, published ports or privileged mode. This tests real dpkg
scripts and installed entrypoints, not systemd activation or real serial adapters.
"""
import argparse
import re
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
def stop(process, clean=False):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    if clean:
        assert process.returncode == 0, 'candidate process did not stop cleanly'
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
def credentials(phase):
    subprocess.run(['sudo', '-u', 'seriald', '--preserve-env', 'python3',
                    '/tmp/installed_ssh_credentials_check.py', '--phase', phase,
                    '--database', str(base / 'ssh-credentials-fixture.sqlite3')],
                   env=environment, check=True, timeout=20)
try:
    collector = start('/usr/bin/seriald-collector')
    control = start('/usr/bin/seriald-control')
    ready()
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        first_pid = json.load(response)['pid']
    installed_id = pathlib.Path('/usr/lib/seriald/.collector-build-id').read_bytes()
    previous = pathlib.Path('/tmp/seriald-previous.deb').is_file()
    if previous:
        previous_version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'seriald'], text=True)
        previous_bundled_mcp = pathlib.Path('/usr/lib/seriald/mcp-runtime/seriald-mcp-worker').is_file()
        with urlopen('http://127.0.0.1:18898/api/access', timeout=3) as response:
            assert bool(json.load(response)['mcp_enabled']) == previous_bundled_mcp, 'previous installed MCP availability differs from package contents'
        if previous_bundled_mcp:
            subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:18898'],
                           env=environment, check=True, timeout=30)
    stop(control)
    subprocess.run(['dpkg', '--force-confold', '-i', '/tmp/seriald.deb'], check=True)
    collector_changed = pathlib.Path('/usr/lib/seriald/.collector-build-id').read_bytes() != installed_id
    restart_marker = pathlib.Path('/run/seriald-collector-restart-required')
    if collector_changed:
        assert previous, 'same-package reinstall changed its collector fingerprint'
        assert restart_marker.is_file(), 'changed collector code did not request a restart'
        # This container has no systemd. Restart only the isolated fixture here;
        # the VM acceptance checks the real maintainer-script service decision.
        stop(collector)
        collector = start('/usr/bin/seriald-collector')
        restart_marker.unlink()
    else:
        assert not restart_marker.exists(), 'unchanged collector code requested a restart'
    assert config.read_bytes() == original + b'\n# installation preservation fixture\n'
    assert sentinel.read_text() == 'runtime data must survive'
    control = start('/usr/bin/seriald-control')
    ready()
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        installed_pid = json.load(response)['pid']
    if collector_changed:
        assert installed_pid != first_pid
        print('PASS: changed collector fingerprint requests restart; isolated container fixture restarted (not systemd acceptance)', flush=True)
    else:
        assert installed_pid == first_pid
        print('PASS: unchanged collector fingerprint keeps the existing collector PID', flush=True)
    credentials('seed')
    # Seed only after the incoming package is active: the old thin upgrade
    # fixture predates remembered credentials. A second real reinstall proves
    # that the new private record survives package scripts and a fresh process.
    stop(control, clean=True)
    subprocess.run(['dpkg', '--force-confold', '-i', '/tmp/seriald.deb'], check=True)
    assert not restart_marker.exists(), 'same-code reinstall requested a collector restart'
    control = start('/usr/bin/seriald-control')
    ready()
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        assert json.load(response)['pid'] == installed_pid
    assert config.read_bytes() == original + b'\n# installation preservation fixture\n'
    assert sentinel.read_text() == 'runtime data must survive'
    credentials('check')
    subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:18898'],
                   env=environment, check=True, timeout=30)
    profile_path = pathlib.Path('/usr/lib/seriald/installation.json')
    if profile_path.is_file() and json.loads(profile_path.read_text()).get('profile') == 'complete':
        probe = r"""
import json, os, pathlib, sys
sys.path.insert(0, '/usr/lib/seriald')
from seriald_core.flash_discovery import executor_command
from seriald_core.flash_loaders import manifest_path
assert not os.environ.get('SERIALD_FLASH_EXECUTOR') and not os.environ.get('SERIALD_FLASH_LOADERS')
assert executor_command() == ('/usr/libexec/seriald/decklab-flash',)
assert manifest_path() == pathlib.Path('/usr/lib/seriald/flash-loaders/manifest.json')
assert 'k3' in json.loads(manifest_path().read_text())['families']
print('PASS: complete installation automatically discovers embedded flashing executor and RAM materials')
"""
        subprocess.run(['sudo', '-u', 'seriald', '--preserve-env', 'python3', '-c', probe], env=environment, check=True)
        access_config = pathlib.Path('/tmp/access-fixture.env')
        access_config.write_text('SERIALD_BASE=/data/seriald-data\nSERIALD_BIND=127.0.0.1\nSERIALD_PORT=18898\n')
        access = json.loads(subprocess.check_output(['/usr/bin/seriald-access', '--config', str(access_config), '--json'],env=environment,text=True))
        assert access['urls'] == ['http://127.0.0.1:18898/'], access
        with urlopen('http://127.0.0.1:18898/api/image-library', timeout=3) as response:
            assert json.load(response)['artifacts'] == []
        with urlopen('http://127.0.0.1:18898/static/image-store.js', timeout=3) as response:
            assert b'SerialdImageStore' in response.read()
        print('PASS: installed browser access links, image cache API and image-store static route', flush=True)
    worker_users = []
    for process in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            command = (process / 'cmdline').read_bytes().split(b'\0')[0]
            if command == b'/usr/lib/seriald/mcp-runtime/seriald-mcp-worker':
                worker_users.append((process / 'status').read_text().split('Uid:', 1)[1].split()[0])
        except (OSError, IndexError):
            pass
    assert worker_users and set(worker_users) == {str(account.pw_uid)}, worker_users
    stop(control, clean=True)
    control = start('/usr/bin/seriald-control')
    ready()
    subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:18898'],
                   env=environment, check=True, timeout=30)
    with urlopen('http://127.0.0.1:18898/api/health', timeout=3) as response:
        assert json.load(response)['pid'] == installed_pid
    credentials('check')
    assert importlib.util.find_spec('mcp') is None
    if previous:
        print('PASS: published %s package upgrades with bundled MCP, configuration and runtime data preserved' % previous_version, flush=True)
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
credentials('purged')
registry = base / 'registry.sqlite3'
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
# Reproduce retained legacy permissions after checking that purge kept private
# data intact. The fixture DB remains owner-only; old registries/snapshots must
# be tightened by postinst itself, before any Store has a chance to open them.
os.chmod(registry, 0o660)
for snapshot in (base / 'backups').glob('*.sqlite3'):
    os.chmod(snapshot, 0o660)
subprocess.run(['dpkg', '--force-confold', '-i', '/tmp/seriald.deb'], check=True)
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
credentials('check')
assert stat.S_IMODE(sentinel.stat().st_mode) == 0o660
assert sentinel.read_text() == 'runtime data must survive'
subprocess.run(['dpkg', '--purge', 'seriald'], check=True)
assert not pathlib.Path('/usr/bin/seriald-collector').exists()
assert not pathlib.Path('/usr/lib/seriald/mcp-runtime').exists()
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
credentials('purged')
print('PASS: purge/fresh reinstall preserves private credentials and snapshots, ordinary log permissions and runtime data', flush=True)
print('NOT COVERED: systemd activation, real UART, privileged USB recovery', flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', choices=('ubuntu:22.04', 'ubuntu:24.04'), default='ubuntu:24.04')
    parser.add_argument('--runtime', type=Path, help='prepared native MCP runtime directory')
    parser.add_argument('--package', type=Path, help='test this already-built package instead of building')
    parser.add_argument('--upgrade-from', type=Path, help='published prior package to install first (thin or bundled MCP)')
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
            subprocess.run(['docker', 'cp', str(Path(__file__).with_name('installed_ssh_credentials_check.py')),
                            name + ':/tmp/installed_ssh_credentials_check.py'], check=True)
            initial = '/tmp/seriald.deb'
            if args.upgrade_from:
                subprocess.run(['docker', 'cp', str(args.upgrade_from.resolve()),
                                name + ':/tmp/seriald-previous.deb'], check=True)
                initial = '/tmp/seriald-previous.deb'
            subprocess.run(['docker', 'exec', name, 'apt-get', 'update', '-qq'], check=True, timeout=300)
            subprocess.run(['docker', 'exec', '-e', 'DEBIAN_FRONTEND=noninteractive', name,
                            'apt-get', 'install', '-y', initial], check=True, timeout=300)
            if args.upgrade_from:
                # Resolve new system dependencies while retaining the old
                # application. Only DeckLab installation/runtime are accepted
                # offline; apt's ordinary OS dependency resolution is online.
                depends = subprocess.check_output(['dpkg-deb', '--field', str(package), 'Depends'], text=True)
                packages = [re.match(r'[a-z0-9][a-z0-9+.-]*', item.strip()).group(0)
                            for item in depends.split(',') if item.strip()]
                subprocess.run(['docker', 'exec', '-e', 'DEBIAN_FRONTEND=noninteractive', name,
                                'apt-get', 'install', '-y', *packages], check=True, timeout=300)
            subprocess.run(['docker', 'network', 'disconnect', 'bridge', name], check=True)
            subprocess.run(['docker', 'exec', name, 'python3', '/tmp/check.py'], check=True, timeout=180)
            subprocess.run(['docker', 'inspect', '--format', '{{.Image}}', name], check=True)
        finally:
            if created:
                subprocess.run(['docker', 'rm', '-f', name], check=True)


if __name__ == '__main__':
    main()
