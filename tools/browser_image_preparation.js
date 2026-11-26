'use strict';
// Real Chromium UI with isolated HTTP fixtures. No USB, serial or flashing.
const assert=require('node:assert/strict'), fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http');
const {launchBrowser,stopBrowser}=require('./browser_launch');
const root=path.resolve(__dirname,'..'),out=path.resolve(process.argv[2]||'build/image-preparation-browser');fs.mkdirSync(out,{recursive:true});
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'decklab-image-ui-'));
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const dialog=html.slice(html.indexOf('<dialog id="flash-dialog"'),html.indexOf('<dialog id="probe-settings-dialog"'));
const artifact={id:'browser-image',name:'Debian-fixture.tar.gz',size:4e9,sha256:'a'.repeat(64),state:'ready'};
let target={target_id:'board-a',target_generation:'bootrom-a',serial:'A',label:'模拟板卡 A'}, preparations=0,probe=0,phase='running',errors=[];
function prepareTask(){return {id:'prepare-original',kind:'image-prepare',state:phase,error:phase==='failed'?'镜像格式或包内布局无效（诊断 abc123）: image-invalid':'',progress:{phase:'checking-payloads',done:6e9,total:12e9},
 flash:{preview_progress:{active:phase==='running',state:phase,sample_count:0,updated:Date.now()/1000,stages:[
 {phase:'checking-image-sha256',state:'completed',elapsed_seconds:8,estimated_seconds:8},
 {phase:'expanding-image',state:'completed',elapsed_seconds:80,estimated_seconds:80},
 {phase:'checking-payloads',state:phase==='running'?'running':'completed',elapsed_seconds:15,estimated_seconds:30},
 {phase:'reading-payload-metadata',state:phase==='running'?'pending':'completed',elapsed_seconds:1,estimated_seconds:3}]}},
 result:phase==='completed'?{preparation:{layout_version:1,chip_families:['k1'],catalogue:[
   {name:'partition_universal.json',format:'gpt',selected:true,partitions:[
     {name:'ESP',offset:1048576,size:272629760,payload:'esp.vfat',payload_size:272629760},
     {name:'rootfs',offset:273678336,remaining:true,payload:'rootfs.ext4',payload_size:12e9}]},
   ...['partition_nor.json','partition_4M.json'].map(name=>({name,format:'mtd',selected:false,
     partitions:[{name:'opensbi',offset:0,size:393216,payload:'long-directory-name-that-cannot-fit-narrow-columns/fw_dynamic.itb',
       available:true,sha256:'b'.repeat(64),payload_size:270000}]})),
   {name:'partition_flash.json',format:'gpt',selected:false,partitions:[]}],
   payload_metadata:[{partition:'rootfs',content:{size:12e9,version:'Debian GNU/Linux 13（模拟）',version_state:'identified',filesystem:{type:'ext4',label:'rootfs'}}},
     {partition:'opensbi',sha256:'b'.repeat(64),content:{size:270000,version:'OpenSBI v1.6 '+ 'a'.repeat(72)}}],
   firmware_versions:[{name:'OpenSBI',member:'long-directory-name-that-cannot-fit-narrow-columns/fw_dynamic.itb',sha256:'b'.repeat(64),version:18,cab_digests:['c'.repeat(64)]}]}}:null};}
