# Host and board configuration

## Web settings, export and import

Open **系统配置 / System settings** using the primary Web credential. The form
covers all host settings below, with JSON editors for `profiles`, `hardware`,
`flash` and the fixed Agent-token map. Embedded documents take precedence over
their file paths. Saving/importing configuration never executes hardware
commands, identifies a board, or changes a board's password.

1. Edit fields or import a version-1 `decklab-settings` JSON package. Import
   validates and stages a patch; omitted keys retain existing values. Explicit
   `{}` clears a document's assignments. Review paths, board assignments and
   helper commands before saving.
2. Choose **预览修改**, then **保存配置**. Revision checks reject concurrent edits.
   Invalid imports and previews do not modify the saved file.
3. Choose **重启并应用**. The page distinguishes saved and active revisions.
   Control-only changes preserve the collector. Collector changes close SSH
   sessions and briefly interrupt terminals; the existing process re-executes
   only after closing its serial readers. Active tasks, flash/recovery holds,
   concurrent writes, console operations and input reservations block activation.
   Release them and retry. Changed addresses/passwords require reopening with
   the new address/credential. Activation failures remain visible on GET.

Settings are stored privately in `<launch SERIALD_BASE>/web-settings.json`
(mode 0600 on Unix). They override the launch environment and listener arguments
after restart. The original configuration anchor remains fixed even if Web
settings change `BASE`. Changing data directories does **not** migrate archives,
registry credentials or grants; retain the original launch directory for future
launches. Corrupt settings fail startup rather than silently dropping access
controls. Offline recovery: stop the service, preserve and repair the file or
move it aside, then restart with the original launch environment. Embedded JSON
is materialized privately under `<launch SERIALD_BASE>/web-settings/<revision>/`
for existing loaders. Source configuration files and `/etc/seriald/seriald.env`
are not overwritten.

**导出分享配置** hides password fields and actor tokens as `{"$redacted":true}`.
Import preserves the target's existing value at that field; a hidden new field
without an existing value must be supplied. Review custom command strings and
positional helper arguments for credentials before sharing. **导出含密钥备份**
explicitly includes private values. Exports do not include board identity/history,
verified per-device SSH credentials or issued access grants; those remain in
existing registry/grant stores. This export is not a full service-data backup.

Device labels and port probe overrides have their own JSON section in the same
Web dialog. They remain in the existing registry. Choose **预览设备偏好**, then
**保存设备偏好** to apply them immediately, separately from restart-based host
settings. Export can optionally include them in the same package. Import stages
each section without saving it; an invalid device section stays editable and
does not prevent reviewing the host section. The complete package is at most
16 MiB; host and device API requests retain their separate 2 MiB / 1 MiB limits.

Device preference patches address registered boards by immutable serial and
ports by attachment path. Unknown boards must be identified first; import never
creates identities or links archives. Port `label` / `probe` set to `null` clears
that override; omission preserves it. Concurrent label changes, reconnections
or identity conflicts invalidate the preview. Busy or reserved devices and
flash recovery holds prevent applying affected settings. Changes and audit
entries commit in one SQLite transaction; label swaps do not move logs. Custom
probe commands are saved only and are never executed by import. Existing
verified SSH records retain their authentication-and-revision checks and are
managed through the device's SSH form; templates do not restore passwords as
already verified credentials.

The browser-preferences checkbox includes only language and personal device
order. They remain in the current browser's local storage; other browsers keep
their own preferences. No passwords/tokens are stored there. Device pages also retain their individual label, probe and verified SSH forms.


