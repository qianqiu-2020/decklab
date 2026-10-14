(function () {
  'use strict';
  var UI = window.SerialdUI, $ = UI.byId, fmt = UI.formatBytes, ago = UI.age, copyText = UI.copyText;
  var fetch = UI.fetch;
  function ownsInput(owner) { return owner && owner.actor === 'web' && owner.client_id === UI.clientId; }
  var curSid = 'serial';              // serial、sshN 或本地只读 log:* 标签
  var sessions = [{ sid: 'serial', kind: 'serial' }];
  var sessionPage = 0, eventPage = 0;
  var recordsTab = 'logs';
  var ACTIVITY_PAGE_SIZE = 10;
  var takenOver = {};
  var deviceDirectory = window.SerialdDevices.create({
    byId: $, fetch: fetch, formatBytes: fmt, age: ago,
    getView: function () { return terminalView.current; },
    isCurrent: function (ctx) { return isCurrent(ctx); },
    open: select, renderInfo: renderInfo, loadSessions: loadSessions, loadActivity: loadActivity,
    updateInputOwner: updateInputOwner
  });
  var pollDevices = deviceDirectory.poll, markSelected = deviceDirectory.markSelected, adoptKey = deviceDirectory.adopt;
  var terminalView = window.SerialdTerminalView.create({
    byId: $, fetch: fetch, ownsInput: ownsInput, setInputMode: setInputMode,
    onControlChange: function () { renderTabs(); },
    board: function (key) { return deviceDirectory.items.find(function (board) { return board.key === key; }); },
    session: function (sid) { return sessions.find(function (session) { return session.sid === sid; }); },
    takenOver: function (sid) { return !!takenOver[sid]; },
    onSelect: function (key, sid) { deviceDirectory.selectionChanged(key); curSid = sid; },
    clearTerminalSearch: function (focus) { clearTerminalSearch(focus); },
    clearCommandDecorations: function (ctx) { clearCommandDecorations(ctx); },
    renderCommandNav: function (ctx) { renderCommandNav(ctx); },
    scheduleCommandNav: function (ctx) { scheduleCommandNav(ctx); },
    searchTerminal: searchTerminal, loadCommands: loadCommands,
    copyText: copyText, copySel: copySel,
    updateInputOwner: function (ctx, owner) {
      if (ctx.ownerPending) return;
      deviceDirectory.advanceOwnership();
      updateInputOwner(ctx, owner);
      var board = deviceDirectory.find(ctx.key);
      if (board) board.input_owner = owner;
    }
  });
  var isCurrent = terminalView.isCurrent, createView = terminalView.create, disposeView = terminalView.dispose;
  var resizeTerm = terminalView.resize, redrawTerminal = terminalView.redraw;
  var writeTerminalPayload = terminalView.write, startStream = terminalView.start, send = terminalView.send;
  var terminalNavigation = window.SerialdTerminalNavigation.create({
    byId: $, isCurrent: isCurrent,
    getView: function () { return terminalView.current; },
    navigationVisible: function () { return recordsTab === 'inputs' && !$('d-records').hidden; }
  });
  var clearTerminalSearch = terminalNavigation.clearSearch;
  var clearCommandDecorations = terminalNavigation.clearDecorations;
  var renderCommandNav = terminalNavigation.renderNavigation;
  var scheduleCommandNav = terminalNavigation.scheduleNavigation;
  var jumpToCommand = terminalNavigation.jump;
  var archives = window.SerialdArchives.create({
    byId: $, formatBytes: fmt, fetch: fetch, isCurrent: isCurrent,
    getView: function () { return terminalView.current; },
    logsVisible: function () { return !$('d-records').hidden && recordsTab === 'logs'; },
    openLogs: function () { recordsTab = 'logs'; setRecordsOpen(true); },
    openArchive: openArchivedLog, searchTerminal: searchTerminal
  });
  var loadLogs = archives.load, searchBootLogs = archives.search, clearBootSearch = archives.clearSearch;
  var taskUI = window.SerialdTasks.create({
    byId: $, fetch: fetch, age: ago,
    getView: function () { return terminalView.current; },
    visible: function () { return !$('d-records').hidden && recordsTab === 'tasks'; },
    openSession: function (task) {
      var result = task.result;
      if (!sessions.some(function (session) { return session.sid === result.sid; })) {
        sessions.push({ sid: result.sid, kind: 'ssh', ip: result.ip, actor: task.actor });
      }
      switchSession(result.sid);
    }
  });
  window.SerialdShareSelection = function () {
    return deviceDirectory.items.filter(function (device) { return device.key === deviceDirectory.selected; })[0] || null;
  };

  function setInputMode(ctx) {
    ['b-serial-size', 'd-in', 'd-kbd', 'd-slow-paste', 'b-send', 'b-cr', 'b-ctrlc', 'b-paste'].forEach(function (id) {
      $(id).disabled = ctx.readonly;
    });
    $('slow-paste-option').hidden = ctx.sid !== 'serial';
    $('b-takeover').hidden = !ctx.canTakeover;
    $('b-input-owner').hidden = ctx.sid !== 'serial';
    $('b-input-owner').disabled = !!ctx.ownerPending;
    var owner = ctx.inputOwner;
    $('input-owner-label').textContent = !owner ? '占用输入' : ownsInput(owner) ? '释放输入' : '接管输入';
    var ownerName = owner && owner.actor === 'web' ? ownsInput(owner) ? '当前页面' : '另一网页客户端' : owner && owner.actor;
    $('b-input-owner').title = owner ? '串口输入由 ' + ownerName + ' 占用；输出仍可共享查看' : '可选占用串口输入，其他人仍可查看输出';
  }

  function updateInputOwner(ctx, owner) {
    var previous = ctx.inputOwner;
    ctx.inputOwner = owner || null;
    if (ctx.sid !== 'serial') return;
    ctx.readonly = !!owner && !ownsInput(owner);
    if (ctx.readonly) {
      ctx.inputQueue = [];
      clearTimeout(ctx.inputTimer);
      ctx.inputTimer = 0;
    }
    if (ctx.term) ctx.term.options.cursorBlink = !ctx.readonly;
    setInputMode(ctx);
    if ((previous && previous.id || '') !== (owner && owner.id || '')) {
      $('d-status').textContent = !owner ? '已恢复共享串口输入'
        : ownsInput(owner) ? '当前页面已占用串口输入' : '另一客户端已占用串口输入；输出仍可查看';
    }
  }

  function searchTerminal(direction) {
    if ($('terminal-search-scope').value === 'boots') { searchBootLogs(0); return; }
    terminalNavigation.search(direction);
  }

  /* ---------- 复制 / 粘贴 ----------
     注意:页面是 http://内网IP,不是安全上下文,所以 navigator.clipboard 用不了。
     复制走 execCommand('copy') + 临时 textarea(用户手势内有效);
     粘贴走原生 paste 事件(非安全上下文里也能拿到 clipboardData)。
     另外终端惯例:Ctrl+C 是发给板子的中断信号,复制用 Ctrl+Shift+C。 */
  function copySel(ctx) {
    if (!isCurrent(ctx)) return;
    var s = ctx.term.getSelection();
    if (!s) { $('d-status').textContent = ' 没有选中内容（鼠标拖选后重试）'; return; }
    copyText(s, false);
    ctx.term.clearSelection();
  }
  function pasteInto(text, ctx) {
    if (!isCurrent(ctx)) return;
    if (ctx.readonly) {
      $('d-status').textContent = ctx.kind === 'log' ? ' 当前是只读归档日志' : ' 当前正在只读查看 agent 会话';
      return;
    }
    if (!text) { $('d-status').textContent = ' 剪贴板里没有文本（终端里 Ctrl+C 是中断信号，复制请用 Ctrl+Shift+C 或拖选）'; return; }
    if (ctx.sid === 'serial' && $('d-slow-paste').checked) {
      Array.from(text).forEach(function (char) { send(char, ctx, true); });
      $('d-status').textContent = ' 正在逐字符粘贴 ' + Array.from(text).length + ' 字符（固件模式）';
    } else {
      send(text, ctx);
      $('d-status').textContent = ' 已粘贴 ' + text.length + ' 字符';
    }
  }
  // 原生粘贴(Ctrl+V / 中键)在终端里也要能送到板子 —— 不受"键盘直通"开关影响。
  // 挂在 document 上而不是 d-term 上：焦点不在终端里时(例如从别处复制完切回浏览器、
  // 没先点一下终端就按 Ctrl+V)，paste 事件的目标是 body，挂在 d-term 上的监听器
  // 根本收不到,表现就是"按了完全没反应"。但同时要给页面自己的输入框让路。
  document.addEventListener('paste', function (e) {
    if (!deviceDirectory.selected) return;
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')
        && String(ae.className).indexOf('xterm-helper-textarea') < 0) return;   // 发送命令框等,照常粘贴
    var cb = e.clipboardData || window.clipboardData;
    var t = cb && cb.getData ? cb.getData('text') : '';
    e.preventDefault(); e.stopPropagation();
    pasteInto(t, terminalView.current);
  }, true);
  // 「粘贴」按钮:借一个临时输入框接住 Ctrl+V —— 非安全上下文下拿剪贴板的可靠办法
  $('b-paste').addEventListener('click', function () {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    var ta = document.createElement('textarea');
    ta.placeholder = '在此按 Ctrl+V,内容会直接送到串口';
    ta.style.cssText = 'position:fixed;left:50%;top:22%;transform:translateX(-50%);'
      + 'z-index:99;width:min(520px,80vw);height:74px;padding:9px;font-size:14px';
    document.body.appendChild(ta);
    ta.focus();
    ta.addEventListener('paste', function (ev) {
      var t = (ev.clipboardData || {}).getData ? ev.clipboardData.getData('text') : '';
      ev.preventDefault(); ta.remove(); pasteInto(t, ctx);
    });
    ta.addEventListener('blur', function () { setTimeout(function () { ta.remove(); }, 200); });
  });

  /* ---------- 设备列表 ---------- */
  function select(key) {
    // Slow paste is a workaround for one firmware input path, not a board-wide
    // default.  Keep it only while staying on the same selected board.
    if (key !== deviceDirectory.selected) { $('d-slow-paste').checked = false; clearBootSearch(); }
    var ctx = createView(key, 'serial');
    // 切设备时立即清掉旧会话标签；迟到的列表/动作响应不能借用新 deviceDirectory.selected。
    sessions = [{ sid: 'serial', kind: 'serial' }];
    var d = (deviceDirectory.items || []).filter(function (x) { return x.key === key; })[0];
    $('detail').hidden = false;
    $('detail').classList.remove('info-open');
    $('b-info').setAttribute('aria-expanded', 'false');
    $('workspace').classList.toggle('selected', true);
    document.body.classList.toggle('detail-open', true);
    clearTerminalSearch(false);
    $('d-name').textContent = d ? d.label : key;
    $('d-dev').textContent = d ? d.dev : '';
    $('d-key').textContent = d && d.serial ? 'SN ' + d.serial : key;
    $('b-switch').hidden = !(d && d.serial && d.alive);
    $('d-status').textContent = '';
    $('d-hint').hidden = true;
    $('d-in').value = '';
    $('d-logs').textContent = '';
    sessionPage = 0; eventPage = 0;
    $('d-activity').textContent = '';
    $('d-events').textContent = '';
    $('d-commands').textContent = '';
    loadActivity();
    taskUI.refresh();
    loadCommands();
    deviceDirectory.rememberInfo(d);
    resizeTerm();
    loadLogs();
    renderInfo(d && d.info);
    markSelected();                    // 高亮立刻跟上,不等轮询
    renderTabs();
    loadSessions();
    // 关键:作废在途请求并立刻开新轮询。否则旧请求还挂在上一台设备上长轮询(最多 15s),
    // 切过去就得干等它返回 —— 这正是"切设备很慢"的原因。
    startStream(ctx);
  }

  function switchSession(sid) {
    if (!deviceDirectory.selected) return;
    if (sid === curSid) {
      if ($('d-term').scrollIntoView) $('d-term').scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    var ctx = createView(deviceDirectory.selected, sid);
    $('d-status').textContent = '';
    $('d-in').value = '';
    resizeTerm();
    renderTabs();
    loadSessions();
    loadLogs();
    loadCommands();
    var selected = sessions.filter(function (s) { return s.sid === sid; })[0];
    if (selected && selected.kind === 'log') loadArchivedLog(ctx, selected);
    else startStream(ctx);
    if ($('d-term').scrollIntoView) $('d-term').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function renderTabs() {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    var bar = $('d-tabs');
    bar.textContent = '';
    sessions.forEach(function (s) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = (s.sid === curSid ? 'on' : '');
      b.dataset.kind = s.kind;
      b.textContent = s.kind === 'serial' ? '串口'
        : (s.kind === 'log' ? s.label : ('SSH ' + (s.actor ? s.actor + ' · ' : '') + (s.ip || s.sid)));
      var readonly = s.kind === 'log' || (s.sid === ctx.sid && ctx.readonly)
        || (s.sid !== ctx.sid && s.actor && s.actor !== 'web' && !takenOver[s.sid]);
      if (readonly) b.textContent += ' · 只读';
      if (s.log_error) b.textContent += ' · 日志写入失败';
      b.title = s.kind === 'serial' ? '板子控制台（实时输出）'
        : (s.kind === 'log' ? '归档日志快照，只读显示在终端中'
          : (s.persistent ? 'agent 持久 SSH 会话；同一 agent 后续命令复用此标签，直到手动关闭'
                           : 'SSH 会话，独立记录到 ssh-*.log'));
      b.addEventListener('click', function () { if (isCurrent(ctx)) switchSession(s.sid); });
      bar.appendChild(b);
      if (s.kind === 'log' || (s.kind === 'ssh' &&
          (!s.actor || s.actor === 'web' || s.persistent))) {
        var x = document.createElement('button');
        x.type = 'button'; x.className = 'x'; x.textContent = '×';
        x.title = s.kind === 'log' ? '关闭这个归档标签'
          : (s.persistent ? '关闭这个 agent 持久 SSH 会话' : '关闭这条 SSH 会话');
        x.addEventListener('click', function (e) {
          e.stopPropagation();
          if (!isCurrent(ctx)) return;
          if (s.kind === 'log') {
            sessions = sessions.filter(function (y) { return y.sid !== s.sid; });
            if (curSid === s.sid) switchSession('serial'); else renderTabs();
            return;
          }
          ctx.sessionsSeq++;
          fetch('/api/session-close', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: s.sid }) })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              if (!isCurrent(ctx)) return;
              if (!d.ok) throw new Error(d.error || '关闭失败');
              ctx.sessionsSeq++;
              sessions = sessions.filter(function (y) { return y.sid !== s.sid; });
              if (curSid === s.sid) switchSession('serial'); else renderTabs();
            })
            .catch(function (e) {
              if (isCurrent(ctx)) $('d-status').textContent = ' 关闭失败：' + e.message;
            });
        });
        bar.appendChild(x);
      }
    });
    var sp = document.createElement('span'); sp.className = 'sp';
    var add = document.createElement('button');
    add.type = 'button'; add.textContent = '+ SSH 会话';
    add.disabled = !!ctx.connecting;
    add.title = '创建独立 SSH 会话；默认 root 密码认证失败时尝试通过串口自动配置';
    add.addEventListener('click', function () {
      if (!isCurrent(ctx) || ctx.connecting) return;
      ctx.connecting = true; add.disabled = true;
      $('d-status').textContent = ' 正在建立 SSH 会话…（认证失败时可能通过串口配置 root 登录）';
      taskUI.run('ssh-connect', ctx)
        .then(function (d) {
          if (!isCurrent(ctx)) return;
          ctx.connecting = false; add.disabled = false;
          if (!d.ok) { $('d-status').textContent = ' 失败：' + (d.error || ''); return; }
          if (d.device_key) adoptKey(ctx, d.device_key, d.serial);
          if (!sessions.some(function (s) { return s.sid === d.sid; })) {
            sessions.push({ sid: d.sid, kind: 'ssh', ip: d.ip });
          }
          switchSession(d.sid);
          $('d-status').textContent = (d.auto_configured ? ' 已通过串口配置 root SSH；连接 ' : ' 已连接 ') + d.ip;
          pollDevices();
        })
        .catch(function (e) {
          if (!isCurrent(ctx)) return;
          ctx.connecting = false; add.disabled = false; $('d-status').textContent = ' 失败：' + e.message;
        });
    });
    sp.appendChild(add);
    bar.appendChild(sp);
  }
  function loadSessions() {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    var seq = ++ctx.sessionsSeq;
    fetch('/api/sessions?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || ''), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx) || seq !== ctx.sessionsSeq || !d.sessions
            || (d.key && d.key !== ctx.key)) return;
        var archived = sessions.filter(function (s) { return s.kind === 'log'; });
        sessions = d.sessions.concat(archived);
        // 当前会话若已不存在(比如被别的窗口关了),退回串口
        if (!sessions.some(function (s) { return s.sid === curSid; })) {
          switchSession('serial'); return;
        }
        renderTabs();
      })
      .catch(function () {});
  }

  function openArchivedLog(ctx, name, label, url) {
    if (!isCurrent(ctx)) return;
    var sid = 'log:' + name;
    var archived = sessions.filter(function (s) { return s.sid === sid; })[0];
    if (!archived) {
      archived = { sid: sid, kind: 'log', name: name, label: label || name, url: url || '',
                   loaded: false, payload: null };
      sessions.push(archived);
    }
    switchSession(sid);
  }

  function loadArchivedLog(ctx, archived) {
    if (!isCurrent(ctx) || !archived || archived.kind !== 'log') return;
    if (archived.loaded && archived.payload) {
      writeTerminalPayload(ctx, archived.payload);
      $('d-status').textContent = archived.status;
      return;
    }
    $('d-status').textContent = ' 正在读取只读归档 ' + archived.name + '…';
    fetch(archived.url || ('/api/log?key=' + encodeURIComponent(ctx.key)
      + '&serial=' + encodeURIComponent(ctx.serial || '')
      + '&file=' + encodeURIComponent(archived.name) + '&full=1'))
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!isCurrent(ctx) || curSid !== archived.sid) return;
        if (d.error) throw new Error(d.error);
        archived.loaded = true;
        archived.payload = { data_b64: d.data_b64, data: d.data };
        archived.status = ' 只读归档 · 字节 ' + d.start_off + '–' + d.off + ' / ' + d.size
          + (d.more ? '（内容未读完）' : '（完整文件）');
        writeTerminalPayload(ctx, archived.payload);
        $('d-status').textContent = archived.status;
      }).catch(function (e) {
        if (isCurrent(ctx) && curSid === archived.sid) $('d-status').textContent = ' 读取归档失败：' + e.message;
      });
  }

  function loadCommands() {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    fetch('/api/commands?key=' + encodeURIComponent(ctx.key)
      + '&serial=' + encodeURIComponent(ctx.serial || ''), { cache: 'no-store' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!isCurrent(ctx) || (data.key && data.key !== ctx.key)) return;
        var box = $('d-commands'); box.textContent = '';
        var commands = data.commands || [];
        if (!commands.length) { box.textContent = '（还没有可复用的命令）'; return; }
        commands.forEach(function (entry) {
          var button = document.createElement('button'); button.type = 'button';
          button.className = 'command-chip';
          button.textContent = entry.command + (entry.use_count > 1 ? '  ×' + entry.use_count : '');
          button.title = '填入命令输入框';
          button.addEventListener('click', function () {
            // Commands belong to the board, not the terminalView.current that loaded them.
            // Switching serial/SSH tabs must not invalidate these buttons.
            if (!isCurrent(terminalView.current) || terminalView.current.key !== ctx.key
                || (terminalView.current.serial || '') !== (ctx.serial || '')) return;
            if (terminalView.current.readonly) {
              $('d-status').textContent = '当前会话只读；请切换可输入的串口 / SSH 会话，或先介入该会话';
              return;
            }
            $('d-in').value = entry.command; $('d-in').focus();
          });
          box.appendChild(button);
        });
      }).catch(function (error) {
        if (isCurrent(ctx)) $('d-commands').textContent = '读取命令历史失败：' + error.message;
      });
  }

  function setPager(prefix, page, total, count) {
    var pages = Math.max(1, Math.ceil(total / ACTIVITY_PAGE_SIZE));
    $(prefix + '-page').textContent = '第 ' + (page + 1) + ' / ' + pages + ' 页 · ' + total + ' 条';
    $(prefix + '-prev').disabled = page === 0;
    $(prefix + '-next').disabled = (page * ACTIVITY_PAGE_SIZE + count) >= total;
  }

  function loadActivity() {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    var seq = ++ctx.activitySeq;
    ['activity-prev', 'activity-next', 'event-prev', 'event-next'].forEach(function (id) {
      $(id).disabled = true;
    });
    var sessionOffset = sessionPage * ACTIVITY_PAGE_SIZE;
    var eventOffset = eventPage * ACTIVITY_PAGE_SIZE;
    fetch('/api/activity?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '')
      + '&session_offset=' + sessionOffset + '&event_offset=' + eventOffset + '&limit=' + ACTIVITY_PAGE_SIZE)
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!isCurrent(ctx) || seq !== ctx.activitySeq || !d.sessions) return;
        var box = $('d-activity');
        box.textContent = '';
        d.sessions.forEach(function (entry) {
          var row = document.createElement('div'); row.className = 'activity-row';
          var main = document.createElement('div'); main.className = 'activity-main';
          var actor = document.createElement('span'); actor.className = 'actor'; actor.textContent = entry.actor;
          var meta = document.createElement('span'); meta.className = 'activity-meta';
          meta.textContent = ' · ' + new Date(entry.started * 1000).toLocaleString('zh-CN') + ' · ' + entry.status
            + (entry.exit_code === null ? '' : ' · 退出码 ' + entry.exit_code);
          var title = document.createElement('code'); title.textContent = entry.title || entry.sid;
          main.appendChild(actor); main.appendChild(meta); main.appendChild(title); row.appendChild(main);
          var button = document.createElement('button'); button.type = 'button';
          button.textContent = entry.status === 'active' ? '实时查看' : '查看记录';
          button.addEventListener('click', function () {
            if (!isCurrent(ctx)) return;
            if (entry.status === 'active') {
              if (!sessions.some(function (s) { return s.sid === entry.sid; })) sessions.push(Object.assign({ kind: 'ssh' }, entry));
              switchSession(entry.sid); return;
            }
            fetch('/api/logs?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || ''))
              .then(function (r) { return r.json(); }).then(function (logs) {
                if (!isCurrent(ctx)) return;
                var file = (logs.files || []).filter(function (f) { return f.name.indexOf('ssh-' + entry.sid + '-') === 0; })[0];
                if (file) openArchivedLog(ctx, file.name, 'SSH 记录 · ' + (entry.actor || entry.sid));
                else $('d-status').textContent = ' 该会话没有可用日志';
              }).catch(function (e) { if (isCurrent(ctx)) $('d-status').textContent = e.message; });
          });
          row.appendChild(button); box.appendChild(row);
        });
        if (!box.children.length) box.textContent = '尚无 SSH 会话记录';
        var sessionTotal = d.session_total === undefined ? d.sessions.length : d.session_total;
        setPager('activity', sessionPage, sessionTotal, d.sessions.length);

        var eventBox = $('d-events'); eventBox.textContent = '';
        var actionNames = { identified: '识别完成', identify: '识别请求',
          'identity-promoted': '身份升级', 'identity-corrected': '身份更正',
          'identity-confirmed': '身份确认', 'boot-marker-initialized': '初始化 /boot 标记',
          rescue: '串口自愈', 'ssh-enable': '启用 SSH', 'session-takeover': '人工介入 SSH',
          'ssh-auto-config': '自动配置 SSH', 'switch-armed': '准备更换设备' };
        (d.events || []).forEach(function (entry) {
          var row = document.createElement('div'); row.className = 'activity-row';
          var main = document.createElement('div'); main.className = 'activity-main';
          var actor = document.createElement('span'); actor.className = 'actor';
          actor.textContent = entry.actor || 'service';
          var meta = document.createElement('span'); meta.className = 'activity-meta';
          meta.textContent = ' · ' + new Date(entry.ts * 1000).toLocaleString('zh-CN') + ' · '
            + (actionNames[entry.action] || entry.action);
          var detail = document.createElement('code'); detail.textContent = entry.detail || '';
          main.appendChild(actor); main.appendChild(meta); main.appendChild(detail);
          row.appendChild(main); eventBox.appendChild(row);
        });
        if (!eventBox.children.length) eventBox.textContent = '尚无关键操作记录';
        var eventTotal = d.event_total === undefined ? (d.events || []).length : d.event_total;
        setPager('event', eventPage, eventTotal, (d.events || []).length);
      }).catch(function () {});
  }
  $('activity-prev').addEventListener('click', function () { if (sessionPage) { sessionPage--; loadActivity(); } });
  $('activity-next').addEventListener('click', function () { sessionPage++; loadActivity(); });
  $('event-prev').addEventListener('click', function () { if (eventPage) { eventPage--; loadActivity(); } });
  $('event-next').addEventListener('click', function () { eventPage++; loadActivity(); });
  $('terminal-search-next').addEventListener('click', function () { searchTerminal(1); });
  $('terminal-search-prev').addEventListener('click', function () { searchTerminal(-1); });
  $('b-serial-size').addEventListener('click', terminalView.syncSerialSize);
  $('b-redraw').addEventListener('click', redrawTerminal);
  $('b-info').addEventListener('click', function () {
    var open = $('detail').classList.toggle('info-open');
    $('b-info').setAttribute('aria-expanded', open ? 'true' : 'false');
    setTimeout(resizeTerm, 0);
  });
  $('b-expand').addEventListener('click', function () {
    var expanded = document.body.classList.toggle('terminal-max');
    $('b-expand-label').textContent = expanded ? '还原' : '放大';
    $('b-expand').title = expanded ? '还原工作台' : '放大终端';
    $('b-expand').setAttribute('aria-pressed', expanded ? 'true' : 'false');
    setTimeout(resizeTerm, 0);
  });
  $('terminal-search-scope').addEventListener('change', function () {
    $('terminal-search').placeholder = $('terminal-search-scope').value === 'boots'
      ? '搜索全部启动日志 · Enter' : '搜索终端 · Ctrl+F';
    $('terminal-search-status').textContent = '';
  });
  $('terminal-search').addEventListener('input', function () {
    if (terminalView.current) { terminalView.current.searchQuery = ''; terminalView.current.searchMatch = -1; terminalView.current.term.clearSelection(); }
    $('terminal-search-status').textContent = '';
  });
  $('terminal-search').addEventListener('keydown', function (event) {
    if (event.key === 'Enter') {
      if (event.preventDefault) event.preventDefault();
      searchTerminal(event.shiftKey ? -1 : 1);
    } else if (event.key === 'Escape') {
      if (event.preventDefault) event.preventDefault();
      clearTerminalSearch(true);
    }
  });
  document.addEventListener('keydown', function (event) {
    if (!deviceDirectory.selected || !(event.ctrlKey || event.metaKey) || String(event.key).toLowerCase() !== 'f') return;
    if (event.preventDefault) event.preventDefault();
    $('terminal-search').focus();
    if ($('terminal-search').select) $('terminal-search').select();
  });
  function setRecordsTab(name) {
    recordsTab = name;
    ['logs', 'inputs', 'sessions', 'events', 'tasks', 'commands'].forEach(function (tab) {
      var selected = tab === name;
      $('record-tab-' + tab).classList.toggle('on', selected);
      $('record-tab-' + tab).setAttribute('aria-selected', selected ? 'true' : 'false');
      $('record-panel-' + tab).hidden = !selected;
    });
    $('d-records').scrollTop = 0;
    $('b-records').setAttribute('aria-pressed', name === 'logs' && !$('d-records').hidden ? 'true' : 'false');
    $('b-tasks').setAttribute('aria-pressed', name === 'tasks' && !$('d-records').hidden ? 'true' : 'false');
    $('b-command-nav').setAttribute('aria-pressed', name === 'inputs' && !$('d-records').hidden ? 'true' : 'false');
    if (name === 'logs' && !$('d-records').hidden) loadLogs();
    if (name === 'tasks' && !$('d-records').hidden) taskUI.refresh();
    if (name === 'inputs' && terminalView.current) renderCommandNav(terminalView.current);
  }
  function setRecordsOpen(open) {
    $('d-records').hidden = !open;
    $('console-layout').classList.toggle('records-open', open);
    $('b-records').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) setRecordsTab(recordsTab);
    else {
      $('b-records').setAttribute('aria-pressed', 'false');
      $('b-command-nav').setAttribute('aria-pressed', 'false');
    }
    setTimeout(resizeTerm, 0);
  }
  function toggleRecordsTab(name) {
    if (!$('d-records').hidden && recordsTab === name) setRecordsOpen(false);
    else { recordsTab = name; setRecordsOpen(true); }
  }
  var recordTabs = ['logs', 'inputs', 'sessions', 'events', 'tasks', 'commands'];
  recordTabs.forEach(function (tab, index) {
    var button = $('record-tab-' + tab);
    button.addEventListener('click', function () { setRecordsTab(tab); });
    button.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      var offset = event.key === 'ArrowRight' ? 1 : -1;
      var next = recordTabs[(index + offset + recordTabs.length) % recordTabs.length];
      setRecordsTab(next);
      $('record-tab-' + next).focus();
    });
  });
  $('b-command-nav').addEventListener('click', function () {
    toggleRecordsTab('inputs');
  });
  $('b-file-transfer').addEventListener('click', function () {
    if (!deviceDirectory.selected) return;
    var selected = deviceDirectory.items.filter(function (device) { return device.key === deviceDirectory.selected; })[0];
    if (selected) SerialdFileTransfer.open(selected, deviceDirectory.items);
  });
  $('b-boot-compare').addEventListener('click', function () {
    if (!deviceDirectory.selected) return;
    var selected = deviceDirectory.items.filter(function (device) { return device.key === deviceDirectory.selected; })[0];
    if (selected) SerialdBootCompare.open(selected, deviceDirectory.items);
  });
  [-1, 1].forEach(function (step) {
    $('command-nav-' + (step < 0 ? 'prev' : 'next')).addEventListener('click', function () {
      var ctx = terminalView.current;
      if (!isCurrent(ctx)) return;
      renderCommandNav(ctx);
      if (!ctx.navEntries.length) return;
      var current = ctx.navSelected;
      if (current < 0) {
        var active = ctx.term.buffer.active;
        var visible = (active.viewportY || 0) + ctx.term.rows / 2;
        current = ctx.navEntries.findIndex(function (entry) { return entry.row >= visible; });
        if (current < 0) current = ctx.navEntries.length - 1;
        current -= step;
      }
      jumpToCommand(ctx, Math.max(0, Math.min(ctx.navEntries.length - 1, current + step)));
    });
  });
  $('b-tasks').addEventListener('click', function () { toggleRecordsTab('tasks'); });
  $('b-records').addEventListener('click', function () { toggleRecordsTab('logs'); });
  $('b-records-close').addEventListener('click', function () { setRecordsOpen(false); });
  /* ---------- 动作 ---------- */
  $('b-send').addEventListener('click', function () {
    var v = $('d-in').value; if (!v || !deviceDirectory.selected) return;
    send(v + '\r'); $('d-in').value = '';
  });
  $('d-in').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); $('b-send').click(); }
  });
  $('b-cr').addEventListener('click', function () { if (deviceDirectory.selected) send('\r'); });
  $('b-ctrlc').addEventListener('click', function () { if (deviceDirectory.selected) send('\x03'); });
  $('b-takeover').addEventListener('click', function () {
    var ctx = terminalView.current;
    if (!isCurrent(ctx) || !ctx.canTakeover) return;
    fetch('/api/session-takeover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, actor: 'web' }) })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (!isCurrent(ctx)) return;
        if (!result.ok) throw new Error(result.error || '介入失败');
        terminalView.updateState(ctx, result);
        if (typeof result.terminal_writable !== 'boolean') {
          ctx.readonly = false; ctx.canTakeover = false;
        }
        takenOver[ctx.sid] = !ctx.readonly;
        setInputMode(ctx);
        renderTabs();
        terminalView.resize();
        $('d-status').textContent = ' 已介入 SSH 会话，输入和输出继续记录';
      }).catch(function (error) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 介入失败：' + error.message;
      });
  });
  $('b-rename').addEventListener('click', function () {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    var cur = $('d-name').textContent;
    var v = window.prompt(window.SerialdI18n ? window.SerialdI18n.translate('修改设备标签（日志目录仍为设备序列号）') : '修改设备标签（日志目录仍为设备序列号）', cur);
    if (!v || !isCurrent(ctx)) return;
    fetch('/api/label', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, label: v }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        if (d.ok) { adoptKey(ctx, d.device_key || d.label, d.serial); pollDevices(); }
        else $('d-status').textContent = ' 改名失败：' + (d.error || '');
      }).catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 改名失败：' + e.message;
      });
  });
  $('b-switch').addEventListener('click', function () {
    var ctx = terminalView.current;
    if (!isCurrent(ctx) || !ctx.serial) return;
    $('d-status').textContent = ' 正在将当前串口转为待识别…';
    fetch('/api/switch-mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, actor: 'web' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        if (!d.ok) { $('d-status').textContent = ' 进入换板模式失败：' + (d.error || ''); return; }
        adoptKey(ctx, d.device_key, '');
        if (terminalView.current) $('d-status').textContent = ' 已进入换板识别模式；现在可切换开发板，启动到登录提示后将自动识别一次';
        pollDevices();
      }).catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 进入换板模式失败：' + e.message;
      });
  });
  $('b-rescue').addEventListener('click', function () { doRescue(); });

  $('b-input-owner').addEventListener('click', function () {
    var ctx = terminalView.current;
    if (!isCurrent(ctx) || ctx.sid !== 'serial') return;
    var owner = ctx.inputOwner;
    var release = ownsInput(owner);
    var payload = { key: ctx.key, serial: ctx.serial, actor: 'web' };
    if (release) payload.id = owner.id;
    else payload.takeover = !!owner;
    ctx.ownerPending = true;
    deviceDirectory.advanceOwnership();
    $('b-input-owner').disabled = true;
    fetch('/api/' + (release ? 'input-release' : 'input-claim'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (result) {
      if (!isCurrent(ctx)) return;
      if (!result.ok) { $('d-status').textContent = result.error || '输入占用操作失败'; return; }
      deviceDirectory.advanceOwnership();
      updateInputOwner(ctx, result.input_owner);
      var board = deviceDirectory.items.find(function (entry) { return entry.key === ctx.key; });
      if (board) board.input_owner = result.input_owner;
      $('d-status').textContent = release ? '已恢复共享串口输入' : '已占用串口输入；其他人仍可查看输出';
    }).catch(function (error) {
      if (isCurrent(ctx)) $('d-status').textContent = error.message;
    }).finally(function () {
      ctx.ownerPending = false;
      if (isCurrent(ctx)) $('b-input-owner').disabled = false;
    });
  });

  /* ---------- 识别设备 / 一键开 SSH ---------- */
  function renderInfo(info) {
    var box = $('d-info');
    if (!info || (!info.serial && !info.hostname && !(info.ips || []).length)) {
      box.hidden = true; $('b-info').disabled = true; return;
    }
    box.hidden = false; $('b-info').disabled = false;
    box.textContent = '';
    var sources = { 'eeprom-onie': '板载 EEPROM（ONIE TLV 0x23）',
      'uboot-injected': '板载 EEPROM（U-Boot 注入）',
      'soc-sysfs': 'SoC / eFuse（内核只读接口）', 'device-tree': '开发板固件',
      'boot-marker': '/boot 软件标记（随系统文件保留）',
      'verified-correction': '管理员核实并更正', 'verified-manual': '管理员核实' };
    [[info.identity_status === 'confirmed' ? '板卡序列号' : '候选身份编号', info.serial, true],
     ['身份来源', sources[info.serial_source] || '', false],
     ['身份状态', info.identity_status === 'confirmed' ? '已确认'
       : '待确认（直接读取 EEPROM 或匹配厂商固件约定后自动确认）', false],
     ['固件原始编号', info.reported_serial && info.reported_serial !== info.serial ? info.reported_serial : '', false],
     ['主机名', info.hostname, false],
     ['IP', (info.ips || []).join('   '), false], ['sshd', info.sshd, false],
     ['识别时间', info.ts ? new Date(info.ts * 1000).toLocaleString('zh-CN') : '', false]
    ].forEach(function (r) {
      if (!r[1]) return;
      var d = document.createElement('div');
      var k = document.createElement('span'); k.className = 'k'; k.textContent = r[0];
      var v = document.createElement('span'); v.className = 'v' + (r[2] ? ' hi' : '');
      if (['身份来源', '身份状态', '识别时间'].indexOf(r[0]) < 0) v.dataset.i18nIgnore = '';
      v.textContent = r[1];
      d.appendChild(k); d.appendChild(v);
      box.appendChild(d);
    });
  }
  function postAction(path, label) {
    var ctx = terminalView.current;
    if (!isCurrent(ctx)) return;
    $('d-status').textContent = ' ' + label + '…（要往板子控制台打字，最多可能等一两分钟）';
    taskUI.run(path.replace('/api/', ''), ctx)
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        if (!d.ok) { $('d-status').textContent = ' 失败：' + (d.error || ''); return; }
        if (d.serial !== undefined || d.ips !== undefined) {
          if (d.ok && d.device_key) adoptKey(ctx, d.device_key, d.serial);
          renderInfo(d); $('d-status').textContent = ' 识别完成';
        } else {
          $('d-status').textContent = ' 完成：ssh d=' + (d.sshd || '?');
        }
        pollDevices();
      })
      .catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 请求失败：' + e.message;
      });
  }
  $('b-identify').addEventListener('click', function () { postAction('/api/identify', '识别设备'); });
  function doRescue() {
    var ctx = terminalView.current;
    if (!isCurrent(ctx) || ctx.reconnecting) return;
    ctx.reconnecting = true;
    $('b-rescue').disabled = true;
    $('d-status').textContent = ' 正在重新连接串口（USB 软拔插）…';
    fetch('/api/rescue', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, mode: 'rebind' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        $('d-status').textContent = ' ' + (d.recovered ? '采集已恢复' :
          (d.ok ? '步骤完成，尚未确认采集恢复' : '自愈失败')) + (d.note ? '：' + d.note : '');
        if (d.hint) { $('d-hint').hidden = false; $('d-hint').textContent = d.hint; }
        else $('d-hint').hidden = true;
        pollDevices();
      })
      .catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 自愈请求失败：' + e.message;
      }).finally(function () {
        ctx.reconnecting = false;
        if (isCurrent(ctx)) $('b-rescue').disabled = false;
      });
  }
  $('d-term').addEventListener('click', function () { if (terminalView.current) terminalView.current.term.focus(); });
  $('b-close').addEventListener('click', function () {
    disposeView(); deviceDirectory.selectionChanged(null); $('detail').hidden = true;
    $('workspace').classList.toggle('selected', false);
    document.body.classList.toggle('detail-open', false);
    document.body.classList.remove('terminal-max');
    $('b-expand-label').textContent = '放大';
    $('b-expand').title = '放大终端';
    $('b-expand').setAttribute('aria-pressed', 'false');
    markSelected(); pollDevices();
  });

  pollDevices();
  setInterval(pollDevices, 3000);
  archives.startRefresh();
})();
