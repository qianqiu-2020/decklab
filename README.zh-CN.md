# DeckLab

[English](README.md) · [文档目录](docs/README.md) · [贡献指南](CONTRIBUTING.md) · [HTTP 接口](docs/API.md)

DeckLab是面向 OS/BSP 开发的开发板工作台：持续采集串口，统一提供网页终端、SSH、历史日志和 Agent 接口。支持 Ubuntu 服务安装及 Windows 上位机。底层项目、命令与服务名保留 `seriald`。

![DeckLab Demo：共享终端、设备列表与启动日志归档](docs/decklab-demo-zh.png?v=ui-1.5.2)

真实应用界面，使用模拟设备，无需接入硬件。Demo 中的设备不代表对应版型已经实测支持。

## 为什么使用 DeckLab

按日常排障的影响排序，它解决了单独使用 minicom 时的这些问题：

1. **多板身份和日志不再跟着 tty 编号漂移。** `/dev/ttyUSB*` 重枚举后可能换号；seriald 将用户标签绑定到板卡序列号，再关联当前串口位置，按序列号保存连续档案，设备列表能直接找到目标板和当前串口。
2. **启动记录可追溯。** 串口输出自动归档，按日期和启动次数浏览；板子跨天运行或一天重启多次，也能定位某一次启动，不必依赖手动保存终端滚屏。
3. **BSP 升级失败时快速找差异。** 对比同一块板升级前后，或两块板的启动日志，突出新增、缺失和变化的内容，帮助定位启动停在哪一步。
4. **人、同事和 Agent 可同时看同一块板。** 服务独占串口采集，再把实时终端、SSH 会话和操作记录共享到网页与 CLI；可生成访问链接或限定设备的 Agent 授权，无需让多个 minicom 抢同一个设备节点。
5. **板子没有网络也能传文件。** 板端有可用 shell 时，通过已有串口连接上传、下载或在两块板间中转文件，并校验大小和哈希。
6. **USB 串口断连可尝试软件恢复。** Ubuntu 上位机可执行 USB 软解绑/重绑定，减少反复手动拔插。

设备身份优先使用 EEPROM ONIE 序列号，并兼容 vendor/upstream 内核的不同暴露方式；有效 DT `serial-number` 也直接作为正式编号；`key` 是用户标签（如 `riscv-118`、`musepi2`）。待确认的 SoC/eFuse 与 `/boot` 身份保存在 `logs/_provisional/`；没有编号时按 USB 连接隔离。标签、USB 插口和 tty 节点可改变。

## 支持版型与默认配置

当前实机验证范围以 **SpacemiT K3** 为准。串口和 Linux 接口可配置，不代表
已经在其他板型验证完整功能。

| 版型 | 验证状态 |
| --- | --- |
| SpacemiT K3（Deb1、Pico ITX / Com260） | 当前实机测试范围；不代表每种板型、系统和内核组合均已验证。 |
| SpacemiT K1 / Milk-V Jupiter / MUSE-Pi | 仅保留历史身份探测记录，不列为完整功能已验证的支持版型。 |
| 其他开发板 | 尚未验证；接入后需确认串口参数、身份识别、登录、SSH 和文件传输。 |

内置默认值沿用 K3/Bianbu 使用习惯：**115200、8N1、无流控，串口及 SSH
使用 `root/bianbu`，启用 Linux 身份探测和 SSH 自动配置**。这是通用配置，
不会根据型号自动选择预设；其他系统镜像需修改账号、密码或串口参数。
显式使用示例 profile 文件时，其中的 Linux 配置会关闭 SSH 自动配置。
详见 [配置说明](docs/CONFIGURATION.md)。
[身份矩阵](docs/DEVICE_IDENTITY_MATRIX.md) 记录的是具体历史观测，不代表完整功能认证。

## 安装到 Ubuntu 上位机

无需开发板即可体验：运行 `python3 seriald_demo.py`，打开
`http://127.0.0.1:8899/`。模拟设备支持串口、SSH、跨天启动归档和文件传输；
不会访问真实硬件或执行主机命令。Windows 安装包可运行 `seriald.exe demo`。
详见 [Demo 使用说明](docs/DEMO.md)。

```sh
python3 tools/build_deb.py
sudo apt install ./dist/seriald_1.5.4_all.deb
```

