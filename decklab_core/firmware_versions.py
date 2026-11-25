"""Read component versions from the same digest-bound receipts used for CAB builds."""
import json
import re
from pathlib import Path

from .tasks import TaskError


def describe_firmware(result, files):
    from .image_tables import table_bytes
    if 'firmware-bundle.json' not in files:
        return
    root = Path(result['root_path'])

    def record(name):
        if name not in files:
            return {}
        value = json.loads(table_bytes(root, files[name]))
        if not isinstance(value, dict):
            raise TaskError('固件版本清单格式错误', 409)
        return value

    bundle = record('firmware-bundle.json')
    if bundle.get('scope') not in ('k1_nor_firmware_flash_bundle', 'k3_nor_firmware_flash_bundle'):
        return
    rows = []
    def component(name, member, receipt, version, digest_key='sha256'):
        file = files.get(member)
        if not file:
            return
        matched = (receipt.get(digest_key) == file['sha256'] and receipt.get('bytes') == file['size']
                   and type(version) is int and 0 < version <= 0xffffffff)
        rows.append(dict(name=name, member=member, sha256=file['sha256'], version=version if matched else None))

    ub = record('build-receipts/uboot_receipt.json')
    entries = ub.get('components') or {}
    boot = entries.get('bootinfo') or {}
    component('BootInfo', 'factory/bootinfo_spinor.bin', boot, boot.get('release_version'))
    component('FSBL', 'factory/FSBL.bin', entries.get('fsbl') or {}, ub.get('spl_release_version'))
    for name, member in [('OpenSBI', 'fw_dynamic.itb'), ('ESOS', 'esos.itb')]:
        receipt = record('build-receipts/' + name.lower() + '_receipt.json')
        component(name, member, receipt, receipt.get('release_version'))
    ed = record('build-receipts/edk2_receipt.json')
    component('EDK2', 'edk2.itb', ed, ed.get('edk2_numeric_version'), 'fit_sha256')
    if bundle.get('ec_included') or 'ec-release.json' in files:
        release = record('ec-release.json')
        member = 'ec-k3-pico-itx.bin' if 'upstream_archive' in release else 'ec.bin'
        image = release.get('image') or {}
        ec = files.get(member) or {}
        version = release.get('version')
        if (bundle.get('scope') != 'k3_nor_firmware_flash_bundle' or bundle.get('ec_included') is not True
                or release.get('scope') != 'csce2010_ec_firmware_release' or release.get('schema_version') != 1
                or release.get('board') != 'spacemit,k3-pico-itx' or release.get('controller') != 'csce2010'
                or release.get('guid') != 'd1b9c82f-97c4-4b1c-8085-c193fea3c813'
                or type(version) is not int or not 0 < version <= 0xffffffff
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}', str(release.get('version_text') or ''))
                or {'ec.bin','ec-k3-pico-itx.bin'}.intersection(files) != {member}
                or image.get('file') != 'ec.bin' or not ec or image.get('sha256') != ec['sha256']
                or image.get('bytes') != ec['size'] or not 64 <= ec['size'] <= 0x3b400 or ec['size'] % 4):
            raise TaskError('EC 发布记录与固件或适用板型不一致，请核对镜像包', 409)
        result['ec'] = dict(member=member, sha256=ec['sha256'], size=ec['size'], version=version,
                            version_text=release['version_text'])
        component('EC', member, image, version)
    from .firmware_cab_catalog import CAB_COMPONENTS
    family = bundle['scope'].split('_')[0]
    for row in rows:
        match = CAB_COMPONENTS.get((family, row['name'], row['sha256']))
        if match and row['version'] in (None, match[0]):
            row.update(version=match[0], cab_digests=list(match[1]))
    result['firmware_versions'] = rows
