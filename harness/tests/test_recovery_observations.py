"""Actual recovery-domain records and interrupted response consumption."""
import asyncio
from pathlib import Path

import pytest

from policy_harness.models import AssessmentBatch, OperationResult
from policy_harness.executor import Executor
from policy_harness.store import digest
from .test_core import FixtureGateway, runtime
from .test_core_recovery_closure import cycle
from .test_grouped_repairs import op
from .test_cleanup_recovery import prepare_repair, accept_repair
from .test_knowledge import seed_skill, learning, operation
from .test_provider_evidence_recovery import FixtureCipher
from .test_providers import Answer, FakePolicy, FakeSettings
from policy_harness.providers import ModelGateway, ProviderError
import httpx


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True])
@pytest.mark.parametrize('lost_at', ['during_observation', 'after_observation'])
async def test_recovery_operation_itself_recovers_without_repeating_stop_or_write(tmp_path, stop, lost_at):
    store,engine,_,_=runtime(tmp_path);engine.executor=Executor(store.data_dir)
    parent=store.create_task('Recover child',['Keep first effects'])
    child=store.create_task('Child',['File'],parent_id=parent['id'])
    lease={'parent_source_hash':parent['source_hash']};store.update_task(child['id'],state={'delegation_lease':lease})
    original=op('file_write',path='answer.txt',text='hello')
    actual=await engine.executor.execute(Path(child['workspace']),original)
    unknown=OperationResult(operation_id=original.id,status='unknown',effect='unknown',stderr='Lost result after actual write').model_dump()
    store.save_operation(child['id'],original.model_dump(),policy_hash=engine.policy.hash)
    store.update_operation(original.id,status='result_recorded',result=unknown)
    recovery=op('child_reconcile',child_id=child['id'],operation_id=original.id,expected_lease_hash=digest(lease),
                expected_result_hash=digest(unknown),reason='Observe actual child write',stop=stop)
    store.save_operation(parent['id'],recovery.model_dump(),policy_hash=engine.policy.hash)
    state={'task_id':parent['id'],'operation_id':recovery.id};await engine._pre(state)
    store.update_operation(recovery.id,status='executing')
    real_reconcile=engine.executor.reconcile;calls=[]
    async def observing(identity,*,stop=False):
        calls.append(stop)
        value=await real_reconcile(identity,stop=stop)
        if lost_at=='during_observation' and len(calls)==1:raise asyncio.CancelledError()
        return value
    engine.executor.reconcile=observing
    if lost_at=='during_observation':
        with pytest.raises(asyncio.CancelledError):await engine._dispatch(parent,recovery)
    else:
        await engine._dispatch(parent,recovery)
    store.update_operation(recovery.id,status='result_recorded',result=OperationResult(operation_id=recovery.id,
        status='unknown',effect='unknown',stderr='Injected interruption before parent result persistence').model_dump())
    resolve=op('reconcile',operation_id=recovery.id,stop=False)
    await cycle(engine,parent,resolve)
    assert store.get_operation(recovery.id)['result']['effect']!='unknown'
    for method in (engine._post,engine._learn,engine._close_cycle):await method(state)
    assert store.get_operation(original.id)['result']==actual.model_dump()
    assert calls==([stop,False] if lost_at=='during_observation' else [stop])
    assert (Path(child['workspace'])/'answer.txt').read_bytes()==b'hello'
    assert len([r for r in store.operations(child['id']) if r['operation']['kind']=='file_write'])==1
    assert store.record_get('recovery_observation',recovery.id)['original_operation']['result']==unknown
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_governed_history_read_consumes_retained_middle_response_without_request_replay(tmp_path):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Read model evidence',['Preserve actual failure'])
    calls=[];middle='EXACT_ORIGINAL_MIDDLE_EVIDENCE'
    raw=('a'*45000+middle+'z'*45000).encode()
    def transport(request):
        calls.append(request)
        return httpx.Response(503,content=raw)
    gateway=ModelGateway(FakeSettings(),FakePolicy(),transport=httpx.MockTransport(transport),
                         response_store=store,response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent','proposal',{'task_id':task['id']},Answer)
    reference=caught.value.metadata['response_record']
    assert middle not in caught.value.metadata['response_diagnostic']['sanitized_response']
    engine.gateway.read_response=gateway.read_response
    read=op('history_read',mode='model_response',reference=reference,offset=45000,limit=len(middle))
    row=await cycle(engine,task,read)
    assert row['result']['data']['text']==middle
    assert row['result']['data']['next_args']['expected_view_hash']==row['result']['data']['view_sha256']
    assert row['status']=='cycle_complete' and len(calls)==1
    foreign=store.create_task('Other',['Separate authority'])
    with pytest.raises(ProviderError,match='different task'):
        await engine._dispatch(foreign,read.model_copy(update={'id':'foreign-read'}))
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['post_assessment','learning_choices_before'])
async def test_partial_assessment_ideas_survive_reconstruction_into_required_set(tmp_path, phase):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Retain ideas',['Observed ideas stay pending'])
    action=op('file_read',path='input.txt');store.save_operation(task['id'],action.model_dump())
    store.update_operation(action.id,status='result_recorded',result=OperationResult(operation_id=action.id,status='failed').model_dump(),
                           pre_bundle={'assessments':[]})
    class Interrupted(FixtureGateway):
        count=0
        async def generate(self,role,actual_phase,payload,schema):
            if schema is AssessmentBatch:
                self.count+=1
                if self.count==2:raise asyncio.CancelledError()
            return await super().generate(role,actual_phase,payload,schema)
    engine.gateway=Interrupted(engine.policy)
    fits=engine.bounded_judgments._fits
    engine.bounded_judgments._fits=lambda task_id,p,payload,schema,role=None: (
        len(payload['targets'])<=1 if schema is AssessmentBatch else fits(task_id,p,payload,schema,role))
    targets=[{'id':'a','statement':'A'},{'id':'b','statement':'B'}]
    with pytest.raises(asyncio.CancelledError):
        await engine._assess_targets(task['id'],phase,targets,{'operation':action.model_dump()})
    rows=[r for r in store.records('bounded_model_call') if r['status']=='succeeded']
    assert len(rows)==1
    ideas=rows[0]['result']['assessments'][0]['assessment']['ideas']
    await engine.close();store.close()
    store,engine,_,_=runtime(tmp_path)
    required=engine._post_required_ideas(store.get_operation(action.id),[])
    by_id={i['id']:i for i in required}
    assert ideas and all(by_id.get(i['id'])==i for i in ideas)
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_advertised_history_read_arguments_produce_a_bound_immutable_index(tmp_path):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Read history',['Exact page'])
    item=op();store.save_operation(task['id'],item.model_dump())
    lookup=engine._history_lookup(task['id'])
    assert 'index_hash' not in lookup and lookup['current_observation_hash']
    page=await engine._dispatch(task,op('history_read',**lookup['read_args']))
    repeat=await engine._dispatch(task,op('history_read',**lookup['read_args'],
        expected_hash=page.data['index_hash'],snapshot_id=page.data['snapshot_id']))
    assert repeat.data==page.data
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True])
async def test_resolved_after_review_returns_to_original_protected_repair(tmp_path, nested):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Repair knowledge',['Retain original failure'])
    engine.executor=Executor(store.data_dir)
    knowledge=engine.knowledge;skill=seed_skill(store,knowledge,task)
    target=knowledge.root/'skills'/skill['id']/'SKILL.md'
    target.write_bytes(b'External preimage to preserve')
    observed=operation('file_read',args={'path':'source.txt'})
    outcome=knowledge.apply(task,observed,learning(),OperationResult(operation_id=observed['id'],status='succeeded').model_dump(),[])
    failure=store.record_get('projection_failure',outcome['projection_status']['failure_id'])
    repair,description=prepare_repair(store,knowledge,task,skill['id'])
    result=knowledge.repair_projection(task,repair['id'],repair['args'])
    accept_repair(store,task,repair,description,result)
    store.update_operation(repair['id'],status='learned')
    review={'id':'repair-review','disposition':{'verdict':'revise','rationale':'Confirm retained preimage'}}
    engine._record_judgment_correction(task,repair,result.model_dump(),review)
    state={'task_id':task['id'],'operation_id':repair['id']}
    await engine._close_cycle(state)
    assert store.record_get('projection_repair_confirmation',repair['id']) is None
    assert knowledge.pending_projection_failures(task)
    original_receipt=knowledge._repair_record(task,repair['id'])[2]
    evidence=await cycle(engine,task,op('file_list'))
    correction=store.record_get('judgment_correction',review['id'])
    resolve=op('judgment_resolve',review_id=correction['id'],expected_hash=correction['hash'],
               reason='Actual subsequent observation supports the protected repair',evidence_operation_ids=[evidence['operation']['id']])
    store.save_operation(task['id'],resolve.model_dump(),policy_hash=engine.policy.hash)
    resolve_state={'task_id':task['id'],'operation_id':resolve.id}
    for method in (engine._pre,engine._execute,engine._post,engine._learn):await method(resolve_state)
    assert store.record_get('projection_repair_confirmation',repair['id']) is None
    if nested:
        engine._record_judgment_correction(task,resolve.model_dump(),store.get_operation(resolve.id)['result'],
            {'id':'resolution-review','disposition':{'verdict':'revise','rationale':'One further observed check'}})
    await engine._close_cycle(resolve_state)
    if nested:
        assert store.record_get('projection_repair_confirmation',repair['id']) is None
        evidence2=await cycle(engine,task,op('file_list'))
        last=store.record_get('judgment_correction','resolution-review')
        await cycle(engine,task,op('judgment_resolve',review_id=last['id'],expected_hash=last['hash'],
            reason='Subsequent evidence resolves the original final opinion',evidence_operation_ids=[evidence2['operation']['id']]))
    assert knowledge.pending_projection_failures(task)==[]
    assert store.record_get('projection_repair_confirmation',repair['id'])
    assert store.record_get('projection_failure',failure['id'])==failure
    assert knowledge._repair_record(task,repair['id'])[2]==original_receipt
    assert store.get_operation(repair['id'])['status']=='cycle_complete'
    await engine.close();store.close()
