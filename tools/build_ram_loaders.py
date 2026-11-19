#!/usr/bin/env python3
"""Build a private K3 RAM-only download environment, without accessing a board.

Input FSBL is checksum-pinned and reused solely for RAM staging. U-Boot is built
from an archived source revision plus the patches in packaging/flash-loaders/patches
(applied in name order and recorded with their digests), using native Fastboot/UUU
commands and the added unzstd and bgblk commands.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PATCHES = sorted((ROOT / 'packaging/flash-loaders/patches').glob('0*.patch'))
BASE_COMMIT = 'd9bce64dfc'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--commit', default=BASE_COMMIT)
    parser.add_argument('--cross-compile', required=True)
    parser.add_argument('--fsbl', type=Path, required=True)
    parser.add_argument('--fsbl-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--opensbi', type=Path, help='OpenSBI fw_dynamic.bin started by SPL before U-Boot')
    parser.add_argument('--opensbi-sha256')
    args = parser.parse_args()
    if args.opensbi and digest(args.opensbi) != args.opensbi_sha256:
        parser.error('OpenSBI checksum mismatch')
    if digest(args.fsbl) != args.fsbl_sha256 or not 0 < args.fsbl.stat().st_size <= 475136:
        parser.error('FSBL checksum or BootROM size limit mismatch')
    if args.output.exists():
        parser.error('output directory already exists; use a new versioned build directory')
    commit = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', args.commit], text=True).strip()
    args.output.mkdir(parents=True)
    (args.output / 'BUILD-INPUT.json').write_text(json.dumps({
        'uboot_commit': commit, 'source_patches': [{'file': p.name, 'sha256': digest(p)} for p in PATCHES],
        'default_env_sha256': digest(ROOT / 'packaging/flash-loaders/k3-ram.env'),
        'fsbl_sha256': args.fsbl_sha256, 'cross_compile': args.cross_compile,
        'configuration': 'k3_defconfig', 'ram_only': True,
        'ddr_training_persistence': False, 'target': 'u-boot.bin', 'jobs': args.jobs,
        'opensbi_sha256': args.opensbi_sha256 if args.opensbi else None
    }, indent=2) + '\n')
    try:
        with tempfile.TemporaryDirectory(prefix='decklab-k3-ram-') as directory:
            work = Path(directory)
            source, build, fit = work / 'source', work / 'build', work / 'fit'
            source.mkdir()
            with (work / 'source.tar').open('wb') as output:
                subprocess.run(['git', '-C', str(args.source), 'archive', commit], stdout=output, check=True)
            with tarfile.open(work / 'source.tar') as archive:
                archive.extractall(source)
            for patch in PATCHES:
                subprocess.run(['patch', '-p1', '--forward', '--batch', '-i', str(patch)], cwd=source, check=True,
                               stdout=subprocess.DEVNULL)
            with (args.output / 'build.log.txt').open('w') as log:
                command = ['make', 'O=' + str(build), 'CROSS_COMPILE=' + args.cross_compile]
                subprocess.run(command + ['k3_defconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                # Native Kconfig only. Skipping board_late_init avoids EEPROM
                # provisioning, DDR persistence, boot scripts and auto-flashing.
                configure = [str(source / 'scripts/config'), '--file', str(build / '.config')]
                for symbol in ('BOARD_LATE_INIT', 'ENV_IS_IN_MMC', 'ENV_IS_IN_UFS',
                               'ENV_IS_IN_MTD', 'ESPI', 'RT7451_RETIMER',
                               'HWMON_SENSORS_CTF2301', 'AUTOBOOT_KEYED',
                               'CROS_EC', 'CROS_EC_ESPI', 'I2C_CROS_EC_TUNNEL',
                               'PWM_CROS_EC', 'CROS_EC_KEYB'):
                    configure += ['-d', symbol]
                # PCI_INIT_R enumerates PCIe once after relocation. Without it
                # every `nvme` command re-runs pci_init() (~1.1 s on K3 boards
                # with absent PCIe links), which dominates block writes.
                for symbol in ('ENV_IS_NOWHERE', 'CMD_NVME', 'CMD_SCSI', 'USE_DEFAULT_ENV_FILE', 'PCI_INIT_R', 'CMD_UNZSTD', 'ZSTD',
                               'CC_OPTIMIZE_LIBS_FOR_SPEED'):
                    configure += ['-e', symbol]
                configure += ['--set-str', 'DEFAULT_ENV_FILE', str(ROOT / 'packaging/flash-loaders/k3-ram.env'),
                              '--set-str', 'LOCALVERSION', '-seriald-ram',
                              '--set-val', 'BOOTDELAY', '-2']
                subprocess.run(configure, check=True)
                subprocess.run(command + ['olddefconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                configuration = (build / '.config').read_text()
                (args.output / 'CONFIGURATION.txt').write_text(configuration)
                for expected in ('# CONFIG_BOARD_LATE_INIT is not set',
                                 '# CONFIG_ENV_IS_IN_MMC is not set',
                                 '# CONFIG_ENV_IS_IN_UFS is not set',
                                 '# CONFIG_ENV_IS_IN_MTD is not set',
                                 'CONFIG_ENV_IS_NOWHERE=y', 'CONFIG_USE_DEFAULT_ENV_FILE=y',
                                 'CONFIG_FASTBOOT_UUU_SUPPORT=y',
                                 'CONFIG_BOOTDELAY=-2', 'CONFIG_PCI_INIT_R=y',
                                 'CONFIG_CMD_UNZSTD=y', 'CONFIG_ZSTD=y',
):
                    if expected not in configuration:
                        raise RuntimeError('required stock RAM configuration missing: ' + expected)
                subprocess.run(command + ['-j%d' % args.jobs, 'u-boot.bin'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                elf = (build / 'u-boot').read_bytes()
                if b'bootcmd=while true; do fastboot 0; done\x00' not in elf or b'bootcmd=run autoboot' in elf:
                    raise RuntimeError('compiled default environment is not Fastboot-only')
                shutil.copyfile(build / 'u-boot', args.output / 'u-boot.elf')
                symbols = subprocess.check_output([args.cross_compile + 'nm', '--defined-only',
                    str(build / 'u-boot')], text=True)
                if any(name in symbols for name in ('board_late_init', 'save_ddr_training_info',
                        'set_dev_serial_no', 'fastboot_decklab_')):
                    raise RuntimeError('unexpected provisioning/custom loader symbol in stock RAM build')
                (args.output / 'SAFETY-CHECK.txt').write_text(
                    'Source archive plus ' + (', '.join(p.name for p in PATCHES) or 'no patches') + '.\n'
                    'Native Kconfig disables board late-init and persistent env.\n'
                    'Compiled symbols exclude board late-init, DDR persistence and serial provisioning.\n')

                (fit / 'configs').mkdir(parents=True)
                (fit / 'dtb').mkdir()
                its = (source / 'board/spacemit/k3/configs/uboot_fdt.its').read_text()
                if args.opensbi:
                    # In USB download mode SPL otherwise jumps to U-Boot in
                    # M-mode, without SBI. As in normal boot, start OpenSBI
                    # first; U-Boot then runs in S-mode with HSM and SRST.
                    shutil.copyfile(args.opensbi, fit / 'fw_dynamic.bin')
                    node = ('\t\topensbi {\n\t\t\tdescription = "OpenSBI fw_dynamic Firmware";\n'
                            '\t\t\ttype = "firmware";\n\t\t\tos = "opensbi";\n\t\t\tarch = "riscv";\n'
                            '\t\t\tcompression = "none";\n\t\t\tload = <0x1 0x00000000>;\n'
                            '\t\t\tentry = <0x1 0x00000000>;\n\t\t\tdata = /incbin/("../fw_dynamic.bin");\n'
                            '\t\t\thash-1 {\n\t\t\t\talgo = "crc32";\n\t\t\t};\n\t\t};\n\n')
                    its, count = re.subn(r'(\timages \{\n)', lambda m: m.group(1) + node, its, count=1)
                    its, confs = re.subn(r'(\t\t\tloadables = "uboot";)', '\t\t\tfirmware = "opensbi";\n\\1', its)
                    if count != 1 or confs == 0 or 'entry' not in node:
                        raise RuntimeError('cannot add OpenSBI to the U-Boot FIT')
                (fit / 'configs/uboot_fdt.its').write_text(its)
                def lzo(incoming, outgoing):
                    with outgoing.open('wb') as output:
                        subprocess.run(['lzop', '-9', '-c', str(incoming)], stdout=output, stderr=log, check=True)
                lzo(build / 'u-boot-nodtb.bin', fit / 'u-boot-nodtb.bin.lzo')
                for path in (build / 'arch/riscv/dts').glob('*.dtb'):
                    lzo(path, fit / 'dtb' / (path.name + '.lzo'))
                (args.output / 'k3').mkdir()
                subprocess.run([str(build / 'tools/mkimage'), '-f', str(fit / 'configs/uboot_fdt.its'),
                    str(args.output / 'k3/u-boot.itb')], check=True, stdout=log, stderr=subprocess.STDOUT)
            shutil.copyfile(args.fsbl, args.output / 'k3/FSBL.bin')
            shutil.copyfile(ROOT / 'packaging/flash-loaders/k3-ram.env', args.output / 'RAM-ENV.txt')
            shutil.copyfile(source / 'Licenses/gpl-2.0.txt', args.output / 'COPYING.txt')
            materials = {'protocol': 'spacemit-ram-v1'}
            for kind, name in (('fsbl', 'k3/FSBL.bin'), ('uboot', 'k3/u-boot.itb')):
                materials[kind] = {'file': name, 'sha256': digest(args.output / name)}
            (args.output / 'manifest.json').write_text(json.dumps({'version': 1, 'families': {'k3': materials}}, indent=2) + '\n')
            (args.output / 'BUILD-INFO.txt').write_text('DeckLab K3 RAM-only download environment\n' +
                'U-Boot source commit: ' + commit + '\nSource patches: ' + (', '.join(p.name + ' ' + digest(p) for p in PATCHES) or 'none') + '\n' +
                'FSBL SHA-256: ' + args.fsbl_sha256 + '\nToolchain prefix: ' + args.cross_compile + '\n' +
                'Build: k3_defconfig + native RAM configuration, u-boot.bin, compressed multi-DTB FIT\n' +
                ('OpenSBI fw_dynamic SHA-256: ' + args.opensbi_sha256 + ' (started by SPL; U-Boot runs in S-mode)\n'
                 if args.opensbi else '') +
                'DDR training policy: decided by the staged FSBL; board_late_init disabled, so nothing is saved.\n' +
                'Loaders are RAM-only, never partition payloads. NOR map selection does not save env or write storage.\n' +
                'EEPROM: native commands; host restricts TLV 0x83 and verifies physical readback.\n' +
                'Compiled/offline checked; no physical-board acceptance implied.\n')
        print(args.output)
    except BaseException:
        print('Build log and input fingerprints retained in: ' + str(args.output))
        raise


if __name__ == '__main__':
    main()
