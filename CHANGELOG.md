# Changelog

记录 DeckLab 公开发布版本的重要变化。[历史构建记录](docs/CHANGELOG_ARCHIVE.md)保留其余版本的原说明。

本文件遵循 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)，公开版本使用[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.18.0] - 2026-11-25

### Added

- 板卡用例库可浏览 TitanX 用例组、搜索用例并查阅步骤和预期结果。已关联工位可下发已评审的自动化用例，按原任务和执行设备核对逐轮回执。
- 控制机可向同机测试执行器返回本次连接已核实的 ADB 物理端口和板卡启动编号。

- 设备页提供独立的 ADB 终端、受管命令与串口记录。可按板卡同时打开多个会话，通过网页、CLI 或 MCP 读写。
- 本地用例支持逐条 Shell 命令、预期串口输出、超时和按轮浏览日志。板卡自动匹配 USB 调试接线，内置深度挂起用例支持 K1 与 K3。
- 远程桌面可通过已确认的 USB 调试连接打开 Debian GNOME 首次设置和 Bianbu LXQt 登录屏，MCP 支持按画面坐标拖动。

### Changed

- 开发板尚未进入下载模式时，刷机和内存启动可在当前工作区绑定控制槽位并继续原操作；URL 刷机同时显示下载和写入进度。
- 识别到 Buildroot 且确认 SSH 服务运行后，可直接使用镜像默认账号连接。重连后重新核对服务状态。

### Fixed

- K3 NOR 工作区可单独选择 FSBL 文件核对写入范围。
- 手机上打开历史日志后，板卡操作按钮可横向滑动，为串口输出保留可读空间。
- 串口回车、粘贴和手机短距离滚动更加稳定；远程桌面通过控制机的其他网卡访问后可直接重连。

## [1.17.74] - 2026-11-24

### Added

- 极速刷机商店可选用独立 Firmware 包，下载后进入普通刷机的 NOR 确认流程。已配对的固件也可以单独选用。
- 普通刷机、URL 极速刷机和内存启动记录实际发出的 Fastboot 命令、设备应答和加载文件。可以复制记录，也可以重开任务查看。
- URL 极速刷机的镜像商店显示配套 Firmware。同一芯片与 Debian 发行版只有一份匹配固件时，可跨构建日期配对。极速刷机覆盖系统盘，发现包内 NOR 分区表时会提示已跳过。需要更新 NOR 时，可从同一镜像位置进入普通刷机。
- URL 极速刷机可直接检测手动进入下载模式的设备、绑定控制槽位，以及读取并选择目标系统盘。
- 板卡页新增用例库，内置软件重启老化与深度挂起和 RTC 唤醒，支持后台运行、逐轮结果和异常串口日志下载。
- 新接入的板卡可从 Home Assistant 控制槽位进入下载模式，识别后再保存接线。

### Changed

- 从设备页打开刷机工作区时，顶部显示当前开发板和准备操作，普通刷机可直接进入下载模式并读取存储。首页入口仍优先显示镜像选择。
- 镜像工作目录改为文件列表与预览并排，JSON、YAML 等文本使用随包提供的 Ace 编辑器。
- 刷机详情先显示当前任务身份、阶段和最近的关键操作，完整命令与应答可展开或复制。
- 组织镜像仓库可填写 Nexus 根地址，由 DeckLab 探测浏览与下载入口。配置的组织仓库支持自签名证书。
- 工位接线沿用控制器槽位编号，从 0 开始。换板同时解除原串口、刷机 USB 和控制槽位关联，离线旧板的槽位可在核对后重新绑定。
- 新绑定默认允许 AI，在保存前可以取消。
- 加载器失败且未开始写入时，可在刷机工作区重新检测设备或确认重新进入下载模式，已选镜像可继续使用。
- 按 Keep a Changelog 1.1.0 整理公开版本说明。

### Fixed

- 串口终端尺寸同步提示浮于终端上方，显示和清除时不再改变终端高度。
- Debian 13 的板卡用例可正常创建临时服务。收到服务拒绝时显示原错误并释放板卡；触发结果未知时继续观察。
- RTC 唤醒用例只按板卡醒着的时间计算取证窗口，给唤醒后的内核日志留出写入时间。
- 读取设备存储时接收执行器的命令与应答记录，避免将这类记录误判为无效事件并中止读取。
- 单刷 NOR 固件时隐藏系统盘选择，固件预览不再继承上次的目标系统盘。清除待刷选择和删除镜像缓存有独立按钮，删除确认紧跟在镜像旁。
- 修复临时加载器从 USB 2 切到 USB 3 后，目标口存在历史任务导致刷机无法继续的问题。
- K3 读取存储时单独标记 MMC 初始化失败，保留已识别的 UFS 和 NVMe。
- 修复网页串口和 SSH 在 Vim 粘贴多行文本时出现的额外空行和缩进。

