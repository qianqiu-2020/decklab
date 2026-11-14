'use strict';
// Actual Chromium mouse/drag events with simulated devices; no hardware service.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http');
const {launchBrowser,stopBrowser}=require('./browser_launch');
const root=path.resolve(__dirname,'..'),out=process.argv[2]||path.join(root,'dist/device-drag-browser');
fs.mkdirSync(out,{recursive:true});
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'decklab-device-drag-'));
let devices=['A','B','C','D'].map(key=>({key,serial:key,label:'模拟设备 '+key,alive:true,state:'online',dev:'/dev/fixture-'+key,info:{},bytes:0}));
const errors=[];
const server=http.createServer((req,res)=>{
  assert.equal(req.method,'GET','device sorting must never mutate the backend');
  if(req.url==='/api/devices'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({devices}));return;}
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');
    res.end(`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/app.css">
      <div hidden><span id="count-online"></span><span id="count-history"></span><span id="count-pending"></span><span id="conn-dot"></span><span id="conn-txt"></span><input id="device-search"><select id="device-filter"><option value="all">all</option></select></div>
      <main id="fixture" style="margin:24px;width:320px;max-height:1140px;overflow:auto"><p>模拟设备 · 实际鼠标拖拽测试</p><div id="grid" class="grid" style="grid-template-columns:1fr"></div></main>
      <script src="/static/devices.js"></script><script>window.ui=SerialdDevices.create({byId:id=>document.getElementById(id),fetch:fetch,formatBytes:n=>n+' B',age:()=> 'now',open:()=>{window.unexpectedSelection=true},getView:()=>null,isCurrent:()=>false,loadSessions(){},loadActivity(){},renderInfo(){}});ui.poll();setInterval(()=>ui.poll(),100);</script>`);return;
  }
  if(!['/static/app.css','/static/devices.js'].includes(req.url)){res.statusCode=404;res.end();return;}
  res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(path.join(root,req.url)));
});
let chrome,ws;
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const launched=await launchBrowser(profile);chrome=launched.chrome;
  const target=await(await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+encodeURIComponent('http://127.0.0.1:'+server.address().port),{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0,intercepted=null;const pending=new Map();
  ws.onmessage=event=>{const value=JSON.parse(event.data);
    if(value.id){const call=pending.get(value.id);pending.delete(value.id);value.error?call.reject(new Error(JSON.stringify(value.error))):call.resolve(value.result);}
    else if(value.method==='Input.dragIntercepted')intercepted=value.params.data;
    else if(value.method==='Runtime.exceptionThrown')errors.push(value.params.exceptionDetails.exception?.description||value.params.exceptionDetails.text);
  };
  const rpc=(method,params={})=>new Promise((resolve,reject)=>{const seq=++id;pending.set(seq,{resolve,reject});ws.send(JSON.stringify({id:seq,method,params}));});
  const evaluate=async expression=>{const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function waitFor(expression){for(let n=0;n<80;n++){if(await evaluate(expression))return;await delay(25);}throw new Error('Timed out: '+expression);}
  const order=()=>evaluate('[...document.querySelectorAll("#grid>.dev")].map(c=>c.dataset.key)');
  const stored=()=>evaluate('localStorage.getItem("decklab.device-order.v1")');
  async function position(key,handle=false){return evaluate(`(()=>{const c=[...document.querySelectorAll('#grid>.dev')].find(c=>c.dataset.key===${JSON.stringify(key)});const r=${handle?"c.querySelector('[data-order-control=drag]')":"c"}.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height};})()`);}
  async function begin(key){
    intercepted=null;const p=await position(key,true);
    await rpc('Input.dispatchMouseEvent',{type:'mouseMoved',x:p.x,y:p.y});
    await rpc('Input.dispatchMouseEvent',{type:'mousePressed',x:p.x,y:p.y,button:'left',buttons:1,clickCount:1});
    await rpc('Input.dispatchMouseEvent',{type:'mouseMoved',x:p.x+8,y:p.y-12,button:'left',buttons:1});
    for(let n=0;n<80&&!intercepted;n++)await delay(25);
    assert.ok(intercepted,'actual mouse gesture must initiate native drag');return intercepted;
  }
  async function hover(data,x,y){await rpc('Input.dispatchDragEvent',{type:'dragEnter',x,y,data});await rpc('Input.dispatchDragEvent',{type:'dragOver',x,y,data});await delay(180);}
  async function release(data,x,y,cancel=false){if(!cancel)await rpc('Input.dispatchDragEvent',{type:'dragOver',x,y,data});await rpc('Input.dispatchDragEvent',{type:cancel?'dragCancel':'drop',x,y,data});await rpc('Input.dispatchMouseEvent',{type:'mouseReleased',x,y,button:'left',buttons:0,clickCount:1});await delay(200);}
  await rpc('Runtime.enable');await rpc('Page.enable');await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1200,deviceScaleFactor:1,mobile:false});
  await rpc('Input.setInterceptDrags',{enabled:true});await waitFor('document.querySelectorAll("#grid>.dev").length===4');
  const first=await position('A'), initial=await stored();
  await evaluate('window.originalA=document.querySelector("#grid>.dev");');
  let data=await begin('C');await hover(data,first.x,first.top+12);
  assert.deepEqual(await order(),['C','A','B','D'],'native dragover must preview the inserted card');
  assert.equal(await stored(),initial,'preview must not save order');
  assert.ok((await position('A')).top>first.top+100,'the original card must physically move down');
  devices.push({key:'E',serial:'E',label:'模拟设备 E',alive:true,state:'online',dev:'/dev/fixture-E',info:{},bytes:0});await delay(220);
  assert.equal(await evaluate('document.querySelectorAll("#grid>.dev").length'),4,'polling must defer new cards until drop');
  assert.equal(await evaluate('window.originalA===[...document.querySelectorAll("#grid>.dev")].find(c=>c.dataset.key==="A")'),true);
  const shot=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,'device-drag-preview-fixture.png'),Buffer.from(shot.data,'base64'));
  let landing=await position('C');await release(data,landing.x,landing.y);await waitFor('document.querySelectorAll("#grid>.dev").length===5');
  assert.deepEqual(await order(),['C','A','B','D','E']);assert.notEqual(await stored(),initial);
  const saved=await stored();data=await begin('C');let bottom=await position('B');await hover(data,bottom.x,bottom.bottom-12);
  assert.deepEqual(await order(),['A','B','C','D','E']);await release(data,bottom.x,bottom.bottom-12,true);
  assert.deepEqual(await order(),['C','A','B','D','E'],'native cancelled drag must roll back');assert.equal(await stored(),saved);
  await rpc('Page.reload',{ignoreCache:true});await waitFor('document.querySelectorAll("#grid>.dev").length===5');assert.deepEqual(await order(),['C','A','B','D','E']);
  // Repeat real dragging on a multi-column overview, including a two-card row.
  await evaluate('document.querySelector("#fixture").style.width="1000px";document.querySelector("#grid").style.gridTemplateColumns="repeat(3,1fr)";');
  data=await begin('E');const d=await position('D');await hover(data,d.left+12,d.y);
  assert.deepEqual(await order(),['C','A','B','E','D'],'horizontal insertion must use the left/right landing side');
  landing=await position('E');await release(data,landing.x,landing.y);assert.deepEqual(await order(),['C','A','B','E','D']);
  assert.equal(await evaluate('window.unexpectedSelection===true'),false);assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'receipt.json'),JSON.stringify({native_mouse_drag:true,live_neighbor_movement:true,drop_persisted:true,cancel_rolled_back:true,poll_preserved_dom:true,horizontal_grid:true,fixtures_only:true},null,2)+'\n');
  console.log('Native device drag passed: live insertion, neighbor movement, drop, cancellation, polling and horizontal grids.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(ws)ws.close();if(chrome)await stopBrowser(chrome);server.close();await fs.promises.rm(profile,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
