# 架构与数据模型

## 身份与连接

- **serial**：当前正式身份编号，也是 SQLite 主键。EEPROM ONIE `0x23`、有效 DT `serial-number`、U-Boot 注入编号和人工核实更正均可作为正式身份；SoC/eFuse 和 `/boot` 标记仍是候选身份。
- **label / key**：当前唯一标签，例如 `riscv-118`、`musepi2`，用户操作入口。改名不改序列号或日志目录。
- **by_path / dev**：本次已确认连接的 USB 物理路径和 tty 节点。离线时当前字段为空；`last_path`、`last_dev` 保留历史。
- **未识别连接**：使用基于 USB by-path 的 `unprovisioned-…` 临时 key；每次连接单独写入
  `logs/_unprovisioned/<path-hash>/<connection-id>/`。换插口会改变临时 key，正式身份不依赖插口。

识别操作读取开发板序列号，再绑定到当前连接。已有序列号保留用户标签，更新位置；新序列号初始标签取 hostname（冲突时分配唯一临时标签），随后可改名。缺少序列号、两处在线报告同一序列号或标签冲突都会拒绝合并。

部分固件的 device-tree 可能填写相同出厂占位值（例如 `0123456789ABCDEF`）。这不能证明是同一块板；冲突连接保留在待识别列表并显示原因，日志继续暂存。已确认的冲突保存在注册表中，拔掉另一块板或重启服务也不会允许误合并。必须核实真实的唯一板载身份，不能用 tty、标签或操作系统 machine-id 静默替代。

识别优先读取 ONIE cell，没有 cell 时校验并解析原始 NVMEM，以覆盖未启用 ONIE layout 的 vendor 内核。
遇到无效固件编号时使用经过验证的 SoC 编号，再以旧 `/boot/seriald-id` 软件标记兜底，
记录 `serial_source` 和固件的 `reported_serial`。普通探测只读标记；创建逻辑封装在 `boot_marker.py`。
读取规则、K1 驱动依据和旧编号更正步骤见 [硬件身份说明](HARDWARE_ID.md)。

collector 干净重启时写入一次性短期 handoff；仅在同一 host boot、两分钟内且 by-path、tty 节点和
内核设备身份完全一致时接续已有绑定，不向板子发送回车、密码或命令。运行中的 USB 串口短暂断开并
重新枚举时，只要 by-path 相同且未点击“准备换板”，沿用该插口已绑定的板卡身份和日志目录；ttyUSB
编号、内核设备 inode 可变化。异常退出或主机重启后若没有有效 handoff，仍须重新识别。

同一个 USB-UART 后面更换开发板时，内核无法凭 USB 路径感知板卡已变化。操作者必须先点击
“准备换板”：服务立即把旧身份置为离线、关闭其 SSH 会话并让该串口进入待识别目录；新板输出登录提示
或 shell prompt 后只自动识别一次。失败后保持待识别，可人工重试。未进入此模式时不会凭串口内容猜测换板。

## 模块边界

板子与串口连接分开建模。`Manager.boards` 按序列号保存长期存在的 `Board`，
包含持久化身份、标签、档案位置与 SSH 会话注册表；`Manager.devices` 按物理端口
保存 `Device` 连接对象，包含 fd、采集线程、字节缓冲和连接代次。重新枚举建立
新连接并绑定到原 `Board`，无需复制或清空 SSH 会话。没有连接时按需创建离线
API 视图以查看归档。点击准备换板会分离原 Board、关闭其会话并给待识别连接
分配独立注册表。SSH 建立请求的预留名额始终归原注册表，即使请求结束时已换板。

生产环境由两个进程组成。`seriald-collector` 长期持有串口、SSH 会话、数据库和内部 API；
`seriald-control` 提供 TCP HTTP、静态页面和 Unix socket 反向代理。重启 control 不关闭 tty fd。
collector 只监听 `SERIALD_SOCKET`，不暴露网络端口。

