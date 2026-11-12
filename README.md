# DeckLab

[简体中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Installation](docs/INSTALL.md) · [Windows](docs/WINDOWS.md) · [Agent access](docs/AGENT_ACCESS.md)

A shared development-board workbench for OS and BSP developers. Capture serial
output continuously, access boards through a browser or agent, and keep boot
history attached to a stable board identity rather than a changing tty number.

[Latest release](https://github.com/qianqiu-2020/decklab/releases/latest) ·
[Windows installer](https://github.com/qianqiu-2020/decklab/releases/latest/download/seriald-windows-x64-setup.exe) ·
[Ubuntu amd64](https://github.com/qianqiu-2020/decklab/releases/latest/download/seriald_latest_amd64.deb) ·
[Ubuntu arm64](https://github.com/qianqiu-2020/decklab/releases/latest/download/seriald_latest_arm64.deb)

![DeckLab demo: shared terminal, board list and boot archives](docs/decklab-demo.png?v=ui-1.5.2)

Actual UI with simulated boards; no physical hardware is required. Demo devices
illustrate the workflow and do not establish hardware support.

## Why DeckLab

- Share serial and SSH terminals with colleagues and agents, with visible session history.
- Keep per-board logs across reconnects and USB serial renumbering.
- Browse logs by date or boot and compare boots to investigate BSP regressions.
- Transfer files over serial when networking is unavailable, including board-to-board copies.
- Recover a stuck USB serial connection without physically visiting the host where supported.
- Install an Ubuntu service or a native Windows host with three simple access modes.

Ordinary commands should use SSH; use serial for bootloader, kernel and networking
failures. Agent grants are revocable and can be scoped to one board. Runtime
service names, command names and installation paths retain the `seriald` name.

Remote MCP clients connect to the host's `/mcp` URL with a web-issued Agent token;
no client-side Python or adapter installation is required. Windows and Debian
packages include the MCP server runtime. Debian 1.7.1 includes a private Python
runtime and SDK, so installation needs no pip command or `SERIALD_MCP_PYTHON`
setting. Source setup and optional compatibility modes are documented in the
[MCP guide](docs/MCP.md).

## Install and use

![DeckLab architecture](docs/seriald-architecture-en.png?v=english-1)

Try without hardware: `python3 seriald_demo.py`, then open
**http://127.0.0.1:8899/**. See the [demo guide](docs/DEMO.md) for simulated
boot comparison, SSH and file transfers.

Download the package matching the host architecture from the links above.
Ubuntu 22.04 / 24.04 packages are available for amd64 and arm64. To build an
amd64 package from source, prepare its MCP runtime on the Ubuntu 22.04 baseline
first (Docker and build-time downloads are required by the default builder):

```sh
python3 tools/build_linux_mcp.py --arch amd64 --output build/linux-mcp/amd64/runtime
python3 tools/build_deb.py --runtime build/linux-mcp/amd64/runtime
sudo apt install ./dists/seriald_1.7.4_amd64.deb
```

For an arm64 host, build on the matching native runner with `--arch arm64` and
the corresponding runtime/output paths. Debian assembly is offline once the
runtime is prepared; a missing or invalid runtime stops the build. See the
[installation guide](docs/INSTALL.md) for details and source use on other
architectures. The legacy `seriald_latest_all.deb` download remains an amd64
compatibility alias; its package architecture is no longer `all`.

On Windows, build with Python 3.12 x64 and Inno Setup 6.5+ using
`Build-seriald.cmd`, then run the generated Setup EXE. Upgrades retain runtime
`data/`. Installed users do not need Python. See the Windows guide for download,
LAN access, firewall approval and portable ZIP alternatives.

```sh
python3 serialctl.py --url http://127.0.0.1:8898 devices
```

The web UI can generate agent access instructions with hosted client and skill
links. Serial input is shared; coordinate interactive work with other users.

The collector checks host serial handles every 30 seconds and verifies idle
Linux consoles with an hourly round trip. Busy or owned input defers verification;
failed probes remain visible on device cards. Automatic setup and transfer
protocol echoes stay out of shared screens and archives; errors and kernel
messages remain visible.

## Board support and defaults

Physical-board validation currently focuses on **SpacemiT K3**. Configurable
serial and Linux interfaces do not imply validation on other board families.

| Board family | Validation status |
| --- | --- |
| SpacemiT K3 (Deb1, Pico ITX / Com260) | Current physical-board test scope; not every board/OS/kernel combination has been validated. |
| SpacemiT K1 / Milk-V Jupiter / MUSE-Pi | Historical identity-probe observations only; not a validated full-function support claim. |
| Other boards | Not validated; test serial framing, identity, login, SSH and file transfer before relying on them. |

Built-in defaults follow the K3/Bianbu workflow: **115200 baud, 8N1, no flow
control, `root` / `bianbu` for console and initial SSH login, and Linux identity
probing**. These are generic settings, not automatic model detection. Override
credentials and framing for other OS images. Successful web SSH connections
remember verified credentials on the server for that device; use **SSH settings**
to change them. Normal connections do not set root passwords or edit sshd policy;
root access can be configured by the user with the separate serial-console guide.
See [configuration](docs/CONFIGURATION.md) and the
[historical identity matrix](docs/DEVICE_IDENTITY_MATRIX.md). The identity matrix
records specific observations, not full-function certification.

## Scope

Ubuntu service installation and native Windows hosting are available. CI and
simulated transports are separate from physical-board validation. Firmware-only
targets can use manual mode without Linux shell probes; unconfirmed identities
remain provisional.

The service is intended for local or trusted-LAN development. Authentication is
optional. Explicit SSH setup and manual root configuration can modify a
development board, and SSH host keys can change after flashing. See
[deployment scope](SECURITY.md).

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) describes hardware-free tests, module
boundaries and release maintenance. Do not start a second collector on active
devices.

## License

Application code and original branding: [Apache-2.0](LICENSE).
Bundled and build dependencies retain their licenses; see [THIRD_PARTY.md](THIRD_PARTY.md).

[Web 与硬件后端分开部署](docs/DISTRIBUTED.md)：Windows / Ubuntu 双角色、HTTPS 节点链路、后端镜像缓存与预备刷机环境。
