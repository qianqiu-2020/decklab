'use strict';
// Chromium exercises real UI modules; every image/board response is simulated.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http');
const {launchBrowser,stopBrowser}=require('./browser_launch');
const root=path.resolve(__dirname,'..'),out=path.resolve(process.argv[2]||'build/image-workspace-browser');fs.mkdirSync(out,{recursive:true});
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'decklab-directory-ui-')),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const dialogs=html.slice(html.indexOf('<dialog id="image-store-dialog"'),html.indexOf('<dialog id="probe-settings-dialog"'));
const target={target_id:'fixture-board',target_generation:'fixture-bootrom',serial:'SIMULATED',label:'模拟板卡'};
const osImage={id:'os-fixture',name:'Debian-13-GNOME-K3-v1.0rc1-202611180100.tar.gz',kind:'package',url:'https://images.example/os.tar.gz',size:4000000000,sha256:'a'.repeat(64),state:'ready'};
const firmware={id:'fw-fixture',name:'Debian-13-Firmware-K3-v1.0rc1-202611180055.tar.gz',kind:'package',url:'https://images.example/fw.tar.gz',size:3000000,sha256:'b'.repeat(64),state:'ready'};
const directory={id:'directory-fixture',name:'Debian-custom.directory',size:2000,directory_bytes:12000000000,sha256:'c'.repeat(64),state:'ready'};
const controllerArchive={id:'archive-fixture',name:'Debian-local.tar.gz',size:3000,sha256:'e'.repeat(64),state:'ready'};
const requests=[],downloads=[],edits=[],previews=[],errors=[];let staged=Buffer.alloc(0);
const prepared=artifact=>({id:'prepare-'+artifact.id,kind:'image-prepare',state:'completed',result:{artifact,preparation:{payload_metadata:[{partition:'rootfs',content:{size:12000000000,filesystem:{type:'ext4'},version:'Debian GNU/Linux 13（模拟）',version_state:'identified'}}]}}});
const server=http.createServer((req,res)=>{requests.push(req.method+' '+req.url);const url=new URL(req.url,'http://localhost');res.setHeader('Cache-Control','no-store');const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
 if(req.method==='POST'){let body='';req.on('data',c=>body+=c);req.on('end',()=>{try{const p=JSON.parse(body||'{}');
 if(url.pathname==='/api/flash-target')return json({target,capabilities:{supported:true,preparable:true,reset_after_supported:true,loader_sources:['builtin','image'],image_preparation_supported:true,profile:'simulation',modes:['full','partitions'],artifacts:[osImage,firmware],storages:[]}});
 if(url.pathname==='/api/image-download'){downloads.push(p);const artifact=p.url===osImage.url?osImage:firmware;return json({task:{id:'download-'+artifact.id,kind:'image-download',state:'completed',result:{artifact,preparation_task:prepared(artifact)}}});}
 if(url.pathname==='/api/image-prepare')return json({task:prepared(p.id===osImage.id?osImage:p.id===firmware.id?firmware:directory)});
 if(url.pathname==='/api/image-workspace'){
  if(p.action==='directories')return json({ok:true,directory:p.directory,parent:'/data',directories:p.directory==='/data/simulated'?[{name:'manual-directory',path:'/data/simulated/manual-directory'}]:[],archives:p.directory==='/data/simulated'?[{name:controllerArchive.name,path:'/data/simulated/'+controllerArchive.name}]:[]});
  if(p.action==='list')return json({ok:true,directory:'/data/simulated/debian-work',revision:'fixture-revision',editable:true,files:[{name:'partition_universal.json',size:200},{name:'rootfs.ext4',size:12000000000}]});
  if(p.action==='read')return json({ok:true,text:'{"format":"gpt","partitions":[{"name":"rootfs","offset":"1M"}]}'});
  if(p.action==='save'){edits.push(p);assert.equal(JSON.parse(p.text).partitions[0].offset,'2M');assert.equal(p.revision,'fixture-revision');return json({task:{id:'edit-directory',state:'completed',result:{artifact:directory,preparation_task:prepared(directory)}}});}
  if(p.action==='stage'){staged=Buffer.alloc(0);return json({artifact:{id:'replacement-file'}});}
  if(p.action==='replace'){edits.push(p);assert.equal(p.replacement_id,'replacement-file');assert.equal(staged.toString(),'replacement payload');return json({task:{id:'replace-directory',state:'completed',result:{artifact:directory,preparation_task:prepared(directory)}}});}
  if(p.action==='import'){
   assert.match(p.request_id,/^image-import-/);
   if(p.path==='/data/simulated/'+controllerArchive.name)return json({task:{id:'import-archive',state:'completed',result:{artifact:controllerArchive,preparation_task:prepared(controllerArchive)}}});
   assert.equal(p.path,'/data/simulated/manual-directory');return json({artifact:directory,preparation_task:prepared(directory)});
  }
  if(p.action==='import-archive'){
   assert.equal(p.path,'/data/simulated/'+controllerArchive.name);
   return json({task:{id:'import-ram-archive',state:'completed',result:{artifact:controllerArchive,preparation_task:prepared(controllerArchive)}}});
  }
 }
 if(url.pathname==='/api/artifact-append'){assert.equal(p.offset,staged.length);staged=Buffer.concat([staged,Buffer.from(p.data_b64,'base64')]);return json({artifact:{id:p.id,received:staged.length}});}
 if(url.pathname==='/api/artifact-seal')return json({artifact:{id:p.id,state:'ready'}});
 if(url.pathname==='/api/flash-plan'){
  previews.push(p);assert.equal(p.verify,'none');assert.equal(p.reset_after,true);assert.equal(p.firmware_artifact_id,firmware.id);assert.equal(p.loader_source,'builtin');
  return json({plan:{id:'combined-preview',ready:true,mode:'full',verify:p.verify,reset_after:p.reset_after,loader_source:p.loader_source,
    ram_loaders:[{name:'u-boot.itb',size:2000000,sha256:'d'.repeat(64)}],
    components:[{label:'Firmware',name:firmware.name,ready:true},{label:'OS',name:directory.name,ready:true}],
    storage_layouts:[{id:'NOR',label:'NOR',component:'Firmware',size:8388608,partitions:[{name:'fsbl',offset:131072,size:524288}]},
      {id:'scsi:0',label:'UFS / SCSI',component:'OS',size:128000000000,partitions:[]}],
    erase_ranges:[{storage:'NOR',component:'Firmware',offset:0,size:8388608}],
    writes:[{storage:'NOR',component:'Firmware',partition:'fsbl',offset:131072,capacity:524288,size:200000},
      {storage:'scsi:0',component:'OS',kind:'layout',target:'gpt',size:400},
      {storage:'scsi:0',component:'OS',partition:'esp',offset:1048576,capacity:268435456,size:268435456},
      {storage:'scsi:0',component:'OS',partition:'rootfs',offset:269484032,capacity:48000000000,size:12000000000}]}});}
 throw new Error('unexpected mutation '+url.pathname);
 }catch(e){errors.push(e.stack);res.statusCode=500;json({error:e.message});}});return;}
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html');return res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/app.css"><body><p>SIMULATED · 模拟目录、镜像与板卡，无硬件操作</p>'+dialogs+['tasks','image-store','image-workspaces','flashing'].map(n=>'<script src="/static/'+n+'.js"></script>').join('')+'<script>var $=id=>document.getElementById(id),fmt=n=>n>=1e9?(n/1e9).toFixed(1)+" GB":n>=1e6?(n/1e6).toFixed(1)+" MB":n>=1e3?(n/1e3).toFixed(1)+" KB":n+" B";var store=SerialdImageStore.create({byId:$,fetch:fetch,formatBytes:fmt}),workspace=SerialdImageWorkspaces.create({byId:$,fetch:fetch,formatBytes:fmt,copyText:p=>window.copiedPath=p});window.ui=SerialdFlashing.create({byId:$,fetch:fetch,formatBytes:fmt,openImageStore:(choose,picker)=>store.open(choose,picker),openImageWorkspace:(artifact,choose,settings)=>workspace.open(artifact,choose,settings)});ui.open();</script></body></html>');}
 if(url.pathname==='/api/flash-discover')return json({targets:[target]});
 if(url.pathname==='/api/tasks')return json({tasks:[]});
 if(url.pathname==='/api/image-catalog')return json({entries:[osImage,firmware],sources:[{id:'public'}]});
 if(url.pathname.startsWith('/static/')&&/^\/static\/[a-z-]+\.(js|css)$/.test(url.pathname)){res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':'application/javascript');return res.end(fs.readFileSync(path.join(root,url.pathname)));}
 res.statusCode=404;res.end();
});
let chrome,ws;const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const launched=await launchBrowser(profile);chrome=launched.chrome;
 const tab=await(await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+encodeURIComponent('http://127.0.0.1:'+server.address().port),{method:'PUT'})).json();ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});let id=0;const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(JSON.stringify(m.params));};
 const rpc=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await delay(100);}throw new Error('UI wait failed '+expression+' '+JSON.stringify({errors,requests,ui:await evaluate('({storeOpen:document.querySelector("#image-store-dialog").open,storeStatus:document.querySelector("#is-status").textContent,source:document.querySelector("#is-source").value,flashStatus:document.querySelector("#fl-status").textContent})')}));};
 const shot=async name=>{const r=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(r.data,'base64'));};
 await rpc('Runtime.enable');await rpc('Page.enable');await rpc('Emulation.setDeviceMetricsOverride',{width:1500,height:1100,deviceScaleFactor:1,mobile:false});
 await wait('window.ui&&document.querySelector("#fl-store")&&!document.querySelector("#fl-store").disabled');await evaluate('document.querySelector("#fl-store").click()');
 await wait('document.querySelector("#is-list").textContent.includes("同时选择")');
 await evaluate('[...document.querySelectorAll("#is-list button")].find(b=>b.textContent==="下载并使用").click()');
 await wait('document.querySelector("#fl-firmware").value==="fw-fixture"');
 assert.equal(await evaluate('document.querySelector("#fl-plan").disabled'),true);
 assert.ok(!requests.some(r=>r.includes('/api/flash-')));
 await evaluate('document.querySelector("#fl-discover").click()');
 await wait('document.querySelector("#fl-firmware").value==="fw-fixture"&&!document.querySelector("#fl-plan").disabled');assert.equal(downloads.length,2);await shot('paired-images-simulated');
 await evaluate('document.querySelector("#fl-workspace").click()');await wait('document.querySelector("#iw-files").textContent.includes("partition_universal.json")');await evaluate('document.querySelector("#iw-copy").click()');assert.equal(await evaluate('window.copiedPath'),'/data/simulated/debian-work');
 await evaluate('[...document.querySelectorAll("#iw-files button")].find(b=>b.textContent.startsWith("partition_universal.json")).click()');await wait('document.querySelector("#iw-text").value.includes("1M")');
 await evaluate('document.querySelector("#iw-text").value=JSON.stringify({format:"gpt",partitions:[{name:"rootfs",offset:"2M"}]})');await shot('partition-editor-simulated');await evaluate('document.querySelector("#iw-save").click()');
 await wait('!document.querySelector("#image-workspace-dialog").open&&document.querySelector("#fl-artifact").value==="directory-fixture"');assert.equal(edits.length,1);assert.equal(await evaluate('document.querySelector("#fl-firmware").value'),firmware.id);
 await evaluate('document.querySelector("#fl-workspace").click()');await wait('document.querySelector("#iw-files").textContent.includes("rootfs.ext4")');await evaluate('[...document.querySelectorAll("#iw-files button")].find(b=>b.textContent.startsWith("rootfs.ext4")).click()');
 await evaluate('(()=>{const d=new DataTransfer();d.items.add(new File(["replacement payload"],"rootfs.ext4"));const input=document.querySelector("#iw-upload");input.files=d.files;input.dispatchEvent(new Event("change"));})()');await wait('!document.querySelector("#image-workspace-dialog").open');assert.equal(edits.length,2);
 await evaluate('var loader=document.querySelector("#fl-loader-source"); if(loader.value!=="builtin"||loader.disabled) throw Error("missing default loader"); loader.value="image"; loader.dispatchEvent(new Event("change")); if(loader.value!=="image") throw Error("image loader unavailable"); loader.value="builtin"; loader.dispatchEvent(new Event("change")); document.querySelector("#fl-plan").click()');await wait('document.querySelector("#fl-preview").textContent.includes("Firmware")&&document.querySelector("#fl-preview").textContent.includes("Debian-custom.directory")');assert.equal(previews.length,1);
 assert.equal(await evaluate('document.querySelectorAll("#fl-preview .fl-map").length'),2);
 assert.equal(await evaluate('document.querySelector("#fl-preview .fl-map-track [data-kind=erase]").style.width'),'100%');
 const ratio=await evaluate('parseFloat([...document.querySelectorAll("#fl-preview .fl-map-track [data-kind=write]")].find(el=>el.textContent==="rootfs").style.width)');
 assert.equal(ratio,9.375);
 assert.deepEqual(await evaluate('[...document.querySelectorAll("#fl-preview .fl-map-review")].map(m=>[...m.querySelectorAll(".fl-lane-label")].map(l=>l.textContent))'),[['现有分区','擦除上限','将写入'],['新分区','将写入']]);
 assert.ok(await evaluate('document.querySelector("#fl-preview").textContent.includes("重写分区表 · 原布局未读取")'));
 assert.ok(await evaluate('document.querySelectorAll("#fl-preview .fl-map-boundary").length >= 5'));
 assert.ok(await evaluate(`[...document.querySelectorAll("#fl-preview .fl-map-track [data-kind=partition] b")].some(b => b.textContent==="rootfs" && b.getBoundingClientRect().top < b.parentNode.getBoundingClientRect().bottom)`));
 await evaluate(`document.querySelectorAll("#fl-preview .fl-map details")[1].open=true; [...document.querySelectorAll("#fl-preview .fl-map-legend li strong")].find(label=>label.textContent==="esp").parentNode.focus()`);
 assert.equal(await evaluate(`document.querySelector('#fl-preview [data-highlight="true"] b').textContent`),'esp');
 await evaluate('document.querySelectorAll("#fl-preview .fl-map details")[1].open=false;document.querySelector("#fl-preview .fl-map").scrollIntoView({block:"center",behavior:"instant"})');
 await wait('document.querySelector("#fl-preview .fl-map").getBoundingClientRect().bottom < innerHeight - 80');await shot('combined-preview-simulated');
 assert.equal(await evaluate('document.querySelector("#fl-setup").getClientRects().length'),0);
 assert.equal(await evaluate('document.querySelector("#fl-start").getClientRects().length'),1);
 await evaluate('document.querySelector("#fl-edit").click()');
 assert.equal(await evaluate('document.querySelector("#fl-start").disabled'),true);
 await evaluate('document.querySelector("#fl-directory").click();document.querySelector("#iw-directory").value="/data/simulated";document.querySelector("#iw-browse").click()');
 await wait('document.querySelector("#iw-directories").textContent.includes("Debian-local.tar.gz")');
 assert.equal(await evaluate('document.querySelector("#iw-title").textContent'),'从控制机导入镜像');
 await evaluate('[...document.querySelectorAll("#iw-directories button")].find(b=>b.textContent==="Debian-local.tar.gz").click()');
 assert.equal(await evaluate('document.querySelector("#iw-directory").value'),'/data/simulated/Debian-local.tar.gz');
 await evaluate('document.querySelector("#iw-import").click()');
 await wait('!document.querySelector("#image-workspace-dialog").open&&document.querySelector("#fl-artifact").value==="archive-fixture"');
 await evaluate('document.querySelector("#fl-directory").click();document.querySelector("#iw-directory").value="/data/simulated";document.querySelector("#iw-browse").click()');await wait('document.querySelector("#iw-directories").textContent.includes("manual-directory/")');await evaluate('[...document.querySelectorAll("#iw-directories button")].find(b=>b.textContent==="manual-directory/").click()');await wait('document.querySelector("#iw-directory").value==="/data/simulated/manual-directory"');await evaluate('document.querySelector("#iw-import").click()');await wait('!document.querySelector("#image-workspace-dialog").open&&document.querySelector("#fl-artifact").value==="directory-fixture"');
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});assert.equal(await evaluate('document.querySelector("#flash-dialog").scrollWidth<=document.querySelector("#flash-dialog").clientWidth+1'),true);await shot('mobile-pair-simulated');
 await evaluate('document.querySelector("#fl-close").click();ui.open(null,null,false,"ram")');
 assert.equal(await evaluate('document.querySelector("#fl-directory").getClientRects().length'),1);
 await evaluate('document.querySelector("#fl-directory").click()');
 assert.equal(await evaluate('document.querySelector("#iw-directory").placeholder'),'镜像包的绝对路径');
 await evaluate('document.querySelector("#iw-directory").value="/data/simulated/Debian-local.tar.gz";document.querySelector("#iw-import").click()');
 await wait('!document.querySelector("#image-workspace-dialog").open&&document.querySelector("#fl-artifact").value==="archive-fixture"');
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({simulated:true,hardware_access:false,automatic_optional_firmware_association:true,paired_downloads:downloads.length,copy_host_path:true,edit_partition_table:true,replace_partition_file:true,control_host_directory_selection:true,control_host_archive_selection:true,combined_preview:true,no_write_submitted:true,mobile_fits:true},null,2));console.log('Controller image import, directory workspace and optional firmware browser flow passed');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
