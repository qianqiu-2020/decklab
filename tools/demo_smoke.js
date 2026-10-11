'use strict';
// Real UI against the demo's production HTTP/SSE layers. Never touches boards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-demo-browser-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let demo, chrome, ws;
const errors = [];
async function until(check, description) {
  for (let n = 0; n < 100; n++) {
    if (await check()) return;
    await delay(100);
  }
  throw new Error('Timed out: ' + description);
}
(async () => {
  demo = spawn(process.env.PYTHON || 'python3', ['seriald_demo.py', '--port', '0'], {cwd: root});
  let output = '';
  demo.stdout.on('data', chunk => { output += chunk; });
  demo.stderr.on('data', chunk => errors.push(String(chunk)));
  demo.on('error', error => errors.push(error.message));
  await until(() => /http:\/\/127.0.0.1:\d+\//.test(output), 'demo launch');
  const address = output.match(/http:\/\/127.0.0.1:\d+\//)[0];
  chrome = spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome', [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
    '--disable-gpu', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
    {stdio: 'ignore'});
  chrome.on('error', error => errors.push(error.message));
  await until(() => fs.existsSync(path.join(profile, 'DevToolsActivePort')), 'Chromium launch');
  const port = fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
  const tab = await (await fetch('http://127.0.0.1:' + port + '/json/new?' + encodeURIComponent(address),
                                {method: 'PUT'})).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const msg = JSON.parse(event.data);
    if (msg.id) {
      const call = pending.get(msg.id);
      if (!call) return;
      pending.delete(msg.id);
      clearTimeout(call.timer);
      msg.error ? call.reject(new Error(JSON.stringify(msg.error))) : call.resolve(msg.result);
    } else if (msg.method === 'Runtime.exceptionThrown') {
      errors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    }
  };
  const rpc = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    const timer = setTimeout(() => {pending.delete(mid); reject(new Error('CDP timeout: ' + method));}, 10000);
    pending.set(mid, {resolve, reject, timer});
    ws.send(JSON.stringify({id: mid, method, params}));
  });
  const evaluate = async expression => {
    const result = await rpc('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  await rpc('Runtime.enable');
  await rpc('Page.enable');
  await rpc('Emulation.setDeviceMetricsOverride', {width: 1440, height: 960, deviceScaleFactor: 1, mobile: false});
  await until(() => evaluate('document.querySelectorAll(".dev").length === 3'), 'device list');
  await evaluate('window.BaseTerminal = Terminal; window.Terminal = class extends BaseTerminal {constructor(...args){super(...args);window.demoTerm=this;}}');
  await evaluate('document.querySelector(\'[data-key="demo-k1"]\').click()');
  const contents = `Array.from({length: demoTerm.buffer.active.length}, (_,i) =>
    demoTerm.buffer.active.getLine(i).translateToString()).join('\\n')`;
  await until(() => evaluate('!!window.demoTerm && ' + contents + '.includes("DeckLab demo")'), 'serial output');
  await evaluate('demoTerm.paste("uname -a\\r")');
  await until(() => evaluate(contents + '.includes("GNU/Linux")'), 'input POST and output SSE');
  await evaluate('document.querySelector("#b-records").click()');
  await until(() => evaluate('document.querySelector("#archive-picker").textContent.length > 0'), 'archive list');
  const logs = await (await fetch(address + 'api/logs?key=demo-k1')).json();
  assert.equal(logs.boots.length, 2);
  assert.ok(logs.boots.some(boot => boot.cross_day));
  const shot = await rpc('Page.captureScreenshot', {format: 'png'});
  fs.mkdirSync(path.join(root, 'dist'), {recursive: true});
  fs.writeFileSync(path.join(root, 'dist', 'demo-ui.png'), Buffer.from(shot.data, 'base64'));
  assert.deepEqual(errors, []);
  console.log('ok - demo browser: devices, real terminal input/SSE, cross-day archives');
})().catch(error => {console.error(error); process.exitCode = 1;}).finally(async () => {
  if (ws) ws.close();
  for (const child of [chrome, demo]) {
    if (child && child.exitCode === null) {
      child.kill('SIGTERM');
      for (let n = 0; n < 40 && child.exitCode === null; n++) await delay(100);
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
  fs.rmSync(profile, {recursive: true, force: true});
});
