// Provider-neutral board controls; the backend owns mapping, secrets and tasks.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.DeckLabTasks.createClient(options.fetch);
    var names = {reset: '复位', 'power-on': '上电', 'power-off': '断电', 'enter-download': '进入下载模式'};
    var selection = '', generation = 0, actions = [], wiring = null, target = null, pending = Object.create(null);
    function scope(ctx) { return ctx && ctx.connectionId ? (ctx.serial || ctx.key || '') + ':' + ctx.connectionId : ''; }
    function current(ctx) { return !!ctx && scope(options.getView()) === scope(ctx); }
    function close() {
      if (target && target.failed) selection = '';
      target = null;
      $('board-control-dialog').close();
    }
    function refresh(ctx) {
      var key = scope(ctx);
      var owner = !!(options.canBind && options.canBind());
      if (key + owner === selection) return;
      close(); selection = key + owner; generation++; actions = []; wiring = null; $('b-board-control').hidden = true;
      $('b-board-slot-bind').hidden = !key || !owner;
      if (!key) return;
      if (!ctx.serial) return;
      var version = generation;
      client.request('/api/hardware?key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial)
        + '&connection_id=' + encodeURIComponent(ctx.connectionId)).then(function (data) {
        if (version !== generation || scope(options.getView()) !== key) return;
        wiring = data.wiring || null;
        actions = (data.actions || []).filter(function (action) { return Object.prototype.hasOwnProperty.call(names, action); });
        $('b-board-control').hidden = (!data.supported || !actions.length) && !owner;
        $('b-board-slot-bind').hidden = !owner || !!wiring && !data.slot_handoff;
        if ($('board-slot-bind')) $('board-slot-bind').textContent = data.slot_handoff ? '确认换板接线' : '绑定控制槽位';
      }).catch(function () { if (version === generation) selection = ''; });
    }
    function open(ctx) {
      if (!scope(ctx) || (!actions.length && !(options.canBind && options.canBind())) || pending[scope(ctx)]) return;
      target = {key: ctx.key, serial: ctx.serial, connectionId: ctx.connectionId};
      $('board-control-target').textContent = (ctx.serial || ctx.key) + (wiring ? ' · ' + wiring.name + ' / ' + wiring.number : '');
      $('board-control-action').textContent = '';
      if ($('board-slot-bind')) {
        $('board-slot-bind').hidden = !(options.canBind && options.canBind());
        $('board-control-action-field').hidden = !actions.length;
        $('board-control-submit').hidden = !actions.length;
      }
      actions.forEach(function (action) {
        var item = document.createElement('option'); item.value = action; item.textContent = names[action];
        $('board-control-action').appendChild(item);
      });
      $('board-control-action').value = actions[0];
      $('board-control-submit').disabled = false; $('board-control-status').textContent = '';
      $('board-control-dialog').showModal();
    }
    function submit() {
      var ctx = target, action = $('board-control-action').value;
      if (!current(ctx) || pending[scope(ctx)] || actions.indexOf(action) === -1) return;
      pending[scope(ctx)] = true; $('board-control-submit').disabled = true;
      $('board-control-status').textContent = '正在执行…';
      var taskId = '';
      client.start({key: ctx.key, serial: ctx.serial, connection_id: ctx.connectionId,
        kind: 'hardware-' + action, actor: 'web', wiring_revision: wiring ? wiring.revision : ''}, function (task) {
        taskId = task.id;
        if (options.onTask) options.onTask();
      }).then(function (result) {
        if (target !== ctx || !current(ctx)) return;
        close();
        if (options.notice) options.notice(result.control_result === 'acknowledged'
          ? '控制请求已确认，请查看终端启动输出。' : '操作已完成。', options.getView(), taskId);
      }).catch(function (error) {
        if (target !== ctx || !current(ctx)) return;
        ctx.failed = true;
        $('board-control-status').textContent = error.submissionUnknown
          ? '提交结果未知，请先在任务中检查记录。' : error.message;
        if (options.openTaskDetails && taskId) {
          var details = document.createElement('button');
          details.type = 'button'; details.textContent = '任务详情';
          details.addEventListener('click', function () {
            if (target === ctx && current(ctx)) options.openTaskDetails(options.getView(), taskId);
          });
          $('board-control-status').appendChild(details);
        }
      }).finally(function () { delete pending[scope(ctx)]; });
      // One submission per dialog. Reopening is an explicit new operation;
      // late replies cannot re-enable a button or act on another board.
    }
    function bind(ctx) {
      if (!current(ctx) || !(options.canBind && options.canBind())) return;
      global.DeckLabControlSlots.open(ctx.serial, ctx.serial ? null : {key:ctx.key, connection_id:ctx.connectionId},
        options.openTaskDetails ? function (id) { options.openTaskDetails(null, id); } : null);
    }
    $('b-board-control').addEventListener('click', function () { open(options.getView()); });
    $('b-board-slot-bind').addEventListener('click', function () { bind(options.getView()); });
    if ($('board-slot-bind')) $('board-slot-bind').addEventListener('click', function () {
      if (!target || !current(target) || !(options.canBind && options.canBind())) return;
      bind(options.getView());
    });
    $('board-control-close').addEventListener('click', close);
    $('board-control-cancel').addEventListener('click', close);
    $('board-control-submit').addEventListener('click', submit);
    $('board-control-dialog').addEventListener('cancel', close);
    return {refresh: refresh, close: close, bind: bind, invalidate: function () { selection = ''; refresh(options.getView()); }};
  }
  global.DeckLabBoardControl = {create: create};
})(window);