## [1.17.54] - 2026-11-23

### Changed

- 系统配置按日常任务分类，分别显示未保存、待重启和已生效，导入、导出与私密备份有独立入口。

### Fixed

- 保存单项设置会保留其他项目对启动默认值和外部配置文件的继承，修复分离部署中板卡后端图形配置无法保存的问题。

## [1.17.53] - 2026-11-23

### Changed

- Windows 安装器检测到兼容的 USB 刷机驱动后，跳过驱动安装选项及管理员确认。

## [1.17.52] - 2026-11-22

### Added

- URL 极速刷机可从镜像商店选择系统包并填入链接，确认后开始流式刷写。

### Fixed

- 镜像准备达到缓存限额时回收空闲解包缓存并重试，保留镜像包和编辑工作目录。

## [1.17.51] - 2026-11-22

### Added

- 普通刷机、URL 极速刷机和内存启动提供独立入口。普通刷机可从控制机导入已有压缩包或解压目录，内存启动也可导入已有系统镜像包。
- 支持 K1 MUSE Pi Pro 和 K3 Pico-ITX 从原始系统镜像启动 RAM 系统。刷写按加载器能力选择 gzip 或原样传输，K3 沿用原厂 USB 协商。
- 可通过工位控制器批量绑定板卡，也可为单块板重新绑定接线。
- 远程桌面支持 Ctrl+C／Ctrl+V 双向复制粘贴文本，RDP 可对同一地址和端口使用当前证书。

### Fixed

- 修复快速切换串口终端时偶发的滚动区报错。

## [1.17.40] - 2026-11-20

### Added

