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

## Archives and history

Archive/history routes take `key`; include expected `serial` to protect against
label reassignment. Boot comparison instead requires both explicit key/serial/ID
triples, without a separate top-level `key`.

| GET route | Additional parameters / result |
| --- | --- |
| `/api/logs` | File list and detected boots with stable IDs |
| `/api/log` | `file`; default last 64 KiB, or `off` for 64 KiB pages; `full=1` reads the full snapshot, `download=1` streams attachment |
| `/api/boot-log` | `id` from the boots list; complete currently archived boot, including cross-day parts |
| `/api/boot-search` | `q`, `offset=0`, `limit=20` (1–50); case-insensitive search across archived boots |
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

Control owns GET `/api/access`, `/api/agent-grants` and POST `/api/agent-share`,
`/api/agent-revoke`, `/api/share-link`. Agent grants cannot mint further grants.
Authenticated browser-share recipients can manage agent grants; creating a
browser share requires the primary credential. A browser device URL locates a
board but does not restrict platform authorization. Agent tokens can restrict
access to one serial. See [AGENT_ACCESS.md](AGENT_ACCESS.md) for sharing, revocation
and command examples; do not call the collector's internal revocation endpoint.

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
