'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() { this.events = {}; this.children = []; this.value = ''; this.style = {}; this.disabled = false; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { return this.events.click && this.events.click(); }
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
    if (route === '/api/access') body = { share_enabled: false, agent_share_enabled: true, open_access: true };
    else if (route === '/api/agent-grants') body = { grants: active.slice() };
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
  const context = {
    document: { getElementById: byId, createElement: () => new Element(), body: new Element() },
    window: { SerialdShareSelection: () => ({ key: 'milkv', label: 'milkv', serial: 'SN1' }) },
    navigator: { clipboard: { writeText: async text => { clipboard.push(text); } } },
    location: { origin: 'http://192.0.2.10:8898', hostname: '192.0.2.10' },
    fetch, URL, Date, Promise, console
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static/share.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(byId('b-agent-share').disabled, false);

  byId('b-agent-share').click();
  assert.equal(byId('agent-share-scope').value, 'device');
  await byId('agent-share-create').click();
  const request = calls.find(call => call.route === '/api/agent-share');
  assert.deepEqual(JSON.parse(request.options.body), { scope: 'device', serial: 'SN1' });
  const text = byId('agent-share-text').value;
  assert.match(text, /seriald-ag-secret/);
  assert.match(text, /skills\/seriald-board\/SKILL\.md/);
  assert.ok(text.indexOf(' exec --command ') < text.indexOf(' read --sid serial'));
  assert.match(text, /其他客户端仍可无令牌访问/);
  await byId('agent-share-copy').click();
  assert.equal(clipboard[0], text);

  const row = byId('agent-share-grants').children[0];
  await row.children[1].click();
  assert.equal(active[0].revoked, 2);
  assert.equal(byId('agent-share-grants').children[0].children.length, 1);
  console.log('ok - scoped agent handoff, self-hosted skill and revocation UI');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
