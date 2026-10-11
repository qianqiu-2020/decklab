// Durable task history and operation watchers survive terminal/device switches.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, fetch = options.fetch;
    var selection = '', offset = 0, sequence = 0, waiting = {};
    var names = { identify: '识别设备', 'ssh-enable': '启用 SSH', 'ssh-connect': '建立 SSH', 'file-copy': '板间复制' };
    var states = { queued: '等待执行', running: '执行中', completed: '已完成', failed: '失败',
                   cancelled: '已取消', interrupted: '已中断' };
    function scope(ctx) { return ctx ? ctx.serial ? 'serial:' + ctx.serial
      : 'connection:' + (ctx.connectionId || ctx.key) : ''; }
    function sameSelection(selected) { return !!selected && scope(options.getView()) === selected; }
    function active(task) { return task.state === 'queued' || task.state === 'running'; }
    function request(url, payload) {
      return fetch(url, payload ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) } : { cache: 'no-store' }).then(function (response) {
        return response.json().then(function (data) {
          if (data.error || data.ok === false || response.ok === false) {
            var error = new Error(data.error || '任务请求失败'); error.status = response.status; throw error;
          }
          return data;
        });
      });
    }
    function finish(entry, task) {
      if (active(task)) return false;
      delete waiting[task.id];
      if (task.state === 'completed') entry.resolve(task.result || { ok: true });
      else entry.reject(new Error(task.error || states[task.state] || '任务失败'));
      return true;
    }
    function monitor(entry) {
      request('/api/task?id=' + encodeURIComponent(entry.id) + '&key=' + encodeURIComponent(entry.key)
        + (entry.serial ? '&serial=' + encodeURIComponent(entry.serial) : '')).then(function (data) {
        if (!data.task || data.task.id !== entry.id) throw new Error('任务响应与请求不匹配');
        if (!finish(entry, data.task)) entry.timer = setTimeout(function () { monitor(entry); }, 1000);
      }).catch(function (error) {
        if ([401, 403, 404].indexOf(error.status) !== -1) {
          delete waiting[entry.id]; entry.reject(error); return;
        }
        // A network outage does not imply the collector stopped the operation.
        entry.timer = setTimeout(function () { monitor(entry); }, 2000);
      });
    }
    function run(kind, ctx) {
      return request('/api/task-start', { key: ctx.key, serial: ctx.serial,
        connection_id: ctx.connectionId, kind: kind, actor: 'web' }).then(function (data) {
        if (!data.task || !data.task.id) throw new Error('服务未返回任务编号');
        refresh();
        return new Promise(function (resolve, reject) {
          var entry = { id: data.task.id, key: ctx.key, serial: ctx.serial, resolve: resolve, reject: reject };
          waiting[entry.id] = entry;
          if (!finish(entry, data.task)) monitor(entry);
        });
      });
    }
    function render(tasks, ctx, selected, more) {
      var box = $('d-tasks'); box.textContent = '';
      tasks.forEach(function (task) {
        var row = document.createElement('div'); row.className = 'activity-row';
        var main = document.createElement('div'); main.className = 'activity-main';
        var title = document.createElement('strong');
        title.textContent = (names[task.kind] || task.kind) + ' · ' + (states[task.state] || task.state);
        var detail = document.createElement('div'); detail.className = 'log-note';
        detail.textContent = task.actor + ' · ' + options.age(task.created)
          + (task.cancel_requested ? ' · 已请求取消' : '');
        main.appendChild(title); main.appendChild(detail);
        if (active(task)) {
          var progress = document.createElement('progress'); progress.className = 'task-progress';
          progress.max = Math.max(1, task.progress.total || 1); progress.value = task.progress.done || 0;
          progress.setAttribute('aria-label', '任务进度'); main.appendChild(progress);
        }
        var output = document.createElement('code');
        output.textContent = task.error || task.result && (task.result.sid ? '会话 ' + task.result.sid
          : task.result.serial ? 'SN ' + task.result.serial
          : task.result.path ? task.result.path + ' · ' + task.result.size + ' B' : '') || task.progress.phase || '';
        main.appendChild(output); row.appendChild(main);
        if (active(task)) {
          var cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消';
          cancel.addEventListener('click', function () {
            if (!sameSelection(selected)) return;
            cancel.disabled = true;
            request('/api/task-cancel', { id: task.id, key: ctx.key, actor: 'web' })
              .then(function () { if (sameSelection(selected)) refresh(); })
              .catch(function (error) {
                if (sameSelection(selected)) { $('task-status').textContent = error.message; cancel.disabled = false; }
              });
          });
          row.appendChild(cancel);
        } else if (task.state === 'completed' && task.result && task.result.sid) {
          var open = document.createElement('button'); open.type = 'button'; open.textContent = '打开会话';
          open.addEventListener('click', function () { if (sameSelection(selected)) options.openSession(task); });
          row.appendChild(open);
        }
        box.appendChild(row);
      });
      if (!tasks.length) box.textContent = '尚无后台任务；任务结果在刷新或重新打开页面后仍可查看。';
      $('task-prev').disabled = offset === 0;
      $('task-next').disabled = !more;
      $('task-page').textContent = '第 ' + (offset / 10 + 1) + ' 页';
      $('task-status').textContent = '任务保存在采集器中；刷新页面不会取消。';
    }
    function refresh() {
      var ctx = options.getView(), selected = scope(ctx);
      if (!ctx) return;
      if (selection !== selected) {
        selection = selected; offset = 0; $('d-tasks').textContent = '';
        $('task-status').textContent = '正在读取任务…';
        $('task-prev').disabled = true; $('task-next').disabled = true; $('task-page').textContent = '';
      }
      var seq = ++sequence;
      request('/api/tasks?key=' + encodeURIComponent(ctx.key) + '&limit=11&offset=' + offset
        + (ctx.serial ? '&target_serial=' + encodeURIComponent(ctx.serial)
          : ctx.connectionId ? '&connection_id=' + encodeURIComponent(ctx.connectionId) : '')).then(function (data) {
        if (seq !== sequence || !sameSelection(selected)) return;
        var tasks = data.tasks || [];
        render(tasks.slice(0, 10), ctx, selected, tasks.length > 10);
      }).catch(function (error) {
        if (seq === sequence && sameSelection(selected)) $('task-status').textContent = '读取任务失败：' + error.message;
      });
    }
    $('task-prev').addEventListener('click', function () { offset = Math.max(0, offset - 10); refresh(); });
    $('task-next').addEventListener('click', function () { offset += 10; refresh(); });
    $('task-refresh').addEventListener('click', refresh);
    setInterval(function () { if (options.visible()) refresh(); }, 5000);
    return { run: run, refresh: refresh };
  }
  global.SerialdTasks = { create: create };
})(window);
