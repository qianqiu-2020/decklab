'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = ['tasks.js', 'flashing.js'].map(name =>
  fs.readFileSync(path.join(__dirname, '..', 'static', name), 'utf8')).join('\n');

class Element {
  constructor() { this.children = []; this.events = {}; this.dataset = {}; this.style = {}; this.value = ''; this.files = []; }
  set textContent(value) { this.content = value; this.children = []; }
  get textContent() { return String(this.content || '') + this.children.map(item => item.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  insertBefore(child, sibling) {
    this.children = this.children.filter(item => item !== child);
    this.children.splice(this.children.indexOf(sibling), 0, child);
  }
  setAttribute(name, value) { (this.attributes ||= {})[name] = value; }
  removeAttribute(name) { if (this.attributes) delete this.attributes[name]; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  emit(type, event = {}) { for (const fn of this.events[type] || []) fn(event); }
  click() { if (!this.disabled) this.emit('click'); }
  show() { this.open = true; }
  showModal() { this.show(); }
  close() { this.open = false; }
  focus() { this.focused = true; }
}
function harness(storage, controls = {}) {
  const elements = new Map(), requests = [], timers = new Map(); let timer = 0, imageChoice, imagePicker;
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('fl-setup').children = ['fl-preview-overall', 'fl-image-section', 'fl-connect', 'fl-fast', 'fl-tuning'].map(get);
  get('fl-mode').value = 'full'; get('fl-verify').value = 'none';
  const sandbox = {document: {createElement: () => new Element()}, Uint8Array, Date, Math, URL,
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    setTimeout: fn => { timers.set(++timer, fn); return timer; }, clearTimeout: id => timers.delete(id),
    crypto: {getRandomValues: values => values.fill(++timer)}};
  if (storage) sandbox.localStorage = {getItem:key => storage.get(key), setItem:(key,value) => storage.set(key,value)};
  if (storage) sandbox.sessionStorage = {getItem:key => storage.get(key) || null,
    setItem:(key,value) => storage.set(key,value), removeItem:key => storage.delete(key)};
  sandbox.window = sandbox;
  vm.runInNewContext(source, sandbox);
  const api = sandbox.SerialdFlashing.create({byId: get, formatBytes: value => value + ' B', ...controls,
    openImageStore: (choose,picker) => {imageChoice=choose;imagePicker=picker;},
    fetch: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, options,
      payload: options.body && JSON.parse(options.body), reject,
      resolve: (data, status = 200) => resolve({ok: status < 400, status, json: async () => data})}))});
  function take(prefix) { const index = requests.findIndex(entry => entry.url.startsWith(prefix));
    assert.notEqual(index, -1, 'missing request ' + prefix); return requests.splice(index, 1)[0]; }
  return {api, get, requests, timers, take, chooseImage:item=>imageChoice(item), imagePicker:()=>imagePicker};
}
async function drain() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
const board = {key: 'board-1', serial: 'SN-1', label: 'Pico'};
const artifact = {id: 'image-1', name: 'image.bin', size: 32, sha256: 'a'.repeat(64)};
const capabilities = {supported: true, binding_revision: 7, profile: 'test-board',
  modes: ['full', 'partitions'], artifacts: [artifact], console: null,
  executor: {provider: 'goflash', node: 'local', endpoint: 'usb-slot-1'}, storages: [
    {id: 'disk-a', name: 'eMMC', partitions: [{id: 'rootfs-a', name: 'rootfs', size: 64}]},
    {id: 'disk-b', name: 'SD', partitions: [{id: 'rootfs-b', name: 'rootfs', size: 128}]}]};
async function open(h) {
  h.api.open(board); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change');
}
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('image preparation is independent, reports byte progress and gates preview until complete', async () => {
  const h=harness(); h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,image_preparation_supported:true}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.get('fl-artifact').value=artifact.id; h.get('fl-artifact').emit('change');
  const request=h.take('/api/image-prepare'); assert.equal(request.payload.id,artifact.id);
  assert.equal(request.payload.serial,undefined);assert.equal(h.get('fl-plan').disabled,true);
  request.resolve({task:{id:'prepare-one',kind:'image-prepare',state:'running',progress:{phase:'checking-payloads',done:16,total:32}}}); await drain();
  assert.match(h.get('fl-image-preparation-status').textContent,/校验包内文件/);
  assert.match(h.get('fl-image-preparation-bytes').textContent,/50%/);
  h.get('fl-close').click();h.api.open({...board});await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/image-prepare'),false);
  h.take('/api/task?id=prepare-one').resolve({task:{id:'prepare-one',state:'completed',result:{preparation:{catalogue:[{name:'partition_universal.json',format:'gpt',selected:true,partitions:[{name:'rootfs'}]}]}}}});await drain();
  assert.equal(h.get('fl-plan').disabled,false);assert.match(h.get('fl-image-information').textContent,/rootfs/);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('refresh exposes recovery of the original preparation without a new task or board action', async () => {
  const h=harness();h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,image_preparation_supported:true});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.get('fl-artifact').value=artifact.id;h.get('fl-artifact').emit('change');
  const original={id:'unknown-image-original',kind:'image-prepare',state:'interrupted',error:'original process requires recovery',
    flash:{recovery_required:true,attempt_id:'original-image-attempt'}};
  h.take('/api/image-prepare').resolve({task:original});await drain();
  assert.equal(h.get('fl-image-recovery').hidden,false);assert.equal(h.get('fl-image-preparation-retry').hidden,true);
  h.get('fl-image-stopped').checked=true;h.get('fl-image-recovery-reason').value='confirmed process stopped';
  h.get('fl-image-recover').click();const recover=h.take('/api/image-prepare');
  assert.equal(recover.payload.action,'recover');assert.equal(recover.payload.task_id,original.id);
  assert.equal(recover.payload.attempt_id,original.flash.attempt_id);assert.equal(recover.payload.serial,undefined);
  recover.resolve({task:{...original,flash:{...original.flash,recovery_required:false}}});await drain();
  assert.equal(h.get('fl-image-recovery').hidden,true);assert.equal(h.get('fl-image-preparation-retry').hidden,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'||r.url==='/api/flash-plan'),false);
});

test('refresh restores the selected image and options but refreshes board capabilities', async () => {
  const storage=new Map(), first=harness(storage);await open(first);
  first.get('fl-verify').value='none';first.get('fl-verify').emit('change');
  const next=harness(storage);next.api.open({...board});next.take('/api/flash-capabilities').resolve(capabilities);await drain();
  next.take('/api/tasks').resolve({tasks:[]});await drain();
  assert.equal(next.get('fl-artifact').value,artifact.id);assert.equal(next.get('fl-verify').value,'none');
  assert.equal(next.get('fl-start').disabled,true);
});

