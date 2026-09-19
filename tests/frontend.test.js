'use strict';

// 无浏览器、无网络、无 tty：执行页面原脚本，用可控的延迟模拟最危险的交错。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { TextDecoder } = require('node:util');

const script = ['ui-common.js', 'terminal-policy.js', 'app.js'].map(name =>
  fs.readFileSync(path.join(__dirname, '..', 'static', name), 'utf8')).join('\n');

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
      this.csi = new Map();
      this.parser = {
        registerCsiHandler: (id, fn) => this.csi.set((id.prefix || '') + id.final, fn),
        registerOscHandler() {}, registerDcsHandler() {},
      };
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
    write(data, callback) { this.queued.push({ data, callback }); }
    flush() {
      // 故意允许 dispose 后仍执行旧解析器，验证回调自身也能拦住 CSI 6n 应答。
      for (const { data, callback } of this.queued.splice(0)) {
        const text = typeof data === 'string' ? data : this.decoder.decode(data, { stream: true });
        this.parsed += text;
        if (text.includes('\x1b[6n') && !this.csi.get('n')?.([6])) this.data('\x1b[1;184R');
        if (callback) callback();
      }
    }
  }
  const document = {
    getElementById: get, createElement: tag => new Element(tag),
    body: new Element('body'), events: {}, activeElement: null,
    addEventListener(name, fn) { (this.events[name] ||= []).push(fn); },
    emit(name, event = {}) { for (const fn of this.events[name] || []) fn(event); },
    visibilityState: 'visible',
    execCommand: () => true,
  };
  const sandbox = {
    document, Terminal, FitAddon: { FitAddon: class { fit() {} } },
    Uint8Array, AbortController, console,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id), setInterval() {},
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      requests.push({ url, options, body: options.body ? JSON.parse(options.body) : null,
        resolve: data => resolve({ json: () => Promise.resolve(data) }), reject, taken: false });
    }),
  };
  // Browser globals live on window.  Mirror that relationship so separately loaded
  // modules expose the same globals here as they do in a real page.
  sandbox.window = sandbox;
  sandbox.addEventListener = function () {};
  sandbox.prompt = () => '新编号';
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
  assert.equal(h.sends().length, 0, 'current serial view is also a passive renderer');
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

test('provisional identity is labeled as a candidate and warns about archive promotion', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [{ key: 'milkv2', label: 'milkv2', serial: '0059D968E583C49A',
    alive: true, dev: '/mock/ttyUSB1', info: { serial: '0059D968E583C49A',
      serial_source: 'soc-sysfs', identity_status: 'provisional' } }] });
  await drain(); h.select('milkv2');
  const rows = h.get('d-info').children.map(row => row.children.map(x => x.textContent).join('='));
  assert.ok(rows.includes('候选身份编号=0059D968E583C49A'));
  assert.ok(rows.includes('身份状态=待确认（使用临时档案目录）'));
  const cardText = h.get('grid').children[0].children.map(x => x.textContent).join(' ');
  assert.ok(cardText.includes('尚未正式确认'));
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
  assert.deepEqual(connect.body, { key: 'A', actor: 'web' });
});

