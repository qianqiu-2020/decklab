# Open-source implementation and acceptance

This tracks the full optimization scope agreed for DeckLab. A working existing
feature is not sufficient evidence for a new requirement. Keep the collector's
single-reader invariant, board/connection distinction and archive continuity.
Do not change an installed service or use live boards during regression tests.

| Workstream | Required result | Acceptance evidence | Status |
| --- | --- | --- | --- |
| Public project | Application license, third-party inventory, English/Chinese README, contribution and issue guidance | Files present; distribution notices included; inventory matches shipped dependencies | In progress |
| Release pipeline | Linux/Windows PR checks; tagged Setup, ZIP, Debian package, checksums and release notes | CI definitions plus validated release outputs; actual platform checks reported separately | In progress |
| Generic configuration | Portable storage defaults; configurable serial framing, credentials, profiles and provisioning policy | Invalid-config tests and cross-platform transport fixtures | In progress |
| Demo | Useful simulated boards, boot history, SSH and transfer scenarios without hardware | HTTP/CLI/browser checks; no serial discovery or real commands | Implemented; native Windows package gate pending |
| Model boundaries | Persistent Board, ephemeral Connection, independent Session | Identity/reconnect/archive migration tests | Implemented; broader transaction integration continues |
| Transactions | Shared coordination for input, probes and transfers; timeout/cancel/generation protection | Concurrent actor, cancellation and replacement tests | Core/API implemented; task/UI integration pending |
| Platform adapters | Common session lifecycle, auditing and logs; Linux/Windows IO isolated | Equivalent contract tests on fake transports | Implemented; native Windows package gate pending |
| Application services | HTTP/CLI share operations; reduce global manager coupling; stable API errors/capabilities | Contract tests through HTTP and direct services | Device/read services implemented; final API audit pending |
| Frontend boundaries | Split device, terminal, archive and navigation state; preserve stale-response protection | Existing isolation suites and real-browser smoke | Pending |
| Storage upgrades | Explicit schema version and forward migrations | Upgrade fixtures, failure/rollback and backup checks | Implemented; native Windows execution pending |
| Collaboration | Optional input ownership with spectators, release and human takeover | Concurrent clients and no interleaved transaction input | Actor reservations/UI implemented; per-client human ownership pending |
| Logs/diagnostics | Optional retention, disk warning and sanitized diagnostic export | Keep-current-file tests, low-space and export redaction fixtures | Pending |
| Long operations | Persistent task status, progress, cancellation and browser reconnect | Task API and UI lifecycle tests | Pending |
| Hardware extension | Configurable power/reset and external flashing interface | Fake driver/process tests; no implicit real-board actions | Pending |
| Documentation | Architecture/config/API/support matrix agree with actual code | Final requirement-by-requirement audit with linked evidence | Pending |

Implementation order: release foundations and configuration, demo, core/service
boundaries, collaboration and operations, then hardware extensions. Use existing
Python/vanilla-JavaScript tooling unless a concrete constraint warrants a change.
Windows can retain a single host process while sharing application services with
the Linux collector/control deployment.

The existing AGENTS.md is preserved under the owner's no-overwrite instruction;
CONTRIBUTING.md documents the current modular repository and test commands.

## Verified checkpoints

- Public-source foundation: LICENSE/NOTICE/third-party summary, bilingual
  landing README, contribution/security/issue guidance and runtime ignore rules
  are present. Transitive Windows license collection and history review remain.
- CI/release definitions parse and the release tag validator accepts the current
  version. Linux/Windows regression and draft tagged release jobs are configured;
  hosted execution remains unverified until the repository runs them on GitHub.
- Source storage now follows the checkout root. All 261 backend tests pass.
- All 51 frontend isolation checks and the sharing suite pass.
- Real Chrome smoke passes on desktop and mobile after fixing the firmware font
  floor and reserving mobile terminal space when records are open. Desktop native
  scrollbar clicks and mobile history scrolling are exercised separately.
- Configuration implementation: validated environment/JSON profiles select by
  board serial or physical port. Linux termios and Windows pySerial use the
  selected framing only when the reader opens. Manual profiles disable shell
  probes and SSH provisioning; separate console/SSH credentials are supported.
  Thirteen focused tests cover invalid configuration, secret omission, actual
  Windows collector calls, Linux framing and SSH credential propagation.
  CONFIGURATION.md and an editable example are included in both packages.
  Native Windows execution remains part of the platform/release gate.
