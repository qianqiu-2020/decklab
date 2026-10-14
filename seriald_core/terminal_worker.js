'use strict';
// One isolated parser process, many terminal instances. No network or UART IO.
global.self = global;
const path = require('path');
const {Terminal} = require('../vendor/terminal/xterm-headless.js');
const {SerializeAddon} = require('../vendor/terminal/addon-serialize.js');
const {serialize} = require('./terminal_snapshot');
global.window = {};
require('../static/serial-progress.js');
const terminals = new Map();

// A screen snapshot cannot serialize an unfinished control string or UTF-8
// character. Reapply only that pending prefix before the subsequent bytes.
function carryAfter(previous, bytes) {
  const b = Buffer.concat([previous, bytes]);
  let state = 0, start = b.length, utf8 = 0, escaped = false;
  for (let i = 0; i < b.length; i++) {
    const c = b[i];
    if (utf8) { if ((c & 0xc0) === 0x80) { if (!--utf8) start = b.length; continue; } utf8 = 0; }
    if (c === 0x18 || c === 0x1a) { state = 0; start = b.length; escaped = false; continue; }
    if (state === 3) {
      if (c === 7 || escaped && c === 92) { state = 0; start = b.length; escaped = false; }
      else if (escaped && c !== 27) {
        start = i - 1; escaped = false;
        if (c === 91) state = 2;
        else if ([93, 80, 94, 95, 88].includes(c)) state = 3;
        else if (c >= 0x30 && c <= 0x7e) { state = 0; start = b.length; }
        else state = 1;
      }
      else escaped = c === 27;
      continue;
    }
    if (c === 27) { state = 1; start = i; continue; }
    if (state === 1) {
      if (c === 91) state = 2;
      else if ([93, 80, 94, 95, 88].includes(c)) { state = 3; escaped = false; }
      else if (c >= 0x30 && c <= 0x7e) { state = 0; start = b.length; }
      continue;
    }
    if (state === 2) { if (c >= 0x40 && c <= 0x7e) { state = 0; start = b.length; } continue; }
    if (c >= 0xc2 && c <= 0xf4) { utf8 = c < 0xe0 ? 1 : c < 0xf0 ? 2 : 3; start = i; }
  }
  return b.subarray(start);
}

