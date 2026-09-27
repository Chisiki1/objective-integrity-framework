"""Integration of self-inspection, update ownership and explanation isolation."""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import PolicyError, Operation, OperationResult
from policy_harness.practical_engine import PracticalEngine
from policy_harness.practical_models import ToolRequest, PracticalStep, PracticalReview
from policy_harness.store import Store
from policy_harness.server import create_app
from policy_harness.explanations import ExplanationService
from tests.test_updates import fixture, docker_fixture
from tests.test_practical_runtime import Gateway, Web
from tests.test_server import Settings

REVIEW={'verdict':'accept','findings':[],'rationale':'Exact requested fixture change preserves behavior'}

def setup(fixture, replies=()):
    manager,_,project=fixture
    store=Store(manager.data_dir)
    gateway=Gateway(replies)
    gateway.settings=SimpleNamespace(public=lambda:{'model':'fixture-model','reasoning_effort':None,'model_api_key':'must-not-leak',
        'request_timeout_seconds':123,'user_agent':'fixture-agent','web_max_response_bytes':2000000},secret=lambda name:None)
    engine=PracticalEngine(store,manager.policy,Executor(manager.data_dir),gateway,Web(),Knowledge(store),updates=manager)
    task=store.create_task('Improve OIF source while preserving behavior', ['Requested improvement works'])
    return engine,store,task,project

async def perform(engine,task,name,**args):
    return await engine._perform(task,ToolRequest(name=name,arguments=args,purpose='Requested OIF improvement'),uuid4().hex)

@pytest.mark.asyncio
async def test_current_settings_and_confined_source_read(fixture):
    engine,store,task,project=setup(fixture)
    try:
        info=await perform(engine,task,'harness_info')
        assert info.status=='succeeded'
        assert info.data['settings']['model']=='fixture-model'
        assert info.data['settings']['request_timeout_seconds']==123
        assert info.data['settings']['user_agent']=='fixture-agent'
        assert info.data['effective_review_model']=='fixture-model'
        assert info.data['sampling']['temperature']['value'] is None
        assert 'must-not-leak' not in str(info.data)
        assert 'src/policy_harness/__init__.py' in info.data['source_files']
        source=await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        assert source.status=='succeeded' and 'ANSWER = 42' in source.data['text']
        event=store.events(task['id'])[-1]
        assert event['detail']['kind']=='harness_read'
        assert event['detail']['purpose']=='Requested OIF improvement'
        outside=await perform(engine,task,'harness_read',path='../credentials.json')
        assert outside.status=='failed' and outside.effect=='none'
    finally: store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption',['apply','rollback'])
