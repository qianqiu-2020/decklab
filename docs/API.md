# HTTP API reference

Use the public control URL, normally `http://127.0.0.1:8898`. Do not expose the
collector directly. Linux uses its private Unix socket; Windows uses the host's
private loopback collector. Both expose the following operations through control.
The standalone `serialctl.py` is the reference client; run its `--help` for CLI
options. API version and optional features come from `GET /api/capabilities`.

## Owner configuration API

`GET /api/settings` returns `configuration` (version 1, unprefixed host keys in
`environment`, optional `profiles`, `hardware`, `flash`, `agent_tokens` objects
in `documents`), `revision`, `active_revision`, field metadata, restart
availability and activation state. Secrets are hidden. Only the primary
credential can access it; Agent tokens and webpage-share credentials cannot.
An open-access installation retains its existing open-owner semantics.
Responses use `Cache-Control: no-store`.

`POST /api/settings` requires `application/json`, UTF-8, `Content-Length`, and a
body of at most 2 MiB. Supported actions:

- `preview` / `save`: `{action, revision, configuration}`. Existing loaders
  validate the complete result. Imports are patches; omitted keys retain existing
  values and `{"$redacted":true}` preserves a same-field secret. Both return
  changed field names; only save persists. Stale revisions return 409. Saving
  does not activate settings.
- `export`: `{action:"export", include_secrets:false}`. Explicit `true` includes
  private host configuration values for an owner-requested backup. Registry
  identity/history and verified per-device SSH credentials are not included.
- `activate`: `{action:"activate", revision}`. Returns 202 after admission,
  409/423 for stale/busy settings. GET reports `activation` status. Control-only
  changes preserve the collector. Other changes refuse active tasks, recovery
  holds, transactions and input reservations before restarting the collector.
  Successful activation may change the address/password and disconnect HTTP.

`GET /api/settings-health` and `POST /api/settings-activate` are private
collector coordination routes. Public control returns 404 for them. They check
revisions and stop the existing collector before that same process re-executes.

`GET /api/device-preferences` is owner-only and returns `configuration`,
`revision` and current connection proofs. Its version-1 configuration contains
`boards: {serial: {label}}` and `ports: {attachment: {label, probe}}`; it never
contains SSH passwords, identity/archive data or grants. `probe` uses the
existing `{mode, command}` schema.

`POST /api/device-preferences` requires JSON of at most 1 MiB and
`{action:"preview"|"save", revision, configuration}`. Patches preserve omitted
entries; a port's explicit `label:null` or `probe:null` removes that override.
Unknown board serials, duplicate final board labels and invalid probes are
rejected. Changed configuration or connection proofs return 409; affected input
reservations/operations or maintenance holds block writes. Preview is read-only;
save applies immediately in the existing registry, with audit, without running
commands, binding identities or migrating logs. Host and device sections are
saved independently and have independent revisions. Control denies Agent and
web-share credentials before proxying this API.

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
include `not-idle`, `cancelled`, `stale`, `disconnected` and `transfer-busy`. Inspect HTTP
status as well as JSON: success of an input request does not prove command
completion, and cancellation is not rollback.
`session-controlled` means another client controls this shared SSH terminal;
explicit takeover is required before writing, resizing or closing it.
`terminal-mode-disabled` is HTTP 409 when serial terminal size setup is requested
outside the `linux` probe mode; it is distinct from the temporary `not-idle` state.
Some synchronous operations return HTTP 200 with `ok=false` and a failure code;
check both fields. JSON errors are not guaranteed after a streaming response has
begun: a truncated download or dropped SSE connection requires client recovery.

## Discovery and captured output

