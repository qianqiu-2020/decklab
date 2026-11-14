# 单机安装与远程访问

推荐在持有板卡串口和 USB 的 Linux 控制机安装完整 DeckLab，其他电脑直接用浏览器打开控制机的访问链接。此方式已能共享终端、SSH、日志、镜像商店、任务、MCP 和刷机，不需要另装 Web 节点或进行节点配对。Windows 也支持完整单机使用。

Linux 安装后运行 `sudo seriald-access` 查看访问地址。Windows 使用局域网启动入口；有密码入口打印授权链接。访问凭证使用现有分享机制，详见 [安装](INSTALL.md) 和 [Windows](WINDOWS.md)。

控制机空间小时优先配置外部数据盘、日志保留和镜像清理。单独搬走 Web 不会搬走后端串口处理、刷机任务及镜像缓存；分离主要适用于统一入口、网络隔离等明确需求。下面的 Web/硬件后端分离模式作为高级部署继续兼容，已有安装升级保留角色。

## 配置多机模式

以下后端部署在专用 Linux 控制机，上位机可选择 Linux 或 Windows。

先为后端准备包含实际主机名或 IP 的 TLS 证书和私钥。可使用组织 CA 签发的 PEM 证书；Web 配置其 CA 文件。自签名证书也须包含后端地址的 subjectAltName，Web 显式信任该证书。仓库的 `tests/fixtures/backend-tls` 是公开测试证书与私钥，不能用于部署。

生成至少 32 字符的随机节点密钥，例如在任一主机运行：

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

后端环境变量（路径按本机实际位置修改）：

```text
SERIALD_BASE=/data/decklab-backend
SERIALD_BACKEND_BIND=0.0.0.0
SERIALD_BACKEND_PORT=8899
SERIALD_BACKEND_TOKEN=<随机节点密钥>
SERIALD_BACKEND_CERT=/etc/decklab/backend-cert.pem
SERIALD_BACKEND_KEY=/etc/decklab/backend-key.pem
```

后端不设置 `SERIALD_BACKEND_URL`。USB、串口和复位配置路径均指向后端本机；原有板卡配置中的 `node: local` 指硬件后端，而非浏览器或 Web 主机。

Web 环境变量：

```text
SERIALD_BASE=/data/decklab-web
SERIALD_BIND=0.0.0.0
SERIALD_PORT=8898
SERIALD_AUTH_TOKEN=<网页主访问密码>
SERIALD_BACKEND_URL=https://backend.example.lan:8899
SERIALD_BACKEND_TOKEN=<同一个随机节点密钥>
SERIALD_BACKEND_CA=/etc/decklab/backend-ca.pem
```

Web 和后端使用各自的数据目录，不共享 SQLite、日志目录或 Unix socket。Web 保存授权与自己的主机配置。已有设备档案、SSH 凭证、镜像和任务应保留在后端原数据目录。

### Ubuntu / Debian 安装包

在每台主机编辑 `/etc/seriald/seriald.env`，再选择该主机角色。先停止默认的本机双服务组合，再启动新角色；切换前保留数据目录，等待正在执行的硬件任务结束。

后端主机：

```sh
sudo systemctl disable --now seriald.service seriald-collector.service
sudo systemctl enable --now seriald-backend.service
```

Web 主机：

```sh
sudo systemctl disable --now seriald.service seriald-collector.service
sudo systemctl enable --now seriald-web.service
```

相关服务互斥，同一个数据目录另有采集器进程锁。只有后端角色打开串口。升级会保留已选择的角色；Web 包升级不要求重启另一台主机的后端。

源码对应入口：

```sh
python3 -m seriald_core.backend_server
python3 -m seriald_core.control_server --require-backend --no-browser
```

### Windows 上位机

在 PowerShell 中设置上述 Web 环境变量，再使用 `Start-seriald-Web.cmd`，或运行：

```powershell
.\seriald.exe web --no-browser
```

CA、数据目录等路径使用该 Windows 主机的绝对路径，例如 `C:\DeckLab\backend-ca.pem`。
Web 角色不加载本机 COM 采集器或刷机执行器，板卡驱动、执行器与 RAM 加载器安装在 Linux 控制机。
两端均不需要另装 MCP SDK，AI 客户端连接 Web 提供的 MCP 地址。

