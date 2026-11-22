// Board-level flash planning and observation; no browser-owned hardware transport.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var board = null, capabilities = null, artifacts = [], plan = null, task = null, discovered = [];
    var flow = 'normal';
    var pageBoard = null, preparingBoard = false, downloadWiring = null, downloadControl = null;
    var sequence = 0, busy = false, uploading = false, requestId = '', cursor = 0;
    var selectionRequestId = '', selectionPlan = null, payloadCandidates = [], inspection = null;
    var eventSequence = 0, eventLines = [], pollTimer = null, submissionUnknown = false;
    var pendingDelete = null, sourceInspection = null, sourceCatalogue = null, sourceAnalysedId = '';
    var downloadTask = null, downloadTimer = null, downloadRequest = null, downloadRetry = false;
    var renderedBackup = '', automaticDownloadIdentity = '';
    var bootPriorityRequest = null;
    var progressSample = null, previewTask = null, previewRequestId = '';
    var previewProgress = null, preparingReviewTaskId = '', confirmedTaskId = '';
    var imagePreparations = Object.create(null), rememberedImage = null;
    try { rememberedImage = JSON.parse(global.sessionStorage && global.sessionStorage.getItem('decklab-flash-image') || 'null'); } catch (_) {}
    var writeOptions = {verify:'none', reset_after:true};
    var savedOptions = rememberedImage && rememberedImage.write_options;
    try {
      var preference = global.localStorage && global.localStorage.getItem('decklab.flash-options.v1');
      if (preference) savedOptions = JSON.parse(preference);
    } catch (_) {}
    if (savedOptions) {
      if (['none','if-supported','required'].includes(savedOptions.verify)) writeOptions.verify = savedOptions.verify;
      if (typeof savedOptions.reset_after === 'boolean') writeOptions.reset_after = savedOptions.reset_after;
    }
    $('fl-verify').value = writeOptions.verify;
    function rememberImage() {
      var source = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (!source) return;
      rememberedImage = source ? {id:source.id, mode:$('fl-mode').value, firmware:$('fl-firmware').value,
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
      return [$('fl-artifact').value, $('fl-mode').value === 'full' && !embeddedTable() && $('fl-firmware').value].filter(Boolean).every(function (artifactId) {
        return imagePreparations[artifactId] && imagePreparations[artifactId].state === 'completed';
      });
    }
    function firmwarePackage(item) { return item && /\.(?:tar\.gz|tgz|tar|zip|directory)$/i.test(item.name || ''); }
    function ramSource(item) {
      return item && /\.(?:tar\.gz|tgz|tar|zip)$/i.test(item.name || '') && !/[-_]firmware[-_.]/i.test(item.name);
    }
    function validStreamURL(value) {
      if (value.length > 8192 || /[\s\\]/.test(value) || !/^https?:\/\//i.test(value)) return false;
      try {
        var url = new URL(value);
        return !url.username && !url.password && !url.hash && /\.(?:tar\.gz|tgz|tar)$/i.test(url.pathname);
      } catch (_) { return false; }
    }
    function embeddedTable() {
      var value = $('fl-firmware').value || '';
      return value.startsWith('table:') ? value.slice(6) : '';
    }
    function setCompanionIntent(payload) {
      if (payload.mode !== 'full') return;
      if (embeddedTable()) payload.firmware_table = embeddedTable();
      else if ($('fl-firmware').value) payload.firmware_artifact_id = $('fl-firmware').value;
    }
    function embeddedChoices(job) {
      var tables = job && job.result && job.result.preparation && job.result.preparation.catalogue || [];
      if (!tables.some(t => t.format === 'gpt' && t.name === 'partition_universal.json')) return [];
      return tables.filter(t => t.format === 'mtd' && t.name !== 'partition_flash.json' &&
        t.partitions.some(p => p.name === 'bootinfo' && p.payload === 'factory/bootinfo_spinor.bin') &&
        t.partitions.every(p => p.available !== false));
    }
    function renderFirmwareOptions(selected) {
      var select = $('fl-firmware'), previous = selected === undefined ? select.value : selected;
      var embedded = embeddedChoices(selectedPreparation());
      select.textContent = ''; text('option', embedded.length ? '仅刷系统盘' : '仅刷当前镜像', select).value = '';
      embedded.forEach(function (table) {
        text('option', translate('当前 OS 包') + ' · ' + table.name, select).value = 'table:' + table.name;
      });
      artifacts.filter(function (item) { return item.id !== $('fl-artifact').value && firmwarePackage(item); }).forEach(function (item) {
        var option = text('option', item.name, select); option.value = item.id; option.dataset.i18nIgnore = '';
      });
      select.value = embedded.some(t => 'table:' + t.name === previous) || artifacts.some(function (item) { return item.id === previous && item.id !== $('fl-artifact').value; }) ? previous : '';
    }
    function renderPreparation() {
      var current = selectedPreparation(), box = $('fl-image-preparation');
      box.hidden = !current;
      if (!current) return;
      var catalogueKey = JSON.stringify([$('fl-artifact').value, current.id, current.state]);
      if ($('fl-firmware').dataset.catalogue !== catalogueKey) {
        $('fl-firmware').dataset.catalogue = catalogueKey;
        var saved = rememberedImage && rememberedImage.id === $('fl-artifact').value && rememberedImage.firmware;
        renderFirmwareOptions($('fl-firmware').value || saved || '');
        if (embeddedTable()) rememberImage();
      }
      var progress = current.progress || {}, active = ['queued','running'].includes(current.state);
      var directorySource = artifacts.find(function(item){return item.id===$('fl-artifact').value&&/\.directory$/i.test(item.name);});
      var phases = {'checking-image-sha256':'校验镜像 SHA-256', 'expanding-image':directorySource?'建立目录快照':'解压镜像',
        'checking-payloads':'校验包内文件', 'reading-payload-metadata':'读取文件系统与版本信息'};
      setMessage('fl-image-preparation-status', current.state === 'completed' ? '镜像已准备，可复用于其他板卡'
        : active ? (phases[progress.phase] || '等待准备镜像') : current.error || '镜像准备未完成');
      $('fl-image-error-copy').hidden = !current.error || active;
      box.dataset.state = current.state;
      var timing = active && current.flash && current.flash.preview_progress;
      $('fl-image-overall').hidden = !timing;
      var bar = $('fl-image-preparation-progress'); bar.hidden = !active;
      if (timing) {
        bar.max = 100;
        drawStageProgress(timing, phases, $('fl-image-stages'), bar, $('fl-image-overall-text'), '总体', true);
      } else {
        bar.max = progress.total || 1;
        if (progress.total) bar.value = Math.min(progress.done || 0, progress.total);
        else bar.removeAttribute('value');
      }
      $('fl-image-preparation-bytes').textContent = active && progress.total
        ? format(progress.done || 0) + ' / ' + format(progress.total)
          + (timing ? '' : ' · ' + Math.floor(100*(progress.done || 0)/progress.total) + '%') : '';
      var recovery = current.flash && current.flash.recovery_required;
      $('fl-image-recovery').hidden = !recovery;
      $('fl-image-preparation-retry').hidden = active || current.state === 'completed' || !!recovery;
      $('fl-image-preparation-cancel').hidden = !active || !current.id;
      $('fl-image-preparation-cancel').disabled = !!current.cancel_requested;
      var info = $('fl-image-information');
      var sources = [$('fl-artifact').value];
      if ($('fl-mode').value === 'full' && !embeddedTable() && $('fl-firmware').value) sources.push($('fl-firmware').value);
      var locked = busy || uploading || submissionUnknown || task && ['queued','running'].includes(task.state);
      var signature = JSON.stringify([!!locked, $('fl-mode').value, embeddedTable(), sources.map(function (id) {
        var job = imagePreparations[id] || {};
        return [id, job.id, job.state];
      })]);
      if (info.dataset.signature === signature) return;
      info.dataset.signature = signature; info.textContent = '';
      var groups = {mtd:[], gpt:[]};
      sources.forEach(function (id) {
        var job = imagePreparations[id], prepared = job && job.result && job.result.preparation;
        if (!prepared) return;
        var metadata = prepared.payload_metadata || [], tables = prepared.catalogue || [];
        tables.forEach(function (table) {
          if (groups[table.format] && table.name !== 'partition_flash.json') {
            groups[table.format].push({id:id, table:table, metadata:metadata, tables:tables, choice:prepared.table_choice});
          }
        });
        if (!tables.length && metadata.length) {
          var section = text('section', '', info);
          text('h4', '镜像分区', section);
          drawImageRows(section, metadata.map(function (item) { return {name:item.partition, content:item.content}; }));
        }
      });
      Object.keys(groups).forEach(function (medium) {
        var entries = groups[medium];
        if (medium === 'mtd' && $('fl-mode').value === 'full' && embeddedTable())
          entries = entries.filter(entry => entry.table.name === embeddedTable());
        if (!entries.length) return;
        var canonical = medium === 'mtd' ? 'partition_nor.json' : 'partition_universal.json';
        var active = entries.find(function (entry) { return entry.table.selected && entry.table.name === canonical; })
          || entries.find(function (entry) { return entry.table.selected; });
        var section = text('section', '', info), title = medium === 'mtd' ? 'NOR' : '系统盘（OS）';
        text('h4', title, section);
        var ordered = active ? [active].concat(entries.filter(function (entry) {return entry !== active;})) : entries;
        var choices = ordered.filter(function (entry, index, rows) {
          return !rows.slice(0,index).some(function (prev) {return prev.table.name === entry.table.name && prev.table.sha256 && prev.table.sha256 === entry.table.sha256;});
        });
        if (active && sources.length > 1) {
          var supplying = artifacts.find(function (item) { return item.id === active.id; });
          if (supplying) text('small', supplying.name, section).dataset.i18nIgnore = '';
        }
        var writable = active && active.table.name === canonical && $('fl-mode').value === 'full';
        var chosen = active && active.choice && active.choice.destination === active.table.name && active.choice;
        if (chosen) {
          choices = choices.filter(function (entry) { return entry === active || entry.table.name !== chosen.name || entry.table.sha256 !== active.table.sha256; });
          if (writable && chosen.original_id) {
            choices.splice(1, 0, {table:{name:canonical}, restoreId:chosen.original_id});
          }
        }
        var select;
        if (choices.length > 1) {
          var field = text('label', writable ? '刷写分区表' : '查看分区表', section);
          select = text('select', '', field);
          choices.forEach(function (entry, i) {
            var option = text('option', entry === active && chosen ? chosen.name : entry.table.name, select); option.value = String(i); option.dataset.i18nIgnore = '';
            if (!chosen && choices.some(function (other) { return other !== entry && other.table.name === entry.table.name; })) {
              var source = artifacts.find(function (item) { return item.id === entry.id; });
              option.textContent += ' · ' + (source ? source.name : entry.id);
            }
          });
          select.value = '0'; select.disabled = !!locked;
        } else if (choices[0].table.name) text('small', chosen ? chosen.name : choices[0].table.name, section).dataset.i18nIgnore = '';
        if (!active) text('small', '随包附带，本次配置未写入此介质', section);
        var contents = text('div', '', section);
        function show(entry) {
          contents.textContent = '';
          var partitions = entry.table.partitions || [];
          var rows = partitions.map(function (part) {
            var matches = entry.metadata.filter(function (item) { return item.partition === part.name; });
            var sameFiles = entry.tables.every(function (other) {
              return !(other.partitions || []).some(function (p) { return p.name === part.name && p.payload !== part.payload; });
            });
            var hit = part.sha256 ? matches.find(function (item) {return item.sha256 === part.sha256;})
              : sameFiles && matches.length === 1 && matches[0];
            return Object.assign({}, part, {content:part.available !== false && hit && hit.content});
          });
          var payloads = rows.filter(function (part) { return part.available !== false; }).map(function (part) {
            return {partition:part.name, offset:part.offset, size:part.payload_size || (part.content || {}).size};
          });
          drawStorageMap(contents, {partitions:partitions}, payloads, [], title, {image:true});
          drawImageRows(contents, rows);
        }
        show(choices[0]);
        if (select) select.addEventListener('change', function () {
          var choice = choices[Number(select.value)];
          if (choice.restoreId) {
            restoreTableRevision(active.id, choice.restoreId);
          } else if (writable && choice !== active) applyImageTable(active, choice);
          else show(choice);
        });
      });
      $('fl-image-information-details').hidden = !info.children.length;
    }
    async function restoreTableRevision(previousId, originalId) {
      if (busy || uploading) return;
      var seq = sequence, primary = $('fl-artifact').value, firmware = $('fl-firmware').value;
      busy = true; invalidatePlan();
      try {
        var original = artifacts.find(function (item) { return item.id === originalId; });
        if (!original) {
          var library = await client.request('/api/image-library');
          original = library.artifacts.find(function (item) { return item.id === originalId; });
        }
        if (seq !== sequence || primary !== $('fl-artifact').value || firmware !== $('fl-firmware').value) return;
        if (!original) throw new Error('原镜像已移除，请重新选择');
        useTableRevision(previousId, original);
      } catch (error) { if (seq === sequence) status(error.message); }
      finally { if (seq === sequence) { busy = false; updateControls(); } }
    }
    function useTableRevision(previousId, revised, preparation) {
      var primary = $('fl-artifact').value, firmware = $('fl-firmware').value;
      invalidatePlan();
      artifacts = artifacts.filter(function (item) {return item.id !== revised.id;}).concat([revised]);
      if (previousId === primary) {
        sourceCatalogue = null; sourceInspection = null; sourceAnalysedId = '';
        artifactOptions($('fl-artifact'), revised.id);
      }
      renderFirmwareOptions(previousId === firmware ? revised.id : firmware);
      rememberImage(); renderPartitions(); prepareSource(revised, preparation); updateControls();
      status('分区表已选择，请重新预览写入范围');
    }
    async function applyImageTable(active, choice) {
      if (busy || uploading) return;
      var seq = sequence, primary = $('fl-artifact').value, firmware = $('fl-firmware').value;
      busy = true; invalidatePlan(); status('正在准备所选分区表…');
      try {
        var response = await client.request('/api/image-workspace', {action:'select-table', id:active.id,
          path:active.table.name, table_id:choice.id, table:choice.table.name, request_id:id()});
        var result = await client.watch(response.task, {key:''}, function (job) {
          var p = job.progress || {};
          if (seq === sequence && p.total) status(translate('正在准备所选分区表…') + ' ' + format(p.done || 0) + ' / ' + format(p.total));
        });
        if (seq !== sequence || primary !== $('fl-artifact').value || firmware !== $('fl-firmware').value) return;
        useTableRevision(active.id, result.artifact, result.preparation_task);
      } catch (error) { if (seq === sequence) status(error.message); }
      finally { if (seq === sequence) { busy = false; updateControls(); } }
    }
    function drawImageRows(section, rows) {
      var table = text('table', '', section); table.className = 'fl-image-table';
      var head = text('tr', '', table);
      ['分区 / 文件', '载荷大小', '版本 / 文件系统'].forEach(function (label) { text('th', label, head); });
      rows.forEach(function (part) {
        var row = text('tr', '', table), file = text('td', '', row), content = part.content || {};
        text('strong', part.name || '镜像文件', file).dataset.i18nIgnore = '';
        if (part.payload) text('small', part.payload, file).dataset.i18nIgnore = '';
        if (part.remaining) text('small', '分区延伸到磁盘末尾', file);
        text('td', Number.isSafeInteger(part.payload_size) ? format(part.payload_size) : Number.isSafeInteger(content.size) ? format(content.size) : '—', row);
        var facts = text('td', '', row);
        if (part.available === false) { text('span', '未附带载荷', facts); return; }
        text('span', [content.version, content.filesystem && content.filesystem.type].filter(Boolean).join(' · ') || '—', facts).dataset.i18nIgnore = '';
        if (part.content) {
          var details = text('details', '', facts); text('summary', '详细信息', details);
          renderContent(text('div', '', details), content, '', true);
        }
      });
    }
    async function prepareSource(artifact, supplied, retry) {
      if (flow !== 'normal' || !artifact || capabilities && !capabilities.image_preparation_supported && !supplied) return;
      var previous = imagePreparations[artifact.id];
      if (!retry && previous) return;
      var marker = {state:'queued'}; imagePreparations[artifact.id] = marker;
      renderPreparation(); updateControls();
      try {
        var suppliedInfo = supplied && supplied.result && supplied.result.preparation;
        if (suppliedInfo && (suppliedInfo.catalogue || []).length && !suppliedInfo.layout_version) supplied = null;
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
    function retainedTask(current) {
      return ['queued','running'].includes(current.state) || !!(current.flash && current.flash.recovery_required);
    }
    function previewUpdate(current) {
      previewTask = current;
      previewProgress = ['queued','running'].includes(current.state) && current.flash && current.flash.preview_progress || null;
      updateControls();
      var p = current.progress || {};
      setProgress(p.phase || 'checking-image', p.done, p.total, ['queued','running'].includes(current.state));
      $('fl-task-details').hidden = false;
      if (!['queued','running'].includes(current.state)) $('fl-execution').hidden = true;
      $('fl-task-id').textContent = current.id + ' · ' + current.state;
      setMessage('fl-task-status', current.error || '');
    }
    function usePreview(result) {
      var value = result.plan || result;
      if (result.requires_prepare || value.requires_prepare) { value.requires_prepare = true; value.ready = false; }
      if (!(value.id || value.plan_id || result.plan_id)) throw new Error('刷机计划缺少写入范围，不能执行。');
      if (value.ready !== false && (!Array.isArray(value.writes) || !value.writes.length)) throw new Error('刷机计划缺少写入范围，不能执行。');
      if (value.mode) $('fl-mode').value = value.mode;
      if (value.loader_source) $('fl-loader-source').value = value.loader_source;
      if (value.firmware_table) renderFirmwareOptions('table:' + value.firmware_table);
      if(value.firmware_artifact_id){(value.components||[]).forEach(function(component){if(component.id&&!artifacts.some(function(item){return item.id===component.id;}))artifacts.push(component);});renderFirmwareOptions(value.firmware_artifact_id);}

      plan = value; plan.id = value.id || value.plan_id || result.plan_id; requestId = id();
      if (result.inspection) { inspection = result.inspection; renderPartitions(); renderStorages(); }
      previewRequestId = ''; previewProgress = null; setProgress('image-ready', 0, 0, false); renderPlan(plan);
      status('计划已准备，请核对板卡和全部写入范围后开始。');
    }
    async function resumePreview(current, selected, seq) {
      var continuePreview = ['queued','running'].includes(current.state) && current.request_id
        && operationReference(selected) === current.request_id;
      busy = true; previewRequestId = current.request_id || operationReference(selected) || '';
      var details = current.flash || {}, intent = details.preview_intent || {};
      if (details.preview_source) artifacts = [details.preview_source];
      if(details.preview_firmware)artifacts.push(details.preview_firmware);
      if (intent.mode) $('fl-mode').value = intent.mode;
      if (intent.verify) $('fl-verify').value = intent.verify;
      if (intent.loader_source) $('fl-loader-source').value = intent.loader_source;
      $('fl-reset-after').checked = !!intent.reset_after;
      if (intent.artifact_id) artifactOptions($('fl-artifact'), intent.artifact_id);
      if (intent.firmware_table) renderFirmwareOptions('table:' + intent.firmware_table);
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
        if (same(selected, seq)) {
          usePreview(result);
          if (continuePreview && needsDevicePreparation(plan, intent.mode)) await submitPlan(plan, true);
        }
      } catch (error) { if (same(selected, seq)) status(error.message, true); }
      finally { if (same(selected, seq)) { busy = false; previewTask = null; updateControls(); } }
    }
    // Replacing even identical text nodes collapses a user's selection during refresh.
    function setMessage(id, message) {
      var node = $(id);
      if (node.textContent !== message && node.textContent !== translate(message)) node.textContent = message;
    }
    function status(message, failed) { setMessage('fl-status', message); $('fl-status').dataset.error = failed ? 'true' : ''; }
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
      if (workspaceSelection()) inspectSource();
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
      var targets = current.flash_targets || [], adopted = false;
      // A USB-target task reached through its serial console must keep using
      // the task's own USB scope; the serial board may have no flash binding.
      if (!selected.target_id && targets.length === 1 && targets[0].target_id) {
        selected.serialIdentity = selected.serial || selected.key;
        selected.target_id = targets[0].target_id; adopted = true;
      }
      var snapshot = targets.find(function (target) {
        return target.target_id === selected.target_id;
      });
      if (!snapshot) return false;
      selected.target_generation = snapshot.generation || snapshot.target_generation;
      var serials = current.board_targets || [];
      selected.serial = current.flash && typeof current.flash.request_serial === 'string' ? current.flash.request_serial
        : serials.length === 1 ? (typeof serials[0] === 'string' ? serials[0] : serials[0].serial || '') : '';
      selected.observingTask = true;
      return adopted;
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
    function needsDevicePreparation(value, mode) {
      return mode === 'full' && value.requires_prepare === true && value.ready === false && value.confirm_after_prepare === true;
    }
    function fullConfirmation() { return task && task.flash && task.flash.confirmation_mode === 'full'; }
    function systemPlan(value) {
      return value && ((value.catalogue || []).some(function (table) { return table.selected && table.format === 'gpt'; })
        || (value.components || []).some(function (component) { return component.label === 'OS'; }));
    }
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
    function taskWriteOptions(current) {
      var details = current.flash || {};
      return (details.operation !== 'nor-workspace' && (details.prepared_plan || details.plan)) || writeOptions;
    }
    function renderStage(active, selecting) {
      var reviewing = selectionPlan || (selecting && fullConfirmation() && task.flash.prepared_plan) || plan;
      var flash = task && task.flash || {};
      var preparingReview = active && !selecting && task.id !== confirmedTaskId && !flash.selection_request
        && (!flash.write_result || flash.write_result === 'not-started')
        && (preparingReviewTaskId === task.id || flash.plan && flash.plan.requires_prepare && flash.plan.confirm_after_prepare);
      var reading = active && !selecting && (flash.operation === 'nor-workspace' || task.kind === 'nor-backup')
        && !flash.selection_request && (!flash.write_result || flash.write_result === 'not-started')
        && !/^(writing|verifying|resetting)/.test((task.progress || {}).phase || '');
      var readEnded = task && flash.operation === 'nor-workspace' && !active && !flash.selection_request
        && flash.write_result === 'not-started' && !flash.recovery_required;
      var stage = readEnded && reviewing ? 'review' : reading || preparingReview || readEnded ? 'setup' : task && !selecting ? 'run' : reviewing ? 'review' : 'setup';
      var dialog = $('flash-dialog');
      if (stage !== dialog.dataset.stage) {
        dialog.dataset.stage = stage;
        $('fl-stage-body').scrollTop = 0;
        ['setup', 'review', 'run'].forEach(function (name) {
          var step = $('fl-step-' + name);
          if (name === stage) step.setAttribute('aria-current', 'step');
          else step.removeAttribute('aria-current');
        });
      }
      $('fl-setup').hidden = stage !== 'setup';
      if (stage !== 'setup' || !busy && !previewTask && !(selecting && previewProgress && previewProgress.active))
        $('fl-preview-overall').hidden = true;
      if (preparingReview) {
        $('fl-preview-overall').hidden = false;
        $('fl-preview-phase').textContent = translate('准备设备并核对现场写入范围…');
        $('fl-overall-text').textContent = '';
        $('fl-overall-bar').removeAttribute('value');
        $('fl-stage-timing').hidden = true;
        $('fl-execution').hidden = true;
      }
      var readStatus = $('fl-read-status');
      readStatus.hidden = !reading;
      if ((reading || selecting && flash.operation === 'nor-workspace') && readStatus.dataset.task !== task.id) {
        $('fl-storage-details').open = true; readStatus.dataset.task = task.id;
      }
      if (reading) {
        readStatus.textContent = (task.progress || {}).phase ? $('fl-progress-text').textContent : '等待读取板上存储…';
        $('fl-execution').hidden = true;
      }
      var links = $('fl-backup-files');
      var slot = $(stage === 'run' ? 'fl-result-backup' : stage === 'review' ? 'fl-review-backup' : 'fl-board-backup');
      if (links.parentNode !== slot) slot.appendChild(links);
      var keep = $('fl-keep-backup');
      var candidate = selectionPlan || plan || flash.prepared_plan;
      var backupScope = task && task.id || candidate && (candidate.id || candidate.plan_id) || '';
      if (keep.dataset.task !== backupScope) {
        keep.dataset.task = backupScope; keep.checked = false;
      }
      $('fl-keep-field').hidden = !(stage === 'review' && (selecting && flash.temporary_snapshot && flash.nor_before
        || candidate && candidate.can_backup_nor && !flash.nor_before && capabilities && capabilities.nor_workspace_supported));
      keep.disabled = busy || submissionUnknown;
      $('fl-review').hidden = stage !== 'review' || !$('fl-preview').children.length;
      $('fl-confirm-options').hidden = flow === 'ram' || stage === 'run' || !!reading;
      if (previewTask && !['queued','running'].includes(previewTask.state) && !task) $('fl-execution').hidden = true;
      if (readEnded && stage === 'setup') $('fl-execution').hidden = true;
      if (stage === 'review' && !$('fl-review').hidden) $('fl-execution').hidden = true;
      $('fl-edit').hidden = stage !== 'review' || !((plan && !task) || (selectionPlan && !fullConfirmation()));
      $('fl-edit').disabled = busy || uploading || submissionUnknown;
      $('fl-new').hidden = stage !== 'run' || active || selectionExpired(task) || !!(task.flash && task.flash.recovery_required);
      $('fl-new').textContent = flow === 'ram' ? '再次内存启动' : '准备下一次刷写';
      $('fl-new').disabled = busy || uploading || submissionUnknown;
      $('fl-plan').hidden = flow !== 'normal' || stage !== 'setup' || !!selecting || preparingReview || !!previewTask || !!previewRequestId && busy;
      $('fl-start').hidden = stage !== 'review' || !plan || !!active;
      $('fl-fast-start').hidden = flow !== 'fast' || stage !== 'setup';
      $('fl-cancel').hidden = $('fl-cancel').disabled && !active && !previewTask;
      if (stage === 'run' && !active) {
        $('fl-execution').hidden = false;
        var state = {completed:'已完成', failed:'失败', cancelled:'已取消', interrupted:'已中断'}[task.state];
        $('fl-execution-title').textContent = translate('执行结果') + ' · ' + translate(state || task.state);
        var finishedWrite = task.kind === 'flash' && task.state === 'completed';
        if (finishedWrite) {
          var written = (task.result || {}).written_bytes ?? flash.written_bytes;
          $('fl-progress-text').textContent = Number.isSafeInteger(written) && written >= 0
            ? translate('本次写入') + ' · ' + format(written) : '';
        } else if (!(task.progress || {}).phase) $('fl-progress-text').textContent = '';
        $('fl-progress').hidden = finishedWrite || !(task.progress || {}).total;
      }
    }
    function updateControls() {
      var available = capabilities && capabilities.supported !== false && !(board && board.observingTask);
      var preparable = preparationPending();
      var uploadAvailable = board && !board.observingTask;
      var active = task && ['queued', 'running'].indexOf(task.state) !== -1;
      var selecting = active && awaitingSelection(task) && task.id !== confirmedTaskId && !(task.flash || {}).selection_request;
      var nor = norContext() && !(sourceInspection && sourceInspection.storages.some(function (item) { return item.physical_device; })), workspace = workspaceSelection();
      $('fl-all-label').textContent = nor ? '全选 NOR 分区' : '全选分区';
      var canResolve = pageBoard && pageBoard.serial && !(board && board.target_id && capabilities);
      var fast = !!(board && board.target_id && capabilities && capabilities.stream_flash_supported && !board.observingTask);
      $('fl-fast').hidden = flow !== 'fast';
      $('fl-target-picker').hidden = !!pageBoard;
      $('fl-connect').hidden = !!pageBoard && flow !== 'normal';
      $('fl-connection-details').hidden = !capabilities;
      var prepare = $('fl-prepare-board');
      prepare.hidden = flow !== 'normal' || !canResolve || !!active;
      prepare.disabled = busy || uploading || preparingBoard || submissionUnknown;
      prepare.textContent = downloadWiring && !downloadControl ? '进入下载模式并读取设备存储' : '读取设备存储';
      var tuning = !!(board && board.target_id && capabilities && capabilities.calibrate_supported && !board.observingTask);
      $('fl-tuning').hidden = !tuning || flow !== 'normal';
      $('fl-tuning-storage-field').hidden = !$('fl-tuning-write').checked;
      var tuneDisk = $('fl-tuning-storage').value.trim();
      $('fl-tuning-run').disabled = !tuning || busy || uploading || submissionUnknown || !!active;
      if ($('fl-tuning-write').checked && tuneDisk && tuneDisk !== 'auto' && !/^(nvme|scsi|mmc|usb):[0-3]$/.test(tuneDisk))
        $('fl-tuning-run').disabled = true;
      $('fl-tuning-forget').hidden = !(tuning && capabilities.flash_configuration && capabilities.flash_configuration.scope === 'port');
      $('fl-tuning-forget').disabled = $('fl-tuning-run').disabled;
      if (tuning) renderTuning(capabilities.flash_configuration);
      $('fl-fast-start').disabled = flow !== 'fast' || !(fast || canResolve) || busy || uploading || preparingBoard
        || submissionUnknown || !!task || !validStreamURL($('fl-fast-url').value.trim());
      $('fl-fast-url').disabled = $('fl-fast-storage').disabled = busy || preparingBoard || !!task || submissionUnknown;
      renderSource(); renderPreparation();
      var primary = artifacts.find(function(item){return item.id===$('fl-artifact').value;});
      var chip = (capabilities && capabilities.chip) || (board && board.chip);
      var selectedFirmware = $('fl-mode').value === 'full' && $('fl-firmware').value;
      var mismatched = artifacts.filter(function (item) {
        if ((chip !== 'k1' && chip !== 'k3') || (item !== primary && item.id !== selectedFirmware)) return false;
        var job = imagePreparations[item.id], families = job && job.result && job.result.preparation && job.result.preparation.chip_families;
        if (families && families.length) return !families.includes(chip);
        var declared = /^Debian-[0-9]+-[a-z0-9]+-(K[13])(?:-v[a-z0-9.]+)?(?:-[0-9]{8,14})?\.(?:tar\.gz|tgz|zip)$/i.exec(item.name);
        return declared && declared[1].toLowerCase() !== chip;
      });
      var chipWarning = $('fl-chip-warning');
      chipWarning.hidden = !mismatched.length;
      chipWarning.textContent = mismatched.length ? translate('设备型号') + ' ' + chip.toUpperCase() + ' · '
        + translate('请更换型号不匹配的镜像') + '：' + mismatched.map(function (item) { return item.name; }).join('、') : '';
      var loader = $('fl-loader-source'), loaderSources = preparable && capabilities.loader_sources || [];
      var choices = loaderSources.filter(function (source) { return source !== 'image' || !primary || packageFile(primary); });
      $('fl-loader-field').hidden = !loaderSources.length || !!active;
      Array.from(loader.children).forEach(function (option) { option.disabled = !choices.includes(option.value); });
      if (!choices.includes(loader.value)) loader.value = choices[0] || '';
      loader.disabled = busy || uploading || !!active || submissionUnknown || !choices.length;
      $('fl-firmware-field').hidden = $('fl-mode').value !== 'full' || !packageFile(primary) || /(?:^|[-_])firmware(?:[-_.]|$)/i.test(primary.name);
      var firmwareLocked = $('fl-firmware-field').hidden || busy || uploading || submissionUnknown || !!(!workspace && (active || board && board.observingTask));
      $('fl-firmware-local').disabled = $('fl-firmware').disabled = firmwareLocked;
      $('fl-firmware-choose').disabled = firmwareLocked || !options.openImageStore;
      $('fl-workspace').hidden = !$('fl-artifact').value;
      $('fl-workspace').disabled = busy || uploading || !!active || !options.openImageWorkspace || !(primary && /\.directory$/i.test(primary.name)) && (!capabilities || capabilities.image_preparation_supported) && !(selectedPreparation() && selectedPreparation().state === 'completed');
      $('fl-directory').disabled = busy || uploading || !!active || !options.openImageWorkspace;
      var firmwarePreparation = imagePreparations[$('fl-firmware').value];
      $('fl-firmware-retry').hidden = !firmwarePreparation || ['queued','running','completed'].includes(firmwarePreparation.state);
      $('fl-firmware-retry').disabled = busy || uploading || !!active;
      $('fl-firmware-status').textContent = !$('fl-firmware').value ? '' : embeddedTable() || firmwarePreparation && firmwarePreparation.state === 'completed' ? '配套固件已准备' : firmwarePreparation && ['failed','cancelled','interrupted'].includes(firmwarePreparation.state) ? firmwarePreparation.error || '配套固件准备失败' : '正在准备配套固件…';
      renderCurrentNOR();
      updateBootControls();
      $('fl-task-details').hidden = !task;
      $('fl-read-nor').disabled = busy || uploading || !!active || submissionUnknown || !!(board && board.observingTask)
        || !(capabilities && capabilities.nor_workspace_supported);
      $('fl-read-nor').hidden = !nor || !(capabilities && capabilities.storage_inventory_supported) && $('fl-mode').value !== 'partitions';
      $('fl-storage-details').hidden = $('fl-read-nor').hidden && $('fl-live-gpt').hidden && !(task && task.flash && (task.flash.storage_inventory || task.flash.nor_before || task.flash.nor_after || task.flash.operation === 'nor-workspace'));
      $('fl-read-nor').textContent = capabilities && capabilities.storage_inventory_supported ? '读取设备存储' : '读取并备份 NOR';
      if (active || busy || uploading) dismissDelete();
      $('fl-delete').disabled = busy || uploading || submissionUnknown || !!active || !!(board && board.observingTask)
        || !board || !artifacts.some(function (item) { return item.id === $('fl-artifact').value; });
      $('fl-delete').hidden = !$('fl-artifact').value;
      if (selecting) uploadAvailable = true;
      $('fl-device').disabled = busy || preparingBoard || submissionUnknown || !!active || uploading;
      $('fl-discover').disabled = busy || preparingBoard || submissionUnknown || !!active || uploading;
      ['fl-mode', 'fl-artifact', 'fl-files', 'fl-verify', 'fl-target-storage', 'fl-reset-after'].forEach(function (name) {
        $(name).disabled = busy || submissionUnknown || !!active;
      });
      $('fl-mode').disabled = busy || submissionUnknown || !!active && !workspace || !workspace && !!preparable && !(capabilities.modes || []).includes('full');
      if (selecting) $('fl-verify').disabled = busy || uploading || submissionUnknown;
      if (workspace) $('fl-target-storage').disabled = busy || uploading || submissionUnknown;
      $('fl-reset-after').disabled = busy || uploading || submissionUnknown || !!active && !selecting
        || (capabilities ? !capabilities.reset_after_supported : !!active);
      if (workspace) Array.from($('fl-mode').children).forEach(function (option) { option.disabled = false; });
      $('fl-mode').title = '';
      $('fl-local').disabled = busy || preparingBoard || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-files').disabled = $('fl-local').disabled;
      $('fl-store').disabled = $('fl-local').disabled;
      $('fl-cache').disabled = $('fl-local').disabled;
      $('fl-url-toggle').disabled = $('fl-local').disabled;
      $('fl-download').disabled = busy || uploading || submissionUnknown || !!active && !selecting || !uploadAvailable;
      $('fl-download').hidden = !downloadRetry;
      $('fl-url').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-url-sha256').disabled = busy || uploading || submissionUnknown || !!active && !selecting;
      $('fl-download-cancel').hidden = !downloadTask || !['queued', 'running'].includes(downloadTask.state);
      var ramBoot = $('fl-ram-boot');
      ramBoot.hidden = flow !== 'ram' || !!active || !!(task && task.kind === 'ram-boot');
      ramBoot.disabled = flow !== 'ram' || busy || uploading || preparingBoard || !!active || submissionUnknown
        || !(canResolve || capabilities && capabilities.ram_boot_supported) || !ramSource(primary) || !!mismatched.length;
      ramBoot.title = capabilities && capabilities.ram_boot_reason || '';
      $('fl-plan').disabled = busy || uploading || preparingBoard || !!active || !!(board && board.observingTask)
        || !(available || preparable) || !$('fl-artifact').value || !imageReady() || !!mismatched.length;
      if (nor && $('fl-mode').value === 'partitions' && !active) $('fl-plan').disabled = true;
      $('fl-plan').textContent = preparable ? ($('fl-mode').value === 'full' ? '准备设备并预览写入范围' : '预览刷机准备步骤') : '预览写入范围';
      $('fl-start').disabled = busy || uploading || !!active || !plan || submissionUnknown;
      $('fl-start').textContent = plan && plan.confirm_after_prepare && plan.ready === false
        ? (systemPlan(plan) ? '准备环境并核对写入范围' : '准备环境并核对固件范围')
        : prepareOnly(plan) ? '准备刷机环境并读取布局' : '开始刷写';
      $('fl-cancel').disabled = previewTask ? !['queued','running'].includes(previewTask.state) || previewTask.cancel_requested : !active || task.cancel_requested;
      $('fl-cancel').textContent = previewTask ? '取消预览' : active && task.kind === 'ram-boot' ? '取消内存启动' : nor && selecting ? '结束准备' : active && (task.flash || {}).operation === 'nor-workspace' && task.flash.write_result === 'not-started' ? '取消读取' : '取消刷机';
      var verifying = active && task.flash && task.flash.can_skip_verification === true
        && ['verifying','verifying-nor','verifying-system'].includes((task.progress || {}).phase);
      $('fl-skip-verify').hidden = !verifying;
      $('fl-skip-verify').disabled = !verifying || busy || task.cancel_requested || task.flash.verify_skip_requested === true;
      $('fl-skip-verify').textContent = task && task.flash && task.flash.verify_skip_requested ? '正在跳过校验…' : '跳过剩余校验';
      if (nor && selecting && !uploading) {
        var preview = task.flash && task.flash.preview_progress;
        previewProgress = preview && preview.active && !selectionPlan && preview.stages ? preview : null;
        if (previewProgress) setProgress(preview.phase, preview.done, preview.total, true);
        else setProgress('awaiting-selection', 0, 0, false);
      }
      $('fl-partitions-field').hidden = !sourceInspection && $('fl-mode').value !== 'partitions' && (!selecting || fullConfirmation());
      $('fl-target-storage-field').hidden = !capabilities && !workspace || (sourceInspection ? !sourceInspection.storages.some(function (item) { return item.physical_device; }) : $('fl-mode').value !== 'full' && !workspace);
      $('fl-select').hidden = !selecting;
      var needsCatalogue = workspace && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; })) && !sourceCatalogue;
      $('fl-select').disabled = busy || uploading || submissionUnknown || !selecting || !imageReady() || !!mismatched.length;
      var packageReview = (workspace && sourceCatalogue && $('fl-mode').value === 'full')
        || (fullConfirmation() && systemPlan(task.flash.prepared_plan));
      $('fl-select').textContent = needsCatalogue ? '解析镜像并预览写入范围' : packageReview ? (selectionPlan ? '确认包内全部目标分区并刷写' : '预览包内全部目标分区') : fullConfirmation() ? (selectionPlan ? '确认固件范围并刷写' : '预览固件写入与擦除范围') : selectionPlan ? (nor ? '确认分区并刷写' : '确认分区并继续') : '预览分区写入范围';
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
      renderStage(active, selecting);
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
      var signature = JSON.stringify([details.nor_before, details.nor_after, task && task.state, details.write_result, board && board.serial]);
      if (box.dataset.signature === signature) return;
      box.dataset.signature = signature; box.textContent = '';
      ['nor_before', 'nor_after'].forEach(function (key) {
        var backup = details[key], artifact = backup && backup.artifact;
        if (!artifact) return;
        if (key === 'nor_before' && details.temporary_snapshot && !details.retain_backup && !((details.selection_request || {}).retain_backup)
            && task && (task.state === 'completed' || ['cancelled','failed'].includes(task.state) && details.write_result === 'not-started')
            && !details.recovery_required) return;
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
      $('fl-backup-files').hidden = !box.children.length;
    }
    function renderCurrentNOR() {
      var box=$('fl-nor-current'), backup=task && task.flash && (task.flash.nor_after || task.flash.nor_before);
      var readback = task && task.flash && task.flash.nor_after;
      box.hidden = !backup;
      var signature = JSON.stringify([!!readback, backup && backup.artifact && backup.artifact.id]);
      if (box.dataset.signature === signature) return;
      box.dataset.signature = signature; box.textContent = '';
      if (!backup) return;
      drawStorageMap(box, {id:'NOR', size:backup.artifact && backup.artifact.size, partitions:backup.partitions}, [], [], '板上已有布局');
      var details = text('details', '', box);
      text('summary', readback ? 'NOR 写后回读内容' : 'NOR 分区与版本', details);
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
      var live = $('fl-live-gpt');
      live.textContent = '';
      if (!inventory && capabilities) (capabilities.storages || []).forEach(function (disk) {
        if (['gpt-header', 'primary-gpt-header'].includes(disk.capacity_source) && (disk.partitions || []).length)
          drawStorageMap(live, disk, [], [], '板上已有布局');
      });
      live.hidden = !live.children.length;
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
        : inventory.priority_source && inventory.priority_source.indexOf('eeprom:') === 0 ? translate('EEPROM 优先启动介质') + ' · ' + inventory.priority_source.slice(7) : '';
      updateBootControls();
      var parts = $('fl-system-parts'); parts.textContent = '';
      var disk = disks.find(function (item) { return item.id === select.value; });
      if (!disk) return;
      drawStorageMap(parts, disk, [], [], '板上已有布局');
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
    function beforeComparison(item) {
      var backup = task && task.flash && !task.flash.nor_after && task.flash.nor_before;
      var comparison = item.board_comparison;
      if (backup && comparison && comparison.phase === 'before'
          && comparison.backup_id === backup.artifact.id && comparison.backup_sha256 === backup.artifact.sha256
          && comparison.size === item.content.size && comparison.expected_sha256 === item.sha256) return comparison;
      return null;
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
        var comparison = applicable && beforeComparison(match);
        if (comparison && content) content = Object.assign({}, content, {payload_comparison:comparison});
        if (row.currentInfo) renderContent(row.currentInfo, content, '尚未读取', true, row.currentHashScope);
      });
    }
    function setProgress(phase, done, total, active, progress) {
      var names = {'checking-upload': '核对上传文件 SHA-256', 'checking-image-sha256': '核对镜像 SHA-256',
        'expanding-image': '解压镜像（压缩包读取）', 'checking-payloads': '核对包内载荷',
        'checking-layout': '核对现场布局', 'reading-payload-metadata': '读取分区文件系统信息', 'reading-nor': '读取整片 NOR', 'checking-nor-baseline': '检查备份后板上内容是否变化',
        'checking-image': '核对写入计划与载荷', 'verifying': '回读校验', 'verifying-nor': '回读整片 NOR 并校验', 'verifying-system': '回读所选系统盘分区', 'writing': '整体写入', 'resetting': '刷写完成，正在复位',
        'preparing-loader': '加载临时刷机器', 'prepare': '准备临时刷机器', 'calibrating': '实测刷写参数',
        'awaiting-selection': '等候核对写入范围', 'image-download': '后端下载镜像',
        'image-ready': '镜像已准备', 'uploading-image': '上传替换文件',
        'ramboot-identify': '核对板型与内存', 'ramboot-prepare': '准备临时内存镜像',
        'ramboot-transfer': '传送并核验内存载荷', 'ramboot-handoff': '交接给临时系统',
        'reading-system-layout': '读取系统盘分区表', 'reading-system-metadata': '读取系统盘版本信息'};
      var label = names[phase] || phase || '尚未开始', bar = $('fl-progress');
      bar.hidden = phase === 'awaiting-selection';
      $('fl-execution').hidden = !phase || phase === 'awaiting-selection';
      done = Math.max(0, Number(done) || 0); total = Math.max(0, Number(total) || 0);
      bar.max = Math.max(1, total);
      if (active && !total && phase !== 'awaiting-selection' && bar.removeAttribute) bar.removeAttribute('value');
      else bar.value = total ? Math.min(done, total) : 0;
      var counts = total ? ((phase === 'prepare' || phase === 'preparing-loader' || phase === 'calibrating')
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
      var partition = $('fl-write-partition');
      partition.hidden = !(active && phase === 'writing' && progress && progress.partition && progress.partition_total > 0);
      if (!partition.hidden) {
        var current = progress.partition_done, size = progress.partition_total;
        $('fl-write-target').textContent = [progress.component || progress.storage, progress.partition].filter(Boolean).join(' · ');
        var rate = progress.bytes_per_second > 0 ? translate('平均写入速率') + ' ' + format(progress.bytes_per_second) + '/s' : translate('测速中…');
        $('fl-write-count').textContent = format(current) + ' / ' + format(size) + ' · ' + Math.floor(100 * current / size) + '% · ' + rate;
        $('fl-partition-progress').max = size;
        $('fl-partition-progress').value = current;
        $('fl-partition-progress').setAttribute('aria-valuetext', $('fl-write-target').textContent + ' · ' + $('fl-write-count').textContent);
        partition.title = translate('按展开载荷字节计算');
      }
      bar.setAttribute('aria-valuetext', $('fl-progress-text').textContent);
      $('fl-execution-title').textContent = translate(previewProgress || previewRequestId ? '预览写入范围'
        : uploading ? '准备镜像与设备' : task && task.kind === 'nor-backup' && phase !== 'writing' && !/^verifying/.test(phase || '') && phase !== 'resetting'
          ? '读取设备存储' : flow === 'ram' ? '内存启动' : flow === 'fast' ? 'URL 极速刷机' : '刷写与校验');
      drawPreviewProgress(names);
    }
    function drawPreviewProgress(names) {
      var box = $('fl-preview-stages'), overall = $('fl-preview-overall'); box.textContent = '';
      var calculating = previewTask ? ['queued','running'].includes(previewTask.state) && !plan
        : !!previewRequestId && busy && !plan || !!(workspaceSelection() && previewProgress && previewProgress.active && !selectionPlan);
      overall.hidden = !calculating;
      if (!calculating) return;
      $('fl-execution').hidden = true;
      $('fl-preview-phase').textContent = $('fl-progress-text').textContent;
      $('fl-stage-timing').hidden = !(previewProgress && previewProgress.active);
      if ($('fl-stage-timing').hidden) {
        $('fl-overall-text').textContent = '';
        $('fl-overall-bar').removeAttribute('value');
        return;
      }
      drawStageProgress(previewProgress, names, box, $('fl-overall-bar'), $('fl-overall-text'), '整体预览进度');
    }
    function drawStageProgress(model, names, box, bar, labelNode, title, compact) {
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
        var name = translate(names[step.phase] || step.phase), state = translate(label);
        var row = text('li', name + (compact ? '' : ' · ' + state
          + (step.state === 'completed' ? ' · ' + Math.ceil(elapsed) + ' ' + translate('秒') : '')), box);
        if (compact) {
          row.setAttribute('aria-label', name + ' · ' + state);
          if (step.state === 'running') row.setAttribute('aria-current', 'step');
        }
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
      var diskView = ((inspection || capabilities || {}).storages || []).some(function (item) { return item.physical_device; });
      var after = task && task.flash && task.flash.nor_after;
      var existing = after || task && task.flash && task.flash.nor_before;
      $('fl-current-heading').textContent = after && !diskView ? '写后回读内容' : '板上现有内容';
      var layout = $('fl-current-layout'); layout.textContent = '';
      ((inspection || capabilities || {}).storages || []).forEach(function (storage) {
        drawStorageMap(layout, storage, [], [], sourceInspection && $('fl-mode').value === 'full' ? '整包拟写入布局' : '分区选择范围');
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
          var current = document.createElement('div'); current.className = 'fl-content'; current.dataset.label = translate(after && !storage.physical_device ? '写后回读内容' : '板上现有内容');
          var snapshot = existing;
          if (storage.physical_device) {
            var inventory = task && task.flash && task.flash.storage_inventory;
            snapshot = inventory && (inventory.storages || []).find(function (disk) { return disk.id === storage.physical_device; });
          }
          var old = snapshot && (snapshot.partitions || []).find(function (part) { return part.name === (partition.name || partition.id); });
          renderContent(current, old && old.content, '尚未读取', true, existing && !storage.physical_device ? 'partition' : undefined); row.appendChild(current);
          row.currentInfo = current; row.currentContent = old && old.content;
          row.currentHashScope = existing && !storage.physical_device ? 'partition' : undefined;
          var replacement = document.createElement('div'); replacement.className = 'fl-content'; replacement.dataset.label = translate('将写入的文件');
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
      text('option', '预览时核对目标磁盘', select).value = '';
      var inventory = workspaceSelection() && task.flash.storage_inventory;
      var stores = inventory ? (inventory.storages || []).slice() : (inspection || capabilities || {}).storages || [];
      var disks = $('fl-tuning-disks');
      disks.textContent = '';
      stores.forEach(function (storage) {
        var value = storage.physical_device || storage.id;
        if (/^(nvme|scsi|mmc|usb):[0-3]$/.test(value)) text('option', storage.label || value, disks).value = value;
      });
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
        'probe-protocol-failed': 'USB 数据传输失败，尚未读取芯片型号。请检查数据线和端口，重新进入下载模式后检测。',
        'download-capability-unavailable': '设备尚未提供下载能力，无法确认下载模式。请重新进入下载模式后检测。',
        'connection-generation-unavailable': '无法确认当前 USB 连接，请重新检测。',
        'layout-discovery-unavailable': '当前下载环境没有提供可识别的存储布局。',
        'live-layout-invalid': '现场存储布局校验失败，不能生成写入计划。',
        'live-gpt-unavailable': '尚未读取到有效的现场 GPT，不能指定分区刷写。'
      };
      var reason = capabilities && capabilities.reason;
      var message = translate(messages[reason] || reason || '此设备尚未配置刷机执行器。');
      if (['probe-protocol-failed', 'target-unavailable'].includes(reason) && capabilities.diagnostic) {
        message += ' (' + capabilities.diagnostic + ')';
      }
      return message;
    }
    function renderCapabilities() {
      var box = $('fl-connections'); box.textContent = '';
      if (board && /^(k1|k3)$/i.test(capabilities.chip)) {
        board.chip = capabilities.chip;
        if (board.target_id) $('fl-board').textContent = targetName(board);
      }
      [['板型', (board && board.chip || '').toUpperCase() || capabilities.profile || (capabilities.mode === 'unknown' ? '尚未识别' : '未配置')], ['复位控制', capabilities.reset_after_supported ? '支持刷后自动复位' : '未提供刷后自动复位'],
       ['串口观察', location(capabilities.console)], ['刷写位置', location(capabilities.executor)]].forEach(function (item) {
        text('dt', item[0], box); text('dd', item[1], box);
      });
      if (capabilities.diagnostic) {
        text('dt', '连接诊断', box);
        text('dd', capabilities.diagnostic, box).dataset.i18nIgnore = '';
      }
      var storages = capabilities.storages || [];
      renderSystemInventory();
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
      }
      $('fl-verify').value = writeOptions.verify;
      $('fl-reset-after').checked = capabilities.reset_after_supported === true && writeOptions.reset_after;
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
        $('fl-loader-source').value = saved.loader_source || '';
      }
      var retainedSource = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (retainedSource) prepareSource(retainedSource);
      $('fl-storage-details').open = $('fl-mode').value === 'partitions';
      status(flow === 'ram' ? ramImageStatus()
        : flow === 'fast' ? (capabilities.stream_flash_supported ? '' : '当前设备不支持 URL 极速刷机。')
        : capabilities.nor_workspace_supported ? ''
        : preparationPending() ? '已检测到 BootROM；上传刷机包后可准备临时环境并读取布局，此阶段不写存储。'
        : capabilities.supported === false ? capabilityReason()
        : capabilities.reason === 'live-gpt-unavailable' && modes.includes('full') ? '可选择整包刷写，由镜像重建 GPT。'
        : '选择镜像并预览写入范围；预览不会复位或写入存储。');
      updateControls();
    }
    function targetName(target) {
      var ports = target.usb && target.usb.port_path || target.device && target.device.port_path;
      var attachment = target.usb_location || (Array.isArray(ports) ? 'USB ' + ports.join('.') : '');
      var link = target.console_link || {};
      var serial = target.serial ? 'SN ' + target.serial
        : link.state === 'linked' && link.serial ? 'SN ' + link.serial + ' · ' + translate('串口已配对') : translate('序列号待识别');
      return [target.label || target.product || translate('下载模式设备'), /^(k1|k3)$/i.test(target.chip) && target.chip.toUpperCase(), serial,
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
      var reference = operationReference(selected);
      if (reference) {
        try {
          var awaitingReceipt = reference.startsWith('write/');
          var originalId = awaitingReceipt ? reference.slice(6) : reference;
          var original = await client.request('/api/task?request_id=' + encodeURIComponent(originalId) + query(selected));
          if (!same(selected, seq)) return true;
          if ((!fresh || retainedTask(original.task)) && original.task.kind === 'flash-preview' && (['queued','running'].includes(original.task.state)
              || Date.now()/1000-original.task.updated < 600)) {
            await resumePreview(original.task, selected, seq); return true;
          }
          if ((retainedTask(original.task) || !fresh && awaitingReceipt) && ['flash','flash-inspect','ram-boot'].includes(original.task.kind)) {
            observe(original.task, selected, seq); return true;
          }
        } catch (error) {
          if (!same(selected, seq)) return true;
          if (![403,404].includes(error.status)) { submissionUnknown = true; previewRequestId = originalId; status(error.message,true); updateControls(); return true; }
        }
        operationReference(selected, null);
      }
      var records = await client.request(taskQuery(selected, 100));
      if (!same(selected, seq)) return true;
      var current = (records.tasks || []).find(function (entry) {
        return ['flash','flash-inspect','flash-preview','ram-boot'].includes(entry.kind) && retainedTask(entry);
      });
      if (current) {
        if (current.kind === 'flash-preview') await resumePreview(current, selected, seq);
        else observe(current, selected, seq);
        return true;
      }
      return false;
    }
    async function selectTarget(target, fresh, probed) {
      if (!target || busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      var selected = { target_id: target.target_id || target.id, serialIdentity: pageBoard && pageBoard.serial,
        target_generation: target.target_generation || target.generation, label: target.label, serial: target.serial || '',
        present: target.present, chip: target.chip };
      rememberImage(); board = selected; var seq = ++sequence; clearTimeout(pollTimer); ++eventSequence;
      sourceInspection = null; sourceCatalogue = null; sourceAnalysedId=''; $('fl-package-contents').textContent='';
      busy = true; capabilities = null; task = null; inspection = null; payloadCandidates = [];
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
        var result = probed || await client.request('/api/flash-target', scope(selected));
        if (!same(selected, seq)) return;
        var resolved = result.target || target;
        if (pageBoard && linkedSerial(resolved) !== pageBoard.serial) {
          board = pageBoard; busy = false; updateControls();
          status('USB 设备身份已变化，请检查当前开发板的接线。', true); return;
        }
        selected.serial = resolved.serial || '';
        selected.target_generation = resolved.target_generation || resolved.generation || selected.target_generation;
        selected.label = resolved.label || selected.label;
        capabilities = result.capabilities || result; renderCapabilities();
        selected.chip = capabilities.chip || resolved.chip;
        Object.assign(selected, {mode:resolved.mode, usb_location:resolved.usb_location, usb:resolved.usb});
        discovered = discovered.map(function (item) { return (item.target_id || item.id) === selected.target_id ? Object.assign({}, item, {chip:selected.chip}) : item; });
        renderDiscovered();
        $('fl-board').textContent = targetName(Object.assign({}, resolved, {chip:selected.chip}));
        linkStatus(resolved.console_link);
      } catch (error) {
        if (same(selected, seq) && error.status === 423) {
          try { if (await loadHistory(selected, seq, null, fresh)) return; } catch (_) { /* Keep the original endpoint error. */ }
        }
        if (same(selected, seq)) status(error.message);
      }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    // The USB probe echoes a one-shot nonce on the board UART; only problems
    // need the operator, a clean pairing is shown in the target name.
    function linkStatus(link) {
      if (!link) return;
      var name = function (serial, label) { return (label ? label + ' · ' : '') + 'SN ' + serial; };
      if (link.state === 'mismatch') {
        status(translate('该 USB 口对应的串口记录为') + ' ' + name(link.serial, link.serial_label) + '，'
          + translate('USB 读到') + ' ' + name(link.usb_serial, link.usb_label) + '。'
          + translate('若已换板，请在该串口设备上点“准备换板”后重新检测。'), true);
      } else if (link.state === 'ambiguous') {
        status(translate('USB 配对暗号同时出现在多个串口上，未自动关联；请检查串口接线。'), true);
      } else if (link.state === 'bind-failed') {
        status(translate('已找到对应串口，但未能自动绑定：') + (link.reason || ''), true);
      }
    }
    function linkedSerial(target) {
      var link = target.console_link || {};
      if (link.state === 'mismatch' || link.state === 'ambiguous') return '';
      return target.serial || (link.state === 'linked' ? link.serial || '' : '');
    }
    async function flashTargets() {
      var result;
      try { result = await client.request('/api/flash-discover'); }
      catch (error) {
        if (error.result && Array.isArray(error.result.targets) && error.result.targets.length) result = error.result;
        else throw error;
      }
      var targets = result.targets || result.devices || [];
      if (!Array.isArray(targets)) throw new Error('下载设备列表格式无效。');
      return {targets: targets, reason: result.reason};
    }
    async function findBoardTarget(targets, context, attempted) {
      var candidates = targets.filter(function (target) { return target.present !== false; });
      function conflicting(target) {
        var link = target.console_link || {};
        return ['mismatch','ambiguous'].includes(link.state)
          && [target.serial, link.serial, link.usb_serial].includes(context.serial);
      }
      if (candidates.some(conflicting)) throw new Error(translate('USB 与串口身份冲突，请核对当前开发板接线。'));
      var matched = candidates.filter(function (target) { return linkedSerial(target) === context.serial; });
      if (matched.length === 1) return {target:matched[0]};
      if (matched.length > 1) throw new Error(translate('同一序列号对应多个 USB 设备，请检查接线。'));
      // BootROM may lack a serial in the USB descriptor. Probe identity only;
      // never infer board identity from the number of connected devices.
      for (var candidate of candidates) {
        if (pageBoard !== context || candidate.serial || candidate.console_link) continue;
        var targetId = candidate.target_id || candidate.id, generation = candidate.target_generation || candidate.generation;
        if (!targetId || !generation || attempted.has(generation)) continue;
        attempted.add(generation);
        var probe;
        try { probe = await client.request('/api/flash-target', scope({target_id:targetId, target_generation:generation})); }
        catch (error) { if (error.result && error.result.recovery_required) throw error; continue; }
        if (probe.target && conflicting(probe.target)) throw new Error(translate('USB 与串口身份冲突，请核对当前开发板接线。'));
        if (probe.target && linkedSerial(probe.target) === context.serial) return {target:probe.target, probe:probe};
      }
      return null;
    }
    function downloadScope(context) {
      return {key:context.key, serial:context.serial, connection_id:context.connection_id};
    }
    async function getDownloadWiring(context) {
      if (!context.connection_id) return null;
      var payload = downloadScope(context);
      var result = await client.request('/api/hardware?' + Object.keys(payload).map(function (key) {
        return key + '=' + encodeURIComponent(payload[key]);
      }).join('&'));
      return (result.actions || []).includes('enter-download') ? result.wiring || {} : null;
    }
    function manualDownloadMessage() {
      return '请让当前开发板进入下载模式，并接好 USB 刷机线。';
    }
    async function prepareDevice(operation) {
      if (board && board.target_id && capabilities) return true;
      if (!pageBoard) return !!capabilities;
      if (preparingBoard) return false;
      var context = pageBoard;
      preparingBoard = true; updateControls();
      try {
        // Recheck first so a board already in download mode is never reset.
        if (!await discover()) return false;
        if (task || flow !== operation || submissionUnknown) return false;
        if (board.target_id && capabilities) { downloadControl = null; return true; }
        if (!downloadWiring) throw new Error(translate(manualDownloadMessage()));
        if (downloadControl) throw new Error(translate('尚未核对到该板的 USB 身份，请检查原控制任务和刷机线。'));
        status('正在让当前开发板进入下载模式…');
        var details = Object.assign(downloadScope(context), {
          kind:'hardware-enter-download', actor:'web', wiring_revision:downloadWiring.revision || '', request_id:id()
        });
        downloadControl = details.request_id;
        try { await client.start(details, function (current) {
          if (!['queued','running','completed'].includes(current.state) && current.outcome === 'not-issued') downloadControl = null;
        }); }
        catch (error) {
          if (!error.submissionUnknown && error.status >= 400 && error.status < 500) downloadControl = null;
          throw error;
        }
        status('正在等待当前开发板的 USB 下载连接…');
        var attempted = new Set(), deadline = Date.now() + 20000;
        do {
          var result = await flashTargets();
          var match = await findBoardTarget(result.targets, context, attempted);
          if (match) {
            discovered = result.targets;
            await selectTarget(match.target, true, match.probe);
            if (board.target_id && capabilities && !task) {
              downloadControl = null;
              return flow === operation;
            }
            return false;
          }
          await new Promise(function (resolve) { setTimeout(resolve, 700); });
        } while (Date.now() < deadline);
        throw new Error(translate('尚未核对到该板的 USB 身份，请检查原控制任务和刷机线。'));
      } catch (error) {
        status(error.submissionUnknown ? '控制请求结果未确认，请先查看原任务。' : error.message, true);
        return false;
      } finally {
        preparingBoard = false; updateControls();
      }
    }
    async function discover() {
      if (busy || uploading || task && ['queued', 'running'].indexOf(task.state) >= 0) return;
      rememberImage();
      invalidatePlan();
      var selected = board = pageBoard || {}, seq = ++sequence;
      clearTimeout(pollTimer); ++eventSequence;
      busy = true; capabilities = null; task = null; inspection = null; payloadCandidates = [];
      if (pageBoard) downloadWiring = null;
      sourceInspection = null; sourceCatalogue = null; sourceAnalysedId = '';
      $('fl-package-contents').textContent = '';
      $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      renderStorages();
      $('fl-task-status').textContent = ''; $('fl-task-id').textContent = ''; $('fl-results').textContent = '';
      renderBackups(); renderSystemInventory(); setProgress('', 0, 0, false);
      $('fl-board').textContent = pageBoard ? pageBoardName() : translate('尚未选择刷机设备');
      $('fl-reset-after').checked = writeOptions.reset_after;
      updateControls(); status('正在检测下载模式设备…');
      try {
        var result = await flashTargets();
        if (!same(selected, seq)) return;
        discovered = result.targets;
        renderDiscovered(); $('fl-device').value = '';
        if (pageBoard) {
          var match = pageBoard.serial && await findBoardTarget(discovered, pageBoard, new Set());
          if (!same(selected, seq)) return;
          if (match) {
            busy = false; await selectTarget(match.target, true, match.probe); return !!(board.target_id && capabilities);
          }
          var wiring = pageBoard.serial && await getDownloadWiring(pageBoard).catch(function () { return null; });
          if (!same(selected, seq)) return;
          downloadWiring = wiring;
          status(wiring ? '可由 DeckLab 让当前开发板进入下载模式。' : manualDownloadMessage());
          return true;
        }
        if (!discovered.length) {
          status(result.reason || '未检测到下载模式设备，请检查 USB 数据线和下载模式。');
        } else if (discovered.length > 1) {
          status('检测到多个下载设备，请按 USB 位置选择本次刷写目标。');
        }
        busy = false; updateControls();
        if (discovered.length === 1) await selectTarget(discovered[0], true);
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function drawStorageMap(parent, storage, writes, erases, title, review) {
      function extent(offset, size) {
        return Number.isSafeInteger(offset) && offset >= 0 && Number.isSafeInteger(size) && size > 0 && Number.isSafeInteger(offset + size);
      }
      function parts(rows) {
        return (rows || []).filter(function (p) { return extent(p.offset, p.size); })
          .map(function (p) { return {name:p.name || p.id, offset:p.offset, size:p.size}; });
      }
      var partitions = parts(storage.partitions), payloads = [], lanes = [];
      writes.forEach(function (write) {
        if (write.kind === 'layout') return;
        var name = write.partition || write.target || translate('完整磁盘'), size = write.write_size || write.size;
        if (extent(write.offset, size)) payloads.push({name:name, offset:write.offset, size:size});
        if (review && review.replace && write.capacity === -1 && extent(write.offset, size)) {
          var remaining = storage.size > write.offset ? storage.size - write.offset : size;
          partitions.push({name:name, offset:write.offset, size:remaining, remaining:true});
        }
        if (extent(write.offset, write.capacity) && !partitions.some(function (p) { return p.offset === write.offset && p.size === write.capacity; }))
          partitions.push({name:name, offset:write.offset, size:write.capacity});
      });
      if (review && review.image) (storage.partitions || []).forEach(function (p) {
        if (!p.remaining || !extent(p.offset, 1)) return;
        var payload = payloads.find(function (w) { return w.name === p.name && w.offset === p.offset; });
        partitions.push({name:p.name, offset:p.offset, size:payload ? payload.size : 1, remaining:true});
      });
      function lane(label, kind, rows) {
        if (rows.length) lanes.push({label:label, kind:kind, rows:rows.sort(function (a,b) { return a.offset-b.offset; })});
      }
      if (review && review.replace) lane('原有分区', 'original', parts(review.before && review.before.partitions));
      if (!review || !review.replace) {
        var label = review && review.image ? '镜像分区' : storage.capacity_source === 'spacemit-ram-v1' ? '加载器分区映射'
          : storage.partitions && storage.partitions.length ? '现有分区' : '目标分区';
        lane(label, 'partition', partitions);
      }
      lane('擦除上限', 'erase', parts(erases));
      if (review && review.replace) lane('新分区', 'partition', partitions);
      lane(review && review.image ? '包内载荷' : '将写入', 'write', payloads);
      if (!lanes.length) return;
      var end = Math.max.apply(null, lanes.flatMap(function (l) { return l.rows.map(function (p) { return p.offset+p.size; }); }));
      var known = extent(0, storage.size) && storage.size >= end, total = known ? storage.size : end;
      var box = text('div', '', parent); box.className = 'fl-map';
      var heading = text('div', '', box); heading.className = 'fl-map-heading';
      text('strong', review ? title : translate(title) + ' · ' + (storage.physical_device || storage.label || storage.id), heading).dataset.i18nIgnore = '';
      text('span', (known && storage.capacity_verified !== false ? '' : translate('范围参考') + ' · ') + format(total), heading);
      var coordinates = document.createElement('details');
      if (review) {
        box.className += ' fl-map-review';
        if (review.source) text('small', review.source, box).className = 'fl-map-source';
        if (review.replace) text('p', review.before && (review.before.partitions || []).length ? '重写分区表' : '重写分区表 · 原布局未读取', box);
        text('summary', '查看范围坐标', coordinates);
      }
      lanes.forEach(function (l) {
        var row = text('div', '', box); row.className = 'fl-map-lane';
        if (review) text('span', l.label, row).className = 'fl-lane-label';
        var track = text('div', '', row); track.className = 'fl-map-track'; track.setAttribute('aria-hidden','true');
        if (review) text('strong', l.label, coordinates);
        var legend = text('ul', '', review ? coordinates : box); legend.className = 'fl-map-legend';
        var edges = new Set();
        l.rows.forEach(function (p) {
          var block = text('span', '', track); block.dataset.kind = l.kind;
          block.style.left = (100*p.offset/total) + '%'; block.style.width = (100*p.size/total) + '%';
          var range = format(p.offset) + '–' + (p.remaining ? translate('磁盘末尾') : format(p.offset+p.size));
          var sizeLabel = p.remaining ? translate('磁盘剩余空间') : format(p.size);
          block.title = [p.name, sizeLabel, range].filter(Boolean).join(' · ');
          if (p.name && l.kind !== 'erase') text('b', p.name, block).dataset.i18nIgnore = '';
          edges.add(p.offset); edges.add(p.offset+p.size);
          var item = text('li', '', legend); item.dataset.kind = l.kind; item.tabIndex = 0;
          function highlight(on) { block.dataset.highlight = on ? 'true' : 'false'; }
          item.addEventListener('mouseenter', function () { highlight(true); });
          item.addEventListener('mouseleave', function () { highlight(document.activeElement === item); });
          item.addEventListener('focus', function () { highlight(true); });
          item.addEventListener('blur', function () { highlight(false); });
          if (p.name) text('strong', p.name, item).dataset.i18nIgnore = '';
          text('span', sizeLabel + ' · ' + range, item).dataset.i18nIgnore = '';
        });
        edges.forEach(function (offset) {
          if (offset <= 0 || offset >= total) return;
          var line = text('i', '', track); line.className = 'fl-map-boundary'; line.style.left = (100*offset/total) + '%';
        });
      });
      if (review) box.appendChild(coordinates);
      return box;
    }
    function writeMetadata(value, write) {
      var matches = (value.payload_metadata || []).filter(function (item) {
        return item.content && (item.partition || '') === (write.partition || '')
          && (item.storage === (write.storage || write.storage_id) || write.component === 'Firmware' && item.storage === 'active')
          && (!item.sha256 || item.sha256 === write.sha256);
      });
      return matches.length === 1 ? matches[0] : null;
    }
    function drawWriteMaps(box, value) {
      var groups = new Map();
      function group(id, component) {
        var key = (component || '') + '/' + id;
        if (!groups.has(key)) groups.set(key, {storage:{id:id, component:component, label:[component, id].filter(Boolean).join(' · ')}, writes:[], erases:[]});
        return groups.get(key);
      }
      (value.storage_layouts || []).forEach(function (storage) { group(storage.id, storage.component).storage = storage; });
      (value.writes || []).forEach(function (write) { group(write.storage || write.storage_id, write.component).writes.push(write); });
      (value.erase_ranges || []).forEach(function (range) { group(range.storage, range.component).erases.push(range); });
      var observed = task && task.flash || {};
      var touchesNOR = false, touchesDisk = false;
      groups.forEach(function (entry) {
        if (!entry.writes.length && !entry.erases.length) return;
        var storage = entry.storage, writes = entry.writes;
        var layout = writes.find(function (w) { return w.kind === 'layout'; });
        var replace = !!layout || value.write_layout && groups.size === 1;
        // A replaced GPT cannot supply the extents of the future partitions.
        var after = Object.assign({}, storage);
        if (replace) after.partitions = [];
        var disk = storage.physical_device || (/^(scsi|mmc|nvme|usb):[0-9]+$/.test(storage.id) ? storage.id : value.physical_storage);
        var tableFormats = (value.catalogue || []).filter(function (table) { return table.selected; })
          .map(function (table) { return table.format; });
        var isNOR = storage.component === 'Firmware' || storage.id === 'NOR' || layout && layout.target === 'mtd'
          || !disk && (storage.capacity_source === 'spacemit-ram-v1'
            || groups.size === 1 && tableFormats.includes('mtd') && !tableFormats.includes('gpt'));
        var isDisk = storage.component === 'OS' || disk || layout && layout.target === 'gpt'
          || groups.size === 1 && tableFormats.includes('gpt');
        var medium = storage.id === 'active' ? translate('目标介质') : storage.label || storage.id;
        if (isNOR) { medium = 'NOR'; touchesNOR = true; }
        else if (isDisk) { medium = [translate('系统盘'),disk].filter(Boolean).join(' · '); touchesDisk = true; }
        var section = text('section', '', box); section.className = 'fl-projection';
        var oldDisk = (observed.storage_inventory && observed.storage_inventory.storages || []).find(function (item) { return item.id === disk; });
        if (!oldDisk && isDisk && !isNOR && capabilities) {
          oldDisk = (capabilities.storages || []).find(function (item) {
            return ['gpt-header', 'primary-gpt-header'].includes(item.capacity_source)
              && (disk ? item.id === disk || item.physical_device === disk : item.id === storage.id);
          });
        }
        var before = isNOR ? observed.nor_before : oldDisk;
        if (replace && oldDisk && !isNOR && !after.size) {
          after.size = oldDisk.size;
          after.capacity_verified = oldDisk.capacity_verified === true;
        }
        var component = (value.components || []).find(function (item) {
          return item.label === storage.component || !storage.component && item.label === (isNOR ? 'Firmware' : isDisk ? 'OS' : '');
        });
        var sourceName = component && [component.name, isNOR && value.firmware_table].filter(Boolean).join(' · ');
        if (!drawStorageMap(section, after, writes, entry.erases, medium,
            {replace:replace, before:before, source:sourceName})) {
          text('h4', medium, section);
          if (component) text('small', sourceName, section).className = 'fl-map-source';
          if (replace) text('p', '重写分区表', section);
        }
        var changed = writes.filter(function (w) { return w.kind !== 'layout'; });
        var partitionName = function (p) { return p.partition || p.target || p.partition_id || '完整磁盘'; };
        if (changed.length) {
          var table = text('table', '', section); table.className = 'fl-scope-table'; table.dataset.compact = 'true';
          var header = text('tr', '', table);
          ['分区','分区容量','写入量','写入版本'].forEach(function (label) { text('th', label, header); });
          changed.forEach(function (write) {
            var name = partitionName(write), next = writeMetadata(value, write);
            var old = ((before || {}).partitions || []).find(function (p) { return p.name === name; });
            var from = old && old.content && old.content.version, to = next && next.content.version;
            var existing = !replace && (storage.partitions || []).find(function (p) { return p.name === name; });
            var capacity = write.capacity || existing && existing.size;
            var bytes = write.write_size || write.size || write.bytes;
            var tr = text('tr', '', table);
            [name, capacity === -1 ? translate('磁盘剩余空间') : capacity > 0 ? format(capacity) : '—',
              Number.isSafeInteger(bytes) ? format(bytes) : '—', to && from && from !== to ? from + ' → ' + to : to || '—']
              .forEach(function (cell) { text('td', cell, tr).dataset.i18nIgnore = ''; });
          });
        }
        if (!replace) {
          var preserved = [], erased = [];
          (storage.partitions || []).forEach(function (p) {
            if (!Number.isSafeInteger(p.offset) || !(p.size > 0) || changed.some(function (w) { return partitionName(w) === p.name; })) return;
            function overlaps(offset, size) { return offset < p.offset + p.size && offset + size > p.offset; }
            if (entry.erases.some(function (r) { return overlaps(r.offset, r.size); })) erased.push(p.name);
            else if (changed.every(function (w) { return Number.isSafeInteger(w.offset) && (w.write_size || w.size) > 0 && !overlaps(w.offset, w.write_size || w.size); })) preserved.push(p.name);
          });
          if (erased.length) text('p', translate('涉及擦除') + ' · ' + erased.join('、'), section);
          if (preserved.length) text('p', translate('保留') + ' · ' + preserved.join('、'), section);
        }
      });
      if (touchesDisk && !touchesNOR && groups.size === 1)
        text('p', 'NOR · 本次不写入', box);
      if (touchesNOR && !touchesDisk && groups.size === 1)
        text('p', '系统盘 · 本次不写入', box);
    }
    function renderPlan(value) {
      if (value.reset_after) status('本次写入和所选校验成功后，将自动复位；复位请求不自动重发。');
      renderReplacementInfo(value);
      var box = $('fl-preview'); box.textContent = '';
      if (!(value.writes || []).length) (value.components || []).forEach(function (component) {
        text('p', component.label + ' · ' + component.name, box).dataset.i18nIgnore = '';
      });
      if ((value.ram_loaders || []).length) {
        var source = {builtin:'DeckLab 内置',image:'镜像自带'}[value.loader_source];
        var loaders = text('details', '', box); loaders.className = 'fl-details';
        text('summary', translate('临时刷机环境（仅内存）') + (source ? ' · ' + translate(source) : ''), loaders).dataset.i18nIgnore = '';
        (value.ram_loaders || []).forEach(function (loader) {
          text('p', loader.name + ' · ' + format(loader.size) + ' · SHA-256 ' + loader.sha256, loaders).dataset.i18nIgnore = '';
        });
      }
      if (prepareOnly(value)) text('p', '此步骤不写入存储。', box);
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
        if (showRanges) cells.push(write.kind === 'layout' ? translate(write.target === 'gpt' ? '覆盖系统盘 GPT' : 'MTD 映射 / env 擦除')
          : (write.offset === undefined ? '未知' : write.offset) + ' / ' + (write.capacity === -1 ? translate('磁盘剩余空间') : format(write.capacity || 0)));
        if (!compact) cells.push(write.write_sha256 || write.sha256 || '未知');
        cells.forEach(function (item) { text('td', item, row); });
        table.appendChild(row);
        var found = writeMetadata(value, write);
        if (found) {
          var infoRow = document.createElement('tr'), cell = document.createElement('td');
          cell.setAttribute('colspan', String(columns.length)); cell.className = 'fl-content fl-write-content';
          renderContent(cell, Object.assign({}, found.content, {sha256: '', payload_comparison:beforeComparison(found)}), '', true); infoRow.appendChild(cell); table.appendChild(infoRow);
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
      var list = text('details', '', box); list.className = 'fl-details';
      text('summary', '逐项写入明细', list); list.appendChild(table);
      if (compact) (value.writes || []).forEach(function (write) {
        text('p', (write.partition || write.target) + ' · ' + translate('写入范围 SHA-256'), list).dataset.i18nIgnore = '';
        text('small', write.write_sha256 || write.sha256 || '未知', list).className = 'fl-hash';
        if (write.write_sha256 && write.sha256 && write.write_sha256 !== write.sha256)
          text('p', '载荷 SHA-256: ' + write.sha256, list).dataset.i18nIgnore = '';
      });
      if ((value.erase_ranges || []).length) {
        text('strong', '擦除风险范围（保守上限）', list);
        value.erase_ranges.forEach(function (range) {
          text('p', range.storage + ' · ' + range.offset + ' + ' + format(range.size), list).dataset.i18nIgnore = '';
        });
      }
      var concise = {
        'NOR 固件恢复将建立包内 MTD 分区映射，并擦除 env；不能保留原环境设置。':'将清除 env 中的原设置。',
        '下载固件只提供 MTD 容量，未独立报告 NOR/NAND 类型；请核对当前板卡使用 NOR，不能用于 NAND。':'请确认板卡使用 NOR。',
        '擦除风险范围按整片 MTD 保守列出；不宣称只影响载荷字节。':'擦除可能涉及整片 NOR。',
        '将覆盖目标系统盘的 GPT，旧分区可能无法访问。确认新分区表后再写入包内系统分区。':'系统盘将重写 GPT，原分区可能无法访问。'
      };
      var risks = [], extras = [];
      (value.warnings || []).forEach(function (warning) {
        var msg = warning.replace(/^(Firmware|OS):\s*/, '');
        if (concise[msg]) risks.push(translate(concise[msg]));
        else if (msg === '本计划仅恢复 NOR 固件；包内可选 EC 升级不执行。') extras.push(msg);
        else risks.push(warning);
      });
      if (risks.length) text('p', Array.from(new Set(risks)).join(' '), box).className = 'fl-risk-summary';
      extras.forEach(function (msg) { text('small', msg, list); });

    }
    async function makePlan() {
      if (!board || $('fl-plan').disabled || $('fl-plan').hidden) return;
      var selected = board, seq = sequence, responseReceived = false;
      var preparing = preparationPending(), chosen = preparing || $('fl-mode').value === 'full' ? [] : selections();
      if (chosen.some(function (item) { return !item.artifact_id; })) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      invalidatePlan(); busy = true; previewTask = null; previewRequestId = id(); updateControls();
      operationReference(selected, previewRequestId);
      $('fl-stage-body').scrollTop = 0;
      setProgress('checking-upload', 0, 0, true); status('正在核对镜像和现场布局…');
      try {
        var payload = Object.assign(scope(selected), {
          artifact_id: $('fl-artifact').value, mode: $('fl-mode').value, selections: chosen,
          binding_revision: capabilities.binding_revision, verify: $('fl-verify').value,
          reset_after: !!(capabilities && capabilities.reset_after_supported && $('fl-reset-after').checked) });
        if (payload.mode === 'full' && $('fl-target-storage').value) payload.target_storage = $('fl-target-storage').value;
        payload.request_id = previewRequestId;
        setCompanionIntent(payload);
        if (!$('fl-loader-field').hidden && $('fl-loader-source').value) payload.loader_source = $('fl-loader-source').value;
        var result = await client.request('/api/flash-plan', payload); responseReceived = true;
        if (result.task) result = await client.watch(result.task, {key:selected.key || '',serial:selected.serial || ''}, function (current) {
          if (!same(selected, seq)) return;
          previewUpdate(current);
          status('正在核对镜像和现场布局…');
        });
        if (!same(selected, seq)) return;
        var value = result.plan || result;
        if (result.requires_prepare || value.requires_prepare) { value.requires_prepare = true; value.ready = false; }
        if (preparing && payload.mode === 'partitions' && (value.ready !== false || !Array.isArray(value.writes) || value.writes.length
            || value.write_layout !== false)) throw new Error('刷机准备计划包含存储写入，不能执行。');
        if (result.inspection && Array.isArray(result.inspection.storages)) {
          inspection = result.inspection; renderPartitions(); renderStorages();
        }
        if (!(value.id || value.plan_id || result.plan_id)
            || !prepareOnly(value) && (!Array.isArray(value.writes) || !value.writes.length))
          throw new Error('刷机计划缺少写入范围，不能执行。');
        plan = value; plan.id = value.id || value.plan_id || result.plan_id; requestId = id();
        previewRequestId = ''; previewProgress = null; setProgress('image-ready', 0, 0, false);
        if (needsDevicePreparation(plan, payload.mode)) {
          await submitPlan(plan, true);
          return;
        }
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
      if ((!task || task.id !== current.id) && capabilities) {
        var original = taskWriteOptions(current);
        $('fl-reset-after').checked = capabilities.reset_after_supported === true && original.reset_after === true;
        $('fl-verify').value = original.verify || writeOptions.verify;
      }
      task = current; $('flash-dialog').dataset.taskState = current.state;
      var p = current.progress || {}, result = current.result || {}, details = current.flash || {};
      if (details.operation === 'calibrate' && current.state === 'completed' && details.configuration && capabilities)
        capabilities.flash_configuration = details.configuration;
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
      if (!sourceInspection && awaitingSelection(current) && details.inspection && details.inspection !== inspection) {
        var changed = !inspection || (inspection.layout_digest || '') !== (details.inspection.layout_digest || '');
        inspection = details.inspection;
        payloadCandidates = Array.isArray(details.payload_candidates) ? details.payload_candidates : [];
        if (changed) { selectionRequestId = ''; selectionPlan = null; renderPartitions(); renderStorages(); }
      }
      if (current.state === 'running' && awaitingSelection(current) && !details.selection_request && current.id !== confirmedTaskId
          && fullConfirmation() && details.prepared_plan && !selectionPlan) {
        var checked = details.prepared_plan;
        if (checked.ready === true && (checked.id || checked.plan_id) && (checked.writes || []).length
            && inspection && inspection.layout_digest && (!checked.verify || checked.verify === $('fl-verify').value)
            && !!checked.reset_after === $('fl-reset-after').checked) selectionPlan = checked;
        renderPlan(checked);
      }
      if (!uploading && !(downloadTask && ['queued', 'running'].includes(downloadTask.state)))
        setProgress(p.phase, p.done, p.total, ['queued', 'running'].includes(current.state), p);
      $('fl-task-id').textContent = current.id + ' · ' + current.state + (p.phase ? ' · ' + p.phase : '');
      var box = $('fl-results'); box.textContent = '';
      if (current.kind === 'ram-boot' && result.handoff === 'acknowledged') {
        text('dt', '内存启动', box);
        text('dd', '临时加载器已确认启动命令，请在串口确认系统和根文件系统。', box);
      }
      var ramReceipt = result.ram_boot || details.ram_boot || {};
      if (current.kind === 'ram-boot' && Array.isArray(ramReceipt.limitations)
          && ramReceipt.limitations.includes('k1-bianbu-rcpu-disabled')) {
        text('dt', '本次内存启动限制', box);
        text('dd', '此 Bianbu 镜像在内存启动期间停用 RCPU（远程处理器）及依赖它的功能。', box);
      }
      [['写入结果', result.write_result || details.write_result], ['校验结果', result.verify_result || details.verify_result],
       ['启动结果', result.boot_result || details.boot_result]].forEach(function (item) {
        var state = typeof item[1] === 'string' ? item[1] : item[1] && item[1].status;
        if (item[0] === '校验结果' && state === 'not-requested' && current.kind === 'flash' && current.state === 'completed') {
          text('dt', item[0], box); text('dd', '未执行', box); return;
        }
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
      setMessage('fl-task-status', selectionExpired(current) ? translate('确认已超时，本次未开始写入。请重新检测设备。') : current.error || (current.cancel_requested
        ? '已请求取消；等待执行器确认停止，可能已部分写入。'
        : awaitingSelection(current) && details.selection_deadline
          ? translate('确认剩余') + ' · ' + Math.floor(remaining / 60) + ':' + String(remaining % 60).padStart(2, '0')
          : board && board.observingTask && !['queued', 'running'].includes(current.state)
            ? '新刷写请先重新检测设备。' : ''));
      updateControls();
      if (workspaceSelection() && !busy && !uploading && !sourceCatalogue && sourceAnalysedId !== sourceIdentity() && imageReady()
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
        // Restore the original full-plan choice when resuming its confirmation.
        var original = taskWriteOptions(current);
        $('fl-reset-after').checked = capabilities.reset_after_supported === true && original.reset_after === true;
        $('fl-verify').value = original.verify || writeOptions.verify;
        // Do not renderCapabilities: it resets image, mode and partition choices.
        updateControls();
      } catch (error) {
        if (same(selected, seq) && task && task.id === current.id)
          status(translate('能力读取失败，请重新打开刷机面板：') + error.message, true);
      }
    }
    function observe(current, selected, seq) {
      if (!same(selected, seq)) return;
      if (current.kind === 'ram-boot') applyFlow('ram');
      else if ((current.flash || {}).operation === 'stream') applyFlow('fast');
      var originalRequest = current.request_id || current.flash && current.flash.request_id;
      if (originalRequest) {
        if (retainedTask(current)) operationReference(selected, 'write/' + originalRequest);
        else if ($('flash-dialog').open && operationReference(selected) === 'write/' + originalRequest)
          operationReference(selected, null);
      }
      var restoring = !capabilities && (!task || task.id !== current.id) && selected.target_id;
      if (restoreTaskScope(current, selected)) {
        // Serial-board capabilities describe a different scope; reload the
        // task's USB target capabilities instead of reusing them.
        capabilities = null; restoring = true;
      }
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
          kind: 'nor-backup', temporary_snapshot:true, binding_revision: capabilities.binding_revision, request_id: requestId });
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
    async function startRamBoot() {
      if (!board || $('fl-ram-boot').disabled) return;
      var artifactId = $('fl-artifact').value;
      if (pageBoard && !(board.target_id && capabilities) && !await prepareDevice('ram')) return;
      if (!capabilities || !capabilities.ram_boot_supported || $('fl-ram-boot').disabled
          || artifactId !== $('fl-artifact').value) {
        status(ramImageStatus(), true); return;
      }
      var selected = board, seq = sequence;
      invalidatePlan(); requestId = id(); busy = true; updateControls();
      status('正在提交内存启动任务…');
      try {
        operationReference(selected, 'write/' + requestId);
        var result = await client.request('/api/task-start', Object.assign(scope(selected), {
          kind: 'ram-boot', artifact_id: artifactId, request_id: requestId,
          client_id: options.clientId || '', binding_revision: capabilities.binding_revision }));
        if (!same(selected, seq)) return;
        if (!result.task || !result.task.id) throw new Error('缺少内存启动任务回执');
        observe(result.task, selected, seq);
      } catch (error) {
        if (!same(selected, seq)) return;
        if (!error.result || error.status >= 500) {
          submissionUnknown = true;
          status('提交结果未确认；请查询原任务。');
        } else {
          operationReference(selected, null);
          status(error.message);
        }
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    async function start() {
      if (busy || !board || !plan || submissionUnknown) return;
      await submitPlan(plan, plan.ready === false && plan.confirm_after_prepare === true);
    }
    async function submitPlan(fixed, forReview) {
      var selected = board, seq = sequence;
      busy = true;
      if (forReview) requestId = 'prepare-' + fixed.id;
      else updateControls();
      status(forReview ? '准备设备并核对现场写入范围…' : '正在提交刷机任务…');
      try {
        operationReference(selected, 'write/' + requestId);
        var result = await client.request('/api/task-start', Object.assign(scope(selected), {
          kind: 'flash', ...(forReview ? {prepare_for_review:true} : {}), ...($('fl-mode').value === 'partitions' ? {temporary_snapshot:true} : {}),
          ...(!$('fl-keep-field').hidden && $('fl-keep-backup').checked ? {retain_backup:true} : {}), plan_id: fixed.id, binding_revision: fixed.binding_revision === undefined
            ? capabilities.binding_revision : fixed.binding_revision, request_id: requestId }));
        if (same(selected, seq)) {
          if (forReview) preparingReviewTaskId = result.task.id;
          plan = null; previewProgress = null;
          status(''); observe(result.task, selected, seq);
        }
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
      if (requestId && flow !== 'normal') {
        busy = true; updateControls();
        try {
          var found = await client.request('/api/task?request_id=' + encodeURIComponent(requestId) + query(selected));
          if (!same(selected, seq)) return;
          if (!found.task || (flow === 'ram' ? found.task.kind !== 'ram-boot' : (found.task.flash || {}).operation !== 'stream'))
            throw new Error(translate('任务响应与请求不匹配'));
          submissionUnknown = false; observe(found.task, selected, seq);
        } catch (error) { if (same(selected, seq)) status(error.message, true); }
        finally { if (same(selected, seq)) { busy = false; updateControls(); } }
        return;
      }
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
    async function submitSelection(previewOnly) {
      if (busy || uploading || submissionUnknown || !task || !awaitingSelection(task) || !inspection) return;
      if (workspaceSelection() && !sourceCatalogue && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; }))) {
        var selectedBoard = board, seq = sequence, taskId = task.id;
        await inspectSource();
        if (!same(selectedBoard, seq) || !workspaceSelection() || task.id !== taskId || !sourceCatalogue) return;
      }
      var fullPackage = workspaceSelection() && sourceCatalogue && $('fl-mode').value === 'full';
      var chosen = fullConfirmation() || fullPackage ? [] : selections();
      if (!fullConfirmation() && !fullPackage && (!chosen.length || chosen.some(function (entry) { return !entry.artifact_id; }))) {
        status('请选择至少一个分区及其载荷。'); return;
      }
      var selected = board, seq = sequence, current = task;
      busy = true; updateControls(); status('正在提交分区选择…');
      try {
        var payload = Object.assign(scope(selected), { id: current.id,
          layout_digest: workspaceSelection() && sourceCatalogue ? current.flash.inspection.layout_digest : inspection.layout_digest || current.flash.layout_digest });
        if (!selectionPlan || previewOnly === true) {
          payload.selections = chosen; payload.preview = true;
          if (workspaceSelection() || capabilities && capabilities.reset_after_supported)
            payload.reset_after = !!(capabilities && capabilities.reset_after_supported && $('fl-reset-after').checked);
          payload.verify = $('fl-verify').value;
          if (workspaceSelection() && sourceCatalogue) {
            payload.artifact_id = $('fl-artifact').value; payload.mode = $('fl-mode').value; payload.verify = $('fl-verify').value;
            if (fullPackage) setCompanionIntent(payload);
            if (sourceCatalogue.firmware_table === payload.firmware_table && sourceCatalogue.firmware_artifact_id === payload.firmware_artifact_id && (!sourceCatalogue.verify || sourceCatalogue.verify === payload.verify))
              payload.plan_id = sourceCatalogue.id || sourceCatalogue.plan_id;
            if (sourceCatalogue.target_storage) payload.target_storage = sourceCatalogue.target_storage;
          }
        }
        else { payload.plan_id = selectionPlan.id || selectionPlan.plan_id;
          if (!$('fl-keep-field').hidden && $('fl-keep-backup').checked) payload.retain_backup = true;
          payload.request_id = selectionRequestId || (selectionRequestId = id()); }
        var result = await client.request('/api/flash-selection', payload);
        if (!same(selected, seq)) return;
        if (result.plan) {
          if (!(result.plan.id || result.plan.plan_id) || !Array.isArray(result.plan.writes) || !result.plan.writes.length)
            throw new Error('刷机计划缺少写入范围，不能执行。');
          selectionPlan = result.plan; renderPlan(selectionPlan);
          status(norContext() ? '' : '计划已准备，请核对板卡和全部写入范围后开始。');
        } else {
          if (payload.plan_id && !payload.preview) confirmedTaskId = current.id;
          observe(result.task, selected, seq);
        }
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
        status(flow === 'ram' ? ramImageStatus() : capabilities ? '镜像已下载并校验 SHA-256；请选择写入范围并预览。' : imageWaitingForBoard());
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
        var payload = Object.assign(imageScope(selected), {
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
    function imageScope(selected) {
      if ((task && awaitingSelection(task)) || (capabilities && (capabilities.supported !== false || preparationPending()))) return scope(selected);
      return {};
    }
    async function upload(input, companion) {
      if (busy || !board || (companion ? $('fl-firmware-local').disabled : $('fl-files').disabled)) return;
      var files = Array.from(input.files || []);
      if (flow === 'ram' && (files.length !== 1 || !ramSource(files[0]))) {
        input.value = ''; status('请选择一个原始系统镜像包（tar.gz、tgz、tar 或 zip）。', true); return;
      }
      if (companion && files.length && (!firmwarePackage(files[0]) || /\.directory$/i.test(files[0].name))) {
        input.value = ''; status('请选择 tar.gz、tgz、tar 或 zip 固件包。', true); return;
      }
      if (!files.length) { status('请选择要上传的镜像文件。'); return; }
      var selected = board, seq = sequence, binding = imageScope(board);
      busy = true; uploading = true; invalidatePlan(); updateControls();
      try {
        for (var file of files) {
          var artifact = null, sealed = false;
          try {
            var staging = Object.assign({}, binding, { name: file.name, size: file.size });
            if (task && ['queued', 'running'].indexOf(task.state) >= 0 && awaitingSelection(task)) staging.task_id = task.id;
            if (binding.target_id || binding.key) artifact = (await client.request('/api/artifact-create', staging)).artifact;
            else artifact = (await client.request('/api/image-workspace', Object.assign({action:'stage'}, staging))).artifact;
            for (var offset = 0; offset < file.size;) {
              var chunk = new Uint8Array(await file.slice(offset, offset + 1024 * 1024).arrayBuffer());
              if (!chunk.length) throw new Error('本地文件读取失败');
              var raw = '';
              for (var i = 0; i < chunk.length; i++) raw += String.fromCharCode(chunk[i]);
              var appended = (await client.request('/api/artifact-append', Object.assign({}, binding, {
                id: artifact.id, offset: offset, data_b64: btoa(raw) }))).artifact;
              offset += chunk.length;
              if (appended.received !== offset) throw new Error('暂存偏移不匹配');
              if (same(selected, seq)) {
                setProgress('uploading-image', offset, file.size, true);
                status('正在上传镜像，尚未写入开发板 · ' + file.name + ' · ' + format(offset) + ' / ' + format(file.size));
              }
            }
            var sealedResult = await client.request('/api/artifact-seal', Object.assign({}, binding, { id: artifact.id, prepare_image: flow === 'normal' }));
            var completed = sealedResult.artifact; completed.preparation_task = sealedResult.preparation_task;
            if (completed.size !== file.size || !/^[a-f0-9]{64}$/.test(completed.sha256)) throw new Error('暂存文件校验失败');
            sealed = true;
            if (same(selected, seq)) {
              if (companion) companion(completed);
              else artifacts.push(completed);
            }
          } finally {
            if (artifact && !sealed) await client.request('/api/artifact-delete', Object.assign({}, binding, { id: artifact.id })).catch(function () {});
          }
        }
        if (same(selected, seq)) {
          if (companion) { status(''); return; }
          if (files.length === 1) addSource(artifacts[artifacts.length - 1]);
          else {
            var uploaded = artifacts.slice(-files.length), firmware = uploaded.find(function (item) { return /(?:^|[-_])firmware(?:[-_.]|$)/i.test(item.name); });
            var os = uploaded.find(function (item) { return item !== firmware && packageFile(item); });
            if (firmware && os && uploaded.length === 2) { os.firmware = firmware; addSource(os); }
            else { addSource(uploaded[uploaded.length-1]); uploaded.forEach(function (item) { prepareSource(item,item.preparation_task); }); }
          }
          status(task && ['queued', 'running'].indexOf(task.state) >= 0 && awaitingSelection(task)
            ? (norContext() ? '' : '载荷已准备，请在原任务选择实际分区并预览。')
            : flow === 'ram' ? ramImageStatus()
            : !capabilities ? imageWaitingForBoard()
            : preparationPending()
            ? '镜像已准备；请预览仅加载临时环境和读取布局的准备步骤。'
            : capabilities && capabilities.supported === false
            ? '镜像已准备；设备尚未提供刷机所需能力：' + capabilityReason()
            : '镜像已准备，服务端已计算 SHA-256；请生成写入预览。');
        }
      } catch (error) { if (same(selected, seq)) status(error.message); }
      finally { if (same(selected, seq)) {
        input.value = ''; busy = false; uploading = false; updateControls();
        if (!companion && files.length === 1 && workspaceSelection() && packageFile(artifacts.find(function (item) { return item.id === $('fl-artifact').value; }))) {
          $('fl-mode').value='full'; $('fl-target-storage').value=''; await inspectSource();
        }
      } }
    }
    function ramImageStatus() {
      if (pageBoard && !board.target_id) return '';
      if (!capabilities) return '请选择下载模式设备。';
      return capabilities.ram_boot_reason || (capabilities.ram_boot_supported ? '' : '当前目标不支持内存启动。');
    }
    function imageWaitingForBoard() {
      return pageBoard ? '镜像已选择；请准备当前开发板。' : '镜像已选择；请选择下载模式设备。';
    }
    function pageBoardName() {
      return (pageBoard.label || pageBoard.key) + ' · SN ' + (pageBoard.serial || translate('序列号待识别'));
    }
    function applyFlow(next) {
      flow = next;
      $('flash-dialog').dataset.flow = next;
      $('fl-title').textContent = translate({ram:'内存启动',fast:'URL 极速刷机',normal:'刷机工作区'}[next]);
      $('fl-image-title').textContent = next === 'ram' ? translate('启动镜像') : translate('待刷镜像');
      $('fl-files').multiple = next === 'normal';
      $('fl-files').accept = next === 'ram' ? '.tar.gz,.tgz,.tar,.zip' : '';
    }
    async function open(selected, existing, detect, requestedFlow) {
      if (uploading || preparingBoard) { $('flash-dialog').show(); return; }
      var identity = function (value) { return value && (value.target_id || value.serial || value.key); };
      if (!existing && board && (!requestedFlow || requestedFlow === flow)
          && (selected ? (identity(board) === identity(selected)
          || !selected.target_id && board.serialIdentity && board.serialIdentity === identity(selected))
          && (!selected.target_generation || !board.target_generation || selected.target_generation === board.target_generation) : !pageBoard)
          && (!task || task.state !== 'completed' || retainedTask(task))
          && (busy || task || plan || submissionUnknown || selectedPreparation())) {
        $('flash-dialog').show(); return;
      }
      applyFlow(requestedFlow || (existing && existing.kind === 'ram-boot' ? 'ram'
        : existing && (existing.flash || {}).operation === 'stream' ? 'fast' : 'normal'));
      sourceInspection = null; sourceCatalogue = null; sourceAnalysedId=''; $('fl-package-contents').textContent='';
      rememberImage();
      $('fl-verify').value = writeOptions.verify;
      $('fl-reset-after').checked = writeOptions.reset_after;
      // Copy so task-scope restoration never mutates the caller's device row.
      board = Object.assign({}, selected || {}); selected = board; var seq = ++sequence; clearTimeout(pollTimer);
      if (!selected.key || selected.target_id || identity(pageBoard) !== identity(selected)
          || pageBoard.connection_id !== selected.connection_id) downloadControl = null;
      pageBoard = selected.key && !selected.target_id ? Object.assign({}, selected) : null;
      downloadWiring = null;
      busy = false; previewTask = null; previewRequestId = ''; capabilities = null; artifacts = []; task = null; $('flash-dialog').dataset.taskState = ''; $('fl-execution').hidden = true; cursor = 0; eventLines = []; ++eventSequence;
      inspection = null; payloadCandidates = []; selectionRequestId = ''; selectionPlan = null; renderedBackup = '';
      downloadRetry = false; automaticDownloadIdentity = '';
      $('fl-files').value = ''; $('fl-url').value = ''; $('fl-url-sha256').value = '';
      $('fl-fast-url').value = ''; $('fl-fast-storage').value = 'auto'; $('fl-fast-disk').open = false;
      $('fl-url-panel').hidden = true; $('fl-url-toggle').setAttribute('aria-expanded', 'false');
      $('fl-target-storage').value = ''; $('fl-recovery-reason').value = '';
      $('fl-tuning-storage').value = ''; $('fl-tuning-disks').textContent = '';
      invalidatePlan(); $('fl-task-id').textContent = ''; $('fl-events').textContent = '';
      $('fl-event-status').textContent = ''; $('fl-results').textContent = ''; $('fl-task-status').textContent = '';
      bootPriorityRequest = null; $('fl-boot-config').hidden = true;
      $('fl-storage-details').open = false;
      $('fl-live-gpt').hidden = true; $('fl-live-gpt').textContent = '';
      $('fl-system').hidden = true; $('fl-system').dataset.signature = ''; $('fl-system-storage').value = ''; $('fl-system-parts').textContent = '';
      $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
      $('fl-board').textContent = selected.target_id ? targetName(selected)
        : selected.key ? (selected.label || selected.key) + ' · SN ' + (selected.serial || '未知') : translate('尚未选择刷机设备');
      setProgress('', 0, 0, false); renderBackups();
      discovered = []; renderDiscovered();
      $('flash-dialog').show(); status('正在读取刷机能力…');
      if (selected.target_id && existing) {
        if (existing.kind === 'flash-preview') await resumePreview(existing, selected, seq);
        else observe(existing, selected, seq);
        return;
      }
      if (!selected.key && !selected.serial && !selected.target_id && !detect) {
        artifactOptions($('fl-artifact')); renderFirmwareOptions('');
        status('请选择下载模式设备。');
        updateControls(); resumeDownload(selected, seq);
        var saved = rememberedImage;
        if (saved) {
          try {
            var library = await client.request('/api/image-library');
            if (!same(selected, seq) || uploading || $('fl-artifact').value) return;
            var main = library.artifacts.find(function (item) { return item.id === saved.id; });
            if (main) {
              main.firmware = library.artifacts.find(function (item) { return item.id === saved.firmware; });
              $('fl-mode').value = saved.mode || 'full';
              addSource(main);
              if (saved.firmware && saved.firmware.startsWith('table:')) { rememberedImage = saved; renderFirmwareOptions(saved.firmware); }
              status(flow === 'ram' ? ramImageStatus() : imageWaitingForBoard());
            }
          } catch (error) { if (same(selected, seq) && !$('fl-artifact').value) status(error.message, true); }
        }
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
        if (same(selected, seq) && !task && (capabilities.supported === false || flow !== 'normal')) await discover();
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
    $('fl-image-error-copy').addEventListener('click', function () {
      var current = selectedPreparation();
      if (current && current.error) status(options.copyText(current.error, true) ? '错误详情已复制' : '复制失败，请手动选中错误详情。');
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
    function setFirmware(firmware) {
      var main = artifacts.find(function (item) { return item.id === $('fl-artifact').value; });
      if (!main || firmware && (!firmwarePackage(firmware) || firmware.id === main.id))
        throw new Error(translate('请选择与主镜像不同的固件包。'));
      if (firmware) {
        artifacts = artifacts.filter(function (item) { return item.id !== firmware.id; }).concat([firmware]);
        main.firmware = firmware;
      } else delete main.firmware;
      renderFirmwareOptions(firmware ? firmware.id : '');
      invalidatePlan(); rememberImage();
      if (firmware) prepareSource(firmware, firmware.preparation_task);
      updateControls();
    }
    function firmwareChoice() {
      var selected = board, seq = sequence, mainId = $('fl-artifact').value;
      return function (firmware) {
        if (!same(selected, seq) || mainId !== $('fl-artifact').value || $('fl-mode').value !== 'full'
            || task && ['queued','running'].includes(task.state) && !workspaceSelection())
          throw new Error(translate('当前镜像或板卡已变化，请重新选择配套固件。'));
        if (firmware) setFirmware(firmware);
      };
    }
    $('fl-firmware-local').addEventListener('click', function () {
      if ($('fl-firmware-local').disabled) return;
      var input = $('fl-firmware-file'), choose = firmwareChoice();
      input.value = '';
      input.onchange = function () {
        try { choose(); } catch (error) { status(error.message, true); return; }
        upload(input, choose);
      };
      input.click();
    });
    $('fl-firmware-choose').addEventListener('click', function () {
      if (!$('fl-firmware-choose').disabled)
        options.openImageStore(firmwareChoice(), {firmwareOnly:true, excludeId:$('fl-artifact').value, serial:board.serial || ''});
    });
    $('fl-firmware-retry').addEventListener('click',function(){var source=artifacts.find(function(item){return item.id===$('fl-firmware').value;});if(source)prepareSource(source,null,true);});
    $('fl-firmware').addEventListener('change', function () {
      if (embeddedTable()) {
        var main = artifacts.find(item => item.id === $('fl-artifact').value);
        if (main) delete main.firmware;
        rememberImage(); invalidatePlan();
        if (workspaceSelection()) inspectSource();
      } else setFirmware(artifacts.find(function (item) { return item.id === $('fl-firmware').value; }));
    });
    function openWorkspace(empty) {
      if (!options.openImageWorkspace) return;
      var selected=board,seq=sequence,firmware=artifacts.find(function(item){return item.id===$('fl-firmware').value;});
      options.openImageWorkspace(empty ? null : artifacts.find(function(item){return item.id===$('fl-artifact').value;}),function(artifact){
        if (!same(selected,seq)) throw new Error(translate('板卡已变化，请从已下载重新选择镜像。'));
        if (flow === 'ram' && !ramSource(artifact)) throw new Error(translate('请选择一个原始系统镜像包（tar.gz、tgz、tar 或 zip）。'));
        if (firmware) artifact.firmware=firmware; addSource(artifact);
      }, {archiveOnly:flow === 'ram'});
    }
    $('fl-directory').addEventListener('click',function(){openWorkspace(true);});
    $('fl-workspace').addEventListener('click',function(){openWorkspace(false);});
    $('fl-read-nor').addEventListener('click', readNOR);
    $('fl-system-storage').addEventListener('change', renderSystemInventory);
    $('fl-boot-priority').addEventListener('change', updateBootControls);
    $('fl-boot-save').addEventListener('click', saveBootPriority);
    $('fl-close').addEventListener('click', function () { $('flash-dialog').close(); });
    $('fl-discover').addEventListener('click', function () { if (!pageBoard) discover(); });
    $('fl-prepare-board').addEventListener('click', async function () {
      if ($('fl-prepare-board').hidden || $('fl-prepare-board').disabled || !await prepareDevice('normal')) return;
      if (capabilities && capabilities.nor_workspace_supported
          && (capabilities.storage_inventory_supported || $('fl-mode').value === 'partitions')) await readNOR();
      else status('已进入下载模式；当前加载器无法读取设备存储。请使用整包刷写预览。');
    });
    $('fl-edit').addEventListener('click', function () {
      if ($('fl-edit').hidden || $('fl-edit').disabled) return;
      invalidatePlan(); setProgress('', 0, 0, false);
      $('fl-mode').focus();
    });
    $('fl-new').addEventListener('click', function () {
      if (!$('fl-new').hidden && !$('fl-new').disabled) discover();
    });
    $('fl-restart').addEventListener('click', function () {
      if (!$('fl-restart').disabled && selectionExpired(task)) discover();
    });
    $('fl-device').addEventListener('change', function () {
      if (pageBoard) return;
      var target = discovered.find(function (entry) { return (entry.target_id || entry.id) === $('fl-device').value; });
      if (target) selectTarget(target, true);
      else if (!busy && !(task && ['queued', 'running'].indexOf(task.state) >= 0)) {
        board = {}; ++sequence; ++eventSequence; clearTimeout(pollTimer);
        task = null; capabilities = null; inspection = null; payloadCandidates = [];
        sourceInspection = null; sourceCatalogue = null; sourceAnalysedId = '';
        $('fl-package-contents').textContent = ''; renderStorages();
        invalidatePlan(); $('fl-connections').textContent = ''; $('fl-partitions').textContent = '';
        $('fl-task-id').textContent = ''; $('fl-results').textContent = '';
        $('fl-board').textContent = translate('尚未选择刷机设备');
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
    function changeWriteOption(verify) {
      if (verify) writeOptions.verify = $('fl-verify').value;
      else writeOptions.reset_after = $('fl-reset-after').checked;
      try { if (global.localStorage) global.localStorage.setItem('decklab.flash-options.v1', JSON.stringify(writeOptions)); } catch (_) {}
      var reviewed = !!plan, selected = !!selectionPlan;
      invalidatePlan();
      if (selected) submitSelection(true);
      else if (reviewed && !(task && awaitingSelection(task))) makePlan();
      else if (verify && workspaceSelection()) inspectSource();
    }
    $('fl-verify').addEventListener('change', function () { changeWriteOption(true); });
    $('fl-loader-source').addEventListener('change', function () { rememberImage(); invalidatePlan(); });
    $('fl-reset-after').addEventListener('change', function () { changeWriteOption(false); });
    $('fl-target-storage').addEventListener('change', function () { invalidatePlan(); if (workspaceSelection()) inspectSource(); });
    $('fl-local').addEventListener('click', function () { if (!$('fl-local').disabled) $('fl-files').click(); });
    $('fl-files').addEventListener('change', function () { upload($('fl-files')); }); $('fl-plan').addEventListener('click', makePlan);
    // The automatic inspection clears the target disk and renderStorages may
    // refill its default on every poll, so the disk is not part of this key;
    // otherwise a failed inspection would be retried every second.
    function sourceIdentity() { return [task && task.id, $('fl-artifact').value, $('fl-firmware').value, $('fl-mode').value, $('fl-verify').value].join('/'); }
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
          artifact_id: artifactId, mode: $('fl-mode').value, verify: $('fl-verify').value,
          selections: [], layout_digest: current.flash.inspection.layout_digest, reset_after: !!(capabilities && capabilities.reset_after_supported && $('fl-reset-after').checked)});
        if (targetStorage) payload.target_storage = targetStorage;
        if (embeddedTable()) setCompanionIntent(payload);
        var result = await client.request('/api/flash-selection', payload);
        if (!same(selected, seq) || !workspaceSelection() || task.id !== current.id
            || $('fl-artifact').value !== artifactId) return;
        if (!result.inspection || !Array.isArray(result.inspection.storages) || !result.plan)
          throw new Error('镜像目标布局响应不完整');
        sourceCatalogue = result.plan; sourceInspection = result.inspection; inspection = result.inspection;
        payloadCandidates = result.payload_candidates || [];
        renderPartitions(); renderStorages(); renderReplacementInfo(result.plan);
        var extra = null;
        var prepared = (selectedPreparation() || {}).result;
        if (!(prepared && prepared.preparation && (prepared.preparation.catalogue || []).length)) (result.plan.catalogue || []).forEach(function (table) {
          if (table.name === 'partition_flash.json') return;
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
            ? (table.format === 'mtd' ? '镜像包内 NOR 分区' : '镜像包内系统分区') : table.name, group);
          if (!table.selected) heading.dataset.i18nIgnore = '';
          text('p', (table.partitions || []).map(function (part) {
            return part.name + (part.payload ? ' ← ' + part.payload
              + (part.available === false ? '（' + translate('未附带载荷') + '）' : '') : '');
          }).join(' · '), group).dataset.i18nIgnore = '';
        });
        $('fl-verify').value = result.plan.verify || $('fl-verify').value;
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
        if (flow === 'ram' && !ramSource(artifact)) throw new Error(translate('请选择一个原始系统镜像包（tar.gz、tgz、tar 或 zip）。'));
        addSource(artifact);
        if (flow === 'ram') { status(ramImageStatus()); return; }
        if (workspaceSelection() && packageFile(artifact)) { $('fl-mode').value='full'; $('fl-target-storage').value=''; return inspectSource(); }
        if (norContext() && $('fl-mode').value === 'partitions' && !task) {
          status('镜像已选择；请先读取设备存储，再核对实际分区与替换内容。');
        } else if (task && task.flash && task.flash.operation === 'nor-workspace') {
          status('镜像已选择；请核对分区与替换内容。');
        } else if (!capabilities) {
          status(imageWaitingForBoard());
        } else if ($('fl-plan').disabled && !imageReady()) {
          status('镜像准备完成后可预览写入范围。');
        } else if ($('fl-plan').disabled && !(task && awaitingSelection(task))) {
          status('镜像已选择；当前设备尚不能预览：' + capabilityReason(), true);
        } else status('已选择控制机缓存中的镜像；请核对写入范围并预览。');
      }, Object.assign({}, configuration, {serial:selected.serial || '', ramBoot:flow === 'ram'}));
    }
    $('fl-store').addEventListener('click', function () { chooseSource(); });
    $('fl-cache').addEventListener('click', function () { chooseSource({source:'local'}); });
    $('fl-url-toggle').addEventListener('click', function () {
      var panel = $('fl-url-panel'); panel.hidden = !panel.hidden;
      $('fl-url-toggle').setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
      if (!panel.hidden && $('fl-url').focus) $('fl-url').focus();
    });
    $('fl-download').addEventListener('click', function () { downloadImage(false); });
    async function fastFlash() {
      if (!board || $('fl-fast-start').disabled) return;
      var url = $('fl-fast-url').value.trim();
      var storage = $('fl-fast-storage').value.trim() || 'auto';
      if (storage !== 'auto' && !/^(nvme|scsi|mmc|usb):[0-3]$/.test(storage)) { status('目标系统盘应为 auto 或 nvme:0、scsi:0 这类编号。', true); return; }
      if (pageBoard && !(board.target_id && capabilities) && !await prepareDevice('fast')) return;
      if ($('fl-fast-start').disabled) {
        if (board.target_id && capabilities && !capabilities.stream_flash_supported) status('当前设备不支持 URL 极速刷机。', true);
        return;
      }
      var selected = board, seq = sequence;
      requestId = id();
      busy = true; updateControls(); status('正在提交极速刷写任务…');
      try {
        operationReference(selected, 'write/' + requestId);
        var result = await client.request('/api/flash-stream', Object.assign(scope(selected), {
          url: url, system_storage: storage, reset_after: $('fl-reset-after').checked,
          request_id: requestId, client_id: options.clientId || ''}));
        if (!same(selected, seq)) return;
        if (!result.task || !result.task.id) throw new Error('缺少刷机任务回执');
        busy = false; observe(result.task, selected, seq);
        status('极速刷写已开始：边下载边写入系统盘。');
      } catch (error) {
        if (!same(selected, seq)) return;
        if (!error.result || error.status >= 500) {
          submissionUnknown = true;
          status('提交结果未确认；请查询原任务。', true);
        } else {
          operationReference(selected, null);
          status(error.message, true);
        }
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    function rate(value) { return value ? (value >= 1e9 ? (value / 1e9).toFixed(2) + ' GB/s' : (value / 1e6).toFixed(1) + ' MB/s') : '—'; }
    function renderTuning(configuration) {
      var box = $('fl-tuning-summary'), state = $('fl-tuning-state');
      box.textContent = '';
      if (!configuration) { state.textContent = translate('（尚未实测，使用内置默认值）'); return; }
      var m = configuration.measured || {}, t = configuration.tuning || {}, predicted = configuration.predicted || {};
      var when = new Date((configuration.measured_at || 0) * 1000).toLocaleString();
      state.textContent = configuration.scope === 'port' ? translate('（此 USB 口已实测）') : translate('（沿用同型号其他 USB 口的实测）');
      var codec = {gzip: translate('gzip，板上单核解压'), raw: translate('不压缩')}[t.codec] || '—';
      [['实测时间', when + (configuration.board_serial ? ' · SN ' + configuration.board_serial : '') + ' · USB ' + (configuration.port || '')],
       ['USB 下载', rate(m.usb_bps)],
       ['板上解压', rate(m.gzip_bps)],
       ['写盘', m.write_bps ? rate(m.write_bps) : translate('未测（按 550 MB/s 估算）')],
       ['命令开销', m.command_s !== undefined ? (m.command_s * 1000).toFixed(1) + ' ms' : '—'],
       ['选用配置', codec + (t.gzip_level ? ' · ' + translate('级别') + ' ' + t.gzip_level : '')
         + ' · ' + translate('每段') + ' ' + Math.round((t.run_bytes || 0) / 1048576) + ' MiB'],
       ['预计耗时', predicted.data_s_per_gb ? translate('每 GB 非零数据') + ' ' + predicted.data_s_per_gb.toFixed(1) + ' s · ' + translate('每 GB 全零') + ' ' + (predicted.zero_s_per_gb || 0).toFixed(1) + ' s' : '—']
      ].forEach(function (row) { text('dt', translate(row[0]), box); text('dd', row[1], box); });
    }
    var tuningRequest = null;
    async function calibrate(action) {
      if (!board || $('fl-tuning-run').disabled) return;
      var selected = board, seq = sequence, measureWrite = !!$('fl-tuning-write').checked;
      var storage = measureWrite ? ($('fl-tuning-storage').value.trim() || 'auto') : 'auto';
      var identity = action + '\n' + measureWrite + '\n' + storage + '\n' + JSON.stringify(scope(selected));
      if (!tuningRequest || tuningRequest.identity !== identity) tuningRequest = {identity: identity, id: id()};
      busy = true; updateControls();
      status(action === 'forget' ? '正在清除刷写配置…' : '正在提交实测任务…');
      try {
        var result = await client.request('/api/flash-calibrate', Object.assign(scope(selected), {
          action: action, measure_write: measureWrite, system_storage: storage,
          request_id: tuningRequest.id, client_id: options.clientId || ''}));
        if (!same(selected, seq)) return;
        tuningRequest = null;
        if (action === 'forget') {
          capabilities.flash_configuration = result.configuration || null;
          status('已清除此 USB 口的刷写配置。');
        } else {
          busy = false; observe(result.task, selected, seq);
          status(measureWrite ? '正在实测 USB、解压和写盘速度…' : '正在实测 USB 和解压速度…');
        }
      } catch (error) {
        if (same(selected, seq)) status(error.result ? error.message : '提交结果未确认；刷新后在任务历史中查看。', true);
      } finally { if (same(selected, seq)) { busy = false; updateControls(); } }
    }
    $('fl-tuning-run').addEventListener('click', function () { calibrate('measure'); });
    $('fl-tuning-forget').addEventListener('click', function () { calibrate('forget'); });
    $('fl-fast-start').addEventListener('click', fastFlash);
    ['fl-tuning-write', 'fl-tuning-storage'].forEach(function (name) {
      $(name).addEventListener('input', updateControls);
      $(name).addEventListener('change', updateControls);
    });
    ['fl-fast-url', 'fl-fast-storage'].forEach(function (name) {
      $(name).addEventListener('input', updateControls); $(name).addEventListener('change', updateControls);
    });
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
    $('fl-ram-boot').addEventListener('click', startRamBoot);
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
      $('fl-image-information').dataset.signature = '';
      $('fl-backups').dataset.signature = ''; renderBackups();
      $('fl-system').dataset.signature = ''; renderSystemInventory();
      renderReplacementInfo(selectionPlan || plan);
      if (board.target_id) $('fl-board').textContent = targetName(board);
      else if (!board.key) $('fl-board').textContent = translate('尚未选择刷机设备');
      updateControls();
    });
    updateControls();
    return { open: open, discover: function () { return open(null, null, true, 'normal'); } };
  }
  global.SerialdFlashing = { create: create };
})(window);
