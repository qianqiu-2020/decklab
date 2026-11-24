// Board test controls are observers of one durable collector-side task.
(function (global) {
  'use strict';
  const names = {'reboot-aging':'软件重启老化（NOR → UFS → Debian）','rtc-wake':'深度挂起与 RTC 唤醒'};
  const states = {queued:'排队',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
  const roundStates = {passed:'通过',failed:'失败',uncertain:'无结果'};
  function create({byId:$,fetch,getView}) {
    const client = global.SerialdTasks.createClient(fetch);
    const dialog = $('board-tests-dialog');
    let selected, task, timer, sequence = 0, pending = false, requestId;
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
    function render() {
      const box = $('board-tests-result');
      box.replaceChildren();
      $('board-tests-run').disabled = !selected || pending || !!requestId || !!active()
        || !$('board-tests-case').value || !!task?.flash?.recovery_required;
      $('board-tests-cancel').hidden = !active();
      $('board-tests-cancel').disabled = pending || !!task?.cancel_requested;
      $('board-tests-lookup').hidden = !requestId;
      $('board-tests-release').hidden = !task?.flash?.recovery_required;
      if (!task) return;
      const result = task.result || {};
      const total = task.progress?.total;
      text('strong', (names[result.case] || '板卡用例') + ' · ' + states[task.state], box);
      text('p','通过 '+(result.completed_rounds || 0)+' 轮'+(total?' / '+total:'')
        +(result.in_flight?' · 正在观察第 '+result.in_flight+' 轮':''),box);
      if (task.cancel_requested && active()) text('p','已请求停止发起后续轮次，等待本轮观察结束。',box);
      if (task.flash?.recovery_required && result.service) {
        text('p','板卡仍保留给本任务。确认板端服务 '+result.service+' 已结束后释放。',box);
      }
      const issue = result.first_issue;
      if (issue) {
        text('p','第 '+issue.round+' 轮 · '+roundStates[issue.status]+' · '+issue.reason,box);
        if (issue.serial_lines) text('pre','第 '+issue.serial_lines+' 行  '+issue.last_serial_line,box);
        link(box,issue.serial_artifact_id,issue.serial_incomplete?'下载异常轮串口（有缺口）':'下载异常轮原始串口');
        if (issue.serial_artifact_error) text('p',issue.serial_artifact_error,box);
      } else if (task.error) text('p',task.error,box);
      for (const row of result.recent_rounds || []) {
        const receipt = {acknowledged:'已确认',unknown:'回执未确认','not-issued':'未发出'}[row.trigger];
        text('div','第 '+row.round+' 轮 · '+roundStates[row.status]+' · 触发'+receipt+' · '+row.reason,box);
      }
      link(box,result.report_artifact_id,'下载全部轮次与原始回读');
      if (result.report_artifact_error) text('p',result.report_artifact_error,box);
    }
    async function follow(id, byRequest=false) {
      clearTimeout(timer);
      const gen=sequence;
      try {
        const result=await client.request('/api/task?'+(byRequest?'request_id=':'id=')+encodeURIComponent(id)+query());
        if (!current(gen)) return;
        task=result.task;
        trackRequest(null);
        message('');
        render();
        if(active()) timer=setTimeout(()=>follow(task.id),1000);
      } catch(error) {
        if (!current(gen)) return;
        message(error.message);
        if (!byRequest && ![401,403,404].includes(error.status)) timer=setTimeout(()=>follow(id),2000);
      }
    }
    async function open(existing) {
      selected=getView();
      if (!selected?.serial) return;
      const gen=++sequence;
      clearTimeout(timer);
      task=null; pending=false; requestId=null;
      try { requestId=global.sessionStorage.getItem('decklab-board-test:'+selected.serial); } catch (_) {}
      $('board-tests-title').textContent='板卡用例 · '+selected.serial;
      $('board-tests-case').replaceChildren();
      $('board-tests-history').replaceChildren();
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
        for(const run of runs.slice(0,10)) text('option',new Date(run.created*1000).toLocaleString()+' · '+states[run.state],
          $('board-tests-history')).value=run.id;
        if (requestId) await follow(requestId,true);
        else if (existing || runs.length) await follow((existing || runs[0]).id);
        if (current(gen) && !$('board-tests-case').value) message(hardware.test_cases_configured
          ? '控制机缺少 adb。' : '请先在控制机的硬件配置中登记此板的 USB 路径和串口。');
        else if (current(gen) && !task && !requestId) message('');
        if (current(gen)) render();
      } catch(error) { if(current(gen)) message(error.message); }
    }
    $('b-board-tests').addEventListener('click',()=>open());
    $('board-tests-close').addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>{sequence++;clearTimeout(timer);});
    $('board-tests-history').addEventListener('change',()=>follow($('board-tests-history').value));
    $('board-tests-case').addEventListener('change',render);
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
        const option=text('option','当前任务',$('board-tests-history'));
        option.value=response.task.id;
        $('board-tests-history').value=option.value;
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
