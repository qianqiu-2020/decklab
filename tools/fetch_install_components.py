"""Fetch a reviewed build-input ZIP, verify its pinned hash, then extract offline.

Used by release builders, never during end-user installation. URLs and hashes
come from release configuration; all package inputs still need manifest validation.
"""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.install_components import load


def extract(archive, destination, platform):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('component destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.components-') as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if not members or len(members) > 4096 or sum(i.file_size for i in members) > 512 * 1024 ** 2:
                raise ValueError('component archive exceeds bounds')
            names = set()
            for entry in members:
                rel = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if (not entry.filename or rel.is_absolute() or '..' in rel.parts or '\\' in entry.filename
                        or ':' in entry.filename or entry.filename in names or
                        stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise ValueError('unsafe component archive entry')
                names.add(entry.filename)
                target = root.joinpath(*rel.parts)
                if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as stream, target.open('xb') as output:
                        shutil.copyfileobj(stream, output, 1024 * 1024)
        load(root / 'manifest.json', platform)
        root.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=('linux-amd64','linux-arm64','windows-amd64'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    url, expected = os.environ.get('DECKLAB_COMPONENTS_URL',''), os.environ.get('DECKLAB_COMPONENTS_SHA256','')
    if urlsplit(url).scheme != 'https' or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        parser.error('Complete release requires DECKLAB_COMPONENTS_URL (HTTPS) and reviewed DECKLAB_COMPONENTS_SHA256')
    with tempfile.TemporaryFile() as archive:
        digest = hashlib.sha256(); count = 0
        with urlopen(url, timeout=30) as stream:
            if urlsplit(stream.geturl()).scheme != 'https': raise ValueError('component download requires HTTPS')
            while True:
                data = stream.read(1024 * 1024)
                if not data: break
                count += len(data)
                if count > 512 * 1024 ** 2: raise ValueError('component download exceeds bounds')
                digest.update(data); archive.write(data)
        if digest.hexdigest() != expected: raise ValueError('component archive checksum mismatch')
        archive.seek(0)
        extract(archive, args.output or Path('build/install-components')/args.platform, args.platform)
    print('Complete installation inputs verified for ' + args.platform)


if __name__ == '__main__': main()
