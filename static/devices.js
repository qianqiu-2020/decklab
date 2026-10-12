// Device directory, filtering, polling and identity transitions.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fetch = options.fetch, fmt = options.formatBytes, ago = options.age;
    var selKey = null, lastDevices = [], lastInfoIdentity = '';
    var ownershipRevision = 0, pollSeq = 0, requestedDevice = '';
    try {
      var sharedMatch = /^#device=(.*)$/.exec(global.location && global.location.hash || '');
      if (sharedMatch) requestedDevice = decodeURIComponent(sharedMatch[1]);
    } catch (_) { /* A malformed bookmark must not break device discovery. */ }
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
      list = list.slice().sort(function (a, b) {
        var rank = function (device) {
          return device.alive ? (device.state === 'pending' ? 1 : 0) : 2;
        };
        return rank(a) - rank(b);
      });
      var g = $('grid');
      if (!list.length) { g.innerHTML = '<p class="empty">没有匹配的设备。新串口连接后将显示在待识别列表。</p>'; return; }
      g.textContent = '';
      list.forEach(function (d) {
        var card = document.createElement('div');
        card.className = 'dev' + (!d.alive ? ' offline' : '') + (d.key === selKey ? ' sel' : '');
        card.dataset.key = d.key;          // 供 markSelected() 就地改高亮用
        card.title = (d.by_path || d.last_path || '')
          + (d.alive ? '\n采集检查：' + ago(d.last_health_check) : '');
        card.tabIndex = 0;
        card.addEventListener('click', function () { options.open(d.key); });
        card.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); options.open(d.key); }
        });

        var r1 = document.createElement('div'); r1.className = 'r1';
        var nm = document.createElement('span'); nm.className = 'nm';
        if (d.state === 'pending') {
          nm.className += ' pending-name';
          var pendingLabel = document.createElement('span'); pendingLabel.textContent = '待识别 · ';
          var portLabel = document.createElement('span'); portLabel.dataset.i18nIgnore = '';
          portLabel.textContent = d.hostname || (d.dev || '').split('/').pop();
          nm.appendChild(pendingLabel); nm.appendChild(portLabel);
        } else nm.textContent = d.label;
        var st = document.createElement('span');
        st.className = 'pill ' + (d.state === 'pending' ? 'pending' : (d.alive ? 'live' : 'dead'));
        st.textContent = d.state === 'pending' ? 'USB 临时身份' : (d.alive ? '在线' : '历史设备');
        r1.appendChild(nm); r1.appendChild(st);
        if (d.hostname && d.hostname !== d.label) {
          var sn = document.createElement('span'); sn.className = 'pill sniff';
          sn.textContent = d.hostname; r1.appendChild(sn);
        }
        card.appendChild(r1);

        var meta = document.createElement('div'); meta.className = 'meta';
        var a = document.createElement('span'); a.textContent = d.dev || ('上次连接 ' + (d.last_dev || '—'));
        var b = document.createElement('span'); b.textContent = d.serial ? 'SN ' + d.serial : (d.by_path || '尚未绑定开发板序列号');
        meta.appendChild(a); meta.appendChild(b);
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
