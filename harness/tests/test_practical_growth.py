import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from policy_harness.practical_models import PracticalStep
from policy_harness.provider_login import ProviderLogin
from policy_harness.settings import SettingsManager, SettingsError
from policy_harness.store import Store, canonical
from tests.test_practical_runtime import runtime, steps, tool, finish, run
from tests.test_usability_learning import lesson
from tests.test_source_input_ui import harness


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['general', 'folder'])
async def test_domain_vocabulary_can_be_shared_without_a_word_allowlist(tmp_path, scope):
    from policy_harness.organization import Organization
    note = lesson(share_scope=scope, sharing_reason='CSV構造の一般的な確認方法。個別の値は含めない')
    note.update(title='CSVのheaderと列名を確認', applies_when='CSVのcolumnsを検証するとき',
                procedure='headerと列名、columnsの数を比較してからCSVを書き出す')
    done = finish('answer.txt');done['learning'] = [note]
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='ok')), done])
    folder = Organization(store).create_folder('CSV project')
    Organization(store).update(task['id'], {'folder_id':folder['id']}, 0)
    store.update_task(task['id'], objective='CSVのheader、columnsと列名を確認')
    await run(engine,store,task)
    learned = store.records('practical_skill')[0]
    assert learned['scope'] == ('general' if scope == 'general' else 'folder:'+folder['id'])
    other = store.create_task('CSV列名を検証', [], options={'folder_id':folder['id']})
    assert engine.learning.available(other,query='CSV')[0]['id'] == learned['id']
    assert len(gateway.calls) == 2
    store.close()


@pytest.mark.asyncio
async def test_default_local_stays_private_even_inside_a_folder(tmp_path):
    from policy_harness.organization import Organization
    done = finish('answer.txt');done['learning'] = [lesson()]
    engine,store,_,_,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),done])
    folder=Organization(store).create_folder('Project')
    Organization(store).update(task['id'],{'folder_id':folder['id']},0)
    await run(engine,store,task)
    assert store.records('practical_skill')[0]['scope']=='task:'+task['id']
    other=store.create_task('Another text file',[],options={'folder_id':folder['id']})
    assert engine.learning.available(other)==[]
    # Historical implicit folder records stay retained but are not shared.
    old=dict(store.records('practical_skill')[0],id='old',scope='folder:'+folder['id'])
    store.record('practical_skill','old',old)
    assert engine.learning.available(other)==[]
    assert any(s['id']=='old' for s in engine.learning.available(task))
    store.close()


@pytest.mark.asyncio
async def test_alternating_format_errors_hold_until_real_progress(tmp_path):
    from policy_harness.practical_engine import Held
    engine,store,_,_,task=runtime(tmp_path,[])
    engine._repair_feedback(task,'next_action',[{'type':'missing','loc':['tools',0,'purpose']}])
    engine._repair_feedback(task,'next_action',[{'type':'string_type','loc':['message']}])
    with pytest.raises(Held,match='進展がない'):
        engine._repair_feedback(task,'next_action',[{'type':'missing','loc':['tools',3,'purpose']}])
    # A successful real file operation gives the next response a fresh frontier.
    from policy_harness.practical_models import ToolRequest
    result=await engine._perform(task,ToolRequest.model_validate(tool('file_write',path='answer.txt',text='hello')),'write-progress')
    assert result.status=='succeeded'
    engine._repair_feedback(store.get_task(task['id']),'next_action',[{'type':'missing','loc':['tools',0,'purpose']}])
    assert len(store.records('practical_response_repair'))==3
    store.close()


@pytest.mark.asyncio
async def test_json_position_changes_do_not_buy_endless_retries(tmp_path):
    from policy_harness.practical_engine import Held
    engine,store,_,_,task=runtime(tmp_path,[])
    def diagnostic(position):return {'sanitized_response':json.dumps([{'type':'json_decode','message':'Expecting delimiter','line':1,'column':position,'position':position}])}
    engine._repair_feedback(task,'next_action',diagnostic(100))
    with pytest.raises(Held):engine._repair_feedback(task,'next_action',diagnostic(200))
    updated=store.append_instruction(task['id'],'Use the same output with this clarified instruction',task['source_hash'])
    engine._repair_feedback(updated,'next_action',diagnostic(200))
    store.close()


