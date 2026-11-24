# 发布与依赖维护

## 发布新版本

版本号以 `seriald_core/version.py` 为准，标签必须是对应的 `v版本号`。
例如发布 1.14.0：

1. 在 [CHANGELOG.md](../CHANGELOG.md) 的 `## [Unreleased]` 下记录用户可见的变化，
   按需使用 `Added`、`Changed`、`Deprecated`、`Removed`、`Fixed`、`Security`。
   发布时更新 `__version__`，将待发布内容移到 `## [版本] - YYYY-MM-DD`，保留空的
   `## [Unreleased]` 并更新末尾的版本比较链接。日期使用实际发布日，同步安装示例中的版本号。
2. 按 [贡献指南](../CONTRIBUTING.md) 运行检查并提交改动。确认 main 的
   Linux、原生 amd64 包验收和 Windows 流水线通过，再给该提交打标签。
3. 推送标签至 GitHub。当前开发仓库的 GitHub remote 名为 `github`；
   从 GitHub 克隆的仓库通常是 `origin`，以 `git remote -v` 为准。

```sh
python3 tools/check_release_tag.py v1.14.0
python3 tools/release_notes.py /tmp/decklab-release-notes.md
git push github main
# 等待该提交的检查通过后执行：
git tag -a v1.14.0 -m "DeckLab 1.14.0"
git push github v1.14.0
```

### main 即公开分支

`main` 就是公开分支：其中每个提交、文件、提交信息和作者身份都会对外发布，每次提交并推送
（无论推到 GitHub 还是内部镜像）都等于一次对外发布。

- 内网地址、内部域名与主机名、凭据和令牌、内部仓库或基础设施结构说明，一律不得进入仓库，
  包括测试、文档、提交信息和二进制材料。测试和示例使用占位值（`example.test`、文档地址段、
  运行时生成的假凭据）。
- 推送前必须通过 `tools/check_public_push.py` 扫描。每个克隆执行一次
  `sh tools/install_hooks.sh`，它为所有 remote 安装 pre-push 闸门；不得用 `--no-verify` 绕过。
  手工复核待推送范围可运行 `python3 tools/check_public_push.py github/main..HEAD`。
  检查当前文件树里仍在发布的内容可运行 `python3 tools/check_public_push.py --tree HEAD`。
- 扫描命中时删除相应内容并改写尚未推送的提交。`tools/public_push_allowlist.txt` 只收录
  经复核的占位值或早已公开的值；内部域名模式只放在本地不入库的 `.git/info/public-push-deny`，
  规则本身也不能写进仓库。
- 一旦推上 GitHub，只能删除整个仓库才能撤回；force push 或改写历史无效，fork、缓存和按提交号
  访问仍能取到原内容。CI 中的 `public_scan` 作业只是事后检查，真正的闸门是 pre-push。

### 单一 main

仓库只保留一个长期分支 `main`，开发与发版都在其上进行，GitHub `main` 必须能
快进到本地 `main`，不得强制推送。

- 不建 `release/*`、`decklab-x.y.z` 等按版本命名的分支；发版就是在 `main` 的
  提交上打 annotated 标签 `v版本号`（`git tag -a`），不用轻量标签。
- 需要隔离的发布验收目录时使用 detached worktree，发版完成后立即删除：

  ```sh
  git worktree add --detach build/publish-1.14.0 v1.14.0
  # 验收、上传附件……
  git worktree remove build/publish-1.14.0
  ```

- 短期任务分支合入 `main` 后即删除。其他镜像 remote 同步 `main` 后逐个推送
  新标签，不使用 `git push --tags`，本地归档标签不推送到任何 remote。

标签推送触发 `Tagged packages`：amd64 在原生 Ubuntu 22.04 runner
构建 MCP 运行时和 deb，Windows 任务构建安装包和便携 ZIP。两平台任务
都通过后才汇总附件，验证版本、运行时架构、逐文件摘要、第三方清单与校验和，
然后创建带附件的 **Release 草稿**。普通代码推送提供
`seriald-linux-amd64` 和 `seriald-windows` Actions Artifacts，
不创建正式 Release。不要移动已发布标签；修复应使用新版本号。

### Debian 构建与草稿验收

每个架构先运行 `tools/build_linux_mcp.py`，再将生成目录通过 `--runtime` 交给
`tools/build_deb.py`，完整命令见 [安装说明](INSTALL.md)。运行时以 Ubuntu 22.04 /
glibc 2.35 为基线，包含私有 Python、MCP SDK 与全部 Python 依赖；原生扩展必须
与目标架构一致。未传 `--runtime` 时仅查找本机架构的默认目录；缺失或校验失败
会停止构建，不允许发布缺少 MCP 的薄包。

### 完整单机安装

1.9.0 历史正式包不包含执行器和 RAM 材料，保持原附件不变。1.10.0 起构建默认要求完整材料，见 [完整包构建](COMPLETE_INSTALL.md)。正式标签流水线按平台下载经过审核、固定摘要的输入 ZIP，缺少输入时停止构建，不能自动生成精简包。1.14.0 已获 goflash 二进制及 RAM 材料的独立公开分发授权；不附 goflash 源码，保留各组件许可证，RAM U-Boot 对应源码另附。

本地完整测试包可显式使用固定材料和版本文件名，直接输出 `dist/`。Windows 在原生构建机生成安装器并验证 USB 驱动签名。普通软件 CI 和标签构建都使用固定公开组件输入；精简包只能显式请求，不能冒充完整安装包。

