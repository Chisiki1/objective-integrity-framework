"""HTTP boundary construction checks. Model and executor fixtures are not live evidence."""
from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from policy_harness.models import PolicyError
from policy_harness.server import create_app
from policy_harness.store import Store


SECRET = 'fixture-only-private-credential-43972'


class Settings:
    def __init__(self):
        self.values = {'model': 'fixture', 'model_api_key_configured': True}
        self.updated = []

    def public(self):
        return self.values.copy()

    def secret(self, name):
        return SECRET if name == 'model_api_key' else None

    def update(self, values):
        self.updated.append(values.copy())
        if 'bad' in values:
            raise ValueError('invalid '+SECRET)
        self.values.update({k:v for k,v in values.items() if not k.endswith('_key')})
        return self.public()


class Policy:
    hash = 'fixture-policy-hash'

    def summary(self):
        return {'conditions': 236, 'rules': 17}


class Health:
    async def health(self):
        return {'ready': False, 'preparation': 'not-observed'}


class Engine:
    policy = Policy()
    executor = Health()
    gateway = Health()

    def __init__(self, store):
        self.store = store
        self.calls = []

    def start_task(self, task_id):
        self.calls.append(('start', task_id))
        self.store.update_task(task_id, status='running')

    def resume_task(self, task_id):
        self.calls.append(('resume', task_id))
        self.store.update_task(task_id, status='running')

    async def stop_task(self, task_id):
        self.calls.append(('stop', task_id))
        self.store.update_task(task_id, status='stopped', state={'unknown_effect':'fixture unknown remains'})
        return self.snapshot(task_id)

    def snapshot(self, task_id):
        return {'task':self.store.get_task(task_id), 'knowledge':{'ideas':[], 'skills':[]},
                'readiness':{'status':'not-observed'}}

    def pending_approvals(self):
        return [{'id':'amend-1','task_id':'fixture','proposal_hash':'exact-hash','old':'old','new':'new','status':'awaiting_user'}]

    def resolve_approval(self, identity, *, decision, expected_hash, reason):
        if identity != 'amend-1' or expected_hash != 'exact-hash':
            raise PolicyError('Approval target changed')
        self.calls.append(('approval',identity,decision,expected_hash,reason))
        return {'id':identity,'status':decision}


@pytest.fixture
def harness(tmp_path):
    store = Store(tmp_path)
    settings = Settings()
    engine = Engine(store)
    app = create_app(tmp_path,engine=engine,settings=settings,store=store)
    with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',41234)) as client:
        assert client.get('/').status_code == 200
        token = client.get('/api/session').json()['csrf_token']
        client.headers['X-CSRF-Token'] = token
        yield client, store, engine, settings, app
    store.close()


def test_browser_entry_and_task_flow(harness):
    client,store,engine,_,_ = harness
    assert '何を完成させたいですか' in client.get('/').text
    assert 'setInterval' not in client.get('/static/app.js').text
    result = client.post('/api/tasks',json={'objective':'日本語の成果物を作る','acceptance':[' 条件を維持する ']})
    assert result.status_code == 201
    task = result.json()['task']
    assert task['status'] == 'running'
    assert task['acceptance'] == [' 条件を維持する ']
    source = store.get_task(task['id'])['source_history'][0]
    raw, source_hash = store.source_bytes(task['id'], source['id'], source['sha256'])
    assert source_hash == source['sha256'] == hashlib.sha256(raw).hexdigest()
    original = json.loads(raw)
    assert original == {'objective':'日本語の成果物を作る','acceptance':[' 条件を維持する ']}
    snapshot = client.get('/api/tasks/'+task['id']).json()
    assert snapshot['task']['objective'] == '日本語の成果物を作る'
    assert snapshot['events'][0]['detail']['objective'] == '日本語の成果物を作る'
    assert engine.calls == [('start',task['id'])]
    assert client.get('/api/status').json()['executor']['ready'] is False