实时终端通过 `GET /api/session-events` 建立 SSE 流；collector 在串口或 SSH 收到字节时唤醒
订阅者，control 逐行转发，不等响应结束。事件携带字节偏移，断线时以 `Last-Event-ID` 续读；
旧服务或 SSE 失败时网页回退到 `GET /api/session?off=…` 长轮询。输入始终通过
`POST /api/session-send`，多人各有读取游标，共享同一串口或 SSH 会话输出。
Agent 分享令牌由 control 保存在独立的 `agent-grants.sqlite3`（仅令牌哈希）；单设备范围以
序列号而非可变标签固定。control 过滤设备列表并钉住后续请求的序列号，撤销后立即拒绝新请求、
终止 SSE 并通知 collector 关闭相关 SSH 会话。开放模式仍允许无令牌访问。

| 路径 | 职责 |
| --- | --- |
| `seriald.py` | 控制面入口 |
| `seriald_core/collector_server.py` | 长期运行的采集/会话后端和 Unix socket API |
| `seriald_core/control_server.py` | 可独立重启的 TCP、静态资源和 API 代理 |
| `seriald_core/agent_grants.py` | Agent 令牌签发、持久化、撤销和范围查验 |
| `seriald_core/httpd.py` | TCP/Unix HTTP 的线程和超时上限 |
| `seriald_core/config.py` | 环境变量和运行配置 |
| `seriald_core/transport.py` | 串口描述符、短写补齐、USB recovery |
| `seriald_core/protocol.py` | 识别命令及字段解析 |
| `seriald_core/identity.py` | EEPROM、固件、SoC/eFuse 与旧软件身份的选择 |
| `seriald_core/handoff.py` | 干净重启时同 boot、短时、一次性的连接接续 |
| `seriald_core/archive.py` | 候选/正式日志的无覆盖暂存和提交后迁移 |
| `seriald_core/device.py` | 串口连接的控制台流、识别事务和会话 API 编排 |
| `seriald_core/models.py` | 持久 Board 身份、可替换连接绑定与独立会话所有权 |
| `seriald_core/transactions.py` | 统一串口事务租约、总超时、连接代次检查与取消 |
| `seriald_core/devices.py` | 串口发现、单读者采集生命周期、身份绑定与在线设备 |
| `seriald_core/sessions.py` | SSH PTY、独立日志、会话回收 |
| `seriald_core/session_base.py` | 共用 SSH 生命周期、输出缓冲、UTF-8/按日日志、审计和日志迁移 |
| `seriald_core/windows_manager.py` / `sessions_windows.py` | Windows COM 采集与 SSH channel 适配 |
| `seriald_core/storage.py` | SQLite 注册表和持久化操作历史 |
| `seriald_core/schema.py` | 数据库版本、升级前 WAL 快照、事务迁移与回滚 |
| `seriald_core/tasks.py` | 持久任务状态、进度、范围查询、有界后台执行和协作取消 |
| `seriald_core/task_actions.py` | 后台识别／SSH 操作适配、连接代次与 actor/client 保护；任务 API／CLI 已接入，网页与文件传输待完成 |
| `seriald_core/web.py` | HTTP 参数校验、API、静态文件 |
| `seriald_core/actions.py` | 注入 Manager 的设备写操作应用服务、错误码与能力查询 |
| `seriald_core/queries.py` | 会话游标、归档、启动搜索/对比与历史查询服务 |
| `seriald_core/results.py` | HTTP/直接调用共用的结果结构与稳定错误码 |
| `seriald_core/archive_io.py` | 普通日志文件校验、符号链接与替换竞态防护 |
| `seriald_core/maintenance.py` | 可选日志保留、活动写入/跨天启动保护、磁盘提示与白名单诊断导出 |
| `seriald_core/admin.py` | 显式迁移旧目录，不访问硬件 |
| `static/app.js` / `static/app.css` | 网页行为和样式；`index.html` 仅布局 |
| `static/terminal-navigation.js` | 终端缓冲搜索、已回显命令导航和高亮；通过注入当前视图检查隔离旧回调 |
| `static/terminal-view.js` | 当前 xterm 视图及连接代次、销毁、SSE／长轮询、输入队列、尺寸协商和固件屏幕重绘 |
| `static/archives.js` | 日期／启动选择、归档列表刷新和跨启动搜索；以标签及序列号隔离选择状态，通过注入回调打开终端标签 |
| `static/devices.js` | 设备目录、选中身份、搜索筛选、轮询顺序和重连／改名对应；输入占用修订号阻止旧轮询覆盖新操作 |
| `serialctl.py` | 无第三方依赖的 agent 客户端 |