@pytest.mark.asyncio
async def test_one_malformed_response_can_recover_and_complete(tmp_path):
    from policy_harness.providers import ProviderError
    async def invalid(payload):
        raise ProviderError('Invalid model JSON', metadata={'schema_validation_failed':True,
            'validation_diagnostic':[{'type':'json_decode','message':'Expecting delimiter','column':99}]})
    engine,store,gateway,_,task=runtime(tmp_path,[invalid,steps(tool('file_write',path='answer.txt',text='hello')),finish('answer.txt')])
    result=await run(engine,store,task)
    assert result['status']=='completed' and len(gateway.calls)==3
    assert store.records('practical_call')[0]['status']=='failed'
    assert len(store.records('practical_response_repair'))==1
    store.close()


@pytest.mark.asyncio
async def test_parent_rejects_unsupported_review_without_another_round(tmp_path):
    engine,store,gateway,_,task=runtime(tmp_path,[
        steps(tool('file_write',path='answer.py',text='print(1)')),finish('answer.py'),
        {'verdict':'revise','findings':['Also create a spreadsheet'],'rationale':'Suggested extra deliverable'},
        {'findings':[{'finding':0,'decision':'reject','reason':'The source only requests this script; no spreadsheet is requested.'}],'rationale':'The requested script and byte evidence satisfy the current source.'}])
    result=await run(engine,store,task)
    assert result['status']=='completed'
    assert result['final']['review']['verdict']=='revise'
    assert result['final']['review_disposition']['proceed']
    assert len(gateway.calls)==4 and len(store.records('practical_review_disposition'))==1
    store.close()


def test_task_metadata_paging_does_not_decode_task_bodies(tmp_path,monkeypatch):
    from policy_harness.organization import Organization
    store=Store(tmp_path);org=Organization(store);folder=org.create_folder('group')
    tasks=[store.create_task('title '+str(i),[]) for i in range(125)]
    for task in tasks[::10]:store.update_task(task['id'],state={'history':'large'*80000})
    org.update(tasks[0]['id'],{'pinned':True},0)
    org.update(tasks[1]['id'],{'archived':True},0)
    org.update(tasks[2]['id'],{'folder_id':folder['id']},0)
    monkeypatch.setattr(store,'list_tasks',lambda: (_ for _ in ()).throw(AssertionError('No full task list')))
    rows=[];cursor=''
    while True:
        page=store.task_page(cursor=cursor,limit=17);rows+=page['tasks'];cursor=page['next_cursor']
        assert len(page['tasks'])<=17 and len(json.dumps(page))<30000
        if not cursor:break
    assert rows[0]['id']==tasks[0]['id'] and len(rows)==124 and len({x['id'] for x in rows})==124
    assert all('state' not in x and 'source_history' not in x and 'final' not in x for x in rows)
    assert store.task_page(view='archive')['tasks'][0]['id']==tasks[1]['id']
    assert store.task_page(folder=folder['id'])['tasks'][0]['id']==tasks[2]['id']
    store.update_task(tasks[0]['id'],status='running')
    assert store.active_task_ids()==[tasks[0]['id']]
    with sqlite3.connect(store.path) as db:
        saved=store.get_task(tasks[0]['id']);saved['status']='completed'
        db.execute('UPDATE tasks SET body=? WHERE id=?',(canonical(saved),saved['id']))
    assert store.active_task_ids()==[]
    assert store.task_page()['tasks'][0]['status']=='completed'
    store.close()


def test_task_list_http_redacts_before_truncation_and_avoids_full_state(harness,monkeypatch):
    client,store,_,identity,app=harness
    secret='opaque-configured-private-credential'
    app.state.settings.secret=lambda name: secret if name=='model_api_key' else None
    store.update_task(identity,objective='x'*630+secret+'tail'*5000,state={'large':'history'*100000})
    monkeypatch.setattr(store,'list_tasks',lambda: (_ for _ in ()).throw(AssertionError('No full list')))
    response=client.get('/api/tasks');assert response.status_code==200
    row=response.json()['tasks'][0]
    assert len(row['objective'])<=640 and 'opaque' not in row['objective']
    assert len(response.content)<5000 and 'state' not in row
    assert client.get('/api/status').status_code==200