async def test_interrupted_activation_can_reach_only_owned_preimage_recovery(fixture,monkeypatch,interruption):
    from tests.test_practical_runtime import steps,tool,finish
    engine,store,task,project=setup(fixture,[REVIEW,REVIEW])
    store.update_task(task['id'],state={'runtime':'practical-v1'})
    manager=engine.updates;docker_fixture(manager,monkeypatch)
    async def restart(payload):pass
    engine.on_restart=restart
    target=project/'src/policy_harness/__init__.py';original=target.read_bytes()
    try:
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        Path(task['workspace'],'change.py').write_bytes(original+b'# improvement\n')
        Path(task['workspace'],'extra.py').write_bytes(b'IMPROVEMENT = True\n')
        extra=project/'src/policy_harness/extra.py'
        proposal=await perform(engine,task,'harness_propose',rationale='Improve documented behavior',changes=[{'path':'src/policy_harness/__init__.py','source':'change.py'},{'path':'src/policy_harness/extra.py','source':'extra.py'}])
        identity=proposal.data['candidate_id']
        assert (await perform(engine,task,'harness_verify',candidate_id=identity)).status=='succeeded'
        if interruption=='apply':
            apply=manager._apply_files
            def interrupted(candidate,**kwargs):
                apply(dict(candidate,changes=candidate['changes'][:1]),**kwargs)
                raise OSError('Injected interruption after the file replacement')
            monkeypatch.setattr(manager,'_apply_files',interrupted)
            failed=await perform(engine,task,'harness_apply',candidate_id=identity)
        else:
            assert (await perform(engine,task,'harness_apply',candidate_id=identity)).status=='succeeded'
            import policy_harness.updates as updates
            write=updates._write
            def interrupted_write(path,data):
                write(path,data)
                if path==target:raise OSError('Injected interruption during preimage restoration')
            monkeypatch.setattr(updates,'_write',interrupted_write)
            engine.gateway.replies.append(REVIEW)
            failed=await perform(engine,task,'harness_rollback',candidate_id=identity)
            monkeypatch.setattr(updates,'_write',write)
        assert failed.effect=='unknown' and engine.maintenance_restart_pending
        first=store.operations(task['id'])[-1]
        other=store.create_task('Unrelated work',[])
        with pytest.raises(PolicyError):engine.start_task(other['id'])
        with pytest.raises(PolicyError):await perform(engine,task,'file_write',path='unsafe.txt',text='no')
        engine.gateway.replies += [steps(tool('harness_rollback',candidate_id=identity)),REVIEW,finish(),REVIEW]
        # Exercise the same HTTP resume caller used by the UI, including CSRF.
        import httpx
        app=create_app(manager.data_dir,engine=engine,store=store,settings=Settings())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8765') as client:
            engine.on_restart=restart
            assert (await client.get('/')).status_code==200
            token=(await client.get('/api/session')).json()['csrf_token']
            resumed=await client.post('/api/tasks/'+task['id']+'/resume',json={},headers={'X-CSRF-Token':token})
            assert resumed.status_code==200,resumed.text
            await engine.running[task['id']]
        assert target.read_bytes()==original
        assert not extra.exists()
        assert store.get_task(task['id'])['status']=='completed'
        assert not engine.maintenance_restart_pending
        original_now=store.get_operation(first['operation']['id'])
        assert original_now['result']['status']==('failed' if interruption=='apply' else 'succeeded')
        assert original_now['result']['effect']=='confirmed'
        assert any(x['operation_id']==first['operation']['id'] and x['result']['effect']=='unknown' for x in store.records('practical_result_history'))
        assert not Path(task['workspace'],'unsafe.txt').exists()
    finally:store.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('secret', ['sk-'+'k'*30, 'sk-'+'k'*12, 'tvly-'+'k'*24,
                                   'api_key: unconfigured-private-token',
                                   'password="unconfigured-private-token"',
                                   '"authorization": "Bearer unconfigured-private-token"'])
async def test_source_secrets_are_screened_before_ranges_or_review(fixture, secret):
    engine,store,task,project=setup(fixture)
    try:
        target=project/'src/policy_harness/__init__.py'
        original=b'#'+b'x'*(131072-12)+b' '+secret.encode()+b'\n'
        target.write_bytes(original)
        first=await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py',max_bytes=131072)
        second=await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py',offset=131072)
        assert first.status==second.status=='succeeded'
        assert first.data['redacted'] and second.data['redacted']
        combined=first.data['text']+second.data['text']
        assert secret not in combined and 'k'*10 not in combined
        # Review a small complete diff separately from the page-size case.
        original=('# '+secret+'\n').encode()
        target.write_bytes(original)
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        Path(task['workspace'],'change.py').write_bytes(original+b'# change\n')
        result=await perform(engine,task,'harness_propose',rationale='Comment improvement',changes=[{'path':'src/policy_harness/__init__.py','source':'change.py'}])
        assert result.status=='failed' and '送信を保留' in result.stderr
        assert not engine.gateway.calls
        assert target.read_bytes()==original
        opaque='private-configured-value-without-key-prefix'
        target.write_text('# '+opaque,encoding='utf-8')
        engine.gateway.settings.secret=lambda name:opaque if name=='model_api_key' else None
        result=await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py',offset=5,max_bytes=8)
        assert result.status=='failed' and opaque not in str(result.model_dump())
    finally: store.close()

