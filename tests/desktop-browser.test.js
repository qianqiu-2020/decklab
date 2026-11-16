'use strict';
// Real Chromium + unmodified noVNC/xterm modules; all sessions and frames below
// are synthetic. This fixture never starts a collector or connects to a board.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {launchBrowser, stopBrowser} = require('../tools/browser_launch');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-desktop-browser-'));
const errors = [], requests = [];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const bootstrap = `
window.fixture = {sent: [], posts: [], streams: [], sockets: [], configured:true, connection:{}, discovered:[
 {id:'rdp-found',provider:'rdp',host:'10.0.0.88',port:3389,certificate_sha256:'a'.repeat(64)},
 {id:'vnc-found',provider:'vnc',host:'10.0.0.88',port:5901}]};
SerialdI18n.setLocale('zh-CN');
const $ = id => document.getElementById(id), noop = () => {};
document.body.classList.add('detail-open'); $('workspace').classList.add('selected');
$('detail').hidden = false; $('d-name').textContent = 'Desktop fixture';
$('d-dev').textContent = 'Synthetic session · no hardware'; $('d-key').textContent = 'FIXTURE';
$('grid').innerHTML = '<article class="dev sel"><strong>Desktop fixture</strong><p>VNC / RDP viewer fixture</p></article>';
$('d-tabs').innerHTML = '<button class="on" type="button">SSH</button><button type="button">串口</button>';
window.fetch = async (url, options) => {
  const body = options && options.body ? JSON.parse(options.body) : null;
  if (body) fixture.posts.push({url, body});
  const setup = () => ({revision:'fixture-revision',connection:fixture.connection,hosts:['10.0.0.88'],suggested_user:'user'});
  let data = url.startsWith('/api/desktop/sessions') ? {supported: fixture.configured, sessions: [],setup:setup()} :
    url === '/api/desktop/open' ? {session: {id: 'fixture-session', serial: 'FIXTURE-SN'}} : {ok: true};
  if(url==='/api/desktop/discover')data={candidates:fixture.discovered,reason:fixture.discovered.length?'':'未发现 VNC／RDP 服务，可填写地址手工连接'};
  if(url==='/api/desktop/configure') {
    fixture.configured=true;fixture.connection={provider:body.provider,host:body.host,port:body.port,username:body.username,password_saved:true,certificate_sha256:body.certificate_sha256};
    data={ok:true,setup:setup()};
  }
  return {ok: true, status: 200, json: async () => data};
};
window.EventSource = class {
  constructor(url) { this.url = url; fixture.streams.push(this); }
  close() { this.closed = true; }
  emit(text, extra = {}) { if (!this.closed) this.onmessage({data: JSON.stringify(Object.assign({key: 'fixture', sid: controller.current.sid,
    off: (controller.current.off || 0) + text.length + 1, data_b64: btoa(text), alive: true}, extra))}); }
};
const controller = window.SerialdTerminalView.create({byId: $, fetch: window.fetch,
  board: () => ({serial: 'FIXTURE-SN'}), session: sid => ({kind: sid === 'serial' ? 'serial' : 'ssh', actor: 'web'}),
  onSelect: noop, takenOver: () => false, ownsInput: () => true, setInputMode: noop,
  clearCommandDecorations: noop, clearTerminalSearch: noop, renderCommandNav: noop,
  scheduleCommandNav: noop, copyText: noop, copySel: noop, searchTerminal: noop, loadCommands: noop});
window.fixture.controller = controller;
fixture.select = sid => { const ctx = controller.create('fixture', sid); controller.resize(); controller.start(ctx); return ctx; };
fixture.select('ssh-fixture');
function picture(width, height, color, text) {
  const source = document.createElement('canvas'); source.width = width; source.height = height;
  const paint = source.getContext('2d'); paint.fillStyle = color; paint.fillRect(0, 0, width, height);
  if (text) { paint.fillStyle = '#fff'; paint.font = '24px sans-serif'; paint.fillText(text, 32, 58);
    paint.font = '16px sans-serif'; paint.fillText('Continuous native-frame viewer · synthetic fixture', 32, 96); }
  return source.toDataURL('image/png').split(',')[1];
}
class Stream {
  constructor(url, protocol) {
    this.url = url; this.protocol = protocol; this.readyState = 0; this.sequence = 0; fixture.sockets.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen(); this.server({type: 'ready',
      session: {id: 'fixture-session', serial: 'FIXTURE-SN'}, capabilities: {}});
      this.frame(true); this.timer = setInterval(() => this.frame(false), 80); }, 10);
  }
  server(message) { if (this.readyState === 1) this.onmessage({data: JSON.stringify(message)}); }
  frame(reset) {
    if (reset) this.mode = (this.mode || 0) + 1;
    this.server({type: 'frame', sequence: ++this.sequence, generation: 1, frame_id: 'fixture-mode-' + this.mode,
      width: 640, height: 400, x: reset ? 0 : 32, y: reset ? 0 : 140,
      rect_width: reset ? 640 : 64, rect_height: reset ? 400 : 40,
      mime: 'image/png', png_base64: reset ? picture(640, 400, '#1d4971', 'DeckLab desktop') :
        picture(64, 40, this.sequence % 2 ? '#47c08c' : '#e5b864'), reset});
  }
  send(value) {
    const message = JSON.parse(value); fixture.sent.push(message);
    if (message.type === 'input' && message.frame_id !== 'fixture-mode-' + this.mode) {
      this.server({type:'error',error:'fixture rejected stale observation',needs_observation:true}); return;
    }
    if (message.type === 'observed') setTimeout(() => this.server({type:'observed',ok:true,frame_id:message.frame_id,sequence:message.sequence}), 1);
    if (message.type === 'heartbeat') setTimeout(() => this.server({type:'heartbeat',ok:true}), 1);
    if (message.type === 'input' || message.type === 'release_all') setTimeout(() => {
      this.server({type: 'ack', seq: message.seq, ok: true});
      if (message.type === 'release_all') this.frame(true);
      if (message.event && message.event.type === 'clipboard-read') this.server({type: 'clipboard', text: 'Remote fixture text', encoding: 'utf-8'});
    }, 5);
  }
  close() { clearInterval(this.timer); this.readyState = 3; if (this.onclose) this.onclose(); }
}
fixture.desktop = SerialdDesktop.create({byId: $, fetch: window.fetch, clientId: 'fixture-page',
  getView: () => controller.current, websocketFactory: (url, protocol) => new Stream(url, protocol)});
fixture.desktop.refresh(controller.current);
fixture.ready = true;
`;
const scripts = ['vendor/xterm.js', 'vendor/addon-fit.js', 'static/i18n.js', 'static/ui-common.js', 'static/tasks.js',
  'static/terminal-policy.js', 'static/terminal-view.js', 'static/desktop.js'];
