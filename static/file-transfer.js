(function (global) {
  'use strict';
  var byId = function (id) { return document.getElementById(id); };
  var dialog = byId('file-transfer-dialog');
  var board = null, devices = [], busy = false, cancelled = false;
  var MAX_BLOB_DOWNLOAD = 128 * 1024 * 1024;

  function status(text) { byId('ft-status').textContent = text; }
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
  function progress(offset, size) {
    byId('ft-progress').value = size ? Math.round(offset / size * 100) : 100;
    status(SerialdUI.formatBytes(offset) + ' / ' + SerialdUI.formatBytes(size) + ' · 串口传输中');
  }
  async function api(device, action, payload) {
    var response = await fetch('/api/file-transfer/' + action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ key: device.key, serial: device.serial || '',
        connection_id: device.connection_id, actor: 'web' }, payload))
    });
    var result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '传输请求失败');
    return result;
  }
  function checkCancelled() { if (cancelled) throw new Error('已取消传输'); }

  async function upload(file, target, overwrite) {
    var transfer = null;
    try {
      transfer = await api(board, 'start', { direction: 'upload', path: target,
        size: file.size, overwrite: overwrite });
      for (var offset = 0; offset < file.size;) {
        checkCancelled();
        var chunk = new Uint8Array(await file.slice(offset, offset + transfer.chunk_size).arrayBuffer());
        var result = await api(board, 'chunk', { id: transfer.id, offset: offset, data_b64: bytes(chunk) });
        offset = result.offset;
        progress(offset, file.size);
      }
      checkCancelled();
      var done = await api(board, 'finish', { id: transfer.id });
      transfer = null;
      byId('ft-progress').value = 100;
      status('上传完成 · ' + done.path + ' · SHA-256 ' + done.sha256);
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
      status('下载完成 · ' + SerialdUI.formatBytes(done.size) + ' · SHA-256 ' + done.sha256);
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
      for (var offset = 0; offset < incoming.size;) {
        checkCancelled();
        var chunk = await api(board, 'chunk', { id: incoming.id, offset: offset });
        var sent = await api(destination, 'chunk', { id: outgoing.id, offset: offset,
          data_b64: chunk.data_b64 });
        if (sent.offset !== chunk.offset) throw new Error('板间传输偏移不匹配');
        offset = sent.offset;
        progress(offset, incoming.size);
      }
      checkCancelled();
      var sourceDone = await api(board, 'finish', { id: incoming.id });
      incoming = null;
      var targetDone = await api(destination, 'finish', { id: outgoing.id });
      outgoing = null;
      if (sourceDone.sha256 !== targetDone.sha256) throw new Error('目标板文件校验失败');
      byId('ft-progress').value = 100;
      status('板间复制完成 · ' + destination.key + ':' + target + ' · SHA-256 ' + targetDone.sha256);
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