- Demo: seriald_demo.py uses an isolated temporary directory and loopback HTTP,
  real control/collector handlers, board registries and archive indexing. Closed
  virtual shell/file adapters execute no host commands and discover no ports.
  HTTP/CLI tests cover agent execution and board-to-board/download SHA-256 checks;
  real Chromium checks terminal input, SSE output and cross-day archive UI.
  Windows packages expose `seriald.exe demo`; hosted native execution remains
  unverified until the Windows package job runs.
- Model implementation: Manager.boards now contains persistent Board objects;
  Device is the replaceable UART/API connection. Board-owned session registries
  survive re-enumeration without dict copying. Nine focused tests cover actual
  discovery/replacement, new-port identification/archive preservation, immutable
  identity, rename, conflicting attachments, swap isolation and late SSH creation
  reservation release. Existing identity/archive suites remain part of validation.
  Shared transaction coordination remains a separate workstream.
- SSH adapters share SessionBase lifecycle, output offsets, logging, date rotation
  and exactly-once end audit. PTY/process and Paramiko IO stay in platform modules.
  Shared contracts use real temporary log files and fake transports to verify
  UTF-8 fragments, bounded independent read cursors, EOF/no-restart, migration
  during concurrent output and rollback. Registry integration checks committed
  promotion, database failure, cleanup failure and pending-opener exclusion.
  Native Paramiko adapter fixtures cover shell/timeout failures, interrupt races
  and client cleanup. The Windows package job includes these checks; its hosted
  execution is still unverified.
- Serial transactions: input, framed commands/probes, SSH provisioning, transfer
  commands and USB recovery use connection/deadline/cancel leases. HTTP exposes
  active operation status and ID-scoped, actor-checked cancellation. Writer and
  wait guards reject stale generations; cancellation wakes waiters and stops
  remaining Linux short writes. Tests exercise independent boards, deadlines,
  generation/stop-event replacement, provisioning cancellation, HTTP ownership
  and upload cancellation without automatic recovery. Task/UI integration remains.
- Transfer lifecycle: completion, abort and idle expiry cancel the owned timer;
  stale expiry callbacks cannot release a newer transfer. All 316 backend tests
  pass, plus 51 frontend isolation checks, sharing checks and real-browser demo
  input/SSE/archive checks. Windows build-script syntax parses locally; native
  Windows execution and hosted CI remain unverified.
- Storage upgrades: registry and grant databases have independent schema versions
  and application IDs. Legacy adoption and forward DDL/data/version changes are
  atomic, with pre-upgrade SQLite snapshots including committed WAL data. Twelve
  focused tests prove preservation, rollback, backup/publish failure, concurrent
  startup, future-version rejection and grant semantics. Full backend regression
  passed 326 tests before the final two test-only additions; package and demo
  browser checks pass. STORAGE_UPGRADES.md is bundled in both distributions.
  Native Windows execution remains pending.
- Application actions: all device POST operations use an injected DeviceActions
  service returning transport-independent results. HTTP retains parsing/selection,
  control retains authorization and scoped serial/actor pinning. Direct and HTTP
  contract tests cover input auditing, actor scope, SSH permissions and cancellation.
  Capabilities require no board lookup and are available to scoped agents; CLI
  exposes capabilities, operation status/cancellation and stable error codes.
  Full regression passed 334 tests before one final CLI test-only addition; the
  final 26-test CLI suite, frontend/share suites and demo browser pass. Read/archive
  application services and task/UI integration remain pending.
- Read/query services: SessionQueries and ArchiveQueries cover cursor reads,
  session lists, archive/history queries, boot search and cross-board comparison.
  Direct/HTTP contracts use real temporary archives. Boot summaries preserve
  cached partition metadata; shared archive IO rejects links, nonregular files
  and replaced file identities for indexing, preview, search and download.
  Control and collector JSON errors share codes, including authentication.
  All 345 backend tests, frontend/share suites and the real demo browser pass.
  SSE framing and streaming downloads remain HTTP adapter responsibilities;
  final API documentation/audit and task/UI work remain pending.
- Collaboration foundation: optional actor-scoped claims guard all serial
  transaction writes, with id-scoped release, human takeover/cancel and revocation
  cleanup. Serial spectators and independent SSH remain usable. CLI and the
  serial toolbar expose claim/release; stale UI callbacks cannot change another
  device. Full regression passed 354 tests, frontend 53 isolation checks and
  sharing checks; desktop/mobile and demo browser checks pass. Final focused
  ownership/HTTP checks also cover takeover racing with agent revocation.
  Separate ownership for human browser clients remains required before this
  workstream is complete; current browser users share the `web` actor.
