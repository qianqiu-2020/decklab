# Changelog

## Unreleased

- Import/export registry-managed labels and port probe overrides through the Web,
  with connection-aware revisions, identity checks, atomic updates and audit.

- Add owner-managed Web settings for all host options and board/hardware/flash
  configuration documents, with validated preview, revision-checked saving,
  redacted sharing and explicit private backups.
- Activate settings from the Web by re-executing existing processes after
  shutdown; preserve the collector for control-only changes and reject collector
  activation during tasks, flash holds, transactions or input reservations.
- Optionally import/export this browser's language and personal device order.

- Discover local download USB devices through the installed flashing executor,
  with explicit target selection, live layout checks and persistent tasks that
  also work without an identified UART connection.
- Add image/partition previews, bounded flashing progress and separate write,
  verification and boot results; preserve resources when cleanup is uncertain.
- Let pending serial connections keep a user label without inventing a board
  identity or merging existing archives.
- Allow dragging or moving device cards, with keyboard controls and a saved
  personal order that survives refreshes and online/offline changes.

## 1.7.4

- Recognize BusyBox ash directory prompts such as `~ #` and `/ #` when
  synchronizing serial terminal dimensions, with the same idle-input and
  ownership checks used for hostname prompts.
- Share backend prompt recognition with internal-output filtering so a
  successful size synchronization does not duplicate the ash prompt.
- Report a distinct disabled-mode error when custom/manual probing prevents
  Linux terminal setup, and verify ash resizing with real BusyBox PTYs in CI.

## 1.7.3

- Remember successful web SSH credentials on the server by board serial, so
  service restarts, USB reconnections and port changes reuse the same account.
- Prefer saved credentials and update them only after a successful web login;
  failed attempts and stale connections preserve the existing configuration.
  Agent connections can reuse saved credentials without replacing them.
- Show saved-account status in SSH settings without returning the password to
  the browser. Unidentified connections remain temporary until a unique board
  identity is established.
- Upgrade the registry to schema 7 with private credential storage and retain
  credentials through explicit board identity corrections and package upgrades.

## 1.7.2

- Try the configured SSH password, then `bianbu` and `debian`, without duplicate
  attempts. Stop on network errors and keep explicitly supplied credentials to
  one authentication attempt.
- Offer SSH username and password entry after authentication fails, with
  connection-scoped retries and credential-free task history.
- Replace automatic root password and sshd changes during SSH connection with
  manual, individually copyable serial-console instructions.
- Document Codex, Claude Code and Orca Web MCP configuration and connection checks.
- This collector update restarts the collector during package upgrade while
  preserving device configuration and recorded runtime data.

## 1.7.1

- Include the MCP SDK, Python runtime and dependencies in architecture-specific
  Debian packages. Install and upgrade without pip or a separately configured
  Python environment; keep the collector on its existing system Python.
- Verify installed remote MCP in clean Ubuntu 22.04 and 24.04 containers with
  networking disabled after package installation.
- Preserve actionable MCP startup errors and bounded worker logs instead of
  reporting every startup failure as a missing SDK.

## 1.7.0

- Keep BusyBox/Buildroot probes below interactive shell input-line limits and
  detect existing SSH services without requiring systemd.
- Configure custom POSIX probe scripts or manual consoles in the browser.
  Identification remains optional for terminal access; no synthetic serial is
  created when an operating system cannot report a hardware identity.
- Share an unidentified live connection with an AI using a revocable grant that
  expires on connection replacement or board switching.
- Add remote MCP access through the DeckLab service with revocable per-client
  grants. AI clients configure a URL and Bearer token; no client-side adapter
  installation is required. Keep stdio available for local integration.
- Generate remote MCP configuration in the browser and expose bounded terminal
  reads, input, SSH execution, task operations and boot history through the
  protected control API.
- Publish stable installer and Debian-package asset names alongside versioned
  files, so download links can follow the latest release automatically.

## 1.6.5

- Recover a previously duplicated board serial from the Identify action: review
  the existing archive, confirm ownership and provide verification details, then
  read the board identity again before retaining its label and continuing logs.
- Reject stale confirmations and active duplicates. Commit conflict resolution,
  its audit reason and the connection binding together; agent grants cannot
  confirm archive associations.
- Add an offline `resolve-conflict` preview/apply command and a packaged Windows
  administration entry point for verified recovery without changing board serials.
- Close read-only registry connections after archive correction and confirmation,
  including validation failures, so Windows can release the database file.

## 1.6.4

