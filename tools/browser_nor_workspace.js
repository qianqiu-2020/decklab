'use strict';
// Real dialog/CSS/JS in Chromium; all USB, NOR bytes and task results are fixtures.
// Screenshots are labelled simulation and are not hardware acceptance evidence.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const crypto = require('node:crypto');
const {launchBrowser, stopBrowser} = require('./browser_launch');
const root = path.resolve(__dirname, '..');
const out = process.argv[2] || path.join(root, 'dist/nor-workspace-20261114/browser');
fs.mkdirSync(out, {recursive: true});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-nor-ui-'));
const source = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
let dialog = source.slice(source.indexOf('<dialog id="flash-dialog"'), source.indexOf('<dialog id="probe-settings-dialog"'));
dialog = dialog.replace('  <div class="fl-head">', '<p style="position:sticky;top:0;z-index:3;background:var(--surface);margin:0;padding:5px 0;color:var(--ink-2);font-size:12px" data-i18n-ignore>SIMULATED · 模拟验收数据，无真实设备操作</p>\n  <div class="fl-head">');
const target = {target_id: 'a'.repeat(24), target_generation: 'b'.repeat(24), label:'浏览器验收 · 模拟 NOR', mode:'bootrom'};
const artifactId = 'c'.repeat(24), taskId = 'd'.repeat(24);
const before = Buffer.alloc(8 << 20, 0xff);
const digest = data => crypto.createHash('sha256').update(data).digest('hex');
const parts = [['bootinfo',0,128<<10],['fsbl',128<<10,512<<10],['env',640<<10,64<<10],
  ['esos',704<<10,1024<<10],['opensbi',1728<<10,384<<10],['uboot',2112<<10,4096<<10]];
const inspection = {layout_digest:'e'.repeat(64),storages:[{id:'active',label:'NOR',size:before.length,capacity_source:'decklab-nor-v1',
  verify:true,partitions:parts.map(([name,offset,size])=>({name,offset,size}))}]};
