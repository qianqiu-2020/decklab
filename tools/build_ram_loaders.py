#!/usr/bin/env python3
"""Build a private K3 RAM-only download environment, without accessing a board.

Input FSBL is checksum-pinned and reused solely for RAM staging. U-Boot is built
from an archived source revision with the reviewed non-writing NOR adapter patch.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
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
    args = parser.parse_args()
    if digest(args.fsbl) != args.fsbl_sha256 or not 0 < args.fsbl.stat().st_size <= 475136:
        parser.error('FSBL checksum or BootROM size limit mismatch')
    if args.output.exists():
        parser.error('output directory already exists; use a new versioned build directory')
    commit = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', args.commit], text=True).strip()
    patch = ROOT / 'packaging/flash-loaders/k3-ram-only-nor.patch'
    args.output.mkdir(parents=True)
    (args.output / 'BUILD-INPUT.json').write_text(json.dumps({
        'uboot_commit': commit, 'patch_sha256': digest(patch),
        'fsbl_sha256': args.fsbl_sha256, 'cross_compile': args.cross_compile,
        'configuration': 'k3_defconfig', 'target': 'u-boot.bin', 'jobs': args.jobs
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
            subprocess.run(['git', 'apply', '--unidiff-zero', str(patch)], cwd=source, check=True)
            with (args.output / 'build.log.txt').open('w') as log:
                command = ['make', 'O=' + str(build), 'CROSS_COMPILE=' + args.cross_compile]
                subprocess.run(command + ['k3_defconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                subprocess.run(command + ['-j%d' % args.jobs, 'u-boot.bin'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
                (fit / 'configs').mkdir(parents=True)
                (fit / 'dtb').mkdir()
                shutil.copyfile(source / 'board/spacemit/k3/configs/uboot_fdt.its', fit / 'configs/uboot_fdt.its')
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
            shutil.copyfile(source / 'Licenses/gpl-2.0.txt', args.output / 'COPYING.txt')
            shutil.copyfile(patch, args.output / patch.name)
            materials = {'protocol': 'decklab-nor-v1'}
            for kind, name in (('fsbl', 'k3/FSBL.bin'), ('uboot', 'k3/u-boot.itb')):
                materials[kind] = {'file': name, 'sha256': digest(args.output / name)}
            (args.output / 'manifest.json').write_text(json.dumps({'version': 1, 'families': {'k3': materials}}, indent=2) + '\n')
            (args.output / 'BUILD-INFO.txt').write_text('DeckLab K3 RAM-only download environment\n' +
                'U-Boot source commit: ' + commit + '\nPatch SHA-256: ' + digest(patch) + '\n' +
                'FSBL SHA-256: ' + args.fsbl_sha256 + '\nToolchain prefix: ' + args.cross_compile + '\n' +
                'Build: k3_defconfig, u-boot.bin, compressed multi-DTB FIT\n' +
                'Loaders are RAM-only, never partition payloads. NOR map selection does not save env or write storage.\n' +
                'Compiled/offline checked; no physical-board acceptance implied.\n')
        print(args.output)
    except BaseException:
        print('Build log and input fingerprints retained in: ' + str(args.output))
        raise


if __name__ == '__main__':
    main()
