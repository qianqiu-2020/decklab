# Changelog

## Unreleased

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
