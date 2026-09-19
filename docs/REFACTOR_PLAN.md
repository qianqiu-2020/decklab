# 重构实施拆分

目标是让串口采集长期稳定运行，同时允许身份逻辑、SSH、API 和 UI 独立演进。每阶段必须保持单串口单读者，
通过现有 Python、前端和真实浏览器回归后再进入下一阶段。

| 阶段 | 边界 | 主要交付物 | 当前状态 |
| --- | --- | --- | --- |
| 1. 身份与归档 | 标签、候选身份、正式序列号、历史目录 | EEPROM/DT/eFuse 选择、冲突保护、`_provisional`、确认与更正迁移 | 已完成 |
| 2. 采集核心 | 串口发现、唯一 reader、原始流、写入互斥 | 独立 collector 进程、Unix socket 内部 API、健康信息 | 已完成 |
| 3. 控制面 | 静态页面、网络 API、collector 代理 | control 可独立重启；collector PID、tty fd 和会话保持不变 | 已完成 |
| 4. SSH 网关 | 会话建立、刷机后 host key、命令审计、日志 | 独立会话管理、临时 known-host 策略、在线旁观与介入 | 已完成 |
| 5. API 与 Agent | HTTP 路由、CLI、skill、访问控制 | CLI 错误码、可选 token、health、agent 能力文档 | 已完成 |
| 6. Web UI | 设备列表、终端、操作记录、日志查看 | 公共 UI/终端策略模块、候选身份提示、历史筛选、agent 会话人工介入 | 已完成 |
| 7. 部署升级 | Ubuntu 包、开发/生产配置、升级切换 | collector 与控制面独立 systemd unit；升级只重启控制面 | 已完成 |
| 8. 运维清理 | 日志容量、健康状态、备份、废弃代码 | 磁盘水位、health 命令、删除旧单体入口；日志保留由运维控制 | 已完成 |

## 模块目标

```text
seriald-collector  ── Unix HTTP socket ── seriald-control ── HTTP/CLI/UI/skill
      │                                              │
      ├── 唯一 tty reader + 日志                     └── 静态资源与 API 代理
      ├── 身份与设备注册表
      └── SSH 网关与审计日志
```

collector 是唯一允许打开串口读描述符的进程。控制面升级只断开短暂的 UI/API 连接，不关闭 collector；
内部协议覆盖设备清单、按偏移读、串口写入、连接代次、SSH 会话和健康检查。

## 实施顺序

1. 已将归档、单板会话、设备采集注册拆成独立模块。
2. 已加入 Unix socket 内部 API 和独立 `seriald-collector.service`。
3. 已将网络控制面切换为代理，并用包级测试验证 control 重启期间 collector PID 与启动时间不变。
4. 已拆分前端公共工具与终端安全策略，并补历史筛选和 agent 会话介入测试。
5. 已加入磁盘水位诊断、可选认证，并删除单进程服务入口。

每阶段都生成可安装包。当前支持不打断采集地更新 control/UI；更新 collector 本身仍需维护窗口。
