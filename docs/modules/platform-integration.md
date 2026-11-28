# 平台通过 HTTP 接入 DeckLab

测试平台、开发工作台或机柜服务可以复用 [`decklabctl.py`](../../decklabctl.py) 的 `Client`，只需
Python 标准库。平台通过 HTTP 调用控制入口，不依赖 MCP，也不直接连接 collector。
原平台继续管理设备业务 ID、任务、用例、重试策略和最终验收；DeckLab 保存执行与原始证据。
这份接口支持单 collector 的执行占用，`cross_platform_exclusive=false`；
BMC、其他工作台等旁路写入口仍须遵守原平台的分配和维护约定。

## 协商和设备映射

先 `GET /api/capabilities`，检查 `api_version=1`、
`contract={name:"decklab-execution",revision:1}` 及所需能力。`Client.negotiate(kind)`
核对持久请求去重、按原请求查询、物理结果字段，以及该 kind 是否支持去重。
能力缺失时返回 `unsupported-contract`，不会尝试提交操作。增加的响应字段可忽略；
不认识的物理结果按 `unknown` 处理。全局能力不证明某块板支持该动作，仍需查询
`/api/hardware` 或 `/api/flash-capabilities`。

平台保存原业务设备 ID 到已核实 `serial`、`connection_id` 的映射，示例见
[`platform-device.json`](../../examples/platform-device.json)。不要把 IP、COM/tty、标签或首个在线口
当作永久板号。`Client.select_mapping()` 只接受唯一的精确序列号和当前连接代次；
连接变化返回 `stale-mapping`，不自动换到其他设备。建立映射时先由平台核实
`info.identity_status`、物理板和接线；候选软件编号不能未经核实就升级为平台永久身份。

```python
from decklabctl import Client

client = Client(control_url, timeout=10, token=platform_token)
client.negotiate("hardware-reset")
client.select_mapping({
    "platform_device_id": "test-room/slot-7",
    "serial": verified_serial,
    "connection_id": verified_connection_id,
})
```

地址和令牌来自平台配置，令牌放 `Authorization: Bearer …`，不放 URL 或日志。
平台执行者应使用独立的 Agent 授权；服务端固定其 actor。`actor` 自报名称不能代替认证。
原请求查询必须使用原凭据和原 actor 范围，不能用新账号猜测其他执行者的请求。
主访问身份通过 `lookup_actor` 查询其先前 POST 保存的 actor，客户端自动填写；
Agent 授权始终由服务端固定 actor，不接受此参数扩大范围。

## 先占用，再执行

板级占用复用 `POST /api/input-claim`，字段为 `scope:"board"`、`ttl:30..3600`，
以及正常的 `key/serial/connection_id`。响应包括公开 `input_owner` 和一次返回的私密
`execution_lease={id,generation,token}`。`generation` 是正整数。
`Client.claim_board()` 将证明只存在当前 Python 对象内，返回值已去掉证明；
后续写请求自动携带证明。不要把 `client.execution_lease` 直接序列化到业务日志。

```python
public_claim = client.claim_board(ttl=300)
public_claim = client.renew_board(ttl=300)  # 长操作期间在到期前续期
ownership = client.input_owner()          # 只读，不返回 token
```

公开查询为 `GET /api/input-owner?key=…`，保留 `input_owner`、
`execution_scope:"collector"`、`cross_platform_exclusive:false`。
续期走 `POST /api/input-renew`，携带 `execution_lease` 与 `ttl`；
释放走 `POST /api/input-release`，携带 `scope:"board"` 与同一证明。
这些后续租约请求按板号和证明校验，不携带已过时的 UART 连接代次。
活动任务尚未结束时不能释放；到期、撤销或进程重启留下的占用进入 `recovery`，不会自动放开。
只有独立确认原执行已停止后，才调用
`client.release_board(process_stopped_confirmed=True, reason="停止核查记录")`。
证明已丢失时由主访问身份按公开占用代次执行人工恢复，不能从只读查询恢复 token。

**claim 不支持 `request_id` 去重。** claim 回执丢失会返回
`code=lease-unconfirmed,outcome=unknown`。先查 `input_owner`，保留现场并由主访问身份恢复；
不能自动再次 claim。续期或释放回执丢失也不自动重发；客户端保留原证明供后续核查。
一个长期运行的平台 executor 可在内存中持有证明并续期；示例不建立第二个租约数据库。

## 一个业务尝试对应一个稳定请求 ID

原平台在发送前保存 8–100 字符的 `request_id`（字母、数字、下划线、连字符），
例如由 `jobId/attemptId/stepId` 的稳定摘要产生。新业务尝试才用新 ID。
同 actor、同 ID、同规范输入返回原任务；同 ID 改输入返回 HTTP 409。
请求 ID 不是设备租约，也不能扩大令牌权限。

```python
from decklabctl import SubmissionUnconfirmed, TaskReceipt

# saved_request_id 已先写入原平台任务；这里不创建第二套业务任务。
try:
    result = client.submit_task("hardware-reset", request_id=saved_request_id)
except SubmissionUnconfirmed as error:
    # error.request_id / error.outcome == "unknown"，不释放占用、不再POST。
    result = client.task_by_request(saved_request_id)  # 恢复连通后只查询原操作
receipt = TaskReceipt.from_response(result, saved_request_id)
```

