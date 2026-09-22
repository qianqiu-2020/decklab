(function (global) {
  'use strict';
  var byId = function (id) { return document.getElementById(id); };
  var dialog = byId('file-transfer-dialog');
  var board = null, devices = [], busy = false, cancelled = false;
  var meter = null, drag = null;
  var MAX_BLOB_DOWNLOAD = 128 * 1024 * 1024;

  function status(text) { byId('ft-status').textContent = text; }
  function rateText(value) { return SerialdUI.formatBytes(Math.round(value)) + '/s'; }
  function basename(path) { return path.split('/').filter(Boolean).pop() || 'board-file'; }
  function bytes(raw) {
    var binary = '';
    for (var i = 0; i < raw.length; i++) binary += String.fromCharCode(raw[i]);
    return btoa(binary);
  }
  function decode(encoded) {
    var binary = atob(encoded), raw = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) raw[i] = binary.charCodeAt(i);
    return raw;
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
  async function api(device, action, payload) {
    var response = await fetch('/api/file-transfer/' + action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ key: device.key, serial: device.serial || '',
        connection_id: device.connection_id, actor: 'web' }, payload))
    });
    var result = await response.json();
    if (!response.ok || !result.ok) {
      var error = new Error(result.error || '传输请求失败');
      error.status = response.status;
      throw error;
    }
    return result;
  }
  async function timedApi(device, action, payload) {
    var started = performance.now();
    var result = await api(device, action, payload);
    return { result: result, seconds: (performance.now() - started) / 1000 };
  }
  async function writeBlock(device, transfer, offset, raw) {
    var started = performance.now(), sent = 0;
    if (transfer.batch_size && raw.length <= transfer.batch_size) {
      var batch = await api(device, 'batch', { id: transfer.id, offset: offset,
        data_b64: bytes(raw) });
      if (batch.offset !== offset + raw.length) throw new Error('目标板批量传输偏移不匹配');
      return { result: batch, seconds: (performance.now() - started) / 1000 };
    }
    while (sent < raw.length) {
      var length = raw.length - sent;
      for (;;) {
        try {
          var result = await api(device, 'chunk', { id: transfer.id, offset: offset + sent,
            data_b64: bytes(raw.subarray(sent, sent + length)) });
          if (result.offset !== offset + sent + length) throw new Error('目标板传输偏移不匹配');
          sent += length;
          break;
        } catch (error) {
          if (error.status !== 413 || length === 1) throw error;
          length = Math.max(1, Math.floor(length / 2));
        }
      }
    }
    return { result: { offset: offset + sent }, seconds: (performance.now() - started) / 1000 };
  }
  function checkCancelled() { if (cancelled) throw new Error('已取消传输'); }

  async function upload(file, target, overwrite) {
    var transfer = null;
    try {
      transfer = await api(board, 'start', { direction: 'upload', path: target,
        size: file.size, overwrite: overwrite });
      beginProgress();
      for (var offset = 0; offset < file.size;) {
        checkCancelled();
        var chunk = new Uint8Array(await file.slice(offset,
          offset + (transfer.batch_size || transfer.chunk_size)).arrayBuffer());
        var written = await writeBlock(board, transfer, offset, chunk);
        offset = written.result.offset;
        progress(offset, file.size);
      }
      checkCancelled();
      var done = await api(board, 'finish', { id: transfer.id });
      transfer = null;
      byId('ft-progress').value = 100;
      status('上传完成 · 平均 ' + rateText(averageRate(done.size))
        + ' · ' + done.path + ' · SHA-256 ' + done.sha256);
    } finally {
      if (transfer) await api(board, 'abort', { id: transfer.id }).catch(function () {});
    }
  }

  async function download(source) {
    var transfer = null, writable = null, parts = [];
    try {
      if (typeof global.showSaveFilePicker === 'function') {
        var handle = await global.showSaveFilePicker({ suggestedName: basename(source) });
        writable = await handle.createWritable();
      }
      transfer = await api(board, 'start', { direction: 'download', path: source });
      beginProgress();
      if (!writable && transfer.size > MAX_BLOB_DOWNLOAD) {
        throw new Error('浏览器下载超过 128 MiB，请用 serialctl file-get，或在支持文件流保存的浏览器中打开');
      }
      for (var offset = 0; offset < transfer.size;) {
        checkCancelled();
        var chunk = await api(board, 'chunk', { id: transfer.id, offset: offset });
        var data = decode(chunk.data_b64);
        if (chunk.offset !== offset + data.length) throw new Error('下载偏移不匹配');
        if (writable) await writable.write(data); else parts.push(data);
        offset = chunk.offset;
        progress(offset, transfer.size);
      }
      checkCancelled();
      var done = await api(board, 'finish', { id: transfer.id });
      transfer = null;
      if (writable) {
        await writable.close(); writable = null;
      } else {
        var url = URL.createObjectURL(new Blob(parts, { type: 'application/octet-stream' }));
        var anchor = document.createElement('a');
        anchor.href = url; anchor.download = basename(source);
        document.body.appendChild(anchor); anchor.click(); anchor.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      }
      byId('ft-progress').value = 100;
      status('下载完成 · ' + SerialdUI.formatBytes(done.size) + ' · 平均 '
        + rateText(averageRate(done.size)) + ' · SHA-256 ' + done.sha256);
    } finally {
      if (transfer) await api(board, 'abort', { id: transfer.id }).catch(function () {});
      if (writable) await writable.abort().catch(function () {});
    }
  }

  async function copy(source, destination, target, overwrite) {
    var incoming = null, outgoing = null;
    try {
      incoming = await api(board, 'start', { direction: 'download', path: source });
      outgoing = await api(destination, 'start', { direction: 'upload', path: target,
        size: incoming.size, overwrite: overwrite });
      beginProgress();
      var sourceSeconds = 0, targetSeconds = 0, sourceBytes = 0, targetBytes = 0;
      var batchSize = outgoing.batch_size || incoming.chunk_size;
      async function readBatch(offset) {
        var parts = [], length = 0, seconds = 0;
        while (offset + length < incoming.size && length < batchSize) {
          var read = await timedApi(board, 'chunk', { id: incoming.id, offset: offset + length });
          var data = decode(read.result.data_b64);
          if (!data.length || read.result.offset !== offset + length + data.length)
            throw new Error('源板传输偏移不匹配');
          parts.push(data); length += data.length; seconds += read.seconds;
        }
        var joined = new Uint8Array(length), cursor = 0;
        parts.forEach(function (part) { joined.set(part, cursor); cursor += part.length; });
        return { data: joined, offset: offset + length, seconds: seconds };
      }
      var chunk = incoming.size ? await readBatch(0) : null;
      if (chunk) { sourceSeconds += chunk.seconds; sourceBytes = chunk.offset; }
      for (var offset = 0; offset < incoming.size;) {
        checkCancelled();
        if (chunk.offset !== offset + chunk.data.length || chunk.offset > incoming.size)
          throw new Error('源板传输偏移不匹配');
        // Source and destination are independent serial ports. Read the next
        // block while writing this one, then inspect both outcomes together.
        var next = chunk.offset < incoming.size ? readBatch(chunk.offset) : null;
        var writes = [writeBlock(destination, outgoing, offset, chunk.data)];
        if (next) writes.push(next);
        var outcomes = await Promise.allSettled(writes);
        var failure = outcomes.find(function (item) { return item.status === 'rejected'; });
        if (failure) throw failure.reason;
        if (outcomes[0].value.result.offset !== chunk.offset) throw new Error('目标板传输偏移不匹配');
        targetSeconds += outcomes[0].value.seconds;
        targetBytes += chunk.offset - offset;
        offset = outcomes[0].value.result.offset;
        if (next) {
          var read = outcomes[1].value;
          sourceSeconds += read.seconds;
          sourceBytes = read.offset;
          chunk = read;
        } else chunk = null;
        var sourceRate = sourceSeconds ? rateText(sourceBytes / sourceSeconds) : '计算中';
        var targetRate = targetSeconds ? rateText(targetBytes / targetSeconds) : '计算中';
        progress(offset, incoming.size, '源读取 ' + sourceRate + ' · 目标写入 ' + targetRate);
      }
      checkCancelled();
      var sourceDone = await api(board, 'finish', { id: incoming.id });
      incoming = null;
      var targetDone = await api(destination, 'finish', { id: outgoing.id });
      outgoing = null;
      if (sourceDone.sha256 !== targetDone.sha256) throw new Error('目标板文件校验失败');
      byId('ft-progress').value = 100;
      status('板间复制完成 · 平均 ' + rateText(averageRate(targetDone.size))
        + ' · 源读取 ' + rateText(sourceBytes / Math.max(sourceSeconds, 0.001))
        + ' · 目标写入 ' + rateText(targetBytes / Math.max(targetSeconds, 0.001))
        + ' · ' + destination.key + ':' + target + ' · SHA-256 ' + targetDone.sha256);
    } finally {
      if (incoming) await api(board, 'abort', { id: incoming.id }).catch(function () {});
      if (outgoing) await api(destination, 'abort', { id: outgoing.id }).catch(function () {});
    }
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
    if (busy) return;
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
    dialog.showModal();
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
    if (busy) { cancelled = true; status('正在取消，等待当前分块结束…'); }
    else dialog.close();
  });
  byId('ft-cancel').addEventListener('click', function () {
    cancelled = true; status('正在取消，等待当前分块结束…');
  });
  dialog.addEventListener('cancel', function (event) {
    if (busy) { event.preventDefault(); cancelled = true; status('正在取消，等待当前分块结束…'); }
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