| Method and route | Parameters / behavior |
| --- | --- |
| GET `/healthz` | Control-process health, not proof of board health |
| GET `/api/health` | Collector uptime, version, device counts, disk and logging status; unavailable to single-board grants |
| GET `/api/capabilities` | Protocol version/features; no board commands |
| GET `/api/devices` | Online, pending and historical device snapshots; grants filter by serial or live connection |
| GET `/api/sessions` | `key`, `serial`; serial plus SSH sessions |
| GET `/api/session` | `key`, `serial`, `sid` (default `serial`), `off` byte cursor; snapshot or long poll |
| GET `/api/serial` | Serial-only cursor read, same byte semantics |
| GET `/api/session-events` | Same session selection; SSE, resumed by `Last-Event-ID` or `off` |
| GET `/api/operation` | Active console lease and optional input owner |
| GET `/api/hardware` | Configured actions and provider names allowed for the caller; never executes tools |
| GET `/api/diagnostics` | Sanitized platform diagnostic summary; unavailable to single-board grants |

Session packets contain `off`, `alive` and `data_b64`. Decode bytes incrementally
for split UTF-8 characters. `off` is the position after returned bytes; if
`off - decoded_length` exceeds the requested cursor, output was evicted from
scrollback. CLI reports this as `gap`. SSE event IDs are byte offsets, with a JSON
packet in `data:` and comment heartbeats. Serial packets also carry nullable `terminal_geometry` (`cols`, `rows`), retained
beyond live-buffer eviction. `terminal_initial_geometry` describes dimensions
before the first returned byte; apply it before replay, then process in-stream
resize markers. Do not use the final geometry to reinterpret earlier controls.
Serial SSE packets also carry
`input_owner` (an owner object or `null`). Ownership changes can produce an empty
`data_b64` packet with the **same `off`**; apply metadata before deduplicating
output bytes. Quiet serial streams check this state within about one second;
device-list polling remains a fallback. Reconnect reads do not send board input.
Use archives for earlier output that no longer fits the live buffer.
Browsers opt into canonical presentation with `screen=1&terminal_rev=<revision>`
and their per-page `client_id`. The packet includes `terminal_state` (geometry,
revision, phase, cursor, modes, controller and control_revision). A new/evicted
cursor or changed geometry returns `terminal_snapshot`: base64 `data`, followed
by base64 `pending` (unfinished UTF-8/control prefix), with its own `off` and
geometry. Reset the local emulator before applying it. Otherwise `data_b64`
contains ordered presentation bytes; their length can differ from raw captured
bytes, so use the returned `off`, never compute the next cursor from that length.
Apply metadata even at unchanged offsets. `terminal_writable` tells an SSH
viewer whether its actor/client can input. Screen reads check quiet state at
least once a second. Raw CLI reads without `screen=1` retain byte/gap semantics.
Only the backend responds to live terminal protocol queries; viewers must
suppress automatic replies, including DSR, DA and terminal-mode queries.

`off=0` returns an immediate buffered snapshot. A cursor at the current end can
long-poll for up to 15 seconds while the stream is alive; there is no public
`wait` parameter. `/api/operation` and `/api/hardware` require `key` and support
expected `serial`, like session routes. `/api/commands` returns at most 20 entries.

Board controls use existing `POST /api/task-start` kinds `hardware-reset`,
`hardware-power-on`, `hardware-power-off` and `hardware-enter-download`.
Only administrator-configured actions are accepted; clients cannot supply a BMC
address, target mapping, credential, reset type or helper arguments. The BMC
provider's outbound `/api/v1/login` belongs to the configured BMC service, not
DeckLab's public API. See [control adapters](HARDWARE_EXTENSIONS.md#板卡控制适配).

`/api/health` returns process responsiveness separately from logging and storage
status: HTTP 200 and `ok=true` do not imply a healthy board or successful log
writes. Inspect `logging_ok`, `log_errors` and `storage.warning`. If disk status
cannot be read, `storage.available=false`, `reason=unavailable` and the legacy
`disk_total`/`disk_free` fields are zero; zero here is not a capacity measurement.