文件复制在两个串口上重叠源读取与目标写入。当前原始分块上限为 10 KiB，上传最多三块一批；若压缩后的命令
超过终端输入行长，客户端自动拆块重试。每块仍等待完整响应帧，最后校验 SHA-256。
上传提交的确认帧丢失时，会只读核对目标的大小和 SHA-256，避免重复执行覆盖。

## 持久化

`SERIALD_BASE` 下的 `registry.sqlite3` 保存设备、SSH 会话和操作事件，使用 SQLite 事务及 WAL。
日志按身份可信度分层：

```text
logs/
├── <confirmed-serial>/                         # 已确认的 EEPROM/人工核实身份
├── _provisional/
│   ├── device-tree/<candidate-serial>/         # 固件或 U-Boot 上报，来源待确认
│   ├── soc-sysfs/<candidate-serial>/           # SoC/eFuse 芯片指纹
│   └── boot-marker/<boot-uuid>/                # /boot 软件身份
└── _unprovisioned/<usb-path-hash>/<connection-id>/
                                                  # 尚无可用编号，每次连接隔离
```

普通编号原样作目录名；含路径特殊字符的编号使用可逆百分号编码。`label`、USB 路径和 tty 节点
永远不作为正式目录名。

同一个候选编号后来由 EEPROM 证实时，服务关闭当前日志并自动把该连接的档案迁入
`logs/<confirmed-serial>/`。编号发生变化，或需要人工确认固件编号时，停服务后分别使用
`seriald-admin correct-serial` 或 `seriald-admin confirm-serial`；命令先检查冲突并复制到目标，
数据库事务成功后删除旧日志和旧目录。会话、事件和标签随身份一起迁移。

每个 SSH 会话采用唯一 sid 和独立 `ssh-<sid>-<日期>.log`。非 `web` actor 在同一设备上复用持久
PTY，直到显式关闭；网页创建的会话仍受闲置回收。关闭、掉线及服务重启不会删除记录。

Linux 与 Windows 共用 `SessionBase` 的启动、关闭、读游标、日志和结束审计。
平台适配器只负责 PTY/子进程组或 Paramiko channel 的打开、收发、尺寸调整和资源清理。
身份确认迁移目录时，先固定会话注册表并关闭需移动的日志句柄，再复制日志和提交注册表。
数据库提交前失败恢复原目录；提交后即使清理源目录失败，也向新目录继续写入。
解码器与半行缓冲跨迁移保留，SSH 连接保持打开；已在目标目录的日志不重复开关。
CLI `--actor`、命令标题、结果和退出码会显示在网页操作记录中；actor 是调用方自报名称，并非认证身份。
SSH 不使用上位机的持久 `known_hosts`；开发板刷机更换 host key 后可重新连接，连接记录仍按板卡身份归档。
agent 会话在网页中默认只读，操作者可显式点击“介入此会话”获得输入权；接管事件以及之后的输入请求
写入操作历史，终端输出继续进入同一 SSH 日志。

已确认连接识别前的临时日志移入对应候选或正式目录。更早、无法证明归属的 USB 路径日志不自动合并；
只能在核实归属后使用明确映射导入。

## 并发和终端

前端由 `app.js` 连接独立控制器：设备目录保存选中 key 与身份元数据，终端控制器独占
当前视图及其代次，归档控制器保存日期／启动选择和搜索请求版本，导航模块只操作当前
终端缓冲。控制器通过注入回调协作，不复制当前终端状态。设备轮询只发布最新请求的
结果；终端、归档及导航的异步回调继续检查视图或请求版本，防止切换后更新新设备。

