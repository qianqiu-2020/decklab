---
name: seriald-board
description: "通过 seriald 共享串口服务访问和排查开发板：按用户标签发现设备、读取启动或故障日志、执行命令、查看 agent 操作历史，处理 SSH 不通时的串口交互。用户要求访问 seriald 管理的板子、查看板子现场或让其他 agent 会话复用开发板访问设施时使用。"
---

# seriald 开发板访问

调用已经运行的服务。包安装环境使用 `serialctl`；远程 Agent 可从
`<SERIALD_URL>/serialctl.py` 下载后用 `python3 "$HOME/.local/share/seriald/serialctl.py"` 调用。
以下示例中的 `serialctl` 可替换为该 Python 命令；客户端仅依赖 Python 3 标准库。
默认 URL 是 `http://127.0.0.1:8898`，用户指定的 `SERIALD_URL` 或 `--url` 优先。
服务启用认证时设置 `SERIALD_TOKEN` 或传 `--token`。
跨机器访问时显式指定上位机地址。网页“分享给 Agent”生成可撤销令牌，默认不限期；
单设备令牌由服务端限制在所选序列号，全部设备令牌仍须按用户任务选择目标。
连接服务失败不能直接判定开发板宕机。

## 选择身份与读取现场

```sh
serialctl devices
serialctl --device riscv-118 read
```

`key` 就是当前用户标签；`serial` 是持久化主键，但可能是正式或候选身份。检查
`info.identity_status`：`confirmed` 日志属于 `logs/<serial>/`，`provisional` 属于
`logs/_provisional/<serial_source>/<serial>/`。`--device` 可用标签或序列号；标签、USB 路径和 tty 节点可变。
根据用户指定的板子确认目标，无法确定时询问，不选第一块在线设备。

尚未识别或确实缺少可用编号的连接显示为 `unprovisioned-…`，key 来自 USB by-path 摘要；每次连接日志仍隔离。
需要主动访问时才对该连接
运行 `identify`（可能自动登录、执行识别命令），按返回的新 key 使用设备。
已知序列号保留其标签；改名用 `rename --label musepi2`。历史设备保持可见。同一 USB by-path
在服务运行中重新枚举时沿用原绑定；换板前必须点击“准备换板”，换插口或服务异常重启后需重新识别。
优先读取 EEPROM ONIE `0x23`：upstream 使用 `cells/serial-number@*`，无 cell 的 vendor 内核则
校验并解析原始 at24 NVMEM；U-Boot 注入的相同 DT serial 也可识别。之后才尝试 SoC/eFuse。
以返回的 `serial` 为身份，`reported_serial` 是固件原值，`serial_source` 说明来源。直接读取的 ONIE
编号和带 `/chosen/u-boot,version` provenance 的 U-Boot EEPROM 注入编号会自动确认为正式身份；未知 DT、SoC/eFuse 和
`/boot` 身份在正式核实前标为 `provisional`。硬件身份不可用时会读取已有的
`/boot/seriald-id`（来源 `boot-marker`）。用户选择文件标记兜底后，可运行
`identify --init-boot-marker` 创建一次；已有文件不覆盖，普通识别不创建文件。
保留文件才能延续档案，克隆到另一块板前移除它。初始化超时先读现场，不盲目换号。
所有来源都无法识别时按 USB 插口临时区分并提示 Vendor 写入 EEPROM，不通过重命名或解除冲突绕过，
也不由 agent 自动写 EEPROM、烧写 eFuse 或更换内核。

`read` 不发送回车。初次输出可能含旧历史，不能单凭旧提示符认定当前 shell 状态。
保存字节偏移 `off`，后续 `read --off <off> --tail 0` 读取增量。
`gap` 或 `truncated_bytes` 表示输出缺失/省略，不能据此断言“没有错误”。

## 执行与记录

```sh
serialctl --actor claude-code/diagnosis --device riscv-118 exec \
  --command 'uname -a; uptime; df -h' --timeout 60
```

