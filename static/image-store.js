// Repository browsing and durable cache downloads. No board write is submitted.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, client = global.SerialdTasks.createClient(options.fetch);
    var source = 'public', path = '', generation = 0, entries = [], choose = null;
    var active = Object.create(null), unknown = false, opening = false;
    var picker = {}, selectionSession = 0;
    var platform = 'k3', operatingSystem = '', channel = 'version', version = '', variant = '';
    var guided = false, directories = Object.create(null), folderChoices = Object.create(null);
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
    function supported(item) { return !/\.(?:img|raw)(?:\.|$)/i.test(item.name || '') && item.kind !== 'disk-image'; }
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
        var variants = Array.from(new Set(rows.filter(function (row) { return row.kind !== 'directory' && supported(row); })
          .map(function (row) { return details(row).variant; }))).sort();
        if (variant && !variants.includes(variant)) variant = '';
        $('is-variants').hidden = variants.length < 2;
        tabs('is-variants', [{id:'',name:'全部变体'}].concat(variants.map(function (name) { return {id:name,name:name}; })),
          variant, function (value) { variant = value; draw(); });
        rows = rows.filter(function (row) { return !variant || row.kind === 'directory' || details(row).variant === variant; });
      }
      render(rows, source === 'local');
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
      direct.filter(function (row) { return supported(row); }).forEach(function (row) {
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
      // Traverse only the selected branch, never recursively crawl old versions/days.
      for (var depth = 0; depth < 4; depth++) {
        var children = rows.filter(function (row) { return row.kind === 'directory'; }).sort(newest);
        if (!children.length) break;
        var hasFiles = rows.some(function (row) { return row.kind !== 'directory' && supported(row); });
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
        if (!selectedPath) break;
        rows = (await directory(current, selectedPath, force)).entries;
        if (!valid()) return;
      }
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
      try { choose(item); $('image-store-dialog').close(); return true; }
      catch (error) { status(error.message); return false; }
    }
    function packageFile(item) { return /\.(tar(?:\.(?:gz|xz|bz2|zst))?|tgz|zip|7z)$/i.test(item.name || ''); }
    function render(rows, local) {
      var box = $('is-list'), filter = $('is-search').value.toLowerCase(); box.textContent = '';
      rows.filter(function (item) { return (local || supported(item)) && item.name.toLowerCase().includes(filter)
        && (!picker.fileOnly || item.kind === 'directory' || !packageFile(item)); }).forEach(function (item) {
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
          } else {
            var download = button(choose ? '下载并使用' : '下载到控制机', row, function () { startDownload(item, download); });
            download.disabled = unknown || !!active[item.url];
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
      if (force) directories = Object.create(null);
      guided = false; catalogVisible(false);
      $('is-refresh').disabled = true;
      entries = []; $('is-list').textContent = '';
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
        (data.tasks || []).forEach(function (task) { follow(task, 'task:' + task.id); });
      } catch (error) { if (seq === generation) status(error.message); }
      finally { if (seq === generation) $('is-refresh').disabled = false; }
    }
    function follow(task, key, useAfterDownload) {
      if (Object.values(active).includes(task.id)) return;
      active[key] = task.id;
      var box = text('div', '', $('is-downloads'));
      var preparing = task.kind === 'image-prepare';
      var progress = text('span', preparing ? '正在准备镜像…' : '正在下载…', box);
      var cancel = button(preparing ? '取消准备' : '取消下载', box, function () {
        cancel.disabled = true;
        client.request('/api/task-cancel', {id:task.id,actor:'web'}).catch(function (error) {
          status(error.message); cancel.disabled = false;
        });
      });
      client.watch(task, {key:''}, function (current) {
        var p = current.progress || {};
        var phases = {'checking-image-sha256':'校验镜像', 'expanding-image':'解压镜像', 'checking-payloads':'校验包内文件', 'reading-payload-metadata':'读取基本信息'};
        progress.textContent = (preparing ? (phases[p.phase] || '准备镜像') + '：' : '控制机下载：') + options.formatBytes(p.done || 0)
          + (p.total ? ' / ' + options.formatBytes(p.total) : '');
      }).then(function (result) {
        if (useAfterDownload && useAfterDownload.session === selectionSession && choose === useAfterDownload.choose
            && $('image-store-dialog').open && result.artifact) {
          progress.textContent = selected(result.artifact) ? '下载完成，已返回当前选择。' : '下载完成，请重新选择替换目标。';
        } else progress.textContent = preparing ? '镜像准备完成，可跨板卡复用。' : '镜像已保存到控制机，正在准备解压与校验。';
        if (source === 'local' && $('image-store-dialog').open) loadLibrary();
      }).catch(function (error) { progress.textContent = error.message; })
        .finally(function () { delete active[key]; cancel.remove(); if (source !== 'local') draw(); });
    }
    async function startDownload(item, button) {
      if (unknown || active[item.url]) return;
      button.disabled = true; active[item.url] = 'submitting';
      var useAfterDownload = choose ? {choose:choose,session:++selectionSession} : null;
      var id = global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
      try {
        var result = await client.request('/api/image-download', {url:item.url,name:item.name,request_id:id,actor:'web'});
        delete active[item.url]; follow(result.task, item.url, useAfterDownload);
      } catch (error) {
        delete active[item.url];
        if (!error.result) { unknown = true; status('提交结果未知，请打开“已下载”检查原下载任务；不会自动重新提交。'); }
        else { status(error.message); button.disabled = false; }
      }
    }
    function open(onChoose, configuration) {
      ++selectionSession; picker = configuration || {}; directories = Object.create(null);
      choose = typeof onChoose === 'function' ? onChoose : null;
      $('is-title').textContent = picker.fileOnly ? '选择分区文件' + (picker.partition ? ' · ' + picker.partition : '') : '镜像商店';
      if (!$('image-store-dialog').open) $('image-store-dialog').showModal();
      if (!opening) { opening = true; source = $('is-source').value || 'public'; }
      if (['local', 'public', 'internal'].includes(picker.source)) { source = picker.source; path = ''; $('is-source').value = source; }
      if (picker.fileOnly) { source = 'local'; path = ''; $('is-source').value = 'local'; }
      load();
    }
    $('is-close').addEventListener('click', function () { ++generation; ++selectionSession; $('image-store-dialog').close(); });
    $('image-store-dialog').addEventListener('cancel', function () { ++selectionSession; });
    $('is-source').addEventListener('change', function () { source = $('is-source').value; path = ''; version = ''; directories = Object.create(null); load(); });
    $('is-refresh').addEventListener('click', function () { load(true); });
    $('is-search').addEventListener('input', draw);
    $('is-platform').addEventListener('change', function () { platform = $('is-platform').value; path = ''; version = ''; operatingSystem = ''; load(); });
    $('is-version').addEventListener('change', function () { version = $('is-version').value; folderChoices = Object.create(null); load(); });
    return {open:open};
  }
  global.SerialdImageStore = {create:create};
})(window);
