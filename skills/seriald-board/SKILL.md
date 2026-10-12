---
name: seriald-board
description: "Access development boards through an existing seriald service: discover boards by user label, read boot or failure logs, run commands and inspect agent activity. Use for seriald-managed boards and shared access from other agent sessions, including serial interaction when SSH is unavailable."
---

# seriald board access

Use the running service. Installed hosts provide `serialctl`; remote agents can
fetch `<SERIALD_URL>/serialctl.py` and run
`python3 "$HOME/.local/share/seriald/serialctl.py"` instead. The client needs only
Python 3's standard library. On Windows use `py -3 serialctl.py`.

The default URL is `http://127.0.0.1:8898`; use `SERIALD_URL` or `--url` for the
host specified by the user. Set `SERIALD_TOKEN` or `--token` when authentication
is enabled. UI agent grants are revocable and have no expiry by default. A
single-board grant is enforced by serial number; all-board access still requires
choosing the board relevant to the task. An unreachable service does not prove
the board is down.

## Select the board and read existing output

```sh
serialctl devices
serialctl --device riscv-118 read
```

`key` is the current user label; `serial` is the persistent identity, which may
be confirmed or provisional. Check `info.identity_status`: confirmed archives
use `logs/<serial>/`; provisional ones use
`logs/_provisional/<serial_source>/<serial>/`. `--device` accepts a label or serial
number. Labels, USB attachment paths and tty nodes can change. Select the board
specified by the user; ask when its identity is unclear, rather than choosing the
first online board.

Unidentified connections use `unprovisioned-…` keys derived from the USB path and
keep each connection's logs isolated. Run `identify` only when active access is
needed: it may log in and execute identity probes. Use the returned canonical
key afterward. Existing serial numbers retain their labels; `rename --label
musepi2` changes only the label. Historical boards remain visible. A temporary
USB re-enumeration on the same path preserves the binding; use **Change board**
before attaching another board to that adapter. A new attachment or an abnormal
service restart may require identification.

Identity probes prefer EEPROM ONIE TLV `0x23`: read an exposed NVMEM cell when
available, otherwise validate and parse raw at24 NVMEM. Valid DT `serial-number`,
including firmware/U-Boot injection, also confirms identity automatically.
Placeholders and known conflicting numbers do not. `reported_serial` is the
firmware's original number; `serial_source` records the selected source.
SoC/eFuse IDs and `/boot` markers stay provisional until verified.

The probe may read an existing `/boot/seriald-id`. Only when the user requests
this fallback should `identify --init-boot-marker` create it; existing files are
not overwritten and ordinary probes never create it. Preserve the marker to
retain continuity, and remove it before cloning an image onto another board.
After a timeout, inspect output before retrying or changing identities. With no
usable identity, keep USB-path isolation and ask the vendor to provision EEPROM;
do not bypass conflicts by renaming, write EEPROM/eFuse or change kernels on
this skill's authority.

`read` sends no Enter key. Initial output can include history; an old prompt does
not establish the current shell state. Save its byte offset `off` and use
`read --off <off> --tail 0` for subsequent output. `gap` or `truncated_bytes`
indicates missing/omitted output and cannot establish an absence of errors.

## Execute through SSH and inspect records

```sh
serialctl --actor claude-code/diagnosis --device riscv-118 exec \
  --command 'uname -a; uptime; df -h' --timeout 60
```

Prefer `exec` over SSH for Linux commands. Use serial for boot, kernel or SSH
failures and interactive firmware. `--actor` identifies the session for human
observation; agent grants fix their actor server-side. Ordinary actor names are
self-reported. The same board/actor reuses a persistent SSH session, visible in
the UI until `ssh-close --sid <sid>` or a human closes it. Use your own session.

Each command runs in a separate POSIX `sh -c` child with stdin at EOF and merged
stdout/stderr. Its `cd` or `export` does not alter the next command; combine those
with the dependent operation. Use interactive `send --sid` to change the parent
shell environment. `exec` is not for interactive programs, binary streams or
long-running tasks. Commands are limited to 2048 UTF-8 bytes and collected output
to 1 MiB; the service buffer is also bounded. Filter or tail large logs first.

