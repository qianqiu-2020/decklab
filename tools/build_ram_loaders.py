#!/usr/bin/env python3
"""Build a private K3 RAM-only download environment, without accessing a board.

The supplied U-Boot commit owns the volatile download profile. FSBL and
OpenSBI are checksum-pinned vendor inputs, reused solely for RAM staging.
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


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--commit', required=True, help='U-Boot revision containing the DeckLab download profile')
    parser.add_argument('--cross-compile', required=True)
    parser.add_argument('--fsbl', type=Path, required=True)
    parser.add_argument('--fsbl-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--opensbi', type=Path, required=True, help='OpenSBI fw_dynamic.bin started by SPL before U-Boot')
    parser.add_argument('--opensbi-sha256', required=True)
    args = parser.parse_args()
    if digest(args.opensbi) != args.opensbi_sha256:
        parser.error('OpenSBI checksum mismatch')
    if digest(args.fsbl) != args.fsbl_sha256 or not 0 < args.fsbl.stat().st_size <= 475136:
        parser.error('FSBL checksum or BootROM size limit mismatch')
    if args.output.exists():
        parser.error('output directory already exists; use a new versioned build directory')
    commit = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', args.commit], text=True).strip()
    args.output.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix='decklab-k3-ram-') as directory:
            work = Path(directory)
            source, build, fit = work / 'source', work / 'build', work / 'fit'
            source.mkdir()
            with (work / 'source.tar').open('wb') as output:
                subprocess.run(['git', '-C', str(args.source), 'archive', commit], stdout=output, check=True)
            with tarfile.open(work / 'source.tar') as archive:
                archive.extractall(source)
            profile = source / 'configs/decklab-k3-download.config'
            environment = source / 'board/spacemit/k3/decklab-download.env'
            (args.output / 'BUILD-INPUT.json').write_text(json.dumps({
                'uboot_commit': commit, 'profile': profile.name, 'profile_sha256': digest(profile),
                'default_env_sha256': digest(environment), 'fsbl_sha256': args.fsbl_sha256,
                'cross_compile': args.cross_compile, 'configuration': 'k3_defconfig', 'ram_only': True,
                'ddr_training_persistence': False, 'target': 'u-boot.bin', 'jobs': args.jobs,
                'opensbi_sha256': args.opensbi_sha256,
            }, indent=2) + '\n')
            with (args.output / 'build.log.txt').open('w') as log:
                command = ['make', 'O=' + str(build), 'CROSS_COMPILE=' + args.cross_compile]
                subprocess.run(command + ['k3_defconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                subprocess.run([str(source / 'scripts/kconfig/merge_config.sh'), '-m', '-O',
                                str(build), str(build / '.config'), str(profile)],
                               cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                subprocess.run([str(source / 'scripts/config'), '--file', str(build / '.config'),
                                '--set-str', 'DEFAULT_ENV_FILE', str(environment)], check=True)
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
                                 'CONFIG_CMD_UNZIP=y', 'CONFIG_GZIP=y', 'CONFIG_CMD_BOOTI=y',
                                 'CONFIG_FASTBOOT_FOLLOW_SPL_USB_SPEED=y'):
                    if expected not in configuration:
                        raise RuntimeError('required stock RAM configuration missing: ' + expected)
                for forbidden in ('CONFIG_CMD_UNZSTD=y', 'CONFIG_CMD_BGBLK=y'):
                    if forbidden in configuration:
                        raise RuntimeError('obsolete flash transport command enabled: ' + forbidden)
                subprocess.run(command + ['-j%d' % args.jobs, 'u-boot.bin'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                elf = (build / 'u-boot').read_bytes()
                if b'bootcmd=while true; do fastboot 0; done\x00' not in elf or b'bootcmd=run autoboot' in elf:
                    raise RuntimeError('compiled default environment is not Fastboot-only')
                shutil.copyfile(build / 'u-boot', args.output / 'u-boot.elf')
                symbols = subprocess.check_output([args.cross_compile + 'nm', '--defined-only',
                    str(build / 'u-boot')], text=True)
                if any(name in symbols for name in ('board_late_init', 'save_ddr_training_info',
                        'set_dev_serial_no', 'fastboot_decklab_', '__u_boot_list_2_cmd_2_unzstd',
                        '__u_boot_list_2_cmd_2_bgblk')):
                    raise RuntimeError('unexpected provisioning/custom loader symbol in stock RAM build')
                (args.output / 'SAFETY-CHECK.txt').write_text(
                    'U-Boot source archive includes the pinned download profile.\n'
                    'Native Kconfig disables board late-init and persistent env.\n'
                    'Compiled symbols exclude board late-init, DDR persistence and serial provisioning.\n')

                (fit / 'configs').mkdir(parents=True)
                (fit / 'dtb').mkdir()
                its = (source / 'board/spacemit/k3/configs/uboot_fdt.its').read_text()
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
                dtbs = build / 'arch/riscv/dts'
                for member in set(re.findall(r'/incbin/\(\"\.\./dtb/([A-Za-z0-9_.-]+\.dtb(?:\.lzo)?)\"\)', its)):
                    destination = fit / 'dtb' / member
                    if member.endswith('.lzo'):
                        lzo(dtbs / member.removesuffix('.lzo'), destination)
                    else:
                        shutil.copyfile(dtbs / member, destination)
                (args.output / 'k3').mkdir()
                subprocess.run([str(build / 'tools/mkimage'), '-f', str(fit / 'configs/uboot_fdt.its'),
                    str(args.output / 'k3/u-boot.itb')], check=True, stdout=log, stderr=subprocess.STDOUT)
            shutil.copyfile(args.fsbl, args.output / 'k3/FSBL.bin')
            shutil.copyfile(environment, args.output / 'RAM-ENV.txt')
            shutil.copyfile(source / 'Licenses/gpl-2.0.txt', args.output / 'COPYING.txt')
            materials = {'protocol': 'spacemit-ram-v1'}
            for kind, name in (('fsbl', 'k3/FSBL.bin'), ('uboot', 'k3/u-boot.itb')):
                materials[kind] = {'file': name, 'sha256': digest(args.output / name)}
            (args.output / 'manifest.json').write_text(json.dumps({'version': 1, 'families': {'k3': materials}}, indent=2) + '\n')
            (args.output / 'BUILD-INFO.txt').write_text('DeckLab K3 RAM-only download environment\n' +
                'U-Boot source commit: ' + commit + '\nProfile: ' + profile.name + '\n' +
                'FSBL SHA-256: ' + args.fsbl_sha256 + '\nToolchain prefix: ' + args.cross_compile + '\n' +
                'Build: k3_defconfig + native RAM configuration, u-boot.bin, compressed multi-DTB FIT\n' +
                'OpenSBI fw_dynamic SHA-256: ' + args.opensbi_sha256 + ' (started by SPL; U-Boot runs in S-mode)\n' +
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
