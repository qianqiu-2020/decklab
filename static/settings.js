(function (global) {
  'use strict';
  var UI = global.SerialdUI, document = global.document;
  var groups = [
    ['访问与 MCP', ['PORT', 'BIND', 'AUTH_TOKEN', 'TRUST_LOOPBACK', 'MCP_ENABLED', 'MCP_PYTHON', 'MCP_ALLOWED_HOSTS', 'SHARE_SECONDS', 'UI_LANGUAGE']],
    ['串口与探测', ['BAUD', 'DATA_BITS', 'PARITY', 'STOP_BITS', 'FLOW_CONTROL', 'NEWLINE', 'PROBE', 'PROBE_COMMAND', 'AUTO_CONFIGURE_SSH']],
    ['登录默认值', ['SSH_USER', 'SSH_PASSWORD', 'CONSOLE_USER', 'CONSOLE_PASSWORD']],
    ['日志与传输', ['LOG_RETENTION_DAYS', 'DISK_MIN_FREE_MB', 'DISK_MIN_FREE_PERCENT', 'TRANSFER_MAX_FILES', 'TRANSFER_MAX_FILE_BYTES', 'TRANSFER_MAX_TOTAL_BYTES', 'TRANSFER_MIN_FREE_BYTES', 'TRANSFER_IDLE_SECONDS', 'TRANSFER_RETENTION_SECONDS', 'SCROLLBACK', 'SSH_LIMIT', 'SSH_IDLE']],
    ['服务路径', ['BASE', 'STATIC', 'SOCKET', 'HANDOFF', 'BY_PATH', 'RESCUE_BIN', 'AGENT_TOKENS_FILE', 'PROFILES', 'HARDWARE', 'FLASH', 'FLASH_EXECUTOR']]
  ];
  var labels = {
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
  var openGeneration = 0;
  function el(tag, text, parent) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function message(text) { status.textContent = text; }
  function redacted(value) { return value && typeof value === 'object' && value.$redacted === true; }
  async function request(payload) {
    var response = await UI.fetch('/api/settings', payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : undefined);
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || '配置请求失败');
    return data;
  }
  function configuration() {
    var result = {version: 1, environment: {}, documents: {}};
    snapshot.fields.forEach(function (field) {
      var input = inputs[field.name];
      result.environment[field.name] = field.secret && !input.dataset.changed && redacted(original.environment[field.name]) ? {$redacted: true} : input.value;
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
    var result = await request({action: 'export', include_secrets: includeSecrets});
    var pack = {format: 'decklab-settings', version: 1, configuration: result.configuration};
    if (browserBox.checked) pack.browser = browserPreferences();
    var url = global.URL.createObjectURL(new Blob([JSON.stringify(pack, null, 2) + '\n'], {type: 'application/json'}));
    var anchor = el('a'); anchor.href = url; anchor.download = includeSecrets ? 'decklab-private-backup.json' : 'decklab-settings.json'; anchor.click();
    global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 1000);
    message(includeSecrets ? '完整备份含密码和令牌，请私密保存。' : '已导出分享配置；密码字段与令牌已隐藏，自定义命令请自行检查。导入时隐藏字段保留目标服务现有值。');
  }
  async function importFile(file) {
    if (!file || file.size > 2 * 1024 * 1024) throw new Error('配置文件须小于 2 MiB');
    var pack = JSON.parse(await file.text());
    if (pack.format !== 'decklab-settings' || pack.version !== 1) throw new Error('不支持的配置包格式');
    var config = pack.configuration;
    pendingBrowser = pack.browser == null ? null : validateBrowser(pack.browser);
    await request({action: 'preview', revision: snapshot.revision, configuration: config});
    // Patch imports preserve omitted fields and documents, matching the server.
    fill({version: 1, environment: Object.assign({}, snapshot.configuration.environment, config.environment), documents: Object.assign({}, snapshot.configuration.documents, config.documents)});
    message('导入校验通过，尚未保存。请检查路径、板卡分配与硬件动作，再点“预览修改”。');
  }
  function run(action) { return function () { Promise.resolve().then(action).catch(function (error) { message(error.message); }); }; }
  function button(parent, title, action) { var node = el('button', title, parent); node.type = 'button'; node.addEventListener('click', run(action)); return node; }
  async function open() {
    var generation = ++openGeneration;
    try {
      var result = await request();
      if (generation !== openGeneration) return;
      snapshot = result;
      if (dialog) dialog.remove();
      inputs = {}; editors = {}; pendingBrowser = null;
      dialog = el('dialog', null, document.body); dialog.className = 'settings-dialog';
      var header = el('div', null, dialog); header.className = 'settings-header';
      el('h2', '系统配置', header);
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
          input.addEventListener('input', function () { input.dataset.changed = '1'; invalidate(); });
          inputs[name] = input;
          if (field.secret) button(label, '清空', function () { input.value = ''; input.dataset.changed = '1'; invalidate(); message('该字段将被清空，请预览后保存。'); });
        });
      });
      var bindings = el('details', null, body); el('summary', '板卡、硬件动作与 Agent 令牌（JSON）', bindings);
      el('p', '按现有配置格式编辑；profiles / hardware 最大 64 KiB，flash 最大 1 MiB。$redacted 标记表示保留已有密钥。清空对象 {} 表示移除该文档的全部分配。', bindings);
      Object.keys(docs).forEach(function (name) { var label = el('label', docs[name], bindings); var editor = el('textarea', null, label); editor.rows = 10; editor.spellcheck = false; editor.addEventListener('input', invalidate); editors[name] = editor; });
      var footer = el('div', null, dialog); footer.className = 'settings-footer';
      var browserLabel = el('label', null, footer); browserBox = el('input', null, browserLabel); browserBox.type = 'checkbox'; browserLabel.appendChild(document.createTextNode('导入 / 导出包含本浏览器语言与排序'));
      browserBox.addEventListener('change', invalidate);
      var actions = el('div', null, footer); actions.className = 'settings-actions';
      button(actions, '导出分享配置', function () { return download(false); });
      button(actions, '导出含密钥备份', function () { return download(true); });
      var file = el('input', null, actions); file.type = 'file'; file.accept = '.json,application/json'; file.hidden = true;
      file.addEventListener('change', run(function () { return importFile(file.files[0]); }));
      button(actions, '导入配置…', function () { file.value = ''; file.click(); });
      preview = button(actions, '预览修改', doPreview);
      save = button(actions, '保存配置', doSave);
      activate = button(actions, '重启并应用', async function () {
        activate.disabled = true;
        try { await request({action: 'activate', revision: snapshot.revision}); message('已请求重启。终端会暂时断开；请稍后刷新，端口或密码修改后需使用新地址与凭证。'); }
        catch (error) { activate.disabled = false; throw error; }
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
