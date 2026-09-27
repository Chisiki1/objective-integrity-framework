"""Conversation provenance, bounded large-record consumers and HTTP privacy."""
import json
import shutil
import subprocess
from pathlib import Path
import policy_harness

from tests.test_source_input_ui import harness
from tests.test_ui_preferences import run_dom

STATIC = Path(policy_harness.__file__).resolve().parent / 'static'


def node_check(code):
    run = subprocess.run([shutil.which('node'), '-e', code], capture_output=True,
                         text=True, encoding='utf-8', timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_source_turns_preserve_completed_answers_and_pending_receipts():
    code = (STATIC/'conversation.js').read_text(encoding='utf-8') + r'''
const assert=require('assert/strict'),C=globalThis.OIFConversation;
const a={id:'a',kind:'initial',text:'first',status:'applied',created_at:'2026-01-01'},b={id:'b',kind:'instruction',text:'second',status:'pending',created_at:'2026-01-02'};
const events=[{seq:91,stage:'task',status:'completed',created_at:'2026-01-01',detail:{summary:'kept answer'}},{seq:92,stage:'source',status:'received',detail:{source:b},created_at:'2026-01-02'},{seq:93,stage:'model',status:'started',created_at:'2026-01-02'}];
let old=C.reconcile(null,C.project({id:'t',source_history:[a]},events.slice(0,1)));old.open.set(91,true);
let groups=C.project({id:'t',source_history:[a]},events),view=C.reconcile(old,groups);
assert.equal(groups.length,2);assert.equal(groups[0].final.summary,'kept answer');assert.equal(groups[1].source.status,'pending');assert.equal(groups[1].events[0].seq,92);
assert.deepEqual([...view.expanded],['b']);assert.equal(view.open.size,0);
view.open.set(93,true);C.collapse(view,groups[1]);assert.equal(view.expanded.size,0);assert.equal(view.open.size,0);
C.reconcile(view,C.project({id:'t',source_history:[a,b]},events));assert.equal(view.expanded.size,0,'updates must respect manual collapse');
console.log(JSON.stringify({ok:true}));'''
    assert node_check(code)['ok']


def test_artifact_latest_version_uses_writes_not_old_file_rereads():
    code = (STATIC/'conversation.js').read_text(encoding='utf-8') + r'''
const assert=require('assert/strict'),a=sha=>({relative_path:'report.md',sha256:sha});
const rows=[{tool_name:'file_write',result:{artifacts:[a('old')]}},{tool_name:'file_edit',result:{artifacts:[a('new')]}},{tool_name:'file_read',result:{artifacts:[a('old')]}}];
const result=OIFConversation.artifacts(rows);assert.equal(result.length,2);assert.equal(result[0].artifact.sha256,'new');assert.equal(result[0].latest,true);assert.equal(result[1].latest,false);console.log(JSON.stringify({ok:true}));'''
    assert node_check(code)['ok']


def test_large_record_first_page_is_lazy_and_unicode_is_lossless():
    code = (STATIC/'record-view.js').read_text(encoding='utf-8') + r'''
const assert=require('assert/strict');
let visited=0;const data={first:'x'.repeat(20_000_000)};Object.defineProperty(data,'last',{enumerable:true,get(){visited++;return 'untouched';}});
const iterator=OIFRecords.json(data);let text='',steps=0;while(text.length<OIFRecords.PAGE){text+=iterator.next().value;steps++;}
assert.equal(visited,0);assert.ok(text.length<27000);assert.ok(steps<25);
const original={quoted:'"\\\n\r\t',unicode:'あ'.repeat(2047)+'😀',nested:[false,null,3,{a:'💡'}]};
assert.deepEqual(JSON.parse([...OIFRecords.json(original)].join('')),original);
assert.ok(OIFRecords.preview(data).length<=12001);assert.equal(visited,0);
console.log(JSON.stringify({ok:true,steps}));'''
    assert node_check(code)['ok']


def test_light_snapshot_delta_and_raw_record_retain_exact_sanitized_content(harness):
    client, store, _, identity, app = harness
    secret = 'fixture-secret-only-123456'
    app.state.settings.secret = lambda name: secret if name == 'model_api_key' else None
    text = ('日本語 <script>alert(1)</script> '+secret+'\n') * 20000
    event = store.event(identity, 'model', 'responded', {'summary': 'Finished reading', 'response': {'text': text}})
    old = client.get('/api/tasks/'+identity)
    light = client.get('/api/tasks/'+identity+'?view=conversation').json()
    row = next(e for e in light['events'] if e['seq']==event['seq'])
    assert len(json.dumps(row,ensure_ascii=False)) < 5000
    assert secret not in json.dumps(light)
    raw = client.get(row['_record_url'])
    assert raw.status_code == 200
    assert raw.json()['detail']['response']['text'] == text.replace(secret,'[非表示]')
    assert client.get(row['_record_url']+'?download=true').headers['content-disposition'].startswith('attachment;')
    assert client.get('/api/tasks/'+identity+'?view=conversation&after='+str(event['seq'])).json()['events'] == []
    assert len(old.content) > 500_000
    all_records = client.get('/api/tasks/'+identity+'/records')
    assert all_records.status_code == 200 and secret not in all_records.text
    assert all_records.json()['events'][-1]['detail']['response']['text'] == text.replace(secret,'[非表示]')
    assert client.get('/api/tasks/'+'f'*32+'/records/events/'+str(event['seq'])).status_code == 404


def test_large_progress_renders_a_page_and_keeps_table_order(harness):
    client, _, _, identity, _ = harness
    initial = client.get('/api/tasks/'+identity).json()
    initial['events'] = [dict(seq=i+1,task_id=identity,stage='task',status='progress',
                              detail={'message':'Progress '+str(i+1)},created_at='2026-09-15T01:00:00Z') for i in range(2000)]
    actual, _ = run_dom(client,initial,mode='presentation')
    assert len(actual['records']) == 80
    assert actual['rows'] == 100
    assert len(actual['recordDetails']) == 80


def test_many_instruction_projection_and_window_are_bounded(harness):
    # The prior regression used only one instruction. This guards the E*S path.
    code = (STATIC/'conversation.js').read_text(encoding='utf-8') + r'''
const assert=require('assert/strict'),sources=[],events=[];let reads=0;
for(let i=0;i<5000;i++){
 const source={id:'s'+i,kind:'instruction',text:'request',status:'applied',_event_seq:i*4+1};
 Object.defineProperty(source,'created_at',{enumerable:true,get(){reads++;return new Date(1700000000000+i*1000).toISOString();}});
 sources.push(source);
 for(let n=0;n<4;n++)events.push({seq:i*4+n+1,stage:'task',status:n===3?'completed':'progress',created_at:new Date(1700000000000+i*1000).toISOString(),detail:{summary:'answer '+i}});
}
const result=OIFConversation.project({id:'t',source_history:sources},events);
assert.equal(result.length,5000);assert.ok(result.every(g=>g.events.length===4));assert.equal(result[4999].final.summary,'answer 4999');assert.ok(reads<150000,'source lookup must not run once per event/source pair');
console.log(JSON.stringify({ok:true,reads}));'''
    assert node_check(code)['ok']


def test_same_page_chat_link_selects_the_linked_task(harness):
    client, store, _, identity, _ = harness
    initial = client.get('/api/tasks/' + identity).json()
    other = store.create_task('Another linked task', ['Keep its own instruction'])
    snapshot = client.get('/api/tasks/' + other['id']).json()
    actual, _ = run_dom(client, initial, mode='navigation', other=snapshot)
    assert actual['taskId'] == other['id']
    assert actual['primary'] == 'Another linked task'
    assert any('/api/tasks/' + other['id'] in call['path'] for call in actual['calls'])
    client, _, _, identity, _ = harness
    initial=client.get('/api/tasks/'+identity).json()
    initial['task']['source_history']=[dict(id='s'+str(i),kind='instruction',text='Instruction '+str(i),status='applied',created_at=f'2026-01-01T00:{i//60:02}:{i%60:02}Z') for i in range(100)]
    initial['events']=[]
    actual,_=run_dom(client,initial,mode='presentation')
    assert actual['turnCount']==20


def test_conversation_reads_only_event_page_and_source_page(harness,monkeypatch):
    client,store,_,identity,app=harness
    real=store._store
    for i in range(42):
        task=real.get_task(identity)
        real.append_instruction(identity,'request '+str(i),task['source_hash'])
    for i in range(600):
        real.event(identity,'task','progress',{'message':str(i)})
    def full_read_forbidden(*args,**kwargs):
        raise AssertionError('UI must not load the full operation/event history')
    monkeypatch.setattr(store,'operations',full_read_forbidden)
    monkeypatch.setattr(store,'events',full_read_forbidden)
    latest=client.get('/api/tasks/'+identity+'?view=conversation').json()
    assert len(latest['events'])==160
    assert len(latest['task']['source_history'])==20
    assert latest['record_page']['source_count']==43
    assert latest['record_page']['pending_source_count']==42
    prior=client.get('/api/tasks/'+identity+'?view=conversation&before='+str(latest['record_page']['event_before'])+'&source_before='+str(latest['record_page']['source_before'])).json()
    assert len(prior['events'])==160
    assert prior['events'][-1]['seq']<latest['events'][0]['seq']
    assert prior['task']['source_history'][-1]['_index']<latest['task']['source_history'][0]['_index']
    assert client.get(latest['task']['source_history'][-1]['_record_url']).json()['text']=='request 41'
    # A reconnection with a backlog starts at the cursor, not at the newest page.
    first=real.db.execute('SELECT min(seq) FROM events WHERE task_id=?',(identity,)).fetchone()[0]
    delta=client.get('/api/tasks/'+identity+'?view=conversation&after='+str(first)).json()
    assert delta['events'][0]['seq']==first+1
    assert delta['record_page']['event_more_after']


def test_operation_pages_keep_latest_artifact_despite_old_readbacks(harness):
    import hashlib
    from policy_harness.store import canonical
    client,store,_,identity,_=harness
    path=Path(store.get_task(identity)['workspace'])/'version.txt';path.write_text('new',encoding='utf-8')
    new_hash=hashlib.sha256(b'new').hexdigest();old_hash=hashlib.sha256(b'old').hexdigest()
    for i in range(130):
        tool='file_write' if i<2 else 'file_read';sha=new_hash if i==1 else old_hash
        value={'task_id':identity,'operation':{'id':'op'+str(i),'kind':tool,'purpose':'fixture'},'tool_name':tool,'status':'succeeded','result':{'data':'large irrelevant body'*3000,'artifacts':[{'path':str(path),'sha256':sha}]}}
        store.db.execute('INSERT INTO operations VALUES(?,?,?)',('op'+str(i),identity,canonical(value)))
    light=client.get('/api/tasks/'+identity+'?view=conversation').json()
    assert len(light['operations'])==80
    assert 'op0' not in [r['operation']['id'] for r in light['operations']]
    assert light['artifact_operations'][0]['operation']['id']=='op1'
    assert light['artifact_operations'][0]['result']['artifacts'][0]['sha256']==new_hash
    older=client.get('/api/tasks/'+identity+'?view=conversation&operation_before='+str(light['record_page']['operation_before'])).json()
    assert len(older['operations'])==50 and older['operations'][0]['operation']['id']=='op0'
    other=path.with_name('second.txt');other.write_bytes(b'new')
    value={'task_id':identity,'operation':{'id':'latest-write','kind':'file_write','purpose':'new artifact'},'tool_name':'file_write','status':'succeeded','result':{'artifacts':[{'path':str(other),'sha256':new_hash}]}}
    store.db.execute('INSERT INTO operations VALUES(?,?,?)',('latest-write',identity,canonical(value)))
    light=client.get('/api/tasks/'+identity+'?view=conversation').json()
    actual,_=run_dom(client,light,mode='presentation')
    assert [a['text'] for a in actual['artifacts'][:2]]==['second.txt','version.txt']


def test_equal_timestamp_source_pages_use_saved_order():
    code=(STATIC/'conversation.js').read_text(encoding='utf-8')+r'''
const assert=require('assert/strict');
const sources=[2,3,0,1].map(i=>({id:'s'+i,_index:i,kind:'instruction',status:'applied',created_at:'2026-01-01T00:00:00.00000'+i+'Z',_event_seq:i+1}));
const groups=OIFConversation.project({id:'t',source_history:sources},sources.map(s=>({seq:s._event_seq,stage:'task',status:'completed',detail:{summary:s.id}})));
assert.deepEqual(groups.map(g=>g.id),['s0','s1','s2','s3']);assert.equal(groups.at(-1).final.summary,'s3');
console.log(JSON.stringify({ok:true}));'''
    assert node_check(code)['ok']


def test_invalid_latest_artifact_does_not_hide_valid_candidate_outside_operation_page(harness):
    import hashlib
    from policy_harness.store import canonical
    client,store,_,identity,app=harness
    path=Path(store.get_task(identity)['workspace'])/'valid.txt';path.write_bytes(b'valid')
    digest=hashlib.sha256(b'valid').hexdigest()
    def operation(key,sha,artifacts=True):
        value={'task_id':identity,'operation':{'id':key,'kind':'file_write','purpose':'fixture'},'tool_name':'file_write','status':'succeeded',
               'result':{'artifacts':[{'path':str(path),'sha256':sha}] if artifacts else []}}
        store.db.execute('INSERT INTO operations VALUES(?,?,?)',(key,identity,canonical(value)))
    operation('original',digest)
    for i in range(90):
        operation('unrelated'+str(i),digest,False)
    for key,sha in [('bad-sha','invalid'),('bad/id',digest),('private-identity',digest)]:
        operation(key,sha)
    app.state.settings.secret=lambda name:'private-identity' if name=='model_api_key' else None
    light=client.get('/api/tasks/'+identity+'?view=conversation').json()
    assert 'original' not in [r['operation']['id'] for r in light['operations']]
    assert [r['operation']['id'] for r in light['artifact_operations']]==['original']
    artifact=light['artifact_operations'][0]['result']['artifacts'][0]
    assert artifact['download_operation_id']=='original'
    assert client.get(artifact['download_url']).content==b'valid'


def test_instruction_post_preserves_order_and_stale_page_cannot_move_cursors(harness,monkeypatch):
    from tests import test_ui_preferences as ui
    client,_,_,identity,_=harness
    initial=client.get('/api/tasks/'+identity).json()
    initial['task']['source_history']=[{'id':'s'+str(i),'kind':'instruction','text':'request '+str(i),'status':'applied','created_at':'2026-01-01T00:00:00.00000'+str(i)+'Z','_index':i} for i in range(4)]
    branch=r'''
 if(input.mode==='source-page-race'){
  const assert=require('assert/strict'),base=input.initial;
  const sources=Array.from({length:5},(_,i)=>({id:'s'+i,kind:'instruction',text:'request '+i,status:'applied',created_at:'2026-01-01T00:00:00.00000'+i+'Z',_index:i}));
  h.renderSnapshot({...base,display_projection:true,task:{...base.task,source_history:[sources[2],sources[3],sources[0],sources[1]]}});
  const post={...base,task:{...base.task,version:100,source_version:100,source_history:sources.map(({_index,...source})=>source)}};
  assert.equal(h.renderSnapshot(post),true);
  assert.deepEqual(Array.from(h.state.snapshot.task.source_history,s=>s.id),['s0','s1','s2','s3','s4']);
  assert.equal(document.getElementById('current-source-text').textContent,'request 4');
  const before=h.pageState();let respond;
  r.context.fetch=()=>new Promise(resolve=>{respond=resolve;});
  const pending=h.loadRecordPage({before:45,source_before:30,operation_before:90,artifact_page:2});
  h.renderSnapshot({...post,task:{...post.task,version:101,source_version:101}});
  respond({ok:true,status:200,json:async()=>({...post,record_page:{event_before:12,source_before:10}})});
  assert.equal(await pending,false);assert.deepEqual(h.pageState(),before);
  assert.equal(h.state.snapshot.task.source_version,101);
  process.stdout.write(JSON.stringify({ok:true}));return;
 }
'''
    runner=ui.DOM_RUNNER.replace('recoverCreation,pendingSubmissions,boot',
        'recoverCreation,pendingSubmissions,boot,loadRecordPage,pageState:()=>({historyBefore,sourceBefore,operationCursor,artifactPage})')
    monkeypatch.setattr(ui,'DOM_RUNNER',runner.replace(" if(input.mode==='presentation')",branch+" if(input.mode==='presentation')",1))
    actual,_=ui.run_dom(client,initial,mode='source-page-race')
    assert actual['ok']


def test_first_loading_of_past_turn_shows_its_short_page(harness,monkeypatch):
    from tests import test_ui_preferences as ui
    client,_,_,identity,_=harness
    initial=client.get('/api/tasks/'+identity).json()
    initial.update(display_projection=True,record_page={'event_before':200})
    initial['task']['source_history']=[{'id':name,'_index':i,'_event_seq':seq,'kind':'instruction','text':name,'status':'applied','created_at':'2026-01-01'} for i,(name,seq) in enumerate([('first',1),('second',200)])]
    initial['events']=[{'seq':i,'task_id':identity,'stage':'task','status':'progress','detail':{'message':'newer '+str(i)},'created_at':'2026-01-01'} for i in range(200,360)]
    branch=r'''
 if(input.mode==='past-turn-first-page'){
  const assert=require('assert/strict'),turns=()=>r.descendants(document.getElementById('chat-events')).filter(e=>e.className==='turn-progress');
  let first=turns()[0];first.open=true;first.listeners.toggle();
  const load=r.descendants(turns()[0]).find(e=>e.tag==='button'&&e.textContent==='Load earlier progress');assert.ok(load);
  r.context.fetch=async()=>({ok:true,status:200,json:async()=>({...input.initial,record_page:{event_before:160},events:Array.from({length:40},(_,i)=>({seq:160+i,task_id:h.state.taskId,stage:'task',status:'progress',detail:{message:'older '+i},created_at:'2026-01-01'}))})});
  await load.onclick();
  const cards=r.descendants(turns()[0]).filter(e=>String(e.className).startsWith('event-card'));
  assert.equal(cards.length,40,'the first fetched page must not be skipped by a fixed 80-row offset');
  assert.ok(r.descendants(cards[0]).some(e=>e.textContent==='older 0'));
  assert.ok(r.descendants(cards.at(-1)).some(e=>e.textContent==='older 39'));
  process.stdout.write(JSON.stringify({ok:true}));return;
 }
'''
    monkeypatch.setattr(ui,'DOM_RUNNER',ui.DOM_RUNNER.replace(" if(input.mode==='presentation')",branch+" if(input.mode==='presentation')",1))
    actual,_=ui.run_dom(client,initial,mode='past-turn-first-page')
    assert actual['ok']


def test_raw_stream_is_lazy_consistent_and_closes_on_cancel(harness,monkeypatch):
    import asyncio
    import sqlite3
    from contextlib import contextmanager
    from policy_harness import record_reader
    from policy_harness.store import canonical
    client,store,_,identity,app=harness
    for i in range(3):
        value={'task_id':identity,'operation':{'id':'large'+str(i),'kind':'file_read','purpose':'fixture'},'status':'succeeded','result':{'data':'a'*500_000}}
        store.db.execute('INSERT INTO operations VALUES(?,?,?)',('large'+str(i),identity,canonical(value)))
    original=record_reader.snapshot_reader;readers=[];seen=[]
    @contextmanager
    def tracked(*args,**kwargs):
        with original(*args,**kwargs) as (reader,engine):
            readers.append(reader);iterator=reader.iter_operations
            def records():
                for record in iterator():
                    seen.append(record['operation']['id']);yield record
            reader.iter_operations=records
            yield reader,engine
    monkeypatch.setattr(record_reader,'snapshot_reader',tracked)
    endpoint=next(r.endpoint for r in app.routes if getattr(r,'path','')=='/api/tasks/{task_id}/records')
    async def consume_first_page():
        response=await endpoint(identity)
        chunk=await anext(response.body_iterator)
        assert len(chunk)<70_000
        assert seen==['large0']
        await response.body_iterator.aclose()
    asyncio.run(consume_first_page())
    import pytest
    with pytest.raises(sqlite3.ProgrammingError):
        readers[-1].db.execute('SELECT 1')

