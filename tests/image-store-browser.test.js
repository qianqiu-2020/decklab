'use strict';
// Real Chromium/UI; synthetic catalog metadata only. No collector, download or board.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {launchBrowser, stopBrowser} = require('../tools/browser_launch');
const root = path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-image-store-'));
const directory = (name, prefix) => ({name, path: prefix + name + '/', kind:'directory'});
const image = name => ({name,kind:'package',url:'https://images.example.test/'+name,size:2048});
const tree = {
  '':[directory('k3','')],
  'k3/':[directory('version','k3/'),directory('daily','k3/')],
  'k3/version/':[directory('bianbu','k3/version/'),directory('debian','k3/version/')],
  'k3/version/bianbu/':[directory('v4.0.8','k3/version/bianbu/'),directory('v4.0.10','k3/version/bianbu/')],
  'k3/version/bianbu/v4.0.10/':[image('Bianbu-LXQt-K3-v4.0.10.tar.gz'),image('Bianbu-LXQt-UEFI-K3-v4.0.10.tar.gz'),image('Bianbu-Minimal-K3-v4.0.10.tar.gz'),image('unsupported.img.gz')],
  'k3/version/bianbu/v4.0.8/':[image('Bianbu-LXQt-K3-v4.0.8.tar.gz')],
  'k3/version/debian/':[image('Debian-13-GNOME-K3-202611161345.tar.gz')],
  'k3/daily/':[directory('20261116','k3/daily/'),directory('20261117','k3/daily/')],
  'k3/daily/20261117/':[image('Bianbu-LXQt-K3-20261117032354.tar.gz'),image('Debian-13-GNOME-K3-202611170510.tar.gz'),image('Debian-13-Firmware-K3-202611170457.tar.gz')],
  'k3/daily/20261116/':[image('Bianbu-LXQt-K3-20261116032354.tar.gz')]
};
const visited = [], errors = [];
const bootstrap = `
SerialdI18n.setLocale('zh-CN');
const $ = id => document.getElementById(id);
window.store = SerialdImageStore.create({byId:$,fetch:window.fetch,formatBytes:SerialdUI.formatBytes});
store.open(); window.fixtureReady=true;
`;
const scripts = ['static/i18n.js','static/ui-common.js','static/tasks.js','static/image-store.js'];
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://fixture');
  if (url.pathname === '/') {
    res.setHeader('Content-Type','text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(root,'index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'')
      .replace('</body>',scripts.map(file=>'<script src="/'+file+'"></script>').join('')+'<script src="/fixture.js"></script></body>')); return;
  }
  if(url.pathname === '/fixture.js'){res.setHeader('Content-Type','text/javascript');res.end(bootstrap);return;}
  if(url.pathname === '/api/image-catalog') {
    const directory = url.searchParams.get('path') || ''; visited.push(directory);
    if(!Object.hasOwn(tree,directory)){res.statusCode=404;res.end(JSON.stringify({error:'Unknown fixture path: '+directory}));return;}
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({entries:tree[directory]}));return;
  }
  const filename = path.resolve(root,'.'+url.pathname);
  if(!filename.startsWith(root+'/')||!/^\/(static|vendor)\//.test(url.pathname)||!fs.existsSync(filename)){res.statusCode=404;res.end();return;}
  res.setHeader('Content-Type',filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':'image/svg+xml');
  res.end(fs.readFileSync(filename));
});
let chrome,ws;
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const launched=await launchBrowser(profile);chrome=launched.chrome;
  const address='http://127.0.0.1:'+server.address().port;
  const tab=await(await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+encodeURIComponent(address),{method:'PUT'})).json();
  ws=new WebSocket(tab.webSocketDebuggerUrl);await new Promise(resolve=>{ws.onopen=resolve;});
  let sequence=0;const pending=new Map();
  ws.onmessage=event=>{
    const message=JSON.parse(event.data);
    if(message.id){const item=pending.get(message.id);pending.delete(message.id);if(message.error)item.reject(new Error(JSON.stringify(message.error)));else item.resolve(message.result);}
    else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
  };
  const rpc=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{
    const value=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});
    if(value.exceptionDetails)throw new Error(JSON.stringify(value.exceptionDetails));return value.result.value;
  };
  async function until(expression){for(let n=0;n<150;n++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,40));}throw new Error('Timed out: '+expression+' '+await evaluate('document.getElementById("is-status").textContent'));}
  const click=async(id,label)=>evaluate(`Array.from(document.getElementById(${JSON.stringify(id)}).querySelectorAll('button')).find(node=>node.textContent===${JSON.stringify(label)}).click()`);
  await rpc('Runtime.enable');await rpc('Page.enable');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1280,height:960,deviceScaleFactor:1,mobile:false});
  await until('window.fixtureReady && !document.getElementById("is-refresh").disabled');
  await until('document.getElementById("is-list").textContent.includes("v4.0.10")');
  assert.equal(await evaluate('document.getElementById("is-list").textContent.includes("unsupported.img")'),false);
  assert.ok(!visited.includes('k3/version/bianbu/v4.0.8/'));
  assert.ok(!visited.includes('k3/daily/20261116/'));
  const screenshots=path.join(root,'build','image-store-browser');fs.mkdirSync(screenshots,{recursive:true});
  fs.writeFileSync(path.join(screenshots,'desktop.png'),Buffer.from((await rpc('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await click('is-variants','Minimal');
  assert.equal(await evaluate('document.getElementById("is-list").textContent.includes("LXQt")'),false);
  await click('is-variants','全部变体');
  const output=path.join(root,'build','image-store-ui');fs.mkdirSync(output,{recursive:true});
  async function screenshot(name){const result=await rpc('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,name),Buffer.from(result.data,'base64'));}
  await screenshot('desktop.png');
  await click('is-channel','Daily');
  await until('!document.getElementById("is-refresh").disabled && document.getElementById("is-list").textContent.includes("20261117")');
  await click('is-os','Debian');
  await until('!document.getElementById("is-refresh").disabled && document.getElementById("is-list").textContent.includes("GNOME")');
  assert.equal(await evaluate('document.getElementById("is-list").textContent.includes("Bianbu")'),false);
  await click('is-variants','Firmware');
  assert.equal(await evaluate('document.getElementById("is-list").textContent.includes("GNOME")'),false);
  await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate('document.getElementById("image-store-dialog").scrollWidth <= document.getElementById("image-store-dialog").clientWidth'),true);
  await screenshot('mobile.png');
  fs.writeFileSync(path.join(screenshots,'mobile.png'),Buffer.from((await rpc('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await evaluate('document.getElementById("is-version").value="day:20261116";document.getElementById("is-version").dispatchEvent(new Event("change"))');
  await until('!document.getElementById("is-refresh").disabled');
  assert.equal(await evaluate('document.querySelectorAll("#is-list .is-row").length'),0);
  await evaluate('SerialdI18n.setLocale("en")');
  assert.equal(await evaluate('document.getElementById("is-os").getAttribute("aria-label")'),'Operating system');
  assert.deepEqual(errors,[]);
  console.log('Image-store Chromium passed: OS/channel/history/variants, numeric ordering, unsupported formats, no historical crawl, desktop/mobile and i18n.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(ws)ws.close();await stopBrowser(chrome);await new Promise(resolve=>server.close(resolve));fs.rmSync(profile,{recursive:true,force:true});
});