def test_start_returns_without_awaiting_long_task(harness):
    client,store,engine,_,_ = harness
    async def pending():
        await asyncio.Event().wait()
    def start(identity):
        handle = asyncio.create_task(pending())
        engine.handle = handle
        return handle
    engine.start_task = start
    result = client.post('/api/tasks',json={'objective':'受付のみ確認'})
    assert result.status_code == 201
    client.portal.call(engine.handle.cancel)


@pytest.mark.parametrize('headers',[{'Host':'attacker.example'}, {'Origin':'https://attacker.example'},
                                    {'Origin':'http://localhost:8765'}, {'Sec-Fetch-Site':'cross-site'},
                                    {'Sec-Fetch-Site':'same-site'}])
def test_foreign_origin_or_host_cannot_operate(harness,headers):
    client,store,engine,_,_ = harness
    assert client.post('/api/tasks',headers=headers,json={'objective':'unauthorized'}).status_code == 403
    assert not engine.calls and not store.list_tasks()


def test_same_origin_csrf_and_session_required(harness):
    client,store,engine,_,_ = harness
    assert client.post('/api/tasks',headers={'X-CSRF-Token':''},json={'objective':'missing token'}).status_code == 403
    assert client.post('/api/tasks',headers={'Origin':'http://127.0.0.1:8765'},json={'objective':'same origin'}).status_code == 201
    client.cookies.clear()
    assert client.get('/api/tasks').status_code == 401


def test_remote_client_blocked(tmp_path):
    store=Store(tmp_path)
    with TestClient(create_app(tmp_path,engine=Engine(store),settings=Settings(),store=store),
                    base_url='http://127.0.0.1:8765',client=('192.0.2.2',1000)) as client:
        assert client.get('/').status_code == 403
    store.close()


def test_settings_write_only_and_validation_no_secret_echo(harness):
    client,store,engine,settings,_=harness
    response=client.post('/api/settings',json={'model_api_key':SECRET,'model':'new-model'})
    assert response.status_code==200 and SECRET not in response.text
    assert settings.updated[0]['model_api_key']==SECRET
    assert SECRET not in client.get('/api/settings').text
    error=client.post('/api/settings',json={'bad':SECRET})
    assert error.status_code==422 and SECRET not in error.text
    error=client.post('/api/tasks',json={'objective':{'key':SECRET}})
    assert error.status_code==422 and SECRET not in error.text
    task=store.create_task('plain',[])
    store.event(task['id'],'pre','failed',{'message':SECRET,'api_key':SECRET})
    assert SECRET not in client.get('/api/tasks/'+task['id']).text


def test_sse_durable_replay_cursor_and_secret_filter(harness):
    client,store,engine,settings,_=harness
    task=store.create_task('stream',[])
    e1=store.event(task['id'],'pre','started',{})
    e2=store.event(task['id'],'pre','succeeded',{'message':SECRET})
    e3=store.event(task['id'],'post','started',{})
    async def finite(identity,after=0):
        for event in store.events(identity,after):
            yield event
    store.subscribe=finite
    response=client.get('/api/tasks/'+task['id']+'/events',params={'after':e1['seq']},headers={'Last-Event-ID':str(e2['seq'])})
    assert response.status_code==200
    assert f"id: {e3['seq']}\n" in response.text
    assert f"id: {e2['seq']}\n" not in response.text
    assert SECRET not in response.text
    assert response.headers['content-type'].startswith('text/event-stream')


def test_no_execute_or_fabricated_completion_endpoint(harness):
    client,store,engine,_,_=harness
    for path in ['/api/execute','/api/permit','/api/mark-pass','/api/tasks/task/pass']:
        assert client.post(path,json={'status':'PASS'}).status_code==404
    response=client.post('/api/tasks',json={'objective':'x','status':'completed','role':'reviewer'})
    assert response.status_code==422 and not engine.calls


def test_stop_preserves_unknown_effect_and_resume_uses_engine(harness):
    client,store,engine,_,_=harness
    task=store.create_task('stop-resume',[])
    stopped=client.post('/api/tasks/'+task['id']+'/stop',json={}).json()['task']
    assert stopped['status']=='stopped' and stopped['state']['unknown_effect']
    resumed=client.post('/api/tasks/'+task['id']+'/resume',json={}).json()['task']
    assert resumed['status']=='running' and resumed['state']['unknown_effect']
    assert engine.calls==[('stop',task['id']),('resume',task['id'])]


