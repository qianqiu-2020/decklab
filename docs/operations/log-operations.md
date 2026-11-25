# Log retention and diagnostics

## Unidentified serial connections

Before a board serial is confirmed, each connection is stored separately under
`logs/_unprovisioned/<attachment-id>/<connection-id>/`. Replugging the same UART
or restarting capture starts a new connection; it does not delete the previous
files. The live terminal shows only the current capture.

The serial archive panel lists current and previous connections on that attachment
by their first log timestamp. Select a capture to browse its dates and boots,
search, open a read-only terminal tab, or download it. Identical daily filenames
in separate captures remain separate. These are attachment records, not proof
that the same board stayed connected. Confirming a serial promotes only the
current connection's logs through the existing identity workflow.

`GET /api/logs` includes `connections` for an unidentified attachment. The optional
`archive_connection` selects a listed capture on `/api/logs`, `/api/log`,
`/api/boot-log` and `/api/boot-search`. Current-connection Agent grants keep their
original scope and cannot enumerate or read previous captures. Physical paths
are never accepted as capture identifiers.

## Retention

DeckLab preserves all archives by default. Optional retention is configured on the
collector (or Windows host), not the restartable control service:

```sh
DECKLAB_LOG_RETENTION_DAYS=30
DECKLAB_DISK_MIN_FREE_MB=1024
DECKLAB_DISK_MIN_FREE_PERCENT=5
```

Retention accepts 0–36500 days; `0` disables deletion. Enabled collectors check
at startup and at most once per hour. Only recognized serial/SSH daily `.log`
files with both a date and modification time older than the configured limit
are eligible. Undated/unknown files, current-day files, links and directories
are preserved. Active serial and SSH log handles are protected, including an
old-dated handle after a clock change. A running boot's indexed daily parts are
also retained, so cross-day boots keep their initial output. If its boot boundary
cannot be established, that active board's serial archives are kept conservatively.

Before each deletion the collector rechecks file identity and open writers under
the same lock order used by archive promotion. Directory enumeration occurs
outside writer locks. Failed deletion is counted and does not stop capture.
Retention removes files only: board identity, labels, command/session history and
database backups remain. Enable it only when removing old log files is desired;
deleted archives are not recoverable through the application.

Ubuntu: add the settings to `/etc/decklab/decklab.env`, then restart the collector.
Windows: set the environment values before starting the installed launcher, for
example `$env:DECKLAB_LOG_RETENTION_DAYS = '30'`. Configuration changes require
a collector/host restart. Editing control/UI alone does not reload them.

## Disk status

The header warns when free space falls below either configured threshold; terminal
access remains usable. `0` disables an individual threshold. The limits default to
1024 MiB and 5%. An unavailable disk check has a separate structured reason.
`decklabctl health` includes `storage` and `retention` status: last run, removed file
count/bytes and error count. The defaults warn without automatically deleting logs.

## Share a diagnostic summary

```sh
decklabctl diagnostics > decklab-diagnostics.json
```

`GET /api/diagnostics` returns the same JSON. It uses an explicit field allowlist:
application/Python/platform versions, uptime, disk summary, retention statistics
and board/session logging status. Board identifiers are hashed; no raw serials,
labels, USB/tty paths, hostnames, IP addresses, SSH titles, commands, raw error
messages, log contents, tokens or passwords are included. This summary is useful
for an issue report; detailed local health/log inspection remains separate.

Single-device agent grants cannot export platform diagnostics. Full-platform
agents and authorized human clients can; open mode follows its existing access
policy. The collector never reads raw log contents to build this export.
