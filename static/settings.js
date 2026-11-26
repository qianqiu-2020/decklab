(function (global) {
  'use strict';
  var UI = global.DeckLabUI, document = global.document;
  var groups = [
    ['access', '访问与共享', [
      ['网页登录', ['PORT', 'BIND', 'AUTH_TOKEN', 'UI_LANGUAGE', 'SHARE_SECONDS']],
      ['团队用例（TitanX）', ['TEAM_CATALOGUE_URL', 'TEAM_CATALOGUE_TOKEN']],
      ['AI 接入（MCP）', ['MCP_ENABLED']],
      ['高级访问设置', ['TRUST_LOOPBACK', 'MCP_PYTHON', 'MCP_ALLOWED_HOSTS', 'AGENT_TOKENS_FILE'], true, ['agent_tokens']]
    ]],
    ['serial', '串口与登录', [
      ['串口连接', ['BAUD', 'DATA_BITS', 'PARITY', 'STOP_BITS', 'FLOW_CONTROL', 'NEWLINE']],
      ['设备识别', ['PROBE', 'AUTO_CONFIGURE_SSH', 'PROBE_COMMAND']],
      ['登录默认值', ['SSH_USER', 'SSH_PASSWORD', 'CONSOLE_USER', 'CONSOLE_PASSWORD'], true]
    ]],
    ['flash', '刷机与接线', [
      ['组织镜像', ['IMAGE_INTERNAL_BROWSE', 'IMAGE_INTERNAL_DOWNLOAD']],
      ['高级刷机设置', ['HARDWARE', 'FLASH', 'FLASH_EXECUTOR', 'FLASH_LOADERS', 'FLASH_USB2_PORTS', 'RESCUE_BIN'], true, ['hardware', 'flash']]
    ]],
    ['storage', '日志与文件', [
      ['日志保留', ['LOG_RETENTION_DAYS', 'DISK_MIN_FREE_MB', 'DISK_MIN_FREE_PERCENT']],
      ['传输与会话限额', ['TRANSFER_MAX_FILES', 'TRANSFER_MAX_FILE_BYTES', 'TRANSFER_MAX_TOTAL_BYTES', 'TRANSFER_MIN_FREE_BYTES', 'TRANSFER_IDLE_SECONDS', 'TRANSFER_RETENTION_SECONDS', 'SCROLLBACK', 'SSH_LIMIT', 'SSH_IDLE'], true]
    ]],
    ['advanced', '高级', [
      ['节点间连接', ['BACKEND_URL', 'BACKEND_TOKEN', 'BACKEND_CA', 'BACKEND_BIND', 'BACKEND_PORT', 'BACKEND_CERT', 'BACKEND_KEY'], true],
      ['服务路径', ['BASE', 'STATIC', 'SOCKET', 'HANDOFF', 'BY_PATH'], true],
      ['板卡配置与图形接入', ['PROFILES', 'DESKTOP'], true, ['profiles', 'desktop']]
    ]]
  ];
  var labels = {
    IMAGE_INTERNAL_BROWSE: '组织 Nexus 地址', IMAGE_INTERNAL_DOWNLOAD: '自定义下载地址（可选）',
    TEAM_CATALOGUE_URL: '平台 HTTPS 地址', TEAM_CATALOGUE_TOKEN: '平台访问令牌',
    DESKTOP: '图形接入配置文件',
    BACKEND_URL: '后端 HTTPS 地址', BACKEND_TOKEN: '节点间访问密钥', BACKEND_CA: '后端 CA 证书文件',
    BACKEND_BIND: '后端监听地址', BACKEND_PORT: '后端监听端口', BACKEND_CERT: '后端 TLS 证书文件', BACKEND_KEY: '后端 TLS 私钥文件',
    FLASH_USB2_PORTS: 'K3 使用 USB 2.0 的刷机端口（填写 USB 路径，多个用逗号分隔）',
    FLASH_LOADERS: '预备刷机环境清单',
    UI_LANGUAGE: '默认界面语言', SCROLLBACK: '实时输出缓存（字节）', SSH_LIMIT: '每设备终端会话上限', SSH_IDLE: '终端会话空闲超时（秒）', SHARE_SECONDS: '网页分享有效期（秒）',
    PORT: '监听端口', BIND: '监听地址', AUTH_TOKEN: '主访问密码', TRUST_LOOPBACK: '信任本机访问（Windows）',
    MCP_ENABLED: '启用服务端 MCP', MCP_PYTHON: 'MCP Python（留空使用内置运行时）', MCP_ALLOWED_HOSTS: 'MCP 代理域名（逗号分隔）',
    BAUD: '波特率', DATA_BITS: '数据位', PARITY: '校验位', STOP_BITS: '停止位', FLOW_CONTROL: '流控', NEWLINE: '换行方式',
    PROBE: '默认探测模式', PROBE_COMMAND: '自定义探测命令', AUTO_CONFIGURE_SSH: '允许显式配置 SSH',
    SSH_USER: 'SSH 用户', SSH_PASSWORD: 'SSH 密码', CONSOLE_USER: '串口用户', CONSOLE_PASSWORD: '串口密码',
    LOG_RETENTION_DAYS: '日志保留天数（0 为不清理）', DISK_MIN_FREE_MB: '磁盘最低剩余 MB', DISK_MIN_FREE_PERCENT: '磁盘最低剩余百分比',
    TRANSFER_MAX_FILES: '缓存文件数上限', TRANSFER_MAX_FILE_BYTES: '单文件上限（字节）', TRANSFER_MAX_TOTAL_BYTES: '文件缓存总额度（字节）',
    TRANSFER_MIN_FREE_BYTES: '暂存保留空闲（字节）', TRANSFER_IDLE_SECONDS: '未完成上传超时（秒）', TRANSFER_RETENTION_SECONDS: '暂存保留时间（秒）',
    BASE: '运行数据目录', STATIC: '页面资源目录', SOCKET: '采集器通信路径（Linux）', HANDOFF: '重启接续文件',
    BY_PATH: '串口发现目录（Linux）', RESCUE_BIN: 'USB 恢复工具', AGENT_TOKENS_FILE: 'Agent 令牌文件',
    PROFILES: '板卡配置文件', HARDWARE: '硬件动作文件', FLASH: '刷机配置文件', FLASH_EXECUTOR: '自动刷机执行器'
  };
  var docs = {profiles: '板卡配置与分配', hardware: '硬件动作', flash: '刷机绑定', desktop: '图形接入', agent_tokens: '固定 Agent 令牌'};
  var labelsByChoice = {
    '0': '关闭', '1': '开启', 'true': '开启', 'false': '关闭', auto: '自动',
    linux: '内置 Linux 探测', manual: '仅终端', custom: '自定义命令',
    none: '无', rtscts: '硬件（RTS/CTS）', xonxoff: '软件（XON/XOFF）',
    N: '无校验', E: '偶校验', O: '奇校验', en: 'English', 'zh-CN': '简体中文'
  };
  var headers = {access: '控制其他人如何访问这台 DeckLab，以及网页分享链接的有效期。',
    serial: '作为新接入串口的默认值。单块板的识别和 SSH 请在设备页设置。',
    flash: '配置镜像来源和开发板的复位、下载模式接线。',
    storage: '管理串口日志保留时间，以及镜像、传输文件和任务附件共用的控制机缓存额度。',
    advanced: '多机部署、服务路径及批量配置。修改数据目录不会迁移已有数据。'};
  var snapshot, inputs = {}, editors = {}, pendingBrowser = null;
  var dialog, status, preview, save, activate, browserBox, body, saveState, selectPage, busy = false;
  var openGeneration = 0, settingsTarget = 'web', remoteBackendAvailable = false;
  var devicePreferences = null, preferenceEditor, preferenceSave, preferencePreview = '', preferenceStatus, includePreferences;
  function el(tag, text, parent) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function message(text, error) { status.textContent = text; status.dataset.error = error ? 'true' : 'false'; }
  function redacted(value) { return value && typeof value === 'object' && value.$redacted === true; }
  async function request(payload, target) {
    var response = await UI.fetch('/api/settings' + ((target || settingsTarget) === 'backend' ? '?target=backend' : ''), payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : undefined);
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || '配置请求失败');
    return data;
  }
  async function preferenceRequest(payload) {
    var response = await UI.fetch('/api/device-preferences', payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : undefined);
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || '设备偏好请求失败');
    return data;
  }
  async function previewPreferences() {
    var config = JSON.parse(preferenceEditor.value), view = dialog;
    var result = await preferenceRequest({action: 'preview', revision: devicePreferences.revision, configuration: config});
    if (view !== dialog) return;
    preferencePreview = JSON.stringify(config); preferenceSave.disabled = false;
    preferenceStatus.textContent = '设备偏好校验通过：' + (result.changes.join('、') || '无变化') + '；保存后即时生效，不执行探测或硬件命令。';
  }
  async function savePreferences() {
    var config = JSON.parse(preferenceEditor.value), view = dialog;
    if (JSON.stringify(config) !== preferencePreview) throw new Error('设备偏好已变化，请重新预览');
    preferenceSave.disabled = true;
    await preferenceRequest({action: 'save', revision: devicePreferences.revision, configuration: config});
    var fresh = await preferenceRequest();
    if (view !== dialog) return;
    devicePreferences = fresh; preferenceEditor.value = JSON.stringify(fresh.configuration, null, 2);
    preferencePreview = ''; preferenceStatus.textContent = '设备偏好已保存并生效。';
    updateActions();
  }
  function fieldValue(field) {
    var input = inputs[field.name];
    if (field.secret && !input.value && !input.dataset.clear
        && (input.dataset.masked || redacted(snapshot.configuration.environment[field.name]))) return {$redacted: true};
    return input.value;
  }
  function documentText(value) { return value ? JSON.stringify(value, null, 2) : ''; }
  function changedFields() {
    return snapshot.fields.filter(function (field) {
      return JSON.stringify(fieldValue(field)) !== JSON.stringify(snapshot.configuration.environment[field.name]);
    });
  }
  function changedDocuments() {
    return Object.keys(editors).filter(function (key) {
      return editors[key].value.trim() !== documentText(snapshot.configuration.documents[key]);
    });
  }
  function preferenceDraft() {
    return devicePreferences && preferenceEditor && preferenceEditor.value.trim() !== documentText(devicePreferences.configuration);
  }
  function hostDraft() {
    return changedFields().length || changedDocuments().length || pendingBrowser && browserBox.checked;
  }
  function configuration() {
    var result = {version: 1, environment: {}, documents: {}};
    changedFields().forEach(function (field) { result.environment[field.name] = fieldValue(field); });
    changedDocuments().forEach(function (key) {
      try { result.documents[key] = editors[key].value.trim() ? JSON.parse(editors[key].value) : {}; }
      catch (_) { throw new Error('JSON 格式错误：' + docs[key]); }
    });
    return result;
  }
  function updateActions() {
    var dirty = hostDraft(), pending = snapshot.active_revision !== snapshot.revision;
    save.disabled = preview.disabled = busy || !dirty;
    activate.hidden = !pending || !snapshot.restart_available;
    activate.disabled = busy || !!dirty || snapshot.activation && snapshot.activation.state === 'applying';
    saveState.textContent = dirty ? '有未保存的配置' : pending ? '已保存，待重启' : '配置已生效';
    if (pending && !snapshot.restart_available && !dirty) saveState.textContent = '已保存，请手动重启服务';
  }
  function changed() {
    if (inputs.PROBE && inputs.PROBE_COMMAND) inputs.PROBE_COMMAND.closest('.settings-field').hidden = inputs.PROBE.value !== 'custom';
    if (inputs.PROBE && inputs.AUTO_CONFIGURE_SSH) {
      var ssh = inputs.AUTO_CONFIGURE_SSH;
      if (inputs.PROBE.value !== 'linux') {
        if (!ssh.disabled) ssh.dataset.enabledValue = ssh.value;
        if (ssh.value === '1' || ssh.value === 'true') ssh.value = '0';
        ssh.disabled = true;
      } else {
        if (ssh.disabled && ssh.dataset.enabledValue) ssh.value = ssh.dataset.enabledValue;
        ssh.disabled = false;
        delete ssh.dataset.enabledValue;
      }
    }
    Object.keys(inputs).forEach(function (name) {
      var input = inputs[name];
      var clear = input.closest('.settings-field').querySelector('button');
      if (clear) clear.hidden = !input.value && !input.dataset.masked;
    });
    updateActions();
    message('');
  }
  function fill(data) {
    snapshot.fields.forEach(function (field) {
      var input = inputs[field.name], value = data.environment[field.name];
      input.value = redacted(value) ? '' : value;
      if (field.secret) input.placeholder = redacted(value) ? '已设置 · 留空保留' : '';
      if (redacted(value)) input.dataset.masked = '1'; else delete input.dataset.masked;
      delete input.dataset.clear;
      delete input.dataset.enabledValue;
      input.disabled = false;
    });
    Object.keys(editors).forEach(function (key) { editors[key].value = documentText(data.documents[key]); });
    changed();
  }
  function browserPreferences() {
    return {language: global.localStorage.getItem('decklab-language'), deviceOrder: JSON.parse(global.localStorage.getItem('decklab.device-order.v1') || 'null')};
  }
  function validateBrowser(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(function (key) { return key !== 'language' && key !== 'deviceOrder'; })) throw new Error('浏览器偏好格式无效');
    if (value.language != null && ['en', 'zh-CN'].indexOf(value.language) < 0) throw new Error('语言无效');
    var order = value.deviceOrder;
    if (order != null && (order.version !== 1 || !Array.isArray(order.ids) || order.ids.length > 4096 || order.ids.some(function (id) { return typeof id !== 'string' || id.length > 2048 || !/^(board|port|connection|key):/.test(id); }))) throw new Error('设备排序格式无效');
    return value;
  }
  async function doPreview() {
    var view = dialog;
    var config = configuration();
    message('正在校验配置…');
    var result = await request({action: 'preview', revision: snapshot.revision, configuration: config});
    if (view !== dialog) return;
    var descriptions = result.changes.map(function (name) {
      return labels[name] || docs[name.replace(/^document:/, '')] || name;
    });
    if (pendingBrowser && browserBox.checked) descriptions.push('本浏览器的语言与排序');
    message('将保存：' + (descriptions.join('、') || '没有变化') + '。');
  }
  async function doSave() {
    var view = dialog, preferences = pendingBrowser, includeBrowser = browserBox.checked;
    var browserError = '';
    var config = configuration();
    var hostChanged = Object.keys(config.environment).length || Object.keys(config.documents).length;
    if (hostChanged) {
      var result = await request({action: 'save', revision: snapshot.revision, configuration: config});
      if (view !== dialog) return;
      snapshot.revision = result.revision;
      snapshot.configuration.environment = Object.assign({}, snapshot.configuration.environment, config.environment);
      Object.keys(config.environment).forEach(function (key) {
        if (inputs[key].type === 'password' && config.environment[key]) snapshot.configuration.environment[key] = {$redacted: true};
      });
    }
    if (preferences && includeBrowser) {
      try {
        if (preferences.language != null) global.localStorage.setItem('decklab-language', preferences.language);
        if (preferences.deviceOrder != null) global.localStorage.setItem('decklab.device-order.v1', JSON.stringify(preferences.deviceOrder));
      } catch (_) { browserError = ' 当前浏览器拒绝保存偏好，请检查隐私设置。'; }
    }
    if (view !== dialog) return;
    pendingBrowser = null;
    if (hostChanged) {
      // A lost read after a successful save must not make the edit resubmittable.
      try {
        var fresh = await request();
        if (view !== dialog) return;
        snapshot = fresh;
      } catch (_) { browserError += ' 无法重新读取配置，请关闭后重新打开。'; }
    }
    fill(snapshot.configuration);
    message((hostChanged ? '配置已保存。' : '') + (preferences && includeBrowser ? '刷新页面后使用导入的浏览器偏好。' : '') + browserError, !!browserError);
  }
  async function download(includeSecrets) {
    var view = dialog, browserSelected = browserBox.checked, preferencesSelected = includePreferences.checked;
    var result = await request({action: 'export', include_secrets: includeSecrets});
    if (view !== dialog) return;
    var pack = {format: 'decklab-settings', version: 1, configuration: result.configuration};
    if (browserSelected) pack.browser = browserPreferences();
    if (preferencesSelected) pack.preferences = (await preferenceRequest()).configuration;
    if (view !== dialog) return;
    var url = global.URL.createObjectURL(new Blob([JSON.stringify(pack) + '\n'], {type: 'application/json'}));
    var anchor = el('a'); anchor.href = url; anchor.download = includeSecrets ? 'decklab-private-backup.json' : 'decklab-settings.json'; anchor.click();
    global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 1000);
    message(includeSecrets ? '完整备份含密码和令牌，请私密保存。' : '已导出分享配置；密码字段与令牌已隐藏，自定义命令请自行检查。导入时隐藏字段保留目标服务现有值。');
  }
  async function importFile(file) {
    var view = dialog, target = snapshot, preferenceTarget = devicePreferences, nodeTarget = settingsTarget;
    if (!file || file.size > 16 * 1024 * 1024) throw new Error('配置包须小于 16 MiB');
    var pack = JSON.parse(await file.text());
    if (view !== dialog) return;
    if (!pack || pack.format !== 'decklab-settings' || pack.version !== 1) throw new Error('不支持的配置包格式');
    var config = pack.configuration;
    var proposedBrowser = pack.browser == null ? null : validateBrowser(pack.browser);
    var reviewed = await request({action: 'preview', revision: target.revision, configuration: config}, nodeTarget);
    if (view !== dialog) return;
    var preferenceError = '';
    if (pack.preferences != null) {
      preferenceEditor.value = JSON.stringify(pack.preferences, null, 2); preferencePreview = ''; preferenceSave.disabled = true;
      try {
        if (!preferenceTarget) throw new Error('采集器尚未就绪，请稍后重新打开配置');
        await preferenceRequest({action: 'preview', revision: preferenceTarget.revision, configuration: pack.preferences});
      } catch (error) { preferenceError = error.message; }
    }
    if (view !== dialog) return;
    pendingBrowser = proposedBrowser;
    if (pendingBrowser) browserBox.checked = true;
    if (pack.preferences != null) preferenceStatus.textContent = preferenceError
      ? preferenceError + '；设备偏好尚未保存，请编辑后重新预览。'
      : '已导入设备偏好，尚未保存。请单独预览并保存设备偏好。';
    // Patch imports preserve omitted fields and documents, matching the server.
    fill({version: 1, environment: Object.assign({}, target.configuration.environment, config.environment), documents: Object.assign({}, target.configuration.documents, config.documents)});
    var names = reviewed.changes.map(function (name) { return labels[name] || docs[name.replace(/^document:/, '')] || name; });
    message('已载入配置，尚未保存。' + (names.length ? ' 服务端改动：' + names.join('、') + '。' : '')
      + (preferenceError ? ' 请检查「设备偏好」中的错误。' : pack.preferences != null ? ' 设备偏好需单独保存。' : ''));
  }
  function run(action) { return function () { var view = dialog; Promise.resolve().then(action).catch(function (error) { if (view === dialog) message(error.message, true); }); }; }
  function whileReady(action) {
    return async function () {
      if (busy) return;
      var view = dialog;
      busy = true; body.inert = true;
      updateActions();
      try { await action(); }
      finally {
        if (view === dialog) {
          busy = false; body.inert = false;
          updateActions();
        }
      }
    };
  }
  function button(parent, title, action) { var node = el('button', title, parent); node.type = 'button'; node.addEventListener('click', run(action)); return node; }
  function renderField(field, grid) {
    var name = field.name;
    var wrapper = el('div', null, grid);
    wrapper.className = 'settings-field';
    wrapper.dataset.field = name;
    if (name === 'PROBE_COMMAND') wrapper.classList.add('settings-wide');
    var label = el('label', labels[name] || name, wrapper), input;
    if (field.choices) {
      input = el('select', null, label);
      field.choices.forEach(function (value) {
        var booleanField = ['MCP_ENABLED', 'TRUST_LOOPBACK', 'AUTO_CONFIGURE_SSH'].includes(name);
        var title = /^\d+$/.test(value) && !booleanField ? value : labelsByChoice[value] || value;
        var option = el('option', title, input);
        option.value = value;
      });
    } else {
      input = el(name === 'PROBE_COMMAND' ? 'textarea' : 'input', null, label);
      if (name === 'PROBE_COMMAND') input.rows = 4;
      else input.type = field.secret ? 'password' : 'text';
      if (name === 'PORT' || name === 'BACKEND_PORT' || name === 'BAUD') input.inputMode = 'numeric';
    }
    input.id = 'setting-' + name.toLowerCase().replace(/_/g, '-');
    input.autocomplete = field.secret ? 'new-password' : 'off'; input.spellcheck = false;
    if (name === 'IMAGE_INTERNAL_BROWSE') {
      input.placeholder = 'https://repo.example.test/';
      el('small', '填写 Nexus 根地址即可使用 image 仓库。组织镜像支持自签证书。', wrapper);
    }
    if (name === 'IMAGE_INTERNAL_DOWNLOAD') {
      input.placeholder = 'https://repo.example.test/repository/image/';
      el('small', '仅自定义浏览目录时填写。', wrapper);
    }
    if (name === 'BIND') el('small', '127.0.0.1 仅本机访问，0.0.0.0 监听所有网卡。', wrapper);
    if (name === 'AUTH_TOKEN') el('small', '清空后，能访问此地址的人可以直接打开 DeckLab。', wrapper);
    if (name === 'AUTO_CONFIGURE_SSH') el('small', '仅在内置 Linux 探测模式下可用。', wrapper);
    input.addEventListener('input', function () { delete input.dataset.clear; changed(); });
    input.addEventListener('change', changed);
    inputs[name] = input;
    if (field.secret) {
      var clear = button(wrapper, '清空', function () { input.value = ''; input.dataset.clear = '1'; delete input.dataset.masked; changed(); input.focus(); });
      clear.setAttribute('aria-label', '清空' + (labels[name] || name));
    }
  }
  function addPage(nav, title, id) {
    var pane = el('section', null, body);
    pane.id = 'settings-panel-' + id;
    pane.className = 'settings-pane';
    pane.hidden = true;
    pane.setAttribute('role', 'tabpanel');
    var tab = button(nav, title, function () { selectPage(tab); });
    tab.id = 'settings-tab-' + id;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', pane.id);
    tab.setAttribute('aria-selected', 'false');
    tab.tabIndex = -1;
    pane.setAttribute('aria-labelledby', tab.id);
    return pane;
  }
  function groupsPage(nav, available, group) {
    var id = group[0], title = group[1];
    var parts = group[2].filter(function (part) {
      return part[1].some(function (name) { return available.has(name); }) ||
        (part[3] || []).some(function (name) { return !snapshot.document_names || snapshot.document_names.includes(name); });
    });
    if (!parts.length) return;
    var pane = addPage(nav, title, id);
    el('h3', title, pane);
    el('p', headers[id], pane);
    if (id === 'flash') {
      var wiring = el('div', null, pane);
      wiring.className = 'settings-callout';
      var caption = el('div', null, wiring);
      el('strong', '工位接线', caption);
      el('small', '将控制器槽位绑定到开发板，用于复位和进入下载模式。', caption);
      button(wiring, '管理接线', function () { global.DeckLabControlSlots.open(); });
    }
    parts.forEach(function (part) {
      var section = el(part[2] ? 'details' : 'section', null, pane);
      section.className = 'settings-group';
      el(part[2] ? 'summary' : 'h4', part[0], section);
      if (part[0] === '组织镜像') el('p', '浏览地址用于列出镜像，下载地址用于获取文件。两个地址都使用 HTTPS，并以 / 结尾。', section);
      if (part[0] === '节点间连接') el('p', '仅在 Web 与板卡后端分开部署时填写。', section);
      var grid = el('div', null, section); grid.className = 'settings-grid';
      part[1].forEach(function (name) { if (available.has(name)) renderField(available.get(name), grid); });
      var configNames = (part[3] || []).filter(function (name) { return !snapshot.document_names || snapshot.document_names.includes(name); });
      if (configNames.length) {
        el('p', '内嵌 JSON 优先于文件路径。填写 {} 可清空对应配置。', section);
        configNames.forEach(function (name) {
          var label = el('label', docs[name] + '（JSON）', section);
          var editor = el('textarea', null, label);
          editor.rows = 8; editor.spellcheck = false;
          editor.addEventListener('input', changed); editors[name] = editor;
        });
      }
    });
  }
  function renderPreferences(nav) {
    var pane = addPage(nav, '设备偏好', 'devices');
    el('h3', '设备偏好', pane);
    el('p', '单块板可点击设备名称改名，端口探测在设备菜单中设置。需要批量更新时，可在这里编辑。', pane);
    var deviceSection = el('details', null, pane);
    deviceSection.className = 'settings-group';
    el('summary', '设备标签与端口探测覆盖（JSON）', deviceSection);
    el('p', '板卡用序列号定位，端口用串口位置定位。保存后立即生效。端口字段设为 null 可清除该项。', deviceSection);
    var label = el('label', '设备偏好（JSON）', deviceSection);
    preferenceEditor = el('textarea', null, label); preferenceEditor.rows = 12; preferenceEditor.spellcheck = false;
    preferenceEditor.value = devicePreferences ? documentText(devicePreferences.configuration) : '';
    preferenceEditor.disabled = !devicePreferences;
    preferencePreview = '';
    preferenceEditor.addEventListener('input', function () { preferencePreview = ''; preferenceSave.disabled = true; preferenceStatus.textContent = ''; });
    var actions = el('div', null, deviceSection); actions.className = 'settings-actions';
    var check = button(actions, '预览设备偏好', whileReady(previewPreferences)); check.disabled = !devicePreferences;
    preferenceSave = button(actions, '保存设备偏好', whileReady(savePreferences)); preferenceSave.disabled = true;
    preferenceStatus = el('p', devicePreferences ? '' : '采集器未就绪，设备偏好暂不可用。', deviceSection);
    preferenceStatus.setAttribute('role', 'status');
    var shortcuts = el('section', null, pane);
    shortcuts.className = 'settings-group';
    el('h4', '本浏览器', shortcuts);
    el('p', '设备排序与快捷键只影响当前浏览器。', shortcuts);
    button(shortcuts, '设备快捷键', function () { UI.byId('b-shortcuts').click(); });
  }
  function renderTransfer(nav) {
    var pane = addPage(nav, '导入与导出', 'transfer');
    el('h3', '导入与导出', pane);
    var importSection = el('section', null, pane); importSection.className = 'settings-group';
    el('h4', '导入', importSection);
    el('p', '载入后可检查改动，再保存到当前节点。', importSection);
    var file = el('input', null, importSection); file.type = 'file'; file.accept = '.json,application/json'; file.hidden = true;
    file.addEventListener('change', run(whileReady(function () { if (file.files[0]) return importFile(file.files[0]); })));
    button(importSection, '导入配置…', function () { file.value = ''; file.click(); });
    var exportSection = el('section', null, pane); exportSection.className = 'settings-group';
    el('h4', '导出', exportSection);
    el('p', '分享配置会隐藏密码和令牌。分享前请检查自定义命令。', exportSection);
    var browserLabel = el('label', null, exportSection); browserLabel.className = 'settings-check';
    browserBox = el('input', null, browserLabel); browserBox.type = 'checkbox';
    browserLabel.appendChild(document.createTextNode('导入 / 导出包含本浏览器语言与排序'));
    browserBox.addEventListener('change', changed);
    var preferenceLabel = el('label', null, exportSection); preferenceLabel.className = 'settings-check';
    includePreferences = el('input', null, preferenceLabel); includePreferences.type = 'checkbox'; includePreferences.disabled = !devicePreferences;
    preferenceLabel.appendChild(document.createTextNode('导出包含设备标签与端口探测覆盖'));
    button(exportSection, '导出分享配置', whileReady(function () { return download(false); }));
    var privateExport = el('details', null, pane); privateExport.className = 'settings-group';
    el('summary', '私密备份', privateExport);
    el('p', '私密备份包含密码和令牌，请仅存放于受保护的位置。', privateExport);
    button(privateExport, '导出含密钥备份', whileReady(function () { return download(true); }));
  }
  function mayLeave() {
    if (busy) return false;
    return !(hostDraft() || preferenceDraft()) || global.confirm('有未保存的修改，确定放弃吗？');
  }
  function render() {
    var previous = dialog;
    if (previous) previous.remove();
    var view = dialog = el('dialog', null, document.body);
    view.className = 'settings-dialog';
    view.setAttribute('aria-labelledby', 'settings-title');
    var header = el('header', null, view); header.className = 'settings-header';
    var title = el('div', null, header);
    el('h2', '系统配置', title).id = 'settings-title';
    var scope = settingsTarget === 'backend' ? '板卡后端' : remoteBackendAvailable ? 'Web 节点' : '当前控制机';
    el('small', scope + ' · ' + '对所有使用者生效', title);
    var headActions = el('div', null, header); headActions.className = 'settings-actions';
    if (remoteBackendAvailable) {
      var node = el('select', null, headActions); node.setAttribute('aria-label', '配置所属节点');
      [['web', 'Web 节点'], ['backend', '板卡后端']].forEach(function (item) { var option = el('option', item[1], node); option.value = item[0]; });
      node.value = settingsTarget;
      node.addEventListener('change', function () {
        var target = node.value;
        node.value = settingsTarget;
        if (mayLeave()) open(target);
      });
    }
    button(headActions, '关闭', function () { if (mayLeave()) view.close(); });
    view.addEventListener('cancel', function (event) { if (!mayLeave()) event.preventDefault(); });
    view.addEventListener('close', function () {
      if (view === dialog) { dialog = null; ++openGeneration; }
      view.remove();
    });
    var layout = el('div', null, view); layout.className = 'settings-layout';
    var nav = el('div', null, layout); nav.className = 'settings-nav'; nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', '配置分类');
    body = el('div', null, layout); body.className = 'settings-body';
    var available = new Map(snapshot.fields.map(function (field) { return [field.name, field]; }));
    groups.forEach(function (group) {
      if (group[0] === 'advanced') renderPreferences(nav);
      groupsPage(nav, available, group);
    });
    renderTransfer(nav);
    var tabs = Array.from(nav.children);
    selectPage = function (tab) {
      tabs.forEach(function (item) {
        var selected = item === tab;
        item.setAttribute('aria-selected', selected ? 'true' : 'false');
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute('aria-controls')).hidden = !selected;
      });
      body.scrollTop = 0;
    };
    nav.addEventListener('keydown', function (event) {
      var index = tabs.indexOf(document.activeElement), offset = {ArrowLeft:-1, ArrowUp:-1, ArrowRight:1, ArrowDown:1}[event.key];
      if (index < 0 || (!offset && event.key !== 'Home' && event.key !== 'End')) return;
      event.preventDefault();
      var next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + offset + tabs.length) % tabs.length];
      selectPage(next);
      next.focus();
    });
    selectPage(tabs[0]);
    var footer = el('footer', null, view); footer.className = 'settings-footer';
    saveState = el('strong', null, footer); saveState.className = 'settings-save-state'; saveState.setAttribute('role', 'status');
    status = el('p', null, footer); status.setAttribute('role', 'status'); status.className = 'settings-status';
    var actions = el('div', null, footer); actions.className = 'settings-actions';
    preview = button(actions, '查看改动', whileReady(doPreview));
    save = button(actions, '保存配置', whileReady(doSave)); save.className = 'pri';
    activate = button(actions, '重启并应用', whileReady(async function () {
      await request({action: 'activate', revision: snapshot.revision});
      snapshot.activation = {state: 'applying'};
      message(settingsTarget === 'backend' ? '后端正在重启。请稍后重新连接设备。'
        : '已请求重启。请稍后刷新，端口或密码修改后需使用新地址与凭证。');
    }));
    fill(snapshot.configuration);
    if (snapshot.activation && snapshot.activation.state === 'failed') message(snapshot.activation.error, true);
    view.showModal();
    tabs[0].focus();
  }
  async function open(target) {
    target = target === 'backend' ? 'backend' : 'web';
    if (busy) return;
    var generation = ++openGeneration;
    var trigger = UI.byId('b-settings'), previous = dialog;
    if (trigger) trigger.disabled = true;
    if (previous) {
      busy = true; body.inert = true; updateActions();
      message('正在读取配置…');
    }
    try {
      var result = await Promise.all([request(null, target), preferenceRequest().catch(function () { return null; })]);
      if (generation !== openGeneration) return;
      snapshot = result[0];
      settingsTarget = target;
      if (target === 'web') remoteBackendAvailable = snapshot.remote_backend === true;
      devicePreferences = result[1];
      inputs = {}; editors = {}; pendingBrowser = null; busy = false;
      render();
    } catch (error) {
      if (generation !== openGeneration) return;
      if (previous && previous === dialog) {
        busy = false; body.inert = false; updateActions(); message(error.message, true);
      } else global.alert(error.message);
    } finally {
      if (trigger && generation === openGeneration) trigger.disabled = false;
    }
  }
  if (document && UI) { var trigger = UI.byId('b-settings'); if (trigger) trigger.addEventListener('click', function () { open('web'); }); }
  global.DeckLabSettings = {validateBrowser: validateBrowser};
})(window);
