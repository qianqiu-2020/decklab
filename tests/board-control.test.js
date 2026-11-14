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
  const nodes = new Map(), requests = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const sandbox = {window: {}, document: {createElement: () => new Element()}, setTimeout() {}};
  vm.runInNewContext(source, sandbox);
  let ctx = {key: 'A', serial: 'SN-A', connectionId: 'A1'};
  const control = sandbox.window.SerialdBoardControl.create({byId: get, getView: () => ctx,
    fetch: (url, options) => new Promise(resolve => requests.push({url, body: options && options.body && JSON.parse(options.body),
      reply: data => resolve({ok: true, json: () => Promise.resolve(data)})}))});
  return {get, requests, control, select(value) { ctx = value; control.refresh(ctx); }, ctx};
}
(async () => {
  const h = harness(); h.control.refresh(h.ctx);
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].body, undefined);
  h.select({key: 'B', serial: 'SN-B', connectionId: 'B1'});
  h.requests[0].reply({supported: true, actions: ['reset']}); await drain();
  assert.equal(h.get('b-board-control').hidden, true, 'stale capabilities stay hidden');
  h.requests[1].reply({supported: true, actions: ['reset', 'flash']}); await drain();
  h.get('b-board-control').click();
  assert.equal(h.get('board-control-dialog').open, true);
  assert.equal(h.get('board-control-target').textContent, 'SN-B');
  assert.equal(h.get('board-control-action').children.length, 1, 'legacy flasher is not a reset action');
  assert.equal(h.requests.length, 2, 'opening confirmation sends no mutation');
  h.get('board-control-submit').click(); h.get('board-control-submit').click();
  assert.equal(h.requests.length, 3, 'double click only submits once');
  assert.equal(h.requests[2].body.serial, 'SN-B');
  assert.equal(h.requests[2].body.connection_id, 'B1');
  assert.equal(h.requests[2].body.kind, 'hardware-reset');
  h.select({key: 'C', serial: 'SN-C', connectionId: 'C1'});
  assert.equal(h.get('board-control-dialog').open, false);
  h.get('board-control-status').textContent = 'new board';
  h.requests[2].reply({task: {id: 'task-B', state: 'completed', result: {control_result: 'acknowledged'}}}); await drain();
  assert.equal(h.get('board-control-status').textContent, 'new board', 'late task receipt cannot update another target');
  console.log('ok - board controls preserve target scope, explicit confirmation and single submission');
})().catch(error => { console.error(error); process.exitCode = 1; });
