"""Cross-component regression cases; model judgments are explicit fixtures."""
import asyncio
from pathlib import Path

import pytest

from policy_harness.engine import Engine
from policy_harness.models import Disposition, Operation, OperationResult, Review
from policy_harness.providers import ProviderError
from .test_core import FixtureGateway, FixtureWeb, runtime


@pytest.mark.asyncio
async def test_invalid_reference_proposal_is_rejudged_without_executing_it(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    class Gateway(FixtureGateway):
        bad=False
        feedback=None
        async def generate(self,role,phase,payload,schema):
            value,usage=await super().generate(role,phase,payload,schema)
            if schema is Disposition and not self.bad:
                self.bad=True
                return value.model_copy(update={'web_refs':['Prepared RFC URL is not an evidence ID']}),usage
            if schema is Disposition and payload.get('actual_format_feedback'):
                self.feedback=payload
            return value,usage
    gateway=Gateway(engine.policy);engine.gateway=gateway
    task=store.create_task('Write hello',['answer.txt contains hello'])
    result=await engine.run_task(task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert gateway.feedback['exact_response_contract']['web_refs']==[]
    assert 'Prepared RFC' in str(gateway.feedback['actual_format_feedback'])
    assert [x['kind'] for x in executor.calls]==['file_write','file_read']
    assert any(e['status']=='rejected_model_output' for e in result['events'])
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_final_outcome_opinions_drive_next_real_correction_input(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    class Gateway(FixtureGateway):
        revised=False
        consumed=[]
        async def generate(self,role,phase,payload,schema):
            if schema is Operation and payload.get('required_judgment_corrections'):
                correction=payload['required_judgment_corrections'][0]
                self.consumed.append(correction)
                reads=[x for x in payload['operations'] if x['operation']['kind']=='file_read' and x['result'] and x['result']['started_at']>=correction['created_at']]
                args={'review_id':correction['id'],'expected_hash':correction['hash'],'reason':'Actual additional governed read resolves the exact requested proof','evidence_operation_ids':[reads[-1]['id']]} if reads else {'path':'answer.txt'}
                return Operation(kind='judgment_resolve' if reads else 'file_read',args=args,purpose='Resolve the actual final review evidence gap',expected_result='New observed proof',decisions=[{'id':'fix','statement':'Consume exact prior opinion','rationale':'Original final review requested one additional read'}]),{'fixture':True}
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='completion_outcome_review' and not self.revised:
                value=Review(summary='Exact final evidence challenge',opinions=[{'id':'read-again','observation':'Read answer.txt once more through the governed reader and cite that result.','rationale':'Fixture evidence gap requires actual new observation.'}])
            if phase=='completion_outcome_disposition' and not self.revised:
                self.revised=True
                value=value.model_copy(update={'verdict':'revise','rationale':'Need the actual additional governed read requested by read-again.'})
            return value,usage
    gateway=Gateway(engine.policy);engine.gateway=gateway
    task=store.create_task('Write hello',['answer.txt contains hello'])
    result=await engine.run_task(task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert gateway.consumed and gateway.consumed[0]['review']['review']['opinions'][0]['id']=='read-again'
    assert [x['kind'] for x in executor.calls]==['file_write','file_read','file_read']
    deferred=next(o for o in result['operations'] if o.get('completion_deferred'))
    assert deferred['finalization']['post_review']['disposition']['verdict']=='revise'
    assert 'read-again' in deferred['completion_deferred']
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_lost_final_review_resumes_same_cleanup_without_reapplying(tmp_path):
    store,engine,executor,base=runtime(tmp_path)
    class Gateway(FixtureGateway):
        interrupted=False
        async def generate(self,role,phase,payload,schema):
            if phase=='completion_choices_after' and not self.interrupted:
                self.interrupted=True
                raise ProviderError('Fixture connection lost before final review response')
            return await super().generate(role,phase,payload,schema)
    engine.gateway=Gateway(engine.policy)
    actual_finish=engine.knowledge.finish
    calls=[]
    def counted(*args,**kwargs):
        calls.append(args)
        return actual_finish(*args,**kwargs)
    engine.knowledge.finish=counted
    task=store.create_task('Write hello',['answer.txt contains hello'])
    first=await engine.run_task(task['id'])
    assert first['task']['status']=='attention_required'
    assert len(calls)==1
    second=await engine.run_task(task['id'])
    assert second['task']['status']=='completed',second['events'][-1]
    assert len(calls)==1
    assert [x['kind'] for x in executor.calls]==['file_write','file_read']
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_policy_change_rejudges_cached_final_outcome_without_replaying_cleanup(tmp_path):
    from policy_harness.engine import RevisionNeeded
    store,engine,executor,gateway=runtime(tmp_path)
    original=engine._journal_judgment_result
    actual_finish=engine.knowledge.finish
    cleanup_calls=[]
    def counted(*args,**kwargs):
        cleanup_calls.append((store.get_task(args[0])['state']['operation_id'],args))
        return actual_finish(*args,**kwargs)
    def interrupt(task,operation,phase,*args):
        if phase=='completion-outcome':
            raise ProviderError('Fixture interruption after saved post-review, before publication')
        return original(task,operation,phase,*args)
    engine.knowledge.finish=counted
    engine._journal_judgment_result=interrupt
    task=store.create_task('Write hello',['answer.txt contains hello'])
    first=await engine.run_task(task['id'])
    assert first['task']['status']=='attention_required'
    row=next(o for o in first['operations'] if o['operation']['kind']=='finish')
    old_hash=engine.policy.hash
    old_review=row['finalization']['post_review']['id']
    old_cleanup=store.record_get('cleanup_result',task['id']+':'+row['operation']['id'])
    assert len(cleanup_calls)==1
    engine._journal_judgment_result=original
    # Simulate the already-authorized current source at this exact restart
    # boundary; policy approval itself is covered by the amendment tests.
    engine.policy.hash='f'*64
    before=len(gateway.calls)
    store.update_task(task['id'],policy_hash=engine.policy.hash)
    with pytest.raises(RevisionNeeded,match='Finalization evidence'):
        await engine._close_cycle({'task_id':task['id'],'operation_id':row['operation']['id']})
    saved=store.get_operation(row['operation']['id'])
    assert saved['finalization']==row['finalization'] and saved['completion_deferred']
    assert saved['finalization']['policy_hash']==old_hash
    assert saved['finalization']['post_review']['id']==old_review
    result=await engine.run_task(task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    new=next(o['finalization'] for o in reversed(result['operations']) if o.get('finalization'))
    assert new['policy_hash']==engine.policy.hash and new['post_review']['id']!=old_review
    assert new['independent_review_id']!=row['finalization']['independent_review_id']
    phases=[phase for role,phase,payload in gateway.calls[before:]]
    assert 'independent_refutation' in phases and 'completion_choices_before' in phases and 'completion_choices_after' in phases
    # A new reviewed finish owns any newly touched knowledge. The original
    # operation's committed cleanup and task effects must never be replayed.
    assert sum(op_id==row['operation']['id'] for op_id,args in cleanup_calls)==1
    assert store.record_get('cleanup_result',old_cleanup['id'])==old_cleanup
    assert [x['kind'] for x in executor.calls]==['file_write','file_read']
    assert store.get_task(task['id'])['status']=='completed'
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_stop_after_executor_consumed_cancel_preserves_result_without_more_calls(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    entered=asyncio.Event()
    async def swallowed_cancel(workspace,operation):
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            return OperationResult(operation_id=operation.id,status='unknown',effect='unknown',stderr='Fixture executor preserved actual interrupted state')
    executor.execute=swallowed_cancel
    task=store.create_task('Write hello',['answer.txt contains hello'])
    engine.start_task(task['id'])
    await asyncio.wait_for(entered.wait(),5)
    calls_before=len(gateway.calls)
    result=await asyncio.wait_for(engine.stop_task(task['id']),5)
    assert result['task']['status']=='stopped'
    assert len(gateway.calls)==calls_before
    interrupted=next(o for o in result['operations'] if o['operation']['kind']=='file_write')
    assert interrupted['result']['effect']=='unknown'
    assert not interrupted.get('post_review')
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_activation_does_not_wait_for_another_cycle_blocked_on_its_lock(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    a=store.create_task('Apply owned reviewed update',['new source active'])
    b=store.create_task('Prepare another update',['candidate staged'])
    class Updates:
        def activate(self,identity):return {'status':'activated','effect':'confirmed','candidate_id':identity}
    class Boundary(asyncio.Event):
        def __init__(self):super().__init__();self.waiting=asyncio.Event()
        async def wait(self):self.waiting.set();return await super().wait()
    engine.updates=Updates();engine.on_restart=lambda payload:None
    engine.cycle_boundary=Boundary();engine.active_cycles={a['id'],b['id']}
    def op(kind):return Operation(kind=kind,args={'candidate_id':'candidate'},purpose='Controlled update fixture',expected_result='Exact state',decisions=[{'id':'d','statement':'Use same owned candidate','rationale':'Fixture boundary'}])
    activating=asyncio.create_task(engine._dispatch_update(a,op('activate_update')))
    await asyncio.wait_for(engine.cycle_boundary.waiting.wait(),2)
    waiting=asyncio.create_task(engine._dispatch_update(b,op('prepare_update')))
    ar,br=await asyncio.wait_for(asyncio.gather(activating,waiting),2)
    assert ar.status=='succeeded'
    assert br.status=='pending' and br.effect=='none' and br.data['controller_quiesced']
    assert engine.controller_waiters==set()
    engine.active_cycles.clear()
    await engine.close();store.close()


def test_only_exact_update_recovery_can_cross_unknown_effect_guard(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    task=store.create_task('Update controller',['source recovered'])
    def op(kind,candidate='same'):return Operation(kind=kind,args={'candidate_id':candidate},purpose='Restore exact owned source',expected_result='Actual result',decisions=[{'id':'d','statement':'Use exact candidate','rationale':'Recovery fixture'}])
    original=op('activate_update');store.save_operation(task['id'],original.model_dump())
    store.update_operation(original.id,result=OperationResult(operation_id=original.id,status='unknown',effect='unknown').model_dump())
    assert engine._allowed_with_unknown(task,op('rollback_update'))
    assert not engine._allowed_with_unknown(task,op('rollback_update','other'))
    assert not engine._allowed_with_unknown(task,op('file_write'))
    store.close()
