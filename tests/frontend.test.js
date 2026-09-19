'use strict';

// 无浏览器、无网络、无 tty：执行页面原脚本，用可控的延迟模拟最危险的交错。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { TextDecoder } = require('node:util');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.events = {};
    this.dataset = {}; this.style = {}; this.hidden = false;
    this.checked = true; this.value = ''; this._text = '';
    this.classList = { toggle() {} };
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text; }
  set innerHTML(value) { this.textContent = value; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  emit(name, event = {}) {
    for (const fn of this.events[name] || []) fn(event);
  }
  click() { if (!this.disabled) this.emit('click', { stopPropagation() {} }); }
  focus() {}
  select() {}
  remove() { this.removed = true; }
}

function harness() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [], terminals = [], timers = new Map();
  let nextTimer = 0;
  class Terminal {
    constructor() {
      this.cols = 80; this.rows = 24; this.queued = []; this.parsed = '';
      this.decoder = new TextDecoder(); terminals.push(this);
    }
    loadAddon() {}
    open() {}
    dispose() { this.disposed = true; }
    onData(fn) { this.data = fn; }
    onSelectionChange(fn) { this.selection = fn; }
    attachCustomKeyEventHandler(fn) { this.key = fn; }
    getSelection() { return ''; }
    clearSelection() {}
    focus() {}
    refresh() {}
    write(data) { this.queued.push(data); }
    flush() {
      // 故意允许 dispose 后仍执行旧解析器，验证回调自身也能拦住 CSI 6n 应答。
      for (const data of this.queued.splice(0)) {
        const text = typeof data === 'string' ? data : this.decoder.decode(data, { stream: true });
        this.parsed += text;
        if (text.includes('\x1b[6n')) this.data('\x1b[1;1R');
      }
    }
  }
  const document = {
    getElementById: get, createElement: tag => new Element(tag),
    body: new Element('body'), addEventListener() {}, visibilityState: 'visible',
    execCommand: () => true,
  };
  const window = { addEventListener() {}, prompt: () => '新编号' };
  const sandbox = {
    document, window, Terminal, FitAddon: { FitAddon: class { fit() {} } },
    Uint8Array, AbortController, console,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id), setInterval() {},
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      requests.push({ url, options, body: options.body ? JSON.parse(options.body) : null,
        resolve: data => resolve({ json: () => Promise.resolve(data) }), reject, taken: false });
    }),
  };
  vm.runInNewContext(script, sandbox, { filename: 'index.html' });
  const take = (route, predicate = () => true) => {
    const request = requests.find(r => !r.taken && r.url.split('?')[0] === route && predicate(r));
    assert.ok(request, 'missing request: ' + route);
    request.taken = true;
    return request;
  };
  const sends = () => requests.filter(r => r.url === '/api/session-send');
  const select = key => {
    const card = get('grid').children.find(child => child.dataset.key === key);
    assert.ok(card, 'device card: ' + key); card.click();
    return terminals.at(-1);
  };
  const timer = delay => [...timers.entries()].find(([, t]) => t.delay === delay);
  const fire = id => { const t = timers.get(id); timers.delete(id); assert.ok(t); t.fn(); };
  return { get, document, requests, terminals, timers, take, sends, select, timer, fire };
}

