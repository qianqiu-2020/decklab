'use strict';
// Real bundled xterm + Chromium, isolated HTTP fixtures. No seriald or board access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {launchBrowser, stopBrowser} = require('./browser_launch');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'seriald-browser-'));
const posts = [], errors = [];
const probeSettingsPosts = [], grantPosts = [], sshConnectPosts = [];
// This private fixture store survives page reloads. Only username/status metadata
// goes through /api/devices; passwords never appear in a fixture API response.
const savedSshCredentials = new Map(), sshAuthentications = [], fixtureSshSessions = [];
// Flash fixtures exercise the real UI only. No executor or physical board runs.
const flashPlans = [], flashSubmissions = [], flashRecords = [], flashRecoveryPosts = [];
const flashTargetReads = [];
const hardwarePosts = [];
const flashDeletedArtifacts = new Set(), flashDeletePosts = [];
const flashUsbTargets = [2, 3].map(port => ({target_id:'fixture-usb-' + port,
  target_generation:'fixture-generation-' + port, label:'模拟 USB 下载设备 ' + port,
  serial:'', mode:'loader', usb:{port_path:[1, port]}}));
const flashArtifact = {id:'aaaaaaaaaaaaaaaaaaaaaaaa', name:'模拟镜像 · fixture.img', size:1048576, sha256:'a'.repeat(64)};
const flashStorages = ['emmc', 'nvme'].map(id => ({id, name:id.toUpperCase(), size:8589934592,
  partitions:[{id:'rootfs', name:'rootfs', size:4294967296}]}));
const norFixturePlan = {id:'nor-final-fixture',ready:true,confirm_after_prepare:true,write_layout:true,
  writes:[{storage:'active',partition:'layout',kind:'layout',target:'mtd',size:967},
    {storage:'active',partition:'fsbl',kind:'partition',offset:131072,size:465504}],
  erase_ranges:[{storage:'active',offset:0,size:8388608}],
  warnings:['浏览器模拟：NOR 固件恢复会擦除 env；包内 EC 升级不执行。']};
