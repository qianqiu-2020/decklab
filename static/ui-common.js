(function (global) {
  'use strict';
  function byId(id) { return document.getElementById(id); }
  function formatBytes(n) {
    if (n == null) return '—';
    var units = ['B', 'KB', 'MB', 'GB'], index = 0;
    while (n >= 1024 && index < units.length - 1) { n /= 1024; index++; }
    return (index === 0 ? n : n.toFixed(1)) + ' ' + units[index];
  }
  function age(ts) {
    if (!ts) return '从未';
    var seconds = Math.max(0, Date.now() / 1000 - ts);
    if (seconds < 60) return Math.floor(seconds) + ' 秒前';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
    return Math.floor(seconds / 3600) + ' 小时前';
  }
  function copyText(text, silent) {
    var area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
    area.remove();
    if (!silent) {
      byId('d-status').textContent = ok ? (' 已复制 ' + text.length + ' 字符') : ' 复制失败（浏览器限制）';
    }
    return ok;
  }
  global.SerialdUI = { byId: byId, formatBytes: formatBytes, age: age, copyText: copyText };
})(window);
