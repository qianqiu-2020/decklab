"""Real systemd/package lifecycle in a disposable Ubuntu cloud-image VM.

Requires qemu-system-x86_64, qemu-img, genisoimage, ssh/scp and a verified cloud
image. Uses a private overlay, temporary SSH keys and loopback-only forwarding;
no physical devices or host directory sharing. Never installs on the host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

from build_deb import build, collector_build_id, __version__


CHECK = r'''
import importlib.util, os, pathlib, pwd, stat, subprocess, time
from urllib.request import urlopen
import json

def command(*args):
    return subprocess.check_output(args, text=True)
def ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen('http://127.0.0.1:8898/api/health', timeout=2) as response:
                value = json.load(response)
                assert value['devices'] == 0
                return value['pid']
        except OSError:
            time.sleep(.1)
    raise AssertionError('collector/control not healthy')
def install(path):
    result = command('dpkg', '--force-confold', '-i', path)
    print(result, flush=True)
    return result
def credentials(phase):
    subprocess.run(['sudo', '-u', 'seriald', 'python3', '/tmp/installed_ssh_credentials_check.py',
                    '--phase', phase, '--database', str(base / 'ssh-credentials-fixture.sqlite3')],
                   check=True, timeout=20)

subprocess.run(['apt-get', 'update', '-qq'], check=True)
previous = pathlib.Path('/tmp/seriald-previous.deb').is_file()
initial = '/tmp/seriald-previous.deb' if previous else '/tmp/seriald.deb'
subprocess.run(['apt-get', 'install', '-y', initial], check=True)
# Keep the connected SSH path on the local virtual subnet, but remove Internet
# routing before MCP acceptance and upgrades. No pip or SDK is installed here.
for family in ('-4', '-6'):
    if command('ip', family, 'route', 'show', 'default').strip():
        subprocess.run(['ip', family, 'route', 'del', 'default'], check=True)
    assert not command('ip', family, 'route', 'show', 'default').strip()
assert importlib.util.find_spec('mcp') is None
assert not os.environ.get('SERIALD_MCP_PYTHON')
first = ready()
fingerprint = pathlib.Path('/usr/lib/seriald/.collector-build-id')
installed_id = fingerprint.read_bytes()
base = pathlib.Path('/data/seriald-data')
marker = base / 'vm-preserve.log'
marker.write_text('preserve runtime across service upgrades')
os.chmod(marker, 0o660)
config = pathlib.Path('/etc/seriald/seriald.env')
saved = config.read_bytes() + b'\n# VM conffile preservation\n'
config.write_bytes(saved)
if previous:
    with urlopen('http://127.0.0.1:8898/api/access', timeout=3) as response:
        assert not json.load(response)['mcp_enabled']
    upgrade = install('/tmp/seriald.deb')
    collector_changed = fingerprint.read_bytes() != installed_id
    upgraded = ready()
    if collector_changed:
        assert 'collector restarted (collector code changed)' in upgrade
        assert upgraded != first
        print('PASS: real systemd upgrade restarts collector when its fingerprint changes', flush=True)
    else:
        assert 'collector kept running (collector code unchanged)' in upgrade
        assert upgraded == first
        print('PASS: real systemd upgrade retains collector when its fingerprint is unchanged', flush=True)
    assert config.read_bytes() == saved
    assert marker.read_text() == 'preserve runtime across service upgrades'
    assert not pathlib.Path('/run/seriald-collector-restart-required').exists()
    first = upgraded
    print('PASS: published 1.7.0 thin package upgrade preserves configuration and runtime data', flush=True)
subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:8898'],
               check=True, timeout=30)
account = pwd.getpwnam('seriald')
control_pid = command('systemctl', 'show', '-p', 'MainPID', '--value', 'seriald.service').strip()
assert pathlib.Path('/proc/' + control_pid).stat().st_uid == account.pw_uid
for unit in ('seriald.service', 'seriald-collector.service'):
    assert command('systemctl', 'is-active', unit).strip() == 'active'
subprocess.run(['serialctl', 'devices'], check=True)
credentials('seed')
unchanged = install('/tmp/seriald.deb')
assert 'collector kept running (collector code unchanged)' in unchanged
assert ready() == first
credentials('check')
subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:8898'],
               check=True, timeout=30)
subprocess.run(['systemctl', 'restart', 'seriald.service'], check=True)
assert ready() == first
credentials('check')
subprocess.run(['python3', '/tmp/installed_mcp_check.py', '--url', 'http://127.0.0.1:8898'],
               check=True, timeout=30)
changed = install('/tmp/seriald-changed.deb')
assert 'collector restarted (collector code changed)' in changed
second = ready()
assert second != first
assert config.read_bytes() == saved
assert marker.read_text() == 'preserve runtime across service upgrades'
assert not pathlib.Path('/run/seriald-collector-restart-required').exists()
credentials('check')
reinstall = install('/tmp/seriald-changed.deb')
assert 'collector kept running (collector code unchanged)' in reinstall
assert ready() == second
credentials('check')
print('PASS: real systemd startup, unchanged/changed collector decisions and conffile/data preservation', flush=True)
subprocess.run(['dpkg', '--purge', 'seriald'], check=True)
for unit in ('seriald.service', 'seriald-collector.service'):
    assert subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0
assert marker.is_file() and (base / 'registry.sqlite3').is_file()
credentials('purged')
registry = base / 'registry.sqlite3'
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
os.chmod(registry, 0o660)
for snapshot in (base / 'backups').glob('*.sqlite3'):
    os.chmod(snapshot, 0o660)
install('/tmp/seriald.deb')
ready()
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
credentials('check')
assert marker.read_text() == 'preserve runtime across service upgrades'
assert stat.S_IMODE(marker.stat().st_mode) == 0o660
subprocess.run(['dpkg', '--purge', 'seriald'], check=True)
for unit in ('seriald.service', 'seriald-collector.service'):
    assert subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
credentials('purged')
print('PASS: purge/fresh reinstall preserves private credentials and snapshots, ordinary log permissions and runtime data', flush=True)
print('NOT COVERED: physical UART/USB recovery or Windows behavior', flush=True)
'''


def changed_package(package, root):
    stage = root / 'changed-package'
    subprocess.run(['dpkg-deb', '-R', str(package), str(stage)], check=True)
    code = stage / 'usr/lib/seriald'
    original_id = (code / '.collector-build-id').read_text().strip()
    with (code / 'seriald_core/device.py').open('a') as target:
        target.write('\n# Isolated VM fixture: exercise a collector-code upgrade.\n')
    policy = code / 'packaging'
    policy.mkdir()
    shutil.copyfile(stage / 'lib/systemd/system/seriald-collector.service', policy / 'seriald-collector.service')
    new_id = collector_build_id(code)
    shutil.rmtree(policy)
    (code / '.collector-build-id').write_text(new_id + '\n')
    preinst = stage / 'DEBIAN/preinst'
    preinst_text = preinst.read_text()
    if original_id not in preinst_text or original_id == new_id:
        raise ValueError('collector fixture does not match the extracted package fingerprint')
    preinst.write_text(preinst_text.replace(original_id, new_id))
    control = stage / 'DEBIAN/control'
    control_text = control.read_text()
    version = next(line.split(': ', 1)[1] for line in control_text.splitlines()
                   if line.startswith('Version: '))
    control.write_text(control_text.replace('Version: ' + version + '\n',
                                           'Version: ' + version + '+vmcheck1\n'))
    result = root / 'seriald-changed.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(result)], check=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--sha256', required=True, help='expected hash from the cloud image provider')
    parser.add_argument('--accel', choices=('kvm', 'tcg'), default='kvm')
    parser.add_argument('--runtime', type=Path, help='prepared native MCP runtime directory')
    parser.add_argument('--package', type=Path, help='test this already-built package instead of building')
    parser.add_argument('--upgrade-from', type=Path, help='published 1.7.0 thin package to install first')
    args = parser.parse_args()
    for package_path in (args.package, args.upgrade_from):
        if package_path is not None and not package_path.is_file():
            parser.error('package must be an existing regular file: ' + str(package_path))
    image = args.image.resolve()
    if not image.is_file():
        parser.error('image must be a regular cloud image file')
    digest = hashlib.sha256()
    with image.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    expected = args.sha256.lower()
    if digest.hexdigest() != expected:
        parser.error('cloud image checksum mismatch; no VM started')
    print('Cloud image checksum verified:', expected, flush=True)
    info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(image)]))
    if info['format'] not in ('qcow2', 'raw'):
        parser.error('unsupported backing image format')
    with tempfile.TemporaryDirectory(prefix='decklab-systemd-vm-') as directory:
        root = Path(directory)
        package = args.package.resolve() if args.package else build(root, __version__, runtime=args.runtime)
        changed = changed_package(package, root)
        key = root / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        (root / 'meta-data').write_text('instance-id: decklab-' + uuid.uuid4().hex + '\nlocal-hostname: decklab-check\n')
        (root / 'user-data').write_text('#cloud-config\nusers:\n  - name: decklab\n'
            '    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    ssh_authorized_keys:\n'
            '      - ' + key.with_suffix('.pub').read_text().strip() + '\n'
            'write_files:\n  - path: /etc/systemd/system/seriald-collector.service.d/check.conf\n'
            '    content: |\n      [Service]\n      Environment=SERIALD_BY_PATH=/tmp/no-serial-devices\n'
            'runcmd:\n  - [mkdir, -p, /tmp/no-serial-devices]\n')
        subprocess.run(['genisoimage', '-quiet', '-output', str(root / 'seed.iso'), '-volid', 'cidata',
                        '-joliet', '-rock', str(root / 'user-data'), str(root / 'meta-data')], check=True)
        disk = root / 'disk.qcow2'
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', info['format'], '-b', str(image), str(disk)], check=True)
        subprocess.run(['qemu-img', 'resize', str(disk), '8G'], check=True)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        ssh = ['ssh', '-F', '/dev/null', '-i', str(key), '-p', str(port), '-o', 'BatchMode=yes',
               '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
               '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(root / 'known_hosts'),
               '-o', 'ConnectTimeout=2', 'decklab@127.0.0.1']
        with (root / 'boot.log').open('wb') as log:
            vm = subprocess.Popen(['qemu-system-x86_64', '-accel', args.accel, '-m', '1536', '-smp', '2',
                '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
                '-drive', 'file=' + str(disk) + ',if=virtio,format=qcow2',
                '-drive', 'file=' + str(root / 'seed.iso') + ',media=cdrom,readonly=on',
                '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:' + str(port) + '-:22',
                '-device', 'virtio-net-pci,netdev=net0'], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 180
                while True:
                    if vm.poll() is not None:
                        raise RuntimeError('VM exited: ' + (root / 'boot.log').read_text(errors='replace')[-2000:])
                    if subprocess.run(ssh + ['true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('VM SSH timeout: ' + (root / 'boot.log').read_text(errors='replace')[-2000:])
                    time.sleep(.5)
                subprocess.run(ssh + ['cloud-init status --wait'], check=True, timeout=180)
                check = root / 'check.py'
                check.write_text(CHECK)
                scp = ['scp', '-F', '/dev/null', '-i', str(key), '-P', str(port), '-o', 'BatchMode=yes',
                       '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
                       '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(root / 'known_hosts')]
                files = [(package, 'seriald.deb'), (changed, 'seriald-changed.deb'), (check, 'check.py'),
                         (Path(__file__).with_name('installed_mcp_check.py'), 'installed_mcp_check.py'),
                         (Path(__file__).with_name('installed_ssh_credentials_check.py'),
                          'installed_ssh_credentials_check.py')]
                if args.upgrade_from:
                    files.append((args.upgrade_from.resolve(), 'seriald-previous.deb'))
                for source, destination in files:
                    subprocess.run(scp + [str(source), 'decklab@127.0.0.1:/tmp/' + destination], check=True)
                subprocess.run(ssh + ['sudo env DEBIAN_FRONTEND=noninteractive python3 /tmp/check.py'], check=True, timeout=600)
                print('Tested cloud image SHA-256:', expected)
            finally:
                vm.terminate()
                try:
                    vm.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vm.kill()
                    vm.wait(timeout=5)


if __name__ == '__main__':
    main()
