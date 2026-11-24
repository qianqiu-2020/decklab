// Repository browsing and durable cache downloads. No board write is submitted.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var source = 'public', path = '', generation = 0, entries = [], choose = null;
    var jobs = Object.create(null), followed = Object.create(null), pairingRows = [];
    var active = Object.create(null), downloaded = Object.create(null), unknown = false;
    var picker = {}, selectionSession = 0;
    var platform = 'k3', operatingSystem = '', channel = 'version', version = '', variant = '';
    var guided = false, directories = Object.create(null), folderChoices = Object.create(null);
    var viewKey = '', restoreScroll = null, views = {};
    try {
      var storedViews = JSON.parse(global.localStorage.getItem('decklab-image-store-views'));
      if (storedViews && typeof storedViews === 'object') views = storedViews;
    } catch (_) {}
    function saveView() {
      if (!viewKey || !$('image-store-dialog').open) return;
      views[viewKey] = {source:source, path:path, platform:platform, os:operatingSystem, channel:channel,
        version:version, variant:variant, folders:folderChoices, search:$('is-search').value,
        scroll:restoreScroll || {dialog:$('image-store-dialog').scrollTop || 0, list:$('is-list').scrollTop || 0}};
      try { global.localStorage.setItem('decklab-image-store-views', JSON.stringify(views)); } catch (_) {}
    }
    var osNames = {bianbu:'Bianbu', debian:'Debian', ubuntu:'Ubuntu', buildroot:'Buildroot', openharmony:'OpenHarmony'};
    function osId(value) {
      var match = String(value).toLowerCase().match(/(?:^|[\/_-])(bianbu|debian|ubuntu|buildroot|openharmony)/);
      return match ? match[1] : 'other';
    }
    function details(item) {
      var name = item.name || '', date = name.match(/(?:^|[-_])(20\d{6})(?:\d{4,6})?(?=[-_.]|$)/);
      var release = name.match(/(?:^|[-_])(v\d+(?:\.\d+)*(?:(?:alpha|beta|rc)\d*)?)/i);
      var desktop = name.match(/(?:^|[-_])(lxqt|gnome|xfce|xfce4|minimal|desktop|server|firmware|agentic|cloud)(?=[-_.]|$)/i);
      var variants = {lxqt:'LXQt',gnome:'GNOME',xfce:'XFCE',xfce4:'XFCE',minimal:'Minimal',desktop:'Desktop',
        server:'Server',firmware:'Firmware',agentic:'Agentic',cloud:'Cloud'};
      return {os:osId(name + '/' + (item.path || '')), date:date ? date[1] : '',
        release:release ? release[1] : '', variant:desktop ? variants[desktop[1].toLowerCase()] : '其他变体'};
    }
    function companion(item) {
      if (picker.fileOnly || picker.firmwareOnly || picker.ramBoot || osId(item.name) !== 'debian' || /[-_]firmware[-_.]/i.test(item.name)) return null;
      function identity(value) {
        var info = details(value), chip = value.name.match(/(?:^|[-_])(k[13])(?=[-_.]|$)/i);
        var major = value.name.match(/debian[-_](\d+)/i);
        return chip && major ? [info.os, major[1], chip[1].toLowerCase(), info.release.toLowerCase()].join('|') : '';
      }
      var info = details(item), key = identity(item);
      if (!key || !info.date && !info.release) return null;
      var candidates = entries.concat(pairingRows).filter(function (value) {
        return value.url && value.kind === 'package' && /[-_]firmware[-_.]/i.test(value.name || '')
          && supported(value) && identity(value) === key && (info.release || details(value).date === info.date);
      });
      candidates = Array.from(new Map(candidates.map(function (value) { return [value.url, value]; })).values());
      if (candidates.length < 2) return candidates[0] || null;
      if (!info.release || !info.date) return null;
      // Prefer the closest preceding build in the same batch when a release
      // contains multiple firmware builds. A cross-date fallback needs a unique match.
      function seconds(value) {
        var time = value.name.match(/(?:^|[-_])20\d{6}([01]\d|2[0-3])([0-5]\d)([0-5]\d)?(?=[-_.]|$)/);
        return time ? Number(time[1])*3600 + Number(time[2])*60 + Number(time[3] || 0) : NaN;
      }
      var builtAt = seconds(item);
      var ranked = candidates.filter(function (value) { return details(value).date === info.date; })
        .map(function (row) { return {row:row, gap:builtAt-seconds(row)}; })
        .filter(function (match) { return match.gap >= 0 && match.gap <= 3600; })
        .sort(function (a,b) { return a.gap-b.gap; });
      return ranked.length && (ranked.length === 1 || ranked[0].gap < ranked[1].gap) ? ranked[0].row : null;
    }
    function supported(item) { return !/\.(?:img|raw)(?:\.|$)/i.test(item.name || '') && item.kind !== 'disk-image'; }
    function catalogItem(item) {
      if (item.kind === 'directory') return true;
      return supported(item) && (!picker.streamURL || item.kind === 'package'
        && !/[-_]firmware[-_.]/i.test(item.name) && picker.streamURL(item.url || ''));
    }
    function newest(a, b) {
      // Natural numeric order, with final releases ahead of the same rc/beta/alpha.
      var x = a.name.replace(/^v(?=\d)/i, ''), y = b.name.replace(/^v(?=\d)/i, '');
      var prerelease = /^(\d+(?:\.\d+)*)(alpha|beta|rc)(\d*)$/i;
      var px = x.match(prerelease), py = y.match(prerelease);
      if (px && px[1] === y) return 1;
      if (py && py[1] === x) return -1;
      return y.localeCompare(x, undefined, {numeric:true, sensitivity:'base'});
    }
    function tabs(id, values, current, action) {
      var box = $(id); box.textContent = '';
      values.forEach(function (value) {
        var node = button(value.name, box, function () { action(value.id); });
        node.setAttribute('aria-pressed', String(value.id === current));
      });
    }
    function selectOptions(node, groups, current, markLatest) {
      node.textContent = '';
      groups.forEach(function (group, index) {
        var option = text('option', group.name + (index === 0 && markLatest !== false ? ' · 最新' : ''), node); option.value = group.id;
      });
      node.value = current;
    }
    async function directory(current, target, force) {
      var key = current + ':' + target;
      if (!directories[key]) {
        directories[key] = client.request('/api/image-catalog?source=' + current + '&path=' + encodeURIComponent(target) + (force ? '&refresh=1' : ''));
      }
      try { return await directories[key]; }
      catch (error) { delete directories[key]; throw error; }
    }
    function catalogVisible(value) {
      $('is-catalog').hidden = !value; $('is-path').hidden = value;
    }
    function draw() {
      var rows = entries;
      if (guided && source !== 'local') {
        var variants = Array.from(new Set(rows.filter(function (row) { return row.kind !== 'directory' && catalogItem(row); })
          .map(function (row) { return details(row).variant; }))).sort();
        if (variant && !variants.includes(variant)) variant = '';
        $('is-variants').hidden = variants.length < 2;
        tabs('is-variants', [{id:'',name:'全部变体'}].concat(variants.map(function (name) { return {id:name,name:name}; })),
          variant, function (value) { variant = value; draw(); });
        rows = rows.filter(function (row) { return !variant || row.kind === 'directory' || details(row).variant === variant; });
      }
      render(rows, source === 'local');
      if (restoreScroll !== null) {
        $('image-store-dialog').scrollTop = Math.max(0, Number(restoreScroll.dialog) || 0);
        $('is-list').scrollTop = Math.max(0, Number(restoreScroll.list) || 0);
        restoreScroll = null;
      }
      saveView();
    }
    async function catalogView(seq, current, force) {
      var valid = function () { return seq === generation && current === source; };
      var board = await directory(current, platform + '/', force);
      if (!valid()) return;
      var versionRoot = board.entries.find(function (row) { return row.kind === 'directory' && row.name === 'version'; });
      var dailyRoot = board.entries.find(function (row) { return row.kind === 'directory' && row.name === 'daily'; });
      var indexes = await Promise.all([versionRoot ? directory(current, versionRoot.path, force) : {entries:[]},
        dailyRoot ? directory(current, dailyRoot.path, force) : {entries:[]}]);
      if (!valid()) return;
      var releases = indexes[0].entries, daily = indexes[1].entries;
      var dates = daily.filter(function (row) { return row.kind === 'directory' && /^20\d{6}$/.test(row.name); }).sort(newest);
      var dailyFiles = dates.length ? (await directory(current, dates[0].path, force)).entries : daily;
      if (!valid()) return;
      var families = releases.filter(function (row) { return row.kind === 'directory'; }).map(function (row) { return osId(row.name); })
        .concat(daily.filter(function (row) { return row.kind === 'directory' && !/^20\d{6}$/.test(row.name); }).map(function (row) { return osId(row.name); }))
        .concat(dailyFiles.filter(function (row) { return row.kind !== 'directory' && supported(row); }).map(function (row) { return details(row).os; }));
      families = Array.from(new Set(families)).sort();
      if (!families.length) {
        // Unconventional administrator layouts stay browsable through the same API.
        guided = false; catalogVisible(false); path = platform + '/'; breadcrumbs(); entries = board.entries; draw(); return;
      }
      if (!families.includes(operatingSystem)) operatingSystem = families.includes('bianbu') ? 'bianbu' : families[0];
      guided = true; catalogVisible(true);
      tabs('is-os', families.map(function (id) { return {id:id,name:osNames[id] || '其他系统'}; }), operatingSystem,
        function (value) { operatingSystem = value; version = ''; variant = ''; folderChoices = Object.create(null); load(); });
      var channels = [];
      var releaseRoots = releases.filter(function (row) { return row.kind === 'directory' && osId(row.name) === operatingSystem; });
      if (releaseRoots.length || releases.some(function (row) { return details(row).os === operatingSystem; })) channels.push({id:'version',name:'版本'});
      if (dailyRoot) channels.push({id:'daily',name:'Daily'});
      if (!channels.some(function (row) { return row.id === channel; })) channel = channels[0].id;
      tabs('is-channel', channels, channel, function (value) { channel = value; version = ''; folderChoices = Object.create(null); load(); });
      var groups = [], direct = [];
      if (channel === 'version') {
        var contents = await Promise.all(releaseRoots.map(function (row) { return directory(current, row.path, force); }));
        if (!valid()) return;
        contents.forEach(function (data) {
          data.entries.forEach(function (row) {
            if (row.kind === 'directory') groups.push({id:row.path,name:row.name,path:row.path});
            else direct.push(row);
          });
        });
        direct = direct.concat(releases.filter(function (row) { return row.kind !== 'directory' && details(row).os === operatingSystem; }));
      } else {
        // Date-first daily repositories mix operating systems in each batch.
        groups = dates.map(function (row) { return {id:row.path,name:row.name,path:row.path}; });
        direct = daily.filter(function (row) { return row.kind !== 'directory'; });
        // OS-first repositories may additionally nest OS major version then dates.
        var dailyOs = daily.filter(function (row) { return row.kind === 'directory' && osId(row.name) === operatingSystem; });
        for (var dailyBranch of dailyOs) {
          var branches = (await directory(current, dailyBranch.path, force)).entries;
          if (!valid()) return;
          // OS/major-version/date is the same daily calendar as date/OS files.
          var majors = branches.filter(function (row) { return row.kind === 'directory' && !/^20\d{6}$/.test(row.name) && /^v?\d+(?:\.\d+)*$/.test(row.name); }).slice(0, 8);
          var nested = await Promise.all(majors.map(function (row) { return directory(current, row.path, force); }));
          if (!valid()) return;
          branches = branches.filter(function (row) { return !majors.includes(row); });
          nested.forEach(function (data) { branches = branches.concat(data.entries); });
          branches.forEach(function (row) {
            if (row.kind === 'directory') groups.push({id:row.path,name:row.name,path:row.path});
            else direct.push(row);
          });
        }
      }
      direct.filter(supported).forEach(function (row) {
        var info = details(row), name = [info.release, info.date].filter(Boolean).join(' · ') || '当前版本';
        var group = groups.find(function (group) { return group.id === 'files:' + name; });
        if (!group) { group = {id:'files:' + name,name:name,files:[]}; groups.push(group); }
        group.files.push(row);
      });
      if (channel === 'daily') {
        var calendar = Object.create(null);
        groups.forEach(function (group) {
          if (group.path && /^20\d{6}$/.test(group.name)) {
            if (!calendar[group.name]) calendar[group.name] = {id:'day:' + group.name,name:group.name,paths:[]};
            calendar[group.name].paths.push(group.path);
          }
        });
        groups = Object.values(calendar).concat(groups.filter(function (group) { return !group.path || !/^20\d{6}$/.test(group.name); }));
      }
      groups.sort(function (a, b) { return (channel === 'daily' ? Number(!a.paths) - Number(!b.paths) : 0)
        || Number(!a.path && !a.paths) - Number(!b.path && !b.paths) || newest(a, b); });
      if (!groups.some(function (group) { return group.id === version; })) version = groups.length ? groups[0].id : '';
      $('is-version-caption').textContent = channel === 'daily' ? '日期' : '版本';
      selectOptions($('is-version'), groups, version); $('is-version').disabled = !groups.length;
      var selectedGroup = groups.find(function (group) { return group.id === version; });
      var rows = [];
      if (selectedGroup) {
        var pages = selectedGroup.files ? [{entries:selectedGroup.files}] : await Promise.all(
          (selectedGroup.paths || [selectedGroup.path]).map(function (target) { return directory(current, target, force); }));
        var seen = new Set();
        pages.forEach(function (page) { page.entries.forEach(function (row) {
          var key = row.url || row.path;
          if (!seen.has(key)) { rows.push(row); seen.add(key); }
        }); });
      }
      if (!valid()) return;
      var folders = $('is-path'); folders.textContent = ''; folders.hidden = true;
      var branchFiles = rows.filter(function (row) { return row.kind !== 'directory'; });
      // Traverse only the selected branch, never recursively crawl old versions/days.
      for (var depth = 0; depth < 4; depth++) {
        var children = rows.filter(function (row) { return row.kind === 'directory'; }).sort(newest);
        if (!children.length) break;
        var hasFiles = rows.some(function (row) { return row.kind !== 'directory' && catalogItem(row); });
        var base = children[0].path.split('/').slice(0, -2).join('/');
        var selectedPath = folderChoices[base];
        if (!children.some(function (row) { return row.path === selectedPath; })) selectedPath = hasFiles ? '' : children[0].path;
        folders.hidden = false;
        var label = text('label', '内容目录 ', folders), chooser = text('select', '', label);
        chooser.setAttribute('aria-label', '内容目录 ' + (depth + 1));
        var choices = children.map(function (row) { return {id:row.path,name:row.name}; });
        if (hasFiles) choices.unshift({id:'',name:'当前目录'});
        selectOptions(chooser, choices, selectedPath, false);
        (function (node, key) { node.addEventListener('change', function () { folderChoices[key] = node.value; load(); }); })(chooser, base);
        if (!picker.firmwareOnly && !picker.fileOnly && !picker.ramBoot && operatingSystem === 'debian') {
          var firmwarePages = await Promise.all(children.filter(function (row) {
            return /^firmware$/i.test(row.name) && row.path !== selectedPath;
          }).map(function (row) {
            return directory(current, row.path, force).catch(function () { return {entries:[]}; });
          }));
          if (!valid()) return;
          firmwarePages.forEach(function (page) {
            branchFiles = branchFiles.concat(page.entries.filter(function (row) { return row.kind !== 'directory'; }));
          });
        }
        if (!selectedPath) break;
        rows = (await directory(current, selectedPath, force)).entries;
        if (!valid()) return;
        branchFiles = branchFiles.concat(rows.filter(function (row) { return row.kind !== 'directory'; }));
      }
      pairingRows = branchFiles;
      entries = rows.filter(function (row) { return row.kind === 'directory' || channel !== 'daily' || details(row).os === operatingSystem; });
      draw(); status('');
    }
    var labels = {'package': '镜像包', 'disk-image': '磁盘镜像', 'partition-file': '分区文件'};
    function text(tag, value, parent) {
      var node = document.createElement(tag); node.textContent = value;
      if (parent) parent.appendChild(node); return node;
    }
    function button(value, parent, action) {
      var node = text('button', value, parent); node.type = 'button'; node.addEventListener('click', action); return node;
    }
    function status(value) { $('is-status').textContent = value; }
    function selected(item) {
      if (!choose) return;
      ++selectionSession;
      try { choose(item); saveView(); $('image-store-dialog').close(); return true; }
      catch (error) { status(error.message); return false; }
    }
    function packageFile(item) { return /\.(tar(?:\.(?:gz|xz|bz2|zst))?|tgz|zip|7z|directory)$/i.test(item.name || ''); }
    function downloadState(item) { return downloaded[item.url] || item.download_state; }
    function render(rows, local) {
      var box = $('is-list'), filter = $('is-search').value.toLowerCase(); box.textContent = '';
      rows.filter(function (item) { return (local || catalogItem(item)) && item.name.toLowerCase().includes(filter)
        && (!picker.fileOnly || item.kind === 'directory' || !packageFile(item))
        && (!picker.ramBoot || item.kind === 'directory' || /\.(?:tar\.gz|tgz|tar|zip)$/i.test(item.name) && !/[-_]firmware[-_.]/i.test(item.name))
        && (!picker.firmwareOnly || item.kind === 'directory' || (/\.(?:tar\.gz|tgz|tar|zip|directory)$/i.test(item.name) && (!picker.excludeId || item.id !== picker.excludeId))); }).forEach(function (item) {
        var row = text('div', '', box); row.className = 'is-row';
        var main = text('div', '', row); main.className = 'is-main';
        if (item.kind === 'directory') {
          button(item.name + '/', main, function () {
            if (guided) {
              // Reuse the selected-branch navigator so OS/date/variant controls
              // survive a folder click alongside ordinary image files.
              folderChoices[item.path.split('/').slice(0, -2).join('/')] = item.path;
            } else path = item.path;
            load();
          });
        } else {
          text('strong', item.name, main);
          var facts = [local ? options.formatBytes(item.size) : labels[item.kind]];
          if (!local && (item.size_label || item.size)) facts.push(item.size ? options.formatBytes(item.size) : item.size_label);
          if (item.modified) facts.push(item.modified);
          if (!local && !picker.streamURL && downloadState(item) === 'cached') facts.push('已缓存');
          if (!local && !picker.streamURL && downloadState(item) === 'downloading') facts.push('正在下载');
          var preparation = item.preparation_task, preparing = preparation && ['queued','running'].includes(preparation.state);
          if (local && preparation) facts.push(preparing ? '准备中' : preparation.state === 'completed' ? '已准备' : '准备未完成');
          if (local && item.state === 'claimed' && !preparing) facts.push('正在使用');
          text('small', facts.join(' · '), main);
          if (local) {
            var actions = text('div', '', row); actions.className = 'is-actions';
            if (choose) {
              var use = button('使用', actions, function () { selected(item); });
              if (!supported(item) || item.state === 'claimed' && !preparing) {
                use.disabled = true; use.title = !supported(item) ? '不支持此镜像格式' : '镜像正在被任务使用';
              }
            }
            var remove = button('删除', actions, function () {
              if (!global.confirm('从控制机删除“' + item.name + '”？')) return;
              // The existing deletion API refuses claimed/in-use artifacts.
              remove.disabled = true;
              client.request('/api/artifact-delete', {id:item.id,actor:'web'}).then(function () {
                status('镜像已从控制机缓存删除。'); loadLibrary();
              }).catch(function (error) { status(error.message); remove.disabled = false; });
            });
            remove.disabled = item.state === 'claimed';
          } else if (picker.streamURL) {
            var companionImage = companion(item);
            if (companionImage) {
              text('small', '配套 Firmware · ' + companionImage.name, main).setAttribute('data-i18n-ignore','');
              text('small', '极速刷机只更新系统盘。若要同时刷写配套 Firmware，请使用普通刷机。', main);
            }
            button('填入 URL', row, function () {
              selected(companionImage ? Object.assign({}, item, {companionFirmware:companionImage.name}) : item);
            });
          } else {
            var firmware=companion(item), includeFirmware=null;
            if(firmware){var paired=text('label','',main);includeFirmware=document.createElement('input');includeFirmware.type='checkbox';includeFirmware.checked=true;paired.appendChild(includeFirmware);text('span','同时选择 '+firmware.name,paired).setAttribute('data-i18n-ignore','');}
            var download = button(choose ? '下载并使用' : '下载到控制机', row, function () { startDownload(item, download, includeFirmware&&includeFirmware.checked?firmware:null); });
            function updateDownload() {
              var selected = includeFirmware && includeFirmware.checked ? [item,firmware] : [item];
              var allCached = selected.every(function (value) { return downloadState(value) === 'cached'; });
              var waiting = !allCached && selected.every(function (value) {
                return ['cached','downloading'].includes(downloadState(value));
              });
              download.textContent = allCached ? (choose ? '使用缓存' : '已缓存')
                : waiting ? (choose ? '等待下载并使用' : '正在下载')
                : choose ? '下载并使用' : '下载到控制机';
              download.disabled = unknown || !!active[item.url] || !choose && (allCached || waiting);
            }
            if (includeFirmware) includeFirmware.addEventListener('change', updateDownload);
            updateDownload();
          }
        }
      });
      if (!box.children.length) text('p', picker.fileOnly ? '当前目录没有可直接替换的分区文件。' : '当前目录没有匹配的受支持镜像。', box).className = 'log-note';
    }
    function breadcrumbs() {
      var box = $('is-path'); box.textContent = '';
      button('K1 / K3', box, function () { path = ''; load(); });
      var prefix = '';
      path.split('/').filter(Boolean).forEach(function (part) {
        prefix += part + '/'; var current = prefix;
        text('span', '/', box); button(part, box, function () { path = current; load(); });
      });
    }
    async function load(force) {
      var seq = ++generation, current = source;
      if (force) { directories = Object.create(null); downloaded = Object.create(null); }
      guided = false; catalogVisible(false);
      $('is-refresh').disabled = true;
      entries = []; pairingRows = []; $('is-list').textContent = '';
      if (current === 'local') { await loadLibrary(seq); return; }
      breadcrumbs(); status('正在读取镜像目录…');
      try {
        var data = await directory(current, path, force);
        if (seq !== generation || current !== source) return;
        if (data.sources) {
          var organization = Array.from($('is-source').children).find(function (item) { return item.value === 'internal'; });
          if (organization) organization.disabled = !data.sources.some(function (item) { return item.id === 'internal'; });
        }
        if (!path && data.entries.some(function (row) { return row.kind === 'directory' && row.path === platform + '/'; })) {
          await catalogView(seq, current, force);
        } else { entries = data.entries; draw(); status(''); }
      } catch (error) { if (seq === generation) { entries = []; $('is-list').textContent = ''; status(error.message); } }
      finally { if (seq === generation) $('is-refresh').disabled = false; }
    }
    async function loadLibrary(seq) {
      if (seq === undefined) seq = ++generation;
      $('is-path').textContent = ''; status('正在读取控制机缓存…');
      try {
        var data = await client.request('/api/image-library');
        if (seq !== generation || source !== 'local') return;
        entries = data.artifacts; draw(); status(''); unknown = false;
        (data.tasks || []).forEach(function (task) { follow(task); });
      } catch (error) { if (seq === generation) status(error.message); }
      finally { if (seq === generation) $('is-refresh').disabled = false; }
    }
    function follow(task, name) {
      if (followed[task.id]) return followed[task.id];
      var box = text('div', '', $('is-downloads'));
      var preparing = task.kind === 'image-prepare';
      var progress = text('span', preparing ? '正在准备镜像…' : '正在下载…', box);
      var cancel = button(preparing ? '取消准备' : '取消下载', box, function () {
        cancel.disabled = true;
        client.request('/api/task-cancel', {id:task.id,actor:'web'}).catch(function (error) {
          status(error.message); cancel.disabled = false;
        });
      });
      var complete = false;
      followed[task.id] = client.watch(task, {key:''}, function (current) {
        var p = current.progress || {};
        var phases = {'checking-image-sha256':'校验镜像', 'expanding-image':'解压镜像', 'checking-payloads':'校验包内文件', 'reading-payload-metadata':'读取基本信息'};
        progress.textContent = (name ? name + ' · ' : '') + (preparing ? (phases[p.phase] || '准备镜像') + '：' : '控制机下载：') + options.formatBytes(p.done || 0)
          + (p.total ? ' / ' + options.formatBytes(p.total) : '');
      }).then(function (result) {
        complete = true;
        if (source === 'local' && $('image-store-dialog').open) loadLibrary();
        return result;
      }).catch(function (error) { progress.textContent = (name ? name + ' · ' : '') + error.message; })
        .finally(function () { delete followed[task.id]; if (complete) box.remove(); else cancel.remove(); });
      return followed[task.id];
    }
    async function startDownload(item, button, firmware) {
      if (unknown || active[item.url]) return;
      button.disabled = true;
      var target = choose ? {choose:choose,session:++selectionSession} : null;
      async function download(value) {
        if(jobs[value.url])return jobs[value.url];
        var id=global.crypto&&global.crypto.randomUUID?global.crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
        active[value.url]='submitting';
        jobs[value.url]=client.request('/api/image-download',{url:value.url,name:value.name,request_id:id,actor:'web'}).then(function(result){
          if (result.task.state === 'completed') return result.task.result;
          return follow(result.task,value.name);
        }).then(function(result){
          if (result && result.artifact) downloaded[value.url] = 'cached';
          else downloaded[value.url] = 'uncached';
          return result;
        }).finally(function(){
          delete jobs[value.url]; delete active[value.url];
          if (source !== 'local' && $('image-store-dialog').open) draw();
        });
        return jobs[value.url];
      }
      try {
        var results=await Promise.all(firmware?[download(item),download(firmware)]:[download(item)]);
        if (!results[0] || firmware && !results[1]) return;
        if(target && target.session===selectionSession && choose===target.choose && $('image-store-dialog').open){
          var artifact=Object.assign({},results[0].artifact,{preparation_task:results[0].preparation_task});
          if(firmware)artifact.firmware=Object.assign({},results[1].artifact,{preparation_task:results[1].preparation_task});
          selected(artifact);
        } else if ($('image-store-dialog').open) status('镜像已缓存在控制机。');
      } catch(error) {
        delete active[item.url];
        if(!error.result){unknown=true;status('提交结果未知，请打开“已下载”检查原下载任务；不会自动重新提交。');}
        else {status(error.message);button.disabled=false;}
      }
    }
    function open(onChoose, configuration) {
      saveView();
      ++selectionSession; picker = configuration || {}; directories = Object.create(null); downloaded = Object.create(null);
      choose = typeof onChoose === 'function' ? onChoose : null;
      var forced = ['local', 'public', 'internal'].includes(picker.source) ? picker.source : '';
      viewKey = picker.streamURL ? 'stream' : picker.fileOnly ? 'partition' : picker.firmwareOnly ? 'firmware' : forced || 'store';
      var saved = picker.fromStream && views.stream || views[viewKey] || (picker.streamURL && views.store) || {};
      source = forced || (['local', 'public', 'internal'].includes(saved.source) ? saved.source : picker.fileOnly || picker.firmwareOnly ? 'local' : 'public');
      if (picker.streamURL && source === 'local') source = 'public';
      path = saved.path || ''; platform = saved.platform === 'k1' ? 'k1' : 'k3';
      if (picker.streamURL && ['k1','k3'].includes(picker.chip) && picker.chip !== platform) {
        platform = picker.chip;
        path = '';
      }
      operatingSystem = saved.os || ''; channel = saved.channel === 'daily' ? 'daily' : 'version';
      version = saved.version || ''; variant = saved.variant || '';
      folderChoices = Object.assign(Object.create(null), saved.folders);
      $('is-source').value = source; $('is-platform').value = platform;
      Array.from($('is-source').children).forEach(function (option) {
        if (option.value === 'local') option.hidden = option.disabled = !!picker.streamURL;
      });
      $('is-downloads').hidden = $('is-download-note').hidden = !!picker.streamURL;
      $('is-search').value = saved.search || '';
      restoreScroll = saved.scroll || {};
      $('is-title').textContent = picker.firmwareOnly ? '选择配套固件' : picker.fileOnly ? '选择分区文件' + (picker.partition ? ' · ' + picker.partition : '') : '镜像商店';
      if (!$('image-store-dialog').open) $('image-store-dialog').showModal();
      load();
    }
    $('is-close').addEventListener('click', function () { saveView(); ++generation; ++selectionSession; $('image-store-dialog').close(); });
    $('image-store-dialog').addEventListener('cancel', function () { saveView(); ++generation; ++selectionSession; });
    if (global.addEventListener) global.addEventListener('pagehide', saveView);
    $('is-source').addEventListener('change', function () { source = $('is-source').value; path = ''; version = ''; directories = Object.create(null); load(); });
    $('is-refresh').addEventListener('click', function () { load(true); });
    $('is-search').addEventListener('input', draw);
    $('is-platform').addEventListener('change', function () { platform = $('is-platform').value; path = ''; version = ''; operatingSystem = ''; load(); });
    $('is-version').addEventListener('change', function () { version = $('is-version').value; folderChoices = Object.create(null); load(); });
    return {open:open};
  }
  global.SerialdImageStore = {create:create};
})(window);
