(function (global) {
  'use strict';
  var fetch = global.SerialdUI.fetch;
  var byId = function (id) { return document.getElementById(id); };
  var dialog = byId('file-transfer-dialog');
  var board = null, devices = [], busy = false, cancelled = false;
  var meter = null, drag = null, activeTask = null, taskStarting = false;
  var taskClient = global.SerialdTasks.createClient(fetch);

  function status(text) { byId('ft-status').textContent = text; }
  function rateText(value) { return SerialdUI.formatBytes(Math.round(value)) + '/s'; }
  function basename(path) { return path.split('/').filter(Boolean).pop() || 'board-file'; }
  function bytes(raw) {
    var binary = '';
    for (var i = 0; i < raw.length; i++) binary += String.fromCharCode(raw[i]);
    return btoa(binary);
  }
  function duration(seconds) {
    if (!Number.isFinite(seconds)) return '计算中';
    seconds = Math.max(0, Math.ceil(seconds));
    if (seconds < 60) return seconds + ' 秒';
    var minutes = Math.floor(seconds / 60);
    return (minutes >= 60 ? Math.floor(minutes / 60) + ' 小时 ' : '')
      + (minutes % 60) + ' 分 ' + (seconds % 60) + ' 秒';
  }
  function beginProgress() {
    var now = performance.now();
    meter = { start: now, samples: [{ time: now, offset: 0 }], shown: 0 };
  }
  function averageRate(size) {
    return meter ? size / Math.max(0.001, (performance.now() - meter.start) / 1000) : 0;
  }
  function progress(offset, size, detail) {
    byId('ft-progress').value = size ? Math.round(offset / size * 100) : 100;
    if (!meter) beginProgress();
    var now = performance.now();
    meter.samples.push({ time: now, offset: offset });
    while (meter.samples.length > 2 && meter.samples[1].time < now - 5000) meter.samples.shift();
    if (now - meter.shown < 500 && offset < size) return;
    var first = meter.samples[0], elapsed = (now - first.time) / 1000;
    var rate = elapsed > 0.05 ? (offset - first.offset) / elapsed : 0;
    var remaining = rate > 0 ? (size - offset) / rate : Infinity;
    status(SerialdUI.formatBytes(offset) + ' / ' + SerialdUI.formatBytes(size)
      + ' · ' + (rate > 0 ? rateText(rate) : '速率计算中')
      + ' · 预计剩余 ' + duration(remaining) + (detail ? ' · ' + detail : ''));
    meter.shown = now;
  }
  function checkCancelled() { if (cancelled) throw new Error('已取消传输'); }
  function scope(selected) { return { key: selected.key, serial: selected.serial || '', actor: 'web' }; }

  async function upload(file, target, overwrite) {
    var selected = board, artifact = null, submitted = false;
    try {
      status('正在暂存文件；提交任务前请保持页面打开，串口仍可使用。');
      artifact = (await taskClient.request('/api/artifact-create', Object.assign(scope(selected),
        { name: file.name, size: file.size }))).artifact;
      beginProgress();
      for (var offset = 0; offset < file.size;) {
        checkCancelled();
        var chunk = new Uint8Array(await file.slice(offset, offset + 1024 * 1024).arrayBuffer());
        checkCancelled();
        if (!chunk.length) throw new Error('本地文件读取失败');
        var staged = (await taskClient.request('/api/artifact-append', Object.assign(scope(selected),
          { id: artifact.id, offset: offset, data_b64: bytes(chunk) }))).artifact;
        offset += chunk.length;
        if (staged.received !== offset) throw new Error('暂存偏移不匹配');
        progress(offset, file.size, '上传到上位机，尚未写入开发板');
      }
      checkCancelled();
      var sealed = (await taskClient.request('/api/artifact-seal', Object.assign(scope(selected),
        { id: artifact.id }))).artifact;
      if (sealed.size !== file.size || !/^[a-f0-9]{64}$/.test(sealed.sha256))
        throw new Error('暂存文件校验失败');
      checkCancelled();
      submitted = true;
      var done = await runTask(Object.assign(scope(selected), { kind: 'file-upload',
        connection_id: selected.connection_id, path: target, overwrite: overwrite, artifact_id: artifact.id }),
        { key: selected.key, serial: selected.serial || '' });
      byId('ft-progress').value = 100;
      status('上传完成 · 平均 ' + rateText(averageRate(done.size))
        + ' · ' + done.path + ' · SHA-256 ' + done.sha256);
    } finally {
      if (artifact && !submitted) await taskClient.request('/api/artifact-delete',
        Object.assign(scope(selected), { id: artifact.id })).catch(function () {});
    }
  }

  async function download(source) {
    var selected = board;
    var done = await runTask(Object.assign(scope(selected), { kind: 'file-download',
      connection_id: selected.connection_id, path: source }),
      { key: selected.key, serial: selected.serial || '' });
    byId('ft-progress').value = 100;
    status('下载已校验 · ' + SerialdUI.formatBytes(done.size) + ' · 平均 '
      + rateText(averageRate(done.size)) + ' · SHA-256 ' + done.sha256 + ' · ');
    var link = document.createElement('a'); link.className = 'log-download';
    link.textContent = '保存到本地'; link.download = done.name || basename(source);
    link.href = '/api/artifact-download?id=' + encodeURIComponent(done.artifact_id)
      + '&key=' + encodeURIComponent(selected.key)
      + (selected.serial ? '&serial=' + encodeURIComponent(selected.serial) : '');
    byId('ft-status').appendChild(link);
  }

  function cancelTask() {
    var entry = activeTask;
    if (!entry || entry.cancelPending) return;
    entry.cancelPending = true;
    taskClient.request('/api/task-cancel', { id: entry.id, key: entry.key, actor: 'web' })
      .then(function () {
        if (activeTask === entry) status('已请求取消；请在后台任务中确认最终结果。');
      })
      .catch(function (error) {
        if (activeTask === entry) status('取消请求失败：' + error.message + '；可在后台任务中重试。');
      })
      .finally(function () { entry.cancelPending = false; });
  }
  async function runTask(payload, ctx) {
    taskStarting = true; activeTask = null; beginProgress();
    status('正在提交后台传输；提交后关闭面板或刷新页面不会取消。');
    try {
      return await taskClient.start(payload, function (task) {
        var first = !activeTask;
        if (first) activeTask = { id: task.id, key: payload.key };
        taskStarting = false;
        if (first && cancelled) cancelTask();
        var p = task.progress || {};
        if (p.total !== undefined) progress(p.done || 0, p.total,
          '任务 ' + task.id + (p.phase === 'verifying' ? ' · 正在校验' : ''));
        else status('后台传输 ' + task.id + ' · 可关闭面板，在任务列表中查看。');
      }, ctx);
    } catch (error) {
      if (!activeTask && (!error.status || error.status >= 500))
        throw new Error(error.message + '；提交结果未确认，请在任务列表中核实后再重试。');
      throw error;
    } finally {
      taskStarting = false; activeTask = null;
    }
  }
  async function copy(source, destination, target, overwrite) {
    var selected = board;
    var done = await runTask(Object.assign(scope(selected), { kind: 'file-copy',
      connection_id: selected.connection_id, path: source, to_key: destination.key,
      to_serial: destination.serial || '', to_connection_id: destination.connection_id,
      to_path: target, overwrite: overwrite }), { key: selected.key });
    byId('ft-progress').value = 100;
    var rates = done.rate_diagnostics || {};
    status('板间复制完成 · 平均 ' + rateText((rates.effective_kib_s || 0) * 1024)
      + ' · 源读取 ' + rateText((rates.source_kib_s || 0) * 1024)
      + ' · 目标写入 ' + rateText((rates.destination_kib_s || 0) * 1024)
      + ' · ' + destination.key + ':' + target + ' · SHA-256 ' + done.sha256);
  }

  function updateMode() {
    var mode = byId('ft-mode').value;
    byId('ft-local-field').hidden = mode !== 'upload';
    byId('ft-source-field').hidden = mode === 'upload';
    byId('ft-target-field').hidden = mode === 'download';
    byId('ft-target-device-field').hidden = mode !== 'copy';
    byId('ft-overwrite-field').hidden = mode === 'download';
    byId('ft-start').disabled = busy || (mode === 'copy' && !byId('ft-target-device').value);
  }

  function open(selected, available) {
    if (busy) { dialog.show(); return; }
    dialog.removeAttribute('style');
    board = selected; devices = available;
    byId('ft-board').textContent = selected.key + ' · ' + (selected.dev || '串口');
    byId('ft-mode').value = 'upload';
    byId('ft-file').value = '';
    byId('ft-source').value = '';
    byId('ft-target').value = '';
    byId('ft-overwrite').checked = false;
    byId('ft-progress').value = 0;
    status('选择文件或填写绝对路径后开始。');
    var select = byId('ft-target-device'); select.textContent = '';
    available.filter(function (device) {
      return device.alive && device.key !== selected.key && device.connection_id;
    }).forEach(function (device) {
      var option = document.createElement('option');
      option.value = device.key; option.textContent = device.key + ' · ' + device.dev;
      select.appendChild(option);
    });
    updateMode();
    // Keep the workbench interactive: only the serial ports in this transfer
    // are reserved by the backend. Other boards and SSH tabs stay usable.
    dialog.show();
  }

  var handle = dialog.querySelector('.ft-head');
  handle.addEventListener('pointerdown', function (event) {
    if (event.button !== 0 || event.target.closest('button')) return;
    var rect = dialog.getBoundingClientRect();
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
      left: rect.left, top: rect.top };
    dialog.style.position = 'fixed';
    dialog.style.inset = 'auto';
    dialog.style.margin = '0';
    dialog.style.left = rect.left + 'px';
    dialog.style.top = rect.top + 'px';
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener('pointermove', function (event) {
    if (!drag || drag.id !== event.pointerId) return;
    var rect = dialog.getBoundingClientRect();
    var left = Math.max(0, Math.min(window.innerWidth - rect.width, drag.left + event.clientX - drag.x));
    var top = Math.max(0, Math.min(window.innerHeight - rect.height, drag.top + event.clientY - drag.y));
    dialog.style.left = left + 'px'; dialog.style.top = top + 'px';
  });
  function endDrag(event) {
    if (drag && drag.id === event.pointerId) drag = null;
  }
  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);
  window.addEventListener('resize', function () {
    if (!dialog.open || !dialog.style.left) return;
    var rect = dialog.getBoundingClientRect();
    dialog.style.left = Math.max(0, Math.min(rect.left, window.innerWidth - rect.width)) + 'px';
    dialog.style.top = Math.max(0, Math.min(rect.top, window.innerHeight - rect.height)) + 'px';
  });

  byId('ft-mode').addEventListener('change', updateMode);
  byId('ft-target-device').addEventListener('change', updateMode);
  byId('ft-file').addEventListener('change', function () {
    var file = byId('ft-file').files[0];
    if (file && !byId('ft-target').value) byId('ft-target').value = '/tmp/' + file.name;
  });
  byId('ft-close').addEventListener('click', function () {
    dialog.close();
  });
  byId('ft-cancel').addEventListener('click', function () {
    cancelled = true;
    if (activeTask || taskStarting) { status('正在请求取消后台传输…'); cancelTask(); }
    else status('正在取消，等待当前分块结束…');
  });
  byId('ft-start').addEventListener('click', async function () {
    if (busy || !board) return;
    var mode = byId('ft-mode').value;
    var source = byId('ft-source').value.trim(), target = byId('ft-target').value.trim();
    var file = byId('ft-file').files[0];
    var overwrite = byId('ft-overwrite').checked;
    var destination = devices.filter(function (device) { return device.key === byId('ft-target-device').value; })[0];
    if ((mode === 'upload' && !file) || (mode !== 'upload' && !source.startsWith('/'))
        || (mode !== 'download' && !target.startsWith('/')) || (mode === 'copy' && !destination)) {
      status('请选择本地文件、在线目标板，并填写板端绝对路径。'); return;
    }
    busy = true; cancelled = false;
    byId('ft-cancel').disabled = false; updateMode();
    byId('ft-progress').value = 0;
    status('正在准备串口连接…');
    try {
      if (mode === 'upload') await upload(file, target, overwrite);
      else if (mode === 'download') await download(source);
      else await copy(source, destination, target, overwrite);
    } catch (error) {
      status('传输未完成：' + error.message);
    } finally {
      busy = false; cancelled = false;
      byId('ft-cancel').disabled = true; updateMode();
    }
  });

  global.SerialdFileTransfer = { open: open };
})(window);
