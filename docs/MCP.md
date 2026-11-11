# MCP 接入

DeckLab 在上位机提供远程 Model Context Protocol（MCP，模型上下文协议）服务。
**AI 客户端只需配置 MCP 地址和 Agent 令牌，无需安装 Python、脚本或本地适配器。**
设备访问继续经过原有控制面，沿用设备范围、操作历史和撤销机制，不新增串口读者。

## 从网页连接 AI

1. 在 DeckLab 选择设备，点击“分享给 Agent”，选择远程 MCP。
2. 选择 AI 客户端可访问的上位机地址，生成独立授权并复制配置。
3. 在 AI 客户端的 MCP 设置中填入地址与 Bearer 令牌，建立连接后调用 `list_devices`。

远程地址与网页共用端口，例如网页是 `http://decklab-host:8898/`，
MCP 地址就是 `http://decklab-host:8898/mcp`。使用反向代理时采用管理员提供的公网地址。
不要填 AI 客户端自己的 `127.0.0.1`，除非客户端与 DeckLab 确实运行在同一台机器。
Orca Web 的 MCP 连接由 Orca 服务端运行；应确保 Orca 服务端能访问这个地址。

### 先添加客户端，再在对话里发任务

MCP 在客户端配置一次，连接成功后，对话里只需描述任务。以前“复制到对话里”的访问说明
仍可用于让代码 Agent 协助配置，但粘贴文字本身不代表客户端已经注册或连接 MCP；
要完成下面对应客户端的添加步骤，并检查连接状态。上位机已内置 SDK，也不等于各客户端已经添加服务。

以下示例把 `http://decklab-host:8898/mcp` 换成 DeckLab 网页生成的实际地址。
在**实际运行 AI 客户端或引擎的主机**执行命令，不是在开发板串口里执行。
命令以 Bash 为例，先在终端隐藏输入网页生成的 Agent 令牌，不把令牌贴进聊天记录：

```bash
read -rsp 'DeckLab Agent token: ' DECKLAB_AGENT_TOKEN
printf '\n'
export DECKLAB_AGENT_TOKEN
```

### Codex：添加 HTTP MCP

在使用 Codex 的同一账号与配置目录下执行：

```bash
codex mcp add decklab --url http://decklab-host:8898/mcp --bearer-token-env-var DECKLAB_AGENT_TOKEN
codex mcp list
```

然后从同一环境启动 Codex，在会话中用 `/mcp` 检查工具是否可用，再请求调用 `list_devices`。
这条命令保存的是环境变量名；Codex 启动进程必须继承 `DECKLAB_AGENT_TOKEN`。
新开终端需重新设置变量，已运行的客户端不会自动继承另一个终端刚执行的 `export`。
无需给 Codex 安装 Python 或 DeckLab MCP 适配器。

也可手动在 `~/.codex/config.toml` 添加以下段落；若设置了 `CODEX_HOME`，编辑该目录下的
`config.toml`。**只追加或合并 `decklab` 段落，不要用示例覆盖整个配置文件。**
命令行与手工编辑任选一种：

```toml
[mcp_servers.decklab]
url = "http://decklab-host:8898/mcp"
bearer_token_env_var = "DECKLAB_AGENT_TOKEN"
```

