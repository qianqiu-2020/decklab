'use strict';
// Real UI against the demo's production HTTP/SSE layers. Never touches boards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const {launchBrowser, stopBrowser} = require('./browser_launch');
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
  demo = spawn(process.env.PYTHON || 'python3', ['decklab_demo.py', '--port', '0'], {cwd: root});
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
  await evaluate('DeckLabI18n.setLocale("zh-CN")');
  await evaluate(`window.stationFetch = window.fetch;
    window.stationFixture = {configured:false, node_id:'demo-station', boards:[
      {serial:'DEMO-A',label:'Pico',online:true,project:'K3'},
      {serial:'DEMO-B',label:'K1',online:true,project:'K1'}]};
    window.fetch = (url,init) => url === '/api/settings' && JSON.parse(init.body).action === 'titanx-status'
      ? Promise.resolve(Response.json(stationFixture)) : stationFetch(url,init);
    DeckLabTitanX.openBoard({serial:'DEMO-A', label:'Pico'});`);
  await until(() => evaluate('document.querySelectorAll(".tx-pick-board").length === 2'), 'station board picker');
  assert.deepEqual(await evaluate('Array.from(document.querySelectorAll(".tx-pick-board [type=checkbox]"), x=>x.checked)'), [true,false]);
  await evaluate(`document.querySelector('#titanx-close').click();
    window.stationFixture = {configured:true,node_id:'demo-station',lab_id:1,lab_name:'Demo',
      worker_state:'active',platform_state:'online',first_com:1,revision:'abc',
      slots:{1:'DEMO-A',2:'DEMO-B'},registered:['DEMO-A','DEMO-B'],
      applied_slots:{1:'DEMO-A',2:'DEMO-B'},board_states:{'DEMO-A':'idle','DEMO-B':'busy'},
      connection:{lab_id:1,api_url:'https://example.test',broker:'mqtt.example.test',project:'K3'}};
    DeckLabTitanX.open();`);
  await until(() => evaluate('document.querySelectorAll("#titanx-registered li").length === 2'), 'registered station');
  for (const width of [1440,390]) {
    await rpc('Emulation.setDeviceMetricsOverride', {width,height:844,deviceScaleFactor:1,mobile:width<500});
    await evaluate('document.querySelector("#titanx-boards-tab").click()');
    assert.ok(await evaluate('document.querySelector("#titanx-board-panel").checkVisibility()'));
    assert.ok(await evaluate('document.querySelector("#titanx-registered").scrollWidth <= document.querySelector("#titanx-registered").clientWidth'));
    await evaluate('document.querySelector("#titanx-connection-tab").click()');
    assert.ok(await evaluate('document.querySelector("#titanx-maintenance").checkVisibility() && !document.querySelector("#titanx-maintain-mqtt-auth").open'));
  }
  await evaluate('document.querySelector("#titanx-close").click(); window.fetch=window.stationFetch; delete window.stationFixture; delete window.stationFetch;');
  await rpc('Emulation.setDeviceMetricsOverride', {width:1440,height:960,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector("#b-system-diagnostics").click()');
  await until(() => evaluate('document.querySelector("#diagnostics-body").textContent.includes("日志磁盘剩余")'), 'system diagnostics');
  await evaluate('document.querySelector("#diagnostics-close").click()');
  const imageImport = await fetch(address + 'api/image-download', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:address+'fixture.itb',name:'fixture.itb',request_id:'demo-image-observation'})});
  assert.equal(imageImport.status, 202);
  const imageTask = (await imageImport.json()).task;
  await until(async () => (await (await fetch(address+'api/task?id='+imageTask.id)).json()).task.state === 'failed', 'local image rejected');
  await evaluate(`window.originalImageFetch=window.fetch;
    window.fetch=(url,init)=>String(url).startsWith('/api/image-catalog')
      ? Promise.resolve(Response.json({entries:[],sources:[{id:'public',name:'Fixture'}]})) : originalImageFetch(url,init);
    document.querySelector('#b-image-store').click();`);
  assert.equal(await evaluate('document.querySelector("#is-task-history").open'), false);
  await evaluate('document.querySelector("#is-task-history summary").click()');
  await until(() => evaluate('document.querySelector("#is-task-list").textContent.includes("fixture.itb")'), 'image task history');
  await evaluate('document.querySelector("#is-task-list button").click()');
  await until(() => evaluate('document.querySelector("#diagnostics-body").textContent.includes("解析镜像服务器")'), 'durable failed image stage');
  assert.match(await evaluate('document.querySelector("#diagnostics-body").textContent'), /fixture\.itb/);
  await evaluate(`document.querySelector('#diagnostics-close').click();document.querySelector('#is-close').click();
    window.fetch=window.originalImageFetch;`);
  const bounds = selector => evaluate(`(() => { const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return {left:r.left, width:r.width, height:r.height, right:r.right}; })()`);
  const meta = await bounds('.dev .meta'), header = await bounds('.directory-columns span:nth-child(2)');
  assert.ok(Math.abs(meta.left - header.left) < 2, 'connection details line up with the column title');
  await rpc('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok((await bounds('.dev .meta')).right <= 390 && (await bounds('.dev-order-controls')).right <= 390);
  await rpc('Emulation.setDeviceMetricsOverride', {width:1440,height:960,deviceScaleFactor:1,mobile:false});
  await evaluate(`document.querySelector('.overview [data-filter="offline"]').click()`);
  assert.deepEqual(await evaluate('Array.from(document.querySelectorAll(".dev"), card => card.dataset.key)'), ['demo-offline']);
  await evaluate(`document.querySelector('#device-search').value='no match';
    document.querySelector('#device-search').dispatchEvent(new Event('input'));
    document.querySelector('#device-clear-filters').click();`);
  assert.equal(await evaluate('document.querySelectorAll(".dev").length'), 3);
  await evaluate('window.BaseTerminal = Terminal; window.Terminal = class extends BaseTerminal {constructor(...args){super(...args);window.demoTerm=this;}}');
  await evaluate(`for (const key of ['demo-k1', 'demo-k3', 'demo-k1']) {
    if (window.demoTerm) demoTerm.reset();
    document.querySelector('[data-key="' + key + '"]').click();
  }`);
  const contents = `Array.from({length: demoTerm.buffer.active.length}, (_,i) =>
    demoTerm.buffer.active.getLine(i).translateToString()).join('\\n')`;
  await until(() => evaluate('!!window.demoTerm && ' + contents + '.includes("DeckLab demo")'), 'serial output');
  await evaluate('document.querySelector("#b-device-diagnostics").click()');
  await until(() => evaluate('document.querySelector("#diagnostics-body").textContent.includes("DEMO-001")'), 'device diagnostics');
  await evaluate('document.querySelector("#diagnostics-close").click()');
  // Use Chromium key events with the real xterm focused. Switching must never send the chord to a board.
  async function chord(key, code, modifiers, repeat = false) {
    await rpc('Input.dispatchKeyEvent', {type:'keyDown',key,code,modifiers,autoRepeat:repeat});
    await rpc('Input.dispatchKeyEvent', {type:'keyUp',key,code,modifiers});
  }
  const device = () => evaluate('document.querySelector(".dev.sel")?.dataset.key');
  const order = await evaluate('Array.from(document.querySelectorAll(".dev"), card => card.dataset.key)');
  await evaluate(`window.shortcutWrites=[]; window.beforeShortcutFetch=window.fetch;
    window.fetch=function(url,...args){if(String(url).includes('/api/session-send'))shortcutWrites.push(url);return beforeShortcutFetch.call(this,url,...args)};
    demoTerm.focus();`);
  await chord('ArrowRight', 'ArrowRight', 9);
  assert.equal(await device(), order[1]);
  await chord('ArrowRight', 'ArrowRight', 9, true);
  assert.equal(await device(), order[1]);
  await chord('!', 'Digit1', 9);
  assert.equal(await device(), order[0]);
  await evaluate('document.querySelector("#b-shortcuts").click();document.querySelector("#shortcut-prev").click()');
  await chord('ArrowRight','ArrowRight',9);
  assert.match(await evaluate('document.querySelector("#shortcuts-status").textContent'), /已用于/);
  await chord('K','KeyK',10);
  assert.equal(await evaluate('document.querySelector("#shortcut-prev").textContent'), 'Ctrl + Shift + K');
  await evaluate('document.querySelector("#shortcuts-close").click();demoTerm.focus()');
  await chord('K','KeyK',10);
  assert.equal(await device(),order.at(-1));
  await evaluate('document.querySelector("#device-search").focus()');
  await chord('ArrowRight','ArrowRight',9);
  assert.equal(await device(),order.at(-1), 'editing a form must not switch boards');
  await evaluate('document.querySelector("#b-shortcuts").click();document.querySelector("#shortcuts-reset").click();document.querySelector("#shortcuts-close").click();demoTerm.focus()');
  await chord('ArrowRight','ArrowRight',9);
  assert.equal(await device(),order[0]);
  assert.equal(await evaluate('shortcutWrites.length'),0);
  await evaluate('window.fetch=window.beforeShortcutFetch');
  const workspaceWidth = (await bounds('#d-term')).width;
  await evaluate('document.querySelector("#b-device-pane").click()');
  assert.equal((await bounds('#device-pane')).width, 0);
  assert.ok((await bounds('#d-term')).width > workspaceWidth + 180);
  assert.equal(await evaluate('localStorage.getItem("decklab-pane-collapsed")'), '1');
  await rpc('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok((await bounds('#device-pane')).width > 0, 'phone switcher still works with a collapsed desktop list');
  await rpc('Emulation.setDeviceMetricsOverride', {width:1440,height:960,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector("#b-device-pane").click()');
  assert.equal((await bounds('#d-term')).width, workspaceWidth);
  await rpc('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate(`['b-send','b-agent-share','b-input-owner'].every(id => {
    const rect=document.getElementById(id).getBoundingClientRect();
    return rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth;
  })`), true);
  await rpc('Emulation.setDeviceMetricsOverride', {width:1440,height:960,deviceScaleFactor:1,mobile:false});
  await evaluate(`document.querySelector('#b-agent-share').click();
    const format=document.querySelector('#agent-share-format');
    format.value='cli'; format.dispatchEvent(new Event('change'));`);
  assert.equal(await evaluate('document.querySelector("#agent-share-scope").value'), 'device');
  await evaluate('document.querySelector("#agent-share-create").click()');
  await until(() => evaluate('!document.querySelector("#agent-share-copy").hidden && !!document.querySelector("#agent-share-grants button")'), 'issued grant');
  await evaluate('document.querySelector("#agent-share-grants button").click()');
  await until(() => evaluate('document.querySelector("#agent-share-text").hidden && !document.querySelector("#agent-share-create").disabled'), 'revoked grant cleared');
  await evaluate('document.querySelector("#agent-share-close").click()');
  await evaluate('demoTerm.paste("uname -a")');
  await until(() => evaluate(contents + '.includes("root@demo-k1:~# uname -a")'), 'echo before Enter');
  await evaluate('demoTerm.paste("\\r")');
  await until(() => evaluate(contents + '.includes("GNU/Linux")'), 'input POST and output SSE');
  await evaluate('document.querySelector("#b-records").click()');
  await until(() => evaluate('!!document.querySelector("#d-logs details[open] .boot-row")'), 'archive list');
  await evaluate('document.querySelector("#record-tab-sessions").focus()');
  await rpc('Input.dispatchKeyEvent', {type:'keyDown', key:'End', code:'End'});
  await rpc('Input.dispatchKeyEvent', {type:'keyUp', key:'End', code:'End'});
  assert.equal(await evaluate('document.activeElement.id'), 'record-tab-events');
  await evaluate('document.querySelector("#b-tasks").click()');
  assert.equal(await evaluate('document.querySelector("#history-tabs").hidden'), true);
  await evaluate('document.querySelector("#b-records-close").click()');
  assert.equal(await evaluate('document.activeElement.id'), 'b-tasks');
  await evaluate('document.querySelector("#terminal-tools-trigger").click(); document.querySelector("#b-commands").click()');
  assert.equal(await evaluate('document.querySelector("#record-panel-commands").hidden'), false);
  await evaluate('document.querySelector("#b-records").click()');
  await rpc('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate(`Array.from(document.querySelectorAll('#history-tabs button,#b-records-close')).every(b=>{
    const r=b.getBoundingClientRect(); return r.width>0 && r.left>=0 && r.right<=innerWidth && r.bottom<=innerHeight;
  })`), true);
  await rpc('Emulation.setDeviceMetricsOverride', {width:1440,height:960,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('document.querySelectorAll("#d-logs details[open]").length'), 1);
  await evaluate('document.querySelector("#d-logs details:nth-child(2) summary").click()');
  await until(() => evaluate('!!document.querySelector("#d-logs details:nth-child(2) .boot-row")'), 'earlier boot list');
  await evaluate('document.querySelector("#d-logs details:nth-child(2) .boot-row").click()');
  await until(() => evaluate('document.querySelector("#d-in").disabled && document.querySelector("#d-tabs").textContent.includes("启动 #")'), 'read-only boot tab');
  await evaluate('document.querySelector("#d-tabs button").click()');
  await until(() => evaluate('!document.querySelector("#d-in").disabled'), 'back to live serial');
  const logs = await (await fetch(address + 'api/logs?key=demo-k1')).json();
  assert.equal(logs.boots.length, 2);
  assert.ok(logs.boots.some(boot => boot.cross_day));
  await evaluate('demoTerm.paste("dmesg\\r")');
  await until(() => evaluate(contents + '.includes("mmc0: initialized")'), 'demo overview output');
  const overview = await rpc('Page.captureScreenshot', {format: 'png'});
  fs.mkdirSync(path.join(root, 'dist'), {recursive: true});
  fs.writeFileSync(path.join(root, 'dist', 'demo-overview.png'), Buffer.from(overview.data, 'base64'));
  await evaluate('window.originalDemoTerm=demoTerm;window.originalClientId=DeckLabUI.clientId;DeckLabI18n.setLocale("en")');
  await until(() => evaluate('document.querySelector("#b-input-owner").textContent.includes("Reserve input")'), 'English UI');
  assert.equal(await evaluate('demoTerm===originalDemoTerm && DeckLabUI.clientId===originalClientId'), true);
  assert.equal(await evaluate('document.querySelector("#b-agent-share").textContent.includes("AI access")'), true);
  const englishOverview = await rpc('Page.captureScreenshot', {format: 'png'});
  fs.writeFileSync(path.join(root, 'dist', 'demo-overview-en.png'), Buffer.from(englishOverview.data, 'base64'));
  await evaluate('DeckLabI18n.setLocale("zh-CN")');
  // Two independent page instances use the production ownership API and UI.
  await evaluate('document.querySelector("#b-input-owner").click()');
  await until(() => evaluate('document.querySelector("#input-owner-label").textContent === "释放输入"'), 'first page ownership');
  await evaluate(`window.peerPage=document.createElement('iframe');
    peerPage.style.cssText='position:fixed;left:-2000px;top:0;width:1024px;height:768px';
    peerPage.src=${JSON.stringify(address)};document.body.appendChild(peerPage);`);
  await until(() => evaluate('peerPage.contentDocument.querySelectorAll(".dev").length === 3'), 'second page list');
  assert.equal(await evaluate('DeckLabUI.clientId !== peerPage.contentWindow.DeckLabUI.clientId'), true);
  await evaluate('peerPage.contentDocument.querySelector(\'[data-key="demo-k1"]\').click()');
  await until(() => evaluate('peerPage.contentDocument.querySelector("#d-in").disabled'), 'second page spectator');
  assert.equal(await evaluate(`peerPage.contentWindow.DeckLabUI.fetch('/api/session-send', {
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
  await evaluate('Array.from(document.querySelector("#d-tasks").firstElementChild.querySelectorAll("button")).find(b => b.textContent === "任务详情").click()');
  await until(() => evaluate('document.querySelector("#diagnostics-body").textContent.includes("探测结束")'), 'persisted task stages');
  await evaluate('document.querySelector("#diagnostics-close").click()');
  const previousClient = await evaluate('DeckLabUI.clientId');
  await rpc('Page.reload');
  await until(() => evaluate('typeof DeckLabUI !== "undefined" && DeckLabUI.clientId !== ' + JSON.stringify(previousClient)
    + ' && document.querySelectorAll(".dev").length === 3'), 'fresh page');
  await evaluate('document.querySelector(\'[data-key="demo-k1"]\').click(); document.querySelector("#b-tasks").click()');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("已完成")'), 'task history after reload');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("/tmp/browser-copy.log")'), 'copy history after reload');
  await evaluate('document.querySelector(\'[data-key="demo-k3"]\').click()');
  await until(() => evaluate('document.querySelector("#d-tasks").textContent.includes("/tmp/browser-copy.log")'), 'copy visible under destination board');
  await evaluate('document.querySelector("#b-records-close").click()');
  let requestedSizeSync = false;
  // Wait for the idle prompt and confirm terminal setup before sending UART
  // work. Use the UI's size sync if switching screens cancelled automatic setup.
  await until(async () => {
    const [operation, packet] = await Promise.all([
      fetch(address + 'api/operation?key=demo-k3').then(response => response.json()),
      fetch(address + 'api/session?key=demo-k3&sid=serial&off=0&screen=1').then(response => response.json())
    ]);
    const state = packet.terminal_state || {};
    const ready = packet.key === 'demo-k3' && !operation.operation && state.serial_ready && !state.internal_output;
    if (ready && !state.serial_configured && !requestedSizeSync) {
      requestedSizeSync = await evaluate(`(() => {
        const control = document.querySelector('#b-serial-size');
        if (control.disabled || control.hidden) return false;
        const menu = document.querySelector('#terminal-view-menu');
        if (!menu.matches(':popover-open')) document.querySelector('#terminal-tools-trigger').click();
        if (!control.getClientRects().length) return false;
        control.click();
        if (menu.matches(':popover-open')) menu.hidePopover();
        return true;
      })()`);
    }
    return ready && state.serial_configured;
  }, 'destination serial setup and idle console');
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
  await stopBrowser(chrome);
  for (const child of [demo]) {
    if (child && child.exitCode === null) {
      child.kill('SIGTERM');
      for (let n = 0; n < 40 && child.exitCode === null; n++) await delay(100);
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }
  await fs.promises.rm(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
});
