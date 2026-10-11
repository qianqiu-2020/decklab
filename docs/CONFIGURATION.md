# Host and board configuration

Configuration is read when the collector starts. Linux packages use
`/etc/seriald/seriald.env`; Windows launchers inherit environment variables.
`SERIALD_BASE` selects runtime storage (the checkout root for source runs).
`SERIALD_STATIC` selects application assets. Keep development and installed
collectors on separate ports/data directories; never open the same serial port
from two collectors.

## Environment defaults

### Host paths, listening and authentication

| Variable | Default / behavior |
| --- | --- |
| `SERIALD_BASE` | Source Linux checkout root; Windows entry point uses adjacent `data/`; installed Ubuntu uses `/data/seriald-data` |
| `SERIALD_STATIC` | Source assets or frozen executable assets; normally leave unchanged |
| `SERIALD_PORT` | 8898; explicit launcher arguments override it |
| `SERIALD_BIND` | Source/control default `127.0.0.1`; Ubuntu package and Windows LAN launchers select `0.0.0.0` |
| `SERIALD_SOCKET` | Linux source `<base>/seriald.sock`; Ubuntu `/run/seriald/collector.sock`; ignored by Windows loopback collector |
| `SERIALD_HANDOFF` | `<base>/collector-handoff.json`; short-lived same-host connection handoff |
| `SERIALD_BY_PATH` | Linux `/dev/serial/by-path`; Windows `Windows COM` display value, discovery uses pySerial |
| `SERIALD_RESCUE_BIN` | Linux source `/usr/local/sbin/seriald-rescue.sh`; Ubuntu `/usr/libexec/seriald/rescue.sh`; no Windows USB helper |
| `SERIALD_AUTH_TOKEN` | Empty: open access. Nonempty: primary Basic/Bearer credential; browser Basic username is `seriald` |
| `SERIALD_AGENT_TOKENS_FILE` | Linux control-only optional JSON actor/token map; requires primary authentication; not loaded by Windows entry point |
| `SERIALD_TRUST_LOOPBACK` | Windows-only, exact value `1` allows credential-free loopback access; LAN authenticated launcher enables it. Linux control does not read it |

Set host/auth variables before starting the entry point. Restart control to apply
Linux listener/auth changes; restart collector for collector configuration.
Windows uses one host process and requires restart. Authenticated-loopback mode
trusts the direct peer address, so do not route untrusted proxy traffic through
that bypass. See [installation](INSTALL.md) and [Windows launchers](WINDOWS.md).

### Serial and board defaults

| Variable | Default | Values / purpose |
| --- | --- | --- |
| `SERIALD_BAUD` | 115200 | Integer 50–4000000; host adapter must support it |
| `SERIALD_DATA_BITS` | 8 | 5, 6, 7, 8 |
| `SERIALD_PARITY` | N | N, E, O |
| `SERIALD_STOP_BITS` | 1 | 1, 2 |
| `SERIALD_FLOW_CONTROL` | none | none, rtscts, xonxoff |
| `SERIALD_NEWLINE` | auto | auto, cr, lf, crlf; generated shell command endings |
| `SERIALD_SSH_USER` / `SERIALD_SSH_PASSWORD` | root / bianbu | SSH credentials |
| `SERIALD_CONSOLE_USER` / `SERIALD_CONSOLE_PASSWORD` | SSH credentials | Separate serial login credentials |
| `SERIALD_PROBE` | linux | linux or manual |
| `SERIALD_AUTO_CONFIGURE_SSH` | 1 for linux; 0 for manual | 0, 1, false, true |
| `SERIALD_PROFILES` | unset | UTF-8 JSON profile file path; at most 64 KiB |

The existing development workflow retains root/bianbu defaults. After explicit
root authentication denial using the configured profile password, automatic
provisioning can change root's password, enable root/password SSH login and
restart sshd through serial.
Set `SERIALD_AUTO_CONFIGURE_SSH=0` to preserve board policy. Custom SSH credentials
for a non-root user are used directly. A configured root password is also used
for serial provisioning when enabled. A per-request password different from
the profile password never triggers provisioning. Empty passwords cannot be
provisioned; explicit root setup requires a root SSH profile.

