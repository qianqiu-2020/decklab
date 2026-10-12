# Agent 接入开发板

## 入口

使用 [seriald-board skill](../skills/seriald-board/SKILL.md)，或直接调用 `serialctl`。
源码环境用 `python3 <源码目录>/serialctl.py` 替代命令名。客户端只需 Python 3 标准库；
远程机器复制客户端即可，使用 `SERIALD_URL` 指向上位机。

网页选中设备后点击“分享给 Agent”，或在首页点击“Agent 接入”分享全部设备。
页面生成可复制的接入文本，包含服务地址、独立令牌和自托管下载命令：
`<服务地址>/serialctl.py` 与 `<服务地址>/skills/seriald-board/SKILL.md`。
“连接位置”可切换本机 `127.0.0.1` 与探测到的局域网地址；若多网卡选择不合适，
可直接修改地址输入框。另一台电脑上的 Agent 应使用局域网地址。
Skill 下载响应明确使用 UTF-8，以便中文指引正确显示。
令牌默认不限期，网页授权列表可随时撤销；单设备令牌绑定序列号，改标签后仍有效。
撤销会拒绝后续 API 请求并关闭该令牌建立的 SSH 会话。开放模式下，其他客户端仍可不带令牌访问，
因此单设备范围只约束使用该令牌的请求。Agent 应优先用 SSH `exec`，启动、内核及 SSH 故障再用串口 `read`。

```sh
export SERIALD_URL=http://127.0.0.1:8898
export SERIALD_TOKEN=服务端配置的访问令牌   # 服务未启用认证时省略
serialctl health
serialctl devices
serialctl capabilities
serialctl --actor claude-code/diagnosis --device riscv-118 read
serialctl --actor claude-code/diagnosis --device riscv-118 exec --command 'uname -a'
```

默认 URL 是本机 `http://127.0.0.1:8898`。现有开发主机可显式设置
`SERIALD_URL=http://上位机局域网地址:8898`。全局选项置于子命令之前。

`capabilities` 不探测开发板，返回 API 版本与支持的功能。串口文件传输仍需板端 Linux shell；
SSH 自动配置是否允许取决于设备 profile，不应根据全局能力查询假定某个板子可执行这些操作。
控制台操作卡住时，可在另一个会话查看并取消：

```sh
serialctl --device riscv-118 operation
serialctl --device riscv-118 operation-cancel 操作ID
```

取消按操作 ID 定位，只允许该操作的 agent 或人工介入；已发出的板端命令不能撤销。
取消上传后还须通过传输流程的 abort 释放预留。详见 [串口事务](CONSOLE_TRANSACTIONS.md)。

需要连续操作而不让其他 agent 插入串口输入时，可用同一 `--actor` 执行 `input-claim`，
完成后用返回的占用 ID 执行 `input-release`。默认输入仍共享；占用不影响输出读取或独立 SSH。
网页可以人工接管，撤销 agent 令牌也会释放占用。不同 agent 工作流应使用不同 actor；
网页仍使用人工 actor，但输入占用另外区分网页客户端；另一页需显式接管后才能写入。
占用、释放和接管状态通过串口实时流同步，板子没有输出也会更新；轮询作为回退。
网页“分享设备”只定位默认打开的设备，网页授权仍覆盖全部设备。
“分享给 Agent”可以选择单设备或全部设备，单设备权限按序列号固定。

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
没有 EEPROM 编号时优先使用有效 DT 编号，再尝试 SoC/eFuse 或旧 `/boot` 标记；
已登记的软件标记兜底会保留其候选身份，直到 EEPROM 确认或管理员明确更正；
检查 `serial_source`、`identity_status` 和 `reported_serial`。候选身份日志位于
`logs/_provisional/<serial_source>/<serial>/`。同一候选编号经 EEPROM 证实后，系统自动迁入
`logs/<serial>/`；编号发生变化或需要人工确认时，使用管理员更正/确认工具迁移。
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

