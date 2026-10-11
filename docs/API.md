# HTTP API reference

Use the public control URL, normally `http://127.0.0.1:8898`. Do not expose the
collector directly. Linux uses its private Unix socket; Windows uses the host's
private loopback collector. Both expose the following operations through control.
The standalone `serialctl.py` is the reference client; run its `--help` for CLI
options. API version and optional features come from `GET /api/capabilities`.

## Requests, identity and errors

GET parameters go in the query string. POST bodies are UTF-8 JSON objects with
`Content-Length`; chunked request bodies are unsupported. The ordinary body limit
is 64 KiB; `/api/artifact-append` permits 2 MiB for at most 1 MiB decoded bytes.
JSON responses use UTF-8. Downloads and SSE use separate streaming formats.
Request JSON rejects duplicate fields, non-finite numbers (including exponent
overflow), lone Unicode surrogates and nesting beyond 64 levels. Malformed
objects return HTTP 400 before device operations; valid Unicode commands and
escaped surrogate pairs are preserved.

Device operations take `key` (current label) and should include expected `serial`
to reject label reassignment. Background operations additionally pin the
`connection_id` returned by `/api/devices`; a changed connection invalidates old
work. `serial` may describe a provisional identity: inspect
`info.identity_status`, not the label or tty node. An offline device can retain
archives and independent SSH sessions. `capture_paused=true` means collection is
paused, including an exclusive external-tool handoff.

Use `Authorization: Bearer <token>` when authentication is enabled. Agent grants
pin the actor and optional board serial server-side; supplied actor fields cannot
widen their scope. Ordinary access uses POST `actor` (default `web`) for audit.
Browser input ownership also uses a per-page `client_id`. Actor names alone are
not authentication. Avoid putting credentials in URLs, screenshots or issues.

Errors contain `error` and `code`. Generic codes include `invalid-argument`,
`unauthorized`, `forbidden`, `not-found`, `conflict`, `too-large`, `input-owned`,
`internal-error`, `operation-failed`, `remote-failure`, `unavailable` and `timeout`; transaction-specific codes also
include `cancelled`, `stale`, `disconnected` and `transfer-busy`. Inspect HTTP
status as well as JSON: success of an input request does not prove command
completion, and cancellation is not rollback.
Some synchronous operations return HTTP 200 with `ok=false` and a failure code;
check both fields. JSON errors are not guaranteed after a streaming response has
begun: a truncated download or dropped SSE connection requires client recovery.

## Discovery and captured output

| Method and route | Parameters / behavior |
| --- | --- |
| GET `/healthz` | Control-process health, not proof of board health |
| GET `/api/health` | Collector uptime, version, device counts, disk and logging status; unavailable to single-board grants |
| GET `/api/capabilities` | Protocol version/features; no board commands |
| GET `/api/devices` | Online, pending and historical device snapshots; grants filter by serial |
| GET `/api/sessions` | `key`, `serial`; serial plus SSH sessions |
| GET `/api/session` | `key`, `serial`, `sid` (default `serial`), `off` byte cursor; snapshot or long poll |
| GET `/api/serial` | Serial-only cursor read, same byte semantics |
| GET `/api/session-events` | Same session selection; SSE, resumed by `Last-Event-ID` or `off` |
| GET `/api/operation` | Active console lease and optional input owner |
| GET `/api/hardware` | Configured actions allowed for the caller; never executes tools |
| GET `/api/diagnostics` | Sanitized platform diagnostic summary; unavailable to single-board grants |

Session packets contain `off`, `alive` and `data_b64`. Decode bytes incrementally
for split UTF-8 characters. `off` is the position after returned bytes; if
`off - decoded_length` exceeds the requested cursor, output was evicted from
scrollback. CLI reports this as `gap`. SSE event IDs are byte offsets, with a JSON
packet in `data:` and comment heartbeats. Reconnect reads do not send board input.
Use archives for earlier output that no longer fits the live buffer.
`off=0` returns an immediate buffered snapshot. A cursor at the current end can
long-poll for up to 15 seconds while the stream is alive; there is no public
`wait` parameter. `/api/operation` and `/api/hardware` require `key` and support
expected `serial`, like session routes. `/api/commands` returns at most 20 entries.

`/api/health` returns process responsiveness separately from logging and storage
status: HTTP 200 and `ok=true` do not imply a healthy board or successful log
writes. Inspect `logging_ok`, `log_errors` and `storage.warning`. If disk status
cannot be read, `storage.available=false`, `reason=unavailable` and the legacy
`disk_total`/`disk_free` fields are zero; zero here is not a capacity measurement.

## Archives and history

Archive/history routes take `key`; include expected `serial` to protect against
label reassignment. Boot comparison instead requires both explicit key/serial/ID
triples, without a separate top-level `key`.

