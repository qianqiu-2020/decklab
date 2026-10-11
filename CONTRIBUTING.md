# Contributing to DeckLab

DeckLab is a shared development-board workbench for OS/BSP developers. Keep
installation and everyday operation simple. Read `docs/ARCHITECTURE.md` and
`docs/OPEN_SOURCE_PLAN.md` before changing ownership or persistence rules.

## Layout

- `seriald_core/`: collector, device identity, sessions, storage and HTTP services.
- `seriald.py` / `seriald_windows.py`: Linux control and Windows host entry points.
- `index.html`, `static/`: UI layout, behavior and original assets.
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
node tests/frontend.test.js
node tests/share.test.js
python -m compileall -q seriald_core serialctl.py seriald.py
bash -n rescue.sh
git diff --check
```

`node tools/browser_smoke.js` uses an isolated HTTP fixture and real Chrome;
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
