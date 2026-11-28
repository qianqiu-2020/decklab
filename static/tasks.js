// Durable task history and operation watchers survive terminal/device switches.
(function (global) {
  'use strict';
  var states = { queued: '等待执行', running: '执行中', completed: '已完成', failed: '失败',
                 cancelled: '已取消', interrupted: '已中断' };
  var stages = {'checking-probe-settings':'检查探测设置', 'acquiring-console':'获取串口输入权限',
    'checking-shell':'确认 shell 提示符或自动登录', 'sending-probe':'发送探测命令',
    'waiting-probe-response':'等待探测命令完整响应', 'reading-probe-result':'解析探测结果',
    'applying-probe-result':'核对身份并保存结果', 'probe-finished':'探测结束',
    'checking-session-target':'核对当前板卡和会话目标', 'selecting-ssh-address':'选择板卡的 SSH 地址',
    'reading-ssh-address-from-console':'通过串口读取 IP 地址', 'found-ssh-address':'选定 SSH 地址',
    'reusing-ssh-session':'复用已有 SSH 会话', 'reading-ssh-settings':'读取 SSH 连接设置',
    'ssh-credentials-needed':'等待用户提供 SSH 凭据', 'authenticating-ssh-explicit':'使用本次填写的账号验证 SSH',
    'authenticating-ssh-saved':'使用已保存的账号验证 SSH', 'authenticating-ssh-default':'使用默认连接设置验证 SSH',
    'opening-ssh-terminal':'打开 SSH 终端', 'listing-adb-usb':'枚举 ADB 连接（显示登记端口）',
    'matching-adb-board':'从 ADB 回读板卡序列号和启动编号', 'verified-adb-port':'确认 ADB 端口',
    'verified-moved-adb-port':'核实板卡已换到新 USB 端口', 'opening-adb-terminal':'打开 ADB 终端',
    'checking-adbd-over-serial':'通过串口检查板端 ADB 服务',
    'adb-serial-evidence-unavailable':'未取得板端 ADB 诊断证据',
    'adbd-running':'板端 adbd 正在运行', 'adbd-not-running':'板端 adbd 服务未运行',
    'adb-gadget-configured':'板端 USB 设备控制器已完成配置',
    'adb-gadget-detached':'板端 USB 设备控制器未连接',
    'adb-usb-transport-closed':'本次启动最近的 ADB USB 日志显示连接关闭',
    'restarting-adbd-over-serial':'通过串口请求重启板端 adbd',
    'adbd-restart-acknowledged':'板端已接受重启 adbd 请求',
    'adbd-restart-unconfirmed':'串口未确认重启结果，正在回查 USB',
    'waiting-adb-recovery':'等待原板卡的 ADB 连接',
    'session-ready':'终端会话已建立',
    'resolving-image-host':'解析镜像服务器', 'connecting-image-server':'连接镜像服务器',
    'checking-image-tls':'验证服务器证书', 'connecting-organization-tls':'连接组织镜像服务器',
    'waiting-image-response':'等待镜像服务器响应', 'following-image-redirect':'跟随镜像重定向',
    'reserving-image-cache':'预留镜像缓存', 'image-download':'接收镜像文件',
    'sealing-image-cache':'计算 SHA-256 并保存镜像', 'image-ready':'镜像已缓存',
    'checking-image-sha256':'校验镜像', 'expanding-image':'解压镜像', 'checking-payloads':'校验包内文件',
    'reading-payload-metadata':'读取基本信息', 'reclaiming-image-cache':'回收镜像准备缓存',
    'image-preparation-ready':'镜像准备完成',
    'checking-board-control-target':'核对板卡和控制权限', 'preparing-board-controller':'准备控制器',
    'sending-board-control':'交付控制请求', 'board-controller-acknowledged':'收到控制器回执',
    'board-control-finished':'控制请求结束', 'recording-existing-usb-targets':'记录操作前的 USB 设备',
    'checking-usb-identity':'等待新 USB 连接并核对串口关联', 'reading-board-serial':'读取板载序列号',
    'board-usb-identity-verified':'已核对板卡的 USB 和串口'};
  function phaseLabel(phase) { return Object.hasOwn(stages, phase) ? stages[phase] : ''; }
  function active(task) { return task.state === 'queued' || task.state === 'running'; }
  function adbRecoverable(task, ctx) {
    return task?.kind === 'adb-connect' && task.state === 'failed' && !!ctx?.serial
      && task.targets?.some(function (target) { return target.serial === ctx.serial && target.connection_id === ctx.connectionId; })
      && task.progress?.stages?.some(function (step) {
        return step.phase === 'adb-usb-transport-closed' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(step.endpoint);
      });
  }
  function errorMessage(message) {
    if (message === 'artifact quota exceeded') {
      return '控制机的文件缓存已达到额度上限。可从「已下载」复用镜像，或删除不用的镜像缓存后重试。';
    }
    return message;
  }
  function createClient(fetch) {
    function request(url, payload, options) {
      return fetch(url, Object.assign({}, options, payload ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) } : { cache: 'no-store' })).then(function (response) {
        return response.json().then(function (data) {
          if (data.error || data.ok === false || response.ok === false) {
            var error = new Error(errorMessage(data.error) || '任务请求失败'); error.status = response.status;
            error.result = data; throw error;
          }
          return data;
        });
      });
    }
    function watch(task, ctx, onUpdate, stillWatching) {
      return new Promise(function (resolve, reject) {
        var id = task && task.id;
        function dismissed() {
          if (!stillWatching || stillWatching()) return false;
          resolve(null);
          return true;
        }
        function inspect(current) {
          if (!id || !current || current.id !== id || !states[current.state]) {
            var error = new Error('任务响应与请求不匹配'); error.permanent = true; throw error;
          }
          if (onUpdate) {
            try { onUpdate(current); } catch (error) { error.permanent = true; throw error; }
          }
          if (active(current)) return false;
          if (current.state === 'completed') resolve(current.result || { ok: true });
          else {
            var failure = new Error(errorMessage(current.error) || states[current.state] || '任务失败');
            failure.result = current.result; failure.taskId = id; failure.task = current; failure.permanent = true; reject(failure);
          }
          return true;
        }
        function monitor() {
          if (dismissed()) return;
          request('/api/task?id=' + encodeURIComponent(id) + '&key=' + encodeURIComponent(ctx.key)
            + (ctx.serial ? '&serial=' + encodeURIComponent(ctx.serial) : '')).then(function (data) {
            if (!dismissed() && !inspect(data.task)) setTimeout(monitor, 1000);
          }).catch(function (error) {
            if (dismissed()) return;
            if (error.permanent || [401, 403, 404].indexOf(error.status) !== -1) { reject(error); return; }
            // Losing a network response does not stop the collector or justify replay.
            setTimeout(monitor, 2000);
          });
        }
        try { if (!dismissed() && !inspect(task)) monitor(); } catch (error) { reject(error); }
      });
    }
    function start(payload, onUpdate, ctx) {
      // Never retry a start whose response was lost: execution may already exist.
      // Keep credentials out of the watcher that can survive for a long task.
      var watchContext = ctx || { key: payload.key, serial: payload.serial };
      return request('/api/task-start', payload).catch(function (error) {
        // A missing start response cannot tell us whether execution began.
        if (!error.result) error.submissionUnknown = true;
        throw error;
      }).then(function (data) {
        return watch(data.task, watchContext, onUpdate);
      });
    }
    return { request: request, watch: watch, start: start };
  }
  function create(options) {
    var $ = options.byId, client = createClient(options.fetch), request = client.request;
    var selection = '', offset = 0, sequence = 0;
    var names = { 'desktop-input': '桌面输入', identify: '识别设备', 'ssh-enable': '启用 SSH', 'ssh-connect': '建立 SSH', 'file-copy': '板间复制',
                  'board-test': '板卡用例', 'team-plan': 'TitanX 用例', 'adb-command': 'ADB 命令', 'adb-connect': '建立 ADB 终端',
                  'file-upload': '上传文件', 'file-download': '下载文件', 'image-download': '下载刷机镜像', 'image-prepare': '准备刷机镜像',
                  'hardware-power-on': '硬件上电', 'hardware-power-off': '硬件断电',
                  'hardware-reset': '硬件复位', 'hardware-enter-download': '进入下载模式', 'hardware-flash': '外部刷机', flash: '刷机', 'flash-inspect': '刷机预检', 'flash-preview': '预览刷写范围', 'ram-boot': '内存启动' };
    function scope(ctx) { return ctx ? ctx.serial ? 'serial:' + ctx.serial
      : 'connection:' + (ctx.connectionId || ctx.key) : ''; }
    function sameSelection(selected) { return !!selected && scope(options.getView()) === selected; }
    function run(kind, ctx, extra) {
      var refreshed = false;
      var payload = { key: ctx.key, serial: ctx.serial, connection_id: ctx.connectionId,
        kind: kind, actor: 'web' };
      if (extra && Object.prototype.hasOwnProperty.call(extra, 'association')) payload.association = extra.association;
      if (kind === 'adb-connect' && extra?.recover_from) payload.recover_from = extra.recover_from;
      if (kind === 'ssh-connect' && extra) {
        ['ssh_user', 'ssh_password'].forEach(function (name) {
          if (Object.prototype.hasOwnProperty.call(extra, name)) payload[name] = extra[name];
        });
      }
      return client.start(payload, function () {
          if (!refreshed) { refreshed = true; refresh(); }
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
        var reported = task.progress || {};
        if ((task.kind === 'board-test' || task.kind === 'team-plan') && options.openBoardTest) {
          var testResult = document.createElement('button'); testResult.type = 'button'; testResult.textContent = '查看轮次';
          testResult.addEventListener('click', function () { if (sameSelection(selected)) options.openBoardTest(task); });
          row.appendChild(testResult);
        }
        if (options.openAdbReceipt && (['adb-command','ssh-command','serial-command'].includes(task.kind) || task.result?.adb_receipt)) {
          var receipt = document.createElement('button'); receipt.type = 'button'; receipt.textContent = task.kind === 'serial-command' ? '串口回执' : task.kind === 'ssh-command' ? 'SSH 回执' : 'ADB 回执';
          receipt.addEventListener('click', function () { if (sameSelection(selected)) options.openAdbReceipt(task); });
          row.appendChild(receipt);
        }
        if (active(task)) {
          var progress = document.createElement('progress'); progress.className = 'task-progress';
          if (reported.total > 0) {
            progress.max = reported.total;
            progress.value = reported.done || 0;
          }
          progress.setAttribute('aria-label', '任务进度'); main.appendChild(progress);
        }
        var output = document.createElement('code');
        output.textContent = task.error || task.result && (task.result.artifact ? task.result.artifact.name + ' · ' + task.result.artifact.size + ' B'
          : task.result.sid ? '会话 ' + task.result.sid
          : task.result.serial ? 'SN ' + task.result.serial
          : task.result.path ? task.result.path + ' · ' + task.result.size + ' B' : '') || phaseLabel(reported.phase) || reported.phase || '';
        main.appendChild(output); row.appendChild(main);
        if (['flash','flash-inspect','flash-preview','ram-boot'].includes(task.kind) && options.openFlash) {
          var inspect = document.createElement('button'); inspect.type = 'button'; inspect.textContent = '查看刷机';
          inspect.addEventListener('click', function () { if (sameSelection(selected)) options.openFlash(task); });
          row.appendChild(inspect);
        }
        if (active(task)) {
          var cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = task.cancel_requested ? '已请求取消' : '取消';
          cancel.disabled = !!task.cancel_requested;
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
        } else if (options.retryADB && adbRecoverable(task, ctx)) {
          var recover = document.createElement('button'); recover.type = 'button';
          recover.textContent = '重启 ADB 服务并重连';
          recover.addEventListener('click', function () { if (sameSelection(selected)) options.retryADB(task); });
          row.appendChild(recover);
        } else if (task.kind === 'ssh-connect' && task.state === 'failed' && task.result
            && task.result.code === 'ssh-authentication-failed' && options.retrySSH) {
          var retry = document.createElement('button'); retry.type = 'button';
          retry.textContent = '使用该账号重试';
          function matchesConnection() {
            var current = options.getView();
            return sameSelection(selected) && !!current && !!current.connectionId
              && (task.targets || []).some(function (target) {
                return target.connection_id === current.connectionId
                  && (!current.serial || (target.resolved_serial || target.serial) === current.serial);
              });
          }
          retry.disabled = !matchesConnection();
          retry.addEventListener('click', function () { if (matchesConnection()) options.retrySSH(task); });
          row.appendChild(retry);
        } else if (task.state === 'completed' && task.result && task.result.artifact_id) {
          var download = document.createElement('a'); download.className = 'log-download';
          download.textContent = '下载文件'; download.download = task.result.name || '';
          download.href = '/api/artifact-download?id=' + encodeURIComponent(task.result.artifact_id)
            + '&key=' + encodeURIComponent(ctx.key)
            + (ctx.serial ? '&serial=' + encodeURIComponent(ctx.serial) : '');
          download.addEventListener('click', function (event) {
            if (!sameSelection(selected) && event.preventDefault) event.preventDefault();
          });
          row.appendChild(download);
        } else if (task.state === 'completed' && task.result && task.result.sid) {
          var open = document.createElement('button'); open.type = 'button'; open.textContent = '打开会话';
          open.addEventListener('click', function () { if (sameSelection(selected)) options.openSession(task); });
          row.appendChild(open);
        }
        if (options.openDetails) {
          var details = document.createElement('button'); details.type = 'button'; details.textContent = '任务详情';
          details.setAttribute('aria-haspopup', 'dialog');
          details.addEventListener('click', function () {
            if (sameSelection(selected)) options.openDetails(ctx, task.id);
          });
          row.appendChild(details);
        }
        box.appendChild(row);
      });
      if (!tasks.length) box.textContent = '尚无后台任务；任务结果在刷新或重新打开页面后仍可查看。';
      $('task-pager').hidden = offset === 0 && !more;
      $('task-prev').disabled = offset === 0;
      $('task-next').disabled = !more;
      $('task-page').textContent = '第 ' + (offset / 10 + 1) + ' 页';
      $('task-status').textContent = '任务保存在采集器中；刷新页面不会取消。';
    }
    function refresh() {
      var ctx = options.getView(), selected = scope(ctx);
      if (!ctx) return;
      if (selection !== selected) {
        $('task-pager').hidden = true;
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
  function createAdb(options) {
    var $ = options.byId, client = createClient(options.fetch);
    var dialog = $('adb-command-dialog'), form = $('adb-command-form'), status = $('adb-command-status');
    var panel = $('adb-command-receipt'), serialPanel = $('adb-command-serial');
    var recover = $('adb-command-recover'), context = null, task = null, generation = 0;
    var labels = {checking:'正在核对 USB 和板卡身份', exited:'已收到退出回执',
      'auth-failed':'SSH 登录被拒绝，命令未执行', 'timed-out':'等待超时，板端结果未知',
      cancelled:'已停止等待，板端结果未知', unconfirmed:'未收到完整回执，请查看串口',
      rejected:'命令未执行', unavailable:'执行通道暂不可用'};
    var decoder, serialOffset, serialBusy;
    function current(seq) {
      var view = options.getView();
      return seq === generation && dialog.open && view && context && view.serial === context.serial && view.key === context.key;
    }
    function boardQuery() {
      return '&key=' + encodeURIComponent(context.key) + '&serial=' + encodeURIComponent(context.serial);
    }
    function output(value, channel) {
      var text = value[channel] || '';
      if (value[channel + '_truncated_bytes']) text += '\n… 省略 ' + value[channel + '_truncated_bytes'] + ' 字节';
      return text || '（无输出）';
    }
    function showText(id, value) {
      var element = $(id);
      if (element.textContent !== value) element.textContent = value;
    }
    function render(record) {
      task = record;
      var result = task.result || {}, latest = result.serial_receipt || result.ssh_receipt || result.adb_receipt || {};
      panel.hidden = !latest.command;
      showText('adb-command-text', latest.command || '');
      var fromSerial = task.kind === 'serial-command';
      var transport = fromSerial ? '串口' : task.kind === 'ssh-command' ? 'SSH' : 'ADB';
      showText('adb-command-output-title', fromSerial ? '串口输出' : '标准输出');
      $('adb-command-error-output').hidden = fromSerial;
      showText('adb-command-stdout', output(latest, 'stdout'));
      showText('adb-command-stderr', output(latest, 'stderr'));
      $('adb-command-read-serial').disabled = !latest.serial_window || serialBusy;
      recover.hidden = !['adb-command','ssh-command','serial-command'].includes(task.kind) || active(task) || result.cleanup_confirmed !== false
        || !!result.recovery_confirmation || !options.canRecover();
      var receiptState = task.state === 'interrupted' && latest.state === 'running' ? 'unconfirmed' : latest.state;
      status.textContent = [latest.started_at && new Date(latest.started_at * 1000).toLocaleString(),
        latest.round && '第 ' + latest.round + ' 轮启动回执', (task.kind === 'serial-command' && receiptState === 'checking' ? '正在核对串口和板卡身份'
          : receiptState === 'running' ? '等待' + transport + '回执'
          : receiptState === 'disconnected' ? transport + ' 已断开，板端结果未知' : labels[receiptState]) || states[task.state],
        Number.isInteger(latest.exit_code) && '板端退出码 ' + latest.exit_code,
        !Number.isInteger(latest.exit_code) && Number.isInteger(latest.ssh_exit_code ?? latest.adb_exit_code)
          && transport + ' 客户端退出码 ' + (latest.ssh_exit_code ?? latest.adb_exit_code),
        task.error, '任务 ' + task.id].filter(Boolean).join(' · ');
    }
    function show(record, seq) {
      form.hidden = true;
      $('adb-command-original').open = ['adb-command', 'ssh-command', 'serial-command'].includes(record.kind);
      render(record);
      if (active(record)) client.watch(record, context, function (latest) {
        render(latest);
      }, function () { return current(seq); }).catch(function (error) {
        if (current(seq) && active(task)) status.textContent = error.message;
      });
    }
    function open(record, initialCommand) {
      var view = options.getView();
      if (!view || !view.serial || (!view.connectionId && !record)) {
        options.notice('请先连接并识别板卡的串口');
        return;
      }
      context = {key: view.key, serial: view.serial, connectionId: view.connectionId};
      var seq = ++generation;
      task = null; decoder = null; serialOffset = null; serialBusy = false;
      panel.hidden = true; serialPanel.hidden = true; form.hidden = !!record;
      $('adb-command-title').textContent = record?.kind === 'serial-command' ? '串口命令回执' : record?.kind === 'ssh-command' ? 'SSH 命令回执' : 'ADB 命令';
      $('adb-command-board').textContent = options.boardLabel(view) + ' · SN ' + view.serial;
      $('adb-command-input').value = initialCommand || ''; $('adb-command-run').disabled = false;
      $('adb-command-read-serial').textContent = '查看本次串口';
      $('adb-command-serial-output').textContent = ''; status.textContent = '';
      if (!dialog.open) dialog.showModal();
      if (record) show(record, seq);
      else $('adb-command-input').focus();
    }
    dialog.addEventListener('close', function () { if (!dialog.open) ++generation; });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var seq = generation, view = options.getView(), run = $('adb-command-run');
      if (!current(seq) || run.disabled) return;
      if (view.connectionId !== context.connectionId) {
        status.textContent = '串口连接已变化，请重新打开 ADB 命令。';
        return;
      }
      var command = $('adb-command-input').value;
      if (!command.trim()) return;
      var id = Array.from(crypto.getRandomValues(new Uint8Array(16)), function (n) {
        return n.toString(16).padStart(2, '0');
      }).join('');
      run.disabled = true; status.textContent = '正在提交 · 请求 ' + id;
      var lookup = '/api/task?request_id=' + id + boardQuery();
      client.request('/api/task-start', {kind:'adb-command', key:context.key, serial:context.serial,
        connection_id:context.connectionId, client_id:options.clientId, request_id:id,
        command:command, timeout:Number($('adb-command-timeout').value)})
        .catch(function (error) {
          if (error.status > 0 && error.status < 500 && error.status !== 408) throw error;
          return client.request(lookup).catch(function () {
            var unknown = new Error('未确认提交结果。请在任务中查询原请求 ' + id);
            unknown.submissionUnknown = true; throw unknown;
          });
        }).then(function (response) {
          options.refresh();
          if (current(seq)) show(response.task, seq);
        }).catch(function (error) {
          if (!current(seq)) return;
          status.textContent = error.message;
          run.disabled = !!error.submissionUnknown;
        });
    });
    $('adb-command-read-serial').addEventListener('click', function () {
      var seq = generation, button = $('adb-command-read-serial');
      var latest = task && task.result && (task.result.serial_receipt || task.result.ssh_receipt || task.result.adb_receipt);
      var cursor = latest && latest.serial_window, note = $('adb-command-serial-status');
      if (!current(seq) || !cursor || button.disabled || serialBusy) return;
      serialPanel.hidden = false;
      var next = serialOffset === null ? cursor.start_off : serialOffset;
      var log = $('adb-command-serial-output');
      serialBusy = true; button.disabled = true; note.textContent = '正在读取原串口…';
      client.request('/api/serial?off=' + next + '&wait=0&max_bytes=65536&connection_id=' + encodeURIComponent(cursor.connection_id) + boardQuery())
        .then(function (data) {
          if (!current(seq)) return;
          var raw = Uint8Array.from(atob(data.data_b64), function (value) { return value.charCodeAt(0); });
          if (!decoder || data.off - raw.length !== next) decoder = new TextDecoder();
          log.textContent = (log.textContent + decoder.decode(raw, {stream:true})).slice(-65536);
          serialOffset = data.off;
          var gap = data.off - raw.length > next ? '原串口窗口的开头已归档。' : '';
          note.textContent = gap + '串口位置 ' + cursor.start_off + '–' + data.off + (data.alive ? '' : ' · 串口已断开');
          button.textContent = data.more ? '继续读取串口' : '刷新本次串口';
        }).catch(function (error) {
          if (current(seq)) note.textContent = error.status === 404 ? '原串口连接已更换，请查看串口归档。' : error.message;
        }).finally(function () { if (current(seq)) { serialBusy = false; button.disabled = false; } });
    });
    $('adb-command-open-logs').addEventListener('click', function () {
      if (!current(generation)) return;
      dialog.close(); options.openLogs();
    });
    recover.addEventListener('click', function () {
      if (!current(generation) || recover.hidden || recover.disabled) return;
      var reason = global.prompt('确认原板端操作已停止后，填写判断依据。');
      if (!reason || !reason.trim()) return;
      var seq = generation;
      recover.disabled = true;
      client.request('/api/task-recovery', {id:task.id, expected_updated:task.updated,
        process_stopped_confirmed:true, reason:reason.trim()}).then(function (response) {
        options.refresh();
        if (current(seq)) render(response.task);
      }).catch(function (error) { if (current(seq)) status.textContent = error.message; })
        .finally(function () { recover.disabled = false; });
    });
    return {open: open};
  }
  global.DeckLabTasks = { create: create, createClient: createClient, createAdb: createAdb, errorMessage: errorMessage,
    phaseLabel: phaseLabel, adbRecoverable: adbRecoverable };
})(window);
