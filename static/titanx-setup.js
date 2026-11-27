(function (global) {
  'use strict';
  const {byId: $, fetch} = global.DeckLabUI;
  const dialog = $('titanx-setup'), form = $('titanx-form'), fields = form.elements;
  const all = $('titanx-all'), labs = $('titanx-labs'), message = $('titanx-message');
  let rows = [], generation = 0, labRequest = 0, saving = false;

  async function request(action, values) {
    const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({action: 'titanx-' + action}, values))});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '工位请求失败');
    return data;
  }
  function refreshSlots() {
    let count = 0;
    for (const row of rows) {
      row.project.disabled = !row.check.checked;
      row.slot.textContent = row.check.checked ? '槽位 ' + (++count) + ' · COM ' + (Number(fields.first_com.value) + count - 1) : '';
    }
    all.checked = count > 0 && count === rows.length;
    all.indeterminate = count > 0 && count < rows.length;
    $('titanx-submit').disabled = !count || !fields.lab_id.value;
  }
  function display(data) {
    const readback = $('titanx-readback');
    readback.hidden = !data.configured;
    form.hidden = data.configured;
    if (data.configured) {
      form.reset();
      const worker = {active:'运行中', activating:'启动中', inactive:'未启动', failed:'启动失败'};
      const location = data.lab_id ? ' · 实验室 ' + data.lab_id : '';
      $('titanx-summary').textContent = data.node_id + location + ' · 本机执行器' + (worker[data.worker_state] || '状态未知')
        + (data.platform_state ? ' · TitanX ' + data.platform_state : '');
      const list = $('titanx-registered'); list.replaceChildren();
      for (const [slot, serial] of Object.entries(data.slots)) {
        const item = document.createElement('li');
        const state = !data.registered ? '待回查' : data.registered.includes(serial) ? '已登记' : '待登记';
        item.textContent = serial + ' · 槽位 ' + slot + ' · COM ' + (data.first_com + Number(slot) - 1) + ' · ' + state;
        list.append(item);
      }
      message.textContent = data.platform_error || '';
      return;
    }
    fields.node_id.value = data.node_id;
    if (global.location.protocol === 'https:') fields.web_origin.value = global.location.origin;
    const container = $('titanx-boards'); container.replaceChildren();
    rows = data.boards.map(board => {
      const line = document.createElement('div'); line.className = 'board-tests-actions';
      const label = document.createElement('label'), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = true;
      label.append(check, ' ' + board.label + (board.label === board.serial ? '' : ' · ' + board.serial) + (board.online ? '（在线）' : '（离线）'));
      const slot = document.createElement('span'), project = document.createElement('input');
      project.placeholder = '沿用默认项目'; project.maxLength = 80; project.setAttribute('aria-label', board.serial + ' 的项目类型');
      line.append(label, slot, project); container.append(line); check.onchange = refreshSlots;
      return {serial:board.serial, check, slot, project};
    });
    refreshSlots();
    message.textContent = rows.length ? '' : '请先接入板卡并在 DeckLab 确认硬件身份。';
  }
  async function load() {
    const gen = ++generation;
    message.textContent = '正在读取工位…';
    try {
      const data = await request('status');
      if (dialog.open && gen === generation) display(data);
    } catch (error) { if (gen === generation) message.textContent = error.message; }
  }
  function open() {
    if (dialog.open) return;
    form.reset(); rows = []; resetLabs(); form.hidden = true; $('titanx-readback').hidden = true;
    dialog.showModal(); load();
  }
  function resetLabs() {
    ++labRequest;
    fields.lab_id.replaceChildren(new Option('请先读取实验室', ''));
    labs.disabled = false; refreshSlots();
  }
  fields.api_url.oninput = fields.api_token.oninput = resetLabs;
  fields.lab_id.onchange = fields.first_com.oninput = refreshSlots;
  all.onchange = () => { for (const row of rows) row.check.checked = all.checked; refreshSlots(); };
  labs.onclick = async () => {
    const id = ++labRequest, gen = generation;
    labs.disabled = true; message.textContent = '正在读取实验室…';
    try {
      const data = await request('labs', {api_url: fields.api_url.value.trim(), api_token: fields.api_token.value});
      if (id !== labRequest || gen !== generation) return;
      fields.lab_id.replaceChildren(new Option('选择实验室', ''));
      for (const lab of data.labs) fields.lab_id.add(new Option(lab.name, lab.id));
      if (data.labs.length === 1) fields.lab_id.selectedIndex = 1;
      refreshSlots(); message.textContent = data.labs.length ? '' : '该账号没有可访问的实验室。';
    } catch (error) { if (id === labRequest && gen === generation) message.textContent = error.message; }
    finally { if (id === labRequest) labs.disabled = false; }
  };
  form.onsubmit = async event => {
    event.preventDefault(); if (saving) return;
    const gen = generation;
    const config = {slots:{}, projects:{}, first_com:Number(fields.first_com.value), lab_id:Number(fields.lab_id.value),
      accept_dispatch:fields.accept_dispatch.checked};
    for (const key of ['api_url','api_token','node_id','broker','web_origin','username','password','project']) {
      const value = ['api_token','password'].includes(key) ? fields[key].value : fields[key].value.trim();
      if (value) config[key] = value;
    }
    rows.filter(row => row.check.checked).forEach((row, i) => {
      config.slots[i+1] = row.serial;
      if (row.project.value.trim()) config.projects[i+1] = row.project.value.trim();
    });
    saving = true; form.inert = true; message.textContent = '正在初始化工位…';
    try {
      const data = await request('initialize', {configuration:config});
      if (gen === generation) { display(data); message.textContent = '已保存。执行器启动后会逐板登记，可刷新查看结果。'; }
    } catch (error) { if (gen === generation) message.textContent = error.message; }
    finally { saving = false; form.inert = false; }
  };
  $('titanx-setup-open').onclick = open;
  $('titanx-refresh').onclick = load;
  $('titanx-close').onclick = () => { if (!saving) dialog.close(); };
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => { ++generation; ++labRequest; form.reset(); });
  global.DeckLabTitanX = {open};
})(window);