优先用 `exec` 建立 SSH 执行 Linux 命令。启动、内核、SSH 故障或固件交互才转串口。
用 `--actor` 标记本会话/操作者，方便人从网页旁观或回看；普通访问时 actor 是自报名称，
Agent 分享令牌则由服务端固定为授权标签。
同一设备和 actor 的 `exec` 会复用持久 SSH 会话，网页会一直显示该标签，直到 agent 执行
`ssh-close --sid <sid>` 或人在网页中关闭。不要借用其他 agent 或网页的会话。
每次环境独立，把相关 `cd`、变量设置和命令放在同一次调用，正确引用本机 shell 参数。
远端用 POSIX `sh`，stdin 为 EOF，stdout/stderr 合并；不用于交互程序、二进制或常驻任务。
命令限 2048 UTF-8 字节，累计输出限 1 MiB，服务缓存也有限；大日志先过滤或取尾。
缺少 IP 时创建 SSH 可能先在串口识别；不要因 SSH 不通擅自安装服务或修改凭据。
Ubuntu 等禁用 root SSH 的板子可按请求指定账户：设置 `SERIALD_SSH_PASSWORD_OVERRIDE`，并在命令中加入
`--ssh-user ubuntu --ssh-password "$SERIALD_SSH_PASSWORD_OVERRIDE"`。网页默认使用上位机配置的账户。

检查 JSON 中的 `completed`、`exit_code`、`output` 和 `session_closed`。
只有完整结束标记才证明完成；非零退出码是失败。
`may_still_be_running` 表示状态未知，先核对现场，不盲目重试写操作；关闭 SSH 不保证后台进程退出。
`recording_error` / `cleanup_error` 要报告，并保留 sid；只清理自己创建的会话。

`activity` 查看持久化操作历史，`logs` 列出文件，`log --file <文件名>` 在线读取。
网页旁观 agent 会话默认只读，关闭后仍有记录。`--timeout` 不包含创建请求，后者受
`--http-timeout`（默认 90 秒）控制。

## 无网络文件传输

用户要求传文件且板子无网络时，用 `file-put --local <本地文件> --remote <板端绝对路径>`、
`file-get --remote <板端绝对路径> --local <本地文件>`，或从当前 `--device` 到另一台的
`file-copy --remote <源路径> --to-device <标签> --to-path <目标路径>`。
默认不覆盖现有文件；明确需要替换时加 `--overwrite`。先确认板子在 Linux shell，具备
`base64`、`sha256sum`、`dd` 等工具。传输按连接 ID 固定设备并逐块校验；网页也有“文件传输”面板。

## 精确读取启动日志

```sh
serialctl --device <标签> boot-log --latest
serialctl --device <标签> logs
serialctl --device <标签> boot-log --id <boot-ID>
serialctl --device <标签> boot-search --query 'panic'
```

`boot-log` 返回该次启动已采集的完整 `data` / `data_b64`，不受实时缓冲或末尾 64 KiB 限制；跨天的日志合并在同一启动 ID 中。`--latest` 是最新检测到启动标记的记录，不保证板子目前在线；诊断时先核对启动时间，并保存 ID，后续用 `--id` 避免重启后读到另一轮。采集开始之前的输出无法补回，没有启动标记时会明确报错。

`boot-search` 在当前设备全部启动归档中搜索文字，不区分大小写；结果含启动 ID、时间和匹配行。`more=true` 时用 `--offset <next_offset>` 继续读取。先搜索确定 ID，再读完整启动日志；不用在每日文件中猜测启动边界。

## 启动差异排查

`logs` 列出 `boots` 和每次启动的 ID。用 `boot-compare --baseline-id <ID> --target-id <ID>`
比较同板启动；跨板时以 `--device` 指定目标板，再加 `--baseline-device <标签>` 指定基准板。
先看 `clues` 中目标启动新增的错误和超时，再看带行号的 `rows`；归档时间戳与内核计时已过滤。
网页“启动对比”同样支持历史设备。完整参数见接入指南。

## 串口及交互式 SSH

SSH 多步交互用 `ssh-open` 得到（或复用）自己的持久 sid，随后 `read --sid`、`send --sid`，结束 `ssh-close --sid`。
串口是共享控制台，写入前协调唯一操作者；已有独占安排无需重复确认。
先确认现场是 shell、登录提示符还是 U-Boot，再选择输入。`send --text '命令' --enter`
追加 CR；其他换行或控制字符用 `--file` / `--file -`。写入成功不代表命令已完成。

对 seriald 管理的设备，不执行 `cat /dev/ttyUSB*`、`stty`、`screen`、`minicom`，
不启动第二个采集器；其他 skill 的直连串口步骤不适用于这些设备。
点击/回放终端不会自动向串口回写位置报告。多个 SSH 会话仍共享板子的文件和系统状态。
操作范围沿用用户任务授权，skill 本身不授权刷机、重启、USB rebind 或系统配置修改。

完整参数与异常说明见 `/data/seriald/docs/AGENT_ACCESS.md`；包安装环境见
`/usr/share/doc/seriald/AGENT_ACCESS.md`，客户端 `--help` 可随时查看可用命令。