test('SSH identification adopts the canonical label before reading or sending', async () => {
  const h = await setup(); h.select('A');
  h.get('d-tabs').children.at(-1).children[0].click();
  h.take('/api/ssh-connect').resolve({ ok: true, sid: 'ssh123', ip: '192.0.2.1',
    device_key: 'musepi2', serial: 'SN001' });
  await drain();
  const currentRead = h.requests.filter(r => r.url.startsWith('/api/session?')).at(-1);
  const query = new URL(currentRead.url, 'http://fixture').searchParams;
  assert.equal(query.get('key'), 'musepi2');
  assert.equal(query.get('serial'), 'SN001');
  assert.equal(query.get('sid'), 'ssh123');
  h.terminals.at(-1).data('uname -a\r'); await drain();
  assert.deepEqual(h.sends().at(-1).body, {
    key: 'musepi2', serial: 'SN001', sid: 'ssh123', data: 'uname -a\r',
  });
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
  await drain(); assert.ok(term.queued[0].data instanceof Uint8Array); term.flush();
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

test('Ctrl+V is handed back to the browser so native paste can reach the terminal', async () => {
  const h = await setup(); const term = h.select('A');
  const key = e => term.key(Object.assign({ type: 'keydown' }, e));
  // 交给 xterm 时它会编成 0x16 并 preventDefault,粘贴被一起吃掉 —— 必须返回 false
  assert.equal(key({ ctrlKey: true, code: 'KeyV', key: 'v' }), false);
  assert.equal(key({ ctrlKey: true, shiftKey: true, code: 'KeyV', key: 'V' }), false);
  // Cmd+V 交给 xterm:xterm 对带 meta 的快捷键跑完 evaluateKeyboardEvent 后 key 为
  // undefined、cancel 为 false,既不发字节也不 preventDefault,浏览器默认粘贴照常
  assert.equal(key({ metaKey: true, code: 'KeyV', key: 'v' }), true);
  // 其余控制键照旧发给板子
  assert.equal(key({ ctrlKey: true, code: 'KeyC', key: 'c' }), true);
  assert.equal(key({ ctrlKey: true, code: 'KeyD', key: 'd' }), true);
  // 原生 paste 挂在 document 上:焦点不在终端里时 paste 的目标是 body,
  // 挂在 d-term 上的监听器收不到,表现就是"按了完全没反应"
  h.document.activeElement = null;
  h.document.emit('paste', { clipboardData: { getData: () => 'ls\n' },
    preventDefault() {}, stopPropagation() {} });
  await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['ls\n']);
  assert.ok(h.get('d-status').textContent.includes('已粘贴'));
});

test('paste is left to the browser while the command box has focus', async () => {
  const h = await setup(); h.select('A');
  const box = h.get('d-in'); box.tagName = 'INPUT';
  h.document.activeElement = box;
  let prevented = false;
  h.document.emit('paste', { clipboardData: { getData: () => 'typed by hand' },
    preventDefault() { prevented = true; }, stopPropagation() {} });
  await drain();
  assert.equal(h.sends().length, 0);
  assert.equal(prevented, false, '不能抢走发送命令框的粘贴');
});

test('an empty clipboard says so instead of looking dead', async () => {
  const h = await setup(); h.select('A');
  h.document.activeElement = null;
  h.document.emit('paste', { clipboardData: { getData: () => '' },
    preventDefault() {}, stopPropagation() {} });
  await drain();
  assert.equal(h.sends().length, 0);
  assert.ok(h.get('d-status').textContent.includes('剪贴板'));
});

test('selecting and replaying the third board never writes cursor position replies', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [{ key: 'musepi2', label: 'musepi2',
    serial: '0123456789ABCDEF', alive: true, dev: '/fake/ttyUSB2' }] });
  await drain();
  const terminal = h.select('musepi2');
  h.take('/api/session').resolve(readResponse('musepi2', 'root@bianbu:~# \x1b[6n'.repeat(20)));
  await drain(); terminal.flush(); await drain();
  assert.equal(h.sends().length, 0, 'opening a console must not type CPR responses');
  // Live serial output is shared as well; a browser must never become a second responder.
  terminal.write('\x1b[6n'); terminal.flush(); await drain();
  assert.equal(h.sends().length, 0);
  terminal.data('uname -a\r'); await drain();
  assert.equal(h.sends()[0].body.data, 'uname -a\r', 'explicit keyboard input still works');
});

test('log content is viewed inline and late log responses cannot replace a new device', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [{ name: 'serial-test.log', size: 20 }] });
  await drain();
  const button = h.get('d-logs').children.at(-1);
  assert.equal(button.tagName, 'button'); button.click();
  const log = h.take('/api/log');
  h.select('B');
  log.resolve({ data: '<script>not HTML</script>', start_off: 0, off: 20, size: 20 });
  await drain();
  assert.equal(h.get('log-viewer').hidden, true);
  assert.notEqual(h.get('log-content').textContent, '<script>not HTML</script>');
});

test('agent SSH activity opens read-only without input or resize writes', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/activity').resolve({ next_offset: 1, sessions: [{ sid: 'sshabc', actor: 'claude-code',
    status: 'active', title: 'uname -a', started: 1, exit_code: null }] });
  await drain();
  h.get('d-activity').children[0].children.at(-1).click();
  const terminal = h.terminals.at(-1);
  terminal.data('must not be sent');
  h.take('/api/session', r => r.url.includes('sid=sshabc')).resolve(readResponse('A', '\x1b[6n', 4, 'sshabc'));
  await drain(); terminal.flush(); await drain();
  assert.equal(h.sends().length, 0);
  assert.equal(h.requests.filter(r => r.url === '/api/session-resize').length, 0);
  assert.equal(h.get('b-takeover').hidden, false);
  h.get('b-takeover').click();
  const takeover = h.take('/api/session-takeover');
  assert.deepEqual(takeover.body, { key: 'A', sid: 'sshabc', actor: 'web' });
  takeover.resolve({ ok: true, sid: 'sshabc' }); await drain();
  assert.equal(h.get('b-takeover').hidden, true);
  terminal.data('human intervention\r'); await drain();
  assert.equal(h.sends().at(-1).body.data, 'human intervention\r');
});

(async () => {
  for (const [name, fn] of tests) {
    await fn(); console.log('ok - ' + name);
  }
  console.log(tests.length + ' frontend isolation tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