test('choosing an OS with its optional firmware previews both and clearing it restores a single image', async () => {
  const h=harness();await open(h);
  const firmware={id:'paired-fw',name:'Debian-13-Firmware-K3.tar.gz',size:100,sha256:'b'.repeat(64)};
  const os={id:'paired-os',name:'Debian-13-GNOME-K3.tar.gz',size:200,sha256:'c'.repeat(64),firmware};
  h.get('fl-cache').click();h.chooseImage(os);await drain();
  assert.equal(h.get('fl-artifact').value,os.id);assert.equal(h.get('fl-firmware').value,firmware.id);
  h.get('fl-plan').click();const paired=h.take('/api/flash-plan');
  assert.equal(paired.payload.artifact_id,os.id);assert.equal(paired.payload.firmware_artifact_id,firmware.id);
  paired.resolve({plan:{id:'pair-preview',ready:true,components:[{label:'Firmware',name:firmware.name,ready:true},{label:'OS',name:os.name,ready:true}],writes:[{storage:'NOR',partition:'fsbl',size:100},{storage:'scsi:0',partition:'rootfs',size:200}]}});await drain();
  assert.match(h.get('fl-preview').textContent,/Firmware/);assert.ok(h.get('fl-preview').textContent.includes(os.name));
  h.get('fl-firmware').value='';h.get('fl-firmware').emit('change');assert.equal(h.get('fl-start').disabled,true);
  h.get('fl-plan').click();const single=h.take('/api/flash-plan');assert.equal(single.payload.firmware_artifact_id,undefined);
  single.resolve({plan:{id:'single-preview',writes:[{storage:'scsi:0',partition:'rootfs',size:200}]}});await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('manually replace an automatic firmware from disk or library without replacing the OS', async () => {
  const h = harness(); await open(h);
  const original = {...artifact, id:'paired', name:'Firmware.tar.gz'};
  const os = {...artifact, id:'os', name:'Debian-OS.tar.gz', firmware:original};
  h.get('fl-cache').click(); h.chooseImage(os); await drain();
  h.get('fl-firmware-choose').click();
  assert.equal(h.imagePicker().firmwareOnly, true);
  assert.equal(h.imagePicker().excludeId, os.id);
  assert.throws(() => h.chooseImage(os), /不同的固件包/);
  h.chooseImage({...artifact,id:'cached',name:'custom.zip'});
  assert.equal(h.get('fl-firmware').value,'cached');
  h.get('fl-firmware-local').click();
  const input = h.get('fl-firmware-file');
  input.files = [{name:'nightly.tar.gz',size:1,slice:()=>({arrayBuffer:async()=>new Uint8Array([1]).buffer})}];
  input.onchange();
  const local = {...artifact,id:'local',name:'nightly.tar.gz',size:1};
  const create = h.take('/api/artifact-create'); assert.equal(create.payload.name,local.name);
  create.resolve({artifact:local}); await drain();
  h.take('/api/artifact-append').resolve({artifact:{received:1}}); await drain();
  h.take('/api/artifact-seal').resolve({artifact:local}); await drain();
  assert.equal(h.get('fl-artifact').value,os.id);
  assert.equal(h.get('fl-firmware').value,local.id);
  h.get('fl-plan').click();
  const preview = h.take('/api/flash-plan');
  assert.equal(preview.payload.artifact_id,os.id);
  assert.equal(preview.payload.firmware_artifact_id,local.id);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('a K1 target identifies a retained K3 package and a mismatched companion before preview', async () => {
  const fw = {id:'fw',name:'Debian-13-Firmware-K3-v1.0rc1-202611172035.tar.gz',size:10};
  const wrong = {id:'os',name:'Debian-13-GNOME-K3-v1.0rc1-202611170510.tar.gz',size:20};
  const good = {id:'new',name:'Debian-13-GNOME-K1-v1.0rc1-202611172255.tar.gz',size:20,firmware:fw};
  const h=harness(new Map([['decklab-flash-image',JSON.stringify({id:wrong.id,mode:'full',firmware:fw.id})]]));
  h.api.open(board); h.take('/api/flash-capabilities').resolve({...capabilities,chip:'k1',artifacts:[wrong,fw]});
  await drain(); h.take('/api/tasks').resolve({tasks:[]}); await drain();
  assert.equal(h.get('fl-plan').disabled,true);
  assert.ok(h.get('fl-chip-warning').textContent.includes(wrong.name));
  h.get('fl-cache').click();h.chooseImage(good);await drain();
  assert.equal(h.get('fl-plan').disabled,true);
  assert.ok(h.get('fl-chip-warning').textContent.includes(fw.name));
  h.get('fl-firmware').value=''; h.get('fl-firmware').emit('change');
  assert.equal(h.get('fl-plan').disabled,false);
  assert.equal(h.get('fl-chip-warning').hidden,true);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-plan'||r.url==='/api/task-start'),false);
});

test('paired image view groups NOR and OS with file sizes and independent versions', async () => {
  const h = harness();
  h.api.open(board); h.take('/api/flash-capabilities').resolve({...capabilities,image_preparation_supported:true});
  await drain(); h.take('/api/tasks').resolve({tasks:[]}); await drain();
  const fw = {id:'firmware',name:'Firmware-K3.tar.gz',size:100};
  const os = {id:'os',name:'Debian-GNOME-K3.tar.gz',size:200,firmware:fw};
  h.get('fl-cache').click(); h.chooseImage(os); await drain();
  for (let i=0;i<2;i++) {
    const request=h.take('/api/image-prepare'), isOS=request.payload.id===os.id;
    const part=isOS?'rootfs':'opensbi', file=isOS?'rootfs.ext4':'fw_dynamic.itb';
    request.resolve({task:{id:'job-'+request.payload.id,state:'completed',result:{preparation:{
      catalogue:[{name:'layout.json',selected:true,format:isOS?'gpt':'mtd',partitions:[{name:part,payload:file}]}],
      payload_metadata:[{partition:part,content:{size:80,version:isOS?'Debian 13':'OpenSBI 1.6'}}]}}}});
    await drain();
  }
  const view=h.get('fl-image-information').textContent;
  assert.match(view,/系统盘（OS）.*rootfs.ext4.*80 B.*Debian 13/);
  assert.match(view,/NOR.*fw_dynamic.itb.*80 B.*OpenSBI 1.6/);
  h.get('fl-firmware').value=''; h.get('fl-firmware').emit('change');
  assert.doesNotMatch(h.get('fl-image-information').textContent,/OpenSBI/);
});

test('a mixed OS package offers only its SPI NOR table as the optional firmware', async () => {
  const h = harness(), os = {id:'mixed-os', name:'Buildroot-K3.zip'};
  h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities, image_preparation_supported:true});
  await drain(); h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.get('fl-cache').click(); h.chooseImage(os); await drain();
  const table = (name, format, payload) => ({name, format, selected:true,
    partitions:[{name:'bootinfo', offset:0, size:1024, payload, available:true}]});
  const catalogue = [table('partition_4M.json','mtd','factory/bootinfo_spinor.bin'),
    table('partition_64M.json','mtd','factory/bootinfo_spinand.bin'),
    table('partition_universal.json','gpt','rootfs.ext4')];
  h.take('/api/image-prepare').resolve({task:{id:'prep-mixed', state:'completed',
    result:{preparation:{catalogue}}}});
  await drain();
  const select = h.get('fl-firmware');
  assert.deepEqual(Array.from(select.children, row=>row.value).filter(v=>v.startsWith('table:')), ['table:partition_4M.json']);
  select.value='table:partition_4M.json'; select.emit('change');
  h.get('fl-plan').click();
  const request = h.take('/api/flash-plan');
  assert.equal(request.payload.artifact_id, os.id);
  assert.equal(request.payload.firmware_table, 'partition_4M.json');
  assert.equal(request.payload.firmware_artifact_id, undefined);
});

test('table choice excludes SD media and binds the selected layout revision to the firmware slot', async () => {
  const h=harness(), fw={id:'table-fw',name:'Firmware-K1.tar.gz'}, os={id:'table-os',name:'OS-K1.tar.gz',firmware:fw};
  h.api.open(board);h.take('/api/flash-capabilities').resolve({...capabilities,image_preparation_supported:true,chip:'k1'});
  await drain();h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.get('fl-cache').click();h.chooseImage(os);await drain();
  const part={name:'bootinfo',offset:0,size:1024,payload_size:80,available:true};
  for(let i=0;i<2;i++){
    const r=h.take('/api/image-prepare'), firmware=r.payload.id===fw.id;
    const table=(name,format,selected)=>({name,format,selected,partitions:[part]});
    const catalogue=firmware?[table('partition_nor.json','mtd',true)]:[table('partition_4M.json','mtd',false),
      table('partition_flash.json','gpt',false),table('partition_universal.json','gpt',true)];
    r.resolve({task:{id:'prep-'+i,state:'completed',result:{preparation:{layout_version:1,chip_families:['k1'],catalogue}}}});await drain();
  }
  const view=h.get('fl-image-information');
  assert.match(view.textContent,/镜像分区.*包内载荷/);assert.doesNotMatch(view.textContent,/partition_flash/);
  const field=view.children[0].children.find(x=>x.textContent.includes('刷写分区表')),select=field.children[0];
  select.value='1';select.emit('change');
  const r=h.take('/api/image-workspace');
  assert.deepEqual([r.payload.id,r.payload.table_id,r.payload.table,r.payload.path],[fw.id,os.id,'partition_4M.json','partition_nor.json']);
  r.resolve({task:{id:'revised',state:'completed',result:{artifact:{id:'fw-revised',name:'Firmware [partition_4M.json].directory'}}}});
  await drain();assert.equal(h.get('fl-firmware').value,'fw-revised');assert.equal(h.get('fl-artifact').value,os.id);
  h.take('/api/image-prepare').resolve({task:{id:'prep-revised',state:'completed',result:{preparation:{
    layout_version:1,table_choice:{name:'partition_4M.json',destination:'partition_nor.json',original_id:fw.id},
    catalogue:[{name:'partition_nor.json',format:'mtd',selected:true,partitions:[part]}]}}}});
  await drain();
  const picker=view.children[0].children.find(x=>x.textContent.includes('刷写分区表')).children[0];
  assert.equal(picker.children[0].textContent,'partition_4M.json');
  picker.value='1';picker.emit('change');assert.equal(h.get('fl-firmware').value,fw.id);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'||r.url==='/api/image-workspace'),false);
});

test('readback can be skipped explicitly without cancelling or restarting the original task', async () => {
  const h=harness(),target={target_id:'skip-usb',target_generation:'original-loader'};
  const current={id:'skip-task',kind:'flash',state:'running',progress:{phase:'verifying-system',done:10,total:32},
    flash_targets:[{target_id:target.target_id,generation:target.target_generation}],flash:{can_skip_verification:true}};
  h.api.open(target,current);await drain();
  assert.equal(h.get('fl-skip-verify').hidden,false);assert.equal(h.get('fl-skip-verify').disabled,false);
  h.get('fl-skip-verify').click();const request=h.take('/api/flash-selection');
  assert.equal(request.payload.id,current.id);assert.equal(request.payload.action,'skip-verification');
  assert.equal(request.payload.target_generation,'original-loader');assert.equal(h.get('fl-skip-verify').disabled,true);
  request.resolve({task:{...current,flash:{...current.flash,verify_skip_requested:true}}});await drain();
  assert.equal(h.get('fl-skip-verify').disabled,true);
  const poll=[...h.timers.values()][0];poll();await drain();
  h.take('/api/task?id=').resolve({task:{...current,state:'completed',result:{write_result:'completed',verify_result:'skipped'}}});await drain();
  assert.equal(h.get('fl-skip-verify').hidden,true);assert.match(h.get('fl-results').textContent,/用户跳过，校验未完成/);
  assert.equal(h.requests.some(r=>r.url==='/api/task-cancel'||r.url==='/api/task-start'),false);
  const writing=harness();writing.api.open({...target},{...current,progress:{phase:'writing'}});await drain();
  assert.equal(writing.get('fl-skip-verify').hidden,true);
});

test('full package is directly previewed without opening or submitting storage backup', async () => {
  const h=harness(),pkg={id:'full-firmware',name:'Firmware.tar.gz',size:1024};
  h.api.open({target_id:'direct-full',target_generation:'direct-generation'});
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.take('/api/flash-target').resolve({capabilities:{...capabilities,nor_workspace_supported:true,
    storage_inventory_supported:true,reset_after_supported:true,modes:['full','partitions']}});await drain();
  assert.equal(h.get('fl-mode').value,'full');assert.equal(h.get('fl-storage-details').open,false);
  h.get('fl-cache').click();h.chooseImage(pkg);assert.equal(h.get('fl-plan').disabled,false);
  h.get('fl-plan').click();const preview=h.take('/api/flash-plan');assert.equal(preview.payload.mode,'full');
  assert.equal(preview.payload.artifact_id,pkg.id);assert.deepEqual(preview.payload.selections,[]);
  preview.resolve({plan:{id:'direct-full-plan',writes:[{storage:'active',partition:'fsbl',size:32}]}});await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.get('fl-start').click();const start=h.take('/api/task-start');
  assert.equal(start.payload.kind,'flash');assert.equal(start.payload.plan_id,'direct-full-plan');
  assert.equal(h.requests.some(r=>r.payload && r.payload.kind==='nor-backup'),false);
});

test('async preview displays actual extraction progress and only returns a reviewed plan', async () => {
  const h=harness();await open(h);
  h.get('fl-plan').click();const submitted=h.take('/api/flash-plan');assert.ok(submitted.payload.request_id);
  assert.match(h.get('fl-progress-text').textContent,/核对上传/);
  submitted.resolve({task:{id:'preview-task',kind:'flash-preview',state:'running',progress:{phase:'expanding-image',done:8,total:32}}},202);await drain();
  assert.match(h.get('fl-progress-text').textContent,/解压镜像.*25%/);
  assert.equal(h.get('fl-start').disabled,true);assert.equal(h.get('fl-cancel').textContent,'取消预览');
  h.take('/api/task?id=preview-task').resolve({task:{id:'preview-task',state:'completed',result:{plan:{id:'preview-plan',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}}});await drain();
  assert.equal(h.get('fl-start').disabled,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('cancel during asynchronous extraction cancels only the preview task', async () => {
  const h=harness();await open(h);h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({task:{id:'cancel-preview',state:'running',progress:{phase:'expanding-image',done:1,total:32}}},202);await drain();
  h.get('fl-cancel').click();const cancel=h.take('/api/task-cancel');assert.equal(cancel.payload.id,'cancel-preview');cancel.resolve({ok:true});
  h.take('/api/task?id=cancel-preview').resolve({task:{id:'cancel-preview',state:'cancelled',error:'task cancelled'}});await drain();
  assert.equal(h.get('fl-start').disabled,true);assert.equal(h.get('fl-plan').disabled,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('closing and reopening a busy preview keeps its original watcher and all stage estimates', async () => {
  const h=harness();await open(h);h.get('fl-plan').click();
  const current={id:'retained-preview',kind:'flash-preview',state:'running',progress:{phase:'expanding-image',done:8,total:32},
    flash:{preview_progress:{active:true,state:'running',sample_count:0,updated:Date.now()/1000,stages:[
      {phase:'checking-upload',state:'completed',elapsed_seconds:2,estimated_seconds:2},
      {phase:'checking-image-sha256',state:'completed',elapsed_seconds:2,estimated_seconds:2},
      {phase:'expanding-image',state:'running',elapsed_seconds:3,estimated_seconds:12},
      {phase:'checking-payloads',state:'pending',estimated_seconds:10},
      {phase:'checking-layout',state:'pending',estimated_seconds:5},
      {phase:'reading-payload-metadata',state:'pending',estimated_seconds:3}]}}};
  h.take('/api/flash-plan').resolve({task:current},202);await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'setup');
  assert.equal(h.get('fl-preview-overall').hidden,false);
  assert.equal(h.get('fl-preview-stages').children.length,6);
  assert.match(h.get('fl-overall-text').textContent,/整体预览进度.*初步估算剩余/);
  h.get('fl-close').click();h.api.open({...board});await drain();
  assert.equal(h.get('flash-dialog').open,true);
  assert.equal(h.requests.some(r=>r.url.startsWith('/api/tasks')||r.url.startsWith('/api/flash-capabilities')),false);
  assert.match(h.get('fl-task-id').textContent,/retained-preview/);
  h.take('/api/task?id=retained-preview').resolve({task:{...current,state:'completed',
    result:{plan:{id:'retained-plan',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}}});await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'review');
  assert.equal(h.get('fl-preview-overall').hidden,true);
  assert.equal(h.get('fl-start').disabled,false);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-plan'||r.url==='/api/task-start'),false);
  h.get('fl-start').click();h.take('/api/task-start').resolve({task:{id:'next-write',kind:'flash',state:'running',progress:{phase:'writing',done:8,total:32}}});await drain();
  assert.equal(h.get('fl-preview-overall').hidden,true);
  assert.equal(h.get('fl-execution-title').textContent,'刷写与校验');
});

test('page refresh recovers preview by saved request ID and restores its original image and intent', async () => {
  const storage=new Map(),h=harness(storage);await open(h);h.get('fl-plan').click();
  const submitted=h.take('/api/flash-plan');submitted.reject(new Error('lost response'));await drain();
  const fresh=harness(storage);fresh.api.open(board);
  fresh.take('/api/flash-capabilities').resolve(capabilities);await drain();
  const lookup=fresh.take('/api/task?request_id=');assert.ok(lookup.url.includes(submitted.payload.request_id));
  lookup.resolve({task:{id:'refresh-preview',kind:'flash-preview',state:'completed',updated:Date.now()/1000,
    flash:{preview_intent:{artifact_id:artifact.id,mode:'full',verify:'none',reset_after:true},preview_source:artifact},
    result:{plan:{id:'refresh-plan',verify:'none',reset_after:true,writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}}});await drain();
  assert.equal(fresh.get('fl-artifact').value,artifact.id);
  assert.equal(fresh.get('fl-verify').value,'none');assert.equal(fresh.get('fl-reset-after').checked,true);
  assert.equal(fresh.get('fl-start').disabled,false);
  assert.equal(fresh.requests.some(r=>r.url==='/api/flash-plan'||r.url==='/api/task-start'),false);
});

test('server task history restores an active preview when browser storage is unavailable', async () => {
  const h=harness();h.api.open(board);h.take('/api/flash-capabilities').resolve(capabilities);await drain();
  h.take('/api/tasks').resolve({tasks:[{id:'server-preview',kind:'flash-preview',state:'running',
    progress:{phase:'checking-layout'},flash:{preview_intent:{artifact_id:artifact.id,mode:'full'},preview_source:artifact}}]});await drain();
  assert.match(h.get('fl-task-id').textContent,/server-preview/);
  assert.equal(h.get('fl-cancel').textContent,'取消预览');
  h.take('/api/task?id=server-preview').resolve({task:{id:'server-preview',state:'failed',error:'layout changed'}});await drain();
  assert.match(h.get('fl-status').textContent,/layout changed/);assert.equal(h.get('fl-start').disabled,true);
});

test('ordinary open ignores a legacy completed receipt and restores the current running flash', async () => {
  const storage = new Map([['decklab-flash-task:' + board.serial, 'old-receipt']]), h = harness(storage);
  h.api.open(board); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  const old = {id:'old', kind:'flash', state:'completed', request_id:'old-receipt',
    progress:{phase:'verifying-system', done:12884901888, total:12884901888}};
  h.take('/api/task?request_id=old-receipt').resolve({task:old}); await drain();
  h.take('/api/tasks').resolve({tasks:[old, {id:'current', kind:'flash', state:'running',
    progress:{phase:'writing', done:10, total:100}}]}); await drain();
  assert.match(h.get('fl-task-id').textContent, /current/);
  assert.doesNotMatch(h.get('fl-progress-text').textContent, /12884901888/);
});

test('an unseen write receipt opens once; reopening after completion starts in setup', async () => {
  const key = 'decklab-flash-task:' + board.serial;
  const storage = new Map([[key,'write/latest']]), h = harness(storage);
  const latest = {id:'latest', request_id:'latest', kind:'flash', state:'completed',
    progress:{phase:'resetting', done:0, total:0},
    flash:{written_bytes:1883586158, write_result:'completed', verify_result:'not-requested'}};
  h.api.open(board); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/task?request_id=latest').resolve({task:latest}); await drain();
  assert.match(h.get('fl-progress-text').textContent, /本次写入.*1883586158 B/);
  assert.match(h.get('fl-results').textContent, /校验结果未执行/);
  assert.equal(h.get('fl-progress').hidden,true);
  assert.equal(storage.has(key),false);
  h.get('fl-close').click(); h.api.open({...board});
  h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/tasks').resolve({tasks:[latest]}); await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'setup');
  assert.equal(h.get('fl-task-id').textContent,'');
  const old = {...latest, id:'older', progress:{phase:'verifying-system',done:12884901888,total:12884901888},
    flash:{written_bytes:13157531648, verify_result:'verified'}};
  h.api.open(board, old); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  assert.match(h.get('fl-task-id').textContent,/older/);
  assert.match(h.get('fl-progress-text').textContent,/本次写入.*13157531648 B/);
  assert.doesNotMatch(h.get('fl-progress-text').textContent,/12884901888/);
  assert.ok(!h.requests.some(r => r.url === '/api/task-start'));
});

test('refresh finds an original flash request even when it is no longer on the first history page', async () => {
  const storage=new Map(),h=harness(storage);await open(h);h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan:{id:'fixed-plan',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}});await drain();
  h.get('fl-start').click();const sent=h.take('/api/task-start');sent.reject(new Error('receipt lost'));await drain();
  const fresh=harness(storage);fresh.api.open(board);fresh.take('/api/flash-capabilities').resolve(capabilities);await drain();
  const lookup=fresh.take('/api/task?request_id=');assert.ok(lookup.url.includes(sent.payload.request_id));
  lookup.resolve({task:{id:'original-write',kind:'flash',state:'running',request_id:sent.payload.request_id,
    progress:{phase:'writing',done:16,total:32},flash:{request_id:sent.payload.request_id}}});await drain();
  assert.match(fresh.get('fl-task-id').textContent,/original-write/);
  assert.equal(fresh.requests.some(r=>r.url==='/api/task-start'||r.url.startsWith('/api/tasks')),false);
});

test('lost preview response recovers its original request without submitting a new preview', async () => {
  const h=harness();await open(h);h.get('fl-plan').click();const lost=h.take('/api/flash-plan');
  lost.reject(new Error('network response lost'));await drain();assert.equal(h.get('fl-recover').hidden,false);
  h.get('fl-recover').click();const recovered=h.take('/api/task?request_id=');
  assert.ok(recovered.url.includes(lost.payload.request_id));
  recovered.resolve({task:{id:'original-preview',state:'completed',result:{plan:{id:'original-plan',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}}});await drain();
  assert.equal(h.get('fl-start').disabled,false);assert.equal(h.get('fl-recover').hidden,true);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-plan'||r.url==='/api/task-start'),false);
});

test('selection timeout has a persistent recovery entry which only detects devices', async () => {
  const h=harness(),expired={...norReady,state:'failed',error:'flash partition selection timed out; storage was not written'};
  h.api.open(norTarget,expired);await drain();
  assert.equal(h.get('fl-expired').hidden,false);assert.equal(h.get('fl-restart').disabled,false);
  assert.match(h.get('fl-task-status').textContent,/确认已超时.*未开始写入.*重新检测/);
  h.get('fl-mode').value='partitions';h.get('fl-mode').emit('change');
  assert.equal(h.get('fl-expired').hidden,false);assert.match(h.get('fl-task-status').textContent,/确认已超时/);
  h.get('fl-restart').click();const detect=h.take('/api/flash-discover');
  assert.equal(detect.payload,undefined);assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  detect.resolve({targets:[]});await drain();
  h.api.open(norTarget,{...expired,flash:{...expired.flash,recovery_required:true}});await drain();
  assert.equal(h.get('fl-restart').disabled,true);
});

test('selection confirmation shows remaining time before expiry', async () => {
  const h=harness();h.api.open(norTarget,{...norReady,flash:{...norReady.flash,selection_deadline:Date.now()/1000+90}});await drain();
  assert.match(h.get('fl-task-status').textContent,/确认剩余.*1:[23][09]/);
  assert.equal(h.get('fl-expired').hidden,true);
});

test('new previews use the current defaults, editing drops the plan and execution resumes in its own stage', async () => {
  const storage = new Map([['decklab-flash-image', JSON.stringify({id:artifact.id, mode:'full', verify:'if-supported', reset_after:false})]]);
  const h = harness(storage);
  h.api.open(board); h.take('/api/flash-capabilities').resolve({...capabilities,reset_after_supported:true}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  const stage = () => h.get('flash-dialog').dataset.stage;
  assert.equal(stage(),'setup');
  assert.equal(h.get('fl-reset-after').checked,true);
  h.get('fl-plan').click(); const first = h.take('/api/flash-plan');
  assert.equal(first.payload.verify,'none'); assert.equal(first.payload.reset_after,true);
  assert.equal(stage(),'setup'); assert.equal(h.get('fl-preview-overall').hidden,false);
  first.resolve({plan:{id:'first',writes:[{storage:'disk-a',size:32}]}}); await drain();
  assert.equal(h.get('fl-review').hidden,false); h.get('fl-edit').click();
  assert.equal(stage(),'setup'); assert.equal(h.get('fl-start').disabled,true);
  h.get('fl-verify').value='required'; h.get('fl-verify').emit('change');
  h.get('fl-reset-after').checked=false; h.get('fl-reset-after').emit('change');
  h.get('fl-plan').click(); const next = h.take('/api/flash-plan');
  assert.equal(next.payload.verify,'required'); assert.equal(next.payload.reset_after,false);
  next.resolve({plan:{id:'edited',writes:[{storage:'disk-a',size:32}]}}); await drain();
  h.get('fl-start').click(); const start = h.take('/api/task-start'); assert.equal(start.payload.plan_id,'edited');
  start.resolve({task:{id:'running',kind:'flash',state:'running',progress:{phase:'writing'}}}); await drain();
  h.get('fl-close').click(); h.api.open(board);
  assert.equal(stage(),'run'); assert.equal(h.get('fl-setup').hidden,true); assert.equal(h.get('fl-review').hidden,true);
  assert.equal(h.get('fl-new').hidden,true); assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('optional readback and explicit reset are sealed by a fresh preview', async () => {
  const h=harness();h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,reset_after_supported:true});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.get('fl-artifact').value=artifact.id;h.get('fl-artifact').emit('change');
  h.get('fl-verify').value='none';h.get('fl-verify').emit('change');
  h.get('fl-reset-after').checked=true;h.get('fl-reset-after').emit('change');
  h.get('fl-plan').click();const plan=h.take('/api/flash-plan');
  assert.equal(plan.payload.verify,'none');assert.equal(plan.payload.reset_after,true);
  plan.resolve({plan:{id:'reset-plan',binding_revision:7,verify:'none',reset_after:true,writes:[{storage:'disk-a',partition:'rootfs-a',size:32}]}});await drain();
  assert.equal(h.get('fl-confirm-options').hidden,false);
  assert.equal(h.get('fl-verify').value,'none');
  assert.equal(h.get('fl-reset-after').checked,true);
  h.get('fl-reset-after').checked=false;h.get('fl-reset-after').emit('change');
  assert.equal(h.get('fl-start').disabled,true);
  h.api.open({...board,key:'other',serial:'SN-OTHER'});
  assert.equal(h.get('fl-reset-after').checked,false);
});

test('footer settings persist without an image and a reviewed change only submits a new preview', async () => {
  const storage=new Map(),first=harness(storage);
  first.api.open(null);first.get('fl-verify').value='required';first.get('fl-verify').emit('change');
  first.get('fl-reset-after').checked=false;first.get('fl-reset-after').emit('change');
  assert.equal(storage.has('decklab-flash-image'),false);
  const h=harness(storage);h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,reset_after_supported:true});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  assert.equal(h.get('fl-verify').value,'required');assert.equal(h.get('fl-reset-after').checked,false);
  h.get('fl-artifact').value=artifact.id;h.get('fl-artifact').emit('change');h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan:{id:'original',verify:'required',writes:[{size:32}]}});await drain();
  h.get('fl-reset-after').checked=true;h.get('fl-reset-after').emit('change');
  const preview=h.take('/api/flash-plan');assert.equal(preview.payload.reset_after,true);
  assert.equal(h.get('fl-start').disabled,true);
  preview.resolve({plan:{id:'changed',verify:'required',reset_after:true,writes:[{size:32}]}});await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.get('fl-start').click();assert.equal(h.take('/api/task-start').payload.plan_id,'changed');
});

test('image store selection only prepares current board and stale choices cannot cross devices', async () => {
  const h=harness();await open(h);h.get('fl-store').click();
  h.chooseImage({id:'global-image',name:'opensbi.itb',size:32,sha256:'b'.repeat(64)});
  assert.equal(h.get('fl-artifact').value,'global-image');assert.equal(h.get('fl-mode').value,'partitions');
  assert.equal(h.get('fl-start').disabled,true);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.api.open({...board,key:'board-2',serial:'SN-2'});
  assert.throws(()=>h.chooseImage(artifact),/当前设备或任务状态已变化/);
});

test('cached image selection explains the required NOR read instead of promising a disabled preview', async () => {
  const h=harness();h.api.open(norTarget);h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.take('/api/flash-target').resolve({target:norTarget,capabilities:{...capabilities,
    nor_workspace_supported:true,preparable:true,modes:['partitions'],storages:[]}});await drain();
  h.get('fl-cache').click();h.chooseImage({id:'os-package',name:'os.tar.gz',size:1024});
  assert.equal(h.get('fl-plan').disabled,true);
  assert.match(h.get('fl-status').textContent,/先读取设备存储/);
  assert.equal(h.get('fl-read-nor').disabled,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('OS package in a read workspace previews GPT and all package payloads without selecting partitions', async () => {
  const h=harness(), state=JSON.parse(JSON.stringify(norReady));
  state.flash.nor_before.partitions.push({name:'rootfs',content:{version:'NOR-only fixture'}});
  h.api.open(norTarget,state);await drain();
  h.get('fl-cache').click();h.chooseImage({id:'os-package',name:'os.tar.gz',size:1024});
  const inspect=h.take('/api/flash-selection');assert.equal(inspect.payload.mode,'full');
  assert.match(h.get('fl-progress-text').textContent, /解析镜像包/);
  assert.equal(inspect.payload.artifact_id,'os-package');assert.deepEqual(inspect.payload.selections,[]);
  const os={layout_digest:'os-gpt',storages:[{id:'active',physical_device:'scsi:0',label:'System disk scsi:0',partitions:[
    {name:'ESP',offset:1048576,size:272629760},{name:'rootfs',offset:273678336,size:12884901888}]}]};
  const candidates=['ESP','rootfs'].map(partition=>({artifact_id:'os-package',storage:'active',partition}));
  inspect.resolve({plan:{id:'catalogue-os',verify:'if-supported',ready:false,writes:[],catalogue:[
    {name:'partition_universal.json',selected:true,referenced:true,partitions:[{name:'ESP',payload:'esp.vfat'},{name:'rootfs',payload:'rootfs.ext4'}]},
    {name:'partition_nor.json',selected:false,referenced:false,partitions:[{name:'uboot',payload:'edk2.itb'}]}]},inspection:os,payload_candidates:candidates});await drain();
  assert.equal(h.get('fl-mode').disabled,false);assert.equal(h.get('fl-mode').value,'full');
  assert.equal(h.get('fl-target-storage').disabled,false);assert.equal(h.get('fl-verify').disabled,false);
  assert.equal(h.get('fl-partitions').children.length,2);assert.equal(h.get('fl-partitions-field').hidden,false);
  assert.match(h.get('fl-package-contents').textContent,/不在本次刷机配置/);
  assert.match(h.get('fl-current-layout').textContent,/整包拟写入布局/);
  assert.equal(h.get('fl-partitions').children.find(row=>row.partitionId==='rootfs').currentContent,undefined);
  assert.match(h.get('fl-nor-current').textContent,/NOR 分区与版本/);
  assert.match(h.get('fl-nor-current').textContent,/opensbi/);
  assert.equal(h.get('fl-partitions').children.some(row=>row.checkbox.checked),false);
  const poll=[...h.timers.values()][0];poll();h.take('/api/task?id=').resolve({task:JSON.parse(JSON.stringify(norReady))});await drain();
  assert.equal(h.get('fl-mode').value,'full');assert.equal(h.get('fl-partitions').children[0].partitionId,'ESP');
  h.get('fl-select').click();const preview=h.take('/api/flash-selection');
  assert.equal(preview.payload.plan_id,'catalogue-os');assert.equal(preview.payload.layout_digest,'nor-live');
  assert.equal(preview.payload.mode,'full');assert.deepEqual(preview.payload.selections,[]);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  preview.resolve({plan:{id:'write-os',write_layout:true,confirm_after_prepare:true,writes:[
    {storage:'active',partition:'layout',kind:'layout',target:'gpt',size:1},
    {storage:'active',partition:'ESP',size:2},{storage:'active',partition:'rootfs',size:3}]}});await drain();
  assert.match(h.get('fl-preview').textContent,/覆盖系统盘 GPT/);
  assert.match(h.get('fl-select').textContent,/确认包内全部/);
  h.get('fl-select').click();const commit=h.take('/api/flash-selection');assert.equal(commit.payload.plan_id,'write-os');
  assert.equal(commit.payload.preview,undefined);assert.equal(commit.payload.artifact_id,undefined);
});

test('NOR Firmware verification changes never inherit the default UFS system disk', async () => {
  const h=harness(),pkg={id:'firmware',name:'Debian-13-Firmware-K3-202611161333.tar.gz',size:3153902};
  const ready={...norReady,flash:{...norReady.flash,storage_inventory:{default_storage:'scsi:0',
    priority_source:'eeprom:UFS',storages:[{id:'scsi:0',size:128*1024**3,partitions:[]}]}}};
  const result={plan:{id:'nor-catalogue',verify:'if-supported',writes:[],catalogue:[{format:'mtd',selected:true,partitions:[]}]},
    inspection:norReady.flash.inspection,payload_candidates:['opensbi','env'].map(partition=>({storage:'active',partition,artifact_id:pkg.id}))};
  h.api.open(norTarget,ready);await drain();h.get('fl-cache').click();h.chooseImage(pkg);
  h.take('/api/flash-selection').resolve(result);await drain();
  assert.equal(h.get('fl-target-storage-field').hidden,true);assert.equal(h.get('fl-target-storage').value,'');
  h.get('fl-verify').value='none';h.get('fl-verify').emit('change');
  const inspect=h.take('/api/flash-selection');assert.equal(inspect.payload.verify,'none');
  assert.equal('target_storage' in inspect.payload,false);assert.equal(h.get('fl-target-storage-field').hidden,true);
  inspect.resolve({...result,plan:{...result.plan,id:'nor-no-readback',verify:'none'}});await drain();
  assert.equal(h.get('fl-select').disabled,false);assert.equal(h.get('fl-verify').value,'none');
  h.get('fl-select').click();const preview=h.take('/api/flash-selection');
  assert.equal('target_storage' in preview.payload,false);assert.equal(preview.payload.verify,'none');
  assert.equal(preview.payload.mode,'full');assert.deepEqual(preview.payload.selections,[]);
});

test('a USB workspace task reached from its serial board keeps the task USB scope', async () => {
  const h=harness(),pkg={id:'firmware-usb',name:'Firmware.tar.gz',size:1024};
  const ready={...norReady,board_targets:['SN-1'],flash:{...norReady.flash,request_serial:''}};
  h.api.open(board);h.take('/api/flash-capabilities?key=').resolve({supported:false,
    reason:'flashing is not configured for this board',board_serial:'SN-1'});await drain();
  h.take('/api/tasks').resolve({tasks:[ready]});await drain();
  const restored=h.take('/api/flash-capabilities');assert.match(restored.url,/target_id=nor-fixture/);
  restored.resolve({...capabilities,reset_after_supported:true});await drain();
  h.get('fl-cache').click();h.chooseImage(pkg);
  const inspect=h.take('/api/flash-selection');
  assert.equal(inspect.payload.target_id,'nor-fixture');assert.equal(inspect.payload.target_generation,'brom-original');
  assert.equal('key' in inspect.payload,false);
});

test('a failed automatic package inspection is not retried on every task poll', async () => {
  const h=harness(),pkg={id:'firmware-fail',name:'Firmware.tar.gz',size:1024};
  const ready={...norReady,flash:{...norReady.flash,storage_inventory:{default_storage:'scsi:0',
    priority_source:'eeprom:UFS',storages:[{id:'scsi:0',size:128*1024**3,partitions:[]}]}}};
  h.api.open(norTarget,ready);await drain();
  h.get('fl-cache').click();h.chooseImage(pkg);
  h.take('/api/flash-selection').resolve({error:'flashing is not configured for this board'},404);await drain();
  for (let round=0;round<3;round++) {
    for (const [id,fn] of [...h.timers]) { h.timers.delete(id); fn(); }
    await drain();
    while (h.requests.some(r=>r.url.startsWith('/api/task?id='))) h.take('/api/task?id=').resolve({task:ready});
    await drain();
  }
  assert.equal(h.requests.some(r=>r.url==='/api/flash-selection'),false);
  assert.match(h.get('fl-status').textContent,/not configured/);
});

test('waiting storage read accepts a manual firmware and requests both scopes before confirming', async () => {
  const h=harness(); h.api.open(norTarget,JSON.parse(JSON.stringify(norReady))); await drain();
  h.get('fl-cache').click(); h.chooseImage({id:'os',name:'os.zip',size:1024});
  h.take('/api/flash-selection').resolve({plan:{id:'os-catalogue',writes:[]},
    inspection:norReady.flash.inspection,payload_candidates:[]}); await drain();
  assert.equal(!!h.get('fl-firmware-local').disabled,false);
  h.get('fl-firmware-choose').click();
  h.chooseImage({id:'custom-fw',name:'nightly.tar.gz',size:100});
  h.get('fl-select').click();
  const req=h.take('/api/flash-selection');
  assert.equal(req.payload.preview,true);assert.equal(req.payload.artifact_id,'os');
  assert.equal(req.payload.firmware_artifact_id,'custom-fw');assert.equal(req.payload.plan_id,undefined);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('missing package catalogue can be explicitly reparsed and previewed without a disabled dead end', async () => {
  const h=harness(),pkg={id:'firmware-retry',name:'Firmware.tar.gz',size:1024};
  h.api.open(norTarget,norReady);await drain();
  h.take('/api/flash-capabilities').resolve({...capabilities,reset_after_supported:true});await drain();
  h.get('fl-cache').click();h.chooseImage(pkg);
  h.take('/api/flash-selection').resolve({error:'fixture import response lost'},502);await drain();
  assert.match(h.get('fl-status').textContent,/fixture import/);
  // Editing an option cleared the old error, leaving only the deadline and a
  // disabled preview in the former implementation.
  h.get('fl-reset-after').checked=true;h.get('fl-reset-after').emit('change');
  assert.equal(h.get('fl-select').disabled,false);
  assert.match(h.get('fl-select').textContent,/解析镜像并预览/);
  h.get('fl-select').click();const retry=h.take('/api/flash-selection');
  assert.equal(retry.payload.preview,true);assert.deepEqual(retry.payload.selections,[]);
  assert.equal(h.get('fl-select').disabled,true);
  retry.resolve({plan:{id:'retry-catalogue',verify:'if-supported',writes:[]},
    inspection:norReady.flash.inspection,payload_candidates:['opensbi','env'].map(partition=>({
      storage:'active',partition,artifact_id:pkg.id}))});await drain();
  const preview=h.take('/api/flash-selection');
  assert.equal(preview.payload.preview,true);assert.equal(preview.payload.plan_id,'retry-catalogue');
  assert.equal(preview.payload.reset_after,true);
  assert.equal(preview.payload.mode,'full');assert.deepEqual(preview.payload.selections,[]);
  preview.resolve({plan:{id:'retry-write-preview',reset_after:true,writes:[{storage:'active',partition:'opensbi',size:32}]}});await drain();
  assert.match(h.get('fl-select').textContent,/确认包内全部/);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'||r.payload && r.url==='/api/flash-selection' && !r.payload.preview),false);
});

test('failed catalogue retries remain actionable and switching devices prevents follow-on preview', async () => {
  const h=harness(),pkg={id:'firmware-retry',name:'Firmware.tar.gz',size:1024};
  h.api.open(norTarget,norReady);await drain();h.get('fl-cache').click();h.chooseImage(pkg);
  h.take('/api/flash-selection').resolve({error:'fixture import failed'},409);await drain();
  h.get('fl-select').click();h.take('/api/flash-selection').resolve({error:'fixture still failed'},409);await drain();
  assert.equal(h.get('fl-select').disabled,false);assert.match(h.get('fl-status').textContent,/fixture still failed/);
  h.get('fl-select').click();const stale=h.take('/api/flash-selection');
  h.api.open({...norTarget,target_id:'other-board'}, {...norReady,id:'other-task'});await drain();
  stale.resolve({plan:{id:'old-catalogue',writes:[]},inspection:norReady.flash.inspection,payload_candidates:[]});await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/flash-selection'||r.url==='/api/task-start'),false);
});

test('reparsing for a partition preview retains only the explicitly selected partition', async () => {
  const h=harness(),pkg={id:'firmware-retry',name:'Firmware.tar.gz',size:1024};
  h.api.open(norTarget,norReady);await drain();h.get('fl-cache').click();h.chooseImage(pkg);
  h.take('/api/flash-selection').resolve({error:'fixture import failed'},409);await drain();
  h.get('fl-mode').value='partitions';h.get('fl-mode').emit('change');
  const refresh=h.take('/api/flash-selection');
  assert.equal(refresh.payload.mode,'partitions');
  refresh.resolve({plan:{id:'partition-retry-catalogue',verify:'if-supported',writes:[]},
    inspection:norReady.flash.inspection,payload_candidates:['opensbi','env'].map(partition=>({storage:'active',partition,artifact_id:pkg.id}))});await drain();
  const row=h.get('fl-partitions').children[0];row.checkbox.checked=true;row.artifactSelect.value=pkg.id;row.checkbox.emit('change');
  h.get('fl-select').click();
  const preview=h.take('/api/flash-selection');
  assert.equal(preview.payload.preview,true);assert.deepEqual(preview.payload.selections.map(x=>x.partition),['opensbi']);
  assert.equal(preview.payload.target_generation,'brom-original');
});

test('partition rows offer only the active package mapping, never arbitrary cached archives', async () => {
  const h=harness(), pkg={id:'package',name:'firmware.tar.gz',size:1024}, other={id:'unrelated',name:'other.zip',size:2000};
  h.api.open(board);h.take('/api/flash-capabilities').resolve({...capabilities,artifacts:[artifact,pkg,other]});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  let row=h.get('fl-partitions').children[0];
  assert.equal(row.artifactSelect.children.some(o=>o.value===pkg.id||o.value===other.id),false);
  row.checkbox.checked=true;row.checkbox.emit('change');
  h.get('fl-artifact').value=pkg.id;h.get('fl-artifact').emit('change');
  row=h.get('fl-partitions').children[0];
  assert.equal(row.artifactSelect.value,pkg.id);
  assert.match(row.artifactSelect.children.find(o=>o.value===pkg.id).textContent,/firmware.tar.gz/);
  assert.equal(row.artifactSelect.children.some(o=>o.value===other.id),false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('single selected partition receives a chosen raw file without a second selection', async () => {
  const h=harness();await open(h);let rows=h.get('fl-partitions').children;
  rows[0].checkbox.checked=true;rows[0].checkbox.emit('change');
  h.get('fl-store').click();h.chooseImage({id:'new-file',name:'opensbi.itb',size:32});
  rows=h.get('fl-partitions').children;
  assert.equal(rows[0].artifactSelect.value,'new-file');assert.equal(rows[1].artifactSelect.value,'');
  assert.equal(rows[1].checkbox.checked,false);assert.equal(h.get('fl-start').disabled,true);
});

test('partition file picker excludes packages and binds only its requested row', async () => {
  const h=harness();await open(h);let rows=h.get('fl-partitions').children;
  rows[1].chooseFile.click();assert.equal(h.imagePicker().fileOnly,true);assert.equal(h.imagePicker().partition,'rootfs');
  assert.throws(()=>h.chooseImage({id:'bad',name:'firmware.tar.gz'}),/整包请在镜像来源中导入/);
  h.chooseImage({id:'replacement',name:'rootfs.ext4',size:32});rows=h.get('fl-partitions').children;
  assert.equal(rows[1].artifactSelect.value,'replacement');assert.equal(rows[1].checkbox.checked,true);
  assert.equal(rows[0].artifactSelect.value,'');assert.equal(rows[0].checkbox.checked,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('a raw file is not automatically duplicated into multiple selected partitions', async () => {
  const h=harness();await open(h);const rows=h.get('fl-partitions').children;
  rows.forEach(row=>{row.checkbox.checked=true;row.checkbox.emit('change');});
  h.get('fl-store').click();h.chooseImage({id:'new-raw',name:'payload.bin',size:32});
  assert.equal(h.get('fl-partitions').children.some(row=>row.artifactSelect.value==='new-raw'),false);
});

test('choosing a new source file replaces the package when no partition override is selected', async () => {
  const h=harness();await open(h);h.get('fl-store').click();h.chooseImage({id:'package',name:'firmware.tar.gz',size:1024});
  h.get('fl-store').click();h.chooseImage({id:'new-source',name:'opensbi.itb',size:512});
  assert.equal(h.get('fl-artifact').value,'new-source');assert.match(h.get('fl-source-summary').textContent,/opensbi.itb/);
  assert.equal(h.get('fl-mode').value,'partitions');
});

test('a per-partition raw override keeps the source package for the other partition mappings', async () => {
  const h=harness(), pkg={id:'package',name:'firmware.tar.gz',size:1024};
  h.api.open(board);h.take('/api/flash-capabilities').resolve({...capabilities,artifacts:[pkg]});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.get('fl-mode').value='partitions';h.get('fl-artifact').value=pkg.id;h.get('fl-artifact').emit('change');
  h.get('fl-partitions').children.forEach(row=>{row.checkbox.checked=true;row.checkbox.emit('change');});
  h.get('fl-partitions').children[1].chooseFile.click();h.chooseImage({id:'override',name:'rootfs.ext4',size:32});
  const rows=h.get('fl-partitions').children;
  assert.equal(h.get('fl-artifact').value,pkg.id);assert.equal(rows[0].artifactSelect.value,pkg.id);assert.equal(rows[1].artifactSelect.value,'override');
});

test('flash preview pins exact storage/partition and upload reference, never a host command', async () => {
  const h = harness(); await open(h);
  h.get('fl-mode').value = 'partitions'; h.get('fl-mode').emit('change');
  const rows = h.get('fl-partitions').children;
  assert.match(rows[0].textContent, /eMMC \/ rootfs/); assert.match(rows[1].textContent, /SD \/ rootfs/);
  rows[1].checkbox.checked = true; rows[1].artifactSelect.value = artifact.id;
  h.get('fl-plan').click();
  const preview = h.take('/api/flash-plan');
  assert.deepEqual(preview.payload.selections, [{storage: 'disk-b', partition: 'rootfs-b', artifact_id: artifact.id}]);
  assert.equal(preview.payload.binding_revision, 7); assert.equal(preview.payload.mode, 'partitions');
  assert.equal('argv' in preview.payload, false); assert.equal('path' in preview.payload, false);
  preview.resolve({plan: {id: 'plan-1', binding_revision: 7, digest: 'fixed', write_layout: false,
    writes: [{storage: 'disk-b', partition: 'rootfs-b', size: 32, sha256: artifact.sha256}]}}); await drain();
  assert.match(h.get('fl-preview').textContent, /disk-b.*rootfs-b/);
  assert.doesNotMatch(h.get('fl-preview').textContent, /将修改分区布局/);
  assert.equal(h.get('fl-start').disabled, false);
  h.get('fl-verify').value = 'required'; h.get('fl-verify').emit('change');
  assert.equal(h.get('fl-start').disabled, true, 'changing intent invalidates preview');
});

test('full-image preview shows OS payload facts as text under the matching write', async () => {
  const h = harness(); await open(h);
  h.get('fl-plan').click();
  const preview = h.take('/api/flash-plan');
  const content = {format: 'ext4', version: 'Bianbu 2.3.5 · 20260908194336', version_state: 'identified',
    filesystem: {type: 'ext4', label: '<b>rootfs</b>'}, kernels: [{release: '6.6.63'}],
    boot: {loader: 'GRUB 2.14', entries: ['/boot/vmlinuz-6.6.63']},
    partitions: [{index: 1, name: 'esp', size: 4096, filesystem: {type: 'fat32'}, versions: [{value: 'GRUB 2.14'}]}]};
  preview.resolve({plan: {id: 'plan-os', binding_revision: 7, digest: 'fixed', write_layout: false,
    writes: [{storage: 'disk-b', partition: 'rootfs-b', size: 32, sha256: artifact.sha256}],
    payload_metadata: [{storage: 'disk-b', partition: 'rootfs-b', sha256: 'f'.repeat(64), content:{...content,version:'wrong-digest-version'}},
                       {storage: 'disk-b', partition: 'rootfs-b', sha256: artifact.sha256, content}]}}); await drain();
  const text = h.get('fl-preview').textContent;
  assert.match(text, /Bianbu 2\.3\.5 · 20260908194336/);
  assert.match(text, /内核 · 6\.6\.63/); assert.match(text, /引导程序 · GRUB 2\.14/);
  assert.match(text, /文件系统 · ext4 · <b>rootfs<\/b>/, 'payload labels stay literal text');
  assert.match(text, /磁盘分区 · 1/); assert.match(text, /esp · .* · fat32 · GRUB 2\.14/);
  assert.doesNotMatch(text, /wrong-digest-version/, 'metadata for a different digest is ignored');
});

test('mirror deletion confirms the exact artifact, preserves cancelled previews and removes only its id', async () => {
  const h = harness(); h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,
    artifacts: [artifact, {...artifact, id:'same-name-other-image'}]}); await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change');
  h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan:{id:'before-delete',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}); await drain();
  h.get('fl-delete').click();
  assert.equal(h.get('fl-delete-field').hidden, false);
  assert.equal(h.get('fl-delete-cancel').focused,true);
  assert.equal(h.get('fl-delete').attributes['aria-expanded'],'true');
  assert.match(h.get('fl-delete-name').textContent, /image.bin.*32 B.*SHA-256/);
  assert.ok(h.get('fl-delete-name').textContent.includes(artifact.sha256));
  assert.equal(h.requests.some(r=>r.url==='/api/artifact-delete'), false);
  h.get('fl-delete-cancel').click();
  assert.equal(h.get('fl-delete-field').hidden, true);
  assert.equal(h.get('fl-start').disabled, false, 'cancel keeps the reviewed plan');
  h.get('fl-delete').click(); h.get('fl-delete-confirm').click();
  assert.equal(h.get('fl-start').disabled, true, 'a deletion invalidates all cached client previews');
  assert.equal(h.get('fl-delete').disabled, true);
  const request=h.take('/api/artifact-delete');
  assert.equal(request.payload.id, artifact.id); assert.equal(request.payload.serial, board.serial);
  assert.equal('path' in request.payload, false);
  h.get('fl-delete-confirm').click(); assert.equal(h.requests.some(r=>r.url==='/api/artifact-delete'),false);
  request.resolve({ok:true,artifact:{...artifact,state:'deleted'}}); await drain();
  assert.match(h.get('fl-status').textContent,/镜像已删除/);
  assert.equal(h.get('fl-artifact').children.some(option=>option.value===artifact.id),false);
  assert.equal(h.get('fl-artifact').children.some(option=>option.value==='same-name-other-image'),true);
  assert.equal(h.get('fl-delete').disabled,true);
});

test('clearing the image draft leaves both cached packages available', async () => {
  const h = harness(new Map()); await open(h);
  const fw = {id:'firmware',name:'Debian-13-Firmware-K3-202611161333.tar.gz',size:200};
  const os = {id:'system',name:'Debian-13-GNOME-K3-202611161345.tar.gz',size:2048,firmware:fw};
  h.get('fl-store').click(); h.chooseImage(os);
  h.get('fl-source-clear').click();
  assert.equal(h.get('fl-artifact').value,'');
  assert.equal(h.get('fl-firmware').value,'');
  assert.equal(h.get('fl-artifact').children.some(option => option.value === os.id),true);
  assert.equal(h.get('fl-artifact').children.some(option => option.value === fw.id),true);
  assert.equal(h.requests.some(request => request.url === '/api/artifact-delete'),false);
});

test('ordinary flash can promote the companion to the only NOR image', async () => {
  const h = harness(); await open(h);
  const fw = {id:'firmware',name:'Debian-13-Firmware-K3-202611161333.tar.gz',size:200};
  const os = {id:'system',name:'Debian-13-GNOME-K3-202611161345.tar.gz',size:2048,firmware:fw};
  h.get('fl-store').click(); h.chooseImage(os);
  h.get('fl-target-storage').value = 'scsi:0';
  assert.equal(h.get('fl-firmware-only').disabled,false);
  h.get('fl-firmware-only').click();
  assert.equal(h.get('fl-artifact').value,fw.id);
  assert.equal(h.get('fl-firmware').value,'');
  assert.equal(h.get('fl-firmware-only-note').hidden,false);
  assert.equal(h.get('fl-target-storage-field').hidden,true);
  h.get('fl-plan').click();
  const review = h.take('/api/flash-plan');
  assert.equal(review.payload.artifact_id,fw.id);
  assert.equal(review.payload.mode,'full');
  assert.equal('target_storage' in review.payload,false);
  assert.equal('firmware_artifact_id' in review.payload,false);
  assert.equal(h.requests.some(request => request.url === '/api/task-start'),false);
});

test('artifact deletion handles active-task races and lost responses without retrying', async () => {
  const h=harness(); await open(h);
  h.get('fl-delete').click();h.get('fl-delete-confirm').click();
  h.take('/api/artifact-delete').resolve({ok:false,error:'artifact is owned by an active task'},409); await drain();
  assert.match(h.get('fl-status').textContent,/任务结束后才能删除/);
  assert.equal(h.get('fl-artifact').children.some(option=>option.value===artifact.id),true);
  h.get('fl-delete').click();h.get('fl-delete-confirm').click();
  h.take('/api/artifact-delete').reject(new Error('response lost')); await drain();
  assert.match(h.get('fl-status').textContent,/删除结果未确认/);
  assert.equal(h.requests.some(r=>r.url==='/api/artifact-delete'),false);
  assert.equal(h.get('fl-start').disabled,true);
  h.api.open(board,{id:'busy-flash',kind:'flash',state:'running',progress:{phase:'writing'},flash:{}});await drain();
  assert.equal(h.get('fl-delete').disabled,true);
  h.get('fl-delete').click(); assert.equal(h.get('fl-delete-field').hidden,true);
});

test('a changed artifact or device cancels delete confirmation and stale deletion cannot update the new panel', async () => {
  const h=harness();await open(h);
  h.get('fl-delete').click();h.get('fl-mode').emit('change');
  assert.equal(h.get('fl-delete-field').hidden,true);
  h.get('fl-delete-confirm').click();assert.equal(h.requests.some(r=>r.url==='/api/artifact-delete'),false);
  h.get('fl-delete').click();h.get('fl-delete-confirm').click();const request=h.take('/api/artifact-delete');
  h.api.open({key:'other-board',serial:'OTHER'});
  h.take('/api/flash-capabilities').resolve({...capabilities,artifacts:[{...artifact,id:'other-artifact'}]});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  request.resolve({ok:true,artifact:{...artifact,state:'deleted'}});await drain();
  assert.equal(h.get('fl-artifact').children.some(option=>option.value==='other-artifact'),true);
  assert.doesNotMatch(h.get('fl-status').textContent,/镜像已删除/);
});

test('flash task survives panel close and refresh restores observation without replay', async () => {
  const h = harness(); await open(h); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan: {id: 'plan-1', digest: 'fixed', binding_revision: 7,
    writes: [{storage: 'disk-a', partition: 'rootfs-a', size: 32}]}}); await drain();
  h.get('fl-start').click(); const submit = h.take('/api/task-start');
  assert.equal(submit.payload.kind, 'flash'); assert.equal(submit.payload.plan_id, 'plan-1');
  assert.equal('connection_id' in submit.payload, false, 'board task does not invent a UART generation');
  assert.ok(submit.payload.request_id);
  const current = {id: 'flash-task', kind: 'flash', state: 'running', progress: {phase: 'writing', done: 4, total: 32}, flash: {request_id: submit.payload.request_id}};
  submit.resolve({task: current}); await drain();
  h.take('/api/flash-events').resolve({events: [{seq: 1, phase: 'writing', done: 4, total: 32}], next_after: 1}); await drain();
  h.get('fl-close').click(); assert.equal(h.get('flash-dialog').open, false);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-cancel'), false);
  const fresh = harness(); fresh.api.open(board); fresh.take('/api/flash-capabilities').resolve(capabilities); await drain();
  fresh.take('/api/tasks').resolve({tasks: [current]}); await drain();
  assert.match(fresh.get('fl-task-id').textContent, /flash-task/);
  assert.equal(fresh.requests.some(entry => entry.url === '/api/task-start'), false);
  assert.equal(fresh.get('fl-progress').value, 4);
});

test('lost flash submission locks start and queries original request rather than replaying', async () => {
  const h = harness(); await open(h); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan: {id: 'plan-1', writes: [{storage: 'disk-a', size: 32}]}}); await drain();
  h.get('fl-start').click(); const request = h.take('/api/task-start'); request.reject(new Error('network lost')); await drain();
  assert.equal(h.get('fl-start').disabled, true); assert.equal(h.get('fl-recover').hidden, false);
  assert.ok(h.get('fl-partitions').children.every(row=>row.checkbox.disabled&&row.artifactSelect.disabled));
  assert.equal(h.get('fl-all-partitions').disabled,true);
  h.get('fl-start').click(); assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
  h.get('fl-recover').click(); h.take('/api/tasks').resolve({tasks: [{id: 'original', kind: 'flash', state: 'completed',
    progress: {}, flash: {request_id: request.payload.request_id}, result: {write_result: 'written', verify_result: 'unsupported', boot_result: 'not-requested'}}]}); await drain();
  assert.match(h.get('fl-results').textContent, /写入完成.*不支持回读校验/);
  assert.equal(h.get('fl-recover').hidden, true);
});

test('stale capabilities and events cannot replace a different board panel', async () => {
  const h = harness(); h.api.open(board); const old = h.take('/api/flash-capabilities');
  h.api.open({key: 'board-2', serial: 'SN-2', label: 'Second'}); const next = h.take('/api/flash-capabilities');
  next.resolve({...capabilities, profile: 'second-profile'}); await drain(); h.take('/api/tasks').resolve({tasks: []}); await drain();
  old.resolve({...capabilities, profile: 'old-profile'}); await drain();
  assert.match(h.get('fl-connections').textContent, /second-profile/);
  assert.doesNotMatch(h.get('fl-connections').textContent, /old-profile/);
});

test('unknown live layout explicitly prepares once then previews selected writes in the original task', async () => {
  const h = harness(); await open(h); h.get('fl-mode').value = 'partitions'; h.get('fl-plan').click();
  const draft = h.take('/api/flash-plan'); assert.deepEqual(draft.payload.selections, []);
  draft.resolve({plan: {id: 'intent-1', ready: false, writes: [], pending_checks: ['live layout']}}); await drain();
  assert.equal(h.get('fl-start').textContent, '准备刷机环境并读取布局');
  h.get('fl-start').click(); h.take('/api/task-start').resolve({task: {id: 'prepared-task', kind: 'flash', state: 'running',
    progress: {phase: 'awaiting-selection'}, flash: {inspection: {layout_digest: 'layout-live', storages: capabilities.storages}}}}); await drain();
  assert.equal(h.get('fl-select').hidden, false); assert.equal(h.get('fl-start').disabled, true);
  const row = h.get('fl-partitions').children[0]; row.checkbox.checked = true; row.artifactSelect.value = artifact.id;
  h.get('fl-select').click(); const preview = h.take('/api/flash-selection');
  assert.equal(preview.payload.id, 'prepared-task'); assert.equal(preview.payload.preview, true);
  assert.equal(preview.payload.layout_digest, 'layout-live'); assert.equal('request_id' in preview.payload, false);
  preview.resolve({plan: {id: 'selected-plan', write_layout: false, writes: [{storage: 'disk-a', partition: 'rootfs-a', size: 32}]}}); await drain();
  assert.equal(h.get('fl-select').textContent, '确认分区并继续');
  h.get('fl-select').click(); const resume = h.take('/api/flash-selection');
  assert.equal(resume.payload.id, 'prepared-task'); assert.equal(resume.payload.plan_id, 'selected-plan');
  assert.ok(resume.payload.request_id); assert.equal('selections' in resume.payload, false);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('empty final write scope cannot be submitted and capability failure preserves useful error', async () => {
  const h = harness(); await open(h); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan: {id: 'empty', writes: []}}); await drain();
  assert.equal(h.get('fl-start').disabled, true); assert.match(h.get('fl-status').textContent, /缺少写入范围/);
  const denied = harness(); denied.api.open(board);
  denied.take('/api/flash-capabilities').resolve({supported: false, reason: 'Executor not configured'}); await drain();
  assert.equal(denied.get('fl-plan').disabled, true); assert.equal(denied.get('fl-files').disabled, false);
});

test('image upload uses bounded artifact chunks and seals before offering a preview', async () => {
  const h = harness(); await open(h);
  const bytes = Uint8Array.from([0, 255, 13, 10]);
  h.get('fl-files').files = [{name: 'payload.bin', size: bytes.length,
    slice: () => ({arrayBuffer: async () => bytes.buffer})}];
  h.get('fl-files').emit('change'); const create = h.take('/api/artifact-create');
  assert.deepEqual(create.payload, {key: 'board-1', serial: 'SN-1', actor: 'web', name: 'payload.bin', size: 4});
  create.resolve({artifact: {id: 'new-image'}}); await drain();
  const append = h.take('/api/artifact-append'); assert.equal(append.payload.data_b64, 'AP8NCg==');
  assert.equal(append.payload.offset, 0); assert.equal(h.get('fl-plan').disabled, true);
  append.resolve({artifact: {received: 4}}); await drain();
  h.take('/api/artifact-seal').resolve({artifact: {id: 'new-image', name: 'payload.bin', size: 4, sha256: 'b'.repeat(64)}}); await drain();
  assert.equal(h.get('fl-artifact').value, 'new-image'); assert.equal(h.get('fl-plan').disabled, false);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false, 'staging never writes board storage');
});

test('event cursors resume without replay and untrusted event text remains text', async () => {
  const h = harness(); const current = {id: 'cursor-task', kind: 'flash', state: 'running', progress: {phase: 'writing'}, flash: {}};
  h.api.open(board, current); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  const first = h.take('/api/flash-events'); assert.match(first.url, /after=0/);
  first.resolve({events: [{seq: 2, phase: 'writing', message: '<img src=x onerror=bad()>'}], next_after: 2}); await drain();
  assert.match(h.get('fl-events').textContent, /<img src=x onerror=bad\(\)>/);
  assert.equal(h.get('fl-events').children.length, 0, 'events never become HTML');
  const poll = Array.from(h.timers.values())[0]; h.timers.clear(); poll();
  h.take('/api/task?id=cursor-task').resolve({task: {...current, state: 'completed', result: {write_result: 'written'}}}); await drain();
  assert.match(h.take('/api/flash-events').url, /after=2/);
});

test('URL flash retains independent archive and partition progress through interleaved events', async () => {
  const h = harness();
  const current = {id:'active-stream',kind:'flash',state:'running',
    flash:{operation:'stream'},progress:{phase:'streaming-image',done:40,total:100}};
  h.api.open(board,current);
  h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/flash-events').resolve({events:[
    {cursor:1,event:'progress',phase:'streaming-image',done:20,total:100},
    {cursor:2,event:'progress',phase:'writing',partition:'rootfs',done:200,total:500},
    {cursor:3,event:'progress',phase:'streaming-image',done:40,total:100}
  ],next_after:3}); await drain();
  assert.equal(h.get('fl-stream-progress').hidden,false);
  assert.equal(h.get('fl-progress').hidden,true);
  assert.match(h.get('fl-stream-read-count').textContent,/40%/);
  assert.match(h.get('fl-stream-write-title').textContent,/rootfs/);
  assert.match(h.get('fl-stream-write-count').textContent,/200 B \/ 500 B/);
});

test('completed URL flash drains all trace pages and shows commands and skipped firmware', async () => {
  const h = harness();
  h.api.open(board, {id:'finished-stream',kind:'flash',state:'completed',progress:{phase:'preparing-loader'},
    flash:{operation:'stream'},result:{write_result:'completed'}});
  h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/flash-events').resolve({events:[{cursor:1,event:'command',command:'download:00000400',status:'issuing'},
    {cursor:2,event:'command',command:'download:00000400',status:'DATA'}],has_more:true,next_after:2});
  await drain();
  const next = h.take('/api/flash-events'); assert.match(next.url, /after=2/);
  next.resolve({events:[{cursor:3,event:'command',command:'download:00000400',status:'OKAY'},
    {cursor:4,event:'firmware-skipped',partition:'partition_nor.json'}],next_after:4});
  await drain();
  assert.match(h.get('fl-events').textContent, /Fastboot download:00000400 · DATA → OKAY/);
  assert.match(h.get('fl-events').textContent, /包内 NOR 固件已跳过 · partition_nor.json/);
  assert.doesNotMatch(h.get('fl-task-id').textContent, /preparing-loader/);
});

test('current run shows compact key operations and retains the full trace behind a collapsed disclosure', async () => {
  const h = harness();
  h.api.open(board, {id:'current-run',kind:'flash',state:'completed',created:1795500000,
    flash:{operation:'stream'},result:{write_result:'completed'}});
  h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  const read = {event:'command',operation:'storage-inventory',command:'getvar:version',status:'issuing'};
  const write = {event:'command',operation:'execute',command:'flash:rootfs',status:'issuing'};
  h.take('/api/flash-events').resolve({events:[
    {...read,cursor:1},{...read,cursor:2,status:'OKAY'},{...write,cursor:3},{...write,cursor:4,status:'OKAY'}
  ],next_after:4}); await drain();
  assert.match(h.get('fl-task-id').textContent,/任务 ID current-run/);
  assert.match(h.get('fl-event-summary').textContent,/读取与核对.*写入分区/);
  assert.match(h.get('fl-key-command-list').textContent,/flash:rootfs.*OKAY/);
  assert.doesNotMatch(h.get('fl-key-command-list').textContent,/getvar/);
  assert.match(h.get('fl-events').textContent,/getvar:version/);
  assert.equal(h.get('fl-event-raw').open,false);
});

test('live Go partition names are submitted with their storage id when no synthetic id exists', async () => {
  const h = harness(); h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities, storages: [
    {id: 'nvme', label: 'NVMe disk', partitions: [{name: 'rootfs', size: 100}]}]}); await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.get('fl-mode').value = 'partitions'; h.get('fl-artifact').value = artifact.id;
  h.get('fl-artifact').emit('change');
  const row = h.get('fl-partitions').children[0]; assert.match(row.textContent, /NVMe disk \/ rootfs/);
  row.checkbox.checked = true; row.artifactSelect.value = artifact.id;
  h.get('fl-plan').click(); const preview = h.take('/api/flash-plan');
  assert.deepEqual(preview.payload.selections, [{storage: 'nvme', partition: 'rootfs', artifact_id: artifact.id}]);
});

