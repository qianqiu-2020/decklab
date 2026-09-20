# Agent 接入开发板

## 入口

使用 [seriald-board skill](../skills/seriald-board/SKILL.md)，或直接调用 `serialctl`。
源码环境用 `python3 /data/seriald/serialctl.py` 替代命令名。客户端只需 Python 3 标准库；
远程机器复制客户端即可，使用 `SERIALD_URL` 指向上位机。

```sh
export SERIALD_URL=http://127.0.0.1:8898
export SERIALD_TOKEN=服务端配置的访问令牌   # 服务未启用认证时省略
serialctl health
serialctl devices
serialctl --actor claude-code/diagnosis --device riscv-118 read
serialctl --actor claude-code/diagnosis --device riscv-118 exec --command 'uname -a'
```

默认 URL 是本机 `http://127.0.0.1:8898`。现有开发主机可显式设置
`SERIALD_URL=http://10.0.90.177:8898`。全局选项置于子命令之前。

## key 是标签，serial 是永久身份

`devices` 包含在线、历史及待识别设备。`key` 等于当前唯一标签，如 `riscv-118`、`musepi2`；
`serial` 是当前正式或候选身份，查看 `info.identity_status` 区分 `confirmed` 与 `provisional`。
`--device` 接受标签或身份编号，不按 ttyUSB 编号或重复 hostname 选板。
CLI 会固定选中时的 serial，后续请求若标签已换绑，会拒绝操作。

未写永久序列号的 key 为 `unprovisioned-…`，摘要来自当前 USB by-path。确认目标连接后运行：

```sh
serialctl --device unprovisioned-实际摘要 identify
serialctl --device 设备当前标签 rename --label riscv-118
```

识别可能自动登录并在串口执行识别命令。已知序列号恢复原标签；标签变更不移动日志。
服务优先读取 EEPROM ONIE `0x23`：支持 upstream 的 `cells/serial-number@*`，也能校验解析
vendor 内核暴露的原始 at24 NVMEM。已登记厂商契约的 U-Boot EEPROM 注入编号也可自动确认；
没有 EEPROM 编号时才尝试未知 DT、SoC/eFuse 或旧 `/boot` 标记；
检查 `serial_source`、`identity_status` 和 `reported_serial`。候选身份日志位于
`logs/_provisional/<serial_source>/<serial>/`，正式确认后由管理员迁入 `logs/<serial>/`。
用户选择软件身份兜底后，可运行 `identify --init-boot-marker` 创建一次；后续普通识别只读它。
标记随系统文件保留，克隆到另一块板前移除标记；不按 USB 插口猜身份。详见 [身份说明](HARDWARE_ID.md)。
未 provision 设备换插口后临时 key 会变化，同一插口的每次连接日志仍分开保存。写入 EEPROM 编号后，
换插口、重连及 vendor/upstream 内核切换均使用相同正式身份。

## 输出和历史

```sh
serialctl --device riscv-118 read
serialctl --device riscv-118 read --off 12345 --tail 0
serialctl --device riscv-118 activity
serialctl --device riscv-118 logs
serialctl --device riscv-118 log --file serial-20260919.log
```

`read` 不发送回车，默认返回缓存最后 16 KiB，可能含旧日志。`off` 是字节偏移，
下次传入可取增量；空轮询最多等待约 15 秒。`--tail 0` 返回所有可用缓存，
`gap` 表示服务已丢弃部分缓存，`truncated_bytes` 表示客户端主动省略的字节。
`data_b64` 保留原始字节，跨块中文应使用流式解码。

`logs` 列出序列号目录中的文件；`log` 默认读取文件末尾 64 KiB，传 `--off 0` 可从头分页，
响应的 `off`、`more` 用于继续读取。网页提供相同的在线查看，不默认下载。

## 执行与操作归属

`--actor` 或 `SERIALD_ACTOR` 指定操作者，例如 `claude-code/session-42`。默认 `agent`。
这是自报名称，不是认证凭据。网页操作记录显示操作者、命令、会话状态、退出码及日志入口。
人在网页中旁观 agent SSH 时默认只读；点击“介入此会话”后可以向同一 shell 输入，系统会记录
`session-takeover` 事件。agent 应把这种输入视为人工接管，不要继续并发发送破坏性命令。

每次 `exec` 创建独立 SSH 会话，在 POSIX `sh` 子进程中执行，最后关闭自己的会话。
工作目录和环境不跨调用保持；将 `cd` 和依赖的命令放在一次 `--command` 内。
stdin 为 EOF，stdout/stderr 合并，返回文本去除 ANSI；命令限 2048 UTF-8 字节，输出限 1 MiB，
高吞吐输出也可能超出服务缓存。使用过滤和取尾限制日志量，不通过它运行交互程序或常驻后台任务。

响应 `completed: true` 表示收到了完整结束标记，`exit_code` 为远端退出码；
`session_closed` 表示会话清理成功。CLI 成功退出 0，访问/命令失败退出 1，参数错误退出 2。
非零退出码、输出缺失、超时不能报告为成功。

`--timeout` 默认 60 秒，从会话创建完成后计时，包含登录和执行；创建请求单独受
`--http-timeout` 控制（默认 90 秒）。缺少 IP 时服务可能先在串口识别；不会自动安装 sshd。
`may_still_be_running: true` 时先检查现场，不盲目重复写操作，关闭 SSH 不能保证后台进程终止。
`recording_error` 表示结果未成功写入历史，`cleanup_error` 表示关闭失败；保留 sid 以便恢复后处理。

## 交互式会话

```sh
serialctl --device riscv-118 ssh-open
serialctl --device riscv-118 read --sid 返回的sid
serialctl --device riscv-118 send --sid 返回的sid --text pwd --enter
serialctl --device riscv-118 ssh-close --sid 返回的sid
```

只使用和关闭自己的 sid。网页旁观 agent SSH 默认只读，已关闭会话可查看持久化日志。
没有 `--sid` 时 `send` 使用共享串口，需协调唯一操作者；先确认是 shell、登录提示符还是 U-Boot。
`send` 成功只证明输入已写入；`--enter` 追加 CR；`--file 文件` 或 `--file -` 传入其他换行与控制字符。

不要绕过服务运行 `cat /dev/ttyUSB*`、`stty`、`screen` 或 `minicom`，不要启动第二个采集器。
多个 SSH 会话共享板子的文件和系统状态，修改同一资源仍需协调。
