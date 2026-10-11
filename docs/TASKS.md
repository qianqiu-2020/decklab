# Durable tasks: implementation contract

Durable tasks currently support `identify`, `ssh-enable` and `ssh-connect` through
the collector API, CLI and browser. Collector-owned `file-copy` is available
through the task API, browser and CLI `file-copy --background`. Collector-owned
upload/download are available through the task API and HTTP artifact staging. The browser task panel can retrieve completed downloads.
CLI `file-put --background` stages and verifies a local file before submitting an
upload; `file-get --background` stores a download on the host for later retrieval.
Without this flag, CLI transfers retain their foreground behavior. Browser-dialog
upload/download task initiation remains pending.

## API and CLI

`POST /api/task-start` accepts `key`, expected `serial`, `connection_id`, `kind`,
actor and optional human `client_id`; it returns HTTP 202 with a task record.
`GET /api/tasks` lists history with `limit` (1–100), `offset` and optional
`connection_id` (for pending connections) or `target_serial` (tasks involving a
board); filters apply before pagination. `serial` remains an all-target visibility
constraint used by scoped grants, separate from the involvement filter;
`GET /api/task?id=…` reads one record. `POST /api/task-cancel` requests cancellation
by `id`. These endpoints return normal JSON errors and retain task IDs after the
initiating HTTP connection closes.

```sh
python3 serialctl.py --device riscv-118 task-start identify
python3 serialctl.py --device riscv-118 tasks
python3 serialctl.py --device riscv-118 task <task-id>
python3 serialctl.py --device riscv-118 task-cancel <task-id>
```

Full-platform callers can list/query without selecting a board. Single-board
grants must supply a device key; control pins the serial and task actor, ignoring
caller-supplied actor headers. They cannot see/cancel another actor's tasks or
tasks with any other board target. Shared agents cannot start privileged
`ssh-enable` or boot-marker creation tasks. Revocation cancels active tasks
directly, without relying on a bounded history page.

Task adapters retain the original client, connection ID and stop-event generation
across background execution. Their cancellation event reaches every serial lease,
including leases acquired after an earlier interaction. A cancelled SSH-connect
task closes a newly created session; an existing reused session remains available.
Final results contain selected operation metadata, not raw probe dumps.

## Storage and execution

`TaskStore` was introduced in registry schema 3; schema 4 adds transfer artifact metadata. It persists a random task ID, operation kind,
actor and human client ID, historical serial/connection targets, state, timestamps,
progress, cancellation request and final result/error. Original request payloads
and executable callbacks remain in memory. Results are bounded to 64 KiB and
reject credential fields, including nested password/token values; operation
adapters must return result metadata rather than raw request or file contents.

States are `queued`, `running`, `completed`, `failed`, `cancelled` and
`interrupted`. Only queued/running rows accept progress or final-state updates.
Terminal rows cannot be overwritten by late callbacks. Progress has `done`,
`total` and `phase`; individual operation adapters choose units and phases.
Historical targets are snapshots, not live board bindings, and do not silently
follow an identity correction to another serial. A single-target task submitted
without a serial can also be found under its final discovered serial; the original
blank target snapshot is retained. This exception cannot widen multi-board access.

One `TaskManager` owns execution in a collector, with four active workers by
default. Capacity exhaustion refuses a new task before creating its record.
Runners receive `TaskContext`, report progress and check cooperative cancellation.
Transport adapters register cancellation hooks to wake waits; hooks must be
nonblocking. They run outside coordinator locks, including a hook registered
just after cancellation. Closing refuses new work, requests cancellation and
joins workers within a total deadline. A false close result means a worker is
still alive: its store must remain open until execution exits.

Cancellation is a request, not rollback. If an operation finishes despite a
concurrent request, the task remains completed with `cancel_requested=true`.
Only an observed cancellation becomes cancelled. The human web actor may cancel
any visible task; agents may cancel their own. HTTP authorization and serial
transaction/connection guards also apply inside task adapters.

On collector startup, unfinished records become interrupted with their last
progress retained. No board command is automatically replayed. Clients can
rediscover history after reconnect through the task API or browser task panel.
Scoped history excludes a multi-board task when any target is outside the grant;
actor and pagination filters are applied in SQLite before returning records.

## Browser behavior

Identification and SSH creation submit background tasks instead of holding a
request open until the operation finishes. The terminal toolbar's task button
opens paginated history with progress, result/error, cancellation and an existing
SSH-session shortcut. Reopening or reloading the page retrieves history without
replaying the operation. Visible history refreshes every five seconds.

