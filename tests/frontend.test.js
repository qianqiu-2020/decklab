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
    this.attributes = {};
    const classes = new Set();
    this.classList = {
      contains: name => classes.has(name),
      toggle(name, force) {
        const present = force === undefined ? !classes.has(name) : !!force;
        if (present) classes.add(name); else classes.delete(name);
        return present;
      },
      remove: name => classes.delete(name),
    };
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text; }
  set innerHTML(value) { this.textContent = value; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  removeEventListener(name, fn) {
    this.events[name] = (this.events[name] || []).filter(listener => listener !== fn);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  emit(name, event = {}) {
    for (const fn of this.events[name] || []) fn(event);
  }
  click() { if (!this.disabled) this.emit('click', { stopPropagation() {} }); }
  focus() {}
  scrollIntoView(options) { this.scrolled = options || true; }
  select() {}
  remove() { this.removed = true; }
}

function harness() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) {
      const element = new Element();
      if (id === 'd-slow-paste') element.checked = false;
      if (id === 'd-records') element.hidden = true;
      elements.set(id, element);
    }
    return elements.get(id);
  };
  const requests = [], terminals = [], timers = new Map(), intervals = [];
  let nextTimer = 0;
  class Terminal {
    constructor(options = {}) {
      this.options = options;
      this.cols = 80; this.rows = 24; this.queued = []; this.parsed = '';
      this.decoder = new TextDecoder(); terminals.push(this);
      const owner = this;
      this.buffer = { active: {
        baseY: 0, cursorY: 0, viewportY: 0,
        get length() { return owner.parsed.split('\n').length; },
        getLine(row) {
          const line = owner.parsed.split('\n')[row];
          return line === undefined ? undefined : { translateToString: () => line.replace(/\s+$/, '') };
        },
      } };
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
    select(col, row, length) { this.selected = { col, row, length }; }
    scrollToLine(row) { this.scrolledToLine = row; }
    focus() {}
    refresh() { this.refreshes = (this.refreshes || 0) + 1; }
    resize(cols, rows) { this.cols = cols; this.rows = rows; }
    registerMarker(offset) {
      const marker = { line: offset, dispose() { this.disposed = true; } };
      (this.markers ||= []).push(marker); return marker;
    }
    registerDecoration(options) {
      const decoration = { options, dispose() { this.disposed = true; } };
      (this.decorations ||= []).push(decoration); return decoration;
    }
    reset() { this.wasReset = true; this.parsed = ''; }
    clear() { this.wasCleared = true; }
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
    document, Terminal, FitAddon: { FitAddon: class {
      fit() { this.fitted = true; }
      proposeDimensions() { return { cols: 140, rows: 42 }; }
    } },
    Uint8Array, AbortController, console,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { intervals.push({ fn, delay }); return intervals.length; },
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
  return { get, document, requests, terminals, timers, intervals, take, sends, select, timer, fire };
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
  assert.deepEqual(h.sends().map(r => r.body.data), ['1', '23']);
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
  assert.ok(rows.some(row => row.startsWith('身份状态=待确认（直接读取 EEPROM')));
  const cardText = h.get('grid').children[0].children.map(x => x.textContent).join(' ');
  assert.ok(cardText.includes('尚未正式确认'));
});

test('switching pending and confirmed devices never reuses identity details', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [
    { key: 'known', label: 'known', serial: 'SN-KNOWN', connection_id: 'known-1', alive: true,
      info: { serial: 'SN-KNOWN', hostname: 'known', serial_source: 'eeprom-onie',
        identity_status: 'confirmed', ts: 11 } },
    { key: 'pending', label: 'pending', serial: '', state: 'pending', connection_id: 'pending-1', alive: true,
      info: {} },
  ] });
  await drain();
  const pendingCard = h.get('grid').children[1];
  assert.ok(pendingCard.children[0].children[0].textContent.includes('待识别'));
  assert.ok(!pendingCard.children.some(x => x.textContent.includes('EEPROM 尚未写入')),
    'unprobed USB reconnect is not proof of missing EEPROM');
  h.select('known');
  assert.ok(h.get('d-info').children.some(row => row.children[0].textContent === '身份来源'));
  h.select('pending');
  assert.equal(h.get('d-info').hidden, true);
  h.select('known');
  assert.equal(h.get('d-info').hidden, false);
  assert.ok(h.get('d-info').children.some(row => row.children[1].textContent.includes('ONIE')));
});

