// Owner-only wiring editor. A draft is scoped to one scanned HA origin.
(function (global) {
  'use strict';
  let current;
  function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function button(parent, text, fn) {
    const node = element('button', text, parent);
    node.type = 'button'; node.onclick = fn;
    return node;
  }
  function selectOption(select, value, label) {
    const node = element('option', label, select);
    node.value = value;
  }
  function input(parent, label, type) {
    const wrap = element('label', label, parent);
    wrap.className = 'ft-field';
    const node = element('input', '', wrap);
    node.type = type;
    return node;
  }
  async function api(data) {
    const response = await global.SerialdUI.fetch('/api/control-slots', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw Object.assign(new Error(result.error || '工位请求失败'), {status:response.status});
    return result;
  }
  async function open(serial, unrecognized) {
    if (current) return;
    const dialog = current = element('dialog', '', document.body);
    dialog.className = 'wiring-dialog';
    const head = element('div', '', dialog); head.className = 'agent-share-head';
    element('strong', serial ? '绑定控制槽位 · ' + serial : '工位接线', head);
    const close = () => dialog.close();
    button(head, '关闭', close);
    dialog.onclose = () => { current = null; dialog.remove(); };
    const body = element('div', '', dialog); body.className = 'wiring-body';
    if (unrecognized) element('p', '确认串口、刷机 USB 和控制线都接到同一块板，然后选择接线槽位。DeckLab 会让该槽位进入下载模式并读取序列号。', body);
    const source = element('fieldset', '', body); source.className = 'wiring-source';
    const url = input(source, 'Home Assistant 地址', 'url'); url.placeholder = 'https://ha.example.com';
    const endpoints = element('datalist', '', source); endpoints.id = 'wiring-origins'; url.setAttribute('list', endpoints.id);
    const token = input(source, '访问令牌', 'password'); token.autocomplete = 'off';
    const scanButton = button(source, '检测槽位', () => scan('discover'));
    const savedButton = button(source, '查看已存接线', () => scan('saved'));
    const pane = element('div', '', body);
    const foot = element('div', '', dialog); foot.className = 'ft-actions wiring-footer';
    const status = element('span', '', foot); status.setAttribute('role','status');
    const save = button(foot, '保存接线', commit);
    save.className = 'pri';
    let revision, initial, scanId, handoff, rows = [], changes = new Map(), sequence = 0, busy = false, controller = '';
    dialog.showModal();
    function invalidate() {
      sequence++; scanId = ''; handoff = null; changes.clear(); pane.replaceChildren(); save.disabled = true;
      save.textContent = '保存接线';
      token.placeholder = initial && initial.connections.includes(url.value.trim()) ? '已保存，留空沿用' : '首次连接时填写';
      status.textContent = '';
    }
    url.oninput = () => { token.value = ''; invalidate(); };
    token.oninput = invalidate;
    function confirmingHandoff() {
      return handoff && changes.get(handoff.key)?.serial === serial;
    }
    function updateDraft() {
      save.disabled = busy || !scanId || !!unrecognized && !serial;
      save.textContent = confirmingHandoff() ? '确认换板接线' : '保存接线';
      status.textContent = changes.size && !confirmingHandoff() ? '待保存 ' + changes.size + ' 个槽位' : '';
    }
    function pending(row, target, agent) {
      if (target === row.serial && agent === row.allow_agent && !row.legacy && !row.suspended) changes.delete(row.key);
      else changes.set(row.key, {serial:target, allow_agent:agent});
      updateDraft();
    }
    function stageBoard(row) {
      rows.forEach(r => {
        if (r.key === row.key) return;
        if (r.serial === serial) pending(r,'',false);
        else if (changes.get(r.key)?.serial === serial) changes.delete(r.key);
      });
      pending(row, serial, row.serial === serial ? row.allow_agent : true);
    }
    function show() {
      pane.replaceChildren();
      const groups = new Map();
      rows.forEach(row => {
        const id = row.key.split('/')[0];
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(row);
      });
      if (!groups.size) { element('p', '未发现板控槽位', pane); return; }
      const picker = element('select', '', pane); picker.setAttribute('aria-label', '控制器');
      const groupNames = [...groups].sort((a,b) => (a[1][0].area+a[1][0].name).localeCompare(b[1][0].area+b[1][0].name));
      groupNames.forEach(([id, group]) => selectOption(picker, id, [group[0].area,group[0].name].filter(Boolean).join(' · ')));
      handoff = serial && initial.handoff && initial.handoff.origin === url.value
        ? rows.find(r => r.key === initial.handoff.key && r.present && r.suspended) : null;
      if (handoff) stageBoard(handoff);
      const own = handoff || rows.find(r => serial && r.serial === serial) || rows.find(r => r.serial);
      if (controller && groups.has(controller)) picker.value = controller;
      else if (own) picker.value = own.key.split('/')[0];
      const filter = serial || unrecognized ? null : input(pane, '查找板卡', 'search');
      if (filter) filter.placeholder = '标签、序列号、串口或刷机 USB';
      const panel = element('div','',pane);
      function draw() {
        panel.replaceChildren();
        controller = picker.value;
        const group = groups.get(picker.value);
        if (confirmingHandoff() && group.includes(handoff)) {
          const note = element('p', '已选槽位 ' + handoff.key.split('/')[1] + '。请核对新板接在同一控制槽位。', panel);
          note.className = 'wiring-handoff';
          element('small', handoff.serial + ' → ' + serial, note);
        }
        for (const row of group) {
          const n = row.key.split('/')[1];
          const delta = changes.get(row.key);
          const selected = delta ? delta.serial : row.serial;
          const item = element('div','',panel); item.className = 'wiring-row';
          const caption = element('div','',item);
          element('strong','槽位 '+n,caption);
          const capabilities = Object.keys(row.entities).map(a => a==='reset'?'复位':'下载模式').join('、');
          element('small',row.suspended?(initial.handoff && initial.handoff.key===row.key?'此串口已换板，待确认':'换板待确认'):row.present?capabilities:'仅有已存记录',caption);
          if (serial && selected === serial) {
            item.dataset.current = '';
            if (!delta && !row.suspended) element('small','当前板卡',caption);
          }
          const board = element('select','',item); board.setAttribute('aria-label','槽位 '+n+' 的板卡');
          selectOption(board,'','未绑定板卡');
          const query = filter ? filter.value.trim().toLowerCase() : '';
          initial.boards.forEach(b => {
            const label = [b.label, b.serial, b.uart, b.flash_usb && 'USB '+b.flash_usb].filter(Boolean).join(' · ');
            if (!query || b.serial===selected || label.toLowerCase().includes(query)) selectOption(board,b.serial,label);
          });
          if (selected && !initial.boards.some(b=>b.serial===selected)) selectOption(board,selected,selected);
          board.value = selected;
          const lab = element('label','',item), agent = element('input','',lab); agent.type='checkbox';
          lab.append(' 允许 AI'); agent.checked = delta?delta.allow_agent:row.allow_agent;
          function change() {
            if (this === board) agent.checked = !!board.value && (board.value === row.serial ? row.allow_agent : true);
            pending(row,board.value,agent.checked);
            agent.disabled = !board.value;
          }
          board.onchange = change; agent.onchange = change; agent.disabled = !selected || !!serial && selected!==serial;
          if (unrecognized && !serial) {
            board.disabled = true; agent.disabled = true;
            if (row.occupied_online) element('small', '原板卡在线，换板请先在原设备页操作', caption);
            else if (row.present && row.entities['enter-download']) {
              const identify = button(item, '进入下载模式并识别', () => identifyBoard(row));
              identify.disabled = busy;
            }
          } else if (serial) {
            board.disabled = true;
            if (selected === serial && !delta && !row.suspended) {
              const remove = button(item, '移除接线', () => { pending(row, '', false); draw(); });
              remove.className = 'wiring-remove';
            } else if (delta) {
              button(item, '撤销变更', () => { changes.clear(); updateDraft(); draw(); });
            } else {
              const bind = button(item, row.suspended ? '选为换板接线' : row.serial ? '改绑到此槽' : '绑定到空槽',
                () => { stageBoard(row); draw(); });
              bind.disabled = !row.present || !!row.occupied_online;
            }
          } else if (row.suspended || row.legacy) {
            button(item,row.suspended?'确认当前板':'接管旧接线', () => { pending(row,board.value,agent.checked); });
          }
        }
      }
      picker.onchange = draw; if (filter) filter.oninput = draw; draw();
    }
    async function scan(action) {
      const gen = ++sequence;
      changes.clear(); pane.replaceChildren(); scanId=''; handoff=null; save.disabled=true;
      save.textContent = '保存接线';
      status.textContent='正在读取接线…';
      try {
        const d=await api({action, revision, origin:url.value.trim(), token:token.value});
        if (current!==dialog || gen!==sequence) return;
        rows=d.slots; scanId=d.scan; status.textContent='';
        show(); updateDraft();
      } catch (e) { if(current===dialog && gen===sequence) status.textContent=e.message; }
    }
    async function identifyBoard(row) {
      if (busy || !unrecognized || serial || !scanId || row.occupied_online || !row.entities['enter-download']) return;
      if (!global.confirm('确认让 '+row.name+' · 槽位 '+row.key.split('/')[1]+' 进入下载模式？板卡会复位。')) return;
      const client = global.SerialdTasks.createClient(global.SerialdUI.fetch);
      const ids = global.crypto.getRandomValues(new Uint8Array(16));
      const requestId = Array.from(ids, n => n.toString(16).padStart(2, '0')).join('');
      busy=true; save.disabled=true; body.inert=true;
      status.textContent='正在等待板卡进入下载模式并核对串口…';
      try {
        let started;
        try {
          started = await api({action:'identify-slot', revision, scan:scanId, slot:row.key, request_id:requestId, ...unrecognized});
        } catch (error) {
          if (error.status && error.status < 500) throw error;
          started = await client.request('/api/task?request_id='+encodeURIComponent(requestId))
            .catch(() => { throw new Error('控制回执未收到，请查看原任务和板卡状态。'); });
        }
        const result = await client.watch(started.task, {key:''}, task => {
          if (current===dialog && task.progress?.phase==='reading-board-serial')
            status.textContent='USB 和串口已核对，正在读取板载序列号…';
        });
        if (current!==dialog) return;
        if (!result.serial) throw new Error('未取得已核验的板卡序列号');
        serial=result.serial;
        if (!initial.boards.some(board=>board.serial===serial)) initial.boards.push({serial});
        stageBoard(row); show();
        status.textContent='已识别 '+serial+'。核对槽位后点击保存接线。';
      } catch(e) { if (current===dialog) status.textContent=e.message; }
      finally { busy=false; body.inert=false; save.disabled=!scanId || !serial; }
    }
    async function commit() {
      if (busy || !scanId) return;
      const lines=[...changes].map(([key,value])=>{
        const r=rows.find(x=>x.key===key);
        return r.name+' / '+key.split('/')[1]+'：'+(r.serial||'未绑定')+' → '+(value.serial||'未绑定');
      });
      if (lines.length && !(confirmingHandoff() && changes.size === 1)
          && !global.confirm(lines.join('\n')+'\n\n保存并立即应用这些接线？')) return;
      busy=true; save.disabled=true; body.inert=true;
      try {
        await api({action:'save', revision, scan:scanId, changes:Object.fromEntries(changes)});
        global.dispatchEvent(new Event('decklab-wiring-saved')); close();
      } catch(e) { status.textContent=e.message; }
      finally { busy=false; body.inert=false; save.disabled=false; }
    }
    save.disabled=true; source.disabled=true;
    try {
      initial=await api({action:'list',serial:serial||''});
      if (current!==dialog) return;
      revision=initial.revision; initial.connections.forEach(value=>selectOption(endpoints,value,value));
      const bound=initial.bindings.find(r=>serial && r.serial===serial);
      url.value=(bound || initial.handoff || {}).origin || initial.connections[0] || '';
      invalidate(); source.disabled=false;
      if (url.value) await scan('discover');
    } catch(e) { if(current===dialog) status.textContent=e.message; }
  }
  global.SerialdControlSlots={open};
})(window);