Windows 单机使用普通 `Start-seriald.cmd` 或局域网启动入口，完整包内置刷机组件，并提供 USB 驱动安装入口。
现有 `Start-seriald-Backend.cmd` / `seriald.exe backend` 入口保留为兼容与调试能力；
多机产品部署与验收按 Linux 控制机执行，不要求 Windows 硬件复位验收。

## 页面管理和恢复

“系统配置”提供“Web 节点 / 后端节点”选择。后端的串口、登录默认值、硬件和刷机配置在后端保存；Web 访问和 MCP 配置在 Web 保存。各自的“重启并应用”只作用于所选节点。保存配置仍不会自动执行复位、识别、刷机或修改板端密码。主访问凭证才能访问配置接口；Agent 不能绕过 Web 直接调用后端配置入口。

Web `/healthz` 的 `backend_transport: https` 说明选择了远程链路；后端失联时设备 API 返回失败，不使用本机采集器兜底。刷机提交断连后查询原任务与请求 ID；不能通过重新提交新任务推测原操作结果。后端重启后保留原任务的中断/恢复记录，不自动回放写入。

## 单文件、刷机环境和 URL

`opensbi.itb`、`bootfs.ext4` 等单文件可直接上传，也可以输入 HTTP(S) URL，由后端下载到镜像缓存。下载是后台任务，支持进度、取消、可选预期 SHA-256、大小/空间限制和有界跳转；不会把签名 URL 查询参数写入任务结果。Web 重启不停止下载。私有局域网仓库可用，主机回环、链路本地和元数据地址被拒绝。

从 BootROM 准备时，后端自动使用预装的 **FSBL 和临时 U-Boot**，仅加载到 RAM，不进入存储写入清单。安装包的 `flash-loaders/manifest.json` 固定芯片、文件和校验值；管理员可用 `SERIALD_FLASH_LOADERS` 指向另一份预备清单。预备材料必须与芯片匹配，缺失或校验失败会停止准备。无需上传完整固件包来提供其他分区的载荷。

完整 NOR 固件包同样支持“指定分区”：包内表只作为载荷目录，勾选哪个分区就取出对应文件，不附带写入表或其他分区。准备完成后，在同一任务选择实际存储、分区和载荷，核对预览，再确认写入。GPT 设备读取现场分区表，不按文件名猜目标。K3 NOR 的专用 RAM 下载环境提供不写存储的标准分区映射，并回报偏移、容量；页面标明这是预备的标准 NOR 布局，需要与板卡固件布局一致。单分区不发送 `flash:mtd`，不写分区表或顺带清空 env；预览保守列出所选 NOR 分区内的擦除风险。完整 NOR 恢复仍是独立模式，会明确列出布局写入和 env 擦除。

## 验证边界

| 验收组合 | 主要覆盖 | 复位验收归属 |
|---|---|---|
| Linux 单机 | 本机主要功能、真实串口与 USB 刷写/回读 | 配有控制适配器时验证 |
| Windows 单机 | 安装升级、主要功能、原生串口与 USB 刷写/回读 | 不要求 Windows 硬件复位验收 |
| Windows 上位机 + Linux 控制机 | 远程终端、任务、镜像、刷写/回读、授权、断线与恢复 | Linux 控制机按实际适配器验证 |
| Linux 上位机 + Linux 控制机 | 同上，并验证 Web 重启与控制机任务独立 | Linux 控制机按实际适配器验证 |

Windows 单机测试中经 Linux BMC 使板子进入下载模式或恢复上电，是外部测试条件与收尾操作，
不计作 Windows 复位能力验收。Windows 通用执行器的进程管理软件回归继续保留。

`python tools/test_split_nodes.py` 启动独立 Web/后端进程和真实 HTTPS 链路，后端显式使用模拟板卡，绝不发现或打开真实串口/USB。`--exe PATH` 对 Windows 包执行同一流程。单元回归还覆盖镜像上传、复位、SSE、授权撤销及 Web 重建后原刷机任务不重复执行。源码与模拟测试不能替代 Windows 原生打包、USB 驱动或实体板刷写验收。
