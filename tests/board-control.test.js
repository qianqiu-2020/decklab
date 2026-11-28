'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = ['tasks.js', 'board-control.js'].map(name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8')).join('\n');
class Element {
  constructor() { this.events = {}; this.children = []; this.value = ''; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  appendChild(child) { this.children.push(child); }
  addEventListener(name, fn) { this.events[name] = fn; }
  click() { if (!this.disabled && this.events.click) this.events.click(); }
  close() { this.open = false; }
  showModal() { this.open = true; }
}
const drain = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function harness() {
  const nodes = new Map(), requests = [], notices = [], shown = [], receipted = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const sandbox = {window: {}, document: {createElement: () => new Element()}, setTimeout() {}};
  vm.runInNewContext(source, sandbox);
  let ctx = {key: 'A', serial: 'SN-A', connectionId: 'A1'};
  const control = sandbox.window.DeckLabBoardControl.create({byId: get, getView: () => ctx,
    notice: (message, view, id) => { notices.push(message); receipted.push([view.key, id]); },
    openTaskDetails: (view, id) => shown.push([view.key, id]),
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, reject, body: options && options.body && JSON.parse(options.body),
      reply: data => resolve({ok: true, json: () => Promise.resolve(data)})}))});
  return {get, requests, notices, shown, receipted, control, select(value) { ctx = value; control.refresh(ctx); }, ctx};
}
(async () => {
  const h = harness(); h.control.refresh(h.ctx);
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].body, undefined);
  h.select({key: 'B', serial: 'SN-B', connectionId: 'B1'});
  h.requests[0].reply({supported: true, actions: ['reset']}); await drain();
  assert.equal(h.get('b-board-control').hidden, true, 'stale capabilities stay hidden');
  h.requests[1].reply({supported: true, actions: ['reset', 'enter-download', 'flash']}); await drain();
  h.get('b-board-control').click();
  assert.equal(h.get('board-control-dialog').open, true);
  assert.equal(h.get('board-control-target').textContent, 'SN-B');
  assert.equal(h.get('board-control-reset').hidden, false);
  assert.equal(h.get('board-control-enter-download').hidden, false);
  assert.equal(h.get('board-control-power-off').hidden, true);
  assert.equal(h.requests.length, 2, 'opening confirmation sends no mutation');
  h.get('board-control-reset').click(); h.get('board-control-enter-download').click();
  assert.equal(h.requests.length, 3, 'double click only submits once');
  assert.equal(h.requests[2].body.serial, 'SN-B');
  assert.equal(h.requests[2].body.connection_id, 'B1');
  assert.equal(h.requests[2].body.kind, 'hardware-reset');
  h.select({key: 'C', serial: 'SN-C', connectionId: 'C1'});
  assert.equal(h.get('board-control-dialog').open, false);
  h.get('board-control-status').textContent = 'new board';
  h.requests[2].reply({task: {id: 'task-B', state: 'completed', result: {control_result: 'acknowledged'}}}); await drain();
  assert.equal(h.get('board-control-status').textContent, 'new board', 'late task receipt cannot update another target');
  assert.deepEqual(h.notices, [], 'late task receipt cannot announce success on another board');

  const next = harness(); next.control.refresh(next.ctx);
  next.requests[0].reply({supported: true, actions: ['enter-download']}); await drain();
  next.get('b-board-control').click();
  next.get('board-control-enter-download').click();
  next.requests[1].reply({task: {id: 'download', state: 'running'}}); await drain();
  assert.equal(next.get('board-control-dialog').open, true, 'wait for the controller receipt');
  next.requests[2].reply({task: {id: 'download', state: 'completed', result: {control_result: 'acknowledged'}}}); await drain();
  assert.equal(next.get('board-control-dialog').open, false, 'success closes the dialog');
  assert.deepEqual(next.notices, ['控制请求已确认，请查看终端启动输出。']);
  assert.deepEqual(next.receipted, [['A', 'download']]);

  next.get('b-board-control').click();
  next.get('board-control-enter-download').click();
  next.requests[3].reply({task: {id: 'failed', state: 'failed', error: 'controller unavailable'}}); await drain();
  next.control.refresh(next.ctx);
  assert.equal(next.get('board-control-dialog').open, true);
  assert.equal(next.get('board-control-status').textContent, 'controller unavailable');
  next.get('board-control-status').children[0].click();
  assert.deepEqual(next.shown, [['A', 'failed']]);
  assert.equal(next.get('board-control-enter-download').disabled, true);
  next.get('board-control-close').click();
  next.control.refresh(next.ctx);
  next.requests[4].reply({supported: true, actions: ['reset']}); await drain();
  next.get('b-board-control').click();
  next.get('board-control-reset').click();
  next.requests[5].reject(new Error('connection closed')); await drain();
  assert.equal(next.get('board-control-dialog').open, true);
  assert.equal(next.get('board-control-status').textContent, '提交结果未知，请先在任务中检查记录。');
  console.log('ok - board control closes on success, retains failures, and preserves target scope and submits each direct action once');
})().catch(error => { console.error(error); process.exitCode = 1; });