包包含独立的 collector/control systemd 服务、CLI、网页与 skill，默认监听 `0.0.0.0:8898`，
生产数据保存在 `/data/seriald-data`。安装输出会打印本机和可用的局域网访问地址。
更新或重启 control 不会关闭 collector 持有的串口和 SSH 会话；`serialctl health` 可核对 collector PID。
在同一 USB-UART 后更换板子前，先在网页点击“准备换板”；新板日志会先隔离，出现登录提示后自动识别一次。
[安装、配置和旧数据迁移](docs/INSTALL.md) · [架构与模块边界](docs/ARCHITECTURE.md)
· [硬件身份、/boot 兜底标记与重复编号处理](docs/HARDWARE_ID.md)
· [版型与内核唯一编号矩阵](docs/DEVICE_IDENTITY_MATRIX.md)

Windows 电脑可直接接管本机 COM 串口并提供相同网页与 agent API。安装构建依赖后，
双击 `Build-seriald.cmd` 生成 Setup 安装包和便携 ZIP。推荐运行 `dist/seriald-1.5.4-windows-x64-setup.exe`：
首次选择安装目录，后续升级记住原目录，保留 `data/`，无需手动解压覆盖。首次从便携版迁移时选中原目录。
开始菜单提供本机、局域网无密码、局域网有密码三个入口。详见 [Windows 构建与安装](docs/WINDOWS.md)。
也支持 Windows Docker 容器构建；IT 可按 EXE SHA-256 审批防火墙规则，升级后需重新审批。

![DeckLab 架构图](docs/seriald-architecture.png?v=30ea099)

## Agent 使用

```sh
serialctl devices
serialctl --device riscv-118 read
serialctl --actor claude-code/debug-session --device riscv-118 exec --command 'uname -a'
serialctl --device riscv-118 activity
serialctl --device riscv-118 file-get --remote /tmp/boot.log --local ./boot.log
```

源码环境用 `python3 serialctl.py` 替代 `serialctl`。远程访问设置 `SERIALD_URL`。
`exec` 使用当前 `--actor` 在每块板上的持久 SSH 会话：同一 agent 的后续命令复用同一个 shell，
返回 JSON 输出和退出码，网页会一直显示可旁观的标签。会话只在显式 `ssh-close` 或网页关闭时结束；
网页自己创建的 `web` 会话仍按独立会话处理。

[Agent 接入指南](docs/AGENT_ACCESS.md) · [seriald-board skill](skills/seriald-board/SKILL.zh-CN.md)

网页的“启动对比”可比较同板两次或跨板的历史启动日志，突出新增异常线索与行内变化字符；
“文件传输”通过现有串口连接上传、下载或跨板复制，无需板子联网。
日志面板打开时会刷新启动索引，此后每 15 秒更新；打开日期或启动归档时保留侧栏，方便连续切换，终端显示全文。
采集器每 30 秒被动检查无输出的串口句柄，失效后自动等待重新枚举；页面同时显示最近收包与采集检查时间。
完全静默的板子与硬件链路故障无法仅凭主机端检查区分，必要时可手动探测或使用 USB 自愈。

[传输速率基线与排查方法](docs/TRANSFER_PERFORMANCE.md)

## 开发与检查

```sh
python3 -m unittest discover -s tests
node tests/frontend.test.js
node tools/browser_smoke.js
python3 -m compileall -q seriald_core seriald.py serialctl.py
bash -n rescue.sh
python3 tools/build_deb.py
```

`seriald.py` 是 control 薄入口，collector 入口为 `python3 -m seriald_core.collector_server`；后端在
`seriald_core/`，布局、样式和交互分属 `index.html`、`static/app.css`、`static/app.js`。
Python 后端使用标准库，SSH 需要系统 `ssh` 与 `sshpass`；前端依赖本地 `vendor/`。
浏览器检查需要 Node.js 22+ 和 Chrome（可用 `CHROME_BIN` 指定），使用隔离模拟设备验证真实 xterm，截图输出到 `dist/`。
源码运行必须先启动 collector，再运行 `python3 seriald.py 8898 127.0.0.1`；只有 collector 打开串口。
隔离开发可设置 `SERIALD_BASE` 为临时目录、`SERIALD_BY_PATH` 为空测试目录；不要打开真实串口验证改动。