Configuration is read when the relevant control or collector process starts. Linux packages use
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
| `SERIALD_PORT` | 8898; launch arguments apply until overridden by saved Web settings |
| `SERIALD_BIND` | Source/control default `127.0.0.1`; Ubuntu package and Windows LAN launchers select `0.0.0.0` |
| `SERIALD_SOCKET` | Linux source `<base>/seriald.sock`; Ubuntu `/run/seriald/collector.sock`; ignored by Windows loopback collector |
| `SERIALD_HANDOFF` | `<base>/collector-handoff.json`; short-lived same-host connection handoff |
| `SERIALD_BY_PATH` | Linux `/dev/serial/by-path`; Windows `Windows COM` display value, discovery uses pySerial |
| `SERIALD_RESCUE_BIN` | Linux source `/usr/local/sbin/seriald-rescue.sh`; Ubuntu `/usr/libexec/seriald/rescue.sh`; no Windows USB helper |
| `SERIALD_AUTH_TOKEN` | Empty: open access. Nonempty: primary Basic/Bearer credential; browser Basic username is `seriald` |
| `SERIALD_AGENT_TOKENS_FILE` | Optional JSON actor/token map; requires primary authentication; Linux and Windows |
| `SERIALD_TRUST_LOOPBACK` | Windows-only, exact value `1` allows credential-free loopback access; LAN authenticated launcher enables it. Linux control does not read it |
| `SERIALD_MCP_ENABLED` | Enabled by default; Debian 1.7.1+ and Windows packages include the server runtime. `0` disables the integrated `/mcp` endpoint |
| `SERIALD_MCP_PYTHON` | Advanced/source setting: absolute Python executable containing the MCP SDK. An explicit value overrides the Debian bundled worker; normally leave unset. Windows frozen packages use their own MCP entry point. No client installation is required |
| `SERIALD_MCP_ALLOWED_HOSTS` | Optional comma-separated public `hostname:port` authorities for MCP behind a proxy; local hostnames, loopback and local LAN addresses are discovered automatically. A port wildcard such as `decklab.example:*` is allowed; a wildcard hostname is not |
| `SERIALD_UI_LANGUAGE` | auto, en or zh-CN; auto follows the installer; browser choice takes precedence |
| `SERIALD_SHARE_SECONDS` | 86400; new webpage share-link lifetime, 60–2592000 seconds |
| `SERIALD_SCROLLBACK` | 262144; real-time per-stream cache bytes, 4096–16777216; byte cursors stay absolute |
| `SERIALD_SSH_LIMIT` | 8; per-device SSH session limit, 1–128 |
| `SERIALD_SSH_IDLE` | 1800; idle SSH expiry, 60–86400 seconds |

Set host/auth variables before starting the entry point. Restart control to apply
Linux listener/auth/MCP changes; restart collector for collector configuration.
Windows uses one host process and requires restart. Authenticated-loopback mode
trusts the direct peer address, so do not route untrusted proxy traffic through
that bypass. See [installation](INSTALL.md) and [Windows launchers](WINDOWS.md).

The Debian MCP worker is installed under `/usr/lib/seriald/mcp-runtime/` and
does not depend on the system Python SDK. Without an explicit Python override,
control selects this worker when present, then falls back to the source entry
point using its own Python interpreter. A present but invalid bundled executable
is an installation error. `SERIALD_STATIC` changes frontend assets only.
Worker diagnostics are written to `<SERIALD_BASE>/mcp-worker.log` with mode 0600;
one 64 KiB file and one 64 KiB `.1` backup are retained. See the
[MCP guide](MCP.md) for startup errors, source setup and override migration.

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
| `SERIALD_PROBE` | linux | linux, custom or manual |
| `SERIALD_PROBE_COMMAND` | empty | POSIX script when `SERIALD_PROBE=custom`; at most 16 KiB |
| `SERIALD_AUTO_CONFIGURE_SSH` | 1 for linux; 0 otherwise | Legacy serial root-provisioning permission; 0, 1, false, true. Normal SSH opens never change board credentials or policy |
| `SERIALD_PROFILES` | unset | UTF-8 JSON profile file path; at most 64 KiB |

