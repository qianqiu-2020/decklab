// Board test controls are observers of one durable collector-side task.
(function (global) {
  'use strict';
  const names = {'reboot-aging':'软件重启老化（NOR → UFS → Debian）','rtc-wake':'深度挂起与 RTC 唤醒'};
  const states = {queued:'排队',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
  const roundStates = {passed:'通过',failed:'失败',uncertain:'无结果'};
  const descriptions = {
    'reboot-aging':'每轮软件重启一次，检查新启动编号和 NOR → UFS → Debian 启动路径。',
    'rtc-wake':'每轮设置 35 秒 RTC 闹钟，进入深度挂起后通过串口核对唤醒。'
  };
  function create({byId:$,fetch,getView,copyText}) {
    const client = global.SerialdTasks.createClient(fetch);
    const dialog = $('board-tests-dialog');
    let selected, task, timer, sequence = 0, followSequence = 0, pending = false, requestId, runningId;
    let round, inspected = '', roundSignature = '', logRequest, rawSerial = '';
    const active = () => task && ['queued','running'].includes(task.state);
    function text(tag, value, parent) {
      const node = document.createElement(tag);
      node.textContent = value;
      parent.appendChild(node);
      return node;
    }
    function scope() {
      return {key:selected.key,serial:selected.serial,connection_id:selected.connectionId};
    }
    function query() {
      return '&key='+encodeURIComponent(selected.key)+'&serial='+encodeURIComponent(selected.serial);
    }
    function current(gen) { return dialog.open && gen === sequence; }
    function message(value) { $('board-tests-status').textContent = value; }
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
      option.textContent = time+(names[run.result?.case] || '板卡用例')+' · '+states[run.state];
    }
    function resetInspector() {
      logRequest?.abort();
      logRequest = null;
      inspected = '';
      rawSerial = '';
      $('board-tests-log').hidden = $('board-tests-copy').hidden = $('board-tests-log-download').hidden = true;
      $('board-tests-log').textContent = '';
    }
    function updateRoundSelection() {
      for (const entry of $('board-tests-rounds').children) {
        const button = entry.firstElementChild;
        button.setAttribute('aria-pressed', String(Number(button.dataset.round) === round?.round));
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
      const summary = [roundStates[row.status], row.reason].filter(Boolean).join(' · ');
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
      $('board-tests-run').disabled = !selected || pending || !!requestId || !!runningId
        || !$('board-tests-case').value || !!task?.flash?.recovery_required;
      $('board-tests-description').textContent = descriptions[$('board-tests-case').value] || '';
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
      text('strong', names[result.case] || '板卡用例', summary);
      text('span',states[task.state] || task.state,summary).dataset.state = task.state;
      text('p','通过 '+(result.completed_rounds || 0)+' 轮'+(total?' / '+total:'')
        +(result.in_flight?' · 正在观察第 '+result.in_flight+' 轮':''),box);
      if (task.cancel_requested && active()) text('p','已请求停止发起后续轮次，等待本轮观察结束。',box);
      if (task.flash?.recovery_required && result.service) {
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
      clearTimeout(timer);
      task=null; pending=false; requestId=null; runningId=null; round=null; roundSignature='';
      resetInspector();
      try { requestId=global.sessionStorage.getItem('decklab-board-test:'+selected.serial); } catch (_) {}
      const name = selected.label || selected.key;
      $('board-tests-board').textContent=name+' · SN '+selected.serial;
      $('board-tests-case').replaceChildren();
      $('board-tests-history').replaceChildren();
      $('board-tests-rounds').replaceChildren();
      if (!dialog.open) dialog.showModal();
      message('正在读取用例…');
      render();
      try {
        const [hardware, history]=await Promise.all([
          client.request('/api/hardware?'+query().slice(1)+'&connection_id='+encodeURIComponent(selected.connectionId || '')),
          client.request('/api/tasks?limit=100&target_serial='+encodeURIComponent(selected.serial))
        ]);
        if (!current(gen)) return;
        for (const value of hardware.test_cases || []) {
          if (names[value]) text('option',names[value],$('board-tests-case')).value=value;
        }
        const runs=history.tasks.filter(t=>t.kind==='board-test');
        const running = runs.find(run=>['queued','running'].includes(run.state));
        runningId = running?.id;
        for(const run of runs) rememberRun(run);
        if (requestId) await follow(requestId,true);
        else if (existing || runs.length) await follow((existing || running || runs[0]).id);
        if (current(gen) && !$('board-tests-case').value) message(hardware.test_cases_configured
          ? '控制机缺少 adb。' : '请先在控制机的硬件配置中登记此板的 USB 路径和串口。');
        else if (current(gen) && !task && !requestId) message('');
        if (current(gen)) render();
      } catch(error) { if(current(gen)) message(error.message); }
    }
    $('b-board-tests').addEventListener('click',()=>open());
    $('board-tests-close').addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>{
      if (!dialog.open) {sequence++;clearTimeout(timer);resetInspector();}
    });
    $('board-tests-history').addEventListener('change',()=>follow($('board-tests-history').value));
    $('board-tests-case').addEventListener('change',render);
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
      const caseId=$('board-tests-case').value;
      if (!global.confirm('开始 '+names[caseId]+'？板卡将'+(caseId==='rtc-wake'?'挂起。':'重启。'))) return;
      const gen=sequence, id=global.crypto.randomUUID?.() || Array.from(global.crypto.getRandomValues(new Uint8Array(16)),
        n=>n.toString(16).padStart(2,'0')).join('');
      trackRequest(id);
      pending=true;
      render();
      try {
        const response=await client.request('/api/task-start',{...scope(),kind:'board-test',case:caseId,request_id:id,
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
      const reason=global.prompt('核实原用例已结束后，填写释放板卡的依据。');
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
  global.SerialdBoardTests={create};
})(window);
