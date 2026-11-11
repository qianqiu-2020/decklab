#!/usr/bin/env python3
"""Build the private, self-contained MCP worker on the oldest supported Linux.

The default command uses an Ubuntu 22.04 Docker build environment. Native CI
runners can install the hash-locked requirements and pass --local. Neither the
Debian package builder nor package installation downloads Python dependencies.
"""
import argparse
import hashlib
import http.client
from importlib import metadata, util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / 'packaging/linux-mcp-requirements.lock'
WHEELS = ROOT / 'packaging/linux-mcp-wheels.json'
MACHINES = {'amd64': ('x86_64', 62), 'arm64': ('aarch64', 183),
            'riscv64': ('riscv64', 243)}
SOURCES = ('seriald_mcp.py', 'serialctl.py', 'tools/linux_mcp_entrypoint.py')
EXECUTABLE = 'seriald-mcp-worker'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative_path(value):
    path = PurePosixPath(value)
    if (not value or value == '.' or path.is_absolute() or '..' in path.parts or '\\' in value
            or ':' in value or path.as_posix() != value):
        raise ValueError('invalid runtime path: ' + value)
    return path


def elf_machine(path):
    with Path(path).open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b'\x7fELF' or header[4:6] != b'\x02\x01':
        raise ValueError('worker must be a little-endian 64-bit ELF executable')
    return struct.unpack('<H', header[18:20])[0]


def runtime_files(directory):
    rows = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_symlink():
            raise ValueError('runtime symlinks are not permitted: ' + str(path))
        relative = path.relative_to(directory).as_posix()
        if path.is_file() and relative != 'runtime-manifest.json':
            rows[relative] = {
                'sha256': sha256(path), 'size': path.stat().st_size}
    return rows