Creating SSH without a known IP may probe serial. Do not install SSH or modify
credentials merely because a connection failed. When the user requests a
non-root account, set `SERIALD_SSH_PASSWORD_OVERRIDE` and pass, for example,
`--ssh-user ubuntu --ssh-password "$SERIALD_SSH_PASSWORD_OVERRIDE"`. The UI uses
host-configured credentials.

Check `completed`, `exit_code`, `output` and `session_closed`. A complete end
marker establishes completion; a nonzero exit code means failure.
`may_still_be_running` means the outcome is unknown: inspect before retrying
writes. Closing SSH does not guarantee background processes have exited. Report
`recording_error` or `cleanup_error`, retain the session ID and close only your
own sessions. `--timeout` excludes connection creation, which uses
`--http-timeout` (default 180 seconds).

`activity` reads persistent operation history; `logs` lists archives;
`log --file <filename>` reads one. Humans view agent sessions read-only by default
and can explicitly take over; closed sessions retain their transcripts.

## Read the exact boot and compare failures

```sh
serialctl --device <label> boot-log --latest
serialctl --device <label> logs
serialctl --device <label> boot-log --id <boot-ID>
serialctl --device <label> boot-search --query 'panic'
```

`boot-log` returns the complete captured `data` / `data_b64` for that boot,
including cross-day portions; it is not limited to live-buffer history or a
64 KiB tail. `--latest` means the most recently detected boot marker, not proof
that the board is online. Check the time and save the ID, then use `--id` to avoid
following another reboot. Output before capture began cannot be recovered;
missing boot markers produce an explicit error.

`boot-search` searches all boots for that board, case-insensitively, and returns
IDs, times and matching lines. Follow `more=true` with `--offset <next_offset>`.
Find the ID first, then read that boot instead of guessing daily-file boundaries.

Use `boot-compare --baseline-id <ID> --target-id <ID>` for the same board. For
cross-board comparison, `--device` selects the target and `--baseline-device
<label>` selects the baseline. Inspect `clues` for new errors/timeouts, then
line-numbered `rows`. Capture timestamps and kernel clocks are ignored for
comparison. `left_spans` / `right_spans` identify changed characters in paired
lines. The UI also compares historical boards.

## Transfer files without networking

Use `file-put --local <local-file> --remote <absolute-board-path>`,
`file-get --remote <absolute-board-path> --local <local-file>`, or
`file-copy --remote <source-path> --to-device <label> --to-path <target-path>`.
Only add `--overwrite` when replacement is requested. Confirm a Linux shell with
`base64`, `sha256sum` and `dd`. Transfers pin connection IDs and verify chunks;
the UI provides the same transfer operations.

These commands submit collector tasks and wait by default; `--background`
returns a task ID. Upload staging needs the client online, but submitted tasks
survive client closure. Use `task <ID>`, `task-cancel <ID>` and
`file-fetch <download-task-ID> --local <path>`. After a lost response, inspect
`tasks` before resubmitting. Cancellation cannot undo completed remote writes.

`hardware` lists configured actions allowed for this actor. Only execute
`task-start hardware-reset` or similar when already authorized by the user's
task. Configuration owns tools/arguments; agents do not supply arbitrary host
commands. Exclusive serial flashing temporarily suspends that port's capture;
other boards and SSH remain usable.

## Interactive serial or SSH

Use `ssh-open` for your persistent session ID, then `read --sid` / `send --sid`,
and finish with `ssh-close --sid`. Serial is a shared console: coordinate one
writer before sending input; an existing authorized arrangement needs no repeat
confirmation. Establish whether the console is a shell, login prompt or firmware
first. `send --text 'command' --enter` appends CR. Use `--file` or `--file -` for
other newlines/control characters. Accepted input does not prove completion.

Do not open managed tty/COM ports with `cat`, `stty`, `screen`, `minicom` or a
second collector. Direct-serial steps from another skill do not apply to managed
ports. Passive terminal viewing/replay never sends position reports. SSH
sessions still share board files and system state. This skill does not authorize
flashing, rebooting, USB rebinds or configuration changes beyond the user's task.

Consult `docs/AGENT_ACCESS.md` in the checkout, or
`/usr/share/doc/seriald/AGENT_ACCESS.md` in a Linux installation, for full API
contracts and exceptions. The client's `--help` lists available commands.
