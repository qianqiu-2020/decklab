#!/usr/bin/env python3
"""Prepare a temporary RISC-V boot payload from a stock SpacemiT OS archive."""
import argparse
import gzip
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from .archive_io import open_archive

GIB = 1 << 30
COPY_BLOCK = 1 << 20
MAX_BOOT_FILE = 256 << 20
IMAGE_MAGIC = b'RISCV\0\0\0RSC\x05'
K1_MUSE_PI_PRO_MODEL = 'spacemit k1-x MUSE-Pi-Pro board'
SUPPORTED_MODELS = {
    'SpacemiT K3 Pico-ITX': 'k3',
    K1_MUSE_PI_PRO_MODEL: 'k1',
}
K3_PICO_MODEL = 'SpacemiT K3 Pico-ITX'
K3_PICO_COMPATIBLE = 'spacemit,k3-pico-itx'
DISABLE_NODE = re.compile(
    r'^(?:mmc|sdh|sdhci|sdio|pcie?|ufs(?:hc)?|sata|ahci|spi|qspi|sfc|nand|nor|usb\d*|'
    r'xhci|ehci|ohci|dwc[23]|dwc[23]-host|block|nvme)(?:[-_@]|$)', re.IGNORECASE)
BOOTARGS = ('console=ttyS0,115200 earlycon=sbi acpi=off clk_ignore_unused '
            'boot=ramroot noresume systemd.gpt_auto=0 rd.systemd.gpt_auto=0 '
            'module_blacklist=usb_storage,uas,nvme,sd_mod,mmc_block,ufshcd,mtdblock,spi_nor')
RAMROOT_SCRIPT = Path(__file__).with_name('ramboot-root.sh')
K1_BIANBU_SHA256 = '6c44685675b9c41c2ce4781604268be32a86c747dea89cb9e82834f7b4bdacf5'
K1_BIANBU_JUNE_SHA256 = '364508121016d452295105894c493362dac594c5b3f61e162b782f46885d9bed'
K1_BIANBU_RCPU = '/soc/rcpu_rproc@0'
K1_BIANBU_ASYNC = ('driver_async_probe=spacemit-hdmi-drv,i2c-spacemit-k1x,ri2c-spacemit-k1x,'
                  'k1xccic,sdhci-spacemit,k1x-dwc-pcie,pxa2xx-uart')


class PrepareError(ValueError):
    pass


def ramboot_tool_path():
    path = os.environ.get('PATH', os.defpath)
    directory = Path(__file__).resolve().parent.parent / 'ramboot-tools'
    if all(shutil.which(tool, path=str(directory)) for tool in ('lz4', 'lz4cat')):
        return str(directory) + os.pathsep + path
    return path


def run(*args, env=None):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env).stdout


def prepare_k1_bianbu_rcpu(dtb, image_digest):
    if image_digest not in (K1_BIANBU_SHA256, K1_BIANBU_JUNE_SHA256):
        raise PrepareError('this Bianbu image has no verified K1 RAM boot profile')
    compatible = run('fdtget', '-t', 's', str(dtb), K1_BIANBU_RCPU, 'compatible').decode().split()
    if compatible != ['spacemit,k1-x-rproc']:
        raise PrepareError('Bianbu K1 RCPU device tree node differs from the verified image')
    if image_digest == K1_BIANBU_SHA256:
        run('fdtput', '-t', 's', str(dtb), K1_BIANBU_RCPU, 'status', 'disabled')
        return ['k1-bianbu-rcpu-disabled']
    if run('fdtget', '-t', 's', str(dtb), K1_BIANBU_RCPU, 'status').decode().strip() != 'okay':
        raise PrepareError('Bianbu K1 RCPU is not enabled in the verified image')
    return []


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(COPY_BLOCK), b''):
            result.update(block)
    return result.hexdigest()