test('raw disk target is explicitly selected and changing it invalidates the fixed plan', async () => {
  const h = harness(); await open(h);
  assert.equal(h.get('fl-target-storage').value, '', 'multiple storages never silently select the first one');
  h.get('fl-target-storage').value = 'disk-b'; h.get('fl-target-storage').emit('change');
  h.get('fl-plan').click(); const preview = h.take('/api/flash-plan');
  assert.equal(preview.payload.target_storage, 'disk-b');
  preview.resolve({plan: {id: 'raw-plan', writes: [{storage: 'disk-b', kind: 'disk', size: 32}], write_layout: true}}); await drain();
  assert.equal(h.get('fl-start').disabled, false);
  h.get('fl-target-storage').value = 'disk-a'; h.get('fl-target-storage').emit('change');
  assert.equal(h.get('fl-start').disabled, true);
  h.get('fl-mode').value = 'partitions'; h.get('fl-mode').emit('change');
  assert.equal(h.get('fl-target-storage-field').hidden, true);
});

test('rare recovery release requires stopped evidence and preserves the original failed task result', async () => {
  const h = harness(); const failed = {id: 'recovery-task', kind: 'flash', state: 'failed',
    error: 'process cleanup unconfirmed', progress: {}, result: {write_result: 'partial-or-unknown'},
    flash: {attempt_id: 'attempt-1', recovery_required: true}};
  h.api.open(board, failed); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  assert.equal(h.get('fl-recovery-field').hidden, false); assert.equal(h.get('fl-release').disabled, true);
  h.get('fl-release').click(); assert.equal(h.requests.some(entry => entry.url === '/api/flash-recovery'), false);
  h.get('fl-recovery-reason').value = 'Checked external process list; no flasher remains';
  h.get('fl-recovery-reason').emit('input'); assert.equal(h.get('fl-release').disabled, false);
  h.get('fl-release').click(); const release = h.take('/api/flash-recovery');
  assert.deepEqual(release.payload, {key: 'board-1', serial: 'SN-1', actor: 'web', id: 'recovery-task',
    attempt_id: 'attempt-1', reason: 'Checked external process list; no flasher remains', confirm_stopped: true});
  release.resolve({task: {...failed, flash: {...failed.flash, recovery_required: false}}}); await drain();
  assert.equal(h.get('fl-recovery-field').hidden, true);
  assert.match(h.get('fl-task-id').textContent, /失败.*任务 ID recovery-task/);
  assert.match(h.get('fl-results').textContent, /可能部分写入，结果待确认/);
  assert.match(h.get('fl-status').textContent, /不表示写入成功/);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('failed inspection cleanup opens its retained recovery task without replaying inspection', async () => {
  const h = harness(); await open(h); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({ok: false, error: 'cleanup unconfirmed', recovery_required: true, task_id: 'inspect-hold'}, 503); await drain();
  h.take('/api/task?id=inspect-hold').resolve({task: {id: 'inspect-hold', kind: 'flash-inspect', state: 'failed', progress: {},
    flash: {attempt_id: 'inspection-attempt', recovery_required: true, write_result: 'not-started'}}}); await drain();
  assert.equal(h.get('fl-recovery-field').hidden, false);
  assert.match(h.get('fl-task-id').textContent, /inspect-hold/);
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-plan'), false);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('refresh restores a terminal inspection recovery hold as well as ordinary flash tasks', async () => {
  const h = harness(); h.api.open(board); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/tasks').resolve({tasks: [{id: 'old-inspection', kind: 'flash-inspect', state: 'interrupted', progress: {},
    flash: {attempt_id: 'old-attempt', recovery_required: true}}]}); await drain();
  assert.equal(h.get('fl-recovery-field').hidden, false);
  assert.match(h.get('fl-task-id').textContent, /已中断.*任务 ID old-inspection/);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('write preview includes the full sector write range and explicit zero padding', async () => {
  const h = harness(); await open(h); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan: {id: 'padded-plan', write_layout: false,
    writes: [{storage: 'disk-a', partition: 'boot', size: 513, write_size: 1024, sha256: artifact.sha256,
      write_sha256: 'c'.repeat(64)}]}}); await drain();
  assert.match(h.get('fl-preview').textContent, /1024 B/);
  assert.match(h.get('fl-preview').textContent, /载荷 513 字节，扇区尾部补零 511 字节/);
  assert.match(h.get('fl-preview').textContent, /写入范围 SHA-256/);
  assert.match(h.get('fl-preview').textContent, new RegExp('c'.repeat(64)));
  assert.match(h.get('fl-preview').textContent, new RegExp('载荷 SHA-256: ' + artifact.sha256));
  assert.equal(h.get('fl-start').disabled, false);
});