test('pending view follows its promoted board after collector restart', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [{
    key: 'unprovisioned-path', label: 'unprovisioned-path', serial: '', state: 'pending',
    connection_id: 'old-connection', by_path: 'usb-port-1', alive: true, dev: '/mock/ttyUSB2',
  }] });
  await drain(); const old = h.select('unprovisioned-path');
  h.intervals[0].fn();
  h.take('/api/devices').resolve({ devices: [{
    key: 'milkv', label: 'milkv', serial: 'MJM10004000001', state: 'online',
    connection_id: 'new-connection', by_path: 'usb-port-1', alive: true, dev: '/mock/ttyUSB2',
    info: { serial: 'MJM10004000001', serial_source: 'eeprom-onie', identity_status: 'confirmed' },
  }] });
  await drain();
  assert.equal(old.disposed, true);
  assert.equal(h.get('d-name').textContent, 'milkv');
  assert.equal(h.get('d-key').textContent, 'SN MJM10004000001');
  assert.ok(h.requests.filter(r => r.url.startsWith('/api/session?')).at(-1).url.includes('key=milkv'));
});

test('preparing a board swap moves the live UART to an unprovisioned view', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [{ key: 'milkv', label: 'milkv', serial: 'SN001',
    state: 'online', connection_id: 'connection-1', by_path: 'usb-1', alive: true,
    dev: '/mock/ttyUSB1', hostname: 'riscv-118',
    info: { serial: 'SN001', hostname: 'riscv-118', serial_source: 'device-tree',
      identity_status: 'provisional', ts: 12 } }] });
  await drain(); h.select('milkv');
  assert.equal(h.get('b-switch').hidden, false);
  h.get('b-switch').click();
  const request = h.take('/api/switch-mode');
  assert.deepEqual(request.body, { key: 'milkv', serial: 'SN001', actor: 'web' });
  request.resolve({ ok: true, device_key: 'unprovisioned-path', serial: '', old_serial: 'SN001' });
  await drain();
  assert.equal(h.get('d-key').textContent, 'unprovisioned-path');
  assert.equal(h.get('d-info').hidden, true);
  assert.equal(h.get('d-name').textContent, 'unprovisioned-path');
  assert.ok(h.get('d-status').textContent.includes('换板识别模式'));
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
    device_key: 'musepi2', serial: 'SN001', auto_configured: true });
  await drain();
  assert.ok(h.get('d-status').textContent.includes('已通过串口配置 root SSH'));
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
  const [id, oldTimer] = h.timer(10);
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
  h.fire(h.timer(10)[0]);
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

test('opt-in firmware paste sends one serial character per acknowledged write', async () => {
  const h = await setup(); h.select('A');
  h.get('d-slow-paste').checked = true;
  h.document.activeElement = null;
  h.document.emit('paste', { clipboardData: { getData: () => 'abc' },
    preventDefault() {}, stopPropagation() {} });
  await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['a']);
  h.sends()[0].resolve({ ok: true }); await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['a']);
  assert.ok(h.timer(50), 'next character waits after the write response');
  h.fire(h.timer(50)[0]); await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['a', 'b']);
  h.sends()[1].resolve({ ok: true }); await drain();
  h.fire(h.timer(50)[0]); await drain();
  assert.deepEqual(h.sends().map(r => r.body.data), ['a', 'b', 'c']);
});

