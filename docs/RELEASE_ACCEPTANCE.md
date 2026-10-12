# Release acceptance audit

Historical accepted source version: 1.5.1, reviewed 2026-10-12. This separates
implemented behavior from platform/release evidence. Local tests use temporary
storage, simulated boards and fake transports; they do not prove real COM or
installed Windows behavior. This record describes the independently inspected
immutable 1.5.1 artifacts, not subsequent main-branch changes. Earlier intermediate
checkpoints remain in Git history.

## Requirement evidence

| Requirement | Implementation and focused evidence | Current assessment |
| --- | --- | --- |
| Public project | LICENSE, NOTICE, THIRD_PARTY.md, bilingual README, CONTRIBUTING.md, SECURITY.md and issue/PR templates; package tests | Source/native notice review and actual Windows distribution checks pass |
| Releases | CI, Windows package and tagged release workflows; version-tag validator; Debian extraction fixtures | Tagged Linux/Windows/publish jobs pass; downloaded draft packages/checksums/notes verified |
| Configurable hosts/boards | profiles.py, transport.py, windows_manager.py; test_profiles.py validates framing, profiles, manual mode and credentials | Local contracts verified |
| Hardware-free demo | demo.py, seriald_demo.py; test_demo.py and demo_smoke.js exercise normal HTTP/CLI/browser paths | Linux demo/browser checks and frozen Windows API/archive smoke pass |
| Board/connection/session separation | models.py, devices.py, session_base.py; model, registry and session-contract tests | Local reconnect and migration contracts verified |
| Console coordination | transactions.py, console_protocol.py; transaction, protocol, handoff and input-ownership tests | Local timeout/cancel/generation contracts verified |
| Platform adapters | sessions.py, sessions_windows.py, windows_manager.py; shared session and fake Windows tests | Linux fixtures and native Windows selected suites pass |
| Application services/API | actions.py, queries.py, results.py, API.md; action/query/HTTP/CLI tests | Implemented; 54-route source/contract content review complete locally |
| Frontend ownership | devices.js, terminal-view.js, archives.js, terminal-navigation.js; frontend.test.js | Isolation checks verified; desktop/mobile browser fixture passes five consecutive runs after synchronization fix |
| Schema upgrades | schema.py registry migrations through version 5; test_schema.py checks WAL backups, rollback, concurrent openers and future versions | Linux and native Windows upgrade/backup fixtures pass |
| Collaboration | transactions.py, agent_grants.py, control_server.py; input-ownership, grants and sharing tests | Local reservation/revocation/takeover verified |
| Log operations | maintenance.py; maintenance/archive/identity tests cover active writers, cross-day boots, disk warnings and allowlisted diagnostics | Linux and native Windows maintenance fixtures pass |
| Long operations | tasks.py, task_actions.py, transfer_tasks.py, transfer_artifacts.py; task/file/CLI tests and demo browser upload/download/copy/reload | Local task and artifact workflows verified |
| Hardware extensions | hardware.py, process_windows.py, exclusive_serial handoff; helper and handoff tests | Linux/fake contracts and native Windows Job/helper/handoff suites pass |
| Distributed documentation | package_docs.py; test_package_docs.py checks both layouts, examples, assets and UTF-8 links | Source content, layouts, installed guides and final tagged artifacts reviewed |

## Final tagged 1.5.1 acceptance

Tag `v1.5.1` points to `4c30196b56f543365ade231055b973e7172d7350`.
The [tagged workflow](https://github.com/qianqiu-2020/decklab/actions/runs/38201924308)
passes Linux regression/package installation, native Windows build/source
replacement/host/demo/installer checks and draft publication. Its publication
job independently requires the native source catalog and matching Paramiko
installed/source version before creating the draft. The six downloaded assets
also pass the same verifier outside the publishing job. Release notes match the
tagged 1.5.1 changelog section exactly.

| Final package | SHA-256 |
| --- | --- |
| `seriald_1.5.1_all.deb` | `6980b73aa8a69e951611d64fa16264fddd2d96faa9d98e6711214944b695d417` |
| `seriald-1.5.1-windows-x64-setup.exe` | `6284d528e2f132662bfe61dbd2c26c3bb715b4f71fd7475f9b4b962f554f0ab2` |
| `seriald-windows-portable.zip` | `06c807c759d4001fd158ca4b545dc8db870b060eff461ba9544fe2c7078c57c2` |

Independent inspection verifies package versions, Debian architecture, all
362 Windows bundle files, 16 distribution notice sets, provider/source/notice
hash bindings and the corresponding-source archive. Candidate regression passes
579 backend tests (three Windows-native cases skipped on Linux and exercised
on Windows), 74 frontend isolation checks and sharing checks. Six release
verification fixtures check malformed checksum/path sets, metadata/version
mismatches and missing source supplements. Repeat on Ubuntu:

```sh
gh release download v1.5.1 --dir /tmp/decklab-final-review
python3 tools/verify_release.py /tmp/decklab-final-review --version 1.5.1
```

The completed optimization workstreams have implementation and scoped
acceptance evidence in the original Git checkpoint. Artifacts intentionally
remain a reviewable draft, as
configured by the release workflow; publication is not an additional automatic
step. The reviewed notice inventory is a documented coverage superset, not an
exact activated dependency SBOM. Physical UART/USB recovery is covered by the
stated fixtures/support matrix, not by an empty-port hosted runner. Linux
systemd lifecycle has actual isolated Ubuntu 22.04/24.04 VM evidence in the
[original full record](https://github.com/qianqiu-2020/decklab/blob/f0ffcbc/docs/RELEASE_ACCEPTANCE.md).
No production service or live board was changed during acceptance.
