'use strict';
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.events={};this.value='';this.disabled=false;this.open=false;this._text='';}
  set textContent(value){this._text=value;this.children=[];} get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  appendChild(node){node.parent=this;this.children.push(node);} remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
  addEventListener(name,fn){this.events[name]=fn;} showModal(){this.open=true;} close(){this.open=false;}
  fire(name='click'){return this.events[name]&&this.events[name]();}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(request){
  const ids={};for(const id of ['is-title','image-store-dialog','is-list','is-search','is-path','is-status','is-refresh','is-downloads','is-source','is-close'])ids[id]=new Element();
  ids['is-source'].value='public';let consent=false;const watches=[];
  const window={crypto:{randomUUID:()=> 'fixture-request'}, confirm:()=>consent,
    SerialdTasks:{createClient:()=>({request,watch:(task,ctx,update)=>new Promise((resolve,reject)=>watches.push({task,update,resolve,reject}))})}};
  vm.runInNewContext(fs.readFileSync('static/image-store.js','utf8'),{window,document:{createElement:()=>new Element()}});
  const store=window.SerialdImageStore.create({byId:id=>ids[id],formatBytes:n=>n+' bytes'});
  function buttons(node=ids['is-list']){return node.children.flatMap(n=>[n,...buttons(n)]).filter(n=>n.events.click);}
  return {ids,store,buttons,watches,consent:value=>{consent=value;},source:async value=>{ids['is-source'].value=value;ids['is-source'].fire('change');await flush();}};
}
(async()=>{
  let resolvePublic;
  const stale=setup(url=> url.startsWith('/api/image-library')?Promise.resolve({artifacts:[{id:'ready',name:'cached.img',size:1}],tasks:[]}):new Promise(resolve=>{resolvePublic=resolve;}));
  stale.store.open();await stale.source('local');resolvePublic({entries:[{name:'stale.img',kind:'disk-image',url:'https://old'}]});await flush();
  assert.ok(stale.ids['is-list'].textContent.includes('cached.img'));assert.ok(!stale.ids['is-list'].textContent.includes('stale.img'));
  const calls=[],item={name:'image.img',kind:'disk-image',url:'https://repo/image.img'};
  const download=setup((url,payload)=>{calls.push({url,payload});return Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[item]}:{task:{id:'existing-task',state:'running'}});});
  download.store.open();await flush();let button=download.buttons().find(n=>n.textContent==='下载到控制机');button.fire();button.fire();await flush();
  assert.equal(calls.filter(c=>c.url==='/api/image-download').length,1);assert.equal(download.watches.length,1);
  download.ids['is-close'].fire();download.buttons(download.ids['is-downloads']).find(n=>n.textContent==='取消下载').fire();await flush();
  assert.equal(calls.find(c=>c.url==='/api/task-cancel').payload.id,'existing-task');
  let submissions=0;
  const unknown=setup(url=>{if(url.startsWith('/api/image-catalog'))return Promise.resolve({entries:[item]});submissions++;return Promise.reject(new Error('lost response'));});
  unknown.store.open();await flush();button=unknown.buttons().find(n=>n.textContent==='下载到控制机');button.fire();await flush();button.fire();await flush();
  assert.equal(submissions,1);assert.ok(unknown.ids['is-status'].textContent.includes('提交结果未知'));
  let deleted=0,selected=0;
  const local=setup(url=>{if(url==='/api/artifact-delete'){deleted++;return Promise.resolve({ok:true});}return Promise.resolve({artifacts:[{id:'cache',name:'local.img',size:1}],tasks:[]});});
  local.store.open(()=>{selected++;});await local.source('local');
  local.buttons().find(n=>n.textContent==='删除').fire();assert.equal(deleted,0);
  local.consent(true);local.buttons().find(n=>n.textContent==='删除').fire();await flush();assert.equal(deleted,1);
  local.buttons().find(n=>n.textContent==='使用').fire();assert.equal(selected,1);assert.equal(local.ids['image-store-dialog'].open,false);
  const archive={id:'archive',name:'firmware.tar.gz',kind:'package',url:'https://repo/firmware.tar.gz',size:100};
  const scoped=setup(url=>Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[archive,item]}:{artifacts:[archive,{id:'raw',name:'opensbi.itb',size:1}],tasks:[]}));
  scoped.store.open(()=>{}, {fileOnly:true,partition:'opensbi'});await flush();
  assert.equal(scoped.ids['is-source'].value,'local');
  await scoped.source('public');
  assert.ok(!scoped.ids['is-list'].textContent.includes('firmware.tar.gz'));
  assert.ok(scoped.ids['is-list'].textContent.includes('image.img'));
  await scoped.source('local');assert.ok(!scoped.ids['is-list'].textContent.includes('firmware.tar.gz'));
  assert.ok(scoped.ids['is-list'].textContent.includes('opensbi.itb'));
  function autoHarness(){return setup(url=>Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[item,{...item,name:'other.img',url:'https://repo/other.img'}]}:{task:{id:url==='never'?'none':Math.random().toString(),state:'running'}}));}
  const auto=autoHarness();let chosen=null;
  auto.store.open(value=>{chosen=value;});await flush();
  auto.buttons().find(n=>n.textContent==='下载并使用').fire();await flush();
  auto.watches[0].resolve({artifact:{id:'downloaded',name:'image.img',state:'ready'}});await flush();
  assert.equal(chosen.id,'downloaded');assert.equal(auto.ids['image-store-dialog'].open,false);
  const staleDownload=autoHarness();let oldChoices=0,newChoices=0;
  staleDownload.store.open(()=>{oldChoices++;});await flush();staleDownload.buttons().find(n=>n.textContent==='下载并使用').fire();await flush();
  staleDownload.ids['is-close'].fire();staleDownload.store.open(()=>{newChoices++;});await flush();
  staleDownload.watches[0].resolve({artifact:{id:'old-download',name:'image.img'}});await flush();
  assert.equal(oldChoices,0);assert.equal(newChoices,0);assert.equal(staleDownload.ids['image-store-dialog'].open,true);
  const latest=autoHarness();let latestChoice=null;
  latest.store.open(value=>{latestChoice=value;});await flush();
  latest.buttons().filter(n=>n.textContent==='下载并使用').forEach(n=>n.fire());await flush();
  latest.watches[0].resolve({artifact:{id:'first',name:'image.img'}});await flush();assert.equal(latestChoice,null);
  latest.watches[1].resolve({artifact:{id:'second',name:'other.img'}});await flush();assert.equal(latestChoice.id,'second');
  const changed=autoHarness();
  changed.store.open(()=>{throw new Error('target changed');});await flush();changed.buttons().find(n=>n.textContent==='下载并使用').fire();await flush();
  changed.watches[0].resolve({artifact:{id:'changed',name:'image.img'}});await flush();
  assert.equal(changed.ids['image-store-dialog'].open,true);assert.equal(changed.ids['is-status'].textContent,'target changed');
  assert.ok(!changed.ids['is-downloads'].textContent.includes('已返回当前选择'));
  console.log('10 image-store tests passed: filtering, direct use, stale dialogs, changed targets, latest selection and existing task safety.');
})().catch(error=>{console.error(error);process.exitCode=1;});
