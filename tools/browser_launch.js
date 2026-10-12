'use strict';
// Shared browser startup for isolated UI fixtures; retain bounded diagnostics.
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

async function launchBrowser(profile) {
  const chrome = spawn(process.env.CHROME_BIN || '/usr/bin/google-chrome', [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
    '--disable-gpu', '--remote-debugging-port=0', '--user-data-dir=' + profile,
    'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
  let diagnostics = '', launchError;
  chrome.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-8192); });
  chrome.on('error', error => { launchError = error; });
  const deadline = Date.now() + 40000;
  try {
    while (Date.now() < deadline) {
      if (launchError) throw launchError;
      if (chrome.exitCode !== null || chrome.signalCode !== null) {
        throw new Error('Chromium exited before its debugging endpoint was ready');
      }
      try {
        const port = fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
        if (/^[0-9]+$/.test(port)) return {chrome, port};
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Chromium debugging endpoint was not ready within 40 seconds');
  } catch (error) {
    chrome.kill('SIGTERM');
    throw new Error(error.message + (diagnostics ? '\nChromium stderr:\n' + diagnostics : ''));
  }
}

module.exports = {launchBrowser};
