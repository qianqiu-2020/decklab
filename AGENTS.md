# Repository Guidelines

## Project Structure & Module Organization

`seriald.py` contains the Linux serial capture service, device registry, SSH sessions, and HTTP API. `index.html` holds the UI with inline CSS and JavaScript; `vendor/` supplies xterm assets. `rescue.sh` is the privileged USB recovery helper. `tests/` contains backend and frontend regression tests. Read `FIXES.md` for architecture decisions and known limitations. Runtime `logs/`, `labels.json`, and `seriald.log` are ignored by Git.

## Build, Test, and Development Commands

There is no build step or package manager configuration. The backend uses Python 3's standard library; frontend tests require Node.js. SSH sessions also require system `ssh` and `sshpass`.

- `python3 seriald.py 8898 127.0.0.1` — start the service locally on an idle development host; it discovers real serial devices.
- `python3 -m unittest discover -s tests` — run backend regression tests.
- `node tests/frontend.test.js` — run the frontend's custom Node assertion harness.
- `python3 -m py_compile seriald.py` — check Python syntax.
- `bash -n rescue.sh` — check shell syntax without executing recovery.
- `git diff --check` — catch whitespace errors before submitting.

## Coding Style & Naming Conventions

Use four-space indentation in Python, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Follow existing two-space indentation and `camelCase` functions in JavaScript. Preserve nearby formatting and explanatory comments; no formatter or linter is configured. Avoid editing bundled `vendor/` files for application changes.

## Testing Guidelines

Python tests use `unittest` and `unittest.mock`, with files named `tests/test_*.py` and methods named `test_*`. Add focused regression coverage for changed behavior, especially reconnects, concurrency, protocol framing, and stale frontend callbacks. Use mocks and temporary directories; tests must not write to real boards or perform USB recovery. No coverage percentage is configured. Run both suites for API/UI changes.

## Commit & Pull Request Guidelines

History uses concise, action-oriented Chinese subjects prefixed with `seriald:` or `FIXES:`. Follow those scopes where appropriate. PRs should describe the problem, resulting behavior, and validation commands/results; link relevant issues and include screenshots for visible UI changes.

## Serial Safety & Configuration

Keep exactly one reader per port; writes use separate write-only descriptors. Configure termios only when opening the collector, never from writers. Do not start another collector against active devices. `SERIALD_BASE` selects runtime storage; `SERIALD_STATIC` selects static assets. Recovery helpers must remain root-owned and non-writable by the service account.