串口可选输入占用由事务协调器按 actor 管理；默认仍共享输入。占用会阻止其他 actor
通过输入、探测或文件传输写串口，输出游标与独立 SSH 不受影响。人工 web 接管取消
旧事务并唤醒等待者，不关闭采集器；占用随连接代次变化失效，撤销 agent 令牌也会释放。
网页人工客户端沿用 web actor 审计，同时以每页 client_id 区分输入占用；同一 actor
的另一页必须显式接管。事务和文件传输保存发起页 client_id，后台分块与等待不能
因执行线程变化绕过占用；CLI agent 则按 actor 复用输入占用。

HTTP 写操作委托给 `DeviceActions(manager)`：输入、识别、换板、标签、SSH 操作、
传输及取消共用同一编排、actor scope 和审计。服务返回 `ActionResult(status, payload)`，
不依赖 HTTP handler 或全局 Manager；HTTP 层负责解析请求、设备选择和响应 key 标注，
control 层仍负责认证、授权范围和 actor 重写。独立 CLI 调用相同 HTTP 接口。
会话与归档读取委托给 `SessionQueries` 和 `ArchiveQueries`，直接调用使用普通标量参数，
不依赖 HTTP 查询字符串。下载返回已选定档案的描述，由 HTTP 层流式发送。
归档列表复制启动摘要，不删除内部缓存中的分段信息；启动读取、搜索和下载共用普通文件校验。
错误保留原有 error/ok 字段并增加稳定 code；control 的认证及代理错误也使用同一错误码约定。
`GET /api/capabilities` 不访问开发板，报告协议能力及使用条件，不报告密码或设备清单。

每个串口只有一个采集线程；网页和 CLI 读取同一个缓存。写通路不读串口、不修改 termios。
设备重连先退出旧读者和旧事务，再创建新连接对象。网页与 CLI 可携带预期 serial，防止标签被重新分配后串台。

串口视图和 agent SSH 旁观视图会抑制 DSR/DA 等自动查询应答；点击设备、回放历史不会向板子回写光标位置报告。网页自己创建的 SSH 会话仅在完成首次历史回放后允许实时协议应答。
串口归档和已结束的 SSH transcript 复用同一 xterm 区域，以可关闭的只读标签展示；输入、粘贴、resize
和终端自动应答均不会进入后端。标签缓存打开时的文件末尾快照，关闭后重新打开可读取更新后的快照。
“输入导航”只索引当前 xterm 缓冲中带 shell 提示符的命令行；跳转只滚动并选中该行，不触发输入。
命令行统一颜色与底色由 xterm 装饰层绘制，原始串口/SSH 输出和归档文件保持不变。
串口归档按日期文件保存原始数据；启动索引按实际字节范围列出每次启动涉及的日期，因此网页可在日期和
启动次数之间双向定位，一天内多次启动及跨天运行都不会改变原始日志。
启动对比直接读取这两个字节范围，可跨设备选择历史档案；只规范化采集时间戳与内核相对计时，
按行展示变化和新增异常关键词。每侧最多比较 4 MiB / 12000 行，最多呈现 1500 行差异上下文。

串口文件传输在现有采集器读者上执行带边界帧的 shell 命令，当前原始分块上限 10 KiB，上传最多三块一批；一次连接只允许一项传输，
并绑定连接代次，防止换板后续传。上传先写板端临时文件，整文件 SHA-256 一致后才移动到目标路径；
下载校验完整文件后才交给浏览器或 CLI。板间复制由上位机逐块中转，不需要板间网络；
源板读下一块可与目标板写当前块并行，两个设备各自仍只运行一个串口事务。
板端可用 gzip 且压缩后不超过原块 80% 时，对上传块自动压缩；下载按前一块压缩率选择下一块的
传输方式。API 对浏览器和 CLI 始终返回原始字节，最终校验覆盖完整解压后的文件。

串口多轮交互由统一事务协调，可选输入占用用于避免不同操作者交错输入；独立 SSH 会话仍共享板子的系统状态。
日志按日期轮转，默认永久保留。可选保留期限跳过活动文件和当前跨天启动涉及的串口档案；
磁盘空间提示及脱敏诊断导出见 [日志运维](LOG_OPERATIONS.md)。目前没有总容量配额。
