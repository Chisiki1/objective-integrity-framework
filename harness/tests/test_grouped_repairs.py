"""Whole-review fault regressions. Fixtures do not establish live model semantics."""
import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from policy_harness.models import Operation,OperationResult,TaskPlan,Review,Disposition,Learning,PolicyError
from policy_harness.policy import PolicyCatalog
from policy_harness.providers import WebCollector,ProviderError
from policy_harness.store import Store,digest,canonical
from policy_harness.sources import validate_reconciliation
from tests.test_core import runtime,FixtureGateway


def op(kind='file_read',**args):
    return Operation(kind=kind,args=args,purpose='Observe exact owned source',expected_result='Actual bounded result',
        decisions=[{'id':'d','statement':'Use exact owned source','rationale':'Regression fixture'}])


def test_original_source_and_append_cas_preserve_exact_private_bytes(tmp_path):
    store=Store(tmp_path)
    original='  original\n';task=store.create_task(original,['  criterion  '])
    raw,sha=store.source_bytes(task['id'],task['source_history'][0]['id'])
    assert raw==canonical({'objective':original,'acceptance':['  criterion  ']}).encode()
    assert sha==hashlib.sha256(raw).hexdigest()
    text='\n追加してください  \n'
    changed=store.append_instruction(task['id'],text,task['source_hash'])
    assert changed['source_history'][-1]['text']==text
    assert store.source_bytes(task['id'],changed['source_history'][-1]['id'])[0]==text.encode()
    with pytest.raises(PolicyError,match='SOURCE_HASH_CONFLICT'):store.append_instruction(task['id'],'stale',task['source_hash'])
    assert len(store.get_task(task['id'])['source_history'])==2
    store.close()


def test_source_change_invalidates_already_issued_permit(tmp_path):
    store=Store(tmp_path);task=store.create_task('Original',['Original']);operation=op()
    store.save_operation(task['id'],operation.model_dump());store.update_operation(operation.id,status='reviewed')
    permit=store.issue_permit(task['id'],operation.id,digest(operation.model_dump()),'policy','bundle')
    store.append_instruction(task['id'],'Add a new condition',task['source_hash'])
    with pytest.raises(PolicyError,match='SOURCE_HASH_CONFLICT'):
        store.consume_permit(permit,task_id=task['id'],operation_id=operation.id,action_hash=digest(operation.model_dump()),policy_hash='policy',bundle_hash='bundle')
    assert store.get_operation(operation.id)['status']=='reviewed'
    store.close()


def replacement_plan(task):
    source=task['source_history'][-1]
    return {'objective':'Write goodbye','acceptance':['answer.txt contains goodbye'],'source_hash':task['source_hash'],
        'source_dispositions':[{'source_id':source['id'],'classification':'replace','reason':'Explicit replacement'}],
        'acceptance_dispositions':[{'index':0,'old_hash':digest(task['acceptance'][0]),'disposition':'replace',
            'criterion':'answer.txt contains goodbye','source_ids':[source['id']],'source_quote':source['text'],'reason':'Exact user replacement'}]}


def test_source_reconciliation_does_not_drop_old_acceptance(tmp_path):
    store=Store(tmp_path);task=store.create_task('Write hello',['answer.txt contains hello'])
    task=store.append_instruction(task['id'],'Replace hello with goodbye',task['source_hash']);plan=replacement_plan(task)
    validate_reconciliation(task,plan)
    broken=dict(plan,acceptance_dispositions=[])
    with pytest.raises(PolicyError,match='Every prior'):validate_reconciliation(task,broken)
    broken=dict(plan,source_dispositions=[dict(plan['source_dispositions'][0],classification='add')])
    with pytest.raises(PolicyError,match='mere addition'):validate_reconciliation(task,broken)
    store.close()


