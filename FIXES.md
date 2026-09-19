# seriald 修复记录（2026-09-19）

代码、隔离验证与线上验收均已完成。服务已于 17:03:55 重启（PID 1965206）加载本版代码，
三台板全部验收通过，详见「线上验收」一节。
所有修改均位于 `/data/seriald/`。

## 按审查编号归类

| 编号 | 实现与原因 |
| --- | --- |
| 1 | 在 preload 前初始化 logpath/dead 等状态，空日志目录不会留下缺失属性。 |
| 2 | 按字符设备号去重 by-path 别名；绑定 inode/设备号及完整 by-path；旧读线程关闭退出后才启动替代者。断线重开也使用新的连接代次，旧控制台操作退出前不会重用 Device。 |
| 3 | 配置失败关闭 fd；EOF 关闭并退避；线程 finally 释放读 fd，扫描器可补回退出的线程；日志错误不会打断内存采集。 |
| 4 | rescue.sh 使用完整锚定 USB 拓扑正则，拒绝路径穿越、畸形名称和额外参数；仅修改源文件。 |
| 5 | 每次切设备/会话销毁旧 xterm；队列、解析及 CSI 6n 的自动应答保留所属视图检查。 |
| 6 | 所有异步读取、会话列表、日志、动作、粘贴、resize 和关闭回调绑定 key/sid/generation；后端响应包含 key。 |
| 7 | 事务锁覆盖自动登录、探测命令、手动输入及自愈；写入口不能在 probing 检查/赋值之间插入。 |
| 8 | 每设备最多 8 个 SSH 会话（含创建中的预留）；闲置 30 分钟回收；退出不隐式重连；取消写入、join 读线程、关闭 fd/日志、终止进程组并 wait。 |
| 9 | 前端顺序发送，切换时丢弃未发送输入；后端补齐短写，处理 EINTR/EAGAIN，写入截止 5 秒；写锁与输出条件锁分离。 |
| 10 | 保存、取消轮询 timer，回调验证创建时的视图，只保留一个轮询链。 |
| 11 | SSH 会话消失时走完整 switchSession，重置终端、offset、resize 和轮询代次。 |
| 12 | 历史仅用于回看，不参与现场提示符判断；本地诊断文字不再进入原始串口字节流。 |
| 13 | 每次命令使用独立 nonce；printf 分拆标记避免回显匹配；等待完整换行结束帧并验证起始帧；必需字段缺失/重复直接失败；字段换行先规范化。 |
| 14 | 累计流统一剥 ANSI；支持免密码 shell、中文密码/登录失败；连接先通过新提示符确认 CR/LF，写失败立即返回。 |
| 15 | systemctl is-active --quiet 分支只输出一个 SSH 状态；明确报告非 root、缺少 systemd/包管理器、安装失败等情况。 |
| 16 | 稳定日志目录包含完整 by-path 的 SHA-256 摘要，区分接口及长键；旧目录保留只读历史入口；重名标签不抢其他设备的别名。 |
| 17（部分） | 串口和 SSH 按日期轮转；preload 用二进制 seek 有界读取尾部；Device 构造、labels/链接 IO 移出 Manager 全局锁。 |
| 18 | SSH 注册表及序号/创建配额有锁；遍历快照；闲置判断和关闭声明在会话锁内原子完成；耗时回收在注册表锁外。 |
| 19（部分） | 在线程创建前限制为 64 个 worker；socket 超时 15 秒；请求体上限 64 KiB；拒绝负长度、非对象 JSON、非法 offset、分块编码及不完整请求体。 |
| 20 | 接口提供 data_b64，xterm 接收原始字节并流式解码；串口/SSH 日志保留 UTF-8 与行尾状态，避免 CJK 分块乱码。 |
| 21（部分） | root/脚本都要求全部请求步骤成功；区分辅助步骤完成和实际新输出；没有新采集字节就不声称已恢复；UI 提供明确的重新绑定按钮。 |

## 有意保留的后续工作

- **17：大小轮转与总保留量**：暂不引入自动删除历史记录的策略；需要先确定每设备预算及保留期限。已有日期轮转、有界预载和磁盘错误隔离，但磁盘历史总量仍无上限。
- **19：每客户端配额与异步操作队列**：全局线程/请求上限已防止无限增长；公平性及后台作业协议会扩大接口和 UI 修改范围，暂缓。15 秒是 socket IO 超时，不是整个长控制台操作的截止时间。
- **21：自动升级 rebind 及等待恢复的状态机**：不再把 sysfs 写成功等同于恢复，也不根据不可靠状态自动拔插；改为显式按钮。quiet 板子没有新输出时恢复状态保持未确认。
- **日志旧目录**：不移动或重写运行中的旧日志。新版本写入新目录，页面仍列出旧目录文件；旧版本已经混在一起的数据无法可靠拆分。

## 修复中另行发现

- 重枚举时替换 stop Event 会让旧识别操作继续使用新连接：重启采集前等待事务锁，发布识别结果时检查来源 Event；SSH 建立结果也检查来源。
- threaded server 中的 preexec_fn 可继承锁：SSH 改用 start_new_session=True。
- 原 main 在绑定 HTTP 端口前启动采集：现在先绑定端口，避免重复启动失败前已有第二组读者。
- 本地诊断文字插入半个 UTF-8/ANSI 序列会破坏终端流：仅写诊断日志，不混入原始采集缓冲。
- 禁止 `.`/`..` 成为标签目录名；重连不 flush 已排队的启动输出。
- 启动/重连保持被动采集；取消扫描循环中的自动回车和自动 USB rescue，显式操作入口仍保留。

