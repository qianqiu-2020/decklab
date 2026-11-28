(function (global) {
  'use strict';
  const {byId: $, fetch} = global.DeckLabUI;
  const dialog = $('titanx-setup'), form = $('titanx-form'), fields = form.elements;
  const all = $('titanx-all'), labs = $('titanx-labs'), message = $('titanx-message');
  const addForm = $('titanx-add-form'), addSerial = $('titanx-add-serial'), addProject = $('titanx-add-project');
  let station = null;
  let board = null, rows = [], generation = 0, labRequest = 0, saving = false;

  async function request(action, values) {
    const response = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({action: 'titanx-' + action}, values))});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '工位请求失败');
    return data;
  }
  function refreshSlots() {
    let count = 0;
    const firstCom = Number(fields.first_com.value);
    for (const row of rows) {
      row.project.disabled = !row.check.checked;
      if (row.check.checked) count++;
      const com = firstCom + count - 1;
      row.slot.textContent = !row.check.checked ? '' : !Number.isInteger(firstCom) || firstCom < 1 || com > 65535
        ? '请调整起始 COM' : '槽位 ' + count + ' · COM ' + com;
    }
    all.checked = count > 0 && count === rows.length;
    all.indeterminate = count > 0 && count < rows.length;
    $('titanx-submit').disabled = !count || !fields.lab_id.value || !Number.isInteger(firstCom) || firstCom < 1 || firstCom + count - 1 > 65535;
  }
  function selectAddition() {
    const selected = station.available_boards.find(item => item.serial === addSerial.value);
    addProject.value = selected ? selected.project || station.default_project || '' : '';
    previewAddition();
  }
  function previewAddition() {
    const com = $('titanx-add-com').valueAsNumber, slot = com - station.first_com + 1;
    const valid = addSerial.value && Number.isInteger(com) && slot >= station.next_slot && com <= 65535;
    $('titanx-add-preview').textContent = valid ? '槽位 ' + slot + ' · COM ' + com + ' · 服务器 100' : '';
    $('titanx-add-submit').disabled = !valid;
  }
  $('titanx-add-com').oninput = previewAddition;
  function displayAdditions(data) {
    station = data;
    const choices = (data.available_boards || []).filter(item => !board || item.serial === board.serial);
    addForm.hidden = !choices.length || !data.next_slot;
    const panel = $('titanx-add-panel');
    panel.hidden = addForm.hidden;
    if (addForm.hidden) return;
    if (board) panel.open = true;
    const com = $('titanx-add-com');
    com.min = com.value = station.first_com + station.next_slot - 1;
    addSerial.replaceChildren(new Option('选择要接入的板卡', ''));
    for (const item of choices) {
      addSerial.add(new Option(item.label + (item.label === item.serial ? '' : ' · ' + item.serial)
        + (item.online ? '（在线）' : '（离线）'), item.serial));
    }
    if (board || choices.length === 1) addSerial.value = choices[0].serial;
    selectAddition();
  }
  addSerial.onchange = selectAddition;
  async function apply(action, values, success) {
    if (saving) return;
    const gen = generation, readback = $('titanx-readback');
    message.dataset.error = 'false';
    saving = true; readback.inert = true;
    message.textContent = '正在核对工位并应用配置…';
    try {
      await request(action, {...values, revision:station.revision});
      if (gen === generation) {
        await load();
        if (dialog.open) message.textContent = [success, message.textContent].filter(Boolean).join(' ');
      }
    } catch (error) { if (gen === generation) showError(error.message); }
    finally { saving = false; readback.inert = false; }
  }
  addForm.onsubmit = event => {
    event.preventDefault();
    if (!addSerial.value) return;
    const com = $('titanx-add-com').valueAsNumber, slot = com - station.first_com + 1;
    return apply('add-board', {node_id:station.node_id, lab_id:station.lab_id, serial:addSerial.value,
      project:addProject.value.trim(), slot, com},
    '板卡配置已保存。执行器重新连接后自动登记，离线板将在上线后登记。');
  };
  const connection = $('titanx-connection');
  connection.onsubmit = event => {
    event.preventDefault();
    const changes = {}, fields = connection.elements;
    for (const name of ['broker','api_url','lab_id','username','project','accept_dispatch']) {
      const value = name === 'accept_dispatch' ? fields[name].checked : name === 'lab_id'
        ? Number(fields[name].value) : fields[name].value.trim();
      if (value !== (station.connection[name] ?? (name === 'accept_dispatch' ? false : ''))) changes[name] = value;
    }
    for (const name of ['password','api_token']) if (fields[name].value) changes[name] = fields[name].value;
    if (fields.clear_mqtt.checked) { changes.username = ''; changes.password = ''; }
    if (!Object.keys(changes).length) { message.textContent = '连接配置没有改动。'; return; }
    return apply('maintain', {operation:'connection', connection:changes}, '连接配置已保存。');
  };
  $('titanx-retry').onclick = () => apply('maintain', {operation:'retry'}, '已请求重新连接并核对登记。');
  function display(data) {
    const readback = $('titanx-readback');
    readback.hidden = !data.configured;
    form.hidden = data.configured;
    if (data.configured) {
      form.reset();
      const worker = {active:'运行中', activating:'启动中', inactive:'未启动', failed:'启动失败'};
      $('titanx-lab-name').textContent = data.lab_name || (data.lab_id ? '实验室 ' + data.lab_id : '—');
      $('titanx-node-name').textContent = data.node_id;
      const workerState = $('titanx-worker-state'), platformState = $('titanx-platform-state');
      workerState.textContent = worker[data.worker_state] || '状态未知';
      workerState.dataset.state = data.worker_state;
      const platform = String(data.platform_state ?? '').toLowerCase();
      platformState.textContent = ({online:'在线', offline:'离线'})[platform] || (data.platform_state ?? '待回查');
      platformState.dataset.state = platform;
      const list = $('titanx-registered'); list.replaceChildren();
      const slots = Object.entries(data.slots);
      const visible = board ? slots.filter(([, serial]) => serial === board.serial) : slots;
      const scope = $('titanx-scope');
      if (board) {
        scope.textContent = '当前板卡 · ' + (board.label || board.key || board.serial)
          + (visible.length ? '' : ' · 尚未分配 TitanX 槽位和 COM');
      } else {
        scope.textContent = '本工位已配置板卡数 · ' + slots.length;
      }
      for (const [slot, serial] of visible) {
        const item = document.createElement('li');
        const identity = document.createElement('div');
        identity.className = 'tx-identity';
        const name = document.createElement('strong');
        name.textContent = board?.label || board?.key || serial;
        name.setAttribute('data-i18n-ignore', '');
        identity.appendChild(name);
        if (name.textContent !== serial) {
          const sn = document.createElement('small');
          sn.textContent = serial;
          sn.setAttribute('data-i18n-ignore', '');
          identity.appendChild(sn);
        }
        const mapping = document.createElement('span');
        mapping.className = 'tx-mapping';
        mapping.textContent = '槽位 ' + slot + ' · COM ' + (data.first_com + Number(slot) - 1);
        const states = document.createElement('div');
        states.className = 'tx-states';
        const registration = document.createElement('span');
        const registered = data.registered?.includes(serial);
        registration.textContent = registered ? '已登记' : !data.registered || data.platform_error ? '待回查' : '待登记';
        registration.dataset.state = registered ? 'active' : 'pending';
        states.appendChild(registration);
        item.appendChild(identity);
        item.appendChild(mapping);
        item.appendChild(states);
        if (data.revision) {
          const paused = (data.paused_slots || []).includes(slot);
          const toggle = document.createElement('button');
          toggle.type = 'button'; toggle.textContent = paused ? '恢复接入' : '暂停接入';
          const applied = data.applied_slots?.[slot] === serial;
          const remote = {idle:'空闲', offline:'离线', busy:'运行中', flashing:'刷机中', error:'错误'};
          const state = data.board_states?.[serial];
          const activity = paused ? (applied ? '正在暂停' : '已暂停')
            : !applied ? '等待执行器应用' : (remote[state] || state);
          if (activity) {
            const status = document.createElement('small');
            status.textContent = activity;
            states.appendChild(status);
          }
          toggle.onclick = () => {
            if (!paused && !global.confirm('暂停 ' + serial + ' 的 TitanX 接入？档案和测试历史将保留。')) return;
            apply('maintain', {operation:paused ? 'resume' : 'pause', slot, serial},
              paused ? '已请求按原槽位和 COM 恢复接入。' : '已保存暂停设置，档案和历史已保留。');
          };
          item.appendChild(toggle);
        }
        list.append(item);
      }
      if (data.connection) {
        connection.reset();
        for (const [key, value] of Object.entries(data.connection)) {
          if (key === 'accept_dispatch') connection.elements[key].checked = !!value;
          else connection.elements[key].value = value || '';
        }
        for (const key of ['api_url','lab_id']) connection.elements[key].readOnly = !!data.connection[key];
      }
      displayAdditions(data);
      if (data.platform_error) showError(data.platform_error);
      else message.textContent = '';
      return;
    }
    fields.node_id.value = data.node_id;
    const container = $('titanx-boards'); container.replaceChildren();
    rows = data.boards.map(candidate => {
      const line = document.createElement('div'); line.className = 'tx-pick-board';
      const label = document.createElement('label'), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = !board || candidate.serial === board.serial;
      label.append(check, ' ' + candidate.label + (candidate.label === candidate.serial ? '' : ' · ' + candidate.serial) + (candidate.online ? '（在线）' : '（离线）'));
      const slot = document.createElement('span'), project = document.createElement('input');
      project.value = candidate.project || ''; project.placeholder = '沿用默认项目'; project.maxLength = 80; project.setAttribute('aria-label', candidate.serial + ' 的项目类型');
      const projectField = document.createElement('label');
      projectField.className = 'ft-field'; projectField.append(project);
      line.append(label, slot, projectField); container.append(line); check.onchange = refreshSlots;
      return {serial:candidate.serial, check, slot, project};
    });
    refreshSlots();
    message.textContent = rows.length ? '' : '请先接入板卡并在 DeckLab 确认硬件身份。';
  }
  function showError(text) {
    message.dataset.error = 'true';
    message.textContent = text;
  }
  async function load() {
    $('titanx-reload').hidden = true;
    message.dataset.error = 'false';
    addForm.hidden = true; $('titanx-add-panel').hidden = true;
    const gen = ++generation;
    message.textContent = '正在读取工位…';
    try {
      const data = await request('status');
      if (dialog.open && gen === generation) display(data);
    } catch (error) {
      if (gen !== generation) return;
      showError(error.message);
      $('titanx-reload').hidden = false;
    }
  }
  function showConnection(show) {
    $('titanx-connection-tab').setAttribute('aria-pressed', String(show));
    $('titanx-boards-tab').setAttribute('aria-pressed', String(!show));
    $('titanx-maintenance').hidden = !show;
    $('titanx-board-panel').hidden = show;
  }
  $('titanx-boards-tab').onclick = () => showConnection(false);
  $('titanx-connection-tab').onclick = () => showConnection(true);
  function open(selected) {
    if (dialog.open) return;
    board = selected || null;
    showConnection(false);
    for (const id of ['titanx-add-panel', 'titanx-mqtt-auth', 'titanx-maintain-mqtt-auth']) $(id).open = false;
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
    message.dataset.error = 'false';
    labs.disabled = true; message.textContent = '正在读取实验室…';
    try {
      const data = await request('labs', {api_url: fields.api_url.value.trim(), api_token: fields.api_token.value});
      if (id !== labRequest || gen !== generation) return;
      fields.lab_id.replaceChildren(new Option('选择实验室', ''));
      for (const lab of data.labs) fields.lab_id.add(new Option(lab.name, lab.id));
      if (data.labs.length === 1) fields.lab_id.selectedIndex = 1;
      refreshSlots(); message.textContent = data.labs.length ? '' : '该账号没有可访问的实验室。';
    } catch (error) { if (id === labRequest && gen === generation) showError(error.message); }
    finally { if (id === labRequest) labs.disabled = false; }
  };
  form.onsubmit = async event => {
    event.preventDefault(); if (saving) return;
    const gen = generation;
    const config = {slots:{}, projects:{}, first_com:Number(fields.first_com.value), lab_id:Number(fields.lab_id.value),
      accept_dispatch:fields.accept_dispatch.checked};
    for (const key of ['api_url','api_token','node_id','broker','username','password','project']) {
      const value = ['api_token','password'].includes(key) ? fields[key].value : fields[key].value.trim();
      if (value) config[key] = value;
    }
    rows.filter(row => row.check.checked).forEach((row, i) => {
      config.slots[i+1] = row.serial;
      if (row.project.value.trim()) config.projects[i+1] = row.project.value.trim();
    });
    message.dataset.error = 'false';
    saving = true; form.inert = true; message.textContent = '正在初始化工位…';
    try {
      const data = await request('initialize', {configuration:config});
      if (gen === generation) { display(data); message.textContent = '已保存。执行器启动后会逐板登记，可刷新查看结果。'; }
    } catch (error) { if (gen === generation) showError(error.message); }
    finally { saving = false; form.inert = false; }
  };
  $('titanx-reload').onclick = $('titanx-refresh').onclick = load;
  $('titanx-close').onclick = () => { if (!saving) dialog.close(); };
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => { if (dialog.open) return; ++generation; ++labRequest; form.reset(); if (connection.reset) connection.reset(); });
  global.DeckLabTitanX = {open: () => open(null), openBoard: open};
})(window);