Each `/api/devices` entry includes `serial_health`: `status` (`pending`, `waiting`,
`ok`, `error`), `running`, `reason`, `last_attempt`, `last_success` and `next_check`
(Unix timestamps). The collector verifies an idle Linux shell hourly; busy or
owned input defers the check by a minute. A valid host-side handle alone does not
prove board responsiveness. Internal setup, probe and transfer echoes are hidden
from managed screens and serial archives; raw session cursors remain available
for protocol clients.

Device snapshots also include `ssh_settings={saved,user}`. `saved` indicates
whether this device has reusable server-side SSH credentials; `user` is the
saved username, or the configured profile username when no credentials are
saved. The password is never returned. Credentials are bound to a known board
serial, independently of its current label, IP address or serial-port name.

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
| `/api/ssh-connect` | Optional `title`, `ssh_user`, `ssh_password`; returns sid, session persistence policy and credential-save status; authentication rejection uses `code=ssh-authentication-failed` and nonsecret `ssh_user` |
| `/api/session-resize` | `sid`, `cols`, `rows`; serial also requires `observed_off`, with optional `automatic` boolean, `presentation` boolean and per-page `client_id` |
| `/api/session-close` | `sid` |
| `/api/session-result` | `sid`, `completed`, `exit_code`; records command outcome |
| `/api/session-takeover` | `sid`; take control of shared SSH input and dimensions |
| `/api/input-claim` | Optional human `takeover=true`; returns ownership ID |
| `/api/input-release` | Ownership `id` |
| `/api/operation-cancel` | Active operation `id` |
| `/api/identify` | Optional `init_boot_marker=true` or web-only `association={nonce,serial,reason}`; active shell probe, may log in |
| `/api/probe` | Sends CR to request a prompt; this writes to the shared console |
| `/api/ssh-enable` | Explicitly install/start SSH from an already-root serial shell; requires authorization for board changes |
| `/api/label` | `label`; rename without moving archives. Pending serial attachments also require expected `serial=""` and `connection_id`; their display label does not change the API key or create a board identity. |
| `/api/switch-mode` | Prepare this physical attachment for another board |
| `/api/rescue` | Explicit host USB recovery where supported |

Pending attachment labels use the existing label format (1–64 ASCII letters,
digits, dots, underscores or hyphens). They persist by the serial attachment's
stable location and return as `attachment_label` and `label`, with `serial=""` and
the existing pending `device_key`. Reconnecting the same attachment restores the
name. First identification may inherit an unused attachment label; reconnecting
an existing board preserves that board's stored label and archive. Labels never
match or merge board identities.

If identification finds an existing offline archive with a previously conflicted
serial, a web caller can receive `code=identity-confirmation-required` and
`identity_confirmation`. The offer contains `nonce`, `serial`, `label`, `hostname`,
`last_seen`, `last_dev`, `last_path`, `detected_at`, `expires_at` and `connection_id`;
times are Unix seconds. After a human verifies uniqueness and archive ownership,
submit another identify request for the same pending connection with
`association={nonce,serial,reason}`. The reason must be nonempty. Only actor `web`
can confirm; agent callers cannot use this mechanism. Offers last 600 seconds
and are invalidated by reconnection, collector restart or changed archive/conflict
state. Confirmation probes the board again before binding and records the reason.
Another online connection reporting the serial prevents confirmation. Ordinary
offline reconnects without a recorded conflict need no extra confirmation.

For long identify/SSH operations, prefer background tasks. Non-web actors reuse
persistent SSH terminals, but CLI `exec` executes each command in a child `sh -c`.
It preserves the terminal, not changes made to the child shell's directory or
variables. Use one command for dependent steps, or interactive session input.
SSH output can be read by collaborators. Input, resize and close require both
session access and the current controlling actor/client. Another page using
the same web actor still needs explicit takeover. Resize/close/takeover select an SSH
`sid`; they do not close or resize the physical serial collector.

