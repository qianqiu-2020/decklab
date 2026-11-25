# 完整包构建

完整安装包含 DeckLab、刷机执行器、K1/K3 临时加载器、远程桌面运行库、MCP 服务及第三方声明。Windows 安装器另含 USB 驱动。Linux 需要 Ubuntu 22.04 或更高版本的系统依赖，Windows 自带 Python 与 Node.js 运行时。

## 准备两平台的依赖

使用工作流固定的公开组件归档及 SHA-256。脚本校对归档内容和目标 CPU 架构，再套用当前提交中审查过的刷机材料。Linux 在 Ubuntu 22.04 的容器构建 MCP 服务，确保在最低支持版本上运行。Windows 在 x64 Windows 上使用 Python 3.12 和 Inno Setup 6。

把组件放在 `build/install-components/<平台>/`，本地 Debian 包和 Windows 安装器都输出到 `dist/`。运行目录与构建目录独立。

## 组件清单

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

## Linux amd64

在构建机准备并校对组件后，使用以下命令生成 MCP 运行时与 Debian 包。

```sh
python3 tools/build_linux_mcp.py --arch amd64 \
  --desktop-runtime build/install-components/linux-amd64/desktop-runtime
python3 tools/build_deb.py --local-fast --runtime build/linux-mcp/amd64/runtime --output dist
```

## Windows x64

本地快速出包复用依赖环境和冻结运行时。每次重新加入 DeckLab 的 Python 模块、网页和文档，并检查清单及打包后主程序的启动。给构建脚本加 `-ValidateRuntime` 可同时检查 MCP、远程桌面与命令行入口。

```powershell
.\Build-decklab.cmd
# 或显式指定已准备清单：
.\tools\build_windows.ps1 -FlashComponents C:\inputs\manifest.json
```

Windows 完整安装提供勾选安装 USB 驱动和稍后安装入口。优先复用已安装的相同驱动；否则请求管理员确认，由已受信任的目录签名或官方签名的安装器安装。DeckLab 脚本不自行向系统根证书库导入证书。

仅做软件 CI 或明确不需要刷机时，Linux 使用 `--core-only`、Windows 使用 `-CoreOnly`，安装元数据明确标为 `core`。这种包不应作为完整单机安装交付。

正式版及软件 CI 都下载经过审核、固定 SHA-256 的 [1.14.0 组件输入](https://github.com/qianqiu-2020/decklab/releases/tag/components-1.14.0)。构建器校验基础归档后，使用当前标签中 `packaging/flash-components` 提供的执行器、K1/K3 加载器、许可文件及远程桌面桥接库。基础归档继续提供驱动和远程桌面依赖库。自定义摘要的完整归档使用其自带材料。ZIP 根目录包含 `manifest.json`、刷机材料和 `desktop-runtime/`；图形 SDK 另有逐文件清单。默认公开 URL 和摘要固定在工作流，不需要设置 secrets。组织可用同名 Actions variables 或 secrets 覆盖 `COMPONENTS_LINUX_AMD64_URL` / `COMPONENTS_LINUX_AMD64_SHA256`、`COMPONENTS_WINDOWS_AMD64_URL` / `COMPONENTS_WINDOWS_AMD64_SHA256`。下载必须 HTTPS，缺失或摘要不符立即停止，不回退精简包，也不从可变的 latest 地址构建。

在 Linux 构建机下载公开输入时，将工作流里的固定 URL 与 SHA-256 分别设为 `DECKLAB_COMPONENTS_URL` 和 `DECKLAB_COMPONENTS_SHA256`，运行 `python3 tools/fetch_install_components.py --platform linux-amd64`。然后执行 `python3 tools/build_linux_mcp.py --arch amd64 --desktop-runtime build/install-components/linux-amd64/desktop-runtime --output build/linux-mcp/amd64/runtime`，再按上文组装。Windows 同理使用 `--platform windows-amd64`，构建时传 `-DesktopRuntime .\build\install-components\windows-amd64\desktop-runtime`。

## 干净构建与依赖许可

使用空的构建输出目录重新生成冻结运行时，再打包。Windows 可通过不带 `-LocalFast` 的脚本调用重新准备 Python 环境并运行完整验证。Linux 复用 Docker 的工具链层，MCP 运行时每次重新生成。首次联网取依赖的耗时取决于各依赖源。

goflash 独立于 DeckLab 的 Apache-2.0 许可证。完整包按组件授权公开 goflash 可执行文件、FSBL、RAM U-Boot 和 USB 驱动。K1 RAM U-Boot 对应源码的索取方式见安装包中的 `flash-loaders/k1/SOURCE-OFFER.txt`。
