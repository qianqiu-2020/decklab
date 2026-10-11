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
| Demo | Useful simulated boards, boot history, SSH and transfer scenarios without hardware | HTTP/CLI/browser checks; no serial discovery or real commands | Pending |
| Model boundaries | Persistent Board, ephemeral Connection, independent Session | Identity/reconnect/archive migration tests | Pending |
| Transactions | Shared coordination for input, probes and transfers; timeout/cancel/generation protection | Concurrent actor, cancellation and replacement tests | Pending |
| Platform adapters | Common session lifecycle, auditing and logs; Linux/Windows IO isolated | Equivalent contract tests on fake transports | Pending |
| Application services | HTTP/CLI share operations; reduce global manager coupling; stable API errors/capabilities | Contract tests through HTTP and direct services | Pending |
| Frontend boundaries | Split device, terminal, archive and navigation state; preserve stale-response protection | Existing isolation suites and real-browser smoke | Pending |
| Storage upgrades | Explicit schema version and forward migrations | Upgrade fixtures, failure/rollback and backup checks | Pending |
| Collaboration | Optional input ownership with spectators, release and human takeover | Concurrent clients and no interleaved transaction input | Pending |
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