@pytest.mark.asyncio
async def test_large_result_cleanup_warning_survives_durable_event(fixture):
    if not shutil.which('node'):
        pytest.skip('Node DOM fixture is unavailable; Windows observation is separate')
    engine,store,task,_=setup(fixture)
    try:
        await perform(engine,task,'harness_info')
        operation=Operation.model_validate(store.operations(task['id'])[-1]['operation'])
        result=OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',
            artifacts=[{'path':'output-'+str(i)+'.txt','sha256':'f'*64} for i in range(100)],
            data={'cleanup':{'removed':None,'reason':'CLEANUP_INTERRUPTED'}})
        engine._record_result(task,operation,result)
        event=store.events(task['id'])[-1]
        assert event['detail']['result']['truncated']
        assert store.get_operation(operation.id)['result']['data']['cleanup']==result.data['cleanup']
        import policy_harness
        script=Path(policy_harness.__file__).parent/'static/presentation.js'
        runner="const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInThisContext(x.script);process.stdout.write(JSON.stringify({important:OIFPresentation.important(x.event),view:OIFPresentation.describe(x.event,null,'')}));"
        done=subprocess.run([shutil.which('node'),'-e',runner],input=json.dumps({'script':script.read_text(encoding='utf-8'),'event':event}),capture_output=True,text=True,encoding='utf-8',check=True)
        view=json.loads(done.stdout)
        assert view['important'] and '後片付け' in view['view']['overview']
        assert 'CLEANUP_INTERRUPTED' in view['view']['outcome']
    finally: store.close()

@pytest.mark.asyncio
async def test_model_reply_and_review_have_concrete_expanded_prose(fixture):
    reply={'action':'complete','message':'現在のモデルはfixture-modelです。','artifacts':[],
           'acceptance':[{'criterion':0,'evidence':'Settings result'}]}
    review={'verdict':'revise','findings':['待ち時間が未記載です。','operation_id '+'a'*32],'rationale':'モデル名だけでは依頼を満たしません。'}
    engine,store,task,_=setup(fixture,[reply,review])
    try:
        await engine._call(task,'next_action',PracticalStep,{})
        await engine._call(task,'final_review',PracticalReview,{},role='reviewer')
        events=[e for e in store.events(task['id']) if e['stage']=='model' and e['status']=='responded']
        assert len(events)==2 and events[-1]['detail']['review']['verdict']=='revise'
        assert store.records('practical_call')[-1]['response']==review
        if not shutil.which('node'):
            pytest.skip('Durable review event checked; Node presentation fixture unavailable')
        import policy_harness
        script=Path(policy_harness.__file__).parent/'static/presentation.js'
        runner="const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));vm.runInThisContext(x.script);process.stdout.write(JSON.stringify(x.events.map(e=>({important:OIFPresentation.important(e),view:OIFPresentation.describe(e,null,'')}))));"
        done=subprocess.run([shutil.which('node'),'-e',runner],input=json.dumps({'script':script.read_text(encoding='utf-8'),'events':events}),capture_output=True,text=True,encoding='utf-8',check=True)
        values=json.loads(done.stdout)
        assert reply['message'] in values[0]['view']['overview'] and reply['message'] in values[0]['view']['outcome']
        assert values[1]['important'] and '修正が必要' in values[1]['view']['overview']
        assert review['rationale'] in values[1]['view']['outcome'] and review['findings'][0] in values[1]['view']['outcome']
        assert 'a'*32 not in values[1]['view']['outcome'] and '関連する作業の記録' in values[1]['view']['outcome']
        assert len(engine.gateway.calls)==2
    finally: store.close()

@pytest.mark.asyncio
async def test_explanation_uses_only_matching_saved_response(fixture):
    explanation={'overview':'次は設定を確認します','purpose':'現在の設定を確かめます','result':'設定確認が選ばれました','limits':'実行結果は別の記録です','next_step':'設定確認'}
    engine,store,task,_=setup(fixture,[explanation,explanation])
    try:
        call={'id':'owned-call','task_id':task['id'],'source_hash':task['source_hash'],'phase':'next_action','status':'responded',
              'response':{'message':'OIFの設定を確認します','action':'tools','tools':[{'name':'harness_info','purpose':'設定を確認'}]}}
        store.record('practical_call',call['id'],call)
        store.event(task['id'],'model','started',{'call_id':call['id'],'source_hash':task['source_hash']})
        event=store.events(task['id'])[-1]
        service=ExplanationService(store,engine.gateway)
        await service.explain(task['id'],event['seq'],'ja',lambda x:x)
        evidence=__import__('json').loads(engine.gateway.calls[-1][2]['evidence'])
        assert evidence['saved_model_response']['response']['message']=='OIFの設定を確認します'
        store.record('practical_call',call['id'],dict(call,source_hash='different-source'))
        await service.explain(task['id'],event['seq'],'ja',lambda x:x)
        evidence=__import__('json').loads(engine.gateway.calls[-1][2]['evidence'])
        assert evidence['saved_model_response'] is None
        assert store.events(task['id'])[-1]==event
    finally: store.close()

