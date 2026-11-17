# Changelog

## 1.14.6

- 修复刷完后重新进入下载模式，点击重新检测仍被旧任务身份卡住：明确重新检测或选择 USB 设备时，只恢复进行中及待人工恢复的任务；已结束任务留在历史中，新刷写读取当前连接身份和能力，不复用旧计划或自动开始写入。

## 1.14.5

- 修复 RAM 系统盘整包预览在现场使用 4096 字节扇区时误报 image-invalid：先从封存候选选择实际扇区对应的载荷，再检查范围、对齐和容量；继续拒绝缺失或重复候选，不重解包、不修改 GPT。

## 1.14.4

- 关闭刷机工作区后重开保留当前任务和预览；刷新页面按原请求 ID 或服务端历史恢复，任务列表也可重新打开预览，失败回执继续可见，不自动重提或刷写。
- 预览同时展示各阶段状态、已完成耗时、整体进度与预计剩余时间；按镜像大小提供初步估算，再用本机同执行器的成功预览和实时速率校准，阶段记录保存在既有任务中。

## 1.14.3

- 镜像商店进入 cloud 等子目录时保留 OS、版本／Daily、日期和变体导航，沿原内容目录选择返回上级，不再切回旧目录布局。

## 1.14.2

- 普通 tar／tar.gz／ZIP 和单文件 gzip 解压时恢复全零块为稀疏缓存，保留逻辑长度、摘要和展开配额；Windows 使用原生稀疏文件，不支持时正常写入。不改变板端写入范围和零块内容。
- 镜像商店按操作系统、版本／Daily 与变体组织，默认展示最新 Daily，并过滤不支持的整盘格式。
- 镜像预览显示校验、解压与布局核对进度，支持取消和原请求查询；细分空间不足、缓存缺失及压缩包错误，并保留控制机诊断。

## 1.14.1

- RDP 常用端口重新检测同时检查 3389／3390，连接设置默认勾选信任并固定证书指纹，保存后继续严格核对；分享菜单明确区分全站与当前设备，网页设备链接标明全站权限。
- 远程桌面连接设置直接说明 Debian GNOME 的 GDM 入口账号、串口查看密码命令，以及当前用户桌面共享的独立凭据和端口区别。

## 1.14.0

- 发布 Linux x86_64 与 Windows x64 完整单机包，内置 MCP 运行时、获授权的 goflash 二进制及 K3 RAM 材料；goflash 源码不公开，K1 不具备本版同等预装 RAM 工作区支持。
- Web 与 MCP 共用设备、任务、权限与输入占用；提供同页 VNC／RDP 桌面、截图、连续输入、明确的连接错误和会话恢复。
- 优化完整包／单分区刷机、镜像来源与缓存、备份、可选回读与刷后复位，并在刷前按替换文件长度比较 NOR 摘要。
- 串口历史按连接分段，改名与个人排序更直接；Linux 安装升级保留配置和数据，所有本地产物统一为 `dist/`。
- 组织镜像源改为管理员配置，移除内部平台地址和具体设备记录；公开文档保留通用设计与准确能力边界。

## 1.13.11

- NOR 工作区在刷前按替换文件的实际长度对照板上备份，显示一致／不同、比较字节数及两侧 SHA-256；保留整分区摘要，更换文件后清除旧结论。复用封存备份与原预览计划，不增加板上读取、不改变分区选择或写后校验策略。

## 1.13.10

- 待识别串口在归档面板按连接时间展示当前和历史连接；重新插拔后，旧日志可继续按日期、启动查看、搜索和下载。同名日志分别缓存，不合并为同一板卡，当前连接 Agent 授权不扩大到历史连接。

## 1.13.9

- 远程桌面会话已在板端结束、服务端返回会话不存在时，断开连接按已断开处理并清除本地连接编号，可重新连接或修改连接设置；其他关闭失败仍保留原连接状态。

## 1.13.8

- 远程桌面连接失败保留建连阶段、目标、异常链与堆栈；FreeRDP 返回原生错误名称、数值和说明，区分网络、证书、认证及组件问题。
- 网页显示可复制的错误详情，公共 HTTP 与 MCP 复用同一诊断。原生错误日志只在建连期间有界保留并隐藏凭据；成功后丢弃，不采集后续输入或剪贴板日志。输入未知不重放、证书校验及登录策略保持不变。

