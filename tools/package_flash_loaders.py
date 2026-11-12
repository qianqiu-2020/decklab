"""Validate and copy an explicit RAM-loader build input into either host package."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def publish(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    manifest = source / 'manifest.json'
    raw = manifest.read_bytes()
    if len(raw) > 65536:
        raise ValueError('RAM loader manifest exceeds size limit')
    document = json.loads(raw)
    if document.get('version') != 1 or set(document.get('families', {})) - {'k1', 'k3'} or not document.get('families'):
        raise ValueError('invalid RAM loader families')
    files = {'manifest.json'}
    for family, material in document['families'].items():
        if material.get('protocol') not in ('stock', 'decklab-nor-v1'):
            raise ValueError('unsupported RAM loader protocol')
        for name in ('fsbl', 'uboot'):
            entry = material[name]
            if (not isinstance(entry, dict) or set(entry) != {'file', 'sha256'}
                    or not isinstance(entry['file'], str) or not entry['file']
                    or entry['file'].startswith('/') or '\\' in entry['file'] or ':' in entry['file']
                    or '..' in entry['file'].split('/')):
                raise ValueError('RAM loader file must be a relative bundle member')
            path = source / entry['file']
            if (not path.is_file() or path.is_symlink() or source not in path.resolve().parents
                    or not 0 < path.stat().st_size <= 16 * 1024 ** 2
                    or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
                raise ValueError('RAM loader checksum, type, path or size mismatch')
            files.add(entry['file'])
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('RAM loader bundle must not contain symbolic links')
        if path.is_file() and path.suffix in ('.txt', '.patch', '.md'):
            if path.stat().st_size > 1024 ** 2:
                raise ValueError('RAM loader build notice exceeds size limit')
            files.add(path.relative_to(source).as_posix())
    if not {'BUILD-INFO.txt', 'COPYING.txt'} <= files:
        raise ValueError('RAM loader build provenance and license are required')
    for name in sorted(files):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        target.chmod(0o644)
    return document


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    publish(args.source, args.destination)