async function drain() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
async function setup() {
  const h = harness();
  h.take('/api/devices').resolve({ devices: ['A', 'B'].map(key => ({
    key, label: key, dev: '/mock/' + key, alive: true, bytes: 0, probes: 0,
  })) });
  await drain();
  return h;
}
function readResponse(key, bytes, off = bytes.length, sid = 'serial') {
  return { key, sid, off, data_b64: Buffer.from(bytes).toString('base64'), dev: '/mock/' + key };
}
function chooseSSH(h, sid = 'ssh1') {
  const button = h.get('d-tabs').children.find(child => child.textContent === 'SSH ' + sid);
  assert.ok(button); button.click();
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('queued old xterm output and CSI 6n cannot reach the new device', async () => {
  const h = await setup();
  const old = h.select('A');
  h.take('/api/session').resolve(readResponse('A', 'A history\x1b[6n'));
  await drain();
  assert.equal(old.queued.length, 1);
  const current = h.select('B');
  assert.equal(old.disposed, true);
  old.flush();
  await drain();
  assert.equal(current.parsed, '');
  assert.equal(h.sends().length, 0);
  h.take('/api/session').resolve(readResponse('B', 'B live\x1b[6n'));
  await drain(); current.flush(); await drain();
  assert.deepEqual(h.sends().map(r => r.body), [{ key: 'B', sid: 'serial', data: '\x1b[1;1R' }]);
});

test('typing is ordered; unsent input and late send errors are discarded on switch', async () => {
  const h = await setup(); const old = h.select('A');
  old.data('1'); old.data('2'); old.data('3'); await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['1']);
  h.sends()[0].resolve({ ok: true }); await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['1', '2']);
  h.select('B'); h.get('d-status').textContent = 'B status';
  h.sends()[1].reject(new Error('old failure')); await drain();
  old.data('stale'); await drain();
  assert.equal(h.sends().length, 2);
  assert.equal(h.get('d-status').textContent, 'B status');
});

test('late session, log and identify responses stay with their original device', async () => {
  const h = await setup(); h.select('A');
  const sessions = h.take('/api/sessions'), logs = h.take('/api/logs');
  h.get('b-identify').click(); const identify = h.take('/api/identify');
  h.select('B');
  sessions.resolve({ key: 'A', sessions: [{ sid: 'ssh-old', kind: 'ssh' }] });
  logs.resolve({ key: 'A', label: 'A logs', files: [{ name: 'old.log', path: 'old', size: 1 }] });
  identify.resolve({ ok: true, serial: 'A serial' }); await drain();
  assert.deepEqual(h.get('d-tabs').children.map(x => x.textContent), ['串口', '']);
  assert.equal(h.get('d-logs').children.length, 0);
  assert.equal(h.get('d-info').hidden, true);
  assert.equal(h.get('d-status').textContent, '');
  assert.equal(identify.body.key, 'A');
});

test('late SSH connect response cannot switch the newly selected device', async () => {
  const h = await setup(); h.select('A');
  h.get('d-tabs').children.at(-1).children[0].click();
  const connect = h.take('/api/ssh-connect');
  h.select('B'); const current = h.terminals.at(-1);
  connect.resolve({ ok: true, sid: 'ssh-old', ip: '192.0.2.1' }); await drain();
  assert.equal(h.terminals.at(-1), current);
  assert.equal(h.get('d-key').textContent, 'B');
  assert.equal(h.get('d-status').textContent, '');
  assert.deepEqual(connect.body, { key: 'A' });
});

test('late rename and SSH close responses cannot mutate another device', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/sessions').resolve({ key: 'A', sessions: [
    { sid: 'serial', kind: 'serial' }, { sid: 'ssh1', kind: 'ssh' },
  ] }); await drain();
  h.get('b-rename').click(); const rename = h.take('/api/label');
  h.get('d-tabs').children.find(child => child.textContent === '×').click();
  const close = h.take('/api/session-close');
  h.select('B');
  h.take('/api/sessions').resolve({ key: 'B', sessions: [
    { sid: 'serial', kind: 'serial' }, { sid: 'ssh1', kind: 'ssh' },
  ] }); await drain(); chooseSSH(h);
  const current = h.terminals.at(-1);
  rename.resolve({ ok: true, label: 'A renamed' });
  close.resolve({ ok: true }); await drain();
  assert.equal(h.terminals.at(-1), current);
  assert.equal(h.get('d-name').textContent, 'B');
  assert.ok(h.get('d-tabs').children.some(child => child.textContent === 'SSH ssh1'));
  assert.deepEqual(close.body, { key: 'A', sid: 'ssh1' });
});

test('late read failure cannot replace the new view status', async () => {
  const h = await setup(); h.select('A'); const oldRead = h.take('/api/session');
  h.select('B'); h.get('d-status').textContent = 'B status';
  oldRead.reject(new Error('old network failure')); await drain();
  assert.equal(h.get('d-status').textContent, 'B status');
});