- 新增「刷写配置」：在 K3 板上实测 USB 下载、板上解压、写盘、命令开销和控制机压缩速度，约 10 秒。按耗时模型选出编码、压缩级别、段大小和是否后台写盘，保存为该 USB 口的配置。之后经此 USB 口的普通刷写和极速刷写都使用这套配置；没有自己配置的 USB 口沿用同型号最近一次实测。写盘测速读出 128 MiB 后原样写回，盘上内容不变。配置只影响速度，不改变写入内容。
- 刷写结果记录本次使用的配置。[刷写耗时模型](docs/FLASH_PERFORMANCE.md#实测生成刷写配置) 说明测量方法、选择规则和 K3 实测结果。

### Fixed

- 修复极速刷写完成后不释放板子和 USB 口占用的问题：此前要先确认恢复才能再次刷写或读取。

## [1.17.39] - 2026-11-20

### Added

- 内置 K3 临时 U-Boot 由供应商 OpenSBI 启动、以 S 模式运行，新增 `unzstd` 与 `bgblk` 两条命令（补丁随包附带）。旧加载器或自定义加载器自动退回 gzip 与同步写盘。
- [刷写耗时模型](docs/FLASH_PERFORMANCE.md) 加入并行解压、写盘重叠和 K1 实测参数（解压与 eMMC 速率），可按 USB、介质和 CPU 条件选择编码和段大小。

### Changed

- K3 刷写系统盘再提速：数据段在控制机切成独立 zstd 帧，板上用全部空闲核并行解压（约 1.2 GB/s，原单核 gzip 约 0.25 GB/s）。写盘在另一个核上进行，同时接收下一段，下一次写盘前确认上一次的结果。从 BootROM 起算，K3 开发板（NVMe，USB 2.0）同一 Bianbu Minimal 镜像：
  - 极速刷写 20.5 秒（含网络下载），1.17.38 为 32.8 秒，goflash 只刷系统盘为 49.6 秒；
  - 普通刷写装入环境加写盘约 21 秒，另需下载和准备镜像。
- 内置 K3 FSBL 在 USB 下载模式下复用板子保存在启动介质上的 DDR 训练结果，按正常启动的规则校验，不写板子。装入临时环境由约 10 秒降到约 5 秒。板上没有有效训练数据时照常训练。若板子停在这一步，DeckLab 提示重新进入下载模式；该 USB 口下一次改用每次都完整训练的原 FSBL。
- 普通刷写在后台提前读取载荷，稀疏镜像的空洞不再读盘；写入数据段由 256 MiB 降为 128 MiB，控制机内存峰值约 1.5 GB。

## [1.17.38] - 2026-11-20

### Added

- 新增[刷写耗时模型](docs/FLASH_PERFORMANCE.md)：列出 USB 速率、压缩比、板上解压、介质写入和命令开销等变量，可按新条件估算耗时并选择参数。

### Changed

- K3 普通刷写与极速刷写使用同一套写盘流程：数据段在控制机 gzip 压缩，板上解压、核对长度后写盘；每段按实测 USB 和解压速率自动选择压缩或原样发送。主机压缩与板上下载、写盘流水并行，首段较小以尽早开始写入。
- 内置 K3 临时 U-Boot 启用原生 `CONFIG_PCI_INIT_R`，PCIe 只在启动时枚举一次，每条 NVMe 写命令的固定开销由约 1.1 秒降到约 6 毫秒；源码仍未修改。
- 普通刷写在写系统盘前不再把全部载荷重新读一遍计算摘要，改为写入时逐字节计算并核对，摘要不符时任务失败。
- 控制机压缩跟不上时自动降低压缩级别；极速刷写结果附带 USB、解压、写盘的实测速率。
- K3 开发板（NVMe，USB 2.0）同一 Bianbu Minimal 镜像从 BootROM 起算：极速刷写 32.8 秒（含网络下载），goflash 仅系统盘 49.6 秒；普通刷写写盘阶段 22.5 秒（1.17.37 为 118 秒）。

## [1.17.33] - 2026-11-19

### Added

- Linux amd64 和 Windows x64 安装包均内置 goflash 及 K1、K3 临时加载器。K1 对应源码可通过 [DeckLab Issues](https://github.com/qianqiu-2020/decklab/issues) 免费索取，详见包内 `flash-loaders/k1/SOURCE-OFFER.txt`。
- 镜像商店按系统、版本和变体浏览，可复用已下载的镜像。支持提前准备镜像、从控制机目录导入，以及组合选择 Firmware 和 OS。
- 关闭已完成的刷写后，再打开工作区进入准备页；进行中与待恢复任务继续保留。历史结果显示该次实际写入量。

### Changed

- K1、K3 系统盘整包刷写按镜像重建 GPT。预览可对照包内布局、板上布局和预计写入范围，刷写时显示当前分区和速率。
- 同一 USB 连接上确认整包范围后，执行阶段直接访问已选目标。K3 的后续分区复用刚读回的新 GPT。
- 设备列表支持个人排序、筛选和自定义切换快捷键；SSH 和远程桌面设置可配合串口使用。

## [1.14.0] - 2026-11-17

### Added

- 发布 Linux x86_64 与 Windows x64 完整单机包，内置 MCP 运行时、获授权的 goflash 二进制及 K3 RAM 材料；goflash 源码不公开，K1 不具备本版同等预装 RAM 工作区支持。
- Web 与 MCP 共用设备、任务、权限与输入占用；提供同页 VNC／RDP 桌面、截图、连续输入、明确的连接错误和会话恢复。

### Changed

- 优化完整包／单分区刷机、镜像来源与缓存、备份、可选回读与刷后复位，并在刷前按替换文件长度比较 NOR 摘要。
- 串口历史按连接分段，改名与个人排序更直接；Linux 安装升级保留配置和数据，所有本地产物统一为 `dist/`。
- 组织镜像源改为管理员配置，移除内部平台地址和具体设备记录；公开文档保留通用设计与准确能力边界。

## [1.9.0] - 2026-11-14

### Added

- 统一板卡控制适配，内置 BMC HTTP 控制，兼容现有固定命令；以后可增加其他复位接口，客户端沿用同一任务协议。
- 网页、CLI 和 MCP 可查询并提交复位、上下电及进入下载模式。网页操作有独立确认；Agent 需管理员明确启用。BMC 凭据只保留在控制机私有文件。

### Changed

- 明确 Linux/Windows 单机和 Linux/Windows 上位机加 Linux 控制机两种部署模式；Windows 硬件验收覆盖串口与刷机，复位在 Linux 控制机侧验收。
- 补充 Windows 11 虚拟机与 test-board-b 的实际串口、USB 刷机和整片回读记录，保留真实覆盖范围。主程序公开发布，刷机执行器和 RAM 材料仍单独安装。

### Fixed

- 修复 Linux Web 进程收到停止信号时缺少线程模块导致的退出异常，并验证从已内置 MCP 的正式版升级时继续保留配置和数据。

### Security

- 控制请求核对固定板卡／模组／USB 映射，丢失回执不重发；任务、取消、串口输入占用和历史沿用原机制。刷机不隐式串联复位。

## [1.8.0] - 2026-11-14

### Added

- 未识别的串口也能设置标签；设备列表支持拖动和键盘排序，顺序仅保存在当前浏览器。
- 网页统一配置上位机、板卡与刷机能力，支持预览、保存、导入导出和显式应用；分享配置会隐藏凭据。
- 支持 Linux/Windows 单机；多机模式采用 Linux/Windows 上位机连接专用 Linux 控制机，通过认证 HTTPS 通信，板卡操作、任务和镜像缓存由控制机持有。新安装默认单机，升级保留既有部署角色。
- 自动发现 USB 下载设备，按物理连接绑定任务；未识别串口不再阻止进入刷机工作区。
- 支持上传单文件或从 URL 下载镜像；镜像准备、取消和删除均通过原有后台任务及文件配额管理。
- NOR 工作区先备份整片内容，显示分区版本、构建时间和摘要，再选择替换文件、预览并二次确认写入范围。未选分区保持原样，单分区模式不改写分区表、不自动擦除 env。
- 安全 RAM 刷机环境支持预装加载材料；写入前重新核对整片基线，写后自动回读，分别记录写入和校验结果，并保留可下载的备份与回读文件。
- 支持从完整 NOR 包选择分区；完整恢复单独显示 env 擦除等风险，不以完整恢复绕过单分区限制。
- 从实际固件、压缩 FIT 和文件系统内容读取版本信息；系统镜像预览可展示发行版、内核和引导组件。缺少有效标记时显示未知，不以文件名推断版本。
- 刷机界面采用分区对照和阶段字节进度，支持手机、明暗主题与中英文；保留原任务恢复和二次确认，移除重复说明与空白占位。

### Changed

- 正式安装包包含 DeckLab 主程序、终端运行时及服务端 MCP 依赖；goflash 刷机执行器、USB 驱动及 FSBL／临时 U-Boot 材料由管理员单独安装，不随本次 GitHub 附件分发。
- 以下设备刷写能力需在硬件后端安装匹配的独立刷机组件后使用。
- Ubuntu BMC 上的 BOARD-SAMPLE-01 已实测整片 8 MiB 备份、原 opensbi 单分区回写及自动整片回读，刷前刷后内容一致。该结果不代表所有板型、不同版本替换或完整恢复均已实测。
- Windows 的双角色、协议和安装包验收由原生 CI 执行；本轮没有可用的 Windows 硬件测试机，不将模拟设备验收表述为 Windows 真机刷写通过。

## [1.7.4] - 2026-11-11

### Fixed

- Recognize BusyBox ash directory prompts such as `~ #` and `/ #` when
  synchronizing serial terminal dimensions, with the same idle-input and
  ownership checks used for hostname prompts.
- Share backend prompt recognition with internal-output filtering so a
  successful size synchronization does not duplicate the ash prompt.
- Report a distinct disabled-mode error when custom/manual probing prevents
  Linux terminal setup, and verify ash resizing with real BusyBox PTYs in CI.

## [1.7.3] - 2026-11-11

### Added

- Remember successful web SSH credentials on the server by board serial, so
  service restarts, USB reconnections and port changes reuse the same account.
- Show saved-account status in SSH settings without returning the password to
  the browser. Unidentified connections remain temporary until a unique board
  identity is established.

### Changed

- Upgrade the registry to schema 7 with private credential storage and retain
  credentials through explicit board identity corrections and package upgrades.

### Security

- Prefer saved credentials and update them only after a successful web login;
  failed attempts and stale connections preserve the existing configuration.
  Agent connections can reuse saved credentials without replacing them.

## [1.7.2] - 2026-11-11

### Added

- Offer SSH username and password entry after authentication fails, with
  connection-scoped retries and credential-free task history.

### Changed

- Try the configured SSH password, then `bianbu` and `debian`, without duplicate
  attempts. Stop on network errors and keep explicitly supplied credentials to
  one authentication attempt.
- Document Codex, Claude Code and Orca Web MCP configuration and connection checks.
- This collector update restarts the collector during package upgrade while
  preserving device configuration and recorded runtime data.

### Security

- Replace automatic root password and sshd changes during SSH connection with
  manual, individually copyable serial-console instructions.

## [1.7.1] - 2026-11-11

### Added

- Include the MCP SDK, Python runtime and dependencies in architecture-specific
  Debian packages. Install and upgrade without pip or a separately configured
  Python environment; keep the collector on its existing system Python.

### Changed

- Verify installed remote MCP in clean Ubuntu 22.04 and 24.04 containers with
  networking disabled after package installation.

### Fixed

- Preserve actionable MCP startup errors and bounded worker logs instead of
  reporting every startup failure as a missing SDK.

## [1.7.0] - 2026-11-11

### Added

- Configure custom POSIX probe scripts or manual consoles in the browser.
  Identification remains optional for terminal access; no synthetic serial is
  created when an operating system cannot report a hardware identity.
- Share an unidentified live connection with an AI using a revocable grant that
  expires on connection replacement or board switching.
- Add remote MCP access through the DeckLab service with revocable per-client
  grants. AI clients configure a URL and Bearer token; no client-side adapter
  installation is required. Keep stdio available for local integration.
- Generate remote MCP configuration in the browser and expose bounded terminal
  reads, input, SSH execution, task operations and boot history through the
  protected control API.

### Changed

- Publish stable installer and Debian-package asset names alongside versioned
  files, so download links can follow the latest release automatically.

### Fixed

- Keep BusyBox/Buildroot probes below interactive shell input-line limits and
  detect existing SSH services without requiring systemd.

## [1.6.5] - 2026-11-03

### Added

- Recover a previously duplicated board serial from the Identify action: review
  the existing archive, confirm ownership and provide verification details, then
  read the board identity again before retaining its label and continuing logs.
- Add an offline `resolve-conflict` preview/apply command and a packaged Windows
  administration entry point for verified recovery without changing board serials.

### Fixed

- Close read-only registry connections after archive correction and confirmation,
  including validation failures, so Windows can release the database file.

### Security

- Reject stale confirmations and active duplicates. Commit conflict resolution,
  its audit reason and the connection binding together; agent grants cannot
  confirm archive associations.

## [1.6.1] - 2026-10-14

### Changed

- Exercise quiet serial/SSH/archive switching and interrupted control strings
  with the bundled xterm renderer and disposable local PTYs.

### Fixed

- Restore shared terminal snapshots through the ordered parser queue, cancel
  incomplete control strings and repaint even when the source remains quiet.
  Switching back from SSH or archived logs no longer needs an extra Enter.
- Keep unchanged terminal fits stable and use current character measurements
  when scaling a shared grid, avoiding repeated font resets and excessive shrink.

## [1.5.3] - 2026-10-12

### Added

- Provide English and Chinese self-hosted agent skills. Generate handoff text and
  download URLs in the selected UI language; reuse the same grant when switching.

### Fixed

- Allow vertical wheel scrolling over boot comparison text and inline highlights
  while preserving horizontal scrolling for long lines.

[Unreleased]: https://github.com/qianqiu-2020/decklab/compare/v1.18.0...HEAD
[1.18.0]: https://github.com/qianqiu-2020/decklab/compare/v1.17.74...v1.18.0
[1.17.74]: https://github.com/qianqiu-2020/decklab/compare/v1.17.54...v1.17.74
[1.17.54]: https://github.com/qianqiu-2020/decklab/compare/v1.17.53...v1.17.54
[1.17.53]: https://github.com/qianqiu-2020/decklab/compare/v1.17.52...v1.17.53
[1.17.52]: https://github.com/qianqiu-2020/decklab/compare/v1.17.51...v1.17.52
[1.17.51]: https://github.com/qianqiu-2020/decklab/compare/v1.17.40...v1.17.51
[1.17.40]: https://github.com/qianqiu-2020/decklab/compare/v1.17.39...v1.17.40
[1.17.39]: https://github.com/qianqiu-2020/decklab/compare/v1.17.38...v1.17.39
[1.17.38]: https://github.com/qianqiu-2020/decklab/compare/v1.17.33...v1.17.38
[1.17.33]: https://github.com/qianqiu-2020/decklab/compare/v1.14.0...v1.17.33
[1.14.0]: https://github.com/qianqiu-2020/decklab/compare/v1.9.0...v1.14.0
[1.9.0]: https://github.com/qianqiu-2020/decklab/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/qianqiu-2020/decklab/compare/v1.7.4...v1.8.0
[1.7.4]: https://github.com/qianqiu-2020/decklab/compare/v1.7.3...v1.7.4
[1.7.3]: https://github.com/qianqiu-2020/decklab/compare/v1.7.2...v1.7.3
[1.7.2]: https://github.com/qianqiu-2020/decklab/compare/v1.7.1...v1.7.2
[1.7.1]: https://github.com/qianqiu-2020/decklab/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/qianqiu-2020/decklab/compare/v1.6.5...v1.7.0
[1.6.5]: https://github.com/qianqiu-2020/decklab/compare/v1.6.1...v1.6.5
[1.6.1]: https://github.com/qianqiu-2020/decklab/compare/v1.5.3...v1.6.1
[1.5.3]: https://github.com/qianqiu-2020/decklab/releases/tag/v1.5.3