- Restore the controller's approved text width when firmware menus hand off to
  Linux, including Debian kernel banners split across serial reads. Preserve
  this preference across clean collector handoffs without sending board input.
- Retry browser presentation sizing after a shared-screen reset, while keeping
  firmware menu coordinates and avoiding repeated requests for a matching grid.

## 1.6.3

- Treat serial-size synchronization's idle-prompt races as waiting, then clear
  its notice when the shared stream confirms the requested grid is configured.
- Ignore delayed setup failures after success, and preserve newer action notices
  and actual input errors. Notice ownership survives UI language changes.
- Read UTF-8 serial archives explicitly in Windows package validation instead
  of relying on the runner's system encoding.

## 1.6.2

- Verify idle Linux serial consoles hourly with a nonce-framed, side-effect-free
  round trip over the existing collector. Defer while input is owned or busy,
  including firmware menus, full-screen applications and file transfers; show
  persistent device-card warnings when the board does not respond.
- Hide automatic terminal setup and file-transfer protocol echoes from shared
  screens and serial archives. Preserve raw protocol bytes and terminal replies,
  file-transfer summaries, unexpected errors and asynchronous kernel messages.
- Restore shared screen state when internal operations finish, preserving updated
  dimensions, input modes, partial prompts and unfinished terminal controls.

## 1.6.1

- Restore shared terminal snapshots through the ordered parser queue, cancel
  incomplete control strings and repaint even when the source remains quiet.
  Switching back from SSH or archived logs no longer needs an extra Enter.
- Keep unchanged terminal fits stable and use current character measurements
  when scaling a shared grid, avoiding repeated font resets and excessive shrink.
- Exercise quiet serial/SSH/archive switching and interrupted control strings
  with the bundled xterm renderer and disposable local PTYs.

## 1.6.0

- Maintain one canonical terminal per serial connection or SSH session in an
  isolated backend xterm parser. Answer live terminal queries once without a
  browser; history and screen snapshots never send protocol replies.
- Restore the screen, alternate buffer, scrolling margins, saved cursor, input
  modes and fragmented UTF-8/control bytes when viewers join or redraw.
  Share ordered presentation corrections without changing raw captures or logs.
- Coordinate shared SSH input and PTY dimensions by actor and browser client.
  Push quiet takeover changes to observers; notify running applications of
  controller resizing. Observers scale the same logical grid.
- Give the Linux sshpass wrapper a controlling PTY so window changes reach SSH
  applications; use an exec helper instead of a threaded preexec callback.
- Finish idle serial shell size/environment setup before queued input. Keep
  active serial applications on their established grid; preserve the screen on
  confirmed re-enumeration and clean same-connection collector handoff.
- Declare Ubuntu's Node runtime dependency and bundle a verified Windows Node
  runtime with its complete upstream license. Add real Vim/local-PTY two-browser
  regression checks and installed-parser checks on supported Ubuntu baselines.

## 1.5.8

- Suppress the far-bottom cursor move used by systemd size probes whenever
  terminal replies are suppressed, keeping boot messages adjacent without
  removing real blank lines or changing firmware menu cursor controls.
- Apply UART geometry outside captured bytes so fragmented OSC/DCS controls,
  shell context metadata and UTF-8 characters remain intact. Wait for queued
  writes and terminal reset before applying replay geometry.
- Send environment setup only after the resize marker and a fresh idle shell
  prompt, preventing the longer setup line from being echoed at the old width.

## 1.5.7

- Clear firmware background, cursor and scroll modes on the earliest kernel
  timestamp, including split headers without a Linux version banner.
- Replay the shared UART geometry from the first captured byte, with subsequent
  resize and APT controls applied in stream order. Smaller viewers scale pixels
  instead of changing logical rows or remapping progress coordinates.
- Fit the complete shared grid to the real viewport with bottom/scrollbar
  clearance. Establish board dimensions before echoing environment setup; run
  the longer environment command only initially or on explicit retry.

## 1.5.6

- Fit early boot and kernel text to the viewport; retain firmware menu coordinates
  and restore normal geometry on Linux boot.
- Automatically synchronize serial shell dimensions at a verified idle prompt.
  Coordinate one sizing authority per connection, preserve shared geometry beyond
  live-buffer eviction, and block stale, typing, owned and busy-console requests.
- Configure visible control-character echo, disable software flow control only
  when the serial profile permits it, enable UTF-8 editing for UTF-8 locales, and
  select xterm-256color only when matching board terminfo is installed.

## 1.5.5

