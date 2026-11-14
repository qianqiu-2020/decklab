'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.events = {}; this.dataset = {};
    this.value = ''; this.attributes = {}; this._text = ''; this.classes = new Set();
    this.classList = { toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name),
      remove: name => this.classes.delete(name) };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this.textContent = value; }
  appendChild(child) { return this.insertBefore(child,null); }
  insertBefore(child, before) {
    if(child.parentNode){const old=child.parentNode.children;old.splice(old.indexOf(child),1);}
    child.parentNode=this;const index=before?this.children.indexOf(before):this.children.length;
    this.children.splice(index,0,child);return child;
  }
  addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  emit(name, extra = {}) {
    const event = { preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...extra };
    (this.events[name] || []).forEach(fn => fn(event));
    return event;
  }
  click() { if (!this.disabled) this.emit('click'); }
  focus() { this.focused = true; }
  getBoundingClientRect() { return this.rect || {top:100,height:100,bottom:200,left:0,width:100,right:100}; }
}

function harness(storage = new Map()) {
  const elements = new Map(), requests = [], opened = [];
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = { document: { createElement: name => new Element(name) }, location: { hash: '' },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/devices.js'), 'utf8'), context);
  const controller = context.SerialdDevices.create({ byId,
    fetch: () => new Promise(resolve => requests.push(payload => resolve({ json: () => Promise.resolve(payload) }))),
    formatBytes: number => String(number || 0), age: () => 'now', open: key => opened.push(key),
    getView: () => null, isCurrent: () => false, loadSessions() {}, loadActivity() {}, renderInfo() {} });
  return { byId, controller, opened, storage,
    async poll(devices) { controller.poll(); requests.shift()({ devices }); await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); },
    cards: () => byId('grid').children,
    order: () => byId('grid').children.map(card => card.dataset.key),
    button(key, control) {
      const card = byId('grid').children.find(item => item.dataset.key === key);
      return card.children.at(-1).children.find(item => item.dataset.orderControl === control);
    } };
}

const board = (serial, values = {}) => ({ key: serial, label: serial, serial, alive: true,
  state: 'online', info: {}, bytes: 0, ...values });
const pending = (path, values = {}) => ({ key: 'pending-' + path, label: 'pending-' + path,
  serial: '', by_path: path, connection_id: 'connection-' + path, dev: '/dev/' + path,
  alive: true, state: 'pending', info: {}, bytes: 0, ...values });

const tests = [];
function test(name, run) { tests.push([name, run]); }

test('move buttons save personal order across reload and online changes without selecting a board', async () => {
  const storage = new Map(), h = harness(storage);
  await h.poll([board('A'), board('B'), board('C', { alive: false, state: 'offline' })]);
  assert.deepEqual(h.order(), ['A', 'B', 'C']);
  h.button('C', 'up').click();
  assert.deepEqual(h.order(), ['A', 'C', 'B']);
  assert.deepEqual(h.opened, []);
  assert.ok(h.button('C', 'up').focused);
  await h.poll([board('A', { alive: false, state: 'offline' }), board('B'), board('C'), board('D')]);
  assert.deepEqual(h.order(), ['A', 'C', 'B', 'D']);
  const reloaded = harness(storage);
  await reloaded.poll([board('B'), board('A'), board('D'), board('C')]);
  assert.deepEqual(reloaded.order(), ['A', 'C', 'B', 'D']);
  assert.equal(reloaded.button('A', 'up').disabled, true);
  assert.equal(reloaded.button('D', 'down').disabled, true);
});

test('filtered moves preserve hidden positions and keyboard movement keeps focus', async () => {
  const h = harness();
  await h.poll([board('A', { label: 'group-A' }), board('B'), board('C', { label: 'group-C' }), board('D')]);
  h.byId('device-search').value = 'group'; h.byId('device-search').emit('input');
  const event = h.button('C', 'up').emit('keydown', { altKey: true, key: 'ArrowUp' });
  assert.equal(event.prevented, true); assert.equal(event.stopped, true);
  assert.deepEqual(h.order(), ['C', 'A']);
  h.byId('device-search').value = ''; h.byId('device-search').emit('input');
  assert.deepEqual(h.order(), ['C', 'B', 'A', 'D']);
  assert.deepEqual(h.opened, []);
});