## 1.13.7

- Web 顶部显示当前 Web 服务的 DeckLab 版本，复用原访问状态接口。
- 板上摘要明确标注整分区范围及字节数，载荷摘要单独展示。写后 NOR 回读按已确认计划的实际文件长度额外比较摘要，展示一致或不同，保留原整分区、整片摘要和执行器校验结果，不裁剪尾部或改变校验策略。

## 1.13.6

- 整包默认流程为选择镜像、预览、确认刷写；查看存储和备份收进可选入口，复用原整包执行路径，不强制建立 NOR 备份工作区。
- 等待确认显示剩余时间；超时后明确提示尚未写入、镜像与备份保留，并提供重新检测入口。恢复只检测设备，不自动读取或刷写；未知结果的恢复占用保持原限制。

## 1.13.5

- 修复 NOR Firmware 包切换回读校验时错误携带系统盘编号导致解析失败；NOR 不显示或传递系统盘目标，系统盘选择严格接受原生 `scsi:0` 等编号。
- 修复镜像包目录缺失时预览按钮永久禁用；点击后可在原任务显式重新解析并生成只读范围预览，仍需另行确认才刷写。解析期间禁止重复提交，切换设备后丢弃旧响应。

## 1.13.4

- 修复重新打开等待选区任务时“刷完自动复位”无法勾选：只读恢复服务端能力，保留原任务连接代次、镜像、刷写方式及分区选择，不重新探测 USB 或提交刷写。

## 1.13.3

- 修复修改 EEPROM 优先启动介质后丢失系统盘关联的问题；UFS 对应原生 SCSI 设备，唯一匹配时恢复默认查看，多盘或缺失时保留明确选择，不改变刷写范围。
- 兼容旧任务中已回读确认启动介质、却未保存默认盘关联的显示。

## 1.13.2

- 修复读取设备存储后，NOR Firmware 包第二次预览因未复用其完整执行配置而报 `image-invalid`；保持原有严格摘要和范围校验。
- Web、HTTP 及 MCP 预览支持显式选择刷后自动复位；默认关闭，仅在写入及所选校验成功后发送一次原生 Fastboot 重启命令。失败或应答未知不重发，不把复位应答当作 OS 启动成功。
- 回读校验可明确关闭，界面和任务轮询不再强制改回“必须校验”；保留写前文件、范围和设备身份检查。

## 1.13.1

- 已下载列表保留设备内下载及正在使用的镜像，使用中禁止再次选择或删除，同名镜像按独立 ID 保存。
- 复用 OS 预览计划时保留 ESP／rootfs 文件系统与版本信息；刷新不再清空包内信息。
- 解析大包显示不定进度与已用时间，写入显示基于已确认逻辑字节的剩余时间估算，明确区别于 USB 传输量。
- 本地私有刷机执行器的计划写入复用稀疏 FILL 块压缩连续零块和重复字数据，保持清零、扇区补齐与写后校验语义。

## 1.13.0

- 桌面入口固定可见，未配置时直接展示连接表单，自动识别设备已知 IP 上的 VNC／RDP 服务，也支持明确地址和自定义端口检测。
- 新连接复用原生 VNC／FreeRDP；首次观察的 RDP 指纹需明确确认，检测不发送凭据或键鼠输入。
- 连接配置沿用原私有配置文档按设备保存并即时生效，无需重启串口采集器；保留原占用、其他板卡、密码隐藏及权限范围。
- MCP 可在列出桌面会话时选择只读检测；Agent 不能修改配置或选择任意检测地址。

## 1.12.5

- deb 安装后等待 Web 就绪再生成访问链接，避免首次启动稍慢时误报凭证不可读取；区分连接失败、配置读取失败和认证失败。
- 保留既有服务自动启动与升级角色策略，服务启动命令失败时显示对应日志入口；服务账号不需要设置登录密码。

## 1.12.4

- 刷机镜像按实际配置引用的分区表显示目标，板上内容与未选入的附带固件分别展示；不再把 OS 包套入 NOR 选区。
- 读取工作区支持明确选盘的 RAM 系统盘分区更新，分块写入并回读；保留现场 GPT 和 NOR，拒绝未独立核验容量或含糊的多盘目标。
- 整包更新无需逐项勾选，预览复用已导入的载荷；任务轮询不再覆盖刷写方式和源镜像对照。
- MCP 通过现有分区预览接口暴露包目录与指定系统盘，继续沿用原任务、授权、占用和确认机制。

