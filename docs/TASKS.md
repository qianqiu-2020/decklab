# Durable tasks: implementation contract

The task foundation currently provides internal storage and worker services.
It is not yet exposed as a user-facing task API or refresh-recovery UI. Existing
file-transfer requests keep their current behavior until operation integration
is complete.

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
follow an identity correction to another serial.

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
transaction/connection guards remain responsibilities of operation integration.

On collector startup, unfinished records become interrupted with their last
progress retained. No board command is automatically replayed. Clients will be
able to rediscover history after reconnect through the task API once integrated.
Scoped history excludes a multi-board task when any target is outside the grant;
actor and pagination filters are applied in SQLite before returning records.
