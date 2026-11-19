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
const inspection = {layout_digest:'e'.repeat(64),storages:[{id:'active',label:'NOR',size:before.length,capacity_source:'spacemit-ram-v1',
  verify:true,partitions:parts.map(([name,offset,size])=>({name,offset,size}))}]};
function backup(id, phase, bytes) {
  return {artifact:{id,name:'fixture-nor-'+phase+'.bin',size:bytes.length,sha256:digest(bytes),state:'available'},partitions:parts.map(([name,offset,size])=>({
    name,offset,size,content:{size,sha256:digest(bytes.subarray(offset,offset+size)),format:'fit',
      version_state:['env','bootinfo'].includes(name)?'not-applicable':name==='fsbl'||name==='opensbi'?'identified':'unknown',
      version:name==='fsbl'?'U-Boot SPL 2022.10（模拟）':name==='opensbi'?'OpenSBI v'+(phase==='before'?'1.6':'1.7')+'（模拟）':null,
      description:name==='esos'?'<img src=x onerror=window.norInjected=1>':undefined,
      built_at:!['env','bootinfo'].includes(name)?'2026-11-14T00:00:00+00:00':null}}))};
}
let previewTask = null, previewSubmissions = 0, verificationSkips = 0, preparations = 0, fullWrites = 0;
let current = null, upload = Buffer.alloc(0), artifact = null, preview = null, rejectPreview = true, urlDownloads = 0;
const packageArtifact = {id:'9'.repeat(24),name:'firmware-fixture.tar.gz',size:2<<20,sha256:'a'.repeat(64),state:'ready'};
const urlArtifact = {id:'8'.repeat(24),name:'url-fixture.itb',size:512,sha256:'b'.repeat(64),state:'ready'};
const submissions = [], selections = [], errors = [], targetReads = [];
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  const json = value => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
  res.setHeader('Cache-Control','no-store');
  if(req.method==='POST') {
    let body='';req.on('data',data=>body+=data);req.on('end',()=>{
      try {
        const payload=JSON.parse(body || '{}');
        if(url.pathname==='/api/flash-target') {targetReads.push(payload);return json({target,capabilities:{supported:true,preparable:true,
          nor_workspace_supported:true,storage_inventory_supported:true,binding_revision:1,modes:['full','partitions'],artifacts:[],storages:[],
          executor:{provider:'goflash',node:'local',endpoint:'fixture-only'},console:null}});}
        if(url.pathname==='/api/flash-plan') {
          previewSubmissions++;
          assert.equal(payload.mode,'full');assert.deepEqual(payload.selections,[]);
          assert.ok(payload.request_id);
          const timing={active:true,state:'running',sample_count:0,updated:Date.now()/1000,stages:[
            {phase:'checking-upload',state:'completed',elapsed_seconds:2,estimated_seconds:2},
            {phase:'checking-image-sha256',state:'completed',elapsed_seconds:2,estimated_seconds:2},
            {phase:'expanding-image',state:'running',elapsed_seconds:3,estimated_seconds:6},
            {phase:'checking-payloads',state:'pending',estimated_seconds:4},
            {phase:'checking-layout',state:'pending',estimated_seconds:5},
            {phase:'reading-payload-metadata',state:'pending',estimated_seconds:3}]};
          previewTask={id:'browser-preview',kind:'flash-preview',state:'running',request_id:payload.request_id,
            flash:{preview_intent:{artifact_id:packageArtifact.id,mode:'full',verify:'none'},preview_source:packageArtifact,preview_progress:timing},
            progress:{phase:'expanding-image',done:1024,total:2048}};
          setTimeout(()=>{previewTask={...previewTask,state:'completed',flash:{...previewTask.flash,
            preview_progress:{...timing,active:false,state:'completed',stages:timing.stages.map(step=>({...step,state:'completed',elapsed_seconds:step.estimated_seconds}))}},result:{plan:{id:'full-package-preview',mode:'full',requires_prepare:true,ready:false,confirm_after_prepare:true,write_layout:true,
            writes:parts.map(([name,offset,size])=>({storage:'active',partition:name,offset,size,capacity:size})),
            erase_ranges:[{storage:'active',offset:0,size:before.length}],warnings:['模拟整包恢复会擦除 env']}}};},4000);
          res.statusCode=202;return json({task:previewTask});
        }
        if(url.pathname==='/api/image-download') {urlDownloads++;return json({task:{id:'url-fixture-download',state:'running'}});}
        if(url.pathname==='/api/task-start' && payload.kind==='flash') {
          assert.equal(payload.prepare_for_review,true);
          assert.equal(payload.plan_id,'full-package-preview');
          preparations++;
          current={id:'full-task',kind:'flash',state:'running',progress:{phase:'prepare'},
            flash:{request_id:payload.request_id,plan:previewTask.result.plan,write_result:'not-started'}};
          setTimeout(()=>{
            current.progress={phase:'awaiting-selection'};
            Object.assign(current.flash,{confirmation_mode:'full',inspection,
              prepared_plan:{...previewTask.result.plan,id:'checked-full-plan',requires_prepare:false,ready:true,verify:'none'}});
          },1200);
          return json({task:current});
        }
        if(url.pathname==='/api/task-start') {
          assert.equal(payload.kind,'nor-backup');assert.equal('artifact_id' in payload,false);assert.equal('plan_id' in payload,false);
          assert.equal(payload.read_system,true); assert.equal(payload.temporary_snapshot,true);
          submissions.push(payload);current={id:taskId,kind:'flash',state:'running',flash_targets:[{target_id:target.target_id,generation:target.target_generation}],
            progress:{phase:'preparing-loader',done:2,total:4},flash:{operation:'nor-workspace',temporary_snapshot:true,request_id:payload.request_id,write_result:'not-started',verify_result:'not-started'}};
          return json({task:current});
        }
        if(url.pathname==='/api/artifact-create') {assert.equal(payload.task_id,taskId);return json({artifact:{id:artifactId,size:payload.size}});}
        if(url.pathname==='/api/artifact-append') {assert.equal(payload.offset,upload.length);upload=Buffer.concat([upload,Buffer.from(payload.data_b64,'base64')]);return json({artifact:{id:artifactId,received:upload.length}});}
        if(url.pathname==='/api/artifact-seal') {artifact={id:artifactId,name:'opensbi-fixture.itb',size:upload.length,sha256:digest(upload),state:'ready'};return json({artifact});}
        if(url.pathname==='/api/flash-selection' && payload.id==='full-task') {
          assert.equal(payload.preview,undefined);
          assert.equal(payload.plan_id,'checked-full-plan');
          assert.equal(payload.layout_digest,inspection.layout_digest);
          assert.ok(payload.request_id); fullWrites++;
          current={...current,state:'completed',progress:{phase:'completed'},result:{write_result:'completed'}};
          return json({task:current});
        }
        if(url.pathname==='/api/flash-selection') {
          if(payload.action==='skip-verification') {
            assert.equal(payload.id,current.id);assert.equal(current.progress.phase,'verifying-nor');
            verificationSkips++;current.state='completed';
            current.flash.verify_skip_requested=true;current.flash.verify_result='skipped';
            current.result={write_result:'completed',verify_result:'skipped',boot_result:'not-requested'};
            return json({task:current});
          }
          assert.equal(payload.id,taskId);assert.equal(payload.target_generation,target.target_generation);
          if(payload.action==='boot-priority') {
            assert.equal(payload.expected,'SSD');assert.equal(payload.value,'UFS');assert.equal('selections' in payload,false);
            current.flash.storage_inventory.priority_source='eeprom:UFS';
            current.flash.boot_priority_change={state:'completed',verify_result:'verified',value:'UFS'};
            return json({task:current,change:current.flash.boot_priority_change});
          }
          selections.push(payload);
          if(payload.preview) {
            if(rejectPreview) {rejectPreview=false;res.statusCode=409;return json({error:'模拟预览错误：分区载荷不匹配'});}
            assert.deepEqual(payload.selections,[{storage:'active',partition:'opensbi',artifact_id:artifactId}]);
            preview={id:'f'.repeat(24),write_layout:false,ready:true,digest:'6'.repeat(64),writes:[{storage:'active',partition:'opensbi',kind:'partition',
              size:upload.length,write_size:512,offset:1769472,capacity:393216,sha256:digest(upload),write_sha256:digest(Buffer.concat([upload,Buffer.alloc(512-upload.length)]))}],
              erase_ranges:[{storage:'active',offset:1769472,size:393216}],payload_metadata:[{storage:'active',partition:'opensbi',sha256:digest(upload),
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
  if(url.pathname==='/api/flash-discover') return json({targets:[target]});
  if(url.pathname==='/api/flash-capabilities' && url.searchParams.has('serial') && url.searchParams.get('serial')!==target.serial) {
    res.statusCode=403;return json({error:'USB flash target board identity changed'});
  }
  if(url.pathname==='/api/flash-capabilities') return json({capabilities:{supported:true,preparable:true,nor_workspace_supported:true,storage_inventory_supported:true,binding_revision:1,modes:['full','partitions'],artifacts:[packageArtifact],storages:[]}});
  if(url.pathname==='/api/task' && previewTask && url.searchParams.get('request_id')===previewTask.request_id) return json({task:previewTask});
  if(url.pathname==='/api/task' && url.searchParams.get('id')==='browser-preview') return json({task:previewTask});
  if(url.pathname==='/api/task') return json({task:url.searchParams.get('id')==='url-fixture-download'
    ? {id:'url-fixture-download',state:'completed',result:{artifact:urlArtifact}} : current});
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
  assert.equal(await evaluate('document.querySelector("#fl-mode").value'),'full');
  assert.equal(await evaluate('document.querySelector("#fl-mode").closest("details")'),null);
  assert.equal(await evaluate('document.querySelector("#fl-read-nor").hidden'),false);
  assert.equal(await evaluate('document.querySelector("#fl-read-nor").textContent'),'读取设备存储');
  await shot('full-package-connect-light-fixture');
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate(fits));await shot('full-package-connect-mobile-fixture');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  await evaluate('document.querySelector("#fl-store").click();window.fixtureChooseFile('+JSON.stringify(packageArtifact)+');document.querySelector("#fl-plan").click()');
  await wait('document.querySelector("#fl-progress-text").textContent.includes("解压镜像")');
  assert.ok(await evaluate('document.querySelector("#fl-progress-text").textContent.includes("50%")'));
  assert.equal(await evaluate('document.querySelectorAll("#fl-preview-stages li").length'),6);
  assert.ok(await evaluate('document.querySelector("#fl-overall-text").textContent.includes("初步估算剩余")'));
  await evaluate('document.querySelector("#fl-close").click();ui.open('+JSON.stringify(target)+')');
  assert.equal(previewSubmissions,1);
  await shot('image-preview-progress-fixture');
  await rpc('Page.reload');
  await wait('document.querySelector("#fl-task-id") && document.querySelector("#fl-task-id").textContent.includes("browser-preview")');
  await wait('document.querySelector("#fl-preview-phase").textContent.includes("准备设备并核对")');
  assert.equal(await evaluate('document.querySelector("#flash-dialog").dataset.stage'),'setup');
  await wait('document.querySelector("#fl-select").checkVisibility() && document.querySelector("#fl-select").textContent.includes("并刷写")');
  assert.equal(previewSubmissions,1);assert.equal(preparations,1);assert.equal(fullWrites,0);
  assert.equal(await evaluate('document.querySelector("#flash-dialog").dataset.stage'),'review');
  assert.ok(await evaluate('document.querySelector("#fl-preview").textContent.includes("模拟整包恢复会擦除 env")'));
  await shot('full-package-preview-light-fixture');
  await evaluate('document.querySelector("#fl-select").click()');
  await wait('document.querySelector("#flash-dialog").dataset.stage==="run"');
  assert.equal(fullWrites,1);
  // Begin the independent backup scenario on an idle simulated controller.
  current=null;previewTask=null;
  await evaluate('sessionStorage.clear()');await rpc('Page.reload');
  await wait('document.querySelector("#fl-read-nor")?.disabled===false');
  await evaluate('document.querySelector("#fl-url-toggle").click();(()=>{const d=new DataTransfer();d.setData("text","https://images.example/url-fixture.itb");document.querySelector("#fl-url").dispatchEvent(new ClipboardEvent("paste",{clipboardData:d,bubbles:true,cancelable:true}));})()');
  await wait('document.querySelector("#fl-source-summary").textContent.includes("url-fixture.itb")');
  await evaluate('document.querySelector("#fl-url").dispatchEvent(new Event("change"))');await delay(100);
  assert.equal(urlDownloads,1);assert.equal(await evaluate('document.querySelector("#fl-download").hidden'),true);
  await evaluate('ui.open('+JSON.stringify(target)+')');
  await wait('document.querySelector("#fl-read-nor").disabled===false && document.querySelector("#fl-mode").value==="partitions"');
  await evaluate('document.querySelector("#fl-mode").value="partitions";document.querySelector("#fl-mode").dispatchEvent(new Event("change"))');
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
  assert.equal(await evaluate('document.querySelector("#flash-dialog").dataset.stage'),'setup');
  assert.ok(await evaluate('document.querySelector("#fl-storage-details").open && !document.querySelector("#fl-execution").checkVisibility()'));
  assert.ok(await evaluate('document.querySelector("#fl-read-status").textContent.includes("2 / 4")'));
  current.progress={phase:'awaiting-selection'};current.flash.inspection=inspection;current.flash.nor_before=backup('1'.repeat(24),'before',before);current.flash.selection_deadline=Date.now()/1000+3600;
  current.flash.resolved_serial='BOARD-SAMPLE-01-FIXTURE';current.flash.request_serial='';
  current.flash.storage_inventory={boot_priority_editable:true,default_storage:'nvme:0',priority_source:'eeprom:SSD',full_backup:false,metadata_bytes_read:131072,
    storages:[{id:'nvme:0',size:128*1024**3,partitions:[{name:'bootfs',offset:1048576,size:512*1024**2,
      content:{filesystem:{type:'fat32'},version:'Linux 6.6.63（模拟）',version_state:'identified'}},
      {name:'rootfs',offset:1024**3,size:120*1024**3,content:{filesystem:{type:'ext4'},version:'Debian 13（模拟）',version_state:'identified'}}]}]};
  await wait('document.querySelectorAll(".fl-partition").length===6');
  assert.equal(await evaluate('document.querySelector("#fl-system-storage").value'),'nvme:0');
  assert.ok(await evaluate('document.querySelector("#fl-system-parts").textContent.includes("Debian 13")'));
  assert.ok(await evaluate('document.querySelector("#fl-system").closest("section").contains(document.querySelector("#fl-device"))'));
  if (!await evaluate('document.querySelector("#fl-storage-details").open')) await evaluate('document.querySelector("#fl-storage-details > summary").click()');
  assert.ok(await evaluate('document.querySelector("#fl-system-parts .fl-map-heading").textContent.includes("板上已有布局")'));
  const destination = await evaluate('document.querySelector("#fl-target-storage").value');
  await evaluate('document.querySelector("#fl-system-storage").value="";document.querySelector("#fl-system-storage").dispatchEvent(new Event("change"))');
  assert.equal(await evaluate('document.querySelector("#fl-target-storage").value'),destination);
  await evaluate('document.querySelector("#fl-system-storage").value="nvme:0";document.querySelector("#fl-system-storage").dispatchEvent(new Event("change"))');
  await evaluate('document.querySelector("#fl-system").scrollIntoView({block:"start"})');
  await evaluate('document.querySelector("#fl-boot-config > summary").click()');
  assert.equal(await evaluate('document.querySelector("#fl-boot-priority").value'),'SSD');
  await evaluate('document.querySelector("#fl-boot-priority").value="UFS";document.querySelector("#fl-boot-priority").dispatchEvent(new Event("change"));document.querySelector("#fl-boot-save").click()');
  await wait('document.querySelector("#fl-boot-status").textContent.includes("已回读校验")');
  assert.equal(await evaluate('document.querySelectorAll(".fl-partition input:checked").length'),0);
  await shot('nor-plus-system-storage-fixture');
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.ok(await evaluate(fits));await shot('nor-plus-system-storage-mobile-fixture');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('document.querySelectorAll(".fl-partition input:checked").length'),0);
  assert.ok(await evaluate('[...document.querySelectorAll(".fl-partition select")].every(s=>s.hidden)'));
  assert.ok(await evaluate('!document.querySelector("#fl-partitions").textContent.includes("保留现有内容")'));
  assert.ok(await evaluate('document.querySelector("#fl-partitions").textContent.includes("版本未知")'));
  const file=path.join(out,'opensbi-fixture.itb');fs.writeFileSync(file,Buffer.from('fixture OpenSBI v1.7\0'));
  const doc=await rpc('DOM.getDocument');const input=await rpc('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#fl-files'});
  await rpc('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[path.resolve(file)]});
  await wait('document.querySelectorAll("#fl-artifact option").length===2');
  await evaluate('(()=>{const row=[...document.querySelectorAll(".fl-partition")].find(r=>r.partitionId==="opensbi");row.chooseFile.click();})()');
  assert.equal(await evaluate('window.fixturePicker.fileOnly'),true);
  await evaluate('window.fixtureChooseFile('+JSON.stringify(artifact)+')');
  assert.equal(await evaluate('document.querySelectorAll(".fl-partition input:checked").length'),1,'file selection checks only its own partition');
  assert.equal(await evaluate('[...document.querySelectorAll(".fl-partition")].find(r=>r.partitionId==="opensbi").artifactSelect.value'),artifactId,'file is bound without a second selection');
  await evaluate('document.querySelector("#fl-partitions-field").scrollIntoView({block:"start"})');
  await shot('nor-partitions-light-fixture');
  const download=await evaluate('fetch(document.querySelector("#fl-backups a").href).then(r=>r.arrayBuffer()).then(b=>b.byteLength)');assert.equal(download,before.length);
  await evaluate('document.querySelector("#fl-select").focus()');
  await rpc('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
  await rpc('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await wait('document.querySelector("#fl-status").textContent.includes("模拟预览错误")');
  await delay(1200);
  assert.ok(await evaluate('document.querySelector("#fl-status").textContent.includes("模拟预览错误")'));
  assert.ok(await evaluate('document.querySelector("#fl-task-status").textContent.includes("确认剩余")'));
  await shot('preview-error-persisted-light-fixture');
  await evaluate('document.querySelector("#fl-select").click()');
  await wait('document.querySelector("#fl-select").textContent.includes("确认分区")');
  assert.equal(selections.length,2);assert.equal(submissions.length,1);
  assert.ok(await evaluate('document.querySelector("#fl-confirm-options #fl-verify").checkVisibility()'));
  await evaluate('(()=>{const v=document.querySelector("#fl-verify");v.value="if-supported";v.dispatchEvent(new Event("change"));})()');
  await wait('document.querySelector("#fl-select").textContent.includes("确认分区") && !document.querySelector("#fl-select").disabled');
  assert.equal(selections.length,3);assert.equal(selections[2].preview,true);assert.equal(selections[2].verify,'if-supported');

  assert.ok(await evaluate('document.querySelector("#fl-keep-field").checkVisibility() && !document.querySelector("#fl-keep-backup").checked'));
  assert.equal(await evaluate('document.querySelector("#fl-backup-files").parentNode.id'),'fl-review-backup');
  await evaluate('document.querySelector("#fl-keep-backup").click()');
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
  assert.equal(selections[3].retain_backup,true); current.flash.selection_request={retain_backup:true};
  assert.equal(selections.length,4);assert.equal(submissions.length,1,'confirmation continues the same task');
  current.progress={phase:'writing',done:80,total:100,storage:'active',partition:'opensbi',partition_done:10,partition_total:20,bytes_per_second:123};
  await wait('document.querySelector("#fl-write-partition").checkVisibility() && document.querySelector("#fl-write-count").textContent.includes("50%")');
  assert.ok(await evaluate('document.querySelector("#fl-write-target").textContent.includes("opensbi") && document.querySelector("#fl-write-count").textContent.includes("123 B/s")'));
  assert.equal(await evaluate('document.querySelector("#fl-progress").value'),80);
  current.progress.done=85;current.progress.partition_done=15;
  await wait('document.querySelector("#fl-partition-progress").value===15 && document.querySelector("#fl-write-count").textContent.includes("75%")');
  await shot('live-partition-progress-fixture');
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
  current={...current,id:'skip-readback-fixture',state:'running',progress:{phase:'verifying-nor',done:4<<20,total:8<<20},
    flash:{...current.flash,can_skip_verification:true,verify_result:'running'},result:null};
  await evaluate('SerialdI18n.setLocale("zh-CN");ui.open('+JSON.stringify(target)+','+JSON.stringify(current)+')');
  await wait('!document.querySelector("#fl-skip-verify").hidden && !document.querySelector("#fl-skip-verify").disabled');
  await evaluate('document.querySelector("#fl-skip-verify").click()');
  await wait('document.querySelector("#fl-results").textContent.includes("用户跳过，校验未完成")');
  assert.equal(verificationSkips,1);assert.equal(submissions.length,1,'skip cannot submit another flash');
  assert.equal(await evaluate('document.querySelector("#fl-skip-verify").hidden'),true);
  await evaluate('document.querySelector(".fl-execution").scrollIntoView({block:"center"})');await shot('readback-skipped-fixture');
  // Restore an ended task, then explicitly detect a new connection at the same
  // USB port. Saved request IDs and history must not restore the old identity.
  current.board_targets=['OLD-SN'];current.flash.request_serial='OLD-SN';
  target.target_generation='new-bootrom-after-flash';target.serial='';
  await evaluate('SerialdI18n.setLocale("zh-CN");ui.open('+JSON.stringify(target)+','+JSON.stringify(current)+')');
  await wait('document.querySelector("#fl-status").textContent.includes("identity changed")');
  const readsBeforeRedetect=targetReads.length,submissionsBeforeRedetect=submissions.length;
  assert.equal(await evaluate('document.querySelector("#fl-new").getClientRects().length'),1);
  await evaluate('document.querySelector("#fl-new").click()');
  await wait('!document.querySelector("#fl-files").disabled');
  assert.equal(targetReads.length,readsBeforeRedetect+1);
  assert.equal(targetReads.at(-1).target_generation,'new-bootrom-after-flash');
  assert.equal(Object.hasOwn(targetReads.at(-1),'serial'),false);
  assert.equal(await evaluate('document.querySelector("#fl-task-status").textContent'),'');
  assert.equal(await evaluate('document.querySelector("#fl-system").hidden'),true,'old storage inventory must not appear on the new connection');
  assert.equal(await evaluate('document.querySelector("#fl-status").textContent.includes("identity changed")'),false);
  assert.equal(await evaluate('document.querySelector("#fl-start").disabled'),true);
  assert.equal(submissions.length,submissionsBeforeRedetect,'redetection cannot submit any read or write task');
  await evaluate('document.querySelector("#flash-dialog").scrollTop=0');await shot('redetect-after-completed-task-fixture');
  // A previous readback count can be larger than the write receipt.
  current={id:'older-readback',request_id:'old-write',kind:'flash',state:'completed',
    progress:{phase:'verifying-system',done:12*2**30,total:12*2**30},
    result:{write_result:'completed',verify_result:'verified',written_bytes:1883586158}};
  await evaluate('ui.open('+JSON.stringify(target)+','+JSON.stringify(current)+')');
  await wait('document.querySelector("#fl-progress-text").textContent.includes("本次写入")');
  assert.equal(await evaluate('document.querySelector("#fl-progress").hidden'),true);
  assert.equal(await evaluate('document.querySelector("#fl-progress-text").textContent.includes("12.0")'),false);
  await evaluate('document.querySelector("#fl-close").click();sessionStorage.setItem("decklab-flash-task:'+target.target_id+'","old-write");ui.open('+JSON.stringify(target)+')');
  await wait('document.querySelector("#flash-dialog").dataset.stage==="setup" && !document.querySelector("#fl-files").disabled');
  assert.equal(await evaluate('document.querySelector("#fl-task-id").textContent'),'');
  assert.equal(submissions.length,submissionsBeforeRedetect);
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({simulated:true,hardware_access:false,unified_storage_read:true,eeprom_priority_edit:true,eeprom_serial_fixture:true,full_package_preview:true,preparations,fullWrites,async_preview_progress:true,pasted_url_downloads:urlDownloads,automatic_file_upload:true,persistent_preview_error:true,submissions:submissions.length,selections:selections.length,
    verification_skips:verificationSkips,redetect_after_completed_task:true,screenshots:fs.readdirSync(out).filter(name=>name.endsWith('.png')),desktop:[1600,1100],mobile:[390,844]},null,2)+'\n');
  console.log('NOR browser workflow passed: full package preview, automatic URL/file imports, persistent errors, image-free read, six partition rows, same-task confirmation, byte progress, backups, desktop/mobile and light/dark (simulated data).');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
