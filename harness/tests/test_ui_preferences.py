"""Actual served JavaScript/ASGI consumer fixtures; not live-browser rendering.

Source semantic reconciliation is exercised by root's actual Engine tests.
Here the controller fixture preserves task/source inputs while real HTTP and
JavaScript handlers exercise presentation, asynchronous identity and receipts.
"""
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_source_input_ui import ADDED, FixtureSettings, harness, server


class MarkedElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') or any(k.startswith('data-i18n') for k in attrs):
            self.items.append({'tag': tag, 'attributes': attrs})


DOM_RUNNER = r'''
const fs=require('fs'),vm=require('vm');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const storage={...(input.storage||{})};
const session={...(input.session||{})};
function runtime(){
 const elements=new Map(),marked=[];
 class Element{
  constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;this.listeners={};this.elements={};this.classList={add(){},toggle(){}};}
  append(...x){this.children.push(...x);}prepend(...x){this.children.unshift(...x);}replaceChildren(...x){this.children=[...x];this.textContent='';}
  replaceWith(x){if(this.id){x.id=this.id;elements.set(this.id,x);}}addEventListener(name,cb){this.listeners[name]=cb;}
  setAttribute(k,v){this.attributes[k]=String(v);}getAttribute(k){return this.attributes[k]??null;}
  close(){this.open=false;}showModal(){this.open=true;}focus(){}remove(){}getBoundingClientRect(){return this.rect||{top:0,bottom:0};}
  reset(){if(this.id==='task-form')document.getElementById('objective').value='';}
 }
 for(const item of input.marked){const e=new Element(item.tag);e.attributes=item.attributes;e.id=item.attributes.id;e.hidden='hidden' in item.attributes;if(e.id)elements.set(e.id,e);marked.push(e);}
 const document={documentElement:new Element('html'),getElementById(id){if(!elements.has(id)){const e=new Element('div');e.id=id;elements.set(id,e);}return elements.get(id);},createElement:t=>new Element(t),createDocumentFragment:()=>new Element('fragment'),createTextNode:t=>Object.assign(new Element('text'),{textContent:t}),querySelectorAll(selector){if(selector.startsWith('[')){const key=selector.slice(1,-1);return marked.filter(e=>key in e.attributes);}return [];}};
 for(const name of ['base_url','model','review_model','api_mode','reasoning_effort','model_context_tokens','max_output_tokens','web_provider','web_base_url','model_api_key','web_api_key'])document.getElementById('settings-form').elements[name]=new Element('input');
 const calls=[],pending=[],deferredGets=[],windowEvents={},streams=[];let current=input.initial,onPost,getDeferred=false;const posted=new Promise(resolve=>onPost=resolve);
 class FileReader{readAsDataURL(file){this.result='data:application/octet-stream;base64,'+(input.base64||'eA==');queueMicrotask(()=>this.onload());}}
 const context={document,console,Map,Set,URL,Blob,FileReader,TextEncoder,AbortController,TextDecoderStream,crypto:{subtle:require('crypto').webcrypto.subtle,randomUUID:()=>input.createReceipt?.submission_id||require('crypto').randomUUID()},navigator:{languages:input.languages||['ja-JP']},sessionStorage:{getItem:k=>session[k]??null,setItem(k,v){if(input.sessionDenied)throw new Error('Denied');session[k]=v;}},localStorage:{getItem(k){if(input.storageDenied)throw new Error('Denied');return storage[k]??null;},setItem(k,v){if(input.storageDenied)throw new Error('Denied');storage[k]=v;}},window:{addEventListener(name,fn){windowEvents[name]=fn;}},history:{replaceState(){}},location:{hash:'',pathname:'/'},EventSource:class{constructor(path){this.path=path;this.closed=false;streams.push(this);}addEventListener(){}close(){this.closed=true;}},
  fetch:async(path,options={})=>{const pathname=path.split('?')[0];calls.push({path,options});if(options.method==='POST'){const response=new Promise((resolve,reject)=>pending.push(value=>value?.transportError?reject(new Error('synthetic response loss')):resolve({ok:true,status:200,json:async()=>value})));onPost();return response;}if(input.deferSnapshot&&!getDeferred&&pathname==='/api/tasks/'+input.initial.task.id){getDeferred=true;return new Promise(resolve=>deferredGets.push(value=>resolve({ok:true,status:200,json:async()=>value})));}if(path.startsWith('/api/submissions/')&&input.lookupMissing)return {ok:false,status:404,json:async()=>({detail:'No saved receipt'})};const value=path==='/api/session'?{csrf_token:'fixture-csrf',instance_id:'fixture-instance'}:path==='/api/approvals'?{approvals:[]}:path==='/api/tasks'?{tasks:[(input.created||current).task]}:path.startsWith('/api/submissions/')?input.createReceipt:input.created&&pathname.endsWith(input.created.task.id)?input.created:input.other&&pathname.endsWith(input.other.task.id)?input.other:current;return {ok:true,status:200,json:async()=>value};}};
 context.windowEvents=windowEvents;vm.createContext(context);vm.runInContext(input.i18n,context);vm.runInContext(input.presentation,context);vm.runInContext(input.conversation,context);vm.runInContext(input.records,context);
 const app=input.app.replace(/\nboot\(\);\s*$/,'\n');if(app===input.app)throw new Error('Explicit boot boundary missing');
 vm.runInContext(app+'\nglobalThis.hooks={state,renderSnapshot,renderEvents,renderApprovals,submitInstruction,submitAttachment,selectTask,instructionDraft,attachmentDraft,showDetails,renderSettingsStatus,refreshSnapshot,appendEvent,recoverCreation,pendingSubmissions,boot};',context);
 const h=context.hooks;
 function select(snapshot){current=snapshot;h.state.taskId=snapshot.task.id;h.state.tasks=[snapshot.task,...(input.other?[input.other.task]:[])];h.state.csrf='fixture-csrf';h.renderSnapshot(snapshot);}
 function type(text){document.getElementById('instruction-text').value=text;document.getElementById('instruction-text').listeners.input();}
 function preferences(locale='en',theme='light'){document.getElementById('language-select').listeners.change({target:{value:locale}});if(context.OIFI18n.theme!==theme)document.getElementById('theme-toggle').listeners.click();}
 function descendants(e){return [e,...e.children.filter(x=>x instanceof Element).flatMap(descendants)];}
 return {context,h,document,calls,pending,deferredGets,posted,select,type,preferences,descendants,marked,streams};
}
(async()=>{
 const r=runtime(),{h,document}=r;
 if(input.mode==='creation-loss'||input.mode==='creation-storage'){
  h.state.csrf='fixture-csrf';document.getElementById('objective').value=input.startPrompt;
  const send=document.getElementById('task-form').listeners.submit({preventDefault(){}});
  if(input.mode==='creation-storage'){await send;process.stdout.write(JSON.stringify({calls:r.calls,session,draft:document.getElementById('objective').value,notice:document.getElementById('notice').textContent}));return;}
  await r.posted;r.pending.shift()({transportError:true});await send;
  const retained={session:JSON.parse(JSON.stringify(session)),draft:document.getElementById('objective').value};
  const recovered=runtime();recovered.h.state.csrf='fixture-csrf';
  if(input.recovery==='retry'){
   recovered.document.getElementById('objective').value=input.startPrompt;
   const retry=recovered.document.getElementById('task-form').listeners.submit({preventDefault(){}});
   await recovered.posted;recovered.pending.shift()(input.createReceipt);await retry;
  }else await recovered.h.boot();
  process.stdout.write(JSON.stringify({calls:r.calls,recoveryCalls:recovered.calls,retained,session,taskId:recovered.h.state.taskId,newDraft:recovered.document.getElementById('objective').value,notice:recovered.document.getElementById('notice').textContent}));return;
 }
 const startup={locale:r.context.OIFI18n.locale,theme:r.context.OIFI18n.theme};
 r.select(input.initial);
 if(input.mode==='presentation'){h.state.events=new Map(input.initial.events.map(e=>[e.seq,e]));h.renderEvents();h.state.approvals=input.approvals||[];h.renderApprovals();}
 const exact={objective:input.initial.task.objective,source:input.initial.task.source_history[0].text,draft:input.text};
 r.type(input.text);h.attachmentDraft(input.initial.task.id).file={name:'retained source.txt',size:3};
 document.getElementById('objective').value='  new-task draft  ';
 document.getElementById('approval-reason').value='  exact decision draft  ';
 document.getElementById('settings-form').elements.model.value='edited-unsaved-model';
 if(input.settings){h.state.settingsView=input.settings;h.renderSettingsStatus();}
 let first,second,afterDelayed,scrollObservation;
 if(input.mode==='reconnect'||input.mode==='reconnect-switch'){
  h.state.instanceId='old-instance';
  const reconnect=document.getElementById('refresh-task').onclick();
  if(input.mode==='reconnect-switch'){
   for(let i=0;i<10&&!r.deferredGets.length;i++)await new Promise(resolve=>setImmediate(resolve));
   if(r.deferredGets.length!==1)throw new Error('Expected a held old-chat GET');
   await h.selectTask(input.other.task.id);r.type('other chat retained draft');
   await document.getElementById('refresh-task').onclick();
   r.deferredGets.shift()(input.initial);
  }
  await reconnect;
  if(h.state.reconnecting)throw new Error('Reconnect lock leaked');
 }else if(input.mode==='create'){
  document.getElementById('objective').value=input.startPrompt;
  first=document.getElementById('task-form').listeners.submit({preventDefault(){}});
  r.preferences();
  await r.posted;
  if(r.pending.length!==1)throw new Error('Prompt-only start must send exactly one request');
  r.pending.shift()(input.createReceipt);await first;
 }else if(input.mode==='order'||input.mode==='withheld-order'){
  const earlier=h.refreshSnapshot();
  if(r.deferredGets.length!==1)throw new Error('Earlier actual GET was not held');
  first=input.action==='attachment'?h.submitAttachment({preventDefault(){}}):h.submitInstruction({preventDefault(){}});
  r.type(input.nextText);await Promise.resolve();await Promise.resolve();
  if(r.pending.length!==1)throw new Error('Expected one source POST');r.pending.shift()(input.accepted);await first;
  r.deferredGets.shift()(input.initial);await earlier;r.preferences();
  afterDelayed={sourceHash:h.state.snapshot?.task.source_hash||null,sourceVersion:h.state.snapshot?.task.source_version||null,taskVersion:h.state.snapshot?.task.version||null,primary:document.getElementById('primary-objective').textContent,withheld:Boolean(h.state.recoveryTask)};
  if(input.mode==='order'){second=h.submitInstruction({preventDefault(){}});if(r.pending.length!==1)throw new Error('Next instruction was not admitted');r.pending.shift()(input.acceptedSecond);await second;}
 }else if(input.mode==='navigation'){
  r.context.location.hash='#task='+input.other.task.id;await r.context.windowEvents.hashchange();
  r.context.location.hash='#main-content';await r.context.windowEvents.hashchange();
 }else if(input.mode==='scroll'){
  const panel=document.getElementById('chat-panel');panel.hidden=false;panel.clientHeight=100;
  Object.defineProperty(panel,'scrollHeight',{get:()=>400+h.state.events.size*50});
  panel.scrollTop=panel.scrollHeight-panel.clientHeight;
  const next=Math.max(0,...h.state.events.keys())+1;
  h.appendEvent({seq:next,task_id:h.state.taskId,stage:'review',status:'succeeded',detail:{summary:'new tail'},created_at:'2026-09-15T02:00:00Z'});
  const followed=panel.scrollTop===panel.scrollHeight;
  panel.scrollTop=40;h.state.eventElements.get(next).open=true;
  h.appendEvent({seq:next+1,task_id:h.state.taskId,stage:'review',status:'failed',detail:{first_fault:'full original fault'},created_at:'2026-09-15T02:00:01Z'});
  const olderTop=panel.scrollTop;r.preferences();
  scrollObservation={followed,olderTop,afterPreferencesTop:panel.scrollTop,keptOpen:h.state.eventElements.get(next).open,failedOpen:h.state.eventElements.get(next+1).open,count:h.state.events.size};
 }else if(input.mode==='rapid'||input.mode==='switch'||input.mode==='withheld'||input.mode==='mismatch'){
  if(input.action==='attachment')first=h.submitAttachment({preventDefault(){}});else first=h.submitInstruction({preventDefault(){}});
  r.type(input.nextText);await h.submitInstruction({preventDefault(){}});
  r.preferences();
  if(input.mode==='switch'){await h.selectTask(input.other.task.id);r.type('  other task draft  ');}
  // Let the FileReader's single completion reach the transport boundary.
  await Promise.resolve();await Promise.resolve();
  if(r.pending.length!==1)throw new Error('Expected exactly one pending POST');r.pending.shift()(input.accepted);await first;
  if(input.mode==='rapid'){second=h.submitInstruction({preventDefault(){}});if(r.pending.length!==1)throw new Error('Next distinct instruction was not sent');r.pending.shift()(input.acceptedSecond);await second;}
 }else r.preferences();
 const preferencePosts=r.calls.filter(x=>x.options.method==='POST').length;
 const before={taskId:h.state.taskId,generation:h.state.generation,snapshot:JSON.stringify(h.state.snapshot),draft:h.instructionDraft(h.state.taskId).text};
 r.preferences('ja','dark');r.preferences('en','light');
 const unchanged=before.taskId===h.state.taskId&&before.generation===h.state.generation&&before.snapshot===JSON.stringify(h.state.snapshot)&&before.draft===h.instructionDraft(h.state.taskId).text;
 const untranslated=r.marked.filter(e=>e.attributes['data-i18n']).filter(e=>/[\u3040-\u30ff\u3400-\u9fff]/.test(e.textContent)).map(e=>e.attributes['data-i18n']);
 const sources=r.descendants(document.getElementById('source-history')).filter(e=>e.className==='source-text').map(e=>e.textContent);
 const reload=runtime();
 const out={startup,storage,restored:{theme:reload.context.OIFI18n.theme,locale:reload.context.OIFI18n.locale},calls:r.calls,unchanged,preferencePosts,postCount:r.calls.filter(x=>x.options.method==='POST').length,taskId:h.state.taskId,sourceHash:h.state.snapshot?.task?.source_hash||null,primary:document.getElementById('primary-objective').textContent,sources,status:document.getElementById('task-status').textContent,taskHidden:document.getElementById('task-view').hidden,recovery:h.state.recoveryTask?.receipt||null,drafts:[...h.state.instructionDrafts],attachmentDrafts:[...h.state.attachmentDrafts],message:document.getElementById('instruction-message').textContent,notice:document.getElementById('notice').textContent,loadingText:r.descendants(document.getElementById('loading-message')).map(e=>e.textContent).join('\n'),newDraft:document.getElementById('objective').value,approvalDraft:document.getElementById('approval-reason').value,settingsDraft:document.getElementById('settings-form').elements.model.value,untranslated,theme:document.documentElement.getAttribute('data-theme'),locale:document.documentElement.lang};
 out.settingsPlaceholders=Object.fromEntries(['model_api_key','web_api_key'].map(name=>[name,document.getElementById('settings-form').elements[name].placeholder]));
 out.settingsError=document.getElementById('settings-error').textContent;
 out.afterDelayed=afterDelayed;out.scroll=scrollObservation;out.streams=r.streams;out.instanceId=h.state.instanceId;
 if(input.mode==='presentation'){
  const cards=r.descendants(document.getElementById('chat-events')).filter(e=>e.tag==='details'&&String(e.className).startsWith('event-card'));
  const describe=e=>({open:e.open,full:r.descendants(e).filter(c=>c.className==='event-summary').map(c=>c.textContent),stage:r.descendants(e).find(c=>c.className==='event-stage').textContent,overview:r.descendants(e).find(c=>c.className==='event-overview')?.textContent||'',status:r.descendants(e).find(c=>String(c.className).startsWith('badge ')).textContent});
  out.records=cards.map(describe);
  out.rows=r.descendants(document.getElementById('event-rows')).filter(e=>e.tag==='tr').length;
  out.turnCount=r.descendants(document.getElementById('chat-events')).filter(e=>e.className==='conversation-turn').length;
  out.recordDetails=cards.map(e=>{r.descendants(e).find(c=>c.className==='event-details').listeners.click();return document.getElementById('evidence-content').textContent;});
  out.artifacts=r.descendants(document.getElementById('task-artifacts')).filter(e=>e.tag==='a').map(e=>({text:e.textContent,href:e.href}));
  out.artifactsHidden=document.getElementById('artifact-result').hidden;
  out.detailsOpen=Boolean(document.getElementById('task-details').open);
  r.preferences('ja','dark');out.recordsJa=[...h.state.eventElements.values()].map(describe);
 }
 process.stdout.write(JSON.stringify(out));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def run_dom(client, initial, **options):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node DOM fixture is unavailable; live rendering remains unobserved')
    payload = {
        'marked': MarkedElements(client.get('/').text).items,
        'app': client.get('/static/app.js').text,
        'i18n': client.get('/static/i18n.js').text,
        'presentation': client.get('/static/presentation.js').text,
        'conversation': client.get('/static/conversation.js').text,
        'records': client.get('/static/record-view.js').text,
        'initial': initial, 'text': '  exact source <b>停止</b>\n  ',
        'nextText': '  second instruction while the first is pending\n',
        **options,
    }
    run = subprocess.run([node, '-e', DOM_RUNNER], input=json.dumps(payload, ensure_ascii=False),
                         capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout), payload


def test_served_oif_identity_and_exact_logo(harness):
    client, _, _, _, app = harness
    html = client.get('/').text
    assert app.title == 'OIF' and '<title>OIF</title>' in html
    assert 'Hermes' not in html and '<small>' not in html
    assert html.count('/static/oif-outline.png') == 1
    assert html.count('/static/oif-desktop.png') == 1
    assert client.get('/static/oif-desktop.png').status_code == 200
    logo = client.get('/static/oif-outline.png')
    assert logo.status_code == 200
    assert hashlib.sha256(logo.content).hexdigest().upper() == '20D25566050BFAD17D0C22B77FF59B1C04BA8BE8CECE0A9377058D2A2902F573'
    assert 'data-theme="dark"' in html and '/static/i18n.js' in html
    assert html.index('id="chat-events"') < html.index('id="instruction-form"')
    assert 'unsafe-inline' not in client.get('/').headers.get('content-security-policy', '')


@pytest.mark.parametrize('switch', [False, True])
def test_chat_refresh_reconnects_without_posts_or_lost_drafts(harness, switch):
    client, store, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    other_id = client.post('/api/tasks', json={'objective':'Another independent chat'}).json()['task']['id']
    other = client.get('/api/tasks/'+other_id).json()
    before = {task['id']: task['source_hash'] for task in store.list_tasks()}
    actual, payload = run_dom(client, initial, mode='reconnect-switch' if switch else 'reconnect',
                              other=other, deferSnapshot=switch)
    assert actual['postCount'] == 0
    assert actual['taskId'] == (other_id if switch else identity)
    assert dict(actual['drafts'])[identity]['text'] == payload['text']
    assert dict(actual['attachmentDrafts'])[identity]['file']['name'] == 'retained source.txt'
    assert actual['settingsDraft'] == 'edited-unsaved-model'
    assert actual['newDraft'] == '  new-task draft  '
    assert actual['instanceId'] == 'fixture-instance'
    streams = [s['path'] for s in actual['streams'] if not s['closed']]
    assert '/api/task-list/events' in streams
    task_streams = [s for s in streams if s.startswith('/api/tasks/')]
    assert len(task_streams) == 1 and '/'+actual['taskId']+'/events?' in task_streams[0]
    if switch:
        assert dict(actual['drafts'])[other_id]['text'] == 'other chat retained draft'
    assert before == {task['id']: task['source_hash'] for task in store.list_tasks()}


def test_prompt_only_start_keeps_conditions_in_original_source(harness):
    client, store, _, identity, _ = harness
    prompt = '  集計ツールを作ってください。\n条件：元のCSVを変更しない。\n成果物と確認結果を日本語で説明する。\n'
    html = client.get('/').text
    assert 'id="acceptance"' not in html
    assert '完成の条件を追加する' not in html
    # The actual server accepts the ordinary one-field request and retains its
    # full source. The served JS must issue that same request exactly once.
    receipt = client.post('/api/tasks', json={'objective': prompt})
    assert receipt.status_code == 201
    created_id = receipt.json()['task']['id']
    created = client.get('/api/tasks/'+created_id).json()
    actual, _ = run_dom(client, client.get('/api/tasks/'+identity).json(),
                        mode='create', startPrompt=prompt,
                        created=created, createReceipt=receipt.json())
    posts = [call for call in actual['calls'] if call['options'].get('method') == 'POST']
    assert len(posts) == 1 and posts[0]['path'] == '/api/tasks'
    assert json.loads(posts[0]['options']['body']) == {'objective': prompt, 'submission_id': receipt.json()['submission_id']}
    assert store.original_inputs[created_id] == {'objective': prompt, 'acceptance': []}
    assert created['task']['source_history'][0]['text'] == prompt
    assert created['task']['source_history'][0]['acceptance'] == []
    assert actual['taskId'] == created_id and not actual['taskHidden']
    assert actual['sources'] == [prompt]
    assert actual['newDraft'] == '' and actual['notice'] == ''


def test_compact_chat_preserves_every_stage_and_visible_failure_without_forced_expansion(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    initial['events'] = [
        {'seq': 11, 'stage': 'pre_review', 'status': 'started', 'created_at': '2026-09-15T01:00:00Z', 'detail': {'summary': '条件を照合しています。'}},
        {'seq': 12, 'stage': 'operation', 'status': 'succeeded', 'created_at': '2026-09-15T01:00:01Z', 'detail': {'summary': '実際の成果物を保存しました。', 'original_marker': 'unchanged <b>source</b>'}},
        {'seq': 13, 'stage': 'post_review', 'status': 'failed', 'created_at': '2026-09-15T01:00:02Z', 'detail': {'summary': '確認結果が一致しません。', 'first_fault': 'original-first-fault'}},
    ]
    actual, _ = run_dom(client, initial, mode='presentation')
    assert len(actual['records']) == actual['rows'] == 3
    assert [row['open'] for row in actual['records']] == [False, False, False]
    assert [json.loads(raw) for raw in actual['recordDetails']] == initial['events']
    assert actual['records'][2]['full'] == ['確認結果が一致しません。\noriginal-first-fault']
    assert actual['records'][2]['status'] == 'Failed'
    assert actual['records'][2]['overview'] == '確認結果が一致しません。\noriginal-first-fault'
    assert actual['calls'] == [] and actual['unchanged']


def test_actual_producer_shapes_keep_adverse_meaning_and_full_original_records(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    shapes = [
        ('pre_review:context:0:page:0', 'started', {'actor': 'reviewer', 'target': 'op-private', 'policy_hash': 'a'*64}),
        ('pre_review:context:0:page:1', 'succeeded', {'actor': 'reviewer', 'result': {'summary': 'Keep the existing files.', 'opinions': []}}),
        ('web_acquisition_disposition_before:page:0', 'succeeded', {'actor': 'worker', 'result': {'verdict': 'revise', 'rationale': 'The evidence is incomplete.'}}),
        ('web_learning_disposition', 'succeeded', {'actor': 'worker', 'result': {'verdict': 'hold', 'rationale': 'Wait for the pending effect.'}}),
        ('web-learning-outcome_choices_after:context:0:page:0', 'succeeded', {'actor': 'worker', 'result': {'assessments': [
            {'target_id': 'hidden-target-1', 'assessment': {'success': 'failed', 'rationale': 'Readback differs.', 'mistakes': ['Do not call this complete.']}},
            {'target_id': 'hidden-target-2', 'assessment': {'success': 'unknown', 'rationale': 'The write effect is unknown.'}},
        ]}}),
        ('execution', 'unknown', {'operation_id': 'hidden-operation', 'status': 'unknown', 'effect': 'unknown', 'stdout': 'actual output', 'stderr': 'transport interrupted'}),
        ('source', 'received', {'source': {'kind': 'instruction', 'text': '  原文 <b>追加指示</b>\n'}, 'source_hash': 'b'*64}),
        ('source', 'received', {'source': {'kind': 'attachment', 'filename': 'source.txt'}, 'source_hash': 'c'*64}),
        ('completion_proposal', 'succeeded', {'actor': 'worker', 'result': {'achieved': False, 'summary': 'No artifact yet.'}}),
        ('task', 'attention_required', {'error': 'ProviderError HTTP 500', 'request_id': 'hidden-request'}),
    ]
    initial['events'] = [dict(seq=i+1, task_id=identity, stage=stage, status=status,
                              detail=detail, created_at='2026-09-15T01:00:00Z')
                         for i, (stage, status, detail) in enumerate(shapes)]
    actual, _ = run_dom(client, initial, mode='presentation')
    assert len(actual['records']) == actual['rows'] == len(shapes)
    assert [json.loads(raw) for raw in actual['recordDetails']] == initial['events']
    en, ja = actual['records'], actual['recordsJa']
    assert all(not row['open'] for row in en + ja)
    assert en[0]['overview'] and ja[0]['stage'] == '実行前レビュー'
    assert en[1]['overview'] == 'Keep the existing files.' and en[1]['status'] == 'Response received'
    assert en[2]['overview'].startswith('Revision required\n') and ja[2]['overview'].startswith('修正が必要\n')
    assert en[3]['overview'].startswith('Decision: hold\n')
    assert 'Assessment: failed' in en[4]['overview'] and 'Assessment: unconfirmed' in en[4]['overview']
    assert 'Readback differs.' in en[4]['full'][0] and 'The write effect is unknown.' in en[4]['full'][0]
    assert en[5]['overview'].startswith('Effect unknown\n') and 'transport interrupted' in en[5]['overview']
    assert en[6]['full'] == ['  原文 <b>追加指示</b>\n'] and ja[6]['full'] == en[6]['full']
    assert en[6]['status'] == 'Received' and ja[6]['status'] == '受領済み'
    assert en[7]['overview'] == 'Attachment: source.txt'
    assert en[8]['overview'].startswith('The primary objective is incomplete.')
    assert en[9]['status'] == 'Attention required' and en[9]['overview'] == 'ProviderError HTTP 500'
    for row in en + ja:
        assert 'hidden-' not in row['overview'] and 'context:' not in row['stage'] and 'page:' not in row['stage']
        assert '{' not in row['overview']
    assert '学習結果' in ja[4]['stage'] and '改善案の検討' in ja[4]['stage']
    assert actual['calls'] == [] and actual['unchanged']


def test_completion_recovery_and_literal_sources_keep_contrary_evidence_visible(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    collision = 'Explicit user request. Preserve in-flight effects and child states.'
    shapes = [
        ('completion_proposal', 'succeeded', {'actor': 'worker', 'result': {
            'achieved': True, 'acceptance': [{'criterion': 'Exact readback is missing.', 'achieved': False, 'evidence_refs': [], 'independent_refs': []}],
            'unresolved': [], 'summary': 'The file is ready.'}}),
        ('completion_proposal', 'succeeded', {'actor': 'worker', 'result': {
            'achieved': True, 'acceptance': [], 'unresolved': ['The output is unverified.'], 'summary': 'Finished.'}}),
        ('cleanup', 'succeeded', {'retained': ['committed knowledge'], 'commit_observation': {
            'status': 'succeeded_with_commit_readback', 'effect': 'unknown',
            'first_fault': {'type': 'OSError', 'message': 'Projection readback failed.'},
            'commit_readback': {'confirmed': True}, 'projection_recovery': {'effect': 'unknown'}}}),
        ('knowledge_application', 'failed', {'failure_ref': 'hidden-failure', 'fault': {'type': 'ValueError', 'message': 'Knowledge input is invalid.'}}),
        ('learning_contract', 'revision_required', {'operation_id': 'hidden-operation', 'fault': {'type': 'ValueError', 'message': 'Learning omitted the retained fault.'}, 'original_result_preserved': True, 'action_replayed': False}),
        ('cycle', 'completed', {'operation_id': 'hidden-operation', 'result_status': 'failed', 'next': 'next-opaque-state'}),
        ('cycle', 'completed', {'operation_id': 'hidden-operation', 'result_status': 'unknown', 'next': 'next-opaque-state'}),
        ('knowledge_application', 'partial', {'projection_status': {'state': 'pending', 'effect': 'unknown', 'first_fault': {'type': 'OSError', 'message': 'Knowledge projection pending.'}, 'recovery': {}}}),
        ('web_selection', 'reprepare', {'reason': 'Selection changed.', 'network_dispatched': False}),
        ('web_selection', 'refused_before_dispatch', {'reason': 'The source is unavailable.', 'network_dispatched': False}),
        ('web_learning_research_query', 'rejected_format', {'reason': 'Expected a source URL.', 'invalid_proposal': {'untrusted': 'hidden-proposal'}}),
        ('policy_admission_reprepare', 'held', {'reason': 'Execution conditions changed.'}),
        ('source', 'received', {'source': {'kind': 'instruction', 'text': collision}}),
        ('pre_review', 'succeeded', {'actor': 'reviewer', 'result': {'summary': collision, 'opinions': []}}),
    ]
    initial['events'] = [dict(seq=i+1, task_id=identity, stage=stage, status=status,
                              detail=detail, created_at='2026-09-15T01:00:00Z')
                         for i, (stage, status, detail) in enumerate(shapes)]
    actual, _ = run_dom(client, initial, mode='presentation')
    assert [json.loads(raw) for raw in actual['recordDetails']] == initial['events']
    assert len(actual['records']) == actual['rows'] == len(shapes)
    en, ja = actual['records'], actual['recordsJa']
    for records, incomplete, unknown, failed in [(en, 'The primary objective is incomplete.', 'Effect unknown', 'Failed'), (ja, '主目的は未完了です。', '作用不明', '失敗')]:
        assert records[0]['overview'].startswith(incomplete+'\nExact readback is missing.')
        assert records[1]['overview'].startswith(incomplete+'\nThe output is unverified.')
        assert unknown in records[2]['overview'] and 'Projection readback failed.' in records[2]['overview']
        assert records[3]['overview'] == 'Knowledge input is invalid.'
        assert records[4]['overview'] == 'Learning omitted the retained fault.'
        assert failed in records[5]['overview'] and unknown in records[6]['overview']
        assert unknown in records[7]['overview'] and 'Knowledge projection pending.' in records[7]['overview']
        assert records[12]['full'] == [collision] and records[13]['full'] == [collision]
        assert all(not row['open'] and 'hidden-' not in row['overview'] for row in records)
    assert en[0]['status'] == 'Response received' and ja[0]['status'] == '応答を受信'
    assert en[2]['status'] == 'Succeeded' and 'Commit readback confirmed' in en[2]['overview']
    assert ja[2]['status'] == '成功' and '保存を再確認済み' in ja[2]['overview']
    assert [en[i]['status'] for i in [4, 7, 8, 9, 10]] == ['Revision required', 'Partially complete', 'Revising preparation', 'Held before execution', 'Response format needs correction']
    assert [ja[i]['status'] for i in [4, 7, 8, 9, 10]] == ['修正が必要', '一部未完了', '準備を見直し中', '実行前に保留', '応答形式の修正が必要']
    assert [ja[i]['stage'] for i in [2, 4, 8, 11]] == ['作業後の整理', '学習結果の確認', '取得する情報の選択', '実行準備の見直し']
    assert en[5]['status'] == 'Completed' and ja[5]['status'] == '完了'
    assert actual['calls'] == [] and actual['unchanged']


def test_existing_artifacts_are_accessible_and_approval_stays_visible(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    digest = hashlib.sha256(b'hello').hexdigest()
    url = f'/api/tasks/{identity}/artifacts/answer.txt?operation_id=op-1&sha256={digest}'
    good = {'relative_path': 'answer.txt', 'download_operation_id': 'op-1', 'sha256': digest, 'download_url': url}
    unsafe = {**good, 'download_url': 'https://unexpected.example/answer.txt'}
    initial['operations'] = [{'id': 'op-1', 'status': 'succeeded', 'operation': {'kind': 'file_write', 'purpose': '成果物の保存'}, 'result': {'artifacts': [good, good, unsafe]}}]
    actual, _ = run_dom(client, initial, mode='presentation', approvals=[{'id': 'approval-1', 'task_id': identity, 'reason': '作業方針の条件変更の提案'}])
    assert actual['artifacts'] == [{'text': 'answer.txt', 'href': url}]
    assert not actual['artifactsHidden'] and not actual['detailsOpen']
    assert actual['calls'] == [] and actual['unchanged']


@pytest.mark.parametrize('languages,storage,expected', [
    (['ja-JP'], {}, ('en', 'dark')),
    (['en-US'], {}, ('en', 'dark')),
    (['fr-FR'], {}, ('en', 'dark')),
    (['en-US'], {'oif.locale': 'ja', 'oif.theme': 'light'}, ('ja', 'light')),
])
def test_preferences_persist_without_source_or_draft_mutation(harness, languages, storage, expected):
    client, store, _, identity, _ = harness
    # These originals intentionally match interface dictionary keys.
    store.update_task(identity, objective='停止')
    history = store.get_task(identity)['source_history']
    history[0]['text'] = '接続設定'
    store.set_fixture_source(identity, source_history=history)
    initial = client.get('/api/tasks/'+identity).json()
    actual, payload = run_dom(client, initial, languages=languages, storage=storage)
    assert (actual['startup']['locale'], actual['startup']['theme']) == expected
    assert actual['restored'] == {'theme': 'light', 'locale': 'en'}
    assert actual['unchanged'] and actual['calls'] == []
    assert actual['primary'] == '停止' and actual['sources'] == ['接続設定']
    assert dict(actual['drafts'])[identity]['text'] == payload['text']
    assert dict(actual['attachmentDrafts'])[identity]['file']['name'] == 'retained source.txt'
    assert actual['newDraft'] == '  new-task draft  '
    assert actual['approvalDraft'] == '  exact decision draft  '
    assert actual['settingsDraft'] == 'edited-unsaved-model'
    assert actual['untranslated'] == []
    assert actual['theme'] == 'light' and actual['locale'] == 'en'


def test_denied_preference_storage_does_not_block_task_controls(harness):
    client, _, _, identity, _ = harness
    actual, _ = run_dom(client, client.get('/api/tasks/'+identity).json(), storageDenied=True)
    assert actual['startup'] == {'locale': 'en', 'theme': 'dark'}
    assert actual['theme'] == 'light' and actual['locale'] == 'en'
    assert actual['restored'] == {'locale': 'en', 'theme': 'dark'}
    assert actual['unchanged'] and actual['calls'] == []


@pytest.mark.parametrize('status,expected', [
    ('not_configured', 'Not configured'),
    ('available', 'Saved. Enter a value only to change it.'),
    ('unavailable', 'Saved key unavailable. Enter it again.'),
])
def test_settings_display_uses_public_credential_status_after_preference_changes(harness, status, expected):
    client, _, _, identity, app = harness
    initial = client.get('/api/tasks/'+identity).json()

    class Settings(FixtureSettings):
        def public(self):
            return {'model': 'fixture', 'model_api_key_configured': status != 'not_configured',
                    'web_api_key_configured': False}

        def redaction_secrets(self):
            return (), ('model_api_key',) if status == 'unavailable' else ()

    app.state.settings = Settings()
    settings = client.get('/api/settings').json()
    assert settings['credential_status']['model'] == status
    actual, _ = run_dom(client, initial, settings=settings)
    assert actual['settingsPlaceholders']['model_api_key'] == expected
    assert actual['settingsPlaceholders']['web_api_key'] == 'Not configured'
    assert bool(actual['settingsError']) == (status == 'unavailable')
    assert actual['settingsDraft'] == 'edited-unsaved-model'
    assert actual['calls'] == [] and actual['unchanged']


def accepted(client, identity, snapshot, text):
    response = client.post('/api/tasks/'+identity+'/instructions',
                           json={'text': text, 'expected_source_hash': snapshot['task']['source_hash']})
    assert response.status_code == 200
    return response.json()


def test_rapid_followups_use_one_task_and_latest_accepted_hash(harness):
    client, store, engine, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    first_text = '  exact source <b>停止</b>\n  '
    second_text = '  second instruction while the first is pending\n'
    first = accepted(client, identity, initial, first_text)
    second = accepted(client, identity, first, second_text)
    actual, _ = run_dom(client, initial, mode='rapid', accepted=first, acceptedSecond=second)
    posts = [x for x in actual['calls'] if x['options'].get('method') == 'POST']
    assert len(posts) == 2 == actual['preferencePosts'] == actual['postCount']
    assert [x['path'] for x in posts] == ['/api/tasks/'+identity+'/instructions']*2
    assert [json.loads(x['options']['body']) for x in posts] == [
        {'text': first_text, 'expected_source_hash': initial['task']['source_hash']},
        {'text': second_text, 'expected_source_hash': first['task']['source_hash']}]
    assert actual['primary'] == initial['task']['objective']
    assert actual['sourceHash'] == second['task']['source_hash']
    assert actual['sources'][-2:] == [first_text, second_text]
    assert actual['taskId'] == identity and len(store.list_tasks()) == 1
    assert dict(actual['drafts'])[identity]['text'] == ''
    assert 'does not mean' in actual['message'] and actual['unchanged']
    assert len(engine.calls) == 2


@pytest.mark.parametrize('action', ['instruction', 'attachment'])
def test_selection_changes_do_not_redirect_inflight_followup(harness, action):
    client, store, engine, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    other_task = store.create_task('別の主目的', ['別の完成条件'])
    engine.start_task(other_task['id'])
    other = client.get('/api/tasks/'+other_task['id']).json()
    if action == 'instruction':
        response = accepted(client, identity, initial, '  exact source <b>停止</b>\n  ')
    else:
        response = client.post('/api/tasks/'+identity+'/attachments', json={
            'filename': 'retained source.txt', 'base64': 'eHl6', 'expected_source_hash': initial['task']['source_hash']}).json()
    actual, _ = run_dom(client, initial, mode='switch', action=action, other=other,
                        accepted=response, base64='eHl6')
    posts = [x for x in actual['calls'] if x['options'].get('method') == 'POST']
    assert len(posts) == 1 and posts[0]['path'] == '/api/tasks/'+identity+('/attachments' if action == 'attachment' else '/instructions')
    assert json.loads(posts[0]['options']['body'])['expected_source_hash'] == initial['task']['source_hash']
    assert actual['taskId'] == other_task['id'] and actual['primary'] == '別の主目的'
    assert actual['sourceHash'] == other['task']['source_hash']
    assert dict(actual['drafts'])[other_task['id']]['text'] == '  other task draft  '
    assert actual['unchanged'] and actual['postCount'] == 1


@pytest.mark.parametrize('action', ['instruction', 'attachment'])
def test_safe_actual_acceptance_receipt_is_not_resent_or_called_applied(harness, monkeypatch, action):
    client, store, engine, identity, app = harness
    initial = client.get('/api/tasks/'+identity).json()
    class HistorySettings(FixtureSettings):
        unavailable = False
        def redaction_secrets(self):
            return (), ('history:synthetic',) if self.unavailable else ()
    settings = HistorySettings()
    app.state.settings = settings
    method = 'submit_'+action
    original = getattr(engine, method)
    async def accepted_then_withheld(*args):
        value = await original(*args)
        settings.unavailable = True
        return value
    monkeypatch.setattr(engine, method, accepted_then_withheld)
    route = '/api/tasks/'+identity+('/attachments' if action == 'attachment' else '/instructions')
    body = {'expected_source_hash': initial['task']['source_hash']}
    body.update({'filename': 'retained source.txt', 'base64': 'eHl6'} if action == 'attachment' else {'text': '  exact source <b>停止</b>\n  '})
    response = client.post(route, json=body)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt['accepted'] is True and receipt['records_withheld'] is True and receipt['action'] == action
    assert set(receipt['task']) == {'id', 'status'}
    actual, _ = run_dom(client, initial, mode='withheld', action=action, accepted=receipt, base64='eHl6')
    assert actual['recovery'] == receipt and actual['taskId'] == identity
    assert actual['taskHidden'] and actual['sourceHash'] is None
    assert 'Do not resend' in actual['loadingText'] and 'display is withheld' in actual['notice']
    assert actual['postCount'] == 1 and actual['unchanged']
    assert dict(actual['drafts'])[identity]['text'] == '  second instruction while the first is pending\n'
    assert len(engine.calls if action == 'instruction' else engine.attachments) == 1
    assert store.get_task(identity)['objective'] == initial['task']['objective']


@pytest.mark.parametrize('action', ['instruction', 'attachment'])
def test_delayed_get_cannot_regress_accepted_source_or_next_send_hash(harness, action):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/' + identity).json()
    if action == 'instruction':
        first = accepted(client, identity, initial, '  exact source <b>停止</b>\n  ')
    else:
        first = client.post('/api/tasks/' + identity + '/attachments', json={
            'filename': 'retained source.txt', 'base64': 'eHl6',
            'expected_source_hash': initial['task']['source_hash']}).json()
    second = accepted(client, identity, first, '  second instruction while the first is pending\n')
    actual, _ = run_dom(client, initial, mode='order', action=action, deferSnapshot=True,
                        accepted=first, acceptedSecond=second, base64='eHl6')
    assert actual['afterDelayed'] == {
        'sourceHash': first['task']['source_hash'], 'sourceVersion': first['task']['source_version'],
        'taskVersion': first['task']['version'], 'primary': first['task']['objective'], 'withheld': False}
    posts = [c for c in actual['calls'] if c['options'].get('method') == 'POST']
    assert len(posts) == 2
    assert json.loads(posts[1]['options']['body']) == {
        'text': '  second instruction while the first is pending\n',
        'expected_source_hash': first['task']['source_hash']}
    assert actual['sourceHash'] == second['task']['source_hash']
    assert actual['taskId'] == identity and actual['unchanged']


@pytest.mark.parametrize('action', ['instruction', 'attachment'])
def test_delayed_public_get_cannot_restore_records_after_withheld_receipt(harness, monkeypatch, action):
    client, _, engine, identity, app = harness
    initial = client.get('/api/tasks/' + identity).json()
    class Withheld(FixtureSettings):
        unavailable = False
        def redaction_secrets(self):
            return (), ('history:synthetic',) if self.unavailable else ()
    settings = Withheld()
    app.state.settings = settings
    original = getattr(engine, 'submit_' + action)
    async def commit_then_withhold(*args):
        result = await original(*args)
        settings.unavailable = True
        return result
    monkeypatch.setattr(engine, 'submit_' + action, commit_then_withhold)
    body = {'expected_source_hash': initial['task']['source_hash']}
    body.update({'filename': 'retained source.txt', 'base64': 'eHl6'} if action == 'attachment'
                else {'text': '  exact source <b>停止</b>\n  '})
    receipt = client.post('/api/tasks/' + identity + ('/attachments' if action == 'attachment' else '/instructions'), json=body).json()
    actual, _ = run_dom(client, initial, mode='withheld-order', action=action,
                        deferSnapshot=True, accepted=receipt, base64='eHl6')
    assert actual['afterDelayed']['withheld'] and actual['afterDelayed']['sourceHash'] is None
    assert actual['recovery'] == receipt and actual['taskHidden']
    assert actual['sourceHash'] is None and actual['postCount'] == 1 and actual['unchanged']
    assert dict(actual['drafts'])[identity]['text'] == '  second instruction while the first is pending\n'


def test_tail_follow_preserves_older_reading_and_open_details_across_preferences(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/' + identity).json()
    actual, _ = run_dom(client, initial, mode='scroll')
    assert actual['scroll'] == {'followed': True, 'olderTop': 40, 'afterPreferencesTop': 40,
                                'keptOpen': True, 'failedOpen': False, 'count': len(initial['events']) + 2}
    assert actual['calls'] == [] and actual['unchanged']


@pytest.mark.parametrize('recovery', ['retry', 'lookup'])
def test_creation_response_loss_survives_refresh_without_plaintext_storage(harness, recovery):
    client, store, _, identity, _ = harness
    prompt = '  response loss: preserve this exact private prompt\n  '
    receipt = client.post('/api/tasks', json={'objective': prompt}).json()
    created = client.get('/api/tasks/' + receipt['task']['id']).json()
    actual, _ = run_dom(client, client.get('/api/tasks/' + identity).json(),
                        mode='creation-loss', recovery=recovery, startPrompt=prompt,
                        created=created, createReceipt=receipt)
    assert actual['retained']['draft'] == prompt
    saved = json.loads(actual['retained']['session']['oif.pending-submissions'])
    assert len(saved) == 1 and set(saved[0]) == {'id', 'payload_hash'}
    assert saved[0]['id'] == receipt['submission_id']
    assert prompt not in json.dumps(actual['retained']['session'])
    posts = [c for c in actual['calls'] + actual['recoveryCalls'] if c['options'].get('method') == 'POST']
    assert len(posts) == (2 if recovery == 'retry' else 1)
    for call in posts:
        body = json.loads(call['options']['body'])
        assert body == {'objective': prompt, 'submission_id': receipt['submission_id']}
        assert client.post(call['path'], json=body).json()['task']['id'] == created['task']['id']
    if recovery == 'lookup':
        assert any(c['path'] == '/api/submissions/' + receipt['submission_id'] for c in actual['recoveryCalls'])
    assert actual['taskId'] == created['task']['id'] and len(store.list_tasks()) == 2
    assert json.loads(actual['session']['oif.pending-submissions']) == []


def test_missing_creation_receipt_after_refresh_stays_pending_without_auto_post(harness):
    client, _, _, identity, _ = harness
    prompt = '  no receipt yet  '
    # A response fixture gives the first browser request its exact opaque ID;
    # the refreshed server lookup deliberately returns not-found.
    receipt = client.post('/api/tasks', json={'objective': prompt}).json()
    created = client.get('/api/tasks/' + receipt['task']['id']).json()
    actual, _ = run_dom(client, client.get('/api/tasks/' + identity).json(),
                        mode='creation-loss', recovery='lookup', lookupMissing=True,
                        startPrompt=prompt, created=created, createReceipt=receipt)
    assert not any(c['options'].get('method') == 'POST' for c in actual['recoveryCalls'])
    assert actual['session'] == actual['retained']['session']
    assert actual['taskId'] is None and actual['newDraft'] == ''
    assert 'No receipt is available yet' in actual['notice']


def test_creation_is_not_sent_if_recovery_identity_cannot_be_saved(harness):
    client, _, _, identity, _ = harness
    actual, _ = run_dom(client, client.get('/api/tasks/' + identity).json(),
                        mode='creation-storage', sessionDenied=True, startPrompt='retained unsent draft')
    assert actual['calls'] == [] and actual['session'] == {}
    assert actual['draft'] == 'retained unsent draft' and 'nothing was sent' in actual['notice']


def test_successful_program_keeps_cleanup_warning_in_progress(harness):
    if not shutil.which('node'):
        pytest.skip('Node DOM fixture is unavailable; Windows observation is separate')
    client,_,_,_,_=harness
    script=client.get('/static/presentation.js').text
    event={'stage':'execution','status':'succeeded','detail':{'kind':'run_python','purpose':'計算結果を保存する',
        'result':{'status':'succeeded','stdout':'5050','data':{'cleanup':{'removed':None,'reason':'CLEANUP_INTERRUPTED'}}}}}
    runner="""const fs=require('fs'),vm=require('vm');const v=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInThisContext(v.script);const original=JSON.stringify(v.event);const result={important:OIFPresentation.important(v.event),view:OIFPresentation.describe(v.event,null,''),unchanged:original===JSON.stringify(v.event)};process.stdout.write(JSON.stringify(result));"""
    done=subprocess.run([shutil.which('node'),'-e',runner],input=json.dumps({'script':script,'event':event}),text=True,capture_output=True,encoding='utf-8',check=True)
    value=json.loads(done.stdout)
    assert value['important'] and value['unchanged']
    assert '後片付け' in value['view']['overview'] and '計算結果を保存する' in value['view']['overview']
    assert '5050' in value['view']['outcome'] and 'CLEANUP_INTERRUPTED' in value['view']['outcome']


def test_friendly_progress_hides_full_and_abbreviated_fingerprints(harness):
    if not shutil.which('node'):
        pytest.skip('Node presentation fixture unavailable')
    client,*_=harness
    values=['SHA256 '+('a'*64), 'SHA256 2cf24dba…9824', 'SHA-256: `2cf24dba...9824`']
    runner="const fs=require('fs'),vm=require('vm');const v=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInThisContext(v.script);process.stdout.write(JSON.stringify(v.values.map(OIFPresentation.friendly)));"
    done=subprocess.run([shutil.which('node'),'-e',runner],input=json.dumps({'script':client.get('/static/presentation.js').text,'values':values}),text=True,capture_output=True,encoding='utf-8',check=True)
    actual=json.loads(done.stdout)
    assert all(v=='ファイルの照合情報（詳細に保存）' for v in actual)