test('partition candidate artifact references are deduplicated across sector variants', async () => {
  const h = harness(); const current = {id: 'candidate-task', kind: 'flash', state: 'running',
    progress: {phase: 'awaiting-selection'}, flash: {inspection: {layout_digest: 'live', storages: capabilities.storages},
      payload_candidates: [{artifact_id: artifact.id, name: '512-byte sectors', size: 32},
        {artifact_id: artifact.id, name: '4096-byte sectors', size: 32}]}};
  h.api.open(board, current); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  const choices = h.get('fl-partitions').children[0].artifactSelect.children;
  assert.equal(choices.length, 2, 'one placeholder and one unique artifact');
  assert.equal(choices[1].value, artifact.id); assert.match(choices[1].textContent, /image.bin/);
});

test('automatic discovery probes one USB target and flashes without a UART or invented serial', async () => {
  const h = harness();
  h.api.discover();
  assert.equal(h.get('flash-dialog').open, true);
  const target = {target_id: 'usb-target-a', target_generation: 'generation-a', serial: '',
    label: 'SpacemiT loader', mode: 'loader', usb: {port_path: [1, 4]}};
  h.take('/api/flash-discover').resolve({available: true, targets: [target]}); await drain();
  const history = h.take('/api/tasks');
  assert.match(history.url, /target_id=usb-target-a/);
  assert.doesNotMatch(history.url, /target_serial=|connection_id=|key=/);
  history.resolve({tasks: []}); await drain();
  const probe = h.take('/api/flash-target');
  assert.deepEqual(probe.payload, {actor: 'web', target_id: 'usb-target-a', target_generation: 'generation-a'});
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
  probe.resolve({target, capabilities}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change'); h.get('fl-plan').click();
  const preview = h.take('/api/flash-plan');
  assert.equal(preview.payload.target_id, target.target_id);
  assert.equal(preview.payload.target_generation, target.target_generation);
  assert.equal('serial' in preview.payload, false);
  assert.equal('key' in preview.payload, false);
  preview.resolve({plan: {id: 'auto-plan', writes: [{storage: 'disk-a', partition: 'rootfs', size: 32}]}}); await drain();
  h.get('fl-start').click();
  const start = h.take('/api/task-start');
  assert.equal(start.payload.target_id, target.target_id);
  assert.equal(start.payload.target_generation, target.target_generation);
  assert.equal('connection_id' in start.payload, false);
});

test('multiple USB candidates require an explicit target and pin its generation', async () => {
  const h = harness(); h.api.discover();
  const first = {target_id: 'usb-a', target_generation: 'first', label: 'One', usb: {port_path: [1, 2]}};
  const second = {target_id: 'usb-b', target_generation: 'second', label: 'Two', usb: {port_path: [1, 3]}};
  h.take('/api/flash-discover').resolve({targets: [first, second]}); await drain();
  assert.equal(h.requests.length, 0, 'do not open an arbitrary USB interface');
  assert.match(h.get('fl-status').textContent, /多个下载设备/);
  assert.match(h.get('fl-device').textContent, /USB 1\.2.*USB 1\.3/);
  assert.equal(h.get('fl-plan').disabled, true);
  h.get('fl-device').value = second.target_id; h.get('fl-device').emit('change');
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  const selected = h.take('/api/flash-target');
  assert.equal(selected.payload.target_id, second.target_id);
  assert.equal(selected.payload.target_generation, second.target_generation);
  selected.resolve({target: second, capabilities: {...capabilities, supported: false, reason: 'layout-unavailable'}}); await drain();
  assert.match(h.get('fl-status').textContent, /layout-unavailable/);
  assert.equal(h.get('fl-start').disabled, true);
});

test('a serial board without a fixed binding probes only its console-paired USB port', async () => {
  const h = harness();
  h.api.open(board);
  h.take('/api/flash-capabilities?key=').resolve({supported:false,reason:'flashing is not configured for this board',board_serial:'SN-1'});await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  const other = {target_id: 'usb-a', target_generation: 'first', label: 'k3 · 1.2', usb: {port_path: [1, 2]},
    console_link: {state: 'linked', serial: 'OTHER-SN'}};
  const mine = {target_id: 'usb-b', target_generation: 'second', label: 'Pico', usb: {port_path: [1, 3]},
    console_link: {state: 'linked', serial: 'SN-1', serial_label: 'Pico'}};
  h.take('/api/flash-discover').resolve({targets: [other, mine]}); await drain();
  assert.match(h.get('fl-device').textContent, /SN SN-1 · 串口已配对/);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  const probe = h.take('/api/flash-target');
  assert.equal(probe.payload.target_id, 'usb-b');assert.equal(probe.payload.target_generation, 'second');
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target'), false);
});

test('a console pairing that names another board asks for an explicit board swap', async () => {
  const h = harness(); h.api.discover();
  const target = {target_id: 'usb-c', target_generation: 'third', label: 'old', usb: {port_path: [1, 4]}};
  h.take('/api/flash-discover').resolve({targets: [target]}); await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target: {...target, serial: 'NEW-SN', console_link: {state: 'mismatch',
    serial: 'OLD-SN', serial_label: 'old', usb_serial: 'NEW-SN'}}, capabilities}); await drain();
  assert.match(h.get('fl-status').textContent, /old · SN OLD-SN.*SN NEW-SN.*准备换板/);
});

