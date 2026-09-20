(function () {
  'use strict';
  var UI = window.SerialdUI, $ = UI.byId, fmt = UI.formatBytes, ago = UI.age, copyText = UI.copyText;
  var selKey = null, lastDevices = [], loopGen = 0, lastInfoTs = 0;
  var curSid = 'serial';              // 当前会话:serial=串口,sshN=SSH 会话
  var sessions = [{ sid: 'serial', kind: 'serial' }];
  var view = null, term = null;
  var activeLog = null, logTabs = [], sessionPage = 0, eventPage = 0;
  var ACTIVITY_PAGE_SIZE = 10;
  var takenOver = {};

  function setInputMode(ctx) {
    ['d-in', 'd-kbd', 'b-send', 'b-cr', 'b-ctrlc', 'b-paste'].forEach(function (id) {
      $(id).disabled = ctx.readonly;
    });
    $('b-takeover').hidden = !ctx.readonly;
  }

  function isCurrent(ctx) {
    return !!ctx && view === ctx && ctx.gen === loopGen
      && ctx.key === selKey && ctx.sid === curSid;
  }

  function disposeView() {
    var old = view;
    view = null; term = null; loopGen++;
    if (!old) return;
    if (old.ctrl) old.ctrl.abort();
    clearTimeout(old.loopTimer);
    clearTimeout(old.sizeTimer);
    clearTimeout(old.selTimer);
    // reset() 不会清空 xterm 的异步解析队列；旧 CSI 6n 还可能触发 onData，
    // 把上一台的终端应答写到新设备。销毁实例，并让所有旧回调检查所属视图。
    old.term.dispose();
  }

  /* ---------- 终端模拟器 ---------- */
  function createView(key, sid) {
    disposeView();
    selKey = key; curSid = sid;
    $('b-rebind').hidden = true;
    var board = lastDevices.filter(function (d) { return d.key === key; })[0] || {};
    var selectedSession = sessions.filter(function (s) { return s.sid === sid; })[0] || {};
    var ctx = { key: key, serial: board.serial, connectionId: board.connection_id,
                byPath: board.by_path || board.last_path,
                readonly: sid !== 'serial' && !!selectedSession.actor && selectedSession.actor !== 'web' && !takenOver[sid],
                sid: sid, gen: loopGen, off: 0, sessionsSeq: 0, activitySeq: 0,
                sendTail: Promise.resolve(), lastSel: '' };
    view = ctx;
    setInputMode(ctx);
    term = ctx.term = new Terminal({
      convertEol: false,          // 串口/pty 发的是真 \r\n,不需要转换
      scrollback: 8000,
      cursorBlink: true,
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: 12.5,
      theme: { background: '#0A0D0F', foreground: '#C8D0D6', cursor: '#7FE0D4',
               selectionBackground: '#2C5B57' }
    });
    ctx.fit = new FitAddon.FitAddon();
    ctx.term.loadAddon(ctx.fit);
    $('d-term').textContent = '';
    ctx.term.open($('d-term'));
    SerialdTerminalPolicy.install(ctx, {
      isCurrent: isCurrent,
      keyboardEnabled: function () { return $('d-kbd').checked; },
      send: send,
      copyText: copyText,
      copySelection: copySel
    });
    return ctx;
  }

  function resizeTerm() {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    try { ctx.fit.fit(); } catch (e) {}
    if (window.requestAnimationFrame) window.requestAnimationFrame(function () {
      if (!isCurrent(ctx)) return;
      try { ctx.fit.fit(); ctx.term.refresh(0, ctx.term.rows - 1); } catch (e) {}
      pushSize(ctx);
    });
    pushSize(ctx);
  }
  // 把浏览器终端尺寸推给后端,后端 ioctl 到 pty —— 否则远端以为自己是 80x24,
  // 宽度用不满,vi/htop 也按错误列数画。串口那条没法协商(线路上没有 window-change)。
  function pushSize(ctx) {
    if (!isCurrent(ctx) || ctx.sid === 'serial' || ctx.readonly) return;
    clearTimeout(ctx.sizeTimer);
    ctx.sizeTimer = setTimeout(function () {
      if (!isCurrent(ctx)) return;
      fetch('/api/session-resize', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, cols: ctx.term.cols, rows: ctx.term.rows })
      }).catch(function () {});
    }, 250);
  }
  window.addEventListener('resize', resizeTerm);
  // 后台标签页里 rAF 被暂停,xterm 渲染器停摆;切回来要重算尺寸 + 强制重绘,
  // 否则表现为"切走再切回来终端就不渲染了,得刷新"(踩过)
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      resizeTerm();
      if (term) { try { term.refresh(0, term.rows - 1); } catch (e) {} }
    }
  });

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
    if (!text) { $('d-status').textContent = ' 剪贴板里没有文本（终端里 Ctrl+C 是中断信号，复制请用 Ctrl+Shift+C 或拖选）'; return; }
    send(text, ctx);
    $('d-status').textContent = ' 已粘贴 ' + text.length + ' 字符';
  }
  // 原生粘贴(Ctrl+V / 中键)在终端里也要能送到板子 —— 不受"键盘直通"开关影响。
  // 挂在 document 上而不是 d-term 上：焦点不在终端里时(例如从别处复制完切回浏览器、
  // 没先点一下终端就按 Ctrl+V)，paste 事件的目标是 body，挂在 d-term 上的监听器
  // 根本收不到,表现就是"按了完全没反应"。但同时要给页面自己的输入框让路。
  document.addEventListener('paste', function (e) {
    if (!selKey) return;
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')
        && String(ae.className).indexOf('xterm-helper-textarea') < 0) return;   // 发送命令框等,照常粘贴
    var cb = e.clipboardData || window.clipboardData;
    var t = cb && cb.getData ? cb.getData('text') : '';
    e.preventDefault(); e.stopPropagation();
    pasteInto(t, view);
  }, true);
  // 「粘贴」按钮:借一个临时输入框接住 Ctrl+V —— 非安全上下文下拿剪贴板的可靠办法
  $('b-paste').addEventListener('click', function () {
    var ctx = view;
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
  function renderDevices(list) {
    $('count-online').textContent = list.filter(function (d) { return d.serial && d.alive; }).length;
    $('count-history').textContent = list.filter(function (d) { return d.serial && !d.alive; }).length;
    $('count-pending').textContent = list.filter(function (d) { return d.state === 'pending'; }).length;
    var query = $('device-search').value.toLowerCase();
    var filter = $('device-filter').value || 'all';
    list = list.filter(function (d) {
      return (filter === 'all' || d.state === filter) &&
        [d.label, d.serial, d.hostname, d.by_path, d.last_path, d.dev].join(' ').toLowerCase().includes(query);
    });
    var g = $('grid');
    if (!list.length) { g.innerHTML = '<p class="empty">没有匹配的设备。新串口连接后将显示在待识别列表。</p>'; return; }
    g.textContent = '';
    list.forEach(function (d) {
      var card = document.createElement('div');
      card.className = 'dev' + (!d.alive ? ' offline' : '') + (d.key === selKey ? ' sel' : '');
      card.dataset.key = d.key;          // 供 markSelected() 就地改高亮用
      card.tabIndex = 0;
      card.addEventListener('click', function () { select(d.key); });
      card.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(d.key); }
      });

      var r1 = document.createElement('div'); r1.className = 'r1';
      var nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = d.state === 'pending' ? '待写序列号 · ' + (d.hostname || (d.dev || '').split('/').pop()) : d.label;
      var st = document.createElement('span');
      st.className = 'pill ' + (d.alive ? 'live' : 'dead');
      st.textContent = d.state === 'pending' ? 'USB 临时身份' : (d.alive ? '在线' : '历史设备');
      r1.appendChild(nm); r1.appendChild(st);
      if (d.hostname) {
        var sn = document.createElement('span'); sn.className = 'pill sniff';
        sn.textContent = '嗅探:' + d.hostname; r1.appendChild(sn);
      }
      card.appendChild(r1);

      var meta = document.createElement('div'); meta.className = 'meta';
      var a = document.createElement('span'); a.textContent = d.dev || ('上次连接 ' + (d.last_dev || '—'));
      var b = document.createElement('span'); b.textContent = d.serial ? 'SN ' + d.serial : (d.by_path || '尚未绑定开发板序列号');
      meta.appendChild(a); meta.appendChild(b);
      card.appendChild(meta);

      var last = document.createElement('div'); last.className = 'last';
      last.textContent = d.last_line || '（暂无输出）';
      card.appendChild(last);

      var stat = document.createElement('div'); stat.className = 'stat';
      var s1 = document.createElement('span'); s1.textContent = fmt(d.bytes) + ' 记录';
      var s2 = document.createElement('span'); s2.textContent = '末次 ' + ago(d.last_rx || d.last_seen);
      var s3 = document.createElement('span'); s3.textContent = d.by_path || d.last_path || '';
      stat.appendChild(s1); stat.appendChild(s2); stat.appendChild(s3);
      card.appendChild(stat);

      if (d.rescue_note) {                       // 自愈结果挂到卡片上,别让人去翻日志
        var rn = document.createElement('div'); rn.className = 'last';
        rn.style.color = 'var(--dead)';
        rn.textContent = d.rescue_note;
        card.appendChild(rn);
      }
      var inf = d.info || {};
      if (d.identity_error || d.needs_provisioning || inf.identity_status === 'provisional') {
        var warning = document.createElement('div'); warning.className = 'identity-error';
        warning.textContent = (d.reported_serial ? 'SN ' + d.reported_serial + '：' : '') +
          (d.identity_error || (d.needs_provisioning
            ? 'EEPROM 尚未写入序列号；当前身份仅作过渡，请写入 ONIE TLV 0x23'
            : '该身份来源尚未正式确认；确认后迁入正式序列号档案'));
        card.appendChild(warning);
      }
      // 板子序列号(device-tree)比 hostname 可靠:hostname 可能是出厂默认名、还会重复
      if (inf.ips && inf.ips.length) {
        var idl = document.createElement('div'); idl.className = 'stat';
        if (inf.ips && inf.ips.length) {
          var s5 = document.createElement('span');
          s5.textContent = inf.ips.map(function (x) { return x.split(' ')[1].split('/')[0]; }).join(' ');
          idl.appendChild(s5);
        }
        card.appendChild(idl);
      }

      g.appendChild(card);
    });
  }

  function pollDevices() {
    fetch('/api/devices', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        $('conn-dot').className = 'dot';
        $('conn-txt').textContent = '已连接 · ' + (d.devices || []).length + ' 台设备 · '
          + new Date().toLocaleTimeString('zh-CN');
        lastDevices = d.devices || [];
        renderDevices(lastDevices);
        // 轮询里同步选中设备的识别结果,但只在**结果真的变了**时重绘 ——
        // 否则每 3 秒重建一次 DOM 会打断用户选中文本
        if (selKey) {
          var cur = lastDevices.filter(function (x) { return view && view.serial ? x.serial === view.serial : (view && view.connectionId ? x.connection_id === view.connectionId : x.key === selKey); })[0];
          // A collector restart changes connection_id.  If a pending connection was
          // promoted meanwhile, follow the same live physical attachment to its
          // canonical key instead of leaving an unusable stale detail panel open.
          if (!cur && view && !view.serial && view.byPath) {
            var samePath = lastDevices.filter(function (x) {
              return x.alive && x.by_path === view.byPath;
            });
            if (samePath.length === 1) cur = samePath[0];
          }
          if (cur && cur.key !== selKey) adoptKey(view, cur.key, cur.serial);
          var ts = cur && cur.info ? cur.info.ts : 0;
          if (ts !== lastInfoTs) { lastInfoTs = ts; renderInfo(cur && cur.info); }
        }
        loadSessions();
        loadActivity();
      })
      .catch(function () {
        $('conn-dot').className = 'dot bad';
        $('conn-txt').textContent = '服务不可达';
      });
  }

  /* ---------- 详情 / 串口实时 ---------- */
  // 选中高亮就地更新 —— 不能等下一次 pollDevices()(3 秒一轮),
  // 否则会出现"终端已经切过去了,卡片高亮还停在上一台"的迟滞感
  function markSelected() {
    var cards = $('grid').children;
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (c.dataset && c.dataset.key) {
        c.classList.toggle('sel', c.dataset.key === selKey);
      }
    }
  }

  function select(key) {
    var ctx = createView(key, 'serial');
    // 切设备时立即清掉旧会话标签；迟到的列表/动作响应不能借用新 selKey。
    sessions = [{ sid: 'serial', kind: 'serial' }];
    var d = (lastDevices || []).filter(function (x) { return x.key === key; })[0];
    $('detail').hidden = false;
    $('workspace').classList.toggle('selected', true);
    $('d-name').textContent = d ? d.label : key;
    $('d-dev').textContent = d ? d.dev : '';
    $('d-key').textContent = d && d.serial ? 'SN ' + d.serial : key;
    $('b-switch').hidden = !(d && d.serial && d.alive);
    $('d-status').textContent = '';
    $('d-hint').hidden = true;
    $('d-in').value = '';
    $('d-logs').textContent = '';
    $('log-viewer').hidden = true; activeLog = null; logTabs = []; sessionPage = 0; eventPage = 0;
    $('d-activity').textContent = '';
    $('d-events').textContent = '';
    loadActivity();
    lastInfoTs = d && d.info ? d.info.ts : 0;
    resizeTerm();
    loadLogs();
    renderInfo(d && d.info);
    markSelected();                    // 高亮立刻跟上,不等轮询
    renderTabs();
    loadSessions();
    // 关键:作废在途请求并立刻开新轮询。否则旧请求还挂在上一台设备上长轮询(最多 15s),
    // 切过去就得干等它返回 —— 这正是"切设备很慢"的原因。
    loop(ctx);
  }
  function loop(ctx) {
    if (!isCurrent(ctx)) return;
    ctx.ctrl = new AbortController();
    fetch('/api/session?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '') + '&sid=' + encodeURIComponent(ctx.sid)
          + '&off=' + ctx.off, { cache: 'no-store', signal: ctx.ctrl.signal })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;           // 切换后到达的旧响应,丢弃
        if (!d || d.error) throw new Error(d && d.error || 'error');
        if ((d.key && d.key !== ctx.key) || (d.sid && d.sid !== ctx.sid)) {
          throw new Error('会话响应与请求不匹配');
        }
        ctx.off = d.off;
        $('d-dev').textContent = d.dev || d.ip || '';
        // 原始字节直接交给 xterm —— 光标定位、颜色、备用屏幕缓冲都由它处理,
        // vim/htop 这类全屏程序才能正确重画(剥 ANSI 再追加会糊成一团)
        // JSON 每块先转字符串会截断跨块 UTF-8（中文尤其明显）。传原始字节，
        // 由此会话自己的 xterm 解码器保留半个字符；切会话时解码器也一起销毁。
        if (typeof d.data_b64 === 'string') {
          var raw = atob(d.data_b64);
          if (raw.length) ctx.term.write(Uint8Array.from(raw, function (c) { return c.charCodeAt(0); }), function () { ctx.initialRendered = true; });
          else ctx.initialRendered = true;
        } else if (d.data) ctx.term.write(d.data, function () { ctx.initialRendered = true; });   // 兼容仍在运行的旧服务
      })
      .catch(function (e) {
        if (!isCurrent(ctx) || (e && e.name === 'AbortError')) return;
        $('d-status').textContent = ' 读取失败：' + e.message;
      })
      .finally(function () {
        if (!isCurrent(ctx)) return;
        // 定时器本身也必须绑定视图，否则旧定时器会多开一条新会话轮询。
        ctx.loopTimer = setTimeout(function () { loop(ctx); }, 300);
      });
  }

  function switchSession(sid) {
    if (!selKey || sid === curSid) return;
    var ctx = createView(selKey, sid);
    $('d-status').textContent = '';
    $('d-in').value = '';
    resizeTerm();
    renderTabs();
    loadSessions();
    loadLogs();
    loop(ctx);
  }
  function renderTabs() {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    var bar = $('d-tabs');
    bar.textContent = '';
    sessions.forEach(function (s) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = (s.sid === curSid ? 'on' : '');
      b.textContent = (s.kind === 'serial' ? '串口' : ('SSH ' + (s.actor ? s.actor + ' · ' : '') + (s.ip || s.sid)));
      if (s.actor && s.actor !== 'web') b.textContent += ' · 只读';
      b.title = s.kind === 'serial' ? '板子控制台(单读者采集,带历史日志)'
                                    : 'SSH 会话,独立记录到 ssh-*.log';
      b.addEventListener('click', function () { if (isCurrent(ctx)) switchSession(s.sid); });
      bar.appendChild(b);
      if (s.kind === 'ssh' && (!s.actor || s.actor === 'web')) {
        var x = document.createElement('button');
        x.type = 'button'; x.className = 'x'; x.textContent = '×'; x.title = '关闭这条 SSH 会话';
        x.addEventListener('click', function (e) {
          e.stopPropagation();
          if (!isCurrent(ctx)) return;
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
    add.title = '使用上位机配置的账户创建独立 SSH 会话（没有 IP 会先识别）';
    add.addEventListener('click', function () {
      if (!isCurrent(ctx) || ctx.connecting) return;
      ctx.connecting = true; add.disabled = true;
      $('d-status').textContent = ' 正在建立 SSH 会话…（没识别过 IP 的话会先跑一次识别）';
      fetch('/api/ssh-connect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: ctx.key, serial: ctx.serial, actor: 'web' }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!isCurrent(ctx)) return;
          ctx.connecting = false; add.disabled = false;
          if (!d.ok) { $('d-status').textContent = ' 失败：' + (d.error || ''); return; }
          if (d.device_key) adoptKey(ctx, d.device_key, d.serial);
          if (!sessions.some(function (s) { return s.sid === d.sid; })) {
            sessions.push({ sid: d.sid, kind: 'ssh', ip: d.ip });
          }
          switchSession(d.sid);
          $('d-status').textContent = ' 已连接 ' + d.ip;
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
    var ctx = view;
    if (!isCurrent(ctx)) return;
    var seq = ++ctx.sessionsSeq;
    fetch('/api/sessions?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || ''), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx) || seq !== ctx.sessionsSeq || !d.sessions
            || (d.key && d.key !== ctx.key)) return;
        sessions = d.sessions;
        // 当前会话若已不存在(比如被别的窗口关了),退回串口
        if (!sessions.some(function (s) { return s.sid === curSid; })) {
          switchSession('serial'); return;
        }
        renderTabs();
      })
      .catch(function () {});
  }

  function send(text, ctx) {
    ctx = ctx || view;
    if (!isCurrent(ctx)) return;
    if (ctx.readonly) { $('d-status').textContent = ' 当前正在只读查看 agent 会话'; return; }
    // 连续按键若并发发 HTTP，后发请求可能先写入。按视图串行发送，切换后
    // 丢弃尚未发出的输入；已在途请求始终保留原 key/sid，绝不改投新设备。
    ctx.sendTail = ctx.sendTail.then(function () {
      if (!isCurrent(ctx)) return;
      return fetch('/api/session-send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, data: text }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (isCurrent(ctx) && !d.ok) $('d-status').textContent = ' 写入失败：' + (d.error || '');
      })
      .catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 写入失败：' + e.message;
      });
    });
  }

  function loadLogs() {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    fetch('/api/logs?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || ''), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx) || (d.key && d.key !== ctx.key)) return;
        var box = $('d-logs');
        box.textContent = '';
        var allFiles = d.files || [];
        var files = allFiles.filter(function (file) {
          return (file.kind || (file.name.indexOf('ssh-') === 0 ? 'ssh' : 'serial')) === 'serial';
        });
        if (!files.length) {
          box.textContent = '（还没有串口日志文件）'; return;
        }
        $('log-location').textContent = d.archive ? 'logs/' + d.archive + '/' : '待识别连接的临时日志';
        files.forEach(function (f) {
          var row = document.createElement('div'); row.className = 'log-row';
          var date = document.createElement('span'); date.className = 'log-date';
          date.textContent = f.date || '日期未知';
          var a = document.createElement('button');
          a.type = 'button';
          a.addEventListener('click', function () { if (isCurrent(ctx)) showLog(ctx, f.name); });
          a.textContent = f.name;
          var size = document.createElement('span'); size.className = 'log-size'; size.textContent = fmt(f.size);
          row.appendChild(date); row.appendChild(a); row.appendChild(size); box.appendChild(row);
        });
        var sshCount = allFiles.length - files.length;
        if (sshCount) {
          var note = document.createElement('div'); note.className = 'log-note';
          note.textContent = sshCount + ' 份 SSH transcript 请从上方对应会话的“查看记录”打开。';
          box.appendChild(note);
        }
      }).catch(function (e) {
        if (isCurrent(ctx)) $('d-logs').textContent = '读取日志列表失败：' + e.message;
      });
  }

  function adoptKey(ctx, key, serial) {
    if (!isCurrent(ctx) || !key || key === ctx.key) return;
    lastDevices.forEach(function (d) { if (d.key === ctx.key) { d.key = key; d.label = key; d.serial = serial; } });
    select(key);
  }

  function renderLogTabs() {
    var bar = $('log-tabs'); bar.textContent = '';
    logTabs.forEach(function (tab) {
      var button = document.createElement('button'); button.type = 'button';
      button.className = tab === activeLog ? 'on' : '';
      button.textContent = tab.name;
      button.addEventListener('click', function () {
        if (!isCurrent(tab.ctx)) return;
        activeLog = tab; renderLogTabs(); renderLogTab(tab, true);
      });
      bar.appendChild(button);
      var close = document.createElement('button'); close.type = 'button'; close.className = 'x';
      close.textContent = '×'; close.title = '关闭这个日志标签';
      close.addEventListener('click', function () { closeLogTab(tab); });
      bar.appendChild(close);
    });
  }

  function renderLogTab(tab, scroll) {
    if (!tab || !isCurrent(tab.ctx)) return;
    $('log-viewer').hidden = false;
    $('log-title').textContent = tab.name;
    $('log-content').textContent = tab.error || (tab.loaded ? tab.data : '读取中…');
    $('log-range').textContent = tab.loaded && !tab.error
      ? '字节 ' + tab.startOff + '–' + tab.off + ' / ' + tab.size : '';
    $('log-more').disabled = !tab.more;
    if (scroll && $('log-viewer').scrollIntoView) {
      $('log-viewer').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function closeLogTab(tab) {
    var index = logTabs.indexOf(tab);
    if (index < 0) return;
    logTabs.splice(index, 1);
    if (activeLog === tab) activeLog = logTabs[Math.min(index, logTabs.length - 1)] || null;
    renderLogTabs();
    if (activeLog) renderLogTab(activeLog, false); else $('log-viewer').hidden = true;
  }

  function showLog(ctx, name, off, append, force) {
    if (!isCurrent(ctx)) return;
    var tab = logTabs.filter(function (item) { return item.name === name; })[0];
    if (!tab) { tab = { ctx: ctx, name: name, seq: 0, loaded: false, data: '' }; logTabs.push(tab); }
    activeLog = tab; renderLogTabs(); renderLogTab(tab, true);
    if (tab.loaded && off === undefined && !append && !force) return;
    var seq = ++tab.seq;
    if (!append) { tab.error = ''; tab.loaded = false; renderLogTab(tab, false); }
    fetch('/api/log?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '')
      + '&file=' + encodeURIComponent(name) + (off === undefined ? '' : '&off=' + off))
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!isCurrent(ctx) || seq !== tab.seq) return;
        if (d.error) throw new Error(d.error);
        tab.data = (append ? tab.data : '') + d.data;
        tab.startOff = append ? tab.startOff : d.start_off;
        tab.off = d.off; tab.size = d.size; tab.more = d.more; tab.loaded = true; tab.error = '';
        if (activeLog === tab) renderLogTab(tab, false);
      }).catch(function (e) {
        if (!isCurrent(ctx) || seq !== tab.seq) return;
        tab.error = e.message; tab.loaded = true; tab.more = false;
        if (activeLog === tab) renderLogTab(tab, false);
      });
  }

  function setPager(prefix, page, total, count) {
    var pages = Math.max(1, Math.ceil(total / ACTIVITY_PAGE_SIZE));
    $(prefix + '-page').textContent = '第 ' + (page + 1) + ' / ' + pages + ' 页 · ' + total + ' 条';
    $(prefix + '-prev').disabled = page === 0;
    $(prefix + '-next').disabled = (page * ACTIVITY_PAGE_SIZE + count) >= total;
  }

  function loadActivity() {
    var ctx = view;
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
                if (file) showLog(ctx, file.name); else $('d-status').textContent = ' 该会话没有可用日志';
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
          'switch-armed': '准备更换设备' };
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
  $('device-search').addEventListener('input', function () { renderDevices(lastDevices); });
  $('device-filter').addEventListener('change', function () { renderDevices(lastDevices); });
  $('activity-prev').addEventListener('click', function () { if (sessionPage) { sessionPage--; loadActivity(); } });
  $('activity-next').addEventListener('click', function () { sessionPage++; loadActivity(); });
  $('event-prev').addEventListener('click', function () { if (eventPage) { eventPage--; loadActivity(); } });
  $('event-next').addEventListener('click', function () { eventPage++; loadActivity(); });
  $('log-close').addEventListener('click', function () { if (activeLog) closeLogTab(activeLog); });
  $('log-refresh').addEventListener('click', function () { if (activeLog) showLog(activeLog.ctx, activeLog.name, undefined, false, true); });
  $('log-more').addEventListener('click', function () { if (activeLog) showLog(activeLog.ctx, activeLog.name, activeLog.off, true); });

  /* ---------- 动作 ---------- */
  $('b-send').addEventListener('click', function () {
    var v = $('d-in').value; if (!v || !selKey) return;
    send(v + '\r'); $('d-in').value = '';
  });
  $('d-in').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); $('b-send').click(); }
  });
  $('b-cr').addEventListener('click', function () { if (selKey) send('\r'); });
  $('b-ctrlc').addEventListener('click', function () { if (selKey) send('\x03'); });
  $('b-takeover').addEventListener('click', function () {
    var ctx = view;
    if (!isCurrent(ctx) || !ctx.readonly || ctx.sid === 'serial') return;
    fetch('/api/session-takeover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, actor: 'web' }) })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (!isCurrent(ctx)) return;
        if (!result.ok) throw new Error(result.error || '介入失败');
        takenOver[ctx.sid] = true;
        ctx.readonly = false;
        setInputMode(ctx);
        $('d-status').textContent = ' 已介入 agent SSH，会话输入和输出继续记录';
      }).catch(function (error) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 介入失败：' + error.message;
      });
  });
  $('b-rename').addEventListener('click', function () {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    var cur = $('d-name').textContent;
    var v = window.prompt('修改设备标签（日志目录仍为设备序列号）', cur);
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
    var ctx = view;
    if (!isCurrent(ctx) || !ctx.serial) return;
    $('d-status').textContent = ' 正在将当前串口转为待识别…';
    fetch('/api/switch-mode', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, actor: 'web' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        if (!d.ok) { $('d-status').textContent = ' 进入换板模式失败：' + (d.error || ''); return; }
        adoptKey(ctx, d.device_key, '');
        if (view) $('d-status').textContent = ' 已进入换板识别模式；现在可切换开发板，启动到登录提示后将自动识别一次';
        pollDevices();
      }).catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 进入换板模式失败：' + e.message;
      });
  });
  // helper 完成设置不等于采集已经恢复；不能因响应里缺少 hint 就自动软拔插。
  // 先报告恢复状态，再由用户明确选择会中断 USB 连接的重新绑定。
  $('b-rescue').addEventListener('click', function () { doRescue('soft'); });
  $('b-rebind').addEventListener('click', function () { doRescue('rebind'); });

  /* ---------- 识别设备 / 一键开 SSH ---------- */
  function renderInfo(info) {
    var box = $('d-info');
    if (!info || (!info.serial && !info.hostname && !(info.ips || []).length)) {
      box.hidden = true; return;
    }
    box.hidden = false;
    box.textContent = '';
    var sources = { 'eeprom-onie': '板载 EEPROM（ONIE TLV 0x23）',
      'soc-sysfs': 'SoC / eFuse（内核只读接口）', 'device-tree': '开发板固件',
      'boot-marker': '/boot 软件标记（随系统文件保留）',
      'verified-correction': '管理员核实并更正', 'verified-manual': '管理员核实' };
    [[info.identity_status === 'confirmed' ? '板卡序列号' : '候选身份编号', info.serial, true],
     ['身份来源', sources[info.serial_source] || '', false],
     ['身份状态', info.identity_status === 'confirmed' ? '已确认' : '待确认（使用临时档案目录）', false],
     ['固件原始编号', info.reported_serial && info.reported_serial !== info.serial ? info.reported_serial : '', false],
     ['主机名', info.hostname, false],
     ['IP', (info.ips || []).join('   '), false], ['sshd', info.sshd, false],
     ['识别时间', info.ts ? new Date(info.ts * 1000).toLocaleString('zh-CN') : '', false]
    ].forEach(function (r) {
      if (!r[1]) return;
      var d = document.createElement('div');
      var k = document.createElement('span'); k.className = 'k'; k.textContent = r[0];
      var v = document.createElement('span'); v.className = 'v' + (r[2] ? ' hi' : '');
      v.textContent = r[1];
      d.appendChild(k); d.appendChild(v);
      box.appendChild(d);
    });
  }
  function postAction(path, label) {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    $('d-status').textContent = ' ' + label + '…（要往板子控制台打字，最多可能等一两分钟）';
    fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, actor: 'web' }) })
      .then(function (r) { return r.json(); })
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
  function doRescue(mode, ctx) {
    ctx = ctx || view;
    if (!isCurrent(ctx)) return;
    $('d-status').textContent = ' 正在自愈(' + (mode === 'soft' ? '关挂起' : '软拔插') + ')…';
    fetch('/api/rescue', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, mode: mode }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        $('d-status').textContent = ' ' + (d.recovered ? '采集已恢复' :
          (d.ok ? '步骤完成，尚未确认采集恢复' : '自愈失败')) + (d.note ? '：' + d.note : '');
        if (d.hint) { $('d-hint').hidden = false; $('d-hint').textContent = d.hint; }
        else $('d-hint').hidden = true;
        $('b-rebind').hidden = !!d.recovered || !!d.requires_setup;
        pollDevices();
      })
      .catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 自愈请求失败：' + e.message;
      });
  }
  $('d-term').addEventListener('click', function () { if (term) term.focus(); });
  $('b-close').addEventListener('click', function () {
    disposeView(); selKey = null; $('detail').hidden = true;
    $('workspace').classList.toggle('selected', false);
    markSelected(); pollDevices();
  });

  pollDevices();
  setInterval(pollDevices, 3000);
})();