Default SSH opens use the server-saved account and password for that board when
available, with one authentication attempt. Without saved credentials, they try
the profile password, `bianbu`, then `debian`, deduplicated. Only authentication
denial advances that list; network failures and timeouts stop. An explicit
`ssh_user` or `ssh_password` override attempts only that credential pair.
Authentication failure returns HTTP 502 with `code=ssh-authentication-failed`
and `ssh_user`, without a password. A failed durable SSH task preserves those
nonsecret fields in its result so the web UI can offer credential entry.

Successful web connections save the verified pair for an identified board;
explicit web overrides replace the previous pair only on success. Non-web
actors may use saved credentials, but do not update them. Unidentified targets
do not persist credentials by IP address or COM/tty port. A successful response
and durable task result include `ssh_credentials_saved` and may include a safe
`ssh_credentials_note` explaining why a working connection was not saved.
Credential storage failure does not turn a working SSH session into a connection
failure. Cancellation and connection state are checked before the save; a later
cancellation does not roll back a committed credential update. Reusing saved
credentials does not update their revision. The private server record is separate
from public board metadata; passwords never appear in task records or responses.
Profile files and serial console credentials are unchanged. See
[remembered SSH credentials](CONFIGURATION.md#remembered-ssh-credentials)
for storage and reuse rules. Normal SSH opens do not reset root's password or
edit sshd configuration; manual root setup is documented in
[configuration](CONFIGURATION.md#manual-root-ssh-setup).

SSH resizes the actual PTY/channel and canonical screen; applications receive
the normal window-size change. Pure UART has no out-of-band window-size message.
Serial size setup requires a fresh idle Linux shell prompt and matching offset;
it accepts complete user/host prompts and BusyBox path prompts such as `~ #`,
`/ #`, `/etc #` and `~/work $`. The path form requires a space or tab before
`#`/`$`; login/password prompts, U-Boot and a prompt with typed input are not idle
shells. The existing two-stage setup sends the short size command before the
environment command and completes before queued browser input; baud, parity and
serial framing are unchanged. Busy applications retain the established grid.

In `manual` or `custom` probe mode, serial size setup sends no Linux commands and
returns HTTP 409 with `code=terminal-mode-disabled` and
`error=当前探测模式未启用 Linux 终端尺寸同步`. In `linux` mode, a stale offset or a
non-idle shell instead returns `code=not-idle`. `presentation=true` adjusts
boot-text display only, writes nothing to the board, and is ignored during
firmware or active programs.

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

### Board-level flashing

The dedicated `flash` task is separate from the legacy `hardware-flash` fixed
command. It supports an automatically discovered USB download target, or an
administrator-configured binding for a registered board. Automatic discovery
does not require prior UART identification or a configuration JSON file when the
local executor is installed. Enter download mode before submitting; this flow
does not operate reset pins. UART is optional and its connection generation is
not the flash target. Other connection-bound operations keep their existing guards.

| Method and route | Parameters / behavior |
| --- | --- |
| GET `/api/flash-discover` | Human-only USB download-device discovery; returns candidate target IDs and observed target generations, without resetting or writing storage |
| POST `/api/flash-target` | Human-only target selection with `target_id` and expected `target_generation`; reads supported device identity/layout information and rejects a changed or ambiguous target |
| GET `/api/flash-capabilities` | `key`, expected `serial`; configured execution location, supported modes and ready artifacts; no reset or storage write |
| POST `/api/flash-plan` | `key`, `serial`, `artifact_id`, `mode` (`full` or `partitions`), `binding_revision`, `verify` (`if-supported` or `required`), optional `selections` and optional full-disk `target_storage`; returns an immutable plan preview |
| GET `/api/flash-events` | `key`, `serial`, task `id` and nonnegative `after` event cursor; bounded events and flash details |
| POST `/api/flash-selection` | For the same task in `awaiting-selection`, `id`, `layout_digest` and `selections` with `preview=true` return a preview; submit its `plan_id` and unique `request_id` to continue |
| POST `/api/flash-recovery` | Human-only recovery receipt: `key`, `serial`, task `id`, expected `attempt_id`, nonempty `reason` and `confirm_stopped=true`; releases recovery resources after the operator verifies the original executor stopped |
| POST `/api/task-start`, `kind=flash` | `key`, `serial`, `plan_id`, `binding_revision` and unique `request_id`; creates or retrieves the same durable task |
| POST `/api/task-start`, `kind=nor-backup` | Human-only K3 NOR workspace: `target_id`, `target_generation`, `binding_revision` and unique `request_id`; no image or write scope. Loads pinned RAM materials, backs up the entire NOR, then waits for selection in the same `flash` task |

NOR workspaces expose `flash.nor_before` and `flash.nor_after` with scoped download
artifact IDs and bounded partition content metadata. Versions are embedded fields
or component banners; build timestamps are not firmware versions. Environment values
are opaque. The immutable executor plan is unchanged by `payload_metadata` sidecars.
The Web UI defaults to selected partitions and required readback in this workflow.
`readback_bytes` counts all returned bytes; `verified_bytes` counts exact padded
payload bytes and bytes outside the selected partition capacities. Selected tails
remain in the readback file without an invented expected value.

The same task stops in `awaiting-selection` before any storage write. Uploads and
URL imports may include its `task_id` plus the **original** target generation, even
when the task loaded a new RAM USB generation. The backend checks the waiting task
and lease before accepting host-side imports. Preview with `/api/flash-selection`,
then confirm its fixed `plan_id` in the original task. Cancelling preparation keeps
the completed backup. `flash.request_id` comes from the durable request table and
allows recovery of an uncertain submission without replaying it.

For automatically discovered devices, `target_id` and `target_generation`
replace the configured board's `key`/`serial` scope in capability, artifact
creation, plan and task requests. The caller cannot supply a USB path or turn a
target ID into an arbitrary board identity. A reliable, unconflicted serial can
associate the target with its existing board; otherwise task history retains a
USB target and no invented serial or UART connection. Target-specific task/event
queries use `target_id` and the original task ID. Human callers may use automatic
targets; agent grants do not gain access by guessing their target IDs.

Automatic partition capabilities require reliable live GPT/layout and target
partition-size checks. Raw full-disk capability additionally requires an
independently verified storage capacity and supported whole-disk target; a GPT
backup location or the sum of partition sizes is not evidence of device capacity.

A detected SpacemiT K1/K3 BootROM may report `preparable=true` and
`can_prepare_from_image=true` before a live layout is available. The caller can
upload a standard flashing package and preview its fixed loader preparation;
only the submitted task loads the verified package members. Loader paths and the
derived execution profile remain private. The task retains its original USB
target/generation snapshot while accepting its bounded, verified loader-mode
transition on the same physical socket. Selection and continuation submit that
original task snapshot; clients do not start another flash task or switch to an
unrelated newly enumerated device. Storage writes require the final live layout
and a separately previewed partition selection.

While that original task is `awaiting-selection`, `/api/artifact-create` may
include its `task_id` together with the original `target_id` and
`target_generation`. This stages an additional payload under the task's approved
USB transition; it does not create another task or load the temporary environment
again. Normal uploads keep their current-generation checks.

Partition selections contain `storage`, `partition` and `artifact_id`. Storage
and partition together identify a destination; two disks may have the same
partition name. A partition plan retains `write_layout=false`. Preview shows
all writes, sizes and SHA-256 values. Missing reliable target layout, unsupported
raw disk handling, capacity mismatch or required verification prevents writing.
No request accepts an executable, command arguments or an arbitrary host path.
For an original full disk image, `target_storage` fixes the exact storage ID;
multiple eligible disks require an explicit selection. This parameter does not
redirect package entries to another disk or apply to partition mode.
When inspection requires a temporary loader, the preview marks preparation as
pending; only the submitted task may load it. A partition task without a final
selection waits for up to ten minutes after reading the real layout. Preview
and commit refer to that same layout digest and task; they do not repeat loader
preparation. Missing or changed layout rejects the selection before writing.

Keep a submission's `request_id` when its response is lost. Reusing it with the
same input retrieves the original task; different input is rejected. Poll the
existing task and event cursor after a page reload. Closing a page does not stop
execution. Query and cancellation use the ordinary task endpoints and actor
scope. Cancellation may leave a partially written target. Write, verification
and boot results are recorded separately; writing does not prove that an
operating system booted. A restart never replays the write, and uncertain process
cleanup keeps the board reserved for recovery.
An inspection process with uncertain cleanup also creates a failed
`flash-inspect` record with `recovery_required=true`; the failed preview response
includes its `task_id`. Query it without retrying the inspection. Recovery
confirmation records the operator's check, keeps the original task result and
state, and does not retry, resume or declare the image successfully written.

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
| GET `/api/access` | `share_enabled`, `agent_share_enabled`, `open_access`, `loopback_open`, `lan_hosts`, `default_language` (`en`, `zh-CN` or empty), `mcp_enabled`, `mcp_url`, `mcp_error`; UI capability hints and host language default |
| GET `/api/agent-grants` | `grants` metadata, at most 200 newest rows; includes revoked rows and never returns token secrets |
| POST `/api/agent-share` | `scope=all`, `device`, or `connection`; device scope requires nonempty `serial` (at most 128 characters); connection scope requires live `key` and expected `connection_id`; returns `token` once and `grant` metadata |
| POST `/api/agent-revoke` | Grant `id` (16 lowercase hex characters); returns revoked `id` and `sessions_closed` cleanup confirmation |
| POST `/api/share-link` | No payload required; primary credential only; returns relative `path` and `expires_in` |

Control grant JSON bodies are limited to 64 KiB. Public grant creation does not
accept a lifetime: it creates non-expiring revocable grants. Revocation rejects
new authenticated operations immediately; `sessions_closed=false` means collector
cleanup could not be confirmed, rather than that the grant remains valid.

## Agent grant restrictions

Web-issued agent grants, including all-board grants, cannot call `/api/label`,
`/api/switch-mode`, `/api/rescue`, `/api/ssh-enable` or `/api/session-takeover`.
GET/POST `/api/probe-settings` is restricted to human web access, including when
using an environment-configured agent credential.
They cannot start `kind=ssh-enable` tasks or create board identity markers through
either identify interface. They can create SSH sessions, including default
password fallback attempts, but authentication rejection does not trigger root
password or sshd policy changes. Configured hardware actions require their
explicit agent policy.

A single-board grant additionally cannot copy between boards, read global health
or diagnostics, or compare another board. Its device-scoped requests require
`key`, including task and artifact queries/cancellation; control pins `serial`
to the grant. Ordinary full-platform task/artifact requests can use IDs without
device selection. Environment-configured agent credentials identify an actor but
do not receive these web-issued grant restrictions. Open anonymous access also
retains full platform access; issuing a grant does not restrict other clients.

A connection grant allows raw terminal access even without a successful probe or
serial number. Control pins its `connection_id` on every scoped request and the
collector verifies the same live generation. It follows an identity discovered
within that connection, but unplugging/replacing it or preparing a board switch
invalidates access. It never authorizes another connection at the same port.
An empty device list means the granted connection is no longer available; ask a
human to issue a new grant. Do not silently retry against another device.

## Custom probe settings

GET `/api/probe-settings` takes `key` and expected `connection_id`, returning
`mode`, `command`, `default_command` and the current `connection_id`. POST the
same route with `key`, `connection_id`, `mode` and `command` to save. Modes are
`linux` (built-in probe), `custom` (1–16384 UTF-8 bytes of POSIX shell script) and
`manual` (no automatic shell login or probing). Non-custom modes use an empty
command. Saving is passive: the human invokes Identify separately to execute.
Preferences persist by physical attachment; they are not board identity and must
be reviewed when attaching a different target to that port.

Custom scripts can output `HOST=`, `SERIAL=`, `MODEL=`, `SSHD=` and `IP=interface
address/prefix` lines, plus the standard optional identity fields. Metadata-only
output is useful without binding a board. A serial still passes the normal
hardware-identity validation, duplicate checks and human archive-association
rules; missing metadata never fabricates a serial. Arbitrary non-POSIX consoles
can use manual mode and the raw terminal instead.

The hosted client `/serialctl.py` and skill `/skills/seriald-board/SKILL.md` are
public downloads. Skill content defaults to English; `?lang=zh-CN` selects
Chinese, and `?lang=en` explicitly selects English. Both return UTF-8 Markdown.
`/healthz` and API routes require the selected access mode's
credential. Browser share links last 24 hours; agent grants default to no expiry
and remain revocable. Query parameters are URL-encoded; POST JSON uses actual
booleans, not strings such as `"true"`.

## Remote MCP

`/mcp` is the same-origin Streamable HTTP endpoint, independently of the
`/api/*` JSON API. It accepts MCP protocol messages without rewriting their
actor or JSON-RPC fields. Every caller supplies its own revocable Agent Bearer
grant; owner passwords, browser share cookies and anonymous access do not grant
MCP access. The SDK runs on a private loopback endpoint managed by control.
`mcp_enabled` reports current server availability; when unavailable, terminal
and log API access continue. Clients only configure the public URL and token.
See [MCP deployment and client configuration](MCP.md).

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

Automatic flash-target responses can include a bounded, printable `diagnostic`
string alongside their stable `reason` code, including `target-unavailable` or
`probe-protocol-failed`. The string explains the opening/read-only probe error;
it never changes `supported`, grants preparation/writes or triggers retries.
Absent layout/readback information during a failed connection remains unknown.

## Split Web/backend and image imports

`GET/POST /api/settings?target=backend` manages hardware-node settings using the
primary Web credential; `/api/settings` manages the Web node. The private
`GET/POST /api/backend-settings` exists only on the authenticated TLS backend
and is not exposed directly by Web. See [deployment](DISTRIBUTED.md).

`GET /api/image-catalog?source=public|internal&path=k3/version/` browses direct
children of the configured public archive or internal Nexus. Empty `path` lists
K1/K3. `refresh=1` bypasses the 60-second metadata cache. Responses contain
`entries` with `name`, `path`, `kind` and, for images, `url`, available size/date
metadata and `validation: preview-required`. Repository failures return 502;
no image download or board action occurs. See [image store](IMAGE_STORE.md).
`GET /api/image-library` returns ready shared uploads and existing active global
image-download tasks. These catalog/library routes and URL import require a human
Web operator; Agent grants cannot access them.

`POST /api/image-download` accepts an optional Web-operated device/USB target scope,
`url`, required stable `request_id`, optional `name` and `sha256`. During prepared
USB selection, include `task_id` and the original USB scope as described above. It returns
HTTP 202 and the existing durable task. Without a device scope, the ready upload
is stored in the shared image cache (`serial: ""`), reusable for different boards
by human operators. An invalid nonempty device scope never becomes a shared import. Poll `/api/task?id=...`, cancel with
`/api/task-cancel`. Completion returns `result.artifact` (`id`, `name`, `size`,
`sha256`, `serial`, `state: ready`), usable by `/api/flash-plan`. Reusing an ID
with identical input retrieves its original task; different input returns 409.
No client token/cookie is forwarded to image servers. URL imports are denied
to Agent tokens. Downloading never prepares or writes a board.
