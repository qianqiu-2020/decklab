(function () {
  'use strict';
  var button = document.getElementById('b-share');
  var status = document.getElementById('share-status');
  var deviceShare = document.getElementById('b-device-share');
  var deviceShareStatus = document.getElementById('device-share-status');
  var shareButtons = [button, deviceShare];
  function shareStatus(text) {
    status.textContent = deviceShareStatus.textContent = text;
  }
  var dialog = document.getElementById('agent-share-dialog');
  var topAgent = document.getElementById('b-agent-share-all');
  var deviceAgent = document.getElementById('b-agent-share');
  var scope = document.getElementById('agent-share-scope');
  var addressMode = document.getElementById('agent-share-address-mode');
  var address = document.getElementById('agent-share-url');
  var hint = document.getElementById('agent-share-hint');
  var output = document.getElementById('agent-share-text');
  var agentStatus = document.getElementById('agent-share-status');
  var create = document.getElementById('agent-share-create');
  var copyButton = document.getElementById('agent-share-copy');
  var grants = document.getElementById('agent-share-grants');
  var openAccess = false;
  var canPageShare = false;
  var selected = null;
  var lanHosts = [];

  fetch('/api/access', { cache: 'no-store' }).then(function (response) { return response.json(); })
    .then(function (access) {
      if (window.SerialdI18n) window.SerialdI18n.setDefaultLocale(access.default_language);
      openAccess = !!access.open_access;
      lanHosts = Array.isArray(access.lan_hosts) ? access.lan_hosts : [];
      canPageShare = openAccess || !!access.share_enabled;
      button.hidden = !canPageShare;
      deviceShare.hidden = false;
      shareButtons.forEach(function (element) { element.disabled = !canPageShare; });
      deviceShare.title = canPageShare
        ? '分享链接直接打开当前设备；网页访问授权覆盖整个平台'
        : '当前凭证无权创建网页分享链接';
      // Selecting a board in this URL does not narrow the web credential.
      var shareText = Array.from(deviceShare.childNodes || []).find(function (node) { return node.nodeType === 3 && node.nodeValue.trim(); });
      if (shareText) shareText.nodeValue = '分享设备（可访问全部设备）';
      [topAgent, deviceAgent].forEach(function (element) {
        element.disabled = !access.agent_share_enabled;
        element.title = access.agent_share_enabled ? '生成可撤销的 Agent 接入令牌'
          : '当前访问凭证无权生成 Agent 授权';
      });
    }).catch(function () {
      shareButtons.forEach(function (element) { element.hidden = true; });
      topAgent.disabled = deviceAgent.disabled = true;
    });

  async function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(text); return; }
      catch (_) { /* Non-secure HTTP or browser permissions may reject this API. */ }
    }
    var field = output.value === text ? output : document.createElement('textarea');
    if (field !== output) {
      field.value = text;
      field.style.position = 'fixed';
      field.style.left = '0';
      field.style.top = '0';
      document.body.appendChild(field);
    }
    field.focus();
    field.select();
    var copied = document.execCommand('copy');
    if (field !== output) field.remove();
    if (!copied) throw new Error('clipboard unavailable');
  }

  function localUrl() {
    var url = new URL(location.origin);
    url.hostname = '127.0.0.1';
    return url.origin;
  }
  function lanUrl() {
    var host = /^(localhost|127\.0\.0\.1|::1)$/.test(location.hostname)
      ? lanHosts[0] : location.hostname;
    if (!host) return '';
    var url = new URL(location.origin);
    url.hostname = host;
    return url.origin;
  }
  function selectAddress() {
    address.value = addressMode.value === 'lan' ? lanUrl() : localUrl();
    agentStatus.textContent = address.value ? '' : '未发现局域网地址，请在下方填写上位机 IP。';
  }

  function shellQuote(value) { return "'" + value.replace(/'/g, "'\\''") + "'"; }
  function selectedDevice() {
    return typeof window.SerialdShareSelection === 'function' ? window.SerialdShareSelection() : null;
  }
  function updateHint() {
    var board = selected;
    if (scope.value === 'device') {
      hint.textContent = board && board.serial
        ? '仅允许访问 ' + board.label + '（SN ' + board.serial + '）；设备改标签后仍绑定此序列号。'
        : '当前设备尚无序列号；请先识别设备，或选择“全部设备”。';
      create.disabled = !(board && board.serial);
    } else {
      hint.textContent = '允许访问当前和今后接入的全部设备。';
      create.disabled = false;
    }
    if (openAccess) hint.textContent += ' 当前服务为开放模式；其他客户端仍可不带令牌直接访问。';
  }
  function baseUrl() {
    var url = new URL(address.value.trim());
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash
        || (url.pathname !== '/' && url.pathname !== ''))
      throw new Error('服务地址应为 http(s)://上位机:端口');
    return url.origin;
  }
  function agentText(url, token, grant, board) {
    var target = grant.scope === 'device' ? board.label : '<先用 devices 选择标签>';
    var selection = grant.scope === 'device'
      ? '只操作 ' + board.label + '（SN ' + grant.serial + '）。令牌在服务端限制为此序列号。'
      : '可访问所有设备；先运行 devices，再根据任务选择明确的设备标签，不要默认选第一台。';
    return [
      '请使用 seriald 访问开发板。' + selection,
      '授权默认不限期；用户可以随时在网页撤销。当前会话的操作标签：' + grant.actor + '。',
      ...(openAccess ? ['当前服务为开放模式；此令牌约束使用它的请求，其他客户端仍可无令牌访问。'] : []),
      '优先通过 SSH 执行 Linux 命令；调试启动、内核、SSH 不通或交互式固件时才用共享串口。',
      '先读取以下自托管 skill 和客户端，再操作设备：', '', '```sh',
      'mkdir -p "$HOME/.local/share/seriald" "$HOME/.claude/skills/seriald-board" "$HOME/.codex/skills/seriald-board"',
      'curl -fsSL ' + shellQuote(url + '/serialctl.py') + ' -o "$HOME/.local/share/seriald/serialctl.py"',
      'curl -fsSL ' + shellQuote(url + '/skills/seriald-board/SKILL.md') + ' -o "$HOME/.claude/skills/seriald-board/SKILL.md"',
      'cp "$HOME/.claude/skills/seriald-board/SKILL.md" "$HOME/.codex/skills/seriald-board/SKILL.md"',
      'export SERIALD_URL=' + shellQuote(url),
      'export SERIALD_TOKEN=' + shellQuote(token),
      'python3 "$HOME/.local/share/seriald/serialctl.py" devices',
      '# 先按任务选定标签；首选 SSH：',
      'python3 "$HOME/.local/share/seriald/serialctl.py" --device ' + shellQuote(target) + ' exec --command ' + shellQuote('uname -a'),
      '# 启动、内核或 SSH 故障时再读取串口：',
      'python3 "$HOME/.local/share/seriald/serialctl.py" --device ' + shellQuote(target) + ' read --sid serial',
      '```', '',
      '如果 Agent 运行在 Windows PowerShell，请用 Invoke-WebRequest 下载相同两个地址，并用 py -3 运行 serialctl.py。',
      'SSH 会话和串口输入会显示在网页中，并保留操作记录。串口由 seriald 独占采集，不要直接打开 /dev/ttyUSB*。',
      '如果通过 SSH 操作失败，先用 read 查看当前串口，再按 skill 指引决定是否发送输入。'
    ].join('\n');
  }

  async function loadGrants() {
    try {
      var response = await fetch('/api/agent-grants', { cache: 'no-store' });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || '读取授权失败');
      grants.textContent = '';
      if (!result.grants.length) { grants.textContent = '尚无 Agent 授权'; return; }
      result.grants.forEach(function (grant) {
        var row = document.createElement('div');
        row.className = 'agent-grant' + (grant.revoked ? ' revoked' : '');
        var detail = document.createElement('span');
        detail.textContent = (grant.scope === 'all' ? '全部设备' : 'SN ' + grant.serial)
          + ' · ' + grant.actor + ' · ' + new Date(grant.created * 1000).toLocaleString('zh-CN')
          + (grant.revoked ? ' · 已撤销' : (grant.expires ? ' · 有效期至 ' + new Date(grant.expires * 1000).toLocaleString('zh-CN') : ' · 不限期'));
        row.appendChild(detail);
        if (!grant.revoked) {
          var revoke = document.createElement('button');
          revoke.type = 'button'; revoke.textContent = '撤销';
          revoke.addEventListener('click', async function () {
            revoke.disabled = true;
            try {
              var response = await fetch('/api/agent-revoke', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: grant.id }) });
              var result = await response.json();
              if (!response.ok) throw new Error(result.error || '撤销失败');
              agentStatus.textContent = result.sessions_closed
                ? '授权已撤销，相关 SSH 会话已关闭'
                : '授权已撤销；若 collector 暂不可用，请稍后关闭遗留 SSH 会话';
              await loadGrants();
            } catch (error) { agentStatus.textContent = error.message; revoke.disabled = false; }
          });
          row.appendChild(revoke);
        }
        grants.appendChild(row);
      });
    } catch (error) { grants.textContent = '读取授权失败：' + error.message; }
  }
  function openAgent(initialScope) {
    selected = selectedDevice();
    scope.value = initialScope;
    addressMode.value = lanUrl() ? 'lan' : 'local';
    selectAddress();
    output.value = '';
    copyButton.disabled = true;
    updateHint();
    dialog.showModal();
    loadGrants();
  }
  topAgent.addEventListener('click', function () { openAgent('all'); });
  deviceAgent.addEventListener('click', function () { openAgent('device'); });
  scope.addEventListener('change', updateHint);
  addressMode.addEventListener('change', selectAddress);
  document.getElementById('agent-share-close').addEventListener('click', function () {
    output.value = ''; dialog.close();
  });
  dialog.addEventListener('close', function () { output.value = ''; copyButton.disabled = true; });
  document.getElementById('agent-share-refresh').addEventListener('click', loadGrants);
  create.addEventListener('click', async function () {
    create.disabled = true;
    agentStatus.textContent = '正在生成授权…';
    try {
      var url = baseUrl();
      var board = selected;
      if (scope.value === 'device' && !(board && board.serial)) throw new Error('先识别当前设备');
      var response = await fetch('/api/agent-share', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: scope.value, serial: scope.value === 'device' ? board.serial : '' }) });
      var result = await response.json();
      if (!response.ok || !result.token) throw new Error(result.error || '授权失败');
      output.value = agentText(url, result.token, result.grant, board);
      copyButton.disabled = false;
      try { await copy(output.value); agentStatus.textContent = '已复制接入文本；令牌只显示这一次'; }
      catch (_) { agentStatus.textContent = '已生成；请点击“再次复制”或手动复制下方文本'; }
      await loadGrants();
    } catch (error) { agentStatus.textContent = '生成失败：' + error.message; }
    finally { updateHint(); }
  });
  copyButton.addEventListener('click', async function () {
    try { await copy(output.value); agentStatus.textContent = '接入文本已复制'; }
    catch (_) { output.focus(); output.select(); agentStatus.textContent = '请手动复制选中的接入文本'; }
  });

  async function sharePage(source) {
    shareButtons.forEach(function (element) { element.disabled = true; });
    shareStatus('正在创建分享链接…');
    try {
      var lan = lanUrl();
      if (!lan) throw new Error('未发现局域网 IP，请先确认上位机已连接局域网');
      var url = new URL('/', lan);
      if (!openAccess) {
        var response = await fetch('/api/share-link', { method: 'POST' });
        var result = await response.json();
        if (!response.ok || !result.path) throw new Error(result.error || '创建失败');
        url = new URL(result.path, lan);
      }
      if (source === deviceShare) {
        var board = selectedDevice();
        if (board) url.hash = 'device=' + encodeURIComponent(board.serial || board.key);
      }
      var copied = openAccess ? '局域网链接已复制' : '链接已复制，24 小时有效';
      try { await copy(url.href); shareStatus(copied); }
      catch (_) {
        window.prompt(openAccess ? '复制局域网链接：' : '复制分享链接（24 小时有效）：', url.href);
        shareStatus(openAccess ? '局域网链接已生成' : '链接已生成，24 小时有效');
      }
    } catch (error) { shareStatus('分享失败：' + error.message); }
    finally { shareButtons.forEach(function (element) { element.disabled = !canPageShare; }); }
  }
  shareButtons.forEach(function (element) {
    element.addEventListener('click', function () { return sharePage(element); });
  });
})();