A submitted operation is checked every second; network failures retry after two
seconds without starting another task. Authentication failures, revoked access or
a missing task stop the watcher. Device switches invalidate displayed callbacks
without cancelling the collector's work. Cancellation does not promise rollback.

## Collector-owned board copy

```sh
python3 serialctl.py --device deb1 file-copy --remote /root/log.txt \
  --to-device milkv2 --to-path /tmp/log.txt --overwrite --background
python3 serialctl.py task <returned-task-id>
python3 serialctl.py task-cancel <returned-task-id>
```

For `kind=file-copy`, `/api/task-start` additionally takes `path`, `to_key`,
`to_serial`, `to_connection_id`, `to_path` and optional `overwrite=true`. Both
connections and input ownership are checked. The task records both targets; a
single-board grant cannot submit, inspect or cancel it. Full-access task history
can filter by either involved board without weakening authorization.

The collector runs the existing framed transfer protocol and audit events,
prefetching one source batch while writing the previous batch. It keeps bounded
buffers, reports committed target bytes and verifies both SHA-256 values before
success. Cancellation wakes both leases; a peer failure also stops and joins
prefetch before cleanup. Abort removes this operation's temporary file only on
the original connection. Local reservations and timers are released by matching
transfer ID and connection even if remote cleanup is impossible. A disconnect
can prevent remote cleanup; cancellation
is not a guarantee that the destination has not already been committed.

The browser file-transfer dialog submits a collector-owned copy and follows its
task ID. Closing it (including Escape) keeps copying; reopening during execution
shows the original transfer. The cancel button requests cancellation by task ID,
even if pressed before submission returns. Delayed cancellation responses cannot
overwrite a later transfer's status. A lost submission response is never
automatically replayed: inspect task history before retrying. Completed copy
results remain visible under both source and destination boards after reload.

The shared browser task client serves copy, identification and SSH creation. It
retries reads on network outages, stops on revoked/missing access and rejects
mismatched task responses. Browser upload/download still require the initiating
page to remain connected until their future staging/task integration is finished.

The host-side artifact foundation is implemented separately in
[TRANSFER_ARTIFACTS.md](TRANSFER_ARTIFACTS.md). HTTP staging and upload/download worker adapters are implemented. Browser transfer initiation remains pending; foreground transfers still depend
on the initiating client. Direct task-API transfers run in the collector after
the submitting connection closes.

## Upload/download task API

Upload staging uses `POST /api/artifact-create` with board `key`, optional expected
`serial`, display `name`, `size` and actor. `POST /api/artifact-append` takes `id`,
`offset` and `data_b64`; decoded host chunks are at most 1 MiB. Only this endpoint
has a 2 MiB JSON body limit; other request limits remain 64 KiB.
`POST /api/artifact-seal` finalizes a complete upload. These host-only operations
do not reserve or write the board's serial console. `GET /api/artifact?id=…` reads
scoped metadata and `POST /api/artifact-delete` removes an inactive file.

Start `kind=file-upload` with `key`, expected serial/connection, `path`,
`artifact_id` and optional `overwrite=true`. The runner claims the sealed artifact,
uploads to a remote temporary file and checks the staged SHA-256 before remote
commit. A cancelled/failed board operation returns valid staging for retry;
corrupted sources are failed and discarded. Successful consumption removes the
host file.

Start `kind=file-download` with `key`, expected serial/connection and absolute
`path`. The collector reserves host space, reads bounded serial chunks and
verifies both remote and host SHA-256 before publishing. Cancellation wakes UART
waits and interrupts host hash verification between chunks. Failure removes the
partial artifact and releases its reservation. Result metadata includes
`artifact_id`, name, size and SHA-256, never file bytes or host paths.

`GET /api/artifact-download?id=…` streams an available download with content length,
attachment name and `X-Checksum-SHA256`. Clients should verify size/hash before
keeping their local file. The task panel offers the same download after reload.
Control pins scoped tokens to their actor/serial, strips untrusted actor headers
and stops forwarding a file after revocation. A single-board grant can stage
and transfer its own board files; cross-board copies still require all-board access.

## Background CLI file transfers

```sh
python3 serialctl.py --device deb1 file-put --local image.bin --remote /tmp/image.bin --background
python3 serialctl.py --device deb1 file-get --remote /tmp/image.bin --background
python3 serialctl.py --device deb1 task <returned-task-id>
```

Background downloads do not accept `--local`: the collector stores the verified
file, and the browser task list provides its download link. Upload staging uses
at most 1 MiB per request and compares its sealed size/SHA-256 before submitting.
A staging failure attempts to delete its inactive artifact. If task submission
loses its response, the client neither resubmits nor deletes staging: inspect task
history before retrying. Once submitted, closing the CLI does not cancel work.