def select_partitions(entries, is_file, name, size):
    selected = {}
    for entry in entries:
        path = PurePosixPath(name(entry))
        filename = path.name
        if filename not in ('rootfs.ext4', 'bootfs.ext4'):
            continue
        if (path.is_absolute() or '..' in path.parts or not is_file(entry)
                or filename in selected):
            raise PrepareError(f'unsafe or ambiguous archive member {path}')
        if size(entry) <= 0:
            raise PrepareError(f'empty archive member {path}')
        selected[filename] = entry
    if 'rootfs.ext4' not in selected:
        raise PrepareError('archive must contain a single rootfs.ext4 partition')
    if len({PurePosixPath(name(entry)).parent for entry in selected.values()}) != 1:
        raise PrepareError('rootfs.ext4 and bootfs.ext4 are from different archive directories')
    return selected


def unpack_partitions(source, workspace, ram_bytes):
    def extract(selected, read, size):
        total = sum(size(entry) for entry in selected.values())
        if total > ram_bytes - 2 * GIB:
            raise PrepareError('partition images exceed the target RAM budget')
        if total > shutil.disk_usage(workspace).free:
            raise PrepareError('insufficient preparation disk space for partition images')
        for filename, entry in selected.items():
            with read(entry) as image, (workspace / filename).open('xb') as output:
                shutil.copyfileobj(image, output, COPY_BLOCK)
                if output.tell() != size(entry):
                    raise PrepareError(f'incomplete archive member {filename}')

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            size = lambda entry: entry.file_size
            selected = select_partitions(archive.infolist(),
                lambda entry: not entry.is_dir() and (entry.external_attr >> 28) in (0, 8),
                lambda entry: entry.filename, size)
            extract(selected, archive.open, size)
    else:
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:*') as archive:
            size = lambda entry: entry.size
            selected = select_partitions(archive, tarfile.TarInfo.isfile,
                                         lambda entry: entry.name, size)
            extract(selected, archive.extractfile, size)


def ext_read(image, filename):
    return run('debugfs', '-R', f'cat {filename}', str(image))


def ext_list(image, directory):
    entries = run('debugfs', '-R', f'ls -p {directory}', str(image)).decode(errors='replace')
    return [(parts[5], parts[2], int(parts[6] or 0)) for row in entries.splitlines()
            if (parts := row.split('/'))[0] == '' and len(parts) == 8
            and re.fullmatch(r'[A-Za-z0-9_.+@-]+', parts[5]) and parts[5] not in ('.', '..')]


def ext_dump(image, filename, path):
    # Keep generated host paths outside the debugfs command grammar.
    if not re.fullmatch(r'/[A-Za-z0-9_./+@-]+', filename):
        raise PrepareError('invalid filesystem path')
    subprocess.run(['debugfs', '-R', f'dump {filename} payload', str(image)],
                            cwd=path.parent, check=True, capture_output=True)
    payload = path.parent / 'payload'
    if not payload.is_file() or not payload.stat().st_size:
        raise PrepareError(f'cannot extract {filename} from {image.name}')
    if payload.stat().st_size > MAX_BOOT_FILE:
        raise PrepareError(f'boot asset too large: {filename}')
    payload.rename(path)


def boot_files(root, boot, workspace):
    directory = '/' if boot != root else '/boot/'
    entries = {name: size for name, mode, size in ext_list(boot, directory) if mode.startswith('10')}
    versions = [name.removeprefix('initrd.img-') for name in entries
                if re.fullmatch(r'initrd.img-[A-Za-z0-9.+_-]+', name)]
    choices = [(version, kernel) for version in versions
               for kernel in (f'vmlinuz-{version}', f'vmlinux-{version}', f'Image-{version}')
               if kernel in entries and f'config-{version}' in entries]
    if len(choices) != 1:
        raise PrepareError('expected one matching kernel, initramfs and kernel config')
    version, kernel_name = choices[0]
    if any(not 0 < entries[name] <= MAX_BOOT_FILE
           for name in (kernel_name, f'initrd.img-{version}', f'config-{version}')):
        raise PrepareError('invalid boot asset size')
    ext_dump(boot, directory + kernel_name, workspace / 'kernel')
    ext_dump(boot, directory + f'initrd.img-{version}', workspace / 'initrd.original')
    ext_dump(boot, directory + f'config-{version}', workspace / 'kernel.config')
    ext_dump(root, '/usr/lib/systemd/systemd', workspace / 'systemd')
    with (workspace / 'systemd').open('rb') as binary:
        header = binary.read(20)
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\xf3\0':
        raise PrepareError('root filesystem userspace is not RISC-V 64-bit')
    return version


