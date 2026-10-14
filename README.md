# DeckLab

[简体中文](README.zh-CN.md) · [Documentation](docs/README.md) · [Installation](docs/INSTALL.md) · [Windows](docs/WINDOWS.md) · [Agent access](docs/AGENT_ACCESS.md)

A shared development-board workbench for OS and BSP developers. Capture serial
output continuously, access boards through a browser or agent, and keep boot
history attached to a stable board identity rather than a changing tty number.

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

## Install and use

![DeckLab architecture](docs/seriald-architecture-en.png?v=english-1)

Try without hardware: `python3 seriald_demo.py`, then open
**http://127.0.0.1:8899/**. See the [demo guide](docs/DEMO.md) for simulated
boot comparison, SSH and file transfers.

Ubuntu packages are built with:

```sh
python3 tools/build_deb.py
sudo apt install ./dist/seriald_1.6.0_all.deb
```

On Windows, build with Python 3.12 x64 and Inno Setup 6.5+ using
`Build-seriald.cmd`, then run the generated Setup EXE. Upgrades retain runtime
`data/`. Installed users do not need Python. See the Windows guide for download,
LAN access, firewall approval and portable ZIP alternatives.

```sh
python3 serialctl.py --url http://127.0.0.1:8898 devices
```

The web UI can generate agent access instructions with hosted client and skill
links. Serial input is shared; coordinate interactive work with other users.

## Board support and defaults

Physical-board validation currently focuses on **SpacemiT K3**. Configurable
serial and Linux interfaces do not imply validation on other board families.

| Board family | Validation status |
| --- | --- |
| SpacemiT K3 (Deb1, Pico ITX / Com260) | Current physical-board test scope; not every board/OS/kernel combination has been validated. |
| SpacemiT K1 / Milk-V Jupiter / MUSE-Pi | Historical identity-probe observations only; not a validated full-function support claim. |
| Other boards | Not validated; test serial framing, identity, login, SSH and file transfer before relying on them. |

Built-in defaults follow the K3/Bianbu workflow: **115200 baud, 8N1, no flow
control, `root` / `bianbu` for console and SSH, Linux identity probing, and
automatic SSH configuration enabled**. These are generic settings, not automatic
model detection. Override credentials and framing for other OS images; the
example profile file disables automatic SSH configuration when explicitly used.
See [configuration](docs/CONFIGURATION.md) and the
[historical identity matrix](docs/DEVICE_IDENTITY_MATRIX.md). The identity matrix
records specific observations, not full-function certification.

## Scope

Ubuntu service installation and native Windows hosting are available. CI and
simulated transports are separate from physical-board validation. Firmware-only
targets can use manual mode without Linux shell probes; unconfirmed identities
remain provisional.

The service is intended for local or trusted-LAN development. Authentication is
optional. SSH root provisioning can modify a development board, and SSH host
keys can change after flashing. See [deployment scope](SECURITY.md).

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) describes hardware-free tests, module
boundaries and release maintenance. Do not start a second collector on active
devices.

## License

Application code and original branding: [Apache-2.0](LICENSE).
Bundled and build dependencies retain their licenses; see [THIRD_PARTY.md](THIRD_PARTY.md).