## 1.12.3

- FreeRDP 引擎禁用不经过 TLS 证书校验的 Standard RDP 回退，确保配置的证书指纹或系统信任链校验始终生效；保留 TLS、NLA 与 RDSTLS。

## 1.12.2

- RDP 可使用内置 FreeRDP 引擎，复用成熟图形管线、系统远程登录重定向与 RDSTLS；沿用既有桌面会话、帧更新、MCP、输入占用和撤销。
- 新引擎支持固定服务端证书 DER SHA256，或使用系统证书与主机名校验；凭据和原生依赖仍只在控制机使用。

## 1.12.1

- 桌面离散输入只接受当前调用者最近一次截图；排队期间被新截图替代的旧输入拒绝发送。原请求仍返回原任务，不因截图更新而重发。
- 输入结果未知时清除旧观察，其他页面截图不会重新放行未知操作前的输入。

## 1.12.0

- 网页增加同页连续远程桌面：复用原生 VNC／RDP 更新与 noVNC 显示、键盘组件，支持按住、拖动、组合键、滚轮、文本输入、显式文本剪贴板、缩放和全屏，凭据始终留在控制机。
- 桌面连续输入与 MCP 共用受管连接、设备授权、占用和撤销；失焦、模式变化及断线释放按键，旧观察和不确定输入不自动重放。

- 公共执行合同增加能力协商、稳定请求 ID、原任务回查和独立物理结果；识别、SSH、文件及硬件任务复用持久去重，超时不自动重发。
- 输入占用支持持久板级范围、私密证明、代次与续期；串口重枚举保留，过期／撤销／重启进入恢复，网页可核对后人工恢复。
- 授权撤销增加持久待确认通知和后端栅栏，断网恢复后继续对账；只有原同步请求、worker、会话及恢复资源确实清理后才确认停止。
- MCP 补齐能力及执行占用工具；平台可直接复用标准库客户端和 Python／Go 对接示例，无需使用 MCP。
- Linux 串口读句柄增加合作式排他锁，阻止不同数据目录的 DeckLab 重复采集；保留独立只写入口。
- 按系统、设备、会话与输入范围重排网页入口；全局设置持续可见，维护、显示与输入选项归入具名菜单。补齐图形配置文件字段，修复系统配置窗口无法打开。

## 1.11.1

- 完整包补齐 RDP 客户端动态加载的加密后端，避免源码连接正常但包内连接失败；客户端仍无需安装依赖。
- 修正原生 RDP 滚轮方向，并为不接收 Unicode 按键的服务提供显式文本剪贴板模式；拒绝或超时后不自动重发。

## 1.11.0

- 网页和 MCP 增加按板卡授权的桌面连接、截图、点击、按键、文本输入和滚动；截图直接返回 MCP 图片。沿用输入所有权、后台任务、请求去重和撤销；显示模式变化或输入结果未知后须重新观察。
- MCP 补齐 USB 下载设备发现与 EEPROM 身份、NOR 备份与系统盘信息、镜像商店及上传下载、整包／分区刷写、EEPROM 启动介质修改；复用既有预览、后台任务、请求去重和回读。
- USB 操作按全部设备／单设备授权限制范围；任务、预览及镜像备份归属原调用者，连接授权不扩展为刷机权限，清理不确定时保留恢复锁。
- 支持在设备存储页修改 EEPROM 第二启动介质并回读校验；RAM 刷机器直接读取 EEPROM 序列号，关联已有板卡记录并保留当前任务，冲突时停止。

- K3 NOR 启动场景统一“读取设备存储”：同一任务保留 NOR 整片备份，并限量读取系统盘分区、系统版本及启动信息；不默认导出系统盘镜像。默认介质参考 EEPROM 第二启动配置，无法确认时显示实际存储供选择。
- 刷机工作区直接展示“整包刷写 / 指定分区”，支持时默认整包；整包按包内映射预览全部范围，指定分区增加全选，切回整包不会夹带隐藏选区。
- 镜像来源统一为本地文件、商店、已下载和 URL；选文件即上传、粘贴 URL 即下载，完成后直接选用，失败才显示重试入口。
- 分区预览错误独立保留，任务倒计时不再覆盖报错；调整选区或重试时更新提示。