| GET route | Additional parameters / result |
| --- | --- |
| `/api/logs` | File list and detected boots with stable IDs |
| `/api/log` | `file`; default last 64 KiB, or `off` for 64 KiB pages; `full=1` reads the full snapshot, `download=1` streams attachment |
| `/api/boot-log` | `id` from the boots list; complete currently archived boot, including cross-day parts |
| `/api/boot-search` | `q` (1–200 characters after trimming), `offset=0`, `limit=20` (1–50); case-insensitive search across archived boots |
| `/api/activity` | `session_offset=0`, `event_offset=0`, `limit=50` (1–50); independent history pages |
| `/api/commands` | Recently recorded commands for this board |
| `/api/boot-compare` | `left_key`, `left_serial`, `left_id`, `right_key`, `right_serial`, `right_id` |

Boot comparison uses two explicit identities; single-board grants cannot compare
another board. It limits each side to 4 MiB / 12000 lines and at most 1500 diff
context rows. Full boot retrieval has no such comparison limit. The newest boot
ID means the newest detected boundary, not necessarily a currently running board.

## Input and sessions

POST device requests use `key`, expected `serial`, `actor` and optional human
`client_id`. Use these operations only within the user's task authorization.

| POST route | Main additional fields |
| --- | --- |
| `/api/send` | `data`; literal serial input |
| `/api/session-send` | `sid`, `data`; serial or SSH input |
| `/api/ssh-connect` | Optional `title`, `ssh_user`, `ssh_password`; returns sid and persistence policy |
| `/api/session-resize` | `sid`, `cols`, `rows` |
| `/api/session-close` | `sid` |
| `/api/session-result` | `sid`, `completed`, `exit_code`; records command outcome |
| `/api/session-takeover` | `sid`; explicit human intervention in an agent session |
| `/api/input-claim` | Optional human `takeover=true`; returns ownership ID |
| `/api/input-release` | Ownership `id` |
| `/api/operation-cancel` | Active operation `id` |
| `/api/identify` | Optional `init_boot_marker=true`; active shell probe, may log in |
| `/api/probe` | Sends CR to request a prompt; this writes to the shared console |
| `/api/ssh-enable` | Explicit SSH provisioning permitted by device profile |
| `/api/label` | `label`; rename without moving archives |
| `/api/switch-mode` | Prepare this physical attachment for another board |
| `/api/rescue` | Explicit host USB recovery where supported |

For long identify/SSH operations, prefer background tasks. Non-web actors reuse
persistent SSH terminals, but CLI `exec` executes each command in a child `sh -c`.
It preserves the terminal, not changes made to the child shell's directory or
variables. Use one command for dependent steps, or interactive session input.
SSH output can be read by collaborators, but input, resize and close require
the session's actor or explicit takeover. Resize/close/takeover select an SSH
`sid`; they do not close or resize the physical serial collector. Root SSH
authentication-denial recovery follows the configured profile password and
provisioning policy; different per-request credentials do not trigger provisioning.

## Tasks, file transfer and sharing

See [TASKS.md](TASKS.md) for task submission payloads, states, pagination,
cancellation and collector-restart behavior, and
[TRANSFER_ARTIFACTS.md](TRANSFER_ARTIFACTS.md) for quotas/expiry. Preferred transfers
use `/api/task-start`, `/api/tasks`, `/api/task` and `/api/task-cancel`, with host
staging through `/api/artifact-create`, `/api/artifact-append`,
`/api/artifact-seal`, `/api/artifact-delete`, `/api/artifact` and
`/api/artifact-download`. Downloads include `Content-Length` and
`X-Checksum-SHA256`; verify both before publishing a local file.

Low-level POST `/api/file-transfer/start`, `/api/file-transfer/chunk`,
`/api/file-transfer/batch`, `/api/file-transfer/finish` and
`/api/file-transfer/abort` remain available. They use a
transfer ID, connection ID and byte offsets; new clients should prefer tasks to
avoid coordinating serial chunks themselves. Hardware task configuration and
exclusive UART ownership are documented in [HARDWARE_EXTENSIONS.md](HARDWARE_EXTENSIONS.md).

All low-level transfer POSTs include `key`, expected `serial`, `actor`, optional
human `client_id` and the current `connection_id`. They return HTTP 200 plus
`ok=true` on success; the transfer ID is separate from a background task ID.

| Suffix under `/api/file-transfer/` | Additional fields / response |
| --- | --- |
| `start` | `direction=upload` or `download`, absolute board `path`, upload `size` (nonnegative integer), optional `overwrite=true`; returns `id`, `size`, `offset=0`, `chunk_size`, `batch_size`, `gzip_available` and download `sha256` |
| `chunk` | `id`, exact integer `offset`, upload `data_b64`; download returns `data_b64`; response `offset` is committed end and `bytes` is this chunk's length |
| `batch` | Upload only: `id`, exact integer `offset`, `data_b64`; bounded by returned batch size and terminal input-line limits |
| `finish` | `id`; requires complete data, verifies integrity and commits an upload; returns `path`, `size`, `sha256`, `direction` |
| `abort` | `id`; releases the operation and attempts remote temporary-file cleanup; returns `aborted=true` |

