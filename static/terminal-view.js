// Owns one terminal view: lifetime, IO, geometry, redraw and ordered input.
// Device selection, archive navigation and session catalogs are injected.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fetch = options.fetch;
    var view = null, term = null, loopGen = 0;
    var DEFAULT_TERMINAL_FONT_SIZE = 14.5;
    function isCurrent(ctx) {
      return !!ctx && view === ctx && ctx.gen === loopGen;
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
      if (old.stream) old.stream.close();
      clearTimeout(old.loopTimer);
      clearTimeout(old.inputTimer);
      clearTimeout(old.sizeTimer);
      clearTimeout(old.selTimer);
      clearTimeout(old.navTimer);
      options.clearCommandDecorations(old);
      if (old.refreshFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(old.refreshFrame);
      // reset() 不会清空 xterm 的异步解析队列；旧 CSI 6n 还可能触发 onData，
      // 把上一台的终端应答写到新设备。销毁实例，并让所有旧回调检查所属视图。
      old.term.dispose();
    }

    /* ---------- 终端模拟器 ---------- */
    function createView(key, sid) {
      disposeView();
      options.onSelect(key, sid);
      $('b-rescue').disabled = false;
      var board = options.board(key) || {};
      var selectedSession = options.session(sid) || {};
      var isAgentSession = selectedSession.kind === 'ssh' && !!selectedSession.actor
        && selectedSession.actor !== 'web' && !options.takenOver(sid);
      var ctx = { key: key, serial: board.serial, connectionId: board.connection_id,
                  byPath: board.by_path || board.last_path,
                  readonly: selectedSession.kind === 'log' || isAgentSession
                    || (sid === 'serial' && !!board.input_owner && !options.ownsInput(board.input_owner)),
                  inputOwner: board.input_owner || null,
                  canTakeover: isAgentSession, kind: selectedSession.kind || 'serial',
                  sid: sid, gen: loopGen, off: 0, readSeq: 0, sessionsSeq: 0, activitySeq: 0,
                  logsSeq: 0,
                  inputQueue: [], inputSending: false, inputTimer: 0,
                  navEntries: [], navSelected: -1, navTimer: 0, navRendered: false,
                  commandDecorations: [], commandColorSignature: '',
                  lastSel: '', searchQuery: '', searchMatch: -1,
                  serialRows: 24, bootScanTail: '', phaseScanTail: '', linuxConsole: false };
      view = ctx;
      resetProgress(ctx);
      options.setInputMode(ctx);
      term = ctx.term = new Terminal({
        // 归档文件常用单独 LF；xterm 的 LF 默认不回到第 1 列，会呈现阶梯状。
        // 实时串口/pty 保留原始 CR/LF，避免改变全屏终端协议。
        convertEol: ctx.kind === 'log',
        scrollback: 8000,
        cursorBlink: !ctx.readonly,
        allowProposedApi: true,
        fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, "Liberation Mono", Menlo, monospace',
        fontSize: DEFAULT_TERMINAL_FONT_SIZE,
        theme: { background: '#0F151D', foreground: '#DCE5F0', cursor: '#82C5FF',
                 selectionBackground: '#E6B45D', selectionForeground: '#0F151D' }
      });
      ctx.fit = new FitAddon.FitAddon();
      ctx.term.loadAddon(ctx.fit);
      $('d-term').textContent = '';
      $('d-command-nav').textContent = '';
      $('command-nav-count').textContent = '0 条';
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
        copyText: options.copyText,
        copySelection: options.copySel
      });
      return ctx;
    }

    function resetProgress(ctx) {
      if (ctx.sid !== 'serial' || !window.SerialdSerialProgress) return;
      ctx.progress = window.SerialdSerialProgress.create({
        rows: function () { return ctx.term.rows; },
        onStart: function () {
          ctx.linuxConsole = true; ctx.serialRows = 24;
          fitTerminal(ctx); syncTerminalLayers(ctx);
        }
      });
    }

    function redrawTerminal() {
      var ctx = view;
      if (!isCurrent(ctx)) return;
      ctx.readSeq++;
      if (ctx.ctrl) ctx.ctrl.abort();
      if (ctx.stream) { ctx.stream.close(); ctx.stream = null; }
      clearTimeout(ctx.loopTimer);
      if (ctx.refreshFrame && window.cancelAnimationFrame) {
        window.cancelAnimationFrame(ctx.refreshFrame); ctx.refreshFrame = 0;
      }
      options.clearTerminalSearch(false);
      options.clearCommandDecorations(ctx);
      try { ctx.term.reset(); ctx.term.clear(); } catch (e) {}
      ctx.navRendered = false;
      options.renderCommandNav(ctx);
      ctx.off = 0; ctx.initialRendered = false;
      resetProgress(ctx);
      $('d-status').textContent = ' 正在从采集缓冲完整重绘…';
      if (ctx.kind === 'log') {
        var archived = options.session(ctx.sid);
        if (archived && archived.payload) {
          writeTerminalPayload(ctx, archived.payload);
          $('d-status').textContent = archived.status || ' 只读归档已重绘';
        }
        return;
      }
      startStream(ctx);
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
      options.scheduleCommandNav(ctx);
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
        // A phone may have less than 350px for 50 firmware rows; an 8px
        // floor clips the menu. Ordinary Linux/SSH text retains its normal size.
        if (required >= 40 && dimensions && dimensions.rows) {
          var fontSize = cap;
          while (visibleRows < required && fontSize > 4) {
            fontSize = Math.max(4, Math.min(fontSize - .5,
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
        options.clearCommandDecorations(ctx);
        try { ctx.term.reset(); ctx.term.clear(); ctx.term.clearSelection(); } catch (e) {}
        ctx.serialRows = 24;
        ctx.phaseScanTail = '';
        ctx.linuxConsole = false;
        ctx.initialRendered = false;
        resetProgress(ctx);
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
        if (!isCurrent(ctx)) return;
        ctx.initialRendered = true;
        if (ctx.archiveSearchQuery) {
          $('terminal-search').value = ctx.archiveSearchQuery;
          ctx.archiveSearchQuery = ''; options.searchTerminal(1);
        }
        if (redraw) scheduleFullRefresh(ctx);
        options.scheduleCommandNav(ctx);
      }
      if (typeof payload.data_b64 === 'string') {
        var raw = atob(payload.data_b64);
        raw = newestBootCycle(raw);
        prepareSerialGeometry(raw);
        if (ctx.progress) raw = ctx.progress.rewrite(raw);
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
        if (ctx.progress) text = ctx.progress.rewrite(text);
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

    function startStream(ctx) {
      if (!isCurrent(ctx)) return;
      if (typeof window.EventSource !== 'function') { loop(ctx); return; }
      var url = '/api/session-events?key=' + encodeURIComponent(ctx.key)
        + '&serial=' + encodeURIComponent(ctx.serial || '')
        + '&sid=' + encodeURIComponent(ctx.sid) + '&off=' + ctx.off;
      var stream = new window.EventSource(url);
      ctx.stream = stream;
      stream.onmessage = function (event) {
        if (!isCurrent(ctx) || ctx.stream !== stream) return;
        var d;
        try { d = JSON.parse(event.data); } catch (e) { stream.onerror(); return; }
        if (!d || d.sid !== ctx.sid || d.key !== ctx.key || typeof d.off !== 'number') {
          stream.onerror(); return;
        }
        if (d.off < ctx.off) return;
        if (ctx.sid === 'serial' && Object.prototype.hasOwnProperty.call(d, 'input_owner')
            && options.updateInputOwner) options.updateInputOwner(ctx, d.input_owner);
        if (d.off === ctx.off && !d.data_b64 && d.alive !== false) return;
        if (d.off === ctx.off && ctx.off > 0 && d.alive !== false) return;
        ctx.off = d.off;
        $('d-dev').textContent = d.dev || d.ip || '';
        writeTerminalPayload(ctx, d);
        if (d.alive === false) {
          stream.close(); ctx.stream = null;
          ctx.loopTimer = setTimeout(function () { loop(ctx); }, 500);
        }
      };
      stream.onerror = function () {
        if (ctx.stream !== stream) return;
        stream.close(); ctx.stream = null;
        if (isCurrent(ctx)) loop(ctx); // Old services and transient SSE failures keep working.
      };
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
          else if (/[\r\n]/.test(item.data)) options.loadCommands();
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

    var controller = {
      isCurrent: isCurrent, create: createView, dispose: disposeView,
      resize: resizeTerm, redraw: redrawTerminal,
      write: writeTerminalPayload, start: startStream, send: send
    };
    Object.defineProperty(controller, 'current', { get: function () { return view; } });
    return controller;
  }
  global.SerialdTerminalView = { create: create };
})(window);