test('leaving a serial view cancels queued firmware paste', async () => {
  const h = await setup(); h.select('A');
  h.get('d-slow-paste').checked = true;
  h.document.activeElement = null;
  h.document.emit('paste', { clipboardData: { getData: () => 'abc' },
    preventDefault() {}, stopPropagation() {} });
  await drain();
  h.select('B');
  assert.equal(h.get('d-slow-paste').checked, false);
  h.sends()[0].resolve({ ok: true }); await drain();
  assert.equal(h.sends().length, 1);
  assert.equal(h.timer(50), undefined);
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

test('archived log terminal is isolated from late responses after switching devices', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [{ name: 'serial-test.log', size: 20 }] });
  await drain();
  const button = h.get('d-logs').children.find(row => row.className === 'log-row').children[1];
  assert.equal(button.tagName, 'button'); button.click();
  const archived = h.terminals.at(-1);
  assert.ok(h.get('d-term').scrolled);
  assert.equal(h.get('b-send').disabled, true);
  const log = h.take('/api/log');
  h.select('B');
  log.resolve({ data: '<script>not HTML</script>', start_off: 0, off: 20, size: 20 });
  await drain(); archived.flush();
  assert.equal(archived.disposed, true);
  assert.equal(archived.parsed, '');
});

test('multiple archived logs open as cached read-only terminal tabs', async () => {
  const h = await setup(); h.select('A');
  const files = [
    { name: 'serial-20260920.log', kind: 'serial', date: '2026-09-20', size: 20 },
    { name: 'serial-20260919.log', kind: 'serial', date: '2026-09-19', size: 10 },
  ];
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files });
  await drain();
  h.get('d-logs').children.find(row => row.className === 'log-row').children[1].click();
  const newest = h.terminals.at(-1);
  assert.equal(newest.options.convertEol, true);
  h.take('/api/log').resolve({ data_b64: Buffer.from('new log').toString('base64'),
    data: 'new log', start_off: 0, off: 7, size: 7, more: false });
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files });
  await drain();
  newest.flush();
  assert.equal(newest.parsed, 'new log');
  const dateSelect = h.get('archive-picker').children[0];
  dateSelect.value = '2026-09-19'; dateSelect.emit('change');
  h.get('d-logs').children.find(row => row.className === 'log-row').children[1].click();
  const oldest = h.terminals.at(-1);
  h.take('/api/log').resolve({ data_b64: Buffer.from('old log').toString('base64'),
    data: 'old log', start_off: 0, off: 7, size: 7, more: false });
  await drain();
  oldest.flush(); oldest.data('must not be sent'); await drain();
  assert.equal(oldest.parsed, 'old log');
  assert.equal(h.sends().length, 0);
  assert.equal(h.requests.filter(r => r.url === '/api/session-resize').length, 0);
  const archiveButtons = h.get('d-tabs').children.filter(child => child.textContent.includes('串口日志'));
  assert.equal(archiveButtons.length, 2);
  archiveButtons[0].click();
  const cached = h.terminals.at(-1); cached.flush();
  assert.equal(cached.parsed, 'new log');
  assert.equal(h.requests.filter(r => r.url.startsWith('/api/log?')).length, 2,
    'switching back must use the cached archive snapshot');
});

