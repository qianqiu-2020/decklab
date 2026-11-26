'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const tick = () => new Promise(resolve => setImmediate(resolve));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const cleanup = [];
function fixture(configuration = {}) {
  const nodes = {}, calls = [], sockets = [], displays = [], keyboards = [], listeners = {}, intervals = [];
  const document = {activeElement: null, hidden: false, fullscreenElement: null,
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    querySelectorAll() { return []; },
    createElement(tag) { return new Element(tag); },
    exitFullscreen() { this.fullscreenElement = null; return Promise.resolve(); }};
  class Element {
    constructor(id) {
      this.children = []; this.id = id; this.listeners = {}; this.hidden = false; this.value = ''; this.textContent = '';
      this.attributes = {}; this.width = 640; this.height = 480; this.clientWidth = 400; this.clientHeight = 200;
      const classes = new Set(); this.classList = {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x)};
    }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return String(this.text || '') + this.children.map(x=>x.textContent).join(''); }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter(x => x !== fn); }
    setAttribute(name, value) { this.attributes[name] = value; }
    cloneNode() { return new Element(this.id); }
    replaceWith(next) { nodes[this.id] = next; }
    getBoundingClientRect() { return {left: 0, top: 0, width: 200, height: 100}; }
    focus() { document.activeElement = this; this.fire('focus'); }
    select() { this.selected = true; }
    setPointerCapture(id) { this.capture = id; }
    hasPointerCapture(id) { return this.capture === id; }
    releasePointerCapture(id) { if (this.capture === id) this.capture = null; }
    contains(node) { return this.id === 'desktop-panel' && node.id === 'desktop-image'; }
    requestFullscreen() { document.fullscreenElement = this; return Promise.resolve(); }
    fire(name, extra = {}) { for (const fn of [...(this.listeners[name] || [])]) fn(Object.assign({type:name, preventDefault() {}, stopImmediatePropagation() {}}, extra)); }
  }
  const $ = id => nodes[id] || (nodes[id] = new Element(id));
  $('desktop-scale').value = 'fit'; $('desktop-keys').value = 'ControlLeft AltLeft Delete';
  class Display {
    constructor(canvas) { this.canvas = canvas; this.rectangles = []; displays.push(this); }
    resize(w, h) { this.canvas.width = w; this.canvas.height = h; }
    imageRect(x, y, w, h, mime, bytes) { this.rectangles.push({x, y, w, h, mime, length: bytes.length}); }
    blitImage() { throw new Error('unexpected raw frame'); }
    flip() { this.flips = (this.flips || 0) + 1; }
    flush() { return this.wait || Promise.resolve(); }
    autoscale(w, h) { this.fit = [w, h]; }
  }
  class Keyboard {
    constructor(canvas) { this.canvas = canvas; this.held = new Map(); keyboards.push(this); }
    grab() { this.grabbed = true; }
    ungrab() { this.grabbed = false; for (const [code, keysym] of this.held) this.onkeyevent(keysym, code, false); this.held.clear(); }
    press(keysym, code, down) { if (down) this.held.set(code, keysym); else this.held.delete(code); this.onkeyevent(keysym, code, down); }
  }
  class Socket {
    constructor(url, protocol) { this.url = url; this.protocol = protocol; this.readyState = 0; this.sent = []; this.acked = 0; sockets.push(this); }
    open() { this.readyState = 1; this.onopen(); }
    send(value) { assert.strictEqual(this.readyState, 1); this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
    server(value) { this.onmessage({data: JSON.stringify(value)}); }
    ackAll() {
      let message;
      while ((message = this.sent.find(x => (x.type === 'input' || x.type === 'release_all') && x.seq > this.acked))) {
        this.acked = message.seq; this.server({type: 'ack', seq: message.seq, ok: true});
      }
    }
    inputs() { return this.sent.filter(x => x.type === 'input'); }
  }
  let view = {key: 'board-a', serial: 'SERIAL-A'}, reads = 0, writes = 0, copied = '';
  let configured = !configuration.setup || !!configuration.saved, connection = configuration.saved || {};
  const fetch = async (url, options) => {
    const body = options && options.body && JSON.parse(options.body); calls.push({url, body});
    if (url.startsWith('/api/desktop/sessions') && configuration.readSettings)
      await configuration.readSettings(options.signal);
    const serial = body && body.serial || new URL(url, 'http://decklab.test').searchParams.get('serial');
    let data = {ok: true};
    if (url.startsWith('/api/desktop/sessions')) data = {supported: configured, sessions: [],
      ...(configuration.setup ? {setup:{revision:'revision-1',quick_connect:!!configuration.quick,connection,hosts:['10.0.0.2'],suggested_user:'user'}} : {})};
    else if (url === '/api/desktop/discover') data = configuration.discovery ? await configuration.discovery() : {candidates:[
      {id:'rdp-service',provider:'rdp',host:'10.0.0.2',port:3389,certificate_sha256:'a'.repeat(64)},
      {id:'vnc-service',provider:'vnc',host:'10.0.0.2',port:5900}]};
    else if (url === '/api/desktop/configure') {
      configured=true; connection={provider:body.provider,host:body.host,port:body.port,username:body.username,password_saved:true,certificate_policy:body.certificate_policy};
      data={ok:true,setup:{revision:'revision-2',connection,hosts:['10.0.0.2']}};
    }
    else if (url === '/api/desktop/close' && configuration.closeFailure)
      return {ok:false,status:configuration.closeFailure,json:async()=>({error:'desktop session not found'})};
    else if (url === '/api/desktop/login-stage') data = {login_stage:configuration.liveStage || 'initial'};
    else if (url === '/api/desktop/quick-open' && configuration.failNextOpen) {
      configuration.failNextOpen = false;
      return {ok:false,status:502,json:async()=>({code:'desktop-connection-refused',error:'starting'})};
    }
    else if (url === '/api/desktop/quick-open' && configuration.quickFailure) {
      return {ok:false,status:502,json:async()=>configuration.quickFailure};
    }
    else if (url === '/api/desktop/quick-open') data = {session:{id:'session-'+serial, serial, login_stage:configuration.stage || 'initial'}};
    else if (url === '/api/desktop/open') {
      if (configuration.connectionFailure && connection.certificate_policy !== 'accept') return {ok:false,status:502,json:async()=>configuration.connectionFailure};
      data = {session: {id: 'session-' + serial, serial}};
    }
    return {ok: true, status: 200, json: async () => data};
  };
  const window = {document, fetch, URL, location: {href: 'http://decklab.test/'},
    navigator: {clipboard: {readText: async () => { reads++; return 'LOCAL'; }, writeText: async text => { writes++; copied = text; }}},
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    requestAnimationFrame: fn => setImmediate(fn), cancelAnimationFrame: clearImmediate,
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }, confirm: () => true};
  const context = {window, AbortController, setTimeout, clearTimeout, setInterval(fn, ms) { intervals.push({fn, ms}); return setInterval(fn, ms); }, clearInterval, console, Date, Math, Promise, Map, Uint8Array};
  vm.createContext(context);
  for (const file of ['static/ui-common.js', 'static/tasks.js', 'static/desktop.js']) vm.runInContext(fs.readFileSync(file, 'utf8'), context);
  const desktop = window.DeckLabDesktop.create({byId: $, fetch: window.DeckLabUI.fetch,
    getView: () => view, canQuickConnect: () => !!configuration.quick, clientId: window.DeckLabUI.clientId, copyText:text=>{copied=text;return true;}, websocketFactory: (url, protocol) => new Socket(url, protocol), libraryLoader: async () => ({Display, Keyboard}),
    stageCheckDelay: configuration.stageCheckDelay, seatRetryDelay: 10, ackTimeout: configuration.ackTimeout, settingsTimeout: configuration.settingsTimeout,
    requestAnimationFrame: window.requestAnimationFrame, cancelAnimationFrame: clearImmediate});
  cleanup.push(() => desktop.close());
  const frame = (sequence = 1, extra = {}) => Object.assign({type: 'frame', sequence, generation: 'display-1',
    frame_id: 'mode-1', width: 1000, height: 500, x: 0, y: 0, rect_width: 1000, rect_height: 500,
    mime: 'image/png', png_base64: Buffer.from('PNG').toString('base64'), reset: sequence === 1}, extra);
  async function open() {
    desktop.refresh(view); await tick(); $('b-desktop').fire('click'); await tick(); await tick();
    const socket = sockets.at(-1); assert.ok(socket, $('desktop-status').textContent); socket.open();
    socket.server({type: 'ready', session: {id: 'session-' + view.serial, serial: view.serial}, capabilities: {text:true, clipboard:{read:true,write:true,encoding:'utf-8'}}});
    socket.server(frame()); await tick(); await tick(); return socket;
  }
  return {nodes, $, calls, sockets, displays, keyboards, desktop, frame, open, window, document, intervals,
    switchBoard() { view = {key: 'board-b', serial: 'SERIAL-B'}; desktop.refresh(view); },
    clipboard: () => ({reads, writes, copied}), fireGlobal(name) { for (const fn of listeners[name] || []) fn(); }};
}
(async function () {
  let attempts = 0;
  const delayed = fixture({setup:true, quick:true, settingsTimeout:15, readSettings(signal) {
    if (++attempts === 1)
      return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  }});
  delayed.desktop.refresh({key:'board-a',serial:'SERIAL-A'});
  delayed.$('b-desktop').fire('click');
  delayed.$('b-desktop').fire('click');
  await pause(40);
  assert.equal(attempts, 1);
  assert.ok(delayed.$('desktop-overlay').textContent.includes('读取桌面设置超时'));
  delayed.$('desktop-refresh').fire('click');
  await tick(); await tick();
  assert.equal(attempts, 2);
  assert.equal(delayed.calls.filter(x => x.url === '/api/desktop/quick-open').length, 1);
  for (const switchBoard of [false, true]) {
    const greeter = fixture({setup:true, quick:true, stage:'lxqt-greeter'});
    const login = await greeter.open();
    login.server({type:'error', code:'desktop-stream-stopped', error:'screen closed'});
    if (switchBoard) greeter.switchBoard();
    await pause(35);
    assert.equal(greeter.calls.filter(x => x.url === '/api/desktop/close').length, switchBoard ? 0 : 1);
    assert.equal(greeter.calls.filter(x => x.url === '/api/desktop/quick-open').length, switchBoard ? 1 : 2);
    assert.equal(login.inputs().length, 0);
    greeter.desktop.close();
  }
  for (const end of ['disconnect', 'still-connected', 'leave']) {
    const settings = {setup:true, quick:true, stage:'initial', stageCheckDelay:10};
    const wizard = fixture(settings), old = await wizard.open();
    settings.stage = 'login';
    if (end === 'disconnect') {
      settings.failNextOpen = true;
      old.server({type:'error', code:'desktop-stream-stopped', error:'closed'});
    } else {
      settings.liveStage = 'login';
      if (end === 'leave') wizard.switchBoard();
    }
    await pause(110);
    assert.equal(wizard.calls.filter(x => x.url === '/api/desktop/close').length, end === 'leave' ? 0 : 1);
    assert.equal(wizard.calls.filter(x => x.url === '/api/desktop/quick-open').length,
      end === 'leave' ? 1 : end === 'disconnect' ? 3 : 2);
    assert.equal(old.inputs().length, 0);
    wizard.desktop.close();
  }
  const firstBoot = fixture({setup:true,quick:true});
  const wizardSocket = await firstBoot.open();
  assert.equal(firstBoot.calls.filter(x => x.url === '/api/desktop/quick-open').length, 1);
  assert.ok(firstBoot.$('desktop-login-stage').textContent.includes('创建普通用户'));
  assert.ok(!firstBoot.calls.some(x => x.url === '/api/desktop/configure'));
  assert.equal(firstBoot.$('desktop-move-wizard').hidden, false);
  wizardSocket.server({type:'observed',ok:true,frame_id:'mode-1',sequence:1});
  firstBoot.$('desktop-move-wizard').fire('click'); wizardSocket.ackAll();
  assert.deepStrictEqual(wizardSocket.inputs().map(x => [x.event.code,x.event.down]), [
    ['MetaLeft',true],['ShiftLeft',true],['ArrowRight',true],
    ['ArrowRight',false],['ShiftLeft',false],['MetaLeft',false]
  ]);
  firstBoot.switchBoard();
  assert.equal(firstBoot.$('desktop-move-wizard').hidden, true);
  assert.equal(firstBoot.$('desktop-login-guide').hidden, true);
  const unavailable = fixture({setup:true,quick:true,quickFailure:{
    code:'desktop-authentication-failed', error:'authentication failed', login_stage:'usb-unavailable'
  }});
  unavailable.desktop.refresh({key:'board-a',serial:'SERIAL-A'});
  await tick(); unavailable.$('b-desktop').fire('click'); await tick(); await tick();
  assert.match(unavailable.$('desktop-overlay').textContent,/入口凭据/);
  assert.match(unavailable.$('desktop-login-stage').textContent,/USB ADB 未就绪/);
  assert.equal(unavailable.$('desktop-login-guide').hidden,false);
  assert.equal(unavailable.$('desktop-move-wizard').hidden,true);
  assert.equal(unavailable.sockets.length,0);
  const fallback = fixture({setup:true,quick:true,stage:'usb-unavailable'});
  await fallback.open();
  assert.match(fallback.$('desktop-login-stage').textContent,/USB ADB 未就绪/);
  assert.equal(fallback.$('desktop-move-wizard').hidden,true);
  fallback.desktop.close();
  const failure=fixture({connectionFailure:{error:'证书不受信任 · ERRCONNECT_TLS_CONNECT_FAILED',code:'desktop-certificate-rejected',
    details:{stage:'certificate',native_code:'0x00020008',traceback:'File "/server/desktop.py", line 10\nTLS failed <script>'}}});
  failure.desktop.refresh({key:'board-a',serial:'SERIAL-A'});await tick();failure.$('b-desktop').fire('click');await tick();await tick();
  assert.strictEqual(failure.$('desktop-error').hidden,false);assert.strictEqual(failure.$('desktop-error').open,false);
  assert.ok(failure.$('desktop-error-details').textContent.includes('0x00020008'));
  assert.ok(failure.$('desktop-error-details').textContent.includes('<script>'));
  assert.strictEqual(failure.$('desktop-error-details').children.length,0,'details are text, never server-supplied markup');
  failure.$('desktop-error-copy').fire('click');assert.ok(failure.clipboard().copied.includes('traceback'));
  failure.switchBoard();assert.strictEqual(failure.$('desktop-error').hidden,true);
  const f = fixture(), ws = await f.open(), $ = f.$;
  assert.strictEqual(ws.protocol, 'decklab-desktop-v1');
  assert.ok(ws.url.startsWith('ws://decklab.test/api/desktop/stream?'));
  assert.ok(!/password|token=/.test(ws.url));
  assert.ok($('detail').classList.contains('desktop-active'));
  assert.strictEqual($('desktop-panel').hidden, false);
  assert.strictEqual(f.calls.find(x => x.url === '/api/desktop/open').body.client_id, f.window.DeckLabUI.clientId);
  assert.strictEqual(f.calls.filter(x => x.url.startsWith('/api/desktop/frame')).length, 0, 'continuous viewing must never poll screenshots');
  assert.strictEqual(ws.sent[0].type, 'observed');
  ws.server({type: 'observed', ok: true, frame_id: 'mode-1', sequence: 1});
  f.intervals.find(x => x.ms === 5000).fn();
  ws.server({type: 'heartbeat', ok: true});
  ws.server({type: 'bound', ok: true});
  assert.strictEqual(ws.readyState, 1, 'real observed/heartbeat/bind receipts keep the stream open');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ws.sent.at(-1))), {type: 'observed', frame_id: 'mode-1', sequence: 1}, 'quiet desktops renew only the flushed observation');
  ws.server(f.frame(2, {x: 4, y: 5, rect_width: 20, rect_height: 10})); await tick(); await tick();
  assert.strictEqual(f.displays[0].rectangles.length, 2, 'native deltas render without any capture request');
  const canvas = $('desktop-image');
  canvas.fire('pointerdown', {button: 0, pointerId: 1, clientX: 100, clientY: 50});
  for (let i = 0; i < 10; i++) canvas.fire('pointermove', {clientX: 110 + i, clientY: 50});
  await tick();
  f.keyboards.at(-1).press(0xffe3, 'ControlLeft', true);
  canvas.fire('pointerup', {button: 0, pointerId: 1, clientX: 120, clientY: 50});
  assert.strictEqual(ws.inputs().length, 1, 'wait for the single in-flight input acknowledgement');
  ws.ackAll();
  const inputs = ws.inputs();
  assert.deepStrictEqual([inputs[0].event.x, inputs[0].event.y, inputs[0].event.buttons], [500, 250, 1]);
  assert.deepStrictEqual(inputs.slice(1).map(x => x.event.type), ['pointer', 'key', 'pointer']);
  assert.strictEqual(inputs[1].event.x, 595, 'pointer movement coalesces without skipping key/button order');
  assert.strictEqual(inputs.at(-1).event.buttons, 0);
  assert.deepStrictEqual(inputs.map(x => x.seq), [1, 2, 3, 4]);
  assert.strictEqual(inputs[0].frame_id, 'mode-1');
  f.keyboards.at(-1).press(65, 'KeyA', true); // queued behind one in-flight key
  f.keyboards.at(-1).press(66, 'KeyB', true);
  canvas.fire('blur'); ws.ackAll();
  assert.strictEqual(ws.inputs().filter(x => x.event.code === 'KeyB').length, 0, 'focus loss drops unsent keys instead of replaying');
  assert.strictEqual(ws.sent.at(-1).type, 'release_all');
  canvas.focus(); $('desktop-key').fire('click');
  ws.server(f.frame(3, {reset: true, frame_id: 'mode-after-blur'})); await tick(); await tick(); ws.ackAll();
  const combo = ws.inputs().slice(-6).map(x => [x.event.code, x.event.down]);
  assert.deepStrictEqual(combo, [['ControlLeft', true], ['AltLeft', true], ['Delete', true], ['Delete', false], ['AltLeft', false], ['ControlLeft', false]]);
  canvas.fire('wheel', {clientX: 20, clientY: 10, deltaY: 120, deltaMode: 0}); ws.ackAll();
  assert.strictEqual(ws.inputs().at(-1).event.steps, 3);
  $('desktop-scale').value = '1.5'; $('desktop-scale').fire('change'); assert.strictEqual(f.displays[0].scale, 1.5);
  const locks = [];
  f.window.navigator.keyboard = {lock: async codes => { locks.push(Array.from(codes)); }, unlock: () => locks.push('released')};
  $('desktop-fullscreen').fire('click'); await tick(); assert.strictEqual(f.document.fullscreenElement, $('desktop-panel'));
  f.fireGlobal('fullscreenchange'); assert.strictEqual($('desktop-fullscreen').textContent, '退出全屏');
  assert.deepEqual(locks, [['MetaLeft', 'MetaRight', 'Tab']]);
  const beforeF8 = ws.inputs().length;
  $('desktop-f8-super').checked = true;
  canvas.fire('keydown',{code:'F8'});
  canvas.fire('keydown',{code:'F8',repeat:true});
  f.keyboards.at(-1).press(0x65,'KeyE',true);
  f.keyboards.at(-1).press(0x65,'KeyE',false);
  canvas.fire('keyup',{code:'F8'});
  ws.ackAll();
  assert.deepEqual(ws.inputs().slice(beforeF8).map(m=>[m.event.code,m.event.down]), [
    ['MetaLeft',true],['KeyE',true],['KeyE',false],['MetaLeft',false]]);
  $('desktop-f8-super').checked = false;
  const metaCount = () => ws.inputs().filter(m => m.event.code === 'MetaRight').length;
  const originalMeta = metaCount();
  const metaPress = () => { for (const type of ['keydown','keyup']) canvas.fire(type,{code:'MetaRight'}); ws.ackAll(); };
  metaPress();
  assert.equal(metaCount(), originalMeta + 2, 'the locked physical key is delivered once');
  f.document.fullscreenElement = null; f.fireGlobal('fullscreenchange');
  metaPress();
  assert.equal(metaCount(), originalMeta + 2, 'a local system key is not duplicated remotely');
  assert.strictEqual(locks.at(-1), 'released');
  assert.deepStrictEqual(f.clipboard(), {reads: 0, writes: 0, copied: ''});
  $('desktop-clipboard-toggle').fire('click'); ws.server({type:'clipboard',text:'REMOTE'}); ws.ackAll();
  assert.strictEqual($('desktop-text').value, 'REMOTE');
  assert.strictEqual(f.clipboard().writes, 0, 'the fallback view never writes the host clipboard');
  $('desktop-clipboard-toggle').fire('click');
  const paste = text => canvas.fire('paste',{clipboardData:{getData:type=>type==='text/plain'?text:''}});
  const beforePaste = ws.inputs().length;
  paste('中文 😀');
  assert.strictEqual(ws.inputs().length,beforePaste+1);
  assert.strictEqual(ws.inputs().at(-1).event.type,'clipboard');
  ws.ackAll();
  assert.deepStrictEqual(ws.inputs().slice(beforePaste).map(x => x.event.code || x.event.type),
    ['clipboard','ControlLeft','KeyV','KeyV','ControlLeft']);
  assert.strictEqual(ws.inputs()[beforePaste].event.text,'中文 😀');
  const beforeCopy = ws.inputs().length;
  canvas.fire('keydown',{code:'KeyC',ctrlKey:true});
  canvas.fire('keyup',{code:'KeyC',ctrlKey:true});
  ws.ackAll(); await pause(170);
  assert.deepStrictEqual(ws.inputs().slice(beforeCopy).map(x => x.event.code || x.event.type),
    ['ControlLeft','KeyC','KeyC','ControlLeft','clipboard-read']);
  ws.server({type:'clipboard',text:'COPIED'}); ws.ackAll(); await tick();
  assert.deepStrictEqual(f.clipboard(),{reads:0,writes:1,copied:'COPIED'});
  assert.strictEqual($('desktop-clipboard').hidden,true);
  $('desktop-clipboard-toggle').fire('click'); ws.server({type:'clipboard',available:false}); ws.ackAll();
  assert.strictEqual($('desktop-text').value,'');
  assert.ok($('desktop-status').textContent.includes('暂无可读'));
  ws.server({type:'ready',session:{id:'session-SERIAL-A',serial:'SERIAL-A'},capabilities:{text:true,clipboard:{read:true,write:true,encoding:'latin-1'}}});
  paste('中文'); ws.ackAll();
  assert.strictEqual(ws.inputs().at(-1).event.type,'text','Unicode VNC input falls back to typing');
  assert.strictEqual(ws.inputs().at(-1).event.text,'中文');
  $('desktop-paste-text').value = '手动粘贴';
  $('desktop-paste-submit').fire('click'); ws.ackAll();
  assert.strictEqual(ws.inputs().at(-1).event.text,'手动粘贴');
  $('desktop-paste-text').value = '';
  $('desktop-paste-submit').fire('click'); await tick(); ws.ackAll();
  assert(ws.inputs().some(m=>m.event.type==='clipboard' && m.event.text==='LOCAL'));
  const beforeRepeat = ws.inputs().length;
  canvas.fire('keydown',{ctrlKey:true,code:'KeyV',repeat:true}); ws.ackAll();
  assert.equal(ws.inputs().length,beforeRepeat);
  const beforeInvalid = ws.inputs().length;
  for (const invalid of ['a\0b','\ud800','\udc00','中'.repeat(1025),'a'.repeat(32769)]) {
    paste(invalid); ws.ackAll();
    assert.strictEqual(ws.inputs().length,beforeInvalid,'invalid or oversized text never reaches the native provider');
  }
  paste('😀'.repeat(1024)); ws.ackAll();
  assert.strictEqual(ws.inputs().at(-1).event.text,'😀'.repeat(1024));
  const oldCanvas = canvas, oldDraws = f.displays[0].rectangles.length;
  f.switchBoard(); assert.strictEqual(ws.readyState, 3);
  ws.server(f.frame(3)); await tick(); assert.strictEqual(f.displays[0].rectangles.length, oldDraws, 'old stream cannot paint another board');
  assert.strictEqual(f.calls.filter(x => x.url === '/api/desktop/close').length, 0, 'changing views never closes the MCP/native session');
  const ws2 = await f.open(); assert.notStrictEqual($('desktop-image'), oldCanvas, 'fresh canvas isolates late noVNC image decodes');
  ws2.server({type: 'error', error: '输入结果未知', needs_observation: true}); await tick();
  assert.strictEqual(ws2.readyState, 3);
  assert.ok($('desktop-status').textContent.includes('未知'));
  assert.strictEqual(f.sockets.length, 2, 'disconnects never automatically reconnect or replay input');
  f.desktop.close();
  const g = fixture(), ws3 = await g.open();
  ws3.server(g.frame(3)); await tick(); assert.strictEqual(ws3.readyState, 3, 'a missing dirty rectangle requires a fresh observation');
  g.desktop.close();
  const reset = fixture(), resetSocket = await reset.open();
  resetSocket.server({type: 'reset', needs_observation: true, reason: 'frame-gap'});
  reset.$('desktop-image').fire('pointerdown', {button: 0, clientX: 20, clientY: 10});
  assert.strictEqual(resetSocket.inputs().length, 0, 'reset disables input until a full frame flushes');
  resetSocket.server(reset.frame(4, {frame_id: 'mode-2', reset: true})); await tick(); await tick();
  assert.strictEqual(resetSocket.readyState, 1);
  assert.strictEqual(resetSocket.sent.filter(x=>x.type==='release_all').length, 0, 'server reset does not start an endless client release/resync cycle');
  reset.$('desktop-image').fire('pointerdown', {button: 0, clientX: 20, clientY: 10});
  assert.strictEqual(resetSocket.inputs().at(-1).frame_id, 'mode-2');
  reset.desktop.close();
  const timeout = fixture({ackTimeout: 15}), timeoutSocket = await timeout.open();
  timeout.$('desktop-image').fire('pointerdown', {button: 0, clientX: 20, clientY: 10});
  timeout.$('desktop-image').fire('pointerup', {button: 0, clientX: 20, clientY: 10});
  await pause(35);
  assert.strictEqual(timeoutSocket.readyState, 3, 'unknown input receipt closes instead of resending');
  assert.strictEqual(timeoutSocket.inputs().length, 1, 'queued input never replays after an unknown outcome');
  assert.strictEqual(timeout.sockets.length, 1);
  timeout.desktop.close();
  const staleRender = fixture(), staleSocket = await staleRender.open();
  let finishOldRender;
  staleRender.displays[0].wait = new Promise(resolve => { finishOldRender = resolve; });
  staleSocket.server(staleRender.frame(2)); await tick();
  staleRender.desktop.close();
  const tabs = fixture(), tabSocket = await tabs.open();
  const initialObservations = tabSocket.sent.filter(x=>x.type==='observed').length;
  tabs.$('desktop-close').fire('click');
  assert.strictEqual(tabSocket.readyState, 1, 'returning to the terminal keeps the same desktop transport');
  tabSocket.ackAll();
  tabSocket.server(tabs.frame(2, {reset:true,frame_id:'hidden-fresh-mode'})); await tick(); await tick();
  assert.strictEqual(tabs.displays[0].rectangles.length, 2, 'hidden desktop still consumes incremental display updates');
  assert.strictEqual(tabSocket.sent.filter(x=>x.type==='observed').length, initialObservations, 'hidden desktop never observes or authorizes input');
  tabs.intervals.find(x=>x.ms===5000).fn();
  assert.strictEqual(tabSocket.sent.at(-1).type, 'heartbeat', 'only heartbeat keeps the hidden transport alive');
  tabs.$('desktop-image').fire('pointerdown', {button:0,clientX:20,clientY:10});
  tabs.$('desktop-clipboard-toggle').fire('click');
  assert.strictEqual(tabSocket.inputs().length, 0, 'hidden physical events and toolbar clicks never enqueue input');
  tabs.$('b-desktop').fire('click'); await tick(); await tick();
  assert.strictEqual(tabs.sockets.length, 1, 'same-page return uses no replacement handshake or native cleanup wait');
  assert.strictEqual(tabs.calls.filter(x=>x.url==='/api/desktop/open').length, 1);
  assert.strictEqual(tabSocket.sent.at(-1).type, 'observed');
  assert.strictEqual(tabSocket.sent.at(-1).frame_id, 'hidden-fresh-mode');
  tabs.$('desktop-close').fire('click'); tabs.$('b-desktop').fire('click'); await tick();
  assert.strictEqual(tabs.sockets.length, 1, 'immediate return before release ack also keeps the same transport');
  tabSocket.ackAll(); tabSocket.server(tabs.frame(3,{reset:true,frame_id:'immediate-return-mode'})); await tick(); await tick();
  tabs.$('desktop-image').fire('pointerdown', {button:0,clientX:20,clientY:10});
  assert.strictEqual(tabSocket.inputs().at(-1).frame_id, 'immediate-return-mode');
  tabSocket.ackAll(); tabs.document.hidden = true; tabs.fireGlobal('visibilitychange'); tabSocket.ackAll();
  const beforeHiddenObservation = tabSocket.sent.filter(x=>x.type==='observed').length;
  tabSocket.server(tabs.frame(4,{reset:true,frame_id:'page-visible-again-mode'})); await tick(); await tick();
  assert.strictEqual(tabSocket.sent.filter(x=>x.type==='observed').length,beforeHiddenObservation,'a background browser tab never observes even after a fresh frame');
  tabs.document.hidden = false; tabs.fireGlobal('visibilitychange'); await tick(); await tick();
  assert.strictEqual(tabSocket.sent.at(-1).type,'observed');
  assert.strictEqual(tabSocket.sent.at(-1).frame_id,'page-visible-again-mode','a quiet display re-observes its flushed frame when the browser tab becomes visible');
  tabs.desktop.close(); assert.strictEqual(tabSocket.readyState, 3, 'closing the board still detaches the transport');
  const toolbar = fixture(), toolbarSocket = await toolbar.open();
  toolbar.$('desktop-image').fire('blur');
  toolbarSocket.server(toolbar.frame(2));
  assert.strictEqual(toolbarSocket.readyState, 1, 'old in-transit deltas before release ack are ignored');
  toolbarSocket.ackAll(); toolbar.$('desktop-clipboard-toggle').fire('click');
  assert.strictEqual(toolbarSocket.inputs().length, 0, 'one new toolbar click waits for a complete observation');
  toolbarSocket.server(toolbar.frame(3, {reset: true, frame_id: 'fresh-after-release'})); await tick(); await tick();
  assert.strictEqual(toolbarSocket.inputs().length, 1);
  assert.strictEqual(toolbarSocket.inputs()[0].event.type, 'clipboard-read');
  assert.strictEqual(toolbarSocket.inputs()[0].frame_id, 'fresh-after-release');
  toolbarSocket.ackAll();
  toolbarSocket.server(toolbar.frame(4)); await tick(); await tick();
  assert.strictEqual(toolbarSocket.inputs().length, 1, 'fresh display updates never repeat the toolbar action');
  toolbar.$('desktop-image').fire('blur'); toolbarSocket.ackAll();
  toolbar.$('desktop-clipboard-toggle').fire('click'); // close the fallback panel
  toolbar.$('desktop-clipboard-toggle').fire('click'); // a new read waits for observation
  toolbar.switchBoard(); toolbarSocket.server(toolbar.frame(5, {reset: true})); await tick();
  const replacement = await toolbar.open();
  assert.strictEqual(replacement.inputs().length, 0, 'a pending toolbar intent never crosses a board/version change');
  toolbar.desktop.close();
  const freshSocket = await staleRender.open();
  finishOldRender(); await tick(); await tick();
  staleRender.displays.at(-1).wait = new Promise(() => {});
  for (let seq = 2; seq <= 18; seq++) freshSocket.server(staleRender.frame(seq));
  assert.strictEqual(freshSocket.readyState, 3, 'late render completion from another connection cannot weaken the 16-frame backpressure limit');
  staleRender.desktop.close();
  const setup = fixture({setup:true});
  setup.desktop.refresh({key:'board-a',serial:'SERIAL-A'}); await tick();
  assert.strictEqual(setup.$('b-desktop').hidden,false,'unconfigured boards keep a discoverable desktop entry');
  setup.$('b-desktop').fire('click'); await tick(); await tick();
  assert.strictEqual(setup.$('desktop-connection').hidden,false);
  assert.strictEqual(setup.calls.filter(x=>x.url==='/api/desktop/discover').length,1);
  assert.strictEqual(setup.calls.filter(x=>x.url==='/api/desktop/open').length,0,'discovery never logs in');
  setup.$('desktop-services').value='rdp-service'; setup.$('desktop-services').fire('change');
  assert.strictEqual(setup.$('desktop-certificate-policy').value,'accept');
  assert.strictEqual(setup.$('desktop-pin-field').hidden,true);
  setup.$('desktop-certificate-policy').value='pin';
  setup.$('desktop-trust').checked=false;
  setup.$('desktop-password').value='server-only'; setup.$('desktop-connection').fire('submit'); await tick();
  assert.strictEqual(setup.calls.filter(x=>x.url==='/api/desktop/configure').length,0,'optional pins still need human trust');
  setup.$('desktop-certificate-policy').value='accept';
  setup.$('desktop-connection').fire('submit'); await tick(); await tick();
  const submitted=setup.calls.find(x=>x.url==='/api/desktop/configure').body;
  assert.strictEqual(submitted.candidate_id,'rdp-service');
  assert.strictEqual(submitted.certificate_policy,'accept');
  assert.strictEqual(submitted.certificate_sha256,'');
  assert.strictEqual(submitted.client_id,setup.window.DeckLabUI.clientId);
  assert.strictEqual(setup.calls.filter(x=>x.url==='/api/desktop/open').length,1);
  assert.strictEqual(setup.$('desktop-password').value,'','passwords disappear from the form after saving');
  setup.desktop.close();
  const pinned = fixture({setup:true,
    saved:{provider:'rdp',host:'10.0.0.2',port:3389,username:'rdp',password_saved:true,certificate_sha256:'a'.repeat(64)},
    connectionFailure:{code:'desktop-certificate-rejected',error:'证书已更换'}});
  pinned.desktop.refresh({key:'board-a',serial:'SERIAL-A'}); await tick();
  pinned.$('b-desktop').fire('click'); await tick(); await tick();
  assert.strictEqual(pinned.$('desktop-connect-without-pin').hidden,false);
  pinned.$('desktop-connect-without-pin').fire('click'); await tick(); await tick();
  const recovery = pinned.calls.find(x=>x.url==='/api/desktop/configure').body;
  assert.strictEqual(recovery.certificate_policy,'accept');
  assert.strictEqual(recovery.reuse_password,true);
  assert.strictEqual(recovery.password,undefined);
  assert.strictEqual(pinned.sockets.length,1);
  pinned.desktop.close();
  const vncSetup=fixture({setup:true});vncSetup.desktop.refresh({key:'board-a',serial:'SERIAL-A'});await tick();
  vncSetup.$('b-desktop').fire('click');await tick();await tick();
  vncSetup.$('desktop-services').value='vnc-service';vncSetup.$('desktop-services').fire('change');
  assert.strictEqual(vncSetup.$('desktop-certificate-field').hidden,true);
  vncSetup.$('desktop-connection').fire('submit');await tick();await tick();
  assert.strictEqual(vncSetup.calls.find(x=>x.url==='/api/desktop/configure').body.provider,'vnc');
  vncSetup.desktop.close();
  let finishDiscovery;
  const lateSetup=fixture({setup:true,discovery:()=>new Promise(resolve=>{finishDiscovery=resolve;})});
  lateSetup.desktop.refresh({key:'board-a',serial:'SERIAL-A'});await tick();lateSetup.$('b-desktop').fire('click');await tick();
  lateSetup.switchBoard();await tick();finishDiscovery({candidates:[{id:'old',host:'old-board',provider:'vnc',port:5900}]});await tick();
  assert.strictEqual(lateSetup.$('desktop-host').value,'','late discovery never replaces the new board fields');
  assert.strictEqual(lateSetup.calls.filter(x=>x.url==='/api/desktop/open').length,0);
  lateSetup.desktop.close();
  const ended=fixture({closeFailure:404});await ended.open();
  ended.$('desktop-disconnect').fire('click');await tick();await tick();
  assert.ok(ended.$('desktop-status').textContent.includes('重新连接'),'a session the server already dropped still disconnects locally');
  ended.$('desktop-settings').fire('click');
  assert.ok(!ended.$('desktop-status').textContent.includes('先断开'),'settings open after the server dropped the session');
  ended.desktop.close();
  const failedClose=fixture({setup:true,saved:{provider:'rdp',host:'desktop.example.test',port:3389,username:'rdp'},closeFailure:500});await failedClose.open();
  failedClose.$('desktop-disconnect').fire('click');await tick();await tick();
  failedClose.$('desktop-settings').fire('click');
  assert.equal(failedClose.$('desktop-connection').hidden,false,'settings remain accessible after a disconnect failure');
  assert.equal(failedClose.$('desktop-save-connect').textContent,'保存并重新连接');
  failedClose.$('desktop-connection').fire('submit');await tick();await tick();
  assert.equal(failedClose.calls.filter(x=>x.url==='/api/desktop/configure').length,0,'save waits for a confirmed close');
  failedClose.desktop.close();
  console.log('Desktop UI: native incremental stream, noVNC rendering/input, ordered drag/keys, release, clipboard, fullscreen and stale isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { for (const close of cleanup) close(); });