## 验证

隔离测试：

```sh
python3 -m py_compile /data/seriald/seriald.py
cd /data/seriald
python3 -m unittest discover -s tests     # 82 个通过
node tests/frontend.test.js               # 14 个通过
bash -n rescue.sh
git diff --check
```

## 线上验收（2026-09-19 17:03 重启后）

**重启前必须知道的一件事**：旧进程 52017 启动于 12:04:52，而 21 项修复提交于 12:51:23 ——
也就是说旧进程比修复早 47 分钟，**这 21 项在重启前一项都没生效**。当时的两个症状都源于此：

- `Device.append()` 没有 `received` 参数，`_say()` 写的本地诊断文字也被计入 `bytes`，
  于是 `/api/rescue` 里 `recovered = d.bytes > rx_before` 恒为真，前端据此永远隐藏
  「重新绑定串口」按钮，状态栏还谎报「采集已恢复」。
- 诊断文字直接进了终端缓冲（第 12 条声称已修，实际没跑）。

重启后逐项实测：

| 检查 | 结果 |
| --- | --- |
| 三台设备 | 3/3 alive |
| `recovered` 诚实性 | 对无新输出的设备返回 `false`（旧进程在同样无新输出的 milkv 上返回过 `true`，bytes 前后都是 7546） |
| 本地诊断文字污染 | 终端缓冲内已无 `手动自愈` 字样 |
| `/index.html` 缓存头 | `Cache-Control: no-cache, must-revalidate` |
| 线上页面 == 仓库 index.html | sha256 相同 |
| 识别设备（milkv / riscv-118） | 自动登录 root 成功，字段完整返回 |

**rebind 全链路**（riscv-118，用户从页面点击）：

```
17:14:10  unbind: ok; bind: ok            [3-7]
17:14:10  /dev/ttyUSB2 读取中断: EOF
17:14:11  root@riscv-118:~#                ← 板子未重启，立刻回来
17:14:12  seriald 已接入 /dev/ttyUSB3      ← 编号变了，按 by-path 自动重连
17:14:37  识别成功: 序列号=26abd51e5bc2dbe1 主机名=riscv-118 sshd=active
```

由此确认三点：设备编号变化不影响跟踪（按 by-path 认设备）、EOF 后自动重连无需人工干预、
恢复前不谎报 recovered。

**一条硬件侧观察**：milkv(ttyUSB1, FT232B) 的采集曾于 16:23:24 停止，内核报
`ftdi_sio ttyUSB1: usb_serial_generic_read_bulk_callback - urb stopped: -32`（-EPIPE，
读取 URB 被 stall），且未跟随任何 USB 重枚举。**重启即恢复**（重开读 fd 清掉了 stall），
未做 rebind；soft 自愈（只关 autosuspend）对它无效，`runtime_status` 本来就是 active。
若该现象反复出现，应先怀疑适配器或线材，而非 seriald —— 同日 15:59 该 USB 树曾在一分钟内
反复枚举三四次。

## 本轮新增修复（提交 83ee06f）

用户报告「网页终端 Ctrl+V 只回显 `^V`、不粘贴」，查出一个前端缺陷与一个只影响 `/index.html`
的 HTTP 缺陷：

- **Ctrl+V 被 xterm 吃掉**：xterm 按 keyCode 86 把 V 编成 `0x16` 发给板子，随后立即
  `preventDefault()` —— 浏览器的粘贴事件根本不会派发，挂在 `d-term` 上的 paste 监听器因此
  永远等不到。改为在 `attachCustomKeyEventHandler` 中对 Ctrl+V（含 Ctrl+Shift+V）返回
  false：xterm 入口即 return，既不编码也不 preventDefault。Cmd+V 无需特判（已核
  `evaluateKeyboardEvent` 的 default 分支：带 meta 的键 key 为 undefined、cancel 为 false）。
- **粘贴监听挂错了节点**：焦点不在终端里时（从别处复制完切回浏览器、没先点一下终端就按
  Ctrl+V），paste 事件的目标是 `body`，挂在 `d-term` 上的监听器收不到，表现是「按了完全
  没反应」。改为挂 `document`，并给页面自己的输入框让路（焦点在发送命令框时不抢）。
- **剪贴板为空时给提示**，不再静默无事发生。
- **`/index.html` 整页缓存**：该路径原先落到 `SimpleHTTPRequestHandler`，不发
  `Cache-Control`，浏览器按启发式规则整页缓存 —— 服务换了新页面、用户那边还是旧的。
  这正是「重新绑定串口」按钮在页面上根本不存在、误导排查方向很久的原因。现在 `/` 与
  `/index.html` 走同一条路，都带 `no-cache`。

删除了 Ctrl+Shift+V 的「借临时输入框接住粘贴」退路：实测它 40ms 采样全周期都未出现
（`b-paste` 的 click 处理器确实被调用，但框没留下来，机制未查明），且 Ctrl+Shift+V 现已
由上述分支交给浏览器原生纯文本粘贴，实测可用。**未查明的部分如实留在此处，不假装已归因。**

## 特权 helper 安装（由用户执行）

```sh
sudo install -o root -g root -m 0755 /data/seriald/rescue.sh /usr/local/sbin/seriald-rescue.sh
```
