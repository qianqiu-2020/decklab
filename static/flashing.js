// Board-level flash planning and observation; no browser-owned hardware transport.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var board = null, capabilities = null, artifacts = [], plan = null, task = null, discovered = [];
    var sequence = 0, busy = false, uploading = false, requestId = '', cursor = 0;
    var selectionRequestId = '', selectionPlan = null, payloadCandidates = [], inspection = null;
    var eventSequence = 0, eventLines = [], pollTimer = null, submissionUnknown = false;
    var pendingDelete = null, sourceInspection = null, sourceCatalogue = null, sourceAnalysedId = '';
    var downloadTask = null, downloadTimer = null, downloadRequest = null, downloadRetry = false;
    var renderedBackup = '', automaticDownloadIdentity = '';
    var bootPriorityRequest = null;
    var progressSample = null, previewTask = null, previewRequestId = '';
    var previewProgress = null;
    var imagePreparations = Object.create(null), rememberedImage = null;
    try { rememberedImage = JSON.parse(global.sessionStorage && global.sessionStorage.getItem('decklab-flash-image') || 'null'); } catch (_) {}
    function rememberImage() {
      var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (!source) return;
      rememberedImage = source ? {id:source.id, mode:$('fl-mode').value, verify:$('fl-verify').value,
        reset_after:$('fl-reset-after').checked, firmware:$('fl-firmware').value,
        loader_source:$('fl-loader-source').value} : null;
      try {
        if (global.sessionStorage) {
          if (rememberedImage) global.sessionStorage.setItem('decklab-flash-image', JSON.stringify(rememberedImage));
          else global.sessionStorage.removeItem('decklab-flash-image');
        }
      } catch (_) {}
    }
    function selectedPreparation() { return imagePreparations[$('fl-artifact').value]; }
    function imageReady() {
      if (!(capabilities && capabilities.image_preparation_supported)) return true;
      return [$('fl-artifact').value, $('fl-mode').value === 'full' && $('fl-firmware').value].filter(Boolean).every(function (artifactId) {
        return imagePreparations[artifactId] && imagePreparations[artifactId].state === 'completed';
      });
    }
    function renderFirmwareOptions(selected) {
      var select = $('fl-firmware'), previous = selected === undefined ? select.value : selected;
      select.textContent = ''; text('option', '仅刷当前镜像', select).value = '';
      artifacts.filter(function (item) { return item.id !== $('fl-artifact').value && packageFile(item); }).forEach(function (item) {
        var option = text('option', item.name, select); option.value = item.id; option.dataset.i18nIgnore = '';
      });
      select.value = artifacts.some(function (item) { return item.id === previous && item.id !== $('fl-artifact').value; }) ? previous : '';
    }
    function renderPreparation() {
      var current = selectedPreparation(), box = $('fl-image-preparation');
      box.hidden = !current;
      if (!current) return;
      var progress = current.progress || {}, active = ['queued','running'].includes(current.state);
      var directorySource = artifacts.find(function(item){return item.id===$('fl-artifact').value&&/\.directory$/i.test(item.name);});
      var phases = {'checking-image-sha256':'校验镜像 SHA-256', 'expanding-image':directorySource?'建立目录快照':'解压镜像',
        'checking-payloads':'校验包内文件', 'reading-payload-metadata':'读取文件系统与版本信息'};
      $('fl-image-preparation-status').textContent = current.state === 'completed' ? '镜像已准备，可复用于其他板卡'
        : active ? (phases[progress.phase] || '等待准备镜像') : current.error || '镜像准备未完成';
      var timing = current.flash && current.flash.preview_progress;
      $('fl-image-overall').hidden = !timing;
      if (timing) drawStageProgress(timing, phases, $('fl-image-stages'), $('fl-image-overall-bar'), $('fl-image-overall-text'), '整体准备进度');
      var bar = $('fl-image-preparation-progress'); bar.hidden = !active;
      bar.max = progress.total || 1;
      if (progress.total) bar.value = Math.min(progress.done || 0, progress.total);
      else bar.removeAttribute('value');
      $('fl-image-preparation-bytes').textContent = active && progress.total
        ? format(progress.done || 0) + ' / ' + format(progress.total) + ' · ' + Math.floor(100*(progress.done || 0)/progress.total) + '%' : '';
      var recovery = current.flash && current.flash.recovery_required;
      $('fl-image-recovery').hidden = !recovery;
      $('fl-image-preparation-retry').hidden = active || current.state === 'completed' || !!recovery;
      $('fl-image-preparation-cancel').hidden = !active || !current.id;
      $('fl-image-preparation-cancel').disabled = !!current.cancel_requested;
      var info = $('fl-image-information'); info.textContent = '';
      if (current.state === 'completed') {
        var prepared = (current.result || {}).preparation || {};
        var metadata = prepared.payload_metadata || [], extra = null;
        var hasFilesystem = metadata.some(function (item) { return item.content && item.content.filesystem; });
        metadata.slice().sort(function (a,b) { return Number(!!(b.content && b.content.filesystem))-Number(!!(a.content && a.content.filesystem)); }).forEach(function (item) {
          var parent = info;
          if (hasFilesystem && !(item.content && item.content.filesystem)) {
            if (!extra) { extra = text('details', '', info); text('summary', '固件与其他分区', extra); }
            parent = extra;
          }
          var row = text('div', '', parent);
          text('strong', item.partition || '镜像文件', row).dataset.i18nIgnore = '';
          renderContent(text('div', '', row), item.content || {}, '', false);
        });
        if (!info.children.length) (prepared.catalogue || []).forEach(function (table) {
          text('p', (table.partitions || []).map(function (part) { return part.name; }).join(' · '), info).dataset.i18nIgnore = '';
        });
      }
    }
    async function prepareSource(artifact, supplied, retry) {
      if (!artifact || !(capabilities && capabilities.image_preparation_supported || supplied)) return;
      var previous = imagePreparations[artifact.id];
      if (!retry && previous) return;
      var marker = {state:'queued'}; imagePreparations[artifact.id] = marker;
      renderPreparation(); updateControls();
      try {
        var current = supplied || (await client.request('/api/image-prepare', Object.assign({id:artifact.id},
          retry ? {request_id:id()} : {}))).task;
        if (imagePreparations[artifact.id] !== marker) return;
        await client.watch(current, {key:''}, function (value) {
          if (imagePreparations[artifact.id] !== marker && imagePreparations[artifact.id].id !== value.id) return;
          marker = value; imagePreparations[artifact.id] = value;
          if ([$('fl-artifact').value, $('fl-firmware').value].includes(artifact.id)) { renderPreparation(); updateControls(); }
        });
      } catch (error) {
        marker = Object.assign({}, imagePreparations[artifact.id], {state:'failed', error:error.message});
        imagePreparations[artifact.id] = marker;
      }
      if ([$('fl-artifact').value, $('fl-firmware').value].includes(artifact.id)) {
        renderPreparation(); updateControls();
        if (marker.state === 'completed' && workspaceSelection() && packageFile(artifact)) inspectSource();
      }
    }
    function operationReference(selected, value) {
      try {
        if (!global.sessionStorage) return null;
        var key = 'decklab-flash-task:' + (selected.target_id || selected.serial || selected.key);
        if (value === undefined) return global.sessionStorage.getItem(key);
        if (value) global.sessionStorage.setItem(key, value);
        else global.sessionStorage.removeItem(key);
      } catch (_) { /* Original tasks remain in server history. */ }
      return null;
    }
    function previewUpdate(current) {
      previewTask = current;
      previewProgress = current.flash && current.flash.preview_progress || null;
      updateControls();
      var p = current.progress || {};
      setProgress(p.phase || 'checking-image', p.done, p.total, ['queued','running'].includes(current.state));
      $('fl-task-details').hidden = false;
      $('fl-task-id').textContent = current.id + ' · ' + current.state;
      $('fl-task-status').textContent = current.error || '';
    }
    function usePreview(result) {
      var value = result.plan || result;
      if (result.requires_prepare || value.requires_prepare) value.ready = false;
      if (!(value.id || value.plan_id || result.plan_id)) throw new Error('刷机计划缺少写入范围，不能执行。');
      if (value.ready !== false && (!Array.isArray(value.writes) || !value.writes.length)) throw new Error('刷机计划缺少写入范围，不能执行。');
      if (value.mode) $('fl-mode').value = value.mode;
      if (value.loader_source) $('fl-loader-source').value = value.loader_source;
      if(value.firmware_artifact_id){(value.components||[]).forEach(function(component){if(component.id&&!artifacts.some(function(item){return item.id===component.id;}))artifacts.push(component);});renderFirmwareOptions(value.firmware_artifact_id);}

      plan = value; plan.id = value.id || value.plan_id || result.plan_id; requestId = id();
      if (result.inspection) { inspection = result.inspection; renderPartitions(); renderStorages(); }
      previewRequestId = ''; setProgress('image-ready', 0, 0, false); renderPlan(plan);
      status('计划已准备，请核对板卡和全部写入范围后开始。');
    }
    async function resumePreview(current, selected, seq) {
      busy = true; previewRequestId = current.request_id || operationReference(selected) || '';
      var details = current.flash || {}, intent = details.preview_intent || {};
      if (details.preview_source) artifacts = [details.preview_source];
      if(details.preview_firmware)artifacts.push(details.preview_firmware);
      if (intent.mode) $('fl-mode').value = intent.mode;
      if (intent.verify) $('fl-verify').value = intent.verify;
      if (intent.loader_source) $('fl-loader-source').value = intent.loader_source;
      $('fl-reset-after').checked = !!intent.reset_after;
      if (intent.artifact_id) artifactOptions($('fl-artifact'), intent.artifact_id);
      if(intent.firmware_artifact_id)renderFirmwareOptions(intent.firmware_artifact_id);
      if (intent.target_storage) $('fl-target-storage').value = intent.target_storage;
      try {
        if (!capabilities) {
          var caps = await client.request('/api/flash-capabilities?' + query(selected).slice(1));
          if (!same(selected, seq)) return;
          capabilities = caps.capabilities || caps;
        }
        var result = await client.watch(current, {key:selected.key || '',serial:selected.serial || ''}, function (value) {
          if (same(selected, seq)) previewUpdate(value);
        });
        if (same(selected, seq)) usePreview(result);
      } catch (error) { if (same(selected, seq)) status(error.message, true); }
      finally { if (same(selected, seq)) { busy = false; previewTask = null; updateControls(); } }
    }
    function status(message, failed) { $('fl-status').textContent = message; $('fl-status').dataset.error = failed ? 'true' : ''; }
    function renderSource() {
      var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      $('fl-source-summary').textContent = source ? source.name + ' · ' + format(source.directory_bytes || source.size) : translate('尚未选择镜像');
      if (source) $('fl-source-summary').dataset.i18nIgnore = '';
      else delete $('fl-source-summary').dataset.i18nIgnore;
    }
    function workspaceSelection() { return task && ['queued','running'].includes(task.state) && task.flash && task.flash.operation === 'nor-workspace' && awaitingSelection(task); }
    function changeMode() {
      rememberImage(); invalidatePlan();
      $('fl-storage-details').open = $('fl-mode').value === 'partitions' && !task;
      renderReplacementInfo($('fl-mode').value === 'full' ? sourceCatalogue : null);
    }
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
      selected.serial = current.flash && typeof current.flash.request_serial === 'string' ? current.flash.request_serial
        : serials.length === 1 ? (typeof serials[0] === 'string' ? serials[0] : serials[0].serial || '') : '';
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
          && !(task && ['queued', 'running'].includes(task.state))) {
        $('fl-mode').value = 'partitions';
      }
    }
    function packageFile(artifact) {
      return artifact && /\.(tar(?:\.(?:gz|xz|bz2|zst))?|tgz|zip|7z|directory)$/i.test(artifact.name || '');
    }
    function partitionOptions(select, selected, storage, partition) {
      select.textContent = '';
      text('option', '选择分区内容', select).value = '';
      var seen = Object.create(null), source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      function add(item, mapped) {
        var key = item.id || item.artifact_id;
        if (!key || seen[key]) return;
        seen[key] = true;
        var label = mapped ? translate('匹配包内分区') + ' ' + partition + ' · ' + item.name : item.name + ' · ' + format(item.size);
        var option = text('option', label, select); option.value = key; option.dataset.i18nIgnore = '';
      }
      artifacts.filter(function (item) { return !packageFile(item); }).forEach(function (item) { add(item, false); });
      if (packageFile(source) && (!sourceCatalogue || payloadCandidates.some(function (item) { return item.storage === storage && item.partition === partition; }))) add(source, !!sourceCatalogue);
      // Prepared candidates are already bound by the executor's partition map.
      // Never offer another partition's candidate or every cached archive as a file.
      payloadCandidates.forEach(function (candidate) {
        if (candidate.storage && candidate.storage !== storage || candidate.partition && candidate.partition !== partition) return;
        var known = artifacts.find(function (item) { return item.id === (candidate.id || candidate.artifact_id); });
        if (packageFile(known)) {
          if (!packageFile(source) || source.id === known.id) add(known, true);
        } else add(known || candidate, !!(candidate.storage && candidate.partition));
      });
      select.value = seen[selected] ? selected : '';
    }
    function addSource(artifact, target) {
      var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      var replacingPartition = target || Array.from($('fl-partitions').children).some(function (row) {
        return row.checkbox && row.checkbox.checked;
      });
      artifacts = artifacts.filter(function (item) { return item.id !== artifact.id; }).concat([artifact]);
      selectPayloadMode(artifact);
      // A partition override does not replace the package supplying other rows
      // (or its RAM preparation materials).
      var anchor = replacingPartition && packageFile(source) && !packageFile(artifact) && $('fl-mode').value === 'partitions' ? source.id : artifact.id;
      if (source && anchor !== source.id) {
        sourceCatalogue=null;sourceInspection=null;sourceAnalysedId='';$('fl-package-contents').textContent='';
        if (workspaceSelection()) inspection=task.flash.inspection;
      }
      artifactOptions($('fl-artifact'), anchor); renderPartitions();
      var rows = Array.from($('fl-partitions').children).filter(function (row) { return row.checkbox; });
      var selected = target ? rows.filter(function (row) { return row.storageId === target.storage && row.partitionId === target.partition; })
        : rows.filter(function (row) { return row.checkbox.checked; });
      if (target && !selected.length) throw new Error('当前设备或任务状态已变化，请重新选择。');
      if (packageFile(artifact) || selected.length === 1) selected.forEach(function (row) {
        if (Array.from(row.artifactSelect.children).some(function (option) { return option.value === artifact.id; })) {
          row.artifactSelect.value = artifact.id; row.checkbox.checked = true;
        }
      });
      if (artifact.firmware) artifacts = artifacts.filter(function (item) { return item.id !== artifact.firmware.id; }).concat([artifact.firmware]);
      renderFirmwareOptions(artifact.firmware ? artifact.firmware.id : source && source.id !== anchor ? '' : undefined);
      invalidatePlan(); renderReplacementInfo(null); rememberImage();
      prepareSource(artifact, artifact.preparation_task);
      if (artifact.firmware) prepareSource(artifact.firmware, artifact.firmware.preparation_task);
    }
    function invalidatePlan() {
      dismissDelete();
      if (plan && board) operationReference(board, null);
      plan = null; requestId = ''; submissionUnknown = false;
      selectionPlan = null; selectionRequestId = '';
      previewProgress = null; $('fl-preview-stages').textContent = ''; $('fl-preview-overall').hidden = true;
      $('fl-preview').textContent = '';
      $('fl-start').disabled = true;
      status('');
      updateControls();
    }
    function prepareOnly(value) {
      return value && value.ready === false && $('fl-mode').value === 'partitions' && !(value.writes || []).length;
    }
    function fullConfirmation() { return task && task.flash && task.flash.confirmation_mode === 'full'; }
    function selectionExpired(current) {
      return current && current.state === 'failed'
        && current.error === 'flash partition selection timed out; storage was not written';
    }
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
      var nor = norContext() && !(sourceInspection && sourceInspection.storages.some(function (item) { return item.physical_device; })), workspace = workspaceSelection();
      $('fl-all-label').textContent = nor ? '全选 NOR 分区' : '全选分区';
      renderSource(); renderPreparation();
      var primary = artifacts.find(function(item){return item.id===$('fl-artifact').value;});
      var loader = $('fl-loader-source'), loaderSources = preparable && capabilities.loader_sources || [];
      var choices = loaderSources.filter(function (source) { return source !== 'image' || !primary || packageFile(primary); });
      $('fl-loader-field').hidden = !loaderSources.length || !!active;
      Array.from(loader.children).forEach(function (option) { option.disabled = !choices.includes(option.value); });
      if (!choices.includes(loader.value)) loader.value = choices[0] || '';
      loader.disabled = busy || uploading || !!active || submissionUnknown || !choices.length;
      $('fl-firmware-field').hidden = $('fl-mode').value !== 'full' || !packageFile(primary) || /(?:^|[-_])firmware(?:[-_.]|$)/i.test(primary.name);
      $('fl-firmware-choose').disabled = busy || uploading || !!active || submissionUnknown || !options.openImageStore;
      $('fl-firmware').disabled = busy || uploading || !!active || submissionUnknown;
      $('fl-workspace').hidden = !$('fl-artifact').value;
      $('fl-workspace').disabled = busy || uploading || !!active || !options.openImageWorkspace || !(primary && /\.directory$/i.test(primary.name)) && capabilities && capabilities.image_preparation_supported && !(selectedPreparation() && selectedPreparation().state === 'completed');
      $('fl-directory').disabled = busy || uploading || !!active || !options.openImageWorkspace;
      var firmwarePreparation = imagePreparations[$('fl-firmware').value];
      $('fl-firmware-retry').hidden = !firmwarePreparation || ['queued','running','completed'].includes(firmwarePreparation.state);
      $('fl-firmware-retry').disabled = busy || uploading || !!active;
      $('fl-firmware-status').textContent = !$('fl-firmware').value ? '' : firmwarePreparation && firmwarePreparation.state === 'completed' ? '配套固件已准备' : firmwarePreparation && ['failed','cancelled','interrupted'].includes(firmwarePreparation.state) ? firmwarePreparation.error || '配套固件准备失败' : '正在准备配套固件…';
      renderCurrentNOR();
      updateBootControls();
      $('fl-review').hidden = !$('fl-preview').children.length;
      $('fl-task-details').hidden = !task;
      $('fl-read-nor').disabled = busy || uploading || !!active || submissionUnknown || !!(board && board.observingTask)
        || !(capabilities && capabilities.nor_workspace_supported);
      $('fl-read-nor').hidden = !nor || !(capabilities && capabilities.storage_inventory_supported) && $('fl-mode').value !== 'partitions';
      $('fl-storage-details').hidden = $('fl-read-nor').hidden;
      $('fl-read-nor').textContent = capabilities && capabilities.storage_inventory_supported ? '读取设备存储' : '读取并备份 NOR';
      if (active || busy || uploading) dismissDelete();
      $('fl-delete').disabled = busy || uploading || submissionUnknown || !!active || !!(board && board.observingTask)
        || !board || !artifacts.some(function (item) { return item.id === $('fl-artifact').value; });
      $('fl-delete').hidden = !$('fl-artifact').value;
      if (selecting) uploadAvailable = true;
      $('fl-device').disabled = busy || submissionUnknown || !!active || uploading;
      $('fl-discover').disabled = busy || submissionUnknown || !!active || uploading;
      ['fl-mode', 'fl-artifact', 'fl-files', 'fl-verify', 'fl-target-storage', 'fl-reset-after'].forEach(function (name) {
        $(name).disabled = busy || submissionUnknown || !!active;
      });
      $('fl-mode').disabled = busy || submissionUnknown || !!active && !workspace || !workspace && !!preparable && !(capabilities.modes || []).includes('full');
      if (workspace) ['fl-verify', 'fl-target-storage'].forEach(function (name) { $(name).disabled = busy || uploading || submissionUnknown; });
      $('fl-reset-after').disabled = busy || uploading || submissionUnknown || !!active && !selecting
        || !capabilities || !capabilities.reset_after_supported;
      if (workspace) Array.from($('fl-mode').children).forEach(function (option) { option.disabled = false; });
      $('fl-mode').title = workspace ? '整包选择刷机配置引用的全部目标分区，保留现场分区表。' : '';
      $('fl-local').disabled = busy || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-files').disabled = $('fl-local').disabled;
      $('fl-store').disabled = $('fl-local').disabled;
      $('fl-cache').disabled = $('fl-local').disabled;
      $('fl-url-toggle').disabled = $('fl-local').disabled;
      $('fl-download').disabled = busy || uploading || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-download').hidden = !downloadRetry;
      $('fl-url').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-url-sha256').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-download-cancel').hidden = !downloadTask || !['queued', 'running'].includes(downloadTask.state);
      $('fl-plan').disabled = busy || uploading || !!active || !!(board && board.observingTask)
        || !(available || preparable) || !$('fl-artifact').value || !imageReady();
      $('fl-plan').hidden = !!selecting;
      if (nor && $('fl-mode').value === 'partitions' && !active) $('fl-plan').disabled = true;
      $('fl-plan').textContent = preparable ? '预览刷机准备步骤' : '预览写入范围';
      $('fl-start').disabled = busy || uploading || !!active || !plan || submissionUnknown;
      $('fl-start').hidden = !!active || nor && $('fl-mode').value === 'partitions';
      $('fl-start').textContent = plan && plan.confirm_after_prepare && plan.ready === false ? '准备环境并核对固件范围' : prepareOnly(plan) ? '准备刷机环境并读取布局' : '开始刷写';
      $('fl-cancel').disabled = previewTask ? !['queued','running'].includes(previewTask.state) || previewTask.cancel_requested : !active || task.cancel_requested;
      $('fl-cancel').textContent = previewTask ? '取消预览' : nor && selecting ? '结束准备并保留备份' : '取消刷机';
      var verifying = active && task.flash && task.flash.can_skip_verification === true
        && ['verifying','verifying-nor','verifying-system'].includes((task.progress || {}).phase);
      $('fl-skip-verify').hidden = !verifying;
      $('fl-skip-verify').disabled = !verifying || busy || task.cancel_requested || task.flash.verify_skip_requested === true;
      $('fl-skip-verify').textContent = task && task.flash && task.flash.verify_skip_requested ? '正在跳过校验…' : '跳过剩余校验';
      if (nor && selecting && !uploading) {
        var preview = task.flash && task.flash.preview_progress;
        previewProgress = preview && preview.stages ? preview : null;
        if (preview && preview.active) setProgress(preview.phase, preview.done, preview.total, true);
        else setProgress('awaiting-selection', 0, 0, false);
      }
      $('fl-partitions-field').hidden = !sourceInspection && $('fl-mode').value !== 'partitions' && (!selecting || fullConfirmation());
      $('fl-target-storage-field').hidden = sourceInspection ? !sourceInspection.storages.some(function (item) { return item.physical_device; }) : $('fl-mode').value !== 'full' && !workspace;
      $('fl-select').hidden = !selecting;
      var needsCatalogue = workspace && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; })) && !sourceCatalogue;
      $('fl-select').disabled = busy || uploading || submissionUnknown || !selecting || !imageReady();
      $('fl-select').textContent = needsCatalogue ? '解析镜像并预览写入范围' : workspace && sourceCatalogue && $('fl-mode').value === 'full' ? (selectionPlan ? '确认包内全部目标分区并刷写' : '预览包内全部目标分区') : fullConfirmation() ? (selectionPlan ? '确认固件范围并刷写' : '预览固件写入与擦除范围') : selectionPlan ? (nor ? '确认分区并刷写' : '确认分区并继续') : '预览分区写入范围';
      $('fl-recover').hidden = !submissionUnknown;
      var recovery = task && task.flash && task.flash.recovery_required === true;
      $('fl-expired').hidden = !selectionExpired(task);
      $('fl-restart').disabled = busy || uploading || submissionUnknown || !!active || !!recovery;
      $('fl-recovery-field').hidden = !recovery;
      $('fl-recovery-reason').disabled = busy || !!active;
      $('fl-release').disabled = busy || !!active || !recovery || !task.flash.attempt_id
        || !$('fl-recovery-reason').value.trim();
      var rows = $('fl-partitions').children;
      var checks = Array.from(rows).filter(function (row) { return row.checkbox; });
      $('fl-all-partitions').checked = !!checks.length && checks.every(function (row) { return row.checkbox.checked; });
      $('fl-all-partitions').indeterminate = checks.some(function (row) { return row.checkbox.checked; }) && !$('fl-all-partitions').checked;
      $('fl-all-partitions').disabled = busy || uploading || submissionUnknown || !checks.length || !!active && !selecting || nor && !selecting;
      var wholePackage = !!sourceCatalogue && $('fl-mode').value === 'full';
      $('fl-partition-tools').hidden = wholePackage;
      Array.from(rows).forEach(function (row) {
        if (row.checkbox) row.checkbox.hidden = wholePackage;
        if (row.sourceName) row.sourceName.hidden = !wholePackage;
        if (row.chooseFile) row.chooseFile.hidden = wholePackage;
        if (row.checkbox) row.checkbox.disabled = busy || uploading || submissionUnknown || !!active && !selecting || nor && !selecting
          || !!sourceCatalogue && !payloadCandidates.some(function (item) { return item.storage === row.storageId && item.partition === row.partitionId; });
        if (row.artifactSelect) row.artifactSelect.hidden = wholePackage || nor && !row.checkbox.checked;
        if (row.replacementField) row.replacementField.dataset.empty = nor && !row.checkbox.checked && !row.chooseFile ? 'true' : '';
        if (row.artifactSelect) row.artifactSelect.disabled = busy || uploading || submissionUnknown || !!active && !selecting || nor && (!selecting || !row.checkbox.checked);
        if (row.chooseFile) row.chooseFile.disabled = busy || uploading || submissionUnknown || !!active && !selecting || nor && !selecting;
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
    function renderContent(parent, content, fallback, omitSize, hashScope) {
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
      var comparison = content.payload_comparison;
      if (comparison && Number.isSafeInteger(comparison.size) && comparison.size > 0) {
        var matchLabel = comparison.phase === 'before'
          ? (comparison.matches ? '与替换文件一致' : '与替换文件不同')
          : (comparison.matches ? '与本次载荷一致' : '与本次载荷不同');
        line(parent, matchLabel, comparison.size + ' ' + translate('字节'));
        var ranges = text('details', '', parent);
        text('summary', translate(comparison.phase === 'before' ? '按替换文件长度 SHA-256' : '载荷范围 SHA-256')
          + ' · ' + comparison.size + ' ' + translate('字节'), ranges).dataset.i18nIgnore = '';
        text('small', translate(comparison.phase === 'before' ? '板上备份' : '板上回读') + ' · ' + comparison.sha256, ranges).className = 'fl-hash';
        text('small', translate(comparison.phase === 'before' ? '替换文件' : '本次载荷') + ' · ' + comparison.expected_sha256, ranges).className = 'fl-hash';
      }
      if (content.sha256) {
        var details = document.createElement('details');
        var scope = hashScope || content.sha256_scope;
        var label = scope === 'partition' ? '整分区 SHA-256' : '载荷 SHA-256';
        text('summary', translate(label) + (Number.isSafeInteger(content.size) ? ' · ' + content.size + ' ' + translate('字节') : ''), details).dataset.i18nIgnore = '';
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
    function renderCurrentNOR() {
      var box=$('fl-nor-current'), backup=task && task.flash && (task.flash.nor_after || task.flash.nor_before);
      var system=sourceInspection && sourceInspection.storages.some(function (item) { return item.physical_device; });
      box.hidden=!(system && backup);
      var signature=JSON.stringify([!!system,backup && backup.artifact && backup.artifact.id]);
      if(box.dataset.signature===signature) return;
      box.dataset.signature=signature;box.textContent='';
      if(!system || !backup) return;
      var details = text('details', '', box);
      text('summary', 'NOR · 本次保留', details);
      (backup.partitions || []).forEach(function (part) {
        var row = text('div', '', details); row.className = 'fl-system-part';
        var title = text('div', '', row);
        text('strong', part.name, title).dataset.i18nIgnore = '';
        text('small', format(part.size), title).dataset.i18nIgnore = '';
        var content = text('div', '', row); content.className = 'fl-content';
        renderContent(content, part.content, undefined, true, 'partition');
      });
    }
    function renderSystemInventory() {
      var inventory = task && task.flash && task.flash.storage_inventory;
      var box = $('fl-system'), select = $('fl-system-storage');
      box.hidden = !inventory;
      if (!inventory) return;
      var disks = inventory.storages || [], signature = JSON.stringify(inventory);
      var kind = {'eeprom:UFS':'scsi', 'eeprom:SSD':'nvme', 'eeprom:MMC':'mmc', 'eeprom:USB':'usb'}[inventory.priority_source];
      var matches = disks.filter(function (disk) { return kind && disk.id.split(':')[0] === kind; });
      // Older saved tasks cleared the association after a verified EEPROM edit.
      // This fallback selects only the information view, never a write target.
      var defaultStorage = inventory.default_storage || (matches.length === 1 ? matches[0].id : '');
      var unmatched = !kind ? '未能确认默认介质，请选择查看。'
        : matches.length > 1 ? '有多个匹配的系统盘，请选择查看。'
        : '未发现与优先启动介质匹配的系统盘，请选择查看。';
      if (box.dataset.signature !== signature) {
        $('fl-boot-priority').value = (inventory.priority_source || '').indexOf('eeprom:') === 0 ? inventory.priority_source.slice(7) : '';
        var previous = select.value; select.textContent = ''; box.dataset.signature = signature;
        text('option', '请选择系统盘', select).value = '';
        disks.forEach(function (disk) {
          var kind = disk.id.split(':')[0], names = {nvme:'NVMe',scsi:'UFS / SCSI',mmc:'eMMC / SD',usb:'USB'};
          var node = text('option', (names[kind] || kind) + ' · ' + disk.id + ' · ' + format(disk.size), select);
          node.value = disk.id; node.dataset.i18nIgnore = '';
        });
        select.value = disks.some(function (disk) { return disk.id === previous; }) ? previous : defaultStorage;
      }
      $('fl-system-origin').textContent = !disks.length ? '未发现可读取的系统盘。'
        : !defaultStorage ? unmatched
        : inventory.priority_source && inventory.priority_source.indexOf('eeprom:') === 0 ? translate('EEPROM 第二启动介质') + ' · ' + inventory.priority_source.slice(7) : '';
      updateBootControls();
      var parts = $('fl-system-parts'); parts.textContent = '';
      var disk = disks.find(function (item) { return item.id === select.value; });
      if (!disk) return;
      drawStorageMap(parts, disk, [], []);
      (disk.partitions || []).forEach(function (part) {
        var row = document.createElement('div'); row.className = 'fl-system-part';
        var title = document.createElement('div');
        text('strong', part.name, title).dataset.i18nIgnore = '';
        text('small', format(part.size) + ' · ' + translate('偏移') + ' ' + part.offset, title).dataset.i18nIgnore = '';
        row.appendChild(title);
        var content = document.createElement('div'); content.className = 'fl-content';
        renderContent(content, part.content, '尚未读取', true); row.appendChild(content); parts.appendChild(row);
      });
      if (!(disk.partitions || []).length) text('p', disk.error || '未读取到有效的系统盘分区表。', parts);
    }
    function updateBootControls() {
      var inventory = task && task.flash && task.flash.storage_inventory;
      var change = task && task.flash && task.flash.boot_priority_change;
      var current = inventory && (inventory.priority_source || '').indexOf('eeprom:') === 0 ? inventory.priority_source.slice(7) : '';
      var pending = change && change.state !== 'completed';
      $('fl-boot-config').hidden = !inventory || !inventory.boot_priority_editable;
      $('fl-boot-priority').disabled = busy || !!pending || !task || !awaitingSelection(task);
      $('fl-boot-save').disabled = $('fl-boot-priority').disabled || !$('fl-boot-priority').value || $('fl-boot-priority').value === current;
      $('fl-boot-status').textContent = change ? change.state === 'completed' ? '已回读校验，下次启动生效。'
        : change.state === 'running' ? '正在保存 EEPROM…' : '保存结果未确认，请重新读取设备存储。' : '下次启动生效。';
      var serial = task && task.flash && task.flash.resolved_serial || board && board.serial || '';
      $('fl-eeprom-identity').hidden = !serial;
      $('fl-eeprom-identity').textContent = serial ? 'SN ' + serial : '';
      $('fl-eeprom-identity').dataset.i18nIgnore = '';
    }
    async function saveBootPriority() {
      if (!board || !task || $('fl-boot-save').disabled) return;
      var selected = board, seq = sequence, current = task;
      var inventory = current.flash.storage_inventory;
      var value = $('fl-boot-priority').value, expected = (inventory.priority_source || '').indexOf('eeprom:') === 0 ? inventory.priority_source.slice(7) : '-';
      if (!bootPriorityRequest || bootPriorityRequest.value !== value || bootPriorityRequest.expected !== expected)
        bootPriorityRequest = {value: value, expected: expected, request_id: id()};
      busy = true; updateControls(); status('正在保存 EEPROM…');
      try {
        var result = await client.request('/api/flash-selection', Object.assign(scope(selected), bootPriorityRequest,
          {action: 'boot-priority', id: current.id}));
        if (!same(selected, seq) || !task || task.id !== current.id) return;
        var change = result.change || result.task && result.task.flash && result.task.flash.boot_priority_change;
        if (!change || change.state !== 'completed' || change.verify_result !== 'verified') {
          if (result.task) observe(result.task, selected, seq);
          throw new Error(translate('保存结果未确认，请重新读取设备存储。'));
        }
        bootPriorityRequest = null;
        observe(result.task, selected, seq);
        status('已回读校验，下次启动生效。');
      } catch (error) { if (same(selected, seq)) status(error.message, true); }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function renderReplacementInfo(value) {
      value = value || sourceCatalogue;
      var metadata = value && value.payload_metadata || [];
      Array.from($('fl-partitions').children).forEach(function (row) {
        if (!row.payloadInfo || !row.artifactSelect) return;
        var match = metadata.find(function (item) { return item.storage === row.storageId && item.partition === row.partitionId; });
        var selected = artifacts.find(function (item) { return item.id === row.artifactSelect.value; });
        var applicable = match && match.content && (row.checkbox.checked || sourceCatalogue && $('fl-mode').value === 'full')
          && (!selected || packageFile(selected) || selected.sha256 === match.sha256);
        if (applicable) renderContent(row.payloadInfo, match.content); else row.payloadInfo.textContent = '';
        var content = row.currentContent;
        var backup = task && task.flash && !task.flash.nor_after && task.flash.nor_before;
        if (applicable && content && match.board_comparison && backup
            && match.board_comparison.backup_id === backup.artifact.id
            && match.board_comparison.backup_sha256 === backup.artifact.sha256
            && match.board_comparison.phase === 'before'
            && match.board_comparison.size === match.content.size
            && match.board_comparison.expected_sha256 === match.sha256) {
          content = Object.assign({}, content, {payload_comparison: match.board_comparison});
        }
        if (row.currentInfo) renderContent(row.currentInfo, content, '尚未读取', true, row.currentHashScope);
      });
    }
    function setProgress(phase, done, total, active) {
      var names = {'checking-upload': '核对上传文件 SHA-256', 'checking-image-sha256': '核对镜像 SHA-256',
        'expanding-image': '解压镜像（压缩包读取）', 'checking-payloads': '核对包内载荷',
        'checking-layout': '核对现场布局', 'reading-payload-metadata': '读取分区文件系统信息', 'reading-nor': '读取整片 NOR', 'checking-nor-baseline': '检查备份后板上内容是否变化',
        'checking-image': '核对写入计划与载荷', 'verifying': '回读校验', 'verifying-nor': '回读整片 NOR 并校验', 'verifying-system': '回读所选系统盘分区', 'writing': '写入所选分区', 'resetting': '刷写完成，正在复位',
        'preparing-loader': '加载临时刷机器', 'prepare': '准备临时刷机器',
        'awaiting-selection': '备份已完成，等待选择替换内容', 'image-download': '后端下载镜像',
        'image-ready': '镜像已准备', 'uploading-image': '上传替换文件',
        'reading-system-layout': '读取系统盘分区表', 'reading-system-metadata': '读取系统盘版本信息'};
      var label = names[phase] || phase || '尚未开始', bar = $('fl-progress');
      bar.hidden = phase === 'awaiting-selection';
      $('fl-execution').hidden = !phase || phase === 'awaiting-selection';
      done = Math.max(0, Number(done) || 0); total = Math.max(0, Number(total) || 0);
      bar.max = Math.max(1, total);
      if (active && !total && phase !== 'awaiting-selection' && bar.removeAttribute) bar.removeAttribute('value');
      else bar.value = total ? Math.min(done, total) : 0;
      var counts = total ? ((phase === 'prepare' || phase === 'preparing-loader')
        ? done + ' / ' + total + ' ' + translate('步骤') : format(done) + ' / ' + format(total)) : done ? format(done) : '';
      $('fl-progress-text').textContent = translate(label) + (counts ? ' · ' + counts : '')
        + (total ? ' · ' + Math.floor(Math.min(done, total) / total * 100) + '%' : '');
      var now = Date.now();
      if (!active || !progressSample || progressSample.phase !== phase || done < progressSample.done || total !== progressSample.total)
        progressSample = active ? {phase:phase,done:done,total:total,time:now} : null;
      if (active && progressSample && now - progressSample.time >= 3000 && done > progressSample.done && total > done) {
        var rate = (done - progressSample.done) / ((now - progressSample.time) / 1000);
        var seconds = Math.ceil((total - done) / rate);
        $('fl-progress-text').textContent += ' · 预计剩余 ' + (seconds < 60 ? seconds + ' 秒' : Math.ceil(seconds / 60) + ' 分钟');
      }
      if (phase === 'writing') $('fl-progress-text').textContent += ' · 按展开载荷计数，非 USB 传输量';
      bar.setAttribute('aria-valuetext', $('fl-progress-text').textContent);
      drawPreviewProgress(names);
    }
    function drawPreviewProgress(names) {
      var box = $('fl-preview-stages'), overall = $('fl-preview-overall'); box.textContent = '';
      $('fl-execution-title').textContent = translate(previewProgress ? '预览写入范围' : '刷写与校验');
      overall.hidden = !previewProgress;
      if (!previewProgress) return;
      $('fl-execution').hidden = false;
      drawStageProgress(previewProgress, names, box, $('fl-overall-bar'), $('fl-overall-text'), '整体预览进度');
    }
    function drawStageProgress(model, names, box, bar, labelNode, title) {
      box.textContent = '';
      var pending = 0, completed = 0, now = Date.now()/1000;
      (model.stages || []).forEach(function (step) {
        var elapsed = Number(step.elapsed_seconds) || 0, estimate = Number(step.estimated_seconds) || 0;
        if (step.state === 'running' && model.active) elapsed += Math.max(0, now-model.updated);
        // A quiet hashing/layout stage can outlive its estimate. Recalibrate
        // instead of leaving the whole preview stuck at "1 second remaining".
        if (step.state === 'running') estimate = Math.max(estimate, elapsed*1.25);
        var left = step.state === 'pending' ? estimate : step.state === 'running' ? Math.max(1, estimate-elapsed) : 0;
        pending += left; completed += ['completed','skipped'].includes(step.state) ? estimate : step.state === 'running' ? Math.min(elapsed,estimate*.98) : 0;
        var label = {pending:'等待',running:'进行中',completed:'完成',skipped:'无需执行',failed:'未完成'}[step.state];
        var row = text('li', translate(names[step.phase] || step.phase) + ' · ' + translate(label)
          + (step.state === 'completed' ? ' · ' + Math.ceil(elapsed) + ' ' + translate('秒') : ''), box);
        row.dataset.state = step.state;
      });
      var percent = model.state === 'completed' ? 100 : Math.min(99, Math.floor(100*completed/Math.max(1,completed+pending)));
      bar.value = percent;
      var seconds = Math.ceil(pending), duration = seconds < 60 ? seconds + ' ' + translate('秒') : Math.ceil(seconds/60) + ' ' + translate('分钟');
      labelNode.textContent = translate(title) + ' · ' + percent + '%'
        + (model.active ? ' · ' + translate(model.sample_count ? '预计剩余' : '初步估算剩余') + ' ' + duration : '');
    }
    function renderPartitions() {
      var box = $('fl-partitions'), previous = {};
      Array.from(box.children).forEach(function (row) { if (row.checkbox && row.artifactSelect)
        previous[row.storageId + '/' + row.partitionId] = {checked: row.checkbox.checked, artifact: row.artifactSelect.value}; });
      box.textContent = '';
      var after = task && task.flash && task.flash.nor_after;
      var existing = after || task && task.flash && task.flash.nor_before;
      $('fl-current-heading').textContent = after ? '写后回读内容' : '板上现有内容';
      var layout = $('fl-current-layout'); layout.textContent = '';
      ((inspection || capabilities || {}).storages || []).forEach(function (storage) {
        drawStorageMap(layout, storage, [], []);
        (storage.partitions || []).forEach(function (partition) {
          var row = document.createElement('div'); row.className = 'fl-partition';
          var label = document.createElement('label');
          var checkbox = document.createElement('input'); checkbox.type = 'checkbox';
          label.appendChild(checkbox);
          var storageName = storage.capacity_source === 'spacemit-ram-v1' ? 'NOR' : storage.label || storage.name || storage.id;
          var prefix = norContext() && ((inspection || capabilities || {}).storages || []).length === 1 ? '' : storageName + ' / ';
          var name = text('span', prefix + (partition.name || partition.id), label);
          checkbox.setAttribute('aria-label', storageName + ' / ' + (partition.name || partition.id));
          name.dataset.i18nIgnore = '';
          text('small', format(partition.size) + (partition.offset === undefined ? '' : ' · ' + translate('偏移') + ' ' + partition.offset), name).dataset.i18nIgnore = '';
          row.appendChild(label);
          var current = document.createElement('div'); current.className = 'fl-content'; current.dataset.label = translate(after ? '写后回读内容' : '板上现有内容');
          var old = existing && (existing.partitions || []).find(function (part) { return part.name === (partition.name || partition.id); });
          if (storage.physical_device && task && task.flash.storage_inventory) {
            var disk = (task.flash.storage_inventory.storages || []).find(function (item) { return item.id === storage.physical_device; });
            old = disk && (disk.partitions || []).find(function (part) { return part.name === (partition.name || partition.id); });
          }
          renderContent(current, old && old.content, '尚未读取', true, existing && !storage.physical_device ? 'partition' : undefined); row.appendChild(current);
          row.currentInfo = current; row.currentContent = old && old.content;
          row.currentHashScope = existing && !storage.physical_device ? 'partition' : undefined;
          var replacement = document.createElement('div'); replacement.className = 'fl-content'; replacement.dataset.label = translate('替换文件');
          var select = document.createElement('select');
          select.setAttribute('aria-label', translate('替换文件') + ' · ' + (partition.name || partition.id));
          var saved = previous[storage.id + '/' + (partition.id || partition.name)];
          var chosen = saved ? saved.artifact : '';
          partitionOptions(select, chosen, storage.id, partition.id || partition.name); replacement.appendChild(select);
          checkbox.checked = !!(saved && saved.checked);
          var payloadInfo = document.createElement('div'); replacement.appendChild(payloadInfo); row.payloadInfo = payloadInfo;
          row.appendChild(replacement); row.checkbox = checkbox; row.artifactSelect = select; row.replacementField = replacement;
          row.storageId = storage.id; row.partitionId = partition.id || partition.name;
          var member = null;
          (sourceCatalogue && sourceCatalogue.catalogue || []).filter(function (table) { return table.selected; }).some(function (table) {
            member = (table.partitions || []).find(function (part) { return part.name === row.partitionId && part.payload; }); return !!member;
          });
          row.sourceName = text('strong', member ? member.payload : '本次不写入', replacement);
          row.sourceName.dataset.i18nIgnore='';

          if (options.openImageStore) {
            var chooseFile = text('button', '从商店选择文件', replacement); chooseFile.type = 'button'; chooseFile.className = 'fl-choose-file'; row.chooseFile = chooseFile;
            chooseFile.addEventListener('click', function () {
              if (chooseFile.disabled) return;
              var context = board, seq = sequence, taskId = task && task.id, target = {storage:row.storageId,partition:row.partitionId};
              options.openImageStore(function (artifact) {
                var current = Array.from($('fl-partitions').children).find(function (item) {
                  return item.storageId === target.storage && item.partitionId === target.partition;
                });
                if (!same(context, seq) || (task && task.id) !== taskId || !current || current.chooseFile.disabled) throw new Error('当前设备或任务状态已变化，请重新选择。');
                if (packageFile(artifact)) throw new Error('请选择直接写入该分区的文件；整包请在镜像来源中导入。');
                addSource(artifact, target); status('已选择分区文件；请预览写入范围。');
              }, {fileOnly:true,partition:partition.name || partition.id,serial:board && board.serial || ''});
            });
          }
          checkbox.addEventListener('change', function () {
            if (checkbox.checked && !select.value) {
              var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
              var checked = Array.from(box.children).filter(function (item) { return item.checkbox && item.checkbox.checked; });
              if (source && (packageFile(source) || checked.length === 1)
                  && Array.from(select.children).some(function (option) { return option.value === source.id; })) select.value = source.id;
            }
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
      text('option', '选择目标系统盘（单盘时可自动核对）', select).value = '';
      var inventory = workspaceSelection() && task.flash.storage_inventory;
      var stores = inventory ? (inventory.storages || []).slice() : (inspection || capabilities || {}).storages || [];
      var actual = sourceInspection && sourceInspection.storages.find(function (item) { return item.physical_device; });
      if (actual && !stores.some(function (item) { return item.id === actual.physical_device; })) stores.push(actual);
      stores.forEach(function (storage) {
        var option = text('option', (storage.label || storage.name || storage.id) + ' · ' + format(storage.size)
          + (storage.capacity_verified === false ? storage.capacity_source ? translate('（GPT 声明，容量待核验）') : ' · 容量尚未独立核验' : ''), select);
        option.value = storage.physical_device || storage.id; option.dataset.i18nIgnore = '';
        option.disabled = !!inventory && storage.capacity_verified === false;
      });
      var desired = sourceInspection && !actual ? '' : actual && (sourceCatalogue && sourceCatalogue.target_storage || actual.physical_device) || previous || inventory && inventory.default_storage || '';
      select.value = Array.from(select.children).some(function (option) { return option.value === desired && !option.disabled; }) ? desired : '';
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
        if (rememberedImage && rememberedImage.id === chosen.artifact.id) {
          rememberedImage = null;
          try { if (global.sessionStorage) global.sessionStorage.removeItem('decklab-flash-image'); } catch (_) {}
        }
        delete imagePreparations[chosen.artifact.id];
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
      [['板型', capabilities.profile || (capabilities.mode === 'unknown' ? '尚未识别' : '未配置')], ['复位控制', capabilities.reset_after_supported ? '支持刷后自动复位' : '未提供刷后自动复位'],
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
        if ($('fl-verify').value === 'required') $('fl-verify').value = 'if-supported';
      }
      var saved = rememberedImage;
      artifacts = (capabilities.artifacts || []).slice();
      artifactOptions($('fl-artifact'), saved && saved.id);
      renderFirmwareOptions(saved && saved.firmware);
      var retainedFirmware = artifacts.find(function (item) { return item.id === $('fl-firmware').value; });
      if (retainedFirmware) prepareSource(retainedFirmware);
      resumeDownload(board, sequence);
      renderPartitions(); renderStorages();
      var modes = preparationPending() ? capabilities.modes || ['partitions'] : capabilities.modes || ['full', 'partitions'];
      Array.from($('fl-mode').children).forEach(function (option) { option.disabled = modes.indexOf(option.value) < 0; });
      $('fl-mode').value = saved && modes.includes(saved.mode) ? saved.mode : modes.indexOf('full') >= 0 ? 'full' : modes[0] || 'full';
      if (saved && $('fl-artifact').value === saved.id) {
        $('fl-verify').value = saved.verify || 'if-supported';
        $('fl-loader-source').value = saved.loader_source || '';
        $('fl-reset-after').checked = !!saved.reset_after;
      }
      var retainedSource = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (retainedSource) prepareSource(retainedSource);
      $('fl-storage-details').open = $('fl-mode').value === 'partitions';
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
    async function loadHistory(selected, seq, existing, fresh) {
      if (existing) {
        if (existing.kind === 'flash-preview') await resumePreview(existing, selected, seq);
        else observe(existing, selected, seq);
        return true;
      }
      function retained(current) {
        return ['queued','running'].includes(current.state)
          || current.flash && current.flash.recovery_required === true;
      }
      var reference = operationReference(selected);
      if (reference) {
        try {
          var original = await client.request('/api/task?request_id=' + encodeURIComponent(reference) + query(selected));
          if (!same(selected, seq)) return true;
          if ((!fresh || retained(original.task)) && original.task.kind === 'flash-preview' && (['queued','running'].includes(original.task.state)
              || Date.now()/1000-original.task.updated < 600)) {
            await resumePreview(original.task, selected, seq); return true;
          }
          if ((!fresh || retained(original.task)) && ['flash','flash-inspect'].includes(original.task.kind)) {
            observe(original.task, selected, seq); return true;
          }
        } catch (error) {
          if (!same(selected, seq)) return true;
          if (![403,404].includes(error.status)) { submissionUnknown = true; previewRequestId = reference; status(error.message,true); updateControls(); return true; }
        }
        operationReference(selected, null);
      }
      var records = await client.request(taskQuery(selected, 100));
      if (!same(selected, seq)) return true;
      var current = (records.tasks || []).find(function (entry) {
        return ['flash','flash-inspect','flash-preview'].includes(entry.kind) && retained(entry);
      });
      if (!current && !fresh) current = (records.tasks || []).find(function (entry) { return ['flash','flash-inspect','flash-preview'].includes(entry.kind); });
      if (current) {
        if (current.kind === 'flash-preview') await resumePreview(current, selected, seq);
        else observe(current, selected, seq);
        return true;
      }
      return false;
    }
    async function selectTarget(target, fresh) {
      if (!target || busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var selected = { target_id: target.target_id || target.id,
        target_generation: target.target_generation || target.generation, label: target.label, serial: target.serial || '',
        present: target.present };
      rememberImage(); board = selected; var seq = ++sequence; clearTimeout(pollTimer); ++eventSequence;
      sourceInspection = null; sourceCatalogue = null; sourceAnalysedId=''; $('fl-package-contents').textContent='';
      busy = true; capabilities = null; task = null; inspection = null; artifacts = []; payloadCandidates = [];
      bootPriorityRequest = null;
      renderedBackup = ''; $('flash-dialog').dataset.taskState = ''; renderBackups(); renderSystemInventory(); setProgress('', 0, 0, false);
      invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      $('fl-task-id').textContent = ''; $('fl-events').textContent = ''; $('fl-results').textContent = '';
      $('fl-task-status').textContent = '';
      $('fl-event-status').textContent = ''; cursor = 0; eventLines = [];
      $('fl-board').textContent = targetName(target); $('fl-device').value = selected.target_id;
      updateControls(); status('正在读取下载设备身份和刷机能力…');
      try {
        // Recover a running task before any USB probe tries to acquire its
        // endpoint. A refreshed BootROM task keeps its initial generation.
        // An explicit detection starts a new round after terminal tasks. Active
        // and recovery tasks still own the endpoint and must be restored first.
        if (await loadHistory(selected, seq, null, fresh)) return;
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
          try { if (await loadHistory(selected, seq, null, fresh)) return; } catch (_) { /* Keep the original endpoint error. */ }
        }
        if (same(selected, seq)) status(error.message);
      }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function discover() {
      if (busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      rememberImage(); var selected = board, seq = sequence;
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
        if (discovered.length === 1) await selectTarget(discovered[0], true);
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function drawStorageMap(parent, storage, writes, erases) {
      function extent(offset, size) {
        return Number.isSafeInteger(offset) && Number.isSafeInteger(size) && offset >= 0 && size > 0
          && Number.isSafeInteger(offset + size);
      }
      var partitions = (storage.partitions || []).filter(function (part) { return extent(part.offset, part.size); });
      var regions = writes.filter(function (write) {
        return write.kind !== 'layout' && extent(write.offset, write.write_size || write.size);
      });
      erases = erases.filter(function (range) { return extent(range.offset, range.size); });
      if (!partitions.length && !regions.length && !erases.length) return;
      var end = Math.max.apply(null, partitions.map(function (part) { return part.offset + part.size; })
        .concat(regions.map(function (write) { return write.offset + (write.write_size || write.size); }),
          erases.map(function (range) { return range.offset + range.size; })));
      var known = extent(0, storage.size) && storage.size >= end;
      var total = known ? storage.size : end;
      var box = text('div', '', parent); box.className = 'fl-map';
      var heading = text('div', '', box); heading.className = 'fl-map-heading';
      text('strong', storage.physical_device || storage.label || storage.id, heading).dataset.i18nIgnore = '';
      text('span', (known && storage.capacity_verified !== false ? '' : translate('范围参考') + ' · ') + format(total), heading).dataset.i18nIgnore = '';
      var track = text('div', '', box); track.className = 'fl-map-track';
      track.setAttribute('aria-hidden', 'true');
      var legend = text('ul', '', box); legend.className = 'fl-map-legend';
      function segment(kind, name, offset, size, label) {
        var block = text('span', '', track);
        block.dataset.kind = kind;
        block.style.left = (100 * offset / total) + '%';
        block.style.width = (100 * size / total) + '%';
        var range = format(offset) + '–' + format(offset + size);
        block.title = name + ' · ' + label + ' ' + format(size) + ' · ' + range;
        if (kind === 'partition' && size / total >= .12) {
          block.textContent = name;
          block.dataset.i18nIgnore = '';
        }
        var item = text('li', '', legend); item.dataset.kind = kind;
        text('strong', name, item).dataset.i18nIgnore = '';
        text('span', label + ' ' + format(size) + ' · ' + range, item).dataset.i18nIgnore = '';
      }
      partitions.forEach(function (part) {
        segment('partition', part.name || part.id, part.offset, part.size, translate('分区'));
      });
      regions.forEach(function (write) {
        if (extent(write.offset, write.capacity) && !partitions.some(function (part) {
          return part.offset === write.offset && part.size === write.capacity;
        })) segment('partition', write.partition || write.target, write.offset, write.capacity, translate('分区'));
      });
      erases.forEach(function (range) {
        segment('erase', translate('擦除范围'), range.offset, range.size, '');
      });
      regions.forEach(function (write) {
        var size = write.write_size || write.size;
        segment('write', write.partition || write.target || translate('完整磁盘'), write.offset, size, translate('将写入'));
      });
    }
    function drawWriteMaps(box, value) {
      var groups = new Map();
      function group(id, component) {
        var key = (component || '') + '/' + id;
        if (!groups.has(key)) groups.set(key, {storage:{id:id, label:[component, id].filter(Boolean).join(' · ')}, writes:[], erases:[]});
        return groups.get(key);
      }
      (value.storage_layouts || []).forEach(function (storage) { group(storage.id, storage.component).storage = storage; });
      (value.writes || []).forEach(function (write) { group(write.storage || write.storage_id, write.component).writes.push(write); });
      (value.erase_ranges || []).forEach(function (range) { group(range.storage, range.component).erases.push(range); });
      groups.forEach(function (entry) {
        if (entry.writes.length || entry.erases.length) drawStorageMap(box, entry.storage, entry.writes, entry.erases);
      });
    }
    function renderPlan(value) {
      if (value.reset_after) status('本次写入和所选校验成功后，将自动复位；复位请求不自动重发。');
      renderReplacementInfo(value);
      var box = $('fl-preview'); box.textContent = '';
      (value.components || []).forEach(function(component){text('p',component.label+' · '+component.name+' · '+(component.ready?'范围已核对':'加载临时环境后核对现场范围'),box).dataset.i18nIgnore='';});
      text('p', '回读校验：' + (value.verify === 'none' ? '不执行' : value.verify === 'required' ? '必须通过' : '能力支持时执行')
        + ' · 刷后复位：' + (value.reset_after ? '自动复位' : '保持当前模式'), box);
      if (value.physical_storage) text('p', translate('目标系统盘') + ' · ' + value.physical_storage, box).dataset.i18nIgnore='';
      if ((value.ram_loaders || []).length) {
        var source = {builtin:'DeckLab 内置',image:'镜像自带'}[value.loader_source];
        text('strong', translate('临时刷机环境（仅内存）') + (source ? ' · ' + translate(source) : ''), box).dataset.i18nIgnore = '';
        (value.ram_loaders || []).forEach(function (loader) {
          text('p', loader.name + ' · ' + format(loader.size) + ' · SHA-256 ' + loader.sha256, box).dataset.i18nIgnore = '';
        });
      }
      if (prepareOnly(value)) text('p', '准备阶段仅加载临时环境并读取现场布局，不写入存储。', box);
      else if (value.confirm_after_prepare && value.ready === false) text('p', '先加载临时环境并核对现场容量；完成后还需确认固件写入与擦除范围，才会写入存储。', box);
      else if (value.ready === false) text('p', '开始后先加载临时环境并核对现场，检查通过后按清单写入。', box);
      if (value.write_layout) text('p', '本计划将修改分区布局。', box);
      drawWriteMaps(box, value);
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
        if (!compact) cells.unshift((write.component ? write.component + ' · ' : '') + (write.storage || write.storage_id || '未知'));
        if (showRanges) cells.push(write.kind === 'layout' ? translate('MTD 映射 / env 擦除')
          : (write.offset === undefined ? '未知' : write.offset) + ' / ' + format(write.capacity || 0));
        if (!compact) cells.push(write.write_sha256 || write.sha256 || '未知');
        cells.forEach(function (item) { text('td', item, row); });
        table.appendChild(row);
        var found = (value.payload_metadata || []).find(function (item) {
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
      if (busy || !board || !$('fl-artifact').value || !imageReady()) return;
      var selected = board, seq = sequence, responseReceived = false;
      var preparing = preparationPending(), chosen = preparing || $('fl-mode').value === 'full' ? [] : selections();
      if (chosen.some(function (item) { return !item.artifact_id; })) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      invalidatePlan(); busy = true; previewTask = null; previewRequestId = id(); updateControls();
      operationReference(selected, previewRequestId);
      setProgress('checking-upload', 0, 0, true); status('正在核对镜像和现场布局…');
      try {
        var payload = Object.assign(scope(selected), {
          artifact_id: $('fl-artifact').value, mode: $('fl-mode').value, selections: chosen,
          binding_revision: capabilities.binding_revision, verify: $('fl-verify').value,
          reset_after: !!$('fl-reset-after').checked });
        if (payload.mode === 'full' && $('fl-target-storage').value) payload.target_storage = $('fl-target-storage').value;
        payload.request_id = previewRequestId;
        if ($('fl-mode').value === 'full' && $('fl-firmware').value) payload.firmware_artifact_id = $('fl-firmware').value;
        if (!$('fl-loader-field').hidden && $('fl-loader-source').value) payload.loader_source = $('fl-loader-source').value;
        var result = await client.request('/api/flash-plan', payload); responseReceived = true;
        if (result.task) result = await client.watch(result.task, {key:selected.key || '',serial:selected.serial || ''}, function (current) {
          if (!same(selected, seq)) return;
          previewUpdate(current);
          status('正在核对镜像和现场布局…');
        });
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
        previewRequestId = ''; setProgress('image-ready', 0, 0, false);
        renderPlan(value); status(prepareOnly(value) ? '需要先准备临时刷机环境，读取布局后再选择实际写入范围。'
          : '计划已准备，请核对板卡和全部写入范围后开始。');
      } catch (error) {
        if (same(selected, seq)) {
          if (!responseReceived && !error.result && !error.permanent) {
            submissionUnknown = true; status('预览提交结果未确认，请查询原任务；不会自动重新提交。', true);
          } else { previewRequestId = ''; status(error.message, true); }
          if (error.result && error.result.recovery_required && error.result.task_id) {
            try {
              var retained = await client.request('/api/task?id=' + encodeURIComponent(error.result.task_id) + query(selected));
              if (same(selected, seq)) observe(retained.task, selected, seq);
            } catch (readError) { if (same(selected, seq)) status(readError.message); }
          }
        }
      }
      finally { if (same(selected, seq)) { busy = false; previewTask = null; updateControls(); } }
    }
    function outcome(value) {
      if (value === undefined || value === null) return '未执行';
      var names = { 'not-started': '未开始', 'not-completed': '未完成', pending: '等待执行',
        skipped: '用户跳过，校验未完成',
        writing: '正在写入', written: '写入完成', completed: '已完成', verified: '校验通过',
        unsupported: '不支持回读校验', 'not-supported': '不支持回读校验',
        'not-requested': '未请求', 'reset-command-acknowledged': '复位已应答', unknown: '结果待确认', failed: '失败', 'partial-or-unknown': '可能部分写入，结果待确认',
        'unknown-or-partial': '写入结果未知或可能部分完成' };
      if (typeof value === 'string') return names[value] || value;
      return value.message || names[value.status] || value.status || JSON.stringify(value);
    }
    function renderTask(current) {
      if (!task || task.id !== current.id || task.state !== current.state
          || (task.progress || {}).phase !== (current.progress || {}).phase) status('');
      if (task && task.id !== current.id) { sourceInspection=null; sourceCatalogue=null; sourceAnalysedId=''; $('fl-package-contents').textContent=''; }
      task = current; $('flash-dialog').dataset.taskState = current.state;
      var p = current.progress || {}, result = current.result || {}, details = current.flash || {};
      if (!awaitingSelection(current)) previewProgress = null;
      renderBackups(); renderSystemInventory();
      if (workspaceSelection() && details.storage_inventory) renderStorages();
      if (details.write_inspection && details.write_inspection.storages && details.write_inspection.storages.some(function (item) { return item.physical_device; })) {
        sourceInspection=details.write_inspection; inspection=sourceInspection;
        if (!sourceCatalogue && details.plan) sourceCatalogue=details.plan;
        payloadCandidates=details.payload_candidates || []; renderPartitions();
      }
      var backupSignature = [details.nor_before && details.nor_before.artifact && details.nor_before.artifact.id,
        details.nor_after && details.nor_after.artifact && details.nor_after.artifact.id].join('/');
      if (renderedBackup !== backupSignature) {
        renderedBackup = backupSignature; renderPartitions();
      }
      if (awaitingSelection(current) && fullConfirmation() && details.prepared_plan && !selectionPlan) renderPlan(details.prepared_plan);
      if (!sourceInspection && awaitingSelection(current) && details.inspection && details.inspection !== inspection) {
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
      var remaining = Math.max(0, Math.ceil((details.selection_deadline || 0) - Date.now() / 1000));
      $('fl-task-status').textContent = selectionExpired(current) ? translate('确认已超时，本次未开始写入。请重新检测设备。') : current.error || (current.cancel_requested
        ? '已请求取消；等待执行器确认停止，可能已部分写入。'
        : awaitingSelection(current) && details.selection_deadline
          ? translate('确认剩余') + ' · ' + Math.floor(remaining / 60) + ':' + String(remaining % 60).padStart(2, '0')
          : board && board.observingTask && !['queued', 'running'].includes(current.state)
            ? '新刷写请先重新检测设备。' : '');
      updateControls();
      if (workspaceSelection() && !busy && !uploading && !sourceCatalogue && sourceAnalysedId !== sourceIdentity()
          && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; }))) { $('fl-target-storage').value=''; inspectSource(); }
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
    async function restoreTaskCapabilities(current, selected, seq) {
      try {
        // A prepared task retains its original generation. Read cached service
        // capabilities without probing USB or replacing the task's write scope.
        var result = await client.request('/api/flash-capabilities?target_id=' + encodeURIComponent(selected.target_id)
          + (selected.serial ? '&serial=' + encodeURIComponent(selected.serial) : ''));
        if (!same(selected, seq) || !task || task.id !== current.id || capabilities) return;
        capabilities = result.capabilities || result;
        // Do not renderCapabilities: it resets image, mode and partition choices.
        updateControls();
      } catch (error) {
        if (same(selected, seq) && task && task.id === current.id)
          status(translate('能力读取失败，请重新打开刷机面板：') + error.message, true);
      }
    }
    function observe(current, selected, seq) {
      if (!same(selected, seq)) return;
      var originalRequest = current.request_id || current.flash && current.flash.request_id;
      if (originalRequest) operationReference(selected, originalRequest);
      var restoring = !capabilities && (!task || task.id !== current.id) && selected.target_id;
      restoreTaskScope(current, selected);
      renderTask(current); loadEvents(selected, seq, current);
      if (restoring) restoreTaskCapabilities(current, selected, seq);
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
          || !capabilities || !capabilities.nor_workspace_supported
          || !capabilities.storage_inventory_supported && $('fl-mode').value !== 'partitions') return;
      var selected = board, seq = sequence;
      invalidatePlan(); requestId = id(); busy = true; updateControls();
      sourceInspection=null;sourceCatalogue=null;sourceAnalysedId='';
      status(capabilities.storage_inventory_supported ? '正在读取 NOR 和系统盘信息…' : '正在提交 NOR 读取与备份任务…');
      try {
        var payload = Object.assign(scope(selected), {
          kind: 'nor-backup', binding_revision: capabilities.binding_revision, request_id: requestId });
        if (capabilities.storage_inventory_supported) payload.read_system = true;
        var result = await client.request('/api/task-start', payload);
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
        operationReference(selected, requestId);
        var result = await client.request('/api/task-start', Object.assign(scope(selected), {
          kind: 'flash', plan_id: fixed.id, binding_revision: fixed.binding_revision === undefined
            ? capabilities.binding_revision : fixed.binding_revision, request_id: requestId }));
        if (same(selected, seq)) { operationReference(selected, null); plan = null; previewProgress = null; observe(result.task, selected, seq); }
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
      if (previewRequestId) {
        try {
          var original = await client.request('/api/task?request_id=' + encodeURIComponent(previewRequestId) + query(selected));
          if (!same(selected, seq)) return;
          busy = true; submissionUnknown = false; updateControls();
          var recovered = await client.watch(original.task, {key:selected.key || '',serial:selected.serial || ''}, function (current) {
            if (!same(selected, seq)) return;
            previewUpdate(current);
          });
          if (!same(selected, seq)) return;
          if (recovered.plan && (recovered.plan.id || recovered.plan.plan_id)) {
            plan = recovered.plan; plan.id = plan.id || plan.plan_id; requestId = id(); renderPlan(plan);
            if (recovered.inspection) { inspection = recovered.inspection; renderPartitions(); renderStorages(); }
            previewRequestId = ''; status('计划已准备，请核对板卡和全部写入范围后开始。');
          }
        } catch (error) { if (same(selected, seq)) status(error.message, true); }
        finally { if (same(selected, seq)) { busy = false; previewTask = null; updateControls(); } }
        return;
      }
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
      if (busy || uploading || submissionUnknown || !task || !awaitingSelection(task) || !inspection) return;
      if (workspaceSelection() && !sourceCatalogue && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; }))) {
        var selectedBoard = board, seq = sequence, taskId = task.id;
        await inspectSource();
        if (!same(selectedBoard, seq) || !workspaceSelection() || task.id !== taskId || !sourceCatalogue) return;
      }
      var chosen = fullConfirmation() ? [] : workspaceSelection() && sourceCatalogue && $('fl-mode').value === 'full'
        ? payloadCandidates.filter(function (item, index, all) { return item.storage && item.partition && all.findIndex(function (other) { return other.storage === item.storage && other.partition === item.partition; }) === index; }).map(function (item) { return {storage:item.storage, partition:item.partition, artifact_id:$('fl-artifact').value}; }) : selections();
      if (!fullConfirmation() && (!chosen.length || chosen.some(function (entry) { return !entry.artifact_id; }))) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      var selected = board, seq = sequence, current = task;
      busy = true; updateControls(); status('正在提交分区选择…');
      try {
        var payload = Object.assign(scope(selected), { id: current.id,
          layout_digest: workspaceSelection() && sourceCatalogue ? current.flash.inspection.layout_digest : inspection.layout_digest || current.flash.layout_digest });
        if (!selectionPlan) {
          payload.selections = chosen; payload.preview = true;
          payload.reset_after = !!$('fl-reset-after').checked;
          if (workspaceSelection()) payload.verify = $('fl-verify').value;
          if (workspaceSelection() && sourceCatalogue) {
            payload.artifact_id = $('fl-artifact').value; payload.mode = 'partitions'; payload.verify = $('fl-verify').value;
            payload.plan_id = sourceCatalogue.id || sourceCatalogue.plan_id;
            if (sourceCatalogue.target_storage) payload.target_storage = sourceCatalogue.target_storage;
          }
        }
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
        if (same(selected, seq)) status(error.result ? error.message : '选择提交结果未确认；正在查询原任务，请勿创建新任务。', true);
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
        artifact.preparation_task = (downloadTask.result || {}).preparation_task;
        addSource(artifact);
        downloadRequest = null;
        status('镜像已下载并校验 SHA-256；请选择写入范围并预览。');
        if (workspaceSelection() && packageFile(artifact)) { $('fl-mode').value='full'; $('fl-target-storage').value=''; await inspectSource(); }
      } catch (error) {
        if (!same(selected, seq)) return;
        if (downloadTask && ['queued', 'running'].includes(downloadTask.state)) {
          status('下载任务仍在后端运行，正在重新查询原任务…');
          downloadTimer = setTimeout(function () { watchDownload(selected, seq, downloadTask); }, 1500);
        } else { busy = false; uploading = false; downloadRetry = true; updateControls(); status(error.message); }
      }
    }
    async function downloadImage(automatic) {
      if (!board || busy || uploading || $('fl-download').disabled) return;
      var selected = board, seq = sequence, url = $('fl-url').value.trim(), sha = $('fl-url-sha256').value.trim().toLowerCase();
      if (!url) { status('请输入镜像 URL。'); return; }
      var identity = url + '\n' + sha + '\n' + JSON.stringify(scope(selected));
      if (automatic && automaticDownloadIdentity === identity && !downloadRetry) return;
      invalidatePlan();
      if (!/^https?:\/\/[^\s]+$/i.test(url)) { status('请输入有效的 HTTP 或 HTTPS 镜像链接。', true); return; }
      if (sha && !/^[a-f0-9]{64}$/.test(sha)) { status('SHA-256 必须为 64 位十六进制字符。', true); return; }
      automaticDownloadIdentity = identity;
      if (!downloadRequest || downloadRequest.identity !== identity) downloadRequest = { identity: identity, id: id() };
      downloadRetry = false; busy = true; uploading = true; updateControls(); status('正在提交后端下载任务…');
      try {
        var payload = Object.assign(scope(selected), {
          url: url, sha256: sha, request_id: downloadRequest.id, client_id: options.clientId || '' });
        if (task && awaitingSelection(task) && selected.target_id) payload.task_id = task.id;
        var result = await client.request('/api/image-download', payload);
        if (!same(selected, seq)) return;
        downloadTask = result.task; downloadReference(selected, downloadTask.id); updateControls();
        watchDownload(selected, seq, downloadTask);
      } catch (error) {
        if (same(selected, seq)) { busy = false; uploading = false; downloadRetry = true; updateControls(); status(error.message + '；重试将查询原请求。'); }
      }
    }
    async function upload() {
      if (busy || !board || $('fl-files').disabled) return;
      var files = Array.from($('fl-files').files || []);
      if (!files.length) { status('请选择要上传的镜像文件。'); return; }
      var selected = board, seq = sequence;
      busy = true; uploading = true; invalidatePlan(); updateControls();
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
            var sealedResult = await client.request('/api/artifact-seal', Object.assign(scope(selected), { id: artifact.id, prepare_image: true }));
            var completed = sealedResult.artifact; completed.preparation_task = sealedResult.preparation_task;
            if (completed.size !== file.size || !/^[a-f0-9]{64}$/.test(completed.sha256)) throw new Error('暂存文件校验失败');
            sealed = true;
            if (same(selected, seq)) artifacts.push(completed);
          } finally {
            if (artifact && !sealed) await client.request('/api/artifact-delete', Object.assign(scope(selected), { id: artifact.id })).catch(function () {});
          }
        }
        if (same(selected, seq)) {
          if (files.length === 1) addSource(artifacts[artifacts.length - 1]);
          else {
            var uploaded = artifacts.slice(-files.length), firmware = uploaded.find(function (item) { return /(?:^|[-_])firmware(?:[-_.]|$)/i.test(item.name); });
            var os = uploaded.find(function (item) { return item !== firmware && packageFile(item); });
            if (firmware && os && uploaded.length === 2) { os.firmware = firmware; addSource(os); }
            else { addSource(uploaded[uploaded.length-1]); uploaded.forEach(function (item) { prepareSource(item,item.preparation_task); }); }
          }
          status(task && ['queued', 'running'].indexOf(task.state) >= 0 && awaitingSelection(task)
            ? (norContext() ? '' : '载荷已准备，请在原任务选择实际分区并预览。')
            : preparationPending()
            ? '镜像已准备；请预览仅加载临时环境和读取布局的准备步骤。'
            : capabilities && capabilities.supported === false
            ? '镜像已准备；设备尚未提供刷机所需能力：' + capabilityReason()
            : '镜像已准备，服务端已计算 SHA-256；请生成写入预览。');
        }
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) {
        $('fl-files').value = ''; busy = false; uploading = false; updateControls();
        if (files.length === 1 && workspaceSelection() && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; }))) {
          $('fl-mode').value='full'; $('fl-target-storage').value=''; await inspectSource();
        }
      } }
    }
    async function open(selected, existing) {
      if (uploading) { $('flash-dialog').show(); return; }
      var identity = function (value) { return value && (value.target_id || value.serial || value.key); };
      if (!existing && board && (!selected || identity(board) === identity(selected)
          && (!selected.target_generation || !board.target_generation || selected.target_generation === board.target_generation))
          && (busy || task || plan || submissionUnknown || selectedPreparation())) { $('flash-dialog').show(); return; }
      sourceInspection = null; sourceCatalogue = null; sourceAnalysedId=''; $('fl-package-contents').textContent='';
      if (capabilities) rememberImage();
      board = selected || {}; selected = board; var seq = ++sequence; clearTimeout(pollTimer);
      busy = false; previewTask = null; previewRequestId = ''; capabilities = null; artifacts = []; task = null; $('flash-dialog').dataset.taskState = ''; $('fl-execution').hidden = true; cursor = 0; eventLines = []; ++eventSequence;
      inspection = null; payloadCandidates = []; selectionRequestId = ''; selectionPlan = null; renderedBackup = '';
      downloadRetry = false; automaticDownloadIdentity = '';
      $('fl-files').value = ''; $('fl-url').value = ''; $('fl-url-sha256').value = '';
      $('fl-url-panel').hidden = true; $('fl-url-toggle').setAttribute('aria-expanded', 'false');
      $('fl-target-storage').value = ''; $('fl-recovery-reason').value = '';
      invalidatePlan(); $('fl-task-id').textContent = ''; $('fl-events').textContent = '';
      $('fl-event-status').textContent = ''; $('fl-results').textContent = ''; $('fl-task-status').textContent = '';
      bootPriorityRequest = null; $('fl-boot-config').hidden = true;
      $('fl-storage-details').open = false;
      $('fl-system').hidden = true; $('fl-system').dataset.signature = ''; $('fl-system-storage').value = ''; $('fl-system-parts').textContent = '';
      $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      $('fl-board').textContent = selected.target_id ? targetName(selected)
        : selected.key ? (selected.label || selected.key) + ' · SN ' + (selected.serial || '未知') : translate('自动检测本机 USB 下载设备');
      setProgress('', 0, 0, false); renderBackups();
      discovered = []; renderDiscovered();
      $('flash-dialog').show(); status('正在读取刷机能力…');
      if (selected.target_id && existing) {
        if (existing.kind === 'flash-preview') await resumePreview(existing, selected, seq);
        else observe(existing, selected, seq);
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
    $('fl-image-recover').addEventListener('click', async function () {
      var current = selectedPreparation(), reason = $('fl-image-recovery-reason').value.trim();
      if (!current || !current.flash || !$('fl-image-stopped').checked || !reason) {
        status('请先确认原准备进程已停止，并填写依据。', true); return;
      }
      try {
        var result = await client.request('/api/image-prepare', {action:'recover', task_id:current.id,
          attempt_id:current.flash.attempt_id, confirm_stopped:true, reason:reason});
        imagePreparations[$('fl-artifact').value] = result.task;
        $('fl-image-stopped').checked = false; $('fl-image-recovery-reason').value = '';
        renderPreparation(); updateControls();
      } catch (error) { status(error.message, true); }
    });
    $('fl-image-preparation-retry').addEventListener('click', function () {
      var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      prepareSource(source, null, true);
    });
    $('fl-image-preparation-cancel').addEventListener('click', async function () {
      var current = selectedPreparation();
      if (!current || !current.id) return;
      try { await client.request('/api/task-cancel', {id:current.id}); }
      catch (error) { status(error.message, true); }
    });
    $('fl-firmware-choose').addEventListener('click',function(){
      if($('fl-firmware-choose').disabled)return;
      var selected=board,seq=sequence,mainId=$('fl-artifact').value;
      options.openImageStore(function(firmware){
        if(!same(selected,seq)||mainId!==$('fl-artifact').value)throw new Error('当前镜像或板卡已变化，请重新选择配套固件。');
        artifacts=artifacts.filter(function(item){return item.id!==firmware.id;}).concat([firmware]);
        renderFirmwareOptions(firmware.id);invalidatePlan();rememberImage();prepareSource(firmware,firmware.preparation_task);updateControls();
      },{source:'public',firmwareOnly:true,serial:selected.serial||''});
    });
    $('fl-firmware-retry').addEventListener('click',function(){var source=artifacts.find(function(item){return item.id===$('fl-firmware').value;});if(source)prepareSource(source,null,true);});
    $('fl-firmware').addEventListener('change', function () {
      invalidatePlan(); rememberImage();
      var source = artifacts.find(function (item) { return item.id === $('fl-firmware').value; });
      var main=artifacts.find(function(item){return item.id===$('fl-artifact').value;});
      if(main){if(source)main.firmware=source;else delete main.firmware;}
      if (source) prepareSource(source,source.preparation_task); updateControls();
    });
    function openWorkspace(empty) {
      if (!options.openImageWorkspace) return;
      var selected=board,seq=sequence,firmware=artifacts.find(function(item){return item.id===$('fl-firmware').value;});
      options.openImageWorkspace(empty ? null : artifacts.find(function(item){return item.id===$('fl-artifact').value;}),function(artifact){
        if (!same(selected,seq)) throw new Error('板卡已变化，请从已下载重新选择镜像目录。');
        if (firmware) artifact.firmware=firmware; addSource(artifact);
      });
    }
    $('fl-directory').addEventListener('click',function(){openWorkspace(true);});
    $('fl-workspace').addEventListener('click',function(){openWorkspace(false);});
    $('fl-read-nor').addEventListener('click', readNOR);
    $('fl-system-storage').addEventListener('change', renderSystemInventory);
    $('fl-boot-priority').addEventListener('change', updateBootControls);
    $('fl-boot-save').addEventListener('click', saveBootPriority);
    $('fl-close').addEventListener('click', function () { $('flash-dialog').close(); });
    $('fl-discover').addEventListener('click', discover);
    $('fl-restart').addEventListener('click', function () {
      if (!$('fl-restart').disabled && selectionExpired(task)) discover();
    });
    $('fl-device').addEventListener('change', function () {
      var target = discovered.find(function (entry) { return (entry.target_id || entry.id) === $('fl-device').value; });
      if (target) selectTarget(target, true);
      else if (!busy && !(task && ['queued', 'running'].indexOf(task.state) >= 0)) {
        board = {}; ++sequence; ++eventSequence; clearTimeout(pollTimer);
        task = null; capabilities = null; inspection = null; artifacts = []; payloadCandidates = [];
        invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
        $('fl-task-id').textContent = ''; $('fl-results').textContent = '';
        $('fl-board').textContent = translate('自动检测本机 USB 下载设备');
        status('选择下载模式设备');
      }
    });
    $('fl-mode').addEventListener('change', changeMode);
    $('fl-all-partitions').addEventListener('change', function () {
      if ($('fl-all-partitions').disabled) return;
      var checked = $('fl-all-partitions').checked;
      Array.from($('fl-partitions').children).forEach(function (row) {
        if (!row.checkbox || row.checkbox.disabled) return;
        row.checkbox.checked = checked;
        if (checked && !row.artifactSelect.value) {
          var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
          if (packageFile(source)) row.artifactSelect.value = source.id;
        }
      });
      invalidatePlan(); renderReplacementInfo(null);
    });
    $('fl-artifact').addEventListener('change', function () {
      var selected = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (selected) addSource(selected); else { renderPartitions(); invalidatePlan(); }
    });
    $('fl-delete').addEventListener('click', beginDelete);
    $('fl-delete-confirm').addEventListener('click', confirmDelete);
    $('fl-delete-cancel').addEventListener('click', dismissDelete);
    $('fl-verify').addEventListener('change', function () { rememberImage(); invalidatePlan(); if (workspaceSelection()) inspectSource(); });
    $('fl-loader-source').addEventListener('change', function () { rememberImage(); invalidatePlan(); });
    $('fl-reset-after').addEventListener('change', function () { rememberImage(); invalidatePlan(); });
    $('fl-target-storage').addEventListener('change', function () { invalidatePlan(); if (workspaceSelection()) inspectSource(); });
    $('fl-local').addEventListener('click', function () { if (!$('fl-local').disabled) $('fl-files').click(); });
    $('fl-files').addEventListener('change', upload); $('fl-plan').addEventListener('click', makePlan);
    function sourceIdentity() { return [task && task.id, $('fl-artifact').value, $('fl-target-storage').value, $('fl-verify').value].join('/'); }
    async function inspectSource() {
      var artifact = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (!workspaceSelection() || busy || uploading || !packageFile(artifact) || !imageReady()) return;
      sourceAnalysedId = sourceIdentity();
      var selected = board, seq = sequence, current = task, artifactId = artifact.id;
      var targetStorage = sourceInspection && !sourceInspection.storages.some(function (item) { return item.physical_device; })
        ? '' : $('fl-target-storage').value;
      // Keep the last observed storage kind while refreshing verification.
      // A NOR package must not inherit the system inventory's default disk.
      sourceCatalogue = null;
      $('fl-package-contents').textContent = '';
      busy = true; updateControls(); status('正在解析包内目标分区并核对板上布局…');
      var started = Date.now();
      setProgress('解析镜像包', 0, 0, true);
      var parsingTimer = setTimeout(function tick() {
        if (same(selected, seq) && busy) status('正在解压、校验并读取包内文件系统信息… 已用 ' + Math.floor((Date.now() - started) / 1000)
          + ' 秒；当前执行器不提供解压百分比，大镜像可能需要数分钟。');
        parsingTimer = setTimeout(tick, 1000);
      }, 1000);
      try {
        var payload = Object.assign(scope(selected), {id: current.id, preview: true,
          artifact_id: artifactId, mode: 'partitions', verify: $('fl-verify').value,
          selections: [], layout_digest: current.flash.inspection.layout_digest, reset_after: !!$('fl-reset-after').checked});
        if (targetStorage) payload.target_storage = targetStorage;
        var result = await client.request('/api/flash-selection', payload);
        if (!same(selected, seq) || !workspaceSelection() || task.id !== current.id
            || $('fl-artifact').value !== artifactId) return;
        if (!result.inspection || !Array.isArray(result.inspection.storages) || !result.plan)
          throw new Error('镜像目标布局响应不完整');
        sourceCatalogue = result.plan; sourceInspection = result.inspection; inspection = result.inspection;
        payloadCandidates = result.payload_candidates || [];
        renderPartitions(); renderStorages(); renderReplacementInfo(result.plan);
        var extra = null;
        (result.plan.catalogue || []).forEach(function (table) {
          var parent = $('fl-package-contents');
          if (!table.selected) {
            if (!extra) {
              extra = text('details', '', parent);
              text('summary', '附带分区表，不在本次刷机配置中', extra);
            }
            parent = extra;
          }
          var group = text('div', '', parent);
          var heading = text('strong', table.selected
            ? (table.format === 'mtd' ? 'NOR 目标分区' : '系统盘目标分区') : table.name, group);
          if (!table.selected) heading.dataset.i18nIgnore = '';
          text('p', (table.partitions || []).map(function (part) {
            return part.name + (part.payload ? ' ← ' + part.payload
              + (part.available === false ? '（' + translate('未附带载荷') + '）' : '') : '');
          }).join(' · '), group).dataset.i18nIgnore = '';
        });
        $('fl-verify').value = result.plan.verify || 'if-supported';
        status('已按刷机配置识别包内目标分区；整包方式无需逐项勾选，确认前仍需预览。');
      } catch (error) { if (same(selected, seq)) status(error.message, true); }
      finally {
        clearTimeout(parsingTimer);
        if (same(selected, seq)) setProgress('', 0, 0, false);
        if (same(selected, seq)) { busy = false; sourceAnalysedId = sourceIdentity(); updateControls(); }
      }
    }
    function chooseSource(configuration) {
      if (!board || $('fl-store').disabled || !options.openImageStore) return;
      var selected = board, seq = sequence, taskId = task && task.id;
      options.openImageStore(function (artifact) {
        if (!same(selected, seq) || (task && task.id) !== taskId || $('fl-store').disabled) throw new Error('当前设备或任务状态已变化，请重新选择。');
        addSource(artifact);
        if (workspaceSelection() && packageFile(artifact)) { $('fl-mode').value='full'; $('fl-target-storage').value=''; return inspectSource(); }
        if (norContext() && $('fl-mode').value === 'partitions' && !task) {
          status('镜像已选择；请先读取设备存储，再核对实际分区与替换内容。');
        } else if (task && task.flash && task.flash.operation === 'nor-workspace') {
          status('镜像已选择；请核对分区与替换内容。');
        } else if ($('fl-plan').disabled && !(task && awaitingSelection(task))) {
          status('镜像已选择；当前设备尚不能预览：' + capabilityReason(), true);
        } else status('已选择控制机缓存中的镜像；请核对写入范围并预览。');
      }, Object.assign({}, configuration, {serial:selected.serial || ''}));
    }
    $('fl-store').addEventListener('click', function () { chooseSource({source:'public'}); });
    $('fl-cache').addEventListener('click', function () { chooseSource({source:'local'}); });
    $('fl-url-toggle').addEventListener('click', function () {
      var panel = $('fl-url-panel'); panel.hidden = !panel.hidden;
      $('fl-url-toggle').setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
      if (!panel.hidden && $('fl-url').focus) $('fl-url').focus();
    });
    $('fl-download').addEventListener('click', function () { downloadImage(false); });
    $('fl-url').addEventListener('paste', function (event) {
      var value = event.clipboardData && event.clipboardData.getData('text');
      if (!value || $('fl-url').disabled) return;
      event.preventDefault(); $('fl-url').value = value.trim(); downloadImage(true);
    });
    $('fl-url').addEventListener('change', function () { if ($('fl-url').value.trim()) downloadImage(true); });
    $('fl-url').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); downloadImage(true); }
    });
    $('fl-download-cancel').addEventListener('click', async function () {
      if (!downloadTask) return;
      try { await client.request('/api/task-cancel', { id: downloadTask.id }); }
      catch (error) { status(error.message); }
    });
    $('fl-start').addEventListener('click', start); $('fl-recover').addEventListener('click', recover);
    $('fl-select').addEventListener('click', submitSelection);
    $('fl-recovery-reason').addEventListener('input', updateControls);
    $('fl-release').addEventListener('click', releaseRecovery);
    $('fl-skip-verify').addEventListener('click', async function () {
      if (!board || !task || $('fl-skip-verify').disabled) return;
      var selected = board, seq = sequence, current = task;
      busy = true; updateControls(); status('正在请求跳过剩余回读校验…');
      try {
        var result = await client.request('/api/flash-selection', Object.assign(scope(selected), {
          id: current.id, action: 'skip-verification'}));
        if (same(selected, seq) && task.id === current.id) observe(result.task, selected, seq);
      } catch (error) {
        if (same(selected, seq)) status(error.result ? error.message : '跳过请求结果未确认；将继续查询原任务。', true);
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    });
    $('fl-cancel').addEventListener('click', async function () {
      if (previewTask) {
        var previewId = previewTask.id;
        $('fl-cancel').disabled = true;
        try { await client.request('/api/task-cancel', {id:previewId}); }
        catch (error) { status(error.message); updateControls(); }
        return;
      }
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
      $('fl-system').dataset.signature = ''; renderSystemInventory();
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
