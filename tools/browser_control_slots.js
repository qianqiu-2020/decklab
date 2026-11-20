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
const boards = [{serial:'A', label:'Alpha', uart:'COM7'}, {serial:'B', label:'Beta', uart:'COM9'}];
const bindings = new Map();
const saved = [];
const server = http.createServer(async (req, res) => {
  if (req.url === '/static/control-slots.js') {
    res.setHeader('Content-Type','text/javascript; charset=utf-8');
    return res.end(source);
  }
  if (req.url !== '/api/control-slots') {
    res.setHeader('Content-Type','text/html; charset=utf-8');
    return res.end('<meta charset="utf-8"><script>window.SerialdUI={fetch:window.fetch.bind(window)};window.confirm=()=>true</script>' +
      '<script src="/static/control-slots.js"></script><button onclick="SerialdControlSlots.open()">Bulk</button>');
  }
  let body = '';
  for await (const chunk of req) body += chunk;
  const op = JSON.parse(body);
  const slots = groups.flatMap(name => [1,2,3].map(number => ({
    key:name+'/'+number, name, area:'Lab', entities:{reset:'reset'}, present:true,
    serial:bindings.get(name+'/'+number)?.serial || '', allow_agent:false
  })));
  const data = op.action==='list' ? {revision:'v1',connections:[origin],boards,bindings:[]} :
    op.action==='discover' ? {slots,scan:'scan'} : {ok:true};
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
    await wait('typeof SerialdControlSlots==="object"');
    await evaluate('SerialdControlSlots.open()');
    const rows = 'document.querySelectorAll(".wiring-row")';
    assert.equal(await evaluate(rows+'.length'),4, await evaluate('document.querySelector(".wiring-footer").textContent'));
    assert.match(await evaluate(rows+'[3].textContent'),/未发现/);
    async function choose(selector,value) {
      await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event("change"));})()`);
    }
    await choose('[aria-label="槽位 1 的板卡"]','A');
    await choose('[aria-label="控制器"]','South');
    await evaluate('const f=document.querySelector("input[type=search]"); f.value="COM9";f.oninput()');
    assert.equal(await evaluate('document.querySelector("[aria-label=\\"槽位 1 的板卡\\"]").options.length'),2);
    await choose('[aria-label="槽位 1 的板卡"]','B');
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[0],{'North/1':{serial:'A',allow_agent:false},'South/1':{serial:'B',allow_agent:false}});
    await evaluate('SerialdControlSlots.open("A")');
    await choose('[aria-label="控制器"]','South');
    await evaluate(rows+'[1].querySelector("button").click()');
    await evaluate('document.querySelector(".wiring-footer button").click()');
    await wait('!document.querySelector(".wiring-dialog")');
    assert.deepEqual(saved[1],{'North/1':{serial:'',allow_agent:false},'South/2':{serial:'A',allow_agent:false}});
    console.log('Chromium passed bulk wiring, UART filter, missing fourth slot and single-board move');
  } finally {
    if(ws) ws.close();
    await stopBrowser(chrome);
    server.close(); fs.rmSync(profile,{recursive:true,force:true});
  }
}
test().catch(error=>{console.error(error);process.exitCode=1;});