function info(s) {
  const b = s.term.buffer.active;
  return {geometry: {cols: s.term.cols, rows: s.term.rows},
    phase: b.type === 'alternate' || s.progress && s.progress.active() ? 'application' : s.phase,
    revision: s.revision, off: s.off, modes: s.term.modes,
    cursor: {x: b.cursorX, y: b.cursorY, buffer: b.type}};
}
function resize(s, cols, rows) {
  if (cols >= 20 && cols <= 500 && rows >= 2 && rows <= 200
      && (cols !== s.term.cols || rows !== s.term.rows)) {
    s.term.resize(cols, rows); s.revision++;
  }
}
function create(m) {
  const term = new Terminal({cols: m.cols, rows: m.rows, scrollback: 8000,
    allowProposedApi: true, windowOptions: {setWinSizeChars: true, getWinSizeChars: true}});
  const serializer = new SerializeAddon(); term.loadAddon(serializer);
  const s = {term, serializer, kind: m.kind, phase: m.kind === 'serial' ? 'text' : 'shell',
    revision: 1 + require('crypto').randomBytes(4).readUInt32BE(), off: m.off || 0,
    replies: [], live: false, scan: '', carry: Buffer.alloc(0)};
  term.onData(data => { if (s.live && data.length <= 8192) s.replies.push(data); });
  term.parser.registerCsiHandler({final: 't'}, p => {
    if (p[0] === 18) {
      if (s.live) s.replies.push('\x1b[8;' + term.rows + ';' + term.cols + 't');
      return true;
    }
    if (p[0] !== 8 || p.length !== 3) return false;
    resize(s, p[2], p[1]); s.phase = 'shell'; return true;
  });
  term.parser.registerDcsHandler({intermediates: '+', final: 'q'}, data => {
    if (s.live) {
      const capabilities = {TN: 'xterm-256color', Co: '256'};
      for (const hex of data.split(';')) {
        if (!/^(?:[0-9a-f]{2}){1,64}$/i.test(hex)) continue;
        const value = capabilities[Buffer.from(hex, 'hex').toString('ascii')];
        s.replies.push('\x1bP' + (value ? '1+r' + hex + '=' + Buffer.from(value).toString('hex') : '0+r') + '\x1b\\');
      }
    }
    return true;
  });
  for (const id of [10, 11, 12]) term.parser.registerOscHandler(id, data => {
    if (data !== '?') return false;
    if (s.live) {
      const colors = {10: 'dcdc/e5e5/f0f0', 11: '0f0f/1515/1d1d', 12: '8282/c5c5/ffff'};
      s.replies.push('\x1b]' + id + ';rgb:' + colors[id] + '\x1b\\');
    }
    return true;
  });
  if (m.kind === 'serial') {
    s.progress = window.SerialdSerialProgress.create({canonical: true, sourceRows: () => term.rows});
    term.parser.registerCsiHandler({prefix: '>', final: 'z'}, p => { resize(s, term.cols, p[0]); return true; });
    term.parser.registerCsiHandler({final: 'J'}, p => {
      if (!s.restoring && p[0] === 2 && !s.linux && s.phase !== 'shell') {
        s.phase = 'firmware'; resize(s, 80, 24);
      }
      return false;
    });
    for (const final of ['H', 'f']) term.parser.registerCsiHandler({final}, p => {
      if (s.restoring) return false;
      if (s.phase === 'firmware' || s.phase === 'text' && !s.linux) {
        const row = p[0] || 1;
        if (row <= 200) { s.phase = 'firmware'; resize(s, 80, Math.max(term.rows, row)); }
      }
      return false;
    });
  }
  terminals.set(m.sid, s); return s;
}
async function handle(m) {
  const s = m.action === 'create' ? create(m) : terminals.get(m.sid);
  if (!s) throw new Error('terminal does not exist');
  let bytesForRender;
  if (m.action === 'feed') {
    let bytes = Buffer.from(m.data, 'base64');
    s.live = m.live === true; s.replies = [];
    if (s.kind === 'serial') {
      const joined = s.scan + bytes.toString('latin1');
      let cut = -1, linux = s.linux;
      const pattern = /(?:\x00sys:\s*0x[0-9a-f]+|(?:^|[\r\n])U-Boot(?: SPL)? \d{4}\.\d{2})|(?:^|[\r\n])(?:\[[^\r\n]{1,40}\]\s*)?Linux version \d|(?:^|[\r\n])\[\s*[0-9]+\.[0-9]{6}\] /ig;
      let match;
      while ((match = pattern.exec(joined))) {
        if (match.index + match[0].length <= s.scan.length) continue;
        if (/Linux version|\[\s*[0-9]+\.[0-9]{6}\]/.test(match[0])) { if (linux) continue; linux = true; }
        else linux = false;
        cut = match.index;
      }
      if (cut >= 0) {
        s.term.reset(); s.phase = 'text'; s.linux = linux;
        resize(s, 80, 24);
        bytes = Buffer.from(joined.slice(cut), 'latin1');
        s.revision++; s.carry = Buffer.alloc(0);
        s.progress = window.SerialdSerialProgress.create({canonical: true, sourceRows: () => s.term.rows});
      }
      s.scan = joined.slice(-128);
      bytes = Buffer.from(s.progress.rewrite(bytes.toString('latin1')), 'latin1');
    }
    s.carry = carryAfter(s.carry, bytes);
    if (s.carry.length > 1024 * 1024) throw new Error('unfinished terminal control exceeds 1 MiB');
    bytesForRender = bytes;
    await new Promise(resolve => s.term.write(bytes, resolve));
    s.live = false; s.off = m.off;
  } else if (m.action === 'restore') {
    s.live = false;
    const bytes = Buffer.concat([Buffer.from(m.snapshot.data, 'base64'), Buffer.from(m.snapshot.pending || '', 'base64')]);
    s.restoring = true;
    await new Promise(resolve => s.term.write(bytes, resolve));
    s.restoring = false;
    s.off = m.snapshot.off;
    const adapter = m.snapshot.adapter_state || {};
    s.phase = adapter.phase || (s.kind === 'serial' ? 'text' : 'shell');
    s.linux = !!adapter.linux; s.scan = adapter.scan || '';
    if (s.progress && adapter.progress) s.progress.restore(adapter.progress);
    s.carry = Buffer.from(m.snapshot.pending || '', 'base64');
  } else if (m.action === 'resize') resize(s, m.cols, m.rows);
  else if (m.action === 'dispose') {
    const result = info(s); s.term.dispose(); terminals.delete(m.sid); return result;
  }
  const result = info(s);
  if (m.action === 'feed') {
    result.replies = s.replies;
    result.render_data = bytesForRender.toString('base64');
  }
  if (m.action === 'snapshot') {
    result.data = Buffer.from(serialize(s.term, s.serializer, {scrollback: 8000}), 'utf8').toString('base64');
    result.pending = s.carry.toString('base64');
    result.adapter_state = {phase: s.phase, linux: s.linux, scan: s.scan,
      progress: s.progress ? s.progress.snapshot() : null};
  }
  return result;
}
let queue = Promise.resolve();
require('readline').createInterface({input: process.stdin, crlfDelay: Infinity}).on('line', line => {
  queue = queue.then(async () => {
    let m;
    try { m = JSON.parse(line); process.stdout.write(JSON.stringify({id: m.id, result: await handle(m)}) + '\n'); }
    catch (error) { process.stdout.write(JSON.stringify({id: m && m.id, error: String(error.message)}) + '\n'); }
  });
}).on('close', () => queue.then(() => {
  for (const s of terminals.values()) s.term.dispose();
  terminals.clear();
}));
