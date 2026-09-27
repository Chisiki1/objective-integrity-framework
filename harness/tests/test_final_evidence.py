"""Final consumers see old and long original evidence, with explicit fixture judgments."""
from copy import deepcopy

import pytest

from policy_harness.engine import CycleHeld, RevisionNeeded
from policy_harness.models import Cleanup, Completion, Operation, OperationResult, Review
from policy_harness.store import canonical, digest
from tests.test_core import FixtureGateway, runtime
from tests.test_web_recovery import SimulatedProcessLoss


@pytest.mark.asyncio
@pytest.mark.parametrize('contrary',[False,True])
async def test_final_consumer_uses_early_long_evidence_and_changes_disposition(tmp_path,contrary):
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Deliver the observed fixture document',['Complete document evidence'])
    task=store.update_task(task['id'],state={'plan':{'objective':task['objective'],'acceptance':task['acceptance']}})
    marker='EARLY_CONTRARY_EVIDENCE' if contrary else 'COMPLETE_EARLY_EVIDENCE'
    originals=[]
    for index in range(20):
        op=Operation(kind='file_read',args={'path':f'fixture-{index}.txt'},purpose='Retained fixture observation',
            expected_result='Complete recorded document',decisions=[{'id':'read','statement':'Observe document','rationale':'Fixture source'}]).model_dump()
        result=OperationResult(operation_id=op['id'],status='succeeded',effect='none',
            data={'text':('earlier content '*500+marker) if index==0 else 'ordinary observation'}).model_dump()
        store.save_operation(task['id'],op);store.update_operation(op['id'],status='cycle_complete',result=result)
        originals.append(deepcopy(store.get_operation(op['id'])))
    finish=Operation(kind='finish',purpose='Compare all observed evidence',expected_result='Reasoned final disposition',
        decisions=[{'id':'finish','statement':'Judge original result','rationale':'All fixture observations are present'}]).model_dump()
    store.save_operation(task['id'],finish)
    row=store.update_operation(finish['id'],status='learned',result=OperationResult(operation_id=finish['id'],status='succeeded').model_dump())

    class Gateway(FixtureGateway):
        exact_input=None
        async def generate(self,role,phase,payload,schema):
            if phase=='independent_refutation':
                self.exact_input=deepcopy(payload['raw_evidence'])
                missing= 'EARLY_CONTRARY_EVIDENCE' in canonical(payload['raw_evidence'])
                return Review(summary='Original contrary record found' if missing else 'Originals observed',
                    opinions=[{'id':'early-defect','observation':'Old document is contrary to acceptance','rationale':'Original content after the former preview boundary'}] if missing else []),{'fixture':True}
            value,usage=await super().generate(role,phase,payload,schema)
            if schema is Completion and payload['independent_refutation']['review']['opinions']:
                value=value.model_copy(update={'achieved':False,'unresolved':['Old contrary document'],
                    'acceptance':[x.model_copy(update={'achieved':False}) for x in value.acceptance]})
            return value,usage
    gateway=Gateway(engine.policy);engine.gateway=gateway
    if contrary:
        with pytest.raises(CycleHeld,match='Completion evidence'):
            await engine._finalize(task,row)
        assert store.get_task(task['id'])['status']!='completed'
    else:
        await engine._finalize(task,row)
        assert store.get_task(task['id'])['status']=='completed'
    evidence=gateway.exact_input
    assert len(evidence['operations'])==21
    assert evidence['operations'][0]['result']==originals[0]['result']
    assert marker in evidence['operations'][0]['result']['data']['text'][3600:]
    assert originals[0]['operation']['id'] not in [r['id'] for r in engine._history_context(task['id'])]
    review=store.records('independent_review')[-1]
    assert review['raw_evidence_hash']==digest(evidence)
    assert store.record_get('history_snapshot',review['history_snapshot_id'])['rows'][0]==originals[0]
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_bounded_final_refuter_gets_every_original_fragment(tmp_path):
    from tests.test_bounded_judgments import make_runtime
    store,engine,bounded,task,op,gateway,_=make_runtime(tmp_path,slack=4500)
    body='ordinary source evidence '*3000+'DECISIVE_TAIL'
    payload={'original_objective':task['objective'],'acceptance':task['acceptance'],
        'raw_evidence':{'old_operation':{'data':body}},'instruction':'Refute original evidence, retaining all contrary facts.'}
    assert not bounded._fits(task['id'],'independent_refutation',payload,Review,'reviewer')
    await bounded.review_proposal(task['id'],op,'independent_refutation',payload)
    coverage=store.records('bounded_review_coverage')[-1]
    assert coverage['source_sha256']==digest(payload)
    actual=[call for call in gateway.calls if call['phase']=='independent_refutation' and call['schema']=='Review']
    assert actual and any('DECISIVE_TAIL' in canonical(call['payload']['source_records']) for call in actual)
    assert all(call['payload']['source_records'] for call in actual)
    assert not gateway.capacity_rejections
    await engine.close();store.close()


