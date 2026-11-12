(function (global) {
  'use strict';
  var UI = global.SerialdUI, document = global.document;
  var groups = [
    ['节点间连接', ['BACKEND_URL', 'BACKEND_TOKEN', 'BACKEND_CA', 'BACKEND_BIND', 'BACKEND_PORT', 'BACKEND_CERT', 'BACKEND_KEY']],
    ['访问与 MCP', ['PORT', 'BIND', 'AUTH_TOKEN', 'TRUST_LOOPBACK', 'MCP_ENABLED', 'MCP_PYTHON', 'MCP_ALLOWED_HOSTS', 'SHARE_SECONDS', 'UI_LANGUAGE']],
    ['串口与探测', ['BAUD', 'DATA_BITS', 'PARITY', 'STOP_BITS', 'FLOW_CONTROL', 'NEWLINE', 'PROBE', 'PROBE_COMMAND', 'AUTO_CONFIGURE_SSH']],
    ['登录默认值', ['SSH_USER', 'SSH_PASSWORD', 'CONSOLE_USER', 'CONSOLE_PASSWORD']],
    ['日志与传输', ['LOG_RETENTION_DAYS', 'DISK_MIN_FREE_MB', 'DISK_MIN_FREE_PERCENT', 'TRANSFER_MAX_FILES', 'TRANSFER_MAX_FILE_BYTES', 'TRANSFER_MAX_TOTAL_BYTES', 'TRANSFER_MIN_FREE_BYTES', 'TRANSFER_IDLE_SECONDS', 'TRANSFER_RETENTION_SECONDS', 'SCROLLBACK', 'SSH_LIMIT', 'SSH_IDLE']],
    ['服务路径', ['BASE', 'STATIC', 'SOCKET', 'HANDOFF', 'BY_PATH', 'RESCUE_BIN', 'AGENT_TOKENS_FILE', 'PROFILES', 'HARDWARE', 'FLASH', 'FLASH_EXECUTOR', 'FLASH_LOADERS']]
  ];
  var labels = {
    BACKEND_URL: '后端 HTTPS 地址', BACKEND_TOKEN: '节点间访问密钥', BACKEND_CA: '后端 CA 证书文件',
    BACKEND_BIND: '后端监听地址', BACKEND_PORT: '后端监听端口', BACKEND_CERT: '后端 TLS 证书文件', BACKEND_KEY: '后端 TLS 私钥文件',
    FLASH_LOADERS: '预备刷机环境清单',
    UI_LANGUAGE: '默认界面语言', SCROLLBACK: '实时输出缓存（字节）', SSH_LIMIT: '每设备 SSH 会话上限', SSH_IDLE: 'SSH 空闲超时（秒）', SHARE_SECONDS: '网页分享有效期（秒）',
    PORT: '监听端口', BIND: '监听地址', AUTH_TOKEN: '主访问密码', TRUST_LOOPBACK: '信任本机访问（Windows）',
    MCP_ENABLED: '启用服务端 MCP', MCP_PYTHON: 'MCP Python（留空使用内置运行时）', MCP_ALLOWED_HOSTS: 'MCP 代理域名（逗号分隔）',
    BAUD: '波特率', DATA_BITS: '数据位', PARITY: '校验位', STOP_BITS: '停止位', FLOW_CONTROL: '流控', NEWLINE: '换行方式',
    PROBE: '默认探测模式', PROBE_COMMAND: '自定义探测命令', AUTO_CONFIGURE_SSH: '允许显式配置 SSH',
    SSH_USER: 'SSH 用户', SSH_PASSWORD: 'SSH 密码', CONSOLE_USER: '串口用户', CONSOLE_PASSWORD: '串口密码',
    LOG_RETENTION_DAYS: '日志保留天数（0 为不清理）', DISK_MIN_FREE_MB: '磁盘最低剩余 MB', DISK_MIN_FREE_PERCENT: '磁盘最低剩余百分比',
    TRANSFER_MAX_FILES: '暂存文件数上限', TRANSFER_MAX_FILE_BYTES: '单文件上限（字节）', TRANSFER_MAX_TOTAL_BYTES: '暂存总量上限（字节）',
    TRANSFER_MIN_FREE_BYTES: '暂存保留空闲（字节）', TRANSFER_IDLE_SECONDS: '未完成上传超时（秒）', TRANSFER_RETENTION_SECONDS: '暂存保留时间（秒）',
    BASE: '运行数据目录', STATIC: '页面资源目录', SOCKET: '采集器通信路径（Linux）', HANDOFF: '重启接续文件',
    BY_PATH: '串口发现目录（Linux）', RESCUE_BIN: 'USB 恢复工具', AGENT_TOKENS_FILE: 'Agent 令牌文件',
    PROFILES: '板卡配置文件', HARDWARE: '硬件动作文件', FLASH: '刷机配置文件', FLASH_EXECUTOR: '自动刷机执行器'
  };
  var docs = {profiles: '板卡配置与分配', hardware: '硬件动作', flash: '刷机绑定', agent_tokens: '固定 Agent 令牌'};
  var snapshot, inputs = {}, editors = {}, original, previewText = '', pendingBrowser = null;
  var dialog, status, preview, save, activate, browserBox;
  var openGeneration = 0, settingsTarget = 'web', remoteBackendAvailable = false;
  var devicePreferences = null, preferenceEditor, preferenceSave, preferencePreview = '', preferenceStatus, includePreferences;
  function el(tag, text, parent) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function message(text) { status.textContent = text; }
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
    preferencePreview = ''; preferenceStatus.textContent = '设备偏好已保存并生效。身份、日志和已验证 SSH 凭据保持原有归属。';
  }
  function configuration() {
    var result = {version: 1, environment: {}, documents: {}};
    snapshot.fields.forEach(function (field) {
      var input = inputs[field.name];
      result.environment[field.name] = field.secret && !input.value && !input.dataset.clear && redacted(original.environment[field.name]) ? {$redacted: true} : input.value;
    });
    Object.keys(editors).forEach(function (key) {
      if (editors[key].value.trim()) result.documents[key] = JSON.parse(editors[key].value);
      else if (original.documents[key]) result.documents[key] = {};
    });
    return result;
  }
  function invalidate() { save.disabled = true; previewText = ''; }
  function fill(data) {
    original = data;
    snapshot.fields.forEach(function (field) {
      var input = inputs[field.name], value = data.environment[field.name];
      input.value = redacted(value) ? '' : value;
      input.placeholder = redacted(value) ? '已设置；留空保留，清空请点右侧按钮' : '';
      delete input.dataset.changed;
      delete input.dataset.clear;
    });
    Object.keys(editors).forEach(function (key) { editors[key].value = data.documents[key] ? JSON.stringify(data.documents[key], null, 2) : ''; });
    invalidate();
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
    save.disabled = true;
    message('正在校验配置…');
    var result = await request({action: 'preview', revision: snapshot.revision, configuration: config});
    if (view !== dialog) return;
    previewText = JSON.stringify(config);
    save.disabled = false;
    message('校验通过。将修改：' + (result.changes.join('、') || '无服务端变化') + (pendingBrowser && browserBox.checked ? '；并导入本浏览器的语言与排序' : '') + '。保存后需重启生效。');
  }
  async function doSave() {
    var view = dialog, preferences = pendingBrowser, includeBrowser = browserBox.checked;
    var browserError = '';
    var config = configuration();
    if (JSON.stringify(config) !== previewText) throw new Error('配置已变化，请重新预览');
    save.disabled = true;
    var result = await request({action: 'save', revision: snapshot.revision, configuration: config});
    if (preferences && includeBrowser) {
      try {
        if (preferences.language != null) global.localStorage.setItem('decklab-language', preferences.language);
        if (preferences.deviceOrder != null) global.localStorage.setItem('decklab.device-order.v1', JSON.stringify(preferences.deviceOrder));
      } catch (_) { browserError = ' 当前浏览器拒绝保存偏好，请检查隐私设置。'; }
    }
    if (view !== dialog) return;
    snapshot.revision = result.revision;
    pendingBrowser = null;
    var fresh = await request();
    if (view !== dialog) return;
    snapshot = fresh;
    fill(snapshot.configuration);
    activate.disabled = !snapshot.restart_available;
    message('配置已保存，尚未重启生效。浏览器偏好如有导入，刷新页面后生效。' + browserError);
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
    if (pack.format !== 'decklab-settings' || pack.version !== 1) throw new Error('不支持的配置包格式');
    var config = pack.configuration;
    var proposedBrowser = pack.browser == null ? null : validateBrowser(pack.browser);
    await request({action: 'preview', revision: target.revision, configuration: config}, nodeTarget);
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
    if (pack.preferences != null) preferenceStatus.textContent = preferenceError
      ? preferenceError + '；设备偏好尚未保存，请编辑后重新预览。'
      : '已导入设备偏好，尚未保存。请单独预览并保存设备偏好。';
    // Patch imports preserve omitted fields and documents, matching the server.
    fill({version: 1, environment: Object.assign({}, target.configuration.environment, config.environment), documents: Object.assign({}, target.configuration.documents, config.documents)});
    message('导入校验通过，尚未保存。请检查路径、板卡分配与硬件动作，再点“预览修改”。' + (preferenceError ? ' 设备偏好需另行修正。' : ''));
  }
  function run(action) { return function () { var view = dialog; Promise.resolve().then(action).catch(function (error) { if (view === dialog) message(error.message); }); }; }
  function button(parent, title, action) { var node = el('button', title, parent); node.type = 'button'; node.addEventListener('click', run(action)); return node; }
  async function open(target) {
    var generation = ++openGeneration;
    settingsTarget = target === 'backend' ? 'backend' : 'web';
    if (dialog) { dialog.remove(); dialog = null; }
    try {
      var result = await request();
      if (generation !== openGeneration) return;
      snapshot = result;
      if (settingsTarget === 'web') remoteBackendAvailable = result.remote_backend === true;
      var preferences = null;
      try { preferences = await preferenceRequest(); } catch (_) { /* Host settings remain usable without a collector. */ }
      if (generation !== openGeneration) return;
      devicePreferences = preferences;
      if (dialog) dialog.remove();
      inputs = {}; editors = {}; pendingBrowser = null;
      dialog = el('dialog', null, document.body); dialog.className = 'settings-dialog';
      var header = el('div', null, dialog); header.className = 'settings-header';
      el('h2', settingsTarget === 'backend' ? '后端节点配置' : '系统配置', header);
      if (remoteBackendAvailable) {
        var node = el('select', null, header); node.setAttribute('aria-label', '配置所属节点');
        [['web', 'Web 节点'], ['backend', '后端节点']].forEach(function (item) { var option = el('option', item[1], node); option.value = item[0]; });
        node.value = settingsTarget; node.addEventListener('change', function () { open(node.value); });
      }
      button(header, '关闭', function () { dialog.close(); });
      el('p', '服务端设置由管理员共享。设备标签、探测覆盖和已验证 SSH 密码仍在各设备页面管理。左侧排序与语言只影响当前浏览器。', dialog);
      el('p', '内嵌 JSON 文档优先于文件路径；修改不会执行硬件命令。数据目录切换不会搬迁已有档案。', dialog);
      var body = el('div', null, dialog); body.className = 'settings-body';
      groups.forEach(function (group) {
        var section = el('details', null, body); section.open = group === groups[0];
        el('summary', group[0], section);
        var grid = el('div', null, section); grid.className = 'settings-grid';
        group[1].forEach(function (name) {
          var field = snapshot.fields.find(function (item) { return item.name === name; });
          if (!field) return;
          var label = el('label', labels[name] || name, grid), input;
          el('small', 'SERIALD_' + name, label);
          if (field.choices) { input = el('select', null, label); field.choices.forEach(function (value) { var option = el('option', value, input); option.value = value; }); }
          else { input = el(name === 'PROBE_COMMAND' ? 'textarea' : 'input', null, label); if (name !== 'PROBE_COMMAND') input.type = field.secret ? 'password' : 'text'; }
          input.autocomplete = 'off'; input.spellcheck = false;
          input.addEventListener('input', function () { input.dataset.changed = '1'; delete input.dataset.clear; invalidate(); });
          inputs[name] = input;
          if (field.secret) button(label, '清空', function () { input.value = ''; input.dataset.clear = '1'; invalidate(); message('该字段将被清空，请预览后保存。'); });
        });
      });
      var bindings = el('details', null, body); el('summary', '板卡、硬件动作与 Agent 令牌（JSON）', bindings);
      el('p', '按现有配置格式编辑；profiles / hardware 最大 64 KiB，flash 最大 1 MiB。$redacted 标记表示保留已有密钥。清空对象 {} 表示移除该文档的全部分配。', bindings);
      Object.keys(docs).filter(function (name) { return !snapshot.document_names || snapshot.document_names.indexOf(name) >= 0; }).forEach(function (name) { var label = el('label', docs[name], bindings); var editor = el('textarea', null, label); editor.rows = 10; editor.spellcheck = false; editor.addEventListener('input', invalidate); editors[name] = editor; });
      var deviceSection = el('details', null, body); el('summary', '设备标签与端口探测覆盖（JSON）', deviceSection);
      el('p', '服务设置与设备偏好分别保存。设备偏好即时生效，不修改身份或日志归属；未知板卡须先识别。端口 label / probe 为 null 时清除该项，省略则保留。', deviceSection);
      preferenceEditor = el('textarea', null, deviceSection); preferenceEditor.rows = 12; preferenceEditor.spellcheck = false;
      preferenceEditor.value = preferences ? JSON.stringify(preferences.configuration, null, 2) : '';
      preferenceEditor.disabled = !preferences;
      preferenceEditor.addEventListener('input', function () { preferencePreview = ''; preferenceSave.disabled = true; });
      var preferenceActions = el('div', null, deviceSection); preferenceActions.className = 'settings-actions';
      var preferenceCheck = button(preferenceActions, '预览设备偏好', previewPreferences); preferenceCheck.disabled = !preferences;
      preferenceSave = button(preferenceActions, '保存设备偏好', savePreferences); preferenceSave.disabled = true;
      preferenceStatus = el('p', preferences ? '按序列号更新已登记板卡，按串口位置更新端口偏好。' : '采集器未就绪，设备偏好暂不可用。', deviceSection); preferenceStatus.setAttribute('role', 'status');
      var footer = el('div', null, dialog); footer.className = 'settings-footer';
      var browserLabel = el('label', null, footer); browserBox = el('input', null, browserLabel); browserBox.type = 'checkbox'; browserLabel.appendChild(document.createTextNode('导入 / 导出包含本浏览器语言与排序'));
      browserBox.addEventListener('change', invalidate);
      var preferenceLabel = el('label', null, footer); includePreferences = el('input', null, preferenceLabel); includePreferences.type = 'checkbox'; includePreferences.disabled = !preferences; preferenceLabel.appendChild(document.createTextNode('导出包含设备标签与端口探测覆盖'));
      var actions = el('div', null, footer); actions.className = 'settings-actions';
      button(actions, '导出分享配置', function () { return download(false); });
      button(actions, '导出含密钥备份', function () { return download(true); });
      var file = el('input', null, actions); file.type = 'file'; file.accept = '.json,application/json'; file.hidden = true;
      file.addEventListener('change', run(function () { return importFile(file.files[0]); }));
      button(actions, '导入配置…', function () { file.value = ''; file.click(); });
      preview = button(actions, '预览修改', doPreview);
      save = button(actions, '保存配置', doSave);
      activate = button(actions, '重启并应用', async function () {
        var view = dialog;
        activate.disabled = true;
        try { await request({action: 'activate', revision: snapshot.revision}); if (view !== dialog) return; message('已请求重启。终端会暂时断开；请稍后刷新，端口或密码修改后需使用新地址与凭证。'); }
        catch (error) { if (view === dialog) activate.disabled = false; throw error; }
      });
      activate.disabled = !snapshot.restart_available || snapshot.active_revision === snapshot.revision;
      status = el('p', null, footer); status.setAttribute('role', 'status'); status.className = 'settings-status';
      fill(snapshot.configuration);
      message(snapshot.active_revision === snapshot.revision ? '配置已加载。' : '当前有保存的配置等待应用；重启会关闭 SSH 会话并短暂中断终端。');
      if (snapshot.activation && snapshot.activation.state === 'failed') message(snapshot.activation.error);
      dialog.showModal();
    } catch (error) { global.alert(error.message); }
  }
  if (document && UI) { var trigger = UI.byId('b-settings'); if (trigger) trigger.addEventListener('click', open); }
  global.SerialdSettings = {validateBrowser: validateBrowser};
})(window);