test('daily logs are downloadable and detected boot epochs open in terminal tabs', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [
    { name: 'serial-20260921.log', kind: 'serial', date: '2026-09-21', size: 100 },
  ], boots: [{ id: 'boot-abc', number: 3, started: '2026-09-21 08:00:00',
    marker: 'U-Boot SPL', size: 70, files: 1, dates: ['2026-09-21'], cross_day: false }] });
  await drain();
  const rows = h.get('d-logs').children;
  const dailyRow = rows.find(row => row.className === 'log-row');
  assert.ok(dailyRow.children[3].href.includes('download=1'));
  assert.equal(dailyRow.children[3].download, 'serial-20260921.log');
  const bootRow = rows.find(row => row.className.includes('boot-row'));
  bootRow.children[1].click();
  assert.equal(h.get('archive-mode-boot').attributes['aria-pressed'], 'true');
  const bootCard = h.get('d-logs').children.find(row => row.className === 'boot-detail');
  bootCard.children[2].children[0].click();
  assert.equal(h.get('archive-mode-date').attributes['aria-pressed'], 'true');
  h.get('d-logs').children.find(row => row.className.includes('boot-row')).children[2].click();
  const request = h.take('/api/boot-log');
  assert.ok(request.url.includes('id=boot-abc'));
  request.resolve({ data: 'one boot', start_off: 0, off: 8, size: 8, more: false });
  await drain(); h.terminals.at(-1).flush();
  assert.equal(h.terminals.at(-1).parsed, 'one boot');
});

test('record tabs avoid scrolling through unrelated history', async () => {
  const h = await setup(); h.select('A');
  h.get('b-records').click();
  assert.equal(h.get('record-panel-logs').hidden, false);
  assert.equal(h.get('record-panel-sessions').hidden, true);
  h.get('record-tab-sessions').click();
  assert.equal(h.get('record-panel-logs').hidden, true);
  assert.equal(h.get('record-panel-sessions').hidden, false);
  h.get('record-tab-events').click();
  assert.equal(h.get('record-panel-events').hidden, false);
  h.get('record-tab-commands').click();
  assert.equal(h.get('record-panel-commands').hidden, false);
});

test('online boards precede pending ports and historical boards', async () => {
  const h = harness();
  h.take('/api/devices').resolve({ devices: [
    { key: 'old', label: 'old', serial: 'SN-OLD', state: 'offline', alive: false },
    { key: 'pending', label: 'pending', state: 'pending', alive: true },
    { key: 'live-a', label: 'live-a', serial: 'SN-A', state: 'online', alive: true },
    { key: 'live-b', label: 'live-b', serial: 'SN-B', state: 'online', alive: true },
  ] });
  await drain();
  assert.deepEqual(h.get('grid').children.map(card => card.dataset.key),
    ['live-a', 'live-b', 'pending', 'old']);
});

test('input navigator jumps to echoed commands without sending input', async () => {
  const h = await setup(); const terminal = h.select('A');
  terminal.parsed = 'root@bianbu:~# uname -a\nLinux riscv64\n'
    + 'ubuntu@ubuntu:~$ sudo dmesg\noutput\nPassword: hidden\n';
  h.get('b-command-nav').click();
  assert.equal(h.get('record-panel-inputs').hidden, false);
  assert.equal(h.get('command-nav-count').textContent, '2 条');
  const entries = h.get('d-command-nav').children;
  assert.equal(entries[0].children[1].children[0].textContent, 'uname -a');
  assert.equal(entries[1].children[1].children[0].textContent, 'sudo dmesg');
  entries[1].click();
  assert.equal(terminal.scrolledToLine, 2);
  h.get('command-nav-prev').click();
  assert.equal(terminal.scrolledToLine, 0);
  assert.equal(h.sends().length, 0);
});

test('echoed shell commands share one default color without changing output', async () => {
  const h = await setup(); const terminal = h.select('A');
  h.take('/api/session').resolve(readResponse('A',
    'root@board:~# uname -a\r\nLinux\r\nroot@board:~# dmesg\r\n'));
  await drain(); terminal.flush();
  const scheduled = h.timer(350); assert.ok(scheduled);
  h.fire(scheduled[0]);
  const foreground = terminal.decorations.filter(item => item.options.foregroundColor);
  const background = terminal.decorations.filter(item => item.options.backgroundColor);
  assert.equal(foreground.length, 2);
  assert.equal(background.length, 2);
  assert.equal(foreground[0].options.foregroundColor, foreground[1].options.foregroundColor);
  assert.ok(terminal.parsed.includes('root@board:~# dmesg'), 'terminal bytes are unchanged');
  assert.equal(h.sends().length, 0);
});

