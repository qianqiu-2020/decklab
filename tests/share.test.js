'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() { this.events = {}; this.children = []; this.value = ''; this.style = {}; this.disabled = false; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { return this.events.click && this.events.click(); }
  change() { return this.events.change && this.events.change(); }
  appendChild(child) { this.children.push(child); return child; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  select() {}
}

async function main() {
  const elements = {};
  const byId = id => elements[id] ||= new Element();
  const calls = [];
  const active = [];
  const clipboard = [];
  let grantGate = null;
  let clipboardGate = null;
  const fetch = async (route, options = {}) => {
    calls.push({ route, options });
    let body = {};
    if (route === '/api/access') body = { share_enabled: true, agent_share_enabled: true,
      open_access: false, lan_hosts: ['192.0.2.161'], mcp_enabled: true, mcp_url: '/mcp', mcp_error: '' };
    else if (route === '/api/agent-grants') body = { grants: active.slice() };
    else if (route === '/api/share-link') {
      if (rejectPageShare) throw new Error('network unavailable');
      body = { path: '/share/browser-token' };
    }
    else if (route === '/api/agent-share') {
      const request = JSON.parse(options.body);
      if (grantGate) {
        const gate = grantGate;
        grantGate = null;
        await gate;
      }
      const grant = { id: 'abc123', actor: 'shared-agent/abc123', scope: request.scope,
        serial: request.serial, key: request.key, connection_id: request.connection_id,
        created: 1, expires: null, revoked: null };
      active.push(grant);
      body = { token: 'seriald-ag-secret', grant };
    } else if (route === '/api/agent-revoke') {
      active[0].revoked = 2;
      body = { ok: true };
    } else throw new Error('unexpected fetch: ' + route);
    return { ok: true, json: async () => body };
  };
  let rejectClipboard = false;
  let rejectPageShare = false;
  let legacyCopies = 0;
  let selected = { key: 'milkv', label: 'milkv', serial: 'SN1' };
  const context = {
    document: { getElementById: byId, createElement: () => new Element(), body: new Element(),
      execCommand: command => { assert.equal(command, 'copy'); legacyCopies++; return true; } },
    window: { SerialdShareSelection: () => selected,
      SerialdI18n: {locale: 'zh-CN', setDefaultLocale() {}},
      addEventListener(name, fn) { this.events ||= {}; this.events[name] = fn; },
      prompt: () => { throw new Error('LAN IP should be discovered without prompting'); } },
    navigator: { clipboard: { writeText: async text => {
      if (clipboardGate) {
        const gate = clipboardGate;
        clipboardGate = null;
        await gate;
      }
      if (rejectClipboard) throw new Error('NotAllowedError');
      clipboard.push(text);
    } } },
    location: { origin: 'http://127.0.0.1:8898', hostname: '127.0.0.1' },
    fetch, URL, Date, Promise, console
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static/share.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(byId('b-agent-share').disabled, false);
  assert.equal(byId('b-share').hidden, false);
  assert.equal(byId('b-device-share').hidden, false);

  byId('b-agent-share').click();
  assert.equal(byId('agent-share-scope').value, 'device');
  assert.equal(byId('agent-share-format').value, 'mcp');
  assert.equal(byId('agent-share-address-mode').value, 'lan');
  assert.equal(byId('agent-share-url').value, 'http://192.0.2.161:8898');
  byId('agent-share-address-mode').value = 'local';
  byId('agent-share-address-mode').change();
  assert.equal(byId('agent-share-url').value, 'http://127.0.0.1:8898');
  byId('agent-share-address-mode').value = 'lan';
  byId('agent-share-address-mode').change();
  await byId('agent-share-create').click();
  const request = calls.find(call => call.route === '/api/agent-share');
  assert.deepEqual(JSON.parse(request.options.body), { scope: 'device', serial: 'SN1' });
  const mcp = JSON.parse(byId('agent-share-text').value);
  assert.deepEqual(mcp, {mcpServers: {decklab: {url: 'http://192.0.2.161:8898/mcp',
    headers: {Authorization: 'Bearer seriald-ag-secret'}}}});
  assert.match(byId('agent-share-setup').textContent, /无需安装/);
  assert.equal(byId('agent-share-mcp-options').hidden, true);
  assert.equal(byId('agent-share-endpoint').value, 'http://192.0.2.161:8898/mcp');
  const originalMcpConfig = byId('agent-share-text').value;
  const grantsBeforeCommandChange = active.length;
  byId('agent-share-url').value = 'https://decklab.example';
  byId('agent-share-url').events.input();
  assert.equal(JSON.parse(byId('agent-share-text').value).mcpServers.decklab.url, 'https://decklab.example/mcp');
  assert.equal(active.length, grantsBeforeCommandChange, 'editing public host must reuse the grant');
  byId('agent-share-url').value = 'https://user:secret@decklab.example';
  byId('agent-share-url').events.input();
  assert.equal(byId('agent-share-text').value, '', 'invalid addresses must not leave a stale config to copy');
  assert.equal(byId('agent-share-copy').disabled, true);
  assert.equal(byId('agent-share-create').disabled, true);
  byId('agent-share-url').value = 'http://192.0.2.161:8898';
  byId('agent-share-url').events.input();
  assert.equal(byId('agent-share-text').value, originalMcpConfig);
  byId('agent-share-format').value = 'stdio';
  byId('agent-share-format').change();
  assert.equal(byId('agent-share-mcp-options').hidden, false);
  const originalStdioConfig = byId('agent-share-text').value;
  assert.deepEqual(JSON.parse(originalStdioConfig), {mcpServers: {decklab: {command: 'seriald-mcp', args: [], env: {
    SERIALD_URL: 'http://192.0.2.161:8898', SERIALD_TOKEN: 'seriald-ag-secret'}}}});
  byId('agent-share-command').value = '/home/ai/.local/share/decklab-mcp/bin/seriald-mcp';
  byId('agent-share-command').events.input();
  assert.equal(JSON.parse(byId('agent-share-text').value).mcpServers.decklab.command,
    '/home/ai/.local/share/decklab-mcp/bin/seriald-mcp');
  assert.equal(active.length, grantsBeforeCommandChange, 'editing launch path must reuse the grant');
  byId('agent-share-command').value = 'seriald-mcp'; byId('agent-share-command').events.input();
  assert.equal(byId('agent-share-text').value, originalStdioConfig);
  byId('agent-share-format').value = 'cli';
  byId('agent-share-format').change();
  const text = byId('agent-share-text').value;
  assert.match(text, /seriald-ag-secret/);
  assert.match(text, /\nexport SERIALD_URL='http:\/\/192\.0\.2\.161:8898'\n/);
  assert.match(text, /skills\/seriald-board\/SKILL\.md/);
  assert.match(text, /SKILL\.md\?lang=zh-CN/);
  assert.ok(text.indexOf(' exec --command ') < text.indexOf(' read --sid serial'));
  assert.doesNotMatch(text, /其他客户端仍可无令牌访问/);
  rejectClipboard = true;
  await byId('agent-share-copy').click();
  assert.equal(legacyCopies, 1);
  rejectClipboard = false;
  await byId('agent-share-copy').click();
  assert.equal(clipboard.at(-1), text);
  const grantsBeforeSwitch = active.length;
  context.window.SerialdI18n.locale = 'en';
  context.window.events['decklab-language-change']();
  const english = byId('agent-share-text').value;
  assert.doesNotMatch(english, /[\u4e00-\u9fff]/);
  assert.match(english, /SKILL\.md\?lang=en/);
  assert.match(english, /seriald-ag-secret/);
  assert.ok(english.indexOf(' exec --command ') < english.indexOf(' read --sid serial'));
  assert.equal(active.length, grantsBeforeSwitch, 'language changes must reuse the existing grant');
  await byId('agent-share-copy').click();
  assert.equal(clipboard.at(-1), english);
  context.window.SerialdI18n.locale = 'zh-CN';
  context.window.events['decklab-language-change']();
  assert.equal(byId('agent-share-text').value, text);
  await byId('b-share').click();
  assert.equal(clipboard.at(-1), 'http://192.0.2.161:8898/share/browser-token');
  const sharing = byId('b-device-share').click();
  assert.equal(byId('b-device-share').disabled, true);
  assert.equal(byId('b-share').disabled, true);
  await sharing;
  assert.equal(clipboard.at(-1), 'http://192.0.2.161:8898/share/browser-token#device=SN1');
  assert.equal(byId('device-share-status').textContent, '链接已复制，24 小时有效');
  assert.equal(byId('b-device-share').disabled, false);
  assert.equal(byId('b-share').disabled, false);

  selected = {key: 'unprovisioned-usb', label: '待识别 COM5', dev: 'COM5', alive: true,
    connection_id: 'fresh-connection', serial: ''};
  byId('b-agent-share').click();
  assert.equal(byId('agent-share-create').disabled, false, 'identity must not gate access');
  assert.match(byId('agent-share-hint').textContent, /无需识别/);
  await byId('agent-share-create').click();
  assert.deepEqual(JSON.parse(calls.filter(call => call.route === '/api/agent-share').at(-1).options.body),
    {scope: 'connection', key: 'unprovisioned-usb', connection_id: 'fresh-connection'});
  assert.equal(JSON.parse(byId('agent-share-text').value).mcpServers.decklab.headers.Authorization, 'Bearer seriald-ag-secret');
  const grantCount = active.length;
  byId('agent-share-format').value = 'cli'; byId('agent-share-format').change();
  assert.match(byId('agent-share-text').value, /当前串口连接/);
  assert.doesNotMatch(byId('agent-share-text').value, / exec --command /, 'raw access must not require SSH');
  assert.equal(active.length, grantCount, 'format changes reuse the current authorization');
  selected = {key: 'unprovisioned-usb', label: 'COM5', alive: false, serial: ''};
  byId('b-agent-share').click();
  assert.equal(byId('agent-share-create').disabled, true, 'offline unidentified ports cannot create grants');
  assert.equal(byId('agent-share-scope').value, 'device', 'never widen scope automatically');
  selected = { key: 'milkv', label: 'milkv', serial: 'SN1' };
  rejectPageShare = true;
  await byId('b-device-share').click();
  assert.match(byId('device-share-status').textContent, /分享失败/);
  assert.equal(byId('b-device-share').disabled, false);
  assert.equal(byId('b-share').disabled, false);


  const row = byId('agent-share-grants').children[0];
  await row.children[1].click();
  assert.equal(active[0].revoked, 2);
  assert.equal(byId('agent-share-grants').children[0].children.length, 1);

  selected = {key: 'board-a', label: 'board-a', serial: 'SERIAL_A'};
  byId('b-agent-share').click();
  let finishGrant;
  grantGate = new Promise(resolve => { finishGrant = resolve; });
  const pendingGrant = byId('agent-share-create').click();
  const requestsWhilePending = calls.filter(call => call.route === '/api/agent-share').length;
  byId('agent-share-format').value = 'cli'; byId('agent-share-format').change();
  byId('agent-share-scope').value = 'all'; byId('agent-share-scope').change();
  assert.equal(byId('agent-share-scope').value, 'device', 'pending scope cannot silently change');
  assert.equal(byId('agent-share-create').disabled, true);
  await byId('agent-share-create').click();
  assert.equal(calls.filter(call => call.route === '/api/agent-share').length, requestsWhilePending,
    'format/address changes cannot duplicate an in-flight grant request');
  byId('agent-share-close').click();
  selected = {key: 'board-b', label: 'board-b', serial: 'SERIAL_B'};
  byId('b-agent-share').click();
  const copiesBeforeLateGrant = clipboard.length;
  finishGrant();
  await pendingGrant;
  assert.equal(byId('agent-share-text').value, '', 'late grant for A cannot populate B');
  assert.equal(clipboard.length, copiesBeforeLateGrant, 'closing a dialog cancels late credential copying');
  assert.match(byId('agent-share-hint').textContent, /SERIAL_B/);
  assert.equal(byId('agent-share-create').disabled, false, 'old finally cannot alter the new dialog');
  await byId('agent-share-create').click();
  assert.ok(byId('agent-share-text').value);
  byId('agent-share-scope').value = 'all'; byId('agent-share-scope').change();
  assert.equal(byId('agent-share-text').value, '', 'changed scope must not display a differently scoped grant');
  assert.equal(byId('agent-share-copy').disabled, true);

  selected = {key: 'board-a', label: 'board-a', serial: 'SERIAL_A'};
  byId('b-agent-share').click();
  await byId('agent-share-create').click();
  let rejectDeferredClipboard;
  clipboardGate = new Promise((resolve, reject) => { rejectDeferredClipboard = reject; });
  const pendingCopy = byId('agent-share-copy').click();
  byId('agent-share-close').click();
  selected = {key: 'board-b', label: 'board-b', serial: 'SERIAL_B'};
  byId('b-agent-share').click();
  const fallbackCopiesBeforeClose = legacyCopies;
  rejectDeferredClipboard(new Error('delayed clipboard rejection'));
  await pendingCopy;
  assert.equal(legacyCopies, fallbackCopiesBeforeClose, 'old credentials cannot enter a late clipboard fallback');
  assert.equal(byId('agent-share-text').value, '');
  assert.match(byId('agent-share-hint').textContent, /SERIAL_B/);
  selected = { key: 'milkv', label: 'milkv', serial: 'SN1' };

  const openElements = {};
  const openById = id => openElements[id] ||= new Element();
  const openCalls = [];
  const openContext = { ...context,
    document: { ...context.document, getElementById: openById },
    fetch: async route => {
      openCalls.push(route);
      if (route === '/api/agent-grants') return {ok: true, json: async () => ({grants: []})};
      assert.equal(route, '/api/access');
      return { ok: true, json: async () => ({ open_access: true, share_enabled: false,
        agent_share_enabled: false, lan_hosts: ['192.0.2.161'] }) };
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static/share.js'), 'utf8'), openContext);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(openById('b-device-share').hidden, false);
  assert.equal(openById('b-device-share').disabled, false);
  await openById('b-device-share').click();
  assert.equal(clipboard.at(-1), 'http://192.0.2.161:8898/#device=SN1');
  assert.deepEqual(openCalls, ['/api/access']);

  openById('b-agent-share').click();
  assert.equal(openById('agent-share-format').value, 'mcp');
  assert.equal(openById('agent-share-create').disabled, true, 'unavailable server must not offer a fake remote config');
  assert.match(openById('agent-share-mcp-status').textContent, /服务端/);
  await openById('agent-share-create').click();
  assert.equal(openCalls.includes('/api/agent-share'), false, 'missing MCP must fail before minting an unused grant');
  openById('agent-share-format').value = 'stdio';
  openById('agent-share-format').change();
  assert.equal(openById('agent-share-create').disabled, false, 'stdio remains an explicit compatibility path');
  console.log('ok - scoped agent handoff, self-hosted skill and revocation UI');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