The development defaults remain root/bianbu. A normal SSH connection first uses
the account and password saved on the server for that board's serial. Saved
credentials are attempted once; authentication rejection opens credential entry
instead of replacing them with a default password. Without saved credentials,
the service tries the profile password, then `bianbu`, then `debian`, without
repeating duplicate values. Only an explicit authentication rejection advances
that list; network failures and timeouts stop the attempt. Requests with an
explicit `ssh_user` or `ssh_password` use only those overridden credentials.

### Remembered SSH credentials

After a web connection succeeds, the server saves the verified account and
password for an identified device. This also remembers the working default
password on the first successful connection. Later connections reuse it after
service restart, replugging or a USB-port change, once the same device identity
has been restored. Labels, IP addresses and COM/tty numbers are not credential
keys. An unidentified connection can use SSH, but its credentials are not saved
for future connections; identify the board first for persistent reuse.

Use **SSH settings** to change the account or password. An existing account such
as `ubuntu` or `debian` can be used directly. The new pair replaces the saved
pair only after successful connection; failed authentication leaves the old
settings intact. The form shows the saved username and a blank password field;
the API never returns the stored password. Connection-generation and revision
checks prevent a late attempt from overwriting another device's or a newer
configuration. Cancellation and connection state are checked before saving;
cancellation after the database commit does not roll back saved settings. If
credential saving fails, the established SSH session remains available and the
response reports that the credentials were not saved. Reusing the saved pair
does not rewrite it.

Credentials are stored in the private `ssh_credentials` table of
`<SERIALD_BASE>/registry.sqlite3`, separately from public device metadata and task
history. They are stored as plaintext so the service can authenticate again.
On Unix, the registry and its WAL/SHM files use owner-only permissions (0600);
Windows access follows the host directory's ACLs. Treat the registry and any
backups containing saved credentials as sensitive files. See
[database upgrades and recovery](STORAGE_UPGRADES.md). Passwords are not kept in
browser storage or task records, and SSH settings do not change profile files or
serial-console login credentials. Agents can use server-saved credentials for
their permitted devices, but Agent connection attempts do not update them;
explicit Agent credential overrides apply only to that connection.

When authentication still fails, `ssh-authentication-failed` lets the web user
enter another SSH username and password. Normal SSH opens never reset root's
password or change sshd policy, regardless of `SERIALD_AUTO_CONFIGURE_SSH`.
The separately requested `ssh-enable` operation may install/start the SSH service
from an already-root serial shell; it does not set root's password or enable
root password login. It requires authorization for that board change.

### Manual root SSH setup

If root password SSH is required, run these steps yourself in the board's serial
terminal using an account with sudo privileges. Finish each interactive prompt
before pasting the next command; the UI only provides guidance and never sends
these commands automatically. With an existing root shell, run `passwd root`,
edit the file directly with an available editor such as `vi` or `nano`, and omit
`sudo` from the validation and restart commands.

Set the root password; sudo first asks for the current user's password, then
passwd asks twice for the new root password:

```sh
sudo passwd root
```

Edit the SSH server configuration:

```sh
sudoedit /etc/ssh/sshd_config
```

Set these directives at the beginning, before any `Include` line:

```text
PermitRootLogin yes
PasswordAuthentication yes
```

Check syntax before restarting, then inspect the effective global settings:

```sh
sudo /usr/sbin/sshd -t && sudo systemctl restart ssh
sudo /usr/sbin/sshd -T | grep -E '^(permitrootlogin|passwordauthentication) '
```

