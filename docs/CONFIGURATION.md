# Host and board configuration

Configuration is read when the collector starts. Linux packages use
`/etc/seriald/seriald.env`; Windows launchers inherit environment variables.
`SERIALD_BASE` selects runtime storage (the checkout root for source runs).
`SERIALD_STATIC` selects application assets. Keep development and installed
collectors on separate ports/data directories; never open the same serial port
from two collectors.

## Environment defaults

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
root/bianbu authentication denial, automatic provisioning can change root's
password, enable root/password SSH login and restart sshd through serial.
Set `SERIALD_AUTO_CONFIGURE_SSH=0` to preserve board policy. Custom SSH credentials
are used directly and do not trigger this provisioning.

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
does not reload them. Linux accepts termios-supported speeds and rejects other
rates before opening a port; Windows delegates supported rates to pySerial.

Optional archive retention and free-space warning thresholds are documented in
[log operations](LOG_OPERATIONS.md). Retention defaults to disabled; diagnostics
can be exported with `serialctl diagnostics` without board credentials or logs.

## Host transfer staging

The collector's artifact storage foundation reserves disk space and recovers
temporary files alongside its registry. Limits and temporary-file lifetimes are
documented in [TRANSFER_ARTIFACTS.md](TRANSFER_ARTIFACTS.md). Collector-owned upload/download tasks and
HTTP staging use these limits; browser and background CLI file transfers are integrated. These settings do not change agent grant expiry or serial/SSH log retention.
