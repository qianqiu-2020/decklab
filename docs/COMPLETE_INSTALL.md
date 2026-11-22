# 完整包构建

1.10.0 起 Linux 和 Windows 构建入口默认生成完整单机安装。构建机提前准备刷机执行器、匹配的 RAM 加载材料、许可证；Windows 还需官方 USB 驱动和有合法发布者签名的 `driverinstall.exe`。安装时不从源码仓库下载刷机组件，不运行 pip。Linux 的发行版依赖由 apt 安装，Windows 内置应用运行时。

goflash 是独立的非开源组件，DeckLab 的 Apache-2.0 不授予其源码或二进制再分发权。本次公开完整包已获 goflash 二进制及 RAM 材料的独立分发授权，不附 goflash 源码。RAM U-Boot 的对应源码、构建配置和许可证另行提供；FSBL 和 USB 驱动遵守各自许可。

材料仅是构建输入，放在 `build/install-components/<平台>/`，正式版交付平台为 `linux-amd64`、`windows-amd64`。主程序不要求用户填写包内路径；已有显式外部组件设置继续优先，便于已有部署升级。

`manifest.json` 示例：

```json
{
  "version": 1,
  "platform": "linux-amd64",
  "executor": {"path": "decklab-flash", "sha256": "64位十六进制摘要"},
  "loaders": {"path": "flash-loaders", "files": {"manifest.json": {"size": 123, "sha256": "摘要"}}},
  "notices": {"path": "notices", "files": {"LICENSE": {"size": 123, "sha256": "摘要"}}}
}
```

`files` 必须包含相应目录全部文件的实际长度和摘要，可由 `tools.install_components.tree_inventory` 生成。Windows 增加 `driver` 同样结构，要求包含 `spacemit_usb.inf`、`WinUSB/spacemit_amd64.cat`、`driverinstall.exe` 和该驱动依赖的其他文件。构建离线核对精确清单、CPU 架构和 RAM 清单；缺少或改变材料立即失败。

Linux 构建机：

```sh
python3 tools/install_components.py --platform linux-amd64
python3 tools/build_deb.py --runtime build/linux-mcp/amd64/runtime --output dist
```

Windows 构建机：

```powershell
.\Build-seriald.cmd
# 或显式指定已准备清单：
.\tools\build_windows.ps1 -FlashComponents C:\inputs\manifest.json
```

Windows 完整安装提供勾选安装 USB 驱动和稍后安装入口。优先复用已安装的相同驱动；否则请求管理员确认，由已受信任的目录签名或官方签名的安装器安装。DeckLab 脚本不自行向系统根证书库导入证书。

仅做软件 CI 或明确不需要刷机时，Linux 使用 `--core-only`、Windows 使用 `-CoreOnly`，安装元数据明确标为 `core`。这种包不应作为完整单机安装交付。

正式版及软件 CI 都下载经过审核、固定 SHA-256 的 [1.14.0 组件输入](https://github.com/qianqiu-2020/decklab/releases/tag/components-1.14.0)。构建器校验基础归档后，使用当前标签中 `packaging/flash-components` 提供的执行器、K1/K3 加载器、许可文件及远程桌面桥接库。基础归档继续提供驱动和远程桌面依赖库。自定义摘要的完整归档使用其自带材料。ZIP 根目录包含 `manifest.json`、刷机材料和 `desktop-runtime/`；图形 SDK 另有逐文件清单。默认公开 URL 和摘要固定在工作流，不需要设置 secrets。组织可用同名 Actions variables 或 secrets 覆盖 `COMPONENTS_LINUX_AMD64_URL` / `COMPONENTS_LINUX_AMD64_SHA256`、`COMPONENTS_WINDOWS_AMD64_URL` / `COMPONENTS_WINDOWS_AMD64_SHA256`。下载必须 HTTPS，缺失或摘要不符立即停止，不回退精简包，也不从可变的 latest 地址构建。

在 Linux 构建机下载公开输入时，将工作流里的固定 URL 与 SHA-256 分别设为 `DECKLAB_COMPONENTS_URL` 和 `DECKLAB_COMPONENTS_SHA256`，运行 `python3 tools/fetch_install_components.py --platform linux-amd64`。然后执行 `python3 tools/build_linux_mcp.py --arch amd64 --desktop-runtime build/install-components/linux-amd64/desktop-runtime --output build/linux-mcp/amd64/runtime`，再按上文组装。Windows 同理使用 `--platform windows-amd64`，构建时传 `-DesktopRuntime .\build\install-components\windows-amd64\desktop-runtime`。

本地交付包直接放 `dist/`，使用版本文件名；不额外套交付目录。正式 GitHub 1.9.0 历史附件保持原样；1.14.0 提供 x86_64 Linux 与 Windows x64 完整包，预装 RAM 材料仅含 K3。

1.17.33 起 Linux amd64 与 Windows x64 完整包均内置 K1、K3 RAM 材料；1.17.38 起 K3 RAM U-Boot 另启用原生 `CONFIG_PCI_INIT_R`。K1 对应源码索取方式见随包的 `flash-loaders/k1/SOURCE-OFFER.txt`。
