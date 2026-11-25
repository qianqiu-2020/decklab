# 发布 DeckLab

DeckLab 的交付物是 Ubuntu amd64 Debian 包和 Windows x64 安装器。版本号来自 `decklab_core/version.py`，发布标签使用 `v<版本号>`。

## 选定本次发布提交

1. 更新版本号和 [CHANGELOG](../../CHANGELOG.md)。把用户可见变化写入本次版本，使用实际发布日期。
2. 按[贡献指南](../../CONTRIBUTING.md)运行检查，提交到 `main`。安装仓库的 pre-push 闸门，审核代码、作者信息及附件中允许公开的内容。
3. 推送 `main`，等待该提交的应用回归、Linux 安装和 Windows 安装验收结束。查验流水线提交号等于准备打标签的提交号。

```sh
sh tools/install_hooks.sh
git push github main
python3 tools/check_release_tag.py v1.19.0
git tag -a v1.19.0 -m "DeckLab 1.19.0"
git push github v1.19.0
```

仓库以 `main` 为长期分支。GitHub 的 `main` 通过快进更新。已发布版本有自己的标签和附件，后续修订使用新版本号。

## 构建与草稿

标签流水线并行运行 Linux 和 Windows 构建。Linux 验收覆盖 Ubuntu 22.04 和 24.04 的全新安装和旧版升级。Windows 使用安装器执行安装、升级和卸载，并验证已安装文件、USB 驱动材料及第三方声明。应用回归由同一个提交的主分支检查覆盖。

两平台通过后，流水线生成附带固定下载别名及 SHA-256 校验文件的 Release 草稿。先下载草稿附件再验收：

```sh
python3 tools/verify_release.py /path/to/draft-assets --version 1.19.0
```

验收通过后在 GitHub 发布并设为 Latest。检查版本化下载及两个固定下载别名，确认它们指向同一发布的包。已经公开的文件和标签由历史链接继续提供。[发布验收](release-acceptance.md)

## 本地打包与计时

在同一提交上同时启动两平台构建，记录从首个构建启动到最后一个必需产物准备好的时间。增量计时允许复用已验证的构建依赖和相同输入对应的冻结运行时，实际执行新版本打包。干净构建使用空输出目录并重新冻结运行时。若同时测试需要从空构建环境安装工具链，也记录这段准备时间。

Linux 传 `--local-fast` 使用快速 Debian 压缩。Windows 传 `-LocalFast`，将安装器所需的依赖与冻结运行时放在 `build/` 下按输入复用。软件资源从本次工作树重新加入。两个平台的出包命令、环境要求和输入验证见 [完整包构建](../development/complete-build.md)。单平台构建时间包含同平台的全部交付产物。

## 依赖和许可

应用遵循项目许可证。刷机执行器、板卡加载材料、MCP SDK、远程桌面库和 Windows 驱动分别有独立的来源和许可。默认发布流水线下载固定 SHA-256 的公开输入，用当前提交的材料覆盖允许更新的组件。缺失材料时构建会失败。[第三方声明](../../THIRD_PARTY.md) · [完整包构建](../development/complete-build.md)
