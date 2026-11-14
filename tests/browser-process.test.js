'use strict';
// A real child saves its profile after SIGTERM; cleanup must wait for its exit.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {stopBrowser} = require('../tools/browser_launch');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'decklab-browser-exit-test-'));
const saved = path.join(profile, 'saved');
const child = spawn(process.execPath, ['-e', `
  const fs = require('node:fs');
  process.on('SIGTERM', () => setTimeout(() => {
    fs.writeFileSync(process.argv[1], 'saved before exit');
    process.exit(0);
  }, 80));
  setInterval(() => {}, 1000);
  console.log('ready');
`, saved], {stdio: ['ignore', 'pipe', 'inherit']});
(async () => {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('fixture did not start')), 5000);
    child.once('error', error => {clearTimeout(timeout); reject(error);});
    child.stdout.once('data', () => {clearTimeout(timeout); resolve();});
  });
  await stopBrowser(child);
  assert.equal(child.exitCode, 0);
  assert.equal(fs.readFileSync(saved, 'utf8'), 'saved before exit');
  await stopBrowser(child);
  await stopBrowser(null);
  console.log('ok - browser shutdown waits for the actual exit before profile cleanup');
})().catch(error => {console.error(error); process.exitCode = 1;}).finally(async () => {
  await stopBrowser(child);
  fs.rmSync(profile, {recursive: true, force: true});
});