test('dates and boot numbers navigate in both directions across midnight', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [
    { name: 'serial-20260921.log', kind: 'serial', date: '2026-09-21', size: 100 },
    { name: 'serial-20260920.log', kind: 'serial', date: '2026-09-20', size: 100 },
  ], boots: [
    { id: 'boot-3', number: 3, started: '2026-09-21 09:00:00', marker: 'U-Boot',
      dates: ['2026-09-21'], size: 30 },
    { id: 'boot-2', number: 2, started: '2026-09-20 23:59:00', marker: 'Boot ROM',
      dates: ['2026-09-20', '2026-09-21'], size: 80, cross_day: true },
    { id: 'boot-1', number: 1, started: '2026-09-20 10:00:00', marker: 'U-Boot',
      dates: ['2026-09-20'], size: 40 },
  ] });
  await drain();
  assert.ok(h.get('archive-picker').children[1].textContent.includes('当天启动 1 次'));
  assert.ok(h.get('archive-picker').children[1].textContent.includes('跨日延续 1 次'));
  const linked = h.get('d-logs').children.filter(row => row.className.includes('boot-row'));
  assert.equal(linked.length, 2);
  assert.ok(linked[1].children[0].textContent.includes('跨日延续'));
  linked[1].children[1].click();
  assert.equal(h.get('archive-mode-boot').attributes['aria-pressed'], 'true');
  const dates = h.get('d-logs').children.find(row => row.className === 'boot-detail').children[2].children;
  assert.deepEqual(dates.map(button => button.textContent), ['2026-09-20', '2026-09-21']);
  dates[0].click();
  assert.equal(h.get('archive-mode-date').attributes['aria-pressed'], 'true');
  assert.equal(h.get('archive-picker').children[0].value, '2026-09-20');
  assert.ok(h.get('archive-picker').children[1].textContent.includes('当天启动 2 次'));
});

test('common commands fill the input without executing on click', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/commands').resolve({ key: 'A', commands: [
    { command: 'journalctl -b', use_count: 4 },
  ] });
  await drain();
  h.get('d-commands').children[0].click();
  assert.equal(h.get('d-in').value, 'journalctl -b');
  assert.equal(h.sends().length, 0);
});

test('SSH sessions and key events have independent bounded pages', async () => {
  const h = await setup(); h.select('A');
  const sessions = Array.from({ length: 10 }, (_, i) => ({ sid: 'ssh' + i, actor: 'agent',
    status: 'completed', title: 'command ' + i, started: i + 1, exit_code: 0 }));
  const events = Array.from({ length: 10 }, (_, i) => ({ id: i, actor: 'service',
    action: 'identified', detail: 'usb-' + i, ts: i + 1 }));
  h.take('/api/activity').resolve({ sessions, session_total: 25, events, event_total: 12 });
  await drain();
  assert.equal(h.get('d-activity').children.length, 10);
  assert.equal(h.get('d-events').children.length, 10);
  assert.ok(h.get('activity-page').textContent.includes('25 条'));
  assert.ok(h.get('event-page').textContent.includes('12 条'));

  h.get('activity-next').click();
  const sessionPage = h.take('/api/activity');
  assert.ok(sessionPage.url.includes('session_offset=10'));
  assert.ok(sessionPage.url.includes('event_offset=0'));
  sessionPage.resolve({ sessions: sessions.slice(0, 3), session_total: 13,
    events, event_total: 12 });
  await drain();

  h.get('event-next').click();
  const eventPage = h.take('/api/activity');
  assert.ok(eventPage.url.includes('session_offset=10'));
  assert.ok(eventPage.url.includes('event_offset=10'));
  eventPage.resolve({ sessions: sessions.slice(0, 3), session_total: 13,
    events: events.slice(0, 2), event_total: 12 });
  await drain();
  assert.equal(h.get('d-events').children.length, 2);
});

