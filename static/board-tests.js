// Board test controls are observers of one durable collector-side task.
(function (global) {
  'use strict';
  const names = {'reboot-aging':'软件重启老化（NOR → UFS → Debian）','rtc-wake':'深度挂起与 RTC 唤醒'};
  const states = {queued:'排队',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
  const roundStates = {passed:'通过',failed:'失败',uncertain:'无结果',cancelled:'已停止'};
  function create({byId:$,fetch,getView,copyText,openAdbReceipt}) {
    const client = global.DeckLabTasks.createClient(fetch);
    const dialog = $('board-tests-dialog');
    let selected, task, timer, sequence = 0, followSequence = 0, pending = false, requestId, runningId;
    let round, inspected = '', roundSignature = '', logRequest, rawSerial = '';
    let cases = [], allowedBuiltins = [], canCreate = false, draft = null, savingCase = false, formSequence = 0;
    const active = () => task && ['queued','running'].includes(task.state);
    function text(tag, value, parent) {
      const node = document.createElement(tag);
      node.textContent = value;
      parent.appendChild(node);
      return node;
    }
    let teamPage = 1, teamQuery = '', teamRequest = 0, detailRequest = 0;
    const groupSelect = $('board-tests-team-group');
    const teamDetail = $('board-tests-team-detail');
    const reviewStates = ['待评审', '评审中', '已评审', '评审未通过'];
    function linkedRetry(serial) {
      const params = new URLSearchParams((global.location?.hash || '').slice(1));
      const plan = params.get('plan'), job = params.get('job'), record = params.get('record');
      if (params.get('device') !== serial || !/^[1-9][0-9]{0,17}$/.test(plan || '')
          || !/^[A-Za-z0-9_-]{1,100}$/.test(job || '') || !/^[1-9][0-9]{0,17}$/.test(record || '')) return null;
      return {plan_id:plan, job_id:job, record_id:record};
    }
    function showOriginalReceipts(rows, gen, request, kind) {
      if (!openAdbReceipt || !Array.isArray(rows) || !rows.length) return;
      const group = text('details', '', teamDetail);
      text('summary', '原作业的' + ({'serial-command':'串口','ssh-command':' SSH ','adb-command':' ADB '}[kind]) + '命令回执', group);
      const list = text('ul', '', group);
      for (const entry of rows) {
        const title = entry.round ? '第 ' + entry.round + ' 轮' : '作业共用步骤';
        const status = Number.isInteger(entry.exit_code) ? '退出码 ' + entry.exit_code : (states[entry.state] || entry.state);
        const row = text('li', '', list);
        const button = text('button', title + ' · ' + status + ' · ' + entry.command_id, row);
        button.type = 'button';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            const result = await client.request('/api/task?id=' + encodeURIComponent(entry.id) + query());
            if (!current(gen) || request !== detailRequest) return;
            if (result.task?.id !== entry.id || result.task?.kind !== kind) {
              throw new Error('回执与选择的任务不符');
            }
            dialog.close();
            openAdbReceipt(result.task);
          } catch (error) {
            if (current(gen) && request === detailRequest) message(error.message);
          } finally {
            button.disabled = false;
          }
        });
      }
    }
    async function showTeamCase(id, retry) {
      const gen = sequence, request = ++detailRequest;
      $('team-detail-prompt').hidden = true;
      teamDetail.hidden = false;
      teamDetail.textContent = '正在读取用例…';
      try {
        const query = retry ? {...scope(),view:'retry',...retry} : {...scope(),view:'detail',id};
        const data = await client.request('/api/team-test-cases?' + new URLSearchParams(query));
        if (!current(gen) || request !== detailRequest) return;
        teamDetail.replaceChildren();
        text('h3', data.case.name, teamDetail).setAttribute('data-i18n-ignore', '');
        text('small', reviewStates[data.case.review_status] || '评审状态未知', teamDetail);
        if (data.retry_of) {
          text('p', '原 TitanX 计划 ' + data.retry_of.plan_id + ' · 第 ' + data.retry_of.round + ' 轮失败', teamDetail);
        }
        if (data.case.prerequisite) {
          text('h4', '执行条件', teamDetail);
          text('p', data.case.prerequisite, teamDetail).setAttribute('data-i18n-ignore', '');
        }
        if (data.case.steps.length) text('h4', '执行步骤', teamDetail);
        const steps = text('ol', '', teamDetail);
        for (const step of data.case.steps) {
          const row = text('li', '', steps);
          text('span', step.command || '', row).setAttribute('data-i18n-ignore', '');
          if (step.expected) {
            const expected = text('p', '预期结果 · ', row);
            text('span', step.expected, expected).setAttribute('data-i18n-ignore', '');
          }
        }
        if (!data.case.steps.length) text('p', '该用例尚未填写步骤。', teamDetail);
        const execution = text('div', '', teamDetail);
        execution.className = 'team-execution';
        text('small', '执行板卡', execution);
        const target = text('strong', selected.label || selected.key || selected.serial, execution);
        target.setAttribute('data-i18n-ignore', '');
        if (!data.mapped) {
          text('p', '此板尚未关联 TitanX 工位，可查看用例。', execution);
          const setup = text('button', '接入当前板卡', execution);
          setup.type = 'button';
          setup.addEventListener('click', () => global.DeckLabTitanX.openBoard(selected));
        } else if (data.case.runnable) {
          const actions = text('div', '', execution);
          actions.className = 'team-run-actions';
          const label = text('label', '轮数', actions);
          label.className = 'ft-field';
          const count = text('input', '', label);
          count.type = 'number'; count.value = count.min = '1'; count.max = '100';
          const run = text('button', retry ? '在当前板上复测' : '在当前板上执行', actions);
          run.className = 'pri';
          run.type = 'button';
          run.disabled = active() || pending || !!requestId;
          run.addEventListener('click', async () => {
            if (!count.reportValidity() || !Number.isInteger(count.valueAsNumber)) return;
            const rounds = count.valueAsNumber;
            const action = retry ? '复测' : '执行';
            if (!global.confirm('在 '+selected.key+' 上'+action+' '+data.case.name+'，共 '+rounds+' 轮？')) return;
            run.disabled = true;
            const rid = global.crypto.randomUUID?.() || Array.from(global.crypto.getRandomValues(new Uint8Array(16)),
              n=>n.toString(16).padStart(2,'0')).join('');
            pending = true;
            trackRequest(rid);
            selectSource(false);
            try {
              const parameters = {...scope(),kind:'board-test',source:'titanx',case:data.case.id,rounds,request_id:rid};
              if (retry) parameters.retry = retry;
              const receipt = await client.request('/api/task-start', parameters);
              if (current(gen)) { rememberRun(receipt.task); await follow(receipt.task.id); }
            } catch (error) {
              if (!current(gen)) return;
              if (error.result && error.status < 500) trackRequest(null);
              message(requestId ? '未收到提交回执，请查询原任务。' : error.message);
            } finally {
              if (current(gen)) {pending = false; render();}
            }
          });
        } else {
          text('p', !data.case.automated ? '此用例尚未自动化。' : data.case.review_status === 3 ? '此用例评审未通过。' : '无法确认用例的评审状态。', execution);
        }
        showOriginalReceipts(data.adb_receipts, gen, request, 'adb-command');
        showOriginalReceipts(data.ssh_receipts, gen, request, 'ssh-command');
        showOriginalReceipts(data.serial_receipts, gen, request, 'serial-command');
      } catch (error) {
        if (current(gen) && request === detailRequest) teamDetail.textContent = error.message;
      }
    }
    function selectSource(team) {
      $('board-tests-team').hidden = !team;
      $('titanx-setup-open').hidden = !team;
      $('board-tests-local').hidden = team;
      $('board-tests-team-tab').setAttribute('aria-pressed', String(team));
      $('board-tests-local-tab').setAttribute('aria-pressed', String(!team));
      detailRequest++;
      teamDetail.hidden = true;
      if (team) loadTeam(teamPage);
      else teamRequest++;
    }
    async function loadTeam(page) {
      const gen = sequence, request = ++teamRequest;
      const status = $('board-tests-team-status'), list = $('board-tests-team-list');
      const prev = $('board-tests-team-prev'), next = $('board-tests-team-next');
      prev.disabled = next.disabled = true;
      detailRequest++;
      teamDetail.hidden = true;
      list.replaceChildren();
      $('team-detail-prompt').hidden = false;
      status.textContent = '正在读取团队用例…';
      try {
        const params = new URLSearchParams({page: String(page), name: teamQuery});
        const group = groupSelect.value;
        if (groupSelect.options.length === 1) {
          const groups = await client.request('/api/team-test-cases?view=groups');
          if (!current(gen) || request !== teamRequest) return;
          for (const item of groups.groups || []) text('option', item.name, groupSelect).value = item.id;
        }
        const data = await client.request('/api/team-test-cases?' +
          (group ? new URLSearchParams({view:'members', id:group}) : params));
        if (!current(gen) || request !== teamRequest) return;
        if (!data.configured) {
          status.textContent = '请在系统设置中配置团队用例平台地址和令牌。';
          return;
        }
        if (group) {
          const members = data.members.filter(item => item.name.toLowerCase().includes(teamQuery.toLowerCase()));
          Object.assign(data, {page, page_size:20, total:members.length, cases:members.slice((page-1)*20, page*20)});
        }
        teamPage = data.page;
        const pages = Math.ceil(data.total / data.page_size);
        status.textContent = data.total ? '第 ' + teamPage + ' / ' + pages + ' 页 · ' + data.total + ' 项用例' : '没有匹配的团队用例';
        for (const item of data.cases) {
          const row = text('li', '', list);
          const link = text('button', '', row);
          text('strong', item.name, link).setAttribute('data-i18n-ignore', '');
          link.type = 'button';
          link.addEventListener('click', () => {
            list.querySelector('[aria-current]')?.removeAttribute('aria-current');
            link.setAttribute('aria-current', 'true');
            showTeamCase(item.id);
            if (global.matchMedia('(max-width: 700px)').matches) teamDetail.scrollIntoView({block:'nearest'});
          });
          if (group) text('small', item.code + ' · 顺序 ' + item.order + ' · ' + item.times + ' 次', link);
          else text('small', [item.code, item.automated ? '已自动化' : '待自动化', reviewStates[item.review_status] || '评审状态未知'].filter(Boolean).join(' · '), link);
        }
        prev.disabled = page <= 1;
        next.disabled = page >= pages;
      } catch (error) {
        if (current(gen) && request === teamRequest) status.textContent = error.message;
      }
    }
    $('board-tests-team-tab').addEventListener('click', () => selectSource(true));
    $('board-tests-local-tab').addEventListener('click', () => selectSource(false));
    groupSelect.addEventListener('change', () => loadTeam(1));
    $('board-tests-team-search').addEventListener('submit', event => {
      event.preventDefault();
      teamQuery = $('board-tests-team-name').value.trim();
      loadTeam(1);
    });
    $('board-tests-team-prev').addEventListener('click', () => loadTeam(teamPage - 1));
    $('board-tests-team-next').addEventListener('click', () => loadTeam(teamPage + 1));
    function scope() {
      return {key:selected.key,serial:selected.serial,connection_id:selected.connectionId};
    }
    function query() {
      return '&key='+encodeURIComponent(selected.key)+'&serial='+encodeURIComponent(selected.serial);
    }
    function current(gen) { return dialog.open && gen === sequence; }
    function message(value) { $('board-tests-status').textContent = value; }
    function chosen() { return cases.find(item=>item.id === $('board-tests-case').value); }
    function usable(item) { return item && (item.kind === 'shell' ? allowedBuiltins.length : allowedBuiltins.includes(item.id)); }
    function populateCases(selectedId) {
      const select = $('board-tests-case');
      select.replaceChildren();
      for (const item of cases) text('option',item.name,select).value = item.id;
      const first = cases.find(usable);
      if (cases.some(item=>item.id===selectedId)) select.value = selectedId;
      else if (first) select.value = first.id;
      render();
    }
    function showDefinition(item) {
      if (!item) return;
      formSequence++;
      draft = null; savingCase = false;
      $('board-tests-definition').hidden = $('board-tests-definition-view').hidden = false;
      $('board-tests-case-form').hidden = true;
      $('board-tests-edit-case').hidden = item.kind !== 'shell';
      $('board-tests-definition-title').textContent = item.name;
      const steps = $('board-tests-case-steps');
      steps.replaceChildren();
      const rules = $('board-tests-case-rules');
      rules.hidden = item.kind !== 'shell';
      steps.hidden = item.kind === 'shell' && !item.commands?.length;
      if (item.kind === 'shell') {
        rules.textContent = '以 root 依次执行，每轮超时 '+item.timeout+' 秒。退出码须为 0，并核对已配置的串口输出。';
        for (const step of item.commands || []) {
          const row = text('li','',steps);
          text('pre',step.command,row).dataset.i18nIgnore = '';
          if (step.expected) text('p','预期串口输出 · '+step.expected,row).dataset.i18nIgnore = '';
        }
      } else for (const step of item.steps) text('li',step,steps);
      $('board-tests-trigger-title').hidden = $('board-tests-case-script').hidden = !item.script;
      $('board-tests-trigger-title').textContent = item.kind === 'builtin' ? '主要触发命令' : 'Shell 脚本';
      $('board-tests-case-script').textContent = item.script || '';
      const mechanisms = $('board-tests-case-mechanisms');
      mechanisms.replaceChildren();
      $('board-tests-mechanisms').hidden = !item.mechanisms?.length;
      $('board-tests-mechanisms').open = false;
      for (const detail of item.mechanisms || []) text('li',detail,mechanisms);
      render();
    }
    function commandControls() {
      const rows = Array.from($('board-tests-commands').children);
      rows.forEach((row, index)=>{
        row.querySelector('strong').textContent = '命令 '+(index+1);
        row.querySelector('button').hidden = rows.length < 2;
      });
      $('board-tests-add-command').disabled = rows.length >= 16;
    }
    function addCommand(step = {}) {
      const list = $('board-tests-commands');
      if (list.children.length >= 16) return;
      const row = text('section','',list);
      row.className = 'board-tests-command';
      const head = text('div','',row);
      head.className = 'board-tests-log-head';
      text('strong','',head);
      const remove = text('button','移除此命令',head);
      remove.type = 'button';
      remove.addEventListener('click',()=>{row.remove();commandControls();});
      const code = text('label','Shell 命令',row);
      code.className = 'ft-field';
      const command = text('textarea','',code);
      command.rows = step.command?.includes('\n') ? 5 : 2;
      command.spellcheck = false;
      command.required = true;
      command.value = step.command || '';
      const check = text('label','预期串口输出（可选）',row);
      check.className = 'ft-field';
      const expected = text('input','',check);
      expected.maxLength = 256;
      expected.spellcheck = false;
      expected.value = step.expected || '';
      commandControls();
      return command;
    }
    function editDefinition(item = {}) {
      formSequence++;
      draft = item; savingCase = false;
      $('board-tests-definition').hidden = $('board-tests-case-form').hidden = false;
      $('board-tests-definition-view').hidden = $('board-tests-edit-case').hidden = true;
      $('board-tests-definition-title').textContent = item.id ? '编辑 Shell 用例' : '新建 Shell 用例';
      $('board-tests-case-name').value = item.name || '';
      $('board-tests-case-timeout').value = item.timeout || 60;
      $('board-tests-commands').replaceChildren();
      for (const step of item.commands?.length ? item.commands : [{command:item.script}]) addCommand(step);
      $('board-tests-edit-status').textContent = '';
      $('board-tests-save-case').disabled = false;
      $('board-tests-case-name').focus();
      render();
    }
    function hideDefinition() {
      formSequence++;
      draft = null; savingCase = false;
      $('board-tests-definition').hidden = true;
      $('board-tests-commands').replaceChildren();
      render();
    }
    function trackRequest(id) {
      requestId=id;
      try {
        const key='decklab-board-test:'+selected.serial;
        if (id) global.sessionStorage.setItem(key,id);
        else global.sessionStorage.removeItem(key);
      } catch (_) { /* The task remains in controller history. */ }
    }
    function link(parent, id, label) {
      if (!id) return;
      const anchor = text('a',label,parent);
      anchor.href = '/api/artifact-download?id='+encodeURIComponent(id)+query();
      anchor.className = 'log-download';
    }
    function artifactURL(id) {
      return '/api/artifact-download?id=' + encodeURIComponent(id) + query();
    }
    function rememberRun(run) {
      const picker = $('board-tests-history');
      const option = Array.from(picker.options).find(entry => entry.value === run.id) || text('option','',picker);
      option.value = run.id;
      const time = run.created ? new Date(run.created*1000).toLocaleString(document.documentElement.lang || undefined)+' · ' : '';
      option.textContent = time+(run.result?.case_name || names[run.result?.case] || '板卡用例')+' · '+states[run.state];
    }
    function resetInspector() {
      logRequest?.abort();
      logRequest = null;
      inspected = '';
      rawSerial = '';
      $('board-tests-log').hidden = $('board-tests-copy').hidden = $('board-tests-log-download').hidden = true;
      $('board-tests-log').textContent = '';
      $('board-tests-command-results').textContent = '';
    }
    function updateRoundSelection() {
      for (const entry of $('board-tests-rounds').children) {
        const button = entry.firstElementChild;
        button.setAttribute('aria-pressed', String(Number(button.dataset.round) === round?.round));
      }
    }
    async function inspectTeamRound(row, summary, note) {
      const request = logRequest = new AbortController();
      const params = new URLSearchParams({...scope(), view:'round', task_id:task.id, round:row.round});
      note.textContent = '正在读取本轮回执…';
      try {
        const response = await fetch('/api/team-test-cases?' + params, {signal:request.signal});
        const data = await response.json();
        if (request !== logRequest) return;
        if (!response.ok) throw new Error(data.error || '读取本轮回执失败');
        const box = $('board-tests-command-results');
        const commands = data.commands || [];
        const channels = [...new Set(commands.filter(step => step.round === row.round)
          .map(step => ({'serial-command':'UART 串口','ssh-command':'SSH','adb-command':'ADB'}[step.kind])))];
        note.textContent = [summary, '计划 ' + task.result.platform_task_id,
          channels.length ? '本轮命令通道 · ' + channels.join('、') : '没有匹配的本轮命令回执',
          data.round.started_at, data.round.ended_at].filter(Boolean).join(' · ');
        function section(label, content, expanded) {
          const block = text('details', '', box);
          block.open = !!expanded;
          text('summary', label, block);
          text('pre', content, block);
          return block;
        }
        const phases = {network:'网络准备与检查', probe:'探测执行通道', prepare:'创建目录', upload:'上传并校验脚本', syntax:'检查脚本语法',
          execute:'执行脚本', cleanup:'清理脚本', command:'板端命令'};
        const channelNames = {'serial-command':'UART 串口','ssh-command':'SSH','adb-command':'ADB'};
        const label = step => (step.scope === 'reservation' ? '同次占用 · ' : step.round ? '' : '作业共用 · ') + (phases[step.phase] || '板端命令') +
          ' · ' + (channelNames[step.kind] || '通道未知') + ' · ' +
          (Number.isInteger(step.exit_code) ? '退出码 ' + step.exit_code : states[step.state] || step.state);
        if (commands.length) {
          section('本轮执行过程', commands.map(label).join('\n') +
            (commands.some(step => step.phase === 'upload') ? '' : '\n上传脚本 · 无匹配回执'), true);
        }
        for (const step of commands) {
          const output = [step.identity_channel && '板号核对通道 · ' + ({uart:'UART 串口',adb:'ADB'}[step.identity_channel] || step.identity_channel),
            '命令\n' + step.command,
            (step.kind === 'serial-command' ? '串口合并输出' : '标准输出') + '\n' + (step.stdout || '（空）'),
            step.kind !== 'serial-command' && '标准错误\n' + (step.stderr || '（空）'),
            step.stdout_truncated_bytes && '输出省略 ' + step.stdout_truncated_bytes + ' 字节',
            step.stderr_truncated_bytes && '标准错误省略 ' + step.stderr_truncated_bytes + ' 字节',
            step.error, 'DeckLab 任务 ' + step.id].filter(Boolean).join('\n\n');
          section(label(step), output);
        }
        if (data.logs?.length) section('用例执行输出（' + data.logs.length + ' 条平台日志）', data.logs.join('\n'));
        const script = data.script || {};
        const scriptBox = section('用例脚本' + (script.stdin_size ? '（' + script.stdin_size + ' 字节）' : ''),
          [script.script_preview || '此执行记录未保存脚本正文。', script.stdin_size > 65536 && '仅显示前 64 KiB。',
            script.matched_digest && '正文来自本板另一份上传回执，已核对本轮上传的 SHA-256。',
            script.stdin_sha256 && '上传时的 SHA-256 · ' + script.stdin_sha256].filter(Boolean).join('\n'));
        if (data.script_url) {
          let url;
          try { url = new URL(data.script_url); } catch (_) {}
          if (url && ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) {
            const link = text('a', '打开用例库中的脚本来源', scriptBox);
            link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          }
        }
        section('同期板卡串口日志', data.serial?.data || '该时段未找到串口归档行。');
        if (data.serial?.limited) text('p', '串口归档预览已截断。', box);
        for (const warning of data.warnings || []) text('p', warning, box);
      } catch (error) {
        if (request === logRequest && !request.signal.aborted) {
          note.textContent = error.message;
          const retry = text('button', '重新读取本轮', $('board-tests-command-results'));
          retry.type = 'button';
          retry.addEventListener('click', () => { inspected = ''; inspectRound(row); });
        }
      }
    }
    async function inspectRound(row) {
      round = row;
      updateRoundSelection();
      const result = task?.result || {};
      const archive = result.serial_artifact_id;
      const id = row?.serial_artifact_id || (row?.serial_size ? archive : '');
      const signature = [task?.id, row?.round, id, active()].join('/');
      if (inspected === signature) return;
      resetInspector();
      inspected = signature;
      $('board-tests-log-title').textContent = row ? '第 ' + row.round + ' 轮串口日志' : '本轮串口日志';
      const note = $('board-tests-log-note');
      if (!row) { note.textContent = active() ? '等待首轮结果' : '暂无可查看的轮次'; return; }
      $('board-tests-command-results').textContent = (row.command_results || []).map(step=>'命令 '+step.index+' '+(roundStates[step.status] || '未执行')
        +(step.expected_matched === true ? '（匹配串口）' : '')).concat(row.failed_expected ? ['预期串口输出 · '+row.failed_expected] : []).join(' · ');
      const summary = [roundStates[row.status], row.reason].filter(Boolean).join(' · ');
      if (result.source === 'titanx') {
        $('board-tests-log-title').textContent = '第 ' + row.round + ' 轮执行详情';
        return inspectTeamRound(row, summary, note);
      }
      if (!id) {
        const missing = row.serial_artifact_error || result.serial_artifact_error
          || (active() && row.serial_size ? '本次任务结束后可查看串口归档。' : '这轮没有保存串口日志。');
        note.textContent = [summary, missing].filter(Boolean).join(' · ');
        return;
      }
      const download = $('board-tests-log-download');
      download.href = artifactURL(id);
      download.textContent = row.serial_artifact_id ? '下载本轮串口' : '下载本次串口';
      download.hidden = false;
      note.textContent = '正在读取本轮串口…';
      const request = logRequest = new AbortController();
      const options = {signal:request.signal};
      if (!row.serial_artifact_id) options.headers = {Range:'bytes=' + row.serial_offset + '-' + (row.serial_offset + row.serial_size - 1)};
      try {
        const response = await fetch(artifactURL(id), options);
        if (!response.ok || options.headers && response.status !== 206) throw new Error('串口日志暂不可用或已过期。');
        const raw = await response.text();
        if (request !== logRequest) return;
        rawSerial = raw;
        const prefix = 'DECKLAB-CASE ' + task.id + ' ' + row.round + ' ';
        $('board-tests-log').textContent = raw.replace(/\r\n/g,'\n').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'')
          .split('\n').map(line => line.startsWith(prefix) ? line.slice(prefix.length) : line).join('\n').trimStart();
        $('board-tests-log').hidden = false;
        $('board-tests-log').scrollTop = 0;
        $('board-tests-copy').hidden = !copyText;
        note.textContent = [summary, row.serial_lines ? row.serial_lines + ' 行' : '',
          row.serial_incomplete ? '串口记录有缺口' : ''].filter(Boolean).join(' · ');
      } catch (error) {
        if (request === logRequest && !request.signal.aborted) note.textContent = error.message;
      }
    }
    async function findRound(number, signal) {
      const id = task?.result?.report_artifact_id;
      if (!id) return null;
      const response = await fetch(artifactURL(id), {signal});
      if (!response.ok) throw new Error('轮次记录暂不可用或已过期。');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let tail = '';
      try {
        while (true) {
          const {value, done} = await reader.read();
          const lines = (tail + decoder.decode(value,{stream:!done})).split('\n');
          tail = done ? '' : lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            const row = JSON.parse(line);
            if (row.round === number) return row;
            if (row.round > number) return null;
          }
          if (done) return null;
        }
      } finally { await reader.cancel().catch(()=>{}); }
    }
    function render() {
      const box = $('board-tests-result');
      box.replaceChildren();
      const item = chosen();
      $('board-tests-run').disabled = !selected || pending || !!requestId || !!runningId
        || !usable(item) || !!draft || !!task?.flash?.recovery_required;
      $('board-tests-view-case').disabled = !item;
      $('board-tests-new-case').disabled = !canCreate;
      $('board-tests-retry-connection').hidden = !canCreate || !!allowedBuiltins.length;
      $('board-tests-description').textContent = item ? (item.description || '在当前板卡运行 Shell 脚本，按退出码判定。')
        + (usable(item) ? '' : ' 当前板卡尚未配置此用例。') : '';
      $('board-tests-cancel').hidden = !active();
      $('board-tests-cancel').disabled = pending || !!task?.cancel_requested;
      $('board-tests-lookup').hidden = !requestId;
      $('board-tests-release').hidden = !task?.flash?.recovery_required;
      $('board-tests-runs').hidden = !$('board-tests-history').options.length;
      if (!task) return;
      const result = task.result || {};
      const total = task.progress?.total;
      const summary = text('div','',box);
      summary.className = 'board-tests-summary';
      text('strong', result.case_name || names[result.case] || '板卡用例', summary);
      text('span',states[task.state] || task.state,summary).dataset.state = task.state;
      if (result.source === 'titanx') text('p','TitanX · ' + result.platform_device + ' · ' +
        (result.platform_task_id ? '任务 '+result.platform_task_id : result.platform_plan),box);
      if (result.retry_of) text('p', '复测 TitanX 原计划 ' + result.retry_of.plan_id +
        ' · 第 ' + result.retry_of.round + ' 轮 · 失败记录 ' + result.retry_of.record_id, box);
      text('p','通过 '+(result.completed_rounds || 0)+' 轮'+(total?' / '+total:'')
        +(result.in_flight?' · 正在观察第 '+result.in_flight+' 轮':'')
        +(result.current_command?' · 命令 '+result.current_command+' / '+result.total_commands:''),box);
      if (task.cancel_requested && active()) text('p',result.current_command
        ? '等待当前命令的串口结果，随后停止。' : '等待本轮观察结束，随后停止。',box);
      if (task.flash?.recovery_required && task.kind === 'team-plan') {
        text('p','先在 TitanX 核对原计划及板端操作是否结束，再解除复测限制。',box);
      } else if (task.flash?.recovery_required && result.service) {
        text('p','板卡仍保留给本任务。确认板端服务 '+result.service+' 已结束后释放。',box);
      }
      const issue = result.first_issue;
      if (issue) {
        text('p','第 '+issue.round+' 轮 · '+roundStates[issue.status]+' · '+issue.reason,box);
      } else if (task.error) text('p',task.error,box);
      link(box,result.report_artifact_id,'下载全部轮次与原始回读');
      if (result.report_artifact_error) text('p',result.report_artifact_error,box);
      const rows = [...(result.recent_rounds || [])].reverse();
      if (issue) {
        const index = rows.findIndex(row => row.round === issue.round);
        if (index >= 0) rows.splice(index,1);
        rows.unshift(issue);
      }
      const key = task.id + '/' + JSON.stringify(rows);
      if (key !== roundSignature) {
        roundSignature = key;
        $('board-tests-rounds').replaceChildren();
        for (const row of rows) {
          const button = text('button','',text('li','',$('board-tests-rounds')));
          button.type = 'button';
          button.dataset.round = row.round;
          const label = text('span','第 '+row.round+' 轮',button);
          label.className = 'board-tests-round-label';
          text('span',roundStates[row.status] || row.status,button).dataset.state = row.status;
          button.title = row.reason || '';
          button.addEventListener('click',()=>inspectRound(row));
        }
      }
      $('board-tests-jump').hidden = !result.report_artifact_id || (result.completed_rounds || 0) <= 20;
      if (!logRequest || !round) inspectRound(rows.find(row => row.round === round?.round) || round || rows[0]);
      else updateRoundSelection();
    }
    async function follow(id, byRequest=false) {
      clearTimeout(timer);
      const gen=sequence;
      const request = ++followSequence;
      try {
        const result=await client.request('/api/task?'+(byRequest?'request_id=':'id=')+encodeURIComponent(id)+query());
        if (!current(gen) || request !== followSequence) return;
        if (task?.id !== result.task.id) { round = null; resetInspector(); }
        task=result.task;
        rememberRun(task);
        if (active()) runningId = task.id;
        else if (runningId === task.id) runningId = null;
        $('board-tests-history').value = task.id;
        trackRequest(null);
        message('');
        render();
        if(active()) timer=setTimeout(()=>follow(task.id),1000);
      } catch(error) {
        if (!current(gen) || request !== followSequence) return;
        message(error.message);
        if (!byRequest && ![401,403,404].includes(error.status)) timer=setTimeout(()=>follow(id),2000);
      }
    }
    async function open(existing) {
      selected=getView();
      if (!selected?.serial) return;
      const gen=++sequence;
      const retry = existing ? null : linkedRetry(selected.serial);
      selectSource(!!retry);
      clearTimeout(timer);
      task=null; pending=false; requestId=null; runningId=null; round=null; roundSignature='';
      cases=[];allowedBuiltins=[];canCreate=false;
      hideDefinition();
      resetInspector();
      try { requestId=global.sessionStorage.getItem('decklab-board-test:'+selected.serial); } catch (_) {}
      const name = selected.label || selected.key;
      $('board-tests-board').textContent=name+' · SN '+selected.serial;
      $('board-tests-case').replaceChildren();
      $('board-tests-history').replaceChildren();
      $('board-tests-rounds').replaceChildren();
      if (!dialog.open) dialog.showModal();
      if (retry) showTeamCase(null, retry);
      message('正在读取用例…');
      render();
      try {
        const [hardware, history, definitions]=await Promise.all([
          client.request('/api/hardware?'+query().slice(1)+'&test_cases=1&connection_id='+encodeURIComponent(selected.connectionId || '')),
          client.request('/api/tasks?limit=100&target_serial='+encodeURIComponent(selected.serial)),
          client.request('/api/board-test-cases')
        ]);
        if (!current(gen)) return;
        allowedBuiltins = hardware.test_cases || [];
        cases = definitions.cases;
        canCreate = true;
        populateCases();
        const runs=history.tasks.filter(t => t.kind === 'board-test' || t.kind === 'team-plan');
        const running = runs.find(run=>['queued','running'].includes(run.state));
        runningId = running?.id;
        for(const run of runs) rememberRun(run);
        if (requestId) await follow(requestId,true);
        else if (existing || runs.length) await follow((existing || running || runs[0]).id);
        if (current(gen) && !allowedBuiltins.length) message(hardware.test_cases_reason || '当前板卡的测试连接暂不可用。');
        else if (current(gen) && !task && !requestId) {
          const link = hardware.test_case_connection;
          message(link ? '已核对 USB 调试连接 '+link.usb_port+' · 板端串口 '+link.uart : '');
        }
        if (current(gen)) render();
      } catch(error) { if(current(gen)) message(error.message); }
    }
    $('titanx-setup-open').addEventListener('click', () => global.DeckLabTitanX.openBoard(selected));
    $('b-board-tests').addEventListener('click',()=>open());
    $('board-tests-retry-connection').addEventListener('click',()=>open());
    $('board-tests-close').addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>{
      if (!dialog.open) {sequence++;clearTimeout(timer);resetInspector();}
    });
    $('board-tests-history').addEventListener('change',()=>follow($('board-tests-history').value));
    $('board-tests-case').addEventListener('change',()=>{
      if (!$('board-tests-definition').hidden && !draft) showDefinition(chosen());
      else render();
    });
    $('board-tests-view-case').addEventListener('click',()=>showDefinition(chosen()));
    $('board-tests-new-case').addEventListener('click',()=>editDefinition());
    $('board-tests-edit-case').addEventListener('click',()=>editDefinition(chosen()));
    $('board-tests-hide-case').addEventListener('click',hideDefinition);
    $('board-tests-add-command').addEventListener('click',()=>addCommand()?.focus());
    $('board-tests-case-form').addEventListener('submit',async event=>{
      event.preventDefault();
      if (!draft || savingCase) return;
      const gen=sequence, edit=formSequence;
      const commands = Array.from($('board-tests-commands').children, row=>({
        command:row.querySelector('textarea').value, expected:row.querySelector('input').value
      }));
      const payload={name:$('board-tests-case-name').value,commands,timeout:Number($('board-tests-case-timeout').value)};
      if (draft.id) Object.assign(payload,{id:draft.id,revision:draft.revision});
      savingCase=true;
      $('board-tests-save-case').disabled=true;
      $('board-tests-edit-status').textContent='正在保存用例…';
      try {
        const result=await client.request('/api/board-test-cases',payload);
        if (!current(gen)) return;
        cases=cases.filter(item=>item.id!==result.case.id).concat(result.case);
        populateCases(edit===formSequence ? result.case.id : $('board-tests-case').value);
        if (edit===formSequence) showDefinition(result.case);
      } catch(error) {
        if (current(gen) && edit===formSequence) $('board-tests-edit-status').textContent=error.message;
      } finally {
        if (current(gen) && edit===formSequence) {
          savingCase=false;
          $('board-tests-save-case').disabled=false;
        }
      }
    });
    $('board-tests-copy').addEventListener('click',()=>{
      if (copyText && copyText(rawSerial, true)) message('已复制串口日志');
    });
    $('board-tests-jump').addEventListener('submit',async event=>{
      event.preventDefault();
      const number=Number($('board-tests-round-number').value);
      if (!Number.isSafeInteger(number) || number < 1 || !task) return;
      const recent = (task.result?.recent_rounds || []).find(row => row.round === number);
      if (recent) { inspectRound(recent); return; }
      resetInspector();
      const request = logRequest = new AbortController();
      const note = $('board-tests-log-note');
      note.textContent = '正在查找轮次…';
      try {
        const row = await findRound(number, request.signal);
        if (request !== logRequest) return;
        if (row) inspectRound(row);
        else note.textContent = '没有找到这一轮。';
      } catch(error) {
        if (request === logRequest && !request.signal.aborted) note.textContent = error.message;
      }
    });
    $('board-tests-run').addEventListener('click',async()=>{
      if ($('board-tests-run').disabled || !dialog.open) return;
      const amount=Number($('board-tests-count').value);
      const minutes=$('board-tests-unit').value==='minutes';
      if (!Number.isInteger(amount) || amount<1 || amount>10000) { message('请填写 1 到 10000 的整数。'); return; }
      const item=chosen();
      const action=item.kind==='shell'?'以 root 执行 Shell 脚本。':item.id==='rtc-wake'?'挂起。':'重启。';
      if (!global.confirm('开始 '+item.name+'？板卡将'+action)) return;
      const gen=sequence, id=global.crypto.randomUUID?.() || Array.from(global.crypto.getRandomValues(new Uint8Array(16)),
        n=>n.toString(16).padStart(2,'0')).join('');
      trackRequest(id);
      pending=true;
      render();
      try {
        const response=await client.request('/api/task-start',{...scope(),kind:'board-test',case:item.id,request_id:id,
          ...(item.kind==='shell'?{case_revision:item.revision}:{}),
          ...(minutes?{seconds:amount*60}:{rounds:amount})});
        if (!current(gen)) return;
        rememberRun(response.task);
        $('board-tests-history').value=response.task.id;
        await follow(response.task.id);
      } catch(error) {
        if (!current(gen)) return;
        if (error.result && error.status<500) trackRequest(null);
        message(requestId?'未收到提交回执，请查询原任务。':error.message);
      } finally {
        if(current(gen)) {pending=false;render();}
      }
    });
    $('board-tests-lookup').addEventListener('click',()=>{if(requestId) follow(requestId,true);});
    $('board-tests-cancel').addEventListener('click',async()=>{
      if (!active() || pending) return;
      const gen=sequence, id=task.id;
      pending=true;render();
      try { await client.request('/api/task-cancel',{...scope(),id}); if(current(gen)) await follow(id); }
      catch(error) { if(current(gen)) message(error.message); }
      finally { if(current(gen)) {pending=false;render();} }
    });
    $('board-tests-release').addEventListener('click',async()=>{
      if (!task?.flash?.recovery_required || pending) return;
      const reason = global.prompt(task.kind === 'team-plan'
        ? '核实 TitanX 原计划及板端操作已结束后，填写解除复测限制的依据。'
        : '核实原用例已结束后，填写释放板卡的依据。');
      if (!reason?.trim()) return;
      const gen=sequence, id=task.id;
      pending=true;
      try {
        await client.request('/api/flash-recovery',{key:selected.key,serial:selected.serial,id,attempt_id:task.flash.attempt_id,
          confirm_stopped:true,reason:reason.trim()});
        if(current(gen)) await follow(id);
      } catch(error) { if(current(gen)) message(error.message); }
      finally { if(current(gen)) {pending=false;render();} }
    });
    return {open};
  }
  global.DeckLabBoardTests={create};
})(window);
