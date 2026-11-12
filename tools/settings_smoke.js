'use strict';
// Real settings UI and HTTP control process, isolated storage and no collector.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {launchBrowser} = require('./browser_launch');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'seriald-settings-ui-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let control, chrome, ws;
(async () => {
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  control = spawn('python3', ['-m', 'seriald_core.control_server', String(port)], {cwd:root,
    env:{...process.env, SERIALD_BASE:temporary, SERIALD_MCP_ENABLED:'0', SERIALD_AUTH_TOKEN:''}, stdio:['ignore','ignore','pipe']});
  let diagnostics = ''; control.stderr.on('data', chunk => {diagnostics = (diagnostics + chunk).slice(-4096);});
  const address = 'http://127.0.0.1:' + port;
  for (let n = 0; n < 100; n++) {
    if (control.exitCode != null) throw new Error(diagnostics);
    try {if ((await fetch(address + '/healthz')).ok) break;} catch (_) {}
    await delay(50);
  }
  const launched = await launchBrowser(path.join(temporary, 'chrome'));
  chrome = launched.chrome;
  const tab = await (await fetch('http://127.0.0.1:' + launched.port + '/json/new?' + encodeURIComponent(address), {method:'PUT'})).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => {ws.onopen = resolve; ws.onerror = reject;});
  let sequence = 0; const pending = new Map(), errors = [];
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id) {const call = pending.get(message.id); pending.delete(message.id); message.error ? call.reject(message.error) : call.resolve(message.result);}
    else if (message.method === 'Runtime.consoleAPICalled') console.log('page:',message.params.args.map(a=>a.value).join(' '));
    else if (message.method === 'Page.javascriptDialogOpening') console.log('dialog:',message.params.message);
    else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
  };
  const rpc = (method, params = {}) => new Promise((resolve,reject) => {const id = ++sequence; const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method+' '+String(params.expression||'').slice(0,100)));},8000);pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}}); ws.send(JSON.stringify({id,method,params}));});
  const evaluate = async expression => {const result = await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true}); if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value;};
  const wait = async expression => {for (let n = 0; n < 100; n++) {if (await evaluate(expression)) return; await delay(50);} throw new Error('UI condition timed out: ' + expression + ' status=' + await evaluate('document.querySelector(".settings-status")?.textContent'));};
  console.log('Settings browser: connected');
  await rpc('Runtime.enable'); await rpc('Page.enable');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1360,height:1000,deviceScaleFactor:1,mobile:false});
  await wait('!!window.SerialdSettings');
  await evaluate('SerialdI18n.setLocale("zh-CN")');
  console.log('Settings browser: opening form');
  await evaluate('document.querySelector("#b-settings").click()');
  await wait('!!document.querySelector(".settings-dialog[open]")');
  console.log('Settings browser: checking form');
  assert.equal(await evaluate('document.querySelectorAll(".settings-grid label").length'),45);
  assert.equal(await evaluate('Array.from(document.querySelectorAll(".settings-body input[type=password]")).every(input => !input.value && input.placeholder.includes("已设置"))'), false); // Empty primary credential has no saved secret.
  assert.equal(await evaluate('Array.from(document.querySelectorAll(".settings-grid label")).find(label => label.querySelector("small").textContent === "SERIALD_SSH_PASSWORD").querySelector("input").value'), '');
  await evaluate(`(() => {const label=Array.from(document.querySelectorAll('.settings-grid label')).find(label=>label.querySelector('small').textContent==='SERIALD_BAUD');const input=label.querySelector('input');input.value='9600';input.dispatchEvent(new Event('input'));Array.from(document.querySelectorAll('.settings-actions button')).find(b=>b.textContent==='预览修改').click();})()`);
  await wait('document.querySelector(".settings-status").textContent.startsWith("校验通过。")');
  await evaluate(`Array.from(document.querySelectorAll('.settings-actions button')).find(b=>b.textContent==='保存配置').click()`);
  await wait('document.querySelector(".settings-status").textContent.includes("配置已保存")');
  assert.equal(JSON.parse(fs.readFileSync(path.join(temporary, 'web-settings.json'))).environment.BAUD,'9600');
  const share = await (await fetch(address + '/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'export',include_secrets:false})})).json();
  assert.deepEqual(share.configuration.environment.SSH_PASSWORD,{$redacted:true});
  await evaluate(`(() => {const pack={format:'decklab-settings',version:1,configuration:{version:1,environment:{BAUD:'19200'}},browser:{language:'zh-CN',deviceOrder:{version:1,ids:['board:fixture-a','board:fixture-b']}}};const file=new File([JSON.stringify(pack)],'preferences.json',{type:'application/json'});const transfer=new DataTransfer();transfer.items.add(file);const input=document.querySelector('.settings-dialog input[type=file]');input.files=transfer.files;input.dispatchEvent(new Event('change'));})()`);
  await wait('document.querySelector(".settings-status").textContent.includes("导入校验通过")');
  assert.equal(JSON.parse(fs.readFileSync(path.join(temporary, 'web-settings.json'))).environment.BAUD,'9600','import must not save automatically');
  await evaluate(`document.querySelector('.settings-footer input[type=checkbox]').click();Array.from(document.querySelectorAll('.settings-actions button')).find(b=>b.textContent==='预览修改').click()`);
  await wait('document.querySelector(".settings-status").textContent.startsWith("校验通过。")');
  await evaluate(`Array.from(document.querySelectorAll('.settings-actions button')).find(b=>b.textContent==='保存配置').click()`);
  await wait('document.querySelector(".settings-status").textContent.includes("配置已保存")');
  assert.equal(await evaluate('JSON.parse(localStorage.getItem("decklab.device-order.v1")).ids[0]'),'board:fixture-a');
  assert.equal(JSON.parse(fs.readFileSync(path.join(temporary, 'web-settings.json'))).environment.BAUD,'19200');
  const output = path.join(root,'dist/settings-ui'); fs.mkdirSync(output,{recursive:true});
  for (const [name,width,height] of [['desktop',1360,1000],['mobile',390,844]]) {
    await rpc('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<600});
    const dimensions = await evaluate(`(() => {const dialog=document.querySelector('.settings-dialog');return {width:dialog.getBoundingClientRect().width,scroll:dialog.scrollWidth,client:dialog.clientWidth}})()`);
    assert.ok(dimensions.width <= width && dimensions.scroll <= dimensions.client + 1, JSON.stringify(dimensions));
    const shot = await rpc('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));
  }
  assert.deepEqual(errors,[]);
  console.log('Settings browser checks passed: real control API, private storage, masked credentials, preview/save/import, personal order, desktop/mobile. No collector or board access.');
})().catch(error => {console.error(error);process.exitCode=1;}).finally(async () => {
  if (ws) ws.close();
  for (const child of [chrome,control]) {if (child && child.exitCode == null) {child.kill('SIGTERM');await Promise.race([new Promise(resolve=>child.once('exit',resolve)),delay(2000)]);if(child.exitCode==null)child.kill('SIGKILL');}}
  fs.rmSync(temporary,{recursive:true,force:true});
});
