// UI-only translation. Never translate board output, commands or log contents.
(function (global) {
  'use strict';
  var dictionary = {
    'DeckLab · 开发板工作台': 'DeckLab · Board workbench',
    '开发板工作台': 'Board workbench',
    '开发板工作台 · 按标签访问，以序列号保存设备档案、终端记录与 agent 操作历史。': 'Access boards by label; retain archives, terminals and agent history by board serial number.',
    '创建可撤销的网页访问链接': 'Create a revocable web access link',
    '分享访问': 'Share access', 'Agent 接入': 'Agent access',
    '分享设备（可访问全部设备）': 'Share board (all-board access)',
    '修改设备标签（日志目录仍为设备序列号）': 'Rename board (archives remain under its serial number)',
    '生成可撤销的 Agent 接入令牌与指引': 'Generate a revocable agent token and instructions',
    '分享当前设备或全部设备给 Claude Code、Codex 等 Agent': 'Share this board or all boards with Claude Code, Codex or other agents',
    '连接中…': 'Connecting…', '在线设备': 'Online boards', '历史设备': 'Historical boards',
    '待识别串口': 'Unidentified ports', '搜索标签、序列号、主机名或串口位置': 'Search label, serial number, hostname or port',
    '搜索设备': 'Search boards', '设备状态': 'Board status', '全部设备': 'All boards',
    '历史 / 离线': 'Historical / offline', '在线': 'Online', '待识别': 'Unidentified',
    '正在扫描串口…': 'Scanning serial ports…', '识别设备': 'Identify board', '设备信息': 'Board info',
    '自动登录并读取板子序列号 / IP / sshd': 'Log in and read board serial number, IP and sshd status',
    '分享链接直接打开当前设备；网页访问授权覆盖整个平台': 'Open this board directly; web access covers the platform',
    '分享设备': 'Share board', '准备换板': 'Change board', '改标签': 'Rename',
    '在同一串口位置更换开发板前点击；旧身份立即离线，新日志先进入待识别目录': 'Use before replacing a board on this port; disconnect its identity and isolate new logs until identification',
    '执行 USB 软解绑与重新绑定，会暂时中断该设备连接': 'Rebind the USB adapter; temporarily interrupts this connection',
    '重新连接串口': 'Reconnect serial', '收起设备终端': 'Close board view',
    '终端内容搜索': 'Terminal search', '搜索范围': 'Search scope', '当前终端': 'Current terminal',
    '全部启动日志': 'All boots', '搜索终端 · Ctrl+F': 'Search terminal · Ctrl+F',
    '搜索终端内容': 'Search terminal contents', '按 Enter 搜索，Esc 清空': 'Enter to search, Esc to clear',
    '上一个匹配（Shift+Enter）': 'Previous match (Shift+Enter)', '下一个匹配（Enter）': 'Next match (Enter)',
    '上一个匹配': 'Previous match', '下一个匹配': 'Next match', '侧栏导航': 'Sidebar navigation',
    '打开串口日志归档': 'Open serial archives', '串口日志': 'Serial logs', '任务': 'Tasks',
    '查看后台任务进度与结果': 'View background task progress and results', '输入导航': 'Commands',
    '在当前终端缓冲中按 shell 命令跳转': 'Jump to a shell command in the terminal buffer', '终端工具': 'Terminal tools',
    '可选占用串口输入，其他人仍可查看输出': 'Reserve serial input; others can still view output',
    '占用输入': 'Reserve input', '释放输入': 'Release input', '接管输入': 'Take over input',
    '比较同一块或不同开发板的两次启动日志': 'Compare boots from the same or different boards',
    '启动对比': 'Compare boots', '文件传输': 'File transfer',
    '通过串口上传、下载或在开发板之间复制文件': 'Upload, download or copy files between boards over serial',
    '从采集缓冲重绘全屏终端': 'Redraw the terminal from captured bytes', '重绘': 'Redraw',
    '同步尺寸': 'Sync size', '在空闲 Linux shell 同步板端尺寸，启用宽屏串口': 'Sync board dimensions at an idle Linux shell to enable a wide serial terminal',
    '请在空闲 Linux shell 提示符处同步尺寸': 'Sync dimensions at an idle Linux shell prompt',
    '放大终端': 'Expand terminal', '放大': 'Expand', '还原工作台': 'Restore workbench', '还原': 'Restore',
    '键盘直通': 'Direct keyboard', '或在此整行输入后回车': 'Or type a command here and press Enter',
    '发送': 'Send', '只发一个回车（唤醒板子 / 看是否还活着）': 'Send Enter to wake the console',
    '发送 Ctrl-C': 'Send Ctrl-C', '粘贴剪贴板内容到会话': 'Paste clipboard into the session', '粘贴': 'Paste',
    'GRUB/固件接管串口输入后若批量粘贴丢字，可逐字符发送': 'Send one character at a time if firmware drops pasted characters',
    '固件慢速粘贴': 'Slow firmware paste', '介入此会话': 'Take over session',
    '允许从网页向当前 agent SSH shell 输入，并记录介入事件': 'Allow input into this agent SSH shell and record the takeover',
    '记录与归档': 'Records and archives', '关闭侧栏': 'Close sidebar', '记录分类': 'Record categories',
    '串口归档': 'Serial archives', 'SSH 会话': 'SSH sessions', '关键操作': 'Key events',
    '后台任务': 'Background tasks', '常用命令': 'Frequent commands', '串口日志归档': 'Serial log archives',
    '全部启动日志搜索结果': 'Search results across all boots', '上一页': 'Previous', '下一页': 'Next',
    '关闭搜索': 'Close search', '按日期': 'By date', '按启动': 'By boot', '终端输入导航': 'Command navigation',
    '当前终端缓冲中的 shell 命令，点击跳转到对应输入行': 'Click a command to jump to its input line in the terminal buffer',
    '↑ 上一条': '↑ Previous', '下一条 ↓': 'Next ↓', 'SSH 会话记录': 'SSH session history',
    '在线会话可实时查看，关闭后 transcript 仍保留': 'View live sessions; transcripts remain after closing',
    '较新': 'Newer', '更早': 'Older', '关键操作记录': 'Key event history',
    '逐按键写入保留在审计库中，此处不重复显示': 'Individual keystrokes remain in the audit database and are omitted here',
    '刷新': 'Refresh', '历史常用命令': 'Frequent command history', '按使用次数排序；点击填入后再执行': 'Sorted by usage; click to fill the input, then execute',
    '拖动标题可移动窗口': 'Drag the title to move this window', '串口文件传输': 'Serial file transfer', '关闭': 'Close',
    '经串口传输，无需开发板联网。板子需进入 Linux shell，并具备 base64、sha256sum、dd；有 gzip 时自动压缩适合的分块。传输时暂停涉及串口的其他输入。板间复制在后台执行，关闭面板或刷新后可在“任务”中查看。': 'Transfer over serial without networking. Requires a Linux shell with base64, sha256sum and dd; suitable chunks use gzip when available. Only involved serial ports are reserved. Background transfers remain visible under Tasks after closing or refreshing.',
    '操作': 'Operation', '本地上传到板子': 'Upload to board', '从板子下载到本地': 'Download from board',
    '复制到另一块板子': 'Copy to another board', '本地文件': 'Local file', '板端源路径': 'Source path on board',
    '目标开发板': 'Target board', '板端目标路径': 'Target path on board', '允许覆盖已存在的目标文件': 'Overwrite existing target file',
    '选择文件或填写绝对路径后开始。': 'Choose a file or enter an absolute path to start.', '取消传输': 'Cancel transfer', '开始传输': 'Start transfer',
    '分享给 Agent': 'Share with agent', '生成独立令牌；默认不限期，可随时撤销': 'Generate a separate token; no expiry by default, revocable anytime',
    '复制接入文本给 Claude Code、Codex 等 Agent。它会安装本机托管的 skill 和 CLI，优先通过 SSH 操作；启动、内核和 SSH 故障再查看串口。': 'Copy instructions for Claude Code, Codex or other agents. They use the self-hosted skill and CLI, prefer SSH, and use serial for boot, kernel or SSH failures.',
    '授权范围': 'Access scope', '当前设备': 'This board', '连接位置': 'Connection', '本机': 'Local', '局域网': 'LAN',
    'Agent 可访问的服务地址': 'Service URL reachable by the agent', 'http://上位机IP:8898': 'http://host-IP:8898',
    '生成并复制': 'Generate and copy', '再次复制': 'Copy again', 'Agent 接入文本': 'Agent instructions',
    '生成后在此显示完整命令和指引': 'Generated commands and instructions appear here', '已有授权': 'Existing grants',
    '启动日志对比': 'Boot log comparison', '左侧基准 · 右侧异常或目标启动': 'Baseline on the left · target on the right',
    '基准启动': 'Baseline boot', '目标启动': 'Target boot', '设备': 'Board', '启动次数': 'Boot',
    '选择两次启动后比较。': 'Select two boots to compare.', '比较差异': 'Compare', '基准日志': 'Baseline log', '目标日志': 'Target log',
    '读取归档失败': 'Could not read archives', '时间未知': 'Unknown time', '日期未知': 'Unknown date',
    '该设备暂无可识别的启动记录。': 'No detected boots for this board.', '目标启动新增的异常线索': 'New warning clues in the target boot',
    '未发现新增的错误或超时关键词；请查看下方差异。': 'No new error or timeout keywords; inspect the differences below.',
    '归一化时间戳后，两次启动的日志内容一致。': 'The logs are identical after timestamp normalization.',
    '请先选择两次启动。': 'Select two boots first.', '请选择不同的启动记录。': 'Choose different boots.',
    '正在比较…': 'Comparing…', '比较失败': 'Comparison failed',
    '对比完成。只显示变化处及前后两行。': 'Comparison complete. Changes are shown with two context lines.',
    'USB 临时身份': 'Temporary USB identity', '尚未绑定开发板序列号': 'No board serial number bound yet',
    '（暂无输出）': '(No output yet)', '没有匹配的设备。新串口连接后将显示在待识别列表。': 'No matching boards. New ports appear as unidentified.',
    'EEPROM 尚未写入序列号；当前身份仅作过渡，请写入 ONIE TLV 0x23': 'EEPROM serial number is missing; write ONIE TLV 0x23 for a stable identity',
    '该身份来源尚未正式确认；确认后迁入正式序列号档案': 'Identity is provisional; confirmation moves its archives to the stable serial directory',
    '服务不可达': 'Service unreachable', '日志磁盘空间不足': 'Low log disk space', '日志磁盘状态不可用': 'Log disk status unavailable',
    '候选身份编号': 'Candidate identity', '板卡序列号': 'Board serial number', '身份来源': 'Identity source',
    '身份状态': 'Identity status', '固件原始编号': 'Firmware-reported number', '主机名': 'Hostname', '识别时间': 'Identified at',
    '已确认': 'Confirmed', '开发板固件': 'Board firmware', '管理员核实并更正': 'Verified and corrected by administrator',
    '板载 EEPROM（ONIE TLV 0x23）': 'Board EEPROM (ONIE TLV 0x23)', '板载 EEPROM（U-Boot 注入）': 'Board EEPROM (U-Boot injection)',
    'SoC / eFuse（内核只读接口）': 'SoC / eFuse (read-only kernel interface)', '/boot 软件标记（随系统文件保留）': '/boot software marker (stored with OS files)',
    '待确认（直接读取 EEPROM 或匹配厂商固件约定后自动确认）': 'Provisional (confirmed by EEPROM or firmware evidence)',
    '当前页面': 'this page', '另一网页客户端': 'another browser client', '串口输入由 ': 'Serial input reserved by ',
    ' 占用；输出仍可共享查看': '; output remains shared', '输入占用操作失败': 'Input reservation failed',
    '已恢复共享串口输入': 'Shared serial input restored', '已占用串口输入；其他人仍可查看输出': 'Serial input reserved; others can still view output',
    '当前页面已占用串口输入': 'Serial input reserved by this page',
    '另一客户端已占用串口输入；输出仍可查看': 'Serial input reserved by another client; output remains visible',
    '串口': 'Serial', '填入命令输入框': 'Fill command input', '查看记录': 'View transcript', '实时查看': 'View live',
    '尚无 SSH 会话记录': 'No SSH session history', '尚无关键操作记录': 'No key event history', '（还没有可复用的命令）': '(No reusable commands yet)',
    '关闭这个归档标签': 'Close archive tab', '关闭这条 SSH 会话': 'Close SSH session', '关闭这个 agent 持久 SSH 会话': 'Close persistent agent SSH session',
    '板子控制台（实时输出）': 'Board console (live output)', '归档日志快照，只读显示在终端中': 'Archive snapshot (read-only)',
    'SSH 会话，独立记录到 ssh-*.log': 'SSH session, recorded in ssh-*.log',
    'agent 持久 SSH 会话；同一 agent 后续命令复用此标签，直到手动关闭': 'Persistent agent SSH session; reused until closed manually',
    '当前会话只读；请切换可输入的串口 / SSH 会话，或先介入该会话': 'Read-only session; switch to an editable terminal or take over this session',
    '当前是只读归档日志': 'Read-only archived log', '当前正在只读查看 agent 会话': 'Viewing an agent session read-only',
    '识别完成': 'Board identified', '创建独立 SSH 会话；默认 root 密码认证失败时尝试通过串口自动配置': 'Create SSH session; try serial provisioning if root password authentication fails',
    '正在建立 SSH 会话…（认证失败时可能通过串口配置 root 登录）': 'Connecting SSH… (may provision root through serial if authentication fails)',
    '正在将当前串口转为待识别…': 'Putting this port into identification mode…',
    '已进入换板识别模式；现在可切换开发板，启动到登录提示后将自动识别一次': 'Ready to change boards; identification runs once after the new login prompt',
    '正在重新连接串口（USB 软拔插）…': 'Reconnecting serial (USB rebind)…', '采集已恢复': 'Capture restored',
    '步骤完成，尚未确认采集恢复': 'Recovery steps finished; capture not confirmed yet', '自愈失败': 'Recovery failed',
    '没有选中内容（鼠标拖选后重试）': 'Nothing selected; drag to select and retry',
    '剪贴板里没有文本（终端里 Ctrl+C 是中断信号，复制请用 Ctrl+Shift+C 或拖选）': 'Clipboard is empty. Use Ctrl+Shift+C or selection to copy; Ctrl+C interrupts the terminal.',
    '上传文件': 'Upload file', '下载文件': 'Download file', '板间复制': 'Board copy', '建立 SSH': 'Connect SSH', '启用 SSH': 'Enable SSH',
    '硬件上电': 'Power on', '硬件断电': 'Power off', '硬件复位': 'Reset', '外部刷机': 'External flashing',
    '等待执行': 'Queued', '执行中': 'Running', '已完成': 'Completed', '已取消': 'Cancelled', '已中断': 'Interrupted', '失败': 'Failed',
    '取消': 'Cancel', '打开会话': 'Open session', '任务进度': 'Task progress', '正在读取任务…': 'Loading tasks…',
    '任务保存在采集器中；刷新页面不会取消。': 'Tasks remain in the collector and survive page refreshes.',
    '尚无后台任务；任务结果在刷新或重新打开页面后仍可查看。': 'No background tasks. Results remain available after refreshing or reopening.',
    '任务响应与请求不匹配': 'Task response does not match request', '任务失败': 'Task failed', '任务请求失败': 'Task request failed',
    '下载': 'Download', '当日日志': 'Daily log', '选择启动次数': 'Choose boot', '选择日志日期': 'Choose date',
    '终端打开': 'Open in terminal', '在终端打开本次启动': 'Open this boot in the terminal', '还没有串口日志文件': 'No serial log files yet',
    '这一天没有检测到 Boot ROM / U-Boot 启动标志；完整日期日志仍可查看。': 'No boot markers detected on this date; the full daily log is available.',
    '涉及日期 · 点击日期返回当日日志': 'Dates covered · click to open daily logs', '待识别连接的临时日志': 'Temporary logs for an unidentified connection',
    '正在搜索当前设备的全部启动日志…': 'Searching all boots for this board…', '搜索全部启动日志 · Enter': 'Search all boots · Enter',
    '保存到本地': 'Save locally', '计算中': 'Calculating', '速率计算中': 'Calculating rate',
    '上传到上位机，尚未写入开发板': 'Staging on host; not yet written to board', '已取消传输': 'Transfer cancelled',
    '正在准备串口连接…': 'Preparing serial connection…', '正在取消，等待当前分块结束…': 'Cancelling after the current chunk…',
    '正在暂存文件；提交任务前请保持页面打开，串口仍可使用。': 'Staging file; keep this page open until submission. Serial input is still available.',
    '正在提交后台传输；提交后关闭面板或刷新页面不会取消。': 'Submitting background transfer; closing or refreshing afterward will not cancel it.',
    '正在请求取消后台传输…': 'Requesting transfer cancellation…', '已请求取消；请在后台任务中确认最终结果。': 'Cancellation requested; check Tasks for the final result.',
    '请选择本地文件、在线目标板，并填写板端绝对路径。': 'Select a local file and online target board, then enter an absolute board path.',
    '暂存偏移不匹配': 'Staging offset mismatch', '暂存文件校验失败': 'Staged file checksum mismatch', '本地文件读取失败': 'Could not read local file',
    '当前凭证无权创建网页分享链接': 'This credential cannot create web share links',
    '当前访问凭证无权生成 Agent 授权': 'This credential cannot create agent grants', '生成可撤销的 Agent 接入令牌': 'Generate a revocable agent token',
    '尚无 Agent 授权': 'No agent grants', '撤销': 'Revoke', '授权失败': 'Authorization failed', '创建失败': 'Creation failed',
    '授权已撤销，相关 SSH 会话已关闭': 'Grant revoked; associated SSH sessions closed',
    '授权已撤销；若 collector 暂不可用，请稍后关闭遗留 SSH 会话': 'Grant revoked; close remaining SSH sessions when the collector is reachable',
    '正在创建分享链接…': 'Creating share link…', '正在生成授权…': 'Generating grant…',
    '当前设备尚无序列号；请先识别设备，或选择“全部设备”。': 'Identify this board first, or choose All boards.',
    '未发现局域网地址，请在下方填写上位机 IP。': 'No LAN address found; enter the host IP below.',
    '未发现局域网 IP，请先确认上位机已连接局域网': 'No LAN IP found; check the host connection',
    '局域网链接已复制': 'LAN link copied', '局域网链接已生成': 'LAN link generated',
    '接入文本已复制': 'Agent instructions copied', '已复制接入文本；令牌只显示这一次': 'Instructions copied; this token is displayed only once',
    '已生成；请点击“再次复制”或手动复制下方文本': 'Generated; click Copy again or copy the text below',
    '请手动复制选中的接入文本': 'Copy the selected instructions manually', '服务地址应为 http(s)://上位机:端口': 'Use http(s)://host:port',
    '链接已复制，24 小时有效': 'Link copied; valid for 24 hours', '链接已生成，24 小时有效': 'Link generated; valid for 24 hours',
    '当前服务为开放模式；此令牌约束使用它的请求，其他客户端仍可无令牌访问。': 'Open access is enabled; the token scopes its requests, while other clients can still connect without one.',
    '当前服务为开放模式；其他客户端仍可不带令牌直接访问。': 'Open access is enabled; other clients can connect without a token.',
    '仅允许访问 ': 'Access limited to ', '先识别当前设备': 'Identify this board first',
    '允许访问当前和今后接入的全部设备。': 'Access to all current and future boards.',
    '串口已静默 ': 'Serial has been quiet for ', '；采集器仍在检查连接，板子也可能只是没有输出': '; capture checks continue; the board may simply be idle',
    '串口日志写入失败：': 'Serial log write failed: ', '采集检查：': 'Capture check: ', '上次连接 ': 'Last connected ',
    '已连接 · ': 'Connected · ', ' 台设备 · ': ' boards · ', ' 记录': ' captured', '末次 ': 'Last received ',
    '从未': 'Never', ' 秒前': ' seconds ago', ' 分钟前': ' minutes ago', ' 小时前': ' hours ago',
    '新增 ': 'Added ', ' 行 · 缺少 ': ' lines · removed ', ' 行 · 变化 ': ' lines · changed ',
    ' · 差异过多，仅显示前 1500 行': ' · truncated to 1500 rows', ' 行  ': ': ', ' 行': ' lines', '第 ': '#', ' 次 · ': ' · ',
    '启动 #': 'Boot #', '启动标志': 'Boot marker', '启动': 'Boot', ' 次': ' times', ' 条': ' entries', ' 页': ' page',
    '当天启动 ': 'Boots on this date: ', '本日启动 · ': 'Boots today · ', '相关启动 · ': 'Related boots · ',
    '跨日延续 · 起于 ': 'Continued across midnight · started ', ' · 启动 ': ' · boots ', ' · 延续 ': ' · continuations ',
    ' · 跨日延续 ': ' · continued boots ', ' · 跨日运行': ' · across midnight', ' · 第 ': ' · #', ' · 还有更多': ' · more results',
    '跨日延续': 'continued across midnight', '延续': 'continuations', '无匹配': 'no matches', '结果': 'matches',
    ' · 无匹配': ' · no matches', ' · 结果 ': ' · matches ', ' · 平均 ': ' · average ', ' · 源读取 ': ' · source ',
    ' · 目标写入 ': ' · target ', ' · 预计剩余 ': ' · remaining ', ' · 正在校验': ' · verifying',
    ' · 可关闭面板，在任务列表中查看。': ' · you can close this panel and follow progress under Tasks.',
    '上传完成': 'Upload complete', '板间复制完成': 'Board copy complete', '下载已校验': 'Download verified',
    '后台传输 ': 'Background transfer ', '传输未完成：': 'Transfer incomplete: ', '取消请求失败：': 'Cancellation failed: ',
    '；可在后台任务中重试。': '; retry under Tasks.', '；提交结果未确认，请在任务列表中核实后再重试。': '; submission unconfirmed; check Tasks before retrying.',
    ' 小时 ': ' h ', ' 分 ': ' min ', ' 秒': ' s', '会话 ': 'Session ', ' · 已请求取消': ' · cancellation requested',
    ' · 不限期': ' · no expiry', ' · 已撤销': ' · revoked', ' · 有效期至 ': ' · expires ',
    '读取任务失败：': 'Could not load tasks: ', '读取日志列表失败：': 'Could not load logs: ', '搜索失败：': 'Search failed: ',
    '读取授权失败：': 'Could not load grants: ', '读取授权失败': 'Could not load grants', '撤销失败': 'Revocation failed',
    '分享失败：': 'Sharing failed: ', '生成失败：': 'Generation failed: ',
    '请求失败：': 'Request failed: ', '读取归档失败：': 'Could not read archive: ', '读取命令历史失败：': 'Could not load command history: ',
    '改名失败：': 'Rename failed: ', '进入换板模式失败：': 'Could not prepare board change: ',
    '正在读取只读归档 ': 'Loading read-only archive ', '只读归档 · 字节 ': 'Read-only archive · bytes ',
    ' · 只读': ' · read-only', ' · 退出码 ': ' · exit code ', ' · 日志写入失败': ' · log write failed',
    '已连接 ': 'Connected ', '已通过串口配置 root SSH；连接 ': 'Root SSH provisioned over serial; connected ',
    '已介入 agent SSH，会话输入和输出继续记录': 'Agent SSH taken over; input and output remain recorded',
    '介入失败：': 'Takeover failed: ', '关闭失败：': 'Close failed: ', '失败：': 'Failed: ', '自愈请求失败：': 'Recovery request failed: ',
    '已粘贴 ': 'Pasted ', '正在逐字符粘贴 ': 'Pasting character by character: ', ' 字符（固件模式）': ' characters (firmware mode)', ' 字符': ' characters',
    '（完整文件）': '(complete file)', '（内容未读完）': '(more content available)', 'SSH 记录 · ': 'SSH transcript · ',
    '串口日志 · ': 'Serial log · ', '管理员核实': 'Verified by administrator', '身份确认': 'Confirm identity',
    '身份更正': 'Correct identity', '身份升级': 'Promote identity', '自动配置 SSH': 'Provision SSH', '人工介入 SSH': 'SSH takeover',
    '串口自愈': 'Serial recovery', '准备更换设备': 'Prepare board change', '初始化 /boot 标记': 'Initialize /boot marker', '识别请求': 'Identification request'
  };
  var keys = Object.keys(dictionary).sort(function (a, b) { return b.length - a.length; });
  var pattern = new RegExp(keys.map(function (key) { return key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }).join('|'), 'g');
  var ignored = '#d-term,.xterm,#d-name,#d-dev,#log-location,#ft-board,#bc-left-device,#bc-right-device,#ft-target-device,.nm:not(.pending-name),.sniff,.last:not(.empty-output),.actor,.command-chip,.bc-content,.bc-clue-text,#d-command-nav,#boot-search-matches,code,script,style,[data-i18n-ignore]';
  var originals = new WeakMap();
  var attributes = new WeakMap();
  var locale = /^zh/i.test(navigator.language || '') ? 'zh-CN' : 'en', userChosen = false;
  try {
    var saved = localStorage.getItem('decklab-language');
    userChosen = saved === 'en' || saved === 'zh-CN';
    if (userChosen) locale = saved;
  } catch (_) {}
  if (locale !== 'en') locale = 'zh-CN';
  function translate(text) { return locale === 'en' ? String(text).replace(pattern, function (key) { return dictionary[key]; }) : text; }
  function excluded(element) { return element && element.closest && element.closest(ignored); }
  function textNode(node) {
    if (excluded(node.parentElement)) return;
    var value = node.nodeValue, record = originals.get(node);
    if (!record || value !== record.rendered) record = { original: value };
    record.rendered = translate(record.original);
    originals.set(node, record);
    if (record.rendered !== value) node.nodeValue = record.rendered;
  }
  function visit(root) {
    if (root.nodeType === 3) { textNode(root); return; }
    if (root.nodeType !== 1 || excluded(root)) return;
    ['title', 'placeholder', 'aria-label'].forEach(function (name) {
      var value = root.getAttribute(name);
      if (value === null) return;
      var saved = attributes.get(root) || {}, record = saved[name];
      if (!record || value !== record.rendered) record = { original: value };
      record.rendered = translate(record.original); saved[name] = record; attributes.set(root, saved);
      if (record.rendered !== value) root.setAttribute(name, record.rendered);
    });
    Array.from(root.childNodes).forEach(visit);
  }
  function applyLocale(next) {
    locale = next === 'en' ? 'en' : 'zh-CN';
    document.documentElement.lang = locale;
    document.querySelectorAll('[data-language-select]').forEach(function (select) { select.value = locale; });
    visit(document.documentElement);
    global.dispatchEvent(new CustomEvent('decklab-language-change', { detail: locale }));
  }
  function setLocale(next) {
    userChosen = true;
    try { localStorage.setItem('decklab-language', next === 'en' ? 'en' : 'zh-CN'); } catch (_) {}
    applyLocale(next);
  }
  global.SerialdI18n = { translate: translate, setLocale: setLocale,
    setDefaultLocale: function (next) { if (!userChosen && (next === 'en' || next === 'zh-CN')) applyLocale(next); },
    get locale() { return locale; } };
  document.querySelectorAll('[data-language-select]').forEach(function (select) {
    select.value = locale;
    select.addEventListener('change', function (event) { setLocale(event.target.value); });
  });
  document.documentElement.lang = locale;
  visit(document.documentElement);
  new MutationObserver(function (changes) {
    changes.forEach(function (change) {
      if (change.type === 'childList') Array.from(change.addedNodes).forEach(visit);
      else visit(change.target);
    });
  }).observe(document.documentElement, { subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['title', 'placeholder', 'aria-label'] });
})(window);
