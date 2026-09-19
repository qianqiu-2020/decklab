'use strict';
// Real bundled xterm + Chromium, isolated HTTP fixtures. No seriald or board access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'seriald-browser-'));
const posts = [], errors = [];
const fixtures = [
  { key: 'musepi2', serial: '5b9d4a0a6f5e25a8', state: 'online', alive: true, dev: '/dev/ttyUSB0', hostname: 'bianbu-k3' },
  { key: 'riscv-118', serial: '26abd51e5bc2dbe1', state: 'offline', alive: false, last_dev: '/dev/ttyUSB4' },
  { key: 'milkv2', serial: '0059D968E583C49A', state: 'online', alive: true, dev: '/dev/ttyUSB2', hostname: 'bianbu' },
  { key: 'milkv', serial: 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6', state: 'online', alive: true, dev: '/dev/ttyUSB1', hostname: 'k1' },
  { key: 'unprovisioned-demo', serial: '', state: 'pending', alive: true, dev: '/dev/ttyUSB3', by_path: 'pci-usb-0:4.2.3' },
].map(d => ({ ...d, label: d.key, bytes: 8192, probes: 0, last_seen: Date.now() / 1000,
  info: ['milkv2', 'milkv'].includes(d.key) ? { serial: d.serial, reported_serial: '0123456789ABCDEF',
    serial_source: d.key === 'milkv' ? 'boot-marker' : 'soc-sysfs', identity_status: 'provisional' } : {} }));
const log = '<script>globalThis.injected = true</script>\n2026-09-19 command: uname -a\nLinux bianbu 6.6.63 riscv64\n';
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://localhost');
  res.setHeader('Cache-Control', 'no-store');
  const json = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  if (req.method === 'POST') { posts.push(u.pathname); json({ ok: false, error: 'read-only fixture' }); return; }
  if (u.pathname === '/api/devices') return json({ devices: fixtures });
  if (u.pathname === '/api/session') {
    const bytes = u.searchParams.get('off') === '0' ? Buffer.from('root@bianbu:~# \x1b[6n\x1b[?6n\x1b[c\x1b]11;?\x07'.repeat(15)) : Buffer.alloc(0);
    return json({ key: u.searchParams.get('key'), sid: u.searchParams.get('sid'), alive: true,
      off: 4096, data_b64: bytes.toString('base64') });
  }
  if (u.pathname === '/api/sessions') return json({ sessions: [
    { sid: 'serial', kind: 'serial' }, { sid: 'ssh123abc', kind: 'ssh', actor: 'claude-code', ip: '10.0.90.42' }] });
  if (u.pathname === '/api/activity') return json({ next_offset: 2, sessions: [
    { sid: 'ssh123abc', actor: 'claude-code', title: 'dmesg | tail -50', status: 'active', started: Date.now()/1000, exit_code: null },
    { sid: 'ssh456def', actor: 'codex/diagnosis', title: 'uname -a', status: 'completed', started: Date.now()/1000-300, exit_code: 0 },
  ] });
  if (u.pathname === '/api/logs') return json({ serial: '0059D968E583C49A', label: 'milkv2', files: [
    { name: 'ssh-ssh456def-20260919.log', size: log.length }, { name: 'serial-20260919.log', size: 8192 }] });
  if (u.pathname === '/api/log') return json({ data: log, off: log.length, start_off: 0, size: log.length, more: false });
  const route = u.pathname === '/' ? '/index.html' : u.pathname;
  if (!['/index.html', '/static/app.js', '/static/ui-common.js', '/static/terminal-policy.js', '/static/app.css', '/vendor/xterm.js', '/vendor/addon-fit.js', '/vendor/xterm.css'].includes(route)) { res.statusCode=404; res.end(); return; }
  res.setHeader('Content-Type', route.endsWith('.js') ? 'application/javascript' : route.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(path.join(root, route)));
});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let chrome, ws;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = 'http://127.0.0.1:' + server.address().port;
  chrome = spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome', [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run', '--disable-gpu',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { stdio: 'ignore' });
  chrome.on('error', error => errors.push(error.message));
  for (let n=0; n<100 && !fs.existsSync(path.join(profile, 'DevToolsActivePort')); n++) await delay(100);
  const port = fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
  const tab = await (await fetch('http://127.0.0.1:' + port + '/json/new?' + encodeURIComponent(address), { method: 'PUT' })).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => { ws.onopen=resolve; ws.onerror=reject; });
  let id=0; const pending=new Map();
  ws.onmessage = event => { const msg=JSON.parse(event.data); if(msg.id) { const call=pending.get(msg.id); pending.delete(msg.id); msg.error?call.reject(new Error(JSON.stringify(msg.error))):call.resolve(msg.result); }
    else if(msg.method==='Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails.text + ': ' + (msg.params.exceptionDetails.exception?.description || '')); };
  const rpc=(method,params={})=>new Promise((resolve,reject)=>{ const mid=++id;pending.set(mid,{resolve,reject});ws.send(JSON.stringify({id:mid,method,params})); });
  const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  await rpc('Runtime.enable'); await rpc('Page.enable');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1500,height:1200,deviceScaleFactor:1,mobile:false});
  for(let n=0;n<50 && !await evaluate('document.querySelectorAll(".dev").length === 5');n++)await delay(100);
  assert.equal(await evaluate('document.querySelectorAll(".dev").length'),5);
  await evaluate('window.OriginalTerminal=Terminal; window.Terminal=class extends OriginalTerminal {constructor(...args){super(...args);window.testTerm=this;}}');
  await evaluate('document.querySelector(\'[data-key="milkv"]\').click()');
  await delay(300);
  assert.ok(await evaluate('document.querySelector("#d-info").textContent.includes("/boot 软件标记")'));
  assert.ok(await evaluate('document.querySelector("#d-info").textContent.includes("候选身份编号")'));
  assert.ok(await evaluate('document.querySelector("#d-info").textContent.includes("待确认")'));
  assert.deepEqual(posts,[], 'viewing software identity must not initialize it');
  await evaluate('document.querySelector(\'[data-key="milkv2"]\').click()');
  await delay(1500);
  assert.deepEqual(posts,[], 'rendering cursor queries must not send a POST');
  assert.ok(await evaluate('document.querySelector("#d-info").textContent.includes("SoC / eFuse")'));
  assert.ok(await evaluate('document.querySelector("#d-info").textContent.includes("0123456789ABCDEF")'));
  await evaluate('document.querySelector("#d-activity button").click()');
  await delay(700);
  assert.deepEqual(posts,[], 'viewing an agent session must not send reports or resize it');
  assert.ok(await evaluate('testTerm.cols > 80'), 'terminal fits the detail panel');
  assert.equal(await evaluate('document.querySelector("#b-send").disabled'), true);
  await evaluate('document.querySelector("#d-logs button").click()');
  await delay(300);
  assert.equal(await evaluate('document.querySelector("#log-content").textContent'),log);
  assert.equal(await evaluate('globalThis.injected === undefined'),true);
  assert.equal(await evaluate('document.querySelector("#log-viewer").hidden'),false);


  const out=path.join(root,'dist');fs.mkdirSync(out,{recursive:true});
  const screenshot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-desktop.png'),Buffer.from(screenshot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(300);
  assert.ok(await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'),'mobile layout must not overflow');
  const mobile=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-mobile.png'),Buffer.from(mobile.data,'base64'));
  assert.deepEqual(errors,[]);
  console.log('Browser smoke passed: real xterm query suppression, agent read-only view, inline logs, desktop/mobile layout.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(ws)ws.close(); if(chrome){chrome.kill('SIGTERM');await delay(500);}server.close();
  fs.rmSync(profile,{recursive:true,force:true});
});