def test_exact_approval_hash_required(harness):
    client,_,engine,_,_=harness
    assert client.get('/api/approvals').json()['approvals'][0]['proposal_hash']=='exact-hash'
    assert client.post('/api/approvals/amend-1',json={'decision':'approve','expected_hash':'stale','reason':'理由'}).status_code==422
    assert not engine.calls
    assert client.post('/api/approvals/amend-1',json={'decision':'reject','expected_hash':'exact-hash','reason':'条件を維持'}).status_code==200
    assert engine.calls[-1]==('approval','amend-1','reject','exact-hash','条件を維持')


def test_safe_artifact_download_and_escape_rejection(harness,tmp_path):
    client,store,_,_,_=harness
    task=store.create_task('artifact',[])
    workspace=Path(task['workspace']);(workspace/'nested').mkdir();(workspace/'nested'/'結果.txt').write_text('成果物',encoding='utf-8')
    prefix='/api/tasks/'+task['id']+'/artifacts/'
    operation = _record_artifact(store, task, 'nested/結果.txt')
    artifact = client.get('/api/tasks/'+task['id']).json()['operations'][0]['result']['artifacts'][0]
    result=client.get(artifact['download_url'])
    assert result.status_code==200 and result.content=='成果物'.encode()
    assert result.headers['x-artifact-operation']==operation
    assert result.headers['x-artifact-sha256']==hashlib.sha256(result.content).hexdigest()
    assert 'attachment;' in result.headers['content-disposition']
    outside=tmp_path/'outside.txt';outside.write_text('outside-private')
    for path in ['%2e%2e/%2e%2e/outside.txt','C%3A/windows/win.ini','nested%5c..%5cother.txt']:
        response=client.get(prefix+path)
        assert response.status_code in {400,404} and 'outside-private' not in response.text
    os.link(outside,workspace/'hardlink.txt')
    assert client.get(prefix+'hardlink.txt').status_code==404


def _record_artifact(store, task, relative_path):
    from policy_harness.models import Operation, OperationResult
    operation=Operation(kind='file_write',args={'path':relative_path},purpose='Explicit HTTP artifact fixture',
        expected_result='Exact bytes',decisions=[{'id':'owned','statement':'Use only fixture files','rationale':'HTTP boundary'}])
    path=Path(task['workspace'])/relative_path
    result=OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',
        artifacts=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}])
    store.save_operation(task['id'],operation.model_dump())
    store.update_operation(operation.id,status='cycle_complete',result=result.model_dump())
    return operation.id


def test_artifact_two_versions_and_missing_recreated_file(harness):
    client,store,_,_,_=harness
    task=store.create_task('versioned artifact',[])
    path=Path(task['workspace'])/'結果 #1.txt'
    path.write_bytes(b'first version')
    first=_record_artifact(store,task,path.name)
    url=client.get('/api/tasks/'+task['id']).json()['operations'][0]['result']['artifacts'][0]['download_url']
    assert client.get(url).content==b'first version'
    path.write_bytes(b'second version')
    second=_record_artifact(store,task,path.name)
    current=client.get('/api/tasks/'+task['id']).json()['operations'][1]['result']['artifacts'][0]['download_url']
    assert first!=second and url!=current
    assert client.get(url).status_code==409
    assert client.get(current).content==b'second version'
    path.unlink()
    assert client.get(url).status_code==404
    path.write_bytes(b'recreated different bytes')
    assert client.get(url).status_code==409
    path.write_bytes(b'first version')
    assert client.get(url).content==b'first version'
    assert client.get(current).status_code==409
    assert client.get(url.split('?')[0]).status_code==409
    other=store.create_task('foreign task',[])
    assert client.get(url.replace(task['id'],other['id'])).status_code==404
    assert client.get(url.replace(first,second)).status_code==409


