"""Validate and copy an explicit RAM-loader build input into either host package."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zlib


def publish(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    manifest = source / 'manifest.json'
    raw = manifest.read_bytes()
    if len(raw) > 65536:
        raise ValueError('RAM loader manifest exceeds size limit')
    document = json.loads(raw)
    if document.get('version') != 1 or set(document.get('families', {})) - {'k1', 'k3'} or not document.get('families'):
        raise ValueError('invalid RAM loader families')
    removed_stream = False
    files = {'manifest.json'}
    def validate(family, material, protocols):
        if not isinstance(material, dict) or material.get('protocol') not in protocols:
            raise ValueError('unsupported RAM loader protocol')
        if 'usb2_uboot' in material and family != 'k3':
            raise ValueError('USB 2.0 download loader requires K3')
        for name in ('fsbl', 'uboot', 'fsbl_fallback', 'usb2_uboot'):
            if name in ('fsbl_fallback', 'usb2_uboot') and name not in material:
                continue  # optional; DeckLab K3 FSBLs since 1.17.41 train DDR themselves
            entry = material[name]
            extra = {'training_slots'} if family == 'k3' and name == 'fsbl' else set()
            if extra and 'training_slots' in entry:
                slots = entry['training_slots']
                if (not isinstance(slots, dict) or set(slots) != {'lpddr5', 'lpddr4x'}
                        or any(not isinstance(v, dict) or set(v) != {'offset', 'size'} or type(v['offset']) is not int
                               or v['offset'] < 0x1000 or v['size'] != 0x9800 for v in slots.values())):
                    raise ValueError('invalid K3 FSBL training slots')
            if (not isinstance(entry, dict) or not {'file', 'sha256'} <= set(entry) <= {'file', 'sha256'} | extra
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
    for family, material in document['families'].items():
        if family == 'k3' and isinstance(material, dict) and 'stream_uboot' in material:
            del material['stream_uboot']
            removed_stream = True
        validate(family, material, ('stock', 'spacemit-ram-v1'))
        if 'ram_boot' in material:
            alternate = material['ram_boot']
            if not isinstance(alternate, dict) or set(alternate) != {'protocol', 'fsbl', 'uboot'}:
                raise ValueError('invalid RAM boot loader manifest')
            validate(family, alternate, (f'spacemit-{family}-ramboot-v1',))
        if 'nor' in material:
            alternate = material['nor']
            if family != 'k1' or not isinstance(alternate, dict) or set(alternate) != {'protocol', 'fsbl', 'uboot'}:
                raise ValueError('invalid K1 NOR loader manifest')
            validate(family, alternate, ('spacemit-k1-nor-v1',))
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
        if name == 'manifest.json' and removed_stream:
            target.write_text(json.dumps(document, indent=2) + '\n')
        else:
            shutil.copyfile(source / name, target)
        target.chmod(0o644)
    return document


def archive_materials(archive, expected_sha256, names):
    with archive.open('rb') as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 ** 2), b''):
            digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise ValueError('Firmware archive checksum mismatch')
        source.seek(0)
        members = {}
        with tarfile.open(fileobj=source, mode='r|gz') as package:
            for entry in package:
                name = entry.name.removeprefix('./')
                if name not in names:
                    continue
                if name in members or not entry.isfile() or not 0 < entry.size <= 16 * 1024 ** 2:
                    raise ValueError('invalid Firmware loader member')
                with package.extractfile(entry) as member:
                    members[name] = member.read()
    if set(members) != names:
        raise ValueError('Firmware archive is missing loader materials')
    return members


def k1_materials(archive, expected_sha256):
    members = archive_materials(archive, expected_sha256,
                                {'factory/FSBL.bin', 'u-boot.itb', 'build-receipts/uboot_receipt.json'})
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


def k3_ram_materials(archive, expected_sha256):
    members = archive_materials(archive, expected_sha256, {'factory/FSBL.bin', 'u-boot.itb', 'fw_dynamic.itb'})
    with tempfile.TemporaryDirectory(prefix='decklab-k3-fit-') as directory:
        directory = Path(directory)
        uboot, opensbi, raw = (directory / name for name in ('u-boot.itb', 'fw_dynamic.itb', 'fw_dynamic.bin'))
        uboot.write_bytes(members['u-boot.itb'])
        opensbi.write_bytes(members['fw_dynamic.itb'])
        def fdt(fit, *args):
            return subprocess.check_output(['fdtget', *args[:2], str(fit), *args[2:]], text=True).split()
        images = subprocess.check_output(['fdtget', '-l', uboot, '/images'], text=True).split()
        configs = subprocess.check_output(['fdtget', '-l', uboot, '/configurations'], text=True).split()
        if 'opensbi' in images or 'uboot' not in images or not configs:
            raise ValueError('unexpected K3 vendor FIT layout')
        address = fdt(opensbi, '-t', 'x', '/images/opensbi', 'load')
        if (address != ['1', '0'] or fdt(opensbi, '-t', 's', '/images/opensbi', 'os') != ['opensbi']
                or fdt(opensbi, '-t', 's', '/images/opensbi', 'compression') != ['none']):
            raise ValueError('unexpected K3 OpenSBI image')
        subprocess.run(['dumpimage', '-T', 'flat_dt', '-p', '0', '-o', raw, opensbi],
                       check=True, stdout=subprocess.DEVNULL)
        sbi = raw.read_bytes()
        crc = zlib.crc32(sbi)
        if fdt(opensbi, '-t', 'x', '/images/opensbi/hash-1', 'value') != [f'{crc:x}']:
            raise ValueError('K3 OpenSBI FIT checksum mismatch')
        fragments = []
        for index, config in enumerate(configs, 1):
            if not all(c.isascii() and (c.isalnum() or c in '_-') for c in config):
                raise ValueError('invalid K3 FIT configuration name')
            if fdt(uboot, '-t', 's', f'/configurations/{config}', 'loadables') != ['uboot']:
                raise ValueError('unexpected K3 FIT loadables')
            fragments.append(f'fragment@{index} {{ target-path = "/configurations/{config}"; '
                             '__overlay__ { firmware = "opensbi"; }; };')
        overlay = directory / 'opensbi.dts'
        overlay.write_text('''/dts-v1/;
/plugin/;
/ {
    fragment@0 {
        target-path = "/images";
        __overlay__ {
            opensbi {
                description = "OpenSBI fw_dynamic Firmware";
                type = "firmware";
                os = "opensbi";
                arch = "riscv";
                compression = "none";
                load = <0x1 0x00000000>;
                entry = <0x1 0x00000000>;
                data = /incbin/("fw_dynamic.bin");
                hash-1 { algo = "crc32"; value = <0x%08x>; };
            };
        };
    };
    %s
};
''' % (crc, '\n    '.join(fragments)))
        compiled, fit = directory / 'opensbi.dtbo', directory / 'ram-boot.itb'
        subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', compiled, overlay], check=True)
        subprocess.run(['fdtoverlay', '-i', uboot, '-o', fit, compiled], check=True)
        combined = fit.read_bytes()
    inputs = {'archive': archive.name, 'archive_sha256': expected_sha256,
              'members': {name: hashlib.sha256(data).hexdigest() for name, data in members.items()},
              'opensbi_payload_sha256': hashlib.sha256(sbi).hexdigest()}
    material, files = {'protocol': 'spacemit-k3-ramboot-v1'}, {}
    for key, name, data in [('fsbl', 'FSBL.bin', members['factory/FSBL.bin']), ('uboot', 'u-boot.itb', combined)]:
        name = 'k3-ram-boot/' + name
        material[key] = {'file': name, 'sha256': hashlib.sha256(data).hexdigest()}
        files[name] = data
    return material, files, inputs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--k1-firmware', type=Path, help='K1 Firmware archive containing the temporary FSBL/U-Boot pair')
    parser.add_argument('--k1-sha256', help='expected K1 Firmware archive SHA-256')
    parser.add_argument('--k3-ram-firmware', type=Path, help='K3 Firmware archive for the vendor RAM boot pair')
    parser.add_argument('--k3-ram-sha256', help='expected K3 Firmware archive SHA-256')
    args = parser.parse_args()
    if bool(args.k1_firmware) != bool(args.k1_sha256):
        parser.error('--k1-firmware and --k1-sha256 must be supplied together')
    if bool(args.k3_ram_firmware) != bool(args.k3_ram_sha256):
        parser.error('--k3-ram-firmware and --k3-ram-sha256 must be supplied together')
    k1 = k1_materials(args.k1_firmware, args.k1_sha256) if args.k1_firmware else None
    k3_ram = k3_ram_materials(args.k3_ram_firmware, args.k3_ram_sha256) if args.k3_ram_firmware else None
    manifest = publish(args.source, args.destination)
    if k1:
        material, files, receipt = k1
        previous = manifest['families'].get('k1', {})
        for name in ('ram_boot', 'nor'):
            alternate = previous.get(name)
            if alternate:
                if {alternate[member]['file'] for member in ('fsbl', 'uboot')} & set(files):
                    raise ValueError('Alternate K1 loader files must not be replaced by firmware loaders')
                material[name] = alternate
        for name, data in files.items():
            target = args.destination / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
        (args.destination / 'k1/BUILD-INFO.txt').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        manifest['families']['k1'] = material
    if k3_ram:
        material, files, receipt = k3_ram
        if 'k3' not in manifest['families']:
            raise ValueError('K3 vendor RAM boot requires an existing K3 loader bundle')
        for name, data in files.items():
            target = args.destination / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
        (args.destination / 'k3-ram-boot/BUILD-INFO.txt').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        manifest['families']['k3']['ram_boot'] = material
    if k1 or k3_ram:
        (args.destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