Both settings should report `yes`. Address-specific `Match` rules may also affect
the real client connection. Reconnect in the web UI with `root` and the password
you chose. See Ubuntu's [user management](https://ubuntu.com/server/docs/how-to/security/user-management/)
and [OpenSSH configuration](https://ubuntu.com/server/docs/how-to/security/openssh-server/)
for sudo prompts, directive precedence and service configuration.

### Console probe modes

`manual` provides raw terminal input/output and logging without shell login,
identity probes or the legacy root SSH provisioning workflow. Use it for firmware-only targets
and serial protocols that must not receive Linux commands. Such targets remain
unprovisioned until identified through a supported explicit workflow, but can
already be shared with an AI using a grant for that live connection.

The web **Probe settings** dialog is available before identification. Choose the
built-in Linux probe, a custom POSIX shell script, or manual mode. Saving does not
send anything to the board; use Identify to run a probe. A custom script may print
metadata such as `HOST=buildroot`, `MODEL=custom` and `IP=eth0 192.0.2.10/24` without
a serial. This returns metadata while leaving the connection unbound. Supplying
`SERIAL=` still requires a valid unique identity and the normal conflict checks.
Keep credentials out of probe scripts. Non-POSIX firmware uses the raw terminal.

Web preferences are stored in the registry by physical attachment and override
the profile's probe selection. Review them after replacing the target at that
port. They do not change baud, login credentials or hardware identity. Custom and
manual modes disable the legacy root SSH provisioning permission. Profile files can also specify
`probe: "custom"` with `probe_command` (at most 16 KiB); explicitly set
`auto_configure_ssh: false` for that mode.

Built-in probes use short POSIX shell input lines to accommodate BusyBox ash
interactive input limits. SSH status detection supports existing sshd/dropbear
processes when systemd is absent; this does not install or start a service on
Buildroot automatically.

## Per-board profiles

Early boot and kernel text fit the viewport. Firmware menu positioning retains
an 80-column layout and detected row count; a Linux boot banner restores text
mode. At an empty Linux shell prompt, the UI waits 400 ms before requesting
automatic size synchronization. The collector verifies the current byte offset,
input state, Linux profile and transaction/ownership state before writing setup.
No historical prompt or attachment alone triggers board input.

Idle-prompt recognition includes BusyBox ash path prompts such as `~ #`, `/ #`,
`/etc #` and `~/work $`, with a space or tab separating the path and prompt
character. Frontend and collector use the same conservative forms; login or
password prompts, U-Boot consoles and a prompt followed by typed input do not
trigger setup. The earlier user/host-only match could leave these BusyBox
prompts waiting indefinitely without sending the size command.

Terminal size setup is enabled only for the `linux` probe mode. `manual` and
`custom` modes continue to provide their existing terminal access without
sending Linux size-configuration commands. A setup request in either mode
returns HTTP 409 with `code=terminal-mode-disabled` and the message
`当前探测模式未启用 Linux 终端尺寸同步`; `not-idle` instead means an enabled Linux
terminal is waiting for a fresh idle prompt or matching output offset.

One viewer coordinates automatic sizing per connection; other viewers follow
the shared geometry. The actual input writer, explicit **Sync size**, or input ownership changes the
coordinator. Resize requests wait for the next idle prompt during a running
program. Geometry survives buffer eviction and resets on detected reboot. Replay starts
with the geometry at its first byte and applies subsequent changes in order.
Smaller viewing windows scale the same logical grid, including its bottom row;
they do not rewrite cursor coordinates or force their own row count.

Shell setup enables `echoctl`, UTF-8 editing in UTF-8 locales, and xterm-256color
only when board terminfo is available. It disables XON/XOFF only for profiles
without software flow control. Baud, parity and application raw modes stay intact.
APT keeps its source coordinates. On the earliest kernel timestamp, the viewer
clears the preceding firmware screen, attributes and cursor/scroll modes. Setup
sends the short resize command before environment configuration; automatic later
resizes omit the longer environment command. Captured bytes remain unchanged;
SSH negotiates dimensions through its own PTY.

BusyBox compatibility is checked with local BusyBox 1.30.1 `ash` and `stty` on an
isolated pseudo-terminal, including a 140-column, 42-row size change. This is
protocol regression coverage, not acceptance of a physical Buildroot board.

Built-in defaults follow the SpacemiT K3/Bianbu development workflow: 115200,
8N1, no flow control, console/SSH `root` with password `bianbu`, Linux probes
and the legacy root-provisioning permission enabled. Normal SSH opens still do
not modify credentials or sshd policy. Profiles are generic settings, not
automatic board-model detection or proof of support on other boards. The
example below explicitly disables the legacy root-provisioning permission for its Linux
profiles. See the [board support list](../README.md#board-support-and-defaults)
for the physical-board validation scope.

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

Packages with the local flash executor automatically discover USB download
devices from the browser's flashing panel. Ordinary discovery does not require
`SERIALD_FLASH` or a previous serial-console identification. A target without a
reliable board serial remains a separate USB target; its location is not a board
identity. Selecting a target reads supported live layout/capability information
before previewing writes. Reliable GPT and target partition sizes can enable
partition-only writes; full-disk writes additionally require independently
verified capacity, sector format and whole-disk capability. Missing information
is reported rather than guessed. Discovery never starts a write or resets a board.

Detected SpacemiT K1/K3 BootROM devices can prepare their temporary environment
from the standard package's fixed FSBL/U-Boot members, with no handwritten JSON.
Uploading or previewing a package does not load it; an explicit background task
performs the fixed steps, then reads the live layout for partition selection in
that same task. Missing loaders, incompatible chips or unchecked layouts stop
the flow. Special boards and nonstandard loader sequences still use advanced
administrator bindings below.

`SERIALD_FLASH_EXECUTOR` optionally overrides automatic discovery's executable
with an administrator-owned absolute path. Without an override, installed Linux
packages use `/usr/libexec/seriald/decklab-flash`; source deployments can
place `decklab-flash` (or `decklab-flash.exe`) in the application source directory.
Other layouts can select the path explicitly. The browser
cannot supply this path. A missing executor is reported as unavailable.

`SERIALD_FLASH` selects the optional administrator-owned JSON configuration for
advanced fixed board-level flashing. Each registered board
has a `binding_revision`, board profile, separate manual control/serial endpoint
descriptions, and a local executor with an absolute executable and fixed USB bus
and port path. The browser submits managed artifact IDs and plan IDs, never host
paths or executable arguments. Source deployments and packages without the
executor need it installed separately; an included executor does not imply that
board-specific temporary loaders are bundled.
Enter download mode manually for this flow. See the
[hardware extension guide](HARDWARE_EXTENSIONS.md) for the adapter contract and
capacity limits. Reload the collector to replace its cached binding configuration;
new plans must match the binding revision and contents.

`SERIALD_HARDWARE` optionally selects a bounded JSON file of explicitly configured
per-serial power/reset/flashing argv (UTF-8 with optional BOM, at most 64 KiB).
Default is disabled; no automatic action or
sudo is used. Agent access is disabled per action unless explicitly configured.
See [hardware extensions](HARDWARE_EXTENSIONS.md) for configuration, offline
power commands, cancellation and exclusive UART handoff.

## Configuration review boundaries

Runtime environment names are covered by these groups. Host settings are
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
| Flash discovery and fixed binding | 2 | `flash_discovery.py`, `flash_runner.py`; executor, task, maintenance and HTTP integration tests |

The configuration inventory checker verifies name coverage only. Platform
behavior requires its adapter tests and installation checks; Linux fixtures and
PowerShell parsing do not establish native Windows installation or COM behavior.

## Distributed node settings

See [Web/backend deployment](DISTRIBUTED.md). `SERIALD_BACKEND_URL` selects the
backend HTTPS origin on a Web node; `SERIALD_BACKEND_TOKEN` is a separate node
credential; `SERIALD_BACKEND_CA` supplies the trusted PEM CA. Backend-only fields
are `SERIALD_BACKEND_BIND`, `SERIALD_BACKEND_PORT`, `SERIALD_BACKEND_CERT` and
`SERIALD_BACKEND_KEY`. Select the matching service/Windows launcher explicitly.
`SERIALD_FLASH_LOADERS` optionally names a backend-local manifest for prebuilt
RAM-only FSBL/U-Boot files; the package default is `flash-loaders/manifest.json`.
The backend verifies the chip family and SHA-256 before staging either file.
