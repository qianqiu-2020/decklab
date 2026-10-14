# Contributing to DeckLab

DeckLab is a shared development-board workbench for OS/BSP developers. Keep
installation and everyday operation simple. Read `docs/ARCHITECTURE.md` before
changing ownership or persistence rules. The [documentation index](docs/README.md)
lists current guides; Git history retains completed plans and old investigations.

## Layout

- `seriald_core/`: collector, device identity, sessions, storage and HTTP services.
  `console_protocol.py` owns login/framing/provisioning dialogue over existing
  connection buffers and guarded writes; it must not open another serial reader.
  `serial_log.py` owns archive text state and writing; Device retains the live
  byte buffer. Identity migration and retention use the writer's same lock.
  `terminal_backend.py` connects to one isolated Node parser process;
  `terminal_worker.js` owns per-stream xterm state and live protocol replies.
  `terminal_snapshot.js` isolates the pinned xterm snapshot adapter.
  HTTP collectors receive their Manager through `server.manager`; importing
  `web.py` must not initialize runtime state or read board profile files.
- `seriald.py` / `seriald_windows.py`: Linux control and Windows host entry points.
- `index.html`, `static/`: UI layout, behavior and original assets.
  `devices.js`, `terminal-view.js`, `archives.js` and `terminal-navigation.js`
  own directory, terminal lifetime, archive selection and buffer navigation;
  `app.js` connects them through injected callbacks. Keep stale-view/request
  checks when adding asynchronous work.
- `vendor/`: bundled terminal libraries; do not patch them for application changes.
- `serialctl.py`, `skills/`: standalone client and agent guidance.
- `tests/`, `tools/`, `packaging/`: regression checks and release tooling.

## Development checks

Use Python 3.10+ on Linux, Python 3.12 x64 for Windows packaging, and Node.js 22
for development/browser checks. The installed collector also needs Node (Ubuntu
22.04's packaged Node 12 is supported); Windows installers bundle it.
Backend tests that exercise Windows adapters need pySerial
and Paramiko even on Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pyserial paramiko
python -m unittest discover -s tests
python tools/check_public_docs.py
node tests/frontend.test.js
node tests/share.test.js
python -m compileall -q seriald_core serialctl.py seriald.py
bash -n rescue.sh
git diff --check
```

`node tools/browser_smoke.js` uses an isolated HTTP fixture and real Chrome;
`node tools/demo_smoke.js` exercises the real HTTP/SSE service with simulated
boards. Run `python3 seriald_demo.py` for interactive hardware-free development;
see `docs/DEMO.md` for its supported commands and virtual files.
`node tools/shared_terminal_smoke.js` runs real Vim on disposable local PTYs
with two differently sized browser tabs. It needs Linux and Vim, and never
discovers boards or opens hardware. It checks serial/SSH late joins, paste,
takeover, resize, redraw and alternate-screen exit.
set `CHROME_BIN` if Chrome is not at `/usr/bin/google-chrome`. No test should
open a real serial port, execute recovery, or modify a board. Never start a
second collector on an active host.

`python3 tools/test_deb_container.py --image ubuntu:24.04` performs actual package
installation, same-version upgrade, conffile/data preservation, service-account
API/CLI startup, independent control restart and purge in a disposable Docker
container. Use `ubuntu:22.04` for the other supported baseline. It downloads the
image/dependencies, exposes no host devices/mounts/ports and removes only its own
container. It does not test systemd activation or privileged USB recovery.

For actual systemd lifecycle checks:

```sh
python3 tools/test_deb_vm.py --image /path/to/ubuntu-cloud.img --sha256 <provider-hash>
```

Obtain an Ubuntu 22.04/24.04
amd64 cloud image and matching SHA256SUMS from the official provider. The tool
requires QEMU, qemu-img, genisoimage and OpenSSH clients. It defaults to KVM;
`--accel tcg` selects software emulation. A temporary overlay, SSH key and
loopback-only SSH port isolate the guest; no physical devices or host directories
are shared. Actual package/service startup, unchanged/changed collector upgrade
decisions and purge are checked. The changed package is a temporary fixture,
not a release version. Image/dependency downloads and sufficient VM resources
are required; this is separate from the fast hardware-free regression suite.

Use four spaces and snake_case in Python, two spaces and camelCase in JavaScript.
Keep one serial reader per connection; writers must not reconfigure termios.
Preserve connection generations and expected serial checks across asynchronous
operations. Add focused coverage for changed behavior, especially cancellation,
reconnection, stale callbacks, framing and archive ownership.

## Pull requests

For version tags, release drafts and dependency updates, follow the
[release guide](docs/RELEASING.md).

Explain the problem, resulting behavior, and validation. Include screenshots for
UI changes and identify any schema/API migration. Link an issue when available.
Existing commit subjects use `seriald:` or `FIXES:` followed by an action; English
or Chinese descriptions are welcome. Never attach passwords, bearer tokens,
private board logs or workplace network details.

## Interface and assets

Keep the terminal central, retain archive lists while browsing, and avoid blocking
unrelated devices during transfers. Use semantic colors in `static/app.css`,
system UI fonts and a consistent monospace terminal font. Preserve xterm's scroll
layer, bottom-row space and full-screen firmware behavior. Check keyboard focus,
contrast, light/dark themes and narrow screens with the isolated browser fixtures.
Update only the needed Tabler symbols; preserve `static/icons-LICENSE.txt`.
Original logo sources and Windows icon handling are described in `docs/BRAND.md`.
UI translations live in `static/i18n.js`. Add translations for new labels and
messages; mark user-provided text with `data-i18n-ignore`. Terminal output, log
contents, commands and board labels must retain their original bytes/text.