Offsets advance only on successful responses. Do not blindly replay a chunk
whose response was lost; this protocol is not the host artifact append protocol.
An idle low-level transfer expires after ten minutes. Compressed data and framed
shell commands are handled internally; API `data_b64` represents original bytes.
Uploads may require smaller chunks when encoded commands exceed the board's
terminal line limit. Task-based clients handle this adaptation automatically.

Control owns GET `/api/access`, `/api/agent-grants` and POST `/api/agent-share`,
`/api/agent-revoke`, `/api/share-link`. Agent grants cannot mint further grants.
Authenticated browser-share recipients can manage agent grants; creating a
browser share requires the primary credential. A browser device URL locates a
board but does not restrict platform authorization. Agent tokens can restrict
access to one serial. See [AGENT_ACCESS.md](AGENT_ACCESS.md) for sharing, revocation
and command examples; do not call the collector's internal revocation endpoint.

| Control route | Payload / response |
| --- | --- |
| GET `/api/access` | `share_enabled`, `agent_share_enabled`, `open_access`, `loopback_open`, `lan_hosts`; UI capability hints for the current credential |
| GET `/api/agent-grants` | `grants` metadata, at most 200 newest rows; includes revoked rows and never returns token secrets |
| POST `/api/agent-share` | `scope=all` or `device`; device scope requires nonempty `serial` (at most 128 characters); returns `token` once and `grant` metadata |
| POST `/api/agent-revoke` | Grant `id` (16 lowercase hex characters); returns revoked `id` and `sessions_closed` cleanup confirmation |
| POST `/api/share-link` | No payload required; primary credential only; returns relative `path` and `expires_in` |

Control grant JSON bodies are limited to 64 KiB. Public grant creation does not
accept a lifetime: it creates non-expiring revocable grants. Revocation rejects
new authenticated operations immediately; `sessions_closed=false` means collector
cleanup could not be confirmed, rather than that the grant remains valid.

## Agent grant restrictions

Web-issued agent grants, including all-board grants, cannot call `/api/label`,
`/api/switch-mode`, `/api/rescue`, `/api/ssh-enable` or `/api/session-takeover`.
They cannot start `kind=ssh-enable` tasks or create board identity markers through
either identify interface. They can create SSH sessions; profile-controlled
automatic SSH provisioning after authentication denial still applies to that
connection workflow. Disable it in the profile when board configuration must
remain unchanged. Configured hardware actions require their explicit agent policy.

A single-board grant additionally cannot copy between boards, read global health
or diagnostics, or compare another board. Its device-scoped requests require
`key`, including task and artifact queries/cancellation; control pins `serial`
to the grant. Ordinary full-platform task/artifact requests can use IDs without
device selection. Environment-configured agent credentials identify an actor but
do not receive these web-issued grant restrictions. Open anonymous access also
retains full platform access; issuing a grant does not restrict other clients.

The hosted client `/serialctl.py` and skill `/skills/seriald-board/SKILL.md` are
public downloads. `/healthz` and API routes require the selected access mode's
credential. Browser share links last 24 hours; agent grants default to no expiry
and remain revocable. Query parameters are URL-encoded; POST JSON uses actual
booleans, not strings such as `"true"`.

## Source review coverage

The 54 public route names have been reviewed in these groups. This is a source
and local-contract review, distinct from hosted release or native platform tests.

| Group | Routes | Contract sources / regression fixtures |
| --- | ---: | --- |
| Discovery, health, streams | 11 | `web.py`, `queries.py`, `control_server.py`; HTTP, SSE, session-contract and collector-isolation tests |
| Archives and history | 7 | `queries.py`, `log_archive.py`, `boot_compare.py`; query, archive, boot comparison and HTTP tests |
| Device input and sessions | 16 | `actions.py`, `transactions.py`, `device.py`; action, protocol, ownership, registry and HTTP tests |
| Tasks and artifacts | 10 | `task_actions.py`, `tasks.py`, `artifact_actions.py`, `transfer_artifacts.py`; task, file-task and artifact tests; detailed payloads in [tasks](TASKS.md) |
| Low-level serial transfers | 5 | `actions.py`, `file_transfer.py`; transfer and transaction tests |
| Control access and sharing | 5 | `control_server.py`, `agent_grants.py`; HTTP, grants and sharing tests |

The route-name inventory check catches missing names; it does not prove payload,
authorization, cancellation or platform semantics. Those require source review
and the relevant contract tests above. Installer and real COM behavior remain
native Windows acceptance requirements.
