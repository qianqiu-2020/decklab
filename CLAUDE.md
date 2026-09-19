# 开发板访问

本仓库的串口设施也供其他 agent 会话使用。优先使用 `seriald-board` skill，
仓库内入口为 [SKILL.md](skills/seriald-board/SKILL.md)。也可直接读
[Agent 接入指南](docs/AGENT_ACCESS.md)，通过 `python3 /data/seriald/serialctl.py`
调用已经运行的服务；服务地址可用 `SERIALD_URL` 或 `--url` 指定。

- 先运行 `devices`，确认设备标签 key 及不可变序列号；用 `read` 查看现有串口输出。
- 普通命令使用 `--device 'riscv-118' exec --command '命令'`，它使用独立 SSH 会话。
- 串口输入是共享的，先协调操作者；不直接打开 tty，不启动第二个采集器。
- 检查 JSON 中的 `completed`、`exit_code` 和 `session_closed`；状态未知时不盲目重试。
- 仓库代码修改遵守 [AGENTS.md](AGENTS.md)。