@pytest.mark.asyncio
async def test_background_task_notifies_list_without_progress_polling(tmp_path):
    import asyncio
    store=Store(tmp_path);task=store.create_task('background',[])
    stream=store.subscribe_task_list();first=await anext(stream)
    pending=asyncio.create_task(anext(stream));await asyncio.sleep(0)
    store.update_task(task['id'],status='completed')
    assert await asyncio.wait_for(pending,1)>first
    await stream.aclose();assert not store.task_list_listeners
    store.close()


@pytest.mark.asyncio
async def test_japanese_general_procedure_reaches_real_new_task(tmp_path):
    note = lesson(share_scope='general', sharing_reason='個別のファイル名や内容を含まない保存確認の手順')
    note.update(title='保存直後の読み戻し結果を確認する', applies_when='短いテキストを保存して内容を確認するとき',
        procedure='file_write の data.text_facts.scope が whole_file か確認し、文字数と改行の有無を指定内容と比較する。既に返された全体の結果で確認できる場合は、重複する読み戻しを避ける。',
        limits='部分的な範囲の結果だけでは全体を確認できない。', next_trigger='次に短いテキストを保存するとき')
    done = finish('answer.txt'); done['learning'] = [note]
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), done])
    store.update_task(task['id'], objective='テキストを保存して内容を確認してください')
    await run(engine, store, task)
    skill = store.records('practical_skill')[0]
    assert skill['scope'] == 'general', store.records('practical_sharing_review')
    other = store.create_task('短いテキストを保存し、改行の有無を確認してください', ['Save exact text'])
    offered = engine.learning.context(other)['skills']
    assert offered[0]['id'] == skill['id']
    assert not any(k in offered[0] for k in ('source_hash', 'evidence', 'owner_task_id'))
    action = steps(tool('file_write', path='new.txt', text='bye'))
    action['skill_uses'] = [{'skill': 0, 'tool_index': 0, 'adaptation': '全体の読み戻し事実を使い重複操作を省く'}]
    complete = finish('new.txt')
    complete['learning_assessments'] = [{'operation_index': 0, 'judgment': 'helpful', 'reason': '1回の保存で文字数と改行の有無を確認できた'}]
    gateway.replies += [action, complete]
    await engine.start_task(other['id'])
    assert store.get_task(other['id'])['status'] == 'completed'
    assert Path(other['workspace'], 'new.txt').read_bytes() == b'bye'
    assert len(store.operations(other['id'])) == 1
    assert store.record_get('practical_skill', skill['id'])['use_outcomes']['helpful'] == 1
    store.close()