## 1.10.1

- 设备排序在拖动中实时预览落地位置，相邻卡片自动让位；取消恢复原顺序，落下后才保存当前浏览器排序，轮询不会打断拖动。
- 修正分区文件选择：整包作为镜像来源，分区行按包内映射取载荷；分区文件选择器隐藏整包。商店下载可直接带回当前分区，无需再次选择；单文件不会自动填入多个分区。

## 1.10.0

- 默认完整单机安装：随包提供刷机执行器、匹配的 RAM 加载材料；Windows 同时提供 USB 驱动及安装入口。缺少材料时停止完整包构建，精简包必须显式选择。
- 推荐在 Linux 控制机安装一次，其他电脑使用浏览器。安装和局域网入口提供访问链接，原有分离部署继续兼容。
- 增加公开归档站和可配置组织镜像商店，按 K1/K3 目录浏览、搜索、下载和管理控制机缓存。镜像可以跨设备复用，下载可取消并恢复查看原任务，刷写仍须预览并确认。

## 1.9.0

- 统一板卡控制适配，内置 BMC HTTP 控制，兼容现有固定命令；以后可增加其他复位接口，客户端沿用同一任务协议。
- 网页、CLI 和 MCP 可查询并提交复位、上下电及进入下载模式。网页操作有独立确认；Agent 需管理员明确启用。BMC 凭据只保留在控制机私有文件。
- 控制请求核对固定板卡／模组／USB 映射，丢失回执不重发；任务、取消、串口输入占用和历史沿用原机制。刷机不隐式串联复位。
- 明确 Linux/Windows 单机和 Linux/Windows 上位机加 Linux 控制机两种部署模式；Windows 硬件验收覆盖串口与刷机，复位在 Linux 控制机侧验收。
- 补充 Windows 11 虚拟机与 test-board-b 的实际串口、USB 刷机和整片回读记录，保留真实覆盖范围。主程序公开发布，刷机执行器和 RAM 材料仍单独安装。
- 修复 Linux Web 进程收到停止信号时缺少线程模块导致的退出异常，并验证从已内置 MCP 的正式版升级时继续保留配置和数据。

## 1.8.0

正式安装包包含 DeckLab 主程序、终端运行时及服务端 MCP 依赖；goflash 刷机执行器、
USB 驱动及 FSBL／临时 U-Boot 材料由管理员单独安装，不随本次 GitHub 附件分发。

### 设备与配置

- 未识别的串口也能设置标签；设备列表支持拖动和键盘排序，顺序仅保存在当前浏览器。
- 网页统一配置上位机、板卡与刷机能力，支持预览、保存、导入导出和显式应用；分享配置会隐藏凭据。
- 支持 Linux/Windows 单机；多机模式采用 Linux/Windows 上位机连接专用 Linux 控制机，通过认证 HTTPS 通信，板卡操作、任务和镜像缓存由控制机持有。新安装默认单机，升级保留既有部署角色。

### 镜像与刷机

以下设备刷写能力需在硬件后端安装匹配的独立刷机组件后使用。

- 自动发现 USB 下载设备，按物理连接绑定任务；未识别串口不再阻止进入刷机工作区。
- 支持上传单文件或从 URL 下载镜像；镜像准备、取消和删除均通过原有后台任务及文件配额管理。
- NOR 工作区先备份整片内容，显示分区版本、构建时间和摘要，再选择替换文件、预览并二次确认写入范围。未选分区保持原样，单分区模式不改写分区表、不自动擦除 env。
- 安全 RAM 刷机环境支持预装加载材料；写入前重新核对整片基线，写后自动回读，分别记录写入和校验结果，并保留可下载的备份与回读文件。
- 支持从完整 NOR 包选择分区；完整恢复单独显示 env 擦除等风险，不以完整恢复绕过单分区限制。
- 从实际固件、压缩 FIT 和文件系统内容读取版本信息；系统镜像预览可展示发行版、内核和引导组件。缺少有效标记时显示未知，不以文件名推断版本。
- 刷机界面采用分区对照和阶段字节进度，支持手机、明暗主题与中英文；保留原任务恢复和二次确认，移除重复说明与空白占位。