test('pending aliases remain visible and identification adopts attachment order without following stale offline paths', async () => {
  const h = harness();
  await h.poll([board('A'), pending('slot2', { attachment_label: '左侧板' }), board('OLD', { alive: false, state: 'offline', last_path: 'slot2' })]);
  h.button('pending-slot2', 'up').click();
  assert.ok(h.cards()[0].textContent.includes('左侧板'));
  assert.deepEqual(h.order(), ['pending-slot2', 'A', 'OLD']);
  await h.poll([board('NEW', { by_path: 'slot2' }), board('A'), board('OLD', { alive: false, state: 'offline', last_path: 'slot2' })]);
  assert.deepEqual(h.order(), ['NEW', 'A', 'OLD']);
  await h.poll([board('NEW', { alive: false, state: 'offline', last_path: 'slot2' }), board('A'), board('OLD', { alive: false, state: 'offline' }), pending('slot2')]);
  assert.deepEqual(h.order(), ['NEW', 'A', 'OLD', 'pending-slot2']);
});

test('renaming preserves serial order and reconnecting a known archive does not move its saved position', async () => {
  const h = harness();
  await h.poll([board('A'), board('B'), board('C', { alive: false, state: 'offline' }), pending('slot')]);
  h.button('pending-slot', 'up').click();
  await h.poll([board('A', { key: 'renamed-A', label: 'renamed-A' }), board('B'), board('C', { by_path: 'slot' })]);
  assert.deepEqual(h.order(), ['renamed-A', 'B', 'C']);
});

test('download USB badges preserve serial-offline state and personal order across attachment changes', async () => {
  const h = harness(), download = board('USB', { alive: false, state: 'offline', flash_connected: true, flash_mode: 'fastboot' });
  await h.poll([board('A'), download]);
  h.button('USB', 'up').click();
  const card = h.cards()[0];
  assert.deepEqual(h.order(), ['USB', 'A']);
  assert.ok(card.className.includes('offline'));
  assert.ok(card.textContent.includes('USB 下载模式'));
  assert.ok(card.textContent.includes('串口离线，刷机 USB 已连接'));
  assert.ok(!card.textContent.includes('历史设备'));
  assert.equal(download.alive, false);
  assert.equal(h.byId('count-online').textContent, '1');
  await h.poll([board('A'), { ...download, flash_connected: false, flash_last_seen: 100 }]);
  assert.deepEqual(h.order(), ['USB', 'A']);
  assert.ok(h.cards()[0].textContent.includes('历史设备'));
  assert.ok(!h.cards()[0].textContent.includes('USB 下载模式'));
  assert.ok(h.cards()[0].textContent.includes('上次检测刷机 USB：now'));
});

test('drag handles reorder exact visible targets while polling cannot replace an active drag', async () => {
  const h = harness();
  await h.poll([board('A'), board('B'), board('C')]);
  const drag = h.button('C', 'drag'), originalCard = h.cards()[0], data = {};
  drag.emit('dragstart', { dataTransfer: { setData: (key, value) => { data[key] = value; } } });
  await h.poll([board('A'), board('B'), board('C'), board('D')]);
  assert.equal(h.cards()[0], originalCard, 'an active browser drag must keep its DOM');
  originalCard.emit('drop', { clientY: 120 });
  assert.deepEqual(h.order(), ['C', 'A', 'B', 'D']);
  assert.equal(data['text/plain'], 'board:C');
  assert.deepEqual(h.opened, []);
});

