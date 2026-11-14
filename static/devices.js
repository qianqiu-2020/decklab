// Device directory, filtering, polling and identity transitions.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fetch = options.fetch, fmt = options.formatBytes, ago = options.age;
    var selKey = null, lastDevices = [], lastInfoIdentity = '';
    var ownershipRevision = 0, pollSeq = 0, requestedDevice = '';
    var orderStorageKey = 'decklab.device-order.v1', deviceOrder = [], visibleOrder = [];
    var orderElements = {}, draggedId = '', renderAfterDrag = false;
    var dragPreview = null, dragAnimations = [];
    try {
      var savedOrder = JSON.parse(global.localStorage && global.localStorage.getItem(orderStorageKey) || 'null');
      if (savedOrder && savedOrder.version === 1 && Array.isArray(savedOrder.ids)) {
        deviceOrder = savedOrder.ids.filter(function (id, index, ids) {
          return typeof id === 'string' && id.length <= 2048 && /^(board|port|connection|key):/.test(id)
            && ids.indexOf(id) === index;
        }).slice(0, 4096);
      }
    } catch (_) { /* Private browsing and damaged preferences do not block devices. */ }
    try {
      var sharedMatch = /^#device=(.*)$/.exec(global.location && global.location.hash || '');
      if (sharedMatch) requestedDevice = decodeURIComponent(sharedMatch[1]);
    } catch (_) { /* A malformed bookmark must not break device discovery. */ }
    function orderId(device) {
      return device.serial ? 'board:' + device.serial : device.by_path ? 'port:' + device.by_path
        : device.connection_id ? 'connection:' + device.connection_id : 'key:' + device.key;
    }
    function saveOrder() {
      try {
        if (global.localStorage) global.localStorage.setItem(orderStorageKey,
          JSON.stringify({ version: 1, ids: deviceOrder }));
      } catch (_) { /* The current page still keeps its layout when storage is unavailable. */ }
    }
    function reconcileOrder(devices) {
      var previous = JSON.stringify(deviceOrder);
      // Only a proven live board may adopt its pending attachment's position.
      // An offline archive's last_path is not evidence about the current board.
      devices.forEach(function (device) {
        if (!device.serial || !device.alive || !device.by_path) return;
        var pendingId = 'port:' + device.by_path, position = deviceOrder.indexOf(pendingId);
        if (position < 0) return;
        var identifiedId = orderId(device);
        if (deviceOrder.indexOf(identifiedId) < 0) deviceOrder[position] = identifiedId;
        else deviceOrder.splice(position, 1);
      });
      devices.slice().sort(function (a, b) {
        function rank(device) { return device.alive ? (device.state === 'pending' ? 1 : 0) : 2; }
        return rank(a) - rank(b);
      }).forEach(function (device) {
        var id = orderId(device);
        if (deviceOrder.indexOf(id) < 0) deviceOrder.push(id);
      });
      if (deviceOrder.length > 4096) {
        var currentIds = devices.map(orderId);
        deviceOrder = deviceOrder.filter(function (id) { return currentIds.indexOf(id) >= 0; });
      }
      if (JSON.stringify(deviceOrder) !== previous) saveOrder();
    }
    function focusOrder(id, control, preventScroll) {
      var elements = orderElements[id];
      if (!elements) return;
      var target = elements[control] || elements.card;
      if (target.disabled) target = elements.card;
      target.focus(preventScroll ? { preventScroll: true } : undefined);
    }
    function applyVisibleOrder(next, id, control) {
      var previous = visibleOrder.slice(), cursor = 0;
      deviceOrder = deviceOrder.map(function (item) {
        return previous.indexOf(item) >= 0 ? next[cursor++] : item;
      });
      saveOrder(); renderDevices(lastDevices); focusOrder(id, control);
    }
    function moveDevice(id, direction, control) {
      var index = visibleOrder.indexOf(id), target = index + direction;
      if (index < 0 || target < 0 || target >= visibleOrder.length) return;
      var next = visibleOrder.slice(), swapped = next[target];
      next[target] = id; next[index] = swapped;
      applyVisibleOrder(next, id, control);
    }
    function finishDrag() {
      var id = draggedId;
      draggedId = ''; dragPreview = null;
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      if (renderAfterDrag || id) {
        renderAfterDrag = false; renderDevices(lastDevices); focusOrder(id, 'drag');
      }
    }
    function previewDrag(card, event) {
      if (!draggedId || card.dataset.orderId === draggedId) return;
      var moving = orderElements[draggedId] && orderElements[draggedId].card;
      if (!moving) return;
      var next = (dragPreview || visibleOrder).filter(function (id) { return id !== draggedId; });
      var target = next.indexOf(card.dataset.orderId);
      if (target < 0) return;
      var rect = card.getBoundingClientRect();
      // The sidebar is vertical; overview grids and mobile strips can have
      // several cards on one row. Use the corresponding insertion midpoint.
      var horizontal = visibleOrder.some(function (id) {
        var other = orderElements[id].card.getBoundingClientRect();
        return id !== card.dataset.orderId && Math.abs(other.top - rect.top) < 2 && Math.abs(other.left - rect.left) > 2;
      });
      var after = horizontal && typeof event.clientX === 'number' && event.clientX >= rect.left && event.clientX <= rect.right
        ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
      if (after) target++;
      next.splice(target, 0, draggedId);
      if (JSON.stringify(next) === JSON.stringify(dragPreview || visibleOrder)) return;
      var before = {};
      visibleOrder.forEach(function (id) { before[id] = orderElements[id].card.getBoundingClientRect(); });
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      var following = next[next.indexOf(draggedId) + 1];
      $('grid').insertBefore(moving, following ? orderElements[following].card : null);
      dragPreview = next;
      if (global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      visibleOrder.forEach(function (id) {
        var node = orderElements[id].card, end = node.getBoundingClientRect(), start = before[id];
        if (id === draggedId || !node.animate || start.left === end.left && start.top === end.top) return;
        dragAnimations.push(node.animate([
          { transform: 'translate(' + (start.left - end.left) + 'px,' + (start.top - end.top) + 'px)' },
          { transform: 'translate(0,0)' }
        ], { duration: 140, easing: 'cubic-bezier(.2,.7,.2,1)' }));
      });
    }
    function commitDrag() {
      if (!draggedId) return;
      var moving = draggedId, next = (dragPreview || visibleOrder).slice();
      if (JSON.stringify(next) === JSON.stringify(visibleOrder)) { finishDrag(); return; }
      draggedId = ''; dragPreview = null; renderAfterDrag = false;
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      applyVisibleOrder(next, moving, 'drag');
    }
    function previewGrid(event) {
      var closest = null, distance = Infinity;
      visibleOrder.forEach(function (id) {
        if (id === draggedId) return;
        var card = orderElements[id].card, r = card.getBoundingClientRect();
        var dx = Math.max(r.left - event.clientX, 0, event.clientX - r.right);
        var dy = Math.max(r.top - event.clientY, 0, event.clientY - r.bottom);
        if (dx * dx + dy * dy < distance) { closest = card; distance = dx * dx + dy * dy; }
      });
      if (closest) previewDrag(closest, event);
    }
    $('grid').addEventListener('dragover', function (event) {
      if (!draggedId) return;
      event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      previewGrid(event);
    });
    $('grid').addEventListener('drop', function (event) {
      if (!draggedId) return;
      event.preventDefault(); event.stopPropagation(); if (!dragPreview) previewGrid(event); commitDrag();
    });
    function orderKeys(event, id, control) {
      if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
        event.preventDefault(); event.stopPropagation();
        moveDevice(id, event.key === 'ArrowUp' ? -1 : 1, control);
        return true;
      }
      return false;
    }
    function renderDevices(list) {
      $('count-online').textContent = list.filter(function (d) { return d.serial && d.alive; }).length;
      $('count-history').textContent = list.filter(function (d) { return d.serial && !d.alive; }).length;
      $('count-pending').textContent = list.filter(function (d) { return d.state === 'pending'; }).length;
      if (draggedId) { renderAfterDrag = true; return; }
      reconcileOrder(list);
      var query = $('device-search').value.toLowerCase();
      var filter = $('device-filter').value || 'all';
      list = list.filter(function (d) {
        return (filter === 'all' || d.state === filter) &&
          [d.label, d.serial, d.hostname, d.by_path, d.last_path, d.dev].join(' ').toLowerCase().includes(query);
      });
      list = list.slice().sort(function (a, b) { return deviceOrder.indexOf(orderId(a)) - deviceOrder.indexOf(orderId(b)); });
      var focusBefore = null;
      Object.keys(orderElements).some(function (id) {
        return Object.keys(orderElements[id]).some(function (control) {
          if (orderElements[id][control] !== document.activeElement) return false;
          focusBefore = { id: id, control: control }; return true;
        });
      });
      visibleOrder = list.map(orderId); orderElements = {};
      var g = $('grid');
      if (!list.length) { g.innerHTML = '<p class="empty">没有匹配的设备。新串口连接后将显示在待识别列表。</p>'; return; }
      g.textContent = '';
      list.forEach(function (d, index) {
        var health = d.serial_health || {};
        var healthFailed = d.alive && health.status === 'error';
        var flashOnly = !!(d.serial && d.flash_connected && !d.alive);
        var card = document.createElement('div');
        card.className = 'dev' + (!d.alive ? ' offline' : '') + (d.key === selKey ? ' sel' : '');
        card.dataset.key = d.key;          // 供 markSelected() 就地改高亮用
        var id = orderId(d); card.dataset.orderId = id;
        card.title = (d.by_path || d.last_path || '')
          + (d.alive ? '\n采集检查：' + ago(d.last_health_check)
            + '\n串口收发验证：' + ago(health.last_success)
            + (health.reason ? '\n' + health.reason : '') : '');
        card.tabIndex = 0;
        card.addEventListener('click', function () { options.open(d.key); });
        card.addEventListener('keydown', function (e) {
          if (e.target && e.target !== card) return;
          if (orderKeys(e, id, 'card')) return;
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); options.open(d.key); }
        });
        card.addEventListener('dragover', function (e) {
          if (!draggedId) return;
          e.preventDefault(); e.stopPropagation(); previewDrag(card, e);
          if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        });
        card.addEventListener('drop', function (e) {
          if (!draggedId) return;
          e.preventDefault(); e.stopPropagation();
          if (!dragPreview) previewDrag(card, e); commitDrag();
        });

        var r1 = document.createElement('div'); r1.className = 'r1';
        var nm = document.createElement('span'); nm.className = 'nm';
        if (d.state === 'pending' && !d.attachment_label) {
          nm.className += ' pending-name';
          var pendingLabel = document.createElement('span'); pendingLabel.textContent = '待识别 · ';
          var portLabel = document.createElement('span'); portLabel.dataset.i18nIgnore = '';
          portLabel.textContent = d.hostname || (d.dev || '').split('/').pop();
          nm.appendChild(pendingLabel); nm.appendChild(portLabel);
        } else { nm.textContent = d.attachment_label || d.label; nm.dataset.i18nIgnore = ''; }
        var st = document.createElement('span');
        st.className = 'pill ' + (healthFailed ? 'dead' : (flashOnly ? 'flash' : (d.state === 'pending' ? 'pending' : (d.alive ? 'live' : 'dead'))));
        st.textContent = healthFailed ? '探测异常' : (flashOnly ? 'USB 下载模式' : (d.state === 'pending' ? 'USB 临时身份' : (d.alive ? '在线' : '历史设备')));
        r1.appendChild(nm); r1.appendChild(st);
        if (d.hostname && d.hostname !== d.label) {
          var sn = document.createElement('span'); sn.className = 'pill sniff';
          sn.textContent = d.hostname; r1.appendChild(sn);
        }
        card.appendChild(r1);

        var meta = document.createElement('div'); meta.className = 'meta';
        var a = document.createElement('span');
        a.textContent = flashOnly ? '串口离线，刷机 USB 已连接' : d.dev || ('上次连接 ' + (d.last_dev || '—'));
        var b = document.createElement('span'); b.textContent = d.serial ? 'SN ' + d.serial : (d.by_path || '尚未绑定开发板序列号');
        meta.appendChild(a); meta.appendChild(b);
        if (d.serial && !d.alive && !flashOnly && d.flash_last_seen) {
          var flashSeen = document.createElement('span');
          flashSeen.textContent = '上次检测刷机 USB：' + ago(d.flash_last_seen);
          meta.appendChild(flashSeen);
        }
        card.appendChild(meta);

        var last = document.createElement('div'); last.className = 'last' + (d.last_line ? '' : ' empty-output');
        last.textContent = d.last_line || '（暂无输出）';
        card.appendChild(last);

        var stat = document.createElement('div'); stat.className = 'stat';
        var s1 = document.createElement('span'); s1.textContent = fmt(d.bytes) + ' 记录';
        var s2 = document.createElement('span'); s2.textContent = '末次 ' + ago(d.last_rx || d.last_seen);
        var s3 = document.createElement('span'); s3.className = 'attachment'; s3.textContent = d.by_path || d.last_path || '';
        stat.appendChild(s1); stat.appendChild(s2); stat.appendChild(s3);
        card.appendChild(stat);
        if (d.alive && (d.last_rx || d.capture_started)
            && Date.now() / 1000 - (d.last_rx || d.capture_started) > 300) {
          var quiet = document.createElement('div'); quiet.className = 'quiet-note';
          quiet.textContent = '串口已静默 ' + ago(d.last_rx || d.capture_started)
            + '；采集器仍在检查连接，板子也可能只是没有输出';
          card.appendChild(quiet);
        }

        if (d.rescue_note) {                       // 自愈结果挂到卡片上,别让人去翻日志
          var rn = document.createElement('div'); rn.className = 'last';
          rn.style.color = 'var(--dead)';
          rn.textContent = d.rescue_note;
          card.appendChild(rn);
        }
        if (d.log_error) {
          var logWarning = document.createElement('div'); logWarning.className = 'identity-error';
          logWarning.textContent = '串口日志写入失败：' + d.log_error;
          card.appendChild(logWarning);
        }
        if (healthFailed) {
          var healthWarning = document.createElement('div'); healthWarning.className = 'identity-error';
          healthWarning.textContent = health.reason;
          card.appendChild(healthWarning);
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

        var controls = document.createElement('div'); controls.className = 'dev-order-controls';
        controls.setAttribute('role', 'group'); controls.setAttribute('aria-label', '设备顺序（仅当前浏览器）');
        controls.title = '顺序保存在当前浏览器，拖动或使用上下移动按钮调整';
        var elements = { card: card }; orderElements[id] = elements;
        function orderButton(control, glyph, label, action) {
          var button = document.createElement('button'); button.type = 'button';
          button.className = 'dev-order-button'; button.textContent = glyph;
          button.dataset.orderControl = control;
          button.setAttribute('aria-label', label); button.title = label;
          button.addEventListener('click', function (e) {
            e.stopPropagation();
            if (action) action();
          });
          button.addEventListener('keydown', function (e) {
            orderKeys(e, id, control); e.stopPropagation();
          });
          elements[control] = button; controls.appendChild(button); return button;
        }
        var drag = orderButton('drag', '↕', '拖动调整设备顺序'); drag.draggable = true;
        drag.addEventListener('dragstart', function (e) {
          draggedId = id; dragPreview = null;
          if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', id);
            if (e.dataTransfer.setDragImage) {
              var rect = card.getBoundingClientRect();
              e.dataTransfer.setDragImage(card, Math.max(0, (e.clientX || rect.left) - rect.left),
                Math.max(0, (e.clientY || rect.top) - rect.top));
            }
          }
          card.classList.toggle('order-dragging', true);
          e.stopPropagation();
        });
        drag.addEventListener('dragend', finishDrag);
        var up = orderButton('up', '↑', '上移设备（Alt+↑）', function () { moveDevice(id, -1, 'up'); });
        var down = orderButton('down', '↓', '下移设备（Alt+↓）', function () { moveDevice(id, 1, 'down'); });
        up.disabled = index === 0; down.disabled = index === list.length - 1;
        card.appendChild(controls);

        g.appendChild(card);
      });
      if (focusBefore) focusOrder(focusBefore.id, focusBefore.control, true);
    }

    function pollDevices() {
      var seq = ++pollSeq;
      var ownerRevision = ownershipRevision;
      fetch('/api/devices', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (seq !== pollSeq) return;
          $('conn-dot').className = 'dot';
          $('conn-txt').textContent = '已连接 · ' + (d.devices || []).length + ' 台设备 · '
            + new Date().toLocaleTimeString('zh-CN');
          if (d.storage && d.storage.warning) {
            $('conn-txt').textContent += d.storage.available ? ' · 日志磁盘空间不足' : ' · 日志磁盘状态不可用';
            $('conn-dot').className = 'dot bad';
          }
          lastDevices = d.devices || [];
          renderDevices(lastDevices);
          if (requestedDevice) {
            var sharedBoard = lastDevices.find(function (board) {
              return board.serial === requestedDevice || board.key === requestedDevice;
            });
            if (sharedBoard) options.open(sharedBoard.key);
          }
          // 轮询里同步选中设备的识别结果,但只在**结果真的变了**时重绘 ——
          // 否则每 3 秒重建一次 DOM 会打断用户选中文本
          if (selKey) {
            var ctx = options.getView();
            var cur = lastDevices.find(function (board) {
              return ctx && ctx.serial ? board.serial === ctx.serial
                : ctx && ctx.connectionId ? board.connection_id === ctx.connectionId : board.key === selKey;
            });
            // A collector restart changes connection_id.  If a pending connection was
            // promoted meanwhile, follow the same live physical attachment to its
            // canonical key instead of leaving an unusable stale detail panel open.
            if (!cur && ctx && !ctx.serial && ctx.byPath) {
              var samePath = lastDevices.filter(function (x) {
                return x.alive && x.by_path === ctx.byPath;
              });
              if (samePath.length === 1) cur = samePath[0];
            }
            if (cur && cur.key !== selKey) adoptKey(ctx, cur.key, cur.serial);
            ctx = options.getView(); // Identity adoption can replace the terminal.
            if (cur && options.isCurrent(ctx)) {
              if (cur.state === 'pending') $('d-name').textContent = cur.label || cur.key;
              if (ownerRevision === ownershipRevision && !ctx.ownerPending) options.updateInputOwner(ctx, cur.input_owner);
              else cur.input_owner = ctx.inputOwner;
            }
            var info = cur && cur.info;
            var infoIdentity = cur ? [cur.key, cur.connection_id, info && info.ts || 0,
              info && info.serial_source || '', info && info.identity_status || ''].join('|') : '';
            if (infoIdentity !== lastInfoIdentity) {
              lastInfoIdentity = infoIdentity;
              options.renderInfo(info);
            }
          }
          options.loadSessions();
          options.loadActivity();
        })
        .catch(function () {
          if (seq !== pollSeq) return;
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

    function adoptKey(ctx, key, serial) {
      if (!options.isCurrent(ctx) || !key || key === ctx.key) return;
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
      options.open(key);
    }

    function rememberInfo(board) {
      var info = board && board.info;
      lastInfoIdentity = board ? [board.key, board.connection_id, info && info.ts || 0,
        info && info.serial_source || '', info && info.identity_status || ''].join('|') : '';
    }
    $('device-search').addEventListener('input', function () { renderDevices(lastDevices); });
    $('device-filter').addEventListener('change', function () { renderDevices(lastDevices); });
    var controller = {
      poll: pollDevices, markSelected: markSelected, adopt: adoptKey,
      selectionChanged: function (key) { selKey = key; requestedDevice = ''; },
      rememberInfo: rememberInfo,
      advanceOwnership: function () { ownershipRevision++; },
      find: function (key) { return lastDevices.find(function (board) { return board.key === key; }); }
    };
    Object.defineProperty(controller, 'items', { get: function () { return lastDevices; } });
    Object.defineProperty(controller, 'selected', { get: function () { return selKey; } });
    return controller;
  }
  global.SerialdDevices = { create: create };
})(window);
