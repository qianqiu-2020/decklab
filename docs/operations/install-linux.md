# Linux 安装和升级

在连接串口线、刷机线或硬件控制器的控制机上安装 DeckLab。当前完整发行包面向 Ubuntu amd64，内置网页、终端解析器、MCP 运行时、ADB、Fastboot、刷机执行器及 K1/K3 的临时加载材料。DeckLab 自动选用内置 ADB。系统依赖由 apt 解析。控制机无需接入开发板就可以完成安装。

## 安装并打开网页

从 [GitHub Releases](https://github.com/qianqiu-2020/decklab/releases/latest) 下载 amd64 Debian 包，在控制机运行：

```sh
sudo apt install ./decklab_latest_amd64.deb
systemctl status decklab decklab-collector
sudo decklab-access
```

`decklab-access` 显示网页入口。如果浏览器也在控制机上，默认地址为 `http://127.0.0.1:8898/`。需要从其他电脑访问时，先在[主机环境变量](../reference/environment.md)里核对监听地址和访问凭据，再使用 DeckLab 生成的分享链接。

## 服务与文件

| 位置 | 用途 |
| --- | --- |
| `decklab.service` | 单机的 Web、HTTP API 和 MCP 入口 |
| `decklab-collector.service` | 单机的硬件采集与执行进程 |
| `decklab-web.service`、`decklab-backend.service` | [分离部署](../modules/distributed-nodes.md)的互斥角色 |
| `/etc/decklab/decklab.env` | systemd 加载的主机启动配置 |
| `/usr/lib/decklab/` | 包管理器安装的程序、静态资源和支持文件 |
| `/data/decklab-data/` | 默认运行目录，含设备库、日志、任务和镜像缓存 |
| `/usr/share/doc/decklab/` | 按模块安装的操作指南及示例 |

采集服务通过控制机的串口权限组访问设备。Linux 串口探测通常从 `/dev/serial/by-path` 枚举物理端口。读取失败时先核对设备节点、用户组和已有串口读者，再检查板端波特率。[串口与 SSH](../modules/serial-and-ssh.md)

## Upgrade an existing installation

将新版 Debian 包安装到原控制机。开始之前结束正在写盘或执行的任务，并保存要保留的启动记录。

点击网页顶部的版本号可检查 GitHub 正式版。网页显示与当前控制机匹配的安装包和命令。在控制机上运行 `sudo decklab-update <版本号>`，例如网页显示的新版本为 `1.20.0` 时运行 `sudo decklab-update 1.20.0`。命令会重新核对所选版本，下载版本化 Debian 包并校验大小、SHA-256、包名、版本和架构，然后交给 dpkg 安装。仅检查更新可运行 `decklab-update`，无需 sudo。

从旧包更名升级时，apt 用 `decklab` 替换旧包。安装脚本沿用原服务账户的数字标识，将已有环境变量转换成 `DECKLAB_*`。默认数据目录迁移后留有旧路径别名供历史任务引用，自定义数据位置按原配置使用。Web 和硬件后端分离的部署应在各自节点上升级，随后核对 HTTPS 后端状态。

升级后依次检查服务状态、`decklab-access` 的 URL、已知板卡和一份历史归档。若服务启动失败，查看 `journalctl -u decklab -u decklab-collector -b`，确认端口占用、数据目录权限和配置是否匹配。安装或卸载不会自动清理操作员的运行数据。[存储升级与恢复](storage-upgrades.md)
