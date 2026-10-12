'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static/i18n.js'), 'utf8');

function fixture(saved, browser = 'en-US') {
  let observe;
  function element(id, ignored = false) {
    return {nodeType: 1, childNodes: [], attributes: {}, value: '',
      closest() { return ignored ? this : null; },
      getAttribute(name) { return this.attributes[name] ?? null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener() {}};
  }
  const root = element('html');
  const select = element('language-select', true);
  const button = element('button');
  const terminal = element('terminal', true);
  const command = element('command', true);
  const label = element('label', true);
  const content = element('bc-content', true);
  function text(parent, value) {
    const node = {nodeType: 3, nodeValue: value, parentElement: parent};
    parent.childNodes.push(node); return node;
  }
  const heading = text(button, '占用输入');
  const output = text(terminal, '启动 在线 设备');
  const commandText = text(command, 'echo 在线');
  const labelText = text(label, '设备');
  const diffText = text(content, '启动 timeout');
  button.attributes.title = '可选占用串口输入，其他人仍可查看输出';
  root.childNodes.push(select, button, terminal, command, label, content);
  const storage = new Map(saved ? [['decklab-language', saved]] : []);
  const sandbox = {navigator: {language: browser},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    document: {documentElement: root, querySelectorAll: () => [select]},
    CustomEvent: class {}, dispatchEvent() {},
    MutationObserver: class {constructor(fn) { observe = fn; } observe() {}}};
  sandbox.window = sandbox;
  vm.runInNewContext(source, sandbox);
  return {api: sandbox.SerialdI18n, heading, button, output, commandText, labelText, diffText,
    root, select, storage, mutate: node => observe([{type: 'characterData', target: node}])};
}

const h = fixture(null, 'en-US');
assert.equal(h.heading.nodeValue, 'Reserve input');
assert.equal(h.root.lang, 'en');
assert.equal(h.select.value, 'en');
assert.equal(h.button.attributes.title, 'Reserve serial input; others can still view output');
h.api.setDefaultLocale('zh-CN');
assert.equal(h.heading.nodeValue, '占用输入');
assert.equal(h.storage.has('decklab-language'), false, 'host defaults are not personal preferences');
h.api.setLocale('en');
h.api.setDefaultLocale('zh-CN');
assert.equal(h.heading.nodeValue, 'Reserve input', 'explicit browser preference wins');
h.heading.nodeValue = '释放输入';
h.mutate(h.heading);
assert.equal(h.heading.nodeValue, 'Release input', 'dynamic ownership text is translated');
h.api.setLocale('zh-CN');
assert.equal(h.heading.nodeValue, '释放输入', 'reverse switch restores the source string');
assert.equal(h.output.nodeValue, '启动 在线 设备');
assert.equal(h.commandText.nodeValue, 'echo 在线');
assert.equal(h.labelText.nodeValue, '设备');
assert.equal(h.diffText.nodeValue, '启动 timeout');
assert.equal(fixture('en', 'zh-CN').api.locale, 'en');
assert.equal(fixture('invalid', 'en-US').api.locale, 'en');
assert.equal(h.api.translate('未知文本'), '未知文本');
console.log('ok - UI language defaults, live switching, persistence and raw-content isolation');
