// Native VNC/RDP sessions remain on the collector. Only display updates and input
// events cross this viewer; no target credentials or desktop protocol run here.
(function (global) {
  'use strict';
  var libraryPromise;
  function libraries(options) {
    if (options.libraryLoader) return options.libraryLoader();
    if (!libraryPromise) libraryPromise = Promise.all([
      import('/vendor/novnc/core/display.js'), import('/vendor/novnc/core/input/keyboard.js')
    ]).then(function (modules) { return {Display: modules[0].default, Keyboard: modules[1].default}; })
      .catch(function (error) { libraryPromise = null; throw error; });
    return libraryPromise;
  }
  function integer(value, low, high) { return Number.isInteger(value) && value >= low && value <= high; }
  function createSurface(options) {
    var canvas = options.canvas, viewport = options.viewport;
    var display = new options.Display(canvas), keyboard = new options.Keyboard(canvas);
    var width = 0, height = 0, writable = false, disposed = false, grabbed = false;
    var keys = new Map(), buttons = 0, pointer = {x: 0, y: 0}, scale = 'fit';
    var clipboardKeys = new Set(), pasteWithShift = false;
    var copyDrag = null, superFromF8 = false;
    var pendingMove = null, moveTimer = null, wheelRemainder = 0;
    var raf = options.requestAnimationFrame || global.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
    var caf = options.cancelAnimationFrame || global.cancelAnimationFrame || clearTimeout;
    function send(event, motion) {
      if (disposed || !writable) return false;
      return options.send(event, !!motion) !== false;
    }
    function flushMove() {
      if (moveTimer !== null) { caf(moveTimer); moveTimer = null; }
      if (pendingMove) { var event = pendingMove; pendingMove = null; send(event, true); }
    }
    function position(event) {
      var rect = canvas.getBoundingClientRect();
      if (!width || !height || !rect.width || !rect.height) return null;
      return {x: Math.max(0, Math.min(width - 1, Math.floor((event.clientX - rect.left) * width / rect.width))),
              y: Math.max(0, Math.min(height - 1, Math.floor((event.clientY - rect.top) * height / rect.height)))};
    }
    function emitKey(keysym, code, down) {
      if (!writable || disposed || !integer(keysym, 0, 0xffffffff)) return;
      flushMove();
      if (down) keys.set(code, keysym); else keys.delete(code);
      send({type: 'key', keysym: keysym, code: code, down: !!down});
    }
    keyboard.onkeyevent = emitKey;
    var keyboardLock = null;
    function keyHint(unavailable) {
      if (options.onKeyLockUnavailable) options.onKeyLockUnavailable(unavailable);
    }
    function unlockSystemKeys() {
      if (keyboardLock) {
        ['MetaLeft','MetaRight'].forEach(function (code) { if (keys.has(code)) emitKey(keys.get(code), code, false); });
        keyboardLock.api.unlock(); keyboardLock = null;
      }
      keyHint(false);
    }
    function systemKeys() {
      var api = global.navigator && global.navigator.keyboard, doc = global.document;
      if (!grabbed || !doc || doc.activeElement !== canvas || !doc.fullscreenElement ||
          !doc.fullscreenElement.contains(canvas)) { unlockSystemKeys(); return; }
      if (!api || !api.lock) { keyHint(true); return; }
      if (keyboardLock) return;
      var request = {api: api}; keyboardLock = request;
      // A denied request is retried only after the user refocuses the viewer.
      api.lock(['MetaLeft', 'MetaRight', 'Tab']).then(function () {
        if (keyboardLock === request) { request.active = true; keyHint(false); }
      }).catch(function () { if (keyboardLock === request) keyHint(true); });
    }
    function grab() { if (!disposed && writable && !grabbed) { keyboard.grab(); grabbed = true; systemKeys(); } }
    function release(reason) {
      clipboardKeys.clear(); pasteWithShift = false; copyDrag = null; superFromF8 = false;
      unlockSystemKeys(); flushMove();
      if (grabbed) { keyboard.ungrab(); grabbed = false; }
      keys.forEach(function (keysym, code) { send({type: 'key', keysym: keysym, code: code, down: false}); });
      keys.clear();
      if (buttons) send({type: 'pointer', x: pointer.x, y: pointer.y, buttons: 0});
      buttons = 0; wheelRemainder = 0;
      send({type: 'release', reason: reason || 'focus-lost'});
    }
    function pointerDown(event) {
      if (!writable || event.button > 2 || event.button < 0) return;
      event.preventDefault(); canvas.focus({preventScroll: true}); grab();
      var point = position(event); if (!point) return;
      flushMove(); pointer = point;
      buttons |= ({0: 1, 1: 2, 2: 4})[event.button];
      if (canvas.setPointerCapture && event.pointerId != null) canvas.setPointerCapture(event.pointerId);
      var accepted = send({type: 'pointer', x: point.x, y: point.y, buttons: buttons});
      copyDrag = accepted && buttons === 1 && event.isPrimary !== false && options.copyDragEnabled && options.copyDragEnabled()
        ? {id:event.pointerId, x:event.clientX, y:event.clientY} : null;
    }
    function pointerMove(event) {
      if (!writable) return;
      var point = position(event); if (!point) return;
      pointer = point;
      pendingMove = {type: 'pointer', x: point.x, y: point.y, buttons: buttons};
      if (moveTimer === null) moveTimer = raf(function () { moveTimer = null; flushMove(); });
    }
    function pointerUp(event) {
      if (event.button > 2 || event.button < 0) return;
      event.preventDefault(); flushMove();
      pointer = position(event) || pointer;
      buttons &= ~({0: 1, 1: 2, 2: 4})[event.button];
      var accepted = send({type: 'pointer', x: pointer.x, y: pointer.y, buttons: buttons});
      var drag = copyDrag; copyDrag = null;
      if (accepted && drag && drag.id === event.pointerId && event.button === 0 && !buttons &&
          Math.hypot(event.clientX - drag.x, event.clientY - drag.y) >= 4 && options.copyDragEnabled() &&
          combination(pasteCodes('KeyC', true), true)) options.copy();
      if (!buttons && canvas.releasePointerCapture && event.pointerId != null &&
          (!canvas.hasPointerCapture || canvas.hasPointerCapture(event.pointerId))) canvas.releasePointerCapture(event.pointerId);
    }
    function wheel(event) {
      if (!writable) return;
      event.preventDefault(); flushMove();
      var point = position(event); if (!point) return;
      wheelRemainder = Math.max(-20, Math.min(20, wheelRemainder +
        event.deltaY * (event.deltaMode === 1 ? 1 : event.deltaMode === 2 ? 12 : 1 / 40)));
      var steps = Math.trunc(wheelRemainder);
      if (!steps) return;
      steps = Math.max(-20, Math.min(20, steps)); wheelRemainder -= steps;
      send({type: 'wheel', x: point.x, y: point.y, steps: steps});
    }
    function layout() {
      if (!width || !height || disposed) return;
      if (scale === 'fit') display.autoscale(Math.max(1, viewport.clientWidth), Math.max(1, viewport.clientHeight));
      else display.scale = Number(scale);
    }
    function combination(codes, clipboard) {
      var keysyms = {ControlLeft: 0xffe3, AltLeft: 0xffe9, ShiftLeft: 0xffe1,
        MetaLeft: 0xffeb, ArrowRight: 0xff53, Delete: 0xffff, Tab: 0xff09, Escape: 0xff1b, KeyC: 0x63, KeyV: 0x76};
      if (!writable || codes.some(function (code) { return !keysyms[code]; })) return false;
      if (clipboard) ['MetaLeft','MetaRight'].forEach(function (code) {
        if (keys.has(code)) emitKey(keys.get(code), code, false);
      });
      var pressed = codes.filter(function (code) { return !keys.has(code); });
      pressed.forEach(function (code) { emitKey(keysyms[code], code, true); });
      pressed.reverse().forEach(function (code) { emitKey(keysyms[code], code, false); });
      return true;
    }
    function clipboardKey(event) {
      var meta = event.code === 'MetaLeft' || event.code === 'MetaRight';
      var captured = keyboardLock && keyboardLock.active;
      var commandClipboard = /Mac|iPhone|iPad/.test((global.navigator || {}).platform || '')
        && event.metaKey && ['KeyC','KeyV'].includes(event.code);
      if (event.code === 'F8' && (superFromF8 || event.type === 'keydown' && !event.repeat &&
          writable && options.useF8 && options.useF8() && !keys.has('MetaLeft'))) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (!event.repeat) {
          superFromF8 = event.type === 'keydown';
          emitKey(0xffeb, 'MetaLeft', superFromF8);
        }
        return;
      }
      if (meta) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (writable && captured && !(superFromF8 && event.code === 'MetaLeft')) {
          if (event.type === 'keydown' && !event.repeat || event.type === 'keyup' && keys.has(event.code))
            emitKey(event.code === 'MetaLeft' ? 0xffeb : 0xffec, event.code, event.type === 'keydown');
        } else if (writable) keyHint(true);
        return;
      }
      if (event.type === 'keydown' && event.metaKey && !captured && !commandClipboard) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (!keys.has(event.code)) clipboardKeys.add(event.code);
        return;
      }
      if (event.type === 'keyup') {
        if (clipboardKeys.delete(event.code)) { event.preventDefault(); event.stopImmediatePropagation(); }
        return;
      }
      if (!writable || event.altKey || !(event.ctrlKey || commandClipboard)
          || !['KeyC','KeyV'].includes(event.code)) return;
      event.stopImmediatePropagation();
      clipboardKeys.add(event.code);
      if (event.code === 'KeyV') {
        if (event.repeat) { event.preventDefault(); return; }
        pasteWithShift = event.shiftKey;
        // Let the browser fire its native paste event, including on HTTP.
        return;
      }
      event.preventDefault();
      if (!event.repeat && options.copy && combination(pasteCodes('KeyC', event.shiftKey), true)) options.copy();
    }
    function pasteCodes(key, shift) { return shift ? ['ControlLeft','ShiftLeft',key] : ['ControlLeft',key]; }
    function paste(event) {
      if (!writable || !options.paste || !event.clipboardData) return;
      event.preventDefault();
      var codes = pasteCodes('KeyV', pasteWithShift);
      pasteWithShift = false;
      options.paste(event.clipboardData.getData('text/plain'), codes);
    }
    canvas.addEventListener('keydown', clipboardKey, true);
    canvas.addEventListener('keyup', clipboardKey, true);
    canvas.addEventListener('paste', paste);
    var handlers = {pointerdown: pointerDown, pointermove: pointerMove, pointerup: pointerUp,
      pointercancel: function () { release('pointer-cancelled'); },
      lostpointercapture: function () { if (buttons) release('pointer-capture-lost'); },
      wheel: wheel, contextmenu: function (event) { event.preventDefault(); },
      focus: grab, blur: function () { release('focus-lost'); }};
    Object.keys(handlers).forEach(function (name) { canvas.addEventListener(name, handlers[name], name === 'wheel' ? {passive: false} : false); });
    var observer = global.ResizeObserver ? new global.ResizeObserver(layout) : null;
    if (observer) observer.observe(viewport);
    return {
      draw: function (rectangle, bytes) {
        if (disposed) return Promise.resolve(false);
        var w = rectangle.frame_width, h = rectangle.frame_height;
        if (!integer(w, 1, 8192) || !integer(h, 1, 8192) || w * h > 32 * 1024 * 1024 ||
            !integer(rectangle.x, 0, w - 1) || !integer(rectangle.y, 0, h - 1) ||
            !integer(rectangle.width, 1, w - rectangle.x) || !integer(rectangle.height, 1, h - rectangle.y)) {
          return Promise.reject(new Error('桌面画面尺寸无效，请重新连接'));
        }
        if (width !== w || height !== h) {
          release('display-resized'); width = w; height = h; display.resize(w, h); layout();
        }
        if (rectangle.mime === 'image/png' || rectangle.mime === 'image/jpeg') {
          display.imageRect(rectangle.x, rectangle.y, rectangle.width, rectangle.height, rectangle.mime, bytes);
        } else if (rectangle.mime === 'application/x-rgba' && bytes.byteLength === rectangle.width * rectangle.height * 4) {
          display.blitImage(rectangle.x, rectangle.y, rectangle.width, rectangle.height, bytes, 0);
        } else return Promise.reject(new Error('桌面画面格式不受支持'));
        display.flip();
        return Promise.resolve(display.flush()).then(function () { return !disposed; });
      },
      setWritable: function (value, localOnly) {
        if (!value && writable) {
          if (localOnly) writable = false;
          release('input-disabled');
        }
        writable = !!value;
        if (writable && global.document && global.document.activeElement === canvas) grab();
      },
      scale: function (value) { scale = value; layout(); },
      resize: layout,
      systemKeys: systemKeys,
      flush: function () { return Promise.resolve(display.flush()).then(function () { return !disposed; }); },
      focus: function () { canvas.focus({preventScroll: true}); grab(); },
      release: release,
      combination: combination,
      dispose: function () {
        if (disposed) return;
        release('viewer-closed'); writable = false; disposed = true;
        if (moveTimer !== null) caf(moveTimer);
        pendingMove = null;
        if (observer) observer.disconnect();
        canvas.removeEventListener('keydown', clipboardKey, true);
        canvas.removeEventListener('keyup', clipboardKey, true);
        canvas.removeEventListener('paste', paste);
        Object.keys(handlers).forEach(function (name) { canvas.removeEventListener(name, handlers[name]); });
      }
    };
  }

  function create(options) {
    var $ = options.byId, client = global.DeckLabTasks.createClient(options.fetch);
    var selected = '', version = 0, target = null, sid = '', visible = false, previousSession = null;
    var socket = null, surface = null, ready = false, inputAllowed = false, frame = null;
    var queue = [], inflight = null, sequence = 0, ackTimer = null, heartbeat = null;
    var renderPromise = Promise.resolve(), renderPending = 0, lastSequence = 0, mode = '', renderEpoch = 0;
    var clipboardTimer = null, localClipboardRead = null;
    var capabilities = {}, needFullFrame = true, hasObserved = false;
    var releaseBarrier = '', pendingIntent = null;
    var availability = null, setupValue = null, candidates = [], candidateId = '', detecting = false, savingSetup = false;
    var settingsRead = null, opening = null, sessionStage = '', seatRetry = null, seatRetried = false, reconnecting = false;
    var stageTimer = null, handoverAttempts = 0;
    var suggestedGdmUser = false;
    var statusValue = '点击画面后控制桌面', overlayValue = '连接后显示远程桌面';
    var ackTimeout = options.ackTimeout || 8000;
    function scope(ctx) { return ctx && ctx.serial || ''; }
    function current(ctx, generation) { return version === generation && scope(options.getView()) === scope(ctx); }
    function panelVisible() { return visible && !(global.document && global.document.hidden); }
    function translate(text) { return global.DeckLabI18n ? global.DeckLabI18n.translate(text) : text; }
    function status(text) { statusValue = text; $('desktop-status').textContent = translate(text); }
    function overlay(text) { overlayValue = text || ''; $('desktop-overlay').textContent = translate(overlayValue); $('desktop-overlay').hidden = !text; }
    function loginGuide(stage) {
      var text = {'lxqt-initial':'Bianbu 首次设置。请在画面中选择语言并创建用户，进入桌面后可重新连接。',
        'lxqt-finishing':'Bianbu 用户已创建。请在向导中点击「重启系统」，重启后可再次打开桌面。',
        'lxqt-greeter':'Bianbu 登录界面。请使用板上的账号登录。',
        'lxqt-unavailable':'未找到可共享的 Bianbu Wayland 桌面。请核对板上的图形登录服务和 wayvnc。',
        initial:'首次设置：按画面提示创建普通用户，完成后用新账号登录 Debian。',
        finishing:'普通用户已创建。请完成向导最后一步，画面将自动切换到 Debian 登录界面。',
        'no-user':'板上尚无普通用户，未检测到活动的首次设置向导。请在板上显示器完成初始化，或检查 GDM 服务。'}[stage];
      $('desktop-login-stage').textContent = text ? translate(text) : '';
      $('desktop-move-wizard').hidden = stage !== 'initial' && stage !== 'finishing';
      $('desktop-login-guide').hidden = !text;
      return text || '';
    }
    function connectionError(error) {
      var result = error.result || {};
      $('desktop-error').hidden = false;
      $('desktop-error').open = false;
      $('desktop-error-details').textContent = result.details ? JSON.stringify(result.details, null, 2) : error.message;
      var saved = setupValue && setupValue.connection;
      $('desktop-connect-without-pin').hidden = !(result.code === 'desktop-certificate-rejected'
        && saved && saved.provider === 'rdp' && saved.host && saved.password_saved && saved.certificate_policy !== 'accept');
      var summary = {'desktop-certificate-rejected':'远程桌面证书与连接设置不符。请核对板卡，或在连接设置中更新指纹。',
        'desktop-authentication-failed':'远程桌面拒绝了入口凭据，请在连接设置中更新账号和密码。'}[result.code];
      overlay(summary || error.message); status('');
    }
    function fullscreenLabel() {
      $('desktop-fullscreen').textContent = translate(global.document && global.document.fullscreenElement === $('desktop-panel') ? '退出全屏' : '全屏');
    }
    function query(ctx, route) {
      return '/api/desktop/' + route + '?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial)
        + '&sid=' + encodeURIComponent(sid) + '&client_id=' + encodeURIComponent(options.clientId || '');
    }
    function send(message) {
      if (!socket || socket.readyState !== 1) return false;
      try { socket.send(JSON.stringify(message)); return true; } catch (_) { return false; }
    }
    function clearTimers() {
      clearTimeout(stageTimer); stageTimer = null;
      if (ackTimer) clearTimeout(ackTimer); ackTimer = null;
      if (heartbeat) clearInterval(heartbeat); heartbeat = null;
      if (clipboardTimer) clearTimeout(clipboardTimer); clipboardTimer = null;
    }
    function resetObservation(reason) {
      renderEpoch++; frame = null; inputAllowed = false; queue = []; needFullFrame = true;
      if (surface) surface.setWritable(false, true);
      overlay(reason || '显示模式已变化，正在重新观察…');
    }
    function detach() {
      $('desktop-drag-copy').checked = false;
      if (surface) { surface.dispose(); surface = null; }
      var old = socket; socket = null; ready = false; inputAllowed = false; frame = null;
      clearTimers(); queue = []; inflight = null;
      releaseBarrier = ''; pendingIntent = null;
      closeClipboard();
      if (old && old.readyState < 2) old.close(1000, 'viewer-detached');
    }
    function stop(message) {
      detach(); overlay(message || '画面连接已断开');
      status(message || '连接已断开，点击“重新连接画面”后重新观察。');
    }
    function pump() {
      if (inflight || !ready || !socket || socket.readyState !== 1 || !queue.length) return;
      if (queue[0].copyLocal && !queue[0].copyReady) {
        if (!clipboardTimer) {
          var reading = queue[0];
          // Give the remote application a turn to publish the new selection.
          clipboardTimer = setTimeout(function () { clipboardTimer = null; reading.copyReady = true; pump(); }, 150);
        }
        return;
      }
      var next = queue.shift(), seq = ++sequence;
      var message = next.release ? {type: 'release_all', seq: seq} :
        {type: 'input', seq: seq, frame_id: next.frame_id, event: next.event};
      inflight = {seq: seq, event: next.event, release: next.release, copyLocal:next.copyLocal};
      if (next.release) releaseBarrier = 'sent';
      if (!send(message)) { stop('输入连接已断开，请重新观察后操作，不要重复输入。'); return; }
      ackTimer = setTimeout(function () {
        stop('输入结果未确认，连接已停止。请重新连接画面核对，不要重复输入。');
      }, ackTimeout);
    }
    function enqueue(event, motion, copyLocal) {
      if (event.type === 'release') {
        // Drop unsent input on focus loss. Release the server-held state after
        // the one in-flight operation; a closed socket also releases server-side.
        pendingIntent = null;
        if (releaseBarrier) return true;
        resetObservation('正在等待桌面画面…'); releaseBarrier = 'queued';
        if (ready && socket && socket.readyState === 1) { queue.push({release: true}); pump(); }
        return true;
      }
      if (!panelVisible() || !ready || !inputAllowed || !frame || !socket || socket.readyState !== 1) return false;
      if (event.type === 'clipboard-read') status('正在读取远端剪贴板…');
      else if (event.type === 'clipboard' || event.type === 'text') status('正在操作…');
      else if (event.type === 'wheel' || event.type === 'key' && event.down ||
          event.type === 'pointer' && event.buttons && !motion) status('已连接 · 点击画面后控制桌面');
      var next = {event: event, frame_id: frame.id, motion: motion, copyLocal:copyLocal};
      var tail = queue[queue.length - 1];
      if (motion && tail && tail.motion && tail.event.buttons === event.buttons && tail.frame_id === next.frame_id) queue[queue.length - 1] = next;
      else {
        if (queue.length >= 64) { stop('输入队列已满，连接已停止。请重新观察，不要重放输入。'); return false; }
        queue.push(next);
      }
      pump(); return true;
    }
    function dispatchIntent(intent) {
      if (typeof intent.paste === 'string') pasteFromBrowser(intent.paste, ['ControlLeft','KeyV']);
      else if (intent.codes) { if (surface) surface.combination(intent.codes); }
      else enqueue(intent.event);
    }
    function pasteFromBrowser(text, codes) {
      if (!text) return;
      var clipboard = capabilities.clipboard || {}, length = 0, count = 0;
      for (var character of text) {
        var point = character.codePointAt(0);
        if (point === 0 || point >= 0xd800 && point <= 0xdfff) {
          status('剪贴板文本包含无效字符，请重新复制'); return;
        }
        length += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
        if (length > 32768) { status('剪贴板文本超过 32 KiB，请缩短后再发送'); return; }
        count++;
      }
      if (!clipboard.write || /^latin[-_]?1$/i.test(clipboard.encoding || '') && /[^\u0000-\u00ff]/.test(text)) {
        if (capabilities.text && count <= 1024) enqueue({type:'text', text:text});
        else status('此桌面无法粘贴该文本');
        return;
      }
      if (enqueue({type:'clipboard', text:text}) && surface) surface.combination(codes, true);
    }
    function revealClipboard(focus) {
      $('desktop-clipboard').hidden = false;
      $('desktop-clipboard-toggle').setAttribute('aria-expanded', 'true');
      if (surface) surface.resize();
      if (focus) { $('desktop-text').focus(); $('desktop-text').select(); }
    }
    function closeClipboard() {
      localClipboardRead = null;
      $('desktop-clipboard').hidden = true;
      $('desktop-clipboard-toggle').setAttribute('aria-expanded', 'false');
      $('desktop-text').value = ''; $('desktop-paste-text').value = '';
      $('desktop-copy-local').disabled = true;
    }
    function copyToBrowser(text, ctx, generation, connection) {
      function stillCurrent() { return current(ctx, generation) && socket === connection && panelVisible(); }
      function fallback() {
        if (!stillCurrent()) return;
        if (options.copyText && options.copyText(text, true, $('desktop-panel'))) {
          if (surface) surface.focus();
          status('文本已复制到本机剪贴板');
        } else {
          revealClipboard(true); status('请按 Ctrl+C 复制远端文本');
        }
      }
      var clipboard = global.navigator && global.navigator.clipboard;
      if (!clipboard || !clipboard.writeText) { fallback(); return; }
      clipboard.writeText(text).then(function () {
        if (stillCurrent()) status('文本已复制到本机剪贴板');
      }).catch(fallback);
    }
    function toolbarIntent(intent) {
      if (!panelVisible() || !ready || !socket || socket.readyState !== 1) return;
      if (inputAllowed && frame && !releaseBarrier) { dispatchIntent(intent); return; }
      // Only a new, explicit toolbar action may wait for observation. Physical
      // key/pointer events and previously issued input are never retained here.
      if (!pendingIntent) pendingIntent = {version: version, socket: socket, intent: intent};
      status('正在等待桌面画面…');
    }
    function showPanel() {
      visible = true; $('desktop-panel').hidden = false;
      $('detail').classList.add('desktop-active'); $('b-desktop').setAttribute('aria-selected', 'true');
    }
    function hidePanel() {
      clearTimeout(seatRetry); seatRetry = null; handoverAttempts = 0;
      if (settingsRead) { settingsRead.abort(); settingsRead = null; }
      var wasVisible = visible;
      visible = false; pendingIntent = null;
      if (clipboardTimer) clearTimeout(clipboardTimer); clipboardTimer = null;
      closeClipboard();
      if (wasVisible && surface) surface.release('view-hidden');
      inputAllowed = false;
      $('desktop-panel').hidden = true;
      $('detail').classList.remove('desktop-active'); $('b-desktop').setAttribute('aria-selected', 'false');
      if (global.document && global.document.fullscreenElement === $('desktop-panel') && global.document.exitFullscreen) {
        Promise.resolve(global.document.exitFullscreen()).catch(function () {});
      }
      var ctx = options.getView(); if (ctx && ctx.term && ctx.term.focus) ctx.term.focus();
      if (global.dispatchEvent && global.Event) global.dispatchEvent(new global.Event('resize'));
    }
    function hideSettings() {
      if ($('desktop-connection').hidden) return;
      $('desktop-connection').hidden = true;
      $('desktop-password').value = '';
      $('desktop-command-status').textContent = '';
    }
    function closePanel() {
      hideSettings(); hidePanel(); version++; detach();
    }
    function refresh(ctx) {
      $('b-desktop').hidden = !ctx; $('b-desktop').disabled = !scope(ctx);
      $('b-desktop').title = scope(ctx) ? '' : '请先识别设备，再连接远程桌面';
      if (scope(ctx) === selected) return;
      $('desktop-error').hidden = true; $('desktop-error-details').textContent = '';
      selected = scope(ctx); sessionStage = ''; seatRetried = false; reconnecting = false; loginGuide(''); closePanel(); target = null; sid = ''; previousSession = null;
      availability = null; setupValue = null; candidates = []; candidateId = '';
      detecting = false; savingSetup = false; $('desktop-detect').disabled = false; $('desktop-save-connect').disabled = false;
      $('b-desktop').hidden = !ctx;
      $('b-desktop').disabled = !selected;
      $('b-desktop').title = selected ? '' : '请先识别设备，再连接远程桌面';
      $('desktop-settings').hidden = true;
      $('desktop-connection').hidden = true;
      $('desktop-services').textContent = ''; $('desktop-services-field').hidden = true;
      $('desktop-detect-status').textContent = ''; connectionMessage('');
      $('desktop-host').value = '';
    }
    function bytes(encoded) {
      if (typeof encoded !== 'string' || encoded.length > 32 * 1024 * 1024) throw new Error('桌面画面数据过大或无效');
      var decoded = global.atob(encoded), result = new Uint8Array(decoded.length);
      for (var i = 0; i < decoded.length; i++) result[i] = decoded.charCodeAt(i);
      return result;
    }
    function observeFrame() {
      if (!panelVisible() || !frame || releaseBarrier || needFullFrame || !socket || socket.readyState !== 1) return;
      if (!send({type: 'observed', frame_id: frame.id, sequence: frame.sequence})) throw new Error('画面观察确认未送达');
      inputAllowed = true; surface.setWritable(true); overlay('');
      if (!hasObserved) {
        hasObserved = true; handoverAttempts = 0;
        status('已连接 · 点击画面后控制桌面');
        if (wizardStage()) watchWizard(target, version, socket);
      }
      var pending = pendingIntent; pendingIntent = null;
      if (pending && pending.version === version && pending.socket === socket) dispatchIntent(pending.intent);
    }
    function resumePanel(ctx, generation, connection) {
      if (surface) surface.resize();
      return renderPromise.then(function () {
        if (!visible || !current(ctx, generation) || socket !== connection || !surface) return false;
        return surface.flush();
      }).then(function (painted) {
        if (painted && visible && current(ctx, generation) && socket === connection) observeFrame();
      }).catch(function (error) { if (current(ctx, generation) && socket === connection) stop(error.message); });
    }
    function receiveFrame(message, ctx, generation, connection) {
      if (!ready || !integer(message.sequence, 1, Number.MAX_SAFE_INTEGER) ||
          typeof message.frame_id !== 'string' || !message.frame_id || message.frame_id.length > 200) {
        throw new Error('桌面画面缺少有效的观察标记');
      }
      // Frames already in transit before release confirmation belong to the
      // old observation. The server sends its resync frame after release ack.
      if (releaseBarrier === 'queued' || releaseBarrier === 'sent') return;
      var nextMode = JSON.stringify([message.generation, message.width, message.height]);
      var changed = !!message.reset || !!mode && nextMode !== mode;
      if ((needFullFrame || changed) && (!message.reset || message.x !== 0 || message.y !== 0 ||
          message.rect_width !== message.width || message.rect_height !== message.height)) {
        throw new Error('桌面画面需要完整更新，请重新连接');
      }
      if (message.sequence <= lastSequence && !changed) return;
      if (lastSequence && message.sequence !== lastSequence + 1 && !changed) throw new Error('桌面画面更新不连续，请重新连接');
      if (changed) {
        if (mode) resetObservation(); else renderEpoch++;
      }
      var paintingEpoch = renderEpoch;
      needFullFrame = false;
      mode = nextMode; lastSequence = message.sequence;
      if (++renderPending > 16) throw new Error('画面渲染来不及处理，已停止连接，请重新观察');
      var data = bytes(message.png_base64);
      var rectangle = {x: message.x, y: message.y, width: message.rect_width, height: message.rect_height,
        frame_width: message.width, frame_height: message.height, mime: message.mime};
      renderPromise = renderPromise.then(function () {
        if (!current(ctx, generation) || socket !== connection || !surface || paintingEpoch !== renderEpoch) return false;
        return surface.draw(rectangle, data);
      }).then(function (painted) {
        // The previous canvas may finish decoding after a manual reconnect.
        // Its completion must not decrement this connection's queue budget.
        if (!current(ctx, generation) || socket !== connection) return;
        renderPending--;
        if (!painted || paintingEpoch !== renderEpoch) return;
        frame = {id: message.frame_id, sequence: message.sequence};
        $('desktop-meta').textContent = ctx.serial + ' · ' + message.width + ' × ' + message.height;
        releaseBarrier = '';
        observeFrame();
      }).catch(function (error) { if (current(ctx, generation) && socket === connection) stop(error.message); });
    }
    function receive(message, ctx, generation, connection) {
      if (!current(ctx, generation) || socket !== connection) return;
      if (message.type === 'ready') {
        if (!message.session || message.session.id !== sid || message.session.serial !== ctx.serial) throw new Error('桌面会话身份不一致');
        capabilities = message.capabilities || {};
        ready = true; status('正在等待桌面画面…');
      } else if (message.type === 'frame') receiveFrame(message, ctx, generation, connection);
      else if (message.type === 'observed' || message.type === 'heartbeat' || message.type === 'bound') {
        if (message.ok !== true) throw new Error('桌面连接状态未确认，请重新观察');
      } else if (message.type === 'reset') {
        pendingIntent = null;
        resetObservation('桌面状态已变化，正在等待完整画面重新观察…');
      } else if (message.type === 'ack') {
        if (!inflight || message.seq !== inflight.seq || message.ok !== true) throw new Error('桌面输入回执不一致');
        if (ackTimer) clearTimeout(ackTimer); ackTimer = null;
        var acknowledged = inflight; inflight = null;
        if (acknowledged.release) releaseBarrier = 'awaiting-frame';
        if (acknowledged.event && acknowledged.event.type === 'clipboard') status('文本已发送到远端剪贴板');
        if (acknowledged.event && acknowledged.event.type === 'text') status('文本已发送到当前应用');
        pump();
      } else if (message.type === 'clipboard') {
        if (!panelVisible() || !inflight || !inflight.event || inflight.event.type !== 'clipboard-read') return;
        $('desktop-copy-local').disabled = !message.text || message.available === false;
        if (message.available === false) {
          $('desktop-text').value = ''; status('暂无可读的远端文本剪贴板'); return;
        }
        if (typeof message.text !== 'string') throw new Error('远端剪贴板响应无效');
        $('desktop-text').value = message.text;
        if (inflight.copyLocal && message.text) copyToBrowser(message.text, ctx, generation, connection);
        else status('已读取远端剪贴板');
      } else if (message.type === 'error') {
        if (message.code === 'desktop-stream-stopped') streamEnded(message.error);
        else stop(message.error || '桌面操作结果未确认，请重新连接后观察。');
      }
      else throw new Error('桌面连接返回了未知消息');
    }
    function connect(ctx, generation) {
      detach(); sequence = 0; lastSequence = 0; mode = ''; renderEpoch = 0; renderPending = 0; renderPromise = Promise.resolve();
      needFullFrame = true; capabilities = {}; hasObserved = false;
      overlay('正在连接桌面画面…'); status('正在连接…');
      return libraries(options).then(function (lib) {
        if (!current(ctx, generation)) return;
        // A detached canvas isolates late image decodes from a different board
        // or stream generation. Do not reach into noVNC's private render queue.
        var old = $('desktop-image'), canvas = old.cloneNode(false);
        old.replaceWith(canvas);
        surface = createSurface({canvas: canvas, viewport: $('desktop-screen'), Display: lib.Display, Keyboard: lib.Keyboard,
          onKeyLockUnavailable: function (value) {
            var hint = $('desktop-system-key-hint');
            var insecure = global.isSecureContext === false;
            hint.textContent = translate(insecure ? 'HTTP 无法锁定实体 Win 键，可点 Win / Super。' : '当前无法独占 Win 键。可点 Win / Super 发送到远端。');
            hint.hidden = !(value || insecure && global.document.fullscreenElement === $('desktop-panel'));
          },
          useF8: function () { return $('desktop-f8-super').checked; },
          copyDragEnabled: function () { return $('desktop-drag-copy').checked && (capabilities.clipboard || {}).read; },
          paste:pasteFromBrowser, copy:function () {
            if ((capabilities.clipboard || {}).read) enqueue({type:'clipboard-read'}, false, true);
          },
          send: enqueue, requestAnimationFrame: options.requestAnimationFrame, cancelAnimationFrame: options.cancelAnimationFrame});
        surface.scale($('desktop-scale').value || 'fit');
        var url = new global.URL(query(ctx, 'stream'), global.location.href);
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
        url.username = ''; url.password = '';
        var connection = options.websocketFactory ? options.websocketFactory(url.toString(), 'decklab-desktop-v1') :
          new global.WebSocket(url.toString(), 'decklab-desktop-v1');
        socket = connection;
        connection.onopen = function () {
          if (!current(ctx, generation) || socket !== connection) { connection.close(); return; }
          heartbeat = setInterval(function () {
            if (!send({type: 'heartbeat'})) { stop('桌面连接已断开'); return; }
            // Renew only the frame that completed Display.flush(). A quiet
            // desktop needs no screenshot request to keep observation current.
            if (visible && frame && !(global.document && global.document.hidden)) {
              if (!send({type: 'observed', frame_id: frame.id, sequence: frame.sequence})) stop('画面观察确认未送达');
            }
          }, 5000);
        };
        connection.onmessage = function (event) {
          try {
            if (typeof event.data !== 'string') throw new Error('桌面连接返回了无效消息');
            receive(JSON.parse(event.data), ctx, generation, connection);
          } catch (error) { if (current(ctx, generation) && socket === connection) stop(error.message); }
        };
        connection.onerror = function () { if (current(ctx, generation) && socket === connection) streamEnded('远程桌面已建立，但网页未连上画面通道。请点击「重新连接画面」重试。'); };
        connection.onclose = function () { if (current(ctx, generation) && socket === connection) streamEnded('桌面连接已断开，请手动重新连接画面。'); };
      }).catch(function (error) { if (current(ctx, generation)) stop('桌面连接失败：' + error.message); });
    }
    function connectionMessage(message) { $('desktop-connection-status').textContent = translate(message); }
    function fillSetup() {
      if (!setupValue) return;
      var saved = setupValue.connection || {};
      $('desktop-protocol').value = saved.provider || 'rdp';
      $('desktop-host').value = saved.host || (setupValue.hosts || [])[0] || '';
      $('desktop-port').value = String(saved.port || 3389);
      $('desktop-user').value = saved.username || setupValue.suggested_user || '';
      suggestedGdmUser = false;
      $('desktop-password').value = '';
      $('desktop-password').placeholder = saved.password_saved ? translate('已保存，留空保留') : '';
      $('desktop-certificate-policy').value = saved.certificate_policy || (saved.certificate_sha256 ? 'pin' : 'accept');
      $('desktop-certificate').value = saved.certificate_sha256 || '';
      $('desktop-trust').checked = true; $('desktop-agent').checked = !!saved.allow_agent;
      candidateId = ''; $('desktop-services').value = ''; setupFields();
    }
    function setupFields() {
      var rdp = $('desktop-protocol').value === 'rdp';
      var family = setupValue && setupValue.os_family;
      var username = $('desktop-user');
      if (family === 'debian' && !(setupValue.connection || {}).provider && (!username.value || suggestedGdmUser)) {
        suggestedGdmUser = rdp && Number($('desktop-port').value) === 3389;
        username.value = suggestedGdmUser ? 'rdp' : '';
      }
      $('desktop-user-field').hidden = !rdp; $('desktop-user').required = rdp; $('desktop-certificate-field').hidden = !rdp;
      var pinned = rdp && $('desktop-certificate-policy').value === 'pin';
      $('desktop-pin-field').hidden = !pinned;
      $('desktop-trust-field').hidden = !pinned || !$('desktop-certificate').value.trim();
      $('desktop-rdp-help').hidden = !rdp || !!family && family !== 'debian';
      var stockHelp = {buildroot:'Buildroot 默认不提供远程桌面；自行启用后可手动连接。',
        bianbu:'已连接 USB 调试线的 Bianbu LXQt 可直接打开桌面。手动使用网络连接时，请先在板上启用 VNC 或 RDP。'}[family];
      $('desktop-stock-help').hidden = !stockHelp || !!(setupValue.connection || {}).provider;
      $('desktop-stock-help').textContent = stockHelp ? translate(stockHelp) : '';
    }
    function showSetup(automatic) {
      if (!setupValue) return;
      hidePanel();
      if (options.prepareSetup) options.prepareSetup();
      $('desktop-connection').hidden = false; $('desktop-screen').hidden = true;
      $('desktop-panel').scrollTop = 0; $('desktop-meta').textContent = scope(options.getView());
      $('desktop-input-tools').hidden = true; $('desktop-session-actions').hidden = true;
      $('desktop-clipboard').hidden = true; $('desktop-settings').hidden = false;
      $('desktop-title').textContent = translate('远程桌面');
      $('desktop-save-connect').textContent = translate(sid ? '保存并重新连接' : '保存并连接');
      $('desktop-save-connect').disabled = opening === version || !!detecting || !!savingSetup;
      connectionMessage('');
      if (automatic && setupValue.os_family !== 'buildroot') detectServices();
    }
    function chooseService() {
      var value = candidates.find(function (item) { return item.id === $('desktop-services').value; });
      candidateId = value ? value.id : '';
      if (!value) return;
      var changed = $('desktop-protocol').value !== value.provider || $('desktop-host').value !== value.host || Number($('desktop-port').value) !== value.port;
      $('desktop-protocol').value = value.provider; $('desktop-host').value = value.host; $('desktop-port').value = String(value.port);
      $('desktop-certificate').value = value.certificate_sha256 || ''; $('desktop-trust').checked = true;
      if (changed) $('desktop-password').value = ''; setupFields(); connectionMessage(value.error || '');
    }
    function detectServices(manual) {
      var ctx = options.getView(); if (!scope(ctx) || detecting || savingSetup) return;
      var generation = version, marker = {}; detecting = marker; $('desktop-detect').disabled = true; $('desktop-save-connect').disabled = true;
      $('desktop-detect-status').textContent = translate('正在检测 VNC／RDP…');
      var request = {key: ctx.key, serial: ctx.serial, client_id: options.clientId || ''};
      if (manual === true && $('desktop-host').value.trim()) {
        request.host = $('desktop-host').value.trim(); request.port = Number($('desktop-port').value); request.provider = $('desktop-protocol').value;
      }
      client.request('/api/desktop/discover', request).then(function (data) {
        if (!current(ctx, generation)) return;
        candidates = data.candidates || []; candidateId = '';
        var select = $('desktop-services'); select.textContent = '';
        var manualOption = global.document.createElement('option'); manualOption.value = '';
        manualOption.textContent = translate('手工填写'); select.appendChild(manualOption);
        candidates.forEach(function (item) {
          var option = global.document.createElement('option'); option.value = item.id;
          option.textContent = item.provider.toUpperCase() + ' · ' + item.host + ':' + item.port + (item.error ? ' · ' + item.error : '');
          option.disabled = !!item.error; select.appendChild(option);
        });
        $('desktop-services-field').hidden = !candidates.length;
        $('desktop-detect-status').textContent = translate(data.reason || '已发现可用服务');
        var available = candidates.filter(function (item) { return !item.error; });
        if (!marker.edited && available.length === 1 && (manual === true || !(setupValue.connection || {}).provider)) { select.value = available[0].id; chooseService(); }
      }).catch(function (error) { if (current(ctx, generation)) $('desktop-detect-status').textContent = error.message; })
        .finally(function () { if (detecting === marker) { detecting = false; if (current(ctx, generation)) { $('desktop-detect').disabled = false; $('desktop-save-connect').disabled = !!savingSetup; } } });
    }
    function saveConnection(event) {
      if (event) event.preventDefault();
      var ctx = options.getView(); if (!scope(ctx) || !setupValue || savingSetup || detecting || opening === version) return;
      var generation = version, marker = {}, saved = setupValue.connection || {};
      var provider = $('desktop-protocol').value, host = $('desktop-host').value.trim(), port = Number($('desktop-port').value);
      var username = provider === 'rdp' ? $('desktop-user').value : '';
      var same = saved.provider === provider && saved.host === host && saved.port === port && saved.username === username;
      var policy = provider === 'rdp' ? $('desktop-certificate-policy').value : '';
      var payload = {key:ctx.key, serial:ctx.serial, client_id:options.clientId || '', revision:setupValue.revision,
        provider:provider, host:host, port:port, username:username,
        certificate_policy:policy, certificate_sha256:policy === 'pin' ? $('desktop-certificate').value.trim() : '',
        trust_certificate:policy === 'pin' && $('desktop-trust').checked, allow_agent:$('desktop-agent').checked};
      if (policy === 'pin' && !/^[0-9a-f]{64}$/i.test(payload.certificate_sha256)) {
        connectionMessage('RDP 证书指纹必须是完整的 SHA256'); return;
      }
      if (payload.certificate_sha256 && !payload.trust_certificate && (saved.provider !== provider || saved.host !== host
          || saved.port !== port || (saved.certificate_sha256 || '').toLowerCase() !== payload.certificate_sha256.toLowerCase())) {
        connectionMessage('请核对并确认首次观察到的 RDP 证书指纹'); return;
      }
      if ($('desktop-password').value) payload.password = $('desktop-password').value;
      else if (same && saved.password_saved) payload.reuse_password = true;
      if (candidateId) payload.candidate_id = candidateId;
      savingSetup = marker; $('desktop-save-connect').disabled = true; $('desktop-detect').disabled = true; connectionMessage('正在保存连接设置…');
      var closing = sid;
      var release = closing ? client.request('/api/desktop/close', {key:ctx.key, serial:ctx.serial,
        sid:closing, client_id:options.clientId || ''}).catch(function (error) {
        if (error.status !== 404) throw error;
      }) : Promise.resolve();
      release.then(function () {
        if (!current(ctx, generation)) return;
        if (closing && sid === closing) { sid = ''; detach(); }
        return client.request('/api/desktop/configure', payload);
      }).then(function (data) {
        if (!current(ctx, generation)) return;
        setupValue = data.setup; availability = {supported:true, setup:setupValue}; fillSetup();
        $('desktop-password').value = ''; connectionMessage('连接设置已保存'); open(true, true);
      }).catch(function (error) { if (current(ctx, generation)) connectionMessage(error.message); })
        .finally(function () { if (savingSetup === marker) { savingSetup = false; $('desktop-save-connect').disabled = false; $('desktop-detect').disabled = !!detecting; } });
    }
    function open(force, manual) {
      var ctx = options.getView(); if (!scope(ctx)) return;
      if (!availability) {
        showPanel(); overlay('正在读取桌面连接设置…');
        if (settingsRead) return;
        var waiting = version, controller = new AbortController();
        settingsRead = controller;
        var timer = setTimeout(function () { controller.abort(); }, options.settingsTimeout || 10000);
        client.request(query(ctx, 'sessions'), undefined, {signal:controller.signal}).then(function (data) {
          if (settingsRead !== controller || !current(ctx, waiting)) return;
          availability = data; setupValue = data.setup || null;
          if (setupValue) fillSetup();
          open(force, manual);
        }).catch(function (error) {
          if (settingsRead !== controller || !current(ctx, waiting)) return;
          var message = controller.signal.aborted ? '读取桌面设置超时，请点击「重新连接画面」重试。' : error.message;
          overlay(message); status(message);
        }).finally(function () {
          clearTimeout(timer);
          if (settingsRead === controller) settingsRead = null;
        });
        return;
      }
      var quick = !manual && setupValue && setupValue.quick_connect && options.canQuickConnect && options.canQuickConnect();
      if (!quick && !availability.supported && setupValue) { showSetup(true); return; }
      $('desktop-connection').hidden = true; $('desktop-screen').hidden = false; $('desktop-input-tools').hidden = false;
      $('desktop-session-actions').hidden = false; $('desktop-settings').hidden = !setupValue;
      if (opening === version) return;
      if (force !== true && visible && ready && surface && target && target.serial === ctx.serial) { surface.focus(); return; }
      if (force !== true && target && target.serial === ctx.serial && socket && socket.readyState < 2) {
        showPanel(); resumePanel(target, version, socket); return;
      }
      showPanel(); version++; var generation = version;
      target = {key: ctx.key, serial: ctx.serial}; previousSession = null; $('desktop-replace').hidden = true;
      if (sid && force !== true) { connect(target, generation); return; }
      overlay('正在连接远程桌面…'); status('正在连接…');
      $('desktop-error').hidden = true; $('desktop-error-details').textContent = '';
      loginGuide('');
      opening = generation;
      client.request(quick ? '/api/desktop/quick-open' : '/api/desktop/open',
        {key: ctx.key, serial: ctx.serial, client_id: options.clientId || ''}).then(function (data) {
        if (!current(ctx, generation)) return;
        if (data.manual) {
          if (handoverAttempts && scheduleHandover()) return;
          handoverAttempts = 0; showSetup(true);
          connectionMessage(loginGuide(data.login_stage) || '暂未能自动取得桌面入口，请选择服务并填写入口凭据。');
          return;
        }
        sessionStage = data.session.login_stage || ''; loginGuide(sessionStage);
        sid = data.session.id;
        if (!$('desktop-connection').hidden) {
          $('desktop-save-connect').textContent = translate('保存并重新连接'); return;
        }
        return connect(target, generation);
      }).catch(function (error) {
        if (!current(ctx, generation)) return;
        var transient = ['desktop-connection-failed', 'desktop-connection-timeout', 'desktop-connection-refused',
          'desktop-network-failed', 'desktop-tls-failed'];
        if (handoverAttempts && transient.includes((error.result || {}).code) && scheduleHandover()) return;
        handoverAttempts = 0; connectionError(error);
        return client.request(query(ctx, 'sessions')).then(function (data) {
          if (!current(ctx, generation)) return;
          previousSession = data.sessions && data.sessions[0]; $('desktop-replace').hidden = !previousSession;
        }).catch(function () {});
      }).finally(function () {
        if (opening !== generation) return;
        opening = null; $('desktop-save-connect').disabled = !!detecting || !!savingSetup;
      });
    }
    function dismissSetup(resume) {
      if ($('desktop-connection').hidden) return;
      if (resume === true && sid && !savingSetup) {
        hideSettings(); $('desktop-screen').hidden = false;
        open(); return;
      }
      closePanel(); availability = null; detecting = false; savingSetup = false;
      $('desktop-detect').disabled = false; $('desktop-save-connect').disabled = false;
    }
    $('desktop-setup-close').addEventListener('click', function () { dismissSetup(true); });
    $('desktop-credentials-stage').addEventListener('click', function () {
      if (options.stageSerialCommand) $('desktop-command-status').textContent = translate(options.stageSerialCommand($('desktop-credentials-command').textContent));
    });
    $('desktop-credentials-copy').addEventListener('click', function () {
      if (options.copyText) $('desktop-command-status').textContent = translate(options.copyText($('desktop-credentials-command').textContent, true) ? '已复制' : '复制失败，请手动选择命令');
    });
    $('b-desktop').addEventListener('click', function () { if (!$('desktop-connection').hidden) { $('desktop-host').focus(); return; } if (!visible && !sid) availability = null; open(); });
    $('desktop-settings').addEventListener('click', function () {
      if (setupValue) { fillSetup(); showSetup(false); return; }
      var ctx = options.getView(), generation = version;
      client.request(query(ctx, 'sessions')).then(function (data) {
        if (!current(ctx, generation)) return;
        availability = data; setupValue = data.setup || null; fillSetup(); showSetup(false);
      }).catch(function (error) { if (current(ctx, generation)) status(error.message); });
    });
    $('desktop-detect').addEventListener('click', function () { detectServices(true); });
    $('desktop-error-copy').addEventListener('click', function () {
      if (options.copyText && options.copyText($('desktop-error-details').textContent, true)) status('错误详情已复制');
    });
    $('desktop-connect-without-pin').addEventListener('click', function () {
      if ($('desktop-connect-without-pin').hidden || !setupValue || savingSetup) return;
      fillSetup(); $('desktop-certificate-policy').value = 'accept'; setupFields();
      showSetup(false); saveConnection();
    });
    $('desktop-services').addEventListener('change', chooseService);
    $('desktop-connection').addEventListener('submit', saveConnection);
    $('desktop-connection').addEventListener('input', function () { if (detecting) detecting.edited = true; });
    $('desktop-user').addEventListener('input', function () { suggestedGdmUser = false; });
    ['desktop-host', 'desktop-port'].forEach(function (id) { $(id).addEventListener('input', function () {
      candidateId = ''; $('desktop-services').value = ''; $('desktop-certificate').value = ''; $('desktop-trust').checked = false; setupFields();
    }); });
    ['desktop-host', 'desktop-port', 'desktop-protocol'].forEach(function (id) { $(id).addEventListener('change', function () {
      candidateId = ''; $('desktop-services').value = ''; $('desktop-certificate').value = ''; $('desktop-trust').checked = false;
      if (id === 'desktop-protocol') $('desktop-port').value = $('desktop-protocol').value === 'rdp' ? '3389' : '5900';
      setupFields();
    }); });
    $('desktop-certificate').addEventListener('input', function () { candidateId = ''; $('desktop-trust').checked = true; setupFields(); });
    $('desktop-certificate-policy').addEventListener('change', setupFields);
    $('d-tabs').addEventListener('click', function (event) {
      if (visible) hidePanel();
      if (event.target && event.target.dataset && event.target.dataset.kind && event.target.dataset.kind !== 'serial') dismissSetup();
    }, true);
    $('desktop-close').addEventListener('click', hidePanel);
    function reconnect() {
      clearTimeout(seatRetry); seatRetry = null;
      if (reconnecting || opening === version) return;
      if (!sid || !target) { open(true); return; }
      var ctx = target, generation = version, closing = sid;
      reconnecting = true;
      detach();
      client.request('/api/desktop/close', {key:ctx.key, serial:ctx.serial, sid:closing, client_id:options.clientId || ''})
        .catch(function (error) { if (error.status !== 404) throw error; })
        .then(function () {
          if (!current(ctx, generation) || sid !== closing) return;
          sid = ''; if (panelVisible()) open(true);
        }).catch(function (error) { if (current(ctx, generation)) stop(error.message); })
        .finally(function () { if (selected === ctx.serial) reconnecting = false; });
    }
    function wizardStage() { return sessionStage === 'initial' || sessionStage === 'finishing'; }
    function watchWizard(ctx, generation, connection) {
      if (!current(ctx, generation) || socket !== connection || !wizardStage()) return;
      stageTimer = setTimeout(async function () {
        if (!current(ctx, generation) || socket !== connection) return;
        if (panelVisible()) {
          try {
            var data = await client.request('/api/desktop/login-stage',
              {key:ctx.key, serial:ctx.serial, sid:sid, client_id:options.clientId || ''});
            if (!current(ctx, generation) || socket !== connection) return;
            if (data.login_stage === 'login' && panelVisible()) {
              streamEnded('正在从登录界面切换到桌面…'); return;
            }
            if (data.login_stage === 'finishing') { sessionStage = 'finishing'; loginGuide(sessionStage); }
          } catch (_) { /* The display can keep working while USB is unavailable. */ }
        }
        watchWizard(ctx, generation, connection);
      }, options.stageCheckDelay || 5000);
    }
    function scheduleHandover() {
      if (!panelVisible() || handoverAttempts >= 5) return false;
      var ctx = target, generation = version;
      clearTimeout(seatRetry);
      seatRetry = setTimeout(function () {
        if (current(ctx, generation) && panelVisible() && !socket) reconnect();
      }, (options.seatRetryDelay || 1800) * ++handoverAttempts);
      overlay('正在从登录界面切换到桌面…'); status('');
      return true;
    }
    function streamEnded(message) {
      var wizard = wizardStage() && (hasObserved || handoverAttempts > 0) && panelVisible();
      var handover = sessionStage === 'lxqt-greeter' && hasObserved && panelVisible() && !inflight && !seatRetried;
      var ctx = target, generation = version;
      stop(handover ? '正在从登录界面切换到桌面…' : message);
      if (wizard && scheduleHandover()) return;
      if (handover) {
        seatRetried = true;
        seatRetry = setTimeout(function () {
          if (current(ctx, generation) && panelVisible() && !socket) reconnect();
        }, options.seatRetryDelay || 1800);
      }
    }
    $('desktop-refresh').addEventListener('click', function () {
      seatRetried = false; handoverAttempts = 0; reconnect();
    });
    $('desktop-disconnect').addEventListener('click', function () {
      clearTimeout(seatRetry); seatRetry = null; handoverAttempts = 0;
      if (!target || !sid) return;
      var ctx = target, generation = version, closing = sid;
      detach();
      function closed() {
        if (!current(ctx, generation)) return;
        sid = ''; overlay('桌面连接已断开'); status('点击“重新连接画面”可重新连接');
      }
      client.request('/api/desktop/close', {key: ctx.key, serial: ctx.serial, sid: closing, client_id: options.clientId || ''}).then(closed, function (error) {
        // The server already dropped a session whose remote desktop ended; keeping its id would block reconnects and settings.
        if (error.status === 404) { closed(); return; }
        if (current(ctx, generation)) stop(error.message);
      });
    });
    $('desktop-replace').addEventListener('click', function () {
      if (!target || !previousSession) return;
      var ctx = target, previous = previousSession, generation = version;
      if (!global.confirm(translate('断开此设备的原桌面连接并重新连接？') + '\n' + ctx.serial)) return;
      client.request('/api/desktop/close', {key: ctx.key, serial: ctx.serial, sid: previous.id,
        takeover: true, client_id: options.clientId || ''}).then(function () {
          if (!current(ctx, generation)) return;
          sid = ''; hidePanel(); open();
        }).catch(function (error) { if (current(ctx, generation)) status(error.message); });
    });
    $('desktop-release').addEventListener('click', function () { if (surface) surface.release('manual'); });
    $('desktop-move-wizard').addEventListener('click', function () {
      toolbarIntent({codes: ['MetaLeft', 'ShiftLeft', 'ArrowRight']});
    });
    $('desktop-super').addEventListener('click', function () { toolbarIntent({codes: ['MetaLeft']}); });
    $('desktop-key').addEventListener('click', function () { toolbarIntent({codes: $('desktop-keys').value.split(' ')}); });
    $('desktop-scale').addEventListener('change', function () { if (surface) surface.scale($('desktop-scale').value); });
    $('desktop-fullscreen').addEventListener('click', function () {
      var panel = $('desktop-panel'), document = global.document;
      if (!panel.requestFullscreen || !document) { status('当前浏览器不支持全屏'); return; }
      var change = document.fullscreenElement === panel ? document.exitFullscreen() : panel.requestFullscreen();
      Promise.resolve(change).then(function () { if (surface && document.fullscreenElement === panel) { surface.focus(); surface.systemKeys(); } }).catch(function () { status('浏览器未允许全屏，可继续在当前页面操作。'); });
    });
    if (global.document) {
      global.document.addEventListener('fullscreenchange', function () {
        fullscreenLabel();
        if (surface) { surface.resize(); surface.systemKeys(); }
      });
      global.document.addEventListener('visibilitychange', function () {
        if (global.document.hidden && surface) surface.release('page-hidden');
        else if (visible && target && socket) resumePanel(target, version, socket);
      });
    }
    if (global.addEventListener) {
      global.addEventListener('blur', function () { if (surface) surface.release('window-blur'); });
      global.addEventListener('decklab-language-change', function () { status(statusValue); overlay(overlayValue); fullscreenLabel(); });
    }
    $('desktop-clipboard-toggle').addEventListener('click', function () {
      if (!$('desktop-clipboard').hidden) {
        closeClipboard();
        if (surface) { surface.resize(); surface.focus(); }
        return;
      }
      revealClipboard(false);
      $('desktop-paste-submit').disabled = !((capabilities.clipboard || {}).write || capabilities.text);
      if ((capabilities.clipboard || {}).read) toolbarIntent({event: {type: 'clipboard-read'}});
    });
    $('desktop-copy-local').addEventListener('click', function () {
      var text = $('desktop-text').value;
      if (text && target && socket) copyToBrowser(text, target, version, socket);
    });
    $('desktop-paste-submit').addEventListener('click', async function () {
      if (!panelVisible() || !socket || !target) return;
      var ctx = target, generation = version, connection = socket, field = $('desktop-paste-text');
      var text = field.value;
      if (!text) {
        if (localClipboardRead) return;
        var reading = {}; localClipboardRead = reading;
        try { text = await global.navigator.clipboard.readText(); } catch (_) { /* Native paste works on HTTP. */ }
        if (localClipboardRead !== reading) return;
        localClipboardRead = null;
        if (!current(ctx, generation) || socket !== connection || !panelVisible() || $('desktop-clipboard').hidden) return;
        if (!text) { field.focus(); status('请在文本框粘贴，再点「粘贴到远端」'); return; }
      }
      toolbarIntent({paste:text});
    });
    return {refresh: refresh, close: closePanel, showTerminal: function () {
      dismissSetup(); if (visible) hidePanel();
    }};
  }
  global.DeckLabDesktop = {create: create, createSurface: createSurface};
})(window);
