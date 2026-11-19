// Board archive selection and boot search. Terminal tabs remain owned by app.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fmt = options.formatBytes;
    var fetch = options.fetch, isCurrent = options.isCurrent;
    var bootSearch = { seq: 0, key: '', serial: '', query: '', offset: 0 };
    var archiveState = { key: '', serial: '', connection: '' };
    function connectionQuery(ctx) {
      return !ctx.serial && ctx.archiveConnection ? '&archive_connection=' + encodeURIComponent(ctx.archiveConnection) : '';
    }
    function archiveName(ctx, name) {
      return !ctx.serial && ctx.archiveConnection ? ctx.archiveConnection + ':' + name : name;
    }
    function clearBootSearch() {
      bootSearch.seq++;
      bootSearch.query = '';
      $('boot-search-results').hidden = true;
      $('boot-search-matches').textContent = '';
    }

    function searchBootLogs(offset) {
      var ctx = options.getView(), query = $('terminal-search').value.trim();
      if (!isCurrent(ctx)) return;
      if (!query) { clearBootSearch(); return; }
      var seq = ++bootSearch.seq;
      bootSearch.key = ctx.key; bootSearch.serial = ctx.serial || '';
      bootSearch.query = query; bootSearch.offset = offset || 0;
      options.openLogs();
      $('boot-search-results').hidden = false;
      $('boot-search-summary').textContent = '正在搜索当前设备的全部启动日志…';
      $('boot-search-matches').textContent = '';
      $('boot-search-prev').disabled = $('boot-search-next').disabled = true;
      fetch('/api/boot-search?key=' + encodeURIComponent(ctx.key)
        + '&serial=' + encodeURIComponent(ctx.serial || '') + '&q=' + encodeURIComponent(query)
        + '&offset=' + bootSearch.offset + '&limit=20' + connectionQuery(ctx), { cache: 'no-store' })
        .then(function (response) { return response.json(); }).then(function (data) {
          var view = options.getView();
          if (seq !== bootSearch.seq || !view || view.key !== ctx.key
              || (view.serial || '') !== bootSearch.serial) return;
          if (data.error) throw new Error(data.error);
          var matches = data.results || [];
          $('boot-search-summary').textContent = matches.length
            ? '全部启动日志 · 结果 ' + (bootSearch.offset + 1) + '–' + (bootSearch.offset + matches.length)
              + (data.more ? ' · 还有更多' : '') : '全部启动日志 · 无匹配';
          $('boot-search-prev').disabled = bootSearch.offset === 0;
          $('boot-search-next').disabled = !data.more;
          matches.forEach(function (match) {
            var button = document.createElement('button'); button.type = 'button';
            button.className = 'boot-search-match';
            var title = document.createElement('strong');
            title.textContent = '启动 #' + match.number + ' · ' + match.started + ' · 第 ' + match.line + ' 行';
            var excerpt = document.createElement('span'); excerpt.textContent = match.excerpt;
            button.appendChild(title); button.appendChild(excerpt);
            button.addEventListener('click', function () {
              var view = options.getView();
              if (seq !== bootSearch.seq || !view || view.key !== ctx.key || (view.serial || '') !== bootSearch.serial) return;
              openBootLog(view, match);
              view = options.getView(); // Opening the archive replaces the terminal context.
              $('terminal-search-scope').value = 'terminal';
              $('terminal-search').placeholder = '搜索终端 · Ctrl+F';
              $('terminal-search').value = query;
              view.archiveSearchQuery = query;
              if (view.initialRendered) options.searchTerminal(1);
            });
            $('boot-search-matches').appendChild(button);
          });
        }).catch(function (error) {
          var view = options.getView();
          if (seq === bootSearch.seq && view && view.key === ctx.key && (view.serial || '') === bootSearch.serial)
            $('boot-search-summary').textContent = '搜索失败：' + error.message;
        });
    }

    function archiveDate(file) {
      if (file.date) return file.date;
      var match = /(\d{4})(\d\d)(\d\d)\.log$/.exec(file.name);
      return match ? match[1] + '-' + match[2] + '-' + match[3] : '日期未知';
    }

    function bootStartDate(boot) {
      return (boot.started || '').slice(0, 10) || (boot.dates || [])[0] || '';
    }

    function openBootLog(ctx, boot) {
      if (!isCurrent(ctx)) return;
      var url = '/api/boot-log?key=' + encodeURIComponent(ctx.key)
        + '&serial=' + encodeURIComponent(ctx.serial || '') + '&id=' + encodeURIComponent(boot.id) + connectionQuery(ctx);
      options.openArchive(ctx, archiveName(ctx, boot.id), '启动 #' + boot.number + ' · ' + (boot.started || ''), url);
    }

    function dailyFileRow(ctx, file, singleFile) {
      var row = document.createElement('div'); row.className = 'log-row';
      var url = '/api/log?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '')
        + '&file=' + encodeURIComponent(file.name) + connectionQuery(ctx);
      var open = document.createElement('button'); open.type = 'button';
      open.textContent = singleFile ? '查看全天日志' : file.name;
      if (!singleFile) open.dataset.i18nIgnore = '';
      open.title = file.name;
      open.dataset.entry = 'file:' + file.name;
      if (ctx.sid === 'log:' + archiveName(ctx, file.name)) open.setAttribute('aria-current', 'true');
      var connection = ctx.archiveConnection;
      open.addEventListener('click', function () {
        if (isCurrent(ctx) && archiveState.connection === connection)
          options.openArchive(ctx, archiveName(ctx, file.name), '串口日志 · ' + archiveDate(file), url + '&full=1');
      });
      var size = document.createElement('span'); size.className = 'log-size'; size.textContent = fmt(file.size);
      var download = document.createElement('a'); download.className = 'log-download';
      download.dataset.entry = 'download:' + file.name;
      download.href = url + '&download=1'; download.download = file.name; download.textContent = '下载';
      row.appendChild(open); row.appendChild(size); row.appendChild(download);
      return row;
    }

    function appendDayEntries(day, ctx, date, entries) {
      entries.files.forEach(function (file) {
        day.appendChild(dailyFileRow(ctx, file, entries.files.length === 1));
      });
      var connection = ctx.archiveConnection;
      entries.boots.forEach(function (boot) {
        var button = document.createElement('button'); button.type = 'button'; button.className = 'boot-row';
        button.dataset.entry = 'boot:' + boot.id;
        if (ctx.sid === 'log:' + archiveName(ctx, boot.id)) button.setAttribute('aria-current', 'true');
        var label = document.createElement('strong'); label.textContent = '启动 #' + boot.number;
        var time = document.createElement('span');
        time.textContent = bootStartDate(boot) === date ? (boot.started || '').slice(11)
          : '跨日延续 · 起于 ' + (boot.started || '时间未知');
        var marker = document.createElement('small'); marker.textContent = boot.marker || '';
        marker.dataset.i18nIgnore = '';
        button.appendChild(label); button.appendChild(time); button.appendChild(marker);
        button.addEventListener('click', function () {
          if (archiveState.connection === connection) openBootLog(ctx, boot);
        });
        day.appendChild(button);
      });
    }

    function renderArchive(ctx) {
      if (!isCurrent(ctx)) return;
      var box = $('d-logs');
      var focused = document.activeElement;
      var focusedDay = focused && focused.closest && focused.closest('#d-logs .archive-day');
      var previous = new Map(Array.from(box.children).map(function (day) { return [day.dataset.date, day.open]; }));
      box.textContent = '';
      var connections = $('archive-connections'); connections.textContent = '';
      connections.hidden = !ctx.archiveConnections || !ctx.archiveConnections.length;
      if (!connections.hidden) {
        var captures = document.createElement('select'); captures.setAttribute('aria-label', '选择串口连接');
        ctx.archiveConnections.forEach(function (capture) {
          var option = document.createElement('option'); option.value = capture.id;
          option.textContent = (capture.current ? '当前连接' : '历史连接') + ' · '
            + (capture.started || new Date(capture.last_seen * 1000).toLocaleString()) + ' · ' + fmt(capture.size);
          captures.appendChild(option);
        });
        captures.value = ctx.archiveConnection;
        captures.addEventListener('change', function () {
          if (!isCurrent(ctx)) return;
          archiveState.connection = captures.value;
          clearBootSearch(); $('d-logs').textContent = '正在读取日志…';
          loadLogs();
        });
        connections.appendChild(captures);
      }
      var days = new Map();
      function entries(date) {
        if (!days.has(date)) days.set(date, {files: [], boots: []});
        return days.get(date);
      }
      ctx.archiveFiles.forEach(function (file) { entries(archiveDate(file)).files.push(file); });
      ctx.archiveBoots.forEach(function (boot) {
        var dates = boot.dates && boot.dates.length ? boot.dates : [bootStartDate(boot) || '日期未知'];
        new Set(dates).forEach(function (date) { entries(date).boots.push(boot); });
      });
      var dates = Array.from(days.keys()).sort().reverse();
      dates.forEach(function (date, index) {
        var day = document.createElement('details'); day.className = 'archive-day'; day.dataset.date = date;
        day.open = previous.has(date) ? previous.get(date) : index === 0;
        var summary = document.createElement('summary'); summary.textContent = date;
        day.appendChild(summary);
        function populate() {
          if (day.open && day.children.length === 1) appendDayEntries(day, ctx, date, days.get(date));
        }
        populate();
        day.addEventListener('toggle', populate);
        box.appendChild(day);
        if (focusedDay && focusedDay.dataset.date === date) {
          var entry = Array.from(day.querySelectorAll('[data-entry]')).find(function (node) {
            return node.dataset.entry === focused.dataset.entry;
          });
          (entry || summary).focus({preventScroll:true});
        }
      });
      if (!dates.length) box.textContent = '还没有串口日志文件';
    }

    function loadLogs() {
      var ctx = options.getView();
      if (!isCurrent(ctx)) return;
      if (archiveState.key !== ctx.key || archiveState.serial !== (ctx.serial || '')
          || archiveState.liveConnection !== ctx.connectionId) {
        archiveState = { key: ctx.key, serial: ctx.serial || '', connection: '',
          liveConnection: ctx.connectionId };
        $('archive-connections').hidden = true; $('archive-connections').textContent = '';
        $('d-logs').textContent = '';
        $('log-location').textContent = '';
      }
      var seq = ++ctx.logsSeq;
      var requestedConnection = archiveState.connection;
      fetch('/api/logs?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '')
        + (requestedConnection ? '&archive_connection=' + encodeURIComponent(requestedConnection) : ''), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!isCurrent(ctx) || seq !== ctx.logsSeq || (d.key && d.key !== ctx.key)
              || archiveState.connection !== requestedConnection) return;
          if (d.error) throw new Error(d.error);
          ctx.archiveConnection = d.archive_connection || '';
          ctx.archiveConnections = d.connections || [];
          archiveState.connection = ctx.archiveConnection;
          ctx.archiveFiles = (d.files || []).filter(function (file) {
            return (file.kind || (file.name.indexOf('ssh-') === 0 ? 'ssh' : 'serial')) === 'serial';
          });
          ctx.archiveBoots = d.boots || [];
          $('log-location').textContent = d.archive
            ? 'logs/' + d.archive + '/' : '待识别连接的临时日志';
          renderArchive(ctx);
        }).catch(function (e) {
          if (isCurrent(ctx) && seq === ctx.logsSeq) $('d-logs').textContent = '读取日志列表失败：' + e.message;
        });
    }

    $('boot-search-prev').addEventListener('click', function () {
      $('terminal-search').value = bootSearch.query;
      searchBootLogs(Math.max(0, bootSearch.offset - 20));
    });
    $('boot-search-next').addEventListener('click', function () {
      $('terminal-search').value = bootSearch.query;
      searchBootLogs(bootSearch.offset + 20);
    });
    $('boot-search-close').addEventListener('click', clearBootSearch);
    return {
      load: loadLogs, search: searchBootLogs, clearSearch: clearBootSearch,
      startRefresh: function () {
        return setInterval(function () {
          if (options.getView() && options.logsVisible()
              && document.visibilityState !== 'hidden') loadLogs();
        }, 15000);
      }
    };
  }
  global.SerialdArchives = { create: create };
})(window);