test('poll timer is cancelled and its callback cannot create a second poll chain', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/session').resolve(readResponse('A', '')); await drain();
  const [id, oldTimer] = h.timer(300);
  h.select('B');
  assert.equal(h.timers.has(id), false);
  const count = h.requests.filter(r => r.url.startsWith('/api/session?')).length;
  oldTimer.fn(); await drain();
  assert.equal(h.requests.filter(r => r.url.startsWith('/api/session?')).length, count);
});

test('missing SSH session performs full terminal, offset, resize and poll transition', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/sessions').resolve({ key: 'A', sessions: [
    { sid: 'serial', kind: 'serial' }, { sid: 'ssh1', kind: 'ssh' },
  ] }); await drain(); chooseSSH(h);
  const oldSSH = h.terminals.at(-1);
  const [, oldResize] = h.timer(250);
  h.take('/api/sessions').resolve({ key: 'A', sessions: [{ sid: 'serial', kind: 'serial' }] });
  await drain();
  assert.equal(oldSSH.disposed, true);
  assert.notEqual(h.terminals.at(-1), oldSSH);
  const lastRead = h.requests.filter(r => r.url.startsWith('/api/session?')).at(-1);
  assert.ok(lastRead.url.includes('sid=serial&off=0'));
  oldResize.fn(); await drain();
  assert.equal(h.requests.filter(r => r.url === '/api/session-resize').length, 0);
  oldSSH.data('stale SSH reply'); await drain();
  assert.equal(h.sends().length, 0);
});

test('split CJK bytes use one streaming xterm decoder per view', async () => {
  const h = await setup(); const term = h.select('A');
  const bytes = Buffer.from('中文');
  h.take('/api/session').resolve(readResponse('A', bytes.subarray(0, 2), 2));
  await drain(); assert.ok(term.queued[0] instanceof Uint8Array); term.flush();
  assert.equal(term.parsed, '');
  h.fire(h.timer(300)[0]);
  h.take('/api/session').resolve(readResponse('A', bytes.subarray(2), bytes.length));
  await drain(); term.flush(); assert.equal(term.parsed, '中文');
});

test('closing the panel cancels late paste and rescue effects', async () => {
  const h = await setup(); h.select('A');
  h.get('b-paste').click(); const paste = h.document.body.children.at(-1);
  h.get('b-rescue').click(); const rescue = h.take('/api/rescue');
  h.get('b-close').click();
  paste.emit('paste', { clipboardData: { getData: () => 'old clipboard' }, preventDefault() {} });
  rescue.resolve({ ok: false, recovered: false, note: 'not recovered' }); await drain();
  assert.equal(h.sends().length, 0);
  assert.equal(h.requests.filter(r => r.url === '/api/rescue').length, 1);
  assert.equal(h.get('detail').hidden, true);
  assert.equal(h.terminals[0].disposed, true);
});

test('rescue distinguishes helper success and requires an explicit rebind click', async () => {
  const h = await setup(); h.select('A'); h.get('b-rescue').click();
  h.take('/api/rescue').resolve({ ok: true, recovered: false, note: 'steps done', requires_setup: false });
  await drain();
  assert.ok(h.get('d-status').textContent.includes('尚未确认采集恢复'));
  assert.equal(h.get('b-rebind').hidden, false);
  assert.equal(h.requests.filter(r => r.url === '/api/rescue').length, 1);
  h.get('b-rebind').click();
  const rebind = h.take('/api/rescue');
  assert.deepEqual(rebind.body, { key: 'A', mode: 'rebind' });
  rebind.resolve({ ok: true, recovered: true, note: 'capture alive' }); await drain();
  assert.equal(h.get('b-rebind').hidden, true);
  assert.ok(h.get('d-status').textContent.includes('采集已恢复'));
});

(async () => {
  for (const [name, fn] of tests) {
    await fn(); console.log('ok - ' + name);
  }
  console.log(tests.length + ' frontend isolation tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
