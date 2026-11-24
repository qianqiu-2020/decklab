# Durable tasks: implementation contract

Durable tasks currently support `identify`, `ssh-enable` and `ssh-connect` through
the collector API, CLI and browser. Collector-owned `file-copy` is available
through the task API, browser and CLI `file-copy --background`. Collector-owned
upload/download are available through the task API and HTTP artifact staging. The browser task panel can retrieve completed downloads.
CLI `file-put --background` stages and verifies a local file before submitting an
upload; `file-get --background` stores a download on the host for later retrieval.
Without this flag, CLI transfers submit the same collector task and wait for its
result; downloads then stream the artifact into the requested local path. The browser
file-transfer dialog uses collector-owned tasks for upload, download and copy.

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

Task IDs are opaque 24-character lowercase hexadecimal strings. A start response
contains `{"ok":true,"task":{...}}`; a query/cancel response contains `task`, and
a history response contains `tasks`. The returned record may already be running
or terminal when the start response arrives. `GET /api/tasks` defaults to 20
records with `offset=0`, newest first; there is no total-count field. Human
`web` queries include all actors; agent queries include only that agent's tasks.

The default collector permits four active workers and returns HTTP 429 when
capacity is exhausted, without creating another task. HTTP 202 confirms
submission, not successful execution. A lost submission response must not be
automatically replayed: consult history first. Task polling and artifact downloads
do not occupy a worker. Cancelling an already terminal task returns its existing
record without changing its outcome. `cancel_requested=true` is a request flag,
not evidence that execution stopped; inspect `state`.

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

For `kind=identify`, a web task can finish with `state=failed` and
`task.result.code=identity-confirmation-required`.
`task.result.identity_confirmation` contains a 600-second offer to associate the
current pending connection with its existing conflicted offline archive: `nonce`,
`serial`, `label`, `hostname`, `last_seen`, `last_dev`, `last_path`, `detected_at`,
`expires_at` and `connection_id`. Times are Unix seconds. This failed task does
not clear the conflict or bind the connection. After a human checks the archive
and supplies a nonempty reason, submit a **new** `kind=identify` task with the
same pending `key`, expected `serial`/`connection_id` and optional request field
`association={nonce,serial,reason}`. The serial inside `association` is the offered
archive identity; the top-level expected serial still describes the pending
connection. Only actor `web` may confirm. The new task probes identity again and
rejects expired offers, changed connections, changed archive/conflict state or
another online connection reporting the same serial. Collector restart also
invalidates offers. Querying a failed task never replays or confirms it; ordinary
unconflicted offline reconnects continue without this extra step.

## Storage and execution

`TaskStore` was introduced in registry schema 3; schema 4 adds transfer artifact metadata; schema 5 adds proven connection identity. It persists a random task ID, operation kind,
actor and human client ID, historical serial/connection targets, state, timestamps,
progress, cancellation request and final result/error. Original request payloads
and executable callbacks remain in memory. Results are bounded to 64 KiB and
reject credential fields, including nested password/token values; operation
adapters must return result metadata rather than raw request or file contents.

States are `queued`, `running`, `completed`, `failed`, `cancelled` and
`interrupted`. Only queued/running rows accept progress or final-state updates.
Terminal rows cannot be overwritten by late callbacks. Progress has `done`,
`total` and `phase`; individual operation adapters choose units and phases.
Historical `targets[].serial` and connection IDs remain immutable submission
snapshots. Registry schema 5 separately records a proven connection-to-board
association. Task targets include `resolved_serial` when such proof exists;
queries use it for board involvement and all-target scope checks. Identification
resolves earlier tasks on that exact generation, including failed tasks and
multi-board transfers, without relying on USB paths or labels. A replacement
board requires a new generation and cannot inherit the old history. Explicit
serial correction or validated EEPROM promotion migrates resolved associations
and task-owned artifact scopes together with board history.

Legacy single-target identify results remain readable through their returned
serial when no association exists. Upgrade does not guess bindings for other
old tasks. Each target of a multi-board task must independently be inside the
grant; resolving one endpoint never grants access to the other. Unsubmitted
upload staging has no proven generation and is not silently reassigned.

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

The shared browser task client serves all file transfers, identification and SSH creation. It
retries reads on network outages, stops on revoked/missing access and rejects
mismatched task responses. Uploads first stage bounded chunks on the host without reserving UART; this
staging phase requires the page to remain open. After task submission, upload
and download continue across page reloads. Completed downloads provide ordinary
streamed save links instead of browser-held Blobs, without the former 128 MiB
limit. Closing the dialog hides it; only its cancel button requests cancellation.

