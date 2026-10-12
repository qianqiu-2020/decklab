(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var devices = [], requestId = { left: 0, right: 0 };

  function option(select, value, label) {
    var item = document.createElement('option');
    item.value = value; item.textContent = label; select.appendChild(item);
  }

  function board(key) { return devices.find(function (item) { return item.key === key; }); }
  function label(item) { return (item.label || item.key) + ' · ' + item.serial; }
  function query(values) { return new URLSearchParams(values).toString(); }
  async function boots(side, preferred) {
    var selected = board($('bc-' + side + '-device').value);
    var pick = $('bc-' + side + '-boot'); pick.textContent = '';
    if (!selected) return;
    var sequence = ++requestId[side];
    try {
      var response = await fetch('/api/logs?' + query({ key: selected.key, serial: selected.serial }), { cache: 'no-store' });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || '读取归档失败');
      if (!$('boot-compare-dialog').open || sequence !== requestId[side]) return;
      (result.boots || []).forEach(function (boot) {
        option(pick, boot.id, '第 ' + boot.number + ' 次 · ' + (boot.started || '时间未知') + ' · ' + (boot.marker || '启动'));
      });
      if (preferred && Array.from(pick.options).some(function (entry) { return entry.value === preferred; })) pick.value = preferred;
      else if (side === 'left' && pick.options.length > 1) pick.selectedIndex = 1;
      $('bc-status').textContent = pick.options.length ? '选择两次启动后比较。' : '该设备暂无可识别的启动记录。';
    } catch (error) {
      if (sequence === requestId[side]) $('bc-status').textContent = error.message;
    }
  }

  function render(result) {
    var counts = result.counts;
    $('bc-results').hidden = false;
    $('bc-summary').textContent = '新增 ' + counts.added + ' 行 · 缺少 ' + counts.removed
      + ' 行 · 变化 ' + counts.changed + ' 行' + (result.truncated ? ' · 差异过多，仅显示前 1500 行' : '');
    var clues = $('bc-clues'); clues.textContent = '';
    var title = document.createElement('strong'); title.textContent = '目标启动新增的异常线索'; clues.appendChild(title);
    if (!result.clues.length) {
      var empty = document.createElement('p'); empty.textContent = '未发现新增的错误或超时关键词；请查看下方差异。'; clues.appendChild(empty);
    } else result.clues.forEach(function (clue) {
      var row = document.createElement('div');
      var number = document.createElement('span'); number.textContent = '第 ' + clue.line + ' 行  ';
      var text = document.createElement('span'); text.className = 'bc-clue-text'; text.textContent = clue.text;
      row.appendChild(number); row.appendChild(text); clues.appendChild(row);
    });
    var diff = $('bc-diff'); diff.textContent = '';
    if (!result.rows.length) {
      var same = document.createElement('p'); same.textContent = '归一化时间戳后，两次启动的日志内容一致。'; diff.appendChild(same);
    }
    result.rows.forEach(function (row) {
      var line = document.createElement('div'); line.className = 'bc-row bc-' + row.type;
      ['left', 'right'].forEach(function (side) {
        var cell = document.createElement('div'); cell.className = 'bc-cell';
        var number = document.createElement('span'); number.className = 'bc-line-no';
        number.textContent = row[side + '_no'] || '';
        var content = document.createElement('span'); content.className = 'bc-content';
        var spans = row[side + '_spans'];
        if (Array.isArray(spans)) spans.forEach(function (part) {
          var segment = document.createElement('span'); segment.textContent = part.text;
          if (part.changed) segment.className = side === 'left' ? 'bc-inline-remove' : 'bc-inline-add';
          content.appendChild(segment);
        });
        else content.textContent = row[side] || '';
        cell.appendChild(number); cell.appendChild(content); line.appendChild(cell);
      });
      diff.appendChild(line);
    });
  }

  async function run() {
    var left = board($('bc-left-device').value), right = board($('bc-right-device').value);
    var leftId = $('bc-left-boot').value, rightId = $('bc-right-boot').value;
    if (!left || !right || !leftId || !rightId) { $('bc-status').textContent = '请先选择两次启动。'; return; }
    if (left.serial === right.serial && leftId === rightId) { $('bc-status').textContent = '请选择不同的启动记录。'; return; }
    $('bc-run').disabled = true; $('bc-status').textContent = '正在比较…'; $('bc-results').hidden = true;
    try {
      var response = await fetch('/api/boot-compare?' + query({
        left_key: left.key, left_serial: left.serial, left_id: leftId,
        right_key: right.key, right_serial: right.serial, right_id: rightId
      }), { cache: 'no-store' });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || '比较失败');
      render(result); $('bc-status').textContent = '对比完成。只显示变化处及前后两行。';
    } catch (error) { $('bc-status').textContent = error.message; }
    finally { $('bc-run').disabled = false; }
  }

  ['left', 'right'].forEach(function (side) {
    $('bc-' + side + '-device').addEventListener('change', function () {
      $('bc-results').hidden = true; boots(side);
    });
  });
  $('bc-run').addEventListener('click', run);
  $('bc-close').addEventListener('click', function () { $('boot-compare-dialog').close(); });
  window.SerialdBootCompare = { open: function (selected, available) {
    devices = (available || []).filter(function (entry) { return !!entry.serial; });
    if (!devices.length) return;
    ['left', 'right'].forEach(function (side) {
      var pick = $('bc-' + side + '-device'); pick.textContent = '';
      devices.forEach(function (entry) { option(pick, entry.key, label(entry)); });
      pick.value = selected && selected.serial ? selected.key : devices[0].key;
    });
    $('bc-results').hidden = true;
    $('boot-compare-dialog').showModal();
    boots('left'); boots('right');
  } };
})();