`exec` 按 `--actor` 和设备复用持久 SSH 终端，会话会在网页中持续显示并可被人旁观；
需要结束时显式运行 `ssh-close --sid <sid>`。每次 `--command` 在独立的 `sh -c` 子 shell
执行：继承父终端的目录和已导出变量，但命令中的 `cd`、`export` 不会改变下一次调用。
将有依赖的命令放在一次 `--command` 内。需要持续改变交互环境时使用
`ssh-open` 与 `send --sid`。网页 `web` 每次创建独立会话。
stdin 为 EOF，stdout/stderr 合并，返回文本去除 ANSI；命令限 2048 UTF-8 字节，输出限 1 MiB，
高吞吐输出也可能超出服务缓存。使用过滤和取尾限制日志量，不通过它运行交互程序或常驻后台任务。

响应 `completed: true` 表示收到了完整结束标记，`exit_code` 为远端退出码；
`session_persistent` 表示服务是否保留了这个 actor 的 SSH 会话；持久会话不会返回
`session_closed: true`，应使用 `ssh-close` 关闭。CLI 成功退出 0，访问/命令失败退出 1，参数错误退出 2。
非零退出码、输出缺失、超时不能报告为成功。
设备 profile 允许自动配置且使用 profile 中的 root 密码认证被拒绝时，已登录且可免密码 sudo 的串口 shell 会自动配置该 root 密码
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
写入串口日志，传敏感文件前应考虑日志权限与保留策略。浏览器也提供“文件传输”面板；网页下载由上位机校验完成后提供流式链接，不再有浏览器 128 MiB 限制。
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


## 按启动读取与搜索

```sh
serialctl --device deb1 boot-log --latest
serialctl --device deb1 logs
serialctl --device deb1 boot-log --id boot-<ID>
serialctl --device deb1 boot-search --query 'panic'
```

`boot-log` 读取该启动已采集的完整内容，包括跨日的各段日志。`--latest` 从列表取最新启动 ID；固定一次故障现场时保存 ID，之后用 `--id`，防止重启后切换到另一轮。

`boot-search` 只搜索所选设备的全部启动归档，支持中文、不区分大小写，按最新启动优先返回匹配行。每页 20 条；`more=true` 时用 `--offset <next_offset>` 继续搜索。分页限制响应大小，不限制历史搜索范围。

对应 API：`GET /api/boot-search?key=<标签>&q=<关键词>&offset=0`、`GET /api/boot-log?key=<标签>&id=<启动ID>`。单设备分享令牌继续由服务端绑定序列号。开始采集之前的输出无法补回；无启动标记时不会猜测启动次数。

通过有效网页分享链接进入的同事也可以在“Agent 分享”中创建、查看和撤销 Agent 令牌；设备页默认选择当前设备。Agent 令牌自身不能创建或撤销其他令牌。网页设备链接按序列号定位，但网页分享的访问授权仍覆盖整个平台。

## 后台文件传输与结果取回

```sh
serialctl --device riscv-118 file-put --local ./firmware.bin --remote /tmp/firmware.bin --background
serialctl --device riscv-118 file-get --remote /var/log/dmesg --background
serialctl --device riscv-118 task <返回的任务ID>
serialctl --device riscv-118 file-fetch <下载任务ID> --local ./dmesg.txt
```

提交后可结束 agent 命令进程；采集器继续执行。上传暂存阶段仍需客户端在线。
下载完成后按任务 ID 取回，CLI 流式校验大小及 SHA-256，再提交本地文件；默认拒绝覆盖，
必要时显式加 `--overwrite`。不要重复提交响应丢失的任务，先查询任务历史。

不带 `--background` 时也由采集器执行同一任务，CLI 等待结果，下载随后自动取回本地。
Ctrl-C 请求取消并报告任务 ID；若查询断网，任务不会被自动重发或取消，可稍后按 ID 查询。

## 配置的硬件操作

`serialctl --device <标签> hardware` 只列出管理员为该板配置的电源、复位和刷机操作，
不会执行工具。授权范围内可用 `task-start hardware-reset` 等发起后台任务；
查状态、取消及重连恢复均使用同一任务 ID。操作默认未配置，agent 能否执行由每项
`allow_agent` 决定。需要独占串口的工具还须设置 `exclusive_serial`；期间只有该串口
停止采集，日志记录这个空档。工具清理失败时保持暂停，需确认工具已退出后再重启采集器。
见 [硬件扩展](HARDWARE_EXTENSIONS.md) 和 [HTTP 接口](API.md)。