def test_artifact_verified_snapshot_is_stable_after_in_place_change(harness,monkeypatch):
    import policy_harness.server as server
    client,store,_,_,_=harness
    task=store.create_task('open handle snapshot',[])
    path=Path(task['workspace'])/'stable.bin'
    original=b'original verified data'*10000
    path.write_bytes(original)
    _record_artifact(store,task,path.name)
    url=client.get('/api/tasks/'+task['id']).json()['operations'][0]['result']['artifacts'][0]['download_url']
    response=server.StreamingResponse
    def mutate_after_verification(*args,**kwargs):
        path.write_bytes(b'changed after hash verification')
        return response(*args,**kwargs)
    monkeypatch.setattr(server,'StreamingResponse',mutate_after_verification)
    received=client.get(url)
    assert received.status_code==200 and received.content==original
    assert received.headers['x-artifact-sha256']==hashlib.sha256(original).hexdigest()
    assert path.read_bytes()==b'changed after hash verification'


def test_security_headers_and_no_cors(harness):
    client,*_=harness
    response=client.get('/')
    assert response.headers['x-frame-options']=='DENY'
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert 'access-control-allow-origin' not in response.headers
    assert response.headers['cache-control']=='no-store'


def test_shutdown_requires_exact_server_and_closes_event_signal(harness):
    client,_,_,_,app=harness
    called=[];app.state.request_shutdown=lambda:called.append(True)
    assert client.post('/api/shutdown',json={'expected_instance_id':'wrong'}).status_code==409
    assert called==[]
    identity=client.get('/api/session').json()['instance_id']
    assert client.post('/api/shutdown',json={'expected_instance_id':identity}).status_code==200
    assert called==[True] and app.state.shutdown_event.is_set()


def test_cli_rejects_remote_and_credential_urls():
    from policy_harness.cli import _local_url
    assert _local_url('http://127.0.0.1:8765/')=='http://127.0.0.1:8765'
    for value in ['https://example.com','http://user:secret@127.0.0.1:8765','http://127.0.0.1:8765/?token=value']:
        with pytest.raises(ValueError):
            _local_url(value)


def test_runtime_owner_lock_excludes_second_process_and_releases(tmp_path):
    from policy_harness.cli import _runtime_lock
    code = ('from policy_harness.cli import _runtime_lock; from pathlib import Path; import sys\n'
            'try:\n'
            ' with _runtime_lock(Path(sys.argv[1])): print("acquired")\n'
            'except ValueError: print("held"); sys.exit(3)\n')
    with _runtime_lock(tmp_path):
        other = subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],capture_output=True,text=True,timeout=15)
        assert other.returncode == 3 and other.stdout.strip() == 'held'
    later = subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],capture_output=True,text=True,timeout=15)
    assert later.returncode == 0 and later.stdout.strip() == 'acquired'


def _restart_source(tmp_path):
    project = tmp_path / 'project'
    source = project / 'src' / 'policy_harness'
    source.mkdir(parents=True)
    (source / 'fixture_version.py').write_text("value = 'before'\n", encoding='utf-8')
    return project


def _restart_attestation(project):
    from policy_harness.capabilities import canonical
    from policy_harness.server import source_manifest
    hashes = source_manifest(project)
    return {'candidate_id':'fixture-reviewed-candidate', 'source_version':hashlib.sha256(canonical(hashes)).hexdigest(),
            'source_hashes':hashes, 'activation_hash':'d' * 64, 'activation_status':'activated',
            'policy_hash':'fixture-policy', 'restart_required':True, 'effect_measurement':'pending-next-use'}


