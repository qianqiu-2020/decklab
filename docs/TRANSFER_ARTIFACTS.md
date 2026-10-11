# Host transfer artifacts

This storage layer supports collector-owned upload/download tasks and scoped HTTP
staging/download endpoints described in [TASKS.md](TASKS.md). Browser task history
can retrieve completed downloads. The browser transfer dialog and CLI `--background` transfers use collector-owned
tasks. Browser uploads require the page during staging only; submitted tasks
survive reload. Default CLI transfers submit the same tasks and wait; no CLI UART transfer loop
remains. Staging and local retrieval require the client, but collector execution
continues independently.

## Files and ownership

`<registry-directory>/transfers/` stores opaque random IDs with `.part` while
receiving and `.bin` after sealing. User filenames are metadata only and cannot
select host paths. Registry schema 4 stores actor, board serial, direction,
declared/received size, SHA-256, state, timestamps and optional owning task ID.
Directory/file creation modes permit the service group; actual access also follows
Unix umask or Windows ACLs. The directory is created only when a file is staged.

The service accepts chunks up to 1 MiB with an exact offset. Already committed
bytes can be retried only with identical content. Symlinks, nonregular files,
linked write targets and opening/replacement races are rejected. A write is
flushed and synced before metadata advances. Sealing checks completeness, hashes
bounded chunks, and publishes the finished file without replacing an existing
sealed path. Publication currently requires filesystem hard-link support.

Agent access checks both actor and requested board scope; the full human actor
can inspect artifacts. Task claims serialize consumers and prohibit deletion or
expiry while active. A failed/cancelled upload can return its unchanged host
source to `ready`; successful consumption removes its file. Downloads remain
unavailable until sealed as `available`. The task adapter verifies the artifact digest before committing a remote upload
or exposing a download. Host hash verification checks cancellation between chunks.

## Capacity and expiry

Declared sizes reserve quota and available disk space before creating files.
Each append rechecks free space. Collector maintenance checks expiry every minute;
active claims, download writers and open readers are excluded. Reading a sealed artifact refreshes
its expiry timestamp. Metadata remains after file deletion for task diagnosis.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERIALD_TRANSFER_MAX_FILES` | 128 | Maximum live files, including empty files |
| `SERIALD_TRANSFER_MAX_FILE_BYTES` | 8589934592 | Maximum declared file size (8 GiB) |
| `SERIALD_TRANSFER_MAX_TOTAL_BYTES` | 34359738368 | Total live declared sizes (32 GiB) |
| `SERIALD_TRANSFER_MIN_FREE_BYTES` | 268435456 | Disk space to preserve (256 MiB) |
| `SERIALD_TRANSFER_IDLE_SECONDS` | 3600 | Idle partial-upload expiry |
| `SERIALD_TRANSFER_RETENTION_SECONDS` | 604800 | Ready upload/download expiry (7 days) |

Values must be integer byte/second counts. File/expiry limits are positive and
total quota must accommodate one maximum-size file. Invalid settings stop startup.
These lifetimes govern temporary transfer files, not agent grants or board logs.

## Restart

One collector owns the spool. After task recovery marks unfinished execution
interrupted, artifact recovery returns unconsumed claimed uploads to ready without
sending board commands. A completed consuming task removes its leftover upload.
Partial downloads or interrupted publication become failed and are cleaned up.
Receiving uploads resume only when file size matches committed metadata; a crash
between file sync and database commit is reported as failure rather than replayed.
Missing files, invalid file types and size mismatches are failed and their quota
released. Same-size content changes must be caught by the task digest check. Completed downloads
and sealed uploads survive restarts until deletion or expiry.

Recovery snapshots cover database metadata, not artifact bytes. Do not start a
second collector on the same spool or restore only metadata while another process
is writing files. Native Windows artifact/installer validation remains outstanding.

## Identity confirmation

Schema 5 resolves task history by proven connection generation. When a pending
connection is identified, task-owned single-board artifacts follow its serial
in the same identity transaction. Unsubmitted staging is left unchanged because
it has no connection proof. Explicit identity correction and validated EEPROM
promotion migrate artifact scopes with the board record; file IDs and hashes
remain unchanged. Actor ownership and per-target grant checks still apply.
