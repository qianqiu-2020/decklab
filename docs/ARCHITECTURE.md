# 架构与数据模型

## 身份与连接

- **serial**：当前候选或正式身份编号，也是 SQLite 主键。EEPROM ONIE `0x23`、人工核实更正的编号是正式身份；DT、SoC/eFuse 和 `/boot` 标记在核实前是候选身份。
- **label / key**：当前唯一标签，例如 `riscv-118`、`musepi2`，用户操作入口。改名不改序列号或日志目录。
- **by_path / dev**：本次已确认连接的 USB 物理路径和 tty 节点。离线时当前字段为空；`last_path`、`last_dev` 保留历史。
- **未 provision 连接**：使用基于 USB by-path 的 `unprovisioned-…` 临时 key；每次连接单独写入
  `logs/_unprovisioned/<path-hash>/<connection-id>/`。换插口会改变临时 key，正式身份不依赖插口。

识别操作读取开发板序列号，再绑定到当前连接。已有序列号保留用户标签，更新位置；新序列号初始标签取 hostname（冲突时分配唯一临时标签），随后可改名。缺少序列号、两处在线报告同一序列号或标签冲突都会拒绝合并。

部分固件的 device-tree 可能填写相同出厂占位值（例如 `0123456789ABCDEF`）。这不能证明是同一块板；冲突连接保留在待识别列表并显示原因，日志继续暂存。已确认的冲突保存在注册表中，拔掉另一块板或重启服务也不会允许误合并。必须核实真实的唯一板载身份，不能用 tty、标签或操作系统 machine-id 静默替代。

识别优先读取 ONIE cell，没有 cell 时校验并解析原始 NVMEM，以覆盖未启用 ONIE layout 的 vendor 内核。
遇到无效固件编号时使用经过验证的 SoC 编号，再以旧 `/boot/seriald-id` 软件标记兜底，
记录 `serial_source` 和固件的 `reported_serial`。普通探测只读标记；创建逻辑封装在 `boot_marker.py`。
读取规则、K1 驱动依据和旧编号更正步骤见 [硬件身份说明](HARDWARE_ID.md)。

collector 干净重启时写入一次性短期 handoff；仅在同一 host boot、两分钟内且 by-path、tty 节点和
内核设备身份完全一致时接续已有绑定，不向板子发送回车、密码或命令。异常退出、主机重启、USB
重新枚举或校验不符时，已注册设备只作为历史设备保留，新采集连接进入待识别。

同一个 USB-UART 后面更换开发板但 tty 不重新枚举时，内核无法感知板卡已变化。操作者必须先点击
“准备换板”：服务立即把旧身份置为离线、关闭其 SSH 会话并让该串口进入待识别目录；新板输出登录提示
或 shell prompt 后只自动识别一次。失败后保持待识别，可人工重试。未进入此模式时不会凭串口内容猜测换板。

## 模块边界

生产环境由两个进程组成。`seriald-collector` 长期持有串口、SSH 会话、数据库和内部 API；
`seriald-control` 提供 TCP HTTP、静态页面和 Unix socket 反向代理。重启 control 不关闭 tty fd。
collector 只监听 `SERIALD_SOCKET`，不暴露网络端口。

| 路径 | 职责 |
| --- | --- |
| `seriald.py` | 控制面入口 |
| `seriald_core/collector_server.py` | 长期运行的采集/会话后端和 Unix socket API |
| `seriald_core/control_server.py` | 可独立重启的 TCP、静态资源和 API 代理 |
| `seriald_core/httpd.py` | TCP/Unix HTTP 的线程和超时上限 |
| `seriald_core/config.py` | 环境变量和运行配置 |
| `seriald_core/transport.py` | 串口描述符、短写补齐、USB recovery |
| `seriald_core/protocol.py` | 识别命令及字段解析 |
| `seriald_core/identity.py` | EEPROM、固件、SoC/eFuse 与旧软件身份的选择 |
| `seriald_core/handoff.py` | 干净重启时同 boot、短时、一次性的连接接续 |
| `seriald_core/archive.py` | 候选/正式日志的无覆盖暂存和提交后迁移 |
| `seriald_core/device.py` | 单块板的控制台流、识别事务和 SSH 会话编排 |
| `seriald_core/devices.py` | 串口发现、单读者采集生命周期、身份绑定与在线设备 |
| `seriald_core/sessions.py` | SSH PTY、独立日志、会话回收 |
| `seriald_core/storage.py` | SQLite 注册表和持久化操作历史 |
| `seriald_core/web.py` | HTTP 参数校验、API、静态文件 |
| `seriald_core/admin.py` | 显式迁移旧目录，不访问硬件 |
| `static/app.js` / `static/app.css` | 网页行为和样式；`index.html` 仅布局 |
| `serialctl.py` | 无第三方依赖的 agent 客户端 |

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

每个 SSH 会话采用唯一 sid 和独立 `ssh-<sid>-<日期>.log`。关闭、掉线及服务重启不会删除记录。
CLI `--actor`、命令标题、结果和退出码会显示在网页操作记录中；actor 是调用方自报名称，并非认证身份。
SSH 不使用上位机的持久 `known_hosts`；开发板刷机更换 host key 后可重新连接，连接记录仍按板卡身份归档。
agent 会话在网页中默认只读，操作者可显式点击“介入此会话”获得输入权；接管事件以及之后的输入请求
写入操作历史，终端输出继续进入同一 SSH 日志。

已确认连接识别前的临时日志移入对应候选或正式目录。更早、无法证明归属的 USB 路径日志不自动合并；
只能在核实归属后使用明确映射导入。

## 并发和终端

每个串口只有一个采集线程；网页和 CLI 读取同一个缓存。写通路不读串口、不修改 termios。
设备重连先退出旧读者和旧事务，再创建新连接对象。网页与 CLI 可携带预期 serial，防止标签被重新分配后串台。

串口视图和 agent SSH 旁观视图会抑制 DSR/DA 等自动查询应答；点击设备、回放历史不会向板子回写光标位置报告。网页自己创建的 SSH 会话仅在完成首次历史回放后允许实时协议应答。
串口归档和已结束的 SSH transcript 复用同一 xterm 区域，以可关闭的只读标签展示；输入、粘贴、resize
和终端自动应答均不会进入后端。标签缓存打开时的文件末尾快照，关闭后重新打开可读取更新后的快照。

串口多轮交互仍需协调唯一操作者；独立 SSH 会话也共享板子的系统状态。日志仅做日期轮转，未配置自动删除或总容量限制。
