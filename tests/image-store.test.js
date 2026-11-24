'use strict';
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.events={};this.value='';this.disabled=false;this.open=false;this._text='';}
  set textContent(value){this._text=value;this.children=[];} get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  appendChild(node){node.parent=this;this.children.push(node);} remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,fn){this.events[name]=fn;} showModal(){this.open=true;} close(){this.open=false;}
  fire(name='click'){return this.events[name]&&this.events[name]();}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(request, storage=new Map()){
  const ids={};for(const id of ['is-version-caption','is-catalog','is-platform','is-os','is-channel','is-version','is-variants','is-title','image-store-dialog','is-list','is-search','is-path','is-status','is-refresh','is-downloads','is-download-note','is-source','is-close'])ids[id]=new Element();
  ids['is-source'].value='public';let consent=false;const watches=[];
  const window={crypto:{randomUUID:()=> 'fixture-request'}, confirm:()=>consent,
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    SerialdTasks:{createClient:()=>({request,watch:(task,ctx,update)=>new Promise((resolve,reject)=>watches.push({task,update,resolve,reject}))})}};
  vm.runInNewContext(fs.readFileSync('static/image-store.js','utf8'),{window,document:{createElement:()=>new Element()}});
  const store=window.SerialdImageStore.create({byId:id=>ids[id],formatBytes:n=>n+' bytes'});
  function buttons(node=ids['is-list']){return node.children.flatMap(n=>[n,...buttons(n)]).filter(n=>n.events.click);}
  return {ids,store,buttons,watches,consent:value=>{consent=value;},source:async value=>{ids['is-source'].value=value;ids['is-source'].fire('change');await flush();}};
}
(async()=>{
  const organization=setup(()=>Promise.resolve({sources:[{id:'public'}],entries:[]}));
  const option=new Element();option.value='internal';organization.ids['is-source'].appendChild(option);
  organization.store.open();await flush();assert.equal(option.disabled,true);
  const configured=setup(()=>Promise.resolve({sources:[{id:'public'},{id:'internal'}],entries:[]}));
  const configuredOption=new Element();configuredOption.value='internal';configuredOption.disabled=true;configured.ids['is-source'].appendChild(configuredOption);
  configured.store.open();await flush();assert.equal(configuredOption.disabled,false);
  const streamImage={name:'Debian-13-GNOME-K1.tar.gz',kind:'package',url:'https://repo.example/os.tar.gz'};
  const streamCalls=[];let streamChoice;
  const stream=setup((url,payload)=>{
    streamCalls.push({url,payload});
    return Promise.resolve({entries:[streamImage,
      {name:'Debian-13-Firmware-K1.tar.gz',kind:'package',url:'https://repo.example/firmware.tar.gz'},
      {name:'Another.zip',kind:'package',url:'https://repo.example/other.zip'}]});
  });
  const localOption=new Element();localOption.value='local';stream.ids['is-source'].appendChild(localOption);
  stream.store.open(item=>{streamChoice=item;},{streamURL:url=>url.endsWith('.tar.gz'),chip:'k1'});await flush();
  assert.equal(stream.ids['is-platform'].value,'k1');
  assert.equal(localOption.disabled,true);
  assert.deepEqual(stream.buttons().map(node=>node.textContent),['填入 URL']);
  assert.equal(stream.ids['is-list'].children.length,1);
  stream.buttons()[0].fire();assert.equal(streamChoice,streamImage);
  assert.equal(stream.ids['image-store-dialog'].open,false);
  assert.ok(streamCalls.every(call=>!call.payload&&call.url.startsWith('/api/image-catalog')));
  stream.store.open();await flush();assert.equal(localOption.disabled,false);
  let resolvePublic;
  const stale=setup(url=> url.startsWith('/api/image-library')?Promise.resolve({artifacts:[{id:'ready',name:'cached.img',size:1}],tasks:[]}):new Promise(resolve=>{resolvePublic=resolve;}));
  stale.store.open();await stale.source('local');resolvePublic({entries:[{name:'stale.img',kind:'disk-image',url:'https://old'}]});await flush();
  assert.ok(stale.ids['is-list'].textContent.includes('cached.img'));assert.ok(!stale.ids['is-list'].textContent.includes('stale.img'));
  const calls=[],item={name:'opensbi.itb',kind:'partition-file',url:'https://repo/opensbi.itb'};
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
  const local=setup(url=>{if(url==='/api/artifact-delete'){deleted++;return Promise.resolve({ok:true});}return Promise.resolve({artifacts:[{id:'cache',name:'local.itb',size:1}],tasks:[]});});
  local.store.open(()=>{selected++;});await local.source('local');
  local.buttons().find(n=>n.textContent==='删除').fire();assert.equal(deleted,0);
  local.consent(true);local.buttons().find(n=>n.textContent==='删除').fire();await flush();assert.equal(deleted,1);
  local.buttons().find(n=>n.textContent==='使用').fire();assert.equal(selected,1);assert.equal(local.ids['image-store-dialog'].open,false);
  const fwPicker=setup(()=>Promise.resolve({artifacts:[
    {id:'primary',name:'os.tar.gz'},{id:'custom',name:'nightly.zip'},{id:'payload',name:'boot.bin'}],tasks:[]}));
  let picked;
  fwPicker.store.open(item=>{picked=item;},{firmwareOnly:true,excludeId:'primary'});await flush();
  assert.equal(fwPicker.ids['is-source'].value,'local');
  assert.ok(fwPicker.ids['is-list'].textContent.includes('nightly.zip'));
  assert.doesNotMatch(fwPicker.ids['is-list'].textContent,/os\.tar|boot\.bin/);
  fwPicker.buttons().find(n=>n.textContent==='使用').fire();assert.equal(picked.id,'custom');
  const pairedOS={name:'Debian-13-GNOME-K3-v1.0rc1-202611180100.tar.gz',kind:'package',url:'https://repo/os.tar.gz'};
  const pairedFW={name:'Debian-13-Firmware-K3-v1.0rc1-202611180055.tar.gz',kind:'package',url:'https://repo/fw.tar.gz'};
  let pairChoice;const pairCalls=[];
  const paired=setup((url,payload)=>{pairCalls.push({url,payload});return Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[pairedOS,pairedFW]}:{task:{id:payload.name,state:'running'}});});
  paired.store.open(value=>{pairChoice=value;});await flush();
  assert.ok(paired.ids['is-list'].textContent.includes('同时选择'));
  paired.buttons().find(n=>n.textContent==='下载并使用').fire();await flush();
  assert.equal(pairCalls.filter(c=>c.url==='/api/image-download').length,2);assert.equal(paired.watches.length,2);
  paired.watches[0].resolve({artifact:{id:'os-selected',name:pairedOS.name}});paired.watches[1].resolve({artifact:{id:'fw-selected',name:pairedFW.name}});await flush();await flush();
  assert.equal(pairChoice.id,'os-selected');assert.equal(pairChoice.firmware.id,'fw-selected');
  let streamedPair;
  const olderFirmware = {...pairedFW, name:'Debian-13-Firmware-K3-v1.0rc1-202611150055.tar.gz'};
  const streamVersion = setup(() => Promise.resolve({entries:[pairedOS,olderFirmware]}));
  streamVersion.store.open(item => { streamedPair = item; }, {streamURL:url => url.endsWith('.tar.gz')});
  await flush();
  assert.equal(streamVersion.ids['is-list'].children.length, 1);
  assert.match(streamVersion.ids['is-list'].textContent, /配套 Firmware.*Debian-13-Firmware-K3/);
  streamVersion.buttons()[0].fire();
  assert.equal(streamedPair.url, pairedOS.url);
  assert.equal(streamedPair.companionFirmware, olderFirmware.name);
  let cachedPair;
  const savedPair=setup((url,payload)=>Promise.resolve(url.startsWith('/api/image-catalog')
    ? {entries:[{...pairedOS,download_state:'cached'},{...pairedFW,download_state:'cached'}]}
    : {task:{id:payload.name,state:'completed',result:{artifact:{id:payload.name,name:payload.name}}}}));
  savedPair.store.open(artifact=>{cachedPair=artifact;});await flush();
  savedPair.buttons().find(n=>n.textContent==='使用缓存').fire();await flush();
  assert.equal(cachedPair.id,pairedOS.name);assert.equal(cachedPair.firmware.id,pairedFW.name);
  assert.equal(savedPair.watches.length,0);assert.equal(savedPair.ids['is-downloads'].children.length,0);
  const k1FirmwareA={...pairedFW,name:'Debian-13-Firmware-K1-v1.0rc1-202611170426.tar.gz',url:'https://repo/fw-a.tar.gz'};
  const k1FirmwareB={...pairedFW,name:'Debian-13-Firmware-K1-v1.0rc1-202611172244.tar.gz',url:'https://repo/fw-b.tar.gz'};
  const k1OsA={...pairedOS,name:'Debian-13-GNOME-K1-v1.0rc1-202611170438.tar.gz',url:'https://repo/os-a.tar.gz'};
  const k1OsB={...pairedOS,name:'Debian-13-GNOME-K1-v1.0rc1-202611172255.tar.gz',url:'https://repo/os-b.tar.gz'};
  const versioned=setup(()=>Promise.resolve({entries:[k1FirmwareA,k1FirmwareB,k1OsA,k1OsB]}));
  versioned.store.open();await flush();
  for(const [os,firmware] of [[k1OsA,k1FirmwareA],[k1OsB,k1FirmwareB]]){
    const row=versioned.ids['is-list'].children.find(node=>node.children[0].children[0].textContent===os.name);
    assert.ok(row.textContent.includes('同时选择 '+firmware.name));
  }
  const ambiguous=setup(()=>Promise.resolve({entries:[k1OsB,k1FirmwareB,{...k1FirmwareB,url:'https://repo/duplicate.tar.gz'}]}));
  ambiguous.store.open();await flush();assert.ok(!ambiguous.ids['is-list'].textContent.includes('同时选择'));
  let crossBoardChoice;
  const crossBoard=setup(()=>Promise.resolve({artifacts:[{id:'prepared-on-a',name:'Debian.tar.gz',size:100,
    serial:'BOARD-A',state:'claimed',preparation_task:{id:'preparation-a',kind:'image-prepare',state:'running'}}],tasks:[]}));
  crossBoard.store.open(value=>{crossBoardChoice=value;},{source:'local',serial:'BOARD-B'});await flush();
  const usePrepared=crossBoard.buttons().find(n=>n.textContent==='使用');
  assert.equal(!!usePrepared.disabled,false);usePrepared.fire();assert.equal(crossBoardChoice.id,'prepared-on-a');
  const archive={id:'archive',name:'firmware.tar.gz',kind:'package',url:'https://repo/firmware.tar.gz',size:100};
  const scoped=setup(url=>Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[archive,item]}:{artifacts:[archive,{id:'raw',name:'opensbi.itb',size:1}],tasks:[]}));
  scoped.store.open(()=>{}, {fileOnly:true,partition:'opensbi'});await flush();
  assert.equal(scoped.ids['is-source'].value,'local');
  await scoped.source('public');
  assert.ok(!scoped.ids['is-list'].textContent.includes('firmware.tar.gz'));
  assert.ok(scoped.ids['is-list'].textContent.includes('opensbi.itb'));
  await scoped.source('local');assert.ok(!scoped.ids['is-list'].textContent.includes('firmware.tar.gz'));
  assert.ok(scoped.ids['is-list'].textContent.includes('opensbi.itb'));
  function autoHarness(){return setup(url=>Promise.resolve(url.startsWith('/api/image-catalog')?{entries:[item,{...item,name:'other.itb',url:'https://repo/other.itb'}]}:{task:{id:url==='never'?'none':Math.random().toString(),state:'running'}}));}
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
  const initial=[];
  const sourced=setup(url=>{initial.push(url);return Promise.resolve(url.startsWith('/api/image-library')?{artifacts:[],tasks:[]}:{entries:[]});});
  sourced.store.open(()=>{},{source:'local'});await flush();assert.equal(sourced.ids['is-source'].value,'local');
  assert.equal(initial[0],'/api/image-library');
  sourced.store.open(()=>{},{source:'public'});await flush();assert.equal(sourced.ids['is-source'].value,'public');
  assert.ok(initial[1].startsWith('/api/image-catalog?source=public'));
  const dir=(name,path)=>({name,path,kind:'directory'});
  const pkg=(name)=>({name,kind:'package',url:'https://images.example.test/'+name});
  const tree={
    '':[dir('k3','k3/'),dir('k1','k1/')],
    'k3/':[dir('version','k3/version/'),dir('daily','k3/daily/')],
    'k3/version/':[dir('bianbu','k3/version/bianbu/'),dir('debian','k3/version/debian/')],
    'k3/daily/':[dir('20261116','k3/daily/20261116/'),dir('20261117','k3/daily/20261117/'),dir('debian','k3/daily/debian/')],
    'k3/daily/debian/':[dir('13','k3/daily/debian/13/')],
    'k3/daily/debian/13/':[dir('20261117','k3/daily/debian/13/20261117/'),dir('20261115','k3/daily/debian/13/20261115/')],
    'k3/daily/debian/13/20261117/':[pkg('Debian-13-Minimal-K3-202611170600.tar.gz')],
    'k3/version/bianbu/':[dir('v4.0.8','k3/version/bianbu/v4.0.8/'),dir('v4.0.10','k3/version/bianbu/v4.0.10/'),dir('v4.0.10rc1','k3/version/bianbu/v4.0.10rc1/')],
    'k3/version/bianbu/v4.0.10/':[pkg('Bianbu-LXQt-K3-v4.0.10.tar.gz'),pkg('Bianbu-Minimal-UEFI-K3-v4.0.10.tar.gz'),pkg('bad.img.gz'),pkg('bad.img.zip')],
    'k3/version/bianbu/v4.0.8/':[pkg('Bianbu-LXQt-K3-v4.0.8.tar.gz')],
    'k3/version/debian/':[pkg('Debian-13-GNOME-K3-202607301654.tar.gz'),pkg('Debian-13-GNOME-K3-202605140900.tar.gz')],
    'k3/daily/20261117/':[dir('cloud','k3/daily/20261117/cloud/'),pkg('Bianbu-LXQt-K3-20261117032354.tar.gz'),pkg('Debian-13-GNOME-K3-202611170510.tar.gz'),pkg('Debian-13-Firmware-K3-202611170457.tar.gz')],
    'k3/daily/20261117/cloud/':[dir('nested','k3/daily/20261117/cloud/nested/'),pkg('Bianbu-Cloud-K3-202611170510.tar.gz'),pkg('Debian-13-Cloud-K3-202611170510.tar.gz')],
    'k3/daily/20261117/cloud/nested/':[pkg('Debian-13-Cloud-K3-202611170600.tar.gz')],
    'k3/daily/20261116/':[pkg('Bianbu-Minimal-K3-20261116032354.tar.gz')],
    'k1/':[dir('version','k1/version/')],
    'k1/version/':[dir('buildroot','k1/version/buildroot/')],
    'k1/version/buildroot/':[dir('v1.0','k1/version/buildroot/v1.0/')],
    'k1/version/buildroot/v1.0/':[pkg('Buildroot-K1-v1.0.zip')]
  };
  const visited=[];
  const viewStorage=new Map();
  const catalogue=url=>{
    const target=new URL(url,'http://fixture');const path=target.searchParams.get('path')||'';
    visited.push(path);assert.ok(Object.hasOwn(tree,path),'unexpected crawl: '+path);
    return Promise.resolve({entries:tree[path]});
  };
  const browse=setup(catalogue,viewStorage);
  async function ready(){for(let n=0;n<20&&browse.ids['is-refresh'].disabled;n++)await flush();assert.equal(browse.ids['is-refresh'].disabled,false,browse.ids['is-status'].textContent);}
  async function clickAt(id,label){const node=browse.buttons(browse.ids[id]).find(n=>n.textContent===label);assert.ok(node,label);node.fire();await ready();}
  browse.store.open();await ready();
  assert.equal(browse.ids['is-catalog'].hidden,false);
  assert.ok(browse.ids['is-list'].textContent.includes('v4.0.10.tar.gz'));
  assert.ok(!browse.ids['is-list'].textContent.includes('.img'));
  assert.equal(browse.ids['is-version'].value,'k3/version/bianbu/v4.0.10/');
  assert.ok(!visited.includes('k3/version/bianbu/v4.0.8/')); // no eager history crawl
  assert.ok(!visited.includes('k3/daily/20261116/'));
  await clickAt('is-variants','Minimal');
  assert.ok(browse.ids['is-list'].textContent.includes('Minimal-UEFI'));
  assert.ok(!browse.ids['is-list'].textContent.includes('LXQt'));
  await clickAt('is-channel','Daily');
  assert.equal(browse.ids['is-version'].value,'day:20261117');
  assert.ok(browse.ids['is-list'].textContent.includes('Bianbu-LXQt'));
  assert.ok(!browse.ids['is-list'].textContent.includes('Debian'));
  browse.ids['is-version'].value='day:20261116';browse.ids['is-version'].fire('change');await ready();
  assert.ok(browse.ids['is-list'].textContent.includes('20261116'));
  await clickAt('is-os','Debian');
  assert.equal(browse.ids['is-version'].value,'day:20261117');
  assert.ok(browse.ids['is-list'].textContent.includes('Firmware'));
  assert.ok(browse.ids['is-list'].textContent.includes('Minimal'));
  assert.equal(browse.ids['is-version'].children.filter(n=>n.value==='day:20261117').length,1);
  assert.ok(!visited.includes('k3/daily/debian/13/20261115/'));
  assert.ok(!browse.ids['is-list'].textContent.includes('Bianbu'));
  assert.ok(!visited.includes('k3/daily/20261117/cloud/')); // no eager subfolder crawl
  await clickAt('is-list','cloud/');
  assert.equal(browse.ids['is-catalog'].hidden,false);
  assert.equal(browse.ids['is-version'].value,'day:20261117');
  assert.ok(browse.ids['is-list'].textContent.includes('Debian-13-Cloud'));
  assert.ok(!browse.ids['is-list'].textContent.includes('Bianbu'));
  assert.ok(browse.buttons(browse.ids['is-os']).find(n=>n.textContent==='Debian')['aria-pressed']==='true');
  await clickAt('is-list','nested/');
  assert.ok(browse.ids['is-list'].textContent.includes('202611170600'));
  assert.equal(browse.ids['is-catalog'].hidden,false);
  browse.ids['is-search'].value='cloud';
  browse.ids['is-search'].fire('input');
  browse.ids['is-list'].scrollTop=40;
  browse.ids['is-close'].fire();
  const reopened=setup(catalogue,viewStorage);
  reopened.store.open(()=>{}, {source:'local'});
  await flush();
  reopened.store.open();await flush();
  assert.equal(reopened.ids['is-version'].value,'day:20261117');
  assert.equal(reopened.ids['is-search'].value,'cloud');
  assert.equal(reopened.ids['is-list'].scrollTop,40);
  assert.ok(reopened.ids['is-list'].textContent.includes('202611170600'));
  browse.store.open();await ready();browse.ids['is-search'].value='';browse.ids['is-search'].fire('input');
  // Back to the selected day's root without changing OS, date or channel.
  const rootFolder=browse.ids['is-path'].children[0].children[0];
  rootFolder.value='';rootFolder.fire('change');await ready();
  assert.equal(browse.ids['is-catalog'].hidden,false);
  assert.equal(browse.ids['is-version'].value,'day:20261117');
  assert.ok(browse.ids['is-list'].textContent.includes('Firmware'));
  assert.ok(!browse.ids['is-list'].textContent.includes('Bianbu'));
  await clickAt('is-channel','版本');
  assert.ok(browse.ids['is-list'].textContent.includes('20260730'));
  assert.ok(!browse.ids['is-list'].textContent.includes('20260514'));
  browse.ids['is-platform'].value='k1';browse.ids['is-platform'].fire('change');await ready();
  assert.ok(browse.ids['is-list'].textContent.includes('Buildroot-K1'));
  assert.equal(browse.buttons(browse.ids['is-channel']).length,1);
  let lateVersion;
  const late=setup(url=>{
    if(url==='/api/image-library')return Promise.resolve({artifacts:[{id:'safe',name:'current.itb',size:1}],tasks:[]});
    const target=new URL(url,'http://fixture');const path=target.searchParams.get('path')||'';
    if(path==='k3/version/bianbu/v4.0.10/')return new Promise(resolve=>{lateVersion=resolve;});
    return Promise.resolve({entries:tree[path]});
  });
  late.store.open();for(let n=0;n<20&&!lateVersion;n++)await flush();assert.ok(lateVersion);
  await late.source('local');lateVersion({entries:[pkg('obsolete.tar.gz')]});await flush();
  assert.ok(late.ids['is-list'].textContent.includes('current.itb'));
  assert.ok(!late.ids['is-list'].textContent.includes('obsolete'));
  const cached=setup(()=>Promise.resolve({artifacts:[{id:'unsupported',name:'old.img',size:1}],tasks:[]}));
  cached.store.open(()=>{}, {source:'local'});await flush();
  assert.equal(cached.buttons().find(n=>n.textContent==='使用').disabled,true);
  assert.equal(cached.buttons().find(n=>n.textContent==='删除').disabled,false);
  console.log('Image-store tests passed: configured catalogs, filtering, direct use, source entry, stale dialogs, changed targets, latest selection and existing task safety.');
})().catch(error=>{console.error(error);process.exitCode=1;});
