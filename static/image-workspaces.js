// Owner-only host file inspection. Edited bytes become a new reviewed image.
(function(global){
  'use strict';
  function create(options){
    var $=options.byId,client=global.SerialdTasks.createClient(options.fetch),artifact=null,choose=null,member='',revision='',generation=0,busy=false,activeTask=null,pendingEdit=null,pendingImport=null,archiveOnly=false;
    var phases={'copying-image-directory':'创建可编辑副本','importing-image':'正在导入镜像包'};
    var codeEditor = null, editorLoad = null, reading = false;
    function status(value){$('iw-status').textContent=value;}
    function controls(){
      ['iw-import','iw-refresh','iw-replace','iw-fork','iw-browse'].forEach(function(id){$(id).disabled=busy;});
      $('iw-save').disabled=busy||reading||!member;
      $('iw-directory').disabled=busy;
      $('iw-close').disabled=busy;
      ['iw-files','iw-directories'].forEach(function(id){
        $(id).querySelectorAll('button').forEach(function(node){node.disabled=busy;});
      });
      if(codeEditor)codeEditor.setReadOnly(busy||reading);
    }
    function editor(){
      if(!editorLoad){
        function script(name){
          return new Promise(function(resolve,reject){
            var node=document.createElement('script');node.src='/vendor/ace/'+name;
            node.onload=resolve;node.onerror=reject;document.head.appendChild(node);
          });
        }
        editorLoad=script('ace.js').then(function(){
          return Promise.all(['mode-json.js','mode-yaml.js','theme-github.js','theme-tomorrow_night.js'].map(script));
        }).then(function(){
          codeEditor=global.ace.edit($('iw-code'),{fontSize:13,showPrintMargin:false,tabSize:2,
            useWorker:false,wrap:true});
          codeEditor.textInput.getElement().setAttribute('aria-label',
            global.SerialdI18n ? global.SerialdI18n.translate('文件内容') : '文件内容');
          codeEditor.session.on('change',function(){$('iw-text').value=codeEditor.getValue();});
          return codeEditor;
        }).catch(function(){return null;});
      }
      return editorLoad;
    }
    function fileRows(files){
      var list=$('iw-files'), folders=new Map();
      list.textContent='';
      files.forEach(function(file){
        var parts=file.name.split('/'), name=parts.pop(), directory=parts.join('/'), group=list;
        if(directory){
          if(!folders.has(directory)){
            var section=document.createElement('details');section.className='iw-folder';section.open=true;
            var label=document.createElement('summary');label.textContent=directory+'/';label.dataset.i18nIgnore='';
            section.appendChild(label);list.appendChild(section);folders.set(directory,section);
          }
          group=folders.get(directory);
        }
        var button=document.createElement('button');button.type='button';button.className='iw-file-row';
        button.title=file.name;button.dataset.i18nIgnore='';button.setAttribute('aria-pressed','false');
        var basename=document.createElement('span');basename.textContent=name;button.appendChild(basename);
        var size=document.createElement('small');size.textContent=options.formatBytes(file.size);button.appendChild(size);
        button.addEventListener('click',function(){select(file,button);});group.appendChild(button);
      });
    }
    function resetPreview(){
      $('iw-editor').hidden=true;$('iw-placeholder').hidden=false;member='';reading=false;
    }
    function payload(action){return {action:action,id:artifact&&artifact.id,path:member,revision:revision};}
    async function browse(){
      var seq=++generation,value=$('iw-directory').value.trim();
      try{
        var result=await client.request('/api/image-workspace',{action:'directories',directory:value});
        if(seq!==generation||!$('image-workspace-dialog').open||$('iw-directory').value.trim()!==value)return;
        $('iw-directory').value=result.directory;$('iw-directories').textContent='';$('iw-directories').hidden=false;status('');
        [{name:'..',path:result.parent}].concat(result.directories||[]).forEach(function(item){var button=document.createElement('button');button.type='button';button.textContent=item.name+'/';button.dataset.i18nIgnore='';button.addEventListener('click',function(){$('iw-directory').value=item.path;browse();});$('iw-directories').appendChild(button);});
        if(!artifact)(result.archives||[]).forEach(function(item){
          var button=document.createElement('button');button.type='button';button.textContent=item.name;button.dataset.i18nIgnore='';
          button.addEventListener('click',function(){$('iw-directory').value=item.path;status('');});$('iw-directories').appendChild(button);
        });
      }catch(error){if(seq===generation)status(error.message);}
    }
    async function load(){
      if(!artifact)return;var seq=++generation;busy=true;controls();
      try{
        var result=await client.request('/api/image-workspace',payload('list'));
        if(seq!==generation)return;
        revision=result.revision;$('iw-directory').value=result.directory;
        $('iw-image-name').textContent=artifact.name || '';resetPreview();
        fileRows(result.files || []);
        status(result.editable?'修改后请重新读取此目录，生成新的镜像预览。':'覆盖文件会创建可编辑副本，保留原镜像缓存。');
      }catch(error){status(error.message);}finally{if(seq===generation){busy=false;controls();}}
    }
    async function select(file, button){
      member=file.name;$('iw-editor').hidden=false;$('iw-placeholder').hidden=true;
      $('iw-files').querySelectorAll('button[aria-pressed="true"]').forEach(function(node){node.setAttribute('aria-pressed','false');});
      button.setAttribute('aria-pressed','true');
      $('iw-name').textContent=file.name;$('iw-meta').textContent=options.formatBytes(file.size);$('iw-text').value='';
      var ext=(file.name.split('.').pop() || '').toLowerCase();
      var textual=['json','yaml','yml','txt','cfg','conf','md'].includes(ext);
      $('iw-code').hidden=$('iw-text').hidden=true;
      $('iw-binary').hidden=textual;$('iw-save').hidden=!textual;
      reading=textual;controls();
      if(!textual)return;
      var selected=member,seq=generation;
      try{
        var result=await client.request('/api/image-workspace',payload('read'));
        if(seq!==generation||selected!==member)return;
        $('iw-text').value=result.text;
        var native=await editor();
        if(seq!==generation||selected!==member)return;
        if(native){
          $('iw-code').hidden=false;
          native.setTheme('ace/theme/'+(global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches
            ? 'tomorrow_night' : 'github'));
          native.session.setMode('ace/mode/'+(ext==='json'?'json':ext==='yaml'||ext==='yml'?'yaml':'text'));
          native.setValue(result.text,-1);native.clearSelection();native.resize();
        }else $('iw-text').hidden=false;
      }catch(error){if(seq===generation&&selected===member){status(error.message);$('iw-save').hidden=true;}}
      finally{if(seq===generation&&selected===member){reading=false;controls();}}
    }
    async function use(result,keepOpen,waiting){
      if(result.task){status(waiting||'正在准备镜像目录…');activeTask=result.task;$('iw-cancel').hidden=false;try{result=await client.watch(result.task,{key:''},function(task){activeTask=task;var p=task.progress||{};status((phases[p.phase]||p.phase||waiting||'正在准备镜像目录…')+(p.total?' · '+options.formatBytes(p.done||0)+' / '+options.formatBytes(p.total):''));});}finally{activeTask=null;$('iw-cancel').hidden=true;}}
      if(!result.artifact)throw new Error('镜像尚未准备好');
      artifact=Object.assign({},result.artifact,{preparation_task:result.preparation_task});
      if(choose)choose(artifact);if(keepOpen)await load();else $('image-workspace-dialog').close();
    }
    async function save(action,replacement){
      if(busy||!member||action==='save'&&reading)return;var seq=generation;busy=true;controls();
      try{var data=payload(action);if(action==='save')data.text=$('iw-text').value;else data.replacement_id=replacement;
        var signature=JSON.stringify(data);
        if(!pendingEdit||pendingEdit.signature!==signature)pendingEdit={signature:signature,id:'image-edit-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
        data.request_id=pendingEdit.id;
        var result=await client.request('/api/image-workspace',data);if(seq===generation){await use(result);pendingEdit=null;}
      }catch(error){if(error.permanent||error.result)pendingEdit=null;if(seq===generation)status(error.message);}finally{if(seq===generation){busy=false;controls();}}
    }
    $('iw-cancel').addEventListener('click',function(){if(activeTask)client.request('/api/task-cancel',{id:activeTask.id}).catch(function(error){status(error.message);});});
    $('iw-close').addEventListener('click',function(){if(!busy){++generation;$('image-workspace-dialog').close();}});
    $('iw-browse').addEventListener('click',browse);
    $('iw-copy').addEventListener('click',function(){options.copyText($('iw-directory').value);});
    $('iw-refresh').addEventListener('click',load);
    $('iw-import').addEventListener('click',async function(){
      if(busy)return;
      var data={action:archiveOnly?'import-archive':'import',path:$('iw-directory').value.trim()};
      var signature=JSON.stringify(data);
      if(!pendingImport||pendingImport.signature!==signature)pendingImport={signature:signature,id:'image-import-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
      data.request_id=pendingImport.id;
      busy=true;controls();status('正在导入镜像…');
      try{await use(await client.request('/api/image-workspace',data),false,'正在导入镜像…');pendingImport=null;}
      catch(error){if(error.permanent||error.result)pendingImport=null;status(error.message);}
      finally{busy=false;controls();}
    });
    $('iw-fork').addEventListener('click',async function(){if(busy||!artifact)return;busy=true;controls();try{await use(await client.request('/api/image-workspace',{action:'fork',id:artifact.id}),true);}catch(error){status(error.message);}finally{busy=false;controls();}});
    $('image-workspace-dialog').addEventListener('cancel',function(event){if(busy)event.preventDefault();else ++generation;});
    $('iw-save').addEventListener('click',function(){save('save');});
    $('iw-replace').addEventListener('click',function(){if(!busy)$('iw-upload').click();});
    $('iw-upload').addEventListener('change',async function(){
      var file=$('iw-upload').files[0];if(!file||busy)return;var staged;
      busy=true;controls();
      try{
        staged=(await client.request('/api/image-workspace',{action:'stage',name:file.name,size:file.size})).artifact;
        for(var offset=0;offset<file.size;){var bytes=new Uint8Array(await file.slice(offset,offset+1024*1024).arrayBuffer()),raw='';for(var i=0;i<bytes.length;i++)raw+=String.fromCharCode(bytes[i]);
          await client.request('/api/artifact-append',{id:staged.id,offset:offset,data_b64:btoa(raw)});offset+=bytes.length;status('上传替换文件 · '+options.formatBytes(offset)+' / '+options.formatBytes(file.size));}
        await client.request('/api/artifact-seal',{id:staged.id});busy=false;await save('replace',staged.id);
      }catch(error){status(error.message);}finally{busy=false;$('iw-upload').value='';controls();}
    });
    return {open:function(selected,onChoose,settings){
      if(busy)return;
      artifact=selected;choose=onChoose;archiveOnly=!!(settings&&settings.archiveOnly);++generation;member='';
      $('iw-title').textContent=artifact?'镜像工作目录':'从控制机导入镜像';
      $('iw-directory').placeholder=archiveOnly?'镜像包的绝对路径':'镜像包或解压目录的绝对路径';
      $('iw-workspace').hidden=$('iw-fork').hidden=!artifact;
      $('iw-import').hidden=!!artifact;
      $('iw-location').open=!artifact;
      $('iw-image-name').textContent=artifact && artifact.name || '';
      $('iw-files').textContent='';$('iw-directories').textContent='';$('iw-directories').hidden=true;
      resetPreview();
      $('iw-directory').value='';status('');$('image-workspace-dialog').showModal();
      if(artifact)load();
    }};
  }
  global.SerialdImageWorkspaces={create:create};
})(window);