`submit_task()` 先协商，再只 POST 一次。传输错误、HTTP 408 或 5xx 后只执行
`GET /api/task?request_id=…`，不重发提交。查询仍失败或未找到时抛出
`SubmissionUnconfirmed` 并保留原 ID；404 只说明当前范围未查到，不证明板端没有执行。
任务 ID 已知时也可 `GET /api/task?id=…`。查询、取消和重新提交是不同动作。
`Client.submit_task` 未传 ID 时为本次调用生成一次 ID；平台集成应显式传入事先保存的 ID。

CLI 在调用方 Linux／Windows 电脑运行：

```sh
decklabctl --device BOARD_SERIAL task-start identify --request-id platform-attempt-001
decklabctl task-request platform-attempt-001
```

文件暂存、刷机预览和执行继续使用已有 API。提交整包／分区刷写前先生成固定预览，
传 `plan_id`、`binding_revision`、稳定 `request_id`；布局变化时重新预览，
不能复用旧 ID 改写输入。不要把任意 shell 命令塞进硬件任务，也不把退出 0 当内容验证。

## 结果、取消和撤销分别读取

任务顶层 `state` 描述 queued/running/completed/failed/cancelled/interrupted；
`outcome` 描述物理证据，二者独立：

| outcome | 调用方可以采用的结论 |
|---|---|
| `not-issued` | 尚无操作已发出的证据 |
| `issued` | 已发出，尚无确定确认；不能直接重放 |
| `acknowledged` | 请求／命令获得确认，不等于板卡启动或内容验证 |
| `observed` | 已观察到目标状态；继续检查业务所需证据 |
| `verified` | 执行器给出了其能力范围内的验证证据；仍不是整个测试计划通过 |
| `unknown` | 最终物理结果未知；观察原操作，保留恢复占用 |

`TaskReceipt` 将结果解析为 `TaskOutcome` 枚举；未知枚举值按 `UNKNOWN` 处理。
读取原始 `task.result`、刷写 write/verify/boot 分项和事件／日志，不只读取 `state`。
任务 `completed` 不自动变成 `verified`；取消请求成功也不证明原进程已经停止。
调用 `POST /api/task-cancel` 后继续查询原任务；不在 HTTP 超时的异常清理里自动复位或释放。

去重范围是能力清单列出的后台任务。原始终端输入、尺寸调整及现有 `execute_command` 交互辅助
不提供持久请求去重；它们仍遵守执行占用。命令辅助返回 `sent/completed/exit_code/may_still_be_running`，
不确定时保留原会话与输出游标，不能把任意 shell 命令当作可重试 RPC。edge 接入这类操作时
须沿用自身命令 ID 与输出关联，不能在传输层自动重发。

主访问身份可显式调用 `Client.revoke_grant(id)`，复用 `POST /api/agent-revoke`。
返回的 `ok:true` 表示本地已持久撤销并阻止新请求；
`sessions_closed:false,revocation_pending:true` 表示后端停止未确认，继续保留占用。
只有匹配 `revocation_generation` 的后端确认才得到 `sessions_closed:true`。
重复撤销同一授权 ID 复用同一代次；控制服务持久重试对账。
`GET /api/agent-grants` 的 `revocation_generation/backend_confirmed_generation` 可用于观察。
撤销不回滚已经发生的板卡改动。

## 示例和隔离合同测试

[`platform_executor.py`](../../examples/platform_executor.py) 复用 Python 客户端：

```sh
# 在调用方电脑执行。inspect/query均只读；先填写经过核实的映射。
python3 examples/platform_executor.py --mapping examples/platform-device.json inspect
python3 examples/platform_executor.py query --request-id platform-attempt-001

# 仅在原平台已授权该设备动作时执行；run 会申请占用并实际提交。
python3 examples/platform_executor.py --mapping examples/platform-device.json run \
  --kind hardware-reset --request-id platform-attempt-001
```

示例只在明确终态且物理结果不是 issued/unknown 时尝试释放；等待超时或网络异常不释放。
它只展示一次尝试，不替代原平台 executor 的常驻续期、故障观察和测试断言。
[`edge_task_lookup.go`](../../examples/edge_task_lookup.go) 使用 Go 标准 `net/http`，只做能力协商和
原请求 GET 回查，便于 edge 在丢回执后恢复观测，不引入新 SDK、MQTT 或调度器。
edge 的 `execute_node`、PC 用例和主机准备仍留在原 edge 宿主；首批委托板端命令、
串口、刷机和电源，不能把整个 CommandRunner 无差别转到另一台主机。

在开发者 Linux 电脑或隔离 CI 运行：

```sh
python3 -m unittest discover -s tests -p 'test_integration_client.py'
python3 -m unittest discover -s tests -p 'test_cli.py'
go test examples/edge_task_lookup.go
```

客户端合同测试通过内存 HTTP 桥调用真实 Handler 与临时 SQLite，只替换设备执行适配器；
不监听端口、不启动串口扫描、不接触板卡。覆盖丢提交回执、按原 ID 回查、输入冲突、
连接代次变化、占用证明、未知结果和撤销待确认回执。它不等于 具体平台正式接入或真板验收。

## 团队用例与测试工位

DeckLab 可接管 TitanX 工位的板卡管理，开发者可在设备页看现场、查日志和执行获授权的操作。设备页的「用例库」还可以派发已有 TitanX 团队用例。切换步骤、工位映射、结果回查和上位机 Agent 的桌面调用见 [TitanX 接入指南](titanx-integration.md)。
