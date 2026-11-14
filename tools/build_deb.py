#!/usr/bin/env python3
"""Build a native Ubuntu package from a prepared MCP runtime, entirely offline."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seriald_core import __version__
from tools.package_docs import publish as publish_docs
from tools.package_flash_loaders import publish as publish_flash_loaders
from tools.build_linux_mcp import MACHINES, validate_runtime


def collector_build_id(root=ROOT):
    """Hash code and service policy that require replacing the live collector."""
    excluded = {'admin.py', 'control_server.py', 'mcp_host.py', 'version.py'}
    sources = [path for path in (root / 'seriald_core').glob('*.py')
               if path.name not in excluded]
    sources.append(root / 'packaging/seriald-collector.service')
    for source in (root / 'seriald_core/terminal_worker.js', root / 'seriald_core/terminal_snapshot.js',
                   root / 'static/serial-progress.js'):
        if source.is_file():
            sources.append(source)
    sources.extend((root / 'vendor/terminal').glob('*.js'))
    digest = hashlib.sha256()
    for path in sorted(sources):
        relative = path.relative_to(root).as_posix().encode('utf-8')
        digest.update(relative + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def flash_executor_metadata(path, architecture):
    """Check an explicit local binary without running it or selecting hardware."""
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('flash executor must be a regular non-symlink file')
    with path.open('rb') as source:
        header = source.read(20)
        if (len(header) != 20 or header[:4] != b'\x7fELF' or header[4:6] != b'\x02\x01'
                or struct.unpack('<H', header[18:20])[0] != MACHINES[architecture][1]):
            raise ValueError('flash executor ELF architecture differs from the package runtime')
        digest = hashlib.sha256(header)
        size = len(header)
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {'schema': 1, 'architecture': architecture,
            'installed_path': '/usr/libexec/seriald/decklab-flash',
            'sha256': digest.hexdigest(), 'size': size,
            'origin': 'explicit-local-build-input'}


def flash_notice_files(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('flash notices require a regular non-symlink directory')
    files, total = {}, 0
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('flash notices must not contain links or special files')
        if path.is_file():
            total += path.stat().st_size
            if len(files) >= 128 or total > 16 * 1024 * 1024:
                raise ValueError('flash notices exceed the size or file limit')
            files[path.relative_to(directory).as_posix()] = {
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}
    if not files:
        raise ValueError('flash notices are empty')
    return files


def build(output, version, runtime=None, flash_executor=None, flash_notices=None, flash_loaders=None,
          complete=False, components_manifest=None):
    if runtime is None:
        architecture = {'x86_64': 'amd64', 'aarch64': 'arm64',
                        'riscv64': 'riscv64'}.get(platform.machine())
        runtime = ROOT / 'build/linux-mcp' / (architecture or platform.machine()) / 'runtime'
    runtime = Path(runtime)
    if not runtime.is_dir():
        raise ValueError('Prepared MCP runtime is required. Run tools/build_linux_mcp.py '
                         '--arch <amd64|arm64> first, then pass --runtime PATH.')
    descriptor = validate_runtime(runtime)
    architecture = descriptor['architecture']
    if complete:
        from tools.install_components import load, default_manifest
        explicit = any(value is not None for value in (flash_executor, flash_notices, flash_loaders))
        if components_manifest is not None and explicit:
            raise ValueError('component manifest cannot be combined with individual flashing inputs')
        if explicit and not all(value is not None for value in (flash_executor, flash_notices, flash_loaders)):
            raise ValueError('complete standalone installation requires executor, notices and RAM materials together')
        if components_manifest is not None or flash_executor is None:
            components = load(components_manifest or default_manifest('linux-' + architecture), 'linux-' + architecture)
            flash_executor, flash_notices, flash_loaders = (components[name] for name in ('executor', 'notices', 'loaders'))
        if not all(value is not None for value in (flash_executor, flash_notices, flash_loaders)):
            raise ValueError('complete standalone installation requires executor, notices and RAM materials')
    executor_metadata = flash_executor_metadata(flash_executor, architecture) if flash_executor is not None else None
    if flash_notices is not None and executor_metadata is None:
        raise ValueError('flash notices require an explicit flash executor')
    notices = flash_notice_files(flash_notices) if flash_notices is not None else None
    output.mkdir(parents=True, exist_ok=True)
    artifact = output.resolve() / ('seriald_%s_%s.deb' % (version, architecture))
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
        build_id = collector_build_id()
        for name in ('seriald.py', 'seriald_demo.py', 'serialctl.py', 'seriald_mcp.py',
                     'requirements-mcp.txt', 'pyproject.toml', 'index.html'):
            put(name, 'usr/lib/seriald/' + name)
        write('usr/lib/seriald/.collector-build-id', build_id + '\n')
        for name in ('seriald_core', 'static', 'vendor'):
            for path in (ROOT / name).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    put(path.relative_to(ROOT), 'usr/lib/seriald/' + str(path.relative_to(ROOT)))
        for path in sorted(runtime.rglob('*')):
            if path.is_file():
                destination = stage / 'usr/lib/seriald/mcp-runtime' / path.relative_to(runtime)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                destination.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        for name in ('SKILL.md', 'SKILL.zh-CN.md'):
            put('skills/seriald-board/' + name, 'usr/lib/seriald/skills/seriald-board/' + name)
        for command, invocation in [('serialctl', '/usr/lib/seriald/serialctl.py'),
                                    ('seriald-access', '-m seriald_core.access_info'),
                                    ('seriald-admin', '-m seriald_core.admin'),
                                    ('seriald-collector', '-m seriald_core.collector_server'),
                                    ('seriald-control', '-m seriald_core.control_server'),
                                    ('seriald-backend', '-m seriald_core.backend_server')]:
            write('usr/bin/' + command, '#!/bin/sh\nexport PYTHONPATH=/usr/lib/seriald\nexec /usr/bin/python3 ' + invocation + ' "$@"\n', 0o755)
        put('rescue.sh', 'usr/libexec/seriald/rescue.sh', 0o755)
        put('packaging/seriald.service', 'lib/systemd/system/seriald.service')
        put('packaging/seriald-collector.service', 'lib/systemd/system/seriald-collector.service')
        put('packaging/seriald-web.service', 'lib/systemd/system/seriald-web.service')
        put('packaging/seriald-backend.service', 'lib/systemd/system/seriald-backend.service')
        put('packaging/seriald.env', 'etc/seriald/seriald.env', 0o640)
        write('etc/sudoers.d/seriald-rescue', 'seriald ALL=(root) NOPASSWD: /usr/libexec/seriald/rescue.sh\n', 0o440)
        for path in (ROOT / 'skills/seriald-board').rglob('*'):
            if path.is_file():
                text = path.read_text().replace('/data/seriald/serialctl.py', '/usr/lib/seriald/serialctl.py').replace('/data/seriald/docs/AGENT_ACCESS.md', '/usr/share/doc/seriald/AGENT_ACCESS.md')
                write('usr/share/seriald/skills/seriald-board/' + str(path.relative_to(ROOT / 'skills/seriald-board')), text)
        publish_docs(stage / 'usr/share/doc/seriald', 'linux')
        write('usr/lib/seriald/installation.json', json.dumps({
            'profile': 'complete' if complete else 'core', 'version': version,
            'flash_executor': '/usr/libexec/seriald/decklab-flash' if flash_executor else '',
            'ram_loaders': '/usr/lib/seriald/flash-loaders/manifest.json' if flash_loaders else ''}, indent=2) + '\n')
        if flash_loaders is not None:
            publish_flash_loaders(flash_loaders, stage / 'usr/lib/seriald/flash-loaders')
        if executor_metadata is not None:
            put('packaging/70-seriald-fastboot.rules', 'lib/udev/rules.d/70-seriald-fastboot.rules')
            target = stage / executor_metadata['installed_path'].lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(flash_executor, target)
            target.chmod(0o755)
            if flash_executor_metadata(target, architecture) != executor_metadata:
                raise ValueError('flash executor changed while preparing the package')
            if notices is not None:
                notice_target = stage / 'usr/share/doc/seriald/flash-third-party'
                for name in notices:
                    put(Path(flash_notices) / name, 'usr/share/doc/seriald/flash-third-party/' + name)
                if flash_notice_files(notice_target) != notices:
                    raise ValueError('flash notices changed while preparing the package')
                executor_metadata['notices'] = notices
            write('usr/share/doc/seriald/flash-executor.json',
                  json.dumps(executor_metadata, sort_keys=True, indent=2) + '\n')
            example = stage / 'usr/share/doc/seriald/examples/flash.json'
            configuration = json.loads(example.read_text())
            for board in configuration['boards'].values():
                board['flash']['argv'] = [executor_metadata['installed_path']]
            example.write_text(json.dumps(configuration, indent=2) + '\n')
        preinst = (ROOT / 'packaging/preinst').read_text().replace('@COLLECTOR_BUILD_ID@', build_id)
        write('DEBIAN/preinst', preinst, 0o755)
        for name in ('postinst', 'prerm', 'postrm'):
            put('packaging/' + name, 'DEBIAN/' + name, 0o755)
        write('DEBIAN/conffiles', '/etc/seriald/seriald.env\n/etc/sudoers.d/seriald-rescue\n')
        flash_dependency = ', libusb-1.0-0, udev' if executor_metadata is not None else ''
        write('DEBIAN/control', 'Package: seriald\nVersion: %s\nSection: admin\nPriority: optional\nArchitecture: %s\nMaintainer: seriald contributors <root@localhost>\nDepends: libc6 (>= 2.35), python3 (>= 3.10), nodejs (>= 12), openssh-client, sshpass, sudo, adduser, init-system-helpers%s\nDescription: Persistent serial board registry and shared agent console\n Serial capture, web terminals, agent operation history and JSON CLI.\n Includes a private Python runtime and MCP server dependencies.\n' % (version, architecture, flash_dependency))
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(artifact)], check=True)
    return artifact


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dists',
                        help='package output directory (default: dists/)')
    parser.add_argument('--version', default=__version__)
    parser.add_argument('--runtime', type=Path, help='prepared architecture-specific MCP runtime')
    parser.add_argument('--components-manifest', type=Path, help='hash-pinned standalone installation input manifest')
    parser.add_argument('--core-only', action='store_true', help='explicit compatibility/CI build without integrated flashing; not a complete standalone installer')
    parser.add_argument('--flash-executor', type=Path,
                        help='optional local decklab-flash ELF to include for private testing')
    parser.add_argument('--flash-notices', type=Path,
                        help='license/build notices accompanying the optional local executor')
    parser.add_argument('--flash-loaders', type=Path, help='prebuilt RAM-only FSBL/U-Boot bundle')
    args = parser.parse_args()
    if args.core_only and any((args.components_manifest, args.flash_executor, args.flash_notices, args.flash_loaders)):
        parser.error('--core-only cannot be combined with flashing components')
    print(build(args.output, args.version, runtime=args.runtime, flash_executor=args.flash_executor,
                flash_notices=args.flash_notices, flash_loaders=args.flash_loaders,
                complete=not args.core_only, components_manifest=args.components_manifest))
