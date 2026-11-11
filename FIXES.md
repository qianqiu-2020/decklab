# Maintenance references

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
