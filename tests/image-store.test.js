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
  const ids={};for(const id of ['image-store-dialog','is-list','is-search','is-path','is-status','is-refresh','is-downloads','is-source','is-close'])ids[id]=new Element();
  ids['is-source'].value='public';let consent=false;const watches=[];
  const window={crypto:{randomUUID:()=> 'fixture-request'}, confirm:()=>consent,
    SerialdTasks:{createClient:()=>({request,watch:(task,ctx,update)=>{watches.push({task,update});return new Promise(()=>{});}})}};
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
  console.log('5 image-store tests passed: stale source, duplicate download, task cancellation, unknown submission, confirmed cache deletion/selection.');
})().catch(error=>{console.error(error);process.exitCode=1;});
