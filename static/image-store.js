// Repository browsing and durable cache downloads. No board write is submitted.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var source = 'public', path = '', generation = 0, entries = [], choose = null;
    var active = Object.create(null), unknown = false, opening = false;
    var labels = {'package': '镜像包', 'disk-image': '磁盘镜像', 'partition-file': '分区文件'};
    function text(tag, value, parent) {
      var node = document.createElement(tag); node.textContent = value;
      if (parent) parent.appendChild(node); return node;
    }
    function button(value, parent, action) {
      var node = text('button', value, parent); node.type = 'button'; node.addEventListener('click', action); return node;
    }
    function status(value) { $('is-status').textContent = value; }
    function selected(item) {
      if (!choose) return;
      try { choose(item); $('image-store-dialog').close(); }
      catch (error) { status(error.message); }
    }
    function render(rows, local) {
      var box = $('is-list'), filter = $('is-search').value.toLowerCase(); box.textContent = '';
      rows.filter(function (item) { return item.name.toLowerCase().includes(filter); }).forEach(function (item) {
        var row = text('div', '', box); row.className = 'is-row';
        var main = text('div', '', row); main.className = 'is-main';
        if (item.kind === 'directory') {
          button(item.name + '/', main, function () { path = item.path; load(); });
        } else {
          text('strong', item.name, main);
          var facts = [local ? options.formatBytes(item.size) : labels[item.kind]];
          if (!local && (item.size_label || item.size)) facts.push(item.size ? options.formatBytes(item.size) : item.size_label);
          if (item.modified) facts.push(item.modified);
          text('small', facts.join(' · '), main);
          if (local) {
            var actions = text('div', '', row); actions.className = 'is-actions';
            if (choose) button('使用', actions, function () { selected(item); });
            var remove = button('删除', actions, function () {
              if (!global.confirm('从控制机删除“' + item.name + '”？')) return;
              // The existing deletion API refuses claimed/in-use artifacts.
              remove.disabled = true;
              client.request('/api/artifact-delete', {id:item.id,actor:'web'}).then(function () {
                status('镜像已从控制机缓存删除。'); loadLibrary();
              }).catch(function (error) { status(error.message); remove.disabled = false; });
            });
          } else {
            var download = button('下载到控制机', row, function () { startDownload(item, download); });
            download.disabled = unknown || !!active[item.url];
          }
        }
      });
      if (!box.children.length) text('p', '当前目录没有匹配的受支持镜像。', box).className = 'log-note';
    }
    function breadcrumbs() {
      var box = $('is-path'); box.textContent = '';
      button('K1 / K3', box, function () { path = ''; load(); });
      var prefix = '';
      path.split('/').filter(Boolean).forEach(function (part) {
        prefix += part + '/'; var current = prefix;
        text('span', '/', box); button(part, box, function () { path = current; load(); });
      });
    }
    async function load(force) {
      var seq = ++generation, current = source;
      $('is-refresh').disabled = true;
      entries = []; $('is-list').textContent = '';
      if (current === 'local') { await loadLibrary(seq); return; }
      breadcrumbs(); status('正在读取镜像目录…');
      try {
        var data = await client.request('/api/image-catalog?source=' + current + '&path=' + encodeURIComponent(path) + (force ? '&refresh=1' : ''));
        if (seq !== generation || current !== source) return;
        entries = data.entries; render(entries, false); status('');
      } catch (error) { if (seq === generation) { entries = []; $('is-list').textContent = ''; status(error.message); } }
      finally { if (seq === generation) $('is-refresh').disabled = false; }
    }
    async function loadLibrary(seq) {
      if (seq === undefined) seq = ++generation;
      $('is-path').textContent = ''; status('正在读取控制机缓存…');
      try {
        var data = await client.request('/api/image-library');
        if (seq !== generation || source !== 'local') return;
        entries = data.artifacts; render(entries, true); status(''); unknown = false;
        (data.tasks || []).forEach(function (task) { follow(task, 'task:' + task.id); });
      } catch (error) { if (seq === generation) status(error.message); }
      finally { if (seq === generation) $('is-refresh').disabled = false; }
    }
    function follow(task, key) {
      if (Object.values(active).includes(task.id)) return;
      active[key] = task.id;
      var box = text('div', '', $('is-downloads'));
      var progress = text('span', '正在下载…', box);
      var cancel = button('取消下载', box, function () {
        cancel.disabled = true;
        client.request('/api/task-cancel', {id:task.id,actor:'web'}).catch(function (error) {
          status(error.message); cancel.disabled = false;
        });
      });
      client.watch(task, {key:''}, function (current) {
        var p = current.progress || {};
        progress.textContent = '控制机下载：' + options.formatBytes(p.done || 0)
          + (p.total ? ' / ' + options.formatBytes(p.total) : '');
      }).then(function () {
        progress.textContent = '镜像已保存到控制机，可从“已下载”中选择。';
        if (source === 'local' && $('image-store-dialog').open) loadLibrary();
      }).catch(function (error) { progress.textContent = error.message; })
        .finally(function () { delete active[key]; cancel.remove(); if (source !== 'local') render(entries, false); });
    }
    async function startDownload(item, button) {
      if (unknown || active[item.url]) return;
      button.disabled = true; active[item.url] = 'submitting';
      var id = global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
      try {
        var result = await client.request('/api/image-download', {url:item.url,name:item.name,request_id:id,actor:'web'});
        delete active[item.url]; follow(result.task, item.url);
      } catch (error) {
        delete active[item.url];
        if (!error.result) { unknown = true; status('提交结果未知，请打开“已下载”检查原下载任务；不会自动重新提交。'); }
        else { status(error.message); button.disabled = false; }
      }
    }
    function open(onChoose) {
      choose = typeof onChoose === 'function' ? onChoose : null;
      if (!$('image-store-dialog').open) $('image-store-dialog').showModal();
      if (!opening) { opening = true; source = $('is-source').value || 'public'; }
      load();
    }
    $('is-close').addEventListener('click', function () { ++generation; $('image-store-dialog').close(); });
    $('is-source').addEventListener('change', function () { source = $('is-source').value; path = ''; load(); });
    $('is-refresh').addEventListener('click', function () { load(true); });
    $('is-search').addEventListener('input', function () { render(entries, source === 'local'); });
    return {open:open};
  }
  global.SerialdImageStore = {create:create};
})(window);
