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
  const fetch = async (route, options = {}) => {
    calls.push({ route, options });
    let body = {};
    if (route === '/api/access') body = { share_enabled: true, agent_share_enabled: true,
      open_access: false, lan_hosts: ['10.0.120.161'] };
    else if (route === '/api/agent-grants') body = { grants: active.slice() };
    else if (route === '/api/share-link') {
      if (rejectPageShare) throw new Error('network unavailable');
      body = { path: '/share/browser-token' };
    }
    else if (route === '/api/agent-share') {
      const request = JSON.parse(options.body);
      const grant = { id: 'abc123', actor: 'shared-agent/abc123', scope: request.scope,
        serial: request.serial, created: 1, expires: null, revoked: null };
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
  const context = {
    document: { getElementById: byId, createElement: () => new Element(), body: new Element(),
      execCommand: command => { assert.equal(command, 'copy'); legacyCopies++; return true; } },
    window: { SerialdShareSelection: () => ({ key: 'milkv', label: 'milkv', serial: 'SN1' }),
      prompt: () => { throw new Error('LAN IP should be discovered without prompting'); } },
    navigator: { clipboard: { writeText: async text => {
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
  assert.equal(byId('agent-share-address-mode').value, 'lan');
  assert.equal(byId('agent-share-url').value, 'http://10.0.120.161:8898');
  byId('agent-share-address-mode').value = 'local';
  byId('agent-share-address-mode').change();
  assert.equal(byId('agent-share-url').value, 'http://127.0.0.1:8898');
  byId('agent-share-address-mode').value = 'lan';
  byId('agent-share-address-mode').change();
  await byId('agent-share-create').click();
  const request = calls.find(call => call.route === '/api/agent-share');
  assert.deepEqual(JSON.parse(request.options.body), { scope: 'device', serial: 'SN1' });
  const text = byId('agent-share-text').value;
  assert.match(text, /seriald-ag-secret/);
  assert.match(text, /\nexport SERIALD_URL='http:\/\/10\.0\.120\.161:8898'\n/);
  assert.match(text, /skills\/seriald-board\/SKILL\.md/);
  assert.ok(text.indexOf(' exec --command ') < text.indexOf(' read --sid serial'));
  assert.doesNotMatch(text, /其他客户端仍可无令牌访问/);
  rejectClipboard = true;
  await byId('agent-share-copy').click();
  assert.equal(legacyCopies, 1);
  rejectClipboard = false;
  await byId('agent-share-copy').click();
  assert.equal(clipboard[0], text);
  await byId('b-share').click();
  assert.equal(clipboard.at(-1), 'http://10.0.120.161:8898/share/browser-token');
  const sharing = byId('b-device-share').click();
  assert.equal(byId('b-device-share').disabled, true);
  assert.equal(byId('b-share').disabled, true);
  await sharing;
  assert.equal(clipboard.at(-1), 'http://10.0.120.161:8898/share/browser-token#device=SN1');
  assert.equal(byId('device-share-status').textContent, '链接已复制，24 小时有效');
  assert.equal(byId('b-device-share').disabled, false);
  assert.equal(byId('b-share').disabled, false);
  rejectPageShare = true;
  await byId('b-device-share').click();
  assert.match(byId('device-share-status').textContent, /分享失败/);
  assert.equal(byId('b-device-share').disabled, false);
  assert.equal(byId('b-share').disabled, false);


  const row = byId('agent-share-grants').children[0];
  await row.children[1].click();
  assert.equal(active[0].revoked, 2);
  assert.equal(byId('agent-share-grants').children[0].children.length, 1);
  const openElements = {};
  const openById = id => openElements[id] ||= new Element();
  const openCalls = [];
  const openContext = { ...context,
    document: { ...context.document, getElementById: openById },
    fetch: async route => {
      openCalls.push(route);
      assert.equal(route, '/api/access');
      return { ok: true, json: async () => ({ open_access: true, share_enabled: false,
        agent_share_enabled: false, lan_hosts: ['10.0.120.161'] }) };
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static/share.js'), 'utf8'), openContext);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(openById('b-device-share').hidden, false);
  assert.equal(openById('b-device-share').disabled, false);
  await openById('b-device-share').click();
  assert.equal(clipboard.at(-1), 'http://10.0.120.161:8898/#device=SN1');
  assert.deepEqual(openCalls, ['/api/access']);
  console.log('ok - scoped agent handoff, self-hosted skill and revocation UI');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
