# DeckLab

DeckLab is a development-board workbench. A control host connects to boards over
serial and USB, keeps logs while browsers are closed, and exposes terminals,
flashing, desktop sessions and automation to the people who use those boards.

[简体中文](README.zh-CN.md) · [Documentation](docs/index.md) · [Releases](https://github.com/qianqiu-2020/decklab/releases/latest)

## Install and open a board

Download the [Windows x64 installer](https://github.com/qianqiu-2020/decklab/releases/latest/download/decklab-windows-x64-setup.exe)
or the [Ubuntu amd64 package](https://github.com/qianqiu-2020/decklab/releases/latest/download/decklab_latest_amd64.deb).
Install it on the computer that is physically connected to the boards. On Ubuntu,
run `sudo apt install ./decklab_latest_amd64.deb`. On Windows, run the installer
and start DeckLab from the Start menu. Open `http://127.0.0.1:8898/` on that
computer. For access from another computer, use the authenticated LAN address
shown by DeckLab.

Connect a board's serial adapter to capture its console. Connect its USB flashing
cable as well if you need to load an image. The two links are independently
identified because they can appear at different times and serve different
purposes. The [device identity guide](docs/modules/device-identity.md) explains
how to confirm that both belong to the same board.

For existing installations, use the [Linux upgrade instructions](docs/operations/install-linux.md#upgrade-an-existing-installation)
or the [Windows upgrade instructions](docs/operations/install-windows.md#upgrade-an-existing-installation).

## Choose the operation

| I need to… | Where to start |
| --- | --- |
| Follow a boot, recover a console or compare two runs | [Serial sessions and archives](docs/modules/serial-and-ssh.md) |
| Write an image, select a disk or recover NOR | [Flashing](docs/modules/flashing.md) |
| Select an image from a shared repository | [Image store](docs/modules/image-store.md) |
| Open a board's graphical session | [Remote desktop](docs/modules/remote-desktop.md) |
| Run a test or give an agent access to one board | [Tasks](docs/modules/tasks.md) and [agent access](docs/modules/agent-access.md) |
| Put the browser service and hardware controller on different hosts | [Distributed nodes](docs/modules/distributed-nodes.md) |

## Try it or build it

Run `python3 decklab_demo.py` to open a hardware-free workspace at
`http://127.0.0.1:8899/`. It provides simulated boards and logs for exploring
the interface. [Demo guide](docs/operations/demo.md)

See the [architecture](docs/modules/architecture.md) for code ownership and the
[complete build guide](docs/development/complete-build.md) for Linux and Windows
build inputs. The [contribution guide](CONTRIBUTING.md) lists the checks for
application changes. Review the [license](LICENSE) and
[third-party notices](THIRD_PARTY.md) before redistributing packages.
