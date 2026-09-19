#!/usr/bin/env python3
"""Build an architecture-independent Ubuntu package without root or network access."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seriald_core import __version__


def build(output, version):
    output.mkdir(parents=True, exist_ok=True)
    artifact = output.resolve() / ('seriald_%s_all.deb' % version)
    with tempfile.TemporaryDirectory(prefix='seriald-deb-') as folder:
        stage = Path(folder)
        def put(source, target, mode=0o644):
            destination = stage / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, destination)
            destination.chmod(mode)
        def write(target, text, mode=0o644):
            destination = stage / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
            destination.chmod(mode)
        for name in ('seriald.py', 'serialctl.py', 'index.html'):
            put(name, 'usr/lib/seriald/' + name)
        for name in ('seriald_core', 'static', 'vendor'):
            for path in (ROOT / name).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    put(path.relative_to(ROOT), 'usr/lib/seriald/' + str(path.relative_to(ROOT)))
        for command, invocation in [('serialctl', '/usr/lib/seriald/serialctl.py'),
                                    ('seriald-admin', '-m seriald_core.admin'),
                                    ('seriald-collector', '-m seriald_core.collector_server'),
                                    ('seriald-control', '-m seriald_core.control_server')]:
            write('usr/bin/' + command, '#!/bin/sh\nexport PYTHONPATH=/usr/lib/seriald\nexec /usr/bin/python3 ' + invocation + ' "$@"\n', 0o755)
        put('rescue.sh', 'usr/libexec/seriald/rescue.sh', 0o755)
        put('packaging/seriald.service', 'lib/systemd/system/seriald.service')
        put('packaging/seriald-collector.service', 'lib/systemd/system/seriald-collector.service')
        put('packaging/seriald.env', 'etc/seriald/seriald.env', 0o640)
        write('etc/sudoers.d/seriald-rescue', 'seriald ALL=(root) NOPASSWD: /usr/libexec/seriald/rescue.sh\n', 0o440)
        for path in (ROOT / 'skills/seriald-board').rglob('*'):
            if path.is_file():
                text = path.read_text().replace('/data/seriald/serialctl.py', '/usr/lib/seriald/serialctl.py').replace('/data/seriald/docs/AGENT_ACCESS.md', '/usr/share/doc/seriald/AGENT_ACCESS.md')
                write('usr/share/seriald/skills/seriald-board/' + str(path.relative_to(ROOT / 'skills/seriald-board')), text)
        for name in ('README.md', 'docs/AGENT_ACCESS.md', 'docs/ARCHITECTURE.md', 'docs/INSTALL.md',
                     'docs/HARDWARE_ID.md', 'docs/DEVICE_IDENTITY_MATRIX.md',
                     'docs/REFACTOR_PLAN.md'):
            put(name, 'usr/share/doc/seriald/' + Path(name).name)
        for name in ('postinst', 'prerm', 'postrm'):
            put('packaging/' + name, 'DEBIAN/' + name, 0o755)
        write('DEBIAN/conffiles', '/etc/seriald/seriald.env\n/etc/sudoers.d/seriald-rescue\n')
        write('DEBIAN/control', 'Package: seriald\nVersion: %s\nSection: admin\nPriority: optional\nArchitecture: all\nMaintainer: seriald contributors <root@localhost>\nDepends: python3 (>= 3.10), openssh-client, sshpass, sudo, adduser, init-system-helpers\nDescription: Persistent serial board registry and shared agent console\n Serial capture, web terminals, agent operation history and JSON CLI.\n' % version)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(artifact)], check=True)
    return artifact


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--version', default=__version__)
    args = parser.parse_args()
    print(build(args.output, args.version))