The host-side artifact foundation is implemented separately in
[TRANSFER_ARTIFACTS.md](TRANSFER_ARTIFACTS.md). HTTP staging and upload/download worker adapters are implemented. Browser and all CLI file transfers use the collector-owned task runners.
Only upload staging and local file retrieval require their client to remain online. Direct task-API transfers run in the collector after
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

Artifact operations return `{"artifact":{...}}`, with `ok=true` on successful
POSTs. IDs are separate opaque 24-character hex strings, even when an artifact
belongs to a task. Create requires a display filename (1–255 UTF-8 bytes, no
path separators or control characters) and a nonnegative integer size. Append
requires a nonempty chunk, integer offset and valid base64. Retrying already
committed bytes is accepted only if the entire chunk is within committed data
and identical; a partially overlapping retry is rejected. Seal is idempotent
for an already ready, unclaimed upload. Delete returns the retained metadata
with `state=deleted`, rather than making metadata queries return 404.

Metadata remains readable after expiry, failure or consumption; it does not
imply file availability. Download requires direction `download` and state
`available`; unavailable states return HTTP 409. Active task-owned files and
open download readers cannot be deleted. A completed download task stays in
history after its artifact expires, so fetch can fail even though the task is
still `completed`. Tasks themselves have no automatic history expiry.

## Background CLI file transfers

```sh
python3 serialctl.py --device deb1 file-put --local image.bin --remote /tmp/image.bin --background
python3 serialctl.py --device deb1 file-get --remote /tmp/image.bin --background
python3 serialctl.py --device deb1 task <returned-task-id>
```

Background downloads do not accept `--local`: the collector stores the verified
file, and `file-fetch <task-id> --local <path>` retrieves it. The browser task
list provides the same file as a download link. Upload staging uses
at most 1 MiB per request and compares its sealed size/SHA-256 before submitting.
A staging failure attempts to delete its inactive artifact. If task submission
loses its response, the client neither resubmits nor deletes staging: inspect task
history before retrying. Once submitted, closing the CLI does not cancel work.

## Retrieve a completed task file

```sh
python3 serialctl.py --device deb1 file-fetch <task-id> --local ./image.bin
```

`file-fetch` requires a completed `file-download` task. Full-platform callers
may omit `--device`; device-scoped grants must supply it. The CLI forwards its
bearer token and expected serial, verifies content length and SHA-256 against
the task result, and streams at most 64 KiB at a time into a local temporary
file. It publishes the file only after verification and a filesystem flush.
Existing paths require `--overwrite`; without it, concurrent destination
creation is also protected. Corrupt/truncated responses, revoked access and
interrupted retrieval leave no partial destination and remove the temporary
file. Retrieval never writes UART and does not delete the host artifact; it can
be repeated until the configured artifact expiry.

## Default CLI waiting and cancellation

`file-put`, `file-get` and `file-copy` use the same task submission as their
`--background` counterparts; the default command polls the returned ID every
500 ms. The CLI no longer coordinates UART chunks or board-copy prefetch.
Ctrl-C requests cancellation of that ID and reports whether the request was
confirmed; it does not promise rollback. A polling failure reports the task ID
without resubmission or implicit cancellation. Use `task <id>` to check its
final state and `file-fetch` to retrieve completed downloads later.

## Configured hardware tasks

`hardware-power-on`, `hardware-power-off`, `hardware-reset`, `hardware-enter-download` and `hardware-flash`
use the same persistent task API, actor scope, ownership and cancellation rules.
`GET /api/hardware?key=…` discovers permitted actions without running a tool.
Configuration fixes argv and optional exclusive UART handoff; clients cannot
choose host executables or image paths. See [HARDWARE_EXTENSIONS.md](HARDWARE_EXTENSIONS.md).

## Board-level flash tasks

`kind=flash` uses an immutable preview returned by `/api/flash-plan` and a
registered board serial. Submit `plan_id`, `binding_revision` and a unique
`request_id`. Its board target is independent of a UART `connection_id`, so an
offline board or an expected serial reconnection does not select a different
flash target. Existing identify, SSH, transfer and hardware tasks retain their
connection checks. This local flow assumes the board is already in download
mode; it does not reset the board.

A flash task reserves its board and configured flash endpoint across UART
disconnects. Passive serial capture continues, while competing input, probe,
transfer and hardware actions are refused. Different boards may run in parallel
when they use distinct resources. Details are exposed as `task.flash`; execution
events are retained in a separate bounded stream queried through
`GET /api/flash-events?id=…&after=…&key=…` rather than filling the final task result.
Use the returned cursor and observe a reported retention gap.

