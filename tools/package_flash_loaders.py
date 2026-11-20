"""Validate and copy an explicit RAM-loader build input into either host package."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile


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
        if material.get('protocol') not in ('stock', 'spacemit-ram-v1'):
            raise ValueError('unsupported RAM loader protocol')
        for name in ('fsbl', 'uboot', 'fsbl_fallback'):
            if name == 'fsbl_fallback' and name not in material:
                continue  # optional: full-training FSBL after a quick-boot failure
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


def k1_materials(archive, expected_sha256):
    with archive.open('rb') as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 ** 2), b''):
            digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise ValueError('K1 Firmware archive checksum mismatch')
        source.seek(0)
        names = {'factory/FSBL.bin', 'u-boot.itb', 'build-receipts/uboot_receipt.json'}
        members = {}
        with tarfile.open(fileobj=source, mode='r|gz') as package:
            for entry in package:
                name = entry.name.removeprefix('./')
                if name not in names:
                    continue
                if name in members or not entry.isfile() or not 0 < entry.size <= 16 * 1024 ** 2:
                    raise ValueError('invalid K1 Firmware loader member')
                with package.extractfile(entry) as member:
                    members[name] = member.read()
    if set(members) != names:
        raise ValueError('K1 Firmware archive lacks its loader pair or U-Boot receipt')
    receipt = json.loads(members.pop('build-receipts/uboot_receipt.json'))
    if receipt.get('board') != 'k1':
        raise ValueError('K1 Firmware build receipt does not identify K1')
    material, files = {'protocol': 'stock'}, {}
    for kind, name in [('fsbl', 'factory/FSBL.bin'), ('uboot', 'u-boot.itb')]:
        data = members[name]
        sha = hashlib.sha256(data).hexdigest()
        recorded = receipt['components'][kind]
        if recorded['sha256'] != sha or recorded['bytes'] != len(data):
            raise ValueError('K1 Firmware loader differs from build receipt')
        relative = 'k1/' + name.rsplit('/', 1)[-1]
        material[kind] = {'file': relative, 'sha256': sha}
        files[relative] = data
    receipt.update(archive=archive.name, archive_sha256=expected_sha256)
    return material, files, receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--k1-firmware', type=Path, help='K1 Firmware archive containing the temporary FSBL/U-Boot pair')
    parser.add_argument('--k1-sha256', help='expected K1 Firmware archive SHA-256')
    args = parser.parse_args()
    if bool(args.k1_firmware) != bool(args.k1_sha256):
        parser.error('--k1-firmware and --k1-sha256 must be supplied together')
    k1 = k1_materials(args.k1_firmware, args.k1_sha256) if args.k1_firmware else None
    manifest = publish(args.source, args.destination)
    if k1:
        material, files, receipt = k1
        for name, data in files.items():
            target = args.destination / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
        (args.destination / 'k1/BUILD-INFO.txt').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        manifest['families']['k1'] = material
        (args.destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