test('late USB discovery does not replace another board and absent devices keep an actionable explanation', async () => {
  const h = harness(); h.api.discover(); const old = h.take('/api/flash-discover');
  h.api.open(board); h.take('/api/flash-capabilities').resolve(capabilities); await drain();
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  old.resolve({targets: [{target_id: 'stale', target_generation: 'old'}]}); await drain();
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target'), false);
  assert.match(h.get('fl-board').textContent, /Pico/);
  const empty = harness(); empty.api.discover();
  empty.take('/api/flash-discover').resolve({available: true, targets: []}); await drain();
  assert.match(empty.get('fl-status').textContent, /USB 数据线和下载模式/);
  assert.equal(empty.get('fl-files').disabled, false);
});

test('select and stage an image on the controller before detecting a board', async () => {
  const h = harness(); await h.api.open();
  assert.equal(h.requests.length, 0);
  for (const id of ['fl-local', 'fl-cache', 'fl-store', 'fl-url-toggle']) assert.equal(h.get(id).disabled, false);
  h.get('fl-files').files = [{name: 'os.tar.gz', size: 1, slice: () => ({arrayBuffer: async () => new Uint8Array([1]).buffer})}];
  h.get('fl-files').emit('change');
  const stage = h.take('/api/image-workspace');
  assert.deepEqual(stage.payload, {action: 'stage', name: 'os.tar.gz', size: 1});
  const image = {...artifact, name: 'os.tar.gz', size: 1};
  stage.resolve({artifact: image}); await drain();
  h.take('/api/artifact-append').resolve({artifact: {received: 1}}); await drain();
  h.take('/api/artifact-seal').resolve({artifact: image, preparation_task: {id:'prepare', state:'completed'}}); await drain();
  assert.equal(h.get('fl-artifact').value, image.id);
  assert.equal(h.get('fl-plan').disabled, true);
  assert.equal(h.get('fl-start').disabled, true);
  h.get('fl-discover').click(); h.take('/api/flash-discover').resolve({targets: []}); await drain();
  assert.match(h.get('fl-source-summary').textContent, /os.tar.gz/);
  assert.equal(h.requests.length, 0);
  h.get('fl-discover').click();
  const target = {target_id:'attached', target_generation:'now'};
  h.take('/api/flash-discover').resolve({targets:[target]}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities:{...capabilities, artifacts:[image]}}); await drain();
  assert.equal(h.get('fl-artifact').value, image.id);
  assert.equal(h.get('fl-plan').disabled, false);
  assert.equal(h.get('fl-start').disabled, true);
  assert.equal(h.requests.length, 0);
});

test('a retained USB task can be observed without probing or reselecting a disconnected device', async () => {
  const h = harness();
  const target = {target_id: 'usb-task-target', target_generation: 'original', label: 'Disconnected target'};
  const current = {id: 'retained-usb-task', kind: 'flash', state: 'running', progress: {phase: 'writing'}};
  h.api.open(target, current); await drain();
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target' || entry.url === '/api/flash-discover'), false);
  assert.match(h.take('/api/flash-events').url, /target_id=usb-task-target/);
  assert.match(h.get('fl-task-id').textContent, /retained-usb-task/);
  assert.equal(h.get('fl-discover').disabled, true, 'cannot switch target under an active operation');
});

test('clearing or refreshing a USB target invalidates a previously reviewed write plan', async () => {
  const h = harness();
  const target = {target_id: 'usb-planned', target_generation: 'old', label: 'USB target'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change'); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan: {id: 'old-plan', writes: [{storage: 'disk-a', size: 32}]}}); await drain();
  assert.equal(h.get('fl-start').disabled, false);
  h.get('fl-device').value = ''; h.get('fl-device').emit('change');
  assert.equal(h.get('fl-start').disabled, true);
  h.get('fl-start').click(); assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
  h.get('fl-discover').click(); h.take('/api/flash-discover').resolve({targets: []}); await drain();
  assert.equal(h.get('fl-plan').disabled, true);
  assert.equal(h.get('fl-start').disabled, true);
});

test('automatic primary-GPT capacity and missing readback remain explicit capabilities', async () => {
  const h = harness();
  const required = new Element(); required.value = 'required'; h.get('fl-verify').appendChild(required);
  const target = {target_id: 'gpt-target', target_generation: 'one'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, storages: [{id: 'active', size: 4096,
    capacity_source: 'gpt-header', capacity_verified: false, verify: false, partitions: [{name:'ESP', offset:512, size:1024}]}]}}); await drain();
  assert.equal(h.get('fl-storage-details').hidden, false);
  assert.match(h.get('fl-live-gpt').textContent, /ESP/);
  assert.match(h.get('fl-connections').textContent, /现场主 GPT.*容量尚未独立核验/);
  assert.match(h.get('fl-connections').textContent, /不支持写后回读校验/);
  assert.match(h.get('fl-target-storage').textContent, /GPT 声明，容量待核验/);
  assert.equal(required.disabled, true);
  assert.equal(h.get('fl-verify').value, 'none');
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change'); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({plan:{id:'live-gpt-projection',write_layout:true,
    writes:[{component:'OS',storage:'active',target:'gpt',kind:'layout'},
      {component:'OS',storage:'active',partition:'rootfs',offset:1024,capacity:-1,size:1024}]}});
  await drain();
  assert.match(h.get('fl-preview').textContent,/原有分区.*ESP.*新分区.*rootfs/);
  assert.doesNotMatch(h.get('fl-preview').textContent,/原布局未读取/);
});

for (const supported of [true, false]) test('BootROM image preparation is explicit, storage-free and continues partition selection in one task (supported=' + supported + ')', async () => {
  const h = harness(), target = {target_id: 'brom-target', target_generation: 'brom-one', mode: 'bootrom'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, supported,
    can_prepare_from_image: true, can_upload: true, modes: ['partitions'], storages: [], readback_supported: false}}); await drain();
  assert.equal(h.get('fl-mode').value, 'partitions');
  assert.equal(h.get('fl-mode').disabled, true);
  assert.equal(h.get('fl-files').disabled, false);
  assert.match(h.get('fl-status').textContent, /BootROM.*不写存储/);
  assert.match(h.get('fl-connections').textContent, /回读支持待确认/);
  assert.match(h.get('fl-partitions').textContent, /准备阶段将读取现场布局/);
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change');
  assert.equal(h.get('fl-plan').disabled, false);
  assert.match(h.get('fl-plan').textContent, /准备步骤/);
  h.get('fl-plan').click(); const preview = h.take('/api/flash-plan');
  assert.equal(preview.payload.mode, 'partitions'); assert.deepEqual(preview.payload.selections, []);
  assert.equal(preview.payload.target_generation, target.target_generation);
  preview.resolve({requires_prepare: true, plan: {id: 'brom-prep', ready: false, write_layout: false, writes: []}}); await drain();
  assert.equal(h.get('fl-start').disabled, false);
  assert.match(h.get('fl-start').textContent, /准备刷机环境并读取布局/);
  assert.match(h.get('fl-preview').textContent, /不写入存储/);
  h.get('fl-start').click(); const submit = h.take('/api/task-start');
  const current = {id: 'same-brom-task', kind: 'flash', state: 'running', progress: {phase: 'awaiting-selection'},
    flash: {inspection: {layout_digest: 'actual-layout', storages: capabilities.storages}, payload_candidates: [artifact]}};
  submit.resolve({task: current}); await drain();
  assert.match(h.get('fl-task-id').textContent, /same-brom-task/);
  const row = h.get('fl-partitions').children[0]; row.checkbox.checked = true; row.artifactSelect.value = artifact.id;
  h.get('fl-select').click(); const selected = h.take('/api/flash-selection');
  assert.equal(selected.payload.id, current.id); assert.equal(selected.payload.preview, true);
  assert.equal(selected.payload.layout_digest, 'actual-layout');
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false, 'never create another preparation task');
});

test('BootROM full preview prepares the board and exposes its checked plan for one write confirmation', async () => {
  const h = harness(), target = {target_id: 'nor-target', target_generation: 'nor-brom'};
  h.api.open(target); h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, preparable: true,
    modes: ['partitions', 'full'], storages: []}}); await drain();
  assert.equal(h.get('fl-mode').disabled, false);
  assert.equal(h.get('fl-mode').value, 'full', 'full package is available without opening advanced settings');
  h.get('fl-mode').value = 'full'; h.get('fl-mode').emit('change');
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change');
  h.get('fl-plan').click(); const preview = h.take('/api/flash-plan');
  assert.equal(preview.payload.mode, 'full');
  const writes = [{storage: 'active', partition: 'layout', kind: 'layout', target: 'mtd', size: 123},
    {storage: 'active', partition: 'fsbl', kind: 'partition', size: 456}];
  preview.resolve({requires_prepare: true, plan: {id: 'nor-prep', ready: false,
    confirm_after_prepare: true, write_layout: true, writes}}); await drain();
  const submit = h.take('/api/task-start');
  assert.equal(submit.payload.prepare_for_review,true);
  submit.resolve({task:{id:'one-nor-task',kind:'flash',state:'queued',progress:{}}});await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'setup');
  assert.equal(h.get('fl-preview-overall').hidden,false);
  const poll = Array.from(h.timers.values())[0];
  poll();
  const finalPlan = {id: 'nor-checked', ready: true, confirm_after_prepare: true, write_layout: true,
    writes, erase_ranges: [{storage: 'active', offset: 0, size: 8388608}]};
  const current = {id: 'one-nor-task', kind: 'flash', state: 'running', progress: {phase: 'awaiting-selection'},
    flash: {confirmation_mode: 'full', prepared_plan: finalPlan,
      inspection: {layout_digest: 'mtd-capacity', storages: []}}};
  h.take('/api/task?id=one-nor-task').resolve({task:current});await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'review');
  assert.match(h.get('fl-preview').textContent, /擦除风险范围.*8388608/);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-selection'),false);
  assert.match(h.get('fl-select').textContent, /确认固件范围并刷写/);
  assert.equal(h.requests.some(r => r.url === '/api/task-start'), false);
  h.get('fl-select').click(); const commit = h.take('/api/flash-selection');
  assert.equal(commit.payload.id, current.id); assert.equal(commit.payload.plan_id, 'nor-checked');
  assert.ok(commit.payload.request_id); assert.equal(commit.payload.preview, undefined);
});

