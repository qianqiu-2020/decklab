# 修复记录

旧版单文件实现的修复与线上验收已归档至 [2026-09-19 历史记录](docs/archive/FIXES-2026-09-19.md)。
当前设备模型、日志归属与模块边界见 [架构说明](docs/ARCHITECTURE.md)，安装及数据迁移见 [安装说明](docs/INSTALL.md)。

1.3.3 集中管理版本号，health 显示实际运行版本及在线/待识别/离线数量；清除旧 recovery 提示，
并为旧版本升级提供一次性的生产数据组权限迁移。

1.3.2 使用 `seriald` 组统一管理生产数据读写权限，目录使用 setgid，服务以 `UMask=0007` 创建组可写文件。

1.3.1 修正 systemd 运行目录所有权：仅 collector 管理 `/run/seriald`，单独重启 control 不再删除
collector 的 Unix socket。

1.3.0 将长期采集后端与 Web/API 控制面拆成独立 systemd 服务，通过本机 Unix socket 通信。
生产包默认使用 `/data/seriald-data` 并监听 `0.0.0.0:8898`，源码开发数据仍留在仓库。
升级或重启 control 不关闭串口和 SSH 会话；新增 collector 健康信息、可选访问 token、刷机 host key
兼容、agent SSH 人工介入审计，以及独立的前端终端安全策略模块。

1.2.0 将身份可信度纳入数据模型：EEPROM/人工核实身份使用正式序列号目录，DT、SoC/eFuse 和
`/boot` 身份进入 `_provisional/<来源>/<编号>/`。编号确认或更正时同步迁移日志、会话和事件，
网页明确区分候选编号与正式板卡序列号。归档迁移、单板会话和设备采集注册已拆成独立模块；SSH
不再依赖上位机持久 known_hosts，开发板刷机更换 host key 后可直接建立新会话。

本次重构修复了点击串口时 xterm 自动应答 DSR/DA 查询、向板子写入光标位置报告的问题。
串口查看和 agent 会话旁观保持被动渲染；真实 xterm 浏览器测试覆盖该行为。

1.1.1 增加无效固件编号的 SoC/eFuse 回退、来源显示和离线档案编号更正。K1 编号格式按厂商
只读驱动校验；无法取得有效硬件编号的连接继续隔离，避免重复出厂编号合并两块板。