const server = http.createServer((req, res) => {
  const route = new URL(req.url, 'http://fixture').pathname; requests.push(route);
  if (route === '/') {
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
      .replace('</body>', scripts.map(file => '<script src="/' + file + '"></script>').join('') + '<script src="/fixture.js"></script></body>');
    res.setHeader('Content-Type', 'text/html'); res.end(html); return;
  }
  if (route === '/fixture.js') { res.setHeader('Content-Type', 'text/javascript'); res.end(bootstrap); return; }
  if (!route.startsWith('/static/') && !route.startsWith('/vendor/')) { res.statusCode = 404; res.end(); return; }
  const filename = path.resolve(root, '.' + route);
  if (!filename.startsWith(root + '/') || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) { res.statusCode = 404; res.end(); return; }
  res.setHeader('Content-Type', route.endsWith('.js') ? 'text/javascript' : route.endsWith('.css') ? 'text/css' :
    route.endsWith('.svg') ? 'image/svg+xml' : route.endsWith('.png') ? 'image/png' : 'text/plain');
  res.end(fs.readFileSync(filename));
});
let chrome, ws;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = 'http://127.0.0.1:' + server.address().port;
  const launched = await launchBrowser(profile); chrome = launched.chrome;
  const tab = await (await fetch('http://127.0.0.1:' + launched.port + '/json/new?' + encodeURIComponent(address), {method: 'PUT'})).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let id = 0; const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const call = pending.get(message.id); pending.delete(message.id);
      if (message.error) call.reject(new Error(JSON.stringify(message.error))); else call.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
  };
  const rpc = (method, params = {}) => new Promise((resolve, reject) => { const mid = ++id; pending.set(mid, {resolve, reject}); ws.send(JSON.stringify({id: mid, method, params})); });
  const evaluate = async expression => {
    const result = await rpc('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, userGesture: true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value;
  };
  const until = async (expression, label) => {
    for (let n = 0; n < 100; n++) { if (await evaluate(expression)) return; await delay(50); }
    throw new Error('Timed out: ' + label + '\n' + await evaluate('document.getElementById("desktop-status").textContent'));
  };
  await rpc('Runtime.enable'); await rpc('Page.enable');
  await rpc('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await until('window.fixture && fixture.ready && !document.getElementById("b-desktop").hidden', 'fixture startup');
  await delay(400);
  await evaluate('fixture.posts.length=0; document.getElementById("b-desktop").click()');
  await until('fixture.sent.filter(m=>m.type==="observed").length>=4', 'real noVNC asynchronous image decoding');
  assert.deepEqual(await evaluate('Array.from(document.getElementById("desktop-image").getContext("2d").getImageData(600,350,1,1).data)'), [29,73,113,255]);
  assert.equal(await evaluate('fixture.sockets[0].protocol'), 'decklab-desktop-v1');
  assert.equal(await evaluate('document.getElementById("d-term").getClientRects().length'), 0);
  const box = await evaluate('JSON.parse(JSON.stringify(document.getElementById("desktop-image").getBoundingClientRect()))');
  const mouse = (type, x, y, extra = {}) => rpc('Input.dispatchMouseEvent', Object.assign({type, x, y}, extra));
  await mouse('mouseMoved', box.x+100, box.y+100);
  await mouse('mousePressed', box.x+100, box.y+100, {button: 'left', buttons: 1, clickCount: 1});
  await mouse('mouseMoved', box.x+180, box.y+150, {button: 'left', buttons: 1});
  await mouse('mouseReleased', box.x+180, box.y+150, {button: 'left', buttons: 0, clickCount: 1});
  await rpc('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Control', code: 'ControlLeft', windowsVirtualKeyCode: 17, modifiers: 2});
  await rpc('Input.dispatchKeyEvent', {type: 'keyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2});
  await rpc('Input.dispatchKeyEvent', {type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2});
  await rpc('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Control', code: 'ControlLeft', windowsVirtualKeyCode: 17});
  await mouse('mouseWheel', box.x+180, box.y+150, {deltaY: 120, deltaX: 0});
  await delay(200);
  const inputs = await evaluate('fixture.sent.filter(m=>m.type==="input").map(m=>m.event)');
  assert.ok(inputs.some(e=>e.type==='pointer' && e.buttons===1));
  assert.ok(inputs.some(e=>e.type==='pointer' && e.buttons===0));
  assert.deepEqual(inputs.filter(e=>e.type==='key').map(e=>[e.code,e.down]), [['ControlLeft',true],['KeyA',true],['KeyA',false],['ControlLeft',false]]);
  assert.ok(inputs.some(e=>e.type==='wheel' && e.steps===3));
  await evaluate('fixture.streams.at(-1).emit("background ssh\\r\\n"); window.dispatchEvent(new Event("resize"))');
  await rpc('Emulation.setDeviceMetricsOverride', {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await delay(700);
  assert.equal(await evaluate('fixture.posts.filter(p=>p.url==="/api/session-resize").length'), 0, 'hidden SSH must not negotiate dimensions');
  assert.ok(await evaluate('Array.from({length:fixture.controller.current.term.buffer.active.length},(_,n)=>fixture.controller.current.term.buffer.active.getLine(n)?.translateToString()).join("\\n").includes("background ssh")'), 'hidden SSH output still parses');
  assert.equal(await evaluate('fixture.streams.length'), 1, 'desktop must not create another terminal reader');
  await evaluate('document.getElementById("desktop-close").click(); document.getElementById("b-desktop").click()');
  assert.equal(await evaluate('fixture.sockets.length'),1,'immediate same-task terminal→desktop return never creates a competing handshake');
  await until('document.getElementById("desktop-overlay").hidden','immediate same-page return after release observation');
  await evaluate('document.getElementById("desktop-close").click()');
  await until('fixture.posts.some(p=>p.url==="/api/session-resize" && p.body.sid==="ssh-fixture")','terminal geometry restored');
  assert.equal(await evaluate('fixture.sockets.length'),1);
  assert.equal(await evaluate('fixture.sockets[0].readyState'),1,'same-page terminal view keeps the original desktop connection');
  assert.ok(await evaluate('fixture.posts.some(p=>p.url==="/api/session-resize" && p.body.sid==="ssh-fixture")'), 'returning to SSH restores geometry negotiation');
  await evaluate('fixture.select("serial"); document.getElementById("b-desktop").click()');
  await until('fixture.sockets.length===1 && document.getElementById("desktop-overlay").hidden', 'same desktop transport resumed for serial view');
  await evaluate(`fixture.posts.length=0; fixture.streams.at(-1).emit('background serial\\r\\n', {
    terminal_state:{managed:true,phase:'text',geometry:{cols:80,rows:24},revision:1,control_revision:1}});
    window.dispatchEvent(new Event('resize')); fixture.controller.syncSerialSize()`);
  await delay(700);
  assert.equal(await evaluate('fixture.posts.filter(p=>p.url==="/api/session-resize").length'), 0, 'hidden serial output must not request presentation geometry');
  await evaluate('document.getElementById("desktop-close").click(); fixture.streams.at(-1).emit("foreground serial\\r\\n", {terminal_state:{managed:true,phase:"text",geometry:{cols:80,rows:24},revision:2,control_revision:1}})');
  await until('fixture.posts.some(p=>p.url==="/api/session-resize" && p.body.presentation)', 'visible serial resumes presentation negotiation');
  await evaluate('document.getElementById("b-desktop").click()');
  await until('fixture.sockets.length===1 && document.getElementById("desktop-overlay").hidden', 'same desktop transport resumed after terminal');
  await evaluate('document.getElementById("desktop-clipboard-toggle").click(); document.getElementById("desktop-image").focus()');
  const readButton = await evaluate('JSON.parse(JSON.stringify(document.getElementById("desktop-clipboard-read").getBoundingClientRect()))');
  const beforeRead = await evaluate('fixture.sent.filter(m=>m.event && m.event.type==="clipboard-read").length');
  await mouse('mousePressed', readButton.x+readButton.width/2, readButton.y+readButton.height/2, {button:'left',buttons:1,clickCount:1});
  await mouse('mouseReleased', readButton.x+readButton.width/2, readButton.y+readButton.height/2, {button:'left',buttons:0,clickCount:1});
  await until('document.getElementById("desktop-text").value==="Remote fixture text"', 'explicit remote clipboard');
  assert.equal(await evaluate('fixture.sent.filter(m=>m.event && m.event.type==="clipboard-read").length'),beforeRead+1, 'one real toolbar click sends one request after focus-release observation');
  await evaluate('SerialdI18n.setLocale("en")');
  assert.equal(await evaluate('document.getElementById("desktop-status").textContent'), 'Remote clipboard read. Edit it or copy it to this computer.');
  assert.equal(await evaluate('document.getElementById("desktop-close").textContent'), 'Back to terminal');
  assert.equal(await evaluate('document.getElementById("desktop-type-text").textContent'), 'Type text');
  await evaluate('SerialdI18n.setLocale("zh-CN")');
  assert.equal(await evaluate('document.getElementById("desktop-status").textContent'), '已读取远端剪贴板，可编辑或复制到本机');
  assert.equal(await evaluate('document.getElementById("desktop-close").textContent'), '返回终端');
  await evaluate('document.getElementById("desktop-text").value="中文输入 😀"; document.getElementById("desktop-image").focus()');
  const textButton = await evaluate('JSON.parse(JSON.stringify(document.getElementById("desktop-type-text").getBoundingClientRect()))');
  await mouse('mousePressed', textButton.x+textButton.width/2, textButton.y+textButton.height/2, {button:'left',buttons:1,clickCount:1});
  await mouse('mouseReleased', textButton.x+textButton.width/2, textButton.y+textButton.height/2, {button:'left',buttons:0,clickCount:1});
  await until('document.getElementById("desktop-status").textContent==="文本已发送到当前应用"', 'Unicode text action after focus-release observation');
  assert.deepEqual(await evaluate('fixture.sent.filter(m=>m.event && m.event.type==="text").map(m=>m.event.text)'), ['中文输入 😀'], 'one real toolbar click sends Unicode application input exactly once');
  await evaluate('SerialdI18n.setLocale("en")');
  assert.equal(await evaluate('document.getElementById("desktop-status").textContent'), 'Text sent to the current application');
  assert.equal(await evaluate('document.getElementById("desktop-text").value'), '中文输入 😀', 'locale changes never translate user text');
  await evaluate('SerialdI18n.setLocale("zh-CN")');
  await evaluate('document.getElementById("desktop-fullscreen").click()');
  await until('document.fullscreenElement===document.getElementById("desktop-panel")', 'true fullscreen');
  await evaluate('document.exitFullscreen()');
  await evaluate('document.getElementById("desktop-scale").value="2"; document.getElementById("desktop-scale").dispatchEvent(new Event("change"))');
  const zoom = await evaluate('({canvas:document.getElementById("desktop-image").getBoundingClientRect().left,viewport:document.getElementById("desktop-screen").getBoundingClientRect().left,scroll:document.getElementById("desktop-screen").scrollWidth,width:document.getElementById("desktop-screen").clientWidth})');
  assert.ok(zoom.canvas >= zoom.viewport && zoom.scroll > zoom.width, 'zoomed display starts inside its scrollable viewport');
  await evaluate('document.getElementById("desktop-scale").value="fit"; document.getElementById("desktop-scale").dispatchEvent(new Event("change"))');
  const output = path.join(root, 'dist', 'desktop-stream-frontend'); fs.mkdirSync(output, {recursive: true});
  for (const [name, width, height, dark] of [['light',1280,900,false],['dark',1280,900,true],['mobile',390,844,false]]) {
    await evaluate('SerialdI18n.setLocale(' + JSON.stringify(name === 'mobile' ? 'en' : 'zh-CN') + ')');
    await rpc('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
    await rpc('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:dark?'dark':'light'}]});
    await delay(150);
    const layout = await evaluate('({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,screen:document.getElementById("desktop-screen").getBoundingClientRect().height})');
    assert.ok(layout.scroll <= layout.width+1, name + ' must not overflow the page'); assert.ok(layout.screen >= 150, name + ' keeps a usable display');
    if (name === 'mobile') {
      await evaluate('document.querySelector(".desktop-clipboard-actions button:last-child").scrollIntoView({block:"nearest"})');
      assert.ok(await evaluate('document.querySelector(".desktop-clipboard-actions button:last-child").getBoundingClientRect().bottom <= innerHeight'), 'mobile clipboard actions remain reachable through panel scrolling');
    }
    const shot = await rpc('Page.captureScreenshot', {format: 'png'}); fs.writeFileSync(path.join(output,name+'.png'), Buffer.from(shot.data,'base64'));
  }
  await evaluate('SerialdI18n.setLocale("zh-CN"); fixture.desktop.close(); fixture.configured=false; fixture.desktop.refresh(null);fixture.desktop.refresh(fixture.controller.current)');
  await until('!document.getElementById("b-desktop").hidden', 'unconfigured desktop entry remains visible');
  await evaluate('document.getElementById("b-desktop").click()');
  await until('document.getElementById("desktop-services").options.length===3 && !document.getElementById("desktop-detect").disabled', 'automatic VNC and RDP discovery');
  assert.equal(await evaluate('document.getElementById("desktop-connection").hidden'),false);
  const beforeConfigure=await evaluate('fixture.posts.filter(x=>x.url==="/api/desktop/configure").length');
  await evaluate('document.getElementById("desktop-services").value="rdp-found";document.getElementById("desktop-services").dispatchEvent(new Event("change"));document.getElementById("desktop-password").value="fixture-only-password";document.getElementById("desktop-connection").requestSubmit()');
  await delay(50);assert.equal(await evaluate('fixture.posts.filter(x=>x.url==="/api/desktop/configure").length'),beforeConfigure,'untrusted certificate never causes connection');
  for (const [name,width,height] of [['setup-desktop',1280,900],['setup-mobile',390,844]]) {
    await rpc('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await delay(80);
    assert.ok(await evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1'),name+' setup fits');
    if (name === 'setup-mobile') {
      await evaluate('document.getElementById("desktop-save-connect").scrollIntoView({block:"nearest"})');
      assert.ok(await evaluate('document.getElementById("desktop-save-connect").getBoundingClientRect().bottom<=innerHeight'),'mobile connection submit remains reachable');
    }
    const shot=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));
  }
  await evaluate('document.getElementById("desktop-trust").checked=true;document.getElementById("desktop-connection").requestSubmit()');
  await until('fixture.sockets.length===2 && document.getElementById("desktop-overlay").hidden','save RDP settings and connect through existing viewer');
  assert.equal(await evaluate('fixture.posts.filter(x=>x.url==="/api/desktop/configure").at(-1).body.provider'),'rdp');
  assert.equal(await evaluate('document.getElementById("desktop-password").value'),'');
  await evaluate('fixture.desktop.close();fixture.configured=false;fixture.connection={};fixture.discovered=[];fixture.desktop.refresh(null);fixture.desktop.refresh(fixture.controller.current);document.getElementById("b-desktop").click()');
  await until('!document.getElementById("desktop-connection").hidden && document.getElementById("desktop-detect-status").textContent.includes("未发现")','manual fallback with no detected server');
  await evaluate('document.getElementById("desktop-protocol").value="vnc";document.getElementById("desktop-protocol").dispatchEvent(new Event("change"));document.getElementById("desktop-host").value="10.0.0.89";document.getElementById("desktop-port").value="5905";document.getElementById("desktop-connection").requestSubmit()');
  await until('fixture.sockets.length===3 && document.getElementById("desktop-overlay").hidden','manual VNC connects through same viewer');
  assert.equal(await evaluate('fixture.posts.filter(x=>x.url==="/api/desktop/configure").at(-1).body.provider'),'vnc');
  assert.equal(requests.filter(r=>r.startsWith('/api/desktop/frame')).length,0, 'viewer must never poll screenshots');
  assert.ok(requests.includes('/vendor/novnc/core/display.js') && requests.includes('/vendor/novnc/core/input/keyboard.js'));
  assert.deepEqual(errors, []);
  console.log('Real Chromium noVNC display/input + hidden terminal resize tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if (ws) ws.close(); await stopBrowser(chrome); await new Promise(resolve=>server.close(resolve));
  fs.rmSync(profile,{recursive:true,force:true});
});
