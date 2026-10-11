// Board archive selection and boot search. Terminal tabs remain owned by app.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fmt = options.formatBytes;
    var fetch = options.fetch, isCurrent = options.isCurrent;
    var bootSearch = { seq: 0, key: '', serial: '', query: '', offset: 0 };
    var archiveState = { key: '', serial: '', mode: 'date', date: '', bootId: '' };
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
        + '&offset=' + bootSearch.offset + '&limit=20', { cache: 'no-store' })
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
        + '&serial=' + encodeURIComponent(ctx.serial || '') + '&id=' + encodeURIComponent(boot.id);
      options.openArchive(ctx, boot.id, '启动 #' + boot.number + ' · ' + (boot.started || ''), url);
    }

    function archiveHeading(box, title) {
      var heading = document.createElement('div');
      heading.className = 'log-subhead'; heading.textContent = title;
      box.appendChild(heading);
    }

    function renderArchive(ctx) {
      if (!isCurrent(ctx)) return;
      var box = $('d-logs'), picker = $('archive-picker');
      box.textContent = ''; picker.textContent = '';
      var files = ctx.archiveFiles || [], boots = ctx.archiveBoots || [];
      var dates = Array.from(new Set(files.map(archiveDate))).sort().reverse();
      var hasBoots = boots.length > 0;
      if (!hasBoots) archiveState.mode = 'date';
      ['date', 'boot'].forEach(function (mode) {
        var button = $('archive-mode-' + mode);
        button.classList.toggle('on', archiveState.mode === mode);
        button.setAttribute('aria-pressed', archiveState.mode === mode ? 'true' : 'false');
        button.disabled = mode === 'boot' && !hasBoots;
      });
      if (!files.length) { box.textContent = '还没有串口日志文件'; return; }
      var select = document.createElement('select');
      select.setAttribute('aria-label', archiveState.mode === 'date' ? '选择日志日期' : '选择启动次数');
      picker.appendChild(select);
      var summary = document.createElement('div');
      summary.className = 'archive-summary'; picker.appendChild(summary);
      if (archiveState.mode === 'date') {
        if (dates.indexOf(archiveState.date) < 0) archiveState.date = dates[0];
        dates.forEach(function (date) {
          var starts = boots.filter(function (boot) { return bootStartDate(boot) === date; }).length;
          var related = boots.filter(function (boot) { return (boot.dates || []).indexOf(date) >= 0; }).length;
          var option = document.createElement('option');
          option.value = date;
          option.textContent = date + ' · 启动 ' + starts + ' 次'
            + (related > starts ? ' · 延续 ' + (related - starts) + ' 次' : '');
          select.appendChild(option);
        });
        select.value = archiveState.date;
        select.addEventListener('change', function () {
          if (!isCurrent(ctx)) return;
          archiveState.date = select.value; renderArchive(ctx);
        });
        var linked = boots.filter(function (boot) {
          return (boot.dates || []).indexOf(archiveState.date) >= 0;
        });
        var startedHere = linked.filter(function (boot) {
          return bootStartDate(boot) === archiveState.date;
        }).length;
        summary.textContent = '当天启动 ' + startedHere + ' 次'
          + (linked.length > startedHere ? ' · 跨日延续 ' + (linked.length - startedHere) + ' 次' : '');
        archiveHeading(box, '当日日志');
        files.filter(function (file) { return archiveDate(file) === archiveState.date; }).forEach(function (file) {
          var row = document.createElement('div'); row.className = 'log-row';
          var date = document.createElement('span'); date.className = 'log-date';
          date.textContent = archiveDate(file);
          var open = document.createElement('button'); open.type = 'button';
          open.textContent = file.name;
          open.addEventListener('click', function () {
            if (isCurrent(ctx)) options.openArchive(ctx, file.name, '串口日志 · ' + archiveDate(file));
          });
          var size = document.createElement('span'); size.className = 'log-size';
          size.textContent = fmt(file.size);
          var download = document.createElement('a'); download.className = 'log-download';
          download.href = '/api/log?key=' + encodeURIComponent(ctx.key)
            + '&serial=' + encodeURIComponent(ctx.serial || '') + '&file=' + encodeURIComponent(file.name)
            + '&download=1';
          download.download = file.name; download.textContent = '下载';
          row.appendChild(date); row.appendChild(open); row.appendChild(size);
          row.appendChild(download); box.appendChild(row);
        });
        archiveHeading(box, '相关启动 · ' + linked.length);
        linked.forEach(function (boot) {
          var row = document.createElement('div'); row.className = 'log-row boot-row';
          var relation = document.createElement('span'); relation.className = 'log-date';
          relation.textContent = bootStartDate(boot) === archiveState.date
            ? '本日启动 · ' + (boot.started || '时间未知') : '跨日延续 · 起于 ' + (boot.started || '时间未知');
          var show = document.createElement('button'); show.type = 'button';
          show.textContent = '启动 #' + boot.number + ' · ' + (boot.marker || '启动标志');
          show.addEventListener('click', function () {
            if (!isCurrent(ctx)) return;
            archiveState.bootId = boot.id; archiveState.mode = 'boot'; renderArchive(ctx);
          });
          var open = document.createElement('button'); open.type = 'button';
          open.className = 'archive-open'; open.textContent = '终端打开';
          open.addEventListener('click', function () { openBootLog(ctx, boot); });
          row.appendChild(relation); row.appendChild(show); row.appendChild(open); box.appendChild(row);
        });
        if (!linked.length) {
          var empty = document.createElement('div'); empty.className = 'log-note';
          empty.textContent = '这一天没有检测到 Boot ROM / U-Boot 启动标志；完整日期日志仍可查看。';
          box.appendChild(empty);
        }
      } else {
        var boot = boots.filter(function (item) { return item.id === archiveState.bootId; })[0] || boots[0];
        archiveState.bootId = boot.id;
        boots.forEach(function (item) {
          var option = document.createElement('option');
          option.value = item.id;
          option.textContent = '启动 #' + item.number + ' · ' + (item.started || '时间未知');
          select.appendChild(option);
        });
        select.value = boot.id;
        select.addEventListener('change', function () {
          if (!isCurrent(ctx)) return;
          archiveState.bootId = select.value; renderArchive(ctx);
        });
        summary.textContent = (boot.marker || '启动标志') + ' · ' + fmt(boot.size)
          + (boot.cross_day ? ' · 跨日运行' : '');
        var card = document.createElement('div'); card.className = 'boot-detail';
        var title = document.createElement('strong');
        title.textContent = '启动 #' + boot.number + ' · ' + (boot.started || '时间未知');
        card.appendChild(title);
        var datesLabel = document.createElement('span'); datesLabel.className = 'log-note';
        datesLabel.textContent = '涉及日期 · 点击日期返回当日日志';
        card.appendChild(datesLabel);
        var links = document.createElement('div'); links.className = 'archive-date-links';
        (boot.dates || []).forEach(function (date) {
          var link = document.createElement('button'); link.type = 'button';
          link.textContent = date;
          link.addEventListener('click', function () {
            if (!isCurrent(ctx)) return;
            archiveState.date = date; archiveState.mode = 'date'; renderArchive(ctx);
          });
          links.appendChild(link);
        });
        card.appendChild(links);
        var openBoot = document.createElement('button'); openBoot.type = 'button';
        openBoot.className = 'pri'; openBoot.textContent = '在终端打开本次启动';
        openBoot.addEventListener('click', function () { openBootLog(ctx, boot); });
        card.appendChild(openBoot); box.appendChild(card);
      }
    }

    function loadLogs() {
      var ctx = options.getView();
      if (!isCurrent(ctx)) return;
      var seq = ++ctx.logsSeq;
      fetch('/api/logs?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || ''), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!isCurrent(ctx) || seq !== ctx.logsSeq || (d.key && d.key !== ctx.key)) return;
          if (archiveState.key !== ctx.key || archiveState.serial !== (ctx.serial || '')) {
            archiveState = { key: ctx.key, serial: ctx.serial || '', mode: 'date', date: '', bootId: '' };
          }
          var oldDates = (ctx.archiveFiles || []).map(archiveDate).sort().reverse();
          var followedDate = archiveState.date && archiveState.date === oldDates[0];
          var followedBoot = archiveState.bootId && (ctx.archiveBoots || []).length
            && archiveState.bootId === ctx.archiveBoots[0].id;
          ctx.archiveFiles = (d.files || []).filter(function (file) {
            return (file.kind || (file.name.indexOf('ssh-') === 0 ? 'ssh' : 'serial')) === 'serial';
          });
          ctx.archiveBoots = d.boots || [];
          if (followedDate && ctx.archiveFiles.length) archiveState.date = archiveDate(ctx.archiveFiles[0]);
          if (followedBoot && ctx.archiveBoots.length) archiveState.bootId = ctx.archiveBoots[0].id;
          $('log-location').textContent = d.archive
            ? 'logs/' + d.archive + '/' : '待识别连接的临时日志';
          renderArchive(ctx);
        }).catch(function (e) {
          if (isCurrent(ctx) && seq === ctx.logsSeq) $('d-logs').textContent = '读取日志列表失败：' + e.message;
        });
    }

    ['date', 'boot'].forEach(function (mode) {
      $('archive-mode-' + mode).addEventListener('click', function () {
        var ctx = options.getView();
        if (!isCurrent(ctx)) return;
        archiveState.mode = mode; renderArchive(ctx);
      });
    });
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
