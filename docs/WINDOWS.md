# Windows 上位机：构建、安装与访问

Windows 版直接接管本机 COM 串口，提供网页终端与 Agent API。推荐分发 **Setup 安装包**：使用者不需要安装 Python，双击即可选择目录并安装。安装不要求管理员权限；局域网防火墙仍由 IT 单独审批。

## 安装与升级

首次运行 `seriald-1.4.48-windows-x64-setup.exe`，选择一个可写的安装目录。默认在安装包所在位置创建可见的 `seriald-installed/`；若安装包位于源码的 `dist/`，默认改用源码目录下的 `seriald-installed/`，与构建目录分开。首次从解压版迁移时，**选择原安装目录**，无需搬运数据。

后续双击新版本 Setup，安装器记住上次目录并覆盖程序文件，保留 `data/` 中的日志、设备档案和认证配置。升级前关闭 seriald 窗口以释放 COM 口；安装器也会检测占用的文件并提示关闭程序。安装完成从开始菜单或安装目录重新启动，三种模式任选其一。卸载仅移除安装的程序与快捷方式，保留运行数据。

## 构建安装包

原生构建需要 Git、**64 位 Python 3.12**（含 `py` 启动器）和 **Inno Setup 6.5 或更新版本**。先安装编译器，再在源码目录构建：

```powershell
winget install --id JRSoftware.InnoSetup -e -s winget -i
.\Build-seriald.cmd
```

构建会创建 `.venv/`、运行回归测试并输出：

- `dist/seriald-1.4.48-windows-x64-setup.exe`：常规安装与升级。
- 对应 `.exe.sha256`：安装包摘要，供可信渠道核对。
- `dist/seriald-windows-portable.zip`：便携备选。

仅需 ZIP 时运行 `.\Build-seriald.cmd -PortableOnly`，无需 Inno Setup。`Install-seriald.cmd` 保留为 **ZIP 安装脚本**：默认装到源码旁的 `seriald-installed/`，保留 `data/`，备份旧程序，替换失败时回滚；也支持 `tools/install_windows.ps1 -InstallDir D:\seriald`。它可复制旧 `%LOCALAPPDATA%\seriald\data`，但不会删除旧目录。IT 可传 `-ExpectedSha256 <批准的程序EXE摘要>` 进行校验。常规用户直接使用 Setup。

如果 Inno Setup 已安装但无法找到，构建脚本会从 PATH、用户/系统安装注册表及常见目录查找 `ISCC.exe`。也可显式指定实际路径。ZIP 已生成时，仅重跑安装器步骤，无需重新执行 PyInstaller：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_windows_installer.ps1
# 自选编译器目录时：
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_windows_installer.ps1 -Compiler 'D:\Tools\Inno Setup 6\ISCC.exe'
```

Windows 11 Pro/Enterprise 的 Docker Desktop 切换到 **Windows containers** 后，也可双击 `Build-seriald-Docker.cmd`，生成同样的 Setup 和 ZIP。容器使用 Windows Server Core、Python 3.12 及已校验发布者签名的 Inno Setup 编译器。Docker 只参与构建，程序在 Windows 主机运行；Linux/WSL2 容器不能交叉生成该 PyInstaller 程序。宿主机需与镜像兼容，首次构建下载较大。[Windows 容器兼容性](https://learn.microsoft.com/en-us/virtualization/windowscontainers/deploy-containers/version-compatibility)

Windows CI 还会验证：迁移已有目录、二次升级记住路径、数据保留及卸载保留数据。可在没有注册 seriald 安装的干净 Windows 测试账户运行 `tools/test_windows_installer.ps1`；测试不打开 COM 口。

## 升级与 IT 防火墙审批

自行构建时先 `git pull --ff-only`，重新构建并运行新版本 Setup。COM 口在升级期间会短暂释放，Windows 单进程不能无中断替换 EXE。IT 管理的安装位置应由 IT 统一升级。

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
`Start-seriald*.cmd` 只存在于打包产物 `dist\seriald\` 和安装目录中。Setup 会添加三个开始菜单快捷方式；ZIP 安装脚本会打印启动路径。

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
