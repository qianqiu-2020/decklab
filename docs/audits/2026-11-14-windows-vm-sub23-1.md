# Windows 安装版与 sub23-1 真机验收

2026-11-14。主程序使用 GitHub 正式版 v1.8.0（源码 `e45d7e8`）的原始 Windows 安装包，
没有替换主程序文件。刷机执行器与 RAM 加载器单独安装，不随主程序公开发行。

本机创建中文 Windows 11 Enterprise 评估版虚拟机，分配 4 个虚拟 CPU、8 GiB 内存、
80 GiB 稀疏磁盘。虚拟机、私有配置和原始验收证据保存在
`/data/vms/decklab-windows-20261114/`，不进入 Git。

本轮属于 Windows 单机模式的串口与刷机验收。进入下载模式、断电和上电由既有 Linux BMC
提供，作为测试准备与归还步骤，不计作 Windows 复位能力验收。
多机模式按 Linux/Windows 上位机连接专用 Linux 控制机验收，详见 [部署模式](../DISTRIBUTED.md)。

| 验收项目 | 实际结果 |
|---|---|
| 正式安装包 | 便携目录迁移、记住升级目录、运行数据保留、快捷方式图标、卸载后保留数据均通过 |
| 内置 MCP | Windows 冻结程序启动服务端 SDK；HTTP 连接、13 个工具、授权撤销通过 |
| MCP 读取真实终端 | `legacy`、`auto` 两种模式均读取到云板 COM2 的 Debian 登录提示 |
| Windows Edge | 实际安装版页面、设备列表、真实 COM2 终端正常；刷新及打开终端无脚本异常 |
| Windows USB 驱动 | 官方 TitanFlasher 驱动目录的签名验证通过；设备显示 SPACEMIT Bootloader Interface、状态 OK |
| BootROM 通信 | Windows 执行器读到 K3、SPA3609、BootROM 1.0 |
| NOR 读取、选区刷写、回读 | 通过，详见下方原始任务与摘要 |

只操作用户指定的 sub23-1：BMC 模组 47、UART `/dev/ttyS_47 -> ttyS51`、
下载 USB 物理路径 `1-8.3`。串口经原 BMC 数据流接入虚拟 COM2，没有另开原始 UART 采集器。
USB 经 USB/IP 和 QEMU USB 重定向接入 Windows，Windows 执行器实际提交 USB 请求。
导出、导入和重定向均限定这一路设备，未使用本机其他下载设备或 FTDI 串口。

本轮现场发现并修复了独立执行器的两处 Windows 兼容问题，提交为 `5007f5e`：

- 已打开 WinUSB 句柄后，使用该句柄读取和校验序列号，避免二次打开失败被当作设备身份变化。
- Windows 可以在重枚举后复用 USB 地址。仅在明确观察到原物理端口断开后，才允许检查同地址的新加载阶段；芯片身份、加载器身份及最终布局仍须验证。

增加了同地址重连、没有观察到断开、仍处于 BootROM、芯片型号错误的回归。
`go test -race ./internal/flashplan ./internal/transport ./cmd/decklab-flash` 通过，
Linux amd64、Windows amd64 编译通过；随后以修复后的 Windows 执行器完成真机流程。
此前任务 `76f8192f37e3d282e49e279e` 已失败，`write_result=not-started`，保留原回执；
确认结果后才重新进入 BootROM、创建新任务，没有重放未知结果的存储写入。

成功任务为 `7ca5cc13ec856b1d8b5fa3c6`，尝试 `2e7d308abcb6676906174c0c`，
最终确认计划 `e04a54eb8c12a7db6b8376c3`。

先通过 Windows 读取完整 8,388,608 字节 NOR，再从本次备份提取原 `opensbi`。
预览及确认只选择该分区：偏移 1,769,472，大小 393,216 字节，`write_layout=false`，
`verify=required`。实际经过 `reading-nor`、`checking-nor-baseline`、`writing`、
`verifying-nor`，结果为 `write_result=completed`、`verify_result=verified`，
`verified_bytes=8388608`。

另行下载刷前、刷后完整备份，逐字节一致，SHA-256 均为：

```text
1503579c8f5d1e3e55a93b07faa0dbcdbca6d583661891346ba4973e33927480
```

这证明本轮原内容回写保持了 env、未选分区及未分配尾部。基线使用本轮新读取的数据，
没有复用此前 Ubuntu 验收的 NOR 备份。

刷写后关闭临时 USB 导出、导入、重定向及串口桥接。sub23-1 断电 30 秒再上电，
新串口日志确认原 `6.18.3+deb13+9-k3-uefi` 内核到达 Debian 登录提示。
原 BMC 三个服务的 PID 和 `prod.yaml` SHA-256 与验收前一致。

本轮仅验收一块板的原内容单分区回写。没有将不同版本升级、多分区、整片恢复、其他介质，
或 Windows 物理 FTDI/USB 转串口驱动列为通过。Windows 通过模拟 COM2 接入真实云板数据；
字符流经 BMC 的 UTF-8 数据接口，不覆盖任意二进制串口、实际电气参数或 RTS/DTR 验收。

Windows 虚拟机与应用保留在本机，硬件接入配置另存为诊断记录。主程序正式版仍为 v1.8.0；
本轮修复的是独立刷机组件，未将该组件或 RAM 材料上传到 GitHub。
