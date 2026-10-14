'use strict';
// Real Vim, local PTYs, two real browser tabs; never connects to a board.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {launchBrowser} = require('./browser_launch');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-shared-browser-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let fixture, chrome, geometryFile, diagnostics = '', sockets = [];
async function wait(check, label) {
  for (let n = 0; n < 120; n++) { if (await check()) return; await delay(50); }
  throw new Error('Timed out: ' + label + '\n' + diagnostics.slice(-3000));
}
async function tab(port, url, width, height) {
  const target = await (await fetch('http://127.0.0.1:' + port + '/json/new?' + encodeURIComponent(url), {method:'PUT'})).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl); sockets.push(socket);
  await new Promise((resolve, reject) => {socket.onopen = resolve; socket.onerror = reject;});
  let sequence = 0; const calls = new Map();
  socket.onmessage = event => {const m=JSON.parse(event.data);if(m.id){const c=calls.get(m.id);calls.delete(m.id);m.error?c.reject(new Error(JSON.stringify(m.error))):c.resolve(m.result);}};
  const rpc = (method, params={}) => new Promise((resolve,reject)=>{const id=++sequence;calls.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
  const evaluate = async expression => {
    const r=await rpc('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;
  };
  await rpc('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  await wait(()=>evaluate('typeof Terminal === "function" && document.querySelectorAll(".dev").length===1'),'fixture UI ready');
  await evaluate('window.OriginalTerminal=Terminal;window.Terminal=class extends OriginalTerminal{constructor(...args){super(...args);window.testTerm=this;}};document.querySelector(".dev").click()');
  return {rpc,evaluate};
}
function input(t, text) { return t.evaluate('testTerm.input(' + JSON.stringify(text) + ')'); }
const screen = `(() => {const b=testTerm.buffer.active;return {cols:testTerm.cols,rows:testTerm.rows,x:b.cursorX,y:b.cursorY,type:b.type,lines:Array.from({length:testTerm.rows},(_,i)=>b.getLine(b.baseY+i)?.translateToString()||'')};})()`;
(async () => {
  fixture=spawn(process.env.PYTHON||'python3',[path.join(__dirname,'shared_terminal_fixture.py')],{stdio:['ignore','pipe','pipe']});
  fixture.stderr.on('data',data=>diagnostics=(diagnostics+data).slice(-8000));
  const url=await new Promise((resolve,reject)=>{let output='';fixture.stdout.on('data',data=>{output+=data;const line=output.split('\n')[0];try{const info=JSON.parse(line);geometryFile=info.geometry_file;resolve(info.url);}catch{}});fixture.once('exit',code=>reject(new Error('fixture exited '+code+'\n'+diagnostics)));});
  const launched=await launchBrowser(profile);chrome=launched.chrome;
  const a=await tab(launched.port,url,1500,900);
  await wait(()=>a.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).some(b=>b.dataset.kind==="ssh")'),'SSH tab');
  await a.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(b=>b.dataset.kind==="ssh").click()');
  await wait(()=>a.evaluate('testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+testTerm.buffer.active.cursorY)?.translateToString().includes("root@fixture")'),'SSH root prompt');
  await delay(500);
  await input(a, 'vim -Nu NONE -n -c \'autocmd VimResized * call writefile([&columns."x".&lines],"vim-geometry.txt")\' sample.txt\r');
  await wait(()=>a.evaluate('testTerm.buffer.active.type==="alternate"'),'SSH Vim alternate screen');
  const b=await tab(launched.port,url,950,620);
  await wait(()=>b.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).some(b=>b.dataset.kind==="ssh")'),'observer SSH tab');
  await b.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(b=>b.dataset.kind==="ssh").click()');
  await wait(()=>b.evaluate('testTerm.buffer.active.type==="alternate"'),'observer joins running Vim');
  await delay(200);
  assert.deepEqual(await b.evaluate(screen),await a.evaluate(screen),'late observer sees the canonical Vim screen');
  assert.equal(await b.evaluate('document.querySelector("#d-in").disabled'),true,'observer cannot resize or write');
  await delay(300);
  await input(a, 'iSHARED-EDIT\x1b');
  await wait(()=>b.evaluate('Array.from({length:testTerm.rows},(_,i)=>testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+i)?.translateToString()||"").some(s=>s.includes("SHARED-EDIT"))'),'live shared edit');
  await input(a, 'i');
  await delay(100);
  await a.evaluate('testTerm.paste("PASTE汉字\\nsecond-line")');
  await wait(()=>b.evaluate('Array.from({length:testTerm.rows},(_,i)=>testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+i)?.translateToString()||"").some(s=>s.includes("PASTE汉字"))'),'UTF-8 multiline paste');
  await input(a, '\x1b');
  await wait(async()=>JSON.stringify(await a.evaluate(screen))===JSON.stringify(await b.evaluate(screen)), 'live screens converge after escape');
  assert.deepEqual(await b.evaluate(screen),await a.evaluate(screen),'incremental output has the same cursor and modes');
  await b.evaluate('document.querySelector("#b-redraw").click()');
  await wait(()=>b.evaluate('testTerm.buffer.active.type==="alternate"'),'observer redraw restores Vim');
  await wait(async()=>JSON.stringify(await a.evaluate(screen))===JSON.stringify(await b.evaluate(screen)), 'snapshot screen converges');
  assert.deepEqual(await b.evaluate(screen),await a.evaluate(screen),'redraw restores the same alternate screen without writing to the source');
  const previousCols=await b.evaluate('testTerm.cols');
  await b.evaluate('document.querySelector("#b-takeover").click()');
  await wait(()=>a.evaluate('document.querySelector("#d-in").disabled'),'old controller becomes read-only');
  await wait(()=>b.evaluate('!document.querySelector("#d-in").disabled'),'new controller input');
  await wait(()=>b.evaluate('testTerm.cols<'+previousCols),'new controller drives the real PTY width');
  try {
    await wait(async()=>fs.existsSync(geometryFile)&&fs.readFileSync(geometryFile,'utf8').trim()===await b.evaluate('testTerm.cols+"x"+testTerm.rows'),'Vim receives the actual SSH PTY resize event');
  } catch (error) {
    console.log('Vim size marker',fs.existsSync(geometryFile)?fs.readFileSync(geometryFile,'utf8'):'missing');
    console.log('Source screen',await b.evaluate(screen)); throw error;
  }
  await delay(500);
  assert.deepEqual(await b.evaluate(screen),await a.evaluate(screen),'PTY resize redraw reaches both viewers');
  await input(b, '\x1b:q!\r');
  await wait(()=>a.evaluate('testTerm.buffer.active.type==="normal"'),'Vim exit restores the normal screen');
  await a.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(b=>b.dataset.kind==="serial").click()');
  await wait(()=>a.evaluate('testTerm.buffer.active.getLine(testTerm.buffer.active.baseY+testTerm.buffer.active.cursorY)?.translateToString().includes("root@fixture")'),'serial root prompt');
  await input(a, 'vim -Nu NONE -n sample.txt\r');
  await wait(()=>a.evaluate('testTerm.buffer.active.type==="alternate"'),'serial Vim');
  await b.evaluate('Array.from(document.querySelectorAll("#d-tabs button")).find(b=>b.dataset.kind==="serial").click()');
  await wait(()=>b.evaluate('testTerm.buffer.active.type==="alternate"'),'serial observer snapshot');
  await delay(200);
  assert.deepEqual(await b.evaluate(screen),await a.evaluate(screen),'serial Vim has the same grid and screen for both viewers');
  await input(a, '\x1b:q!\r');
  await wait(()=>a.evaluate('testTerm.buffer.active.type==="normal"'),'serial Vim exit');
  console.log('Shared terminal smoke passed: real Vim over serial/SSH PTYs, late join, live edits, takeover, resize and exit.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  for(const socket of sockets)socket.close();
  if(chrome){chrome.kill('SIGTERM');await delay(500);}if(fixture){fixture.kill('SIGTERM');await delay(700);}
  fs.rmSync(profile,{recursive:true,force:true});
});
