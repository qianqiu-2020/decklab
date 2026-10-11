# Console transactions

Each connection has one `SerialTransactions` coordinator. Input, framed shell
commands, root SSH provisioning, serial file commands and USB recovery share the
collector's operation lock. Other devices and SSH streams remain independent.
Collector reads continue through their existing sole reader; transactions never
open a second reader or configure termios.

A lease captures the connection ID and stop event, owns a total deadline, and
has a unique operation ID. Every serial write and response wait checks that the
lease still belongs to that connection. Linux partial writes check its cancel
signal between writes. Windows checks before sending and after the transaction;
a pySerial write already in progress is bounded by its configured five-second
write timeout. Cancellation does not close the shared collector handle.

The command timeout includes shell login and response waiting. Root SSH
provisioning has a 110-second total deadline; USB recovery has 35 seconds.
File-transfer commands use their operation-specific deadlines. An uncertain
transfer timeout retains checksum-based acknowledgement recovery; explicit
cancellation or replacement stops it. A cancelled upload remains failed until
the client calls the existing transfer abort endpoint, which attempts temporary
file cleanup and releases the transfer's exclusive console reservation.

## Inspect and cancel

`GET /api/operation?key=<label>&serial=<serial>` returns `operation: null` or an
object with `id`, `kind`, `actor`, `connection_id`, `started`, `remaining`, and
`cancelled`. Device snapshots also include this object. IDs refer to a single
operation, so a delayed cancellation cannot cancel its replacement.

```sh
curl 'http://127.0.0.1:8899/api/operation?key=demo-k1'
curl -X POST http://127.0.0.1:8899/api/operation-cancel \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-k1","id":"operation-id-from-status","actor":"web"}'
```

With authentication, include the normal bearer token. The control plane assigns
shared-agent actors and pins scoped tokens to their board serial. An agent can
cancel its own operation; the human `web` actor can intervene. Open LAN mode
uses caller-reported actors as elsewhere in the API, not authenticated identities.
Cancellation errors include stable `code` values (`not-found`, `forbidden`);
transaction failures distinguish busy, cancelled, stale, disconnected and timeout.

Cancellation stops host waiting and further writes. It cannot undo bytes already
sent or commands already running on the board. It wakes response waiters through
the collector condition variable and leaves serial output available for recovery.
The browser cancellation control and persistent task lifecycle are tracked in
the remaining application-services/long-operations workstreams.

## Optional input reservation

Input is shared by default. An agent can reserve serial input for its actor:

```sh
serialctl --actor claude-code/diagnosis --device riscv-118 input-claim
serialctl --actor claude-code/diagnosis --device riscv-118 operation
serialctl --actor claude-code/diagnosis --device riscv-118 input-release <ownership-id>
```

`POST /api/input-claim` returns `input_owner` with `id`, `actor`, `client_id`,
`connection_id` and `since`. Repeating a claim by the same owner returns its existing ID.
`POST /api/input-release` requires that ID; delayed releases cannot clear a newer
reservation. `GET /api/operation` and device snapshots expose the owner.
Other actors receive HTTP 423 / `input-owned` for input, probes and transfer
commands. Serial output and independent SSH sessions remain available.

The serial toolbar provides claim/release and human takeover. A human `web`
takeover cancels the old active transaction and wakes its waiter without closing
the collector; it cannot undo board-side commands. A failed transfer still needs
abort to release its separate transfer reservation. Revoking an agent grant also
releases its input claim. Claims end on connection replacement, board swap or
collector restart; they are not durable locks and have no idle expiry.

Agent ownership uses the actor, so the same agent can reuse its claim across CLI
calls. Human ownership also checks `client_id`: each page gets a fresh identifier,
and all terminal/probe/transfer requests carry it. Separate tabs and browsers
cannot write under another page's claim even though their audit actor remains
`web`. A different human page must explicitly take over before writing or releasing
the reservation. Reloading a page creates a new client; click takeover to continue
an existing reservation. Closing a page does not silently release an active board
operation; another human can take over.

Client IDs are coordination metadata, not credentials. They add no account or
login requirement. Use distinct actor names for independent agent workflows.
Open LAN actors remain caller-reported; authenticated agent grants override those
names at control and cannot adopt human ownership by supplying a client ID.
