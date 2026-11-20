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
    if (!response.ok) throw new Error(result.error || '工位请求失败');
    return result;
  }
  async function open(serial) {
    if (current) return;
    const dialog = current = element('dialog', '', document.body);
    dialog.className = 'wiring-dialog';
    const head = element('div', '', dialog); head.className = 'agent-share-head';
    element('strong', serial ? '绑定控制槽位 · ' + serial : '工位接线', head);
    const close = () => dialog.close();
    button(head, '关闭', close);
    dialog.onclose = () => { current = null; dialog.remove(); };
    const body = element('div', '', dialog); body.className = 'wiring-body';
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
    let revision, initial, scanId, rows = [], changes = new Map(), sequence = 0, busy = false;
    dialog.showModal();
    function invalidate() {
      sequence++; scanId = ''; changes.clear(); pane.replaceChildren(); save.disabled = true;
      token.placeholder = initial && initial.connections.includes(url.value.trim()) ? '已保存，留空沿用' : '首次连接时填写';
      status.textContent = '';
    }
    url.oninput = () => { token.value = ''; invalidate(); };
    token.oninput = invalidate;
    function pending(row, target, agent) {
      if (target === row.serial && agent === row.allow_agent && !row.legacy && !row.suspended) changes.delete(row.key);
      else changes.set(row.key, {serial:target, allow_agent:agent});
      save.disabled = busy || !scanId;
      status.textContent = changes.size ? '待保存 ' + changes.size + ' 个槽位' : '';
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
      const own = rows.find(r => serial && r.serial === serial) || rows.find(r => initial.handoff && initial.handoff.origin===url.value && r.key===initial.handoff.key) || rows.find(r => r.serial);
      if (own) picker.value = own.key.split('/')[0];
      const filter = serial ? null : input(pane, '查找板卡', 'search');
      if (filter) filter.placeholder = '标签、序列号、串口或刷机 USB';
      const panel = element('div','',pane);
      function draw() {
        panel.replaceChildren();
        const group = groups.get(picker.value);
        for (let n=1;n<=4;n++) {
          const row = group.find(r => r.key.endsWith('/'+n));
          const item = element('div','',panel); item.className = 'wiring-row';
          const caption = element('div','',item);
          element('strong','槽位 '+n,caption);
          if (!row) { element('small','未发现控制实体',caption); continue; }
          const capabilities = Object.keys(row.entities).map(a => a==='reset'?'复位':'下载模式').join('、');
          element('small',row.suspended?(initial.handoff && initial.handoff.key===row.key?'此串口已换板，待确认':'换板待确认'):row.present?capabilities:'仅有已存记录',caption);
          const delta = changes.get(row.key);
          const selected = delta ? delta.serial : row.serial;
          const board = element('select','',item); board.setAttribute('aria-label','槽位 '+n+' 的板卡');
          selectOption(board,'','未绑定');
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
            if (!board.value || board.value!==selected && this===board) agent.checked = false;
            pending(row,board.value,agent.checked);
            agent.disabled = !board.value;
          }
          board.onchange = change; agent.onchange = change; agent.disabled = !selected || !!serial && selected!==serial;
          if (serial) {
            board.disabled = true;
            const btn = button(item, selected===serial && !row.suspended?'解除绑定':'绑定此板', () => {
              const next = selected===serial && !row.suspended?'':serial;
              if (next) rows.forEach(r => {
                if (r.key!==row.key && (changes.get(r.key)||r).serial===next) pending(r,'',false);
              });
              pending(row,next,!!next && selected===next && agent.checked); draw();
            });
            btn.disabled = !row.present && selected!==serial;
          } else if (row.suspended || row.legacy) {
            button(item,row.suspended?'确认当前板':'接管旧接线', () => { pending(row,board.value,agent.checked); });
          }
        }
      }
      picker.onchange = draw; if (filter) filter.oninput = draw; draw();
    }
    async function scan(action) {
      const gen = ++sequence;
      changes.clear(); pane.replaceChildren(); scanId=''; save.disabled=true;
      status.textContent='正在读取接线…';
      try {
        const d=await api({action, revision, origin:url.value.trim(), token:token.value});
        if (current!==dialog || gen!==sequence) return;
        rows=d.slots; scanId=d.scan; status.textContent='';
        show(); save.disabled=false;
      } catch (e) { if(current===dialog && gen===sequence) status.textContent=e.message; }
    }
    async function commit() {
      if (busy || !scanId) return;
      const lines=[...changes].map(([key,value])=>{
        const r=rows.find(x=>x.key===key);
        return r.name+' / '+key.split('/')[1]+'：'+(r.serial||'未绑定')+' → '+(value.serial||'未绑定');
      });
      if (lines.length && !global.confirm(lines.join('\n')+'\n\n保存并立即应用这些接线？')) return;
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
