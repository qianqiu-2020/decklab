# Web 与硬件后端分开部署

当前支持一个 Web 节点连接一个硬件后端。Web/API/MCP 留在 Web 节点；串口采集、SSH 会话、USB 刷机、复位执行器、设备档案、日志、任务和镜像缓存都由同一个后端节点持有。两个节点分别可运行 Windows 或 Ubuntu，也可跨系统组合。只重启 Web 不会重建后端连接或重新执行任务。

节点间使用 HTTPS 与独立 Bearer 密钥，校验服务器证书和主机名；不自动降级 HTTP、跳转或重试写操作。网页主密码和 Agent 令牌由 Web 校验，不作为节点间凭证。后端只应接受 Web 所在网络的访问。当前不支持一个 Web 聚合多个后端，也不支持把串口、USB 和复位拆成三个硬件节点。

## 配置两台主机

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

### Windows 安装包 / 便携包

分别使用 `Start-seriald-Backend.cmd` 和 `Start-seriald-Web.cmd`，或在各自 PowerShell 窗口设置 `$env:SERIALD_变量名='值'` 后运行：

```powershell
.\seriald.exe backend
.\seriald.exe web --no-browser
```

证书、CA、数据目录等路径使用该 Windows 主机的绝对路径，例如 `C:\DeckLab\backend-cert.pem`。Web 角色不加载 COM 采集器；后端采用现有 WindowsManager 和相同的 HTTPS API。后端需要实际串口/USB 驱动和适配 Windows 的刷机执行器；构建时可通过 `tools/build_windows.ps1 -FlashExecutor ... -FlashNotices ... -FlashLoaders ...` 打包。两端均不需要安装 MCP SDK，AI 客户端连接 Web 提供的 MCP 地址。

## 页面管理和恢复

“系统配置”提供“Web 节点 / 后端节点”选择。后端的串口、登录默认值、硬件和刷机配置在后端保存；Web 访问和 MCP 配置在 Web 保存。各自的“重启并应用”只作用于所选节点。保存配置仍不会自动执行复位、识别、刷机或修改板端密码。主访问凭证才能访问配置接口；Agent 不能绕过 Web 直接调用后端配置入口。

Web `/healthz` 的 `backend_transport: https` 说明选择了远程链路；后端失联时设备 API 返回失败，不使用本机采集器兜底。刷机提交断连后查询原任务与请求 ID；不能通过重新提交新任务推测原操作结果。后端重启后保留原任务的中断/恢复记录，不自动回放写入。

## 单文件、刷机环境和 URL

`opensbi.itb`、`bootfs.ext4` 等单文件可直接上传，也可以输入 HTTP(S) URL，由后端下载到镜像缓存。下载是后台任务，支持进度、取消、可选预期 SHA-256、大小/空间限制和有界跳转；不会把签名 URL 查询参数写入任务结果。Web 重启不停止下载。私有局域网仓库可用，主机回环、链路本地和元数据地址被拒绝。

从 BootROM 准备时，后端自动使用预装的 **FSBL 和临时 U-Boot**，仅加载到 RAM，不进入存储写入清单。安装包的 `flash-loaders/manifest.json` 固定芯片、文件和校验值；管理员可用 `SERIALD_FLASH_LOADERS` 指向另一份预备清单。预备材料必须与芯片匹配，缺失或校验失败会停止准备。无需上传完整固件包来提供其他分区的载荷。

准备完成后，在同一任务选择实际存储、分区和单文件，核对预览，再确认写入。GPT 设备读取现场分区表，不按文件名猜目标。K3 NOR 的专用 RAM 下载环境提供不写存储的标准分区映射，并回报偏移、容量；页面标明这是预备的标准 NOR 布局，需要与板卡固件布局一致。单分区不发送 `flash:mtd`，不写分区表或顺带清空 env；预览保守列出所选 NOR 分区内的擦除风险。完整 NOR 恢复仍是独立模式，会明确列出布局写入和 env 擦除。

## 验证边界

`python tools/test_split_nodes.py` 启动独立 Web/后端进程和真实 HTTPS 链路，后端显式使用模拟板卡，绝不发现或打开真实串口/USB。`--exe PATH` 对 Windows 包执行同一流程。单元回归还覆盖镜像上传、复位、SSE、授权撤销及 Web 重建后原刷机任务不重复执行。源码与模拟测试不能替代 Windows 原生打包、USB 驱动或实体板刷写验收。
