# Windows 上位机：构建、安装与访问

Windows 版独占接管本机 USB 串口的 COM 端口，提供网页与 `serialctl` agent API。原生构建需 Git、Python 3.12（含 `py` 启动器）；Docker 构建需 Git 和支持 Windows containers 的 Docker Desktop。两种构建都需联网下载依赖，安装已构建的 ZIP 不需要 Python。

在源码目录双击 `Build-seriald.cmd`，或运行 `powershell -ExecutionPolicy Bypass -File .\tools\build_windows.ps1`。构建脚本创建 `.venv\`、运行 Windows 模拟测试，并在 `dist\` 生成 `seriald-windows-portable.zip`。然后双击 `Install-seriald.cmd`；它把包安装到源码目录下可见的 `seriald-installed\`，与源码及 `dist\` 构建目录分开，更新时保留其中的 `data\`。首次使用新路径时，若旧版 `%LOCALAPPDATA%\seriald\data` 存在，会复制旧数据；旧目录不删除。自选位置可运行 `powershell -ExecutionPolicy Bypass -File .\tools\install_windows.ps1 -InstallDir D:\seriald`。安装前先关闭旧的 seriald 窗口，释放 COM 口。

也可以在 Windows 11 Pro/Enterprise 的 Docker Desktop 中切换到 **Windows containers**，双击 `Build-seriald-Docker.cmd` 构建同一种 ZIP。构建使用 Windows Server Core + Python 3.12 镜像；Linux/WSL2 容器不能交叉生成这个 Windows PyInstaller 程序。Docker 只参与构建，运行时仍在 Windows 主机安装并直接接管 COM 口。此方式需要 Docker Desktop、Windows 容器功能和较大的镜像下载；宿主机/镜像版本需兼容。当前仓库没有 Windows Docker 测试环境，首次使用请在目标 Windows 机器验证构建与启动。[Docker Desktop 要求](https://docs.docker.com/desktop/setup/install/windows-install/) · [Windows 容器版本兼容性](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/version-compatibility) · [PyInstaller 平台限制](https://pyinstaller.org/en/stable/)

两种构建方式都会在包内生成 `seriald.exe.sha256`；安装时先核对 EXE 与清单，再备份旧程序文件，更新失败时回滚；`data\` 始终留在安装目录。清单用于检测包内文件不一致，IT 审批须从可信渠道独立确认摘要。若回滚本身失败，
错误会给出临时备份目录，供手动恢复。

## 升级与 IT 防火墙审批

普通升级：关闭正在运行的 seriald 窗口，执行 `git pull --ff-only`，重新构建并运行 `Install-seriald.cmd`，再从 `seriald-installed\` 启动。安装脚本保留 `data\`，并在失败时恢复旧程序；COM 口在升级期间会短暂释放。不能在运行中的 Windows 单进程上无中断替换 EXE。IT 管理的升级可向安装脚本传 `-ExpectedSha256 <IT批准的EXE摘要>`，在替换旧程序前再次核对。

需要局域网入站访问时，请 IT 审核脚本并将其置于 IT 管理的位置，再在**管理员 PowerShell** 中运行。IT 应从可信的构建记录核对 EXE 的 SHA-256；脚本拒绝与批准摘要不同的程序，只创建与 `seriald.exe` 的绝对路径、TCP 端口、专用/域网络及本地子网匹配的规则。seriald 本身仍以普通用户运行。

```powershell
$root = 'C:\path\to\seriald-source'
$approvalScript = 'C:\IT\approved\Approve-seriald-Firewall.ps1'
$hash = '由 IT 独立核实的 64 位 SHA-256'
& $approvalScript -InstallDir "$root\seriald-installed" -Port 8898 -ExpectedSha256 $hash
& $approvalScript -Action Verify -InstallDir "$root\seriald-installed" -Port 8898
```

如同事位于另一网段，IT 可用 `-RemoteAddress 10.0.120.0/21` 指定范围。撤销时用 `-Action Remove -Port 8898`。每次升级 EXE 都会改变摘要：应在升级前移除旧规则，安装后让 IT 用新摘要重新批准。Windows 防火墙规则本身按**路径**匹配，并不会在每次连接时重新计算哈希；若需要防止用户在批准后替换 EXE，应由 IT 把程序安装在普通用户不可写的受保护目录并管理升级。本仓库默认的 `seriald-installed\` 便于开发，但不提供持续的哈希强制执行。[Windows 防火墙规则参数](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)

安装目录提供三个启动入口，同一时间只运行其中一个。源码目录只有构建与安装入口；
`Start-seriald*.cmd` 只存在于打包产物 `dist\seriald\` 和安装目录中。安装完成会打印三个启动脚本的完整路径。

| 入口 | 监听与访问 |
| --- | --- |
| `Start-seriald.cmd` | `127.0.0.1:8898`，本机无密码 |
| `Start-seriald-LAN-Open.cmd` | `0.0.0.0:8898`，局域网无密码；仅用于可信网络 |
| `Start-seriald-LAN.cmd` | `0.0.0.0:8898`，远程需要密码；本机浏览器和 agent 无密码 |

有密码入口首次运行会生成令牌，按 Windows 用户 DPAPI 加密存于 `data\lan-token.dpapi`，窗口显示浏览器登录用户名 `seriald` 和密码。网页顶部“分享访问”会自动选择上位机局域网 IP 并复制 24 小时有效的链接；访问者打开后获得浏览器会话，无须再次输入密码。分享链接具备完整操作权限，只发给可信的人。同机 agent 直接运行 `serialctl.cmd devices`，无需令牌。远程 agent 设置 `SERIALD_URL=http://<Windows-IP>:8898` 和 `SERIALD_TOKEN=<窗口中的令牌>`；也可把分享链接最后一段作为有效期内的 `SERIALD_TOKEN`。例如在远程 Windows 命令提示符中：

给 Claude Code、Codex 等 Agent 使用时，网页点击“分享给 Agent”，选择当前设备或全部设备并复制接入文本。
它会提供独立令牌及当前主机托管的 `serialctl.py`、`seriald-board` skill 下载地址。
Agent 令牌默认不限期，可在同一窗口撤销；授权记录保存在安装目录 `data\agent-grants.sqlite3`。
无密码局域网模式下，令牌范围只约束使用该令牌的请求，其他客户端仍可直接访问服务。

```cmd
set SERIALD_URL=http://192.168.1.20:8898
set SERIALD_TOKEN=分享链接最后一段或窗口令牌
py serialctl.py devices
```

远程 agent 只需复制仓库中的 `serialctl.py`，它不打开本机 COM 口。三个入口都不应直接暴露到公网；跨不可信网络需 TLS 代理。

不要在安装目录开发或直接在 `dist\seriald` 下运行测试；安装数据只保存在安装目录 `data\`。COM 口不能同时被其他串口软件占用；换板前在网页点击“准备换板”。

开发者可独立使用源码与临时 `SERIALD_BASE` 运行 `seriald_windows.py --no-browser`，但日常实机验收应先构建并安装，再通过安装目录的三个入口测试。PyInstaller 必须在 Windows 上生成 Windows 可执行文件；GitLab 不运行仓库中的 GitHub Actions 工作流。