const caps=()=>({supported:true,chip:'k1',image_preparation_supported:true,profile:'fixture',modes:['full','partitions'],artifacts:[artifact],storages:[]});
const server=http.createServer((req,res)=>{const url=new URL(req.url,'http://localhost');res.setHeader('Cache-Control','no-store');
 const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
 if(req.method==='POST'){let body='';req.on('data',d=>body+=d);req.on('end',()=>{try{const p=JSON.parse(body||'{}');
  if(url.pathname==='/api/flash-target'){probe++;return json({target,capabilities:caps()});}
  if(url.pathname==='/api/image-prepare'){preparations++;assert.equal(p.id,artifact.id);assert.equal(p.serial,undefined);return json({task:prepareTask()});}
  throw new Error('unexpected mutation '+url.pathname);
 }catch(e){errors.push(e.stack);res.statusCode=500;json({error:e.message});}});return;}
 if(url.pathname==='/') {res.setHeader('Content-Type','text/html');return res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/app.css"><body>'+dialog+'<p>SIMULATED · 模拟镜像与板卡，无真实设备操作</p><script src="/static/ui-common.js"></script><script src="/static/tasks.js"></script><script src="/static/flashing.js"></script><script>window.ui=DeckLabFlashing.create({byId:id=>document.getElementById(id),fetch:fetch,copyText:DeckLabUI.copyText,openImageStore:choose=>choose('+JSON.stringify(artifact)+'),formatBytes:n=>(n/1e9).toFixed(1)+" GB"});ui.open('+JSON.stringify(target)+');setInterval(()=>ui.refreshUSB(),500);</script></body></html>');}
 if(url.pathname==='/api/tasks')return json({tasks:[]});
 if(url.pathname==='/api/flash-discover')return json({targets:[target]});
 if(url.pathname==='/api/task')return json({task:prepareTask()});
 if(['/static/app.css','/static/ui-common.js','/static/tasks.js','/static/flashing.js'].includes(url.pathname)){res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':'application/javascript');return res.end(fs.readFileSync(path.join(root,url.pathname)));}
 res.statusCode=404;res.end();
});
let chrome,ws;const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const launched=await launchBrowser(profile);chrome=launched.chrome;
 const tab=await(await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+encodeURIComponent('http://127.0.0.1:'+server.address().port),{method:'PUT'})).json();
 ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});let id=0;const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(JSON.stringify(m.params));};
 const rpc=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await delay(100);}throw new Error('UI wait failed '+expression+' '+JSON.stringify({errors,preparations,probe,progress:await evaluate('document.querySelector("#fl-image-preparation").textContent'),status:await evaluate('document.querySelector("#fl-status").textContent')}));};
 const shot=async name=>{const s=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(s.data,'base64'));};
 await rpc('Runtime.enable');await rpc('Page.enable');await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await wait('document.querySelector("#fl-artifact option[value=browser-image]") && !document.querySelector("#fl-cache").disabled');
 await evaluate('document.querySelector("#fl-cache").click()');
 await wait('document.querySelector("#fl-image-preparation-bytes").textContent.includes("6.0 GB / 12.0 GB")');
 assert.equal(await evaluate('document.querySelectorAll("#fl-image-preparation progress").length'),1);
 assert.equal(await evaluate('document.querySelector("#fl-image-stages [aria-current=step]").textContent'),'校验包内文件');
 assert.equal(await evaluate('document.querySelector("#fl-image-overall-text").textContent.startsWith("总体 · ")'),true);
 assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),true);await shot('preparing-simulated');
 await evaluate('document.querySelector("#fl-close").click();ui.open()');
 assert.equal(preparations,1,'closing must not restart image preparation');
 target={...target,target_generation:'bootrom-a-reconnected'};
 for(let n=0;n<100 && probe<2;n++)await delay(100);
 assert.ok(probe>=2,'reconnect must automatically probe the new generation');
 await wait('!document.querySelector("#fl-cache").disabled && document.querySelector("#fl-artifact").value==="browser-image"');
 assert.equal(preparations,1);assert.equal(await evaluate('document.querySelector("#fl-start").disabled'),true);
 target={target_id:'board-b',target_generation:'bootrom-b',serial:'B',label:'模拟板卡 B'};
 await wait('document.querySelector("#fl-device option[value=board-b]") && !document.querySelector("#fl-device").value');
 await evaluate('document.querySelector("#fl-device").value="board-b";document.querySelector("#fl-device").dispatchEvent(new Event("change"))');
 await wait('document.querySelector("#fl-board").textContent.includes("模拟板卡 B") && !document.querySelector("#fl-cache").disabled');
 assert.equal(preparations,1);assert.equal(await evaluate('document.querySelector("#fl-artifact").value'),artifact.id);
 phase='completed';await wait('document.querySelector("#fl-image-information").textContent.includes("Debian GNU/Linux 13")');
 assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),false);
 assert.equal(await evaluate('document.querySelector("#fl-image-information-details").open'),false);
 await evaluate('document.querySelector("#fl-image-information-details summary").click()');
 assert.equal(await evaluate('document.querySelector("#fl-image-information-details").closest("section.fl-section").getAttribute("aria-labelledby")'),'fl-image-title');
 assert.equal(await evaluate('document.querySelector("#fl-system").hidden'),true,'prepared package does not provide board readings');
 assert.equal(await evaluate('document.querySelector("#fl-board").textContent.includes("K1")'),true);
 assert.equal(await evaluate('document.querySelectorAll("#fl-image-information .fl-map").length'),2);
 assert.equal(await evaluate('document.querySelector("#fl-image-information").textContent.includes("partition_flash.json")'),false);
 assert.equal(await evaluate('document.querySelector("#fl-image-information .fl-image-table td:last-child").textContent.includes("CAB 版本 18")'),true);
 const stageFits='document.querySelector("#fl-stage-body").scrollWidth <= document.querySelector("#fl-stage-body").clientWidth+1';
 await evaluate('document.querySelector("#fl-image-information td details").open=true');
 assert.equal(await evaluate(stageFits),true,'long version, payload and CAB digest fit inside the wide preparation form');
 await evaluate('(()=>{const s=document.querySelector("#fl-image-information select");s.value="1";s.dispatchEvent(new Event("change"));})()');
 assert.equal(await evaluate('document.querySelector("#fl-image-information select").selectedOptions[0].textContent'),'partition_4M.json');
 await shot('prepared-simulated');
 await rpc('Page.reload');await wait('document.querySelector("#fl-image-information").textContent.includes("ext4")');
 assert.equal(preparations,2,'refresh resolves the existing task through server dedup');assert.equal(await evaluate('document.querySelector("#fl-artifact").value'),artifact.id);
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await evaluate('document.querySelector("#fl-image-information-details").open=true;document.querySelector("#fl-image-information td details").open=true');
 assert.equal(await evaluate(stageFits),true,'expanded NOR and CAB details fit on a phone');
 assert.equal(await evaluate('document.querySelector("#flash-dialog").scrollWidth<=document.querySelector("#flash-dialog").clientWidth+1'),true);await shot('prepared-mobile-simulated');
 phase='failed';
 await rpc('Page.reload');
 await wait('document.querySelector("#fl-image-error-copy").checkVisibility()');
 const error=await evaluate('document.querySelector("#fl-image-preparation-status").textContent');
 assert.match(error,/abc123/);
 await evaluate(`{
   const selection = getSelection();
   selection.selectAllChildren(document.querySelector('#fl-image-preparation-status'));
   document.querySelector('#fl-reset-after').dispatchEvent(new Event('change'));
 }`);
 assert.equal(await evaluate('getSelection().toString()'),error,'a controls refresh must preserve selected error text');
 await rpc('Browser.grantPermissions',{origin:'http://127.0.0.1:'+server.address().port,permissions:['clipboardReadWrite','clipboardSanitizedWrite']});
 const button=await evaluate(`(() => {
   const b = document.querySelector('#fl-image-error-copy');
   b.scrollIntoView({block:'center'});
   const r = b.getBoundingClientRect();
   return {x:r.x+r.width/2,y:r.y+r.height/2};
 })()`);
 await rpc('Input.dispatchMouseEvent',{type:'mousePressed',...button,button:'left',clickCount:1});
 await rpc('Input.dispatchMouseEvent',{type:'mouseReleased',...button,button:'left',clickCount:1});
 await wait('document.querySelector("#fl-status").textContent==="错误详情已复制"');
 assert.equal(await evaluate('navigator.clipboard.readText()'),error);
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({simulated:true,hardware_access:false,progress_bytes:true,all_preparation_stages:true,filesystem_and_version:true,close_reopen_preserved:true,usb_reconnect_preserved:true,another_board_reused:true,refresh_restored:true,mobile_fits:true,preparations,probes:probe},null,2));console.log('Image preparation browser flow passed');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
