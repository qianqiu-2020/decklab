# Ubuntu 安装与升级

支持 Ubuntu 22.04 / 24.04 的 Python 3.10+ 环境，包为 `Architecture: all`，适用于 amd64、arm64 等架构。构建不访问网络、不需要 root；安装依赖由 apt 处理。

## 构建和安装

```sh
python3 tools/build_deb.py
sudo apt install ./dist/seriald_1.4.7_all.deb
systemctl status seriald
serialctl devices
```

包安装后自动启动 `seriald-collector.service` 和 `seriald.service`。collector 是唯一串口读者；
`seriald.service` 是可独立重启的 Web/API 控制面。**同一组串口不可同时运行源码服务和包服务**，切换安装前先停止旧服务。
默认监听 `0.0.0.0:8898`；浏览器访问 `http://上位机地址:8898`。

编辑 `/etc/seriald/seriald.env` 后执行 `sudo systemctl restart seriald`：

```ini
SERIALD_SOCKET=/run/seriald/collector.sock
SERIALD_BIND=0.0.0.0
SERIALD_PORT=8898
SERIALD_SSH_USER=root
SERIALD_SSH_PASSWORD=你的开发板密码
SERIALD_AUTH_TOKEN=一段足够长的随机值
```

配置文件是 root/seriald 可读的 conffile，升级保留用户修改。远程客户端设置 `SERIALD_URL=http://上位机:8898`。
如果不同板子的 SSH 账户不同，agent 可在单次请求中传入 `serialctl --ssh-user ubuntu
--ssh-password "$BOARD_PASSWORD" ...` 覆盖服务默认值；密码不要写进命令历史，优先使用环境变量
`SERIALD_SSH_PASSWORD_OVERRIDE`。网页创建的 SSH 仍使用服务默认账户。
`SERIALD_AUTH_TOKEN` 为空时不启用认证，仅适合可信开发网络。设置后，浏览器使用用户名 `seriald`、
密码为该 token 的 HTTP Basic 登录；agent 设置 `SERIALD_TOKEN` 或传 `serialctl --token`。
终端访问等同于使用开发板登录账户，即使启用 token 也应限制防火墙访问范围。

## 安装布局

| 路径 | 内容 |
| --- | --- |
| `/usr/lib/seriald` | Python 后端、网页和本地 xterm 资源 |
| `/usr/bin/serialctl`、`seriald-admin` | CLI 与迁移工具 |
| `/etc/seriald/seriald.env` | 可修改的服务配置 |
| `/data/seriald-data/registry.sqlite3` | 设备与会话档案 |
| `/data/seriald-data/logs/<serial>/` | 已确认身份的串口及独立 SSH 日志 |
| `/data/seriald-data/logs/_provisional/<source>/<serial>/` | 尚待正式确认的 DT、SoC/eFuse 或 `/boot` 身份日志 |
| `/data/seriald-data/logs/_unprovisioned/<path-hash>/<connection-id>/` | 没有可用编号时按连接隔离的临时日志 |
| `/usr/share/seriald/skills/seriald-board` | 可复用 skill |
| `/usr/share/doc/seriald` | 操作和架构说明 |

服务用 `seriald` 用户运行，只有 collector 加入 `dialout`。日志分别通过
`journalctl -u seriald-collector -f` 和 `journalctl -u seriald -f` 查看。USB recovery 仅允许 sudo 执行
root 拥有的固定 helper，helper 验证叶子设备名和模式。
需要直接排查生产数据时，将操作者加入 `seriald` 组：`sudo usermod -aG seriald $USER`，重新登录后即可
读写 `/data/seriald-data`。目录使用 setgid，服务以 `UMask=0007` 创建组可写文件，不依赖 ACL。
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
两分钟内且 tty 内核身份不变时通过一次性 handoff 恢复板卡绑定。异常退出、主机重启或 USB 重新枚举
仍进入待识别。首次从 1.3.6 或更早版本升级时，旧包还没有代码指纹，因此仍会重启；若旧 collector
早于 1.3.6，也没有 handoff，需要重新点击“识别设备”一次。内部 socket
`/run/seriald/collector.sock` 不应暴露到网络。
`serialctl health` 同时返回数据盘总量和剩余字节；服务不会自动删除审计日志，磁盘保留策略由运维方决定。

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
