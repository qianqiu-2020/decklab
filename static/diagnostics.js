(function (global) {
  'use strict';
  var taskStates = {queued:'等待执行', running:'执行中', completed:'已完成', failed:'失败', cancelled:'已取消', interrupted:'已中断'};
  var outcomes = {'not-issued':'尚未发出', issued:'请求已发出', acknowledged:'执行方已确认', observed:'已观察到结果', verified:'已校验', unknown:'结果未知'};
  var boardStates = {online:'在线', pending:'待识别', offline:'离线'};
  var probeModes = {linux:'内置 Linux 探测', custom:'自定义 shell 命令', manual:'仅使用终端'};
  var healthStates = {pending:'等待首次检查', waiting:'等待空闲控制台', ok:'往返检查通过', error:'往返检查异常'};
  var osNames = {debian:'Debian', bianbu:'Bianbu', buildroot:'Buildroot'};
  var activationPhases = {'validating-settings':'核对保存的配置', 'checking-web-listener':'检查网页监听地址',
    'checking-service-paths':'检查服务目录', 'checking-backend-config':'检查硬件后端配置',
    'checking-collector-idle':'检查采集器是否空闲', 'waiting-collector-ready':'等待采集器就绪',
    'requesting-collector-restart':'请求采集器重启', 'waiting-collector-revision':'等待采集器加载配置',
    'collector-revision-active':'采集器已加载配置', 'restarting-web':'重启网页服务',
    'restarting-backend':'重启硬件后端', 'restarting-windows':'重启 Windows 服务',
    'revision-active':'已确认加载目标配置'};
  function create(options) {
    var $ = options.byId, dialog = $('diagnostics-dialog'), active = null;
    var status = $('diagnostics-status'), body = $('diagnostics-body'), copy = $('diagnostics-copy');
    var report = [];
    function translate(value) { return global.DeckLabI18n ? global.DeckLabI18n.translate(value) : value; }
    function request(url) {
      return options.fetch(url, {cache:'no-store'}).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      });
    }
    function time(value) { return value > 0 ? new Date(value * 1000).toLocaleString() : ''; }
    function size(value) { return Number.isFinite(value) ? options.formatBytes(value) : ''; }
    function known(table, value) { return Object.hasOwn(table, value) ? translate(table[value]) : value; }
    function section(title, fields) {
      fields = fields.filter(function (row) { return row[1] !== '' && row[1] != null; });
      if (!fields.length) return;
      var heading = document.createElement('h3'); heading.textContent = title;
      var list = document.createElement('dl');
      report.push(translate(title));
      fields.forEach(function (row) {
        var term = document.createElement('dt'); term.textContent = row[0];
        var value = document.createElement('dd'); value.textContent = String(row[1]);
        value.dataset.i18nIgnore = '';
        list.appendChild(term); list.appendChild(value);
        report.push(translate(row[0]) + ' ' + value.textContent);
      });
      body.appendChild(heading); body.appendChild(list);
    }
    function sideError(result) { return result.status === 'rejected' ? result.reason.message : ''; }
    function system() {
      return Promise.allSettled([request('/healthz'), request('/api/health')]).then(function (results) {
        return function () {
          var web = results[0].value || {}, collector = results[1].value || {}, storage = collector.storage || {};
          var listeners = web.listeners || {}, https = listeners.https || {};
          section('网页服务', [['当前版本', web.version], ['访问地址', global.location.origin],
            ['硬件连接', known({local:'本机采集器', https:'远程硬件后端'}, web.backend_transport)],
            ['HTTP 监听端口', listeners.http_port],
            ['内置 HTTPS', known({disabled:'未启用', active:'监听中', failed:'启动失败'}, https.state)],
            ['HTTPS 端口', https.port || null],
            ['证书来源', known({'decklab-local':'DeckLab 本地证书', provided:'指定的证书'}, https.certificate)],
            ['失败阶段', known({configuration:'读取 HTTPS 配置', bind:'绑定端口', certificate:'加载或签发证书'}, https.phase)],
            ['读取失败', sideError(results[0])]]);
          section('采集与归档', [['当前版本', collector.version], ['启动时间', time(collector.started)],
            ['在线设备', collector.online], ['待识别串口', collector.pending], ['历史设备', collector.offline],
            ['日志写入', collector.logging_ok == null ? '' : translate(collector.logging_ok ? '正常' : '有写入错误')],
            ['日志磁盘剩余', size(storage.free)], ['日志磁盘容量', size(storage.total)],
            ['日志错误数', Array.isArray(collector.log_errors) ? collector.log_errors.length : ''],
            ['读取失败', sideError(results[1])]]);
        };
      });
    }
    function activation(target) {
      return request('/api/settings' + (target === 'backend' ? '?target=backend' : '')).then(function (snapshot) {
        return function () {
          var attempt = snapshot.activation || {};
          section('配置修订', [['已保存的配置修订', snapshot.revision], ['当前生效的配置修订', snapshot.active_revision],
            ['加载状态', translate(snapshot.active_revision === snapshot.revision ? '配置已生效' : '已保存的配置待应用')]]);
          section('最近一次应用', [['状态', known({idle:'暂无应用记录', applying:'正在应用', applied:'已确认加载目标配置',
            failed:'应用失败', unknown:'结果未知', interrupted:'已中断'}, attempt.state || 'idle')],
            ['目标修订', attempt.revision],
            ['重启范围', known({web:'网页服务', 'web-and-collector':'网页服务和采集器',
              backend:'硬件后端', windows:'Windows 主程序'}, attempt.mode)],
            ['阶段', known(activationPhases, attempt.phase)],
            ['阻塞原因', known({'active-api':'还有 API 操作未完成', 'active-tasks':'后台任务仍在运行',
              'board-holds':'存在刷机占用或恢复保留', 'serial-occupied':'串口有进行中的操作或输入占用',
              'file-transfer':'文件传输仍在运行', 'session-starting':'SSH 仍在建立连接'}, attempt.blocker)],
            ['HTTP 状态', attempt.http_status]]);
          if (Array.isArray(attempt.steps)) section('执行经过', attempt.steps.map(function (step) {
            return [known(activationPhases, step.phase), time(step.at_ms / 1000)];
          }));
        };
      });
    }
    function query(ctx) {
      return 'key=' + encodeURIComponent(ctx.key) + '&serial=' + encodeURIComponent(ctx.serial || '')
        + (ctx.connectionId ? '&connection_id=' + encodeURIComponent(ctx.connectionId) : '');
    }
    function device(ctx) {
      var board = options.getBoard(ctx.key);
      if (!board || (ctx.connectionId && board.connection_id !== ctx.connectionId)) {
        return Promise.reject(new Error(translate('设备连接已变化，请重新打开诊断。')));
      }
      return Promise.allSettled([ctx.serial ? request('/api/hardware?' + query(ctx)) : Promise.resolve({}),
        request('/api/sessions?' + query(ctx))])
        .then(function (results) {
          return function () {
            var health = board.serial_health || {}, info = board.info || {};
            var uart = board.serial_settings || {}, hardware = results[0].value || {}, wiring = hardware.wiring || {};
            var sessions = results[1].value?.sessions;
            var owner = board.input_owner || {}, operation = board.operation || {};
            section('串口采集', [['设备状态', known(boardStates, board.state)], ['串口', board.dev || board.last_dev],
              ['串口位置', board.by_path || board.last_path], ['连接编号', board.connection_id],
              ['采集状态', typeof board.alive !== 'boolean' ? '' : translate(board.capture_paused ? '已暂停' : board.alive ? '采集中' : '未连接')],
              ['串口配置', board.profile], ['待应用设置', board.settings_pending ? translate('重新连接串口时生效') : ''],
              ['波特率', uart.baud], ['数据位 / 校验 / 停止位', uart.data_bits ? [uart.data_bits, uart.parity, uart.stop_bits].join(' / ') : ''],
              ['采集开始', time(board.capture_started)], ['收到数据', size(board.bytes)],
              ['最后收到数据', time(board.last_rx) || (board.alive ? translate('本次连接尚未收到数据') : '')],
              ['日志文件', board.log], ['日志错误', board.log_error], ['连续打开失败', board.fail_count || ''],
              ['最近一次恢复', board.rescue_note]]);
            section('串口健康与占用', [['检查状态', health.running ? translate('正在检查') : known(healthStates, health.status)],
              ['检查原因', health.reason], ['上次检查', time(health.last_attempt)], ['上次通过', time(health.last_success)],
              ['下次检查', time(health.next_check)], ['输入占用', owner.actor || translate('无')],
              ['运行中的操作', operation.kind], ['操作编号', operation.id]]);
            section('板卡身份', [['板卡序列号', board.serial], ['固件原始编号', board.reported_serial || info.reported_serial],
              ['身份来源', info.serial_source], ['身份状态', known({confirmed:'已确认', provisional:'待确认'}, info.identity_status)],
              ['探测方式', known(probeModes, board.probe_mode)], ['当前识别的系统', known(osNames, board.ssh_settings?.os_family)],
              ['识别时间', time(info.ts)], ['识别异常', board.identity_error]]);
            section('会话与硬件控制', [['打开的 SSH 会话', sessions?.filter(function (s) { return s.kind === 'ssh' && s.alive !== false; }).length],
              ['打开的 ADB 会话', sessions?.filter(function (s) { return s.kind === 'adb' && s.alive !== false; }).length],
              ['会话读取失败', sideError(results[1])],
              ['电源与启动', !ctx.serial ? translate('识别设备后可查看控制槽位') : typeof hardware.supported !== 'boolean'
                ? '' : translate(hardware.supported && hardware.actions?.length ? '可用' : '未配置或不支持')],
              ['控制器槽位', wiring.name != null ? wiring.name + ' / ' + wiring.number : ''],
              ['可用控制动作', hardware.actions?.join(', ')], ['硬件信息读取失败', sideError(results[0])]]);
          };
        });
    }
    function task(ctx, id) {
      // The history query already established board involvement. A current
      // connection or all-target serial filter would hide old and two-board tasks.
      var url = '/api/task?id=' + encodeURIComponent(id) + (ctx ? '&key=' + encodeURIComponent(ctx.key) : '');
      return request(url).then(function (response) {
        if (response.task?.id !== id) throw new Error(translate('任务响应与请求不匹配'));
        return function () {
          var t = response.task, p = t.progress || {};
          section('任务详情', [['任务编号', t.id], ['请求编号', t.request_id], ['操作', t.kind],
            ['发起者', t.actor], ['状态', known(taskStates, t.state)], ['执行结果', known(outcomes, t.outcome)],
            ['阶段', translate(global.DeckLabTasks.phaseLabel(p.phase) || p.phase || '')], ['进度', p.total > 0 ? p.done + ' / ' + p.total : ''],
            ['提交时间', time(t.created)], ['最后更新', time(t.updated)],
            ['取消请求', t.cancel_requested ? translate('已请求取消') : ''],
            ['错误代码', t.result?.code], ['错误详情', t.error]]);
          var targets = (t.targets || []).map(function (target) {
            return ['目标连接', [target.resolved_serial || target.serial, target.connection_id].filter(Boolean).join(' · ')];
          });
          (t.flash_targets || []).forEach(function (target) {
            targets.push(['刷机目标', [target.target_id, target.generation].filter(Boolean).join(' · ')]);
          });
          section('任务目标', targets);
          var result = t.result || {}, dl = result.download || {}, artifact = result.artifact || {};
          if (t.kind === 'image-download' || t.kind === 'image-prepare') {
            section('镜像详情', [['文件名', artifact.name || dl.name || result.image?.name],
              ['下载连接', known({http:'HTTP', 'verified-https':'HTTPS（验证证书）',
                'organization-https':'组织镜像 HTTPS（允许自签名证书）'}, dl.transport)],
              ['最近的 HTTP 响应', dl.http_status], ['已跟随重定向', dl.redirects],
              ['服务器声明大小', size(dl.declared_bytes)], ['请求预留的缓存', size(dl.cache_reservation_bytes)],
              ['已处理', Number.isFinite(p.done) && p.done > 0 ? size(p.done) : ''],
              ['文件大小', size(artifact.size)], ['SHA-256', artifact.sha256],
              ['散列核对', known({matched:'与提供的 SHA-256 一致', calculated:'已计算文件 SHA-256'}, dl.checksum)]]);
          }
          if (t.kind.startsWith('hardware-')) {
            var control = result.control || {};
            section('控制与板卡证据', [['控制方式', known({command:'外部程序', bmc:'BMC', 'home-assistant':'Home Assistant'}, control.provider || result.provider)],
              ['控制器槽位', control.controller && control.controller + ' / ' + control.slot],
              ['控制器回执', (control.acknowledged || result.control_result === 'acknowledged') ? translate('已确认') : ''],
              ['板卡响应', known({'not-checked':'板卡响应未核对', waiting:'尚未核实刷机 USB 与串口的关联',
                'usb-identity-verified':'已确认刷机 USB 属于当前板卡'}, control.observation)],
              ['核对序列号的任务', result.identity_task_id],
              ['控制耗时', Number.isFinite(result.duration_ms) ? result.duration_ms + ' ms' : '']]);
          }
          if (Array.isArray(p.stages)) section('执行经过', p.stages.map(function (stage) {
            return [translate(global.DeckLabTasks.phaseLabel(stage.phase) || stage.phase), [time(stage.at_ms / 1000), stage.endpoint].filter(Boolean).join(' · ')];
          }));
        };
      });
    }
    function current(state) {
      return active === state && dialog.open && (!state.context || options.isCurrent(state.context));
    }
    function refresh() {
      if (!active) return;
      var state = active, revision = ++state.revision;
      copy.disabled = true; $('diagnostics-refresh').disabled = true; status.textContent = '正在读取诊断…';
      state.load().then(function (render) {
        if (!current(state) || revision !== state.revision) return;
        body.textContent = ''; report = []; render();
        status.textContent = translate('读取时间') + ' ' + new Date().toLocaleString();
        copy.disabled = !report.length;
      }).catch(function (error) {
        if (!current(state) || revision !== state.revision) return;
        status.textContent = translate('读取诊断失败') + ' · ' + error.message;
        copy.disabled = !report.length;
      }).finally(function () {
        if (current(state) && revision === state.revision) $('diagnostics-refresh').disabled = false;
      });
    }
    function open(title, context, load) {
      body.textContent = ''; report = [];
      $('diagnostics-title').textContent = title;
      active = {context:context, load:load, revision:0};
      if (!dialog.open) dialog.showModal();
      refresh();
    }
    $('diagnostics-refresh').addEventListener('click', refresh);
    $('diagnostics-close').addEventListener('click', function () { dialog.close(); });
    copy.addEventListener('click', function () {
      options.copyText([translate($('diagnostics-title').textContent), status.textContent, ...report].join('\n'));
    });
    dialog.addEventListener('close', function () { if (!dialog.open) { active = null; report = []; } });
    return {
      system:function () { open('运行状态', null, system); },
      activation:function (target) { open('配置应用详情', null, function () { return activation(target); }); },
      device:function (ctx) { if (ctx) open('设备诊断', ctx, function () { return device(ctx); }); },
      task:function (ctx, id) { open('任务详情', ctx, function () { return task(ctx, id); }); },
      selectionChanged:function () { if (active?.context && dialog.open) dialog.close(); }
    };
  }
  global.DeckLabDiagnostics = {create:create};
})(window);
