# Windows 便携上位机

Windows 版直接接管本机 USB 串口对应的 COM 端口，提供与 Linux 版相同的网页和 `serialctl` API。它不是远程访问 Linux 采集器的客户端。

在 Windows 上从 GitLab 拉取源码并构建 `seriald-windows-portable.zip`，解压后运行 `Start-seriald.cmd`。网页默认打开 `http://127.0.0.1:8898/`，注册表和日志写入解压目录下的 `data/`。请将整个目录放在可写位置；更新时替换程序文件并保留 `data/`。此程序只管理它能在本机看到的 COM 端口，不能与另一串口软件同时打开同一端口。

要让局域网内其他电脑访问，双击 `Start-seriald-LAN.cmd`，默认监听 `0.0.0.0:8898`；可在命令行传 `-Port 9000` 改端口。首次启动自动生成访问令牌，保存在 `data/lan-token.dpapi`（Windows 当前用户 DPAPI 加密），窗口会显示网页 Basic Auth 的用户名 `seriald` 和令牌。后续同一 Windows 用户启动会复用令牌；换用户或换电脑时需重新生成。浏览器使用 Windows 的实际 IP，例如 `http://192.168.1.20:8898/`。如被防火墙拦截，需一次性允许该端口的专用网络入站 TCP；不要将此无 TLS 的服务直接暴露到公网。

Agent 在同一台 Windows 电脑上可以运行 `serialctl.cmd devices`、`serialctl.cmd --device riscv-118 read` 等命令；其他电脑上的 agent 设置 `SERIALD_URL=http://<Windows-IP>:8898` 和 `SERIALD_TOKEN=<窗口显示的令牌>` 后使用 `serialctl.py`。脚本会自动设置服务端的 `SERIALD_AUTH_TOKEN`；Windows 防火墙仍需允许相应端口。对外暴露时应使用受信任网络或反向代理 TLS。

Windows COM 采集使用 pySerial 的单个独占句柄，读写共享该句柄；Linux 仍保留单读者和独立只写描述符。设备身份来自板上序列号；COM 编号只作为可变节点。若适配器报告 USB 拓扑位置，程序以该位置锚定连接；否则退回 COM 编号。换板前点击“准备换版”。Windows 版不提供 Linux USB 自愈。

开发者可在 Windows 的 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-windows.txt
.venv\Scripts\python -m unittest discover -s tests -p test_windows_portable.py
.venv\Scripts\python seriald_windows.py --no-browser
```

在 Windows 上安装 Git、Python 3.12（含 `py` 启动器）后，打开 PowerShell：

```powershell
git clone git@gitlab.dc.com:bianbu/seriald.git
cd seriald
powershell -ExecutionPolicy Bypass -File .\tools\build_windows.ps1
```

后续更新源码时，在仓库目录先运行 `git pull --ff-only`，再运行同一构建命令。源码仓库与实际运行的解压目录分开放置，避免构建时碰到 `data/`。如果 Windows 尚未配置 GitLab SSH 密钥，使用你能访问的 HTTPS 克隆地址代替上面的 SSH 地址。

脚本安装依赖、运行 Windows 模拟测试并生成 `dist\seriald-windows-portable.zip`；构建期间不会打开真实 COM 口。将 ZIP 解压到可写目录，进入其中的 `seriald` 文件夹，双击 `Start-seriald.cmd`（仅本机）或 `Start-seriald-LAN.cmd`（局域网）。先在网页或运行 `serialctl.cmd devices` 确认板子出现，再验证读写、拔插后的识别、SSH 和文件传输。`data/` 是持久数据，后续更新时保留它；不要在曾经运行过程序的 `dist/seriald` 目录直接重新构建。

PyInstaller 必须在 Windows 上构建 Windows 可执行文件。仓库中的 `.github/workflows/windows-portable.yml` 是 GitHub Actions 配置；GitLab 不会自动运行它，目前以 Windows 本机构建为准。
