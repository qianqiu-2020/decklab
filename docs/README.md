# Documentation

Start with the [project overview](../README.md) or [简体中文](../README.zh-CN.md).
The demo uses simulated boards; current physical-board validation focuses on
SpacemiT K3. See the overview for the board support list and default credentials.

## Install and connect

- [Ubuntu installation](INSTALL.md): services, data paths and upgrades.
- [Windows](WINDOWS.md): installer, launch modes, LAN access and firewall setup.
- [Configuration](CONFIGURATION.md): serial framing, credentials and board profiles.
- [Demo](DEMO.md): explore the application without hardware.
- [Agent access](AGENT_ACCESS.md): shared SSH/serial access, grants and CLI commands.
- [MCP](MCP.md): standard AI client tools over the existing protected control API.

## Operate boards

- [Hardware identity](HARDWARE_ID.md) and [observations by board/kernel](DEVICE_IDENTITY_MATRIX.md).
- [Logs](LOG_OPERATIONS.md): archives, retention and diagnostics.
- [File transfers](TRANSFER_ARTIFACTS.md) and [throughput measurements](TRANSFER_PERFORMANCE.md).
- [Background tasks](TASKS.md): task status, cancellation and result retrieval.
- [Flashing workspace](FLASH_WORKSPACE.md): NOR backup, partition selection, progress and readback.
- [Hardware extensions](HARDWARE_EXTENSIONS.md): automatic USB discovery, manual download mode and configured hardware controls.
- [Database upgrades](STORAGE_UPGRADES.md): automatic migration and backups.

## Develop and maintain

- [Contributing](../CONTRIBUTING.md): modules, tests and interface conventions.
- [Architecture](ARCHITECTURE.md), [HTTP API](API.md) and [console coordination](CONSOLE_TRANSACTIONS.md).
- [Releasing](RELEASING.md): version tags, drafts and dependency updates.
- [Third-party components](../THIRD_PARTY.md), [native dependency review](WINDOWS_NATIVE_NOTICES.md)
  and [Windows library replacement/rebuild](WINDOWS_REBUILD.md).
- [Security scope](../SECURITY.md) and [changelog](../CHANGELOG.md).

Completed implementation plans and historical investigations are retained in Git
history rather than distributed as current instructions.

[部署模式](DISTRIBUTED.md)：Linux/Windows 单机，或 Linux/Windows 上位机与专用 Linux 控制机。

- [完整包构建](COMPLETE_INSTALL.md)
- [镜像商店](IMAGE_STORE.md)
