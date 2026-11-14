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

Single-file partition flashing uses preinstalled RAM-only FSBL/U-Boot rather
than requiring another firmware package. The separate `decklab-nor-v1` RAM
adapter selects a declared standard NOR map without `flash:mtd` or env save,
then checks target-reported offsets/sizes before binding a selected payload.
It never adds layout or other-partition writes to that plan. Show the source of
the standard map and selected-partition erase risk; this adapter does not make
the old stock full-recovery namespace operation safe for single partitions.
The K3 loader patch/build recipe and simulated execution tests are source and
build evidence, not physical-board acceptance.
The [BMC single-board field record](docs/audits/2026-11-14-bmc-sub23-1.md)
covers real BS01DCMA NOR partition writing, independent whole-NOR readback and
serial handoff. Its boot recovery and verification limits remain explicit;
this does not establish acceptance for other boards or built-in readback.
The dedicated RAM build must enter fastboot before normal board provisioning,
DDR-training persistence or SD/USB auto-flash. Its build recipe checks the
configuration and late-init disassembly. When installed, the same RAM environment
also prepares full firmware packages without changing their write catalogue.

The NOR workspace changes add bounded whole-NOR snapshots and
readback verification to the safe RAM partition path. Backups use the existing
artifact ownership/quota service, with opaque env contents. Comparison checks
exact padded payload bytes and every byte outside selected partition capacities;
it does not invent erased-tail contents. A separate current-code field run on
sub23-1 verified the complete Web backup, selected opensbi write, and automatic
whole-NOR readback; before/after bytes matched. This remains a single-board,
original-content rewrite, not different-version or multi-partition acceptance.
Single-file previews accept a null executor package catalogue as empty, while
required readback checks must use the proven RAM NOR reader capability.
Current workflow and validation limits: [NOR workspace](docs/FLASH_WORKSPACE.md).

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

Current behavior and module ownership: [architecture](docs/ARCHITECTURE.md).
Release changes: [CHANGELOG.md](CHANGELOG.md).
Contributor checks and conventions: [CONTRIBUTING.md](CONTRIBUTING.md).

The detailed historical fix diary and completed plans remain in Git history.
This entry point is retained for existing contributor instructions.

Debian 1.7.1+ packages include a private frozen MCP/Python runtime, built on the
native Ubuntu 22.04 baseline for amd64 and arm64. Installation must not download
Python packages or require `SERIALD_MCP_PYTHON`. The package builder requires a
validated runtime; it must not silently produce a thin package. An explicit
Python override remains available for source/advanced deployments. See
[installation](docs/INSTALL.md), [MCP](docs/MCP.md) and
[release acceptance](docs/RELEASING.md).

The MCP worker remains a control-owned private loopback process and uses the
existing authenticated API; it never becomes a second serial reader. Startup
diagnostics are bounded, credential-redacted and private under
`SERIALD_BASE/mcp-worker.log`, with one `.1` backup. A failed worker leaves the
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
already-root shell. See [configuration](docs/CONFIGURATION.md#manual-root-ssh-setup).

Serial terminal setup recognizes complete BusyBox ash path prompts (`~ #`,
`/ #`, `/etc #`, `~/work $`) as well as user/host prompts. Both frontend and
collector require the conservative path form, including a space/tab before the
prompt character; typed input, login/password prompts and U-Boot are excluded.
Keep the existing two-stage stty/environment setup, baud/parity settings, fresh
offset checks and input ownership. `manual`/`custom` probe modes send no Linux
size setup; their HTTP 409 uses `terminal-mode-disabled`, distinct from
`not-idle`. A local BusyBox 1.30.1 ash/stty PTY check exercises 140 columns by
42 rows; it is not physical-board acceptance.