@pytest.mark.asyncio
async def test_candidate_requires_full_read_and_rejects_foreign_source(fixture):
    engine,store,task,project=setup(fixture,[REVIEW])
    try:
        Path(task['workspace'],'change.py').write_text('ANSWER = 42\n# reviewed change\n')
        args={'rationale':'Improve source documentation','changes':[{'path':'src/policy_harness/__init__.py','source':'change.py'}]}
        denied=await perform(engine,task,'harness_propose',**args)
        assert denied.status=='failed' and 'Read the complete' in denied.stderr
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        proposal=await perform(engine,task,'harness_propose',**args)
        assert proposal.status=='succeeded' and proposal.data['staged']
        assert (project/'src/policy_harness/__init__.py').read_text()=='ANSWER = 42\n'
        other=store.create_task('Other task',['Keep isolation'])
        denied=await perform(engine,other,'harness_verify',candidate_id=proposal.data['candidate_id'])
        assert denied.status=='failed'
        store.append_instruction(task['id'],'Only give a proposal',task['source_hash'])
        denied=await perform(engine,store.get_task(task['id']),'harness_apply',candidate_id=proposal.data['candidate_id'])
        assert denied.status=='failed'
    finally: store.close()

@pytest.mark.asyncio
async def test_real_update_route_and_restart_boundary(fixture,monkeypatch):
    completed={'action':'complete','message':'The installed OIF source was updated and read back',
               'acceptance':[{'criterion':0,'evidence':'Applied, loaded and read back through controller tools'}],
               'artifacts':[]}
    rejected=dict(completed,artifacts=['src/policy_harness/__init__.py'])
    engine,store,task,project=setup(fixture,[REVIEW,REVIEW,rejected,completed,REVIEW])
    manager=engine.updates
    driver=docker_fixture(manager,monkeypatch)
    requests=[]
    async def restart(payload): requests.append(payload)
    engine.on_restart=restart
    try:
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        Path(task['workspace'],'change.py').write_text('ANSWER = 42\n# reviewed change\n')
        proposal=await perform(engine,task,'harness_propose',rationale='Requested documentation improvement',changes=[{'path':'src/policy_harness/__init__.py','source':'change.py'}])
        assert proposal.status=='succeeded'
        identity=proposal.data['candidate_id']
        verify=await perform(engine,task,'harness_verify',candidate_id=identity)
        assert verify.status=='succeeded',verify
        applied=await perform(engine,task,'harness_apply',candidate_id=identity)
        assert applied.status=='succeeded',applied
        assert 'reviewed change' in (project/'src/policy_harness/__init__.py').read_text()
        assert await engine._restart_if_needed(task['id'])
        assert len(requests)==1 and requests[0]['resume_task_ids']==[task['id']]
        with pytest.raises(PolicyError): engine.start_task(task['id'])
        # Reconciliation observes the existing activation; it does not apply twice.
        last=store.operations(task['id'])[-1]
        result=await engine.maintenance.recover(task, __import__('policy_harness.models',fromlist=['Operation']).Operation.model_validate(last['operation']))
        assert result.status=='succeeded' and result.data['replayed'] is False
        assert not engine._controller_update_evidence(task)[0]['activated_source_loaded']
        # Fresh manager is a construction fixture, not proof of a process restart.
        # The separate isolated runtime test observes an actual new worker.
        from policy_harness.updates import UpdateManager
        fresh=UpdateManager(manager.data_dir,project,manager.policy)
        engine.updates=engine.maintenance.updates=fresh
        assert not await engine._restart_if_needed(task['id'])
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        proof=engine._controller_update_evidence(task)[0]
        assert proof['activated_source_loaded']
        assert proof['subsequent_controller_reads'][0]['current_bytes_match']
        consumed=store.record_get('maintenance_restart_consumed',last['operation']['id'])
        store.record('maintenance_restart_consumed',last['operation']['id'],dict(consumed,candidate_id='foreign'))
        assert not engine._controller_update_evidence(task)[0]['activated_source_loaded']
        store.record('maintenance_restart_consumed',last['operation']['id'],consumed)
        await engine.run_task(task['id'])
        final=store.get_task(task['id'])
        assert final['status']=='completed' and final['final']['artifacts']==[]
        review_payload=[c[2] for c in engine.gateway.calls if c[1]=='final_review'][-1]
        assert review_payload['controller_updates'][0]['activated_source_loaded']
        assert review_payload['feedback'] is None
        assert review_payload['previous_completion_feedback']['status']=='historical_rejected_proposal'
        assert review_payload['previous_completion_feedback']['feedback']['rejected_proposal']==PracticalStep.model_validate(rejected).model_dump()
        assert len(store.records('practical_completion_rejection'))==1
        # Completion did not manufacture a workspace copy of installed source.
        assert not Path(task['workspace'],'src').exists()
    finally: store.close()

