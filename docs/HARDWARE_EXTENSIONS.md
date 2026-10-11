# Configured hardware actions

Hardware actions are disabled by default. A host administrator can configure
power-on, power-off, reset and an external flashing tool for specific board
serials. This is an adapter interface, not a bundled relay or board flasher.
Linux/POSIX process-group execution is implemented; native Windows execution
is currently rejected until its process-tree adapter is available.

Set `SERIALD_HARDWARE=/path/to/hardware.json` in the collector environment and
restart the collector when changing configuration. Start with
[`examples/hardware.json`](../examples/hardware.json), replace the board serial
and helper paths, and configure the helper's own relay/programmer mapping.
No configured action runs on service startup, probing or reconnection.

Each action has `argv` (an absolute executable and up to 63 arguments), an
integer `timeout` of 1–3600 seconds (default 60), and optional `allow_agent`
(default false). There is no shell expansion. Argument placeholders are limited
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
a file transfer cannot be interrupted by starting a helper. Serial capture and
other devices/SSH readers remain active. Do not configure helpers that open the
same serial port while the collector owns it. A serial flasher needing exclusive
port ownership requires a future explicit collector handoff adapter.

Tasks record progress and exit status/duration. Operation history records start,
completion or stop. Helper stdout/stderr is discarded rather than exposing host
secrets in board history; consult the helper's configured external logging for
its detailed diagnostics. No passwords or argv are persisted in task results.
Timeout, cancellation or identity/connection change kills the owned Linux
process group, including descendants. Even successful helper exit cleans up
remaining children. A nonzero exit, disconnect during execution or timeout is
reported as failed; cancellation is a request and cannot undo power changes or
a partially written image. Service restart interrupts tasks without replay.

Validate with simulation first. The regression suite launches only local Python
fixtures in temporary directories; it does not invoke relays, real flashers,
serial readers or recovery helpers.
