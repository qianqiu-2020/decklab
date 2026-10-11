"""Offline inventory/integrity check of the complete Windows distribution tree.

Hashes detect stale or modified build files; this is not a signature or license SBOM.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

NAME = 'bundle-manifest.json'


def files(root):
    root = Path(root)
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise ValueError('bundle root must not be redirected')
    if not root.is_dir():
        raise ValueError('bundle directory is missing')
    if os.path.lexists(root / 'data'):
        raise ValueError('bundle contains runtime data')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('bundle contains a redirected path')
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('bundle contains a nonregular file')
        relative = path.relative_to(root).as_posix()
        if relative == NAME:
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        result[relative] = {'size': size, 'sha256': digest.hexdigest(),
                            'kind': 'native' if path.suffix.lower() in ('.exe', '.dll', '.pyd') else 'resource'}
    return result


def create(root):
    root = Path(root)
    entries = files(root)
    if not entries:
        raise ValueError('bundle is empty')
    manifest = {'schema': 1, 'scope': 'complete-distribution-files', 'files': entries}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.manifest-',
                                         dir=root, delete=False) as source:
            temporary = Path(source.name)
            json.dump(manifest, source, ensure_ascii=False, indent=2)
            source.write('\n')
        temporary.replace(root / NAME)
        return manifest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify(root):
    root = Path(root)
    actual = files(root)
    manifest = json.loads((root / NAME).read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('scope') != 'complete-distribution-files'
            or not isinstance(manifest.get('files'), dict) or not manifest['files']):
        raise ValueError('invalid bundle manifest')
    expected = manifest['files']
    if set(expected) != set(actual):
        raise ValueError('bundle file set changed; rebuild before packaging')
    for name, entry in actual.items():
        if expected[name] != entry:
            raise ValueError('bundle file content changed: ' + name)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    result = verify(args.bundle) if args.verify else create(args.bundle)
    print('Bundle %s: %d files' % ('verified' if args.verify else 'inventoried', len(result['files'])))
