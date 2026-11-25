# Repository Guidelines

## Project Structure & Module Organization

`decklab.py` and `decklab_windows.py` start the Linux collector and Windows host. Application modules live in `decklab_core/`, browser code in `static/` and `index.html`, dependencies in `vendor/`, and release tooling in `tools/` and `packaging/`. Start with the [documentation index](docs/index.md) and the [architecture guide](docs/modules/architecture.md).

## Build, Test, and Development Commands

Use `python3 decklab_demo.py` for local development with simulated devices. Use the real collector only on an idle development host. Run the checks related to your changes before packaging.

- `python3 -m unittest discover -s tests` runs backend tests.
- `node tests/frontend.test.js` runs frontend tests.
- `python3 tools/check_public_docs.py` checks public configuration and routes against their references.
- `node tools/demo_smoke.js` exercises the Web UI, API and simulated devices in Chromium.
- `bash -n rescue.sh` checks shell syntax without performing recovery.
- `git diff --check` catches whitespace errors before submitting.

See the [build guide](docs/development/complete-build.md) for Linux and Windows package commands, reviewed inputs and clean build procedures.

Local Debian packages go directly in `dist/`, using versioned filenames such as
`decklab_1.7.5~web8_amd64.deb`; do not create a per-build delivery subdirectory.
Keep build inputs and diagnostic records separate from the delivered packages.

## Coding Style & Naming Conventions

Use four-space indentation in Python, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Follow existing two-space indentation and `camelCase` functions in JavaScript. Preserve nearby formatting and explanatory comments; no formatter or linter is configured. Avoid editing bundled `vendor/` files for application changes.

## Testing Guidelines

Python tests use `unittest` and `unittest.mock`, with files named `tests/test_*.py` and methods named `test_*`. Add focused regression coverage for changed behavior, especially reconnects, concurrency, protocol framing, and stale frontend callbacks. Use mocks and temporary directories; tests must not write to real boards or perform USB recovery. No coverage percentage is configured. Run both suites for API/UI changes.

## Commit & Pull Request Guidelines

History uses concise, action-oriented Chinese subjects prefixed with `decklab:` or `FIXES:`. Follow those scopes where appropriate. PRs should describe the problem, resulting behavior, and validation commands/results; link relevant issues and include screenshots for visible UI changes.

## Public Branch

`main` is the public branch: every commit, file, commit message and author identity on it is published, and every push to any remote that carries `main` or its tags is a public release of that content. Private network addresses, internal domains and hostnames, credentials and tokens, and descriptions of internal repository or infrastructure layout must never enter the repository, including tests, docs, commit messages and binaries. Use placeholders (`example.test`, documentation address ranges, generated dummy credentials) instead. Once something reaches GitHub it can only be withdrawn by deleting the whole repository; force-pushing or rewriting history does not remove it from forks, caches or direct commit links. Every push must pass `tools/check_public_push.py`: run `sh tools/install_hooks.sh` once per clone, which installs a pre-push hook for all remotes, and never bypass it with `--no-verify`. If the scanner reports a hit, remove the content and rewrite the unpushed commits; add a `tools/public_push_allowlist.txt` entry only for reviewed placeholder or already-public values, and keep internal domain patterns in the local, uncommitted `.git/info/public-push-deny`.

## Branches & Releases

Keep a single long-lived branch, `main`; GitHub `main` must always fast-forward to it and is never force-pushed. Do not create `release/*` or version-named branches (such as `decklab-1.17.40`). A release is an annotated `v<version>` tag on a `main` commit, following [the release guide](docs/operations/release.md). For isolated release acceptance use `git worktree add --detach` and remove the worktree once the release is published. Delete short-lived task branches after they merge. Push tags one by one, never with `--tags`; local archive tags stay local.

## Serial Safety & Configuration

Keep exactly one reader per port; writes use separate write-only descriptors. Configure termios only when opening the collector, never from writers. Do not start another collector against active devices. `DECKLAB_BASE` selects runtime storage; `DECKLAB_STATIC` selects static assets. Recovery helpers must remain root-owned and non-writable by the service account.