def finish_task(store):
    task=store.create_task('Judge the saved fixture output',['Original observation is present'])
    read=Operation(kind='file_read',args={'path':'answer.txt'},purpose='Retained explicit fixture read',
        expected_result='Original observation',decisions=[{'id':'read','statement':'Observe original content','rationale':'Offline fixture evidence'}]).model_dump()
    store.save_operation(task['id'],read)
    store.update_operation(read['id'],status='cycle_complete',result=OperationResult(operation_id=read['id'],status='succeeded',
        effect='none',data={'observed':'Original observation is present'}).model_dump())
    finish=Operation(kind='finish',purpose='Judge saved evidence',expected_result='Original-bound decision',
        decisions=[{'id':'close','statement':'Check the original result','rationale':'Explicit fixture source'}]).model_dump()
    task=store.update_task(task['id'],state={'plan':{'objective':task['objective'],'acceptance':task['acceptance']},
        'operation_id':finish['id'],'cycle_id':'fixture-cycle'})
    store.save_operation(task['id'],finish)
    row=store.update_operation(finish['id'],status='learned',result=OperationResult(operation_id=finish['id'],
        status='succeeded',effect='none',data={'observed':'Original observation is present'}).model_dump())
    return task,row


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['unchanged','legacy','source','acceptance','review','snapshot'])
async def test_cached_finalization_consumes_original_binding_without_replaying_cleanup(tmp_path,monkeypatch,change):
    store,engine,_,_=runtime(tmp_path);task,row=finish_task(store)
    assess=engine._assess_targets;finish=engine.knowledge.finish;cleanup_calls=[]
    async def interrupted(task_id,phase,*args,**kwargs):
        if phase=='completion_choices_after':raise SimulatedProcessLoss('Cleanup committed; post-review has no response')
        return await assess(task_id,phase,*args,**kwargs)
    def counted(*args,**kwargs):
        cleanup_calls.append(deepcopy(args));return finish(*args,**kwargs)
    monkeypatch.setattr(engine,'_assess_targets',interrupted);monkeypatch.setattr(engine.knowledge,'finish',counted)
    with pytest.raises(SimulatedProcessLoss):await engine._finalize(task,row)
    monkeypatch.setattr(engine,'_assess_targets',assess)
    row=store.get_operation(row['operation']['id']);draft=deepcopy(row['finalization'])
    committed=deepcopy(store.records('cleanup_result'));assert len(cleanup_calls)==1 and len(committed)==1
    if change=='legacy':
        for key in ('source_hash','acceptance_hash','independent_review_id','independent_review_sha256','frozen_evidence_sha256','history_snapshot_id'):
            draft.pop(key,None)
        row=store.update_operation(row['operation']['id'],finalization=draft)
    elif change=='source':
        store.append_instruction(task['id'],'Also preserve the raw source.',task['source_hash'])
    elif change=='acceptance':store.update_task(task['id'],acceptance=['A changed accepted fixture condition'])
    elif change=='review':
        review=store.record_get('independent_review',draft['independent_review_id'])
        review['review']['summary']='A different saved judgment'
        store.record('independent_review',review['id'],review)
    elif change=='snapshot':
        snapshot=store.record_get('history_snapshot',draft['history_snapshot_id'])
        snapshot['rows'][0]['result']['data']['observed']='CHANGED ORIGINAL'
        store.record('history_snapshot',snapshot['id'],snapshot)
    old_row=deepcopy(row['finalization'])
    if change=='unchanged':
        await engine._finalize(store.get_task(task['id']),row)
        assert store.get_task(task['id'])['status']=='completed'
    else:
        with pytest.raises(CycleHeld):await engine._finalize(store.get_task(task['id']),row)
        assert store.get_task(task['id'])['status']!='completed'
        assert store.get_operation(row['operation']['id'])['finalization']==old_row
    assert len(cleanup_calls)==1 and store.records('cleanup_result')==committed
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown_cleanup',[False,True])
async def test_legacy_finish_returns_to_work_and_preserves_unobserved_cleanup(tmp_path,monkeypatch,unknown_cleanup):
    store,engine,_,_=runtime(tmp_path);task,row=finish_task(store)
    update=store.update_operation
    def interrupt(identity,**fields):
        result=update(identity,**fields)
        if fields.get('finalization'):raise SimulatedProcessLoss('Draft saved before any cleanup')
        return result
    monkeypatch.setattr(store,'update_operation',interrupt)
    with pytest.raises(SimulatedProcessLoss):await engine._finalize(task,row)
    monkeypatch.setattr(store,'update_operation',update)
    row=store.get_operation(row['operation']['id']);draft=deepcopy(row['finalization'])
    for key in ('source_hash','acceptance_hash','independent_review_id','independent_review_sha256','frozen_evidence_sha256','history_snapshot_id'):
        draft.pop(key,None)
    store.update_operation(row['operation']['id'],finalization=draft)
    intent=None
    if unknown_cleanup:intent=engine.knowledge.capture_cleanup_intent(task,row['operation'],draft['cleanup']['decisions'])
    def no_replay(*args,**kwargs):raise AssertionError('No cleanup may be replayed from this legacy finish')
    monkeypatch.setattr(engine.knowledge,'finish',no_replay)
    with pytest.raises(RevisionNeeded):await engine._close_cycle({'task_id':task['id'],'operation_id':row['operation']['id']})
    saved=store.get_operation(row['operation']['id'])
    assert saved['finalization']==draft and saved['status']=='cycle_complete' and saved['completion_deferred']
    assert store.records('cleanup_result')==[] and 'operation_id' not in store.get_task(task['id'])['state']
    if intent:
        assert store.record_get('cleanup_intent',intent['id'])==intent
        failure=store.record_get('cleanup_failure',intent['id'])
        assert failure['effect']=='unknown' and failure['cleanup_intent_ref']['sha256']==digest(intent)
        assert engine.knowledge.pending_projection_failures(task)
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('contrary',[False,True])
async def test_configured_capacity_completion_reaches_parent_with_contrary_evidence(tmp_path,contrary):
    from policy_harness.bounded_judgments import ContextObservation
    from tests.test_bounded_judgments import make_runtime, PackedFixtureGateway
    store,engine,bounded,_,_,packed,_=make_runtime(tmp_path,slack=6500)
    task,row=finish_task(store)
    marker='CONTRARY_ORIGINAL_RESULT' if contrary else 'CONFIRMED_ORIGINAL_RESULT'
    first=store.operations(task['id'])[0]
    result=deepcopy(first['result']);result['data']['observed']='preserved source record '*2400+marker
    store.update_operation(first['operation']['id'],result=result)
    fixture=FixtureGateway(engine.policy)
    class FinalGateway(PackedFixtureGateway):
        async def generate(self,role,phase,payload,schema,**kwargs):
            if schema in {Completion,Cleanup}:
                assert bounded._fits(task['id'],phase,payload,schema,role),'Actual final call exceeds configured capacity'
                self.calls.append({'phase':phase,'role':role,'schema':schema.__name__,'payload':deepcopy(payload)})
                value,usage=await fixture.generate(role,phase,payload,schema)
                if schema is Completion and 'CONTRARY_ORIGINAL_RESULT' in canonical(payload['independent_refutation']):
                    value=value.model_copy(update={'achieved':False,'unresolved':['Contrary original result'],
                        'acceptance':[x.model_copy(update={'achieved':False}) for x in value.acceptance]})
                return value,usage
            value,usage=await super().generate(role,phase,payload,schema,**kwargs)
            if schema is ContextObservation and marker in canonical(payload):
                value=value.model_copy(update={'summary':value.summary+' '+marker})
            if schema is Review and phase=='independent_refutation':
                observed='CONTRARY_ORIGINAL_RESULT' in canonical(payload)
                value=Review(summary='Bounded original-page refutation',opinions=[{'id':'contrary','observation':'CONTRARY_ORIGINAL_RESULT',
                    'rationale':'Decisive original result was supplied on this exact source page'}] if observed else [])
            return value,usage
    gateway=FinalGateway(engine.policy);gateway.settings=packed.settings;gateway.response_store=store;engine.gateway=gateway
    if contrary:
        with pytest.raises(CycleHeld,match='Completion evidence'):await engine._finalize(task,row)
        assert store.get_task(task['id'])['status']!='completed'
    else:
        await engine._finalize(task,row)
        assert store.get_task(task['id'])['status']=='completed'
    calls=[x for x in gateway.calls if x['schema']=='Completion']
    assert len(calls)==1 and 'bounded_context' in calls[0]['payload']
    assert calls[0]['payload']['acceptance']==task['acceptance']
    assert not gateway.capacity_rejections
    assert any(marker in canonical(x['payload']) for x in gateway.calls if x['schema']=='ContextObservation')
    await engine.close();store.close()
