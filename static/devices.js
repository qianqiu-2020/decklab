// Device directory, filtering, polling and identity transitions.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fetch = options.fetch, fmt = options.formatBytes, ago = options.age;
    var selKey = null, lastDevices = [], lastInfoIdentity = '';
    var ownershipRevision = 0, pollSeq = 0, requestedDevice = '';
    var orderStorageKey = 'decklab.device-order.v1', deviceOrder = [], visibleOrder = [];
    var directoryOrder = [], manualOrder = false;
    var orderElements = {}, draggedId = '', renderAfterDrag = false;
    var dragPreview = null, dragAnimations = [];
    var grid = $('grid'), edgeTimer = null, dragPoint = null;
    function stopEdgeScroll() {
      if (edgeTimer !== null) global.clearInterval(edgeTimer);
      edgeTimer = null; dragPoint = null;
    }
    function edgeStep(position, start, end) {
      var band = Math.min(48, (end - start) / 4);
      var depth = position < start + band ? position - start - band : Math.max(0, position - end + band);
      return band > 0 ? Math.round(24 * Math.max(-1, Math.min(1, depth / band))) : 0;
    }
    function scrollAtEdge() {
      if (!draggedId || !dragPoint) return stopEdgeScroll();
      var rect = grid.getBoundingClientRect(), left = grid.scrollLeft, top = grid.scrollTop;
      var dx = grid.scrollWidth > grid.clientWidth ? edgeStep(dragPoint.clientX, rect.left, rect.left + grid.clientWidth) : 0;
      var dy = grid.scrollHeight > grid.clientHeight ? edgeStep(dragPoint.clientY, rect.top, rect.top + grid.clientHeight) : 0;
      grid.scrollBy(dx, dy);
      if (dx || dy) previewGrid(dragPoint, false);
      if (grid.scrollLeft === left && grid.scrollTop === top) stopEdgeScroll();
    }
    function nearGridEdge(event) {
      var rect = grid.getBoundingClientRect(), x = event.clientX, y = event.clientY;
      var nearVerticalEdge = grid.scrollHeight > grid.clientHeight && x >= rect.left && x <= rect.right
        && y >= rect.top - 32 && y <= rect.bottom + 32;
      var nearHorizontalEdge = grid.scrollWidth > grid.clientWidth && y >= rect.top && y <= rect.bottom
        && x >= rect.left - 32 && x <= rect.right + 32;
      return nearVerticalEdge || nearHorizontalEdge;
    }
    // Native dragover can pause while the pointer stays still. Keep scrolling
    // the list and recompute its landing slot as new cards come into view.
    if (grid.parentElement) {
      var pane = grid.parentElement;
      pane.addEventListener('dragover', function (event) {
        if (!draggedId) return;
        var inGrid = grid.contains(event.target);
        if (!inGrid && !nearGridEdge(event)) return stopEdgeScroll();
        dragPoint = {clientX:event.clientX, clientY:event.clientY};
        if (!inGrid) { event.preventDefault(); previewGrid(dragPoint, false); }
        if (edgeTimer === null) edgeTimer = global.setInterval(scrollAtEdge, 24);
      }, true);
      pane.addEventListener('dragleave', function (event) {
        if (!pane.contains(event.relatedTarget)) stopEdgeScroll();
      });
      pane.addEventListener('drop', function (event) {
        if (draggedId && !grid.contains(event.target) && nearGridEdge(event)) {
          event.preventDefault(); previewGrid(event, false); commitDrag();
        }
      });
    }
    try {
      var savedOrder = JSON.parse(global.localStorage && global.localStorage.getItem(orderStorageKey) || 'null');
      if (savedOrder && savedOrder.version === 1 && Array.isArray(savedOrder.ids)) {
        manualOrder = savedOrder.manual === true;
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
    $('device-sort').value = manualOrder ? 'manual' : 'status';
    function orderId(device) {
      return device.serial ? 'board:' + device.serial : device.by_path ? 'port:' + device.by_path
        : device.connection_id ? 'connection:' + device.connection_id : 'key:' + device.key;
    }
    function saveOrder() {
      try {
        if (global.localStorage) global.localStorage.setItem(orderStorageKey,
          JSON.stringify({ version: 1, ids: deviceOrder, manual: manualOrder }));
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
      devices.forEach(function (device) {
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
      // Start a manual move from the order on screen, including filtered-out
      // rows. Keep saved slots for attachments absent from this poll.
      var visible = new Set(visibleOrder), present = new Set(directoryOrder), cursor = 0;
      var reordered = directoryOrder.map(function (item) { return visible.has(item) ? next[cursor++] : item; });
      cursor = 0;
      deviceOrder = deviceOrder.map(function (item) { return present.has(item) ? reordered[cursor++] : item; });
      manualOrder = true;
      $('device-sort').value = 'manual';
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
      stopEdgeScroll(); grid.classList.remove('reordering');
      draggedId = ''; dragPreview = null;
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      if (renderAfterDrag || id) {
        renderAfterDrag = false; renderDevices(lastDevices); focusOrder(id, 'drag');
      }
    }
    function previewDrag(card, event, animate) {
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
      var after = horizontal && typeof event.clientX === 'number' && event.clientY >= rect.top && event.clientY <= rect.bottom
        ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
      if (after) target++;
      next.splice(target, 0, draggedId);
      if (JSON.stringify(next) === JSON.stringify(dragPreview || visibleOrder)) return;
      var before = {};
      if (animate !== false) visibleOrder.forEach(function (id) { before[id] = orderElements[id].card.getBoundingClientRect(); });
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      var following = next[next.indexOf(draggedId) + 1];
      $('grid').insertBefore(moving, following ? orderElements[following].card : null);
      dragPreview = next;
      if (animate === false || global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
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
      stopEdgeScroll(); grid.classList.remove('reordering');
      draggedId = ''; dragPreview = null; renderAfterDrag = false;
      dragAnimations.forEach(function (animation) { animation.cancel(); }); dragAnimations = [];
      applyVisibleOrder(next, moving, 'drag');
    }
    function previewGrid(event, animate) {
      var closest = null, distance = Infinity;
      visibleOrder.forEach(function (id) {
        if (id === draggedId) return;
        var card = orderElements[id].card, r = card.getBoundingClientRect();
        var dx = Math.max(r.left - event.clientX, 0, event.clientX - r.right);
        var dy = Math.max(r.top - event.clientY, 0, event.clientY - r.bottom);
        if (dx * dx + dy * dy < distance) { closest = card; distance = dx * dx + dy * dy; }
      });
      if (closest) previewDrag(closest, event, animate);
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
      var query = $('device-search').value.trim().toLowerCase();
      var filter = $('device-filter').value || 'all';
      var position = new Map(deviceOrder.map(function (id, index) { return [id, index]; }));
      function priority(d) { return d.state === 'pending' ? 0 : d.state === 'online' ? 1 : 2; }
      list = list.slice().sort(function (a, b) {
        return (manualOrder ? 0 : priority(a) - priority(b)) || position.get(orderId(a)) - position.get(orderId(b));
      });
      directoryOrder = list.map(orderId);
      list = list.filter(function (d) {
        return (filter === 'all' || d.state === filter || (filter === 'active' && priority(d) < 2)) &&
          [d.attachment_label, d.label, d.key, d.serial, d.hostname, d.by_path, d.last_path, d.dev, d.last_dev].join(' ').toLowerCase().includes(query);
      });
      var focusBefore = null;
      Object.keys(orderElements).some(function (id) {
        return Object.keys(orderElements[id]).some(function (control) {
          if (orderElements[id][control] !== document.activeElement) return false;
          focusBefore = { id: id, control: control }; return true;
        });
      });
      visibleOrder = list.map(orderId); orderElements = {};
      var g = $('grid');
      if (!list.length) {
        g.innerHTML = query || filter !== 'all'
          ? '<p class="empty">没有符合当前搜索和筛选条件的设备。</p>'
          : '<p class="empty">等待连接开发板串口。</p>';
        return;
      }
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
        var activity = document.createElement('div'); activity.className = 'device-activity';
        activity.appendChild(last); card.appendChild(activity);

        var stat = document.createElement('div'); stat.className = 'stat';
        var s1 = document.createElement('span'); s1.textContent = fmt(d.bytes) + ' 记录';
        var s2 = document.createElement('span'); s2.textContent = '末次 ' + ago(d.last_rx || d.last_seen);
        var s3 = document.createElement('span'); s3.className = 'attachment'; s3.textContent = d.by_path || d.last_path || '';
        stat.appendChild(s1); stat.appendChild(s2); stat.appendChild(s3);
        activity.appendChild(stat);
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
          var addresses = document.createElement('span'); addresses.className = 'device-addresses';
          addresses.textContent = inf.ips.map(function (x) { return x.split(' ')[1].split('/')[0]; }).join(' ');
          meta.appendChild(addresses);
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
          draggedId = id; dragPreview = null; grid.classList.toggle('reordering', true);
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

    function showSerialWarnings(rows) {
      var panel = $('serial-warning');
      if (!panel) return;
      var matched = rows.filter(function (row) { return row.brltty_rule; });
      function ports(items) { return items.map(function (row) { return row.chip + ' · USB ' + row.port; }).join(' / '); }
      panel.hidden = !rows.length;
      if (panel.hidden) panel.open = false;
      $('serial-warning-ports').textContent = ports(rows);
      $('serial-warning-brltty').hidden = !matched.length;
      $('serial-brltty-ports').textContent = ports(matched);
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
          showSerialWarnings(d.usb_serial_warnings || []);
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
          showSerialWarnings([]);
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
    $('device-sort').addEventListener('change', function () {
      manualOrder = $('device-sort').value === 'manual';
      saveOrder(); renderDevices(lastDevices);
    });
    $('device-filter').addEventListener('change', function () { renderDevices(lastDevices); });
    function jump(direction, position) {
      if (draggedId || !visibleOrder.length) return false;
      var current = visibleOrder.findIndex(function (id) { return orderElements[id].card.dataset.key === selKey; });
      var count = visibleOrder.length;
      var index = position != null ? position : current < 0 ? (direction > 0 ? 0 : count - 1) : (current + direction + count) % count;
      if (index >= count || index === current) return false;
      var card = orderElements[visibleOrder[index]].card;
      options.open(card.dataset.key);
      if (card.scrollIntoView) card.scrollIntoView({block:'nearest', inline:'nearest'});
      return true;
    }
    var controller = {
      jump: jump, poll: pollDevices, markSelected: markSelected, adopt: adoptKey,
      selectionChanged: function (key) { selKey = key; requestedDevice = ''; },
      rememberInfo: rememberInfo,
      advanceOwnership: function () { ownershipRevision++; },
      find: function (key) { return lastDevices.find(function (board) { return board.key === key; }); }
    };
    Object.defineProperty(controller, 'items', { get: function () { return lastDevices; } });
    Object.defineProperty(controller, 'selected', { get: function () { return selKey; } });
    return controller;
  }
  function bindShortcuts($, jump) {
    var storageKey = 'decklab.device-shortcuts.v1';
    var defaults = {prev:'Alt+Shift+ArrowLeft', next:'Alt+Shift+ArrowRight', number:'Alt+Shift'};
    var dialog = $('device-shortcuts'), status = $('shortcuts-status'), recording = '';
    var bindings = Object.assign({}, defaults);
    var modifiers = ['Ctrl', 'Alt', 'Shift', 'Meta'];
    var keyPattern = /^(?:[A-Z0-9]|Arrow(?:Left|Right|Up|Down)|PageUp|PageDown|Home|End|F(?:[1-9]|1[0-2]))$/;
    function matchesNumber(combo, number) {
      return number && /^[1-9]$/.test(combo.slice(-1)) && combo.slice(0, -1) === number + '+';
    }
    function load() {
      var value;
      try { value = JSON.parse(global.localStorage.getItem(storageKey)); } catch (_) {}
      bindings = Object.assign({}, defaults);
      if (!value || typeof value !== 'object') return;
      if (!['prev','next'].every(function (name) {
        if (value[name] === '') return true;
        if (typeof value[name] !== 'string') return false;
        var parts = value[name].split('+'), key = parts.pop();
        return parts.length && parts.join('+') === modifiers.filter(function (m) { return parts.includes(m); }).join('+')
          && keyPattern.test(key);
      }) || !['', 'Alt', 'Alt+Shift', 'Ctrl+Alt', 'Ctrl+Shift'].includes(value.number)
        || value.prev && value.prev === value.next || matchesNumber(value.prev,value.number) || matchesNumber(value.next,value.number)) return;
      bindings = {prev:value.prev, next:value.next, number:value.number};
    }
    function render() {
      ['prev','next'].forEach(function (name) {
        var button = $('shortcut-' + name);
        button.textContent = recording === name ? '请按组合键' : (bindings[name] || '未设置').replace(/\+/g, ' + ')
          .replace('ArrowLeft','←').replace('ArrowRight','→').replace('ArrowUp','↑').replace('ArrowDown','↓');
        button.setAttribute('aria-pressed', String(recording === name));
      });
      $('shortcut-number').value = bindings.number;
    }
    function save(next) {
      if ((next.prev && next.prev === next.next) || matchesNumber(next.prev,next.number) || matchesNumber(next.next,next.number)) {
        status.textContent = '组合键已用于其他切换操作'; render(); return;
      }
      try { global.localStorage.setItem(storageKey, JSON.stringify(next)); }
      catch (_) { status.textContent = '浏览器未能保存快捷键'; render(); return; }
      bindings = next; recording = ''; status.textContent = '快捷键已保存'; render();
    }
    load();
    $('b-shortcuts').addEventListener('click', function () {
      load(); recording = ''; status.textContent = ''; render(); dialog.showModal();
    });
    $('shortcuts-close').addEventListener('click', function () { dialog.close(); });
    ['prev','next'].forEach(function (name) {
      var button = $('shortcut-' + name);
      button.addEventListener('click', function () { recording = name; status.textContent = ''; render(); });
      button.addEventListener('blur', function () { if (recording === name) { recording = ''; render(); } });
    });
    $('shortcut-number').addEventListener('change', function () { save(Object.assign({}, bindings, {number:this.value})); });
    $('shortcuts-reset').addEventListener('click', function () { save(Object.assign({}, defaults)); });
    if (global.addEventListener) global.addEventListener('storage', function (event) {
      if (event.key === storageKey || event.key === null) { load(); recording = ''; render(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.isComposing || event.keyCode === 229 || event.getModifierState && event.getModifierState('AltGraph')) return;
      var pressed = [event.ctrlKey, event.altKey, event.shiftKey, event.metaKey];
      var mods = modifiers.filter(function (_, index) { return pressed[index]; }).join('+');
      var physical = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(event.code || '');
      var key = physical ? physical[1] || physical[2] : event.key || '';
      var combo = mods + '+' + key;
      function consume() { event.preventDefault(); event.stopImmediatePropagation(); }
      if (dialog.open && recording) {
        if (event.key === 'Tab') return;
        consume();
        if (event.repeat || ['Control','Alt','Shift','Meta'].includes(key)) return;
        if (key === 'Escape') { recording = ''; status.textContent = ''; render(); return; }
        if (key === 'Delete' && !mods) combo = '';
        else if (!mods || mods === 'Shift' || !keyPattern.test(key) || ['Ctrl+F','Meta+F','Alt+ArrowUp','Alt+ArrowDown'].includes(combo)) {
          status.textContent = '请换一个含 Ctrl、Alt 或 Meta 的组合键'; return;
        }
        var next = Object.assign({}, bindings); next[recording] = combo; save(next);
        return;
      }
      var direction = combo === bindings.next ? 1 : combo === bindings.prev ? -1 : 0;
      var position = matchesNumber(combo, bindings.number) ? Number(key) - 1 : null;
      if (!direction && position === null) return;
      var target = event.target;
      if (document.querySelector('dialog[open], :popover-open') || target && target.closest &&
          !target.closest('#d-term') && (target.isContentEditable || target.closest('input, textarea, select, #desktop-panel'))) return;
      consume();
      if (!event.repeat) jump(direction, position);
    }, true);
  }
  global.SerialdDevices = { create: create, bindShortcuts: bindShortcuts };
})(window);