- Add explicit idle-shell size synchronization for wide serial terminals.
  Enable visible control-character echo with `stty echoctl` so echoed arrows
  cannot move the output cursor during installation; retain keyboard passthrough.
  Apply shared resize markers only after successful board-side `stty`; preserve
  dimensions across browser viewers and buffered replay.

## 1.5.4

- Preserve the large live serial viewport while mapping APT's reserved progress
  row and scroll region to its bottom. Restore full-screen scrolling when APT
  finishes, including streamed escape sequences and redraws. Captured logs remain
  unchanged.

## 1.5.3

- Allow vertical wheel scrolling over boot comparison text and inline highlights
  while preserving horizontal scrolling for long lines.
- Provide English and Chinese self-hosted agent skills. Generate handoff text and
  download URLs in the selected UI language; reuse the same grant when switching.

## 1.5.2

- Add live Chinese/English UI switching without reconnecting terminals or changing
  browser input ownership. Honor the Windows installer language as a preserved
  host default; explicit browser preferences take priority.
- Push serial input reservation/release changes through SSE even without new
  output, including metadata packets at unchanged byte offsets.
- Expand boot comparison to nearly the whole viewport and highlight changed
  Unicode characters within lines while ignoring kernel clock differences.
- Echo simulated serial/SSH input before Enter and support deletion and Ctrl-C.
- Rename agent sharing and clarify that web board links grant platform-wide
  access; retain actual single-board scope for agent grants.

- Add an English architecture diagram, demo screenshot and explicit K3 hardware
  validation scope. Consolidate documentation navigation and remove completed
  plans, duplicate fix histories and obsolete generated artifacts.

## 1.5.1

- Add reviewed Windows Rust source-graph notices and CFFI/libffi source headers.
  Pin the catalog to three observed frozen provider hashes; reject stale source
  notices and changed binaries instead of silently reusing old attribution.
- Include observed Rust standard-library copyright summaries and Paramiko's
  corresponding source archive. Allow replacing Paramiko from an explicit source
  archive in the isolated builder and retain that same archive in the output.

## 1.5.0

- Run native Windows package, installer upgrade and simulated-host checks in
  GitHub Actions; fix Unicode pipe output, SQLite backup flushing and Windows
  test-file cleanup discovered by native execution.
- Ship hash-pinned OpenSSL licenses selected from actual frozen provider versions,
  plus reviewed CPython source dependency notices. Preserve upstream notice bytes
  across Windows checkouts and verify both provider and notice hashes.
- Wait for the browser debugging endpoint and retain bounded startup diagnostics
  in isolated UI checks. Publish this changelog section with tagged draft packages.

- Add a disposable cloud-image VM check for actual systemd startup, collector
  fingerprint upgrade decisions and purge/data preservation. Verify the image
  against the supplied provider SHA-256; use no physical hardware or host mounts.
- Add disposable Ubuntu 22.04/24.04 package installation checks covering actual
  dpkg upgrade/purge, conffile/runtime preservation, service-account API/CLI and
  independent control restart. No host serial devices or service changes.
- Record an initial Windows native-wheel attribution audit with exact source,
  binary-member and notice hashes. Ship the observations and remaining review
  steps with documentation; do not treat them as frozen-output verification.
- Complete the local API/configuration content review. Document task capacity,
  cancellation outcomes, artifact retries and expiry separately from retained
  task history; distinguish local contract checks from native release evidence.
- Keep collector health readable when disk status is unavailable, and snapshot
  SSH sessions under their registry lock before collecting logging errors.
- Snapshot board-profile environment defaults and configuration at collector
  startup. Later identity/port selection cannot silently change credentials,
  probing policy or framing; recreate the collector to apply changes.
- SSH root provisioning uses the configured board-profile password instead of a
  hardcoded password. Non-root/empty-password profiles and different per-request
  credentials cannot trigger root provisioning.
- Remove import-time collector Manager initialization. Linux, Windows and demo
  entry points inject one manager into each HTTP server, isolating device state
  and mutations; unconfigured servers return unavailable without lazy startup.
- Extract serial archive writing into a transport-independent component owning
  UTF-8 decoding, partial lines, rotation, file handles and write-error recovery.
  Keep live byte fan-out and identity/retention lock ordering in the connection.
- Inventory and verify all Windows distribution files before ZIP/installer
  packaging, including native libraries, resources, scripts and notices. Reject
  changed, missing or extra files and refuse runtime data or redirected paths.
- Share request JSON validation between control and collector. Reject duplicate
  fields, non-finite numbers, invalid Unicode and excessive nesting before board
  operations; direct task/artifact services also reject non-object submissions.
