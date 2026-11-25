# Windows 安装和升级

DeckLab 的 Windows x64 安装器包含主程序、网页、MCP 和刷机所需的运行时。下载并运行 [Windows 安装器](https://github.com/qianqiu-2020/decklab/releases/latest/download/decklab-windows-x64-setup.exe)，选择控制机上的安装目录。

在安装完成页启动 DeckLab，或从开始菜单选择本机访问。默认入口是 `http://127.0.0.1:8898/`。接入串口板卡后，在页内按 COM 端口或板卡名称查找。使用刷机 USB 时，需要板通过数据线进入下载模式，控制机还须能访问该 USB 接口。[设备身份](../modules/device-identity.md) · [硬件控制](../modules/hardware-control.md)

## 局域网访问

开始菜单可选本机、局域网有密码、局域网无密码。桌面快捷方式默认启动有密码的局域网模式。控制窗口保持打开，其他电脑使用窗口显示的访问链接。

若本机能打开网页而同事电脑打不开，先查两台电脑的连通性，再查 Windows 入站防火墙是否允许当前程序和端口。管理员可从安装目录用 PowerShell 读取规则：

```powershell
.\Get-decklab-LANFirewall.ps1 -Program "$PWD\decklab.exe" -Port 8898,8443
```

有兼容 USB 驱动时，安装器跳过驱动安装。需要另行安装时，使用开始菜单的「安装 USB 刷机驱动」，或由管理员安装与板卡匹配的已签名驱动。如果设备管理器枚举到下载设备而刷机工作区仍找不到，检查驱动绑定和其他刷机软件是否占用该接口。

## Upgrade an existing installation

运行新版安装器并使用原安装目录。升级保留该目录下的 `data\`，其中包含设备状态、设置、日志和任务。已登记的安装会沿用原位置。早期未登记的安装若在自定义目录，手动选中该目录。

安装器会检查正在运行的 DeckLab，在升级之后恢复可识别的本机或局域网启动方式。升级前结束活动任务。升级完成后打开网页确认版本、设备列表和一条历史记录。若网页没有恢复，使用开始菜单的对应入口，再查看启动窗口的错误。[文档目录](../index.md)