test('dragover previews a landing slot and moves neighbors before saving on the placeholder', async () => {
  const h=harness();await h.poll([board('A'),board('B'),board('C')]);
  const initial=h.storage.get('decklab.device-order.v1'), source=h.cards()[2], target=h.cards()[0];
  let dragImage=null;
  h.button('C','drag').emit('dragstart',{clientX:50,clientY:120,dataTransfer:{setData(){},setDragImage:node=>{dragImage=node;}}});
  const event=target.emit('dragover',{clientY:110,dataTransfer:{}});
  assert.equal(event.prevented,true);assert.equal(dragImage,source);
  assert.deepEqual(h.order(),['C','A','B']);assert.equal(h.cards()[1],target,'existing cards shift instead of being rebuilt');
  assert.equal(h.storage.get('decklab.device-order.v1'),initial,'preview must not persist');
  source.emit('drop',{clientY:110});
  assert.deepEqual(h.order(),['C','A','B']);
  assert.deepEqual(JSON.parse(h.storage.get('decklab.device-order.v1')).ids,['board:C','board:A','board:B']);
  assert.deepEqual(h.opened,[]);
});

test('cancel restores the original order and keeps updates deferred during preview', async () => {
  const h=harness();await h.poll([board('A'),board('B'),board('C')]);
  const initial=h.storage.get('decklab.device-order.v1'), handle=h.button('C','drag');
  handle.emit('dragstart');h.cards()[0].emit('dragover',{clientY:110});
  await h.poll([board('A'),board('B'),board('C'),board('D')]);
  assert.deepEqual(h.order(),['C','A','B']);
  assert.equal(h.storage.get('decklab.device-order.v1'),initial);
  handle.emit('dragend');
  assert.deepEqual(h.order(),['A','B','C','D']);
  assert.deepEqual(JSON.parse(h.storage.get('decklab.device-order.v1')).ids,['board:A','board:B','board:C','board:D']);
});

test('downward preview commits its visible position and a late dragend does not undo it', async () => {
  const h=harness();await h.poll([board('A'),board('B'),board('C')]);
  const handle=h.button('A','drag'), source=h.cards()[0], target=h.cards()[2];
  handle.emit('dragstart');target.emit('dragover',{clientY:190});
  assert.deepEqual(h.order(),['B','C','A']);
  source.emit('dragover',{clientY:120});source.emit('drop');handle.emit('dragend');
  assert.deepEqual(h.order(),['B','C','A']);
});

test('filtered preview preserves hidden slots and horizontal layouts use the x midpoint', async () => {
  const h=harness();await h.poll([board('A',{label:'group-A'}),board('B'),board('C',{label:'group-C'}),board('D')]);
  h.byId('device-search').value='group';h.byId('device-search').emit('input');
  h.cards()[0].rect={top:0,bottom:100,height:100,left:0,right:100,width:100};
  h.cards()[1].rect={top:0,bottom:100,height:100,left:110,right:210,width:100};
  h.button('A','drag').emit('dragstart');h.cards()[1].emit('dragover',{clientX:200,clientY:10});
  assert.deepEqual(h.order(),['C','A']);h.cards()[1].emit('drop');
  h.byId('device-search').value='';h.byId('device-search').emit('input');
  assert.deepEqual(h.order(),['C','B','A','D']);
});

test('external drops and corrupted or unavailable browser storage cannot break discovery', async () => {
  const badStorage = new Map([['decklab.device-order.v1', '{malformed']]);
  const h = harness(badStorage);
  await h.poll([board('A'), board('B')]);
  h.cards()[0].emit('drop', { dataTransfer: { getData: () => 'board:B' } });
  assert.deepEqual(h.order(), ['A', 'B']);
  const unavailable = { get: () => { throw new Error('private'); }, set: () => { throw new Error('private'); } };
  const privatePage = harness(unavailable);
  await privatePage.poll([board('A'), board('B')]);
  privatePage.button('B', 'up').click();
  assert.deepEqual(privatePage.order(), ['B', 'A']);
});

(async function () {
  for (const [name, run] of tests) { await run(); console.log('ok - ' + name); }
  console.log(tests.length + ' device order tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
