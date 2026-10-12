'use strict';
// Real bundled xterm + Chromium, isolated HTTP fixtures. No seriald or board access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {launchBrowser} = require('./browser_launch');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'seriald-browser-'));
const posts = [], errors = [];
const fixtures = [
  { key: 'musepi2', serial: '5b9d4a0a6f5e25a8', state: 'online', alive: true, dev: '/dev/ttyUSB0', hostname: 'bianbu-k3' },
  { key: 'riscv-118', serial: '26abd51e5bc2dbe1', state: 'offline', alive: false, last_dev: '/dev/ttyUSB4' },
  { key: 'milkv2', serial: '0059D968E583C49A', state: 'online', alive: true, dev: '/dev/ttyUSB2', hostname: 'bianbu' },
  { key: 'milkv', serial: 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6', state: 'online', alive: true, dev: '/dev/ttyUSB1', hostname: 'k1' },
  { key: 'unprovisioned-demo', serial: '', state: 'pending', alive: true, dev: '/dev/ttyUSB3', by_path: 'pci-usb-0:4.2.3' },
].map(d => ({ ...d, label: d.key, connection_id: 'test-' + d.key,
  bytes: 8192, probes: 0, last_seen: Date.now() / 1000,
  info: ['milkv2', 'milkv'].includes(d.key) ? { serial: d.serial, reported_serial: '0123456789ABCDEF',
    serial_source: d.key === 'milkv' ? 'boot-marker' : 'soc-sysfs', identity_status: 'provisional' } : {} }));
const log = '<script>globalThis.injected = true</script>\n'
  + '2026-09-19 command: uname -a\nLinux bianbu 6.6.63 riscv64\n'
  + '2026-09-19 10:00:00  root@bianbu:~# uname -a\nLinux riscv64\n'
  + '2026-09-19 10:01:00  root@bianbu:~# dmesg | tail -1\nready\n';
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://localhost');
  res.setHeader('Cache-Control', 'no-store');
  const json = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  if (req.method === 'POST') { posts.push(u.pathname); json({ ok: false, error: 'read-only fixture' }); return; }
  if (u.pathname === '/api/devices') return json({ devices: fixtures });
  if (u.pathname === '/api/session') {
    const bytes = u.searchParams.get('off') === '0' ? Buffer.from(
      'root@bianbu:~# \x1b[6n\x1b[?6n\x1b[c\x1b]11;?\x07'.repeat(15)
      + Array.from({length:80},(_,i)=>'\r\nserial line '+i).join('')
      + '\x1b[2J\x1b[50;1Hlast firmware row') : Buffer.alloc(0);
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
    { name: 'ssh-ssh456def-20260919.log', size: log.length },
    { name: 'serial-20260921.log', kind: 'serial', date: '2026-09-21', size: 8192 },
    { name: 'serial-20260920.log', kind: 'serial', date: '2026-09-20', size: 4096 }],
    boots: [
      { id: 'boot-three', number: 3, started: '2026-09-21 08:00:00', marker: 'U-Boot SPL',
        dates: ['2026-09-21'], size: 1024 },
      { id: 'boot-two', number: 2, started: '2026-09-20 23:59:00', marker: 'Boot ROM',
        dates: ['2026-09-20', '2026-09-21'], size: 6144, cross_day: true },
      { id: 'boot-one', number: 1, started: '2026-09-20 10:00:00', marker: 'U-Boot',
        dates: ['2026-09-20'], size: 1024 }] });
  if (u.pathname === '/api/boot-compare') return json({
    counts: { added: 1, removed: 0, changed: 0 }, clues: [{ line: 2, text: 'mmc timeout' }],
    rows: [{ type: 'equal', left_no: 1, left: 'U-Boot', right_no: 1, right: 'U-Boot' },
      { type: 'insert', left_no: null, left: '', right_no: 2, right: 'mmc timeout' }], truncated: false });
  if (u.pathname === '/api/log') return json({ data: log, off: log.length, start_off: 0, size: log.length, more: false });
  if (u.pathname === '/api/access') return json({ share_enabled: false });
  const route = u.pathname === '/' ? '/index.html' : u.pathname;
  if (!['/index.html', '/static/app.js', '/static/file-transfer.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js', '/static/terminal-policy.js', '/static/terminal-navigation.js', '/static/terminal-view.js', '/static/archives.js', '/static/devices.js', '/static/tasks.js', '/static/app.css', '/static/icons.svg', '/static/decklab.svg', '/static/decklab.png', '/vendor/xterm.js', '/vendor/addon-fit.js', '/vendor/xterm.css'].includes(route)) { res.statusCode=404; res.end(); return; }
  res.setHeader('Content-Type', route.endsWith('.js') ? 'application/javascript' : route.endsWith('.css') ? 'text/css' : route.endsWith('.svg') ? 'image/svg+xml' : route.endsWith('.png') ? 'image/png' : 'text/html');
  res.end(fs.readFileSync(path.join(root, route)));
});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let chrome, ws;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = 'http://127.0.0.1:' + server.address().port;
  const launched = await launchBrowser(profile);
  chrome = launched.chrome;
  const port = launched.port;
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
  const out=path.join(root,'dist');fs.mkdirSync(out,{recursive:true});
  const overviewShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-overview.png'),Buffer.from(overviewShot.data,'base64'));
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
  assert.equal(await evaluate('getComputedStyle(document.querySelector("#d-info")).display'),'none',
    'identity details should start collapsed to leave room for the terminal');
  await evaluate('document.querySelector("#b-file-transfer").click()');
  assert.equal(await evaluate('document.querySelector("#file-transfer-dialog").open'), true);
  assert.equal(await evaluate('document.querySelector("#file-transfer-dialog").matches(":modal")'), false,
    'transfer must leave the rest of the workbench interactive');
  assert.ok(await evaluate('document.querySelectorAll("#ft-target-device option").length >= 2'));
  await evaluate('document.querySelector("#ft-mode").value="copy"; document.querySelector("#ft-mode").dispatchEvent(new Event("change"))');
  assert.equal(await evaluate('document.querySelector("#ft-source-field").hidden'), false);
  assert.equal(await evaluate('document.querySelector("#ft-local-field").hidden'), true);
  const transferShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-file-transfer.png'),Buffer.from(transferShot.data,'base64'));
  const beforeDrag=await evaluate(`(() => { const r=document.querySelector('#file-transfer-dialog').getBoundingClientRect();
    return {left:r.left,top:r.top,x:r.left+60,y:r.top+22}; })()`);
  await rpc('Input.dispatchMouseEvent',{type:'mouseMoved',x:beforeDrag.x,y:beforeDrag.y});
  await rpc('Input.dispatchMouseEvent',{type:'mousePressed',x:beforeDrag.x,y:beforeDrag.y,button:'left',clickCount:1});
  await rpc('Input.dispatchMouseEvent',{type:'mouseMoved',x:beforeDrag.x-50,y:beforeDrag.y+30,button:'left',buttons:1});
  await rpc('Input.dispatchMouseEvent',{type:'mouseMoved',x:beforeDrag.x-110,y:beforeDrag.y+65,button:'left',buttons:1});
  await rpc('Input.dispatchMouseEvent',{type:'mouseReleased',x:beforeDrag.x-110,y:beforeDrag.y+65,button:'left',clickCount:1});
  const afterDrag=await evaluate(`(() => { const r=document.querySelector('#file-transfer-dialog').getBoundingClientRect();
    return {left:r.left,top:r.top}; })()`);
  assert.ok(afterDrag.left < beforeDrag.left-50 && afterDrag.top > beforeDrag.top+25,
    'transfer dialog header should drag the floating panel');
  await evaluate('document.querySelector("#ft-close").click()');
  assert.deepEqual(posts,[], 'opening transfer controls must not write to a board');
  await evaluate('document.querySelector("#b-boot-compare").click()');
  for(let n=0;n<30 && !await evaluate('document.querySelectorAll("#bc-left-boot option").length > 1');n++)await delay(100);
  assert.equal(await evaluate('document.querySelector("#bc-left-boot").value'),'boot-two');
  assert.equal(await evaluate('document.querySelector("#bc-right-boot").value'),'boot-three');
  await evaluate('document.querySelector("#bc-run").click()');
  for(let n=0;n<30 && !await evaluate('document.querySelectorAll("#bc-diff .bc-row").length === 2');n++)await delay(100);
  assert.ok(await evaluate('document.querySelector("#bc-clues").textContent.includes("mmc timeout")'));
  const compareShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-boot-compare.png'),Buffer.from(compareShot.data,'base64'));
  await evaluate('document.querySelector("#bc-close").click()');
  await evaluate('document.querySelector("#b-info").click()');
  assert.notEqual(await evaluate('getComputedStyle(document.querySelector("#d-info")).display'),'none');
  await evaluate('document.querySelector("#b-info").click()');
  await evaluate('document.querySelector("#d-activity button").click()');
  await delay(700);
  assert.deepEqual(posts,[], 'viewing an agent session must not send reports or resize it');
  assert.equal(await evaluate('testTerm.options.fontSize'), 14.5, 'SSH uses the readable default font size');
  assert.ok(await evaluate('testTerm.cols > 80'), 'terminal fits the detail panel');
  const columnsFit = `(() => {
    const host=document.querySelector('#d-term');
    const screen=host.querySelector('.xterm-screen').getBoundingClientRect();
    const viewport=host.querySelector('.xterm-viewport');
    const cell=testTerm._core._renderService.dimensions.css.cell.width;
    const drawn=testTerm.cols*cell;
    return drawn <= screen.width+1 && screen.width <= viewport.clientWidth+1 &&
      viewport.clientWidth-drawn < cell+1;
  })()`;
  assert.ok(await evaluate(columnsFit), 'SSH columns must fit inside the visible screen');
  await evaluate('document.querySelector("#b-records").click()');
  await delay(200);
  assert.ok(await evaluate(columnsFit), 'SSH columns must still fit with records open');
  const recordsShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-records.png'),Buffer.from(recordsShot.data,'base64'));
  await evaluate('new Promise(resolve => testTerm.write("\\r\\nroot@bianbu:~# uname -a\\r\\nLinux riscv64\\r\\n", resolve))');
  await evaluate('document.querySelector("#b-command-nav").click()');
  assert.ok(await evaluate('document.querySelectorAll("#d-command-nav .command-nav-item").length >= 1'));
  await evaluate('Array.from(document.querySelectorAll("#d-command-nav .command-nav-item")).at(-1).click()');
  assert.ok(await evaluate('testTerm.getSelection().includes("uname -a")'));
  const inputNavShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-input-nav.png'),Buffer.from(inputNavShot.data,'base64'));
  await evaluate('document.querySelector("#record-tab-logs").click()');
  assert.ok(await evaluate('document.querySelectorAll("#d-logs .boot-row").length >= 2'),
    'date view lists the boots touching the selected day');
  await evaluate('document.querySelectorAll("#d-logs .boot-row")[1].querySelector("button").click()');
  assert.equal(await evaluate('document.querySelector("#archive-mode-boot").getAttribute("aria-pressed")'), 'true');
  assert.equal(await evaluate('document.querySelectorAll("#d-logs .archive-date-links button").length'), 2,
    'cross-day boot links back to both daily archives');
  await evaluate('document.querySelector("#d-logs .archive-date-links button").click()');
  assert.equal(await evaluate('document.querySelector("#archive-mode-date").getAttribute("aria-pressed")'), 'true');
  await evaluate('document.querySelector("#b-records").click()');
  await delay(100);
  assert.ok(await evaluate(`new Promise(resolve => {
    testTerm.reset();
    testTerm.write('x'.repeat(testTerm.cols - 1) + 'YZ', () => {
      const first=testTerm.buffer.active.getLine(0)?.translateToString(true) || '';
      const second=testTerm.buffer.active.getLine(1)?.translateToString(true) || '';
      resolve(first.endsWith('Y') && second.startsWith('Z'));
    });
  })`), 'SSH wraps immediately after its last visible column');
  assert.equal(await evaluate('document.querySelector("#b-send").disabled'), true);
  await evaluate('document.querySelector("#d-logs button").click()');
  await delay(500);
  assert.ok(await evaluate('testTerm.markers.length >= 2'),
    'archived shell commands get default xterm line decorations');
  assert.ok(await evaluate('Array.from({length:testTerm.buffer.active.length},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString(true)||"").join("\\n").includes("Linux bianbu 6.6.63 riscv64")'));
  assert.ok(await evaluate('Array.from({length:testTerm.buffer.active.length},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString(true)||"").some(line=>line.startsWith("Linux bianbu 6.6.63 riscv64"))'), 'archived LF must return to column zero');
  assert.ok(await evaluate('document.querySelector("#d-tabs").textContent.includes("串口日志")'));
  assert.equal(await evaluate('document.querySelector("#b-send").disabled'),true);
  assert.equal(await evaluate('testTerm.options.convertEol'),true);
  assert.equal(await evaluate('testTerm.options.fontSize'), 14.5, 'archive uses the readable default font size');
  assert.equal(await evaluate('globalThis.injected === undefined'),true);
  assert.deepEqual(posts,[], 'viewing an archived terminal must remain read-only');
  const viewportFits = `(() => {
    const host = document.querySelector('#d-term').getBoundingClientRect();
    const viewport = document.querySelector('#d-term .xterm-viewport').getBoundingClientRect();
    const controls = document.querySelector('.sendrow').getBoundingClientRect();
    return document.scrollingElement.scrollHeight <= window.innerHeight + 1 &&
      viewport.bottom <= host.bottom - 8 && host.bottom <= controls.top + 1 &&
      host.height >= 200;
  })()`;
  assert.ok(await evaluate(viewportFits), 'desktop page and terminal must fit the viewport with bottom clearance');


  const screenshot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-desktop.png'),Buffer.from(screenshot.data,'base64'));
  const transferMetrics=await evaluate(`(async () => {
    const original=window.fetch.bind(window), values=[];
    let phase='upload', taskPolls=0, starts=0;
    const output=document.querySelector('#ft-status');
    const observer=new MutationObserver(()=>values.push(output.textContent));
    observer.observe(output,{childList:true});
    const polls={upload:0,copy:0};
    window.fetch=async (url,options) => {
      if (String(url)==='/api/task-start') {
        starts++;
        return {ok:true,json:async()=>({task:{id:'browser-'+phase,state:'running',progress:{done:0,total:4096}}})};
      }
      if (String(url).startsWith('/api/task?')) {
        taskPolls++; polls[phase]++;
        const task=polls[phase]===1 ? {id:'browser-'+phase,state:'running',progress:{done:2048,total:4096}}
          : {id:'browser-'+phase,state:'completed',progress:{done:4096,total:4096},result:{ok:true,size:4096,
              path:'/tmp/target.bin',sha256:'mock-sha',rate_diagnostics:{effective_kib_s:8,source_kib_s:13,destination_kib_s:8}}};
        return {ok:true,json:async()=>({task})};
      }
      if (String(url).startsWith('/api/artifact-')) {
        const action=String(url).split('-').pop();
        return {ok:true,json:async()=>({artifact: action==='create' ? {id:'stage'}
          : action==='append' ? {received:4096} : {size:4096,sha256:'a'.repeat(64)}})};
      }
      if (String(url).startsWith('/api/file-transfer/')) throw new Error('foreground UART API called');
      return original(url,options);
    };
    try {
      document.querySelector('#b-file-transfer').click();
      const selected=new DataTransfer(); selected.items.add(new File([new Uint8Array(4096)],'demo.bin'));
      document.querySelector('#ft-file').files=selected.files;
      document.querySelector('#ft-target').value='/tmp/demo.bin';
      document.querySelector('#ft-start').click();
      for(let n=0;n<40 && !output.textContent.includes('上传完成');n++)
        await new Promise(resolve=>setTimeout(resolve,50));
      const answer={values,final:output.textContent};
      phase='copy';
      document.querySelector('#ft-mode').value='copy';
      document.querySelector('#ft-mode').dispatchEvent(new Event('change'));
      document.querySelector('#ft-source').value='/tmp/source.bin';
      document.querySelector('#ft-target').value='/tmp/target.bin';
      document.querySelector('#ft-start').click();
      for(let n=0;n<40 && !output.textContent.includes('板间复制完成');n++)
        await new Promise(resolve=>setTimeout(resolve,50));
      answer.copyFinal=output.textContent; answer.starts=starts; answer.taskPolls=taskPolls;
      document.querySelector('#ft-close').click();
      return answer;
    } finally {window.fetch=original;observer.disconnect();}
  })()`);
  assert.ok(transferMetrics.values.some(value=>value.includes('/s') && value.includes('预计剩余')),
    'active transfer should show estimated rate and remaining time');
  assert.ok(transferMetrics.final.includes('平均') && transferMetrics.final.includes('/s'));
  assert.equal(transferMetrics.starts, 2, 'upload and copy must each start exactly one collector task');
  assert.ok(transferMetrics.taskPolls >= 2, 'browser must follow collector progress');
  assert.ok(transferMetrics.copyFinal.includes('板间复制完成'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:1366,height:768,deviceScaleFactor:1,mobile:false});
  await delay(150);
  assert.ok(await evaluate(viewportFits), 'laptop-height terminal must keep the last row visible');
  assert.ok(await evaluate(columnsFit), 'laptop-width terminal must wrap before clipping');
  assert.ok(await evaluate('document.querySelector("#d-term").getBoundingClientRect().height >= 480'),
    'laptop-height view must give most of the screen to the terminal');
  const laptopShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-laptop.png'),Buffer.from(laptopShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:850,height:800,deviceScaleFactor:1,mobile:false});
  await delay(100);
  assert.ok(await evaluate(viewportFits), 'tablet-height terminal must fit without page scrolling');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1366,height:768,deviceScaleFactor:1,mobile:false});
  await rpc('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'}]});
  await delay(100);
  const darkShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-dark.png'),Buffer.from(darkShot.data,'base64'));
  await rpc('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'light'}]});
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(300);
  assert.ok(await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'),'mobile layout must not overflow');
  assert.ok(await evaluate(viewportFits), 'mobile page and terminal must fit the viewport with bottom clearance');
  assert.ok(await evaluate(columnsFit), 'mobile terminal must wrap before clipping');
  await evaluate('document.querySelector("#d-tabs button").click()');
  await delay(200);
  assert.ok(await evaluate('testTerm.rows >= 50'), 'firmware must retain its full logical screen');
  const firmwareFit = await evaluate(`(() => {const host=document.querySelector('#d-term');const screen=host.querySelector('.xterm-screen');return {screen:screen.getBoundingClientRect().height,viewport:host.querySelector('.xterm-viewport').clientHeight,fontSize:testTerm.options.fontSize,rows:testTerm.rows}})()`);
  assert.ok(firmwareFit.screen <= firmwareFit.viewport + 1,
    'firmware rows should fit the viewport: ' + JSON.stringify(firmwareFit));
  assert.ok(await evaluate(`(() => {const v=document.querySelector('#d-term .xterm-viewport');return v.scrollHeight>v.clientHeight})()`),
    'prior serial output should remain scrollable');
  assert.ok(await evaluate(`(() => {const r=document.querySelector('#d-term .xterm-viewport').getBoundingClientRect();return document.elementFromPoint(r.right-5,(r.top+r.bottom)/2)?.classList.contains('xterm-viewport')})()`),
    'serial scrollbar edge must receive clicks even at narrow widths');
  const mobileScroll = await evaluate(`(() => {const v=document.querySelector('#d-term .xterm-viewport');v.scrollTop=0;const r=v.getBoundingClientRect();return {x:r.left+20,y:r.top+20}})()`);
  await rpc('Input.dispatchMouseEvent',{type:'mouseWheel',x:mobileScroll.x,y:mobileScroll.y,deltaX:0,deltaY:180});
  await delay(100);
  assert.ok(await evaluate('document.querySelector("#d-term .xterm-viewport").scrollTop > 0'),
    'mobile serial history should remain scrollable');
  const regularHeight = await evaluate('document.querySelector("#d-term").getBoundingClientRect().height');
  await evaluate('document.querySelector("#b-expand").click()');
  await delay(200);
  assert.ok(await evaluate('document.querySelector("#d-term").getBoundingClientRect().height') > regularHeight + 100,
    'maximized firmware terminal must reclaim space from board chrome');
  assert.ok(await evaluate('document.scrollingElement.scrollHeight <= window.innerHeight + 1'));
  const expandedShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-max.png'),Buffer.from(expandedShot.data,'base64'));
  await evaluate('document.querySelector("#b-expand").click()');
  await delay(100);
  await evaluate('document.querySelector("#b-records").click()');
  await delay(200);
  assert.ok(await evaluate('document.scrollingElement.scrollHeight <= window.innerHeight + 1'),
    'opening records must not make the page scroll');
  assert.ok(await evaluate('document.querySelector("#d-term").getBoundingClientRect().height >= 120'),
    'terminal must remain usable with records open');
  const mobile=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-mobile.png'),Buffer.from(mobile.data,'base64'));
  // Native mouse scrollbar tracks belong to desktop mode. Mobile Chromium
  // uses an overlay scroll indicator rather than a clickable desktop track.
  await rpc('Emulation.setDeviceMetricsOverride',{width:1366,height:768,deviceScaleFactor:1,mobile:false});
  await delay(300);
  // Reset through xterm so its logical position and the DOM viewport agree.
  // A direct scrollTop assignment races xterm's queued viewport synchronization
  // after the mobile/desktop resize and can be undone during the native click.
  await evaluate('new Promise(resolve => { testTerm.scrollToTop(); requestAnimationFrame(() => requestAnimationFrame(resolve)); })');
  const track = await evaluate(`(() => {const v=document.querySelector('#d-term .xterm-viewport');const r=v.getBoundingClientRect();return {x:r.right-5,y:r.bottom-12,top:v.scrollTop,height:v.clientHeight,scrollHeight:v.scrollHeight,width:v.offsetWidth,clientWidth:v.clientWidth,hit:document.elementFromPoint(r.right-5,r.bottom-12)?.className}})()`);
  assert.equal(track.top, 0, 'desktop scrollbar fixture must start at the top');
  assert.ok(track.scrollHeight > track.height, 'desktop scrollbar fixture must have scrollable history');
  assert.ok(track.width > track.clientWidth, 'desktop fixture must expose a native scrollbar track');
  assert.equal(track.hit, 'xterm-viewport', 'desktop scrollbar track must receive mouse input');
  await rpc('Input.dispatchMouseEvent',{type:'mousePressed',x:track.x,y:track.y,button:'left',clickCount:1});
  await rpc('Input.dispatchMouseEvent',{type:'mouseReleased',x:track.x,y:track.y,button:'left',clickCount:1});
  // Native scroll application is asynchronous; require observable movement,
  // with a bounded wait rather than assuming a particular frame duration.
  for (let n = 0; n < 20 && !await evaluate('document.querySelector("#d-term .xterm-viewport").scrollTop > 0'); n++) await delay(50);
  assert.ok(await evaluate('document.querySelector("#d-term .xterm-viewport").scrollTop > 0'),
    'clicking the visible serial scrollbar must move through history: ' + JSON.stringify(track));
  assert.deepEqual(errors,[]);
  console.log('Browser smoke passed: real xterm query suppression, read-only agent/archive tabs, desktop/mobile layout.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(ws)ws.close(); if(chrome){chrome.kill('SIGTERM');await delay(500);}server.close();
  fs.rmSync(profile,{recursive:true,force:true});
});
