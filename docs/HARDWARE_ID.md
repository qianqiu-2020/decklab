# 开发板身份

各版型与内核组合的实测结果维护在[开发板唯一编号矩阵](DEVICE_IDENTITY_MATRIX.md)。新增内核或版型时先追加矩阵，
再按本页规则决定是否可作为正式身份。

## 自动选择规则

识别同时读取固件的 `/proc/device-tree/serial-number`、ONIE EEPROM、Linux 的
`/sys/devices/soc0/{serial_number,family,soc_id}`，并只读检查 `/boot/seriald-id`。有效的已有板子编号保持不变；固件编号缺失、
为占位值（例如 `0123456789ABCDEF`）、全零/全 F，或已确认重复时，优先使用有效的 SoC 编号。
已经建立的 SoC 身份会优先匹配，避免固件随后修改上报编号而新建另一份档案。

API 的 `serial` 是当前身份编号；`reported_serial` 保留固件原值，`serial_source` 表示
`eeprom-onie`、`device-tree`、`soc-sysfs` 或 `boot-marker`，`identity_status` 区分 `confirmed` 与
`provisional`。网页详情展示来源和状态。重复保护作用于所有身份来源。

普通识别只读取内核属性和已有标记，不创建文件、不访问物理内存，也不调整时钟、复位、内核或 eFuse 内容。
ONIE `0x23` 是跨内核的规范板卡身份。upstream 内核优先从
`/sys/bus/nvmem/devices/*/cells/serial-number@*` 读取；旧 vendor 内核没有 cell 时，服务从
`at24` NVMEM 原始内容解析 TLV，并校验 header、长度与 CRC。U-Boot 注入 DT 的相同编号作为兼容入口。
没有永久编号时按 USB by-path 摘要显示 `unprovisioned-…`，日志分连接暂存到
`logs/_unprovisioned/<path-hash>/<connection-id>/`，避免同一插口先后接板时静默合并。

## /boot 软件标记兜底

确认目标连接后，显式初始化一次：

```sh
serialctl --actor operator --device unprovisioned-实际摘要 identify --init-boot-marker
serialctl --device 返回的device_key rename --label milkv
```

源码环境用 `python3 serialctl.py`。命令通过现有串口服务访问板子；在可写的 `/boot/seriald-id`
生成 `boot-<UUIDv4>`，同步到磁盘后重新识别。不会自动挂载、重新挂载或创建 `/boot`。
已有有效文件直接复用，损坏文件、符号链接、只读分区或写入失败均报错，不覆盖、不自动换号。
普通网页“识别设备”及不带该参数的 CLI 命令只读标记。API 对应 `init_boot_marker: true`，
成功操作带 actor 写入历史。已有硬件身份的在线设备不需要初始化标记。

这是系统文件身份，不是出厂硬件序列号。API 使用 `serial` 字段，日志目录是
`logs/_provisional/boot-marker/boot-<UUID>/`，网页注明“/boot 软件标记”。改标签、换 tty/USB 插口、重启服务不改变编号。
已登记的标记保持档案连续，即使后来内核能读取硬件编号，也应通过明确的档案更正进行迁移。

保留文件才能延续身份；重装丢失后不能凭相同 USB 插口找回。制作通用镜像或克隆到其他板子前
移除标记，在目标板重新初始化。检测到重复标记会保留冲突并拒绝合并；两块克隆设备从未同时出现时，
仅靠该文件无法证明它们是不同硬件。初始化超时先读取文件与设备状态，不盲目生成新编号。

## SpacemiT K1 / Milk-V Jupiter

已核对厂商驱动：K1 的 `soc_chip_id` 来自 eFuse bank 7 的 55 位芯片编号；socinfo 驱动将其
以 16 位大写十六进制输出到 `/sys/devices/soc0/serial_number`。识别拒绝空值、全零、全一及超出该范围的值。
实现依据固定版本的 [SoC 驱动](https://github.com/spacemit-com/linux-6.6/blob/31c449aeaad8c7759bc983ca0e26946e5b6746dc/drivers/soc/spacemit/spacemit-socinfo.c)、
[eFuse 布局](https://github.com/spacemit-com/linux-6.6/blob/31c449aeaad8c7759bc983ca0e26946e5b6746dc/arch/riscv/boot/dts/spacemit/k1-x-efuse.dtsi) 和
[只读 NVMEM 驱动](https://github.com/spacemit-com/linux-6.6/blob/31c449aeaad8c7759bc983ca0e26946e5b6746dc/drivers/nvmem/spacemit-efuse.c)。

当前现场的 Bianbu 6.6.63 提供此接口；另一块运行 7.2.2+ 的板子没有对应设备树节点和 sysfs 属性，
手工限定范围的只读访问返回 `EFAULT`。这只能说明当前系统未能读取，不能说明芯片没有唯一 ID。
获取真实硬件编号可以补齐只读 NVMEM/socinfo 驱动与设备树，或由固件将真实编号传入设备树；
当前也可使用上面的 `/boot` 软件标记。安装 seriald 包不会修改开发板内核。
现场两块板的有线 MAC 也相同，不能作为它们的唯一身份；“永久 MAC”并不保证没有出厂或克隆重复。

## Vendor 6.18 实测

SpacemiT K3 Deb1 的 Bianbu 4.0.7 / `6.18.3-generic` 已提供 `at24` 和 NVMEM，EEPROM 为
`atmel,24c02`、256 字节。设备树含 `onie,tlv-layout`，但内核配置为
`# CONFIG_NVMEM_LAYOUT_ONIE_TLV is not set`，因此没有 `cells/serial-number@*`。
EEPROM 原始 ONIE 表中的 `0x23` 为 `5b9d4a0a6f5e25a8`，CRC 有效，DT serial 也是同一值。
服务可从原始 NVMEM 得到稳定身份；vendor 内核启用该配置后即可与 upstream 使用相同 cell 路径。

## 更正已经核实的旧档案编号

确认旧档案的所有日志都属于同一块板后，停服务并备份，再使用管理命令（默认预览）：

```sh
sudo systemctl stop seriald
sudo seriald-admin --base /data/seriald-data correct-serial --old OLD_SERIAL --new VERIFIED_SOC_SERIAL
sudo seriald-admin --base /data/seriald-data correct-serial --old OLD_SERIAL --new VERIFIED_SOC_SERIAL --apply
sudo chown -R seriald:seriald /data/seriald-data
sudo systemctl start seriald
```

更正保留标签、会话和事件，将日志迁入新编号目录；数据库更新成功后删除旧日志和旧目录。
已有目标身份或不同内容的同名日志会阻止更正。旧编号的冲突记录继续保留。
不要将已经混入多块板记录的旧档案整体更正为某一块板。
源码环境使用 `python3 -m seriald_core.admin`；当前源码服务由 `systemctl --user … seriald-source` 管理。

候选编号经生产记录或多板对比确认后，如果编号本身不变，使用：

```sh
sudo systemctl stop seriald
sudo seriald-admin --base /data/seriald-data confirm-serial --serial VERIFIED_SERIAL
sudo seriald-admin --base /data/seriald-data confirm-serial --serial VERIFIED_SERIAL --apply
sudo chown -R seriald:seriald /data/seriald-data
sudo systemctl start seriald
```

该操作把 `device-tree`、`soc-sysfs` 或 `boot-marker` 候选目录迁入 `logs/<serial>/`，并记录
`identity-confirmed` 事件。预览不修改文件或数据库。