test('completed SSH transcript opens in a read-only terminal tab', async () => {
  const h = await setup(); h.select('A');
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [] });
  h.take('/api/activity').resolve({ sessions: [{ sid: 'ssh-done', actor: 'claude-code',
    status: 'completed', title: 'uname -a', started: 1, exit_code: 0 }], events: [] });
  await drain();
  h.get('d-activity').children[0].children.at(-1).click();
  h.take('/api/logs').resolve({ key: 'A', serial: 'SN-A', files: [
    { name: 'ssh-ssh-done-20260920.log', kind: 'ssh', size: 10 },
  ] });
  await drain();
  const terminal = h.terminals.at(-1);
  h.take('/api/log').resolve({ data_b64: Buffer.from('ssh output').toString('base64'),
    data: 'ssh output', start_off: 0, off: 10, size: 10, more: false });
  await drain(); terminal.flush(); terminal.data('must not be sent'); await drain();
  assert.equal(terminal.parsed, 'ssh output');
  assert.equal(h.sends().length, 0);
  assert.ok(h.get('d-tabs').children.some(child => child.textContent.includes('SSH 记录 · claude-code')));
  assert.equal(h.get('b-takeover').hidden, true);
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

test('terminal search selects matches and wraps in both directions', async () => {
  const h = await setup(); const terminal = h.select('A');
  h.take('/api/session').resolve(readResponse('A', 'alpha root\nbeta\nroot gamma'));
  await drain(); terminal.flush();
  h.get('terminal-search').value = 'root';
  h.get('terminal-search-next').click();
  assert.deepEqual(terminal.selected, { col: 6, row: 0, length: 4 });
  assert.equal(h.get('terminal-search-status').textContent, '1 / 2');
  h.get('terminal-search-next').click();
  assert.deepEqual(terminal.selected, { col: 0, row: 2, length: 4 });
  h.get('terminal-search-next').click();
  assert.deepEqual(terminal.selected, { col: 6, row: 0, length: 4 });
  h.get('terminal-search-prev').click();
  assert.deepEqual(terminal.selected, { col: 0, row: 2, length: 4 });
});

test('live serial stays at firmware-compatible 80 columns and redraw replays from zero', async () => {
  const h = await setup(); const terminal = h.select('A');
  assert.equal(terminal.cols, 80);
  assert.equal(terminal.rows, 42, 'height should still follow the available terminal area');
  const firstRead = h.requests.filter(r => r.url.startsWith('/api/session?')).at(-1);
  h.get('b-redraw').click();
  assert.equal(terminal.wasReset, true);
  assert.equal(terminal.wasCleared, true);
  const reads = h.requests.filter(r => r.url.startsWith('/api/session?'));
  assert.equal(reads.length, 2);
  assert.ok(reads.at(-1).url.includes('off=0'));
  reads.at(-1).resolve(readResponse('A', '\x1b[2J\x1b[HGRUB menu'));
  await drain(); terminal.flush();
  assert.ok(terminal.refreshes > 0, 'full-screen control output should force a complete repaint');
  // A late response from the pre-redraw read must remain harmless.
  firstRead.resolve(readResponse('A', 'stale', 5)); await drain(); terminal.flush();
  assert.ok(!terminal.parsed.includes('stale'));
});

test('live serial learns an EDK2 80x50 screen before parsing its cursor positions', async () => {
  const h = await setup(); const terminal = h.select('A');
  assert.equal(terminal.options.fontSize, 14.5);
  assert.equal(terminal.rows, 42);
  h.take('/api/session').resolve(readResponse('A',
    '\x1b[47m\x1b[05;58HDevice Path\x1b[50;27H<Enter>=Select'));
  await drain();
  assert.equal(terminal.cols, 80);
  assert.equal(terminal.rows, 50, 'EDK2 row 50 must not be clamped into the visible browser height');
  h.get('b-expand').click();
  const expanded = h.timer(0); assert.ok(expanded); h.fire(expanded[0]);
  assert.ok(terminal.options.fontSize <= 10, 'maximized firmware screen should fit more rows');
  h.get('b-expand').click();
  const restored = h.timer(0); assert.ok(restored); h.fire(restored[0]);
  assert.ok(terminal.options.fontSize <= 14.5);
  terminal.flush(); await drain();
  assert.ok(terminal.refreshes > 0);
});

