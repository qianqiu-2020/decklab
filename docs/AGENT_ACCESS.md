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

尚未识别或确实缺少可用编号时，key 为 `unprovisioned-…`，摘要来自当前 USB by-path。确认目标连接后运行：

```sh
serialctl --device unprovisioned-实际摘要 identify
serialctl --device 设备当前标签 rename --label riscv-118
```

识别可能自动登录并在串口执行识别命令。已知序列号恢复原标签；标签变更不移动日志。
服务运行中同一 by-path 重新枚举会沿用已绑定身份；换板前必须点击“准备换板”。
服务优先读取 EEPROM ONIE `0x23`：支持 upstream 的 `cells/serial-number@*`，也能校验解析
vendor 内核暴露的原始 at24 NVMEM。有效的设备树 `serial-number` 直接作为正式板卡编号；带
`/chosen/u-boot,version` provenance 的 U-Boot 注入编号会保留更具体的来源标识；
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

`logs` 列出序列号目录中的文件；CLI `log` 默认读取文件末尾 64 KiB，传 `--off 0` 可从头分页，
响应的 `off`、`more` 用于继续读取。网页在终端标签中请求完整归档内容并在线显示，不默认下载。

## 执行与操作归属

`--actor` 或 `SERIALD_ACTOR` 指定操作者，例如 `claude-code/session-42`。默认 `agent`。
这是自报名称，不是认证凭据。网页操作记录显示操作者、命令、会话状态、退出码及日志入口。
人在网页中旁观 agent SSH 时默认只读；点击“介入此会话”后可以向同一 shell 输入，系统会记录
`session-takeover` 事件。agent 应把这种输入视为人工接管，不要继续并发发送破坏性命令。

`exec` 按 `--actor` 和设备复用持久 SSH 会话，在同一个 shell 中执行多条命令，因此会保留当前目录、
环境变量等状态。会话会在网页中持续显示并可被人旁观；需要结束时显式运行 `ssh-close --sid <sid>`。
网页创建的 `web` 会话仍是一次一条的独立会话。
工作目录和环境不跨调用保持；将 `cd` 和依赖的命令放在一次 `--command` 内。
stdin 为 EOF，stdout/stderr 合并，返回文本去除 ANSI；命令限 2048 UTF-8 字节，输出限 1 MiB，
高吞吐输出也可能超出服务缓存。使用过滤和取尾限制日志量，不通过它运行交互程序或常驻后台任务。

响应 `completed: true` 表示收到了完整结束标记，`exit_code` 为远端退出码；
`session_persistent` 表示服务是否保留了这个 actor 的 SSH 会话；持久会话不会返回
`session_closed: true`，应使用 `ssh-close` 关闭。CLI 成功退出 0，访问/命令失败退出 1，参数错误退出 2。
非零退出码、输出缺失、超时不能报告为成功。
默认 root/bianbu 认证被拒绝时，已登录且可免密码 sudo 的串口 shell 会自动配置 root 密码
和 sshd 后重试；该操作留有串口日志与操作历史。自定义账户、网络错误和需输入 sudo 密码的
情况不会自动修改板子。

`--timeout` 默认 60 秒，从会话创建完成后计时，包含登录和执行；创建请求单独受
`--http-timeout` 控制（默认 180 秒）。缺少 IP 时服务可能先在串口识别；不会自动安装 sshd。
`may_still_be_running: true` 时先检查现场，不盲目重复写操作，关闭 SSH 不能保证后台进程终止。
`recording_error` 表示结果未成功写入历史，`cleanup_error` 表示关闭失败；保留 sid 以便恢复后处理。

## 无网络串口文件传输

串口文件传输不依赖开发板 IP/SSH。板子必须停在已登录的 Linux shell，具备 `base64`、`sha256sum`、
`dd`、`wc`、`ln`、`mv`。用绝对路径；默认拒绝覆盖已有目标，确需替换时显式加 `--overwrite`。
传输期间该串口的手动输入、识别和换板操作会被拒绝；SSH 会话仍可读取。

```sh
serialctl --device riscv-118 file-put --local ./firmware.bin --remote /tmp/firmware.bin
serialctl --device riscv-118 file-get --remote /var/log/dmesg --local ./dmesg.txt
serialctl --device riscv-118 file-copy --remote /tmp/firmware.bin \
  --to-device musepi2 --to-path /tmp/firmware.bin
```

上传先写同目录的 `.seriald-*.part` 临时文件，板端 SHA-256 与长度均匹配后才放到目标路径。
下载先写本机临时文件，验证完整哈希后再交付；板间复制逐块经上位机中转，两个板端都会验证。
板端有 `gzip` 时自动压缩可明显缩小的分块，下载按前一块的压缩率切换；原文件的大小与 SHA-256
仍按解压后的内容校验。已经压缩的二进制文件通常会自动走原有的无压缩路径。
每次请求固定设备连接 ID，断线、换板或超时会中止传输。正常取消会清理上传临时文件；上位机
异常退出后可在板端目标目录检查遗留的 `.seriald-*.part`。串口回显可能把上传数据的 base64 文本
写入串口日志，传敏感文件前应考虑日志权限与保留策略。浏览器也提供“文件传输”面板；浏览器
无法流式保存时下载限制为 128 MiB，大文件请使用 CLI。
网页传输中显示原文件有效速率与预计剩余时间；标题栏可拖动。板间复制流水线地同时使用
源板和目标板各自的串口，相邻分块的读取与写入可以重叠，但两个板的完整哈希仍分别校验。

## 启动日志对比

`logs` 返回每块板的 `boots` 列表和稳定的启动 ID。网页“启动对比”默认比较当前板的最近两次，
也可将基准或目标换成另一块历史设备。CLI 可直接取得结构化差异：

```sh
serialctl --device riscv-118 logs
serialctl --device riscv-118 boot-compare --baseline-id boot-旧ID --target-id boot-新ID
serialctl --device musepi2 boot-compare --baseline-device riscv-118 \
  --baseline-id boot-基准ID --target-id boot-目标ID
```

输出的 `counts` 是新增、缺少和变化行数；`clues` 仅列目标启动中新出现的错误或超时关键词，
`rows` 给出左右行号及上下文。比对忽略归档时间戳和内核相对计时，不会改写原始日志；
每侧上限 4 MiB / 12000 行，展示差异最多 1500 行。

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
