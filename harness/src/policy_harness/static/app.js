'use strict';
const $ = id => document.getElementById(id);
const UI = globalThis.OIFI18n;
const P = globalThis.OIFPresentation;
const C = globalThis.OIFConversation;
const conversationThreads=new Map();
const recordPages=new Map(),progressPages=new Map();
let pinnedHistory=new Set(),operationCursor=0,artifactPage=0,listWindow=null;
const historyEnds=new Set();
let tablePage=0,turnPage=0,historyBefore=null,sourceBefore=null;
const conversationText=(ja,en)=>UI?.locale==='en'?en:ja;
const conversationKey=()=>state.taskId+'|'+state.progressFilter;
const latestSequence=()=>{let latest=0;for(const seq of state.events.keys())if(seq>latest)latest=seq;return latest;};
const plain = value => P ? P.friendly(value) : String(value??'');
const tr = (key,params) => UI ? UI.text(key,params) : key;
const uiMessage = (key,params={}) => ({uiKey:key,params});
const messageText = value => value?.uiKey ? tr(value.uiKey,Object.fromEntries(Object.entries(value.params||{}).map(([key,item])=>[key,item?.uiKey?messageText(item):item]))) : String(value||'');
const state = {csrf: '', instanceId:null, reconnecting:false, tasks: [], taskId: null, snapshot: null, events: new Map(), stream: null, generation: 0, refreshQueued: false, refreshAgain: false, approvals: [], approval: null, instructionDrafts:new Map(), instructionSending:new Set(), attachmentDrafts:new Map(), attachmentSending:new Set(), attachmentInputTask:null, attachmentMaxBytes:10*1024*1024, noticeMessage:'', noticeAction:null, streamMessage:uiMessage('記録を読み込んでいます。'), connectionKey:'接続を確認中', recoveryTask:null, evidenceTitle:'記録の詳細', settingsView:null, settingsError:''};
Object.assign(state,{requestSerial:0,responseOrders:new WeakMap(),frontiers:new Map(),conversationViews:new Map(),eventElements:new Map(),creationSending:false,progressFilter:'important',evidenceEvent:null,explanationSerial:0});
const statusNames = {created:'受付済み',running:'実行中',stopping:'停止処理中',stopped:'停止済み',completed:'完了',succeeded:'成功',failed:'失敗',unknown:'作用不明',pending:'未確定',waiting:'待機中',held:'保留',waiting_configuration:'接続設定が必要',awaiting_user:'ユーザー判断待ち',started:'開始',result:'結果',finished:'終了',applied:'反映済み',rejected:'不採用',accepted:'受領済み',error:'エラー',cancelled:'中断',recovered:'回復',deferred:'次回へ引継ぎ',adopt:'採用',reject:'不採用',investigate:'追加確認',create:'作成',use:'使用',improve:'改良',organize:'整理',merge:'統合',retire:'退役',configuration_required:'接続設定が必要'};
Object.assign(statusNames,{responded:'応答を受信',progress:'進行中',used:'実操作に使用',assessed:'使用結果を評価',use_unconfirmed:'使用を未確認',approved:'許可済み',denied:'許可されませんでした',changed:'変更済み'});
const stageNames = {task:'タスク',initialize:'目的と条件の確認',objective_contract:'目的と条件の確認',plan:'計画',select:'次の操作を選択',proposal:'操作の提案',pre:'事前評価',pre_assessment:'事前評価',post:'事後評価',post_assessment:'事後評価',review:'敵対レビュー',pre_review:'実行前レビュー',post_review:'実行後レビュー',research:'Web情報収集',web:'Web情報収集',web_plan:'情報収集の計画',web_before:'検索・取得の事前評価',web_after:'検索・取得の事後評価',execution:'操作の実行',execute:'操作の実行',decision:'確定判断',learning:'結果の学習',measurement:'効果の測定',skill_selection:'Skillの選択',skill:'Skillへの反映',ideas:'改善のアイデア',cycle:'操作単位の確認',finish:'終了条件の確認',final:'最終結果',finalize:'最終結果の確認',policy_amendment:'方針変更',child:'子タスク',delegate:'作業の分担',recovery:'状態の回復',stop:'停止',resume:'再開',readiness:'実行環境の確認',model:'モデルの判断',disposition:'意見を受けた最終判断'};
Object.assign(stageNames,{context:'作業の記憶を整理'});
const statusName = value => statusNames[value] ? tr(statusNames[value]) : value || tr('未観測');
Object.assign(statusNames,{source_update_required:'反映待ち',source_preparation_required:'出典の準備が必要',recorded:'保存済み',withdrawn:'撤回済み',retired:'退役'});
Object.assign(statusNames,{start_claimed:'開始を受付',dispatch_unknown:'開始結果未確認'});Object.assign(stageNames,{creation:'作業の受付'});
Object.assign(stageNames,{operation:'操作の実行',policy_admission_scope:'作業範囲の確認',policy_admission_blind_scenarios:'通常動作と影響の確認',policy_admission_blind_investigation:'原因と必要な確認',policy_admission_applicability:'実行条件の確認',policy_admission_prepared:'作業の準備'});
Object.assign(statusNames,{configuration_required:'接続設定が必要',attention_required:'確認・修正が必要',recovery_required:'状態の回復が必要',resume_requested:'再開',stop_requested:'停止を要求',interrupted:'中断・結果未確認',required:'確認が必要',reconciled:'作用を照合済み',cycle_complete:'操作単位の処理完了',executing:'実行中',pre_reviewed:'事前審査済み',result_recorded:'実結果を保存',post_reviewed:'事後審査済み',learned:'知識へ反映済み'});
Object.assign(stageNames,{task_plan:'目的・全条件と計画の確認',operation_proposal:'次の操作の提案',learning_proposal:'結果の学習・改善案',knowledge_application:'知識・Skillへの反映',independent_refutation:'独立した反証',completion_proposal:'全成果の完了判断',skill_cleanup_proposal:'Skillの整理・統合・退役判断',revision:'提案の修正',controller_restart:'審査済み更新のプロセス引継ぎ',web_acquisition:'検索・資料の取得',web_acquisition_learning:'取得結果の学習',web_knowledge_application:'情報収集の知識への反映',web_learning_review:'情報収集の学習案レビュー',web_learning_disposition:'情報収集の学習案への判断'});
Object.assign(statusNames,{received:'受領済み',observed_before:'事前の状態を記録',observed_after:'事後の状態を記録',projected:'表示用の記録',rejected_model_output:'応答の修正が必要',terminal_preserved:'停止状態を保持'});
Object.assign(stageNames,{source:'ユーザーの指示',delegation_scope:'子タスクの作業範囲',web_learning:'取得結果の学習',web_recovery:'情報収集の回復',completion:'終了条件の確認','web-learning-outcome':'情報収集の学習結果'});
Object.assign(statusNames,{revision_required:'修正が必要',rejected_format:'応答形式の修正が必要',partial:'一部未完了',reprepare:'準備を見直し中',refused_before_dispatch:'実行前に保留',succeeded_with_commit_readback:'保存を再確認済み'});
Object.assign(stageNames,{cleanup:'作業後の整理',learning_contract:'学習結果の確認',web_selection:'取得する情報の選択',policy_admission_reprepare:'実行準備の見直し',permission:'アクセス権限'});
function stageName(value){
  // Capacity partitions change call identity, not the meaning of the stage.
  // The untouched name and every individual partition remain in the full record.
  const name=String(value||'').replace(/(?::(?:context|page):\d+)+$/,'');
  function label(name){
    if(stageNames[name])return tr(stageNames[name]);
    for(const [suffix,key] of [['_research_query','情報収集の計画'],['_web','Web情報収集'],['_review','レビュー'],['_disposition','意見を受けた最終判断'],['_choices','改善案の検討'],['_before','前の評価'],['_after','後の評価']]){
      if(name.endsWith(suffix)){const base=label(name.slice(0,-suffix.length));if(base)return base+tr('・')+tr(key);}
    }
    return null;
  }
  return label(name)||tr('作業の記録');
}
const pretty = value => globalThis.OIFRecords.preview(value);
function readableMessage(value){const text=pretty(value);const known={'Configure the provider, model, context capacity and output capacity first.':tr('接続設定で、契約済みAPIの接続先・モデル・文脈と出力の容量を指定してください。'),'Explicit user request. Preserve in-flight effects and child states.':tr('停止を受け付けました。進行中の作用と子タスクの状態を保存します。')};return known[text]||text;}
const time = value => {const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString(UI?.locale==='en'?'en-US':'ja-JP',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});};
function node(tag, className, text) {const el = document.createElement(tag); if(className) el.className=className;if(text!==undefined)el.textContent=text;return el;}
function renderNotice(){const target=$('notice');target.textContent=messageText(state.noticeMessage);target.hidden=!state.noticeMessage;if(state.noticeAction){const button=node('button','quiet',tr(state.noticeAction.key));button.onclick=state.noticeAction.run;target.append(document.createTextNode(' '),button);}}
function note(message,action=null){state.noticeMessage=message;state.noticeAction=action;renderNotice();}
function streamMessage(key){state.streamMessage=uiMessage(key);$('stream-state').textContent=messageText(state.streamMessage);}
function showDetails(title,detail){
  state.evidenceTitle=title;state.evidenceEvent=Number.isSafeInteger(detail?.seq)&&detail.task_id?detail:null;state.explanationSerial++;
  $('evidence-title').textContent=tr(title);globalThis.OIFRecords.open(detail);
  $('evidence-explain').hidden=!state.evidenceEvent;$('evidence-explain').disabled=false;
  $('evidence-explanation').hidden=true;$('evidence-explanation').replaceChildren();$('evidence-dialog').showModal();
}
async function explainEvidence(event){
  if(!event)return;const serial=++state.explanationSerial,target=$('evidence-explanation');
  target.hidden=false;target.textContent=tr('この記録を説明しています…');$('evidence-explain').disabled=true;
  try{
    const reply=await api(`/api/tasks/${encodeURIComponent(event.task_id)}/events/${event.seq}/explanation`,{method:'POST',body:JSON.stringify({language:UI?.locale||'ja'})});
    if(serial!==state.explanationSerial)return;
    target.replaceChildren(node('h3','',tr('記録の解説')));
    for(const [key,label] of [['overview','要点'],['purpose','何のために'],['result','分かったこと'],['limits','この記録では分からないこと'],['next_step','次の確認']])if(reply.explanation?.[key])target.append(node('strong','',tr(label)),node('p','',reply.explanation[key]));
  }catch(error){if(serial===state.explanationSerial)target.textContent=tr('解説を取得できませんでした。原記録はそのまま確認できます。')+' '+readableMessage(error.message);}
  finally{if(serial===state.explanationSerial)$('evidence-explain').disabled=false;}
}
async function api(path, options={}) {
  const order=++state.requestSerial;
  const headers = {...options.headers};
  if(options.method && options.method!=='GET'){headers['Content-Type']='application/json';headers['X-CSRF-Token']=state.csrf;}
  const response = await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  const data = await response.json();
  if(data&&typeof data==='object')state.responseOrders.set(data,order);
  if(!response.ok){const message=response.status===401&&state.taskId?tr('接続情報を更新するには「表示を更新」を押してください。入力中の内容は保持しています。'):typeof data.detail==='string'?data.detail:pretty(data.detail || data);const error=new Error(message);error.status=response.status;error.kind=data.kind;throw error;}
  return data;
}
const creationStorageKey='oif.pending-submissions';
function pendingSubmissions(){
  const raw=globalThis.sessionStorage?.getItem(creationStorageKey);
  if(!raw)return [];
  const entries=JSON.parse(raw);
  if(!Array.isArray(entries)||entries.some(x=>!x||!/^[a-f0-9]{32}$/.test(x.id)||!/^[a-f0-9]{64}$/.test(x.payload_hash)||(x.task_id&&!/^[a-f0-9]{32}$/.test(x.task_id))))throw new Error(tr('creationStorageUnavailable'));
  return entries;
}
function saveSubmissions(entries){
  // Only opaque IDs and digests cross refresh. Never persist prompt or keys.
  if(!globalThis.sessionStorage)throw new Error(tr('creationStorageUnavailable'));
  const raw=JSON.stringify(entries.map(x=>({id:x.id,payload_hash:x.payload_hash,...(x.task_id?{task_id:x.task_id}:{})})));
  globalThis.sessionStorage.setItem(creationStorageKey,raw);
  if(globalThis.sessionStorage.getItem(creationStorageKey)!==raw)throw new Error(tr('creationStorageUnavailable'));
}
function saveSubmission(entry){const entries=pendingSubmissions();const index=entries.findIndex(x=>x.id===entry.id);if(index<0)entries.push(entry);else entries[index]=entry;saveSubmissions(entries);}
function acknowledgeSubmission(id){saveSubmissions(pendingSubmissions().filter(x=>x.id!==id));}
function creationReceipt(receipt,id){if(receipt?.submission_id!==id||!/^[a-f0-9]{32}$/.test(receipt?.task?.id||'')||(receipt.records_withheld&&(!receipt.accepted||receipt.action!=='create')))throw new Error(tr('responseMismatch'));globalThis.OIFWorkspace?.creationReceived(id);}
async function showCreationReceipt(receipt,entry){
  creationReceipt(receipt,entry.id);entry.task_id=receipt.task.id;saveSubmission(entry);
  if(receipt.records_withheld){state.generation++;state.taskId=receipt.task.id;renderSnapshot(receipt);}
  else await selectTask(receipt.task.id);
  acknowledgeSubmission(entry.id);
}
async function recoverCreation(entry,{open=true}={}){
  const receipt=await api('/api/submissions/'+encodeURIComponent(entry.id));creationReceipt(receipt,entry.id);
  entry.task_id=receipt.task.id;saveSubmission(entry);
  if(open)await showCreationReceipt(receipt,entry);
  else note(uiMessage('creationReceived'),{key:'受理済みの作業を開く',run:()=>showCreationReceipt(receipt,entry).catch(error=>note(error.message))});
}
async function submitTask(event){
  event.preventDefault();if(state.creationSending)return;
  const text=$('objective').value,generation=state.generation;if(!text.trim())return;
  state.creationSending=true;$('submit-task').disabled=true;note('');
  let entry,persisted=false,prepared;
  try{
    prepared=await globalThis.OIFWorkspace?.prepareCreate();
    const payload={objective:text,...(prepared?.data||{})};
    const bytes=new TextEncoder().encode(JSON.stringify(payload));
    const payload_hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
    entry=pendingSubmissions().find(x=>x.payload_hash===payload_hash)||{id:crypto.randomUUID().replaceAll('-',''),payload_hash};
    saveSubmission(entry);persisted=true;prepared?.bindSubmission(entry.id);
    if(entry.task_id){await recoverCreation(entry,{open:generation===state.generation});return;}
    const receipt=await api('/api/tasks',{method:'POST',body:JSON.stringify({...payload,submission_id:entry.id})});
    creationReceipt(receipt,entry.id);entry.task_id=receipt.task.id;saveSubmission(entry);
    if(generation===state.generation){await showCreationReceipt(receipt,entry);if($('objective').value===text)$('objective').value='';}
    else note(uiMessage('creationReceived'),{key:'受理済みの作業を開く',run:()=>showCreationReceipt(receipt,entry).catch(error=>note(error.message))});
  }catch(error){note(entry?.task_id?uiMessage('creationReceived'):persisted?uiMessage('creationUnknown'):uiMessage('creationStorageUnavailable'),persisted?{key:'送信結果を確認',run:()=>recoverCreation(entry).catch(error=>note(error.status===404?uiMessage('creationNotFound'):error.message))}:null);}
  finally{state.creationSending=false;$('submit-task').disabled=false;}
}
function rememberSnapshot(snapshot){
  const task=snapshot?.task;if(!task||typeof task.id!=='string')return false;
  const previous=state.frontiers.get(task.id),order=state.responseOrders.get(snapshot)||0;
  if(previous?.snapshot===snapshot)return true; // Presentation-only rerender.
  const version=Number.isSafeInteger(task.version)?task.version:null,source=Number.isSafeInteger(task.source_version)?task.source_version:null;
  if(previous){
    if(version!==null&&previous.version!==null&&version<previous.version)return false;
    if(source!==null&&previous.source!==null&&source<previous.source)return false;
    if(!snapshot.records_withheld&&source===previous.source&&previous.sourceHash&&task.source_hash!==previous.sourceHash)return false;
    if(previous.withheld&&!snapshot.records_withheld&&order<=previous.withheldOrder)return false;
    if(!snapshot.records_withheld&&order&&order<previous.order&&version===previous.version&&source===previous.source)return false;
  }
  state.frontiers.set(task.id,{version:version??previous?.version??null,source:source??previous?.source??null,
    sourceHash:task.source_hash||previous?.sourceHash||null,order:Math.max(order,previous?.order||0),
    withheld:Boolean(snapshot.records_withheld),withheldOrder:snapshot.records_withheld?Math.max(order,state.requestSerial,previous?.withheldOrder||0):0,snapshot});
  return true;
}
function captureConversation(){
  const panel=$('chat-panel'),key=conversationKey(),previous=state.conversationViews.get(key);
  if(!state.taskId||panel.hidden||!panel.clientHeight)return previous||{tail:true,top:0};
  const view={top:panel.scrollTop||0,tail:panel.scrollHeight-panel.clientHeight-panel.scrollTop<=32};
  const thread=conversationThreads.get(state.taskId);
  for(const [seq,element] of state.eventElements)if(thread)thread.open.set(seq,element.open);
  state.conversationViews.set(key,view);return view;
}
function restoreConversation(view){
  const panel=$('chat-panel');if(!view)return;
  panel.scrollTop=view.tail?panel.scrollHeight:view.top||0;
  state.conversationViews.set(conversationKey(),view);
  $('follow-latest').hidden=Boolean(view.tail);
}
async function loadRecordPage(params,view={tail:false,top:0}){
  const taskId=state.taskId,generation=state.generation;
  try{
    const query=Object.entries(params).map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');
    const reply=await api('/api/tasks/'+encodeURIComponent(taskId)+'?view=conversation&'+query);
    if(taskId!==state.taskId||generation!==state.generation)return false;
    if(!renderSnapshot(reply,params))return false;
    renderEvents(view);return reply;
  }catch(error){note(error.message);return false;}
}
function currentGroups(){return C.project(state.snapshot?.task,[...state.events.values()]);}
function setProgressOpen(group,open){
  const thread=conversationThreads.get(state.taskId);if(!thread)return;
  if(open)thread.expanded.add(group.id);else{C.collapse(thread,group);for(const filter of ['important','all'])progressPages.delete(state.taskId+'|'+group.id+'|'+filter);}
  // Clear DOM state too, before the next capture can save it again.
  if(!open)for(const event of group.events){const element=state.eventElements.get(event.seq);if(element)element.open=false;}
  const view=captureConversation();renderEvents(view);if(group.id===thread.latest)restoreConversation({tail:true,top:0});
}
function renderConversationControls(groups,thread){
  const group=groups.at(-1),button=$('toggle-current-progress');button.hidden=!group;
  if(!group)return;const open=thread.expanded.has(group.id);
  button.textContent=conversationText('今回の進捗を'+(open?'畳む':'開く'),'Show '+(open?'less':'progress')+' for this instruction');
  button.setAttribute('aria-expanded',String(open));button.onclick=()=>setProgressOpen(group,!thread.expanded.has(group.id));
}
function badge(value,label){return node('span','badge '+String(value || '').replace(/[^a-z_]/g,''),label||statusName(value));}
function detailButton(label,data){const button=node('button','event-details',tr(label));button.addEventListener('click',()=>showDetails(label,data));return button;}
function empty(target,text){target.replaceChildren(node('p','empty',text));}
function summary(event){
  const d=event.detail||{},flags=[],reasons=[],lines=[];
  // User/model prose is literal, even when it matches an interface notice.
  const add=value=>{if(typeof value==='string'&&value.trim())lines.push(value);};
  const reason=value=>{if(typeof value==='string'&&value.trim())reasons.push(value);};
  function result(value,depth=0){
    if(typeof value==='string'){add(value);return;}
    if(!value||typeof value!=='object'||Array.isArray(value)||depth>4)return;
    const verdicts={proceed:'続行の判断',revise:'修正が必要',hold:'保留の判断'};
    const assessments={expected:'実行前の見通し',succeeded:'評価結果：成功',failed:'評価結果：失敗',unknown:'評価結果：未確認'};
    if(verdicts[value.verdict])flags.push(tr(verdicts[value.verdict]));
    if(assessments[value.success])flags.push(tr(assessments[value.success]));
    const unmet=Array.isArray(value.acceptance)?value.acceptance.filter(item=>item.achieved===false):[];
    const unresolved=Array.isArray(value.unresolved)?value.unresolved:[];
    if(value.achieved===false||unmet.length||unresolved.length)flags.push(tr('主目的は未完了です。'));
    unmet.forEach(item=>reason(item.criterion));unresolved.forEach(reason);
    if(value.effect==='unknown')flags.push(tr('作用不明'));
    if(value.operation_id&&value.status)flags.push(tr('結果：')+statusName(value.status));
    if(value.result_status)flags.push(tr('結果：')+statusName(value.result_status));
    for(const key of ['fault','first_fault','error'])reason(value[key]?.message);
    for(const key of ['commit_observation','projection_status']){
      const observation=value[key];if(!observation)continue;
      if(observation.status)flags.push(tr('復旧：')+statusName(observation.status));
      if(observation.state==='pending')flags.push(tr('記録の反映を確認中'));
      result(observation,depth+1);
    }
    for(const key of ['summary','outcome_summary','message','error','first_fault','reason','rationale','stdout','stderr'])add(value[key]);
    if(Array.isArray(value.mistakes))value.mistakes.forEach(add);
    if(!lines.length)add(value.objective_link);
    if(value.data)add(value.data.summary);
    for(const key of ['result','response','assessment'])if(value[key])result(value[key],depth+1);
    if(Array.isArray(value.assessments)){
      // Put contrary/unknown assessments first, even when the model call succeeded.
      const adverse=item=>['failed','unknown'].includes((item.assessment||item).success)?0:1;
      [...value.assessments].sort((a,b)=>adverse(a)-adverse(b)).forEach(item=>result(item.assessment||item,depth+1));
    }
  }
  result(d);
  if(d.source){add(d.source.text);if(d.source.filename)add(tr('添付ファイル')+': '+d.source.filename);}
  if(!lines.length&&d.operation)add(d.operation.purpose);
  if(!lines.length&&typeof d.query==='string')add(tr('調査：')+d.query);
  return [...new Set([...flags,...reasons,...lines])].join('\n');
}
function eventNodes(event){
  const article=node('details','event-card '+(event.status==='started'?'started':''));
  article.open=conversationThreads.get(state.taskId)?.open.get(event.seq)??false;
  article.hidden=state.progressFilter==='important'&&P&&!P.important(event);
  article.setAttribute('data-seq',String(event.seq));state.eventElements.set(event.seq,article);
  const statusLabel=event.status==='succeeded'&&event.detail?.actor&&event.detail?.result?tr('応答を受信'):statusName(event.status);
  const meta=node('div','event-meta');meta.append(badge(event.status,statusLabel),node('time','',time(event.created_at)));
  const elapsed=event.detail?.measurement?.elapsed_seconds??event.detail?.elapsed_seconds;if(typeof elapsed==='number')meta.append(node('span','',elapsed.toFixed(1)+tr('秒')));
  const raw=summary(event),view=P?P.describe(event,state.snapshot,raw):{overview:raw,outcome:raw};
  const text=view.overview||tr('作業の記録');
  const heading=node('summary','event-heading');heading.append(node('span','event-stage',stageName(event.stage)),badge(event.status,statusLabel),node('span','event-overview',text.length>180?text.slice(0,180)+'…':text));
  const body=node('div','event-narrative');
  for(const [key,label] of [['purpose','目的'],['outcome','結果・現在の状態'],['next','予定している操作']])if(view[key]){body.append(node('strong','',tr(label)),node('p','event-summary',view[key].length>2500?view[key].slice(0,2500)+'…':view[key]));}
  const explain=node('button','event-details',tr('解説'));explain.onclick=()=>{showDetails('根拠と結果の全文',event);explainEvidence(event);};
  article.append(heading,meta,body,detailButton('根拠と結果の全文',event),explain);
  const row=node('tr');row.setAttribute('data-seq',String(event.seq));row.append(node('td','',time(event.created_at)),node('td','',stageName(event.stage)));
  const status=node('td');status.append(badge(event.status,statusLabel));const content=node('td','',text.length>320?text.slice(0,320)+'…':text);content.append(detailButton('全文',event));row.append(status,content);return [article,row];
}
function renderEvents(savedView){
  let view=savedView||captureConversation();
  const groups=currentGroups(),thread=C.reconcile(conversationThreads.get(state.taskId),groups);
  conversationThreads.set(state.taskId,thread);
  if(thread.resetFollow){
    for(const filter of ['important','all'])state.conversationViews.set(state.taskId+'|'+filter,{tail:true,top:0});
    view={tail:true,top:0};thread.resetFollow=false;turnPage=0;
  }
  state.eventElements.clear();const chat=$('chat-events'),rows=$('event-rows');chat.replaceChildren();rows.replaceChildren();
  const turnEnd=Math.max(0,groups.length-turnPage*20),turnStart=Math.max(0,turnEnd-20);
  const turnPager=node('div','progress-pages');
  if(turnStart||sourceBefore){const older=node('button','quiet',conversationText('以前の指示と結果','Earlier instructions and results'));older.onclick=async()=>{
    if(!turnStart&&sourceBefore&&!await loadRecordPage({source_before:sourceBefore}))return;
    turnPage++;renderEvents({tail:false,top:0});};turnPager.append(older);}
  if(turnPage){const latest=node('button','quiet',conversationText('最新の指示と結果','Latest instructions and results'));latest.onclick=()=>{turnPage=0;renderEvents({tail:true,top:0});};turnPager.append(latest);}
  if(turnPager.children.length)chat.append(turnPager);
  for(const group of groups.slice(turnStart,turnEnd)){
    const section=node('section','conversation-turn');section.setAttribute('data-turn',group.id);
    const meta=node('div','turn-meta');meta.append(node('strong','',(group?conversationText('指示 '+(group.index+1),'Instruction '+(group.index+1)):conversationText('以前の指示','Earlier instruction'))),node('time','',time(group.source.created_at)));
    const current=group.id===thread.latest;
    meta.append(badge(group.final?'completed':current?state.snapshot?.task.status:'recorded',group.final?tr('完了'):current?statusName(state.snapshot?.task.status):conversationText('次の指示へ引継ぎ','Continued with the next instruction')));
    section.append(meta,node('p','turn-instruction',group.source.text||group.source.filename||''));
    const progress=node('details','turn-progress');progress.open=thread.expanded.has(group.id);
    const visible=group.events.filter(e=>state.progressFilter==='all'||!P||P.important(e));
    const label=node('summary','turn-progress-heading',conversationText('作業の進捗','Work progress')+' · '+visible.length);
    const content=node('div','turn-events');
    const all=group.events.length<=80?group.events:visible,offset=progressPages.get(state.taskId+'|'+group.id+'|'+state.progressFilter)||0;
    const end=Math.max(0,all.length-offset),start=Math.max(0,end-80);
    const firstSeq=group.events[0]?.seq,historyKey=state.taskId+'|'+group.id,earlier=Boolean(state.snapshot?.display_projection&&!historyEnds.has(historyKey)&&historyBefore&&firstSeq!==group.start);
    if(progress.open){
      if(start||offset||earlier){const pager=node('div','progress-pages');
        if(!start&&earlier){const load=node('button','quiet',conversationText('以前の進捗を読み込む','Load earlier progress'));load.onclick=async()=>{load.disabled=true;const next=groups[groups.indexOf(group)+1],oldCount=group.events.length,filter=state.progressFilter;
          const reply=await loadRecordPage({before:firstSeq||next?.start||0});if(!reply)return;
          const updated=currentGroups().find(g=>g.id===group.id),added=(updated?.events.length||0)-oldCount;
          if(!added||!reply.record_page?.event_before)historyEnds.add(historyKey);
          if(added){
            const selected=updated.events.length<=80?updated.events:updated.events.filter(e=>filter==='all'||!P||P.important(e));
            const boundary=firstSeq?selected.findIndex(e=>e.seq>=firstSeq):selected.length;
            progressPages.set(state.taskId+'|'+group.id+'|'+filter,selected.length-(boundary<0?selected.length:boundary));
          }
          renderEvents({tail:false,top:0});};pager.append(load);}
        if(start){const older=node('button','quiet',conversationText('前の進捗','Earlier progress'));older.onclick=()=>{progressPages.set(state.taskId+'|'+group.id+'|'+state.progressFilter,offset+80);renderEvents({tail:false,top:0});};pager.append(older);}
        if(offset){const newer=node('button','quiet',conversationText('新しい進捗','Newer progress'));newer.onclick=()=>{progressPages.set(state.taskId+'|'+group.id+'|'+state.progressFilter,Math.max(0,offset-80));renderEvents({tail:true,top:0});};pager.append(newer);}content.append(pager);}
      for(const event of all.slice(start,end)){const [article]=eventNodes(event);content.append(article);}
    }
    if(!visible.length)content.append(node('p','empty',conversationText(state.progressFilter==='important'?'重要な進捗が届くと、ここに表示します。':'進捗を待っています。',state.progressFilter==='important'?'Important updates will appear here.':'Waiting for progress.')));
    progress.append(label,content);
    progress.addEventListener('toggle',()=>{
      if(progress.isConnected===false)return;
      if(progress.open===thread.expanded.has(group.id))return;
      if(progress.open)thread.expanded.add(group.id);else{
        C.collapse(thread,group);for(const filter of ['important','all'])progressPages.delete(state.taskId+'|'+group.id+'|'+filter);for(const event of group.events){const el=state.eventElements.get(event.seq);if(el)el.open=false;}
      }
      renderEvents(captureConversation());
    });
    section.append(progress);
    if(group.final){
      const result=node('section','result-card turn-result'),completion=group.final.completion||group.final;
      result.append(node('h2','',tr('作業の結果')));
      for(const key of ['summary','answer','result','report'])if(completion[key])result.append(node('p','',plain(pretty(completion[key]))));
      result.append(detailButton('終了判断・全結果',group.finalRecord?{_record_url:group.finalRecord}:group.final));section.append(result);
    }
    chat.append(section);
  }
  const sorted=[...(listWindow||state.events.values())].sort((a,b)=>b.seq-a.seq),start=listWindow?0:tablePage*100;
  for(const event of sorted.slice(start,start+100)){const saved=state.eventElements.get(event.seq);const [,row]=eventNodes(event);if(saved)state.eventElements.set(event.seq,saved);else state.eventElements.delete(event.seq);rows.append(row);}
  let pager=$('stage-pages');if(!pager){pager=node('div','progress-pages');pager.id='stage-pages';$('list-panel').append(pager);}pager.replaceChildren();
  if(tablePage){const prev=node('button','quiet',conversationText('新しい工程','Newer stages'));prev.onclick=()=>{tablePage=0;listWindow=null;renderEvents();};pager.append(prev);}
  if(start+100<sorted.length||historyBefore){const next=node('button','quiet',conversationText('前の工程','Earlier stages'));next.onclick=async()=>{if(state.snapshot?.display_projection){const before=sorted.slice(start,start+100).at(-1)?.seq;if(!before)return;const reply=await loadRecordPage({before});if(!reply)return;listWindow=reply.events;}tablePage++;renderEvents();};pager.append(next);}
  renderConversationControls(groups,thread);restoreConversation(view);
}
function appendEvent(event){
  if((event.task_id&&event.task_id!==state.taskId)||state.events.has(event.seq))return;
  const view=captureConversation();state.events.set(event.seq,event);
  if(state.events.size>1600){
    const ordered=[...state.events.keys()].sort((a,b)=>a-b);
    for(const seq of ordered){if(state.events.size<=1400)break;if(pinnedHistory.has(seq)||state.eventElements.has(seq))continue;state.events.delete(seq);}
    // Removed pages remain retrievable from the original SQLite records.
    historyBefore=Math.min(...state.events.keys());
  }
  if(view.tail){const latest=currentGroups().at(-1);if(latest)progressPages.delete(state.taskId+'|'+latest.id+'|'+state.progressFilter);}renderEvents(view);
  // Source receipts must be visible even while the snapshot request is pending.
  if(event.stage==='source'&&event.status==='received'&&state.snapshot)renderSourceHistory(state.snapshot.task);
}
function taskListProjection(task){const frontier=state.frontiers.get(task.id);if(frontier&&(frontier.withheld||task.version<frontier.version||task.source_version<frontier.source))return frontier.snapshot.task;return task;}
function renderTaskList(){if(globalThis.OIFWorkspace)return globalThis.OIFWorkspace.renderTasks();const list=$('task-list');list.replaceChildren();if(!state.tasks.length){empty(list,tr('まだタスクはありません。'));return;}for(const task of state.tasks.filter(t=>!t.parent_id).map(taskListProjection)){const b=node('button','task-link'+(task.id===state.taskId?' active':''));b.append(node('strong','',task.objective??(tr('記録表示を保留中：')+task.id)),node('small','',`${statusName(task.status)}${task.created_at?' · '+time(task.created_at):''}`));b.addEventListener('click',()=>selectTask(task.id).catch(e=>note(e.message)));list.append(b);}}
Object.assign(state,{listSerial:0,listCursor:'',listNext:null,listHistory:[],listQuery:'',listStream:null,listRefreshing:false,listRefreshAgain:false});
async function loadTasks(options={}){
  const filters=globalThis.OIFWorkspace?.listQuery?.()||{};
  const key=JSON.stringify(filters);
  if(options.reset||key!==state.listQuery){state.listCursor='';state.listHistory=[];state.listQuery=key;}
  if(options.cursor!==undefined)state.listCursor=options.cursor;
  const cursor=state.listCursor,serial=++state.listSerial,query=new URL('http://localhost/').searchParams;
  for(const [name,value] of Object.entries(filters))query.set(name,value);
  if(cursor)query.set('cursor',cursor);
  let reply;
  try{reply=await api('/api/tasks'+(query.size?'?'+query:''));}
  catch(error){if(error.kind!=='ConfigurationRequired')throw error;reply=await api('/api/recovery/tasks'+(cursor?'?cursor='+encodeURIComponent(cursor):''));note(error.message);}
  if(serial!==state.listSerial)return;
  state.tasks=reply.tasks;state.folders=reply.folders||[];state.listNext=reply.next_cursor||null;renderTaskList();renderTaskPager();
}
function renderTaskPager(){
  $('task-pager')?.remove?.();
  if(!state.listNext&&!state.listHistory.length)return;
  const pager=node('div','progress-pages');pager.id='task-pager';
  const previous=node('button','quiet',conversationText('前へ','Previous')),next=node('button','quiet',conversationText('次へ','Next'));
  previous.disabled=!state.listHistory.length;next.disabled=!state.listNext;
  previous.onclick=()=>loadTasks({cursor:state.listHistory.pop()||''}).catch(e=>note(e.message));
  next.onclick=()=>{state.listHistory.push(state.listCursor);return loadTasks({cursor:state.listNext}).catch(e=>note(e.message));};
  pager.append(previous,next);$('task-list').append(pager);
}
async function refreshTaskList(){
  if(state.listRefreshing){state.listRefreshAgain=true;return;}state.listRefreshing=true;
  try{do{state.listRefreshAgain=false;await loadTasks();}while(state.listRefreshAgain);}
  catch(error){note(error.message);}finally{state.listRefreshing=false;}
}
function connectTaskList(){
  state.listStream?.close();state.listStream=new EventSource('/api/task-list/events');
  state.listStream.addEventListener('changed',refreshTaskList);
}
window.addEventListener('beforeunload',()=>state.listStream?.close());
function showRecoveryTask(task,detail,receipt=null){
  if(!rememberSnapshot(receipt||{task,detail,records_withheld:true}))return;
  state.stream?.close();state.stream=null;state.snapshot=null;state.taskId=task.id;state.recoveryTask={task,detail,receipt};
  $('welcome').hidden=true;$('task-view').hidden=true;$('loading').hidden=false;history.replaceState(null,'','#task='+encodeURIComponent(task.id));
  const target=$('loading-message');target.replaceChildren(node('p','',tr('recoveryTask',{task:task.id,status:statusName(task.status)})));
  if(receipt?.accepted)target.append(node('p','',tr('withheldReceipt',{task:task.id})));
  target.append(detailButton('原記録',receipt||{task,detail}));
  const button=node('button','quiet',tr('このタスクを停止'));button.onclick=async()=>{button.disabled=true;const generation=state.generation;try{const result=await api(`/api/tasks/${encodeURIComponent(task.id)}/stop`,{method:'POST',body:'{}'});if(state.taskId===task.id&&state.generation===generation)showRecoveryTask(result.task,result.detail||'',result);note(uiMessage('taskNotice',{task:task.id,detail:result.detail||uiMessage('停止操作を受け付けました。')}));}catch(error){note(error.message);button.disabled=false;}};target.append(button);
}
function listObjects(id,values,none,render){
  const container=$(id);container.replaceChildren();if(!values?.length){empty(container,none);return;}
  const key=state.taskId+'|'+id,page=Math.min(recordPages.get(key)||0,Math.floor((values.length-1)/30));
  const ordered=id==='operations'?[...values].reverse():values;
  ordered.slice(page*30,(page+1)*30).forEach(value=>{const item=node('div','detail-item');render(item,value);container.append(item);});
  if(values.length>30){const pager=node('div','progress-pages');pager.append(node('span','',`${page*30+1}–${Math.min((page+1)*30,values.length)} / ${values.length}`));
    for(const [delta,label] of [[-1,conversationText('前へ','Previous')],[1,conversationText('次へ','Next')]]){const button=node('button','quiet',label);button.disabled=page+delta<0||(page+delta)*30>=values.length;button.onclick=()=>{recordPages.set(key,page+delta);listObjects(id,values,none,render);};pager.append(button);}container.append(pager);}
}
function instructionDraft(taskId){if(!state.instructionDrafts.has(taskId))state.instructionDrafts.set(taskId,{text:'',message:'',isError:false});return state.instructionDrafts.get(taskId);}
function renderInstruction(task){
  const draft=instructionDraft(task.id),input=$('instruction-text');if(input.value!==draft.text)input.value=draft.text;
  const sending=state.instructionSending.has(task.id)||state.attachmentSending.has(task.id);$('submit-instruction').disabled=sending||!task.source_hash||!draft.text.trim();$('submit-instruction').textContent=tr(state.instructionSending.has(task.id)?'指示を送信中…':'指示を送信');
  $('instruction-message').className='instruction-message'+(draft.isError?' inline-error':'');$('instruction-message').textContent=messageText(draft.message)||(!task.source_hash?tr('このタスクの指示履歴を取得できていません。接続とタスクの状態を確認してください。'):'');
}
function renderSourceHistory(task){
  const groups=C.project(task,[...state.events.values()]),sources=[...(task.source_history||[])];
  for(const group of groups)if(!sources.some(s=>s.id===group.id))sources.push(group.source);
  const latest=groups.at(-1);$('current-source-label').textContent=conversationText('最新の指示','Latest instruction');$('current-source-text').textContent=latest?.source.text||'';
  $('current-source-status').replaceChildren(badge(latest?.source.status,latest?.source.status==='pending'?tr('反映待ち'):statusName(latest?.source.status)));
  $('source-count').textContent=state.snapshot?.record_page?.source_count||sources.length;
  $('source-state').textContent=tr('sourceVersion',{version:task.source_version??'—',pending:state.snapshot?.record_page?.pending_source_count??(sources.every(source=>source.status)?sources.filter(source=>source.status==='pending').length:'—')});
  listObjects('source-history',sources,tr('指示履歴はまだ取得できていません。'),(item,source)=>{const attached=source.kind==='attachment'||Boolean(source.filename);const meta=node('div','source-meta');meta.append(node('strong','',attached?tr('添付ファイル'):source.kind==='initial'?tr('最初の依頼'):tr('ユーザーの指示')));if(source.created_at)meta.append(node('time','',time(source.created_at)));if(source.status){const label=badge(source.status);if(source.status==='pending')label.textContent=tr('反映待ち');meta.append(label);}const text=attached?[source.filename,(source.bytes??source.size_bytes)!==undefined?`${source.bytes??source.size_bytes} bytes`:''].filter(Boolean).join(' · '):typeof source.text==='string'?source.text:pretty(source);item.append(meta,node('p','source-text',text),detailButton('指示の出典と反映状態',source));});
  if(sourceBefore){const older=node('button','quiet',conversationText('以前の指示を読み込む','Load earlier instructions'));older.onclick=()=>loadRecordPage({source_before:sourceBefore});$('source-history').append(older);}
}
function attachmentDraft(taskId){if(!state.attachmentDrafts.has(taskId))state.attachmentDrafts.set(taskId,{file:null,message:'',isError:false});return state.attachmentDrafts.get(taskId);}
function renderAttachment(task){
  const draft=attachmentDraft(task.id);if(state.attachmentInputTask!==task.id){$('attachment-file').value='';state.attachmentInputTask=task.id;}
  $('attachment-selected').textContent=draft.file?tr('selectionFile',{name:draft.file.name,bytes:draft.file.size}):'';
  $('submit-attachment').disabled=state.attachmentSending.has(task.id)||state.instructionSending.has(task.id)||!task.source_hash||!draft.file||draft.file.size>state.attachmentMaxBytes;
  $('submit-attachment').textContent=tr(state.attachmentSending.has(task.id)?'添付を送信中…':'添付を送信');
  $('attachment-limit').textContent=tr('fileLimit',{limit:state.attachmentMaxBytes/(1024*1024)});
  $('attachment-message').className='instruction-message'+(draft.isError?' inline-error':'');$('attachment-message').textContent=messageText(draft.message);
}
async function sourceFailure(error,draft,taskId,generation,retained){
  draft.isError=true;
  if(error.status===409&&error.message.startsWith('SOURCE_HASH_CONFLICT:')){
    draft.message=uiMessage('sourceConflict',{retained:uiMessage(retained)});
    try{const snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'?view=conversation&after='+latestSequence()+'&operation_before='+operationCursor+'&artifact_page='+artifactPage);if(state.taskId===taskId&&state.generation===generation)renderSnapshot(snapshot);}catch(refreshError){note(uiMessage('最新の履歴はまだ取得できていません。'));}
  }else draft.message=error.status?error.message:uiMessage('sourceUnknown',{retained:uiMessage(retained)});
}
function checkSourceReceipt(snapshot,taskId,action){
  if(snapshot?.task?.id!==taskId||(snapshot.records_withheld&&(!snapshot.accepted||snapshot.action!==action))||(!snapshot.records_withheld&&(!/^[a-fA-F0-9]{64}$/.test(snapshot.task.source_hash||'')||!Array.isArray(snapshot.task.source_history))))throw new Error(tr('responseMismatch'));
}
function fileBase64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>{const result=String(reader.result),separator=result.indexOf(',');if(separator<0)reject(new Error(tr('ファイルを読み取れませんでした。')));else resolve(result.slice(separator+1));};reader.onerror=reader.onabort=()=>reject(new Error(tr('ファイルを読み取れませんでした。')));reader.readAsDataURL(file);});}
async function submitAttachment(event){
  event.preventDefault();const taskId=state.taskId,task=state.snapshot?.task;
  if(!taskId||task?.id!==taskId||state.instructionSending.has(taskId)||state.attachmentSending.has(taskId))return;
  const draft=attachmentDraft(taskId),file=draft.file,expected=task.source_hash,generation=state.generation;
  if(!file||!expected||file.size>state.attachmentMaxBytes)return;
  state.attachmentSending.add(taskId);draft.message='';draft.isError=false;renderAttachment(task);renderInstruction(task);
  try{
    const encoded=await fileBase64(file);
    const snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'/attachments',{method:'POST',body:JSON.stringify({filename:file.name,base64:encoded,expected_source_hash:expected})});
    checkSourceReceipt(snapshot,taskId,'attachment');
    rememberSnapshot(snapshot);
    if(draft.file===file)draft.file=null;draft.message=snapshot.records_withheld?uiMessage('withheldReceipt',{task:taskId}):uiMessage('receivedAttachment');
    if(state.taskId===taskId&&state.generation===generation){if(!draft.file)$('attachment-file').value='';renderSnapshot(snapshot);}
  }catch(error){await sourceFailure(error,draft,taskId,generation,'選択したファイル');}
  finally{state.attachmentSending.delete(taskId);if(state.taskId===taskId&&state.snapshot?.task?.id===taskId){renderAttachment(state.snapshot.task);renderInstruction(state.snapshot.task);}}
}
function artifactLink(taskId,artifact){
  // The checked operation and content hash identify this historical result.
  const path=artifact?.relative_path,operation=artifact?.download_operation_id,hash=artifact?.sha256;
  if(typeof path!=='string'||!path||/[\\\x00]/.test(path))return null;
  // The server projects native absolute paths only in the task's current full
  // mode and checks that permission again when opening this bound artifact.
  const parts=path.replace(/^(?:[A-Za-z]:\/|\/{1,2})/,'').split('/');
  if(parts.some(p=>!p||p==='.'||p==='..'||p.includes(':')))return null;
  if(typeof operation!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(operation)||typeof hash!=='string'||!/^[a-fA-F0-9]{64}$/.test(hash))return null;
  // Match Python quote(..., safe='') including the extra RFC3986 punctuation.
  const component=value=>encodeURIComponent(value).replace(/[!'()*]/g,char=>'%'+char.charCodeAt(0).toString(16).toUpperCase());
  const url=`/api/tasks/${component(taskId)}/artifacts/${path.split('/').map(component).join('/')}?operation_id=${component(operation)}&sha256=${hash.toLowerCase()}`;
  if(artifact.download_url!==url)return null;
  const link=node('a','artifact-link',path);link.href=url;link.title=tr('保存内容を確認したファイル')+' · '+path;link.setAttribute('aria-haspopup','dialog');
  link.onclick=event=>{if(event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||event.button>0||!globalThis.OIFArtifacts)return;event.preventDefault();globalThis.OIFArtifacts.open({url,path,operation,sha256:hash});};return link;
}
async function submitInstruction(event){
  event.preventDefault();const taskId=state.taskId,task=state.snapshot?.task;
  if(!taskId||task?.id!==taskId||state.instructionSending.has(taskId)||state.attachmentSending.has(taskId))return;
  const draft=instructionDraft(taskId),text=$('instruction-text').value,expected=task.source_hash,generation=state.generation;draft.text=text;
  if(!text.trim()||!expected)return;
  state.instructionSending.add(taskId);draft.message='';draft.isError=false;renderInstruction(task);renderAttachment(task);
  const path='/api/tasks/'+encodeURIComponent(taskId);
  try{
    const snapshot=await api(path+'/instructions',{method:'POST',body:JSON.stringify({text,expected_source_hash:expected})});
    checkSourceReceipt(snapshot,taskId,'instruction');
    rememberSnapshot(snapshot);
    if(draft.text===text)draft.text='';draft.message=snapshot.records_withheld?uiMessage('withheldReceipt',{task:taskId}):uiMessage('receivedInstruction');
    if(state.taskId===taskId&&state.generation===generation)renderSnapshot(snapshot);
  }catch(error){await sourceFailure(error,draft,taskId,generation,'入力文');}
  finally{state.instructionSending.delete(taskId);if(state.taskId===taskId&&state.snapshot?.task?.id===taskId){renderInstruction(state.snapshot.task);renderAttachment(state.snapshot.task);}}
}
function renderArtifactList(snapshot){
  const target=$('task-artifacts');target.replaceChildren();const groups=C.project(snapshot.task,snapshot.events||[]);
  const older=node('details','artifact-older'),olderBody=node('div','artifact-versions');older.append(node('summary','',conversationText('以前の版','Previous versions')),olderBody);
  let count=0,oldCount=0;
  const valid=artifact=>Boolean(artifactLink(snapshot.task.id,artifact));
  const items=C.artifacts(snapshot.artifact_operations||snapshot.operations,valid);
  if(snapshot.artifact_operations){const latest=new Map(items.map(item=>[item.artifact.relative_path,item.artifact.sha256]));
    for(const old of C.artifacts(snapshot.operations,valid))if(latest.has(old.artifact.relative_path)&&latest.get(old.artifact.relative_path)!==old.artifact.sha256)items.push({...old,latest:false});}
  for(const item of items){
    const link=artifactLink(snapshot.task.id,item.artifact);if(!link)continue;
    let group=null;for(const candidate of groups)if(Date.parse(item.operation.started_at)>=Date.parse(candidate.source.created_at))group=candidate;
    const row=node('div','artifact-entry');row.append(link,node('small','',conversationText(item.latest?'最新版':'以前の版',item.latest?'Latest version':'Previous version')+' · '+(group?conversationText('指示 '+(group.index+1),'Instruction '+(group.index+1)):conversationText('以前の指示','Earlier instruction'))+' · '+time(item.operation.started_at)));
    if(item.latest){target.append(row);count++;}else{olderBody.append(row);oldCount++;}
  }
  if(oldCount)target.append(older);
  if(snapshot.record_page?.artifact_more||artifactPage){const pager=node('div','progress-pages');
    for(const [page,label] of [[0,conversationText('最新の成果物','Latest artifacts')],[artifactPage+1,conversationText('他の成果物','More artifacts')]]){if(page===0&&!artifactPage||page>0&&!snapshot.record_page?.artifact_more)continue;const button=node('button','quiet',label);button.onclick=()=>loadRecordPage({artifact_page:page,operation_before:operationCursor});pager.append(button);}target.append(pager);}
  $('artifact-result').hidden=!(count||oldCount||artifactPage);
}
function renderSnapshot(snapshot,pageParams=null){
  if(!rememberSnapshot(snapshot)||snapshot.task.id!==state.taskId)return false;
  if(snapshot.records_withheld){showRecoveryTask(snapshot.task,snapshot.detail,snapshot);note(snapshot.accepted?uiMessage('withheldReceipt',{task:snapshot.task.id}):snapshot.detail);return;}
  if(pageParams){
    if(pageParams.before){historyBefore=snapshot.record_page?.event_before||0;pinnedHistory=new Set((snapshot.events||[]).map(e=>e.seq));}
    if(pageParams.source_before)sourceBefore=snapshot.record_page?.source_before||0;
    if('operation_before' in pageParams)operationCursor=pageParams.operation_before;
    if('artifact_page' in pageParams)artifactPage=pageParams.artifact_page;
  }
  const conversation=captureConversation();
  if(!snapshot.display_projection)snapshot.task={...snapshot.task,source_history:(snapshot.task.source_history||[]).map((source,index)=>({...source,_index:index}))};
  if(state.snapshot?.task?.id===snapshot.task.id){
    const sources=new Map((state.snapshot.task.source_history||[]).map(source=>[source.id,source]));
    for(const [index,source] of (snapshot.task.source_history||[]).entries()){
      const previous=sources.get(source.id);
      sources.set(source.id,{...previous,...source,_index:source._index??(snapshot.display_projection?previous?._index:index)});
    }
    snapshot.task={...snapshot.task,source_history:[...sources.values()].sort(C.sourceOrder)};
  }
  if(historyBefore===null)historyBefore=snapshot.record_page?.event_before||0;
  if(sourceBefore===null)sourceBefore=snapshot.record_page?.source_before||0;
  state.recoveryTask=null;state.snapshot=snapshot;const task=snapshot.task;const taskState=task.state||{};
  const title=task.ui?.title||task.objective;
  $('task-id').textContent='OIF';$('task-title').textContent=title.length>78?title.slice(0,78)+'…':title;$('primary-objective').textContent=task.objective;
  $('task-status').replaceWith(Object.assign(badge(task.status),{id:'task-status'}));renderTaskState(task);
  $('acceptance-list').replaceChildren(...(task.acceptance||[]).map(x=>node('li','',typeof x==='string'?x:pretty(x))));
  renderSourceHistory(task);renderInstruction(task);renderAttachment(task);
  const unresolved=taskState.open_outcomes||taskState.unfinished||taskState.pending||snapshot.readiness?.unresolved||task.final?.unresolved||[];
  const lastStateEvent=[...(snapshot.events||[])].reverse().find(event=>event.stage==='task'&&event.status===task.status);
  const reason=(['held','recovery_required','attention_required'].includes(task.status)?taskState.last_hold?.reason:null)||taskState.hold_reason||taskState.blocker||taskState.error||taskState.reason||taskState.wait_reason||lastStateEvent?.detail?.message||lastStateEvent?.detail?.reason;
  $('pending-work').className=reason?'pending':'';$('pending-work').textContent=reason?OIFPresentation.friendly(readableMessage(reason)):'';
  $('stop-task').hidden=['stopped','completed'].includes(task.status);$('stop-task').disabled=task.status==='stopping';
  $('resume-task').hidden=['running','stopping','completed'].includes(task.status);$('resume-task').textContent=tr(task.status==='created'?'開始':'再開');
  const issues=Array.isArray(unresolved)?unresolved:[unresolved];
  listObjects('open-details',issues,task.status==='completed'?tr('終了判断に記録された未完了事項はありません。最終結果と証拠の範囲をご確認ください。'):tr('主目的はまだ完了していません。工程の結果と再開条件を確認してください。'),(item,x)=>item.append(node('p','',pretty(x))));
  if(taskState.plan?.deliverables&&task.status!=='completed')$('open-details').append(detailButton('予定している成果物・条件',taskState.plan));
  if(reason)$('open-details').prepend(node('p','pending',OIFPresentation.friendly(readableMessage(reason))));
  if(taskState.unknown_effect)$('open-details').append(node('p','pending',tr('未確定の作用：')+pretty(taskState.unknown_effect)));
  const knowledge=snapshot.knowledge||{};const ideas=knowledge.ideas||[];const skills=knowledge.skills||[];const children=snapshot.children||[];
  $('idea-count').textContent=ideas.length;$('skill-count').textContent=skills.length;$('child-count').textContent=children.length;
  listObjects('ideas',ideas,tr('アイデアの導出結果は、記録が届くと表示されます。'),(item,x)=>{item.append(badge(x.disposition||x.status),node('p','',x.proposal||x.content||x.title||pretty(x)),node('small','',x.target==='workflow'?tr('対象：作業方針'):tr('対象：主目的の作業')),detailButton('採否と根拠',x));});
  listObjects('skills',skills,tr('使用・作成したSkillはまだありません。'),(item,x)=>{item.append(node('strong','',x.title||x.name||x.id),node('p','',x.summary||x.knowledge||x.content||''),node('small','',x.display_status?tr(x.display_status):statusName(x.status||x.disposition)),detailButton('出典・適用条件・使用結果',x));if(x.applies_when)item.append(node('p','',tr('使う場面：')+x.applies_when));for(const use of x.uses||[])if(use.assessment)item.append(node('p','',tr('使用後の評価：')+use.assessment.reason));});
  if(snapshot.record_page?.knowledge_more){for(const id of ['ideas','skills'])$(id).prepend(node('p','empty',conversationText('最近の記録を表示しています。過去の全記録は「根拠・出典・適用状態」から確認できます。','Showing recent records. Earlier records are available under Evidence and application.')));}
  listObjects('children',children,tr('子タスクはありません。'),(item,x)=>{item.append(badge(x.status),node('p','',x.objective),node('small','',x.actor||''),detailButton('担当と実行状態',x));const open=node('button','',tr('担当の全工程を開く'));open.onclick=()=>selectTask(x.id).catch(error=>note(error.message));item.append(open);});
  listObjects('operations',snapshot.operations||[],tr('まだ操作は実行されていません。'),(item,x)=>{const operation=x.operation||x;item.append(node('strong','',P?P.toolName(x.tool_name||operation.kind):(operation.kind||tr('操作'))),node('p','',plain(operation.purpose||tr('操作の記録'))),badge(x.status),detailButton('操作・引数・実結果',x));for(const artifact of x.result?.artifacts||[]){const link=artifactLink(task.id,artifact);if(link)item.append(link);}});
  if(snapshot.record_page?.operation_before||operationCursor){const pager=node('div','progress-pages');
    if(snapshot.record_page?.operation_before){const older=node('button','quiet',conversationText('以前の操作を読み込む','Load earlier operations'));older.onclick=()=>{recordPages.delete(state.taskId+'|operations');return loadRecordPage({operation_before:snapshot.record_page.operation_before,artifact_page:artifactPage});};pager.append(older);}
    if(operationCursor){const latest=node('button','quiet',conversationText('最新の操作','Latest operations'));latest.onclick=()=>{recordPages.delete(state.taskId+'|operations');return loadRecordPage({operation_before:0,artifact_page:artifactPage});};pager.append(latest);}$('operations').append(pager);}
  renderArtifactList(snapshot);
  $('evidence').replaceChildren(node('p','empty',tr('この作業に適用した方針と、実際の確認結果を保存しています。')),detailButton('状態と根拠の全記録',snapshot),detailButton('実行前提と未確認範囲',snapshot.readiness||{status:'not-observed'}));
  $('final-result').hidden=true;$('final-content').replaceChildren();
  const index=state.tasks.findIndex(t=>t.id===task.id);if(index>=0)state.tasks[index]={...state.tasks[index],status:task.status,version:task.version,source_version:task.source_version,ui:task.ui};renderTaskList();renderTaskPager();renderApprovals();
  for(const event of snapshot.events||[])if(!state.events.has(event.seq))state.events.set(event.seq,event);
  renderEvents(conversation);globalThis.OIFWorkspace?.renderComposer();return true;
}
async function refreshSnapshot(){
  if(state.refreshQueued){state.refreshAgain=true;return;}state.refreshQueued=true;
  try{do{state.refreshAgain=false;const taskId=state.taskId;if(!taskId)break;const generation=state.generation;const snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'?view=conversation&after='+latestSequence()+'&operation_before='+operationCursor+'&artifact_page='+artifactPage);if(state.taskId===taskId&&generation===state.generation){renderSnapshot(snapshot);if(snapshot.record_page?.event_more_after)state.refreshAgain=true;}}while(state.refreshAgain);}catch(error){note(error.message);}finally{state.refreshQueued=false;}
}
function connectStream(taskId,generation){
  const after=latestSequence();const stream=new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?view=conversation&after=${after}`);state.stream=stream;
  stream.addEventListener('open',()=>{if(generation===state.generation){streamMessage(state.snapshot?.task?.status==='completed'?'保存された全工程と結果を表示しています。':'実際の工程開始・結果を受信中。接続が切れても保存済みの続きから再表示します。');if(state.snapshot?.task)renderTaskState(state.snapshot.task);}});
  stream.addEventListener('stage',event=>{if(generation!==state.generation)return;try{const item=JSON.parse(event.data);appendEvent(item);refreshSnapshot();if(['policy_amendment','permission'].includes(item.stage))loadApprovals();}catch(error){note(tr('記録を表示できませんでした：')+error.message);}});
  stream.addEventListener('error',()=>{if(generation===state.generation){streamMessage('記録の接続が中断しています。実行状態は未確認です。同じタスクへの接続を確認しています。');if(state.snapshot?.task?.status==='running')$('task-state-banner').replaceChildren(node('strong','',tr('接続を確認中')),node('span','',tr('最後に確認した状態：実行中。現在の状態を再確認しています。')));recoverStream(taskId,generation);}});
}
async function recoverStream(taskId,generation){
  if(state.taskId!==taskId||generation!==state.generation||(state.reconnecting?.taskId===taskId&&state.reconnecting?.generation===generation))return;
  const attempt={taskId,generation};state.reconnecting=attempt;
  try{const response=await fetch('/',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error(tr('サービスへの接続を確認してください。'));const session=await api('/api/session');
    if(generation!==state.generation||state.taskId!==taskId)return;
    const changed=state.instanceId!==session.instance_id;state.csrf=session.csrf_token;state.instanceId=session.instance_id;
    if(changed){connectTaskList();await refreshTaskList();}
    if(generation!==state.generation||state.taskId!==taskId)return;
    let cursor=latestSequence(),snapshot;
    do{snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'?view=conversation&after='+cursor+'&operation_before='+operationCursor+'&artifact_page='+artifactPage);
      if(generation!==state.generation||state.taskId!==taskId)return;
      if(!renderSnapshot(snapshot)||snapshot.records_withheld)return;
      for(const event of snapshot.events||[])cursor=Math.max(cursor,event.seq);
    }while(snapshot.record_page?.event_more_after);
    if(!snapshot.records_withheld){state.stream?.close();connectStream(taskId,generation);note(changed?uiMessage('新しい実行プロセスへ接続し直しました。同じタスクの保存済み記録を表示しています。'):tr('このチャットの表示を更新しました。'));}
  }catch(error){if(generation===state.generation){note(uiMessage('通信が途切れています。表示中の記録は保持しています。サービス起動後に接続を確認できます。'),{key:'同じタスクへの接続を確認',run:()=>recoverStream(taskId,generation)});}}
  finally{if(state.reconnecting===attempt)state.reconnecting=false;}
}
async function selectTask(taskId){
  captureConversation();const savedView=state.conversationViews.get(taskId+'|'+state.progressFilter);
  state.stream?.close();state.stream=null;state.generation++;const generation=state.generation;state.taskId=taskId;state.snapshot=null;state.recoveryTask=null;state.events.clear();state.eventElements.clear();tablePage=0;turnPage=0;historyBefore=null;sourceBefore=null;operationCursor=0;artifactPage=0;listWindow=null;pinnedHistory.clear();$('task-details').open=false;
  $('welcome').hidden=true;$('task-view').hidden=true;$('loading').hidden=false;$('loading-message').textContent=tr('選択したタスクの記録を読み込んでいます。');history.replaceState(null,'','#task='+encodeURIComponent(taskId));
  try{const snapshot=await api('/api/tasks/'+encodeURIComponent(taskId)+'?view=conversation');if(generation!==state.generation)return;
    if(!renderSnapshot(snapshot)){const current=state.frontiers.get(taskId)?.snapshot;if(current&&current!==snapshot)renderSnapshot(current);}
    if(state.recoveryTask)return;
    renderEvents();$('loading').hidden=true;$('task-view').hidden=false;restoreConversation(savedView||{tail:true,open:new Map()});connectStream(taskId,generation);loadApprovals();
  }catch(error){if(generation===state.generation){$('loading-message').textContent=tr('このタスクの記録を読み込めませんでした。接続とタスク一覧を確認してください。');if(error.kind==='ConfigurationRequired'){const control=(await api('/api/recovery/tasks')).tasks.find(task=>task.id===taskId);if(control&&generation===state.generation)showRecoveryTask(control,error.message);}}throw error;}
}
function renderApprovals(){
  document.querySelectorAll('.approval-item').forEach(x=>x.remove());
  for(const approval of state.approvals.filter(a=>a.task_id===state.taskId)){
    if(approval.kind==='tool'&&globalThis.OIFWorkspace){globalThis.OIFWorkspace.renderApproval(approval);continue;}
    const open=()=>{state.approval=approval;$('approval-content').textContent=pretty(approval);$('approval-reason').value='';$('approval-error').textContent='';$('approval-dialog').showModal();};
    const item=node('div','approval-item');item.append(node('p','',plain(approval.reason||approval.title||tr('作業方針の条件を変更する提案があります。'))));
    const b=node('button','',tr('変更内容を確認'));b.onclick=open;item.append(b);$('open-details').prepend(item);
    const alert=node('button','approval-item quiet',tr('変更内容を確認'));alert.onclick=open;$('task-state-banner').append(alert);
  }
}
async function loadApprovals(){try{state.approvals=(await api('/api/approvals')).approvals;renderApprovals();}catch(error){note(error.message);}}
function renderSettingsStatus(){if(!state.settingsView)return;const settings=state.settingsView,form=$('settings-form'),unavailable=[];
  for(const name of ['model_api_key','web_api_key']){const status=settings.credential_status?.[name==='model_api_key'?'model':'web'];const unreadable=status==='unavailable';form.elements[name].placeholder=tr(unreadable?'保存キーを読み出せません。再入力してください':status==='available'?'保存済み。変更する場合だけ入力':'未設定');if(unreadable)unavailable.push(tr(name==='model_api_key'?'モデルAPI':'Web検索API'));}
  $('settings-error').textContent=state.settingsError||(settings.redaction_status==='historical_recovery_required'?settings.recovery_detail:unavailable.length?tr('savedKeysUnavailable',{names:unavailable.join(tr('・'))}):'');
}
async function showSettings(){const [settings,status]=await Promise.all([api('/api/settings'),api('/api/status')]);state.settingsView=settings;state.settingsError='';const form=$('settings-form');for(const name of ['base_url','model','review_model','api_mode','reasoning_effort','model_context_tokens','max_output_tokens','web_provider','web_base_url'])form.elements[name].value=settings[name]??'';for(const name of ['model_api_key','web_api_key'])form.elements[name].value='';$('environment-details').textContent=pretty(status);renderSettingsStatus();if(typeof loadProviderProfiles==='function')await loadProviderProfiles();if(!$('settings-dialog').open)$('settings-dialog').showModal();}
function renderTaskState(task){
  if(task.status==='completed')streamMessage('保存された全工程と結果を表示しています。');
  const states={running:['作業中','結果が届くたびに表示を更新します。','◌'],completed:['完了','成果物と最終結果を確認できます。','✓'],stopped:['停止済み','記録を保存しています。再開できます。','Ⅱ'],stopping:['停止しています','進行中の処理を止めて記録を保存します。','◌'],created:['開始待ち','依頼を受け付けました。','○']};
  const item=states[task.status]||['確認が必要','理由を確認し、必要な指示や設定を追加してください。','!'];
  const banner=$('task-state-banner');banner.className='task-state-banner '+task.status;
  banner.replaceChildren(node('span','state-icon',item[2]),node('strong','',tr(item[0])),node('span','',tr(item[1])));
}
$('progress-filter').onchange=()=>{captureConversation();state.progressFilter=$('progress-filter').value;renderEvents(state.conversationViews.get(conversationKey())||{tail:true,top:0});};
$('follow-latest').onclick=()=>{const group=currentGroups().at(-1);if(group)progressPages.delete(state.taskId+'|'+group.id+'|'+state.progressFilter);renderEvents({tail:true,top:0});};
$('chat-panel').addEventListener('scroll',()=>{const view=captureConversation();$('follow-latest').hidden=Boolean(view.tail);});
$('progress-filter').value=state.progressFilter;
$('task-details').addEventListener('toggle',()=>{if($('task-details').open)for(const section of $('task-details').querySelectorAll('.detail-body > details'))section.open=false;});
$('evidence-explain').onclick=()=>explainEvidence(state.evidenceEvent);
$('task-form').addEventListener('submit',submitTask);
$('instruction-form').addEventListener('submit',event=>globalThis.OIFWorkspace?.hasFiles(state.taskId)?globalThis.OIFWorkspace.submitMessage(event):submitInstruction(event));
$('instruction-text').addEventListener('input',()=>{if(!state.taskId)return;const draft=instructionDraft(state.taskId);draft.text=$('instruction-text').value;draft.message='';draft.isError=false;if(state.snapshot?.task?.id===state.taskId)renderInstruction(state.snapshot.task);});
$('attachment-form').addEventListener('submit',submitAttachment);
$('attachment-file').addEventListener('change',()=>{if(!state.taskId)return;const draft=attachmentDraft(state.taskId);draft.file=$('attachment-file').files[0]||null;draft.isError=Boolean(draft.file&&draft.file.size>state.attachmentMaxBytes);draft.message=draft.isError?uiMessage('fileTooLarge',{limit:state.attachmentMaxBytes/(1024*1024)}):'';if(state.snapshot?.task?.id===state.taskId)renderAttachment(state.snapshot.task);});
$('new-task').onclick=()=>{captureConversation();state.stream?.close();state.stream=null;state.generation++;state.taskId=null;state.snapshot=null;state.recoveryTask=null;state.events.clear();state.eventElements.clear();$('welcome').hidden=false;$('task-view').hidden=true;$('loading').hidden=true;history.replaceState(null,'',location.pathname);renderTaskList();$('objective').focus();};
$('reload-tasks').onclick=()=>loadTasks().catch(error=>note(error.message));
$('refresh-task').onclick=()=>recoverStream(state.taskId,state.generation);
for(const mode of ['chat','list'])$(mode+'-tab').onclick=()=>{captureConversation();for(const value of ['chat','list']){$(value+'-panel').hidden=mode!==value;$(value+'-tab').classList.toggle('active',mode===value);$(value+'-tab').setAttribute('aria-selected',String(mode===value));}$('progress-filter-label').hidden=mode==='list';$('conversation-controls').hidden=mode==='list';if(mode==='chat')restoreConversation(state.conversationViews.get(conversationKey())||{tail:true,top:0});};
for(const action of ['stop','resume'])$(action+'-task').onclick=async()=>{const taskId=state.taskId,generation=state.generation;const button=$(action+'-task');button.disabled=true;try{const result=await api(`/api/tasks/${encodeURIComponent(taskId)}/${action}`,{method:'POST',body:'{}'});if(result.records_withheld){if(state.taskId===taskId&&state.generation===generation)showRecoveryTask(result.task,result.detail,result);note(uiMessage('taskNotice',{task:taskId,detail:result.detail}));}else await refreshSnapshot();}catch(error){note(error.message);}finally{button.disabled=false;}};
$('export-task').onclick=()=>{if(!state.taskId)return;const link=node('a');link.href='/api/tasks/'+encodeURIComponent(state.taskId)+'/records?download=true';link.download='oif-record.json';link.click();};
globalThis.OIFRecords.bind();
$('settings-open').onclick=()=>showSettings().catch(error=>note(error.message));$('settings-close').onclick=()=>$('settings-dialog').close();$('evidence-close').onclick=()=>$('evidence-dialog').close();$('approval-close').onclick=()=>$('approval-dialog').close();
$('settings-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;const values={};for(const name of ['base_url','model','review_model','api_mode','web_provider'])values[name]=form.elements[name].value;for(const name of ['reasoning_effort','web_base_url'])values[name]=form.elements[name].value||null;for(const name of ['model_context_tokens','max_output_tokens'])values[name]=form.elements[name].value?Number(form.elements[name].value):null;for(const name of ['model_api_key','web_api_key'])if(form.elements[name].value)values[name]=form.elements[name].value;if(state.settingsView?.auth_method==='openrouter_oauth'&&values.base_url!=='https://openrouter.ai/api/v1')values.auth_method='api_key';const button=form.querySelector('button[type=submit]');button.disabled=true;try{const result=await api('/api/settings',{method:'POST',body:JSON.stringify(values)});for(const name of ['model_api_key','web_api_key'])form.elements[name].value='';$('settings-dialog').close();note(result.redaction_status==='historical_recovery_required'?uiMessage('taskNotice',{task:'OIF',detail:result.recovery_detail}):uiMessage('接続設定を保存しました。設定待ちのタスクは「再開」から続けられます。'));}catch(error){state.settingsError=error.message;$('settings-error').textContent=error.message;}finally{button.disabled=false;}});
$('approval-form').addEventListener('submit',async event=>{event.preventDefault();const approval=state.approval;if(!approval)return;try{const result=await api('/api/approvals/'+encodeURIComponent(approval.id),{method:'POST',body:JSON.stringify({decision:event.submitter.value,expected_hash:approval.proposal_hash,reason:$('approval-reason').value})});$('approval-dialog').close();if(result.records_withheld)note(result.detail);else{await loadApprovals();await refreshSnapshot();}}catch(error){$('approval-error').textContent=error.message;}});
window.addEventListener('beforeunload',()=>state.stream?.close());
window.addEventListener('hashchange',async()=>{const match=location.hash.match(/^#task=([a-f0-9]{32})$/);if(match&&match[1]!==state.taskId){try{await selectTask(match[1]);}catch(error){note(error.message);}}});
async function boot(){try{const session=await api('/api/session');state.csrf=session.csrf_token;state.instanceId=session.instance_id;if(Number.isSafeInteger(session.attachment_max_bytes)&&session.attachment_max_bytes>0)state.attachmentMaxBytes=session.attachment_max_bytes;await loadTasks();connectTaskList();state.connectionKey='ローカル接続中';$('connection').textContent=tr(state.connectionKey);$('connection').classList.add('online');const match=location.hash.match(/^#task=(.+)$/);if(match)await selectTask(decodeURIComponent(match[1]));const pending=pendingSubmissions().at(-1);if(pending){try{await recoverCreation(pending,{open:!match});}catch(error){note(error.status===404?uiMessage('creationNotFound'):uiMessage('creationUnknown'));}}}catch(error){state.connectionKey='接続を確認してください';$('connection').textContent=tr(state.connectionKey);$('connection').classList.add('error');note(error.message);}}
function renderPreferences(){
  if(!UI)return;UI.apply();$('language-select').value=UI.locale;
  const themeLabel=tr(UI.theme==='dark'?'ライトに切り替え':'ダークに切り替え');$('theme-toggle').textContent=themeLabel;$('theme-toggle').setAttribute('aria-label',themeLabel);$('theme-toggle').setAttribute('aria-pressed',String(UI.theme==='light'));
  $('connection').textContent=tr(state.connectionKey);$('stream-state').textContent=messageText(state.streamMessage);$('evidence-title').textContent=tr(state.evidenceTitle);
  if(state.snapshot&&state.snapshot.task.id===state.taskId)renderSnapshot(state.snapshot);
  else if(state.recoveryTask&&state.recoveryTask.task.id===state.taskId)showRecoveryTask(state.recoveryTask.task,state.recoveryTask.detail,state.recoveryTask.receipt);
  else if(!$('loading').hidden)$('loading-message').textContent=tr('選択したタスクの記録を読み込んでいます。');
  renderTaskList();renderEvents();renderNotice();renderSettingsStatus();
  globalThis.OIFWorkspace?.renderComposer();
}
if(UI){$('language-select').addEventListener('change',event=>UI.setLocale(event.target.value));$('theme-toggle').addEventListener('click',()=>UI.setTheme(UI.theme==='dark'?'light':'dark'));UI.subscribe(renderPreferences);renderPreferences();}
boot();
