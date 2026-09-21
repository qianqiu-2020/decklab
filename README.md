# seriald

供人和 agent 共用的 Linux 开发板工作台：持续采集串口、独立 SSH 会话、在线日志、历史设备和操作记录。

设备身份优先使用 EEPROM ONIE 序列号，并兼容 vendor/upstream 内核的不同暴露方式；有效 DT `serial-number` 也直接作为正式编号；`key` 是用户标签（如 `riscv-118`、`musepi2`）。待确认的 SoC/eFuse 与 `/boot` 身份保存在 `logs/_provisional/`；没有编号时按 USB 连接隔离。标签、USB 插口和 tty 节点可改变。

## 安装到 Ubuntu 上位机

```sh
python3 tools/build_deb.py
sudo apt install ./dist/seriald_1.4.7_all.deb
```

包包含独立的 collector/control systemd 服务、CLI、网页与 skill，默认监听 `0.0.0.0:8898`，
生产数据保存在 `/data/seriald-data`。
更新或重启 control 不会关闭 collector 持有的串口和 SSH 会话；`serialctl health` 可核对 collector PID。
在同一 USB-UART 后更换板子前，先在网页点击“准备换板”；新板日志会先隔离，出现登录提示后自动识别一次。
[安装、配置和旧数据迁移](docs/INSTALL.md) · [架构与模块边界](docs/ARCHITECTURE.md)
· [硬件身份、/boot 兜底标记与重复编号处理](docs/HARDWARE_ID.md)
· [版型与内核唯一编号矩阵](docs/DEVICE_IDENTITY_MATRIX.md)
· [分阶段重构计划](docs/REFACTOR_PLAN.md)

## Agent 使用

```sh
serialctl devices
serialctl --device riscv-118 read
serialctl --actor claude-code/debug-session --device riscv-118 exec --command 'uname -a'
serialctl --device riscv-118 activity
```

源码环境用 `python3 serialctl.py` 替代 `serialctl`。远程访问设置 `SERIALD_URL`。
`exec` 使用当前 `--actor` 在每块板上的持久 SSH 会话：同一 agent 的后续命令复用同一个 shell，
返回 JSON 输出和退出码，网页会一直显示可旁观的标签。会话只在显式 `ssh-close` 或网页关闭时结束；
网页自己创建的 `web` 会话仍按独立会话处理。

[Agent 接入指南](docs/AGENT_ACCESS.md) · [seriald-board skill](skills/seriald-board/SKILL.md)

## 开发与检查

```sh
python3 -m unittest discover -s tests
node tests/frontend.test.js
node tools/browser_smoke.js
python3 -m compileall -q seriald_core seriald.py serialctl.py
bash -n rescue.sh
python3 tools/build_deb.py
```

`seriald.py` 是 control 薄入口，collector 入口为 `python3 -m seriald_core.collector_server`；后端在
`seriald_core/`，布局、样式和交互分属 `index.html`、`static/app.css`、`static/app.js`。
Python 后端使用标准库，SSH 需要系统 `ssh` 与 `sshpass`；前端依赖本地 `vendor/`。
浏览器检查需要 Node.js 22+ 和 Chrome（可用 `CHROME_BIN` 指定），使用隔离模拟设备验证真实 xterm，截图输出到 `dist/`。
源码运行必须先启动 collector，再运行 `python3 seriald.py 8898 127.0.0.1`；只有 collector 打开串口。
隔离开发可设置 `SERIALD_BASE` 为临时目录、`SERIALD_BY_PATH` 为空测试目录；不要打开真实串口验证改动。
