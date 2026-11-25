"""Create a clean packaging interpreter without touching development environments."""
import argparse
import hashlib
from pathlib import Path
import sysconfig
import sys
import venv


def prepare(root, local_fast=False, paramiko_source=None):
    root = Path(root).resolve()
    build = root / 'build'
    environment = build / 'windows-venv'
    # Do not follow a user-created junction/symlink when clearing the build env.
    for path in (build, environment):
        if path.exists() and path.resolve() != path.absolute():
            raise ValueError('Build environment must not be a symlink or junction: %s' % path)
        if path.is_symlink():
            raise ValueError('Build environment must not be a symlink: %s' % path)
    if local_fast:
        # Only the packaging script marks dependencies ready after both pip steps succeed.
        digest = hashlib.sha256()
        for value in (sys.executable, sys.version, sysconfig.get_platform(),
                      str(root), 'paramiko-source' if paramiko_source else 'standard'):
            digest.update(value.encode() + b'\0')
        for path in sorted(root.glob('requirements-*.txt')) + [Path(__file__), root / 'tools/build_windows.ps1']:
            digest.update(path.name.encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        if paramiko_source:
            digest.update(hashlib.sha256(Path(paramiko_source).read_bytes()).digest())
        key = digest.hexdigest()
        stamp = environment / 'decklab-dependencies.sha256'
        if (stamp.is_file() and stamp.read_text().strip() == key
                and (environment / 'Scripts/pyinstaller.exe').is_file()
                and (environment / 'Scripts/python.exe').is_file()):
            return 'cached'
    venv.EnvBuilder(clear=True, with_pip=True, system_site_packages=False).create(environment)
    return key if local_fast else environment


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, nargs='?', default=Path(__file__).resolve().parents[1])
    parser.add_argument('--local-fast', action='store_true')
    parser.add_argument('--paramiko-source', type=Path)
    args = parser.parse_args()
    print(prepare(args.root, args.local_fast, args.paramiko_source))