- Bound POSIX hardware-helper cleanup and verify process-group/thread exit before
  restoring exclusive UART capture, including successful root-before-child exit.
  Failed verification keeps the attachment paused for operator recovery.
- Document host/authentication settings and web-issued agent restrictions,
  including SSH provisioning behavior. Check public route/environment name
  coverage offline during CI and Windows builds without importing collectors.

- Recreate a dedicated Windows packaging environment on every build, keeping
  development environments untouched and excluding leftover development packages
  from dependency and license collection. Installer verification uses the same
  packaging interpreter.

- Run identify, SSH and copy/upload/download as collector-owned durable tasks
  through API, CLI and browser. Preserve progress/results across page reloads;
  check actor, board, client and connection generations before writes. CLI
  foreground commands wait on the same tasks as background commands, and later
  clients can retrieve verified download artifacts.

- Registry schema 5 includes task history, scoped transfer artifacts and proven
  connection-to-board associations. Interrupted work is recorded without replay;
  identity promotion preserves earlier task visibility without guessing ownership.

- Add configured power/reset/flash helpers with fixed argument lists, task
  cancellation and process cleanup. Exclusive helpers pause only their UART;
  capture resumes after cleanup, with unproven cleanup keeping that UART paused.

- Extract console login, command framing and SSH provisioning into a protocol
  service that uses the connection's existing reader and guarded writes.

- Ship API/configuration guides with validated links in both package layouts.
  Windows builds collect and verify dependency license files; native binary
  attribution and actual Windows/GitHub release execution remain release gates.

- Extract device directory, selection and identity transitions into a controller.
  Out-of-order device responses and failures cannot replace newer directory or
  connection status; ownership revision protection remains intact.

- Give terminal views an independent lifecycle controller for generation guards,
  disposal, streaming/poll fallback, ordered input and firmware/PTY rendering.

- Extract archive selection, list refresh and all-boot search into a controller;
  detached archive controls and superseded search buttons cannot change the
  current device or open stale results.

- Extract terminal-buffer search, echoed-command navigation and highlighting
  into an injected module, retaining view-generation checks for stale callbacks.

- Add opt-in daily archive retention with active-writer/running-boot protection,
  disk-space warnings and allowlisted diagnostic exports. Retention is disabled
  by default; metadata and database backups are never pruned by this policy.

- Distinguish human input reservations by page client ID while keeping the web
  audit actor. Separate clients must explicitly take over; transfer commands keep
  their originating client identity across background work.

- Add optional actor-scoped serial input reservations, CLI claim/release and
  browser human takeover. Spectators retain output access and independent SSH;
  agent revocation and connection replacement release reservations.

- Extract session/archive queries from HTTP handling and share result/error codes
  with control authentication. Keep boot partition caches intact when listing;
  reject archive links, nonregular files and replacement races across readers.

- Separate device write actions from HTTP handling into injected application
  services. Add protocol capabilities and CLI operation status/cancellation;
  action errors expose stable codes while preserving existing response fields.

- Version registry and agent-grant schemas; preserve legacy data with automatic
  pre-upgrade WAL-aware backups, atomic migrations and future-version rejection.

- Release transfer expiry timers immediately after completion, abort or expiry,
  so repeated transfers do not accumulate ten-minute timer threads.

- Coordinate console input, commands, provisioning, transfer and recovery with
  total deadlines and connection-generation guards. Expose owner-checked,
  ID-scoped cancellation without closing the shared serial reader.

- Share SSH lifecycle, streaming, log rotation and audit across Linux/Windows IO
  adapters. Relocate live SSH log writers during identity promotion, preserving
  partial UTF-8 and resuming the correct directory after migration failures.

- Separate persistent board identities and SSH ownership from replaceable UART
  connections. Preserve session registries across re-enumeration and release
  in-flight SSH reservations against their original board after a swap.

- Add an isolated hardware-free demo with simulated boards, cross-day boot logs,
  SSH, agent commands and virtual file transfers through the normal API.

- Add validated serial framing and per-board/port profiles, separate console and
  SSH credentials, and a manual console mode without Linux probes or SSH
  provisioning. Include configuration instructions and examples in packages.

- Add public-project licensing, contributor guidance and bilingual overview.
- Configure Linux/Windows PR checks and tagged draft release artifacts.
- Include application license notices in Ubuntu and Windows distributions.
- Use the source checkout as the default Linux development data root.
- Fit tall firmware menus on mobile and retain terminal space beside records.

## 1.4.50

DeckLab branding and independent desktop/Start-menu ICO resources.
Earlier development changes and investigations remain available in Git history.
