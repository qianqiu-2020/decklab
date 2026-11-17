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
function prepareTask(){return {id:'prepare-original',kind:'image-prepare',state:phase,progress:{phase:'checking-payloads',done:6e9,total:12e9},
 flash:{preview_progress:{active:phase==='running',state:phase,sample_count:0,updated:Date.now()/1000,stages:[
 {phase:'checking-image-sha256',state:'completed',elapsed_seconds:8,estimated_seconds:8},
 {phase:'expanding-image',state:'completed',elapsed_seconds:80,estimated_seconds:80},
 {phase:'checking-payloads',state:phase==='running'?'running':'completed',elapsed_seconds:15,estimated_seconds:30},
 {phase:'reading-payload-metadata',state:phase==='running'?'pending':'completed',elapsed_seconds:1,estimated_seconds:3}]}},
 result:phase==='completed'?{preparation:{payload_metadata:[{partition:'rootfs',content:{size:12e9,version:'Debian GNU/Linux 13（模拟）',version_state:'identified',filesystem:{type:'ext4',label:'rootfs'}}}]}}:null};}
const caps=()=>({supported:true,image_preparation_supported:true,profile:'fixture',modes:['full','partitions'],artifacts:[artifact],storages:[]});
const server=http.createServer((req,res)=>{const url=new URL(req.url,'http://localhost');res.setHeader('Cache-Control','no-store');
 const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
 if(req.method==='POST'){let body='';req.on('data',d=>body+=d);req.on('end',()=>{try{const p=JSON.parse(body||'{}');
  if(url.pathname==='/api/flash-target'){probe++;return json({target,capabilities:caps()});}
  if(url.pathname==='/api/image-prepare'){preparations++;assert.equal(p.id,artifact.id);assert.equal(p.serial,undefined);return json({task:prepareTask()});}
  throw new Error('unexpected mutation '+url.pathname);
 }catch(e){errors.push(e.stack);res.statusCode=500;json({error:e.message});}});return;}
 if(url.pathname==='/') {res.setHeader('Content-Type','text/html');return res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/app.css"><body>'+dialog+'<p>SIMULATED · 模拟镜像与板卡，无真实设备操作</p><script src="/static/tasks.js"></script><script src="/static/flashing.js"></script><script>window.ui=SerialdFlashing.create({byId:id=>document.getElementById(id),fetch:fetch,formatBytes:n=>(n/1e9).toFixed(1)+" GB"});ui.open('+JSON.stringify(target)+');</script></body></html>');}
 if(url.pathname==='/api/tasks')return json({tasks:[]});
 if(url.pathname==='/api/flash-discover')return json({targets:[target]});
 if(url.pathname==='/api/task')return json({task:prepareTask()});
 if(['/static/app.css','/static/tasks.js','/static/flashing.js'].includes(url.pathname)){res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':'application/javascript');return res.end(fs.readFileSync(path.join(root,url.pathname)));}
 res.statusCode=404;res.end();
});
let chrome,ws;const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const launched=await launchBrowser(profile);chrome=launched.chrome;
 const tab=await(await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+encodeURIComponent('http://127.0.0.1:'+server.address().port),{method:'PUT'})).json();
 ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});let id=0;const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(JSON.stringify(m.params));};
 const rpc=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await delay(100);}throw new Error('UI wait failed '+expression);};
 const shot=async name=>{const s=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(s.data,'base64'));};
 await rpc('Runtime.enable');await rpc('Page.enable');await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
 await wait('document.querySelector("#fl-cache") && !document.querySelector("#fl-cache").disabled');
 await evaluate('document.querySelector("#fl-artifact").value="browser-image";document.querySelector("#fl-artifact").dispatchEvent(new Event("change"))');
 await wait('document.querySelector("#fl-image-preparation-bytes").textContent.includes("50%")');
 assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),true);await shot('preparing-simulated');
 await evaluate('document.querySelector("#fl-close").click();ui.open()');
 assert.equal(preparations,1,'closing must not restart image preparation');
 target={...target,target_generation:'bootrom-a-reconnected'};
 await evaluate('document.querySelector("#fl-discover").click()');
 await wait('!document.querySelector("#fl-cache").disabled && document.querySelector("#fl-artifact").value==="browser-image"');
 assert.equal(preparations,1);assert.equal(await evaluate('document.querySelector("#fl-start").disabled'),true);
 target={target_id:'board-b',target_generation:'bootrom-b',serial:'B',label:'模拟板卡 B'};
 await evaluate('document.querySelector("#fl-discover").click()');
 await wait('document.querySelector("#fl-board").textContent.includes("模拟板卡 B") && !document.querySelector("#fl-cache").disabled');
 assert.equal(preparations,1);assert.equal(await evaluate('document.querySelector("#fl-artifact").value'),artifact.id);
 phase='completed';await wait('document.querySelector("#fl-image-information").textContent.includes("Debian GNU/Linux 13")');
 assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),false);await shot('prepared-simulated');
 await rpc('Page.reload');await wait('document.querySelector("#fl-image-information").textContent.includes("ext4")');
 assert.equal(preparations,2,'refresh resolves the existing task through server dedup');assert.equal(await evaluate('document.querySelector("#fl-artifact").value'),artifact.id);
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 assert.equal(await evaluate('document.querySelector("#flash-dialog").scrollWidth<=document.querySelector("#flash-dialog").clientWidth+1'),true);await shot('prepared-mobile-simulated');
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({simulated:true,hardware_access:false,progress_bytes:true,all_preparation_stages:true,filesystem_and_version:true,close_reopen_preserved:true,usb_reconnect_preserved:true,another_board_reused:true,refresh_restored:true,mobile_fits:true,preparations,probes:probe},null,2));console.log('Image preparation browser flow passed');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
