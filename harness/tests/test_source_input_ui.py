"""Source-input and artifact UI boundary checks; Engine/model semantics are fixtures.

When staged, import the adjacent candidate files without changing production.
After integration, use the installed package. A Node DOM fixture checks the
actual JavaScript handlers; it is explicitly not a live-browser assertion.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from policy_harness.executor import Executor
from policy_harness.models import Operation, PolicyError, now
from policy_harness.store import Store, digest


def ui_module(name):
    candidate = Path(__file__).resolve().parents[1] / (name + '.py')
    if candidate.exists() and (candidate.parent/'static/index.html').exists():
        identity = 'policy_harness._source_ui_' + name
        spec = importlib.util.spec_from_file_location(identity, candidate)
        module = importlib.util.module_from_spec(spec)
        sys.modules[identity] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module('policy_harness.' + name)


server = ui_module('server')
cli = ui_module('cli')
INITIAL = '  最初の依頼\n元の条件を保つ。  '
ADDED = '  追加の条件 <b>文字列</b>\n改行と末尾を残す。 \t'


class FixtureSettings:
    def public(self): return {'model':'fixture', 'model_api_key_configured':False}
    def secret(self, name): return None


class FixturePolicy:
    hash = 'fixture-policy-only'
    def summary(self): return {'fixture':True}


class FixtureSourceStore:
    """Explicit future source projection, not a persistence/CAS proof.

    Existing Store protection remains intact. Root's source protocol tests own
    durable storage, admission, semantic reconciliation and actual continuation.
    """
    def __init__(self, data_dir):
        self.fixture_sources={}
        self.original_inputs={}
        self._store=Store(data_dir)

    def __getattr__(self, name):
        return getattr(self._store,name)

    def create_task(self, objective, acceptance, parent_id=None, **kwargs):
        task=self._store.create_task(objective,acceptance,parent_id,**kwargs)
        self.original_inputs[task['id']]={'objective':objective,'acceptance':list(acceptance)}
        return task

    def set_fixture_source(self, identity, **fields):
        self.fixture_sources[identity]=dict(self.fixture_sources.get(identity,{}),**fields)

    def get_task(self, identity):
        return dict(self._store.get_task(identity),**self.fixture_sources.get(identity,{}))

    def list_tasks(self):
        return [self.get_task(task['id']) for task in self._store.list_tasks()]


class FixtureInstructionEngine:
    policy = FixturePolicy()

    def __init__(self, store):
        self.store, self.calls, self.race = store, [], False
        self.attachments=[]

    def start_task(self, task_id):
        task = self.store.get_task(task_id)
        original=self.store.original_inputs[task_id]
        history = [{'id':uuid4().hex, 'kind':'initial', 'text':original['objective'],
                    'acceptance':original['acceptance'], 'created_at':now()}]
        self.store.set_fixture_source(task_id, source_history=history, source_hash=digest(history), source_version=1)

    def snapshot(self, task_id):
        return {'task':self.store.get_task(task_id), 'operations':self.store.operations(task_id),
                'events':self.store.events(task_id), 'knowledge':{'ideas':[], 'skills':[]},
                'readiness':{'fixture':True}, 'children':[]}

    async def submit_instruction(self, task_id, text, expected_source_hash):
        self.calls.append((task_id, text, expected_source_hash))
        task = self.store.get_task(task_id)
        if self.race or task['source_hash'] != expected_source_hash:
            raise PolicyError('SOURCE_HASH_CONFLICT: fixture CAS changed inside the engine')
        history = [*task['source_history'], {'id':uuid4().hex, 'kind':'instruction', 'text':text, 'created_at':now()}]
        self.store.set_fixture_source(task_id, source_history=history, source_hash=digest(history), source_version=task['source_version']+1)
        self.store.update_task(task_id, status='running')
        self.store.event(task_id, 'source_update', 'accepted', {'text':text, 'fixture':True})
        return self.snapshot(task_id)

    async def submit_attachment(self, task_id, filename, raw_bytes, expected_source_hash):
        self.attachments.append((task_id,filename,raw_bytes,expected_source_hash))
        task=self.store.get_task(task_id)
        if self.race or task['source_hash']!=expected_source_hash:
            raise PolicyError('SOURCE_HASH_CONFLICT: fixture attachment CAS changed')
        history=[*task['source_history'],{'id':uuid4().hex,'kind':'attachment','filename':filename,
                 'bytes':len(raw_bytes),'sha256':hashlib.sha256(raw_bytes).hexdigest(),'created_at':now()}]
        self.store.set_fixture_source(task_id,source_history=history,source_hash=digest(history),source_version=task['source_version']+1)
        return self.snapshot(task_id)

    def pending_approvals(self): return []


@pytest.fixture
def harness(tmp_path_factory):
    tmp_path=tmp_path_factory.mktemp('ui')
    store = FixtureSourceStore(tmp_path/'data')
    engine = FixtureInstructionEngine(store)
    task = store.create_task('元の目的', ['元の完成条件'])
    history = [{'id':'original-source', 'kind':'initial', 'text':INITIAL, 'created_at':now()}]
    store.set_fixture_source(task['id'], source_history=history, source_hash=digest(history), source_version=1)
    store.update_task(task['id'],state={'unknown_effect':'fixture pending effect must remain visible'})
    app = server.create_app(store.data_dir, engine=engine, settings=FixtureSettings(), store=store)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1',41321)) as client:
        assert client.get('/').status_code == 200
        client.headers['X-CSRF-Token'] = client.get('/api/session').json()['csrf_token']
        yield client, store, engine, task['id'], app
    store.close()


def test_original_instruction_same_task_and_cas(harness):
    client, store, engine, identity, _ = harness
    original = client.get('/api/tasks/'+identity).json()
    response = client.post('/api/tasks/'+identity+'/instructions',
                           json={'text':ADDED, 'expected_source_hash':original['task']['source_hash']})
    assert response.status_code == 200
    current = response.json()
    assert engine.calls == [(identity, ADDED, original['task']['source_hash'])]
    assert current['task']['id'] == identity and len(store.list_tasks()) == 1
    assert current['task']['objective'] == original['task']['objective']
    assert current['task']['acceptance'] == original['task']['acceptance']
    assert current['task']['state'] == original['task']['state']
    assert [s['text'] for s in current['task']['source_history']] == [INITIAL, ADDED]
    assert current['task']['source_hash'] != original['task']['source_hash']
    assert current['task']['source_version'] == 2


def test_initial_source_is_passed_to_store_without_trimming(harness):
    client,store,_,_,_=harness
    body={'objective':INITIAL,'acceptance':['  条件を保つ \t','\n二つ目\n']}
    response=client.post('/api/tasks',json=body)
    assert response.status_code==201
    task=response.json()['task']
    assert store.original_inputs[task['id']]==body
    assert task['source_history'][0]['text']==INITIAL
    assert task['source_history'][0]['acceptance']==body['acceptance']
    assert client.post('/api/tasks',json={'objective':' \n\t'}).status_code==422
    assert client.post('/api/tasks',json={'objective':'有効','acceptance':[' \t']}).status_code==422


def test_public_source_projection_scrubs_credential_patterns(harness):
    client,store,engine,identity,_=harness
    synthetic='sk-'+'a'*40
    response=client.post('/api/tasks/'+identity+'/instructions',json={'text':'  '+synthetic+'\n',
        'expected_source_hash':store.get_task(identity)['source_hash']})
    assert response.status_code==200 and synthetic not in response.text
    assert engine.calls[0][1]=='  '+synthetic+'\n'


@pytest.mark.parametrize('engine_race', [False, True])
def test_stale_hash_is_409_without_replay_or_new_source(harness, engine_race):
    client, store, engine, identity, _ = harness
    original = store.get_task(identity)
    engine.race = engine_race
    response = client.post('/api/tasks/'+identity+'/instructions', json={'text':ADDED,
        'expected_source_hash':original['source_hash'] if engine_race else '0'*64})
    assert response.status_code == 409 and response.json()['detail'].startswith('SOURCE_HASH_CONFLICT:')
    assert store.get_task(identity) == original
    assert len(engine.calls) == int(engine_race)


@pytest.mark.parametrize('body', [{'text':' \n\t','expected_source_hash':'0'*64},
    {'text':ADDED}, {'text':ADDED, 'expected_source_hash':'not-a-hash'},
    {'text':ADDED, 'expected_source_hash':'0'*64, 'skip_review':True}])
def test_invalid_instruction_does_not_enter_engine(harness, body):
    client, _, engine, identity, _ = harness
    assert client.post('/api/tasks/'+identity+'/instructions', json=body).status_code == 422
    assert engine.calls == []


@pytest.mark.parametrize('kind',['instructions','attachments'])
def test_instruction_security_and_shutdown_boundary(harness,kind):
    client, store, engine, identity, app = harness
    payload={'text':ADDED, 'expected_source_hash':store.get_task(identity)['source_hash']}
    if kind=='attachments':payload={'filename':'sample.txt','base64':'eA==','expected_source_hash':payload['expected_source_hash']}
    route='/api/tasks/'+identity+'/'+kind
    assert client.post(route,json=payload,headers={'X-CSRF-Token':''}).status_code == 403
    assert client.post(route,json=payload,headers={'Origin':'https://outside.invalid'}).status_code == 403
    assert client.post(route,json=payload,headers={'Host':'outside.invalid'}).status_code == 403
    app.state.shutting_down=True
    assert client.post(route,json=payload).status_code == 409
    assert engine.calls == [] and engine.attachments == []


def test_attachment_exact_raw_bytes_same_source_and_no_workspace_write(harness):
    client,store,engine,identity,_=harness
    task=store.get_task(identity);raw=b'\x00\xff\r\nUTF-8: '+ '原文'.encode('utf-8')
    before=sorted(Path(task['workspace']).rglob('*'))
    response=client.post('/api/tasks/'+identity+'/attachments',json={'filename':'資料 #1.bin',
        'base64':base64.b64encode(raw).decode('ascii'),'expected_source_hash':task['source_hash']})
    assert response.status_code==200
    current=response.json()['task']
    assert engine.attachments==[(identity,'資料 #1.bin',raw,task['source_hash'])]
    assert current['id']==identity and current['source_hash']!=task['source_hash']
    assert current['source_history'][0]==task['source_history'][0]
    assert current['source_history'][-1]['sha256']==hashlib.sha256(raw).hexdigest()
    assert current['source_history'][-1]['bytes']==len(raw)
    assert sorted(Path(task['workspace']).rglob('*'))==before


@pytest.mark.parametrize('engine_race',[False,True])
def test_attachment_stale_conflict_never_resends(harness,engine_race):
    client,store,engine,identity,_=harness
    original=store.get_task(identity);engine.race=engine_race
    response=client.post('/api/tasks/'+identity+'/attachments',json={'filename':'sample.txt','base64':'eA==',
        'expected_source_hash':original['source_hash'] if engine_race else '0'*64})
    assert response.status_code==409 and store.get_task(identity)==original
    assert len(engine.attachments)==int(engine_race)


@pytest.mark.parametrize('filename,encoded',[('../sample','eA=='),('C:sample','eA=='),('a\\b','eA=='),
    ('\x00','eA=='),('sample.txt','??invalid??'),('sample.txt','data:application/octet-stream;base64,eA==')])
def test_attachment_invalid_input_is_held_before_engine(harness,filename,encoded):
    client,store,engine,identity,_=harness
    response=client.post('/api/tasks/'+identity+'/attachments',json={'filename':filename,'base64':encoded,
        'expected_source_hash':store.get_task(identity)['source_hash']})
    assert response.status_code==422 and engine.attachments==[]


def test_attachment_exact_limit_and_one_byte_over(harness):
    client,store,engine,identity,_=harness
    limit=client.get('/api/session').json()['attachment_max_bytes']
    assert limit==10*1024*1024
    payload={'filename':'bounded.bin','base64':base64.b64encode(b'x'*(limit+1)).decode('ascii'),
             'expected_source_hash':store.get_task(identity)['source_hash']}
    assert client.post('/api/tasks/'+identity+'/attachments',json=payload).status_code==413
    assert engine.attachments==[]
    payload['base64']=base64.b64encode(b'x'*limit).decode('ascii')
    assert client.post('/api/tasks/'+identity+'/attachments',json=payload).status_code==200
    assert len(engine.attachments)==1 and len(engine.attachments[0][2])==limit


def add_actual_artifact(store, identity, *, name='reports/結果 #1.txt'):
    task=store.get_task(identity)
    operation=Operation(kind='file_write',args={'path':name,'text':'実際の成果物\n'},
        purpose='Explicit isolated UI artifact fixture',expected_result='Exact file bytes',
        decisions=[{'id':'owned','statement':'Use only this fixture workspace','rationale':'UI boundary test'}])
    result=asyncio.run(Executor(store.data_dir).execute(Path(task['workspace']),operation))
    assert result.status=='succeeded' and result.effect=='confirmed'
    store.save_operation(identity,operation.model_dump())
    store.update_operation(operation.id,status='cycle_complete',result=result.model_dump())
    return operation,result


def test_actual_absolute_artifact_metadata_to_download(harness):
    client, store, _, identity, _ = harness
    operation, actual=add_actual_artifact(store,identity)
    assert Path(actual.artifacts[0]['path']).is_absolute()
    snapshot=client.get('/api/tasks/'+identity).json()
    artifact=snapshot['operations'][0]['result']['artifacts'][0]
    assert artifact['path']==actual.artifacts[0]['path']
    assert artifact['relative_path']=='reports/結果 #1.txt'
    assert '%23' in artifact['download_url'] and artifact['download_url'].startswith('/api/tasks/'+identity+'/artifacts/')
    download=client.get(artifact['download_url'])
    assert download.status_code==200 and download.content=='実際の成果物\n'.encode('utf-8')
    assert download.headers['content-type']=='application/octet-stream'
    assert store.get_operation(operation.id)['result']['artifacts']==actual.artifacts


def test_artifact_projection_does_not_trust_foreign_or_forged_links(harness,tmp_path):
    client, store, _, identity, _=harness
    operation, actual=add_actual_artifact(store,identity)
    outside=tmp_path/'outside.txt';outside.write_text('owned negative fixture',encoding='utf-8')
    other=store.create_task('別タスク',[])
    foreign=Path(other['workspace'])/'other.txt';foreign.write_text('other fixture',encoding='utf-8')
    forged=[{'path':str(outside),'relative_path':'reports/結果 #1.txt','download_url':'https://outside.invalid/'},
            {'path':str(foreign)}, {'path':'../outside.txt'}, {'path':'C:outside.txt'},
            {'path':'missing.txt','download_url':'/api/settings'}]
    store.update_operation(operation.id,result=dict(actual.model_dump(),artifacts=forged))
    artifacts=client.get('/api/tasks/'+identity).json()['operations'][0]['result']['artifacts']
    assert all('relative_path' not in a and 'download_url' not in a for a in artifacts)
    assert store.get_operation(operation.id)['result']['artifacts']==forged
    assert client.get('/api/tasks/'+identity+'/artifacts/%2e%2e/outside.txt').status_code in (400,404)


def test_hardlinked_artifact_is_not_offered_or_downloaded(harness,tmp_path):
    client, store, _, identity, _=harness
    operation, actual=add_actual_artifact(store,identity)
    outside=tmp_path/'linked-source.txt';outside.write_text('owned negative fixture',encoding='utf-8')
    linked=Path(store.get_task(identity)['workspace'])/'linked.txt'
    try: os.link(outside,linked)
    except OSError: pytest.skip('Hard links unavailable on this filesystem; not observed')
    store.update_operation(operation.id,result=dict(actual.model_dump(),artifacts=[{'path':str(linked)}]))
    artifact=client.get('/api/tasks/'+identity).json()['operations'][0]['result']['artifacts'][0]
    assert 'download_url' not in artifact and 'relative_path' not in artifact
    assert client.get('/api/tasks/'+identity+'/artifacts/linked.txt').status_code==404


@pytest.mark.parametrize('explicit', [False, True])
def test_cli_instruction_uses_exact_hash_once_and_real_route(harness,monkeypatch,explicit):
    client, store, engine, identity, _=harness
    calls=[]
    class Bridge:
        def __init__(self,url): pass
        def get(self,path): calls.append(('get',path));return client.get(path).json()
        def post(self,path,body):
            calls.append(('post',path,body));response=client.post(path,json=body)
            if response.status_code>=400:raise ValueError(response.json()['detail'])
            return response.json()
        def close(self): calls.append(('close',))
    monkeypatch.setattr(cli,'Client',Bridge)
    monkeypatch.setattr(cli,'_print',lambda value:None)
    expected=store.get_task(identity)['source_hash']
    argv=['instruction',identity,ADDED]
    if explicit:argv+=['--expected-source-hash',expected]
    assert cli.main(argv)==0
    assert len([c for c in calls if c[0]=='get'])==int(not explicit)
    assert [c[2] for c in calls if c[0]=='post']==[{'text':ADDED,'expected_source_hash':expected}]
    assert engine.calls==[(identity,ADDED,expected)]


def test_cli_stale_hash_never_refetches_or_resends(harness,monkeypatch):
    client, _, engine, identity, _=harness
    calls=[]
    class Bridge:
        def __init__(self,url): pass
        def get(self,path):raise AssertionError('Explicit hash must not be replaced')
        def post(self,path,body):
            calls.append(body);response=client.post(path,json=body)
            raise ValueError(response.json()['detail'])
        def close(self):pass
    monkeypatch.setattr(cli,'Client',Bridge)
    assert cli.main(['instruction',identity,ADDED,'--expected-source-hash','0'*64])==1
    assert calls==[{'text':ADDED,'expected_source_hash':'0'*64}] and engine.calls==[]


NODE_DOM = r'''
const fs=require('fs'),vm=require('vm');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements=new Map();
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.value='';this.textContent='';this.hidden=false;this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;this.classList={add(){},toggle(){}};this.listeners={};}
 append(...items){this.children.push(...items);}prepend(...items){this.children.unshift(...items);}replaceChildren(...items){this.children=[...items];}
 replaceWith(item){if(this.id)elements.set(this.id,item);}addEventListener(name,cb){this.listeners[name]=cb;}setAttribute(){}focus(){}remove(){}close(){}showModal(){}
}
 const document={documentElement:new Element('html'),getElementById(id){if(!elements.has(id)){const e=new Element('div');e.id=id;elements.set(id,e);}return elements.get(id);},
 createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),createTextNode:text=>Object.assign(new Element('text'),{textContent:text}),querySelectorAll:()=>[]};
class FileReader {readAsDataURL(file){this.result='data:application/octet-stream;base64,'+input.base64;queueMicrotask(()=>this.onload());}}
const calls=[];const context={document,console,Map,Set,URL,Blob,FileReader,window:{addEventListener(){}},history:{replaceState(){}},location:{hash:'',pathname:'/'},EventSource:class{},
 fetch:async(path,options={})=>{calls.push({path,options});if(options.method==='POST'&&input.mode==='conflict')return {ok:false,status:409,json:async()=>({detail:'SOURCE_HASH_CONFLICT: fixture race'})};return {ok:true,status:200,json:async()=>input.accepted};}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(input.script),'i18n.js'),'utf8'),context);
for(const name of ['conversation.js','record-view.js'])vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(input.script),name),'utf8'),context);
let code=fs.readFileSync(input.script,'utf8');if(!code.match(/\nboot\(\);\s*$/))throw new Error('Boot boundary missing');
code=code.replace(/\nboot\(\);\s*$/,'\n');
vm.runInContext(code+'\nglobalThis.hooks={state,renderSnapshot,submitInstruction,submitAttachment,artifactLink,instructionDraft,attachmentDraft};',context);
(async()=>{
 const h=context.hooks;h.state.taskId=input.initial.task.id;h.state.tasks=[input.initial.task];h.state.csrf='fixture-csrf';h.renderSnapshot(input.initial);
 function descendants(el){return [el,...el.children.filter(x=>x instanceof Element).flatMap(descendants)];}
 const links=descendants(document.getElementById('operations')).filter(x=>x.tag==='a').map(x=>({href:x.href,text:x.textContent}));
 if(input.action==='attachment'){h.attachmentDraft(input.initial.task.id).file={name:input.filename,size:input.bytes};await h.submitAttachment({preventDefault(){}});}
 else {document.getElementById('instruction-text').value=input.text;await h.submitInstruction({preventDefault(){}});}
 const sources=descendants(document.getElementById('source-history')).filter(x=>x.className==='source-text').map(x=>x.textContent);
 const invalid=h.artifactLink(input.initial.task.id,{relative_path:'../outside.txt',download_url:'https://outside.invalid'});
 process.stdout.write(JSON.stringify({calls,links,sources,invalid:invalid===null,text:document.getElementById('instruction-text').value,
   filename:h.attachmentDraft(input.initial.task.id).file?.name||null,
   message:document.getElementById(input.action==='attachment'?'attachment-message':'instruction-message').textContent,taskId:h.state.taskId,sourceHash:h.state.snapshot.task.source_hash}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.parametrize('mode',['success','conflict'])
def test_actual_javascript_handlers_with_server_snapshot(harness,mode):
    node=shutil.which('node')
    if not node:pytest.skip('Node DOM fixture unavailable; live-browser behavior is separate')
    client, store, _, identity, _=harness
    add_actual_artifact(store,identity)
    initial=client.get('/api/tasks/'+identity).json()
    accepted=client.post('/api/tasks/'+identity+'/instructions',json={'text':ADDED,
        'expected_source_hash':initial['task']['source_hash']}).json()
    payload={'initial':initial,'accepted':accepted,'text':ADDED,'mode':mode,'script':str(server.STATIC/'app.js')}
    run=subprocess.run([node,'-e',NODE_DOM],input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert run.returncode==0,run.stderr
    actual=json.loads(run.stdout)
    posts=[c for c in actual['calls'] if c['options'].get('method')=='POST']
    assert len(posts)==1
    assert json.loads(posts[0]['options']['body'])=={'text':ADDED,'expected_source_hash':initial['task']['source_hash']}
    assert actual['taskId']==identity and actual['sources']==[INITIAL,ADDED]
    assert actual['sourceHash']==accepted['task']['source_hash']
    assert actual['invalid'] is True
    assert len(actual['links'])==1 and client.get(actual['links'][0]['href']).content=='実際の成果物\n'.encode('utf-8')
    assert actual['text']==(ADDED if mode=='conflict' else '')
    assert ('not resent' in actual['message'])==(mode=='conflict')
    assert len(actual['calls'])==(2 if mode=='conflict' else 1)
    page=client.get('/').text
    for identity in ['instruction-form','instruction-text','submit-instruction','instruction-message','source-history','source-count']:
        assert 'id="'+identity+'"' in page


@pytest.mark.parametrize('mode',['success','conflict'])
def test_attachment_javascript_exact_bytes_and_conflict_retention(harness,mode):
    node=shutil.which('node')
    if not node:pytest.skip('Node DOM fixture unavailable; live-browser behavior is separate')
    client,_,_,identity,_=harness
    initial=client.get('/api/tasks/'+identity).json();raw=b'\x00\xff\r\n'
    encoded=base64.b64encode(raw).decode('ascii')
    accepted=client.post('/api/tasks/'+identity+'/attachments',json={'filename':'資料 #1.bin','base64':encoded,
        'expected_source_hash':initial['task']['source_hash']}).json()
    payload={'initial':initial,'accepted':accepted,'mode':mode,'action':'attachment','filename':'資料 #1.bin',
             'bytes':len(raw),'base64':encoded,'script':str(server.STATIC/'app.js')}
    run=subprocess.run([node,'-e',NODE_DOM],input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert run.returncode==0,run.stderr
    actual=json.loads(run.stdout)
    posts=[c for c in actual['calls'] if c['options'].get('method')=='POST']
    assert len(posts)==1 and posts[0]['path']=='/api/tasks/'+identity+'/attachments'
    assert json.loads(posts[0]['options']['body'])=={'filename':'資料 #1.bin','base64':encoded,'expected_source_hash':initial['task']['source_hash']}
    assert actual['sources']==[INITIAL,'資料 #1.bin · 4 bytes']
    assert actual['filename']==('資料 #1.bin' if mode=='conflict' else None)
    assert actual['sourceHash']==accepted['task']['source_hash']
    assert len(actual['calls'])==(2 if mode=='conflict' else 1)
    assert ('not resent' in actual['message'])==(mode=='conflict')