test('BootROM preparation cannot acquire a hidden storage-write scope', async () => {
  const h = harness(), target = {target_id: 'brom-invalid', target_generation: 'brom-one'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, supported: true,
    preparable: true, modes: ['partitions'], storages: []}}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change'); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({requires_prepare: true, plan: {id: 'unsafe', ready: false,
    write_layout: false, writes: [{storage: 'disk-a', partition: 'rootfs', size: 32}]}}); await drain();
  assert.equal(h.get('fl-start').disabled, true);
  assert.match(h.get('fl-status').textContent, /准备计划包含存储写入/);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('detect again leaves completed history behind and probes the current USB identity', async () => {
  const h=harness(),target={target_id:'re-entered',target_generation:'new-bootrom',serial:''};
  const old={id:'last-write',kind:'flash',state:'completed',board_targets:['OLD-SN'],
    flash_targets:[{target_id:target.target_id,generation:'old-loader'}],progress:{},flash:{}};
  h.api.open({...target},old);await drain();
  h.take('/api/flash-capabilities').resolve({error:'USB flash target board identity changed'},403);await drain();
  assert.match(h.get('fl-status').textContent,/identity changed/);
  h.get('fl-discover').click();h.take('/api/flash-discover').resolve({targets:[target]});await drain();
  h.take('/api/tasks').resolve({tasks:[old]});await drain();
  const probe=h.take('/api/flash-target');
  assert.equal(probe.payload.target_generation,'new-bootrom');assert.equal('serial' in probe.payload,false);
  probe.resolve({target:{...target,serial:'CURRENT-SN'},capabilities});await drain();
  assert.equal(h.get('fl-files').disabled,false);assert.equal(h.get('fl-task-status').textContent,'');
  h.get('fl-artifact').value=artifact.id;h.get('fl-artifact').emit('change');h.get('fl-plan').click();
  const preview=h.take('/api/flash-plan');assert.equal(preview.payload.serial,'CURRENT-SN');
  assert.equal(preview.payload.target_generation,'new-bootrom');
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('explicit target selection ignores a saved completed preview and other terminal tasks', async () => {
  const storage=new Map([['decklab-flash-task:usb-next','saved-preview']]),h=harness(storage);
  const target={target_id:'usb-next',target_generation:'new-connection',serial:'NEW-SN'};
  const preview={id:'previous-preview',request_id:'saved-preview',kind:'flash-preview',state:'completed',
    updated:Date.now()/1000,result:{plan:{id:'old-plan',writes:[{storage:'disk-a',partition:'rootfs',size:32}]}}};
  h.api.discover();h.take('/api/flash-discover').resolve({targets:[target,{target_id:'other-usb'}]});await drain();
  h.get('fl-device').value=target.target_id;h.get('fl-device').emit('change');
  h.take('/api/task?request_id=').resolve({task:preview});await drain();
  h.take('/api/tasks').resolve({tasks:[preview,{id:'cancelled-write',kind:'flash',state:'cancelled'},
    {id:'failed-inspect',kind:'flash-inspect',state:'failed'}]});await drain();
  const probe=h.take('/api/flash-target');assert.equal(probe.payload.target_generation,'new-connection');
  probe.resolve({target,capabilities});await drain();
  assert.equal(h.get('fl-start').disabled,true);assert.equal(h.get('fl-files').disabled,false);
  assert.equal(storage.has('decklab-flash-task:usb-next'),false);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-plan'||r.url==='/api/task-start'),false);
});

test('refresh finds a prepared task before USB probing and preserves its original generation for selection', async () => {
  const h = harness(), target = {target_id: 're-enumerated', target_generation: 'loader-new', label: 'New loader'};
  const original = {id: 'original-preparation', kind: 'flash', state: 'running', board_targets: [],
    flash_targets: [{target_id: target.target_id, generation: 'brom-initial'}],
    progress: {phase: 'awaiting-selection'}, flash: {inspection: {layout_digest: 'actual', storages: capabilities.storages},
      payload_candidates: [artifact]}};
  h.api.discover(); h.take('/api/flash-discover').resolve({targets: [target]}); await drain();
  const history = h.take('/api/tasks');
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target'), false);
  history.resolve({tasks: [original]}); await drain();
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target'), false, 'endpoint remains owned by the original task');
  assert.match(h.take('/api/flash-events').url, /target_generation=brom-initial/);
  const row = h.get('fl-partitions').children[0]; row.checkbox.checked = true; row.artifactSelect.value = artifact.id;
  h.get('fl-select').click(); const preview = h.take('/api/flash-selection');
  assert.equal(preview.payload.id, original.id); assert.equal(preview.payload.target_generation, 'brom-initial');
  preview.resolve({plan: {id: 'original-selection', writes: [{storage: row.storageId, partition: row.partitionId, size: 32}]}}); await drain();
  h.get('fl-select').click(); const commit = h.take('/api/flash-selection');
  assert.equal(commit.payload.id, original.id); assert.equal(commit.payload.target_generation, 'brom-initial');
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('an unavailable executor can still show a disconnected recovery target and release the original task', async () => {
  const h = harness(), target = {target_id: 'disconnected', target_generation: 'fresh-list', present: false};
  const original = {id: 'retained-recovery', kind: 'flash', state: 'failed', board_targets: [],
    flash_targets: [{target_id: target.target_id, generation: 'original-generation'}], progress: {},
    flash: {recovery_required: true, attempt_id: 'original-attempt', write_result: 'unknown-or-partial'}};
  h.api.discover(); h.take('/api/flash-discover').resolve({available: false, targets: [target]}, 503); await drain();
  assert.match(h.get('fl-device').textContent, /设备未连接，原任务待处理/);
  h.take('/api/tasks').resolve({tasks: [original]}); await drain();
  assert.equal(h.requests.some(entry => entry.url === '/api/flash-target'), false);
  h.get('fl-recovery-reason').value = 'Checked the original external process is stopped';
  h.get('fl-recovery-reason').emit('input'); h.get('fl-release').click();
  const release = h.take('/api/flash-recovery');
  assert.equal(release.payload.target_generation, 'original-generation');
  assert.equal(release.payload.id, original.id);
  release.resolve({task: {...original, flash: {...original.flash, recovery_required: false}}}); await drain();
  assert.equal(h.get('fl-plan').disabled, true, 'a task snapshot cannot submit a new flash');
  assert.equal(h.get('fl-files').disabled, true);
  assert.equal(h.get('fl-discover').disabled, false);
});

test('awaiting-selection upload carries the original task id and does not create another preparation', async () => {
  const h = harness(), target = {target_id: 'upload-target', target_generation: 'new-loader'};
  const original = {id: 'upload-original', kind: 'flash', state: 'running', board_targets: [],
    flash_targets: [{target_id: target.target_id, generation: 'initial-brom'}], progress: {phase: 'awaiting-selection'},
    flash: {inspection: {layout_digest: 'live', storages: capabilities.storages}, payload_candidates: [artifact]}};
  h.api.open(target, original); await drain();
  h.get('fl-files').files = [{name: 'payload.bin', size: 3, slice: () => ({arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer})}];
  assert.equal(h.get('fl-files').disabled, false);
  h.get('fl-files').emit('change'); const create = h.take('/api/artifact-create');
  assert.equal(create.payload.task_id, original.id);
  assert.equal(create.payload.target_generation, 'initial-brom');
  create.resolve({artifact: {id: 'new-payload', size: 3}}); await drain();
  const append = h.take('/api/artifact-append');
  assert.equal('task_id' in append.payload, false);
  append.resolve({artifact: {received: 3}}); await drain();
  h.take('/api/artifact-seal').resolve({artifact: {id: 'new-payload', name: 'payload.bin', size: 3, sha256: 'e'.repeat(64)}}); await drain();
  assert.match(h.get('fl-status').textContent, /原任务选择实际分区/);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start' || entry.url === '/api/flash-target'), false);
});


test('unavailable USB connection shows its diagnostic and unknown readback without enabling writes', async () => {
  const h = harness(), target = {target_id:'io-target', target_generation:'one', mode:'unknown'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities:{supported:false, mode:'unknown',
    reason:'target-unavailable', diagnostic:'flush read error: -1', can_upload:true,
    storages:[], modes:['partitions'], readback_supported:false, artifacts:[artifact]}}); await drain();
  assert.match(h.get('fl-status').textContent, /无法打开 USB 下载连接/);
  assert.match(h.get('fl-connections').textContent, /flush read error: -1/);
  assert.match(h.get('fl-connections').textContent, /回读支持待确认/);
  assert.doesNotMatch(h.get('fl-connections').textContent, /已进入下载模式|不支持写后回读/);
  assert.match(h.get('fl-partitions').textContent, /USB 连接尚未就绪/);
  h.get('fl-artifact').value=artifact.id; h.get('fl-artifact').emit('change');
  assert.equal(h.get('fl-plan').disabled,true);
  assert.equal(h.get('fl-start').disabled,true);
  assert.equal(h.get('fl-files').disabled,false);
  const raw=h.get('fl-connections').children.find(node=>node.textContent==='flush read error: -1');
  assert.equal(Object.hasOwn(raw.dataset,'i18nIgnore'),true);
});

test('URL import runs on backend and only a completed sealed image becomes selectable', async () => {
  const h = harness(); await open(h);
  h.get('fl-url').value = 'https://images.example/bootfs.ext4?signature=private';
  h.get('fl-url-sha256').value = 'A'.repeat(64);
  h.get('fl-download').click();
  const submitted = h.take('/api/image-download');
  assert.equal(submitted.payload.sha256, 'a'.repeat(64));
  assert.equal(submitted.payload.serial, board.serial);
  assert.ok(submitted.payload.request_id);
  assert.equal(h.get('fl-plan').disabled, true);
  submitted.resolve({task: {id:'download-1', state:'running'}}); await drain();
  assert.equal(h.get('fl-download-cancel').hidden, false);
  const downloaded = {id:'url-image', name:'bootfs.ext4', size:32, sha256:'a'.repeat(64), state:'ready'};
  h.take('/api/task?id=download-1').resolve({task:{id:'download-1', state:'completed', result:{artifact:downloaded}}});
  await drain();
  assert.equal(h.get('fl-artifact').value, 'url-image');
  assert.equal(h.get('fl-mode').value, 'partitions');
  assert.equal(h.get('fl-start').disabled, true);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
});

test('lost URL submission reuses its original request and cancellation does not cancel flashing', async () => {
  const h = harness(); await open(h);
  h.get('fl-url').value = 'https://images.example/opensbi.itb';
  h.get('fl-download').click(); const first = h.take('/api/image-download');
  first.reject(new Error('network lost')); await drain();
  h.get('fl-download').click(); const again = h.take('/api/image-download');
  assert.equal(again.payload.request_id, first.payload.request_id);
  again.resolve({task:{id:'download-original', state:'running'}}); await drain();
  h.get('fl-download-cancel').click();
  assert.equal(h.take('/api/task-cancel').payload.id, 'download-original');
});

test('refresh resumes the same backend image download without submitting another import', async () => {
  const storage = new Map(), first = harness(storage); await open(first);
  first.get('fl-url').value = 'https://images.example/opensbi.itb'; first.get('fl-download').click();
  first.take('/api/image-download').resolve({task:{id:'persistent-image-task', kind:'image-download', state:'running'}}); await drain();
  const refreshed = harness(storage); await open(refreshed);
  const resumed = refreshed.take('/api/task?id=persistent-image-task');
  resumed.resolve({task:{id:'persistent-image-task', kind:'image-download', state:'running'}}); await drain();
  assert.equal(refreshed.requests.some(entry => entry.url === '/api/image-download'), false);
  refreshed.take('/api/task?id=persistent-image-task').resolve({task:{id:'persistent-image-task', kind:'image-download', state:'completed',
    result:{artifact:{id:'restored-image',name:'opensbi.itb',size:16,sha256:'b'.repeat(64),state:'ready'}}}}); await drain();
  assert.equal(refreshed.get('fl-artifact').value,'restored-image');
  assert.equal(storage.has('decklab-flash-download:SN-1'),false);
  assert.equal(JSON.parse(storage.get('decklab-flash-image')).id,'restored-image');
});

test('a confirmed failed import permits an explicit new download request', async () => {
  const h = harness(); await open(h); h.get('fl-url').value = 'https://images.example/opensbi.itb';
  h.get('fl-download').click(); const first = h.take('/api/image-download');
  first.resolve({task:{id:'failed-image',state:'running'}}); await drain();
  h.take('/api/task?id=failed-image').resolve({task:{id:'failed-image',state:'failed',error:'fixture download failure'}}); await drain();
  h.get('fl-download').click(); const next = h.take('/api/image-download');
  assert.notEqual(next.payload.request_id, first.payload.request_id);
});


const norTarget = {target_id:'nor-fixture', target_generation:'brom-original', label:'模拟 NOR'};
const norBackup = {artifact:{id:'b'.repeat(24), name:'nor-before.bin', size:8388608, sha256:'1'.repeat(64)},
  partitions:[{name:'opensbi', offset:1769472, size:393216, content:{version:'OpenSBI v1.6',version_state:'identified',
    size:393216,sha256:'2'.repeat(64)}}, {name:'env',offset:655360,size:65536,content:{version_state:'not-applicable',sha256:'3'.repeat(64)}}]};
const norReady = {id:'nor-task',kind:'flash',state:'running',flash_targets:[{target_id:norTarget.target_id,generation:norTarget.target_generation}],
  progress:{phase:'awaiting-selection'},flash:{operation:'nor-workspace',nor_before:norBackup,write_result:'not-started',
    inspection:{layout_digest:'nor-live',storages:[{id:'active',label:'NOR',size:8388608,partitions:[
      {name:'opensbi',offset:1769472,size:393216},{name:'env',offset:655360,size:65536}]}]},payload_candidates:[artifact]}};

test('reopening a prepared task restores reset capability without probing or changing selections', async () => {
  const h=harness();h.api.open(norTarget,norReady);await drain();
  assert.equal(h.get('fl-reset-after').disabled,true);
  const query=h.take('/api/flash-capabilities');
  assert.match(query.url,/target_id=nor-fixture/);assert.doesNotMatch(query.url,/target_generation=/);
  h.get('fl-mode').value='partitions';h.get('fl-mode').emit('change');
  const row=h.get('fl-partitions').children[0];row.checkbox.checked=true;
  row.artifactSelect.value=artifact.id;row.checkbox.emit('change');
  query.resolve({...capabilities,supported:false,reset_after_supported:true});await drain();
  assert.equal(h.get('fl-reset-after').disabled,false);
  assert.equal(h.get('fl-mode').value,'partitions');assert.equal(row.checkbox.checked,true);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-target'||r.url==='/api/task-start'),false);
  h.get('fl-reset-after').checked=true;h.get('fl-reset-after').emit('change');
  h.get('fl-select').click();const preview=h.take('/api/flash-selection');
  assert.equal(preview.payload.reset_after,true);assert.equal(preview.payload.target_generation,'brom-original');
  preview.resolve({plan:{id:'restored-reset-plan',reset_after:true,writes:[{storage:'active',partition:'opensbi',size:32}]}});await drain();
  const poll=[...h.timers.values()][0];poll();await drain();
  h.take('/api/task?id=').resolve({task:{...norReady,progress:{phase:'writing'}}});await drain();
  assert.equal(h.get('fl-reset-after').disabled,true);
  assert.equal(h.requests.some(r=>r.url.startsWith('/api/flash-capabilities')),false);
});

test('history recovery restores reset capability and late capability results cannot cross devices', async () => {
  const h=harness();h.api.open(norTarget);h.take('/api/tasks').resolve({tasks:[norReady]});await drain();
  const stale=h.take('/api/flash-capabilities');
  h.api.open({...norTarget,target_id:'other-target'}, {...norReady,id:'other-task',
    flash_targets:[{target_id:'other-target',generation:'other-generation'}]});await drain();
  const current=h.take('/api/flash-capabilities');
  current.resolve({...capabilities,reset_after_supported:false});await drain();
  stale.resolve({...capabilities,reset_after_supported:true});await drain();
  assert.equal(h.get('fl-reset-after').disabled,true);
  assert.equal(h.requests.some(r=>r.url==='/api/flash-target'||r.url==='/api/task-start'),false);
});

test('image-free NOR preparation posts no payload or write scope and recovers an uncertain submission', async () => {
  const h=harness();h.api.open(norTarget);
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.take('/api/flash-target').resolve({target:norTarget,capabilities:{...capabilities,artifacts:[],storages:[],
    nor_workspace_supported:true,preparable:true,modes:['partitions']}});await drain();
  assert.equal(h.get('fl-read-nor').disabled,false);
  assert.equal(h.get('fl-mode').value,'partitions');assert.equal(h.get('fl-verify').value,'none');
  h.get('fl-read-nor').click();const request=h.take('/api/task-start');
  assert.equal(request.payload.kind,'nor-backup');assert.equal(request.payload.target_generation,'brom-original');
  assert.equal('artifact_id' in request.payload,false);assert.equal('plan_id' in request.payload,false);
  request.reject(new Error('lost response'));await drain();
  assert.equal(h.get('fl-read-nor').disabled,true);assert.equal(h.get('fl-recover').hidden,false);
  assert.equal(h.get('fl-files').disabled,true);
  h.get('fl-read-nor').click();assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.get('fl-recover').click();h.take('/api/tasks').resolve({tasks:[{...norReady,flash:{...norReady.flash,request_id:request.payload.request_id}}]});await drain();
  assert.match(h.get('fl-task-id').textContent,/nor-task/);
  assert.equal(h.get('fl-recover').hidden,true);
});

test('NOR partition comparison shows real backup versions and file metadata after preview in the original task', async () => {
  const h=harness();h.api.open(norTarget,norReady);await drain();
  assert.match(h.get('fl-partitions').textContent,/OpenSBI v1.6/);
  assert.match(h.get('fl-partitions').textContent,/配置内容，无软件版本/);
  assert.match(h.get('fl-backups').textContent,/写前整片备份/);
  const link=h.get('fl-backups').children[0].children[1];
  assert.match(link.href,/artifact-download\?id=bbbbbbbbbbbbbbbbbbbbbbbb/);
  assert.equal(h.get('fl-progress').hidden,true);
  assert.match(h.get('fl-progress-text').textContent,/等候核对写入范围/);
  const row=h.get('fl-partitions').children[0];
  assert.equal(row.artifactSelect.hidden,true);
  assert.equal(row.payloadInfo.textContent,'');
  assert.doesNotMatch(h.get('fl-results').textContent,/未开始|未请求|启动结果/);
  row.checkbox.checked=true;row.artifactSelect.value=artifact.id;
  row.checkbox.emit('change');assert.equal(row.artifactSelect.hidden,false);h.get('fl-select').click();
  const preview=h.take('/api/flash-selection');assert.equal(preview.payload.id,'nor-task');
  assert.equal(preview.payload.target_generation,'brom-original');
  preview.resolve({plan:{id:'new-partition-plan',write_layout:false,writes:[{storage:'active',partition:'opensbi',size:32}],
    payload_metadata:[{storage:'active',partition:'opensbi',content:{version:'OpenSBI v1.7',version_state:'identified',sha256:'4'.repeat(64),size:32}}]}});await drain();
  assert.match(row.payloadInfo.textContent,/OpenSBI v1.7/);assert.equal(row.checkbox.checked,true);
  assert.match(row.payloadInfo.textContent,/载荷 SHA-256.*32.*字节/);
  h.get('fl-select').click();const commit=h.take('/api/flash-selection');
  assert.equal(commit.payload.id,'nor-task');assert.equal(commit.payload.plan_id,'new-partition-plan');
  assert.equal('selections' in commit.payload,false);
});

test('NOR preview compares replacement-file length before writing and clears it when changing files', async () => {
  const h=harness();h.api.open(norTarget,norReady);await drain();
  let row=h.get('fl-partitions').children[0];
  row.checkbox.checked=true;row.artifactSelect.value=artifact.id;row.checkbox.emit('change');
  h.get('fl-select').click();
  const comparison={size:32,sha256:artifact.sha256,expected_sha256:artifact.sha256,matches:true,
    phase:'before',backup_id:norBackup.artifact.id,backup_sha256:norBackup.artifact.sha256};
  h.take('/api/flash-selection').resolve({plan:{id:'comparison-plan',write_layout:false,
    writes:[{storage:'active',partition:'opensbi',size:32}],payload_metadata:[{
      storage:'active',partition:'opensbi',sha256:artifact.sha256,
      content:{version:'OpenSBI v1.7',size:32,sha256:artifact.sha256},board_comparison:comparison}]}});await drain();
  assert.match(row.currentInfo.textContent,/与替换文件一致.*32.*字节/);
  assert.match(row.currentInfo.textContent,/按替换文件长度 SHA-256.*32.*字节/);
  assert.match(row.currentInfo.textContent,/板上备份.*aaaa.*替换文件.*aaaa/);
  assert.match(row.currentInfo.textContent,/整分区 SHA-256.*393216.*字节/);
  assert.equal(row.checkbox.checked,true,'comparison never changes the reviewed write scope');
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.get('fl-cache').click();h.chooseImage({...artifact,id:'different-file',name:'opensbi.itb',sha256:'5'.repeat(64)});
  row=h.get('fl-partitions').children[0];
  assert.doesNotMatch(row.currentInfo.textContent,/与替换文件一致|与替换文件不同|按替换文件长度/);
  assert.match(row.currentInfo.textContent,/整分区 SHA-256/);
});

test('NOR preview reports unequal bytes and rejects comparisons from another backup or payload', async () => {
  for (const changed of [{matches:false},{backup_id:'wrong-backup'},{expected_sha256:'9'.repeat(64)}]) {
    const h=harness();h.api.open(norTarget,norReady);await drain();
    const row=h.get('fl-partitions').children[0];
    row.checkbox.checked=true;row.artifactSelect.value=artifact.id;row.checkbox.emit('change');
    h.get('fl-select').click();
    const comparison={size:32,sha256:'6'.repeat(64),expected_sha256:artifact.sha256,matches:false,
      phase:'before',backup_id:norBackup.artifact.id,backup_sha256:norBackup.artifact.sha256,...changed};
    h.take('/api/flash-selection').resolve({plan:{id:'comparison-plan',writes:[{storage:'active',partition:'opensbi'}],
      payload_metadata:[{storage:'active',partition:'opensbi',sha256:artifact.sha256,
        content:{size:32,sha256:artifact.sha256},board_comparison:comparison}]}});await drain();
    if (changed.matches===false) assert.match(row.currentInfo.textContent,/与替换文件不同.*32.*字节/);
    else assert.doesNotMatch(row.currentInfo.textContent,/与替换文件一致|与替换文件不同/);
  }
});

test('NOR readback distinguishes full partition digest from exact payload-range comparison', async () => {
  const h=harness(),content={size:393216,sha256:'a'.repeat(64),version:'OpenSBI v1.7',
    payload_comparison:{size:80,sha256:'b'.repeat(64),expected_sha256:'b'.repeat(64),matches:true}};
  const after={...norBackup,partitions:[{name:'opensbi',size:393216,content}],artifact:{...norBackup.artifact,id:'c'.repeat(24)}};
  h.api.open(norTarget,{...norReady,state:'completed',flash:{...norReady.flash,nor_after:after}});await drain();
  assert.match(h.get('fl-partitions').textContent,/整分区 SHA-256.*393216.*字节/);
  assert.match(h.get('fl-partitions').textContent,/与本次载荷一致.*80.*字节/);
  assert.match(h.get('fl-partitions').textContent,/载荷范围 SHA-256.*80.*字节/);
  assert.match(h.get('fl-partitions').textContent,/板上回读.*bbbb.*本次载荷.*bbbb/);
  h.api.open(norTarget,{...norReady,state:'failed',flash:{...norReady.flash,nor_after:{...after,partitions:[
    {name:'opensbi',size:393216,content:{...content,payload_comparison:{...content.payload_comparison,matches:false}}}]}}});await drain();
  assert.match(h.get('fl-partitions').textContent,/与本次载荷不同/);
});

test('NOR byte progress and readback failures remain separate from completed writes', async () => {
  const h=harness();h.api.open(norTarget,{...norReady,state:'failed',error:'readback mismatch',
    progress:{phase:'verifying-nor',done:4194304,total:8388608},flash:{...norReady.flash,
      write_result:'completed',verify_result:'failed',nor_after:{...norBackup,artifact:{...norBackup.artifact,id:'c'.repeat(24)}}}});await drain();
  assert.equal(h.get('fl-progress').value,4194304);assert.equal(h.get('fl-progress').max,8388608);
  assert.match(h.get('fl-progress-text').textContent,/回读整片 NOR 并校验.*50%/);
  assert.match(h.get('fl-results').textContent,/写入结果.*已完成/);assert.match(h.get('fl-results').textContent,/校验结果.*失败/);
  assert.match(h.get('fl-backups').textContent,/写后整片回读/);
  assert.equal(h.get('fl-current-heading').textContent,'写后回读内容');
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('URL payload import during NOR selection carries the original task generation', async () => {
  const h=harness();h.api.open(norTarget,norReady);await drain();
  assert.equal(h.get('fl-download').disabled,false);
  h.get('fl-url').value='https://firmware.example/opensbi.itb';h.get('fl-download').click();
  const download=h.take('/api/image-download');assert.equal(download.payload.task_id,'nor-task');
  assert.equal(download.payload.target_generation,'brom-original');
});

test('full package is the NOR default and never carries hidden partition selections', async () => {
  const h=harness();h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,nor_workspace_supported:true,preparable:true});await drain();
  h.take('/api/tasks').resolve({tasks:[]});await drain();
  assert.equal(h.get('fl-mode').value,'full');assert.equal(h.get('fl-read-nor').hidden,true);
  assert.equal(h.get('fl-verify').value,'none');
  h.get('fl-artifact').value=artifact.id;h.get('fl-artifact').emit('change');
  const row=h.get('fl-partitions').children[0];row.checkbox.checked=true;row.artifactSelect.value=artifact.id;
  h.get('fl-plan').click();
  const request=h.take('/api/flash-plan');assert.equal(request.payload.mode,'full');assert.deepEqual(request.payload.selections,[]);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('select all maps the package to every partition, deselection stays scoped and full mode drops the checks', async () => {
  const pkg={...artifact,id:'full-package',name:'firmware.tar.gz'},h=harness();h.api.open(board);
  h.take('/api/flash-capabilities').resolve({...capabilities,artifacts:[pkg]});await drain();h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.get('fl-artifact').value=pkg.id;h.get('fl-artifact').emit('change');
  h.get('fl-mode').value='partitions';h.get('fl-mode').emit('change');
  h.get('fl-all-partitions').checked=true;h.get('fl-all-partitions').emit('change');
  const rows=h.get('fl-partitions').children;assert.ok(rows.every(row=>row.checkbox.checked&&row.artifactSelect.value===pkg.id));
  h.get('fl-plan').click();const selected=h.take('/api/flash-plan');assert.equal(selected.payload.selections.length,2);
  selected.resolve({error:'fixture preview failure'},409);await drain();
  rows[0].checkbox.checked=false;rows[0].checkbox.emit('change');assert.equal(h.get('fl-all-partitions').indeterminate,true);
  h.get('fl-mode').value='full';h.get('fl-mode').emit('change');h.get('fl-plan').click();
  assert.deepEqual(h.take('/api/flash-plan').payload.selections,[]);
});

test('selection preview errors survive task polling and clear when the user edits the selection', async () => {
  const h=harness();h.api.open(norTarget,norReady);await drain();
  h.get('fl-store').click();h.chooseImage(artifact);
  const row=h.get('fl-partitions').children[0];row.checkbox.checked=true;row.artifactSelect.value=artifact.id;
  h.get('fl-select').click();h.take('/api/flash-selection').resolve({error:'fixture layout rejected: mismatched-size'},409);await drain();
  assert.match(h.get('fl-status').textContent,/mismatched-size/);
  const poll=[...h.timers.values()][0];poll();await drain();
  h.take('/api/task?id=').resolve({task:{...norReady,flash:{...norReady.flash,selection_deadline:1900000000}}});await drain();
  assert.match(h.get('fl-status').textContent,/mismatched-size/);
  assert.match(h.get('fl-task-status').textContent,/确认剩余/);
  row.checkbox.emit('change');assert.equal(h.get('fl-status').textContent,'');
});

test('pasting a URL downloads immediately and blur cannot submit it again after completion', async () => {
  const h=harness();await open(h);let prevented=false;
  const url='https://images.example/firmware.tar.gz';
  h.get('fl-url').emit('paste',{clipboardData:{getData:()=>url},preventDefault:()=>{prevented=true;}});
  const request=h.take('/api/image-download');assert.equal(prevented,true);assert.equal(request.payload.url,url);
  assert.equal(h.get('fl-download').hidden,true);
  request.resolve({task:{id:'pasted-image',state:'running'}});await drain();
  h.take('/api/task?id=pasted-image').resolve({task:{id:'pasted-image',state:'completed',result:{artifact:{...artifact,name:'firmware.tar.gz',state:'ready'}}}});await drain();
  assert.match(h.get('fl-source-summary').textContent,/firmware.tar.gz/);
  h.get('fl-url').emit('change');assert.equal(h.requests.some(r=>r.url==='/api/image-download'),false);
});

test('typed URL enters once on Enter and invalid or incomplete values do not submit', async () => {
  const h=harness();await open(h);
  h.get('fl-url').value='https://';h.get('fl-url').emit('change');assert.equal(h.requests.some(r=>r.url==='/api/image-download'),false);
  h.get('fl-url').value='https://images.example/opensbi.itb';h.get('fl-url-sha256').value='bad';
  h.get('fl-url').emit('keydown',{key:'Enter',preventDefault(){}});assert.match(h.get('fl-status').textContent,/SHA-256/);
  assert.equal(h.requests.some(r=>r.url==='/api/image-download'),false);
  h.get('fl-url-sha256').value='';h.get('fl-url').emit('keydown',{key:'Enter',preventDefault(){}});
  assert.equal(h.take('/api/image-download').payload.url,'https://images.example/opensbi.itb');
});

test('downloaded cache opens local files while the store can restore its last source', async () => {
  const h=harness();await open(h);h.get('fl-cache').click();assert.equal(h.imagePicker().source,'local');
  h.chooseImage(artifact);assert.match(h.get('fl-source-summary').textContent,/image.bin.*32 B/);
  h.get('fl-store').click();assert.equal(h.imagePicker().source,undefined);
});

test('normal URL download, fast flash and RAM boot have separate submission paths', async () => {
  const h = harness();
  h.api.open({}, null, false, 'ram'); await drain();
  assert.equal(h.get('flash-dialog').dataset.flow, 'ram');
  assert.equal(h.get('fl-image-title').textContent, '启动镜像');
  assert.equal(h.get('fl-ram-boot').hidden, false);
  assert.equal(h.get('fl-plan').hidden, true);
  assert.equal(h.get('fl-fast').hidden, true);
  h.api.open(null, null, false, 'fast'); await drain();
  assert.equal(h.get('fl-fast').hidden, false);
  assert.equal(h.get('fl-fast-start').hidden, false);
  assert.equal(h.get('fl-fast-start').disabled, true);
  assert.equal(h.get('fl-ram-boot').hidden, true);
  h.get('fl-fast-store').click();
  assert.equal(h.imagePicker().streamURL('https://repo.example/os.zip'), false);
  h.chooseImage({url:'https://repo.example/os.tar.gz'});
  assert.equal(h.get('fl-fast-url').value,'https://repo.example/os.tar.gz');
  h.api.open(null, null, false, 'normal'); await drain();
  assert.throws(() => h.chooseImage({url:'https://repo.example/another.tar.gz'}), /状态已变化/);
  assert.equal(h.get('fl-fast').hidden, true);
  assert.equal(h.get('fl-plan').hidden, false);
  h.get('fl-url-toggle').click();
  assert.equal(h.get('fl-url-panel').hidden, false);
  assert.equal(h.requests.some(request => request.payload), false);
});

test('URL store firmware selection moves to the standard NOR review without starting a stream', async () => {
  const h = harness(); h.api.open(null,null,false,'fast'); await drain();
  h.get('fl-fast-store').click();
  assert.equal(h.imagePicker().streamFirmware,true);
  const fw = {id:'standalone-firmware',name:'Debian-13-Firmware-K1-202611161333.tar.gz',size:200,norFirmware:true};
  h.chooseImage(fw);
  assert.equal(h.get('flash-dialog').dataset.flow,'normal');
  assert.equal(h.get('fl-artifact').value,fw.id);
  assert.equal(h.get('fl-target-storage-field').hidden,true);
  assert.deepEqual(h.take('/api/image-prepare').payload, {id:fw.id});
  assert.equal(h.requests.some(entry => entry.payload),false);
});

const controlledBoard = {...board, connection_id:'uart-generation-1'};
const scopedUSB = {target_id:'confirmed-usb', target_generation:'usb-generation-2', serial:board.serial, present:true};
const otherUSB = {target_id:'unrelated-usb', target_generation:'usb-generation-1', serial:'OTHER-SN', present:true};
const usbCapabilities = {...capabilities, stream_flash_supported:true, ram_boot_supported:true,
  nor_workspace_supported:true, storage_inventory_supported:true};

async function openWithoutUSB(h, operation) {
  h.api.open(controlledBoard, null, false, operation);
  h.take('/api/flash-capabilities').resolve({...capabilities, supported:false}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.take('/api/flash-discover').resolve({targets:[otherUSB]}); await drain();
  h.take('/api/hardware').resolve({actions:['enter-download'],wiring:{revision:'revision-1'}}); await drain();
  assert.equal(h.get('fl-target-picker').hidden, true);
  assert.equal(h.requests.some(r => r.url === '/api/flash-target' || r.payload), false);
}

async function confirmDownload(h, button, detectedCapabilities = usbCapabilities) {
  h.get(button).click();
  h.get(button).click();
  h.take('/api/flash-discover').resolve({targets:[otherUSB]}); await drain();
  h.take('/api/hardware').resolve({actions:['enter-download'],wiring:{revision:'revision-2'}}); await drain();
  const control = h.take('/api/task-start');
  assert.equal(control.payload.kind,'hardware-enter-download');
  assert.equal(control.payload.serial,board.serial);
  assert.equal(control.payload.connection_id,controlledBoard.connection_id);
  assert.equal(control.payload.wiring_revision,'revision-2');
  assert.ok(control.payload.request_id);
  control.resolve({task:{id:'original-control',state:'completed'}}); await drain();
  h.take('/api/flash-discover').resolve({targets:[otherUSB,scopedUSB]}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  const probe = h.take('/api/flash-target');
  assert.equal(probe.payload.target_id,scopedUSB.target_id);
  probe.resolve({target:scopedUSB,capabilities:detectedCapabilities}); await drain();
}

test('device fast flash enters download and submits exactly one stream with one confirmation', async () => {
  const h = harness(new Map()); await openWithoutUSB(h, 'fast');
  h.get('fl-fast-url').value='https://images.example/os.tar.gz?key=temporary'; h.get('fl-fast-url').emit('input');
  assert.equal(h.get('fl-fast-start').disabled, false);
  await confirmDownload(h, 'fl-fast-start');
  const stream = h.take('/api/flash-stream');
  assert.equal(stream.payload.target_generation,scopedUSB.target_generation);
  assert.equal(stream.payload.serial,board.serial);
  assert.equal(stream.payload.system_storage,'auto');
  assert.equal(stream.payload.url,'https://images.example/os.tar.gz?key=temporary');
  stream.reject(new Error('response lost')); await drain();
  assert.equal(h.get('fl-fast-start').disabled,true);
  h.get('fl-recover').click();
  assert.match(h.take('/api/task?request_id=').url,new RegExp(stream.payload.request_id));
  assert.equal(h.requests.some(r => r.payload),false);
});

test('device RAM boot keeps the chosen image through download mode and starts on one confirmation', async () => {
  const h = harness(); await openWithoutUSB(h,'ram');
  const os = {...artifact, name:'Debian-K3.tar.gz'};
  h.get('fl-cache').click(); h.chooseImage(os);
  await confirmDownload(h,'fl-ram-boot',{...usbCapabilities,artifacts:[os]});
  const start = h.take('/api/task-start');
  assert.equal(start.payload.kind,'ram-boot');
  assert.equal(start.payload.artifact_id,artifact.id);
  assert.equal(start.payload.target_id,scopedUSB.target_id);
  assert.equal(start.payload.binding_revision,usbCapabilities.binding_revision);
  assert.equal(h.requests.some(r => r.payload),false);
});

test('ordinary flash enters download and reads storage without a write plan', async () => {
  const h = harness(); await openWithoutUSB(h,'normal');
  assert.equal(h.get('fl-setup').children[1], h.get('fl-connect'));
  assert.equal(h.get('fl-board-name').textContent, board.label || board.key);
  assert.match(h.get('fl-board-identity').textContent, new RegExp(board.serial));
  assert.equal(h.get('fl-prepare-board').hidden, false);
  assert.equal(h.get('fl-prepare-board').disabled, false);
  assert.equal(h.get('fl-prepare-board').textContent,'进入下载模式并读取设备存储');
  assert.doesNotMatch(h.get('fl-status').textContent, /可由 DeckLab/);
  await confirmDownload(h,'fl-prepare-board');
  const read = h.take('/api/task-start');
  assert.equal(read.payload.kind,'nor-backup');
  assert.equal(read.payload.target_id,scopedUSB.target_id);
  assert.equal(read.payload.read_system,true);
  assert.ok(!('artifact_id' in read.payload));
  assert.equal(h.requests.some(r => r.payload),false);
});

test('a pre-write loader failure can re-enter download without leaving the flash workspace', async () => {
  const h = harness();
  await openWithoutUSB(h,'normal');
  h.get('fl-cache').click(); h.chooseImage(artifact);
  await confirmDownload(h,'fl-prepare-board');
  const error = {id:'failed-loader',kind:'flash',state:'failed',error:'加载器超时: loader-fsbl-timeout',
    flash:{operation:'nor-workspace',write_result:'not-started'}};
  h.take('/api/task-start').resolve({task:error}); await drain();
  h.take('/api/flash-events').resolve({events:[],details:error.flash}); await drain();
  h.take('/api/hardware').resolve({actions:['enter-download'],wiring:{revision:'reentry-wiring'}}); await drain();
  assert.equal(h.get('fl-reenter-dialog').open,true);
  h.get('fl-reenter-cancel').click();
  assert.equal(h.get('fl-reenter-dialog').open,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
  h.get('fl-reenter').click();
  h.get('fl-reenter-submit').click(); h.get('fl-reenter-submit').click();
  h.take('/api/flash-discover').resolve({targets:[scopedUSB]}); await drain();
  const control = h.take('/api/task-start');
  assert.equal(control.payload.kind,'hardware-enter-download');
  assert.equal(control.payload.serial,controlledBoard.serial);
  assert.equal(control.payload.connection_id,controlledBoard.connection_id);
  assert.equal(control.payload.wiring_revision,'reentry-wiring');
  control.resolve({task:{id:'reentered',state:'completed'}}); await drain();
  h.take('/api/flash-discover').resolve({targets:[scopedUSB]}); await drain();
  assert.equal(h.requests.some(r=>r.url==='/api/flash-target'||r.url==='/api/task-start'),false);
  const timer = h.timers.values().next().value; h.timers.clear(); timer(); await drain();
  const fresh = {...scopedUSB,target_generation:'usb-generation-3'};
  h.take('/api/flash-discover').resolve({targets:[scopedUSB,fresh]}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  const probe = h.take('/api/flash-target');
  assert.equal(probe.payload.target_generation,fresh.target_generation);
  probe.resolve({target:fresh,capabilities:usbCapabilities}); await drain();
  assert.equal(h.get('flash-dialog').open,true);
  assert.equal(h.get('fl-artifact').value,artifact.id);
  assert.match(h.get('fl-status').textContent,/已重新进入下载模式/);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('re-entry is only offered with a supported control and a confirmed no-write result', async () => {
  for (const writeResult of ['not-started','unknown-or-partial']) {
    const h = harness();
    await openWithoutUSB(h,'normal');
    await confirmDownload(h,'fl-prepare-board');
    const error = {id:'failed-loader',kind:'flash',state:'failed',error:'加载器超时: loader-fsbl-timeout',
      flash:{operation:'nor-workspace',write_result:writeResult}};
    h.take('/api/task-start').resolve({task:error}); await drain();
    h.take('/api/flash-events').resolve({events:[],details:error.flash}); await drain();
    if (writeResult === 'not-started') {
      h.take('/api/hardware').resolve({actions:['reset']}); await drain();
    }
    assert.equal(h.get('fl-reenter-dialog').open,false);
    assert.equal(h.get('fl-reenter').hidden,true);
    assert.equal(h.requests.some(r=>r.url.startsWith('/api/hardware') || r.url==='/api/task-start'),false);
  }
});

test('a failed storage read can redetect the board in place without resetting or losing the image', async () => {
  const h = harness();
  await openWithoutUSB(h,'normal');
  h.get('fl-cache').click(); h.chooseImage(artifact);
  await confirmDownload(h,'fl-prepare-board');
  const error = {id:'storage-failure',kind:'flash',state:'failed',error:'flashing executor reported failure: protocol-failed',
    progress:{phase:'reading-system-layout'},flash:{operation:'nor-workspace',write_result:'not-started'}};
  h.take('/api/task-start').resolve({task:error}); await drain();
  h.take('/api/flash-events').resolve({events:[],details:error.flash}); await drain();
  assert.equal(h.get('flash-dialog').dataset.stage,'setup');
  assert.match(h.get('fl-task-status').textContent,/protocol-failed/);
  assert.equal(h.get('fl-new').hidden,false);
  assert.equal(h.get('fl-new').textContent,'重新检测设备');
  h.get('fl-new').click();
  const fresh = {...scopedUSB,target_generation:'fresh-usb'};
  h.take('/api/flash-discover').resolve({targets:[fresh]}); await drain();
  h.take('/api/tasks').resolve({tasks:[error]}); await drain();
  const probe = h.take('/api/flash-target');
  assert.equal(probe.payload.target_generation,fresh.target_generation);
  probe.resolve({target:fresh,capabilities:usbCapabilities}); await drain();
  assert.equal(h.get('fl-task-status').textContent,'');
  assert.equal(h.get('fl-artifact').value,artifact.id);
  assert.equal(h.get('flash-dialog').open,true);
  assert.equal(h.get('fl-read-nor').disabled,false);
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('a matching USB target already in download mode bypasses hardware control', async () => {
  const h = harness(); await openWithoutUSB(h,'fast');
  h.get('fl-fast-url').value='https://images.example/os.tar.gz';h.get('fl-fast-url').emit('input');
  h.get('fl-fast-start').click();
  const anonymous = {...scopedUSB,serial:''};
  h.take('/api/flash-discover').resolve({targets:[anonymous,otherUSB]}); await drain();
  h.take('/api/flash-target').resolve({target:{...anonymous,console_link:{state:'linked',serial:board.serial}},capabilities:usbCapabilities}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  const stream = h.take('/api/flash-stream');
  assert.equal(stream.payload.serial,undefined);
  assert.equal(stream.payload.target_generation,scopedUSB.target_generation);
  assert.equal(h.requests.some(r => r.url.startsWith('/api/hardware') || r.url === '/api/task-start' || r.url === '/api/flash-target'),false);
});

test('first-use fast flash offers binding and lets an unpaired BootROM be selected explicitly', async () => {
  const bindings = [];
  const h = harness(new Map(), {canBind:()=>true, bindControl:board=>bindings.push(board.serial)});
  h.api.open(controlledBoard, null, false, 'fast');
  h.take('/api/flash-capabilities').resolve({...capabilities,supported:false}); await drain();
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.take('/api/flash-discover').resolve({targets:[]}); await drain();
  h.take('/api/hardware').resolve({actions:[]}); await drain();
  assert.equal(h.get('fl-fast-bind').hidden,false);
  h.get('fl-fast-bind').click(); assert.deepEqual(bindings,[controlledBoard.serial]);
  h.get('fl-fast-url').value='https://images.example/os.tar.gz';
  h.get('fl-fast-detect').click();
  const unpaired = {...scopedUSB,serial:'',console_link:{state:'paired'}};
  h.take('/api/flash-discover').resolve({targets:[unpaired,otherUSB]}); await drain();
  h.take('/api/flash-target').resolve({target:unpaired,capabilities:usbCapabilities}); await drain();
  h.take('/api/hardware').resolve({actions:[]}); await drain();
  assert.equal(h.get('fl-target-picker').hidden,false);
  assert.deepEqual(h.get('fl-device').children.map(node=>node.value),['',unpaired.target_id]);
  assert.equal(h.requests.length,0);
  h.get('fl-device').value=unpaired.target_id; h.get('fl-device').emit('change');
  h.take('/api/tasks').resolve({tasks:[]}); await drain();
  h.take('/api/flash-target').resolve({target:unpaired,capabilities:usbCapabilities}); await drain();
  assert.equal(h.get('fl-fast-start').disabled,false);
  assert.equal(h.get('fl-fast-url').value,'https://images.example/os.tar.gz');
  assert.equal(h.requests.length,0);
});

test('fast flash lists verified disks from the prepared USB generation and waits for an explicit choice', async () => {
  const h = harness(new Map()); await openWithoutUSB(h, 'fast');
  h.get('fl-fast-url').value='https://images.example/os.tar.gz';
  await confirmDownload(h,'fl-fast-read-disks');
  const read=h.take('/api/task-start');
  assert.equal(read.payload.inventory_only,true);
  assert.equal(read.payload.read_system,true);
  const replacement={...scopedUSB,target_generation:'prepared-generation'};
  const inventory={storages:[{id:'scsi:0',size:100,capacity_verified:true},
    {id:'nvme:0',size:200,capacity_verified:true},{id:'mmc:0',size:0,capacity_verified:false}]};
  const ready={id:'fast-inventory',kind:'flash',state:'completed',request_id:read.payload.request_id,
    result:{target_generation:replacement.target_generation},flash:{operation:'storage-inventory',storage_inventory:inventory}};
  read.resolve({task:ready}); await drain();
  h.take('/api/flash-events').resolve({events:[]});
  h.take('/api/flash-discover').resolve({targets:[replacement]}); await drain();
  h.take('/api/tasks').resolve({tasks:[ready]}); await drain();
  h.take('/api/flash-target').resolve({target:replacement,capabilities:usbCapabilities}); await drain();
  const select=h.get('fl-fast-storage');
  assert.equal(select.children.find(node=>node.value==='auto').disabled,true);
  assert.equal(select.children.find(node=>node.value==='mmc:0').disabled,true);
  assert.equal(h.get('fl-fast-start').disabled,true);
  assert.equal(h.requests.length,0);
  select.value='scsi:0'; select.emit('change'); h.get('fl-fast-start').click();
  const start=h.take('/api/flash-stream');
  assert.equal(start.payload.system_storage,'scsi:0');
  assert.equal(start.payload.target_generation,replacement.target_generation);
});

test('a physical USB and UART mismatch stops the one-click flow before reset or write', async () => {
  const h = harness(); await openWithoutUSB(h,'fast');
  h.get('fl-fast-url').value='https://images.example/os.tar.gz'; h.get('fl-fast-url').emit('input');
  h.get('fl-fast-start').click();
  const mismatch = {...scopedUSB,serial:otherUSB.serial,console_link:{state:'mismatch',serial:board.serial,usb_serial:otherUSB.serial}};
  h.take('/api/flash-discover').resolve({targets:[mismatch]}); await drain();
  assert.match(h.get('fl-status').textContent,/身份冲突/);
  assert.equal(h.requests.length,0);
});

test('benchmarks skip disk selection by default and send the disk chosen for a write test', async () => {
  for (const measureWrite of [false, true]) {
    const h = harness();
    h.api.open(norTarget);
    h.take('/api/tasks').resolve({tasks:[]}); await drain();
    h.take('/api/flash-target').resolve({target:norTarget, capabilities:{...capabilities,calibrate_supported:true}});
    await drain();
    assert.equal(h.get('fl-tuning-storage-field').hidden, true);
    h.get('fl-target-storage').value = 'scsi:0';
    if (measureWrite) {
      h.get('fl-tuning-write').checked = true; h.get('fl-tuning-write').emit('change');
      assert.equal(h.get('fl-tuning-storage-field').hidden, false);
      h.get('fl-tuning-storage').value = 'nvme:0'; h.get('fl-tuning-storage').emit('input');
    }
    h.get('fl-tuning-run').click();
    const {payload} = h.take('/api/flash-calibrate');
    assert.equal(payload.measure_write, measureWrite);
    assert.equal(payload.system_storage, measureWrite ? 'nvme:0' : 'auto');
  }
});

test('unified storage read starts once from full mode and requests system metadata with NOR backup', async () => {
  const h=harness();h.api.open(norTarget);h.take('/api/tasks').resolve({tasks:[]});await drain();
  h.take('/api/flash-target').resolve({target:norTarget,capabilities:{...capabilities,nor_workspace_supported:true,
    storage_inventory_supported:true,preparable:true,modes:['full','partitions']}});await drain();
  assert.equal(h.get('fl-mode').value,'full');assert.equal(h.get('fl-read-nor').hidden,false);
  assert.equal(h.get('fl-read-nor').textContent,'读取设备存储');
  h.get('fl-read-nor').click();const request=h.take('/api/task-start');assert.equal(request.payload.kind,'nor-backup');
  assert.equal(request.payload.read_system,true);assert.equal('artifact_id' in request.payload,false);
  h.get('fl-read-nor').click();assert.equal(h.requests.some(r=>r.url==='/api/task-start'),false);
});

test('system storage metadata uses the proven default and changing views never changes write selections', async () => {
  const h=harness(),inventory={default_storage:'scsi:0',priority_source:'eeprom:UFS',storages:[
    {id:'nvme:0',size:64*1024**3,partitions:[{name:'rootfs',offset:1048576,size:4096,content:{version:'Debian 13',version_state:'identified'}}]},
    {id:'scsi:0',size:128*1024**3,partitions:[{name:'rootfs',offset:1048576,size:4096,content:{version:'Bianbu 2.3.5',version_state:'identified'}}]}]};
  h.api.open(norTarget,{...norReady,flash:{...norReady.flash,storage_inventory:inventory}});await drain();
  assert.equal(h.get('fl-system').hidden,false);assert.equal(h.get('fl-system-storage').value,'scsi:0');
  assert.match(h.get('fl-system-parts').textContent,/Bianbu 2.3.5/);
  assert.match(h.get('fl-system-origin').textContent,/EEPROM.*UFS/);
  h.get('fl-system-storage').value='nvme:0';h.get('fl-system-storage').emit('change');
  assert.match(h.get('fl-system-parts').textContent,/Debian 13/);
  assert.ok(h.get('fl-partitions').children.every(row=>!row.checkbox.checked));
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'||r.url==='/api/flash-selection'),false);
  h.api.open(norTarget,{...norReady,flash:{...norReady.flash,storage_inventory:{...inventory,default_storage:'',priority_source:'ram-policy'}}});await drain();
  assert.equal(h.get('fl-system-storage').value,'');assert.match(h.get('fl-system-origin').textContent,/未能确认默认介质/);
});

test('old EEPROM edit records resolve UFS to the unique SCSI disk for viewing only', async () => {
  const h=harness(), inventory={default_storage:'',default_candidate:'',priority_source:'eeprom:UFS',storages:[
    {id:'scsi:0',size:128*1024**3,partitions:[{name:'rootfs',size:4096,offset:1048576,content:{version:'Debian 13'}}]}]};
  h.api.open(norTarget,{...norReady,flash:{...norReady.flash,storage_inventory:inventory}});await drain();
  assert.equal(h.get('fl-system-storage').value,'scsi:0');
  assert.match(h.get('fl-system-origin').textContent,/EEPROM.*UFS/);
  assert.match(h.get('fl-system-parts').textContent,/Debian 13/);
  assert.ok(h.get('fl-partitions').children.every(row=>!row.checkbox.checked));
  assert.equal(h.requests.some(r=>r.url==='/api/task-start'||r.url==='/api/flash-selection'),false);
  for (const [disks,message] of [
    [[...inventory.storages,{id:'scsi:1',size:128*1024**3,partitions:[]}],/多个匹配/],
    [[{id:'nvme:0',size:64*1024**3,partitions:[]}],/未发现与优先启动介质匹配/]]) {
    h.api.open(norTarget,{...norReady,flash:{...norReady.flash,storage_inventory:{...inventory,storages:disks}}});await drain();
    assert.equal(h.get('fl-system-storage').value,'');assert.match(h.get('fl-system-origin').textContent,message);
  }
});

test('EEPROM priority edit sends a separate fixed action and retains the flash selection scope', async () => {
  const h=harness(), inventory={default_storage:'',priority_source:'eeprom:UFS',boot_priority_editable:true,storages:[]};
  h.api.open(norTarget,{...norReady,flash:{...norReady.flash,resolved_serial:'BOARD-SAMPLE-01',storage_inventory:inventory}});await drain();
  assert.equal(h.get('fl-boot-priority').value,'UFS');assert.equal(h.get('fl-boot-save').disabled,true);
  assert.match(h.get('fl-eeprom-identity').textContent,/BOARD-SAMPLE-01/);
  h.get('fl-boot-priority').value='SSD';h.get('fl-boot-priority').emit('change');h.get('fl-boot-save').click();
  const save=h.take('/api/flash-selection');
  assert.equal(save.payload.action,'boot-priority');assert.equal(save.payload.value,'SSD');assert.equal(save.payload.expected,'UFS');
  assert.equal('selections' in save.payload,false);assert.equal(h.get('fl-boot-save').disabled,true);
  save.resolve({task:{...norReady,flash:{...norReady.flash,storage_inventory:{...inventory,priority_source:'eeprom:SSD'},
    boot_priority_change:{state:'completed',verify_result:'verified'}}}});await drain();
  assert.match(h.get('fl-boot-status').textContent,/已回读校验.*下次启动/);
  assert.ok(h.get('fl-partitions').children.every(row=>!row.checkbox.checked));
});

test('uncertain EEPROM write cannot be resubmitted as another change', async () => {
  const h=harness(),inventory={default_storage:'',priority_source:'eeprom:UFS',boot_priority_editable:true,storages:[]};
  h.api.open(norTarget,{...norReady,flash:{...norReady.flash,storage_inventory:inventory,boot_priority_change:{state:'unknown'}}});await drain();
  assert.equal(h.get('fl-boot-priority').disabled,true);assert.equal(h.get('fl-boot-save').disabled,true);
  assert.match(h.get('fl-boot-status').textContent,/未确认/);
});

(async () => {
  for (const [name, fn] of tests) { await fn(); console.log('ok - ' + name); }
  console.log(tests.length + ' flash frontend isolation tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