let probeSettings = {mode: 'linux', command: '', default_command: "printf 'HOST=%s\\n' \"$(hostname)\""};
let serialPayload = null;
const serialLiveChunks = [];
const fixtures = [
  { key: 'musepi2', serial: '5b9d4a0a6f5e25a8', state: 'online', alive: true, dev: '/dev/ttyUSB0', hostname: 'bianbu-k3' },
  { key: 'riscv-118', serial: '26abd51e5bc2dbe1', state: 'offline', alive: false, last_dev: '/dev/ttyUSB4' },
  { key: 'milkv2', serial: '0059D968E583C49A', state: 'online', alive: true, dev: '/dev/ttyUSB2', hostname: 'bianbu' },
  { key: 'milkv', serial: 'boot-0e9badc5-706a-4421-93a7-50e76cc240a6', state: 'online', alive: true, dev: '/dev/ttyUSB1', hostname: 'k1' },
  { key: 'unprovisioned-demo', serial: '', state: 'pending', alive: true, dev: '/dev/ttyUSB3', by_path: 'pci-usb-0:4.2.3' },
].map(d => ({ ...d, label: d.key, connection_id: 'test-' + d.key,
  ssh_settings: {saved: false, user: 'root'},
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
  if (req.method === 'POST') {
    posts.push(u.pathname);
    if (u.pathname === '/api/flash-target') {
      let body = ''; req.on('data', chunk => {body += chunk;});
      req.on('end', () => {
        const payload = JSON.parse(body); flashTargetReads.push(payload);
        const target = flashUsbTargets.find(item => item.target_id === payload.target_id
          && item.target_generation === payload.target_generation);
        if (!target) {res.statusCode=409; return json({error:'simulated target changed'});}
        const preparable = target.mode === 'bootrom';
        // The real engine supports BootROM while still requiring preparation.
        json({target,capabilities:{supported:true,can_prepare_from_image:preparable,
          can_upload:true,target_id:target.target_id,
          target_generation:target.target_generation,binding_revision:1,
          profile:'自动识别 · 模拟 K3 下载环境',console:null,
          executor:{provider:'goflash',node:'local',endpoint:target.target_id},
          artifacts:[flashArtifact].filter(item=>!flashDeletedArtifacts.has(item.id)),storages:preparable?[]:flashStorages,
          modes:preparable&&!target.nor?['partitions']:['full','partitions']}});
      }); return;
    }
    if (u.pathname === '/api/artifact-delete') {
      let body='';req.on('data',chunk=>{body+=chunk;});
      req.on('end',()=>{const payload=JSON.parse(body);flashDeletePosts.push(payload);
        if(payload.id!==flashArtifact.id){res.statusCode=404;return json({error:'fixture artifact missing'});}
        flashDeletedArtifacts.add(payload.id);json({ok:true,artifact:{...flashArtifact,state:'deleted'}});
      });return;
    }
    if (u.pathname === '/api/flash-selection') {
      let body = ''; req.on('data', chunk => {body += chunk;});
      req.on('end', () => {
        const payload = JSON.parse(body), current = flashRecords.find(item => item.id === payload.id);
        if (!current || payload.target_generation !== current.flash_targets[0].generation) {
          res.statusCode=409; return json({error:'simulated task generation changed'});
        }
        if (current.flash.confirmation_mode === 'full') {
          if (payload.preview) return json({plan:norFixturePlan});
          current.progress.phase='writing';current.flash.write_result='writing';return json({task:current});
        }
        json({plan:{id:'same-task-selection-plan' ,write_layout:false,writes:payload.selections.map(item=>({
          storage:item.storage,partition:item.partition,size:1048576,sha256:flashArtifact.sha256}))}});
      }); return;
    }
    if (u.pathname === '/api/flash-recovery') {
      let body = ''; req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body); flashRecoveryPosts.push(payload);
        const task = flashRecords.find(item => item.id === payload.id);
        if (!task || payload.attempt_id !== task.flash.attempt_id || !payload.reason || payload.confirm_stopped !== true) {
          res.statusCode = 409; return json({ok:false,error:'invalid simulated recovery receipt'});
        }
        task.flash.recovery_required = false;
        json({ok:true,task});
      }); return;
    }
    if (u.pathname === '/api/flash-plan') {
      let body = ''; req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body); flashPlans.push(payload);
        const target = flashUsbTargets.find(item => item.target_id === payload.target_id);
        if (target && target.nor && payload.mode === 'full') return json({requires_prepare:true,plan:{...norFixturePlan,ready:false,erase_ranges:[]}});
        if (target && target.mode === 'bootrom') return json({ok:true,requires_prepare:true,
          plan:{id:'brom-preparation-plan',binding_revision:1,ready:false,write_layout:false,writes:[],digest:'d'.repeat(64)}});
        const writes = payload.mode === 'partitions'
          ? payload.selections.map(item => ({storage:item.storage, partition:item.partition, size:1048576, sha256:'a'.repeat(64)}))
          : flashStorages.map(item => ({storage:item.id, partition:'rootfs', size:1048576, sha256:'a'.repeat(64)}));
        json({ok:true, plan:{id:'bbbbbbbbbbbbbbbbbbbbbbbb', digest:'b'.repeat(64), binding_revision:7,
          writes, write_layout:payload.mode !== 'partitions', warnings:['浏览器测试夹具：模拟计划，不接触硬件。']}});
      }); return;
    }
    if (u.pathname === '/api/probe-settings' || u.pathname === '/api/agent-share' || u.pathname === '/api/task-start') {
      let body = ''; req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        if (u.pathname === '/api/probe-settings') {
          probeSettingsPosts.push(payload); probeSettings = {...probeSettings, ...payload}; return json({ok:true, ...probeSettings});
        }
        if (u.pathname === '/api/task-start') {
          if (payload.kind === 'hardware-reset') {
            hardwarePosts.push(payload);
            return json({ok:true, task:{id:'hardware-fixture-' + hardwarePosts.length,
              state:'completed',result:{ok:true,control_result:'acknowledged'}}});
          }
          if (payload.kind === 'flash') {
            flashSubmissions.push(payload);
            let task = flashRecords.find(item => item.flash.request_id === payload.request_id);
            if (!task) {
              const target = flashUsbTargets.find(item => item.target_id === payload.target_id);
              const preparing = target && target.mode === 'bootrom';
              task = {id:'cccccccccccccccccccccccc', kind:'flash', state:'running', actor:'web', created:Date.now()/1000,
                targets:[], board_targets:payload.serial?[{serial:payload.serial}]:[],
                flash_targets:target?[{target_id:target.target_id,generation:target.target_generation}]:[],
                progress:preparing?{done:0,total:0,phase:'awaiting-selection'}:{done:524288,total:1048576,phase:'writing'},
                flash:{request_id:payload.request_id,write_result:'writing',verify_result:'pending',boot_result:'not-requested'}};
              if (preparing) Object.assign(task.flash,{target_id:target.target_id,
                target_generation:target.target_generation,write_result:'not-started',
                inspection:{layout_digest:'post-loader-layout',storages:flashStorages},payload_candidates:[flashArtifact]});
              if (target && target.nor) Object.assign(task.flash,{confirmation_mode:'full',prepared_plan:norFixturePlan});
              flashRecords.push(task);
            }
            return json({ok:true,task});
          }
          if (payload.kind !== 'ssh-connect') return json({ok:false, error:'unsupported fixture task'});
          sshConnectPosts.push(payload);
          const board = fixtures.find(device => device.key === payload.key
            && device.connection_id === payload.connection_id);
          const explicit = Object.hasOwn(payload, 'ssh_user') || Object.hasOwn(payload, 'ssh_password');
          const credentials = explicit ? {user: payload.ssh_user, password: payload.ssh_password}
            : board && savedSshCredentials.get(board.serial);
          sshAuthentications.push({key: payload.key, explicit, ...credentials});
          if (board && board.serial && credentials && credentials.user === 'ubuntu'
              && credentials.password === 'simulated-saved-password-only') {
            savedSshCredentials.set(board.serial, credentials);
            board.ssh_settings = {saved: true, user: credentials.user};
            const sid = 'ssh-fixture-session-' + sshConnectPosts.length;
            fixtureSshSessions.push({sid, kind: 'ssh', actor: 'web', ip: '192.0.2.42'});
            return json({ok:true, task:{id:'ssh-fixture-' + sshConnectPosts.length, state:'completed',
              result:{ok:true, sid, ip:'192.0.2.42', device_key:board.key, serial:board.serial,
                ssh_credentials_saved:true}}});
          }
          return json({ok:true, task:{id:'ssh-fixture-' + sshConnectPosts.length, state:'failed',
            error:'SSH 认证失败', result:{code:'ssh-authentication-failed',
              ssh_user:credentials && credentials.user || 'root'}}});
        }
        grantPosts.push(payload);
        return json({token: 'seriald-ag-simulated-fixture-only', grant: {id: 'fixture', actor: 'shared-agent/fixture',
          created: 1, expires: null, revoked: null, ...payload}});
      }); return;
    }
    json(u.pathname === '/api/session-send' && serialPayload ? {ok:true} : { ok: false, error: 'read-only fixture' }); return;
  }
  if (u.pathname === '/api/devices') return json({ devices: fixtures });
  if (u.pathname === '/api/hardware') return json({supported:true,actions:['reset'],providers:{reset:'bmc'}});
  if (u.pathname === '/api/session') {
    const chunk = u.searchParams.get('off') === '0' ? serialPayload ||
      'root@bianbu:~# \x1b[6n\x1b[?6n\x1b[c\x1b]11;?\x07'.repeat(15)
      + Array.from({length:80},(_,i)=>'\r\nserial line '+i).join('')
      + '\x1b[2J\x1b[50;1Hlast firmware row' : serialLiveChunks.shift() || '';
    const bytes = Buffer.from(typeof chunk === 'string' ? chunk : chunk.data);
    return json({ key: u.searchParams.get('key'), sid: u.searchParams.get('sid'), alive: true,
      off: u.searchParams.get('off') === '0' ? 4096 : Number(u.searchParams.get('off')) + bytes.length, data_b64: bytes.toString('base64'),
      ...(typeof chunk === 'object' ? {terminal_initial_geometry: chunk.geometry, terminal_geometry: chunk.geometry} : {}) });
  }
  if (u.pathname === '/api/sessions') return json({ sessions: [
    { sid: 'serial', kind: 'serial' }, { sid: 'ssh123abc', kind: 'ssh', actor: 'claude-code', ip: '10.0.90.42' },
    ...fixtureSshSessions] });
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
    rows: [{ type: 'replace', left_no: 1, left: 'clock=120MHz', right_no: 1, right: 'clock=160MHz',
      left_spans: [{text:'clock=1',changed:false},{text:'2',changed:true},{text:'0MHz',changed:false}],
      right_spans: [{text:'clock=1',changed:false},{text:'6',changed:true},{text:'0MHz',changed:false}] },
      { type: 'insert', left_no: null, left: '', right_no: 2, right: 'mmc timeout' }], truncated: false });
  if (u.pathname === '/api/log') return json({ data: log, off: log.length, start_off: 0, size: log.length, more: false });
  if (u.pathname === '/api/access') return json({ share_enabled: false, agent_share_enabled: true, lan_hosts: ['127.0.0.1'],
    mcp_enabled: true, mcp_url: '/mcp', mcp_error: '' });
  if (u.pathname === '/api/agent-grants') return json({grants: []});
  if (u.pathname === '/api/probe-settings') return json(probeSettings);
  if (u.pathname === '/api/flash-discover') return json({available:true,targets:flashUsbTargets});
  if (u.pathname === '/api/image-catalog') {
    if(u.searchParams.get('source') === 'internal') {res.statusCode=502;return json({error:'内部镜像源暂不可访问（模拟）'});}
    return json({entries:u.searchParams.get('path') ? [{name:'模拟固件.img',path:'k3/模拟固件.img',kind:'disk-image',size:1024,url:'https://example.test/fixture.img'}] : [{name:'k3',path:'k3/',kind:'directory'}]});
  }
  if (u.pathname === '/api/image-library') return json({artifacts:[flashArtifact],tasks:[]});
  if (u.pathname === '/api/flash-capabilities') return json({supported:true,board_serial:fixtures[1].serial,
    binding_revision:7,profile:'模拟 K3 · 浏览器测试夹具',control:{provider:'manual'},
    console:null,executor:{provider:'goflash',node:'local',endpoint:'fixture-usb-4'},
    artifacts:[flashArtifact],storages:flashStorages,modes:['full','partitions'],verification:{required:true}});
  if (u.pathname === '/api/flash-events') return json({events:Number(u.searchParams.get('after') || 0) < 1
    ? [{seq:1,type:'progress',phase:'writing',done:524288,total:1048576,
      message:'模拟事件 <script>window.flashInjected=true</script>'}] : [],next_after:1,truncated_before:0});
  if (u.pathname === '/api/task' && flashRecords.length) return json({task:flashRecords[0]});
  if (u.pathname === '/api/tasks') return json({tasks:flashRecords});
  const route = u.pathname === '/' ? '/index.html' : u.pathname;
  if (!['/index.html', '/static/i18n.js', '/static/app.js', '/static/file-transfer.js', '/static/flashing.js', '/static/image-store.js', '/static/boot-compare.js', '/static/share.js', '/static/ui-common.js', '/static/terminal-policy.js', '/static/serial-progress.js', '/static/terminal-navigation.js', '/static/terminal-view.js', '/static/archives.js', '/static/devices.js', '/static/tasks.js', '/static/board-control.js', '/static/app.css', '/static/icons.svg', '/static/decklab.svg', '/static/decklab.png', '/vendor/xterm.js', '/vendor/addon-fit.js', '/vendor/xterm.css'].includes(route)) { res.statusCode=404; res.end(); return; }
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
  await evaluate('SerialdI18n.setLocale("zh-CN")');
  const out=path.join(root,'dist');fs.mkdirSync(out,{recursive:true});
  const overviewShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-overview.png'),Buffer.from(overviewShot.data,'base64'));
  await evaluate('document.querySelector("#b-image-store").click()');
  for(let n=0;n<30 && !await evaluate('document.querySelectorAll("#is-list .is-row").length === 1');n++)await delay(50);
  await evaluate('document.querySelector("#is-list button").click()');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#is-list").textContent.includes("模拟固件.img")');n++)await delay(50);
  assert.ok(await evaluate('document.querySelector("#is-list").textContent.includes("下载到控制机")'));
  const storeShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-image-store-fixture.png'),Buffer.from(storeShot.data,'base64'));
  await evaluate('document.querySelector("#is-source").value="internal";document.querySelector("#is-source").dispatchEvent(new Event("change"))');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#is-status").textContent.includes("内部镜像源暂不可访问")');n++)await delay(50);
  assert.equal(await evaluate('document.querySelectorAll("#is-list .is-row").length'),0);
  await evaluate('document.querySelector("#is-source").value="local";document.querySelector("#is-source").dispatchEvent(new Event("change"))');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#is-list").textContent.includes("fixture.img")');n++)await delay(50);
  assert.ok(await evaluate('document.querySelector("#is-list").textContent.includes("删除")'));
  assert.equal(await evaluate('Array.from(document.querySelectorAll("#is-list button")).some(b=>b.textContent==="使用")'),false);
  await evaluate('document.querySelector("#is-close").click()');
  assert.deepEqual(posts,[], 'browsing image repositories must not submit a download or flash');
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
  assert.equal(await evaluate('document.querySelector(".bc-inline-remove").textContent'), '2');
  assert.equal(await evaluate('document.querySelector(".bc-inline-add").textContent'), '6');
  assert.ok(await evaluate('document.querySelector("#boot-compare-dialog").getBoundingClientRect().width > innerWidth * .95'));
  const compareShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-boot-compare.png'),Buffer.from(compareShot.data,'base64'));
  // A horizontal scroller inside each log line must pass vertical wheel input
  // to the comparison pane, including over the actual changed characters.
  await evaluate(`(() => {
    const diff=document.querySelector('#bc-diff'), row=diff.querySelector('.bc-row');
    row.querySelectorAll('.bc-content').forEach(cell=>cell.appendChild(document.createTextNode('x'.repeat(2000))));
    for(let n=0;n<100;n++)diff.appendChild(row.cloneNode(true));
  })()`);
  for (const selector of ['.bc-content', '.bc-inline-add', '.bc-line-no', '.bc-clue-text']) {
    await evaluate('document.querySelector("#bc-results").scrollTop=0');
    const point=await evaluate(`(() => {const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.left+Math.min(10,r.width/2),y:r.top+r.height/2};})()`);
    await rpc('Input.dispatchMouseEvent',{type:'mouseWheel',...point,deltaX:0,deltaY:180});
    for(let n=0;n<20 && !await evaluate('document.querySelector("#bc-results").scrollTop>0');n++)await delay(50);
    assert.ok(await evaluate('document.querySelector("#bc-results").scrollTop>0'),'vertical wheel over '+selector);
  }
  await evaluate('document.querySelector("#bc-results").scrollTop=0');
  const horizontalPoint=await evaluate(`(() => {const r=document.querySelector('.bc-content').getBoundingClientRect();return {x:r.left+10,y:r.top+r.height/2};})()`);
  await rpc('Input.dispatchMouseEvent',{type:'mouseWheel',...horizontalPoint,deltaX:200,deltaY:0});
  for(let n=0;n<20 && !await evaluate('document.querySelector(".bc-content").scrollLeft>0');n++)await delay(50);
  assert.ok(await evaluate('document.querySelector(".bc-content").scrollLeft>0'),'horizontal scrolling remains available');
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
  // Live handoff without Linux version: clear all old firmware cells/attributes
  // on the earliest kernel clock, including an incomplete header across reads.
  await evaluate("new Promise(resolve => testTerm.write(String.fromCharCode(27)+'[47m'+String.fromCharCode(27)+'[2J'+String.fromCharCode(27)+'[50;1Hlast firmware row', resolve))");
  assert.notEqual(await evaluate('testTerm.buffer.active.getLine(testTerm.buffer.active.baseY).getCell(0).getBgColorMode()'),0,'fixture must actually contain a firmware background');
  serialLiveChunks.push('\r\n[    0.000');
  serialLiveChunks.push('000] SBI DBCN extension detected\r\n[    0.000000] earlycon: sbi0 enabled\r\n');
  for(let n=0;n<40 && !await evaluate(`testTerm.buffer.active.getLine(0)?.translateToString().includes('SBI DBCN')`);n++)await delay(50);
  assert.ok(await evaluate(`testTerm.buffer.active.getLine(0)?.translateToString().includes('SBI DBCN')`));
  assert.equal(await evaluate('testTerm.buffer.active.getLine(0).getCell(0).getBgColorMode()'),0,'firmware background must reset');
  assert.ok(await evaluate(`Array.from({length:testTerm.rows},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString()||'').every(s=>!s.includes('last firmware row'))`));
  // Captured systemd size probe: without a CPR reply it never restores the
  // original cursor. Merely suppressing replies leaves a screen-sized gap.
  serialLiveChunks.push('[    1.326449] pcieport: failed with rc=-22\r\n\x1bP+q6E616D65\x1b\\\x1b[6n\x1b[32766;');
  serialLiveChunks.push('32766H\x1b[6n\x1b[!p\x1b]104\x1b\\\x1b[0m\x1b[?7h\x1b[1G\x1b[0J\x1b]3008;start=test-boot;type=boot\x1b\\[    3.516763] dracut-cmdline: copymods warning\r\n');
  for(let n=0;n<40 && !await evaluate(`Array.from({length:testTerm.buffer.active.length},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString()||'').some(s=>s.includes('copymods warning'))`);n++)await delay(50);
  const probeLines=await evaluate(`Array.from({length:testTerm.buffer.active.length},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString(true)||'')`);
  assert.equal(probeLines.findIndex(s=>s.includes('copymods warning')),probeLines.findIndex(s=>s.includes('pcieport: failed'))+1,
    'systemd size probe must not insert blank rows between kernel messages');
  assert.deepEqual(posts,[], 'passive size probes must not send terminal replies to the board');
  await evaluate(`document.querySelector('#d-status').textContent=' 正在从采集缓冲完整重绘…'`);
  await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  await delay(100);
  const sourceRows=await evaluate(`(() => {const v=testTerm.element.querySelector('.xterm-viewport');return Math.floor((v.clientHeight-2)/testTerm._core._renderService.dimensions.css.cell.height);})()`), sourceCols=await evaluate(`(() => {const v=testTerm.element.querySelector('.xterm-viewport');return Math.floor((v.clientWidth-17)/testTerm._core._renderService.dimensions.css.cell.width);})()`);
  const preMetrics=await evaluate(`({font:testTerm.options.fontSize,cols:testTerm.cols,rows:testTerm.rows,cell:testTerm._core._renderService.dimensions.css.cell,width:testTerm.element.querySelector('.xterm-viewport').clientWidth,height:testTerm.element.querySelector('.xterm-viewport').clientHeight})`);
  assert.ok(sourceRows>24,'kernel text retains the large viewport');
  // A board already synchronized to the primary window now starts APT.
  serialPayload = 'Linux version 6.18.0\r\n' + '\x1b[8;'+sourceRows+';'+sourceCols+'t'
    + Array.from({length:80},(_,i)=>'apt preamble '+i+'\r\n').join('')
    + '\n\x1b7\x1b[0;'+(sourceRows-1)+'r\x1b8\x1b[1A'
    + '\x1b7\x1b['+sourceRows+';0fProgress: [ 88%]\x1b8'
    + Array.from({length:40},(_,i)=>'Unpacking package '+i+'\r\n').join('');
  await evaluate('document.querySelector("#b-redraw").click()');
  for(let n=0;n<30 && !await evaluate(`Array.from({length:testTerm.rows},(_,i)=>testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+i)?.translateToString()||'').some(s=>s.includes('Progress:'))`);n++)await delay(100);
  const cursorBefore = await evaluate('testTerm.buffer.active.cursorY');
  const postsBefore = posts.length;
  await evaluate('testTerm.focus()');
  for (const [key, code] of [['ArrowUp',38],['ArrowDown',40]]) {
    await rpc('Input.dispatchKeyEvent',{type:'keyDown',key,code:key,windowsVirtualKeyCode:code});
    await rpc('Input.dispatchKeyEvent',{type:'keyUp',key,code:key,windowsVirtualKeyCode:code});
  }
  await delay(150);
  assert.ok(posts.length > postsBefore,'APT arrows retain normal board passthrough');
  await evaluate("new Promise(resolve => testTerm.write('^[[A^[[B', resolve))");
  assert.equal(await evaluate('testTerm.buffer.active.cursorY'),cursorBefore,'visible arrow echo must not move stdout cursor');
  serialPayload += '\n\x1b7\x1b[0;'+sourceRows+'r\x1b8\x1b[1A\x1b[J'
    + Array.from({length:30},(_,i)=>'post-install line '+i+'\r\n').join('')
    + 'root@ubuntu:~# ';
  await evaluate('document.querySelector("#b-redraw").click()');
  for(let n=0;n<30 && !await evaluate(`testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+testTerm.buffer.active.cursorY)?.translateToString().includes('root@ubuntu')`);n++)await delay(100);
  const aptScreen=await evaluate(`(() => {const b=testTerm.buffer.active;return {cols:testTerm.cols,rows:testTerm.rows,fontSize:testTerm.options.fontSize,cursor:b.cursorY,lines:Array.from({length:testTerm.rows},(_,i)=>b.getLine(b.baseY+i)?.translateToString())};})()`);
  assert.equal(aptScreen.fontSize,14.5,'APT must not shrink the terminal font: '+JSON.stringify({sourceRows,sourceCols,preMetrics,current:await evaluate(`({font:testTerm.options.fontSize,cols:testTerm.cols,rows:testTerm.rows,cell:testTerm._core._renderService.dimensions.css.cell,width:testTerm.element.querySelector('.xterm-viewport').clientWidth,height:testTerm.element.querySelector('.xterm-viewport').clientHeight})`)}));
  assert.ok(aptScreen.rows > 24,'serial must retain the large browser viewport');
  assert.equal(aptScreen.cursor,aptScreen.rows-1,'ordinary output must return to the bottom row after apt');
  assert.ok(aptScreen.lines[aptScreen.rows-1].includes('root@ubuntu:~#'));
  assert.ok(aptScreen.lines[aptScreen.rows-2].includes('post-install line 29'));
  assert.ok(!aptScreen.lines.some(line=>line.includes('Progress:')),'finished progress must not remain in the middle');
  // Re-open the same capture in a shorter/narrower desktop window. The logical
  // screen and cursor must be identical; only font pixels adapt to its viewport.
  await rpc('Emulation.setDeviceMetricsOverride',{width:1100,height:620,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector(\'[data-key="milkv2"]\').click()');
  for(let n=0;n<40 && !await evaluate(`testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+testTerm.buffer.active.cursorY)?.translateToString().includes('root@ubuntu')`);n++)await delay(50);
  const observerScreen=await evaluate(`(() => {const b=testTerm.buffer.active;return {cols:testTerm.cols,rows:testTerm.rows,cursor:b.cursorY,lines:Array.from({length:testTerm.rows},(_,i)=>b.getLine(b.baseY+i)?.translateToString())};})()`);
  assert.deepEqual(observerScreen,{cols:aptScreen.cols,rows:aptScreen.rows,cursor:aptScreen.cursor,lines:aptScreen.lines},'shared replay must not depend on browser resolution');
  const bottomFits=await evaluate(`(() => {const v=testTerm.element.querySelector('.xterm-viewport');const c=testTerm._core._renderService.dimensions.css.cell;return {painted:testTerm.rows*c.height,visible:v.clientHeight,columns:testTerm.cols*c.width,width:v.clientWidth-16};})()`);
  assert.ok(bottomFits.painted <= bottomFits.visible && bottomFits.columns <= bottomFits.width,'last row and rightmost column must remain visible: '+JSON.stringify(bottomFits));
  // Shared geometry metadata accompanies arbitrary UART fragments, including
  // fragments inside OSC 3008 and a split string terminator. It is not an ANSI
  // prefix: injecting CSI here cancels OSC and exposes shell context text.
  const shellGeometry={cols:aptScreen.cols,rows:aptScreen.rows};
  const multibyte=Buffer.from('中文');
  const contextFragments=['\r\n\x1b]3008;start=test-command;machine',
    'id=marker-device;bootid=marker-boot;cwd=/root\x1b',
    '\\OSC-replay-output',multibyte.subarray(0,1),multibyte.subarray(1),
    '\r\n\x1b]3008;end=test-command;exit=su',
    'ccess\x1b\\root@ubuntu:~# '];
  for(const data of contextFragments)serialLiveChunks.push({data,geometry:shellGeometry});
  for(let n=0;n<40 && serialLiveChunks.length;n++)await delay(50);
  await delay(100);
  const contextScreen=await evaluate(`Array.from({length:testTerm.buffer.active.length},(_,i)=>testTerm.buffer.active.getLine(i)?.translateToString(true)||'').join(String.fromCharCode(10))`);
  assert.ok(contextScreen.includes('OSC-replay-output中文'), 'metadata must also preserve fragmented UTF-8');
  assert.ok(!/machineid|marker-device|marker-boot|exit=success|exit=su|cwd=/.test(contextScreen),
    'split shell context must stay invisible while geometry metadata updates');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1366,height:768,deviceScaleFactor:1,mobile:false});
  await delay(100);
  // Shared stty marker: use real viewport measurements, then check wrapping.
  await evaluate('if(!document.querySelector("#d-records").hidden)document.querySelector("#b-records-close").click()');
  await delay(100);
  const wideCols = await evaluate(`(() => {const v=testTerm.element.querySelector('.xterm-viewport');const c=testTerm._core._renderService.dimensions.css.cell.width;return Math.floor((v.clientWidth-24)/c);})()`);
  assert.ok(wideCols>80,'desktop serial viewport should support wider columns');
  await evaluate(`new Promise(resolve => testTerm.write(String.fromCharCode(27)+'[8;32;${wideCols}t', resolve))`);
  assert.equal(await evaluate('testTerm.cols'),wideCols);
  assert.equal(await evaluate('testTerm.rows'),32);
  const wideBounds = await evaluate(`(() => {const v=testTerm.element.querySelector('.xterm-viewport');const c=testTerm._core._renderService.dimensions.css.cell.width;return {drawn:testTerm.cols*c,visible:v.clientWidth-16};})()`);
  assert.ok(wideBounds.drawn <= wideBounds.visible,'wide serial columns must remain visible');
  await evaluate(`new Promise(resolve => {testTerm.reset();testTerm.write('X'.repeat(${wideCols-1})+'YZ',resolve);})`);
  assert.ok(await evaluate(`testTerm.buffer.active.getLine(0)?.translateToString(true).endsWith('Y')`));
  assert.ok(await evaluate(`testTerm.buffer.active.getLine(1)?.translateToString(true).startsWith('Z')`));
  // These are isolated HTTP fixtures, with no collector or real board. Label
  // screenshots explicitly so they cannot be mistaken for field evidence.
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate(`(() => {const banner=document.createElement('div'); banner.textContent='浏览器测试夹具 · 模拟设备与令牌';
    banner.style.cssText='position:fixed;top:8px;left:8px;z-index:10000;background:#713f12;color:white;padding:8px 12px;border-radius:6px;font:13px sans-serif';
    document.body.appendChild(banner);})()`);
  await evaluate('document.querySelector(\'[data-key="unprovisioned-demo"]\').click();document.querySelector("#b-probe-settings").click()');
  for(let n=0;n<30 && await evaluate('document.querySelector("#probe-settings-mode").disabled');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#probe-settings-dialog").open'),true);
  await evaluate(`document.querySelector('#probe-settings-mode').value='custom';document.querySelector('#probe-settings-mode').dispatchEvent(new Event('change'));
    document.querySelector('#probe-settings-command').value="echo HOST=buildroot\\necho MODEL=K3";
    document.querySelector('#probe-settings-command').dispatchEvent(new Event('input'))`);
  assert.equal(await evaluate('document.querySelector("#probe-settings-save").disabled'),false);
  await evaluate('document.querySelector("#probe-settings-save").click()');
  for(let n=0;n<30 && !probeSettingsPosts.length;n++)await delay(50);
  assert.equal(probeSettingsPosts[0].mode,'custom');
  assert.equal(probeSettingsPosts[0].connection_id,'test-unprovisioned-demo');
  assert.equal(posts.includes('/api/task-start'),false,'save must not execute the probe');
  const probeSettingsShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-probe-settings.png'),Buffer.from(probeSettingsShot.data,'base64'));
  await evaluate('document.querySelector("#probe-settings-close").click();document.querySelector("#b-agent-share").click()');
  assert.equal(await evaluate('document.querySelector("#agent-share-create").disabled'),false);
  assert.ok(await evaluate('document.querySelector("#agent-share-hint").textContent.includes("无需识别")'));
  await evaluate('document.querySelector("#agent-share-create").click()');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#agent-share-text").value.includes("mcpServers")');n++)await delay(50);
  assert.equal(grantPosts[0].scope,'connection');
  const generatedConfig=await evaluate('JSON.parse(document.querySelector("#agent-share-text").value)');
  assert.equal(generatedConfig.mcpServers.decklab.url, address + '/mcp');
  assert.equal(generatedConfig.mcpServers.decklab.headers.Authorization,'Bearer seriald-ag-simulated-fixture-only');
  assert.equal(await evaluate('document.querySelector("#agent-share-mcp-options").hidden'),true);
  const mcpShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-share-mcp.png'),Buffer.from(mcpShot.data,'base64'));
  await evaluate('document.querySelector("#agent-share-close").click();Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.includes("+ SSH")).click()');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#ssh-credentials-dialog").open');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-dialog").open'),true);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-user").value'),'root');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").type'),'password');
  assert.equal(sshConnectPosts.length,1);
  assert.equal(sshConnectPosts[0].connection_id,'test-unprovisioned-demo');
  await evaluate('document.querySelector("#ssh-credentials-target").textContent += " · 浏览器测试夹具，模拟设备"');
  const sshShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-credentials.png'),Buffer.from(sshShot.data,'base64'));
  const serialWrites=posts.filter(route=>route==='/api/session-send').length;
  await evaluate('document.querySelector("#ssh-root-guide").open=true;document.querySelector("#ssh-root-copy-passwd").click()');
  assert.ok(await evaluate('document.querySelector("#ssh-root-copy-status").textContent.includes("分步执行")'));
  assert.equal(posts.filter(route=>route==='/api/session-send').length,serialWrites,'root guide must only copy');
  const rootGuideShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-root-guide.png'),Buffer.from(rootGuideShot.data,'base64'));
  await evaluate('document.querySelector("#ssh-root-guide").open=false;document.querySelector("#ssh-credentials-user").value="ubuntu";document.querySelector("#ssh-credentials-password").value="simulated-password-only";document.querySelector("#ssh-credentials-form").requestSubmit()');
  for(let n=0;n<30 && sshConnectPosts.length<2;n++)await delay(50);
  assert.equal(sshConnectPosts[1].ssh_user,'ubuntu');
  assert.equal(sshConnectPosts[1].ssh_password,'simulated-password-only');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#ssh-credentials-dialog").open');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  await evaluate('document.querySelector("#ssh-credentials-target").textContent += " · 浏览器测试夹具，模拟设备"');
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate('(()=>{const r=document.querySelector("#ssh-credentials-dialog").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;})()'));
  const sshMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-credentials-mobile.png'),Buffer.from(sshMobileShot.data,'base64'));
  await evaluate('document.querySelector("#ssh-root-guide").open=true');
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#ssh-credentials-dialog");const r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&d.scrollWidth<=d.clientWidth+1;})()'), 'expanded mobile guidance must stay within the viewport without horizontal overflow');
  const rootGuideMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-root-guide-mobile.png'),Buffer.from(rootGuideMobileShot.data,'base64'));
  await evaluate('document.querySelector("#ssh-credentials-password").value="must-clear";document.querySelector("#ssh-credentials-cancel").click()');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  // A verified account belongs to the board serial in the server fixture, so a
  // new page can reuse it without receiving or storing the password in JS.
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector(\'[data-key="musepi2"]\').click();Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.includes("+ SSH")).click()');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#ssh-credentials-dialog").open');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-dialog").open'),true);
  assert.ok(await evaluate('document.querySelector("#ssh-credentials-intro").textContent.includes("服务端")'));
  await evaluate('document.querySelector("#ssh-credentials-user").value="ubuntu";document.querySelector("#ssh-credentials-password").value="simulated-saved-password-only";document.querySelector("#ssh-credentials-form").requestSubmit()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#d-status").textContent.includes("192.0.2.42")');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-dialog").open'),false);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  const rememberedBoard = fixtures.find(device => device.key === 'musepi2');
  assert.deepEqual(rememberedBoard.ssh_settings,{saved:true,user:'ubuntu'});
  await evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.startsWith("SSH 设置")).click()');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-user").value'),'ubuntu');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  assert.ok(await evaluate('document.querySelector("#ssh-credentials-saved-status").textContent.includes("已保存")'));
  const rememberedShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-remembered.png'),Buffer.from(rememberedShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#ssh-credentials-dialog");const r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'));
  const rememberedMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-ssh-remembered-mobile.png'),Buffer.from(rememberedMobileShot.data,'base64'));
  await evaluate('document.querySelector("#ssh-credentials-cancel").click()');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate('window.sshReloadMarker=true');
  await rpc('Page.navigate',{url:address + '/#device=' + rememberedBoard.serial});
  for(let n=0;n<50 && !await evaluate('!window.sshReloadMarker && Array.from(document.querySelectorAll("#d-tabs button")).some(button=>button.textContent.startsWith("SSH 设置"))');n++)await delay(100);
  await evaluate('SerialdI18n.setLocale("zh-CN");Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.startsWith("SSH 设置")).click()');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-user").value'),'ubuntu','new page must prefill the server-stored account');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'','new page must never receive the stored password');
  assert.equal(await evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}}).includes("simulated-saved-password-only")'),false);
  assert.equal(await evaluate('fetch("/api/devices").then(response=>response.text()).then(text=>text.includes("simulated-saved-password-only")||text.includes("ssh_password"))'),false);
  // A rejected replacement does not overwrite the remembered account. A fresh
  // settings form uses server metadata instead of the failed retry's username.
  const beforeReplacement = sshConnectPosts.length;
  await evaluate('document.querySelector("#ssh-credentials-user").value="debian";document.querySelector("#ssh-credentials-password").value="rejected-fixture-password";document.querySelector("#ssh-credentials-form").requestSubmit()');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#ssh-credentials-dialog").open');n++)await delay(50);
  assert.equal(sshConnectPosts.length,beforeReplacement+1);
  assert.deepEqual(rememberedBoard.ssh_settings,{saved:true,user:'ubuntu'});
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  await evaluate('document.querySelector("#ssh-credentials-cancel").click();Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.startsWith("SSH 设置")).click()');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-user").value'),'ubuntu');
  await evaluate('document.querySelector("#ssh-credentials-cancel").click();Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.includes("+ SSH")).click()');
  for(let n=0;n<30 && sshConnectPosts.length<beforeReplacement+2;n++)await delay(50);
  assert.equal(sshConnectPosts.length,beforeReplacement+2);
  assert.equal(Object.hasOwn(sshConnectPosts.at(-1),'ssh_user'),false);
  assert.equal(Object.hasOwn(sshConnectPosts.at(-1),'ssh_password'),false);
  assert.equal(sshAuthentications.at(-1).user,'ubuntu','default connection must reuse the server account');
  assert.equal(sshAuthentications.at(-1).password,'simulated-saved-password-only');
  for(let n=0;n<30 && !await evaluate('document.querySelector("#d-status").textContent.includes("192.0.2.42")');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-dialog").open'),false);
  await evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(button=>button.textContent.startsWith("SSH 设置")).click();document.querySelector("#ssh-credentials-password").value="must-not-reach-replacement"');
  rememberedBoard.connection_id = 'test-musepi2-replacement';
  for(let n=0;n<80 && await evaluate('document.querySelector("#ssh-credentials-dialog").open');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-dialog").open'),false,'replacement connection must close an old credentials form');
  assert.equal(await evaluate('document.querySelector("#ssh-credentials-password").value'),'');
  const beforeStaleSubmit=sshConnectPosts.length;
  await evaluate('document.querySelector("#ssh-credentials-form").requestSubmit()');
  await delay(100);
  assert.equal(sshConnectPosts.length,beforeStaleSubmit,'stale form must not submit credentials for a replacement connection');
  // A registered offline board can plan flashing without opening a UART. The
  // fixtures below only return data and record requests; no tool is executed.
  await evaluate('document.querySelector(\'[data-key="riscv-118"]\').click();document.querySelector("#b-flash").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-artifact").options.length>1');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#flash-dialog").open'),true);
  assert.ok(await evaluate('document.querySelector("#fl-board").textContent.includes("riscv-118")'));
  assert.ok(await evaluate('document.querySelector("#fl-connections").textContent.includes("本次不执行复位")'));
  assert.equal(flashSubmissions.length,0,'opening capabilities must not start flashing');
  await evaluate('document.querySelector("#fl-artifact").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"));document.querySelector("#fl-mode").value="partitions";document.querySelector("#fl-mode").dispatchEvent(new Event("change"));document.querySelector("#fl-partitions input").checked=true;document.querySelector("#fl-partitions input").dispatchEvent(new Event("change"));document.querySelector("#fl-partitions select").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-plan").click()');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-start").disabled');n++)await delay(50);
  assert.equal(flashPlans.length,1);
  assert.deepEqual(flashPlans[0].selections,[{storage:'emmc',partition:'rootfs',artifact_id:flashArtifact.id}]);
  assert.equal(flashPlans[0].mode,'partitions');
  assert.equal(Object.hasOwn(flashPlans[0],'connection_id'),false);
  assert.ok(await evaluate('document.querySelector("#fl-preview table").textContent.includes("rootfs")'));
  assert.ok(await evaluate('!document.querySelector("#fl-review").hidden'));
  assert.equal(await evaluate('document.querySelectorAll("#fl-preview tr").length'),2,'partition preview must contain exactly the selected write');
  assert.equal(flashSubmissions.length,0,'preview must not submit a write');
  const flashPreviewShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-preview.png'),Buffer.from(flashPreviewShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#flash-dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'), 'mobile flash form must stay inside the viewport');
  const flashMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-preview-mobile.png'),Buffer.from(flashMobileShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector("#fl-start").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-task-id").textContent.includes("cccccccc")');n++)await delay(50);
  assert.equal(flashSubmissions.length,1);
  assert.equal(flashSubmissions[0].binding_revision,7);
  assert.equal(Object.hasOwn(flashSubmissions[0],'connection_id'),false,'flash submission must use board identity rather than a UART generation');
  assert.ok(flashSubmissions[0].request_id);
  await evaluate('document.querySelector("#fl-close").click()');
  assert.equal(flashRecords[0].state,'running','closing the dialog must leave server work running');
  await rpc('Page.navigate',{url:address + '/#device=' + fixtures[1].serial});
  for(let n=0;n<50 && !await evaluate('document.querySelector("#d-name").textContent.includes("riscv-118")');n++)await delay(100);
  await evaluate('SerialdI18n.setLocale("zh-CN");document.querySelector("#b-flash").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-task-id").textContent.includes("cccccccc")');n++)await delay(50);
  assert.equal(flashSubmissions.length,1,'reload must recover the original task without submitting again');
  await evaluate('document.querySelector("#flash-dialog details").open=true');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-events").textContent.includes("模拟事件")');n++)await delay(50);
  assert.equal(await evaluate('window.flashInjected === true'),false,'events must be rendered as text');
  const flashTaskShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-task.png'),Buffer.from(flashTaskShot.data,'base64'));
  flashRecords[0].state = 'failed';
  Object.assign(flashRecords[0].flash,{recovery_required:true,attempt_id:'simulated-recovery-attempt',
    write_result:'unknown-or-partial',verify_result:'not-completed'});
  for(let n=0;n<50 && await evaluate('document.querySelector("#fl-recovery-field").hidden');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#fl-recovery-field").hidden'),false);
  assert.equal(await evaluate('document.querySelector("#fl-release").disabled'),true,'recovery must require an operator reason');
  await evaluate('document.querySelector("#fl-recovery-reason").value="浏览器模拟夹具：已在执行主机确认原进程停止";document.querySelector("#fl-recovery-reason").dispatchEvent(new Event("input"));document.querySelector("#fl-recovery-field").scrollIntoView({block:"center"})');
  assert.equal(await evaluate('document.querySelector("#fl-release").disabled'),false);
  const flashRecoveryShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-recovery.png'),Buffer.from(flashRecoveryShot.data,'base64'));
  await evaluate('document.querySelector("#fl-release").click()');
  for(let n=0;n<40 && flashRecoveryPosts.length<1;n++)await delay(50);
  assert.equal(flashRecoveryPosts.length,1);
  assert.equal(flashRecoveryPosts[0].attempt_id,'simulated-recovery-attempt');
  assert.equal(flashRecoveryPosts[0].confirm_stopped,true);
  assert.equal(flashRecords[0].state,'failed','releasing recovery must preserve the original failed state');
  assert.equal(flashRecords[0].flash.write_result,'unknown-or-partial');
  assert.equal(flashSubmissions.length,1,'recovery must not repeat the flash operation');
  // Personal device positions survive connection state changes and page reload.
  // These are browser fixtures, never a board or registry mutation.
  await evaluate('document.querySelector("#fl-close").click()');
  const directoryOrder = () => evaluate('Array.from(document.querySelectorAll("#grid>.dev")).map(card=>card.dataset.key)');
  const originalOrder = await directoryOrder(), selectedBeforeOrder = await evaluate('document.querySelector("#d-name").textContent');
  await evaluate('document.querySelectorAll("#grid>.dev")[1].querySelector(\'[data-order-control="up"]\').click()');
  const movedOrder = [originalOrder[1],originalOrder[0],...originalOrder.slice(2)];
  assert.deepEqual(await directoryOrder(),movedOrder);
  assert.equal(await evaluate('document.querySelector("#d-name").textContent'),selectedBeforeOrder,'moving a card must not select it');
  const dragOrder = await evaluate(`(() => {
    const cards=Array.from(document.querySelectorAll('#grid>.dev')), source=cards[2],target=cards[0],transfer=new DataTransfer();
    source.querySelector('[data-order-control="drag"]').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));
    const r=target.getBoundingClientRect();
    target.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer,clientY:r.top+1}));
    return Array.from(document.querySelectorAll('#grid>.dev')).map(card=>card.dataset.key);
  })()`);
  assert.deepEqual(dragOrder,[movedOrder[2],movedOrder[0],movedOrder[1],...movedOrder.slice(3)]);
  const reorderedFixture=fixtures.find(item=>item.key===dragOrder[0]), previousAlive=reorderedFixture.alive;
  reorderedFixture.alive=!previousAlive;
  for(let n=0;n<40 && await evaluate('document.querySelector("#grid>.dev").classList.contains("offline")') !== previousAlive;n++)await delay(100);
  assert.equal(await evaluate('document.querySelector("#grid>.dev").classList.contains("offline")'),previousAlive);
  assert.deepEqual(await directoryOrder(),dragOrder,'online state changes must preserve chosen positions');
  reorderedFixture.alive=previousAlive;
  await rpc('Page.reload',{ignoreCache:true});
  for(let n=0;n<50 && !await evaluate('document.querySelectorAll("#grid>.dev").length === 5');n++)await delay(100);
  assert.deepEqual(await directoryOrder(),dragOrder,'a new page must restore personal device positions');
  await evaluate('SerialdI18n.setLocale("zh-CN")');
  const deviceOrderShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-device-order.png'),Buffer.from(deviceOrderShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate(`(() => {
    const card=document.querySelector('#grid>.dev'),button=card.querySelector('[data-order-control="down"]');
    const c=card.getBoundingClientRect(),b=button.getBoundingClientRect();
    return b.width>=24&&b.height>=24&&b.top>=c.top&&b.bottom<=c.bottom&&document.documentElement.scrollWidth<=innerWidth+1;
  })()`),'touch move controls must remain inside compact mobile device cards');
  await evaluate('document.querySelector("#grid>.dev").querySelector(\'[data-order-control="down"]\').click()');
  assert.deepEqual(await directoryOrder(),[dragOrder[1],dragOrder[0],...dragOrder.slice(2)]);
  const deviceMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-device-order-mobile.png'),Buffer.from(deviceMobileShot.data,'base64'));
  // USB recovery targets do not require a UART or a previously registered serial.
  // Detection/probing is data-only in this fixture; no hardware tool is executed.
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  flashRecords.splice(0);
  await evaluate('document.querySelector("#b-flash-discover").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-device").options.length === 3');n++)await delay(50);
  assert.equal(flashTargetReads.length,0,'multiple USB targets must not choose the first device');
  assert.ok(await evaluate('document.querySelector("#fl-status").textContent.includes("多个下载设备")'));
  await evaluate('document.querySelector("#fl-device").value="fixture-usb-3";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-artifact").options.length === 2');n++)await delay(50);
  assert.equal(flashTargetReads.length,1);
  assert.equal(flashTargetReads[0].target_id,'fixture-usb-3');
  assert.equal(flashTargetReads[0].target_generation,'fixture-generation-3');
  assert.equal(Object.hasOwn(flashTargetReads[0],'serial'),false,'USB-only target must not invent a serial');
  assert.equal(flashSubmissions.length,1,'automatic detection and probing must not flash storage');
  await evaluate('document.querySelector("#fl-artifact").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-plan").disabled');n++)await delay(50);
  await evaluate('document.querySelector("#fl-plan").click()');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-start").disabled');n++)await delay(50);
  assert.equal(flashPlans[flashPlans.length-1].target_id,'fixture-usb-3');
  assert.equal(flashPlans[flashPlans.length-1].target_generation,'fixture-generation-3');
  assert.equal(Object.hasOwn(flashPlans[flashPlans.length-1],'key'),false);
  await evaluate(`(() => {const banner=document.createElement('p');banner.textContent='浏览器模拟夹具：自动发现 USB，无真实硬件';banner.style.cssText='color:#713f12;font-weight:bold';document.querySelector('#flash-dialog').prepend(banner);})()`);
  const flashDiscoveryShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-discovery.png'),Buffer.from(flashDiscoveryShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#flash-dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'), 'USB target chooser must fit mobile viewport');
  const flashDiscoveryMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-discovery-mobile.png'),Buffer.from(flashDiscoveryMobileShot.data,'base64'));
  // A detected BootROM can prepare from a selected package. The preview has no
  // storage writes, and actual partition selection stays in the same task.
  await evaluate('document.querySelector("#fl-close").click()');
  flashUsbTargets[1].mode='bootrom'; flashUsbTargets[1].target_generation='fixture-brom-generation';
  await evaluate('document.querySelector("#b-flash-discover").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-device").options.length === 3');n++)await delay(50);
  await evaluate('document.querySelector("#fl-device").value="fixture-usb-3";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-mode").value === "partitions"');n++)await delay(50);
  assert.equal(await evaluate('document.querySelector("#fl-mode").disabled'),true);
  assert.ok(await evaluate('document.querySelector("#fl-status").textContent.includes("BootROM")'));
  await evaluate('document.querySelector("#fl-artifact").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-plan").disabled');n++)await delay(50);
  await evaluate('document.querySelector("#fl-plan").click()');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-start").disabled');n++)await delay(50);
  assert.equal(flashPlans[flashPlans.length-1].mode,'partitions');
  assert.deepEqual(flashPlans[flashPlans.length-1].selections,[]);
  assert.ok(await evaluate('document.querySelector("#fl-preview").textContent.includes("不写入存储")'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await delay(100);
  await evaluate('document.querySelector("#fl-preview").scrollIntoView({block:"center"})');
  const flashBootromShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-bootrom-preparation.png'),Buffer.from(flashBootromShot.data,'base64'));
  await evaluate('document.querySelector("#fl-start").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-task-id").textContent.includes("awaiting-selection")');n++)await delay(50);
  assert.equal(flashSubmissions.length,2,'one original flash task and one explicit preparation task');
  assert.equal(flashSubmissions[1].target_generation,'fixture-brom-generation');
  assert.equal(await evaluate('document.querySelector("#fl-partitions").children.length'),2);
  assert.equal(await evaluate('document.querySelector("#fl-select").hidden'),false);
  assert.equal(flashRecords[0].flash.write_result,'not-started');
  const readsBeforeTaskRefresh=flashTargetReads.length;
  flashUsbTargets[1].mode='loader'; flashUsbTargets[1].target_generation='fixture-new-loader-generation';
  await evaluate('document.querySelector("#fl-close").click();document.querySelector("#b-flash-discover").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-device").options.length === 3');n++)await delay(50);
  await evaluate('document.querySelector("#fl-device").value="fixture-usb-3";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-task-id").textContent.includes("awaiting-selection")');n++)await delay(50);
  assert.equal(flashTargetReads.length,readsBeforeTaskRefresh,'a refreshed active task must not probe its owned endpoint');
  await evaluate('document.querySelector("#fl-partitions input").checked=true;document.querySelector("#fl-partitions select").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-select").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-select").textContent.includes("确认分区并继续")');n++)await delay(50);
  assert.equal(flashSubmissions.length,2,'refresh must recover the original preparation rather than start again');
  assert.ok(await evaluate('document.querySelector("#fl-preview table").textContent.includes("rootfs")'));
  assert.ok(await evaluate('!document.querySelector("#fl-review").hidden'));
  // NOR scope confirmation is a separate human step after loader preparation.
  await evaluate('document.querySelector("#fl-close").click()');
  flashRecords.splice(0); flashUsbTargets[1].mode='bootrom';flashUsbTargets[1].nor=true;
  flashUsbTargets[1].target_generation='fixture-nor-brom';
  await evaluate('document.querySelector("#b-flash-discover").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-device").options.length===3');n++)await delay(50);
  await evaluate('document.querySelector("#fl-device").value="fixture-usb-3";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-mode").disabled');n++)await delay(50);
  await evaluate('document.querySelector("#fl-mode").value="full";document.querySelector("#fl-mode").dispatchEvent(new Event("change"));document.querySelector("#fl-artifact").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"));document.querySelector("#fl-plan").click()');
  for(let n=0;n<40 && await evaluate('document.querySelector("#fl-start").disabled');n++)await delay(50);
  assert.ok(await evaluate('document.querySelector("#fl-start").textContent.includes("准备环境")'));
  await evaluate('document.querySelector("#fl-start").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-select").textContent.includes("预览固件写入")');n++)await delay(50);
  assert.equal(flashRecords[0].flash.write_result,'not-started');
  assert.ok(await evaluate('document.querySelector("#fl-preview").textContent.includes("擦除风险范围")'));
  await evaluate('document.querySelector("#fl-select").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-select").textContent.includes("确认固件范围")');n++)await delay(50);
  assert.equal(flashRecords[0].flash.write_result,'not-started','a scope preview cannot begin storage writes');
  await evaluate('document.querySelector("#fl-preview").scrollIntoView({block:"center"})');
  const norShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-nor-confirmation-fixture.png'),Buffer.from(norShot.data,'base64'));
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await delay(100);
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#flash-dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'),'NOR review must fit mobile viewport');
  const norMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-nor-confirmation-mobile-fixture.png'),Buffer.from(norMobileShot.data,'base64'));
  const norSubmissions=flashSubmissions.length;
  await evaluate('document.querySelector("#fl-select").click()');
  for(let n=0;n<40 && flashRecords[0].flash.write_result!=='writing';n++)await delay(50);
  assert.equal(flashRecords[0].flash.write_result,'writing');
  assert.equal(flashSubmissions.length,norSubmissions,'final confirmation continues the same task');
  // Deletion touches this fixture's in-memory artifact list only.
  await evaluate('document.querySelector("#fl-close").click()');flashRecords.splice(0);
  await evaluate('document.querySelector("#b-flash-discover").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-device").options.length===3');n++)await delay(50);
  await evaluate('document.querySelector("#fl-device").value="fixture-usb-3";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-artifact").options.length===2');n++)await delay(50);
  await evaluate('document.querySelector("#fl-artifact").value="aaaaaaaaaaaaaaaaaaaaaaaa";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"));document.querySelector("#fl-delete").click()');
  assert.equal(await evaluate('document.querySelector("#fl-delete-field").hidden'),false);
  assert.ok(await evaluate('document.querySelector("#fl-delete-name").textContent.includes("模拟镜像")'));
  assert.ok(await evaluate('document.querySelector("#fl-delete-name").textContent.includes("SHA-256")'));
  assert.equal(flashDeletePosts.length,0);
  await evaluate('document.querySelector("#fl-delete-field").scrollIntoView({block:"center"})');
  const deleteMobileShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-image-delete-mobile-fixture.png'),Buffer.from(deleteMobileShot.data,'base64'));
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#flash-dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'),'image delete confirmation must fit mobile');
  await evaluate('document.querySelector("#fl-delete-cancel").click()');
  assert.equal(flashDeletePosts.length,0,'cancel never removes an image');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector("#fl-delete").click();document.querySelector("#fl-delete-field").scrollIntoView({block:"center"})');
  const deleteShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-flash-image-delete-fixture.png'),Buffer.from(deleteShot.data,'base64'));
  await evaluate('document.querySelector("#fl-delete-confirm").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#fl-status").textContent.includes("镜像已删除")');n++)await delay(50);
  assert.equal(flashDeletePosts.length,1);assert.equal(flashDeletePosts[0].id,flashArtifact.id);
  assert.equal(await evaluate('document.querySelector("#fl-artifact").options.length'),1);
  assert.equal(await evaluate('document.querySelector("#fl-delete").disabled'),true);
  assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),true);
  assert.deepEqual(errors,[]);
  await evaluate('document.querySelector("#fl-close").click();document.querySelector(\'[data-key="milkv2"]\').click()');
  for(let n=0;n<40 && !await evaluate('!document.querySelector("#b-board-control").hidden');n++)await delay(50);
  await evaluate('document.querySelector("#b-board-control").click()');
  assert.equal(await evaluate('document.querySelector("#board-control-dialog").open'),true);
  assert.equal(hardwarePosts.length,0,'control confirmation never submits by opening');
  assert.equal(await evaluate('document.querySelector("#board-control-target").textContent'),'0059D968E583C49A');
  await evaluate('document.querySelector("#board-control-title").textContent="板卡控制 · 模拟界面验收"');
  const controlShot=await rpc('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(out,'ui-board-control-fixture.png'),Buffer.from(controlShot.data,'base64'));
  await evaluate('document.querySelector("#board-control-submit").click();document.querySelector("#board-control-submit").click()');
  for(let n=0;n<40 && !await evaluate('document.querySelector("#board-control-status").textContent.includes("控制请求已确认")');n++)await delay(50);
  assert.equal(hardwarePosts.length,1);assert.equal(hardwarePosts[0].serial,'0059D968E583C49A');
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate('(()=>{const d=document.querySelector("#board-control-dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1;})()'));
  assert.deepEqual(errors,[]);
  console.log('Browser smoke passed: real xterm, desktop/mobile layout, unidentified probe settings, MCP sharing, server-remembered SSH credentials and offline-board flash preview/task recovery (isolated simulated fixtures).');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(ws)ws.close(); await stopBrowser(chrome);server.close();
  await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});
});
