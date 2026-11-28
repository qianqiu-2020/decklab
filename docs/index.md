# DeckLab 技术文档

初次配置时，从控制机的操作系统选择安装说明。排查设备或开发集成时，按负责该操作的模块查找。

## 安装与维护

- [Linux 安装和升级](operations/install-linux.md)介绍 Debian 包、systemd 服务、日志位置和数据保留。
- [Windows 安装和升级](operations/install-windows.md)介绍安装器、USB 驱动、启动方式和局域网访问。
- [日志和归档](operations/log-operations.md)说明如何定位串口日志、检查采集和设置保留时间。
- [数据库升级和备份](operations/storage-upgrades.md)说明迁移触发时机、备份位置和恢复步骤。
- [演示环境](operations/demo.md)可用来学习界面和验证集成。

## 功能模块

| 模块 | 负责的工作 |
| --- | --- |
| [整体架构](modules/architecture.md) | 浏览器、Web 服务、硬件后端和后台任务之间的边界 |
| [设备身份](modules/device-identity.md) · [硬件控制](modules/hardware-control.md) | 串口、USB、板卡序列号、复位槽位和换板 |
| [串口与 SSH](modules/serial-and-ssh.md) · [终端协调](modules/console-transactions.md) | 日志、终端尺寸、输入权、探测和命令执行 |
| [ADB](modules/adb.md) · [远程桌面](modules/remote-desktop.md) | USB 调试和图形会话，包括身份匹配与连接诊断 |
| [镜像商店](modules/image-store.md) · [刷机](modules/flashing.md) · [内存启动](modules/ram-boot.md) | 镜像缓存、目标盘、写入范围、进度和临时启动 |
| [文件传输](modules/file-transfer.md) · [任务](modules/tasks.md) | 上传、下载、板间传输及后台执行的结果与恢复 |
| [Agent 接入](modules/agent-access.md) · [MCP](modules/mcp.md) · [平台集成](modules/platform-integration.md) | 授权、工具调用和共享执行记录 |
| [TitanX 接入](modules/titanx-integration.md) | 接管板卡、旁观测试、失败后跳转排查与复测、上位机 Agent 图形测试 |
| [分离部署](modules/distributed-nodes.md) | 把浏览器入口和设备所在的硬件节点部署在不同主机 |

## 接口、开发与发布

- [环境变量参考](reference/environment.md)和 [HTTP API](reference/http-api.md)列出当前接口。
- [刷机性能](reference/flash-performance.md)、[文件传输性能](reference/transfer-performance.md)及[板卡身份观测](reference/device-identity-observations.md)保留测试条件和结果。
- [完整包构建](development/complete-build.md)、[Windows 依赖审查](development/windows-native-notices.md)和[第三方库替换](development/windows-rebuild.md)说明依赖与交付材料。
- [发布流程](operations/release.md)和[发布验收](operations/release-acceptance.md)列出出包、核对和发布步骤。
- [开发贡献](../CONTRIBUTING.md)、[项目安全](../SECURITY.md)、[品牌资源](development/brand-assets.md)、[当前变更](../CHANGELOG.md)、[历史构建记录](history/changelog-archive.md)及[历史维护记录](history/maintenance-notes.md)。
