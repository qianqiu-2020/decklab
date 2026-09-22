(function () {
  'use strict';
  var button = document.getElementById('b-share');
  var status = document.getElementById('share-status');
  fetch('/api/access', { cache: 'no-store' }).then(function (response) { return response.json(); })
    .then(function (access) { button.hidden = !access.share_enabled; })
    .catch(function () { button.hidden = true; });

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText)
      return navigator.clipboard.writeText(text);
    var field = document.createElement('textarea');
    field.value = text;
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    var copied = document.execCommand('copy');
    field.remove();
    return copied ? Promise.resolve() : Promise.reject(new Error('clipboard unavailable'));
  }

  button.addEventListener('click', async function () {
    button.disabled = true;
    status.textContent = '正在创建分享链接…';
    try {
      var host = location.hostname;
      if (host === 'localhost' || host === '127.0.0.1' || host === '::1') {
        host = window.prompt('输入这台 Windows 电脑的局域网 IP，以便其他人打开链接：', '');
        if (!host) { status.textContent = '未创建分享链接'; return; }
      }
      var response = await fetch('/api/share-link', { method: 'POST' });
      var result = await response.json();
      if (!response.ok || !result.path) throw new Error(result.error || '创建失败');
      var url = new URL(result.path, location.origin);
      url.hostname = host;
      try {
        await copy(url.href);
        status.textContent = '链接已复制，24 小时有效';
      } catch (_) {
        window.prompt('复制分享链接（24 小时有效）：', url.href);
        status.textContent = '链接已生成，24 小时有效';
      }
    } catch (error) {
      status.textContent = '分享失败：' + error.message;
    } finally {
      button.disabled = false;
    }
  });
})();