def skill(identity, title, **changes):
    return dict(id=identity, revision=1, title=title, scope='general', owner_task_id='original',
        applies_when=title, procedure=title, limits='Use only when applicable', status='provisional',
        next_trigger=title, tools=[], updated_at=identity, touched_tasks=[], evidence=[], **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope',['local','general'])
async def test_merge_retains_shared_sources_when_replacement_is_private(tmp_path,scope):
    from policy_harness.practical_models import ToolRequest
    engine,store,_,_,task=runtime(tmp_path,[])
    await engine._perform(task,ToolRequest.model_validate(tool('file_write',path='sample.txt',text='ok')),'merge-evidence')
    originals=[skill('shared-'+str(i),'Reusable text verification') for i in range(2)]
    for row in originals:store.record('practical_skill',row['id'],row)
    catalog=[engine.learning.public_lesson(row) for row in originals]
    note=lesson(share_scope=scope,sharing_reason='Generic byte verification without case-specific values')
    note.update(action='merge',skill=0,merge_skills=[1],title='Combined verification procedure')
    engine.learning.apply(task,PracticalStep.model_validate(dict(finish(),learning=[note])),'merge-decision',catalog)
    other=store.create_task('Next text verification',[])
    visible={row['id'] for row in engine.learning.available(other,query='verification')}
    if scope=='local':
        assert visible=={row['id'] for row in originals}
        assert all(store.record_get('practical_skill',row['id'])==row for row in originals)
        local=[row for row in store.records('practical_skill') if row['scope']=='task:'+task['id']]
        assert len(local)==1 and local[0]['title']==note['title']
    else:
        assert visible=={'shared-0'}
        assert store.record_get('practical_skill','shared-0')['revision']==2
        assert store.record_get('practical_skill','shared-1')['merged_into']=='shared-0'
    store.close()


@pytest.mark.parametrize('query',['CSV','改行',''])
def test_legacy_private_folder_rows_never_fill_shared_search_slots(tmp_path,query):
    from policy_harness.organization import Organization
    from policy_harness.practical_learning import PracticalLearning
    store=Store(tmp_path);folder=Organization(store).create_folder('Shared project')
    owner=store.create_task('Legacy owner',[],options={'folder_id':folder['id']})
    other=store.create_task('CSV 改行',[],options={'folder_id':folder['id']})
    originals=[]
    for i in range(40):
        row=dict(skill('legacy-'+str(i),'CSV headers 改行'),scope='folder:'+folder['id'],owner_task_id=owner['id'],updated_at='9'+str(i))
        originals.append(row);store.record('practical_skill',row['id'],row)
    general=skill('valid-general','CSV headers 改行')
    shared=dict(skill('valid-folder','CSV headers 改行'),scope='folder:'+folder['id'],sharing_reason='Generic format procedure',updated_at='0')
    for row in (general,shared):store.record('practical_skill',row['id'],row)
    # Simulate the previous derived index; reopening migrates it without
    # rewriting any original lesson or its history.
    store.record('controller_schema','skill_search',{'version':1})
    with store.lock:store.db.execute("UPDATE skill_search SET scope=? WHERE owner=?",('folder:'+folder['id'],owner['id']));store.db.commit()
    store.close();store=Store(tmp_path)
    visible=PracticalLearning(store).available(other,query=query)
    assert {row['id'] for row in visible}=={'valid-general','valid-folder'}
    assert any(row['id'].startswith('legacy-') for row in PracticalLearning(store).available(owner,query=query))
    assert all(store.record_get('practical_skill',row['id'])==row for row in originals)
    assert store.record_get('controller_schema','skill_search')=={'version':2}
    store.close()


def test_old_relevant_knowledge_survives_large_catalog_and_connections(tmp_path):
    store = Store(tmp_path)
    target = skill('00001', '日本語の改行数を保存直後に確認する')
    store.record('practical_skill', target['id'], target)
    for n in range(1000):
        row = skill(str(n + 10000), 'Unrelated geometric triangles and astronomy')
        store.record('practical_skill', row['id'], row)
    store.record('practical_skill', 'private', dict(target, id='private', scope='task:someone-else'))
    other = Store(tmp_path)
    result = other.available_skills('new-task', 'task:new-task', query='日本語の改行数を確認')
    assert [r['id'] for r in result] == ['00001']
    # Another connection's update and a rolled-back retirement maintain the index.
    def fail():
        store.record('practical_skill', '00001', dict(target, status='retired'))
        raise RuntimeError('rollback')
    with pytest.raises(RuntimeError):store._transaction(fail)
    assert other.available_skills('new-task', 'task:new-task', query='日本語の改行数')
    store.record('practical_skill', '00001', dict(target, status='retired'))
    assert other.available_skills('new-task', 'task:new-task', query='日本語の改行数') == []
    store.close();other.close()


def test_current_revision_harm_is_visible_and_downranked(tmp_path):
    engine, store, _, _, task = runtime(tmp_path, [])
    task = store.create_task('Write exact text file', ['Requested text exists'])
    store.record('practical_skill', 'bad', skill('bad', 'Write exact text file', use_outcomes={'harmful': 1}))
    store.record('practical_skill', 'good', skill('good', 'Write exact text file', use_outcomes={'helpful': 1}))
    values = engine.learning.context(task)['skills']
    assert [s['id'] for s in values][:2] == ['good', 'bad']
    assert values[1]['use_outcomes']['harmful'] == 1
    store.close()


def test_two_character_shared_query_keeps_scope_retirement_and_bounds(tmp_path):
    store=Store(tmp_path)
    for index in range(60):
        row=skill('general-'+str(index),'保存後に改行の有無を確認する')
        store.record('practical_skill',row['id'],row)
    original=skill('private','別の作業の改行を確認する')
    store.record('practical_skill','private',dict(original,scope='task:other'))
    store.record('practical_skill','retired',dict(original,id='retired',status='retired'))
    result=store.available_skills('new','task:new',query='改行')
    assert len(result)==32 and all(row['id'].startswith('general-') for row in result)
    assert store.available_skills('new','task:new',query='不存在 " OR *')==[]
    assert len(store.available_skills('new','task:new',query='改行 " OR *'))==32
    store.close()


class Cipher:
    # Tests transport/state independently; native DPAPI has its own real test.
    def protect(self, value):return b'enc:' + value
    def unprotect(self, value):return value[4:]


def test_profiles_keep_distinct_credentials_and_web_settings(tmp_path):
    manager = SettingsManager(tmp_path, protector=Cipher())
    manager.update({'base_url': 'https://first.example/v1', 'model': 'first', 'model_api_key': 'FIRST_PRIVATE', 'web_provider': 'public_url'})
    first = manager.save_profile('First')
    manager.update({'base_url': 'https://second.example/v1', 'model': 'second', 'model_api_key': 'SECOND_PRIVATE'})
    # Preparing a new connection does not overwrite the old named credentials.
    second = manager.save_profile('Second', values={'base_url': 'https://third.example/v1', 'model': 'third'}, credential='THIRD_PRIVATE')
    manager.activate_profile(second)
    assert manager.secret('model_api_key') == 'THIRD_PRIVATE'
    manager.activate_profile(first)
    assert manager.secret('model_api_key') == 'FIRST_PRIVATE'
    assert manager.public()['web_provider'] == 'public_url'
    assert not any(k in canonical(manager.profiles()) for k in ('FIRST_PRIVATE', 'SECOND_PRIVATE', 'THIRD_PRIVATE'))
    manager.delete_profile(second)
    assert set(manager.redaction_secrets()[0]) == {'FIRST_PRIVATE', 'SECOND_PRIVATE', 'THIRD_PRIVATE'}
    assert manager.secret('model_api_key') == 'FIRST_PRIVATE'
    before = manager.path.read_bytes()
    with pytest.raises(SettingsError):manager.update({'base_url': 'https://unexpected.example/v1'})
    assert manager.path.read_bytes() == before


@pytest.mark.asyncio
async def test_pkce_single_use_bound_exchange_preserves_current_provider(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    import base64, hashlib
    manager = SettingsManager(tmp_path, protector=Cipher())
    manager.update({'base_url': 'https://current.example/v1', 'model': 'current', 'model_api_key': 'CURRENT_PRIVATE'})
    requests = []
    def exchange(request):
        assert str(request.url) == 'https://openrouter.ai/api/v1/auth/keys'
        body = json.loads(request.content)
        assert body['code'] == 'ONE_TIME_CODE' and body['code_challenge_method'] == 'S256'
        assert base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest()).rstrip(b'=').decode() == challenge
        requests.append(body)
        return httpx.Response(200, json={'key': 'NEW_OAUTH_PRIVATE'})
    login = ProviderLogin(manager, transport=httpx.MockTransport(exchange))
    flow = parse_qs(urlsplit(login.begin('http://127.0.0.1:8765')['url']).query)
    challenge = flow['code_challenge'][0]
    state = flow['callback_url'][0].rsplit('/', 1)[1]
    identity = await login.finish(state, 'ONE_TIME_CODE')
    assert manager.secret('model_api_key') == 'CURRENT_PRIVATE'
    assert manager.public()['model'] == 'current'
    assert len(requests) == 1
    with pytest.raises(SettingsError):await login.finish(state, 'ONE_TIME_CODE')
    assert len(requests) == 1
    manager.activate_profile(identity)
    assert manager.secret('model_api_key') == 'NEW_OAUTH_PRIVATE'
    assert manager.public()['auth_method'] == 'openrouter_oauth'


def test_profile_http_csrf_callback_and_running_provider_boundary(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_server import Engine
    from policy_harness.server import create_app
    manager=SettingsManager(tmp_path,protector=Cipher())
    manager.update({'model':'original','model_api_key':'ORIGINAL_PRIVATE'})
    store=Store(tmp_path);engine=Engine(store)
    with TestClient(create_app(tmp_path,settings=manager,store=store,engine=engine),base_url='http://127.0.0.1',client=('127.0.0.1',50000)) as client:
        client.get('/');token=client.get('/api/session').json()['csrf_token']
        path='/api/settings/profiles'
        assert client.post(path,json={'action':'save','name':'first'}).status_code==403
        client.headers['x-csrf-token']=token
        saved=client.post(path,json={'action':'save','name':'first'})
        assert saved.status_code==200 and 'ORIGINAL_PRIVATE' not in saved.text
        identity=saved.json()['active']
        task=store.create_task('Keep current connection',[]);store.update_task(task['id'],status='running')
        assert client.post(path,json={'action':'activate','id':identity}).status_code==409
        assert manager.public()['model']=='original'
        assert client.post('/api/settings/login/openrouter',json={},headers={'origin':'https://foreign.example'}).status_code==403
        # Cross-site navigation is admitted only for the one-use PKCE callback.
        response=client.get('/oauth/openrouter/invalid?code=PRIVATE_CODE',headers={'sec-fetch-site':'cross-site'})
        assert response.status_code==400 and 'PRIVATE_CODE' not in response.text
        assert response.headers['referrer-policy']=='no-referrer'
    store.close()


@pytest.mark.asyncio
async def test_expired_pkce_does_not_exchange_or_change_settings(tmp_path):
    from urllib.parse import parse_qs,urlsplit
    manager=SettingsManager(tmp_path,protector=Cipher());manager.update({'model':'original'})
    before=manager.path.read_bytes()
    def unexpected(request):raise AssertionError('Expired login must not reach provider')
    login=ProviderLogin(manager,transport=httpx.MockTransport(unexpected))
    state=parse_qs(urlsplit(login.begin('http://127.0.0.1:8765')['url']).query)['callback_url'][0].rsplit('/',1)[-1]
    login.pending[state]['expires']=0
    with pytest.raises(SettingsError):await login.finish(state,'CODE')
    assert manager.path.read_bytes()==before


@pytest.mark.asyncio
async def test_long_task_stop_and_display_keep_large_records_out_of_working_memory(tmp_path,monkeypatch):
    from policy_harness.record_reader import snapshot_reader
    engine,store,_,_,task=runtime(tmp_path,[])
    for index in range(120):
        store.record('practical_idea',str(index),{'id':str(index),'task_id':task['id'],'proposal':'Retained result '+str(index)})
        store.record('practical_call',str(index),{'id':str(index),'task_id':task['id'],'phase':'next_action','elapsed_seconds':.5,'response':{'message':'x'*16000}})
    # Stop and metrics must not call the APIs that materialize full histories.
    monkeypatch.setattr(store,'operations',lambda *a: (_ for _ in ()).throw(AssertionError('full operations loaded')))
    monkeypatch.setattr(store,'task_records',lambda *a,**k: (_ for _ in ()).throw(AssertionError('full call history loaded')))
    receipt=await engine.stop_task(task['id'])
    assert receipt['task']['status']=='stopped' and receipt['refresh_conversation']
    assert store.practical_metrics(task['id'])['model_calls']==120
    with snapshot_reader(store,engine,task['id'],display=True) as (reader,view):
        visible=view.snapshot(task['id'])
        assert len(visible['knowledge']['ideas'])==80 and reader.page['knowledge_more']
    assert store.db.execute("SELECT count(*) FROM records WHERE kind='practical_idea'").fetchone()[0]==120
    with snapshot_reader(store,engine,task['id']) as (reader,view):
        reader.metadata_only=True
        assert view.snapshot(task['id'])['knowledge']['ideas']==[]
        reader.metadata_only=False
        records=reader.knowledge_records(view)['ideas']
        assert not isinstance(records,list)
        assert len(list(records))==120
    store.close()
def test_pending_learning_is_not_hidden_by_newer_assessed_work(tmp_path):
    from policy_harness.store import Store
    store = Store(tmp_path / 'pending-learning')
    for index in range(30):
        common = {'id': str(index), 'task_id': 'same', 'status': 'result_observed'}
        store.record('practical_skill_use', str(index), dict(common, assessment=None if index < 12 else {'judgment': 'helpful'}))
        store.record('maintenance_ordinary_use', str(index), dict(common, assessment_call=None if index < 12 else 'already-reviewed'))
    assert [r['id'] for r in store.pending_skill_uses('same')] == list(map(str, range(8)))
    assert [r['id'] for r in store.pending_update_uses('same')] == list(map(str, range(8)))
    for row in store.pending_update_uses('same'):
        store.record('maintenance_ordinary_use', row['id'], dict(row, assessment_call='next-review'))
    assert [r['id'] for r in store.pending_update_uses('same')] == list(map(str, range(8, 12)))
    assert store.pending_update_uses('another-task') == []
    store.close()
