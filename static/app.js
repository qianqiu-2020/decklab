(function () {
  'use strict';
  var UI = window.SerialdUI, $ = UI.byId, fmt = UI.formatBytes, ago = UI.age, copyText = UI.copyText;
  var selKey = null, lastDevices = [], loopGen = 0, lastInfoTs = 0, lastInfoIdentity = '';
  var curSid = 'serial';              // serial、sshN 或本地只读 log:* 标签
  var sessions = [{ sid: 'serial', kind: 'serial' }];
  var view = null, term = null;
  var sessionPage = 0, eventPage = 0;
  var ACTIVITY_PAGE_SIZE = 10;
  var DEFAULT_TERMINAL_FONT_SIZE = 14.5;
  var takenOver = {};

  function setInputMode(ctx) {
    ['d-in', 'd-kbd', 'd-slow-paste', 'b-send', 'b-cr', 'b-ctrlc', 'b-paste'].forEach(function (id) {
      $(id).disabled = ctx.readonly;
    });
    $('slow-paste-option').hidden = ctx.sid !== 'serial';
    $('b-takeover').hidden = !ctx.canTakeover;
  }

  function isCurrent(ctx) {
    return !!ctx && view === ctx && ctx.gen === loopGen
      && ctx.key === selKey && ctx.sid === curSid;
  }

  function disposeView() {
    var old = view;
    view = null; term = null; loopGen++;
    if (!old) return;
    if (old.scrollHandler && $('d-term').removeEventListener) {
      $('d-term').removeEventListener('click', old.scrollHandler, true);
    }
    if (old.wheelHandler && $('d-term').removeEventListener) {
      $('d-term').removeEventListener('wheel', old.wheelHandler, false);
    }
    if (old.ctrl) old.ctrl.abort();
    clearTimeout(old.loopTimer);
    clearTimeout(old.inputTimer);
    clearTimeout(old.sizeTimer);
    clearTimeout(old.selTimer);
    if (old.refreshFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(old.refreshFrame);
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
    var isAgentSession = selectedSession.kind === 'ssh' && !!selectedSession.actor
      && selectedSession.actor !== 'web' && !takenOver[sid];
    var ctx = { key: key, serial: board.serial, connectionId: board.connection_id,
                byPath: board.by_path || board.last_path,
                readonly: selectedSession.kind === 'log' || isAgentSession,
                canTakeover: isAgentSession, kind: selectedSession.kind || 'serial',
                sid: sid, gen: loopGen, off: 0, readSeq: 0, sessionsSeq: 0, activitySeq: 0,
                inputQueue: [], inputSending: false, inputTimer: 0,
                lastSel: '', searchQuery: '', searchMatch: -1,
                serialRows: 24, bootScanTail: '', phaseScanTail: '', linuxConsole: false };
    view = ctx;
    setInputMode(ctx);
    term = ctx.term = new Terminal({
      // 归档文件常用单独 LF；xterm 的 LF 默认不回到第 1 列，会呈现阶梯状。
      // 实时串口/pty 保留原始 CR/LF，避免改变全屏终端协议。
      convertEol: ctx.kind === 'log',
      scrollback: 8000,
      cursorBlink: !ctx.readonly,
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: DEFAULT_TERMINAL_FONT_SIZE,
      theme: { background: '#0A0D0F', foreground: '#C8D0D6', cursor: '#7FE0D4',
               selectionBackground: '#2C5B57' }
    });
    ctx.fit = new FitAddon.FitAddon();
    ctx.term.loadAddon(ctx.fit);
    $('d-term').textContent = '';
    ctx.term.open($('d-term'));
    // Some Chromium/Linux themes expose an overlay scrollbar whose track
    // accepts wheel input but ignores a track click.  Keep native dragging,
    // and provide the expected page jump when the click lands on xterm's
    // viewport edge.
    ctx.scrollHandler = function (event) {
      if (!isCurrent(ctx) || !event || !event.target || !event.target.classList
          || !event.target.classList.contains('xterm-viewport')) return;
      var viewport = event.target;
      var rect = viewport.getBoundingClientRect && viewport.getBoundingClientRect();
      if (!rect || event.clientX < rect.right - 24 || !viewport.scrollHeight) return;
      var ratio = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
      viewport.scrollTop = Math.max(0, ratio * viewport.scrollHeight - viewport.clientHeight / 2);
    };
    $('d-term').addEventListener('click', ctx.scrollHandler, true);
    // xterm consumes wheel input, but at the first/last line some browsers
    // chain the unused delta to the page.  Stop that chain while preserving
    // xterm's own target listener and scrollbar behavior.
    ctx.wheelHandler = function (event) {
      if (!isCurrent(ctx) || !event) return;
      var viewport = ctx.term.element && ctx.term.element.querySelector
        ? ctx.term.element.querySelector('.xterm-viewport') : null;
      if (!viewport) return;
      var atTop = viewport.scrollTop <= 0;
      var atBottom = viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 1;
      if ((event.deltaY < 0 && atTop) || (event.deltaY > 0 && atBottom)) {
        if (event.preventDefault) event.preventDefault();
      }
      if (event.stopPropagation) event.stopPropagation();
    };
    $('d-term').addEventListener('wheel', ctx.wheelHandler, { passive: false });
    SerialdTerminalPolicy.install(ctx, {
      isCurrent: isCurrent,
      keyboardEnabled: function () { return $('d-kbd').checked; },
      send: send,
      copyText: copyText,
      copySelection: copySel
    });
    return ctx;
  }

  function terminalMatches(ctx, query) {
    var active = ctx && ctx.term && ctx.term.buffer && ctx.term.buffer.active;
    if (!active || !query) return [];
    var needle = query.toLocaleLowerCase();
    var matches = [];
    for (var row = 0; row < active.length; row++) {
      var line = active.getLine(row);
      if (!line || !line.translateToString) continue;
      var text = line.translateToString(true).toLocaleLowerCase();
      var from = 0, found;
      while ((found = text.indexOf(needle, from)) !== -1) {
        matches.push({ row: row, col: found });
        from = found + Math.max(1, needle.length);
      }
    }
    return matches;
  }

  function searchTerminal(direction) {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    var query = $('terminal-search').value;
    if (!query) {
      ctx.term.clearSelection(); ctx.searchQuery = ''; ctx.searchMatch = -1;
      $('terminal-search-status').textContent = ''; return;
    }
    var matches = terminalMatches(ctx, query);
    if (!matches.length) {
      ctx.term.clearSelection(); ctx.searchQuery = query; ctx.searchMatch = -1;
      $('terminal-search-status').textContent = '无匹配'; return;
    }
    if (ctx.searchQuery !== query || ctx.searchMatch < 0 || ctx.searchMatch >= matches.length) {
      ctx.searchMatch = direction < 0 ? matches.length - 1 : 0;
    } else {
      ctx.searchMatch = (ctx.searchMatch + direction + matches.length) % matches.length;
    }
    ctx.searchQuery = query;
    var match = matches[ctx.searchMatch];
    ctx.term.select(match.col, match.row, query.length);
    ctx.term.scrollToLine(match.row);
    $('terminal-search-status').textContent = (ctx.searchMatch + 1) + ' / ' + matches.length;
  }

  function clearTerminalSearch(focusTerminal) {
    $('terminal-search').value = '';
    $('terminal-search-status').textContent = '';
    if (view && view.term) {
      view.searchQuery = ''; view.searchMatch = -1; view.term.clearSelection();
      if (focusTerminal) view.term.focus();
    }
  }

  function redrawTerminal() {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    ctx.readSeq++;
    if (ctx.ctrl) ctx.ctrl.abort();
    clearTimeout(ctx.loopTimer);
    if (ctx.refreshFrame && window.cancelAnimationFrame) {
      window.cancelAnimationFrame(ctx.refreshFrame); ctx.refreshFrame = 0;
    }
    clearTerminalSearch(false);
    try { ctx.term.reset(); ctx.term.clear(); } catch (e) {}
    ctx.off = 0; ctx.initialRendered = false;
    $('d-status').textContent = ' 正在从采集缓冲完整重绘…';
    if (ctx.kind === 'log') {
      var archived = sessions.filter(function (s) { return s.sid === ctx.sid; })[0];
      if (archived && archived.payload) {
        writeTerminalPayload(ctx, archived.payload);
        $('d-status').textContent = archived.status || ' 只读归档已重绘';
      }
      return;
    }
    loop(ctx);
  }

  function resizeTerm() {
    var ctx = view;
    if (!isCurrent(ctx)) return;
    try { fitTerminal(ctx); syncTerminalLayers(ctx); } catch (e) {}
    if (window.requestAnimationFrame) window.requestAnimationFrame(function () {
      if (!isCurrent(ctx)) return;
      try { fitTerminal(ctx); syncTerminalLayers(ctx); ctx.term.refresh(0, ctx.term.rows - 1); } catch (e) {}
      pushSize(ctx);
    });
    pushSize(ctx);
  }
  function visibleTerminalRows(ctx, dimensions) {
    var proposed = dimensions && dimensions.rows ? dimensions.rows : ctx.term.rows;
    var element = ctx.term.element;
    if (!element || !element.querySelector) return proposed;
    var viewport = element.querySelector('.xterm-viewport');
    var screen = element.querySelector('.xterm-screen');
    var cellHeight = screen && ctx.term.rows
      ? screen.getBoundingClientRect().height / ctx.term.rows : 0;
    return viewport && viewport.clientHeight && cellHeight > 0
      ? Math.min(proposed, Math.floor(viewport.clientHeight / cellHeight)) : proposed;
  }
  function fitTerminal(ctx) {
    // UART has no PTY window-size negotiation. Linux serial consoles,
    // readline, EDK2 and GRUB normally lay out against 80 columns; fitting
    // xterm to a wide browser makes wrapped input and cursor-left disagree
    // about the visual row. Keep only the live serial console at 80 columns.
    if (ctx.sid === 'serial') {
      var required = ctx.serialRows || 24;
      var cap = required >= 40 && document.body.classList.contains('terminal-max')
        ? 10 : DEFAULT_TERMINAL_FONT_SIZE;
      if (ctx.term.options.fontSize !== cap) ctx.term.options.fontSize = cap;
      var dimensions = ctx.fit.proposeDimensions ? ctx.fit.proposeDimensions() : null;
      var visibleRows = visibleTerminalRows(ctx, dimensions);
      // xterm's viewport does not scroll through extra logical screen rows.
      // Shrink only a firmware-sized screen until the requested rows fit.
      if (required >= 40 && dimensions && dimensions.rows) {
        var fontSize = cap;
        while (visibleRows < required && fontSize > 8) {
          fontSize = Math.max(8, Math.min(fontSize - .5,
            Math.floor(fontSize * visibleRows / required * 2) / 2));
          ctx.term.options.fontSize = fontSize;
          dimensions = ctx.fit.proposeDimensions();
          if (!dimensions || !dimensions.rows) break;
          visibleRows = visibleTerminalRows(ctx, dimensions);
        }
      }
      var rows = Math.max(required, visibleRows);
      if (ctx.term.cols !== 80 || ctx.term.rows !== rows) ctx.term.resize(80, rows);
      return;
    }
    if (ctx.term.options.fontSize !== DEFAULT_TERMINAL_FONT_SIZE)
      ctx.term.options.fontSize = DEFAULT_TERMINAL_FONT_SIZE;
    // FitAddon calculates width from the host and subtracts a scrollbar even
    // when Chromium uses overlay scrollbars. Measure the actual viewport so
    // the PTY column count matches the pixels xterm can paint.
    var viewport = ctx.term.element && ctx.term.element.querySelector
      ? ctx.term.element.querySelector('.xterm-viewport') : null;
    var renderDimensions = ctx.term._core && ctx.term._core._renderService
      && ctx.term._core._renderService.dimensions;
    var cell = renderDimensions && renderDimensions.css && renderDimensions.css.cell
      && renderDimensions.css.cell.width;
    if (!viewport || !viewport.clientWidth || !cell) {
      ctx.fit.fit();
      return;
    }
    var proposed = ctx.fit.proposeDimensions();
    var cols = Math.max(2, Math.floor((viewport.clientWidth - 1) / cell));
    var rows = Math.max(1, visibleTerminalRows(ctx, proposed));
    if (ctx.term.cols !== cols || ctx.term.rows !== rows) ctx.term.resize(cols, rows);
  }
  function syncTerminalLayers(ctx) {
    if (!ctx || !ctx.term || !ctx.term.element || !ctx.term.element.querySelector) return;
    var viewport = ctx.term.element.querySelector('.xterm-viewport');
    var screen = ctx.term.element.querySelector('.xterm-screen');
    if (!viewport || !screen || !viewport.clientWidth) return;
    if (ctx.sid !== 'serial') {
      // Columns already fit the measured viewport; clip only xterm's spare
      // pixels beyond that width, never a rendered character cell.
      screen.style.width = viewport.clientWidth + 'px';
      screen.style.maxWidth = viewport.clientWidth + 'px';
      screen.style.overflow = 'hidden';
      return;
    }
    // The 80-column serial canvas can be wider than a narrow viewport. Leave
    // an explicit hit area for the scrollbar, including overlay scrollbars.
    var screenWidth = Math.max(0, viewport.clientWidth - 16) + 'px';
    screen.style.width = screenWidth;
    screen.style.maxWidth = screenWidth;
    screen.style.overflow = 'hidden';
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
  // Device metadata and tab controls can change height without a window resize.
  // Refit after the terminal host itself changes so its last row stays visible.
  if (window.ResizeObserver) {
    new window.ResizeObserver(resizeTerm).observe($('d-term'));
  }
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
      var nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = d.state === 'pending' ? '待识别 · ' + (d.hostname || (d.dev || '').split('/').pop()) : d.label;
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
      if (d.identity_error || (d.needs_provisioning && Object.keys(inf).length)
          || inf.identity_status === 'provisional') {
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
          var info = cur && cur.info;
          var infoIdentity = cur ? [cur.key, cur.connection_id, info && info.ts || 0,
            info && info.serial_source || '', info && info.identity_status || ''].join('|') : '';
          if (infoIdentity !== lastInfoIdentity) {
            lastInfoIdentity = infoIdentity;
            lastInfoTs = info && info.ts || 0;
            renderInfo(info);
          }
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
    // Slow paste is a workaround for one firmware input path, not a board-wide
    // default.  Keep it only while staying on the same selected board.
    if (key !== selKey) $('d-slow-paste').checked = false;
    var ctx = createView(key, 'serial');
    // 切设备时立即清掉旧会话标签；迟到的列表/动作响应不能借用新 selKey。
    sessions = [{ sid: 'serial', kind: 'serial' }];
    var d = (lastDevices || []).filter(function (x) { return x.key === key; })[0];
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
    loadCommands();
    lastInfoTs = d && d.info ? d.info.ts : 0;
    lastInfoIdentity = d ? [d.key, d.connection_id, lastInfoTs,
      d.info && d.info.serial_source || '', d.info && d.info.identity_status || ''].join('|') : '';
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

  function writeTerminalPayload(ctx, payload) {
    if (!isCurrent(ctx)) return;
    // The archive endpoint returns the complete file.  Expand xterm's buffer
    // to the payload's line count before writing so the UI does not silently
    // evict the beginning of a large daily log.
    if (ctx.kind === 'log' && typeof payload.data === 'string') {
      var lines = payload.data.split('\n').length + 10;
      if (lines > ctx.term.options.scrollback) ctx.term.options.scrollback = lines;
    }
    var redraw = false;
    function newestBootCycle(raw) {
      if (ctx.sid !== 'serial' || !raw) return raw;
      // The collector buffer can contain several resets.  Replaying an old
      // EDK2 screen followed by a new boot's plain text preserves highlighted
      // background rows because Boot ROM/U-Boot do not clear a terminal they
      // did not create.  Treat an unmistakable boot banner as a new terminal
      // epoch and show only the newest epoch from this payload onward.
      var prefix = ctx.bootScanTail || '';
      var joined = prefix + raw;
      var pattern = /(?:\x00sys:\s*0x[0-9a-f]+|(?:^|\r?\n)U-Boot(?: SPL)? \d{4}\.\d{2})/ig;
      var match, cut = -1;
      while ((match = pattern.exec(joined))) {
        if (match.index + match[0].length > prefix.length) {
          cut = Math.max(0, match.index - prefix.length);
        }
      }
      ctx.bootScanTail = joined.slice(-32);
      if (cut < 0) return raw;
      try { ctx.term.reset(); ctx.term.clear(); ctx.term.clearSelection(); } catch (e) {}
      ctx.serialRows = 24;
      ctx.phaseScanTail = '';
      ctx.linuxConsole = false;
      ctx.initialRendered = false;
      return raw.slice(cut);
    }
    function prepareSerialGeometry(raw) {
      if (ctx.sid !== 'serial' || !raw) return;
      // UEFI commonly paints an 80x50 text console with absolute CUP/HVP
      // coordinates.  Clamping row 50 into a browser-fitted 40-row terminal
      // makes later erases overwrite the whole visible menu.  A full clear
      // starts a new logical screen: GRUB/Linux can therefore leave EDK2's
      // 50-row mode, while another row-50 CUP later in the same frame restores
      // it before xterm parses the payload. Linux boot output ends firmware
      // mode even if firmware never sent a final clear-screen sequence.
      var prefix = ctx.phaseScanTail || '';
      var joined = prefix + raw;
      var pattern = /\x1b\[(?:([0-9]{1,3})(?:;[0-9]{0,3})?[Hf]|2J)|Linux version [0-9]/g;
      var match, rows = ctx.serialRows || 24;
      while ((match = pattern.exec(joined))) {
        if (match.index + match[0].length <= prefix.length) continue;
        if (match[0].startsWith('Linux version ')) {
          ctx.linuxConsole = true;
          rows = 24;
          continue;
        }
        if (!match[1]) {
          rows = 24;
          try { ctx.term.clearSelection(); } catch (e) {}
          continue;
        }
        var row = Number(match[1] || 1);
        if (!ctx.linuxConsole && row >= 1 && row <= 200) rows = Math.max(rows, row);
      }
      ctx.phaseScanTail = joined.slice(-32);
      if (rows !== ctx.serialRows) {
        ctx.serialRows = rows;
        fitTerminal(ctx);
        syncTerminalLayers(ctx);
      }
    }
    function written() {
      ctx.initialRendered = true;
      if (redraw) scheduleFullRefresh(ctx);
    }
    if (typeof payload.data_b64 === 'string') {
      var raw = atob(payload.data_b64);
      raw = newestBootCycle(raw);
      prepareSerialGeometry(raw);
      // Firmware menus paint the screen with cursor positioning and erases.
      // xterm normally tracks dirty rows, but a large sequence split across
      // HTTP reads can leave canvas layers visually stale until the next input.
      redraw = /\x1b(?:\[[0-9;?]*[ABCDEFGHJKSTfhl]|[78])/.test(raw);
      if (raw.length) {
        ctx.term.write(Uint8Array.from(raw, function (c) { return c.charCodeAt(0); }),
          written);
      } else ctx.initialRendered = true;
    } else if (payload.data) {
      var text = newestBootCycle(payload.data);
      prepareSerialGeometry(text);
      redraw = /\x1b(?:\[[0-9;?]*[ABCDEFGHJKSTfhl]|[78])/.test(text);
      ctx.term.write(text, written);
    } else ctx.initialRendered = true;
  }

  function scheduleFullRefresh(ctx) {
    if (!isCurrent(ctx) || ctx.refreshFrame) return;
    var refresh = function () {
      ctx.refreshFrame = 0;
      if (!isCurrent(ctx)) return;
      try { ctx.term.refresh(0, ctx.term.rows - 1); } catch (e) {}
    };
    if (window.requestAnimationFrame) ctx.refreshFrame = window.requestAnimationFrame(refresh);
    else refresh();
  }

  function loop(ctx) {
    if (!isCurrent(ctx)) return;
    var readSeq = ++ctx.readSeq;
    var nextDelay = 10;
    ctx.ctrl = new AbortController();
    fetch('/api/session?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '') + '&sid=' + encodeURIComponent(ctx.sid)
          + '&off=' + ctx.off, { cache: 'no-store', signal: ctx.ctrl.signal })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx) || readSeq !== ctx.readSeq) return; // 切换/重绘后的旧响应,丢弃
        if (!d || d.error) throw new Error(d && d.error || 'error');
        if ((d.key && d.key !== ctx.key) || (d.sid && d.sid !== ctx.sid)) {
          throw new Error('会话响应与请求不匹配');
        }
        ctx.off = d.off;
        nextDelay = d.alive === false ? 500 : 10;
        $('d-dev').textContent = d.dev || d.ip || '';
        // 原始字节直接交给 xterm —— 光标定位、颜色、备用屏幕缓冲都由它处理,
        // vim/htop 这类全屏程序才能正确重画(剥 ANSI 再追加会糊成一团)
        // JSON 每块先转字符串会截断跨块 UTF-8（中文尤其明显）。传原始字节，
        // 由此会话自己的 xterm 解码器保留半个字符；切会话时解码器也一起销毁。
        writeTerminalPayload(ctx, d);   // data 字段兼容仍在运行的旧服务
      })
      .catch(function (e) {
        if (!isCurrent(ctx) || readSeq !== ctx.readSeq || (e && e.name === 'AbortError')) return;
        $('d-status').textContent = ' 读取失败：' + e.message;
      })
      .finally(function () {
        if (!isCurrent(ctx) || readSeq !== ctx.readSeq) return;
        // 定时器本身也必须绑定视图，否则旧定时器会多开一条新会话轮询。
        // /api/session is already a long poll.  Start the next read promptly
        // after output so terminal echo is not held behind an extra 300 ms.
        ctx.loopTimer = setTimeout(function () { loop(ctx); }, nextDelay);
      });
  }

  function switchSession(sid) {
    if (!selKey) return;
    if (sid === curSid) {
      if ($('d-term').scrollIntoView) $('d-term').scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    var ctx = createView(selKey, sid);
    $('d-status').textContent = '';
    $('d-in').value = '';
    resizeTerm();
    renderTabs();
    loadSessions();
    loadLogs();
    var selected = sessions.filter(function (s) { return s.sid === sid; })[0];
    if (selected && selected.kind === 'log') loadArchivedLog(ctx, selected);
    else loop(ctx);
    if ($('d-term').scrollIntoView) $('d-term').scrollIntoView({ behavior: 'smooth', block: 'start' });
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
      b.textContent = s.kind === 'serial' ? '串口'
        : (s.kind === 'log' ? s.label : ('SSH ' + (s.actor ? s.actor + ' · ' : '') + (s.ip || s.sid)));
      if (s.kind === 'log' || (s.actor && s.actor !== 'web' && !takenOver[s.sid])) b.textContent += ' · 只读';
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
    var ctx = view;
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

  function flushInput(ctx) {
    if (!isCurrent(ctx) || ctx.inputSending || ctx.inputTimer || !ctx.inputQueue.length) return;
    var item = ctx.inputQueue.shift();
    ctx.inputSending = true;
    fetch('/api/session-send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, data: item.data }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent(ctx)) return;
        if (!d.ok) $('d-status').textContent = ' 写入失败：' + (d.error || '');
        else if (/[\r\n]/.test(item.data)) loadCommands();
      })
      .catch(function (e) {
        if (isCurrent(ctx)) $('d-status').textContent = ' 写入失败：' + e.message;
      }).finally(function () {
        ctx.inputSending = false;
        if (!isCurrent(ctx)) return;
        if (item.slow && ctx.inputQueue.length) {
          // EFI Serial I/O polling in GRUB can lose a bulk write.  Spacing is
          // opt-in and affects only pasted serial bytes, never normal typing.
          ctx.inputTimer = setTimeout(function () {
            ctx.inputTimer = 0;
            flushInput(ctx);
          }, 50);
        } else flushInput(ctx);
      });
  }

  function send(text, ctx, slow) {
    ctx = ctx || view;
    if (!isCurrent(ctx)) return;
    if (ctx.readonly) {
      $('d-status').textContent = ctx.kind === 'log' ? ' 当前是只读归档日志' : ' 当前正在只读查看 agent 会话';
      return;
    }
    // Preserve order.  Ordinary keystrokes coalesce during a network round
    // trip; firmware paste deliberately stays one character per write.
    var last = ctx.inputQueue[ctx.inputQueue.length - 1];
    if (!slow && last && !last.slow) last.data += text;
    else ctx.inputQueue.push({ data: text, slow: !!slow });
    flushInput(ctx);
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
        $('log-location').textContent = '点击后在终端标签打开 · '
          + (d.archive ? 'logs/' + d.archive + '/' : '待识别连接的临时日志');
        files.forEach(function (f) {
          var row = document.createElement('div'); row.className = 'log-row';
          var date = document.createElement('span'); date.className = 'log-date';
          date.textContent = f.date || '日期未知';
          var a = document.createElement('button');
          a.type = 'button';
          a.addEventListener('click', function () {
            if (isCurrent(ctx)) openArchivedLog(ctx, f.name, '串口日志 · ' + (f.date || f.name));
          });
          a.textContent = f.name;
          var size = document.createElement('span'); size.className = 'log-size'; size.textContent = fmt(f.size);
          var download = document.createElement('a'); download.className = 'log-download';
          download.href = '/api/log?key=' + encodeURIComponent(ctx.key)
            + '&serial=' + encodeURIComponent(ctx.serial || '') + '&file=' + encodeURIComponent(f.name)
            + '&download=1';
          download.download = f.name; download.textContent = '下载';
          row.appendChild(date); row.appendChild(a); row.appendChild(size); row.appendChild(download); box.appendChild(row);
        });
        var boots = d.boots || [];
        if (boots.length) {
          var heading = document.createElement('div'); heading.className = 'log-subhead';
          heading.textContent = '检测到 ' + boots.length + ' 次启动'; box.appendChild(heading);
          boots.forEach(function (boot) {
            var row = document.createElement('div'); row.className = 'log-row boot-row';
            var date = document.createElement('span'); date.className = 'log-date';
            date.textContent = boot.started || '时间未知';
            var button = document.createElement('button'); button.type = 'button';
            button.textContent = '启动 #' + boot.number + ' · ' + (boot.marker || '启动标志');
            button.addEventListener('click', function () {
              var url = '/api/boot-log?key=' + encodeURIComponent(ctx.key)
                + '&serial=' + encodeURIComponent(ctx.serial || '') + '&id=' + encodeURIComponent(boot.id);
              openArchivedLog(ctx, boot.id, '启动 #' + boot.number + ' · ' + (boot.started || ''), url);
            });
            var meta = document.createElement('span'); meta.className = 'log-size';
            meta.textContent = fmt(boot.size) + (boot.cross_day ? ' · 跨日' : '');
            row.appendChild(date); row.appendChild(button); row.appendChild(meta); box.appendChild(row);
          });
        } else {
          var noBoot = document.createElement('div'); noBoot.className = 'log-note';
          noBoot.textContent = '尚未在归档中检测到 Boot ROM / U-Boot 启动标志。'; box.appendChild(noBoot);
        }
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
    lastDevices.forEach(function (d) {
      if (d.key !== ctx.key) return;
      d.key = key; d.label = key; d.serial = serial;
      if (!serial) {
        // /api/switch-mode detaches the old board immediately.  Clear the
        // optimistic local copy too; otherwise the old board's candidate
        // serial/hostname remains visible until the next device poll.
        d.info = {};
        d.hostname = '';
        d.reported_serial = '';
        d.identity_error = '';
        d.needs_provisioning = true;
        d.state = 'pending';
      }
    });
    select(key);
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
    var ctx = view;
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
            if (!isCurrent(ctx) || ctx.readonly) return;
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
  $('terminal-search-next').addEventListener('click', function () { searchTerminal(1); });
  $('terminal-search-prev').addEventListener('click', function () { searchTerminal(-1); });
  $('terminal-search-clear').addEventListener('click', function () { clearTerminalSearch(true); });
  $('b-redraw').addEventListener('click', redrawTerminal);
  $('b-info').addEventListener('click', function () {
    var open = $('detail').classList.toggle('info-open');
    $('b-info').setAttribute('aria-expanded', open ? 'true' : 'false');
    setTimeout(resizeTerm, 0);
  });
  $('b-expand').addEventListener('click', function () {
    var expanded = document.body.classList.toggle('terminal-max');
    $('b-expand').textContent = expanded ? '还原工作台' : '放大终端';
    $('b-expand').setAttribute('aria-pressed', expanded ? 'true' : 'false');
    setTimeout(resizeTerm, 0);
  });
  $('terminal-search').addEventListener('input', function () {
    if (view) { view.searchQuery = ''; view.searchMatch = -1; }
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
    if (!selKey || !(event.ctrlKey || event.metaKey) || String(event.key).toLowerCase() !== 'f') return;
    if (event.preventDefault) event.preventDefault();
    $('terminal-search').focus();
    if ($('terminal-search').select) $('terminal-search').select();
  });
  function setRecordsOpen(open) {
    $('d-records').hidden = !open;
    $('console-layout').classList.toggle('records-open', open);
    $('b-records').setAttribute('aria-expanded', open ? 'true' : 'false');
    setTimeout(resizeTerm, 0);
  }
  $('b-records').addEventListener('click', function () { setRecordsOpen($('d-records').hidden); });
  $('b-records-close').addEventListener('click', function () { setRecordsOpen(false); });
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
    if (!isCurrent(ctx) || !ctx.canTakeover) return;
    fetch('/api/session-takeover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: ctx.key, serial: ctx.serial, sid: ctx.sid, actor: 'web' }) })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (!isCurrent(ctx)) return;
        if (!result.ok) throw new Error(result.error || '介入失败');
        takenOver[ctx.sid] = true;
        ctx.readonly = false; ctx.canTakeover = false;
        setInputMode(ctx);
        renderTabs();
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
    document.body.classList.toggle('detail-open', false);
    document.body.classList.remove('terminal-max');
    $('b-expand').textContent = '放大终端';
    $('b-expand').setAttribute('aria-pressed', 'false');
    markSelected(); pollDevices();
  });

  pollDevices();
  setInterval(pollDevices, 3000);
})();
