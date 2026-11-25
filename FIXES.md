# Maintenance references

BootROM support and layout readiness are separate. The engine may return
`supported: true` together with `can_prepare_from_image: true`; the web UI must
offer the storage-free loader preparation preview before partition selection,
and defer readback capability conclusions until the live layout is available.

NOR firmware recovery uses the fixed `partition_nor.json` six-partition layout
for explicit full-package recovery. Keep its MTD adapter separate from GPT discovery.
Loader completion must stop for a new human scope review before storage writes;
`flash:mtd` changes the namespace and can erase env. Include the conservative
whole-MTD erase risk, unknown NOR/NAND type and excluded optional EC operation in
the preview. Never mark missing readback as verification success or replay writes.

NOR packages also support partition mode through the separate RAM adapter.
Import `partition_nor.json` as a payload catalogue only; it grants no layout-write
capability. After preparing RAM, bind only selected payloads to independently
checked NOR offsets/capacities in the same task. The running RAM environment may
also import a package without restaging. Missing safe loaders must ask for a
backend upgrade, never tell a single-partition caller to choose full recovery.

U-Boot remains unmodified. The RAM builder uses native Kconfig to disable
board late-init and persistent environment storage, and boot directly into
Fastboot. Host-side fixed commands reuse UCmd, MTD, I2C, OEM read and upload;
there are no DeckLab firmware opcodes or getvars. EEPROM write confirmation
requires fresh physical readback, not the native command's OKAY alone.
NOR partition writes retain whole-chip backup, explicit erase/write scope,
and whole-chip readback checking bytes outside selected partitions.
See [storage workspace design](docs/modules/flashing.md) for the native protocol,
source/build constraints and hardware validation limits.

Single-host deployments support Linux and Windows. Split deployments use a
Windows/Linux Web host and one authenticated HTTPS Linux hardware owner.
All serial readers, durable hardware tasks, image downloads
and cache stay on that backend. A Web restart must not restart the backend or
replay task submissions. Each node keeps private storage; do not share SQLite.

Board control providers share HardwareOperation authorization, task persistence
and console leases. Fixed commands remain compatible; BMC control pins the
configured board/module/USB mapping before one HTTP mutation. Lost receipts
remain unknown and are never retried. The control provider does not own UART,
loaders or flash writes. See the hardware extension guide for configuration.

Current behavior and module ownership: [architecture](docs/modules/architecture.md).
Release changes: [CHANGELOG.md](CHANGELOG.md).
Contributor checks and conventions: [CONTRIBUTING.md](CONTRIBUTING.md).

The detailed historical fix diary and completed plans remain in Git history.
This entry point is retained for existing contributor instructions.

Debian 1.7.1+ packages include a private frozen MCP/Python runtime, built on the
native Ubuntu 22.04 baseline for amd64 and arm64. Installation must not download
Python packages or require `DECKLAB_MCP_PYTHON`. The package builder requires a
validated runtime; it must not silently produce a thin package. An explicit
Python override remains available for source/advanced deployments. See
[installation](docs/operations/install-linux.md), [MCP](docs/modules/mcp.md) and
[release acceptance](docs/operations/release.md).

The MCP worker remains a control-owned private loopback process and uses the
existing authenticated API; it never becomes a second serial reader. Startup
diagnostics are bounded, credential-redacted and private under
`DECKLAB_BASE/mcp-worker.log`, with one `.1` backup. A failed worker leaves the
terminal and log APIs available.

SSH connections first use credentials saved on the server for the identified
board serial, with one attempt. Without saved credentials, use the configured
password, then deduplicated `bianbu`/`debian` fallbacks, advancing only after
authentication rejection. Explicit request credentials are attempted once.
Successful web connections save verified credentials; failed changes preserve
the previous pair. Agents may reuse but do not update that private record.
Keep passwords out of public snapshots, responses, durable task records and
browser storage. Registry schema 7 stores them separately from board metadata;
keep the registry/WAL/SHM owner-only on Unix and preserve private backups.
Saving checks connection identity and credential revision; never persist by IP
or COM/tty for unidentified targets. Exhausted authentication returns
`ssh-authentication-failed`; the web UI can request credentials and display
manual root SSH instructions without sending them to the serial port.
Normal SSH opens must not reset root passwords or edit sshd policy.
The separate authorized `ssh-enable` operation only installs/starts SSH from an
already-root shell. See [configuration](docs/reference/environment.md#manual-root-ssh-setup).

Serial terminal setup recognizes complete BusyBox ash path prompts (`~ #`,
`/ #`, `/etc #`, `~/work $`) as well as user/host prompts. Both frontend and
collector require the conservative path form, including a space/tab before the
prompt character; typed input, login/password prompts and U-Boot are excluded.
Keep the existing two-stage stty/environment setup, baud/parity settings, fresh
offset checks and input ownership. `manual`/`custom` probe modes send no Linux
size setup; their HTTP 409 uses `terminal-mode-disabled`, distinct from
`not-idle`. A local BusyBox 1.30.1 ash/stty PTY check exercises 140 columns by
42 rows; it is not physical-board acceptance.

Image preparation is host-only and content-addressed under flashing/images/<SHA-256>.
A prepared receipt seals extraction and payload digests independently of USB/board
identity. Reconnection retains the chosen image, but invalidates board facts and
write plans. Never reuse a previous board's authorization or skip execute-time
payload validation. Preparation uses the existing task/resource registry; unknown
workers retain image/source data until explicit human recovery. GC must protect
fresh plans, active tasks and recovery holds, including alternate payload roots.