`manual` provides raw terminal input/output and logging without shell login,
identity probes or automatic SSH configuration. Use it for firmware-only targets
and serial protocols that must not receive Linux commands. Such targets remain
unprovisioned until identified through a supported explicit workflow.

## Per-board profiles

Copy `examples/profiles.json` and replace its example serial
and physical port key. Omitted fields inherit environment defaults. Selection
uses board serial first, physical port key second, configured default last.
Labels and tty device names are not selectors. Linux keys are names under
`/dev/serial/by-path`; Windows uses USB location/interface keys and falls back
to COM names when topology is unavailable.

```sh
export SERIALD_PROFILES="$PWD/examples/profiles.json"
export SERIALD_CONSOLE_PASSWORD='your-board-password'
python3 seriald.py 8898 127.0.0.1
```

Windows PowerShell, before running an installed launcher:

```powershell
$env:SERIALD_PROFILES = 'D:\DeckLab\profiles.json'
$env:SERIALD_CONSOLE_PASSWORD = 'your-board-password'
.\Start-seriald.cmd
```

Profiles can contain credentials; keep password files out of Git. The example
inherits passwords from the environment. Unknown fields, duplicate JSON keys
and invalid settings fail startup before opening devices. API snapshots expose
profile names and framing, never passwords.

Only the collector applies framing when opening a port. Identity binding may
select a different board profile; `settings_pending` reports that its framing
awaits reconnect. Assign a port profile when framing is needed before identity
discovery. Editing profiles requires a collector restart; restarting control/UI
does not reload them. The profile book snapshots environment defaults and the
configuration document at construction; later profile selection does not reread
environment variables or the JSON file. Linux accepts termios-supported speeds
and rejects other rates before opening a port; Windows delegates supported rates
to pySerial.

Optional archive retention and free-space warning thresholds are documented in
[log operations](LOG_OPERATIONS.md). Retention defaults to disabled; diagnostics
can be exported with `serialctl diagnostics` without board credentials or logs.

## Host transfer staging

The collector's artifact storage foundation reserves disk space and recovers
temporary files alongside its registry. Limits and temporary-file lifetimes are
documented in [TRANSFER_ARTIFACTS.md](TRANSFER_ARTIFACTS.md). Collector-owned upload/download tasks and
HTTP staging use these limits; browser and background CLI file transfers are integrated. These settings do not change agent grant expiry or serial/SSH log retention.

## Hardware helpers

`SERIALD_HARDWARE` optionally selects a bounded JSON file of explicitly configured
per-serial power/reset/flashing argv (UTF-8 with optional BOM, at most 64 KiB).
Default is disabled; no automatic action or
sudo is used. Agent access is disabled per action unless explicitly configured.
See [hardware extensions](HARDWARE_EXTENSIONS.md) for configuration, offline
power commands, cancellation and exclusive UART handoff.

## Configuration review boundaries

The 34 runtime environment names are covered by these groups. Host settings are
read by the relevant entrypoint; board, maintenance, staging and helper settings
belong to the collector. Windows launchers can select listener/authentication
values before starting the host.

| Group | Names | Implementation / verification |
| --- | ---: | --- |
| Paths, listener, authentication | 11 | `config.py`, control/collector/Windows entrypoints and packaged launchers; config, sharing and package tests |
| Serial framing, credentials, profiles | 13 | `profiles.py`, `transport.py`, `console_protocol.py`; profile/protocol tests, including startup snapshots and credential overrides |
| Retention and disk warnings | 3 | `maintenance.py`; maintenance tests; see [log operations](LOG_OPERATIONS.md) |
| Transfer quotas and expiry | 6 | `transfer_artifacts.py`; artifact tests; see [transfer staging](TRANSFER_ARTIFACTS.md) |
| Hardware helper file | 1 | `hardware.py`; helper/handoff tests; see [hardware extensions](HARDWARE_EXTENSIONS.md) |

The configuration inventory checker verifies name coverage only. Platform
behavior requires its adapter tests and installation checks; Linux fixtures and
PowerShell parsing do not establish native Windows installation or COM behavior.
