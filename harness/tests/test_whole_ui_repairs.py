"""Actual ASGI/full JavaScript consumers with explicit Engine, DOM and cipher fixtures.

No browser, paid provider, production state or real Windows credentials are used.
JS uses recorded ASGI responses; its exact settings POST is then consumed by the
same ASGI application. This is bounded consumer evidence, not live-browser proof.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
import policy_harness
from fastapi.testclient import TestClient

from policy_harness.models import ConfigurationRequired, Operation, OperationResult, PolicyError
from policy_harness.server import create_app
from policy_harness.settings import SettingsManager
from policy_harness.store import Store


MODEL_KEY = 'synthetic-model-credential-ui3'
WEB_KEY = 'synthetic-web-credential-ui3'
NEW_MODEL_KEY = 'synthetic-replacement-model-ui3'
NEW_WEB_KEY = 'synthetic-replacement-web-ui3'


class CipherFixture:
    """Reversible test encoding, explicitly not encryption or DPAPI evidence."""

    def __init__(self):
        self.unreadable = set()
        self.fail_protect = False

    def protect(self, raw):
        if self.fail_protect:
            raise ConfigurationRequired('Synthetic protection failure ' + NEW_MODEL_KEY)
        return b'fixture:' + bytes(value ^ 0xAA for value in raw)

    def unprotect(self, raw):
        if raw in self.unreadable:
            raise ConfigurationRequired('Synthetic unreadable value ' + MODEL_KEY)
        assert raw.startswith(b'fixture:')
        return bytes(value ^ 0xAA for value in raw[8:])


class EngineFixture:
    policy = None

    def __init__(self, store):
        self.store = store
        self.executor = self.gateway = self
        self.health_calls = 0
        self.snapshot_error = None

    def snapshot(self, task_id):
        if self.snapshot_error:
            raise PolicyError(self.snapshot_error)
        return {'task': self.store.get_task(task_id)}

    async def health(self):
        self.health_calls += 1
        return {'status': 'fixture', 'message': MODEL_KEY + ' ' + WEB_KEY}


@pytest.fixture
def harness(tmp_path):
    store = Store(tmp_path)
    cipher = CipherFixture()
    settings = SettingsManager(tmp_path, protector=cipher)
    settings.update({'model': 'fixture-model', 'model_api_key': MODEL_KEY, 'web_api_key': WEB_KEY})
    engine = EngineFixture(store)
    app = create_app(tmp_path, engine=engine, settings=settings, store=store)
    try:
        with TestClient(app, base_url='http://127.0.0.1:8765',
                        client=('127.0.0.1', 41235), raise_server_exceptions=False) as client:
            assert client.get('/').status_code == 200
            session = client.get('/api/session').json()
            client.headers['X-CSRF-Token'] = session['csrf_token']
            yield client, store, engine, settings, cipher
    finally:
        store.close()


def break_credentials(settings, cipher, names):
    document = json.loads(settings.path.read_text(encoding='utf-8'))
    for name in names:
        cipher.unreadable.add(base64.b64decode(document['secrets'][name]))


def record_artifact(store, task, name, body):
    path = Path(task['workspace']) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    operation = Operation(kind='file_write', args={'path': name}, purpose='Explicit ASGI artifact fixture',
                          expected_result='Exact bytes', decisions=[{
                              'id': 'fixture', 'statement': 'Record local fixture output',
                              'rationale': 'Bind the real download consumer'}])
    result = OperationResult(operation_id=operation.id, status='succeeded', effect='confirmed',
                             artifacts=[{'path': str(path), 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}])
    store.save_operation(task['id'], operation.model_dump())
    store.update_operation(operation.id, status='cycle_complete', result=result.model_dump())
    return operation.id


NODE_UI = r'''
const fs=require('fs'),vm=require('vm'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements=new Map(),calls=[],downloads=[],blobs=new Map(),revoked=[];let blobIndex=0;
let bootDone;const bootReady=new Promise(resolve=>bootDone=resolve);
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.value='';this.textContent='';this.hidden=false;this.listeners={};this.elements={};
 this.classList={toggle(){},add:(name)=>{if(this.id==='connection'&&['online','error'].includes(name))bootDone();}};}
 append(...items){this.children.push(...items);}prepend(...items){this.children.unshift(...items);}replaceChildren(...items){this.children=[...items];}
 replaceWith(item){if(this.id)elements.set(this.id,item);}addEventListener(name,fn){this.listeners[name]=fn;}
 setAttribute(){}focus(){}remove(){}showModal(){this.open=true;}close(){this.open=false;}
 querySelector(){return this.submit||(this.submit=new Element('button'));}
 click(){downloads.push({filename:this.download,href:this.href,blob:blobs.get(this.href)});}
}
const document={getElementById(id){if(!elements.has(id)){const el=new Element('div');el.id=id;elements.set(id,el);}return elements.get(id);},
 createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),createTextNode:text=>Object.assign(new Element('text'),{textContent:text}),querySelectorAll:()=>[],documentElement:new Element('html')};
for(const name of ['base_url','model','review_model','api_mode','reasoning_effort','model_context_tokens','max_output_tokens','web_provider','web_base_url','model_api_key','web_api_key'])document.getElementById('settings-form').elements[name]=new Element('input');
let deferExports=false;const pending=new Map();
const response=record=>({ok:record.status>=200&&record.status<300,status:record.status,json:async()=>record.body});
const context={document,console,Map,Set,Blob,window:{addEventListener(){}},history:{replaceState(){}},location:{hash:'',pathname:'/'},EventSource:class{},
 URL:{createObjectURL(blob){const id='blob:fixture-'+(++blobIndex);blobs.set(id,blob);return id;},revokeObjectURL(id){revoked.push(id);blobs.delete(id);}},
 fetch:async(path,options={})=>{calls.push({path,options});if(deferExports&&path.startsWith('/api/tasks/'))return await new Promise(resolve=>pending.set(path,resolve));
 const record=input.responses[(options.method||'GET')+' '+path];if(!record)throw new Error('Missing explicit ASGI response fixture '+path);return response(record);}};
vm.createContext(context);
// Use the same dependency/order as the production index, including Japanese.
vm.runInContext(fs.readFileSync(input.i18n,'utf8'),context);
for(const name of ['conversation.js','record-view.js'])vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(input.script),name),'utf8'),context);
// Execute the entire actual script, including its boot and event registration.
vm.runInContext(fs.readFileSync(input.script,'utf8')+'\nglobalThis.hooks={state,renderSnapshot,artifactLink};',context);
(async()=>{
 await bootReady;const h=context.hooks;
 if(input.mode==='artifacts'){
  h.state.taskId=input.snapshot.task.id;h.renderSnapshot(input.snapshot);
  function descendants(el){return [el,...el.children.filter(x=>x instanceof Element).flatMap(descendants)];}
  const links=descendants(document.getElementById('operations')).filter(x=>x.tag==='a').map(x=>({href:x.href,text:x.textContent}));
  const rejected=input.invalid.map(value=>h.artifactLink(input.snapshot.task.id,value)===null);
  const mainLinks=descendants(document.getElementById('task-artifacts')).filter(x=>x.tag==='a').map(x=>({href:x.href,text:x.textContent}));
  process.stdout.write(JSON.stringify({links,mainLinks,rejected,calls}));
 }else if(input.mode==='settings'){
  await document.getElementById('settings-open').onclick();
  const form=document.getElementById('settings-form'),dialog=document.getElementById('settings-dialog');
  const opened=dialog.open,message=document.getElementById('settings-error').textContent;
  const placeholders=Object.fromEntries(['model_api_key','web_api_key'].map(name=>[name,form.elements[name].placeholder]));
  const initialSecrets=Object.fromEntries(['model_api_key','web_api_key'].map(name=>[name,form.elements[name].value]));
  for(const [name,value] of Object.entries(input.replacements))form.elements[name].value=value;
  await form.listeners.submit({preventDefault(){},currentTarget:form});
  const cleared=['model_api_key','web_api_key'].every(name=>form.elements[name].value==='');
  process.stdout.write(JSON.stringify({opened,message,placeholders,initialSecrets,cleared,closed:!dialog.open,calls}));
 }else{
  const handler=document.getElementById('export-task').onclick;
  if(input.mode==='empty_export'){h.state.taskId=null;await handler();}
  else{
   h.state.taskId=input.first.task.id;await handler();
   h.state.taskId=input.second.task.id;
   if(input.mode==='exports'){
    await handler();
   }
  }
  const saved=downloads.map(item=>({filename:item.filename,href:item.href}));
  process.stdout.write(JSON.stringify({saved,calls,revoked,notice:document.getElementById('notice').textContent}));
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def node_ui(client, tmp_path, **values):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for this explicit full-script DOM fixture')
    routes = ['/api/session', '/api/tasks', '/api/settings', '/api/status']
    responses = {}
    for route in routes:
        response = client.get(route)
        responses['GET ' + route] = {'status': response.status_code, 'body': response.json()}
    # JS only consumes POST success/failure here. The exact captured payload is
    # passed to the same real ASGI application by the Python test below.
    responses['POST /api/settings'] = {'status': 200, 'body': {'fixture_transport_ack': True}}
    payload = {'script': str(Path(policy_harness.__file__).parent / 'static/app.js'),
               'i18n': str(Path(policy_harness.__file__).parent / 'static/i18n.js'),
               'responses': responses, **values}
    completed = subprocess.run([node, '-e', NODE_UI], input=json.dumps(payload, ensure_ascii=False),
                               capture_output=True, text=True, encoding='utf-8', timeout=15)
    (tmp_path / 'node.stdout').write_text(completed.stdout, encoding='utf-8')
    (tmp_path / 'node.stderr').write_text(completed.stderr, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def asgi_download(client, url):
    # This TestClient release unquotes httpx.URL.path a second time. ASGITransport
    # uses the already decoded path once, preserving literal names such as %2F.
    # Both call the same real app; application paths and links are not changed.
    async def receive():
        transport = httpx.ASGITransport(app=client.app, client=('127.0.0.1', 41236))
        # Starlette may use httpx2; preserve values, not its distinct Cookies type.
        async with httpx.AsyncClient(transport=transport, base_url=str(client.base_url), cookies=dict(client.cookies)) as direct:
            return await direct.get(url)
    return client.portal.call(receive)


def test_punctuation_unicode_percent_artifacts_reach_links_and_exact_download(harness, tmp_path):
    client, store, _, _, _ = harness
    task = store.create_task('Ordinary artifact names', [])
    names = ['plain.txt', 'nested/report (final).txt', "nested/report'name!.txt", 'nested/結果 #1% +.txt', 'literal %2F.txt']
    bodies = {}
    for index, name in enumerate(names):
        body = ('Actual fixture bytes ' + str(index)).encode()
        record_artifact(store, task, name, body)
        bodies[name] = body
    snapshot = client.get('/api/tasks/' + task['id']).json()
    original = snapshot['operations'][0]['result']['artifacts'][0]
    invalid = [dict(original, relative_path='../plain.txt'), dict(original, download_url='https://elsewhere.invalid'),
               dict(original, sha256='0' * 64), dict(original, download_operation_id='other'),
               dict(original, relative_path='/plain.txt'), dict(original, relative_path='a\\plain.txt')]
    observed = node_ui(client, tmp_path, mode='artifacts', snapshot=snapshot, invalid=invalid)
    assert all(observed['rejected'])
    assert len(observed['links']) == len(names)
    for link, row in zip(observed['links'], reversed(snapshot['operations'])):
        artifact = row['result']['artifacts'][0]
        assert link['href'] == artifact['download_url']
        response = asgi_download(client, link['href'])
        assert response.status_code == 200, (artifact['relative_path'], response.text)
        assert response.content == bodies[artifact['relative_path']]
        assert response.headers['x-artifact-operation'] == row['operation']['id']
        assert response.headers['x-artifact-sha256'] == hashlib.sha256(response.content).hexdigest()
    (Path(task['workspace']) / names[0]).write_bytes(b'new content')
    assert client.get(next(link['href'] for link in observed['links'] if link['text']==names[0])).status_code == 409
    assert client.get(observed['links'][1]['href'], headers={'Origin': 'https://elsewhere.invalid'}).status_code == 403


@pytest.mark.parametrize('names', [('model_api_key',), ('web_api_key',), ('model_api_key', 'web_api_key')])
def test_unreadable_credentials_open_settings_and_exact_ui_payload_replaces_them(harness, tmp_path, names):
    client, store, engine, settings, cipher = harness
    task = store.create_task('Raw record contains ' + MODEL_KEY, [])
    store.event(task['id'], 'fixture', 'failed', {'message': MODEL_KEY + ' ' + WEB_KEY})
    break_credentials(settings, cipher, names)
    before = settings.path.read_bytes()
    for route in ['/api/settings', '/api/status']:
        response = client.get(route)
        assert response.status_code == 200
        assert MODEL_KEY not in response.text and WEB_KEY not in response.text
    assert engine.health_calls == 0
    for route in ['/api/tasks', '/api/tasks/' + task['id'], '/api/tasks/' + task['id'] + '/events']:
        response = client.get(route)
        assert response.status_code == 409
        assert MODEL_KEY not in response.text and WEB_KEY not in response.text
    engine.snapshot_error = 'A failed caller containing ' + MODEL_KEY
    response = client.get('/api/tasks/' + task['id'])
    assert response.status_code == 409 and MODEL_KEY not in response.text
    assert settings.path.read_bytes() == before
    replacements = {name: NEW_MODEL_KEY if name == 'model_api_key' else NEW_WEB_KEY for name in names}
    observed = node_ui(client, tmp_path, mode='settings', replacements=replacements)
    assert observed['opened'] and observed['cleared'] and observed['closed']
    assert all(not value for value in observed['initialSecrets'].values())
    assert 'Enter them again' in observed['message']
    assert all('Enter' in observed['placeholders'][name] for name in names)
    posts = [call for call in observed['calls'] if call['options'].get('method') == 'POST']
    assert len(posts) == 1 and posts[0]['path'] == '/api/settings'
    payload = json.loads(posts[0]['options']['body'])
    assert {name: payload[name] for name in names} == replacements
    response = client.post(posts[0]['path'], json=payload)
    assert response.status_code == 200
    assert all(value not in response.text and value.encode() not in settings.path.read_bytes() for value in replacements.values())
    assert all(response.json()['credential_status']['model' if name == 'model_api_key' else 'web'] == 'available' for name in names)
    assert all(settings.secret(name) == value for name, value in replacements.items())
    recovered = SettingsManager(settings.data_dir, protector=cipher)
    assert all(recovered.secret(name) == value for name, value in replacements.items())


def test_main_settings_can_switch_from_oauth_to_another_api_provider(harness, tmp_path):
    client, _, _, settings, _ = harness
    settings.update({'base_url': 'https://openrouter.ai/api/v1',
                     'model_api_key': 'fixture-openrouter-test', 'auth_method': 'openrouter_oauth'})
    replacements = {'base_url': 'https://example.com/v1', 'model_api_key': 'fixture-other-provider'}
    observed = node_ui(client, tmp_path, mode='settings', replacements=replacements)
    post = next(c for c in observed['calls'] if c['options'].get('method') == 'POST')
    payload = json.loads(post['options']['body'])
    assert payload['auth_method'] == 'api_key'
    response = client.post(post['path'], json=payload)
    assert response.status_code == 200, response.text
    assert settings.get()['auth_method'] == 'api_key'
    assert settings.secret('model_api_key') == replacements['model_api_key']


def test_partial_replacement_preserves_unreadable_other_key_and_failed_write(harness):
    client, store, _, settings, cipher = harness
    task = store.create_task('Do not expose arbitrary task data', [])
    break_credentials(settings, cipher, ('model_api_key', 'web_api_key'))
    before = settings.path.read_bytes()
    cipher.fail_protect = True
    response = client.post('/api/settings', json={'model_api_key': NEW_MODEL_KEY})
    assert response.status_code == 409 and NEW_MODEL_KEY not in response.text
    assert settings.path.read_bytes() == before
    cipher.fail_protect = False
    response = client.post('/api/settings', json={'model_api_key': NEW_MODEL_KEY})
    assert response.status_code == 200
    assert response.json()['credential_status'] == {'model': 'available', 'web': 'unavailable'}
    assert client.get('/api/tasks/' + task['id']).status_code == 409
    assert client.get('/api/settings').status_code == 200
    invalid = client.post('/api/settings', json={'not_a_setting': NEW_MODEL_KEY})
    assert invalid.status_code == 422 and NEW_MODEL_KEY not in invalid.text


def test_normal_credential_redaction_and_settings_auth_guards(harness):
    client, store, engine, settings, _ = harness
    task = store.create_task('Known credential ' + MODEL_KEY, [])
    response = client.get('/api/tasks/' + task['id'])
    assert response.status_code == 200 and MODEL_KEY not in response.text
    response = client.get('/api/status')
    assert response.status_code == 200 and engine.health_calls == 2
    assert MODEL_KEY not in response.text and WEB_KEY not in response.text
    before = settings.path.read_bytes()
    for headers in [{'X-CSRF-Token': ''}, {'Origin': 'https://elsewhere.invalid'}]:
        assert client.post('/api/settings', headers=headers, json={'model_api_key': NEW_MODEL_KEY}).status_code == 403
    assert settings.path.read_bytes() == before


@pytest.mark.parametrize('mode', ['exports', 'selection_changed', 'empty_export'])
def test_export_click_identity_survives_selection_and_response_reordering(harness, tmp_path, mode):
    client, store, _, _, _ = harness
    first = store.create_task('First selected task', [])
    second = store.create_task('Second selected task', [])
    snapshots = [client.get('/api/tasks/' + task['id']).json() for task in (first, second)]
    observed = node_ui(client, tmp_path, mode=mode, first=snapshots[0], second=snapshots[1])
    expected=[first['id'],second['id']] if mode=='exports' else [first['id']] if mode=='selection_changed' else []
    assert [item['href'] for item in observed['saved']]==['/api/tasks/'+identity+'/records?download=true' for identity in expected]
    for item,identity in zip(observed['saved'],expected):
        response=client.get(item['href'])
        assert response.status_code==200 and response.json()['task']['id']==identity
        assert response.headers['content-disposition'].startswith('attachment;')
    assert observed['revoked']==[]
    assert not any(call['path'].startswith('/api/tasks/') for call in observed['calls'])