function backup(id, phase, bytes) {
  return {artifact:{id,name:'fixture-nor-'+phase+'.bin',size:bytes.length,sha256:digest(bytes),state:'available'},partitions:parts.map(([name,offset,size])=>({
    name,offset,size,content:{size,sha256:digest(bytes.subarray(offset,offset+size)),format:'fit',
      version_state:['env','bootinfo'].includes(name)?'not-applicable':name==='fsbl'||name==='opensbi'?'identified':'unknown',
      version:name==='fsbl'?'U-Boot SPL 2022.10（模拟）':name==='opensbi'?'OpenSBI v'+(phase==='before'?'1.6':'1.7')+'（模拟）':null,
      description:name==='esos'?'<img src=x onerror=window.norInjected=1>':undefined,
      built_at:!['env','bootinfo'].includes(name)?'2026-11-14T00:00:00+00:00':null}}))};
}
let current = null, upload = Buffer.alloc(0), artifact = null, preview = null;
const submissions = [], selections = [], errors = [];
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  const json = value => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
  res.setHeader('Cache-Control','no-store');
  if(req.method==='POST') {
    let body='';req.on('data',data=>body+=data);req.on('end',()=>{
      try {
        const payload=JSON.parse(body || '{}');
        if(url.pathname==='/api/flash-target') return json({target,capabilities:{supported:true,preparable:true,
          nor_workspace_supported:true,binding_revision:1,modes:['partitions'],artifacts:[],storages:[],
          executor:{provider:'goflash',node:'local',endpoint:'fixture-only'},console:null}});
        if(url.pathname==='/api/task-start') {
          assert.equal(payload.kind,'nor-backup');assert.equal('artifact_id' in payload,false);assert.equal('plan_id' in payload,false);
          submissions.push(payload);current={id:taskId,kind:'flash',state:'running',flash_targets:[{target_id:target.target_id,generation:target.target_generation}],
            progress:{phase:'preparing-loader',done:2,total:4},flash:{operation:'nor-workspace',request_id:payload.request_id,write_result:'not-started',verify_result:'not-started'}};
          return json({task:current});
        }
        if(url.pathname==='/api/artifact-create') {assert.equal(payload.task_id,taskId);return json({artifact:{id:artifactId,size:payload.size}});}
        if(url.pathname==='/api/artifact-append') {assert.equal(payload.offset,upload.length);upload=Buffer.concat([upload,Buffer.from(payload.data_b64,'base64')]);return json({artifact:{id:artifactId,received:upload.length}});}
        if(url.pathname==='/api/artifact-seal') {artifact={id:artifactId,name:'opensbi-fixture.itb',size:upload.length,sha256:digest(upload),state:'ready'};return json({artifact});}
        if(url.pathname==='/api/flash-selection') {
          assert.equal(payload.id,taskId);assert.equal(payload.target_generation,target.target_generation);selections.push(payload);
          if(payload.preview) {
            assert.deepEqual(payload.selections,[{storage:'active',partition:'opensbi',artifact_id:artifactId}]);
            preview={id:'f'.repeat(24),write_layout:false,ready:true,digest:'6'.repeat(64),writes:[{storage:'active',partition:'opensbi',kind:'partition',
              size:upload.length,write_size:512,offset:1769472,capacity:393216,sha256:digest(upload),write_sha256:digest(Buffer.concat([upload,Buffer.alloc(512-upload.length)]))}],
              erase_ranges:[{storage:'active',offset:1769472,size:393216}],payload_metadata:[{storage:'active',partition:'opensbi',
                content:{version:'OpenSBI v1.7（模拟）',version_state:'identified',size:upload.length,sha256:digest(upload)}}]};
            return json({plan:preview});
          }
          assert.equal(payload.plan_id,preview.id);current.progress={phase:'checking-nor-baseline',done:before.length,total:before.length};
          current.flash.plan=preview;return json({task:current});
        }
        throw new Error('unexpected mutation: '+url.pathname);
      }catch(error){errors.push(error.stack);res.statusCode=500;json({error:error.message});}
    });return;
  }
  if(url.pathname==='/') {
    res.setHeader('Content-Type','text/html');
    return res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/app.css"><body>'+dialog+
      '<script src="/static/i18n.js"></script><script src="/static/tasks.js"></script><script src="/static/flashing.js"></script><script>'+
      'SerialdI18n.setLocale("zh-CN");window.ui=SerialdFlashing.create({byId:id=>document.getElementById(id),fetch:fetch,translate:SerialdI18n.translate,openImageStore:(choose,picker)=>{window.fixtureChooseFile=choose;window.fixturePicker=picker;},'+
      'formatBytes:n=>n>=1048576?(n/1048576).toFixed(1)+" MiB":n>=1024?(n/1024).toFixed(1)+" KiB":n+" B"});ui.open('+JSON.stringify(target)+');</script></body></html>');
  }
  if(url.pathname==='/api/tasks') return json({tasks:current?[current]:[]});
  if(url.pathname==='/api/task') return json({task:current});
  if(url.pathname==='/api/flash-events') return json({events:[],next_after:0});
  if(url.pathname==='/api/artifact-download') {res.setHeader('Content-Type','application/octet-stream');return res.end(before);}
  if(['/static/app.css','/static/i18n.js','/static/tasks.js','/static/flashing.js'].includes(url.pathname)) {
    res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':'application/javascript');
    return res.end(fs.readFileSync(path.join(root,url.pathname)));
  }
  res.statusCode=404;res.end();
});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let chrome,ws;
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const {chrome:process,port}=await launchBrowser(profile);chrome=process;
  const tab=await(await fetch('http://127.0.0.1:'+port+'/json/new?'+encodeURIComponent('http://127.0.0.1:'+server.address().port),{method:'PUT'})).json();
  ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0;const pending=new Map();
  ws.onmessage=event=>{const msg=JSON.parse(event.data);if(msg.id){const call=pending.get(msg.id);pending.delete(msg.id);msg.error?call.reject(new Error(JSON.stringify(msg.error))):call.resolve(msg.result);}
    else if(msg.method==='Runtime.exceptionThrown')errors.push(JSON.stringify(msg.params.exceptionDetails));};
  const rpc=(method,params={})=>new Promise((resolve,reject)=>{const mid=++id;pending.set(mid,{resolve,reject});ws.send(JSON.stringify({id:mid,method,params}));});
  const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const wait=async expression=>{for(let n=0;n<70;n++){if(await evaluate(expression))return;await delay(100);}const debug=await evaluate('({active:document.activeElement&&document.activeElement.id,progress:document.querySelector("#fl-progress-text").textContent,status:document.querySelector("#fl-status").textContent,disabled:document.querySelector("#fl-read-nor").disabled})');throw new Error('UI wait failed: '+expression+' '+JSON.stringify({debug,submissions,errors}));};
  const shot=async name=>{const image=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(image.data,'base64'));};
  const fits=`(()=>{const d=document.querySelector('#flash-dialog'),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth+1})()`;
  await rpc('Runtime.enable');await rpc('Page.enable');await rpc('Page.bringToFront');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  await rpc('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'light'},{name:'prefers-reduced-motion',value:'reduce'}]});
  await wait('document.querySelector("#fl-read-nor") && !document.querySelector("#fl-read-nor").disabled');
  const contrast=selector=>`(()=>{const s=getComputedStyle(document.querySelector(${JSON.stringify(selector)}));const l=c=>{const a=c.match(/[\\d.]+/g).slice(0,3).map(x=>{x=Number(x)/255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4});return .2126*a[0]+.7152*a[1]+.0722*a[2]};const a=l(s.color),b=l(s.backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05)})()`;
  assert.ok(await evaluate(contrast('#fl-read-nor'))>=4.5,'light primary button contrast');
  assert.ok(await evaluate(contrast('#fl-url'))>=4.5,'light input contrast');
  assert.ok(await evaluate(fits));
  assert.equal(await evaluate('document.querySelectorAll(".fl-steps,#fl-intro,#fl-read-note").length'),0);
  await shot('nor-connect-light-fixture');
  await evaluate('document.querySelector("#fl-read-nor").focus()');
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await wait('document.querySelector("#fl-progress-text").textContent.includes("2 / 4")');
  assert.equal(submissions.length,1);
  current.progress={phase:'awaiting-selection'};current.flash.inspection=inspection;current.flash.nor_before=backup('1'.repeat(24),'before',before);
  await wait('document.querySelectorAll(".fl-partition").length===6');
  assert.equal(await evaluate('document.querySelectorAll(".fl-partition input:checked").length'),0);
  assert.ok(await evaluate('[...document.querySelectorAll(".fl-partition select")].every(s=>s.hidden)'));
  assert.ok(await evaluate('!document.querySelector("#fl-partitions").textContent.includes("保留现有内容")'));
  assert.ok(await evaluate('document.querySelector("#fl-partitions").textContent.includes("版本未知")'));
  const file=path.join(out,'opensbi-fixture.itb');fs.writeFileSync(file,Buffer.from('fixture OpenSBI v1.7\0'));
  const doc=await rpc('DOM.getDocument');const input=await rpc('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#fl-files'});
  await rpc('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[path.resolve(file)]});
  await evaluate('document.querySelector("#fl-upload").click()');
  await wait('document.querySelectorAll("#fl-artifact option").length===2');
  await evaluate('(()=>{const row=[...document.querySelectorAll(".fl-partition")].find(r=>r.partitionId==="opensbi");row.chooseFile.click();})()');
  assert.equal(await evaluate('window.fixturePicker.fileOnly'),true);
  await evaluate('window.fixtureChooseFile('+JSON.stringify(artifact)+')');
  assert.equal(await evaluate('document.querySelectorAll(".fl-partition input:checked").length'),1,'file selection checks only its own partition');
  assert.equal(await evaluate('[...document.querySelectorAll(".fl-partition")].find(r=>r.partitionId==="opensbi").artifactSelect.value'),artifactId,'file is bound without a second selection');
  await evaluate('document.querySelector("#fl-sources").open=false;document.querySelector("#fl-partitions-field").scrollIntoView({block:"start"})');
  await shot('nor-partitions-light-fixture');
  const download=await evaluate('fetch(document.querySelector("#fl-backups a").href).then(r=>r.arrayBuffer()).then(b=>b.byteLength)');assert.equal(download,before.length);
  await evaluate('document.querySelector("#fl-select").focus()');
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await wait('document.querySelector("#fl-select").textContent.includes("确认分区")');
  assert.equal(selections.length,1);assert.equal(submissions.length,1);
  assert.ok(await evaluate('document.querySelector("#fl-partitions").textContent.includes("OpenSBI v1.7")'));
  await rpc('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'dark'}]});
  assert.ok(await evaluate(contrast('#fl-select'))>=4.5,'dark primary button contrast');
  assert.ok(await evaluate(contrast('#fl-url'))>=4.5,'dark input contrast');
  assert.ok(await evaluate('globalThis.norInjected===undefined && !document.querySelector(".fl-content img")'),'firmware metadata remains text');
  await evaluate('document.querySelector("#fl-partitions-field").scrollIntoView({block:"start"})');await shot('nor-partitions-dark-fixture');
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate(fits),'mobile dialog must not overflow');
  await evaluate('(()=>{const d=document.querySelector("#flash-dialog");[...document.querySelectorAll(".fl-partition")].find(r=>r.partitionId==="opensbi").scrollIntoView({block:"start"});d.scrollTop-=45})()');await shot('nor-partitions-mobile-fixture');
  assert.equal(await evaluate('getComputedStyle(document.querySelector(".fl-partition")).gridTemplateColumns.split(" ").length'),1);
  assert.ok(await evaluate('(()=>{const t=document.querySelector("#fl-preview table");return t.scrollWidth<=t.clientWidth+1})()'),'mobile selected write scope fits without horizontal scrolling');
  await evaluate('document.querySelector("#fl-select").click()');await wait('document.querySelector("#fl-progress-text").textContent.includes("检查备份")');
  assert.equal(selections.length,2);assert.equal(submissions.length,1,'confirmation continues the same task');
  current.progress={phase:'verifying-nor',done:4<<20,total:8<<20};current.flash.write_result='completed';
  await wait('document.querySelector("#fl-progress-text").textContent.includes("50%")');
  await evaluate('document.querySelector(".fl-execution").scrollIntoView({block:"center"})');await shot('nor-progress-mobile-fixture');
  const after=Buffer.from(before);upload.copy(after,1769472);
  current.state='completed';current.progress.done=8<<20;current.flash.verify_result='verified';current.flash.readback_bytes=8<<20;current.flash.verified_bytes=(8<<20)-393216+512;current.flash.verification_scope='payloads-and-outside-selected-partitions';current.flash.nor_after=backup('2'.repeat(24),'after',after);
  await wait('document.querySelectorAll("#fl-backups a").length===2');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector(".fl-execution").scrollIntoView({block:"center"})');await shot('nor-results-dark-fixture');
  await evaluate('SerialdI18n.setLocale("en");document.querySelector("#flash-dialog").scrollTop=0');await delay(100);await shot('nor-results-english-fixture');
  assert.equal(await evaluate('document.querySelector("#fl-title").textContent'),'Flashing workspace');
  assert.ok(await evaluate('[...document.querySelectorAll(".fl-partition")].every(r=>r.artifactSelect.hidden===!r.checkbox.checked)'),'language changes preserve selection visibility');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({simulated:true,hardware_access:false,submissions:submissions.length,selections:selections.length,
    screenshots:fs.readdirSync(out).filter(name=>name.endsWith('.png')),desktop:[1600,1100],mobile:[390,844]},null,2)+'\n');
  console.log('NOR browser workflow passed: image-free read, six partition rows, upload, scope preview, same-task confirmation, real byte progress, backups, desktop/mobile and light/dark (simulated data).');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
