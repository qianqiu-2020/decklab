'use strict';
// Real UI against the demo's production HTTP/SSE layers. Never touches boards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const {launchBrowser} = require('./browser_launch');
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
  const launched = await launchBrowser(profile);
  chrome = launched.chrome;
  const port = launched.port;
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
  await evaluate('demoTerm.paste("dmesg\\r")');
  await until(() => evaluate(contents + '.includes("mmc0: initialized")'), 'demo overview output');
  const overview = await rpc('Page.captureScreenshot', {format: 'png'});
  fs.mkdirSync(path.join(root, 'dist'), {recursive: true});
  fs.writeFileSync(path.join(root, 'dist', 'demo-overview.png'), Buffer.from(overview.data, 'base64'));
  // Two independent page instances use the production ownership API and UI.
  await evaluate('document.querySelector("#b-input-owner").click()');
  await until(() => evaluate('document.querySelector("#input-owner-label").textContent === "释放输入"'), 'first page ownership');
  await evaluate(`window.peerPage=document.createElement('iframe');
    peerPage.style.cssText='position:fixed;left:-2000px;top:0;width:1024px;height:768px';
    peerPage.src=${JSON.stringify(address)};document.body.appendChild(peerPage);`);
  await until(() => evaluate('peerPage.contentDocument.querySelectorAll(".dev").length === 3'), 'second page list');
  assert.equal(await evaluate('SerialdUI.clientId !== peerPage.contentWindow.SerialdUI.clientId'), true);
  await evaluate('peerPage.contentDocument.querySelector(\'[data-key="demo-k1"]\').click()');
  await until(() => evaluate('peerPage.contentDocument.querySelector("#d-in").disabled'), 'second page spectator');
  assert.equal(await evaluate(`peerPage.contentWindow.SerialdUI.fetch('/api/session-send', {
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:'demo-k1',sid:'serial',data:'blocked\\r',actor:'web'})
  }).then(response=>response.status)`), 423);
  await evaluate('peerPage.contentDocument.querySelector("#b-input-owner").click()');
  await until(() => evaluate('peerPage.contentDocument.querySelector("#input-owner-label").textContent === "释放输入"'), 'second page takeover');
  await until(() => evaluate('document.querySelector("#d-in").disabled'), 'first page becomes spectator');
  await evaluate('peerPage.contentDocument.querySelector("#b-input-owner").click()');
  await until(() => evaluate('document.querySelector("#input-owner-label").textContent === "占用输入"'), 'shared input restored');
  await evaluate('peerPage.remove()');
  // Use the production copy task API through the actual file-transfer dialog.
  await evaluate('document.querySelector("#b-file-transfer").click()');
  await evaluate(`document.querySelector('#ft-mode').value='copy';
    document.querySelector('#ft-mode').dispatchEvent(new Event('change'));
    document.querySelector('#ft-target-device').value='demo-k3';
    document.querySelector('#ft-source').value='/root/log.txt';
    document.querySelector('#ft-target').value='/tmp/browser-copy.log';
    document.querySelector('#ft-start').click();`);
  await until(() => evaluate('document.querySelector("#ft-status").textContent.includes("板间复制完成")'), 'collector-owned browser copy');
  const copied = (await (await fetch(address + 'api/tasks?target_serial=DEMO-001')).json()).tasks
    .find(task => task.kind === 'file-copy');
  assert.equal(copied.state, 'completed');
  assert.equal(copied.targets.length, 2);
  const expected = Buffer.from(('[    0.000000] Linux version 6.18.0-demo\n[    1.234000] mmc0: initialized\n').repeat(512));
  assert.equal(copied.result.sha256, require('node:crypto').createHash('sha256').update(expected).digest('hex'));
  await evaluate('document.querySelector("#ft-close").click()');
  // Start via the real UI, then reload the page and recover server-owned history.
  await evaluate('document.querySelector("#b-identify").click()');
  await until(() => evaluate('document.querySelector("#d-status").textContent.includes("识别完成")'), 'background identification');
  await evaluate('document.querySelector("#b-tasks").click()');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("已完成")'), 'persisted task result');
  const previousClient = await evaluate('SerialdUI.clientId');
  await rpc('Page.reload');
  await until(() => evaluate('typeof SerialdUI !== "undefined" && SerialdUI.clientId !== ' + JSON.stringify(previousClient)
    + ' && document.querySelectorAll(".dev").length === 3'), 'fresh page');
  await evaluate('document.querySelector(\'[data-key="demo-k1"]\').click(); document.querySelector("#b-tasks").click()');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("已完成")'), 'task history after reload');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("/tmp/browser-copy.log")'), 'copy history after reload');
  await evaluate('document.querySelector(\'[data-key="demo-k3"]\').click()');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("/tmp/browser-copy.log")'), 'copy visible under destination board');
  // Submit upload/download through the real dialog, never the foreground UART API.
  const uploadedBytes = Buffer.from('browser upload verified\n'.repeat(8192));
  await evaluate('document.querySelector("#b-file-transfer").click()');
  await evaluate(`const input=document.querySelector('#ft-file'); const dt=new DataTransfer();
    dt.items.add(new File([${JSON.stringify(uploadedBytes.toString())}], 'browser-upload.bin'));
    input.files=dt.files; input.dispatchEvent(new Event('change'));
    document.querySelector('#ft-target').value='/tmp/browser-upload.bin';
    document.querySelector('#ft-start').click();`);
  await until(() => evaluate('document.querySelector("#ft-status").textContent.includes("上传完成")'), 'browser staged upload');
  const uploaded=(await (await fetch(address+'api/tasks')).json()).tasks.find(task=>task.kind==='file-upload');
  assert.equal(uploaded.state,'completed');
  await evaluate(`document.querySelector('#ft-mode').value='download';
    document.querySelector('#ft-mode').dispatchEvent(new Event('change'));
    document.querySelector('#ft-source').value='/tmp/browser-upload.bin';
    document.querySelector('#ft-start').click(); document.querySelector('#ft-close').click();`);
  await until(async () => {
    const task=(await (await fetch(address+'api/tasks')).json()).tasks.find(task=>task.kind==='file-download');
    if (task && task.state==='failed') throw new Error(task.error);
    return task && task.state==='completed';
  }, 'browser background download after closing');
  await rpc('Page.reload');
  await until(() => evaluate(`!!document.querySelector('[data-key="demo-k3"]')`), 'reload downloaded board');
  await evaluate(`document.querySelector('[data-key="demo-k3"]').click()`);
  await evaluate('document.querySelector("#b-tasks").click()');
  await evaluate('document.querySelector("#task-refresh").click()');
  await until(() => evaluate('!!document.querySelector("#d-tasks a[download]")'), 'recovered download link');
  const downloaded = await evaluate(`(async () => {
    const link=document.querySelector('#d-tasks a[download]');
    const response=await fetch(link.href); const data=await response.arrayBuffer();
    const hash=await crypto.subtle.digest('SHA-256',data);
    return {size:data.byteLength,header:response.headers.get('X-Checksum-SHA256'),
      sha:Array.from(new Uint8Array(hash),value=>value.toString(16).padStart(2,'0')).join('')};
  })()`);
  assert.equal(downloaded.size, uploadedBytes.length);
  assert.equal(downloaded.sha, uploaded.result.sha256);
  assert.equal(downloaded.header, uploaded.result.sha256);
  const shot = await rpc('Page.captureScreenshot', {format: 'png'});
  fs.mkdirSync(path.join(root, 'dist'), {recursive: true});
  fs.writeFileSync(path.join(root, 'dist', 'demo-ui.png'), Buffer.from(shot.data, 'base64'));
  assert.deepEqual(errors, []);
  console.log('ok - demo browser: terminal input/SSE, archives, independent client takeover, copy/upload/download via UI, task results after reload, verified download link');
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
