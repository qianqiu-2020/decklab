// Board-level flash planning and observation; no browser-owned hardware transport.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var board = null, capabilities = null, artifacts = [], plan = null, task = null, discovered = [];
    var sequence = 0, busy = false, uploading = false, requestId = '', cursor = 0;
    var selectionRequestId = '', selectionPlan = null, payloadCandidates = [], inspection = null;
    var eventSequence = 0, eventLines = [], pollTimer = null, submissionUnknown = false;
    var pendingDelete = null;
    var downloadTask = null, downloadTimer = null, downloadRequest = null;
    var renderedBackup = '';
    function status(message) { $('fl-status').textContent = message; }
    function translate(value) { return global.SerialdI18n ? global.SerialdI18n.translate(value) : value; }
    function scope(selected) {
      var value = { actor: 'web' };
      if (selected.target_id) {
        value.target_id = selected.target_id;
        value.target_generation = selected.target_generation || selected.generation;
        if (selected.serial) value.serial = selected.serial;
      } else { value.key = selected.key; value.serial = selected.serial; }
      return value;
    }
    function query(selected) {
      var value = scope(selected);
      delete value.actor;
      return Object.keys(value).map(function (key) { return '&' + key + '=' + encodeURIComponent(value[key]); }).join('');
    }
    function taskQuery(selected, limit) {
      return '/api/tasks?limit=' + limit + '&offset=0'
        + (selected.target_id ? '' : '&target_serial=' + encodeURIComponent(selected.serial)) + query(selected);
    }
    function restoreTaskScope(current, selected) {
      var snapshot = (current.flash_targets || []).find(function (target) {
        return target.target_id === selected.target_id;
      });
      if (!snapshot) return;
      selected.target_generation = snapshot.generation || snapshot.target_generation;
      var serials = current.board_targets || [];
      selected.serial = serials.length === 1 ? (typeof serials[0] === 'string' ? serials[0] : serials[0].serial || '') : '';
      selected.observingTask = true;
    }
    function same(selected, seq) { return board === selected && sequence === seq; }
    function downloadReference(selected, value) {
      try {
        if (!global.sessionStorage) return null;
        var key = 'decklab-image-download:' + (selected.target_id || selected.serial || selected.key);
        if (value === undefined) return global.sessionStorage.getItem(key);
        if (value) global.sessionStorage.setItem(key, value);
        else global.sessionStorage.removeItem(key);
      } catch (_) { /* A private browser may disable storage; backend tasks still persist. */ }
      return null;
    }
    async function resumeDownload(selected, seq) {
      var current = downloadReference(selected);
      if (!current || task && ['queued', 'running'].includes(task.state)) return;
      try {
        var result = await client.request('/api/task?id=' + encodeURIComponent(current));
        if (!same(selected, seq) || task && ['queued', 'running'].includes(task.state)) return;
        if (!result.task || result.task.id !== current || result.task.kind !== 'image-download') {
          downloadReference(selected, null); return;
        }
        downloadTask = result.task;
        busy = true; uploading = true; updateControls();
        watchDownload(selected, seq, downloadTask);
      } catch (_) { if (same(selected, seq)) downloadReference(selected, null); }
    }
    function format(size) { return options.formatBytes(size); }
    function id() {
      if (global.crypto && global.crypto.getRandomValues) {
        var values = new Uint8Array(16); global.crypto.getRandomValues(values);
        return Array.from(values, function (value) { return value.toString(16).padStart(2, '0'); }).join('');
      }
      return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    }
    function text(tag, value, parent) {
      var node = document.createElement(tag); node.textContent = value;
      if (parent) parent.appendChild(node); return node;
    }
    function artifactOptions(select, selected, candidates) {
      select.textContent = '';
      text('option', '选择已准备镜像', select).value = '';
      var seen = Object.create(null);
      artifacts.concat(candidates || []).forEach(function (artifact) {
        if (!artifact || !(artifact.id || artifact.artifact_id)) return;
        var artifactId = artifact.id || artifact.artifact_id;
        if (seen[artifactId]) return;
        seen[artifactId] = true;
        var option = text('option', artifact.name + ' · ' + format(artifact.size), select);
        option.value = artifactId;
        option.dataset.i18nIgnore = '';
      });
      select.value = selected || '';
    }
    function selectPayloadMode(artifact) {
      if (artifact && /\.(itb|ext[234]|f2fs|squashfs|ubifs|dtb)$/i.test(artifact.name || '')
          && !(task && ['queued', 'running'].includes(task.state))) $('fl-mode').value = 'partitions';
    }
    function invalidatePlan() {
      dismissDelete();
      plan = null; requestId = ''; submissionUnknown = false;
      selectionPlan = null; selectionRequestId = '';
      $('fl-preview').textContent = '';
      $('fl-start').disabled = true;
      updateControls();
    }
    function prepareOnly(value) {
      return value && value.ready === false && $('fl-mode').value === 'partitions' && !(value.writes || []).length;
    }
    function fullConfirmation() { return task && task.flash && task.flash.confirmation_mode === 'full'; }
    function preparationPending() {
      // Supported BootROM targets still need an image-provided loader before
      // they expose a live layout. Readiness is separate from support.
      return capabilities
        && (capabilities.can_prepare_from_image === true || capabilities.preparable === true);
    }
    function updateControls() {
      var available = capabilities && capabilities.supported !== false && !(board && board.observingTask);
      var preparable = preparationPending();
      var uploadAvailable = !(board && board.observingTask) && (available || preparable || capabilities && capabilities.can_upload === true);
      var active = task && ['queued', 'running'].indexOf(task.state) !== -1;
      var selecting = active && awaitingSelection(task);
      var nor = norContext();
      $('fl-review').hidden = !$('fl-preview').children.length;
      $('fl-task-details').hidden = !task;
      $('fl-read-nor').disabled = busy || uploading || !!active || submissionUnknown || !!(board && board.observingTask)
        || !(capabilities && capabilities.nor_workspace_supported);
      if (active || busy || uploading) dismissDelete();
      $('fl-delete').disabled = busy || uploading || submissionUnknown || !!active || !!(board && board.observingTask)
        || !board || !artifacts.some(function (item) { return item.id === $('fl-artifact').value; });
      if (selecting) uploadAvailable = true;
      $('fl-device').disabled = busy || submissionUnknown || !!active || uploading;
      $('fl-discover').disabled = busy || submissionUnknown || !!active || uploading;
      ['fl-mode', 'fl-artifact', 'fl-files', 'fl-verify', 'fl-target-storage'].forEach(function (name) {
        $(name).disabled = busy || submissionUnknown || !!active;
      });
      $('fl-mode').disabled = busy || submissionUnknown || !!active || !!preparable && !(capabilities.modes || []).includes('full');
      $('fl-files').disabled = busy || submissionUnknown || !!active && !selecting;
      $('fl-upload').disabled = busy || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-download').disabled = busy || uploading || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-url').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-url-sha256').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-download-cancel').hidden = !downloadTask || !['queued', 'running'].includes(downloadTask.state);
      $('fl-plan').disabled = busy || uploading || !!active || !!(board && board.observingTask)
        || !(available || preparable) || !$('fl-artifact').value;
      $('fl-plan').hidden = !!selecting;
      if (nor && $('fl-mode').value === 'partitions' && !active) $('fl-plan').disabled = true;
      $('fl-plan').textContent = preparable ? '预览刷机准备步骤' : '预览写入范围';
      $('fl-start').disabled = busy || uploading || !!active || !plan || submissionUnknown;
      $('fl-start').hidden = !!active || nor && $('fl-mode').value === 'partitions';
      $('fl-start').textContent = plan && plan.confirm_after_prepare && plan.ready === false ? '准备环境并核对固件范围' : prepareOnly(plan) ? '准备刷机环境并读取布局' : '开始刷写';
      $('fl-cancel').disabled = !active || task.cancel_requested;
      $('fl-cancel').textContent = nor && selecting ? '结束准备并保留备份' : '取消刷机';
      if (nor && selecting && !uploading) setProgress('awaiting-selection', 0, 0, false);
      $('fl-partitions-field').hidden = $('fl-mode').value !== 'partitions' && (!selecting || fullConfirmation());
      $('fl-target-storage-field').hidden = $('fl-mode').value !== 'full';
      $('fl-select').hidden = !selecting;
      $('fl-select').disabled = busy || uploading || !selecting;
      $('fl-select').textContent = fullConfirmation() ? (selectionPlan ? '确认固件范围并刷写' : '预览固件写入与擦除范围') : selectionPlan ? (nor ? '确认分区并刷写' : '确认分区并继续') : '预览分区写入范围';
      $('fl-recover').hidden = !submissionUnknown;
      var recovery = task && task.flash && task.flash.recovery_required === true;
      $('fl-recovery-field').hidden = !recovery;
      $('fl-recovery-reason').disabled = busy || !!active;
      $('fl-release').disabled = busy || !!active || !recovery || !task.flash.attempt_id
        || !$('fl-recovery-reason').value.trim();
      var rows = $('fl-partitions').children;
      Array.from(rows).forEach(function (row) {
        if (row.checkbox) row.checkbox.disabled = busy || uploading || !!active && !selecting || nor && !selecting;
        if (row.artifactSelect) row.artifactSelect.hidden = nor && !row.checkbox.checked;
        if (row.replacementField) row.replacementField.dataset.empty = nor && !row.checkbox.checked ? 'true' : '';
        if (row.artifactSelect) row.artifactSelect.disabled = busy || uploading || !!active && !selecting || nor && (!selecting || !row.checkbox.checked);
      });
    }
    function awaitingSelection(current) {
      return (current.progress || {}).phase === 'awaiting-selection'
        || (current.flash || {}).phase === 'awaiting-selection';
    }
    function norContext() {
      return !!(capabilities && capabilities.nor_workspace_supported || task && task.flash
        && (task.flash.nor_before || task.flash.operation === 'nor-workspace'));
    }
    function renderContent(parent, content, fallback, omitSize) {
      parent.textContent = '';
      if (!content) { text('small', fallback || '尚未读取', parent); return; }
      var labels = {'unknown': content.format === 'compressed' ? '压缩镜像，未解析内容' : '版本未知',
        'not-applicable': '配置内容，无软件版本', 'empty': '空白分区', 'invalid-format': '无法解析内容格式',
        'no-version-files': '未找到版本文件'};
      var version = text('span', content.version || labels[content.version_state] || '版本未知', parent);
      version.className = 'fl-content-title';
      if (content.version) version.dataset.i18nIgnore = '';
      if (content.description) text('small', content.description, parent).dataset.i18nIgnore = '';
      if (content.built_at) text('small', translate('构建时间') + ' · ' + content.built_at, parent).dataset.i18nIgnore = '';
      if (!omitSize && content.size !== undefined) text('small', format(content.size), parent).dataset.i18nIgnore = '';
      renderSystem(parent, content);
      if (content.sha256) {
        var details = document.createElement('details');
        text('summary', '内容 SHA-256', details);
        text('small', content.sha256, details).className = 'fl-hash';
        parent.appendChild(details);
      }
    }
    // OS image facts come from the payload bytes; every value is data, never markup.
    function line(parent, label, value) {
      var node = text('small', translate(label) + ' · ' + value, parent);
      node.dataset.i18nIgnore = ''; return node;
    }
    function systemSummary(content) {
      var fs = content.filesystem || {};
      return [fs.type, fs.label].filter(Boolean).join(' · ');
    }
    function renderSystem(parent, content) {
      if (content.sparse) line(parent, 'Android sparse，展开后', format(content.expanded_size || 0));
      if (content.filesystem) line(parent, '文件系统', systemSummary(content));
      (content.kernels || []).slice(0, 3).forEach(function (kernel) { line(parent, '内核', kernel.release); });
      if (!(content.kernels || []).length && (content.kernel_modules || []).length)
        line(parent, '内核模块', content.kernel_modules.join(', '));
      var boot = content.boot || {};
      if (boot.loader && boot.loader !== content.version) line(parent, '引导程序', boot.loader);
      if ((boot.entries || []).length) line(parent, '引导项', boot.entries.slice(0, 4).join(' ; '));
      var parts = content.partitions || [];
      if (!parts.length) return;
      var details = document.createElement('details');
      text('summary', translate('磁盘分区') + ' · ' + parts.length, details).dataset.i18nIgnore = '';
      parts.forEach(function (part) {
        var inner = part.content || part, facts = [part.name || ('#' + part.index), format(part.size)];
        if (part.filesystem) facts.push(systemSummary(part));
        var found = (part.versions || [])[0] || (inner.version ? {value: inner.version} : null);
        if (found) facts.push(found.value);
        (part.kernels || []).slice(0, 1).forEach(function (kernel) { facts.push('Linux ' + kernel.release); });
        text('small', facts.join(' · '), details).dataset.i18nIgnore = '';
      });
      parent.appendChild(details);
    }
    function renderBackups() {
      var box = $('fl-backups'), details = task && task.flash || {};
      var signature = JSON.stringify([details.nor_before, details.nor_after, board && board.serial]);
      if (box.dataset.signature === signature) return;
      box.dataset.signature = signature; box.textContent = '';
      ['nor_before', 'nor_after'].forEach(function (key) {
        var backup = details[key], artifact = backup && backup.artifact;
        if (!artifact) return;
        var row = document.createElement('div'); row.className = 'fl-backup-row';
        text('span', translate(key === 'nor_before' ? '写前整片备份' : '写后整片回读') + ' · ' + format(artifact.size), row);
        // Only a server-issued artifact id selects a download. Device content
        // and firmware descriptions can never supply a URL or HTML.
        if (typeof artifact.id === 'string' && /^[a-f0-9]{24}$/.test(artifact.id)) {
          var link = text('a', key === 'nor_before' ? '下载备份' : '下载回读', row);
          link.href = '/api/artifact-download?id=' + encodeURIComponent(artifact.id);
          if (board && board.serial) link.href += '&serial=' + encodeURIComponent(board.serial);
          link.download = artifact.name || 'nor.bin';
        }
        box.appendChild(row);
        var hash = document.createElement('details'); text('summary', '整片 SHA-256', hash);
        text('small', artifact.sha256 || '未知', hash).className = 'fl-hash'; box.appendChild(hash);
      });
      box.hidden = !box.children.length;
    }
    function renderReplacementInfo(value) {
      var metadata = value && value.payload_metadata || [];
      Array.from($('fl-partitions').children).forEach(function (row) {
        if (!row.payloadInfo || !row.artifactSelect) return;
        var match = metadata.find(function (item) { return item.storage === row.storageId && item.partition === row.partitionId; });
        if (match && match.content && row.checkbox.checked) renderContent(row.payloadInfo, match.content);
        else row.payloadInfo.textContent = '';
      });
    }
    function setProgress(phase, done, total, active) {
      var names = {'reading-nor': '读取整片 NOR', 'checking-nor-baseline': '检查备份后板上内容是否变化',
        'verifying-nor': '回读整片 NOR 并校验', 'writing': '写入所选分区',
        'preparing-loader': '加载临时刷机器', 'prepare': '准备临时刷机器',
        'awaiting-selection': '备份已完成，等待选择替换内容', 'image-download': '后端下载镜像',
        'image-ready': '镜像已准备', 'uploading-image': '上传替换文件'};
      var label = names[phase] || phase || '尚未开始', bar = $('fl-progress');
      bar.hidden = phase === 'awaiting-selection';
      $('fl-execution').hidden = !phase || phase === 'awaiting-selection';
      done = Math.max(0, Number(done) || 0); total = Math.max(0, Number(total) || 0);
      bar.max = Math.max(1, total);
      if (active && !total && phase !== 'awaiting-selection' && bar.removeAttribute) bar.removeAttribute('value');
      else bar.value = total ? Math.min(done, total) : 0;
      var counts = total ? ((phase === 'prepare' || phase === 'preparing-loader')
        ? done + ' / ' + total + ' ' + translate('步骤') : format(done) + ' / ' + format(total)) : '';
      $('fl-progress-text').textContent = translate(label) + (counts ? ' · ' + counts + ' · ' + Math.floor(Math.min(done, total) / total * 100) + '%' : '');
      bar.setAttribute('aria-valuetext', $('fl-progress-text').textContent);
    }
    function renderPartitions() {
      var box = $('fl-partitions'), previous = {};
      Array.from(box.children).forEach(function (row) { if (row.checkbox && row.artifactSelect)
        previous[row.storageId + '/' + row.partitionId] = {checked: row.checkbox.checked, artifact: row.artifactSelect.value}; });
      box.textContent = '';
      var after = task && task.flash && task.flash.nor_after;
      var existing = after || task && task.flash && task.flash.nor_before;
      $('fl-current-heading').textContent = after ? '写后回读内容' : '板上现有内容';
      ((inspection || capabilities || {}).storages || []).forEach(function (storage) {
        (storage.partitions || []).forEach(function (partition) {
          var row = document.createElement('div'); row.className = 'fl-partition';
          var label = document.createElement('label');
          var checkbox = document.createElement('input'); checkbox.type = 'checkbox';
          label.appendChild(checkbox);
          var storageName = storage.capacity_source === 'decklab-nor-v1' ? 'NOR' : storage.label || storage.name || storage.id;
          var prefix = norContext() && ((inspection || capabilities || {}).storages || []).length === 1 ? '' : storageName + ' / ';
          var name = text('span', prefix + (partition.name || partition.id), label);
          checkbox.setAttribute('aria-label', storageName + ' / ' + (partition.name || partition.id));
          name.dataset.i18nIgnore = '';
          text('small', format(partition.size) + (partition.offset === undefined ? '' : ' · ' + translate('偏移') + ' ' + partition.offset), name).dataset.i18nIgnore = '';
          row.appendChild(label);
          var current = document.createElement('div'); current.className = 'fl-content'; current.dataset.label = translate(after ? '写后回读内容' : '板上现有内容');
          var old = existing && (existing.partitions || []).find(function (part) { return part.name === (partition.name || partition.id); });
          renderContent(current, old && old.content, '尚未读取', true); row.appendChild(current);
          var replacement = document.createElement('div'); replacement.className = 'fl-content'; replacement.dataset.label = translate('替换文件');
          var select = document.createElement('select');
          select.setAttribute('aria-label', translate('替换文件') + ' · ' + (partition.name || partition.id));
          var saved = previous[storage.id + '/' + (partition.id || partition.name)];
          var chosen = saved && artifacts.concat(payloadCandidates).some(function (item) { return (item.id || item.artifact_id) === saved.artifact; }) ? saved.artifact : norContext() ? '' : $('fl-artifact').value;
          artifactOptions(select, chosen, payloadCandidates); replacement.appendChild(select);
          checkbox.checked = !!(saved && saved.checked);
          var payloadInfo = document.createElement('div'); replacement.appendChild(payloadInfo); row.payloadInfo = payloadInfo;
          row.appendChild(replacement); row.checkbox = checkbox; row.artifactSelect = select; row.replacementField = replacement;
          row.storageId = storage.id; row.partitionId = partition.id || partition.name;
          checkbox.addEventListener('change', function () {
            if (checkbox.checked && !select.value && artifacts.length === 1) select.value = artifacts[0].id;
            invalidatePlan(); renderReplacementInfo(null);
          });
          select.addEventListener('change', function () { invalidatePlan(); renderReplacementInfo(null); });
          box.appendChild(row);
        });
      });
      if (!box.children.length) text('p', preparationPending()
        ? (norContext() ? '先读取并备份 NOR，完成后在此查看各分区内容。' : '准备阶段将读取现场布局，完成后在同一任务选择实际分区和载荷。')
        : capabilities && ['target-unavailable', 'probe-protocol-failed', 'download-capability-unavailable'].indexOf(capabilities.reason) >= 0
        ? 'USB 连接尚未就绪，尚未读取存储和分区；请先恢复连接并重新检测。'
        : '执行器尚未提供可靠的现场分区布局，不能指定分区刷写。', box);
    }
    function renderStorages() {
      var select = $('fl-target-storage'), previous = select.value;
      select.textContent = '';
      text('option', '刷机包按包内映射；原始整盘镜像请选择存储', select).value = '';
      ((inspection || capabilities || {}).storages || []).forEach(function (storage) {
        var option = text('option', (storage.label || storage.name || storage.id) + ' · ' + format(storage.size)
          + (storage.capacity_verified === false && storage.capacity_source ? translate('（GPT 声明，容量待核验）') : ''), select);
        option.value = storage.id; option.dataset.i18nIgnore = '';
      });
      select.value = Array.from(select.children).some(function (option) { return option.value === previous; }) ? previous : '';
    }
    function dismissDelete() {
      pendingDelete = null; $('fl-delete-field').hidden = true;
      $('fl-delete-name').textContent = '';
    }
    function beginDelete() {
      if (!board || busy || uploading || submissionUnknown || board.observingTask
          || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var artifact = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (!artifact) return;
      pendingDelete = { selected: board, sequence: sequence, artifact: artifact };
      $('fl-delete-name').textContent = artifact.name + ' · ' + format(artifact.size)
        + ' · SHA-256 ' + artifact.sha256;
      $('fl-delete-field').hidden = false;
    }
    async function confirmDelete() {
      if (!pendingDelete || busy || uploading || submissionUnknown || !same(pendingDelete.selected, pendingDelete.sequence)
          || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var chosen = pendingDelete, selected = chosen.selected, seq = chosen.sequence;
      invalidatePlan(); busy = true; updateControls(); status('正在删除镜像…');
      try {
        var result = await client.request('/api/artifact-delete', Object.assign(scope(selected), { id: chosen.artifact.id }));
        if (!same(selected, seq)) return;
        if (!result.artifact || result.artifact.id !== chosen.artifact.id || result.artifact.state !== 'deleted')
          throw new Error('删除结果未确认，请重新检测镜像列表。');
        artifacts = artifacts.filter(function (item) { return item.id !== chosen.artifact.id; });
        payloadCandidates = payloadCandidates.filter(function (item) { return (item.artifact_id || item.id) !== chosen.artifact.id; });
        if (capabilities) capabilities.artifacts = artifacts.slice();
        artifactOptions($('fl-artifact')); renderPartitions();
        status('镜像已删除：' + chosen.artifact.name);
      } catch (error) {
        if (same(selected, seq)) status(!error.result ? '删除结果未确认，请重新检测镜像列表。'
          : error.message === 'artifact is owned by an active task'
          ? '镜像正在被任务使用，任务结束后才能删除。' : error.message);
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function selections() {
      return Array.from($('fl-partitions').children).filter(function (row) {
        return row.checkbox && row.checkbox.checked;
      }).map(function (row) {
        var selected = { storage: row.storageId, partition: row.partitionId,
          artifact_id: row.artifactSelect.value };
        return selected;
      });
    }
    function location(value) {
      if (!value) return '未配置';
      if (typeof value === 'string') return value;
      return [value.provider || value.adapter || value.name, value.node, value.endpoint].filter(Boolean).join(' · ') || '未配置';
    }
    function capabilityReason() {
      var messages = {
        'target-unavailable': '无法打开 USB 下载连接。请查看连接诊断，检查数据线、权限或其他刷机程序占用后重新检测。',
        'probe-protocol-failed': 'USB 通信失败，尚未完成设备识别。请检查数据线或 Hub，重新进入下载模式后检测。',
        'download-capability-unavailable': '设备尚未提供下载能力，无法确认下载模式。请重新进入下载模式后检测。',
        'connection-generation-unavailable': '无法确认当前 USB 连接，请重新检测。',
        'layout-discovery-unavailable': '当前下载环境没有提供可识别的存储布局。',
        'live-layout-invalid': '现场存储布局校验失败，不能生成写入计划。',
        'live-gpt-unavailable': '尚未读取到有效的现场 GPT，不能指定分区刷写。'
      };
      return messages[capabilities && capabilities.reason] || capabilities && capabilities.reason || '此设备尚未配置刷机执行器。';
    }
    function renderCapabilities() {
      var box = $('fl-connections'); box.textContent = '';
      [['板型', capabilities.profile || (capabilities.mode === 'unknown' ? '尚未识别' : '未配置')], ['复位控制', '本次不执行复位'],
       ['串口观察', location(capabilities.console)], ['刷写位置', location(capabilities.executor)]].forEach(function (item) {
        text('dt', item[0], box); text('dd', item[1], box);
      });
      if (capabilities.diagnostic) {
        text('dt', '连接诊断', box);
        text('dd', capabilities.diagnostic, box).dataset.i18nIgnore = '';
      }
      var storages = capabilities.storages || [];
      var unverifiedCapacity = storages.some(function (storage) {
        return storage.capacity_verified === false && ['gpt-header', 'primary-gpt-header'].indexOf(storage.capacity_source) >= 0;
      });
      if (unverifiedCapacity) {
        text('dt', '存储核对', box); text('dd', '已读取现场主 GPT；存储容量尚未独立核验。', box);
      }
      var requiredSupported = capabilities.nor_workspace_supported || (!(capabilities.verification && capabilities.verification.required === false)
        && capabilities.readback_supported !== false
        && !storages.some(function (storage) { return storage.verify === false; }));
      Array.from($('fl-verify').children).forEach(function (option) {
        if (option.value === 'required') option.disabled = !requiredSupported;
      });
      if (!requiredSupported) {
        text('dt', '回读能力', box); text('dd', capabilities.supported === false || preparationPending()
          ? '尚未取得设备能力，回读支持待确认。' : '当前下载环境不支持写后回读校验。', box);
        $('fl-verify').value = 'if-supported';
      }
      artifacts = (capabilities.artifacts || []).slice(); artifactOptions($('fl-artifact'));
      resumeDownload(board, sequence);
      renderPartitions(); renderStorages();
      var modes = preparationPending() ? capabilities.modes || ['partitions'] : capabilities.modes || ['full', 'partitions'];
      Array.from($('fl-mode').children).forEach(function (option) { option.disabled = modes.indexOf(option.value) < 0; });
      $('fl-mode').value = preparationPending() ? 'partitions' : modes.indexOf('full') >= 0 ? 'full' : modes[0] || 'full';
      if (capabilities.nor_workspace_supported) { $('fl-mode').value = 'partitions'; $('fl-verify').value = 'required'; $('fl-sources').open = false; }
      status(capabilities.nor_workspace_supported ? ''
        : preparationPending() ? '已检测到 BootROM；上传刷机包后可准备临时环境并读取布局，此阶段不写存储。'
        : capabilities.supported === false ? capabilityReason()
        : '选择镜像并预览写入范围；预览不会复位或写入存储。');
      updateControls();
    }
    function targetName(target) {
      var ports = target.usb && target.usb.port_path || target.device && target.device.port_path;
      var attachment = target.usb_location || (Array.isArray(ports) ? 'USB ' + ports.join('.') : '');
      return [target.label || target.product || translate('下载模式设备'), target.serial ? 'SN ' + target.serial : translate('序列号待识别'),
        target.mode === 'unknown' ? translate('模式待确认') : target.mode,
        attachment, target.present === false ? translate('设备未连接，原任务待处理') : ''].filter(Boolean).join(' · ');
    }
    function renderDiscovered() {
      var select = $('fl-device'); select.textContent = '';
      text('option', '选择下载模式设备', select).value = '';
      discovered.forEach(function (target) {
        var option = text('option', targetName(target), select);
        option.value = target.target_id || target.id; option.dataset.i18nIgnore = '';
      });
      if (board && board.target_id && !discovered.some(function (target) { return (target.target_id || target.id) === board.target_id; })) {
        var current = text('option', targetName(board), select); current.value = board.target_id; current.dataset.i18nIgnore = '';
      }
      select.value = board && board.target_id || '';
    }
    async function loadHistory(selected, seq, existing) {
      if (existing) { observe(existing, selected, seq); return true; }
      var records = await client.request(taskQuery(selected, 20));
      if (!same(selected, seq)) return true;
      var current = (records.tasks || []).find(function (entry) {
        return (entry.kind === 'flash' || entry.kind === 'flash-inspect') && (['queued', 'running'].indexOf(entry.state) >= 0
          || entry.flash && entry.flash.recovery_required === true);
      });
      if (current) { observe(current, selected, seq); return true; }
      return false;
    }
    async function selectTarget(target) {
      if (!target || busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var selected = { target_id: target.target_id || target.id,
        target_generation: target.target_generation || target.generation, label: target.label, serial: target.serial || '',
        present: target.present };
      board = selected; var seq = ++sequence; clearTimeout(pollTimer); ++eventSequence;
      busy = true; capabilities = null; task = null; inspection = null; artifacts = []; payloadCandidates = [];
      renderedBackup = ''; $('flash-dialog').dataset.taskState = ''; renderBackups(); setProgress('', 0, 0, false);
      invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      $('fl-task-id').textContent = ''; $('fl-events').textContent = ''; $('fl-results').textContent = '';
      $('fl-event-status').textContent = ''; cursor = 0; eventLines = [];
      $('fl-board').textContent = targetName(target); $('fl-device').value = selected.target_id;
      updateControls(); status('正在读取下载设备身份和刷机能力…');
      try {
        // Recover a running task before any USB probe tries to acquire its
        // endpoint. A refreshed BootROM task keeps its initial generation.
        if (await loadHistory(selected, seq)) return;
        if (selected.present === false) {
          status('设备未连接，原任务待处理'); return;
        }
        var result = await client.request('/api/flash-target', scope(selected));
        if (!same(selected, seq)) return;
        var resolved = result.target || target;
        selected.serial = resolved.serial || '';
        selected.target_generation = resolved.target_generation || resolved.generation || selected.target_generation;
        selected.label = resolved.label || selected.label;
        capabilities = result.capabilities || result; renderCapabilities();
        $('fl-board').textContent = targetName(resolved);
      } catch (error) {
        if (same(selected, seq) && error.status === 423) {
          try { if (await loadHistory(selected, seq)) return; } catch (_) { /* Keep the original endpoint error. */ }
        }
        if (same(selected, seq)) status(error.message);
      }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function discover() {
      if (busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var selected = board, seq = sequence;
      busy = true; capabilities = null; artifacts = []; inspection = null; payloadCandidates = [];
      invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      artifactOptions($('fl-artifact')); renderStorages();
      $('fl-board').textContent = translate('自动检测本机 USB 下载设备');
      updateControls(); status('正在检测下载模式设备…');
      try {
        var result;
        try { result = await client.request('/api/flash-discover'); }
        catch (error) {
          if (error.result && Array.isArray(error.result.targets) && error.result.targets.length) result = error.result;
          else throw error;
        }
        if (!same(selected, seq)) return;
        discovered = result.targets || result.devices || [];
        if (!Array.isArray(discovered)) throw new Error('下载设备列表格式无效。');
        renderDiscovered(); $('fl-device').value = '';
        if (!discovered.length) {
          status(result.reason || '未检测到下载模式设备，请检查 USB 数据线和下载模式。');
        } else if (discovered.length > 1) {
          status('检测到多个下载设备，请按 USB 位置选择本次刷写目标。');
        }
        busy = false; updateControls();
        // Only a single detected USB target can be probed automatically.
        // Probing reads identity/capabilities; no reset, loader or storage write.
        if (discovered.length === 1) await selectTarget(discovered[0]);
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function renderPlan(value) {
      renderReplacementInfo(value);
      var box = $('fl-preview'); box.textContent = '';
      if ((value.ram_loaders || []).length) {
        text('strong', '临时刷机环境（仅内存）', box);
        (value.ram_loaders || []).forEach(function (loader) {
          text('p', loader.name + ' · ' + format(loader.size) + ' · SHA-256 ' + loader.sha256, box).dataset.i18nIgnore = '';
        });
      }
      if (prepareOnly(value)) text('p', '准备阶段仅加载临时环境并读取现场布局，不写入存储。', box);
      else if (value.confirm_after_prepare && value.ready === false) text('p', '先加载临时环境并核对现场容量；完成后还需确认固件写入与擦除范围，才会写入存储。', box);
      else if (value.ready === false) text('p', '开始后先加载临时环境并核对现场，检查通过后按清单写入。', box);
      if (value.write_layout) text('p', '本计划将修改分区布局。', box);
      var table = document.createElement('table');
      var heading = document.createElement('tr');
      var showRanges = value.confirm_after_prepare === true || !!(value.erase_ranges || []).length;
      var compact = norContext() && new Set((value.writes || []).map(function (write) {
        return write.storage || write.storage_id;
      })).size === 1;
      table.dataset.compact = String(compact);
      var columns = compact ? ['分区', '写入字节'] : ['存储', '分区或范围', '写入字节'];
      if (showRanges) columns.push('偏移 / 容量');
      if (!compact) columns.push('写入范围 SHA-256');
      columns.forEach(function (label) { text('th', label, heading); });
      table.appendChild(heading);
      (value.writes || []).forEach(function (write) {
        var row = document.createElement('tr');
        var cells = [write.partition || write.target || write.partition_id || '完整磁盘',
          format(write.write_size || write.size || write.bytes || 0)];
        if (!compact) cells.unshift(write.storage || write.storage_id || '未知');
        if (showRanges) cells.push(write.kind === 'layout' ? translate('MTD 映射 / env 擦除')
          : (write.offset === undefined ? '未知' : write.offset) + ' / ' + format(write.capacity || 0));
        if (!compact) cells.push(write.write_sha256 || write.sha256 || '未知');
        cells.forEach(function (item) { text('td', item, row); });
        table.appendChild(row);
        var found = !norContext() && (value.payload_metadata || []).find(function (item) {
          return item.content && item.storage === (write.storage || write.storage_id)
            && (item.partition || '') === (write.partition || '') && (!item.sha256 || item.sha256 === write.sha256);
        });
        if (found) {
          var infoRow = document.createElement('tr'), cell = document.createElement('td');
          cell.setAttribute('colspan', String(columns.length)); cell.className = 'fl-content fl-write-content';
          renderContent(cell, Object.assign({}, found.content, {sha256: ''}), '', true); infoRow.appendChild(cell); table.appendChild(infoRow);
        }
        if (write.write_size > write.size) {
          var paddingRow = document.createElement('tr');
          var padding = text('td', '载荷 ' + write.size + ' 字节，扇区尾部补零 ' + (write.write_size - write.size) + ' 字节', paddingRow);
          padding.setAttribute('colspan', String(columns.length)); table.appendChild(paddingRow);
        }
        if (!compact && write.write_sha256 && write.sha256 && write.write_sha256 !== write.sha256) {
          var sourceRow = document.createElement('tr');
          text('td', '载荷 SHA-256: ' + write.sha256, sourceRow).setAttribute('colspan', String(columns.length));
          table.appendChild(sourceRow);
        }
      });
      box.appendChild(table);
      var digest = document.createElement('details');
      digest.className = 'fl-details'; text('summary', '计划摘要', digest);
      text('p', value.digest || value.sha256 || '未知', digest).dataset.i18nIgnore = '';
      if (compact) (value.writes || []).forEach(function (write) {
        text('p', (write.partition || write.target) + ' · ' + translate('写入范围 SHA-256'), digest).dataset.i18nIgnore = '';
        text('small', write.write_sha256 || write.sha256 || '未知', digest).className = 'fl-hash';
        if (write.write_sha256 && write.sha256 && write.write_sha256 !== write.sha256)
          text('p', '载荷 SHA-256: ' + write.sha256, digest).dataset.i18nIgnore = '';
      });
      box.appendChild(digest);
      (value.erase_ranges || []).forEach(function (range) { text('p', '擦除风险范围（保守上限） · ' + range.storage + ' · ' + range.offset + ' + ' + format(range.size), box).dataset.i18nIgnore = ''; });
      (value.warnings || []).forEach(function (warning) { text('p', warning, box); });
      (value.pending_checks || []).forEach(function (check) { text('p', check, box); });
    }
    async function makePlan() {
      if (busy || !board || !$('fl-artifact').value) return;
      var selected = board, seq = sequence;
      var preparing = preparationPending(), chosen = preparing ? [] : selections();
      if (chosen.some(function (item) { return !item.artifact_id; })) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      invalidatePlan(); busy = true; updateControls(); status('正在核对镜像和现场布局…');
      try {
        var payload = Object.assign(scope(selected), {
          artifact_id: $('fl-artifact').value, mode: $('fl-mode').value, selections: chosen,
          binding_revision: capabilities.binding_revision, verify: $('fl-verify').value });
        if (payload.mode === 'full' && $('fl-target-storage').value) payload.target_storage = $('fl-target-storage').value;
        var result = await client.request('/api/flash-plan', payload);
        if (!same(selected, seq)) return;
        var value = result.plan || result;
        if (result.requires_prepare || value.requires_prepare) value.ready = false;
        if (preparing && payload.mode === 'partitions' && (value.ready !== false || !Array.isArray(value.writes) || value.writes.length
            || value.write_layout !== false)) throw new Error('刷机准备计划包含存储写入，不能执行。');
        if (result.inspection && Array.isArray(result.inspection.storages)) {
          inspection = result.inspection; renderPartitions(); renderStorages();
        }
        if (!(value.id || value.plan_id || result.plan_id)
            || !prepareOnly(value) && (!Array.isArray(value.writes) || !value.writes.length))
          throw new Error('刷机计划缺少写入范围，不能执行。');
        plan = value; plan.id = value.id || value.plan_id || result.plan_id; requestId = id();
        renderPlan(value); status(prepareOnly(value) ? '需要先准备临时刷机环境，读取布局后再选择实际写入范围。'
          : '计划已准备，请核对板卡和全部写入范围后开始。');
      } catch (error) {
        if (same(selected, seq)) {
          status(error.message);
          if (error.result && error.result.recovery_required && error.result.task_id) {
            try {
              var retained = await client.request('/api/task?id=' + encodeURIComponent(error.result.task_id) + query(selected));
              if (same(selected, seq)) observe(retained.task, selected, seq);
            } catch (readError) { if (same(selected, seq)) status(readError.message); }
          }
        }
      }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function outcome(value) {
      if (value === undefined || value === null) return '未执行';
      var names = { 'not-started': '未开始', 'not-completed': '未完成', pending: '等待执行',
        writing: '正在写入', written: '写入完成', completed: '已完成', verified: '校验通过',
        unsupported: '不支持回读校验', 'not-supported': '不支持回读校验', skipped: '已跳过',
        'not-requested': '未请求', unknown: '结果待确认', failed: '失败', 'partial-or-unknown': '可能部分写入，结果待确认',
        'unknown-or-partial': '写入结果未知或可能部分完成' };
      if (typeof value === 'string') return names[value] || value;
      return value.message || names[value.status] || value.status || JSON.stringify(value);
    }
    function renderTask(current) {
      task = current; $('flash-dialog').dataset.taskState = current.state;
      var p = current.progress || {}, result = current.result || {}, details = current.flash || {};
      if (details.operation === 'nor-workspace') { $('fl-mode').value = 'partitions'; $('fl-verify').value = 'required'; }
      renderBackups();
      var backupSignature = [details.nor_before && details.nor_before.artifact && details.nor_before.artifact.id,
        details.nor_after && details.nor_after.artifact && details.nor_after.artifact.id].join('/');
      if (renderedBackup !== backupSignature) {
        renderedBackup = backupSignature; renderPartitions();
        if (details.nor_before && !details.nor_after && !artifacts.length) $('fl-sources').open = true;
      }
      if (awaitingSelection(current) && fullConfirmation() && details.prepared_plan && !selectionPlan) renderPlan(details.prepared_plan);
      if (awaitingSelection(current) && details.inspection && details.inspection !== inspection) {
        var changed = !inspection || (inspection.layout_digest || '') !== (details.inspection.layout_digest || '');
        inspection = details.inspection;
        payloadCandidates = Array.isArray(details.payload_candidates) ? details.payload_candidates : [];
        if (changed) { selectionRequestId = ''; selectionPlan = null; renderPartitions(); renderStorages(); }
      }
      if (!uploading && !(downloadTask && ['queued', 'running'].includes(downloadTask.state)))
        setProgress(p.phase, p.done, p.total, ['queued', 'running'].includes(current.state));
      $('fl-task-id').textContent = current.id + ' · ' + current.state + (p.phase ? ' · ' + p.phase : '');
      var box = $('fl-results'); box.textContent = '';
      [['写入结果', result.write_result || details.write_result], ['校验结果', result.verify_result || details.verify_result],
       ['启动结果', result.boot_result || details.boot_result]].forEach(function (item) {
        var state = typeof item[1] === 'string' ? item[1] : item[1] && item[1].status;
        if (!item[1] || ['not-requested', 'not-started'].includes(state)) return;
        text('dt', item[0], box); text('dd', outcome(item[1]), box).dataset.result = typeof item[1] === 'string' ? item[1] : '';
      });
      if (box.children.length && !awaitingSelection(current)) $('fl-execution').hidden = false;
      box = $('fl-verification'); box.textContent = '';
      var evidence = result.verification_scope || details.verification_scope;
      if (evidence === 'payloads-and-outside-selected-partitions') {
        text('dt', '校验范围', box); text('dd', '写入文件与所选分区之外的全部内容', box);
        text('dt', '回读 / 比较', box);
        text('dd', format(result.readback_bytes || details.readback_bytes || 0) + ' / '
          + format(result.verified_bytes || details.verified_bytes || 0), box).dataset.i18nIgnore = '';
      }
      if (!uploading) status(current.error || (current.cancel_requested
        ? '已请求取消；等待执行器确认停止，可能已部分写入。'
        : awaitingSelection(current) && details.selection_deadline
          ? translate('选区截止') + ' · ' + new Date(details.selection_deadline * 1000).toLocaleTimeString()
          : board && board.observingTask && !['queued', 'running'].includes(current.state)
            ? '新刷写请先重新检测设备。' : ''));
      updateControls();
    }
    async function loadEvents(selected, seq, current) {
      var eventSeq = ++eventSequence;
      try {
        var result = await client.request('/api/flash-events?id=' + encodeURIComponent(current.id)
          + '&after=' + cursor + query(selected));
        if (!same(selected, seq) || !task || task.id !== current.id || eventSeq !== eventSequence) return;
        if (result.truncated_before && cursor < result.truncated_before) eventLines.push('较早的事件已超出保留范围。');
        $('fl-event-status').textContent = '';
        (result.events || []).forEach(function (event) {
          eventLines.push([event.seq, event.phase || event.type, event.message,
            event.done !== undefined ? event.done + ' / ' + event.total : ''].filter(function (part) { return part !== undefined && part !== ''; }).join(' · '));
        });
        eventLines = eventLines.slice(-300); cursor = result.next_after === undefined ? cursor : result.next_after;
        $('fl-events').textContent = eventLines.join('\n');
      } catch (error) {
        if (same(selected, seq)) $('fl-event-status').textContent = '事件读取失败，将继续查询：' + error.message;
      }
    }
    function observe(current, selected, seq) {
      if (!same(selected, seq)) return;
      restoreTaskScope(current, selected);
      renderTask(current); loadEvents(selected, seq, current);
      clearTimeout(pollTimer);
      if (['queued', 'running'].indexOf(current.state) < 0) return;
      pollTimer = setTimeout(async function poll() {
        try {
          var result = await client.request('/api/task?id=' + encodeURIComponent(current.id) + query(selected));
          if (same(selected, seq)) observe(result.task, selected, seq);
        } catch (error) {
          if (!same(selected, seq)) return;
          status('任务查询失败：' + error.message);
          if ([401, 403, 404].indexOf(error.status) < 0) pollTimer = setTimeout(poll, 2000);
        }
      }, 1000);
    }
    async function readNOR() {
      if (!board || busy || uploading || submissionUnknown || task && ['queued', 'running'].includes(task.state)
          || !capabilities || !capabilities.nor_workspace_supported) return;
      var selected = board, seq = sequence;
      invalidatePlan(); requestId = id(); busy = true; updateControls();
      status('正在提交 NOR 读取与备份任务…');
      try {
        var result = await client.request('/api/task-start', Object.assign(scope(selected), {
          kind: 'nor-backup', binding_revision: capabilities.binding_revision, request_id: requestId }));
        if (!same(selected, seq)) return;
        if (!result.task || !result.task.id) throw new Error('缺少备份任务回执');
        observe(result.task, selected, seq);
      } catch (error) {
        if (!same(selected, seq)) return;
        if (!error.result || error.status >= 500) {
          submissionUnknown = true; status('提交结果未确认；请查询原任务，勿重新刷写。');
        } else status(error.message);
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function start() {
      if (busy || !board || !plan || submissionUnknown) return;
      var selected = board, seq = sequence, fixed = plan;
      busy = true; updateControls(); status('正在提交刷机任务…');
      try {
        var result = await client.request('/api/task-start', Object.assign(scope(selected), {
          kind: 'flash', plan_id: fixed.id, binding_revision: fixed.binding_revision === undefined
            ? capabilities.binding_revision : fixed.binding_revision, request_id: requestId }));
        if (same(selected, seq)) { plan = null; observe(result.task, selected, seq); }
      } catch (error) {
        if (!same(selected, seq)) return;
        if (!error.result || error.status >= 500) {
          submissionUnknown = true;
          status('提交结果未确认；请查询原任务，勿重新刷写。');
        } else status(error.message);
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function recover() {
      if (!board || busy) return;
      var selected = board, seq = sequence;
      try {
        var result = await client.request(taskQuery(selected, 100));
        if (!same(selected, seq)) return;
        var current = (result.tasks || []).find(function (entry) {
          return entry.kind === 'flash' && (!requestId || entry.flash && entry.flash.request_id === requestId);
        });
        if (current) { submissionUnknown = false; plan = null; observe(current, selected, seq); }
        else status('未找到原刷机任务；请在任务记录中核实，勿直接重新提交。');
      } catch (error) { if (same(selected, seq)) status(error.message); }
      updateControls();
    }
    async function submitSelection() {
      if (busy || !task || !awaitingSelection(task) || !inspection) return;
      var chosen = fullConfirmation() ? [] : selections();
      if (!fullConfirmation() && (!chosen.length || chosen.some(function (entry) { return !entry.artifact_id; }))) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      var selected = board, seq = sequence, current = task;
      busy = true; updateControls(); status('正在提交分区选择…');
      try {
        var payload = Object.assign(scope(selected), { id: current.id,
          layout_digest: inspection.layout_digest || current.flash.layout_digest });
        if (!selectionPlan) { payload.selections = chosen; payload.preview = true; }
        else { payload.plan_id = selectionPlan.id || selectionPlan.plan_id;
          payload.request_id = selectionRequestId || (selectionRequestId = id()); }
        var result = await client.request('/api/flash-selection', payload);
        if (!same(selected, seq)) return;
        if (result.plan) {
          if (!(result.plan.id || result.plan.plan_id) || !Array.isArray(result.plan.writes) || !result.plan.writes.length)
            throw new Error('刷机计划缺少写入范围，不能执行。');
          selectionPlan = result.plan; renderPlan(selectionPlan);
          status(norContext() ? '' : '计划已准备，请核对板卡和全部写入范围后开始。');
        } else observe(result.task, selected, seq);
      } catch (error) {
        if (same(selected, seq)) status(error.result ? error.message : '选择提交结果未确认；正在查询原任务，请勿创建新任务。');
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function releaseRecovery() {
      if (busy || !board || !task || !task.flash || !task.flash.recovery_required
          || !task.flash.attempt_id || ['queued', 'running'].indexOf(task.state) >= 0) return;
      var reason = $('fl-recovery-reason').value.trim();
      if (!reason) { status('请填写确认执行器已停止的依据。'); return; }
      var selected = board, seq = sequence, current = task;
      busy = true; updateControls(); status('正在核对并释放恢复占用…');
      try {
        var result = await client.request('/api/flash-recovery', Object.assign(scope(selected), {
          id: current.id, attempt_id: current.flash.attempt_id, reason: reason, confirm_stopped: true }));
        if (!same(selected, seq)) return;
        if (!result.task) result = await client.request('/api/task?id=' + encodeURIComponent(current.id) + query(selected));
        if (!same(selected, seq)) return;
        if (!result.task || result.task.id !== current.id || !result.task.flash || result.task.flash.recovery_required !== false)
          throw new Error('恢复占用尚未确认释放，请重新查询原任务。');
        observe(result.task, selected, seq);
        status('已释放恢复占用；这不表示写入成功，原任务结果保持不变。');
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function watchDownload(selected, seq, current) {
      try {
        var result = await client.request('/api/task?id=' + encodeURIComponent(current.id));
        if (!same(selected, seq) || !downloadTask || downloadTask.id !== current.id) return;
        if (!result.task || result.task.id !== current.id) throw new Error('下载任务响应与请求不匹配');
        downloadTask = result.task;
        var progress = downloadTask.progress || {};
        setProgress('image-download', progress.done, progress.total, ['queued', 'running'].includes(downloadTask.state));
        status('后端下载镜像：' + format(progress.done || 0) + (progress.total ? ' / ' + format(progress.total) : ''));
        if (['queued', 'running'].includes(downloadTask.state)) {
          downloadTimer = setTimeout(function () { watchDownload(selected, seq, downloadTask); }, 700);
          return;
        }
        uploading = false; busy = false; updateControls();
        downloadReference(selected, null); downloadRequest = null;
        if (downloadTask.state !== 'completed') throw new Error(downloadTask.error || '镜像下载未完成');
        var artifact = (downloadTask.result || {}).artifact;
        if (!artifact || artifact.state !== 'ready') throw new Error('镜像尚未准备好');
        artifacts = artifacts.filter(function (item) { return item.id !== artifact.id; }).concat([artifact]);
        selectPayloadMode(artifact);
        artifactOptions($('fl-artifact'), artifact.id); renderPartitions(); invalidatePlan();
        if (norContext()) $('fl-sources').open = false;
        downloadRequest = null;
        status('镜像已下载并校验 SHA-256；请选择写入范围并预览。');
      } catch (error) {
        if (!same(selected, seq)) return;
        if (downloadTask && ['queued', 'running'].includes(downloadTask.state)) {
          status('下载任务仍在后端运行，正在重新查询原任务…');
          downloadTimer = setTimeout(function () { watchDownload(selected, seq, downloadTask); }, 1500);
        } else { busy = false; uploading = false; updateControls(); status(error.message); }
      }
    }
    async function downloadImage() {
      if (!board || busy || uploading) return;
      var selected = board, seq = sequence, url = $('fl-url').value.trim(), sha = $('fl-url-sha256').value.trim().toLowerCase();
      if (!url) { status('请输入镜像 URL。'); return; }
      var identity = url + '\n' + sha + '\n' + JSON.stringify(scope(selected));
      if (!downloadRequest || downloadRequest.identity !== identity) downloadRequest = { identity: identity, id: id() };
      busy = true; uploading = true; invalidatePlan(); updateControls(); status('正在提交后端下载任务…');
      try {
        var payload = Object.assign(scope(selected), {
          url: url, sha256: sha, request_id: downloadRequest.id, client_id: options.clientId || '' });
        if (task && awaitingSelection(task) && selected.target_id) payload.task_id = task.id;
        var result = await client.request('/api/image-download', payload);
        if (!same(selected, seq)) return;
        downloadTask = result.task; downloadReference(selected, downloadTask.id); updateControls();
        watchDownload(selected, seq, downloadTask);
      } catch (error) {
        if (same(selected, seq)) { busy = false; uploading = false; updateControls(); status(error.message + '；重试将查询原请求。'); }
      }
    }
    async function upload() {
      if (busy || !board) return;
      var files = Array.from($('fl-files').files || []);
      if (!files.length) { status('请选择要上传的镜像文件。'); return; }
      var selected = board, seq = sequence;
      busy = true; uploading = true; updateControls();
      try {
        for (var file of files) {
          var artifact = null, sealed = false;
          try {
            var staging = Object.assign(scope(selected), { name: file.name, size: file.size });
            if (task && ['queued', 'running'].indexOf(task.state) >= 0 && awaitingSelection(task)) staging.task_id = task.id;
            artifact = (await client.request('/api/artifact-create', staging)).artifact;
            for (var offset = 0; offset < file.size;) {
              var chunk = new Uint8Array(await file.slice(offset, offset + 1024 * 1024).arrayBuffer());
              if (!chunk.length) throw new Error('本地文件读取失败');
              var raw = '';
              for (var i = 0; i < chunk.length; i++) raw += String.fromCharCode(chunk[i]);
              var appended = (await client.request('/api/artifact-append', Object.assign(scope(selected), {
                id: artifact.id, offset: offset, data_b64: btoa(raw) }))).artifact;
              offset += chunk.length;
              if (appended.received !== offset) throw new Error('暂存偏移不匹配');
              if (same(selected, seq)) {
                setProgress('uploading-image', offset, file.size, true);
                status('正在上传镜像，尚未写入开发板 · ' + file.name + ' · ' + format(offset) + ' / ' + format(file.size));
              }
            }
            var completed = (await client.request('/api/artifact-seal', Object.assign(scope(selected), { id: artifact.id }))).artifact;
            if (completed.size !== file.size || !/^[a-f0-9]{64}$/.test(completed.sha256)) throw new Error('暂存文件校验失败');
            sealed = true;
            if (same(selected, seq)) artifacts.push(completed);
          } finally {
            if (artifact && !sealed) await client.request('/api/artifact-delete', Object.assign(scope(selected), { id: artifact.id })).catch(function () {});
          }
        }
        if (same(selected, seq)) {
          if (files.length === 1) selectPayloadMode(artifacts[artifacts.length - 1]);
          invalidatePlan(); artifactOptions($('fl-artifact'), artifacts[artifacts.length - 1].id); renderPartitions();
          if (norContext()) $('fl-sources').open = false;
          status(task && ['queued', 'running'].indexOf(task.state) >= 0 && awaitingSelection(task)
            ? (norContext() ? '' : '载荷已准备，请在原任务选择实际分区并预览。')
            : preparationPending()
            ? '镜像已准备；请预览仅加载临时环境和读取布局的准备步骤。'
            : capabilities && capabilities.supported === false
            ? '镜像已准备；设备尚未提供刷机所需能力：' + capabilityReason()
            : '镜像已准备，服务端已计算 SHA-256；请生成写入预览。');
        }
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) { busy = false; uploading = false; updateControls(); } }
    }
    async function open(selected, existing) {
      if (uploading) { $('flash-dialog').show(); return; }
      board = selected || {}; selected = board; var seq = ++sequence; clearTimeout(pollTimer);
      busy = false; capabilities = null; artifacts = []; task = null; $('flash-dialog').dataset.taskState = ''; $('fl-execution').hidden = true; cursor = 0; eventLines = []; ++eventSequence;
      inspection = null; payloadCandidates = []; selectionRequestId = ''; selectionPlan = null; renderedBackup = '';
      $('fl-target-storage').value = ''; $('fl-recovery-reason').value = '';
      invalidatePlan(); $('fl-task-id').textContent = ''; $('fl-events').textContent = '';
      $('fl-event-status').textContent = ''; $('fl-results').textContent = '';
      $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      $('fl-board').textContent = selected.target_id ? targetName(selected)
        : selected.key ? (selected.label || selected.key) + ' · SN ' + (selected.serial || '未知') : translate('自动检测本机 USB 下载设备');
      setProgress('', 0, 0, false); renderBackups();
      discovered = []; renderDiscovered();
      $('flash-dialog').show(); status('正在读取刷机能力…');
      if (selected.target_id && existing) {
        observe(existing, selected, seq);
        return;
      }
      if (!selected.serial || selected.target_id) {
        updateControls();
        if (selected.target_id) await selectTarget(selected);
        else await discover();
        return;
      }
      try {
        var result = await client.request('/api/flash-capabilities?key=' + encodeURIComponent(selected.key));
        if (!same(selected, seq)) return;
        capabilities = result.capabilities || result; renderCapabilities();
        await loadHistory(selected, seq, existing);
        if (same(selected, seq) && capabilities.supported === false && !task) await discover();
      } catch (error) { if (same(selected, seq)) status(error.message); }
    }
    $('fl-read-nor').addEventListener('click', readNOR);
    $('fl-close').addEventListener('click', function () { $('flash-dialog').close(); });
    $('fl-discover').addEventListener('click', discover);
    $('fl-device').addEventListener('change', function () {
      var target = discovered.find(function (entry) { return (entry.target_id || entry.id) === $('fl-device').value; });
      if (target) selectTarget(target);
      else if (!busy && !(task && ['queued', 'running'].indexOf(task.state) >= 0)) {
        board = {}; ++sequence; ++eventSequence; clearTimeout(pollTimer);
        task = null; capabilities = null; inspection = null; artifacts = []; payloadCandidates = [];
        invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
        $('fl-task-id').textContent = ''; $('fl-results').textContent = '';
        $('fl-board').textContent = translate('自动检测本机 USB 下载设备');
        status('选择下载模式设备');
      }
    });
    $('fl-mode').addEventListener('change', invalidatePlan);
    $('fl-artifact').addEventListener('change', invalidatePlan);
    $('fl-delete').addEventListener('click', beginDelete);
    $('fl-delete-confirm').addEventListener('click', confirmDelete);
    $('fl-delete-cancel').addEventListener('click', dismissDelete);
    $('fl-verify').addEventListener('change', invalidatePlan);
    $('fl-target-storage').addEventListener('change', invalidatePlan);
    $('fl-upload').addEventListener('click', upload); $('fl-plan').addEventListener('click', makePlan);
    $('fl-download').addEventListener('click', downloadImage);
    $('fl-download-cancel').addEventListener('click', async function () {
      if (!downloadTask) return;
      try { await client.request('/api/task-cancel', { id: downloadTask.id }); }
      catch (error) { status(error.message); }
    });
    $('fl-start').addEventListener('click', start); $('fl-recover').addEventListener('click', recover);
    $('fl-select').addEventListener('click', submitSelection);
    $('fl-recovery-reason').addEventListener('input', updateControls);
    $('fl-release').addEventListener('click', releaseRecovery);
    $('fl-cancel').addEventListener('click', async function () {
      if (!board || !task) return;
      var selected = board, seq = sequence, current = task;
      try {
        var result = await client.request('/api/task-cancel', Object.assign(scope(selected), { id: current.id }));
        if (same(selected, seq) && task.id === current.id) observe(result.task, selected, seq);
      } catch (error) { if (same(selected, seq)) status(error.message); }
    });
    if (global.addEventListener) global.addEventListener('decklab-language-change', function () {
      if (!board || !$('flash-dialog').open) return;
      renderDiscovered(); renderStorages(); renderPartitions();
      $('fl-backups').dataset.signature = ''; renderBackups();
      renderReplacementInfo(selectionPlan || plan);
      if (board.target_id) $('fl-board').textContent = targetName(board);
      else if (!board.key) $('fl-board').textContent = translate('自动检测本机 USB 下载设备');
      updateControls();
    });
    updateControls();
    return { open: open, discover: function () { return open(); } };
  }
  global.SerialdFlashing = { create: create };
})(window);
