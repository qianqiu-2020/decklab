'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const window = {};
vm.runInNewContext(fs.readFileSync('static/serial-progress.js', 'utf8'), {window});
const begin = '\x1b7\x1b[0;23r\x1b8\x1b[1A';
const end = '\x1b7\x1b[0;24r\x1b8\x1b[1A';
const raw = 'before\n' + begin + '\x1b7\x1b[24;0fProgress: [88%]\x1b8\r\nmore\n' + end + '\x1b[Jafter';
const expected = raw.replace('[0;23r', '[0;41r').replace('[24;0f', '[42;0f').replace('[0;24r', '[0;42r');
// Every possible split of the real APT sequence must produce identical bytes.
for (let split = 0; split <= raw.length; split++) {
  let starts = 0;
  const mapper = window.SerialdSerialProgress.create({rows: () => 42, onStart: () => starts++});
  assert.equal(mapper.rewrite(raw.slice(0, split)) + mapper.rewrite(raw.slice(split)), expected);
  assert.equal(starts, 1);
}
const bytewise = window.SerialdSerialProgress.create({rows: () => 42});
assert.equal(Array.from(raw, c => bytewise.rewrite(c)).join(''), expected);
for (const untouched of ['\x1b[50;1Hfirmware\x1b[24;0f', '\x1b7\x1b[1;23r\x1b8\x1b[1A', '\x1b]0;title ' + begin + '\x07text', '\x1bP' + begin + '\x1b\\text']) {
  const mapper = window.SerialdSerialProgress.create({rows: () => 42});
  assert.equal(Array.from(untouched, c => mapper.rewrite(c)).join(''), untouched);
}
let rows = 42;
const resize = window.SerialdSerialProgress.create({rows: () => rows});
resize.rewrite(begin);
rows = 55;
assert.equal(resize.rewrite('\x1b[24;0fbar'), '\x1b7\x1b[0;54r\x1b8\x1b[55;0fbar');
resize.rewrite(end);
assert.equal(resize.rewrite('\x1b[24;0fplain'), '\x1b[24;0fplain');
console.log('Serial progress stream tests passed');