def test_server_restart_requires_boundary_and_binds_complete_marker(tmp_path):
    from policy_harness.server import validate_restart_marker
    project = _restart_source(tmp_path)
    data = tmp_path / 'data'
    store = Store(data)
    task = store.create_task('same task after reviewed update', ['preserve source'])
    engine = Engine(store)
    class Updates:
        def restart_attestation(self, candidate_id):
            assert candidate_id == 'fixture-reviewed-candidate'
            return _restart_attestation(project)
    engine.updates = Updates()
    context = {'launch_id':uuid4().hex, 'supervisor_id':uuid4().hex, 'generation_id':uuid4().hex}
    app = create_app(data, engine=engine, settings=Settings(), store=store)
    app.state.worker_context = context
    app.state.project_root = project
    exits = []
    app.state.request_restart_exit = lambda:exits.append(75)
    payload = {'candidate_id':'fixture-reviewed-candidate', 'resume_task_ids':[task['id']]}
    async def exercise():
        async with app.router.lifespan_context(app):
            operations = store.operations
            store.operations = lambda identity:[{'status':'executing'}]
            with pytest.raises(PolicyError, match='NOT_AT_BOUNDARY'):
                await engine.on_restart(payload)
            assert not (data / '.controller-restarts' / 'pending.json').exists()
            store.operations = operations
            with pytest.raises(PolicyError, match='ATTESTATION_CHANGED'):
                await engine.on_restart(dict(payload, source_version='stale'))
            await engine.on_restart(payload)
            assert app.state.shutdown_event.is_set()
    asyncio.run(exercise())
    marker, path = validate_restart_marker(data, context, project_root=project)
    assert exits == [75] and marker['worker_pid'] == os.getpid()
    assert marker['resume_task_ids'] == [task['id']]
    assert marker['attestation'] == _restart_attestation(project)
    assert store.events(task['id'])[-1]['status'] == 'requested'
    original = path.read_text(encoding='utf-8')
    invalid = json.loads(original); invalid['resume_task_ids'] = [{}]
    path.write_text(json.dumps(invalid), encoding='utf-8')
    with pytest.raises(PolicyError, match='TASKS_INVALID'):
        validate_restart_marker(data, context, project_root=project)
    path.write_text(original, encoding='utf-8')
    (project / 'src/policy_harness/fixture_version.py').write_text("value = 'unreviewed'\n")
    with pytest.raises(PolicyError, match='SOURCE_CHANGED'):
        validate_restart_marker(data, context, project_root=project)
    store.close()


