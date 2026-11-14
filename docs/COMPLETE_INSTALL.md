# 完整包构建

1.10.0 起 Linux 和 Windows 构建入口默认生成完整单机安装。构建机提前准备刷机执行器、匹配的 RAM 加载材料、许可证；Windows 还需官方 USB 驱动和有合法发布者签名的 `driverinstall.exe`。安装时不从源码仓库下载刷机组件，不运行 pip。Linux 的发行版依赖由 apt 安装，Windows 内置应用运行时。

材料仅是构建输入，放在 `build/install-components/<平台>/`，平台为 `linux-amd64`、`linux-arm64`、`windows-amd64`。主程序不要求用户填写包内路径；已有显式外部组件设置继续优先，便于已有部署升级。

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
python3 tools/build_deb.py --runtime build/linux-mcp/amd64/runtime --output dists
```

Windows 构建机：

```powershell
.\Build-seriald.cmd
# 或显式指定已准备清单：
.\tools\build_windows.ps1 -FlashComponents C:\inputs\manifest.json
```

Windows 完整安装提供勾选安装 USB 驱动和稍后安装入口。优先复用已安装的相同驱动；否则请求管理员确认，由已受信任的目录签名或官方签名的安装器安装。DeckLab 脚本不自行向系统根证书库导入证书。

仅做软件 CI 或明确不需要刷机时，Linux 使用 `--core-only`、Windows 使用 `-CoreOnly`，安装元数据明确标为 `core`。这种包不应作为完整单机安装交付。

正式版流水线在组装前下载经过人工审核并固定 SHA-256 的构建输入 ZIP，ZIP 根目录是上述 `manifest.json` 及相对目录。为每个平台配置 GitHub Actions secrets：`COMPONENTS_LINUX_AMD64_URL` / `COMPONENTS_LINUX_AMD64_SHA256`、`COMPONENTS_LINUX_ARM64_URL` / `COMPONENTS_LINUX_ARM64_SHA256`、`COMPONENTS_WINDOWS_AMD64_URL` / `COMPONENTS_WINDOWS_AMD64_SHA256`。下载地址必须 HTTPS，缺少配置时正式构建停止，不能回退为精简包。材料更新需重新审核摘要，不能从不固定内容的“latest”地址直接构建正式包。

本地交付包直接放 `dists/`，使用版本文件名；不额外套交付目录。正式 GitHub 1.9.0 历史附件保持原样，完整包由后续版本提供。