test('full clear lets GRUB leave the previous EDK2 50-row screen', async () => {
  const h = await setup(); const terminal = h.select('A');
  const first = h.take('/api/session');
  first.resolve(readResponse('A', '\x1b[2J\x1b[50;27HEDK2', 18));
  await drain();
  assert.equal(terminal.rows, 50);
  terminal.flush(); await drain();
  const poll = h.timer(10); assert.ok(poll); h.fire(poll[0]);
  h.take('/api/session').resolve(readResponse('A',
    '\x1b[0m\x1b[37m\x1b[40m\x1b[2J\x1b[01;01H\x1b[?25l', 54));
  await drain();
  assert.equal(terminal.rows, 42, 'a new lower screen must discard EDK2 logical height');
  terminal.flush(); await drain();
  assert.ok(terminal.refreshes > 0);
});

test('Linux boot restores the normal serial font after an uncleared EDK2 screen', async () => {
  const h = await setup(); const terminal = h.select('A');
  h.take('/api/session').resolve(readResponse('A', '\x1b[50;27HEDK2', 18));
  await drain();
  assert.equal(terminal.rows, 50);
  const poll = h.timer(10); assert.ok(poll); h.fire(poll[0]);
  h.take('/api/session').resolve(readResponse('A', '\r\n[    0.000000] Linux ver', 46));
  await drain();
  const next = h.timer(10); assert.ok(next); h.fire(next[0]);
  h.take('/api/session').resolve(readResponse('A', 'sion 6.18.3+\r\n\x1b[50;01Hfedora login:', 90));
  await drain();
  assert.equal(terminal.rows, 42);
  assert.equal(terminal.options.fontSize, 14.5);
});

test('a new boot epoch discards stale EDK2 screen state and older boots', async () => {
  const h = await setup(); const terminal = h.select('A');
  h.take('/api/session').resolve(readResponse('A',
    'old boot\x1b[47m\x1b[50;01HOLD BAR\r\n' +
    'U-Boot SPL 2022.10 (build one)\r\nfirst boot\r\n' +
    '\x00sys: 0x10000200\r\nU-Boot SPL 2022.10 (build two)\r\nlatest boot'));
  await drain(); terminal.flush(); await drain();
  assert.equal(terminal.wasReset, true);
  assert.equal(terminal.wasCleared, true);
  assert.ok(terminal.parsed.includes('U-Boot SPL 2022.10 (build two)'));
  assert.ok(terminal.parsed.includes('latest boot'));
  assert.ok(!terminal.parsed.includes('OLD BAR'));
  assert.ok(!terminal.parsed.includes('first boot'));
});

test('boot banner split across reads still resets the live terminal once', async () => {
  const h = await setup(); const terminal = h.select('A');
  h.take('/api/session').resolve(readResponse('A', 'stale\x1b[47m\r\nU-Boot SP', 20));
  await drain(); terminal.flush(); await drain();
  const poll = h.timer(10); assert.ok(poll); h.fire(poll[0]);
  h.take('/api/session').resolve(readResponse('A', 'L 2022.10 fresh', 35));
  await drain(); terminal.flush(); await drain();
  assert.equal(terminal.wasReset, true);
  assert.ok(terminal.parsed.includes('L 2022.10 fresh'));
  assert.ok(!terminal.parsed.includes('stale'));
});

(async () => {
  for (const [name, fn] of tests) {
    await fn(); console.log('ok - ' + name);
  }
  console.log(tests.length + ' frontend isolation tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