@pytest.mark.asyncio
async def test_same_completed_task_accepts_explicit_replacement_and_delivers_new_file(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    task=store.create_task('Write hello',['answer.txt contains hello'])
    first=await engine.run_task(task['id']);assert first['task']['status']=='completed'
    class Gateway(FixtureGateway):
        async def generate(self,role,phase,payload,schema):
            if schema is TaskPlan and payload['source_context']['pending_ids']:
                value,usage=await super().generate(role,phase,payload,schema)
                return value.model_copy(update=replacement_plan(store.get_task(task['id']))),usage
            if schema is Operation:
                current=store.get_task(task['id'])
                rows=[x for x in store.operations(task['id']) if x.get('source_hash')==current['source_hash']]
                if not any(x['operation']['kind']=='file_write' for x in rows):return op('file_write',path='answer.txt',text='goodbye'),{'fixture':True}
                if not any(x['operation']['kind']=='file_read' for x in rows):return op('file_read',path='answer.txt'),{'fixture':True}
            return await super().generate(role,phase,payload,schema)
    engine.gateway=Gateway(engine.policy)
    await engine.submit_instruction(task['id'],'Replace hello with goodbye',first['task']['source_hash'])
    result=await asyncio.wait_for(engine.running[task['id']],120)
    assert result['task']['status']=='completed',result['events'][-1]
    assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'goodbye'
    assert result['task']['acceptance']==['answer.txt contains goodbye']
    assert all(s['status']=='applied' for s in result['task']['source_history'])
    assert store.records('task_final_history') and store.records('task_source_history')
    await engine.close();store.close()


def test_policy_approval_failure_rolls_back_record_version_event_and_live_object(tmp_path,monkeypatch):
    store,engine,executor,gateway=runtime(tmp_path);task=store.create_task('Policy test',['x'])
    key=next(iter(engine.policy.conditions));old=engine.policy.hash
    proposal=engine.policy.amendment({'condition_id':key,'old_text':engine.policy.conditions[key]['body'],
        'new_text':engine.policy.conditions[key]['body']+'\nFixture approved clarification.',
        'reason':'fixture','impact':'fixture','recovery':'original exact bytes'})
    item=dict(proposal,id=uuid4().hex,task_id=task['id'],status='awaiting_user');store.record('policy_amendment',item['id'],item)
    real=store.event
    def fail(*args,**kwargs):
        result=real(*args,**kwargs)
        if args[1]=='policy_amendment':raise OSError('Injected loss after event insertion before outer commit')
        return result
    monkeypatch.setattr(store,'event',fail)
    with pytest.raises(OSError):engine.resolve_approval(item['id'],'approve',item['proposal_hash'])
    assert engine.policy.hash==old and store.record_get('policy_amendment',item['id'])['status']=='awaiting_user'
    assert store.records('policy_version')==[] and store.record_get('policy_current','current') is None
    assert not any(e['stage']=='policy_amendment' for e in store.events(task['id']))
    monkeypatch.setattr(store,'event',real)
    approved=engine.resolve_approval(item['id'],'approve',item['proposal_hash']);assert approved['status']=='applied'
    reopened=PolicyCatalog(engine.policy.path)
    for amendment in store.records('policy_amendment'):reopened.apply_amendment(amendment)
    assert reopened.hash==engine.policy.hash!=old
    store.close()


def test_judgment_ideas_from_repeated_operations_keep_episode_identity_and_time(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path);task=store.create_task('Repeat',['Repeat'])
    idea={'id':'same','target':'workflow','proposal':'same method','disposition':'investigate','rationale':'observed recurrence'}
    for operation in [op(),op()]:
        engine._journal_judgment_result(task,operation.model_dump(),'same-phase',[{'target_id':'d','assessment':{'ideas':[idea]}}],{'status':'succeeded'}, {})
    rows=store.records('idea');assert len(rows)==2 and len({x['id'] for x in rows})==2
    for idea in rows:assert idea['created_at']==store.record_get('episode',idea['source_episode'])['created_at']
    store.close()


@pytest.mark.asyncio
async def test_web_learning_revision_consumes_same_opinions_without_reacquisition(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    class Gateway(FixtureGateway):
        revised=False;feedback=[]
        async def generate(self,role,phase,payload,schema):
            if phase=='web_acquisition_learning':self.feedback.append(payload.get('actual_revision_feedback'))
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='web_learning_review' and not self.revised:
                value=Review(summary='Revise knowledge only',opinions=[{'id':'original-opinion','observation':'Remove an unsupported use claim','rationale':'No application evidence; do not fetch again'}])
            if phase=='web_learning_disposition' and not self.revised:
                self.revised=True;value=value.model_copy(update={'verdict':'revise','rationale':'Correct only the learning record'})
            return value,usage
    gateway=Gateway(engine.policy);engine.gateway=gateway;task=store.create_task('Web',['Evidence']);operation=op('web_fetch',url='https://example.com/a').model_dump()
    result=await engine._research(task['id'],operation,'pre',{})
    assert result['sources'] and gateway.feedback[0] is None
    after=[x for x in store.records('web_work') if x['stage']=='after'];assert len(after)==1 and after[0]['status']=='complete'
    assert len(after[0]['learning_attempts'])==2
    from tests.test_web_learning_resume import assert_revision_facts
    previous=after[0]['learning_attempts'][0]
    assert previous['review']['opinions'][0]['id']=='original-opinion'
    assert_revision_facts(store,gateway.feedback[1],previous)
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_after_acquisition_revise_still_records_learning_before_new_proposal(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    class Gateway(FixtureGateway):
        async def generate(self,role,phase,payload,schema):
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='web_acquisition_disposition_after':value=value.model_copy(update={'verdict':'revise','rationale':'Observed response needs different source'})
            return value,usage
    engine.gateway=Gateway(engine.policy);task=store.create_task('Web',['Evidence']);operation=op('web_fetch',url='https://example.com/a').model_dump()
    from tests.test_web_recovery import collector
    requests=[];engine.web=collector(store,requests)
    with pytest.raises(PolicyError,match='new reviewed proposal'):
        await asyncio.wait_for(engine._research(task['id'],operation,'pre',{}),30)
    after=[x for x in store.records('web_work') if x['stage']=='after']
    assert after[0]['status']=='complete' and after[0]['learning_applied']
    assert store.record_get('episode',after[0]['learning_applied']['episode_id'])
    assert len(after)==len(requests)==len(store.records('knowledge_application'))==1
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_real_web_adapter_recovers_response_after_judgment_disconnect_without_http_replay(tmp_path):
    store=Store(tmp_path);task=store.create_task('Fetch public text',['text']);requests=[]
    class Settings:
        def secret(self,name):return ''
        def get(self):return {'web_provider':'public_url','user_agent':'Fixture','request_timeout_seconds':5,'web_max_response_bytes':10000}
    async def resolver(host):return ['93.184.216.34']
    async def transport(request):requests.append(request);return httpx.Response(200,headers={'content-type':'text/plain'},content=b'actual fixture transport bytes')
    interrupted=False
    async def evaluation(stage,detail):
        nonlocal interrupted
        if stage=='after' and not interrupted:interrupted=True;raise ProviderError('Lost model connection after actual response')
    web=WebCollector(Settings(),transport=httpx.MockTransport(transport),resolver=resolver);web.acquisition_store=store
    args={'phase':'pre','task_id':task['id'],'operation_id':'original-operation','evaluate':evaluation}
    with pytest.raises(ProviderError):await web.collect('https://example.com/a',**args)
    again=WebCollector(Settings(),transport=httpx.MockTransport(transport),resolver=resolver);again.acquisition_store=store
    result=await again.collect('https://example.com/a',**args)
    assert len(requests)==1 and result['sources'][0]['text']=='actual fixture transport bytes'
    assert result['acquisitions'][0]['response_body_available'] is True
    store.close()


@pytest.mark.asyncio
async def test_history_growth_keeps_model_projection_bounded_and_raw_records_readable(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path);task=store.create_task('Long task',['file'])
    first=None
    for i in range(80):
        operation=op('file_read',path=f'{i}.txt');first=first or operation.id
        store.save_operation(task['id'],operation.model_dump())
        store.update_operation(operation.id,status='revision_required',revision_reason='Exact cause '+str(i),
            result=OperationResult(operation_id=operation.id,status='failed',stderr='original first fault',data={'large':'x'*10000}).model_dump())
    context=engine._history_context(task['id'])
    assert len(context)==16 and len(canonical(context))<120000 and context[-1]['revision_reason']=='Exact cause 79'
    assert engine._history_lookup(task['id'])['operation_count']==80
    raw=await engine._dispatch(task,op('history_read',operation_ids=[first],max_chars=20000))
    assert raw.data['history'][0]['result']['stderr']=='original first fault'
    assert len(raw.data['history'][0]['result']['data']['large'])==10000
    store.close()


@pytest.mark.asyncio
async def test_legacy_acquisition_recovery_preserves_actual_learning_revision_and_never_fetches(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    task=store.create_task('Continue acquired outcome',['Evidence'])
    operation=op('web_fetch',url='https://example.com/a').model_dump()
    store.save_operation(task['id'],operation,policy_hash=engine.policy.hash)
    detail={'id':'legacy-acquisition','url':'https://example.com/a','status':'succeeded','http_status':200,'bytes':5,'elapsed_seconds':0.1}
    review=Review(summary='Acquisition metadata observed',opinions=[]).model_dump()
    disposition=Disposition(verdict='proceed',rationale='Keep actual result',opinion_responses=[],web_refs=[]).model_dump()
    store.event(task['id'],'pre_research_query','succeeded',{'result':{'query':'https://example.com/a','rationale':'Actual original choice','private_data_excluded':True}})
    store.event(task['id'],'web_acquisition','observed_after',detail)
    learning=Learning(outcome_summary='Original learning proposal',classifications=['create'],skill_updates=[],recurrence='unknown',next_use_trigger='Next similar result',ideas=[],evidence_refs=[]).model_dump()
    store.event(task['id'],'web_acquisition_learning','succeeded',{'result':learning})
    old_review=Review(summary='Repair this learning only',opinions=[{'id':'preserved-original-opinion','observation':'No actual Skill use was observed','rationale':'Do not fetch again to fix a learning record'}]).model_dump()
    store.event(task['id'],'web_learning_review','succeeded',{'result':old_review})
    old_disposition=Disposition(verdict='revise',rationale='Repair learning with original evidence',opinion_responses=[{'opinion_id':'preserved-original-opinion','disposition':'accept','rationale':'Actual missing-use observation'}],web_refs=[]).model_dump()
    event=store.event(task['id'],'web_learning_disposition','succeeded',{'result':old_disposition})
    store.record('web_review','legacy-acquisition:after',{'id':'legacy-acquisition:after','task_id':task['id'],'operation_id':operation['id'],'stage':'after','acquisition':detail,'assessments':[],'review':review,'disposition':disposition})
    engine.web_judgments.recover_legacy()
    saved=store.record_get('web_work','legacy-acquisition:after')
    assert saved['status']=='pending' and saved['learning_attempts'][0]['disposition']==old_disposition
    assert saved['learning_attempts'][0]['legacy_event_refs'][-1]=={'seq':event['seq'],'hash':event['hash']}
    class Gateway(FixtureGateway):
        async def generate(self,role,phase,payload,schema):
            if phase=='web_acquisition_learning':
                from tests.test_web_learning_resume import assert_revision_facts
                assert saved['learning_attempts'][0]['review']==old_review
                assert_revision_facts(store,payload['actual_revision_feedback'],saved['learning_attempts'][0])
                assert payload['research_choice']['rationale']=='Actual original choice'
            return await super().generate(role,phase,payload,schema)
    class NoFetch:
        async def collect(self,*args,**kwargs):raise AssertionError('Recovery must not perform a new HTTP request')
    engine.gateway=Gateway(engine.policy);engine.web=NoFetch()
    await engine.web_judgments.drain(task['id'])
    final=store.record_get('web_work','legacy-acquisition:after')
    assert final['status']=='complete' and final['learning_applied']
    assert final['detail']==detail and 'Missing raw response bytes' in final['legacy_recovery']
    await engine.close();store.close()
