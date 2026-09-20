# 开发板唯一编号矩阵

本表记录已实测的**版型与内核组合**。同一块板切换内核后应新增一行，不覆盖旧结论。
`DT` 表示根设备树的 `serial-number`；“未暴露”只说明当前内核没有可读接口，不代表硬件不存在。

最后更新：2026-09-20

| 版型（现场标签） | 内核 | DT `serial-number` | EEPROM / Linux 接口 | SoC / eFuse | 身份选择 | 结论与待办 | 实测日期 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Milk-V Jupiter K1（`milkv2`） | Vendor 6.6.63 | `0123456789ABCDEF`，重复占位值 | 当前未暴露 | `0059D968E583C49A`；`soc0/serial_number`，K1 bank 7 芯片 ID | `0059D968E583C49A`，`soc-sysfs` | 可作临时硬件指纹；建议将同值写入 EEPROM，完成后改用 EEPROM 身份。 | 2026-09-19 |
| Milk-V Jupiter K1（`milkv`） | Upstream 7.2.2+ | `MJM10004000001`，写入 EEPROM 后由固件注入 | `atmel,24c04`，512 B；ONIE `0x23`/`cells/serial-number@*` 返回 `MJM10004000001` | 当前内核未暴露；限定只读返回 `EFAULT` | `MJM10004000001`，`eeprom-onie` | 已写入稳定板卡身份；原 `/boot` 候选档案由 seriald 1.3.4 在同一 USB 路径自动迁移。 | 2026-09-20 |
| Milk-V Jupiter K1（`milkv`） | Vendor 6.6.63 | `MJM10004000001`，U-Boot 从生产 EEPROM 注入 | 当前内核未枚举 I²C EEPROM NVMEM，仅有 eFuse 和 SPI MTD NVMEM；`/chosen/u-boot,version` 可见 | `0059D968E583CA22`；`soc0/serial_number` | `MJM10004000001`，`uboot-injected` | 由 U-Boot provenance 确认注入，不依赖编号格式；seriald 1.4.0 起自动迁移。 | 2026-09-20 |
| SpacemiT K3 Deb1（`deb1`） | Vendor 6.18.3-generic | `5b9d4a0a6f5e25a8` | `atmel,24c02`，256 B；ONIE `0x23` 有效，内核未启用 ONIE layout cell | `soc0/serial_number` 为全零，无效 | `5b9d4a0a6f5e25a8`，`eeprom-onie` | 已有稳定板卡身份。seriald 从原始 NVMEM 校验并解析 ONIE TLV。 | 2026-09-19 |
| MUSE-Pi K1-X（SSH `musepi2`） | Vendor 6.6.63 | `78ad7410ec82` | DT 有 `atmel,24c02@50`，但 `status=disabled`；推断由 U-Boot 读取并注入 DT | `soc_chip_id` 原始 8 B 全零；`soc0/serial_number` 全零 | `78ad7410ec82`，`device-tree`（接入时） | 当前可用。再用第二块同版板或 U-Boot 源码确认 EEPROM 编号唯一；更换 Bootloader 后需复测。 | 2026-09-19 |

## 判定规则

- **正式板卡身份**优先使用 EEPROM ONIE `0x23`；同值由 U-Boot 注入 DT 也可作为兼容入口。
- SoC/eFuse ID 是芯片指纹，可在 EEPROM 未写入时回退；换 SoC 后会变化，因此不等同于板卡序列号。
- 空值、全零、全 `F`、已知占位值和跨板重复值均无效。MAC、hostname、`machine-id`、USB 路径和 tty 节点不能证明板卡身份。
- EEPROM 存在、Linux 驱动绑定和序列号已写入是三个独立状态。表中必须分别记录。
- 只在至少两块同版板比较过，或已核对生产写入流程后，才能将某字段标为“已确认唯一”。

## 新增实测记录

每次新增版型或内核组合时记录：完整 `uname -r`、设备树 model/compatible、DT serial、EEPROM
型号与容量、NVMEM/cell 路径、SoC/eFuse 值、seriald 最终来源、实测日期及下一步。seriald 管理的板子通过
`serialctl --actor <操作者> --device <标签> exec --command '…'` 检查，避免直接打开其串口设备。

```markdown
| <版型/标签> | <Vendor/Upstream + uname -r> | <值/无/占位> | <芯片、容量、路径、状态> | <值、路径、状态> | <值、serial_source> | <结论/待办> | YYYY-MM-DD |
```

如同一版型仅升级小版本且所有身份接口和数值不变，可在原行追加版本范围；暴露路径、Bootloader 注入方式或
编号选择发生变化时必须新增一行。原始证据和选择算法见[硬件身份说明](HARDWARE_ID.md)。

## 对应档案目录

| 现场标签 | 当前状态 | 当前/规划目录 | 正式确认后的动作 |
| --- | --- | --- | --- |
| `milkv2` | SoC/eFuse 候选身份 | `logs/_provisional/soc-sysfs/0059D968E583C49A/` | EEPROM 写入同值后自动迁到 `logs/0059D968E583C49A/`。 |
| `milkv` | EEPROM 正式身份 | `logs/MJM10004000001/` | Upstream 走 `eeprom-onie`；Vendor 6.6.63 走 `uboot-injected`，候选档案自动迁移。 |
| `deb1` | EEPROM 正式身份 | `logs/5b9d4a0a6f5e25a8/` | 无需迁移。 |
| SSH `musepi2` | 尚未接入；DT 值待生产侧确认 | 接入后先用 `logs/_provisional/device-tree/78ad7410ec82/` | 确认 EEPROM 来源且编号不变后，用 `confirm-serial` 迁到 `logs/78ad7410ec82/`。 |