@pytest.mark.asyncio
async def test_explanation_is_cached_and_does_not_change_task(fixture):
    explanation={'overview':'File saved','purpose':'Create the requested file','result':'Five bytes were saved','limits':'Content meaning is not established by the hash','next_step':'Read the final result'}
    engine,store,task,_=setup(fixture,[explanation])
    try:
        store.event(task['id'],'execution','succeeded',{'operation_id':'example','result':{'status':'succeeded','stdout':'hello'}})
        event=store.events(task['id'])[-1]
        original=store.get_task(task['id'])
        service=ExplanationService(store,engine.gateway)
        first,second=await asyncio.gather(*[service.explain(task['id'],event['seq'],'ja',lambda x:x) for _ in range(2)])
        assert first['explanation']==explanation and second['cached']
        assert len(engine.gateway.calls)==1
        assert store.get_task(task['id'])==original
        assert store.events(task['id'])[-1]==event
    finally: store.close()

@pytest.mark.asyncio
async def test_actual_docker_self_update_route(fixture):
    if os.getenv('HARNESS_RUN_UPDATE_DOCKER_TESTS')!='1':
        pytest.skip('Opt-in real isolated Docker verification')
    engine,store,task,project=setup(fixture,[REVIEW,REVIEW])
    async def restart(payload): pass
    engine.on_restart=restart
    try:
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        Path(task['workspace'],'change.py').write_text('ANSWER = 42\n# real isolated update\n')
        proposal=await perform(engine,task,'harness_propose',rationale='Requested fixture improvement',changes=[{'path':'src/policy_harness/__init__.py','source':'change.py'}])
        assert proposal.status=='succeeded',proposal
        verified=await perform(engine,task,'harness_verify',candidate_id=proposal.data['candidate_id'])
        assert verified.status=='succeeded',verified
        applied=await perform(engine,task,'harness_apply',candidate_id=proposal.data['candidate_id'])
        assert applied.status=='succeeded',applied
        assert 'real isolated update' in (project/'src/policy_harness/__init__.py').read_text()
    finally: store.close()


@pytest.mark.asyncio
async def test_active_improvement_can_be_restored_from_a_new_task(fixture,monkeypatch):
    engine,store,task,project=setup(fixture,[REVIEW,REVIEW,REVIEW])
    async def restart(payload):pass
    engine.on_restart=restart
    docker_fixture(engine.updates,monkeypatch)
    try:
        path=project/'src/policy_harness/__init__.py';original=path.read_bytes()
        await perform(engine,task,'harness_read',path='src/policy_harness/__init__.py')
        Path(task['workspace'],'change.py').write_bytes(original+b'# useful fixture change\n')
        proposed=await perform(engine,task,'harness_propose',rationale='Requested fixture improvement',changes=[{'path':'src/policy_harness/__init__.py','source':'change.py'}])
        identity=proposed.data['candidate_id']
        assert (await perform(engine,task,'harness_verify',candidate_id=identity)).status=='succeeded'
        assert (await perform(engine,task,'harness_apply',candidate_id=identity)).status=='succeeded'
        from policy_harness.updates import UpdateManager
        engine.updates=engine.maintenance.updates=UpdateManager(engine.updates.data_dir,project,engine.updates.policy)
        engine.maintenance_restart_pending=False
        other=store.create_task('Restore the active improvement from its saved preimage',['Original behavior restored'])
        restored=await perform(engine,other,'harness_rollback',candidate_id=identity)
        assert restored.status=='succeeded',restored
        assert path.read_bytes()==original
        review=engine.gateway.calls[-1][2]
        assert review['action']=='rollback' and review['sources'][0]['text']==other['objective']
    finally:store.close()
