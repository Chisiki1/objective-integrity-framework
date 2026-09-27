/* Task organization and composer behavior. Original instructions stay immutable. */
'use strict';
(() => {
  const say = (ja, en) => UI?.locale === 'en' ? en : ja;
  const layout=document.querySelector('.app-layout'), sidebar=$('chat-sidebar'), splitter=$('sidebar-resizer');
  const sidebarKey='oif.sidebar-width', defaultWidth=260;
  let preferredWidth=null, resizing=null;
  try {const saved=Number(localStorage.getItem(sidebarKey));if(Number.isFinite(saved)&&saved>=200&&saved<=520)preferredWidth=saved;} catch {}
  const desktop=()=>window.matchMedia('(min-width:701px)').matches;
  const sidebarBounds=()=>({min:200,max:Math.min(520,Math.max(200,layout.clientWidth-466))});
  function renderSidebarWidth(){
    if(!desktop())return;
    const {min,max}=sidebarBounds(),width=Math.min(max,Math.max(min,preferredWidth??defaultWidth));
    layout.style.setProperty('--sidebar-width',width+'px');
    splitter.setAttribute('aria-valuemin',String(min));splitter.setAttribute('aria-valuemax',String(max));
    splitter.setAttribute('aria-valuenow',String(Math.round(width)));
  }
  function saveSidebarWidth(){try{if(preferredWidth===null)localStorage.removeItem(sidebarKey);else localStorage.setItem(sidebarKey,String(preferredWidth));}catch{}}
  function resetSidebarWidth(){preferredWidth=null;renderSidebarWidth();saveSidebarWidth();}
  function endResize(event){
    if(!resizing||event.pointerId!==resizing.id)return;
    resizing=null;document.body.classList.remove('sidebar-resizing');saveSidebarWidth();
  }
  splitter.addEventListener('pointerdown',event=>{
    if(event.button!==0||!desktop())return;
    event.preventDefault();splitter.focus();
    resizing={id:event.pointerId,x:event.clientX,width:sidebar.getBoundingClientRect().width};
    splitter.setPointerCapture(event.pointerId);document.body.classList.add('sidebar-resizing');
  });
  splitter.addEventListener('pointermove',event=>{
    if(!resizing||event.pointerId!==resizing.id)return;
    const {min,max}=sidebarBounds();preferredWidth=Math.min(max,Math.max(min,resizing.width+event.clientX-resizing.x));renderSidebarWidth();
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture'])splitter.addEventListener(name,endResize);
  splitter.addEventListener('dblclick',resetSidebarWidth);
  splitter.addEventListener('keydown',event=>{
    if(!desktop()||!['ArrowLeft','ArrowRight','Home','End','Enter'].includes(event.key))return;
    event.preventDefault();if(event.key==='Enter'){resetSidebarWidth();return;}
    const {min,max}=sidebarBounds(),step=event.shiftKey?40:10;
    preferredWidth=event.key==='Home'?min:event.key==='End'?max:Math.min(max,Math.max(min,sidebar.getBoundingClientRect().width+(event.key==='ArrowRight'?step:-step)));
    renderSidebarWidth();saveSidebarWidth();
  });
  window.addEventListener('resize',renderSidebarWidth);renderSidebarWidth();
  const drafts = new Map(), pendingMessages = new Map();
  let newAccessConfirmed=false, newAccessMode='workspace', newAccessVersion=0, accessChanging=false;
  const creationDrafts=new Map();
  function resetNewAccess(){newAccessConfirmed=false;newAccessMode='workspace';newAccessVersion++;$('new-access').value='workspace';renderComposer();}
  function creationReceived(id){const received=creationDrafts.get(id);if(received){creationDrafts.delete(id);received();}}
  async function confirmFullAccess(){
    const dialog=$('full-access-dialog'), input=$('full-access-text'), allow=$('full-access-allow');
    if(dialog.open)return false;
    const phrase=say('フルアクセス','FULL ACCESS');
    $('full-access-title').textContent=say('フルアクセスを許可しますか？','Allow full access?');
    $('full-access-description').textContent=say('このチャットでは、作業フォルダー外のファイルの読み取り・変更、PC上でのプログラム実行、ネットワーク通信を許可します。Windowsにログイン中のユーザーの権限で動作します。実行済みの変更は、権限を戻しても自動では元に戻りません。','This chat can read and change files outside its workspace, run programs on your PC and use the network with your current user permissions. Changing permissions later does not undo completed changes.');
    $('full-access-input-label').textContent=say('確認のため「フルアクセス」と入力してください。','Type FULL ACCESS to confirm.');
    $('full-access-cancel').textContent=say('キャンセル','Cancel');allow.textContent=say('フルアクセスを許可','Allow full access');
    input.value='';allow.disabled=true;
    input.oninput=()=>{allow.disabled=input.value.trim()!==phrase;};
    return new Promise(resolve=>{
      let accepted=false;
      $('full-access-form').onsubmit=event=>{event.preventDefault();if(input.value.trim()===phrase){accepted=true;dialog.close();}};
      $('full-access-cancel').onclick=()=>dialog.close();
      dialog.addEventListener('close',()=>resolve(accepted),{once:true});
      dialog.showModal();input.focus();
    });
  }
  let folder = '', view = 'active', menuOwner = null, editing = null;
  const queue = owner => {if (!drafts.has(owner)) drafts.set(owner, []); return drafts.get(owner);};
  const ownerNow = () => state.taskId || 'new';
  const folderName = id => (state.folders || []).find(f => f.id === id)?.name || say('未分類', 'Unfiled');
  const titleOf = task => (task.ui?.title || task.objective || task.id).slice(0,640);
  const action = (ja, en, run, className='quiet') => {
    const b=node('button',className,say(ja,en));b.type='button';
    b.onclick=async()=>{b.disabled=true;try{await run();}catch(e){note(e.message);}finally{b.disabled=false;}};return b;
  };
  function addFiles(owner, files) {
    const current=queue(owner), incoming=Array.from(files);
    if(current.length+incoming.length>12)throw new Error(say('添付は一度に12件までです。','Attach up to 12 files at once.'));
    if(incoming.some(f=>f.size>state.attachmentMaxBytes)||[...current,...incoming].reduce((n,f)=>n+f.size,0)>20*1024*1024)
      throw new Error(say('添付は1件10 MiB、合計20 MiB以下にしてください。','Attachments must be at most 10 MiB each and 20 MiB total.'));
    current.push(...incoming);renderComposer();
  }
  function renderFiles(id, owner) {
    const target=$(id);target.replaceChildren();
    for(const file of queue(owner)){
      const chip=node('span','file-chip');chip.append(node('span','',file.name));
      const remove=action('×','×',()=>{const files=queue(owner);const i=files.indexOf(file);if(i>=0)files.splice(i,1);renderComposer();},'icon-button');
      remove.setAttribute('aria-label',say('添付を外す：','Remove attachment: ')+file.name);chip.append(remove);target.append(chip);
    }
  }
  const encode = files => Promise.all(files.map(async file=>({filename:file.name,base64:await fileBase64(file)})));
  const clearSent=(owner,files)=>{drafts.set(owner,queue(owner).filter(f=>!files.includes(f)));renderComposer();};
  async function prepareCreate(){
    const files=[...queue('new')], access_mode=$('new-access').value, folder_id=folder||null, version=newAccessVersion;
    if(access_mode==='full'&&!newAccessConfirmed)throw new Error(say('フルアクセスの許可範囲を確認してください。','Confirm the full access scope first.'));
    return {data:{attachments:await encode(files),access_mode,folder_id,...(access_mode==='full'?{confirm_full_access:true}:{})},bindSubmission:id=>creationDrafts.set(id,()=>{clearSent('new',files);if(newAccessVersion===version)resetNewAccess();})};
  }
  function renderComposer(){
    renderFiles('new-files','new');renderFiles('message-files',ownerNow());
    $('new-folder-label').textContent=folder?say('保存先：','Folder: ')+folderName(folder):'';
    const task=state.snapshot?.task, access=state.snapshot?.access;
    $('task-access').disabled=accessChanging||!task||task.state?.runtime!=='practical-v1'||Boolean(task.ui?.deleted);
    if(access)$('task-access').value=access.mode;
    const hasFiles=queue(ownerNow()).length>0;
    $('instruction-text').required=!hasFiles;
    if(task){
      const sending=state.instructionSending.has(task.id)||state.attachmentSending.has(task.id);
      $('submit-instruction').disabled=sending||Boolean(task.ui?.deleted)||!task.source_hash||(!instructionDraft(task.id).text.trim()&&!hasFiles);
      $('message-pick').disabled=Boolean(task.ui?.deleted);
    }
    for(const select of [$('new-access'),$('task-access')]){
      const labels={workspace:say('作業フォルダーのみ','Task workspace only'),ask:say('変更・通信の前に確認','Ask before changes or network'),read_only:say('読み取り専用','Read only'),full:say('フルアクセス','Full access')};
      for(const option of select.options)option.textContent=labels[option.value];
      const descriptions={workspace:say('作業フォルダー内の編集・実行を許可します。公開Webも参照できます。','Allow edits and execution inside the task workspace, and access to public web pages.'),ask:say('ファイルの変更・プログラム実行・Web通信の前に確認します。','Ask before changing files, running programs or accessing the web.'),read_only:say('ファイルの参照と公開Webの取得ができます。変更やプログラム実行は許可しません。','Read files and public web pages. File changes and program execution are not allowed.')};
      select.title=descriptions[select.value];
      if(select.value==='full')select.title=say('PC上のファイル・プログラム・ネットワークに、現在のユーザー権限でアクセスします。','Access PC files, programs and the network with your current user permissions.');
      select.classList.toggle('full-access',select.value==='full');
      const description=$(select.id+'-description');
      if(description)description.textContent=select.title;
    }
  }
  async function submitMessage(event){
    event.preventDefault();const task=state.snapshot?.task, taskId=state.taskId, generation=state.generation;
    if(!taskId||task?.id!==taskId||state.instructionSending.has(taskId)||task.ui?.deleted)return;
    const files=[...queue(taskId)],draft=instructionDraft(taskId),text=$('instruction-text').value;draft.text=text;
    if(!files.length&&!text.trim())return;
    state.instructionSending.add(taskId);draft.message='';draft.isError=false;renderComposer();
    try{
      const attachments=await encode(files),content={text,attachments};
      const signature=JSON.stringify(content),saved=pendingMessages.get(taskId);
      const payload=saved?.signature===signature?saved.payload:{...content,expected_source_hash:task.source_hash,submission_id:crypto.randomUUID().replaceAll('-','')};
      pendingMessages.set(taskId,{signature,payload});
      const snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'/messages',{method:'POST',body:JSON.stringify(payload)});
      checkSourceReceipt(snapshot,taskId,'message');rememberSnapshot(snapshot);pendingMessages.delete(taskId);
      clearSent(taskId,files);if(draft.text===text)draft.text='';draft.message=uiMessage('receivedAttachment');
      if(state.taskId===taskId&&state.generation===generation)renderSnapshot(snapshot);
    }catch(e){if(e.status===409)pendingMessages.delete(taskId);await sourceFailure(e,draft,taskId,generation,'入力文');}
    finally{state.instructionSending.delete(taskId);if(state.taskId===taskId&&state.snapshot?.task?.id===taskId){renderInstruction(state.snapshot.task);renderComposer();}}
  }
  async function paste(event, owner){
    const files=Array.from(event.clipboardData?.files||[]);
    if(files.length){event.preventDefault();try{addFiles(owner,files);}catch(e){note(e.message);}return;}
    if(event.clipboardData?.getData('text/plain'))return;
    // Explorer puts paths in CF_HDROP, which browsers do not expose as Files.
    // Capture the draft owner before waiting; a task switch must not move the paste.
    event.preventDefault();
    try{
      const reply=await api('/api/clipboard/files',{method:'POST',body:JSON.stringify({gesture:'paste'})});
      const result=(reply.files||[]).map(item=>{const data=atob(item.base64),bytes=Uint8Array.from(data,c=>c.charCodeAt(0));return new File([bytes],item.filename);});
      if(result.length)addFiles(owner,result);
    }catch(e){note(e.message);}
  }
  function bindInput(input, picker, owner){
    input.addEventListener('paste',e=>paste(e,owner()));
    const container=input.closest('form');
    container.addEventListener('dragover',e=>{if(Array.from(e.dataTransfer?.types||[]).includes('Files')){e.preventDefault();container.classList.add('drag-over');}});
    container.addEventListener('dragleave',()=>container.classList.remove('drag-over'));
    container.addEventListener('drop',e=>{container.classList.remove('drag-over');if(e.dataTransfer?.files.length){e.preventDefault();try{addFiles(owner(),e.dataTransfer.files);}catch(error){note(error.message);}}});
    picker.addEventListener('change',()=>{try{addFiles(picker.dataset.owner||owner(),picker.files);}catch(e){note(e.message);}picker.value='';});
  }
  function pick(picker,owner){picker.dataset.owner=owner;picker.click();}
  async function updateTask(task,changes){
    const reply=await api('/api/tasks/'+encodeURIComponent(task.id)+'/organization',{method:'PATCH',body:JSON.stringify({...changes,expected_revision:task.ui?.revision||0})});
    const current=state.tasks.find(t=>t.id===task.id);if(current)current.ui=reply.ui;
    if(state.snapshot?.task?.id===task.id){state.snapshot.task.ui=reply.ui;renderSnapshot(state.snapshot);}
    await loadTasks({reset:true});
  }
  function closeMenu(){const el=$('task-menu');el.hidden=true;menuOwner=null;}
  function openMenu(task,x,y){
    const menu=$('task-menu');menu.replaceChildren();menuOwner=task.id;
    const add=(ja,en,run)=>menu.append(action(ja,en,async()=>{closeMenu();await run();},'menu-action'));
    add('名前を変更','Rename',()=>edit('rename',task));
    add(task.ui?.pinned?'ピン留めを外す':'ピン留め',task.ui?.pinned?'Unpin':'Pin',()=>updateTask(task,{pinned:!task.ui?.pinned}));
    add('フォルダーに移動','Move to folder',()=>edit('move',task));
    add('チャットのリンクをコピー','Copy chat link',async()=>{
      const url=new URL(location.href);url.hash='task='+task.id;await navigator.clipboard.writeText(url.href);note(say('このPCで開くチャットのリンクをコピーしました。','Copied a chat link for this PC.'));
    });
    menu.append(node('hr'));
    if(task.ui?.deleted)add('ごみ箱から戻す','Restore from trash',()=>updateTask(task,{deleted:false}));
    else{
      add(task.ui?.archived?'アーカイブから戻す':'アーカイブ',task.ui?.archived?'Unarchive':'Archive',()=>updateTask(task,{archived:!task.ui?.archived}));
      add('削除（ごみ箱へ）','Delete (move to trash)',()=>updateTask(task,{deleted:true}));
    }
    menu.hidden=false;menu.style.left=Math.max(8,Math.min(x,innerWidth-menu.offsetWidth-8))+'px';menu.style.top=Math.max(8,Math.min(y,innerHeight-menu.offsetHeight-8))+'px';menu.querySelector('button')?.focus();
  }
  function edit(kind,task=null){
    editing={kind,task};const dialog=$('organize-dialog'),name=$('organize-name'),select=$('organize-folder');
    $('organize-title').textContent=kind==='rename'?say('タスクの名前','Task name'):kind==='move'?say('フォルダーに移動','Move to folder'):say('フォルダーを作成','Create folder');
    name.hidden=kind==='move';name.required=kind!=='move';name.maxLength=kind==='rename'?160:80;name.value=kind==='rename'?titleOf(task).slice(0,160):'';
    select.hidden=kind==='rename';select.replaceChildren(new Option(say('未分類 / 最上位','Unfiled / top level'),''),...(state.folders||[]).map(f=>new Option(folderPath(f),f.id)));
    select.value=kind==='move'?task.ui?.folder_id||'':folder;
    $('organize-error').textContent='';dialog.showModal();(kind==='move'?select:name).focus();
  }
  function folderPath(item){const parts=[item.name],seen=new Set([item.id]);let parent=item.parent_id;
    while(parent&&!seen.has(parent)){seen.add(parent);const f=(state.folders||[]).find(x=>x.id===parent);if(!f)break;parts.unshift(f.name);parent=f.parent_id;}return parts.join(' / ');}
  function renderTasks(){
    const list=$('task-list');list.replaceChildren();
    $('task-scope').value=view;
    const folders=$('folder-list');folders.replaceChildren();
    const choose=async(id)=>{folder=id;await loadTasks({reset:true});renderComposer();};
    const all=action('すべてのタスク','All tasks',()=>choose(''),'folder-link'+(!folder?' selected':''));folders.append(all);
    for(const f of state.folders||[]){const b=action(folderPath(f),folderPath(f),()=>choose(f.id),'folder-link'+(folder===f.id?' selected':''));b.title=folderPath(f);folders.append(b);}
    const rows=state.tasks.filter(t=>!t.parent_id).map(t=>({...taskListProjection(t),ui:t.ui||taskListProjection(t).ui||{}})).filter(t=>{
      const meta=t.ui;return (!folder||meta.folder_id===folder)&&(view==='trash'?meta.deleted:view==='archive'?meta.archived&&!meta.deleted:!meta.archived&&!meta.deleted);
    });
    rows.sort((a,b)=>Number(Boolean(b.ui.pinned))-Number(Boolean(a.ui.pinned))||String(b.created_at).localeCompare(String(a.created_at)));
    if(!rows.length)empty(list,say('ここにはタスクがありません。','No tasks here.'));
    let pinned=false;
    for(const task of rows){
      if(task.ui.pinned&&!pinned){list.append(node('p','list-subheading',say('ピン留め','Pinned')));pinned=true;}
      if(!task.ui.pinned&&pinned){list.append(node('p','list-subheading',say('最近のタスク','Recent tasks')));pinned=false;}
      const row=node('div','task-row'+(task.id===state.taskId?' active':'')),button=node('button','task-link');
      button.append(node('strong','',titleOf(task)),node('small','',statusName(task.status)));button.title=titleOf(task);
      button.onclick=()=>selectTask(task.id).catch(e=>note(e.message));
      const more=node('button','task-more','⋯');more.setAttribute('aria-label',say('タスクのメニュー：','Task menu: ')+titleOf(task));
      more.onclick=()=>{const box=more.getBoundingClientRect();openMenu(task,box.left,box.bottom);};
      row.oncontextmenu=e=>{e.preventDefault();openMenu(task,e.clientX,e.clientY);};row.append(button,more);list.append(row);
    }
    renderTaskPager();
  }
  function renderApproval(approval){
    const box=node('section','approval-item permission-card');box.append(node('strong','',say('操作の許可を確認','Allow this operation')),node('p','',plain(approval.reason)),node('small','',(P?P.toolName(approval.tool):approval.tool)));
    const target=approval.arguments?.path||approval.arguments?.url;if(target)box.append(node('p','',String(target)));
    box.append(detailButton('操作・引数・実結果',{tool:approval.tool,arguments:approval.arguments,purpose:approval.reason}));
    const answer=async decision=>{await api('/api/approvals/'+encodeURIComponent(approval.id),{method:'POST',body:JSON.stringify({decision,expected_hash:approval.proposal_hash,reason:say('入力欄でこの操作に回答','Answered this operation in the composer')})});await loadApprovals();await refreshSnapshot();};
    const buttons=node('div','permission-actions');buttons.append(action('許可しない','Deny',()=>answer('reject')),action('今回許可','Allow once',()=>answer('approve'),'primary'));box.append(buttons);$('permission-requests').append(box);
  }
  globalThis.OIFWorkspace={prepareCreate,creationReceived,renderTasks,renderComposer,renderApproval,submitMessage,hasFiles:id=>queue(id||'new').length>0,listQuery:()=>({... (view==='active'?{}:{view}),... (folder?{folder}:{})})};
  bindInput($('objective'),$('new-file-input'),()=> 'new');bindInput($('instruction-text'),$('message-file-input'),ownerNow);
  $('new-pick').onclick=()=>pick($('new-file-input'),'new');$('message-pick').onclick=()=>pick($('message-file-input'),ownerNow());
  $('instruction-text').addEventListener('input',renderComposer);$('new-task').addEventListener('click',()=>{if(newAccessMode==='full')resetNewAccess();else renderComposer();});
  $('new-access').onchange=async()=>{
    const select=$('new-access'),mode=select.value,generation=state.generation;
    if(mode==='full'){
      select.value=newAccessMode;renderComposer();
      if(!await confirmFullAccess()||state.generation!==generation)return;
      newAccessConfirmed=true;
    }else newAccessConfirmed=false;
    newAccessVersion++;newAccessMode=mode;select.value=mode;renderComposer();
  };
  $('task-access').onchange=async()=>{
    const taskId=state.taskId,access=state.snapshot?.access,generation=state.generation;
    if(!taskId||!access)return;
    const mode=$('task-access').value;
    $('task-access').value=access.mode;renderComposer();
    if(mode==='full'&&access.mode!=='full'&&!await confirmFullAccess())return;
    if(state.taskId!==taskId||state.generation!==generation)return;
    accessChanging=true;renderComposer();
    try{await api('/api/tasks/'+taskId+'/access',{method:'POST',body:JSON.stringify({mode,expected_revision:access.revision,confirm_full_access:mode==='full'})});if(state.taskId===taskId){await refreshSnapshot();await loadApprovals();}}
    catch(e){note(e.message);}finally{accessChanging=false;renderComposer();}
  };
  $('task-scope').onchange=()=>{view=$('task-scope').value;return loadTasks({reset:true}).catch(e=>note(e.message));};$('folder-create').onclick=()=>edit('folder');
  $('organize-close').onclick=()=>$('organize-dialog').close();
  $('organize-form').onsubmit=async e=>{e.preventDefault();const button=$('organize-save');button.disabled=true;try{const {kind,task}=editing;if(kind==='rename')await updateTask(task,{title:$('organize-name').value});else if(kind==='move')await updateTask(task,{folder_id:$('organize-folder').value||null});else{await api('/api/folders',{method:'POST',body:JSON.stringify({name:$('organize-name').value,parent_id:$('organize-folder').value||null})});await loadTasks();}$('organize-dialog').close();renderComposer();}catch(error){$('organize-error').textContent=error.message;}finally{button.disabled=false;}};
  document.addEventListener('pointerdown',e=>{if(menuOwner&&!$('task-menu').contains(e.target))closeMenu();});
  document.addEventListener('keydown',e=>{if(!menuOwner)return;if(e.key==='Escape')closeMenu();if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();const buttons=[...$('task-menu').querySelectorAll('button')],i=buttons.indexOf(document.activeElement),next=e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;buttons[next]?.focus();}});
  window.addEventListener('resize',closeMenu);UI?.subscribe(()=>{renderTasks();renderComposer();});
  renderTasks();renderComposer();
})();
