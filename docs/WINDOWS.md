# Windows 上位机：构建、安装与访问

Windows 版独占接管本机 USB 串口的 COM 端口，提供网页与 `serialctl` agent API。Windows 需安装 Git、Python 3.12（含 `py` 启动器）；构建需要网络下载 Python 依赖，安装已构建的 ZIP 不需要 Python。

在源码目录双击 `Build-seriald.cmd`，或运行 `powershell -ExecutionPolicy Bypass -File .\tools\build_windows.ps1`。构建脚本创建 `.venv\`、运行 Windows 模拟测试，并在 `dist\` 生成 `seriald-windows-portable.zip`。然后双击 `Install-seriald.cmd`；它把包安装到 `%LOCALAPPDATA%\seriald`，与源码和测试目录分开，更新时保留其中的 `data\`。自选位置可运行 `powershell -ExecutionPolicy Bypass -File .\tools\install_windows.ps1 -InstallDir D:\seriald`。安装前先关闭旧的 seriald 窗口，释放 COM 口。

安装目录提供三个启动入口，同一时间只运行其中一个：

| 入口 | 监听与访问 |
| --- | --- |
| `Start-seriald.cmd` | `127.0.0.1:8898`，本机无密码 |
| `Start-seriald-LAN-Open.cmd` | `0.0.0.0:8898`，局域网无密码；仅用于可信网络 |
| `Start-seriald-LAN.cmd` | `0.0.0.0:8898`，远程需要密码；本机浏览器和 agent 无密码 |

有密码入口首次运行会生成令牌，按 Windows 用户 DPAPI 加密存于 `data\lan-token.dpapi`，窗口显示浏览器登录用户名 `seriald` 和密码。网页顶部“分享访问”会复制 24 小时有效的链接；访问者打开后获得浏览器会话，无须再次输入密码。分享链接具备完整操作权限，只发给可信的人。同机 agent 直接运行 `serialctl.cmd devices`，无需令牌。远程 agent 设置 `SERIALD_URL=http://<Windows-IP>:8898` 和 `SERIALD_TOKEN=<窗口中的令牌>`；也可把分享链接最后一段作为有效期内的 `SERIALD_TOKEN`。例如在远程 Windows 命令提示符中：

```cmd
set SERIALD_URL=http://192.168.1.20:8898
set SERIALD_TOKEN=分享链接最后一段或窗口令牌
py serialctl.py devices
```

远程 agent 只需复制仓库中的 `serialctl.py`，它不打开本机 COM 口。三个入口都不应直接暴露到公网；跨不可信网络需 TLS 代理。

更新时在源码目录执行 `git pull --ff-only`，再运行 `Build-seriald.cmd`、`Install-seriald.cmd`。不要在安装目录开发或直接在 `dist\seriald` 下运行测试；安装数据只保存在安装目录 `data\`。Windows 防火墙如阻止局域网访问，允许相应端口的专用网络入站 TCP。COM 口不能同时被其他串口软件占用；换板前在网页点击“准备换板”。

开发者可独立使用源码与临时 `SERIALD_BASE` 运行 `seriald_windows.py --no-browser`，但日常实机验收应先构建并安装，再通过安装目录的三个入口测试。PyInstaller 必须在 Windows 上生成 Windows 可执行文件；GitLab 不运行仓库中的 GitHub Actions 工作流。