def kernel_image(source, output):
    data = source.read_bytes()
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as kernel:
            data = kernel.read(MAX_BOOT_FILE + 1)
        if len(data) > MAX_BOOT_FILE:
            raise PrepareError('expanded kernel exceeds the boot asset size limit')
    if data[48:60] != IMAGE_MAGIC:
        if len(data) < 64 or data[:2] != b'MZ':
            raise PrepareError('kernel is neither a RISC-V Image nor a RISC-V EFI unified kernel')
        pe_offset = struct.unpack_from('<I', data, 60)[0]
        if pe_offset > len(data) - 24 or data[pe_offset:pe_offset + 6] != b'PE\0\0\x64\x50':
            raise PrepareError('kernel EFI machine is not RISC-V 64-bit')
        count = struct.unpack_from('<H', data, pe_offset + 6)[0]
        optional = struct.unpack_from('<H', data, pe_offset + 20)[0]
        table = pe_offset + 24 + optional
        if not count or table + count * 40 > len(data):
            raise PrepareError('invalid EFI section table')
        linux = []
        for offset in range(table, table + count * 40, 40):
            if data[offset:offset + 8] != b'.linux\0\0':
                continue
            length, _, raw_length, raw_offset = struct.unpack_from('<IIII', data, offset + 8)
            if length > raw_length or raw_offset + raw_length > len(data):
                raise PrepareError('invalid EFI Linux section bounds')
            linux.append(data[raw_offset:raw_offset + length])
        if len(linux) != 1 or linux[0][48:60] != IMAGE_MAGIC:
            raise PrepareError('EFI kernel does not contain a single uncompressed RISC-V Image')
        data = linux[0]
    output.write_bytes(data)


def choose_dtb(root, boot, version, board, workspace):
    candidates = {}
    for image, directory in dict.fromkeys(
            [(boot, f'/spacemit/{version}'), (root, f'/usr/lib/modules/{version}/dtb/spacemit'),
             (root, f'/usr/lib/linux-image-{version}/spacemit')]):
        for name, mode, size in ext_list(image, directory):
            if not (name.endswith('.dtb') and mode.startswith('10') and 0 < size <= 1 << 20):
                continue
            path = workspace / 'candidate.dtb'
            ext_dump(image, f'{directory}/{name}', path)
            model = run('fdtget', '-d', '', '-t', 's', str(path), '/', 'model').decode().strip()
            if not model:
                continue
            compatible = run('fdtget', '-d', '', '-t', 's', str(path), '/', 'compatible').decode().split()
            if board == K3_PICO_MODEL:
                matches = (model in (K3_PICO_MODEL, 'SpacemiT K3 Pico ITX')
                           and K3_PICO_COMPATIBLE in compatible)
            else:
                matches = model == board and 'spacemit,k1-x' in compatible
            if matches:
                candidates[digest(path)] = path.read_bytes()
    if len(candidates) != 1:
        raise PrepareError(f'expected one distinct device tree matching {board!r}; found {len(candidates)}')
    return next(iter(candidates.values()))


def disable_storage(dtb):
    disabled = []
    pending = ['/']
    while pending:
        parent = pending.pop()
        children = run('fdtget', '-l', str(dtb), parent).decode().splitlines()
        for child in children:
            path = f'{parent.rstrip("/")}/{child}'
            if child.split('@', 1)[0] in ('pinctrl', 'pinmux'):
                continue
            if DISABLE_NODE.match(child):
                run('fdtput', '-t', 's', str(dtb), path, 'status', 'disabled')
                disabled.append(path)
            else:
                pending.append(path)
    if not disabled:
        raise PrepareError('device tree does not describe expected storage buses')
    return sorted(disabled)


def clear_k1_boot_properties(dtb):
    if b'chosen' not in run('fdtget', '-l', str(dtb), '/').splitlines():
        return
    properties = run('fdtget', '-p', str(dtb), '/chosen').decode().splitlines()
    for name in properties:
        # K1 U-Boot merges DT bootargs into the volatile environment. EFI
        # pointers in a captured tree refer to the original firmware session.
        if name == 'bootargs' or name.startswith('linux,uefi-'):
            run('fdtput', '-d', str(dtb), '/chosen', name)


