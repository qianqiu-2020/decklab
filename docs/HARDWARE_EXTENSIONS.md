# Configured hardware actions

Hardware actions are disabled by default. A host administrator can configure
power-on, power-off, reset and an external flashing tool for specific board
serials. This is an adapter interface, not a bundled relay or board flasher.
Linux/POSIX uses an owned process group; Windows 10+ uses a Job Object.
Native Windows runtime validation is a separate release gate, not proven by
Linux mocks. Windows helpers should be EXEs (or an explicit interpreter plus
script), with absolute paths; no implicit cmd.exe or PowerShell wrapper is used.

Set `SERIALD_HARDWARE=/path/to/hardware.json` in the collector environment and
restart the collector when changing configuration. Start with
[`examples/hardware.json`](../examples/hardware.json), replace the board serial
and helper paths, and configure the helper's own relay/programmer mapping.
No configured action runs on service startup, probing or reconnection.

Each action has `argv` (an absolute executable and up to 63 arguments), an
integer `timeout` of 1–3600 seconds (default 60), and optional `allow_agent`
(default false). Optional `exclusive_serial` is a boolean (default false);
set it to true only for helpers that open the board's current serial port.
There is no shell expansion. Argument placeholders are limited
to `{serial}`, `{device}` and `{port}`; clients cannot supply command arguments,
executables or host image paths. Flash images are selected by host configuration
or the configured helper. Credentials and environment are not exposed in task
results. Keep configuration and helper permissions appropriate to the service
account; the application does not run sudo or request elevation.

```sh
serialctl --device riscv-118 hardware
serialctl --device riscv-118 task-start hardware-reset
serialctl --device riscv-118 task <returned-task-id>
serialctl --device riscv-118 task-cancel <returned-task-id>
```

`GET /api/hardware?key=…` lists actions visible to the caller, without executing
anything or exposing argv. `POST /api/task-start` uses `kind=hardware-power-on`,
`hardware-power-off`, `hardware-reset` or `hardware-flash` with the usual expected
serial/connection and actor. Agent grants retain device and actor scope;
configuration must explicitly enable that action for agents. These are trusted
host commands with the service account's authority, not a sandbox.

Independent fixed power commands can operate on an identified offline board.
Commands using device/port placeholders require those attachment fields.
Unidentified boards are rejected. All operations pin identity and generation,
respect input ownership, and acquire the existing console transaction lease;
a file transfer cannot be interrupted by starting a helper. Other devices and SSH readers remain active. Without `exclusive_serial`,
serial capture also remains active and the helper must not open that port.
With `exclusive_serial: true`, the task waits for the existing reader to close
its handle before starting the helper. Discovery cannot reopen that attachment
while the helper owns it. Once helper process-tree cleanup succeeds, capture
resumes on the same connection and archive without flushing pending bytes.
The log records the capture gap; output during exclusive ownership is not
recorded by DeckLab. Disconnect, changed host attachment or service shutdown
prevents resuming the old reader. If helper cleanup fails, the port stays paused;
confirm the external tool is stopped before restarting the collector. This
mode requires an active collector, so it cannot flash a known offline board.

Tasks record progress and exit status/duration. Operation history records start,
completion or stop. Helper stdout/stderr is discarded rather than exposing host
secrets in board history; consult the helper's configured external logging for
its detailed diagnostics. No passwords or argv are persisted in task results.
Timeout, cancellation or identity/connection change terminates the owned Linux
process group or Windows Job, including descendants. Even successful helper exit cleans up
remaining children. A nonzero exit, disconnect during execution or timeout is
reported as failed; cancellation is a request and cannot undo power changes or
a partially written image. Service restart interrupts tasks without replay.

Validate with simulation first. The regression suite launches only local Python
fixtures in temporary directories; it does not invoke relays, real flashers,
serial readers or recovery helpers.

## Windows process ownership

The Windows adapter uses native Unicode `CreateProcessW` with
`PROC_THREAD_ATTRIBUTE_JOB_LIST`, assigning the process before its first thread
can run. A second attribute restricts handle inheritance to a NUL standard-IO
handle; the Job handle is not inherited. `KILL_ON_JOB_CLOSE` provides cleanup if
the collector dies. Normal cleanup terminates the Job, verifies its active
process count reaches zero, waits for the root, and releases handles. If Job
creation/assignment fails, there is no fallback to an uncontained helper. The
Windows command line is limited to fewer than 32767 UTF-16 code units.

See [Microsoft’s atomic Job assignment explanation](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/)
and [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Cross-platform tests check ABI widths, attribute/handle ownership, quoting,
failure cleanup, timeout and cancellation. Native Windows tests also exercise
Unicode arguments, root-before-child exit and abrupt collector-helper-owner
termination; the Windows build must run these tests before packaging.
