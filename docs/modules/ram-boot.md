# Start an original Debian or Bianbu image in RAM

In DeckLab, open **内存启动**, enter USB download mode on the K1 MUSE Pi Pro
or K3 Pico-ITX, and detect its USB target. Select the original OS archive
from the local upload, image cache or repository, then choose **从镜像内存启动**.
MCP clients can use `boot_ram` as described in [the MCP guide](mcp.md#k1--k3-刷机回归).
The Linux control node needs a compatible DeckLab flash executor and the
matching RAM boot loader. For the verified K3 firmware bundle, DeckLab combines the
vendor U-Boot and OpenSBI into one FIT and stages that file after the original
FSBL. The vendor U-Boot runs with its original version string.
The board owner can follow or cancel the operation in the task panel.

DeckLab checks the EEPROM serial, the temporary loader's board model and its
reported DRAM. It reserves the download endpoint for the operation and stages
only volatile boot components. The task receipt reports the source SHA-256,
the minimum estimated memory and the generated payload checksums. Completion
confirms the loader accepted the boot command. Use the board console to check
that userspace started and verify the root backing mount. Rebooting from RAM
uses the board's normal boot settings.

## Offline preparation

The standalone command also accepts the original image and the exact board
model and memory capacity when working without an attached board.

```sh
python3 tools/prepare_ramboot.py system.tar.gz \
    --board-model 'SpacemiT K3 Pico-ITX' --ram-bytes 32000000000 \
    --output ./ramboot-output
```

The output is a RISC-V kernel, matching device tree, initramfs and a JSON
receipt with boot arguments, input and output SHA-256 digests, and a memory
estimate. The output path must be new. Preparation runs on the control host
without a board or a storage mount. It uses Python 3.10+, `debugfs`,
`lsinitramfs`, `fdtget`, `fdtput`, `gzip` and the decompressors required by the
archive's initramfs and overlay module (for example, `lz4` or `zstd`).

The tool accepts a tar or ZIP partition archive containing `rootfs.ext4` and
an optional `bootfs.ext4`. It locates matching kernel, initramfs and device-tree
assets within the original archive. The kernel must support initramfs, ext4,
the loop device and overlayfs. The board model must match the device tree
by model and compatible string. The supported offline preparation profiles
are the K1 MUSE Pi Pro and the K3 Pico-ITX.
A board image can have the boot files in the root partition or in a separate
boot partition. Raw disks, ISO images, and architectures other than RISC-V
64-bit are outside these layouts.

The temporary device tree disables storage buses. At boot, the initramfs
stops if it sees an unexpected block or MTD device. It mounts a read-only
loopback ext4 image stored in tmpfs, with a writable tmpfs overlay. Its
temporary root drops persistent mount entries and disables GPT auto-mount
and hibernation resume. The offline preparation command produces files for
diagnosis on the control node. DeckLab checks its load addresses against the
DRAM boundaries reported by the temporary loader.

From the serial console, check the distribution and root mount, the ext4
lower mount, the loop backing file, and the tmpfs that contains it. Verify
independent reboot and persistent-storage readback before accepting a board:

```sh
cat /etc/os-release
findmnt -T / -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T /run/ramroot/lower -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T /run/ramroot/rootfs.ext4 -o TARGET,SOURCE,FSTYPE,OPTIONS
losetup --list --output NAME,BACK-FILE,RO
lsblk -o NAME,TYPE,MOUNTPOINTS
cat /proc/mtd
```

Linux 完整安装可以在锁定的组件清单中提供随包 LZ4 CLI。该工具只加入内存启动准备进程的辅助命令路径，来源和许可随包保存在 `ramboot-lz4` 文档目录。未提供组件时沿用发行版的 `lz4` 依赖。
