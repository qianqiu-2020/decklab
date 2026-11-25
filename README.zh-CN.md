# DeckLab 开发板工作台

DeckLab 运行在接有开发板的控制机上。串口日志由控制机持续采集，网页关闭后仍会记录。团队成员可以通过浏览器使用串口、SSH、ADB 和远程桌面，检查历史日志，刷写镜像或运行用例。自动化可通过命令行、HTTP API 或 MCP 使用同一套设备和任务。

[English](README.md) · [完整文档](docs/index.md) · [下载最新版](https://github.com/qianqiu-2020/decklab/releases/latest)

## 从一块板开始

1. 在接有开发板的电脑上安装 [Windows x64 安装器](https://github.com/qianqiu-2020/decklab/releases/latest/download/decklab-windows-x64-setup.exe)，或用 `sudo apt install ./decklab_latest_amd64.deb` 安装 [Ubuntu amd64 包](https://github.com/qianqiu-2020/decklab/releases/latest/download/decklab_latest_amd64.deb)。
2. 接上串口线，在控制机打开 `http://127.0.0.1:8898/`。设备进入列表后，点开它即可看启动输出。需要从其他电脑访问时，使用控制机提供的带访问权限的局域网链接。
3. 需要刷机时，再接 USB 刷机线。在板卡页打开「刷机与启动」，按操作选择普通刷机、URL 极速刷机或内存启动。普通刷机先预览目标盘和实际写入范围，再确认。

安装升级、端口和历史数据的处理见 [Linux 安装](docs/operations/install-linux.md) 或 [Windows 安装](docs/operations/install-windows.md)。串口、刷机 USB 和 ADB 如何识别同一板卡，见 [设备身份](docs/modules/device-identity.md)。

## 按任务查文档

| 要做的事情 | 文档 |
| --- | --- |
| 看启动日志、使用 SSH、查当天串口记录 | [串口与 SSH](docs/modules/serial-and-ssh.md)、[日志与归档](docs/operations/log-operations.md) |
| 选镜像、刷系统盘、更新固件或从内存启动 | [刷机工作区](docs/modules/flashing.md)、[镜像商店](docs/modules/image-store.md)、[内存启动](docs/modules/ram-boot.md) |
| 给板卡绑控制槽位，远程复位或进入下载模式 | [硬件控制](docs/modules/hardware-control.md) |
| 打开远程桌面或使用 ADB | [远程桌面](docs/modules/remote-desktop.md)、[ADB](docs/modules/adb.md) |
| 管理任务、用例和 AI 访问范围 | [后台任务](docs/modules/tasks.md)、[Agent 接入](docs/modules/agent-access.md)、[MCP](docs/modules/mcp.md) |
| 构建安装包或维护接口 | [模块架构](docs/modules/architecture.md)、[完整包构建](docs/development/complete-build.md)、[发布流程](docs/operations/release.md) |

没有开发板时可以运行 `python3 decklab_demo.py`，再打开 `http://127.0.0.1:8899/` 体验设备列表、日志和终端。[演示环境](docs/operations/demo.md)
