# 发布与依赖维护

## 发布新版本

版本号以 `seriald_core/version.py` 为准，标签必须是对应的 `v版本号`。
例如发布 1.5.2：

1. 更新 `__version__` 为 `1.5.2`；将 CHANGELOG.md 的待发布内容整理为
   `## 1.5.2`，保留新的 `## Unreleased`。同步安装示例中的版本号。
2. 按 [贡献指南](../CONTRIBUTING.md) 运行检查并提交改动。确认 main 的
   Linux 和 Windows 流水线通过，再给该提交打标签。
3. 推送标签至 GitHub。当前开发仓库的 GitHub remote 名为 `github`；
   从 GitHub 克隆的仓库通常是 `origin`，以 `git remote -v` 为准。

```sh
python3 tools/check_release_tag.py v1.5.2
python3 tools/release_notes.py /tmp/decklab-release-notes.md
git push github main
# 等待该提交的检查通过后执行：
git tag -a v1.5.2 -m "DeckLab 1.5.2"
git push github v1.5.2
```

标签推送触发 `Tagged packages`：构建 Debian 包、Windows 安装包和便携 ZIP，
验证版本、第三方清单与校验和，然后创建带附件的 **Release 草稿**。
在 GitHub Actions 检查结果，下载候选包验证升级和实机串口，再在 Releases
打开该草稿并点击 **Publish release**。普通代码推送运行检查，并在 Actions
运行页面底部的 Artifacts 提供 `seriald-linux` 和 `seriald-windows` 测试包，
不创建正式 Release。不要移动已发布标签；修复应使用新版本号。

## 第三方依赖

应用保留上游库原样，优先更新自己的调用代码。前端资源在 `vendor/`，
Windows Python 依赖范围在 `requirements-windows.txt`，许可证及对应源码
要求见 [THIRD_PARTY.md](../THIRD_PARTY.md)。Linux Debian 包的系统依赖
由包管理器安装。

当前 Windows 依赖文件使用版本范围，尚不是完整锁文件。打包会记录实际
版本、文件摘要和许可证，并校验已审查的原生二进制；依赖变化可能使构建
停止，必须先更新审查资料，而不是跳过校验。

升级依赖时应单独提交：检查上游变更及许可证，更新源码/许可证和原生摘要
目录，运行回归及 Windows 打包，检查生成清单，再验证安装升级。不要只改
摘要而省略内容审查。后续可增加完整版本及哈希锁文件，让依赖升级显式进行。
Paramiko 的对应源码随 Windows 包提供；替换与重建步骤见
[Windows 重建指南](WINDOWS_REBUILD.md)。

## Rust 的位置

DeckLab 应用代码是 Python、HTML、CSS 和 JavaScript。Windows SSH 使用
Paramiko，其依赖中的 cryptography 和 bcrypt 带有已编译的 Rust 扩展。
包内 Rust 许可证属于这些上游组件的声明，并不意味着项目改用了 Rust。
正常使用预编译 wheel 打包或安装时不需要 Rust 工具链；自行从源码编译
这些上游扩展时才需要相应编译环境。

## 更新架构图

使用 Graphviz 和 Noto Sans CJK SC 字体，从同一份源文件生成两种格式：

```sh
dot -Tsvg docs/seriald-architecture.dot -o docs/seriald-architecture.svg
dot -Tpng docs/seriald-architecture.dot -o docs/seriald-architecture.png
dot -Tsvg docs/seriald-architecture-en.dot -o docs/seriald-architecture-en.svg
dot -Tpng docs/seriald-architecture-en.dot -o docs/seriald-architecture-en.png
```

检查 PNG 中的文字、箭头和连线后，一起提交 DOT、SVG 和 PNG。
两种语言保持相同的节点和数据流；英文首页使用英文图，中文 README 使用中文图。
运行 `node tools/demo_smoke.js` 会生成中英文截图 `dist/demo-overview.png` 和
`dist/demo-overview-en.png`；检查界面后，分别复制到 `docs/decklab-demo-zh.png`
和 `docs/decklab-demo.png`。该测试使用临时模拟服务，不连接真实开发板。
