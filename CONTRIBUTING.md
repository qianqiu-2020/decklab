# Contributing to DeckLab

DeckLab is a shared development-board workbench for OS/BSP developers. Keep
installation and everyday operation simple. Read `docs/ARCHITECTURE.md` and
`docs/OPEN_SOURCE_PLAN.md` before changing ownership or persistence rules.

## Layout

- `seriald_core/`: collector, device identity, sessions, storage and HTTP services.
  `console_protocol.py` owns login/framing/provisioning dialogue over existing
  connection buffers and guarded writes; it must not open another serial reader.
  `serial_log.py` owns archive text state and writing; Device retains the live
  byte buffer. Identity migration and retention use the writer's same lock.
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
for browser checks. Backend tests that exercise Windows adapters need pySerial
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
set `CHROME_BIN` if Chrome is not at `/usr/bin/google-chrome`. No test should
open a real serial port, execute recovery, or modify a board. Never start a
second collector on an active host.

Use four spaces and snake_case in Python, two spaces and camelCase in JavaScript.
Keep one serial reader per connection; writers must not reconfigure termios.
Preserve connection generations and expected serial checks across asynchronous
operations. Add focused coverage for changed behavior, especially cancellation,
reconnection, stale callbacks, framing and archive ownership.

## Pull requests

Explain the problem, resulting behavior, and validation. Include screenshots for
UI changes and identify any schema/API migration. Link an issue when available.
Existing commit subjects use `seriald:` or `FIXES:` followed by an action; English
or Chinese descriptions are welcome. Never attach passwords, bearer tokens,
private board logs or workplace network details.