### 验证范围

- Ubuntu BMC 上的 BOARD-SAMPLE-01 已实测整片 8 MiB 备份、原 opensbi 单分区回写及自动整片回读，刷前刷后内容一致。该结果不代表所有板型、不同版本替换或完整恢复均已实测。
- Windows 的双角色、协议和安装包验收由原生 CI 执行；本轮没有可用的 Windows 硬件测试机，不将模拟设备验收表述为 Windows 真机刷写通过。

## 1.7.4

- Recognize BusyBox ash directory prompts such as `~ #` and `/ #` when
  synchronizing serial terminal dimensions, with the same idle-input and
  ownership checks used for hostname prompts.
- Share backend prompt recognition with internal-output filtering so a
  successful size synchronization does not duplicate the ash prompt.
- Report a distinct disabled-mode error when custom/manual probing prevents
  Linux terminal setup, and verify ash resizing with real BusyBox PTYs in CI.

## 1.7.3

- Remember successful web SSH credentials on the server by board serial, so
  service restarts, USB reconnections and port changes reuse the same account.
- Prefer saved credentials and update them only after a successful web login;
  failed attempts and stale connections preserve the existing configuration.
  Agent connections can reuse saved credentials without replacing them.
- Show saved-account status in SSH settings without returning the password to
  the browser. Unidentified connections remain temporary until a unique board
  identity is established.
- Upgrade the registry to schema 7 with private credential storage and retain
  credentials through explicit board identity corrections and package upgrades.

## 1.7.2

- Try the configured SSH password, then `bianbu` and `debian`, without duplicate
  attempts. Stop on network errors and keep explicitly supplied credentials to
  one authentication attempt.
- Offer SSH username and password entry after authentication fails, with
  connection-scoped retries and credential-free task history.
- Replace automatic root password and sshd changes during SSH connection with
  manual, individually copyable serial-console instructions.
- Document Codex, Claude Code and Orca Web MCP configuration and connection checks.
- This collector update restarts the collector during package upgrade while
  preserving device configuration and recorded runtime data.

## 1.7.1

- Include the MCP SDK, Python runtime and dependencies in architecture-specific
  Debian packages. Install and upgrade without pip or a separately configured
  Python environment; keep the collector on its existing system Python.
- Verify installed remote MCP in clean Ubuntu 22.04 and 24.04 containers with
  networking disabled after package installation.
- Preserve actionable MCP startup errors and bounded worker logs instead of
  reporting every startup failure as a missing SDK.

## 1.7.0

- Keep BusyBox/Buildroot probes below interactive shell input-line limits and
  detect existing SSH services without requiring systemd.
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
- Publish stable installer and Debian-package asset names alongside versioned
  files, so download links can follow the latest release automatically.

## 1.6.5

- Recover a previously duplicated board serial from the Identify action: review
  the existing archive, confirm ownership and provide verification details, then
  read the board identity again before retaining its label and continuing logs.
- Reject stale confirmations and active duplicates. Commit conflict resolution,
  its audit reason and the connection binding together; agent grants cannot
  confirm archive associations.
- Add an offline `resolve-conflict` preview/apply command and a packaged Windows
  administration entry point for verified recovery without changing board serials.
- Close read-only registry connections after archive correction and confirmation,
  including validation failures, so Windows can release the database file.

## 1.6.4

- Restore the controller's approved text width when firmware menus hand off to
  Linux, including Debian kernel banners split across serial reads. Preserve
  this preference across clean collector handoffs without sending board input.
- Retry browser presentation sizing after a shared-screen reset, while keeping
  firmware menu coordinates and avoiding repeated requests for a matching grid.

## 1.6.3

- Treat serial-size synchronization's idle-prompt races as waiting, then clear
  its notice when the shared stream confirms the requested grid is configured.
- Ignore delayed setup failures after success, and preserve newer action notices
  and actual input errors. Notice ownership survives UI language changes.
- Read UTF-8 serial archives explicitly in Windows package validation instead
  of relying on the runner's system encoding.

## 1.6.2

- Verify idle Linux serial consoles hourly with a nonce-framed, side-effect-free
  round trip over the existing collector. Defer while input is owned or busy,
  including firmware menus, full-screen applications and file transfers; show
  persistent device-card warnings when the board does not respond.
