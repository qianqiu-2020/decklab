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
| Frontend boundaries | Split device, terminal, archive and navigation state; preserve stale-response protection | Existing isolation suites and real-browser smoke | Implemented; native Windows package execution pending |
| Storage upgrades | Explicit schema version and forward migrations | Upgrade fixtures, failure/rollback and backup checks | Implemented; native Windows execution pending |
| Collaboration | Optional input ownership with spectators, release and human takeover | Concurrent clients and no interleaved transaction input | Implemented; native Windows execution pending |
| Logs/diagnostics | Optional retention, disk warning and sanitized diagnostic export | Keep-current-file tests, low-space and export redaction fixtures | Implemented; native Windows execution pending |
| Long operations | Persistent task status, progress, cancellation and browser reconnect | Task API and UI lifecycle tests | Identify/SSH task API/CLI/browser and collector-owned copy API/CLI implemented; upload/download task API implemented; background CLI and browser transfers implemented; CLI result retrieval implemented; CLI orchestration consolidated; final audit pending |
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
- Per-client collaboration: browser requests now carry a fresh per-page ID;
  reservations match actor plus human client, and leases/transfer state retain
  that identity across background work. Different human clients cannot write or
  release another page's reservation, and explicit takeover cancels old work.
  Agent grants still bind actors independently of supplied human client IDs.
  All 358 backend tests and 54 frontend isolation checks pass, plus sharing and
  desktop/mobile browser smoke. A real two-page demo check proves input rejection,
  takeover, spectator transition and restoration of shared input. Native Windows
  execution remains pending.
- Log operations: opt-in daily archive retention protects active serial/SSH
  writers, current cross-day boot parts, unknown boot boundaries and replaced
  files. An unknown active archive location aborts cleanup conservatively.
  Disk warnings do not disable terminal input; CLI/API diagnostic exports use
  an allowlist and single-board grants cannot access platform diagnostics.
  All 369 backend tests, 55 frontend isolation checks and sharing checks pass.
  The real demo browser verifies terminal input/SSE, archives and independent
  client takeover. Native Windows execution remains pending.
- Frontend navigation boundary: terminal-buffer search, echoed-command indexing,
  navigation rendering and decoration work moved into an injected module. Old
  buttons and dispatched timers cannot affect a replacement terminal. Production
  static allowlist/cache rules include the module; demo HTTP checks every script
  referenced by the page. All 369 backend tests, 56 frontend isolation checks,
  sharing and demo browser checks pass. Desktop/mobile smoke passed on repeat;
  its first attempt failed the native scrollbar timing assertion. Device,
  terminal lifecycle and archive state extraction remain pending.
- Archive controller: date/boot selection, list refresh and all-boot search now
  own their state outside app.js and depend on injected current-view, panel and
  archive-opening callbacks. Selection resets for a changed serial even if the
  label is reused. Detached controls and superseded search buttons are guarded;
  opening a search result attaches its query to the replacement archive terminal.
  All 369 backend tests, 58 frontend isolation checks and sharing checks pass;
  demo and desktop/mobile browser smoke pass. Device and terminal lifecycle
  extraction, durable tasks and native Windows release validation remain pending.
- Terminal lifecycle controller: owns the current xterm view and generation,
  invalidates it before cleanup, and encapsulates stream/poll reads, ordered input,
  geometry, resize negotiation and firmware redraw. Device/session catalogs and
  navigation callbacks are injected; app.js reads the controller's current view.
  Closing a view stops queued input and late SSE fallback. All 369 backend tests
  (including extracted-package module HTTP checks), 59 frontend isolation checks
  and sharing checks pass; demo and desktop/mobile browser smoke pass. Device
  controller extraction remains required to finish the frontend workstream.
- Device controller: directory, selected identity, filtering, poll publication,
  metadata refresh, bookmark selection and identity adoption now own their state
  outside app.js. Only the latest device poll publishes results/errors; ownership
  revisions still prevent a poll from undoing an explicit input claim. App wiring
  reads controller state instead of retaining duplicate directory/view globals.
  All 369 backend tests, 60 frontend isolation checks and sharing checks pass;
  demo and desktop/mobile browser smoke pass. Extracted-package HTTP checks verify
  all new modules and cache headers. The four requested frontend boundaries are
  implemented; native Windows package execution and durable task/UI work remain.