@pytest.mark.parametrize('code', [0, 9, 75])
def test_supervisor_does_not_retry_normal_crash_or_unattested_exit(tmp_path, code):
    from policy_harness.cli import _supervise
    project = _restart_source(tmp_path)
    data = tmp_path / 'data'
    observed = tmp_path / 'launches.txt'
    script = ('import pathlib,sys; p=pathlib.Path(sys.argv[1]); '
              'p.open("a").write("launch\\n"); sys.exit(int(sys.argv[2]))')
    args = argparse.Namespace(host='127.0.0.1', port=8876, data_dir=str(data), launch_id=uuid4().hex)
    command = lambda context:[sys.executable, '-B', '-c', script, str(observed), str(code)]
    if code == 75:
        with pytest.raises(FileNotFoundError):
            _supervise(args, worker_command=command, project_root=project)
    else:
        assert _supervise(args, worker_command=command, project_root=project) == code
    assert observed.read_text().splitlines() == ['launch']
    records = list((data / '.controller-restarts').glob('process-*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding='utf-8'))
    assert record['exit_code'] == code
    assert record['status'] == ('restart_rejected' if code == 75 else 'exited')


# This child uses the actual server lifecycle, Store and supervisor in fresh
# Python processes. Only the review/activation and Engine work are fixtures.
RESTART_WORKER = r'''
import argparse, asyncio, hashlib, importlib.util, json, os, sys, traceback
from pathlib import Path
import uvicorn
import policy_harness.server as server_module
from policy_harness.cli import _serve
from policy_harness.capabilities import canonical
from policy_harness.server import source_manifest
from policy_harness.store import Store

project = Path(sys.argv[1])
context = json.loads(os.environ['HERMES_POLICY_WORKER'])
data = Path(context['data_dir'])
source = project / 'src/policy_harness/fixture_version.py'
spec = importlib.util.spec_from_file_location('fixture_version', source)
loaded = importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
store = Store(data)
task = store.list_tasks()[0]
class Policy:
    hash = 'fixture-policy'
class Settings:
    pass
class Updates:
    def restart_attestation(self, identity):
        hashes = source_manifest(project)
        return {'candidate_id':identity, 'source_version':hashlib.sha256(canonical(hashes)).hexdigest(),
                'source_hashes':hashes, 'activation_hash':'d' * 64, 'activation_status':'activated',
                'policy_hash':'fixture-policy', 'restart_required':True, 'effect_measurement':'pending-next-use'}
class Engine:
    policy = Policy()
    updates = Updates()
    def resume_task(self, identity):
        previous = store.get_task(identity)
        state = dict(previous['state'], resumed_by=os.getpid(), loaded_version=loaded.value)
        store.update_task(identity, state=state, status='running')
        store.event(identity, 'fixture_consumer', 'observed', state)
    async def close(self):
        pass
engine = Engine()
server_module.ROOT = project
server_module.create_runtime = lambda path:(store, Settings(), engine.policy, engine)
errors = []
async def advance(app):
    try:
        with (data / 'observed-processes.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps({'pid':os.getpid(), 'task_id':task['id'], 'loaded':loaded.value,
                                     'generation_id':context['generation_id']}) + '\n')
        if context['resume'] is None:
            old_stat = source.stat()
            source.write_text("value = 'newest'\n", encoding='utf-8')
            # Same length and timestamp would make an existing pyc look current.
            os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            await engine.on_restart({'candidate_id':'fixture-reviewed-candidate', 'resume_task_ids':[task['id']]})
        else:
            assert store.get_task(task['id'])['state']['loaded_version'] == 'newest'
            app.state.request_shutdown()
    except Exception as error:
        errors.append(type(error).__name__)
        traceback.print_exc()
        app.state.request_shutdown()
class ObservedServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started:
            asyncio.create_task(advance(self.config.app))
uvicorn.Server = ObservedServer
args = argparse.Namespace(host='127.0.0.1', port=0, data_dir=str(data),
                          launch_id=context['launch_id'], internal_worker=True)
exit_code = asyncio.run(_serve(args))
sys.exit(1 if errors else exit_code)
'''


def test_real_supervisor_new_process_consumes_new_source_and_same_task_once(tmp_path):
    from policy_harness.cli import _supervise
    from policy_harness.server import validate_restart_marker
    from policy_harness.capabilities import sha_file
    project = _restart_source(tmp_path)
    py_compile.compile(str(project / 'src/policy_harness/fixture_version.py'), doraise=True)
    data = tmp_path / 'data'
    store = Store(data)
    task = store.create_task('source-bound continuing task', ['same identity', 'retain unknown effects'])
    store.update_task(task['id'], state={'unknown_effect':'must remain explicit'})
    original_events = store.events(task['id'])
    store.close()
    program = tmp_path / 'fixture_worker.py'
    program.write_text(RESTART_WORKER, encoding='utf-8')
    args = argparse.Namespace(host='127.0.0.1', port=8876, data_dir=str(data), launch_id=uuid4().hex)
    result = _supervise(args, worker_command=lambda context:[sys.executable,'-B',str(program),str(project)], project_root=project)
    assert result == 0
    observations = [json.loads(line) for line in (data / 'observed-processes.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(observations) == 2
    assert observations[0]['pid'] != observations[1]['pid']
    assert [item['loaded'] for item in observations] == ['before','newest']
    assert {item['task_id'] for item in observations} == {task['id']}
    store = Store(data)
    current = store.get_task(task['id'])
    assert current['objective'] == task['objective'] and current['acceptance'] == task['acceptance']
    assert current['state']['unknown_effect'] == 'must remain explicit'
    assert current['state']['resumed_by'] == observations[1]['pid']
    events = store.events(task['id'])
    assert events[:len(original_events)] == original_events
    assert [(e['stage'], e['status']) for e in events[len(original_events):]] == [
        ('controller_restart','requested'), ('controller_restart','resuming'), ('fixture_consumer','observed')]
    store.close()
    directory = data / '.controller-restarts'
    records = [json.loads(path.read_text(encoding='utf-8')) for path in directory.glob('process-*.json')]
    assert sorted(record['exit_code'] for record in records) == [0,75]
    assert sorted(record['status'] for record in records) == ['exited','restart_accepted']
    receipt_paths = list(directory.glob('consumed-*.json'))
    assert len(receipt_paths) == 1 and not (directory / 'pending.json').exists()
    receipt = json.loads(receipt_paths[0].read_text(encoding='utf-8'))
    assert receipt['status'] == 'resume_dispatched'
    accepted = next(directory.glob('accepted-*.json'))
    context = json.loads((directory / 'supervisor.json').read_text(encoding='utf-8'))
    reference = dict(context['resume'], sha256=sha_file(accepted))
    with pytest.raises(PolicyError, match='ALREADY_CONSUMED'):
        validate_restart_marker(data, context, reference=reference, project_root=project)


def test_runtime_factory_connects_real_update_manager_without_model_calls(tmp_path):
    from policy_harness.server import create_runtime, ROOT
    async def check():
        store, settings, policy, engine = create_runtime(tmp_path)
        try:
            assert engine.updates.project_root == ROOT
            assert engine.updates.policy is policy
            assert engine.on_restart is None  # Enabled only by supervised server startup.
            assert not store.list_tasks()
        finally:
            await engine.close()
            store.close()
    asyncio.run(check())


def test_explicit_shutdown_wins_over_scheduled_restart(tmp_path, monkeypatch):
    import uvicorn
    import policy_harness.cli as cli
    from policy_harness.capabilities import atomic_json
    from policy_harness.server import restart_directory
    context = {'schema':'supervised-worker-v1', 'supervisor_id':uuid4().hex,
               'launch_id':uuid4().hex, 'generation_id':uuid4().hex, 'data_dir':str(tmp_path)}
    atomic_json(restart_directory(tmp_path) / 'supervisor.json', context)
    monkeypatch.setenv('HERMES_POLICY_WORKER', json.dumps(context))
    class Server:
        def __init__(self, config):
            self.app = config.app
            self.started = True
            self.should_exit = False
        async def serve(self):
            self.app.state.request_restart_exit()
            self.app.state.request_shutdown()
            assert self.should_exit
    class OwnedPipe:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass  # Keep the owner present; test the explicit stop race only.
    monkeypatch.setattr(uvicorn, 'Server', Server)
    monkeypatch.setattr(cli.threading, 'Thread', OwnedPipe)
    args = argparse.Namespace(host='127.0.0.1', port=8876, data_dir=str(tmp_path),
                              launch_id=context['launch_id'], internal_worker=True)
    assert asyncio.run(cli._serve(args)) == 0


def test_real_supervised_http_shutdown_exits_cleanly_without_buffered_stdin_fault(tmp_path):
    from policy_harness.cli import Client
    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-B', '-m', 'policy_harness.cli', 'supervise', '--port', '0',
            '--data-dir', str(tmp_path), '--launch-id', uuid4().hex,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        output = bytearray()
        try:
            while True:  # Incoming log events, not a timed readiness poll.
                line = await asyncio.wait_for(process.stdout.readline(), timeout=25)
                output.extend(line)
                if not line:
                    raise AssertionError(output.decode('utf-8', errors='replace'))
                match = re.search(rb'Uvicorn running on (http://127\.0\.0\.1:\d+)', line)
                if match:
                    break
            client = Client(match.group(1).decode())
            try:
                status = client.get('/api/status')
                assert status['supervised'] is True and status['active_tasks'] == 0
                assert status['model']['paid_request_performed'] is False
                stopped = client.post('/api/shutdown', {'expected_instance_id':status['instance_id']})
                assert stopped['status'] == 'shutdown_requested'
            finally:
                client.close()
            tail, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            output.extend(tail)
            (tmp_path / 'real-http-lifecycle.log').write_bytes(output)
            assert process.returncode == 0, output.decode('utf-8', errors='replace')
            assert b'Fatal Python error' not in output
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)
    asyncio.run(exercise())
    records = list((tmp_path / '.controller-restarts').glob('process-*.json'))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding='utf-8'))['exit_code'] == 0