- Hide automatic terminal setup and file-transfer protocol echoes from shared
  screens and serial archives. Preserve raw protocol bytes and terminal replies,
  file-transfer summaries, unexpected errors and asynchronous kernel messages.
- Restore shared screen state when internal operations finish, preserving updated
  dimensions, input modes, partial prompts and unfinished terminal controls.

## 1.6.1

- Restore shared terminal snapshots through the ordered parser queue, cancel
  incomplete control strings and repaint even when the source remains quiet.
  Switching back from SSH or archived logs no longer needs an extra Enter.
- Keep unchanged terminal fits stable and use current character measurements
  when scaling a shared grid, avoiding repeated font resets and excessive shrink.
- Exercise quiet serial/SSH/archive switching and interrupted control strings
  with the bundled xterm renderer and disposable local PTYs.

## 1.6.0

- Maintain one canonical terminal per serial connection or SSH session in an
  isolated backend xterm parser. Answer live terminal queries once without a
  browser; history and screen snapshots never send protocol replies.
- Restore the screen, alternate buffer, scrolling margins, saved cursor, input
  modes and fragmented UTF-8/control bytes when viewers join or redraw.
  Share ordered presentation corrections without changing raw captures or logs.
- Coordinate shared SSH input and PTY dimensions by actor and browser client.
  Push quiet takeover changes to observers; notify running applications of
  controller resizing. Observers scale the same logical grid.
- Give the Linux sshpass wrapper a controlling PTY so window changes reach SSH
  applications; use an exec helper instead of a threaded preexec callback.
- Finish idle serial shell size/environment setup before queued input. Keep
  active serial applications on their established grid; preserve the screen on
  confirmed re-enumeration and clean same-connection collector handoff.
- Declare Ubuntu's Node runtime dependency and bundle a verified Windows Node
  runtime with its complete upstream license. Add real Vim/local-PTY two-browser
  regression checks and installed-parser checks on supported Ubuntu baselines.

## 1.5.8

- Suppress the far-bottom cursor move used by systemd size probes whenever
  terminal replies are suppressed, keeping boot messages adjacent without
  removing real blank lines or changing firmware menu cursor controls.
- Apply UART geometry outside captured bytes so fragmented OSC/DCS controls,
  shell context metadata and UTF-8 characters remain intact. Wait for queued
  writes and terminal reset before applying replay geometry.
- Send environment setup only after the resize marker and a fresh idle shell
  prompt, preventing the longer setup line from being echoed at the old width.

## 1.5.7

- Clear firmware background, cursor and scroll modes on the earliest kernel
  timestamp, including split headers without a Linux version banner.
- Replay the shared UART geometry from the first captured byte, with subsequent
  resize and APT controls applied in stream order. Smaller viewers scale pixels
  instead of changing logical rows or remapping progress coordinates.
- Fit the complete shared grid to the real viewport with bottom/scrollbar
  clearance. Establish board dimensions before echoing environment setup; run
  the longer environment command only initially or on explicit retry.

## 1.5.6

- Fit early boot and kernel text to the viewport; retain firmware menu coordinates
  and restore normal geometry on Linux boot.
- Automatically synchronize serial shell dimensions at a verified idle prompt.
  Coordinate one sizing authority per connection, preserve shared geometry beyond
  live-buffer eviction, and block stale, typing, owned and busy-console requests.
- Configure visible control-character echo, disable software flow control only
  when the serial profile permits it, enable UTF-8 editing for UTF-8 locales, and
  select xterm-256color only when matching board terminfo is installed.

## 1.5.5

- Add explicit idle-shell size synchronization for wide serial terminals.
  Enable visible control-character echo with `stty echoctl` so echoed arrows
  cannot move the output cursor during installation; retain keyboard passthrough.
  Apply shared resize markers only after successful board-side `stty`; preserve
  dimensions across browser viewers and buffered replay.

## 1.5.4

- Preserve the large live serial viewport while mapping APT's reserved progress
  row and scroll region to its bottom. Restore full-screen scrolling when APT
  finishes, including streamed escape sequences and redraws. Captured logs remain
  unchanged.

## 1.5.3

- Allow vertical wheel scrolling over boot comparison text and inline highlights
  while preserving horizontal scrolling for long lines.