- Durable task foundation: registry schema 3 adds task records and historical
  targets with pre-upgrade snapshots. Bounded workers persist progress/results;
  restart marks unfinished work interrupted rather than replaying commands.
  Cancellation hooks wake cooperative runners without holding coordinator locks;
  completed work can win a cancellation race without claiming rollback. Scoped
  history rejects cross-board tasks involving any unshared target. Full backend
  regression passes 379 tests; final focused task checks pass all nine tests,
  including nested credential-result rejection. See TASKS.md for the internal
  contract. Live operation adapters, HTTP/CLI and browser recovery remain required.
- Task operation/API integration: collector owns a task manager; identify,
  SSH-enable and SSH-connect run through DeviceActions with retained actor/client
  identity and original connection/stop generation. Cancellation wakes serial
  waits and prevents later leases; newly connected SSH sessions are cleaned up
  on cancellation while reused sessions remain. Task API/CLI expose start,
  history, status and cancel. Control pins agent actor/board scope, blocks
  privileged task kinds for shared agents and revocation cancels active work
  independently of history pagination. All 389 backend tests, 60 frontend checks,
  sharing and demo browser checks pass; real demo HTTP/CLI rediscovers an async
  identify result through a separate invocation. Browser recovery and server-owned
  file-transfer tasks remain pending; native Windows validation remains pending.

- Browser tasks: identification and SSH creation now submit durable operations;
  task history exposes progress, cancellation and completed SSH sessions. Pending
  connection filtering happens before pagination, and completed identification
  remains visible under its discovered serial without rewriting the initial
  snapshot or widening multi-board grants. Network loss retries the same task;
  permanent access loss stops its watcher. Frontend isolation tests cover stale
  histories, cancellation, fresh-page progress and retry behavior; a real demo
  browser verifies completed results survive reload. File-transfer tasks and
  actual Windows/GitHub release gates remain pending.

- Collector-owned copy tasks: the API accepts two pinned connections, validates
  both input owners and retains both historical targets. One-batch prefetch
  overlaps destination writes; peer failures wake and join the reader before
  aborting. CLI `file-copy --background` exits after submission, with a separate
  invocation retrieving the durable result. Tests exercise the actual protocol
  against simulated boards, cancellation/temp cleanup, concurrent prefetch
  failure, overwrite/empty files and scoped authorization. The production demo
  HTTP/CLI regression verifies copied content after the submitting process exits.
  Upload/download staging and browser-owned-copy replacement remain outstanding.
  Verification: 400 backend tests and 65 frontend isolation tests pass, along
  with the sharing suite and real-browser demo reload smoke. No live hardware
  or installed production service was used or changed. Native Windows execution
  and hosted GitHub release jobs remain unverified.

- Browser-owned-copy replacement: the file dialog now submits one persistent
  collector copy and observes its task ID through a shared browser task client,
  removing the duplicate browser chunk pipeline. Closing/reopening leaves the
  task running, cancellation during submission is deferred until the ID exists,
  and late cancellation replies cannot overwrite another transfer. Network read
  failures retry the same task; malformed or revoked responses stop monitoring.
  A real demo browser verifies SHA-256 and history after reload under either
  involved board. Upload/download staging remains outstanding, as do native
  Windows and hosted GitHub release verification.
  Verification: 400 backend tests, 69 frontend isolation tests, sharing tests,
  desktop/mobile browser smoke and real demo UI copy/reload checks pass. The
  generated task-panel screenshot was inspected. No installed service or live
  board was changed.

- Host transfer artifact foundation: registry schema 4 adds scoped temporary-file
  metadata and task ownership with a pre-upgrade WAL snapshot. Collector startup
  recovers artifacts after interrupting unfinished tasks, and its existing loop
  expires idle files. Random internal paths, per-file locks, checked/idempotent
  chunks, sealed SHA-256, bounded quotas/free-space checks and task claims support
  the forthcoming upload/download adapters. Restart returns unconsumed uploads
  without replay, fails partial downloads, and detects file/DB write gaps. HTTP
  staging and upload/download execution/UI/CLI remain outstanding.
  Verification: 422 backend tests, 69 frontend isolation tests, the sharing
  suite and real demo browser smoke pass. Artifact tests cover quotas (including
  empty-file slots), scope isolation, idempotent writes, disk/commit/publication
  failures, missing files, active readers and restart recovery. Schema-3 upgrade
  fixtures prove task preservation and the schema-4 pre-upgrade snapshot. Debian
  package tests verify the new storage guide is shipped; Windows packaging
  definitions include it, but native Windows validation remains outstanding.

