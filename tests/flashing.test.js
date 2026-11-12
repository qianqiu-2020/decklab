'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = ['tasks.js', 'flashing.js'].map(name =>
  fs.readFileSync(path.join(__dirname, '..', 'static', name), 'utf8')).join('\n');

class Element {
  constructor() { this.children = []; this.events = {}; this.dataset = {}; this.value = ''; this.files = []; }
  set textContent(value) { this.content = value; this.children = []; }
  get textContent() { return String(this.content || '') + this.children.map(item => item.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute() {}
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  emit(type) { for (const fn of this.events[type] || []) fn(); }
  click() { if (!this.disabled) this.emit('click'); }
  show() { this.open = true; }
  close() { this.open = false; }
}
function harness() {
  const elements = new Map(), requests = [], timers = new Map(); let timer = 0;
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('fl-mode').value = 'full'; get('fl-verify').value = 'if-supported';
  const sandbox = {document: {createElement: () => new Element()}, Uint8Array, Date, Math,
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    setTimeout: fn => { timers.set(++timer, fn); return timer; }, clearTimeout: id => timers.delete(id),
    crypto: {getRandomValues: values => values.fill(++timer)}};
  sandbox.window = sandbox;
  vm.runInNewContext(source, sandbox);
  const api = sandbox.SerialdFlashing.create({byId: get, formatBytes: value => value + ' B',
    fetch: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, options,
      payload: options.body && JSON.parse(options.body), reject,
      resolve: (data, status = 200) => resolve({ok: status < 400, status, json: async () => data})}))});
  function take(prefix) { const index = requests.findIndex(entry => entry.url.startsWith(prefix));
    assert.notEqual(index, -1, 'missing request ' + prefix); return requests.splice(index, 1)[0]; }
  return {api, get, requests, timers, take};
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
  assert.match(h.get('fl-preview').textContent, /保留现有分区布局/);
  assert.equal(h.get('fl-start').disabled, false);
  h.get('fl-verify').value = 'required'; h.get('fl-verify').emit('change');
  assert.equal(h.get('fl-start').disabled, true, 'changing intent invalidates preview');
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
  h.get('fl-start').click(); assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
  h.get('fl-recover').click(); h.take('/api/tasks').resolve({tasks: [{id: 'original', kind: 'flash', state: 'completed',
    progress: {}, flash: {request_id: request.payload.request_id}, result: {write_result: 'written', verify_result: 'unsupported', boot_result: 'not-requested'}}]}); await drain();
  assert.match(h.get('fl-results').textContent, /写入完成.*不支持回读校验.*未请求/);
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
  assert.equal(denied.get('fl-plan').disabled, true); assert.equal(denied.get('fl-upload').disabled, true);
});

test('image upload uses bounded artifact chunks and seals before offering a preview', async () => {
  const h = harness(); await open(h);
  const bytes = Uint8Array.from([0, 255, 13, 10]);
  h.get('fl-files').files = [{name: 'payload.bin', size: bytes.length,
    slice: () => ({arrayBuffer: async () => bytes.buffer})}];
  h.get('fl-upload').click(); const create = h.take('/api/artifact-create');
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
  assert.match(h.get('fl-task-id').textContent, /recovery-task.*failed/);
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
  assert.match(h.get('fl-task-id').textContent, /old-inspection.*interrupted/);
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
  assert.equal(empty.get('fl-upload').disabled, true);
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
    capacity_source: 'primary-gpt-header', capacity_verified: false, verify: false, partitions: []}]}}); await drain();
  assert.match(h.get('fl-connections').textContent, /现场主 GPT.*容量尚未独立核验/);
  assert.match(h.get('fl-connections').textContent, /不支持写后回读校验/);
  assert.match(h.get('fl-target-storage').textContent, /GPT 声明，容量待核验/);
  assert.equal(required.disabled, true);
  assert.equal(h.get('fl-verify').value, 'if-supported');
});

test('BootROM image preparation is explicit, storage-free and continues partition selection in one task', async () => {
  const h = harness(), target = {target_id: 'brom-target', target_generation: 'brom-one', mode: 'bootrom'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, supported: false,
    can_prepare_from_image: true, can_upload: true, modes: ['partitions'], storages: [], readback_supported: false}}); await drain();
  assert.equal(h.get('fl-mode').value, 'partitions');
  assert.equal(h.get('fl-mode').disabled, true);
  assert.equal(h.get('fl-upload').disabled, false);
  assert.match(h.get('fl-status').textContent, /BootROM.*不写存储/);
  assert.match(h.get('fl-intro').textContent, /准备阶段.*不写入存储/);
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

test('BootROM preparation cannot acquire a hidden storage-write scope', async () => {
  const h = harness(), target = {target_id: 'brom-invalid', target_generation: 'brom-one'};
  h.api.open(target);
  h.take('/api/tasks').resolve({tasks: []}); await drain();
  h.take('/api/flash-target').resolve({target, capabilities: {...capabilities, supported: false,
    preparable: true, modes: ['partitions'], storages: []}}); await drain();
  h.get('fl-artifact').value = artifact.id; h.get('fl-artifact').emit('change'); h.get('fl-plan').click();
  h.take('/api/flash-plan').resolve({requires_prepare: true, plan: {id: 'unsafe', ready: false,
    write_layout: false, writes: [{storage: 'disk-a', partition: 'rootfs', size: 32}]}}); await drain();
  assert.equal(h.get('fl-start').disabled, true);
  assert.match(h.get('fl-status').textContent, /准备计划包含存储写入/);
  assert.equal(h.requests.some(entry => entry.url === '/api/task-start'), false);
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
  assert.equal(h.get('fl-upload').disabled, true);
  assert.equal(h.get('fl-discover').disabled, false);
});

test('awaiting-selection upload carries the original task id and does not create another preparation', async () => {
  const h = harness(), target = {target_id: 'upload-target', target_generation: 'new-loader'};
  const original = {id: 'upload-original', kind: 'flash', state: 'running', board_targets: [],
    flash_targets: [{target_id: target.target_id, generation: 'initial-brom'}], progress: {phase: 'awaiting-selection'},
    flash: {inspection: {layout_digest: 'live', storages: capabilities.storages}, payload_candidates: [artifact]}};
  h.api.open(target, original); await drain();
  h.get('fl-files').files = [{name: 'payload.bin', size: 3, slice: () => ({arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer})}];
  assert.equal(h.get('fl-upload').disabled, false);
  h.get('fl-upload').click(); const create = h.take('/api/artifact-create');
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

(async () => {
  for (const [name, fn] of tests) { await fn(); console.log('ok - ' + name); }
  console.log(tests.length + ' flash frontend isolation tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
