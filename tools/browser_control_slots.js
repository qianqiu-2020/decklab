'use strict';
// Exercise the production editor with two four-way controllers and no hardware.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {launchBrowser, stopBrowser} = require('./browser_launch');
const source = fs.readFileSync(path.join(__dirname, '../static/control-slots.js'));
const origin = 'https://fixture.invalid';
const groups = ['North', 'South'];
const boards = [{serial:'A', label:'Alpha', uart:'COM7'}, {serial:'B', label:'Beta', uart:'COM9'},
  {serial:'C', label:'Replacement', uart:'COM7'}];
const bindings = new Map();
const saved = [];
const activated = [];
let handoff;
const server = http.createServer(async (req, res) => {
  if (req.url === '/static/tasks.js') {
    res.setHeader('Content-Type','text/javascript; charset=utf-8');
    return res.end(fs.readFileSync(path.join(__dirname, '../static/tasks.js')));
  }
  if (req.url === '/static/control-slots.js') {
    res.setHeader('Content-Type','text/javascript; charset=utf-8');
    return res.end(source);
  }
  if (req.url !== '/api/control-slots') {
    res.setHeader('Content-Type','text/html; charset=utf-8');
    return res.end('<meta charset="utf-8"><script>window.DeckLabUI={fetch:window.fetch.bind(window)};window.confirmCalls=0;window.confirm=()=>{window.confirmCalls++;return true}</script>' +
      '<script src="/static/tasks.js"></script><script src="/static/control-slots.js"></script><button onclick="DeckLabControlSlots.open()">Bulk</button>');
  }
  let body = '';
  for await (const chunk of req) body += chunk;
  const op = JSON.parse(body);
  const slots = groups.flatMap(name => (name === 'North' ? [0,1,2,3] : [1,2,3,4]).map(number => ({
    key:name+'/'+number, name, area:'Lab', entities:{reset:'reset','enter-download':'download'}, present:true,
    serial:bindings.get(name+'/'+number)?.serial || '', allow_agent:false,
    occupied_online:name==='South' && number===1 && !!bindings.get('South/1'),
    suspended:handoff?.key===name+'/'+number
  })));
  const data = op.action==='list' ? {revision:'v1',connections:[origin],boards,bindings:[],handoff:op.serial ? handoff : undefined} :
    op.action==='discover' ? {slots,scan:'scan'} : op.action==='identify-slot'
      ? {task:{id:'fixture-identify',state:'completed',result:{serial:'NEW-BOARD'}}} : {ok:true};
  if (op.action==='identify-slot') activated.push(op);
  if (op.action==='save') {
    saved.push(op.changes);
    for (const [key, value] of Object.entries(op.changes)) bindings.set(key, value);
  }
  res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(data));
});
async function test() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),'decklab-wiring-'));
  let chrome, ws;
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const launched = await launchBrowser(profile);
    chrome = launched.chrome;
    const url = 'http://127.0.0.1:'+server.address().port;
    const tab = await (await fetch('http://127.0.0.1:'+launched.port+'/json/new?'+url,{method:'PUT'})).json();
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{ws.onopen=resolve; ws.onerror=reject;});
    let id = 0;
    const pending = new Map();
    ws.onmessage = event => {
      const msg = JSON.parse(event.data);
      if (pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    };
    async function evaluate(expression) {
      const seq = ++id;
      const reply = new Promise(resolve=>pending.set(seq,resolve));
      ws.send(JSON.stringify({id:seq,method:'Runtime.evaluate',params:{expression,returnByValue:true,awaitPromise:true}}));
      const msg = await reply;
      assert.ok(!msg.error && !msg.result.exceptionDetails, expression);
      return msg.result.result.value;
    }
    async function wait(expression) {
      for(let i=0;i<100;i++) {
        if(await evaluate(expression)) return;
        await new Promise(resolve=>setTimeout(resolve,25));
      }
      throw Error('Timed out waiting for '+expression);
    }
    await wait('typeof DeckLabControlSlots==="object"');
    await evaluate('DeckLabControlSlots.open()');
    const rows = 'document.querySelectorAll(".wiring-row")';
    await wait(rows+'.length===4');
    assert.equal(await evaluate(rows+'.length'),4, await evaluate('document.querySelector(".wiring-footer").textContent'));
    assert.deepEqual(await evaluate('Array.from('+rows+', row => row.querySelector("strong").textContent)'),
                     ['槽位 0','槽位 1','槽位 2','槽位 3']);
    async function choose(selector,value) {
      await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event("change"));})()`);
    }
    await choose('[aria-label="槽位 0 的板卡"]','A');
    assert.equal(await evaluate(rows+'[0].querySelector("input[type=checkbox]").checked'),true);
    await choose('[aria-label="控制器"]','South');
    assert.deepEqual(await evaluate('Array.from('+rows+', row => row.querySelector("strong").textContent)'),
                     ['槽位 1','槽位 2','槽位 3','槽位 4']);
    await evaluate('const f=document.querySelector("input[type=search]"); f.value="COM9";f.oninput()');
    assert.equal(await evaluate('document.querySelector("[aria-label=\\"槽位 1 的板卡\\"]").options.length'),2);
    await choose('[aria-label="槽位 1 的板卡"]','B');
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[0],{'North/0':{serial:'A',allow_agent:true},'South/1':{serial:'B',allow_agent:true}});
    await evaluate('DeckLabControlSlots.open("A")');
    await wait(rows+'.length===4');
    await choose('[aria-label="控制器"]','South');
    await evaluate(rows+'[1].querySelector("button").click()');
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[1],{'North/0':{serial:'',allow_agent:false},'South/2':{serial:'A',allow_agent:true}});
    handoff = {key:'South/2', origin};
    const confirmations = await evaluate('window.confirmCalls');
    await evaluate('DeckLabControlSlots.open("C")');
    await wait('!!document.querySelector(".wiring-handoff")');
    assert.match(await evaluate('document.querySelector(".wiring-handoff").textContent'),/已选槽位 2.*A → C/);
    assert.equal(await evaluate('document.querySelector(".wiring-footer button").textContent'),'确认换板接线');
    assert.equal(await evaluate('document.querySelector(".wiring-row[data-current] select").value'),'C');
    assert.equal(saved.length,2);
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[2],{'South/2':{serial:'C',allow_agent:true}});
    assert.equal(await evaluate('window.confirmCalls'),confirmations);
    handoff = undefined;
    bindings.set('North/1',{serial:'A'});
    await evaluate('DeckLabControlSlots.open(null,{key:"pending-uart",connection_id:"fixture-connection"})');
    await wait(rows+'.length===4');
    assert.equal(await evaluate('document.querySelector(".wiring-footer button").disabled'),true);
    await choose('[aria-label="控制器"]','South');
    assert.match(await evaluate(rows+'[0].textContent'),/原板卡在线/);
    assert.equal(await evaluate('Boolean('+rows+'[0].querySelector("button"))'),false);
    await choose('[aria-label="控制器"]','North');
    assert.equal(await evaluate(rows+'[1].querySelector("button").disabled'),false);
    await evaluate(rows+'[1].querySelector("button").click()');
    await wait('document.querySelector(".wiring-footer").textContent.includes("NEW-BOARD")');
    assert.equal(activated.length,1);
    assert.equal(activated[0].slot,'North/1');
    assert.equal(activated[0].connection_id,'fixture-connection');
    assert.equal(saved.length,3);
    const allowAgent = rows+'[1].querySelector("input[type=checkbox]")';
    assert.equal(await evaluate(allowAgent+'.checked'),true);
    await evaluate(allowAgent+'.click()');
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[3],{'North/1':{serial:'NEW-BOARD',allow_agent:false}});
    console.log('Chromium passed slot numbering, board moves, and replacement identification with explicit save');
  } finally {
    if(ws) ws.close();
    await stopBrowser(chrome);
    server.close(); fs.rmSync(profile,{recursive:true,force:true,maxRetries:5});
  }
}
test().catch(error=>{console.error(error);process.exitCode=1;});
