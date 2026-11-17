// Owner-only host file inspection. Edited bytes become a new reviewed image.
(function(global){
  'use strict';
  function create(options){
    var $=options.byId,client=global.SerialdTasks.createClient(options.fetch),artifact=null,choose=null,member='',revision='',generation=0,busy=false,activeTask=null,pendingEdit=null;
    function status(value){$('iw-status').textContent=value;}
    function controls(){['iw-import','iw-refresh','iw-save','iw-replace','iw-fork','iw-browse'].forEach(function(id){$(id).disabled=busy;});$('iw-directory').disabled=busy;['iw-files','iw-directories'].forEach(function(id){Array.from($(id).children).forEach(function(node){node.disabled=busy;});});}
    function payload(action){return {action:action,id:artifact&&artifact.id,path:member,revision:revision};}
    async function browse(){
      var seq=++generation,value=$('iw-directory').value.trim();
      try{
        var result=await client.request('/api/image-workspace',{action:'directories',directory:value});
        if(seq!==generation||!$('image-workspace-dialog').open||$('iw-directory').value.trim()!==value)return;
        $('iw-directory').value=result.directory;$('iw-directories').textContent='';
        [{name:'..',path:result.parent}].concat(result.directories||[]).forEach(function(item){var button=document.createElement('button');button.type='button';button.textContent=item.name+'/';button.dataset.i18nIgnore='';button.addEventListener('click',function(){$('iw-directory').value=item.path;browse();});$('iw-directories').appendChild(button);});
      }catch(error){if(seq===generation)status(error.message);}
    }
    async function load(){
      if(!artifact)return;var seq=++generation;busy=true;controls();
      try{
        var result=await client.request('/api/image-workspace',payload('list'));
        if(seq!==generation)return;
        revision=result.revision;$('iw-directory').value=result.directory;$('iw-files').textContent='';$('iw-editor').hidden=true;member='';
        (result.files||[]).forEach(function(file){
          var button=document.createElement('button');button.type='button';button.textContent=file.name+' · '+options.formatBytes(file.size);button.dataset.i18nIgnore='';
          button.addEventListener('click',function(){select(file);});$('iw-files').appendChild(button);
        });
        status(result.editable?'修改后请重新读取此目录，生成新的镜像预览。':'覆盖文件会创建可编辑副本，保留原镜像缓存。');
      }catch(error){status(error.message);}finally{if(seq===generation){busy=false;controls();}}
    }
    async function select(file){
      member=file.name;$('iw-editor').hidden=false;$('iw-name').textContent=file.name;$('iw-text').value='';
      $('iw-text').hidden=!/\.(json|ya?ml|txt|cfg|conf|md)$/i.test(file.name);$('iw-save').hidden=$('iw-text').hidden;
      if($('iw-text').hidden)return;
      var selected=member,seq=generation;
      try{var result=await client.request('/api/image-workspace',payload('read'));if(seq===generation&&selected===member)$('iw-text').value=result.text;}
      catch(error){status(error.message);}
    }
    async function use(result,keepOpen){
      if(result.task){status('正在准备修改后的目录…');activeTask=result.task;$('iw-cancel').hidden=false;try{result=await client.watch(result.task,{key:''},function(task){activeTask=task;var p=task.progress||{};status((p.phase==='copying-image-directory'?'创建可编辑副本':p.phase||'正在准备镜像目录…')+(p.total?' · '+options.formatBytes(p.done||0)+' / '+options.formatBytes(p.total):''));});}finally{activeTask=null;$('iw-cancel').hidden=true;}}
      if(!result.artifact)throw new Error('镜像目录尚未生成');
      artifact=Object.assign({},result.artifact,{preparation_task:result.preparation_task});
      if(choose)choose(artifact);if(keepOpen)await load();else $('image-workspace-dialog').close();
    }
    async function save(action,replacement){
      if(busy||!member)return;var seq=generation;busy=true;controls();
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
    $('iw-import').addEventListener('click',async function(){if(busy)return;busy=true;controls();try{await use(await client.request('/api/image-workspace',{action:'import',directory:$('iw-directory').value.trim()}));}catch(error){status(error.message);}finally{busy=false;controls();}});
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
    return {open:function(selected,onChoose){if(busy)return;artifact=selected;choose=onChoose;++generation;member='';$('iw-files').textContent='';$('iw-directories').textContent='';$('iw-editor').hidden=true;$('iw-directory').value='';status('');$('image-workspace-dialog').showModal();if(artifact)load();}};
  }
  global.SerialdImageWorkspaces={create:create};
})(window);
