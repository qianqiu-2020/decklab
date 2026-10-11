# Durable tasks: implementation contract

Durable tasks currently support `identify`, `ssh-enable` and `ssh-connect` through
the collector API, CLI and browser. Server-owned file transfer is still pending. Existing file-transfer requests keep their current behavior.

## API and CLI

`POST /api/task-start` accepts `key`, expected `serial`, `connection_id`, `kind`,
actor and optional human `client_id`; it returns HTTP 202 with a task record.
`GET /api/tasks` lists history with `limit` (1–100), `offset` and optional
`connection_id` (for pending connections); filters apply before pagination;
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

`TaskStore` uses registry schema 3. It persists a random task ID, operation kind,
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
