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

SSH connection attempts use the configured password, then the deduplicated
`bianbu`/`debian` fallbacks, and advance only after authentication rejection.
Explicit request credentials are attempted once. Exhausted authentication
returns `ssh-authentication-failed`; the web UI can request credentials and
display manual root SSH instructions without sending them to the serial port.
Normal SSH opens must not reset root passwords or edit sshd policy. Keep request
passwords out of durable task records and preserve stale-connection checks.
The separate authorized `ssh-enable` operation only installs/starts SSH from an
already-root shell. See [configuration](docs/CONFIGURATION.md#manual-root-ssh-setup).
