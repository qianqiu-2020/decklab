# Changelog

## Unreleased

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
For earlier development changes, see FIXES.md and docs/archive/.