Repeated submission with the same actor, `request_id` and input digest returns
the original task, including after completion. A reused request ID with changed
input is rejected. Browser reconnection queries the original task instead of
restarting the operation. Results distinguish `write_result`, `verify_result`
and `boot_result`; a completed write with no boot observation is not a boot pass.
Cancellation and timeout do not roll back writes or trigger automatic retries.
On restart, unfinished tasks become `interrupted`; flash recovery ownership
remains until executor cleanup can be confirmed, and no write is replayed.

If the real partition layout becomes available only after temporary loader
preparation, the same task pauses at `awaiting-selection` for up to ten minutes.
`/api/flash-selection` previews the chosen storage/partition payloads against
that task's `layout_digest`, then accepts its immutable `plan_id` and a unique
selection `request_id`. The commit is deduplicated; it does not create another
task or prepare the loader again. Cancellation or expiry stops the waiting
operation without writing storage.

## 板卡用例

板卡页的「用例库」用同一个后台任务 API。控制机按板卡序列号预约，
串口继续被动采集。主访问身份可用 `kind=board-test` 提交，
附带 `key`、`serial`、`connection_id`、唯一 `request_id`、已选 `case`
以及 `rounds` 或 `seconds`。自定义 Shell 用例还需提供 `case_revision`。
取消停止后续轮次，已触发的一轮继续观察
到结果或超时。浏览器关闭后任务继续运行。采集器重启后在原任务中确认
旧服务已停止再释放板卡。观察超时且板端操作仍未确认结束时，也在原任务中
释放板卡。用例不会重复触发未确认的轮次，也不会自动复位。

运行结束后可在用例库选择任意已记录轮次，在线查看该轮的命令、输出和原始串口。
全部轮次的回读和完整串口仍可下载。超过最近轮次列表的记录可按轮号查找。

选择用例并点「查看用例」，可以查看内置用例的主要触发命令和「运行与通过判定」，
其中含板端检查、控制机等待及重连后取证。
从「新建 Shell 用例」添加一条或多条命令，可为每条填写一段预期串口文本。
所选板卡以 root 启动独立的 `/bin/sh` 执行每条命令。需要共用变量、工作目录或
Shell 函数时，把相关代码放在同一条。退出码必须是 0，并匹配已配置的文本才能
执行下一条。脚本超时记为退出码 124。

采集器按字节读取串口，匹配可以跨 USB 读取块，也允许输出末尾没有换行。
控制机只在当条命令的输出起止标记间查找原文。板端将命令的标准输出与错误输出
按同一字节流转发到串口，该区间内自然打印的内核日志也可用于匹配。连接中断或
采集缓冲缺失时记为无结果。若预期输出由后台进程稍后发出，应让当前命令等待
该事件。运行记录保留匹配结果、实际命令、串口和退出码。停止后不再启动下一条。
编辑只影响后续任务。检测整机重启或深度挂起时，使用相应的内置用例。

```sh
printf '检查系统版本文件\n'
test -r /etc/os-release
```

Linux 控制机需要通过本机 ADB（USB 调试桥）访问板卡，目标系统需要 root ADB shell、
systemd、Python 3 和可写的板端串口。用例库默认读取已在线的 USB 调试连接，
按连接回读板卡真实序列号，匹配成功后读取板端串口。板端必须有唯一的 ttyS 控制台。
若需固定接线或启用指定启动路径的重启老化，可在硬件 JSON 中登记 USB 路径及
**板上的设备节点**。USB 路径来自本机 `adb devices -l` 的 `usb:` 栏。

```json
{
  "board_tests": {
    "YOUR-BOARD-SERIAL": {"usb_port": "2-1.3", "uart": "/dev/ttyS0", "cases": ["rtc-wake"]}
  }
}
```

- `reboot-aging` 用于 K3 Pico-ITX 的 NOR → UFS → Debian。要求 Kingston
  TY7B-128、UFS EFI 启动项和实际 UFS 根盘，逐轮核对新启动编号、固件、
  ESP 和根盘与起始状态一致，并从串口核对 NOR 启动记录。
- `rtc-wake` 要求 Linux 已选中 deep。K1 可读 PMIC RTC 专用中断计数，K3 读取
  `rtc0` 设备自身的唤醒事件计数。带多个设备的共享中断不能作为 RTC 的证据。
  板端进程用 RTC 闹钟、串口布防标记、同一次启动内的挂起日志与唤醒计数共同取证，
  控制机等待窗口为 235 秒。
  恢复命令由操作者另行提交，在任务历史单独留痕。

结果页可查看最近轮次和首个异常，下载完整轮次清单及命令回读。首个异常的
原始串口按采集范围附上，显示最后一行的行号。串口连接改变或缓冲丢失会
标记证据缺口。每轮发送命令前都会核对当前 USB transport ID、板卡序列号
和启动编号。连接缺失期间只等待并观察，观察不足记为无结果。