def overlay_module(config, root, version, workspace):
    if not all(f'CONFIG_{name}=y' in config for name in ('BLK_DEV_LOOP', 'EXT4_FS', 'BLK_DEV_INITRD')):
        raise PrepareError('kernel must build in initramfs, loopback and ext4 support')
    if 'CONFIG_OVERLAY_FS=y' in config:
        return None
    if 'CONFIG_OVERLAY_FS=m' not in config:
        raise PrepareError('kernel lacks overlay filesystem support')
    directory = f'/usr/lib/modules/{version}'
    dependencies = ext_read(root, f'{directory}/modules.dep').decode(errors='replace')
    match = re.search(r'^([A-Za-z0-9_/-]*/overlay\.ko(?:\.zst|\.xz|\.gz)?):[ \t]*$',
                      dependencies, re.MULTILINE)
    if not match:
        raise PrepareError('expected a dependency-free overlay module for this kernel')
    path = workspace / Path(match[1]).name
    ext_dump(root, f'{directory}/{match[1]}', path)
    module = workspace / 'ramroot-overlay.ko'
    commands = {'.zst': 'zstd', '.xz': 'xz', '.gz': 'gzip'}
    with module.open('xb') as output:
        if path.suffix in commands:
            subprocess.run([commands[path.suffix], '-dc', str(path)], stdout=output, check=True)
        else:
            with path.open('rb') as source:
                shutil.copyfileobj(source, output, COPY_BLOCK)
    return module


def newc_entry(output, inode, filename, mode, length, data=None):
    name = filename.encode('ascii') + b'\0'
    fields = (inode, mode, 0, 0, 2 if mode & 0o170000 == 0o040000 else 1, 0,
              length, 0, 0, 0, 0, len(name), 0)
    output.write(b'070701' + ''.join(f'{value:08x}' for value in fields).encode('ascii'))
    output.write(name)
    output.write(b'\0' * (-(110 + len(name)) % 4))
    remaining = length
    while remaining:
        data_block = data.read(min(remaining, COPY_BLOCK))
        if not data_block:
            raise PrepareError(f'payload ended while writing {filename}')
        output.write(data_block)
        remaining -= len(data_block)
    output.write(b'\0' * (-length % 4))


def build_initramfs(output, original, packed, module):
    with output.open('xb') as archive:
        newc_entry(archive, 1, 'scripts', 0o040755, 0)
        newc_entry(archive, 2, 'ramroot', 0o040700, 0)
        with RAMROOT_SCRIPT.open('rb') as script:
            newc_entry(archive, 3, 'scripts/ramroot', 0o100644, RAMROOT_SCRIPT.stat().st_size, script)
        if module:
            with module.open('rb') as data:
                newc_entry(archive, 4, 'ramroot/overlay.ko', 0o100600, module.stat().st_size, data)
        with packed.open('rb') as data:
            remaining = packed.stat().st_size
            index = 0
            while remaining:
                length = min(remaining, GIB)
                newc_entry(archive, 5 + index, f'ramroot/rootfs.{index:04d}', 0o100600, length, data)
                remaining -= length
                index += 1
        newc_entry(archive, 0, 'TRAILER!!!', 0, 0)
        with original.open('rb') as source:
            shutil.copyfileobj(source, archive, COPY_BLOCK)