- Provide English and Chinese self-hosted agent skills. Generate handoff text and
  download URLs in the selected UI language; reuse the same grant when switching.

## 1.5.2

- Add live Chinese/English UI switching without reconnecting terminals or changing
  browser input ownership. Honor the Windows installer language as a preserved
  host default; explicit browser preferences take priority.
- Push serial input reservation/release changes through SSE even without new
  output, including metadata packets at unchanged byte offsets.
- Expand boot comparison to nearly the whole viewport and highlight changed
  Unicode characters within lines while ignoring kernel clock differences.
- Echo simulated serial/SSH input before Enter and support deletion and Ctrl-C.
- Rename agent sharing and clarify that web board links grant platform-wide
  access; retain actual single-board scope for agent grants.

- Add an English architecture diagram, demo screenshot and explicit K3 hardware
  validation scope. Consolidate documentation navigation and remove completed
  plans, duplicate fix histories and obsolete generated artifacts.

## 1.5.1

- Add reviewed Windows Rust source-graph notices and CFFI/libffi source headers.
  Pin the catalog to three observed frozen provider hashes; reject stale source
  notices and changed binaries instead of silently reusing old attribution.
- Include observed Rust standard-library copyright summaries and Paramiko's
  corresponding source archive. Allow replacing Paramiko from an explicit source
  archive in the isolated builder and retain that same archive in the output.

## 1.5.0

- Run native Windows package, installer upgrade and simulated-host checks in
  GitHub Actions; fix Unicode pipe output, SQLite backup flushing and Windows
  test-file cleanup discovered by native execution.
- Ship hash-pinned OpenSSL licenses selected from actual frozen provider versions,
  plus reviewed CPython source dependency notices. Preserve upstream notice bytes
  across Windows checkouts and verify both provider and notice hashes.
- Wait for the browser debugging endpoint and retain bounded startup diagnostics
  in isolated UI checks. Publish this changelog section with tagged draft packages.

- Add a disposable cloud-image VM check for actual systemd startup, collector
  fingerprint upgrade decisions and purge/data preservation. Verify the image
  against the supplied provider SHA-256; use no physical hardware or host mounts.
- Add disposable Ubuntu 22.04/24.04 package installation checks covering actual
  dpkg upgrade/purge, conffile/runtime preservation, service-account API/CLI and
  independent control restart. No host serial devices or service changes.
- Record an initial Windows native-wheel attribution audit with exact source,
  binary-member and notice hashes. Ship the observations and remaining review
  steps with documentation; do not treat them as frozen-output verification.
- Complete the local API/configuration content review. Document task capacity,
  cancellation outcomes, artifact retries and expiry separately from retained
  task history; distinguish local contract checks from native release evidence.
- Keep collector health readable when disk status is unavailable, and snapshot
  SSH sessions under their registry lock before collecting logging errors.
- Snapshot board-profile environment defaults and configuration at collector
  startup. Later identity/port selection cannot silently change credentials,
  probing policy or framing; recreate the collector to apply changes.
- SSH root provisioning uses the configured board-profile password instead of a
  hardcoded password. Non-root/empty-password profiles and different per-request
  credentials cannot trigger root provisioning.
- Remove import-time collector Manager initialization. Linux, Windows and demo
  entry points inject one manager into each HTTP server, isolating device state
  and mutations; unconfigured servers return unavailable without lazy startup.
- Extract serial archive writing into a transport-independent component owning
  UTF-8 decoding, partial lines, rotation, file handles and write-error recovery.
  Keep live byte fan-out and identity/retention lock ordering in the connection.
- Inventory and verify all Windows distribution files before ZIP/installer
  packaging, including native libraries, resources, scripts and notices. Reject
  changed, missing or extra files and refuse runtime data or redirected paths.
- Share request JSON validation between control and collector. Reject duplicate
  fields, non-finite numbers, invalid Unicode and excessive nesting before board
  operations; direct task/artifact services also reject non-object submissions.
- Bound POSIX hardware-helper cleanup and verify process-group/thread exit before
  restoring exclusive UART capture, including successful root-before-child exit.
  Failed verification keeps the attachment paused for operator recovery.
- Document host/authentication settings and web-issued agent restrictions,
  including SSH provisioning behavior. Check public route/environment name
  coverage offline during CI and Windows builds without importing collectors.

