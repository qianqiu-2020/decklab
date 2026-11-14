# Ubuntu 安装与升级

支持 Ubuntu 22.04 / 24.04，发布包分别为 `Architecture: amd64` 和 `Architecture: arm64`。
1.7.1 起，deb 内置 MCP 所需的私有 Python、官方 SDK 及依赖；安装过程中不运行 pip，
不需要手动配置 `SERIALD_MCP_PYTHON`。核心服务仍使用系统 Python 3.10+，系统依赖由 apt 处理。
其他上位机架构可使用源码运行方式，目前不提供对应的预编译 deb。

推荐在持有板卡的 Linux 控制机安装完整 DeckLab，其他电脑直接用浏览器访问，无需安装客户端或配对。Windows 也支持完整单机使用。已有 Web/后端分离部署继续兼容，见 [部署模式](DISTRIBUTED.md)。

统一终端解析需要发行版 `nodejs`（Node 12+）；`apt install ./dist/…deb` 会一并安装。
如果用 `dpkg -i` 安装且提示缺少依赖，执行一次 `sudo apt -f install`。
Windows 安装器已内置该运行时，使用者无需额外安装。

## 下载和安装

用 `dpkg --print-architecture` 核对上位机架构，从固定地址下载对应包：

- [amd64 最新安装包](https://github.com/qianqiu-2020/decklab/releases/latest/download/seriald_latest_amd64.deb)
- [arm64 最新安装包](https://github.com/qianqiu-2020/decklab/releases/latest/download/seriald_latest_arm64.deb)

例如安装已下载的 amd64 固定文件名安装包：

```sh
sudo apt install ./seriald_latest_amd64.deb
systemctl status seriald
serialctl devices
```

下载链接跟随 GitHub Latest 正式发布，草稿不改变下载目标。历史链接
`seriald_latest_all.deb` 保留为 amd64 兼容别名，文件内的 `Architecture` 是 `amd64`，
不能用于 arm64 安装。

## 完整安装内容

1.10.0 起默认构建完整包，包含主程序、MCP、刷机执行器和匹配的 RAM 加载材料，自动选择包内路径；Linux 的 Node、libusb、udev 等系统依赖由 apt 安装。当前内置 RAM 材料支持 K3；列出 K1 镜像不代表已验证 K1 刷机。
历史 GitHub 1.9.0 及更早的精简包仍需[单独安装刷机组件](HARDWARE_EXTENSIONS.md#独立安装刷机组件)，升级完整包时保留显式配置的外部组件路径。

镜像商店从[公开站](https://archive.spacemit.com/image/)和[内部 Nexus](https://nexus.bianbu.xyz/service/rest/repository/browse/image/)读取目录，下载保存在控制机。使用见[镜像商店](IMAGE_STORE.md)。

## 从源码构建安装包

在与目标架构匹配的原生构建机上，先冻结 Ubuntu 22.04 / glibc 2.35 基线的 MCP 运行时，
准备 `build/install-components/linux-amd64/manifest.json` 中经过校验的完整材料，再离线组装 deb。材料清单格式见 [完整包构建](COMPLETE_INSTALL.md)。默认运行时构建使用 Docker，并下载锁定版本和哈希的构建依赖；
组装步骤不访问网络、不需要 root：

```sh
python3 tools/build_linux_mcp.py --arch amd64 --output build/linux-mcp/amd64/runtime
python3 tools/build_deb.py --runtime build/linux-mcp/amd64/runtime
sudo apt install ./dists/seriald_1.10.1_amd64.deb
```

arm64 构建将上述 `amd64` 改为 `arm64`，在对应原生构建机执行。
`build_deb.py` 未传 `--runtime` 时只查找本机架构的 `build/linux-mcp/<arch>/runtime`；
不存在或校验失败会停止构建，不生成缺少 MCP 依赖的包。已准备运行时还可通过
`python3 tools/build_linux_mcp.py --arch amd64 --output build/linux-mcp/amd64/runtime --verify`
离线核对清单及内容。

## 服务与配置

包安装后自动启动 `seriald-collector.service` 和 `seriald.service`。collector 是唯一串口读者；
`seriald.service` 是可独立重启的 Web/API 控制面。**同一组串口不可同时运行源码服务和包服务**，切换安装前先停止旧服务。
默认监听 `0.0.0.0:8898`；浏览器访问 `http://上位机地址:8898`。
安装后运行 `sudo seriald-access` 可打印本机和局域网访问链接；启用访问密码时使用现有分享授权，有效期内可直接打开。链接仅分享给受信任的使用者。若局域网地址未出现，请检查网卡和防火墙配置。

远程 MCP 使用同一地址的 `/mcp` 路径，AI 客户端只需填写 URL 和网页生成的 Agent 令牌。
Debian 包的内置 MCP 由 control 自动管理；升级到 1.7.1 后无需另装服务端 SDK。
若旧配置设置了 `SERIALD_MCP_PYTHON`，它会继续优先使用；要改用内置运行时，删除该项并重启
`seriald.service`，无需重启 collector。源码部署与故障排查见 [MCP 接入](MCP.md)。

编辑 `/etc/seriald/seriald.env` 后，监听地址和认证配置通过
`sudo systemctl restart seriald` 生效；默认板卡密码、串口参数、profile、日志和传输配置
需要 `sudo systemctl restart seriald-collector`。仅重启网页服务不加载采集器配置。
网页“SSH 设置”保存的设备账号密码则在连接成功后立即生效，无需重启服务。

```ini
SERIALD_SOCKET=/run/seriald/collector.sock
SERIALD_BIND=0.0.0.0
SERIALD_PORT=8898
SERIALD_SSH_USER=root
SERIALD_SSH_PASSWORD=你的开发板密码
SERIALD_AUTH_TOKEN=一段足够长的随机值
```

配置文件是 root/seriald 可读的 conffile，升级保留用户修改。远程客户端设置 `SERIALD_URL=http://上位机:8898`。
网页“SSH 设置”可填写每块板的账号密码，成功连接后服务端按序列号保存，后续建连优先复用；
服务重启、重新插拔或更换 USB 口后，只要恢复相同设备身份即可继续使用。修改时同样先验证连接，
失败不会覆盖旧值。未识别连接不按 IP 或 COM/tty 保存长期凭据。
Agent 也可复用用户保存的账号，或在单次请求中传入 `serialctl --ssh-user ubuntu
--ssh-password "$BOARD_PASSWORD" ...` 临时覆盖；Agent 建连不更新持久凭据。密码不要写进命令历史，
优先使用环境变量 `SERIALD_SSH_PASSWORD_OVERRIDE`。服务默认值仅在没有该设备保存值时使用。
`SERIALD_AUTH_TOKEN` 为空时不启用认证，仅适合可信开发网络。设置后，浏览器使用用户名 `seriald`、
密码为该 token 的 HTTP Basic 登录；agent 设置 `SERIALD_TOKEN` 或传 `serialctl --token`。
无需预先设置令牌文件：网页“分享给 Agent”会生成独立且可撤销的接入令牌，默认不限期，
存于 `SERIALD_BASE/agent-grants.sqlite3`（只保存令牌哈希）。单设备分享按序列号限制该令牌的访问，
撤销时会关闭其 SSH 会话；开放模式下未带令牌的访问仍保持原有权限。
远程 Agent 可直接下载 `/serialctl.py` 和 `/skills/seriald-board/SKILL.md`，由网页复制完整命令。
若要可信地区分不同 agent，设置 `SERIALD_AGENT_TOKENS_FILE` 指向仅 control 服务可读的 JSON 文件，
例如 `{"claude-code/debug":"至少32字符的随机令牌"}`，同时启用 `SERIALD_AUTH_TOKEN`。
agent 使用其专用令牌时，control 会覆盖请求中的 `actor`，操作记录和持久 SSH 会话便绑定到该标签。
共享的主令牌仍有完整管理权限；无认证模式下 `actor` 只是客户端自报的标签。
终端访问等同于使用开发板登录账户，即使启用 token 也应限制防火墙访问范围。

普通 SSH 建连认证失败时会提示填写账号密码，不会自动修改 root 密码或 sshd 登录策略，
无论旧配置中的 `SERIALD_AUTO_CONFIGURE_SSH` 为何值。已有普通用户可直接用于 SSH；
确需 root 密码登录时，由用户按[串口手工指引](CONFIGURATION.md#manual-root-ssh-setup)分步执行。
指引按钮只复制命令，不自动发送。单独授权的 `ssh-enable` 可从已有 root 串口 shell 安装或启动
SSH 服务，但不设置 root 密码或启用 root 密码登录。

## 安装布局

| 路径 | 内容 |
| --- | --- |
| `/usr/lib/seriald` | Python 后端、网页和本地 xterm 资源 |
| `/usr/lib/seriald/mcp-runtime/` | root 拥有的私有 MCP/Python 运行时及第三方清单；服务账户不可修改 |
| `/usr/bin/serialctl`、`seriald-admin` | CLI 与迁移工具 |
| `/etc/seriald/seriald.env` | 可修改的服务配置 |
| `/data/seriald-data/registry.sqlite3` | 设备、会话档案与私有 SSH 凭据；Unix 权限 0600 |
| `/data/seriald-data/mcp-worker.log` | MCP 进程诊断日志，0600；每份最多 64 KiB，保留一份 `.1` 轮转 |
| `/data/seriald-data/logs/<serial>/` | 已确认身份的串口及独立 SSH 日志 |
| `/data/seriald-data/logs/_provisional/<source>/<serial>/` | SoC/eFuse 或 `/boot` 候选身份日志；旧版本 DT 候选目录在确认后迁移，有效 DT 编号现直接确认 |
| `/data/seriald-data/logs/_unprovisioned/<path-hash>/<connection-id>/` | 没有可用编号时按连接隔离的临时日志 |
| `/usr/share/seriald/skills/seriald-board` | 可复用 skill |
| `/usr/share/doc/seriald` | 操作和架构说明 |

服务用 `seriald` 用户运行，只有 collector 加入 `dialout`。日志分别通过
`journalctl -u seriald-collector -f` 和 `journalctl -u seriald -f` 查看。USB recovery 仅允许 sudo 执行
root 拥有的固定 helper，helper 验证叶子设备名和模式。
需要直接排查日志时，可将操作者加入 `seriald` 组：`sudo usermod -aG seriald $USER`，重新登录后访问
组可读写的日志。目录使用 setgid，服务以 `UMask=0007` 创建普通运行文件，不依赖 ACL。
含已保存 SSH 密码的注册表及其 WAL/SHM 为例外，只允许服务账户读写（0600）；备份也应按敏感文件管理。
凭据以明文存于服务端，不回传浏览器、不写任务记录，也不修改串口登录或 profile 文件；
见[凭据保存](CONFIGURATION.md#remembered-ssh-credentials)和[数据库升级恢复](STORAGE_UPGRADES.md)。
服务升级以及卸载不删除 `/data/seriald-data`，历史数据由操作者决定保留期限。

## 控制面热更新与 collector 升级

只修改页面或 control 配置时可单独重启 `seriald.service`，collector 的 PID、串口 fd 和 SSH 会话保持不变：

```sh
serialctl health
sudo systemctl restart seriald
serialctl health
```

两次输出的 collector `pid` 和 `started` 应相同。deb 升级会比较安装前后的 collector 代码指纹；仅修改
页面、control 或文档时只重启 control，collector 的串口 fd 和 SSH 会话保持不变。采集、身份、存储代码
或 collector systemd 单元有变化时才重启 collector。此时串口 fd 和 SSH 会话会短暂关闭；1.3.6 起，干净重启在同一 host boot、
两分钟内且 tty 内核身份不变时通过一次性 handoff 恢复板卡绑定。运行中的 USB 重新枚举若保持相同
by-path 且未进入“准备换板”模式，也会沿用原绑定；异常退出或主机重启后没有有效 handoff 时仍进入待识别。
首次从 1.3.6 或更早版本升级时，旧包还没有代码指纹，因此仍会重启；若旧 collector
早于 1.3.6，也没有 handoff，需要重新点击“识别设备”一次。内部 socket
`/run/seriald/collector.sock` 不应暴露到网络。
`serialctl health` 同时返回数据盘总量、剩余字节以及 `logging_ok` / `log_errors`。
日志写入失败不会停止实时终端，但会在设备列表和健康接口报警；排除磁盘或权限故障后会重试。
服务不会自动删除审计日志，磁盘保留策略由运维方决定。备份时应同时保存 `registry.sqlite3`、
其 WAL 文件、`agent-grants.sqlite3` 及其 WAL 文件、`logs/`，建议先停止相关服务再复制，以获得一致的档案。

源码双进程开发：

```sh
SERIALD_BASE=$PWD/.dev-data SERIALD_SOCKET=$PWD/.dev-data/collector.sock \
  python3 -m seriald_core.collector_server
SERIALD_BASE=$PWD/.dev-data SERIALD_SOCKET=$PWD/.dev-data/collector.sock \
  SERIALD_BIND=127.0.0.1 python3 -m seriald_core.control_server
```

## 给 agent 安装 skill

```sh
mkdir -p ~/.claude/skills ~/.codex/skills
ln -s /usr/share/seriald/skills/seriald-board ~/.claude/skills/seriald-board
ln -s /usr/share/seriald/skills/seriald-board ~/.codex/skills/seriald-board
```

已有同名 skill 时先核对来源再更新链接。新会话使用 `seriald-board`；远程 agent 只需复制 `serialctl.py`，配置服务 URL。

## 迁移旧版按 USB 路径存放的数据

`labels.json` 只描述旧标签与插口，不能证明开发板序列号。先核对旧设备身份，准备 JSON 映射，例如：

```json
[
  {
    "serial": "实际开发板序列号",
    "label": "riscv-118",
    "by_path": "旧 by-path 名称",
    "log_dirs": ["logs 下经过核实的旧目录名"]
  }
]
```

停旧服务后，先预览再复制（默认 dry-run，不修改数据）：

```sh
sudo systemctl stop seriald
sudo seriald-admin --base /data/seriald-data import-legacy --source /data/seriald --mapping mapping.json
sudo seriald-admin --base /data/seriald-data import-legacy --source /data/seriald --mapping mapping.json --apply
sudo chown -R seriald:seriald /data/seriald-data
sudo systemctl start seriald
```

源码环境使用 `python3 -m seriald_core.admin` 替代 `seriald-admin`。
导入复制日志，不删除源文件；相同内容可重复执行，目标文件内容不一致则拒绝覆盖。
**同一旧目录混入多块板的日志时，不要将整个目录指定给某一序列号。**保留原始归档，人工确认后再导入。
历史设备导入后可在线查看记录，连接设备需点击“识别设备”核对当前序列号，再使用用户标签访问。

## Separate Web and hardware roles

See [distributed deployment](DISTRIBUTED.md) for Windows/Ubuntu role launchers, TLS certificates, node credentials and data ownership. The default local role remains available.
