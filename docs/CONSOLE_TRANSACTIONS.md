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