包含执行器的安装包启动后会自动发现本机 USB 下载设备；普通手动下载模式刷机
无需先登记串口序列号或手写板卡配置。单文件 NOR 刷写还需匹配的预装 RAM 材料。
外部复位控制、自定义板型或固定接线使用
`/usr/share/doc/seriald/examples/flash.json` 及 [硬件扩展说明](HARDWARE_EXTENSIONS.md#本机刷机流程)
配置；管理员设置 `SERIALD_FLASH` 后仍优先使用显式配置。
本地测试包不打发布标签、不上传 GitHub，也不修改 Latest；要公开分发执行器时另行确认其来源与许可。

CI 先复用 runner 已有的 BusyBox/Vim，缺失时才安装；真实 ash/PTY 和 Vim 检查仍执行。
Ubuntu 包任务通过 `tools/ci_ubuntu_network.py --repair` 检查软件源解析。该工具默认只诊断；
修复模式只允许在 GitHub-hosted 临时 runner 中运行，先核对普通用户与 `_apt` 的解析，
再验证公共 DNS 的实际响应，满足条件后才用 `resolvectl` 调整临时解析器并复验。
它不替换 APT 源、关闭签名校验或复用未经本次验证的安装包。失败时保留作业日志，
区分网络预检与应用测试失败，不直接重复触发构建。

Linux CI 对每个架构在 Ubuntu 22.04 和 24.04 容器中安装候选包并从固定 SHA-256 的
已发布 1.7.0 `all` 包升级。安装系统依赖后断开容器网络，以 `seriald` 服务账户运行
已安装的 collector/control，系统 Python 不安装 SDK，也不设置 `SERIALD_MCP_PYTHON`。
验收覆盖公开 `/mcp` 的授权、工具调用与撤销，独立重启 control，保留配置和运行数据，
以及 collector 指纹不变时保留进程、变化时请求重启，并核验凭据及私有备份在重装后保留。
容器不访问真实串口，不验证 systemd 激活或 USB recovery；
候选包仍需按本次改动范围补充真实安装与硬件验收。

发布前下载草稿的全部附件，对下载内容再次执行：

```sh
python3 tools/verify_release.py /path/to/downloaded-draft --version 1.14.0
```

确认 amd64 版本化 deb、下载别名、Windows 安装包/ZIP、运行时和第三方清单均齐全，
再在 Releases 打开该草稿并点击 **Publish release**，设为 Latest。流水线绿色和草稿创建
不代表已经正式发布；发布后还应核对 Latest 指向及固定下载地址的实际内容。

草稿同时提供固定文件名的下载别名，内容必须与同次发布的
版本化安装包完全一致，且一起纳入 `SHA256SUMS.txt`：

- Windows：`seriald-windows-x64-setup.exe`。
- Ubuntu amd64：`seriald_latest_amd64.deb`。
- 兼容旧链接：`seriald_latest_all.deb` 与 amd64 包相同，包内架构为 `amd64`；不表示支持任意架构。

对外文档使用 [Latest 发布页](https://github.com/qianqiu-2020/decklab/releases/latest)，
或 `https://github.com/qianqiu-2020/decklab/releases/latest/download/<固定文件名>`。
这些地址跟随 GitHub 的 Latest Release；发布草稿不会改变它们。验证当前标签
草稿的所有附件后，再正式发布并设为 Latest。历史版本仍可通过原来的版本化文件名获取。

组件输入先发布为 `components-1.14.0` 的非 Latest 附件，包含两平台 ZIP、SHA256SUMS 和 U-Boot 对应源码。审核 ZIP 中不得含私有主机地址、凭据、运行数据或 goflash 源码；组件 URL/摘要固定后再推主程序提交。新版本不得覆盖既有组件附件或移动标签。1.14.0 仅交付 Linux x86_64 / Windows x64，RAM 仅含 K3；历史 ARM 代码保留。

## 第三方依赖

应用保留上游库原样，优先更新自己的调用代码。前端资源在 `vendor/`，
Windows Python 依赖范围在 `requirements-windows.txt`，许可证及对应源码
要求见 [THIRD_PARTY.md](../THIRD_PARTY.md)。Linux Debian 包的系统依赖
由包管理器安装；MCP 的 Python、SDK 和 Python 依赖随包交付，不在安装脚本中下载。

Linux 冻结依赖在 `packaging/linux-mcp-requirements.lock` 中按版本与 wheel 哈希锁定。
构建记录 Ubuntu/Python 基线、架构、适配器源码摘要、运行时文件清单和第三方声明；
deb 组装和发布验收都重新核对这些内容。更新依赖或适配器后必须重新生成运行时，
不能复用与当前源码摘要不一致的 bundle。本轮只构建并验收 Linux x86_64 与 Windows x64，不要求 ARM。私有图形运行时使用 Python 3.12，保留 Ubuntu 22.04 / glibc 2.35 基线；构建容器的新版 Python 来自 deadsnakes PPA，该构建源不会写入用户系统。

当前 Windows 依赖文件使用版本范围，尚不是完整锁文件。打包会记录实际
版本、文件摘要和许可证，并校验已审查的原生二进制；依赖变化可能使构建
停止，必须先更新审查资料，而不是跳过校验。

升级依赖时应单独提交：检查上游变更及许可证，更新源码/许可证和原生摘要
目录，运行回归及 Windows 打包，检查生成清单，再验证安装升级。不要只改
摘要而省略内容审查。Windows 后续可增加完整版本及哈希锁文件，让依赖升级显式进行。
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
