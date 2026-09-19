# seriald 修复记录（2026-09-19）

代码及隔离验证已完成；尚未应用到运行中的 Python 服务。允许的一次重启尚未使用。
所有修改均位于 `/data/seriald/`，没有打开/发送到真实串口，没有针对硬件执行自愈脚本。

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

## 验证及当前阻塞

已验证：

```sh
python3 -m py_compile /data/seriald/seriald.py
cd /data/seriald
python3 -m unittest discover -s tests
node tests/frontend.test.js
bash -n rescue.sh
git diff --check
```

80 个 Python 隔离测试、11 个无浏览器 JS 隔离测试通过。实际 Handler._page 测试返回与 index.html 完全相同的字节、HTTP 200、正确类型和 Content-Length；这不等同于已验证在线页面。

本会话看到的是隔离的 PID/设备/网络环境：

- `curl --max-time 5 -sS http://10.0.90.177:8898/api/devices` 返回 `curl: (7) Couldn't connect to server`。
- `systemctl` 与 `systemctl --user` 返回 `Failed to connect to bus: Operation not permitted`。
- `/dev/serial/by-path` 在本沙箱不可见。

因此未重启服务、未验证线上页面，也未声称三个设备均 alive 或已有新增输出。待访问运行服务的权限恢复后，只重启一次，然后通过 GET `/api/devices` 验证三台均 alive，并被动观察每台 bytes/last_rx 或采集增量增长；不发送字符、不另开 tty、不调用 rescue。

## 特权 helper 安装（由用户执行）

```sh
sudo install -o root -g root -m 0755 /data/seriald/rescue.sh /usr/local/sbin/seriald-rescue.sh
```