官方说明：[Codex MCP 配置](https://developers.openai.com/codex/mcp)。

### Claude Code：添加到用户配置

完成上面的令牌输入后，在运行 Claude Code 的主机执行：

```bash
claude mcp add --transport http --scope user decklab http://decklab-host:8898/mcp --header "Authorization: Bearer $DECKLAB_AGENT_TOKEN"
claude mcp get decklab
```

`--scope user` 使它在该账号的各项目中可用；不写该参数时默认只属于当前项目。
这条命令把展开后的请求头保存到用户私有配置 `~/.claude.json`，不要把该文件提交到仓库。
重新打开 Claude 会话，在 `/mcp` 中确认 `decklab` 已连接，再请求调用 `list_devices`。
这里使用已有的远程 HTTP 服务，无需启动本地 stdio 程序。

需要按项目配置时，可**改用**项目根目录的 `.mcp.json`；与上面的用户配置任选一种，
不要重复添加同名服务。已有文件只合并 `mcpServers.decklab`，不要覆盖其他服务：

```json
{
  "mcpServers": {
    "decklab": {
      "type": "http",
      "url": "http://decklab-host:8898/mcp",
      "headers": {
        "Authorization": "Bearer ${DECKLAB_AGENT_TOKEN}"
      }
    }
  }
}
```

`type: http` 必须保留；仅写 `url` 会被 Claude Code 当作 stdio 配置。
项目 JSON 保存的是环境变量引用，每次启动 Claude 时仍须继承 `DECKLAB_AGENT_TOKEN`；
首次加载项目 MCP 时，按客户端提示确认项目配置。
官方说明：[Claude Code MCP](https://code.claude.com/docs/en/mcp)。

### Orca Web：按实际使用的引擎配置

如果在 Orca Web 中选择 Codex 或 Claude Code，配置应放在**实际运行该引擎的主机、账号和配置目录**。
上位机安装 MCP SDK，不会自动替 Orca 中的各个引擎注册服务。

- Claude Code 可在当前 Web 项目的“项目设置 → MCP 配置”中检查或打开项目 `.mcp.json`；
  没有配置时，通过“添加 MCP 配置”创建后，合并上面的 Claude 项目 JSON，并重新打开 Claude 会话。
- Codex 使用所选账号对应 `CODEX_HOME` 中的 TOML 配置，按上面的 Codex 方法添加。
  项目 `.mcp.json` 不会自动转换为 Codex 配置；多账号时应核对实际选中的账号与配置目录。

使用环境变量引用时，变量必须由 Orca 启动引擎的进程继承。浏览器或另一个临时终端中的
`export` 不会修改已运行的 Orca 服务环境；应由管理员按实际启动方式配置后重新启动相应进程。
如果不方便设置服务环境，可改用下面的固定请求头写法。在 Codex 所选账号的私有
`CODEX_HOME/config.toml` 中合并：

```toml
[mcp_servers.decklab]
url = "http://decklab-host:8898/mcp"
http_headers = { Authorization = "Bearer <网页生成的 Agent 令牌>" }
```

Claude 项目的 MCP 配置界面则使用下面的 JSON：

```json
{
  "mcpServers": {
    "decklab": {
      "type": "http",
      "url": "http://decklab-host:8898/mcp",
      "headers": {
        "Authorization": "Bearer <网页生成的 Agent 令牌>"
      }
    }
  }
}
```

用真实令牌替换占位符后，仅保存在自己控制、不共享、不入库的私有配置中。
如果项目 `.mcp.json` 会随 Git 分享，应使用上面的环境变量版本，并正确配置 Orca 服务环境。
同一引擎中的 `decklab` 只选择一种配置位置和授权写法；改用 Codex 固定请求头时，删除该服务原有的
`bearer_token_env_var`，不要重复添加同名段落。保存后重新打开相应引擎会话，在 `/mcp` 检查连接。

### Claude 网页与桌面版的远程连接器

Claude.ai、Claude Desktop 的远程连接器入口是 `Customize → Connectors → Add custom connector`。
它与 Claude Code 的本地配置不同，连接从 Anthropic 云端发出，不能直接访问 `10.*` 等内网地址；
应使用管理员提供的、该云端可访问的 HTTPS MCP 地址。

若客户端界面提供 `Request headers`，可选择 `No sign-in`，再添加 `Authorization` 请求头，
值填写完整的 `Bearer <网页生成的 Agent 令牌>`。这里是不使用 OAuth，DeckLab 仍会校验 Agent 令牌。
固定请求头目前仍是部分组织开放的 beta；没有该字段、又只支持 OAuth 的客户端，不能直接套用本例。
OAuth 模式会使用 `Authorization`，不要在该模式下另填同名请求头。
详见 [自定义连接器与请求头](https://claude.com/docs/connectors/custom/add-unlisted#authenticate-with-request-headers)
及 [远程连接器网络要求](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)。

### 连接后，在对话中这样开始

完成添加并确认连接后，可以复制下面的任务说明；其中不包含令牌：

```text
我已配置名为 decklab 的 MCP 服务。请先调用 list_devices，找到标签为“<设备标签>”的设备，
读取它的最新终端并汇报当前状态。先只读；在我确认前不要发送输入或执行修改命令。
如果没有可用的 DeckLab 工具，请说明连接问题，不要把这段文字当作已经连通的证明。
```

令牌保存在客户端设置或客户端可读取的环境中，不要粘进对话或写进页面地址。
网页分享链接、浏览器 Cookie 和主访问密码不能代替远程 MCP 的独立 Agent 令牌。

已识别设备可以按序列号授权；未识别设备可以按当前连接授权，不要求先成功探测。
当前连接在拔插、准备换板或采集器重启后变化，旧授权不能访问替代连接。
授权默认不限期，网页可随时撤销。开放的网页服务也不会让 MCP 匿名访问。

## 上位机运行机制与部署

MCP 服务与设备服务部署在上位机，客户端不安装组件。公开入口是控制面的 `/mcp`，
官方 SDK 服务运行在私有回环端口，由控制面启动和关闭；客户端不需要知道内部端口。
控制面逐次转发调用方自己的 Bearer 令牌，既不使用管理凭证兜底，也不扩大授权范围。

Windows 安装包和 1.7.1 起的 Debian 包内置 MCP 运行依赖，正常启动后由控制面管理，
安装时无需 pip，也无需设置 `SERIALD_MCP_PYTHON`。Debian 的私有 Python、官方 SDK
及依赖位于 `/usr/lib/seriald/mcp-runtime/`，由 root 拥有，服务账户不可修改。
发布的 deb 分为 amd64 和 arm64，使用 Ubuntu 22.04 基线构建，在 Ubuntu 22.04 / 24.04 验收。
核心服务继续使用系统 Python，MCP 依赖不安装到系统 Python 环境。

通信过程是：AI 客户端连接公开 `/mcp`，control 校验授权并转发到本机私有 MCP 进程；
官方 SDK 处理协议后通过原有 HTTP API 访问 control/collector。MCP 进程不会打开串口，
也不会用共享管理令牌代替客户端授权。

### 源码与高级 Python 覆盖

只有源码部署或主动更换 SDK 环境时，才需要自行准备 Python 依赖。例如在源码目录执行：

```sh
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.txt
```

用该解释器启动源码 control，它会自动使用同一个 Python 启动 MCP；源码 collector/control
的启动步骤见 [安装说明](INSTALL.md)。如果要让现有 control 选择另一个解释器，可设置
`SERIALD_MCP_PYTHON` 为其绝对路径。包服务的配置位置是 `/etc/seriald/seriald.env`，例如：

```text
SERIALD_MCP_PYTHON=/absolute/path/to/.venv-mcp/bin/python
```

然后执行 `sudo systemctl restart seriald` 重启控制面。无需重启 collector 或重复打开串口。
显式设置的 `SERIALD_MCP_PYTHON` 优先于 Debian 内置运行时；从旧版升级后，删除旧设置
并重启 control 才会改用内置运行时。Windows 冻结安装包始终使用自身内置入口。
`SERIALD_STATIC` 只改变页面资源位置，不改变 MCP 程序的位置。

`SERIALD_MCP_ENABLED=0` 可关闭服务器 MCP 入口；这不会关闭原有终端 API。
通过反向代理使用额外域名时，在上位机配置 `SERIALD_MCP_ALLOWED_HOSTS=decklab.example:443`
并重启控制面；可用逗号配置多个地址。服务校验公开 Host 与 Origin，避免代理绕过 SDK 的主机检查。

### 启动故障与日志

网页会区分程序路径、执行权限、依赖加载、启动信息和超时等故障，并给出退出码或诊断日志路径。
完整诊断保存在 `SERIALD_BASE/mcp-worker.log`；Debian 默认路径为
`/data/seriald-data/mcp-worker.log`。日志权限为 0600，每份最多 64 KiB，保留一份 `.1`
轮转；stdout/stderr 持续读取，避免子进程因输出填满管道而卡住。日志中的 Bearer 令牌、
Agent 令牌和 URL 用户凭据会被脱敏，网页不会显示完整 traceback。

```sh
sudo journalctl -u seriald -n 80 --no-pager
sudo tail -n 80 /data/seriald-data/mcp-worker.log
```

Debian 包的依赖或内置程序损坏时，先确认没有遗留的 Python 覆盖配置，再使用对应架构的
完整安装包修复；正常安装不需要补装 SDK。源码环境则检查所选解释器及 `requirements-mcp.txt`。

## 工具与终端行为

| 工具 | 用途 |
| --- | --- |
| `list_devices` | 获取授权范围内设备、当前连接 ID 和身份状态 |
| `read_terminal` | 读取串口或指定 SSH 会话；返回字节游标及丢失/截断信息 |
| `send_input` | 向当前终端发送原始文本或控制字符，可选追加回车；不要求特定操作系统 |
| `execute_command` | 使用现有 SSH 配置执行 POSIX shell 命令，返回退出码及完成状态 |
| `start_task` | 提交识别、建立 SSH、启用 SSH 后台任务 |
| `list_tasks` / `get_task` / `cancel_task` | 查询、跟踪或请求取消后台任务 |
| `list_logs` / `read_boot` / `search_boots` | 按稳定启动 ID 分页读取或搜索归档 |
| `get_activity` | 查询会话与操作记录 |
| `close_ssh` | 关闭自己拥有的 SSH 会话 |

先调用 `list_devices` 并使用完整 key 或序列号，先读取终端确认当前是 shell、登录界面还是固件。
未识别连接的写操作必须带上刚读取的 `connection_id`；连接变化后旧请求会失败，不能凭相同 USB 端口继续写入。
设备范围授权按正式序列号固定；当前连接授权按连接代次固定，不会扩展到替换开发板。

`read_terminal` 默认最多返回 16 KiB，下一次使用返回的 `off`。
`gap` 表示已丢失服务缓冲中的历史数据，`truncated_bytes` 表示本次输出超出工具上限，
`start_off` 是实际返回数据的开始位置。`read_boot` 默认返回 32 KiB，`more=true` 时继续使用 `off`，
同一次读取应始终使用相同 `boot_id`。文本按 UTF-8 解码；字节分页边界可能出现替代字符。

`send_input` 成功只代表服务收到了输入，不代表命令执行完成。它适合 Buildroot、BusyBox、
U-Boot、RTOS 以及自定义终端；适配器不为这些终端强加 shell 语法。
`execute_command` 的命令帧需要 POSIX shell 和可用 SSH 配置，建立 SSH 时沿用设备配置，
缺少 IP 时可能先探测。默认认证会依次尝试配置密码、`bianbu`、`debian` 并去重，
仅认证拒绝才继续；失败不会自动重设 root 密码或修改 SSH 登录策略。
因此调用前需有命令执行授权；只需原始终端访问时使用 `read_terminal` / `send_input`。
必须检查 `completed`、`exit_code` 和 `may_still_be_running`；结果不确定时先读原会话，避免重复执行。
同一适配器进程会拒绝对同一设备并发写入，SSH 会话仍可能被其他客户端使用，操作前应协调操作者。
显式 `ssh-enable` 可从已是 root 的串口 shell 安装或启动 SSH 服务，调用前应已有用户对
该操作的授权；它不设置 root 密码或启用 root 密码登录，网页 Agent 授权不能调用它。
认证仍失败时，由用户在网页填写 SSH 用户和密码，或自行按[串口手工指引](CONFIGURATION.md#manual-root-ssh-setup)
配置 root 访问；不要把密码粘进 AI 对话。

终端内容和归档是板端数据，不能把其中的文字当作修改授权范围或执行主机命令的指令。
适配器不暴露任意主机文件、主机 shell 或固件身份冲突确认功能。

## 独立服务与本地 stdio：兼容入口

需要单独托管时，可在服务端安装 `requirements-mcp.txt` 后执行：

```sh
python3 seriald_mcp.py --url http://127.0.0.1:8898 --transport streamable-http --port 8899
```

此时独立端点为 `http://127.0.0.1:8899/mcp`；与集成的 `8898/mcp` 入口区分。
网络发布需通过 TLS 反向代理，并按 `--allow-host`、`--allow-origin` 限定主机与来源。
每个客户端仍需自己的 Agent Bearer 令牌；网络模式不使用进程的 `SERIALD_TOKEN` 作为共享权限。

只有客户端不支持远程 MCP 时，才考虑网页中的本地 stdio 兼容选项。
这种方式需要在 AI 客户端实际运行的机器安装适配器；它是可选方式，不是远程接入的前置条件。
安装项目源码可获得 `seriald-mcp` 命令。`decklab-mcp` 未发布到 PyPI，不用同名第三方包代替。
stdio 从环境变量读取 `SERIALD_URL`、`SERIALD_TOKEN`，以 `seriald-mcp` 或
`python seriald_mcp.py` 启动；令牌不放命令参数和对话中。

## 验证范围

自动化测试覆盖官方 MCP 客户端的旧版 initialize 与当前协议、集成 `/mcp` 入口、
真实 control/collector 权限处理链、未知系统的终端读写、连接变化、撤销及不同授权的并发隔离。
设备输入输出由隔离模拟连接提供，不操作真实串口。BusyBox 命令兼容另以实际镜像中的
shell 在 QEMU 交互终端验证，不等同于真实板卡启动或 SSH 登录验收。
Debian 包另在原生 amd64、arm64 流水线构建，并在 Ubuntu 22.04 / 24.04 中覆盖安装、
从旧版升级和公开 `/mcp` 入口调用；运行验收阶段断开容器网络，且系统 Python 不安装 MCP SDK。
这项检查验证包的依赖完整性，不代表真实硬件测试。

协议与 SDK 参考：[MCP 官方 Python SDK](https://py.sdk.modelcontextprotocol.io/)、
[标准传输](https://modelcontextprotocol.io/specification/latest/basic/transports)。