- Upload/download task API: scoped staging endpoints accept bounded 1 MiB host
  chunks (only append expands the body limit to 2 MiB), without reserving UART.
  A shared transfer port guard now pins actor/client, serial, connection and stop
  generation for copy/upload/download, including cleanup. Uploads validate staged
  hashes before remote commit and retain valid sources after cancellation;
  downloads reserve space and validate remote/host content before publication.
  Task history exposes completed download links; proxy actor/serial pinning and
  revocation protect streamed files. Production demo HTTP tests upload staging,
  start both tasks, retrieve results from a separate CLI process and compare bytes
  and SHA-256. Browser upload/download initiation and CLI local artifact retrieval remain outstanding, as do
  actual native Windows and hosted GitHub release gates.

- Background upload/download CLI: `file-put --background` performs bounded host
  staging and sealed checksum verification before collector submission;
  `file-get --background` returns a durable task whose verified file is available
  in browser task history. Lost submission responses are never replayed or used
  to delete potentially claimed artifacts. Browser-dialog initiation, CLI local
  artifact retrieval, and final removal of duplicated foreground orchestration
  remain outstanding.

  Verification for the upload/download task and CLI checkpoint: 443 backend
  tests, 70 frontend isolation tests and sharing tests pass. Real demo browser
  checks verify copy/reload history and a SHA-256-checked download link; desktop,
  laptop, dark, expanded and mobile browser smoke passes. Production demo HTTP
  tests submit upload/download from exiting CLI processes, query their task
  results from new processes and verify downloaded bytes/checksum. Syntax and
  whitespace checks pass. No installed service or real board was touched.

- Browser upload/download task integration: removed per-request UART transfer
  loops from the dialog. Uploads stage bounded 1 MiB host chunks before submission;
  closing the panel keeps staging/monitoring, while explicit cancellation cleans
  incomplete staging or cancels the submitted task. Downloads publish scoped
  streamed save links, removing the browser Blob/128 MiB limit. Uncertain task
  submission never replays or deletes potentially claimed uploads. Original
  board/generation remains pinned when the dialog reopens from another device.
  Verification: 443 backend and 74 frontend isolation tests plus sharing tests
  pass. Real demo browser submits copy/upload/download through the dialog, closes
  the running download, reloads history and verifies file size and SHA-256.
  CLI artifact retrieval, legacy orchestration consolidation, identity-history
  audit, hardware extensions and native Windows/hosted release gates remain.

  Desktop/mobile smoke passes on rerun. Its desktop scrollbar assertion failed
  once after mobile-to-desktop emulation; the unchanged rerun passed. This is
  retained as an unresolved browser-test timing concern, rather than claiming
  that one successful run proves the assertion deterministic.

- CLI task-file retrieval: `file-fetch <task-id> --local <path>` streams a
  completed download without UART IO, forwarding token and expected identity.
  Task metadata, response length/hash and actual bytes must agree before fsync
  and atomic publication. Existing targets need explicit overwrite; competing
  local creation cannot be overwritten without it. Tests cover corrupt, short,
  oversized, empty and malformed responses, revoked access, overwrite and local
  creation races. Real demo HTTP submits tasks from one CLI process and retrieves
  bytes from a later process. Legacy CLI orchestration consolidation, pending
  identity-history audit and platform/release gates remain outstanding.

  Verification: 452 backend tests, 74 frontend isolation tests and sharing tests
  pass; syntax/whitespace checks pass. The production demo HTTP test retrieves
  a completed download with an independent CLI process and checks its bytes and
  SHA-256. No installed service or real board was changed.

- CLI transfer orchestration consolidation: removed duplicate upload/download
  chunk loops, client-side copy prefetch and abort helpers. Default commands
  submit the same collector task as `--background`, wait for its ID and retrieve
  completed download artifacts. Ctrl-C requests task cancellation and reports
  confirmed/unconfirmed status; polling failures report the original ID without
  replay or implicit cancellation. Retired CLI-local shell tests are replaced by
  collector-owned incompressible/no-gzip shell coverage, existing task prefetch
  overlap tests, and actual demo HTTP/CLI upload/copy/download checks. Pending
  identity-history, hardware-extension and platform/release audits remain.

  Verification: 455 backend regression tests, 74 frontend isolation tests and
  sharing tests pass. Extended real demo HTTP/CLI tests also pass after adding
  default upload/copy/download end-to-end byte/hash checks. Real demo browser
  validates all transfer modes and reload retrieval. Syntax/whitespace checks
  pass; no installed service or live board was changed.