def notice_module():
    spec = util.spec_from_file_location('linux_mcp_notices', ROOT / 'tools/collect_linux_mcp_notices.py')
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_runtime(directory, architecture=None):
    """Validate a prepared runtime offline before placing it in a Debian package."""
    directory = Path(directory)
    manifest_path = directory / 'runtime-manifest.json'
    if directory.is_symlink() or manifest_path.is_symlink():
        raise ValueError('runtime and manifest must not be symlinks')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    arch = manifest.get('architecture')
    if (manifest.get('schema') != 1 or arch not in MACHINES
            or (architecture is not None and arch != architecture)):
        raise ValueError('runtime architecture or schema mismatch')
    expected_machine = MACHINES[arch][1]
    baseline = manifest.get('build_platform', {})
    if (baseline.get('os'), baseline.get('version'), baseline.get('glibc')) != ('ubuntu', '22.04', '2.35'):
        raise ValueError('runtime must use the Ubuntu 22.04 glibc 2.35 build baseline')
    if baseline.get('machine') != MACHINES[arch][0]:
        raise ValueError('runtime build machine mismatch')
    if (manifest.get('mcp_version') != '2.3.0'
            or not str(manifest.get('python_version', '')).startswith('3.10.')
            or manifest.get('elf_machine') != expected_machine
            or manifest.get('executable') != EXECUTABLE):
        raise ValueError('runtime Python, MCP version or executable mismatch')
    if manifest.get('requirements_sha256') != sha256(LOCK):
        raise ValueError('runtime requirements differ from the reviewed hash lock')
    sources = {name: sha256(ROOT / name) for name in SOURCES}
    if manifest.get('source_files') != sources:
        raise ValueError('runtime adapter source differs from this checkout; rebuild the worker')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('runtime file inventory is missing')
    for name, row in files.items():
        relative_path(name)
        if (not isinstance(row, dict) or not isinstance(row.get('size'), int)
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', '')))):
            raise ValueError('invalid runtime file inventory')
    if files != runtime_files(directory):
        raise ValueError('runtime files are missing, unexpected, or have changed checksums')
    executable = directory / EXECUTABLE
    if not executable.stat().st_mode & stat.S_IXUSR or elf_machine(executable) != expected_machine:
        raise ValueError('runtime executable permission or ELF architecture mismatch')
    for name in files:
        path = directory / name
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic == b'\x7fELF' and elf_machine(path) != expected_machine:
            raise ValueError('native library architecture mismatch: ' + name)
    if manifest.get('notices') != 'third-party/inventory.json':
        raise ValueError('runtime notices inventory is missing')
    notice_module().verify(directory / 'third-party', bundle=directory)
    return manifest


def check_local(architecture):
    machine, _ = MACHINES[architecture]
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            release[key] = value.strip('"')
    if (sys.platform != 'linux' or platform.machine() != machine
            or release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '22.04'
            or platform.libc_ver() != ('glibc', '2.35')
            or sys.version_info[:2] != (3, 10)):
        raise ValueError('freeze on native Ubuntu 22.04 / Python 3.10 / glibc 2.35; use the default Docker build')
    for name, version in re.findall(r'^([A-Za-z0-9_.-]+)==([^\s]+)', LOCK.read_text(), re.M):
        if metadata.version(name) != version:
            raise ValueError('build environment differs from the hash lock: ' + name)


def flatten_symlinks(directory):
    # PyInstaller sometimes deduplicates .so aliases with relative symlinks.
    # Shipping ordinary files makes manifest verification and package extraction
    # independent of symlink handling while preserving each library name.
    for path in sorted(Path(directory).rglob('*')):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(Path(directory).resolve()) or not target.is_file():
                raise ValueError('unexpected PyInstaller symlink: ' + str(path))
            data, mode = target.read_bytes(), target.stat().st_mode
            path.unlink()
            path.write_bytes(data)
            path.chmod(stat.S_IMODE(mode))


def smoke_worker(bundle):
    """Exercise the frozen SDK server without opening ports on a real board."""
    with tempfile.TemporaryDirectory(prefix='mcp-smoke-') as temporary:
        ready = Path(temporary) / 'ready.json'
        error_log = Path(temporary) / 'stderr.log'
        with error_log.open('wb') as errors:
            child = subprocess.Popen(
                [str(Path(bundle) / EXECUTABLE), '--url', 'http://127.0.0.1:1',
                 '--transport', 'streamable-http', '--host', '127.0.0.1', '--port', '0',
                 '--ready-file', str(ready), '--parent-pid', str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise ValueError('frozen MCP worker exited: ' + error_log.read_text(errors='replace')[-4000:])
                    if ready.exists():
                        address = json.loads(ready.read_text())
                        connection = http.client.HTTPConnection('127.0.0.1', address['port'], timeout=1)
                        try:
                            connection.request('POST', '/mcp', body=b'{}', headers={'Content-Type': 'application/json'})
                            response = connection.getresponse()
                            response.read()
                            if response.status != 401:
                                raise ValueError('frozen MCP endpoint must reject unauthenticated access')
                            return
                        except (OSError, http.client.HTTPException):
                            pass  # Ready file precedes ASGI lifespan startup.
                        finally:
                            connection.close()
                    time.sleep(.05)
                raise ValueError('frozen MCP SDK server did not become ready: ' + error_log.read_text(errors='replace')[-4000:])
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)


def build_local(output, architecture, wheelhouse):
    check_local(architecture)
    output, wheelhouse = Path(output).resolve(), Path(wheelhouse).resolve()
    if output.exists():
        raise ValueError('runtime output already exists; choose a fresh output directory')
    if not wheelhouse.is_dir():
        raise ValueError('a hash-checked build wheelhouse is required')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.linux-mcp-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                   '--name', EXECUTABLE, '--distpath', str(temporary / 'dist'),
                   '--workpath', str(temporary / 'work'), '--specpath', str(temporary / 'spec'),
                   '--paths', str(ROOT), '--hidden-import', 'serialctl',
                   '--hidden-import', 'seriald_mcp']
        for module in ('mcp.server', 'mcp.shared', 'mcp.client'):
            command += ['--collect-submodules', module]
        command += ['--collect-data', 'mcp', '--collect-data', 'mcp_types',
                    '--collect-all', 'uvicorn', '--recursive-copy-metadata', 'mcp',
                    str(ROOT / 'tools/linux_mcp_entrypoint.py')]
        subprocess.run(command, cwd=temporary, check=True)
        bundle = temporary / 'dist' / EXECUTABLE
        flatten_symlinks(bundle)
        notice_module().collect(bundle, bundle / 'third-party', wheelhouse, LOCK)
        manifest = {'schema': 1, 'architecture': architecture,
                    'elf_machine': MACHINES[architecture][1], 'executable': EXECUTABLE,
                    'python_version': platform.python_version(), 'mcp_version': metadata.version('mcp'),
                    'build_platform': {'os': 'ubuntu', 'version': '22.04', 'glibc': '2.35',
                                       'machine': platform.machine()},
                    'requirements_sha256': sha256(LOCK),
                    'source_files': {name: sha256(ROOT / name) for name in SOURCES},
                    'notices': 'third-party/inventory.json', 'files': runtime_files(bundle)}
        (bundle / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        validate_runtime(bundle, architecture)
        smoke_worker(bundle)
        bundle.rename(output)
    return output


def build_docker(output, architecture, image):
    if architecture not in ('amd64', 'arm64'):
        raise ValueError('riscv64 requires a native --local build and a validated PyInstaller bootloader')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('runtime output already exists; choose a fresh output directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    shell = '\n'.join([
        'set -eu',
        'export DEBIAN_FRONTEND=noninteractive',
        'apt-get update',
        'apt-get install -y --no-install-recommends python3 python3-venv python3-dev binutils ca-certificates',
        'python3 -m venv /tmp/mcp-build',
        '/tmp/mcp-build/bin/python -m pip download --disable-pip-version-check --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r /source/packaging/linux-mcp-requirements.lock -d /tmp/wheelhouse',
        '/tmp/mcp-build/bin/python -m pip install --disable-pip-version-check --no-index --find-links /tmp/wheelhouse --only-binary=:all: --require-hashes -r /source/packaging/linux-mcp-requirements.lock',
        '/tmp/mcp-build/bin/python /source/tools/build_linux_mcp.py --local --arch "$MCP_ARCH" --wheelhouse /tmp/wheelhouse --output "$MCP_OUTPUT"',
        'chown -R "$MCP_UID:$MCP_GID" "$MCP_OUTPUT"'])
    subprocess.run(['docker', 'run', '--rm', '--platform', 'linux/' + architecture,
                    '-v', str(ROOT) + ':/source:ro', '-v', str(output.parent) + ':/output',
                    '-e', 'MCP_ARCH=' + architecture, '-e', 'MCP_OUTPUT=/output/' + output.name,
                    '-e', 'MCP_UID=' + str(os.getuid()), '-e', 'MCP_GID=' + str(os.getgid()),
                    image, 'bash', '-c', shell], check=True)
    return validate_runtime(output, architecture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=tuple(MACHINES), default={'x86_64': 'amd64', 'aarch64': 'arm64', 'riscv64': 'riscv64'}.get(platform.machine(), 'amd64'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--local', action='store_true', help='freeze in a prepared native Ubuntu 22.04 Python 3.10 build environment')
    parser.add_argument('--wheelhouse', type=Path, help='hash-locked wheels used to prepare the local environment')
    parser.add_argument('--image', default='ubuntu:22.04')
    parser.add_argument('--verify', action='store_true', help='only verify the existing runtime offline')
    args = parser.parse_args()
    output = args.output or ROOT / 'build/linux-mcp' / args.arch / 'runtime'
    try:
        if args.verify:
            validate_runtime(output, args.arch)
        elif args.local:
            if args.wheelhouse is None:
                parser.error('--local requires --wheelhouse')
            build_local(output, args.arch, args.wheelhouse)
        else:
            build_docker(output, args.arch, args.image)
    except (OSError, ValueError, subprocess.CalledProcessError, metadata.PackageNotFoundError) as error:
        parser.exit(1, 'Linux MCP runtime: %s\n' % error)
    print(output)


if __name__ == '__main__':
    main()