- Recreate a dedicated Windows packaging environment on every build, keeping
  development environments untouched and excluding leftover development packages
  from dependency and license collection. Installer verification uses the same
  packaging interpreter.

- Run identify, SSH and copy/upload/download as collector-owned durable tasks
  through API, CLI and browser. Preserve progress/results across page reloads;
  check actor, board, client and connection generations before writes. CLI
  foreground commands wait on the same tasks as background commands, and later
  clients can retrieve verified download artifacts.

- Registry schema 5 includes task history, scoped transfer artifacts and proven
  connection-to-board associations. Interrupted work is recorded without replay;
  identity promotion preserves earlier task visibility without guessing ownership.

- Add configured power/reset/flash helpers with fixed argument lists, task
  cancellation and process cleanup. Exclusive helpers pause only their UART;
  capture resumes after cleanup, with unproven cleanup keeping that UART paused.

- Extract console login, command framing and SSH provisioning into a protocol
  service that uses the connection's existing reader and guarded writes.

- Ship API/configuration guides with validated links in both package layouts.
  Windows builds collect and verify dependency license files; native binary
  attribution and actual Windows/GitHub release execution remain release gates.

- Extract device directory, selection and identity transitions into a controller.
  Out-of-order device responses and failures cannot replace newer directory or
  connection status; ownership revision protection remains intact.

- Give terminal views an independent lifecycle controller for generation guards,
  disposal, streaming/poll fallback, ordered input and firmware/PTY rendering.

- Extract archive selection, list refresh and all-boot search into a controller;
  detached archive controls and superseded search buttons cannot change the
  current device or open stale results.

- Extract terminal-buffer search, echoed-command navigation and highlighting
  into an injected module, retaining view-generation checks for stale callbacks.

- Add opt-in daily archive retention with active-writer/running-boot protection,
  disk-space warnings and allowlisted diagnostic exports. Retention is disabled
  by default; metadata and database backups are never pruned by this policy.

- Distinguish human input reservations by page client ID while keeping the web
  audit actor. Separate clients must explicitly take over; transfer commands keep
  their originating client identity across background work.

- Add optional actor-scoped serial input reservations, CLI claim/release and
  browser human takeover. Spectators retain output access and independent SSH;
  agent revocation and connection replacement release reservations.

- Extract session/archive queries from HTTP handling and share result/error codes
  with control authentication. Keep boot partition caches intact when listing;
  reject archive links, nonregular files and replacement races across readers.

- Separate device write actions from HTTP handling into injected application
  services. Add protocol capabilities and CLI operation status/cancellation;
  action errors expose stable codes while preserving existing response fields.

- Version registry and agent-grant schemas; preserve legacy data with automatic
  pre-upgrade WAL-aware backups, atomic migrations and future-version rejection.

- Release transfer expiry timers immediately after completion, abort or expiry,
  so repeated transfers do not accumulate ten-minute timer threads.

- Coordinate console input, commands, provisioning, transfer and recovery with
  total deadlines and connection-generation guards. Expose owner-checked,
  ID-scoped cancellation without closing the shared serial reader.

- Share SSH lifecycle, streaming, log rotation and audit across Linux/Windows IO
  adapters. Relocate live SSH log writers during identity promotion, preserving
  partial UTF-8 and resuming the correct directory after migration failures.

- Separate persistent board identities and SSH ownership from replaceable UART
  connections. Preserve session registries across re-enumeration and release
  in-flight SSH reservations against their original board after a swap.

- Add an isolated hardware-free demo with simulated boards, cross-day boot logs,
  SSH, agent commands and virtual file transfers through the normal API.

- Add validated serial framing and per-board/port profiles, separate console and
  SSH credentials, and a manual console mode without Linux probes or SSH
  provisioning. Include configuration instructions and examples in packages.

- Add public-project licensing, contributor guidance and bilingual overview.
- Configure Linux/Windows PR checks and tagged draft release artifacts.
- Include application license notices in Ubuntu and Windows distributions.
- Use the source checkout as the default Linux development data root.
- Fit tall firmware menus on mobile and retain terminal space beside records.

## 1.4.50

DeckLab branding and independent desktop/Start-menu ICO resources.
Earlier development changes and investigations remain available in Git history.
