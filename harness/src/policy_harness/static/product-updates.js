/* Official releases are checked outside task execution; installation is opt-in. */
(() => {
  'use strict';
  const words = {
    updates:['更新','Updates'], available:['更新あり','Update available'], restartNeeded:['再起動が必要','Restart needed'],
    title:['OIFの更新','OIF updates'], close:['閉じる','Close'], check:['更新を確認','Check for updates'],
    install:['更新して再起動','Update and restart'], restart:['サービスを再起動','Restart service'],
    releases:['更新内容を見る','View release notes'], current:['最新の版を使用しています。','OIF is up to date.'],
    offline:['更新情報を取得できませんでした。後で再確認できます。','Updates could not be checked. You can try again later.'],
    checking:['更新を確認しています…','Checking for updates…'], preparing:['更新をダウンロードして確認しています…','Downloading and verifying the update…'],
    installing:['更新中です。OIFは自動で開き直します。ブラウザーで見ている場合は、OIFのウィンドウを閉じてください。','Installing. OIF will reopen. If you are viewing this in a browser, close the OIF desktop window.'],
    restarting:['サービスを再起動しています…','Restarting the service…'],
    reconnect:['再接続を待っています。しばらくしてから画面を開き直してください。','Waiting to reconnect. Reopen the page shortly.'],
    preserve:['更新を選ぶとOIFを開き直します。チャット・成果物・接続設定は引き継ぎます。','Choosing Update reopens OIF and keeps your chats, files and connection settings.'],
    busy:['実行中の作業があります。終了後に更新・再起動できます。','Tasks are running. Update or restart after they finish.'],
    unsent:['未送信の文や添付があります。各チャットと新規作成欄の入力を送信するか、不要な入力を消してから更新・再起動してください。','There is unsent text or an attachment. Send it or clear unwanted drafts in your chats and the new-task composer before updating or restarting.'],
    stale:['更新された本体を読み込む必要があります。作業記録を保持してサービスを再起動します。','The service needs to load the updated application files. Restart it with task records preserved.'],
    source:['ソースからの起動では、配布ページのWindows版から更新できます。','For a source checkout, get the Windows package from the release page.'],
    failed:['前回の更新は完了しませんでした。元のアプリと記録は保持しています。','The last update did not finish. Your previous application and records were retained.'],
    rolledback:['前回の更新を取り消し、元のアプリに戻しました。','The previous update was rolled back.'],
    version:['使用中の版','Installed version'], next:['新しい版','New version']
  };
  const text = key => words[key][globalThis.OIFI18n?.locale === 'ja' ? 0 : 1];
  const button = document.createElement('button'); button.type='button'; button.className='quiet app-update-button'; button.id='app-update-open';
  document.querySelector('.top-actions').append(button);
  const dialog=document.createElement('dialog'); dialog.id='app-update-dialog'; dialog.className='app-update-dialog';
  dialog.innerHTML='<div class="app-update-heading"><h2></h2><button type="button" class="quiet app-update-close">×</button></div><p class="app-update-version muted"></p><p class="app-update-description"></p><p class="app-update-status" role="status"></p><a class="app-update-release" target="_blank" rel="noopener noreferrer"></a><div class="app-update-actions"><button type="button" class="quiet app-update-check"></button><button type="button" class="quiet app-update-restart" hidden></button><button type="button" class="primary app-update-install" hidden></button></div>';
  document.body.append(dialog);
  const find = name => dialog.querySelector('.app-update-'+name);
  let info=null, maintenance=null, working=false, message='', error='';
  const unsent=()=>Boolean(globalThis.OIFWorkspace?.hasUnsentInputs());
  function requireSavedInputs(){if(unsent())throw new Error(text('unsent'));}
  async function call(path, body) {
    const options={cache:'no-store'};
    if(body!==undefined){
      const session=await fetch('/api/session',{cache:'no-store'});
      if(!session.ok)throw new Error(text('offline'));
      options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':(await session.json()).csrf_token};options.body=JSON.stringify(body);
    }
    const response=await fetch(path,options), value=await response.json();
    if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:text('offline'));
    return value;
  }
  function render(){
    const fresh=!!info?.available, stale=!!maintenance?.restart_required, busy=!!maintenance?.active_tasks;
    button.textContent=text(stale?'restartNeeded':fresh?'available':'updates');button.classList.toggle('has-update',fresh||stale);
    dialog.querySelector('h2').textContent=text('title');find('close').setAttribute('aria-label',text('close'));
    find('version').textContent=info?text('version')+' '+info.current_version+(fresh?' · '+text('next')+' '+info.available.version:''):'';
    find('description').textContent=busy?text('busy'):unsent()?text('unsent'):stale?text('stale'):fresh?text('preserve'):'';
    const last=info?.last_result?.status;
    find('status').textContent=error||(message?text(message):last==='failed'?text('failed'):last==='rolled_back'?text('rolledback'):info?.state==='unavailable'?text('offline'):info?.state==='current'?text('current'):!info?.installable&&fresh?text('source'):'');
    const link=find('release');link.textContent=text('releases');link.href=info?.available?.page||info?.release_page||'https://github.com/Chisiki1/objective-integrity-framework/releases';
    find('check').textContent=text('check');find('check').disabled=working;
    find('restart').textContent=text('restart');find('restart').hidden=!stale;find('restart').disabled=working||busy||unsent();
    find('install').textContent=text('install');find('install').hidden=!fresh||!info?.installable;find('install').disabled=working||busy||unsent();
    find('close').disabled=working;
  }
  async function refresh(force=false){
    working=true;message=force?'checking':'';error='';render();
    try{maintenance=await call('/api/maintenance');info=await call('/api/app-updates/check',{force});}
    catch(e){error=e.message;}finally{working=false;message='';render();}
  }
  button.onclick=()=>{dialog.showModal();refresh(false);};
  find('close').onclick=()=>dialog.close();dialog.addEventListener('cancel',e=>{if(working)e.preventDefault();});
  find('check').onclick=()=>refresh(true);
  find('restart').onclick=async()=>{
    working=true;message='restarting';error='';render();
    try{
      requireSavedInputs();
      const before=await call('/api/maintenance');
      await call('/api/maintenance/restart',{expected_instance_id:before.instance_id,only_if_idle:true});
      const deadline=Date.now()+60000;
      while(Date.now()<deadline){
        await new Promise(resolve=>setTimeout(resolve,1000));
        try{await fetch('/',{cache:'no-store'});const next=await call('/api/session');if(next.instance_id!==before.instance_id){location.reload();return;}}catch{/* Service is switching. */}
      }
      message='reconnect';
    }catch(e){error=e.message;message='';}finally{working=false;render();}
  };
  find('install').onclick=async()=>{
    working=true;message='preparing';error='';render();
    try{
      requireSavedInputs();
      const version=info.available.version;info=await call('/api/app-updates/prepare',{version});
      maintenance=await call('/api/maintenance');
      requireSavedInputs();
      const result=await call('/api/app-updates/install',{version,expected_instance_id:maintenance.instance_id});
      message='installing';render();
      if(result.close_window)window.chrome?.webview?.postMessage('oif-update-close');
    }catch(e){error=e.message;message='';working=false;render();}
  };
  globalThis.OIFI18n?.subscribe(render);render();
  // Start once after the ordinary page is ready. The server caches feed results
  // for six hours; this never adds work to a task or a model request.
  window.addEventListener('load',()=>refresh(false),{once:true});
})();
