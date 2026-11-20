#!/usr/bin/env python3
"""Build the K3 FSBL staged by DeckLab in USB download mode, without a board.

The FSBL is the vendor U-Boot SPL from an archived source revision plus the
patches in packaging/flash-loaders/patches/fsbl-*.patch, if any (in name
order; none since 1.17.41). In USB download mode the vendor SPL never uses
training data saved on the board's boot storage: it trains DDR (~5.6 s) and
leaves the result in SRAM, unless the image itself carries a result in the
space of the training firmware of the other DDR generation (the vendor's
"update FSBL" layout), which it then uses. DeckLab keeps the result per board
and embeds it for later loads of the same board. Nothing is written to the
board: the FSBL only runs from SRAM.

The build records the image offsets of both training-firmware arrays
(training_slots) so the host can embed a result without rebuilding.
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


# SPL is loaded with the image header at the start of SRAM.
LOAD_ADDRESS = 0xc0800000
TRAINING_INFO_SIZE = 0x9800


def training_slots(nm, elf, image):
    """Image offsets of the training-firmware arrays, by DDR type they hold
    results for: an LPDDR5 result goes over the LPDDR4X firmware and back."""
    symbols = {}
    for line in subprocess.check_output([nm, '-S', str(elf)], text=True).splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[3] in ('lp4x_training_fw', 'lp5_training_fw'):
            symbols[fields[3]] = (int(fields[0], 16) - LOAD_ADDRESS, int(fields[1], 16))
    slots = {}
    for kind, symbol in (('lpddr5', 'lp4x_training_fw'), ('lpddr4x', 'lp5_training_fw')):
        offset, size = symbols[symbol]
        if size < TRAINING_INFO_SIZE or offset < 0x1000 or offset + TRAINING_INFO_SIZE > len(image):
            raise RuntimeError('training slot %s does not fit the image' % symbol)
        slots[kind] = {'offset': offset, 'size': TRAINING_INFO_SIZE}
    return slots


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
        slots = training_slots(args.cross_compile + 'nm', source / 'spl/u-boot-spl', image)
        (args.output / 'k3').mkdir()
        shutil.copyfile(fsbl, args.output / 'k3/FSBL.bin')
        shutil.copyfile(source / '.config', args.output / 'FSBL-CONFIGURATION.txt')
    patches = [{'file': p.name, 'sha256': digest(p)} for p in PATCHES]
    (args.output / 'BUILD-INPUT.json').write_text(json.dumps({
        'uboot_commit': commit, 'source_patches': patches, 'cross_compile': args.cross_compile,
        'configuration': 'k3_defconfig', 'localversion': '-decklab-ram', 'training_slots': slots,
        'fsbl_sha256': digest(args.output / 'k3/FSBL.bin')}, indent=2) + '\n')
    print(args.output / 'k3/FSBL.bin')


if __name__ == '__main__':
    main()
