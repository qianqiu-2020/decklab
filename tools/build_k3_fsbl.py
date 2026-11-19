#!/usr/bin/env python3
"""Build the K3 FSBL staged by DeckLab in USB download mode, without a board.

The FSBL is the vendor U-Boot SPL from an archived source revision plus the
patches in packaging/flash-loaders/patches/fsbl-*.patch (in name order). The
patch lets SPL reuse the DDR training data the board saved on its own boot
storage instead of training again (~5.6 s); the data is still validated and a
board without valid saved data is trained as before. Nothing is written to
the board: the FSBL only runs from SRAM.
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
PATCHES = sorted((ROOT / 'packaging/flash-loaders/patches').glob('fsbl-*.patch'))
BOOTROM_LIMIT = 475136


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='vendor U-Boot git repository')
    parser.add_argument('--commit', required=True)
    parser.add_argument('--cross-compile', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=8)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output directory already exists; use a new versioned build directory')
    commit = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', args.commit], text=True).strip()
    args.output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix='decklab-k3-fsbl-') as directory:
        source = Path(directory) / 'source'
        source.mkdir()
        with (Path(directory) / 'source.tar').open('wb') as output:
            subprocess.run(['git', '-C', str(args.source), 'archive', commit], stdout=output, check=True)
        with tarfile.open(Path(directory) / 'source.tar') as archive:
            archive.extractall(source)
        for patch in PATCHES:
            subprocess.run(['patch', '-p1', '--forward', '--batch', '-i', str(patch)], cwd=source, check=True,
                           stdout=subprocess.DEVNULL)
        make = ['make', 'CROSS_COMPILE=' + args.cross_compile]
        with (args.output / 'build.log.txt').open('w') as log:
            subprocess.run(make + ['k3_defconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
            subprocess.run([str(source / 'scripts/config'), '--file', str(source / '.config'),
                            '--set-str', 'LOCALVERSION', '-decklab-ram'], check=True)
            subprocess.run(make + ['olddefconfig'], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
            subprocess.run(make + ['-j%d' % args.jobs], cwd=source, check=True, stdout=log, stderr=subprocess.STDOUT)
        fsbl = source / 'FSBL.bin'
        if not 0 < fsbl.stat().st_size <= BOOTROM_LIMIT:
            raise RuntimeError('FSBL exceeds the BootROM size limit')
        image = fsbl.read_bytes()
        if b'U-Boot SPL 2022.10-decklab-ram' not in image or b'DDR quick boot consume' not in image:
            raise RuntimeError('FSBL lacks the DeckLab version or quick-boot path')
        (args.output / 'k3').mkdir()
        shutil.copyfile(fsbl, args.output / 'k3/FSBL.bin')
        shutil.copyfile(source / '.config', args.output / 'FSBL-CONFIGURATION.txt')
    patches = [{'file': p.name, 'sha256': digest(p)} for p in PATCHES]
    (args.output / 'BUILD-INPUT.json').write_text(json.dumps({
        'uboot_commit': commit, 'source_patches': patches, 'cross_compile': args.cross_compile,
        'configuration': 'k3_defconfig', 'localversion': '-decklab-ram',
        'fsbl_sha256': digest(args.output / 'k3/FSBL.bin')}, indent=2) + '\n')
    print(args.output / 'k3/FSBL.bin')


if __name__ == '__main__':
    main()