def prepare(image, board, ram_bytes, output, expected_sha256=None):
    if board not in SUPPORTED_MODELS:
        raise PrepareError(f'unsupported board model: {board!r}')
    if ram_bytes < 4 * GIB:
        raise PrepareError('at least 4 GiB of reported target memory is required')
    if output.exists() or output.is_symlink():
        raise PrepareError('output directory already exists')
    with tempfile.TemporaryDirectory(prefix='.ramboot-', dir=output.parent) as temp:
        workspace = Path(temp)
        result = workspace / 'result'
        result.mkdir(mode=0o700)
        with open_archive(image) as source:
            before = os.fstat(source.fileno())
            hashed = hashlib.sha256()
            for block in iter(lambda: source.read(COPY_BLOCK), b''):
                hashed.update(block)
            if expected_sha256 is not None and hashed.hexdigest() != expected_sha256:
                raise PrepareError('input does not match the sealed image digest')
            source.seek(0)
            unpack_partitions(source, workspace, ram_bytes)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PrepareError('input changed during preparation')
        root = workspace / 'rootfs.ext4'
        boot = workspace / 'bootfs.ext4'
        if not boot.exists():
            boot = root
        release = ext_read(root, '/usr/lib/os-release').decode(errors='replace')
        match = re.search(r'^ID="?((?:debian|bianbu))"?$', release, re.MULTILINE)
        if not match:
            raise PrepareError('root filesystem does not identify Debian or Bianbu')
        version = boot_files(root, boot, workspace)
        original = workspace / 'initrd.original'
        tool_env = dict(os.environ, PATH=ramboot_tool_path())
        initramfs_files = run('lsinitramfs', str(original), env=tool_env).decode(errors='replace').splitlines()
        if ('init' not in initramfs_files or 'scripts/local' not in initramfs_files
                or 'scripts/ramroot' in initramfs_files):
            raise PrepareError('expected an unmodified initramfs-tools image')
        kernel_image(workspace / 'kernel', result / 'Image')
        dtb = result / 'board.dtb'
        dtb.write_bytes(choose_dtb(root, boot, version, board, workspace))
        disabled = disable_storage(dtb)
        limitations = []
        bootargs = BOOTARGS
        if board == K1_MUSE_PI_PRO_MODEL:
            clear_k1_boot_properties(dtb)
            if match[1] == 'bianbu':
                limitations = prepare_k1_bianbu_rcpu(dtb, hashed.hexdigest())
                bootargs += ' ' + K1_BIANBU_ASYNC
            else:
                bootargs += ' swiotlb=65536'
        config = set((workspace / 'kernel.config').read_text().splitlines())
        module = overlay_module(config, root, version, workspace)
        packed = workspace / 'rootfs.gz'
        with packed.open('xb') as archive:
            subprocess.run(['gzip', '-1nc', str(root)], stdout=archive, check=True)
        # At extraction time, reserve space for the transferred initrd and its copy
        # in initramfs. During boot, reserve room for decompressing the ext4 image.
        initrd_bytes = packed.stat().st_size + original.stat().st_size + (16 << 20)
        estimated = max(2 * initrd_bytes, initrd_bytes + root.stat().st_size) + 2 * GIB
        if estimated > ram_bytes:
            raise PrepareError(f'need at least {estimated} bytes of RAM for this image, got {ram_bytes}')
        build_initramfs(result / 'initrd.img', original, packed, module)
        receipt = {
            'image': image.name, 'image_sha256': hashed.hexdigest(), 'distribution': match[1],
            'board_model': board, 'chip': SUPPORTED_MODELS[board], 'architecture': 'riscv64',
            'kernel_version': version, 'bootargs': bootargs, 'ram_bytes': ram_bytes,
            'estimated_min_ram_bytes': estimated, 'ram_ext4_bytes': root.stat().st_size,
            'disabled_device_tree_nodes': disabled,
            'files': {path.name: {'size': path.stat().st_size, 'sha256': digest(path)}
                      for path in sorted(result.iterdir())},
        }
        if limitations:
            receipt['limitations'] = limitations
        (result / 'ramboot.json').write_text(json.dumps(receipt, indent=2) + '\n')
        result.rename(output)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, help='original OS partition archive (.tar, .tar.gz, .zip)')
    parser.add_argument('--board-model', required=True, help='exact model reported by the target board')
    parser.add_argument('--ram-bytes', type=int, required=True, help='usable RAM reported by the target board')
    parser.add_argument('--output', type=Path, required=True, help='new directory on the control host')
    parser.add_argument('--expected-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--task-result', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    def reject(message):
        if args.task_result:
            print(json.dumps({'error': message[:2000]}))
        else:
            print(f'RAM boot preparation failed: {message}', file=sys.stderr)
        return 1
    try:
        receipt = prepare(args.image, args.board_model, args.ram_bytes, args.output, args.expected_sha256)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b'').decode(errors='replace').strip()
        return reject(detail or str(error))
    except (OSError, EOFError, PrepareError, tarfile.TarError, zipfile.BadZipFile) as error:
        return reject(str(error))
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
