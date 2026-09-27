"""Whole-1 observed core faults: real persistence/files, explicit AI/Web fixtures."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from policy_harness.engine import CycleHeld
from policy_harness.models import EngineeringScope, EngineeringScenarios, OperationResult, PolicyError
from policy_harness.store import Store, digest
from tests.test_core import runtime, FixtureExecutor
from tests.test_core_recovery_closure import ClosureGateway, cycle
from tests.test_grouped_repairs import op
from tests.test_model_routing import configure, selection
from tests.test_source_preparation import actual_runtime, attach, preparation
from tests.fixture_preparation import prepare_operation


def test_task_plan_targets_separate_bindings_without_losing_semantic_choices():
    from policy_harness.decisions import task_plan_targets, targets
    from policy_harness.models import TaskPlan
    plan = TaskPlan(task_kind='document', objective='Keep the requested checksum in the report',
        acceptance=['Report the selected source version'], deliverables=['answer.txt'],
        constraints=['Retain source_hash as document content'], preservation=['Original request'],
        needed_capabilities=['file tools'], missing_capabilities=[], phases=['write', 'read'],
        source_hash='controller-source', deferred_index_hash='controller-index',
        source_coverage=[{'id':'R03', 'binding':'Full source remains mandatory'}],
        source_dispositions=[{'source_id':'source-1', 'classification':'clarify', 'reason':'Keep version'}],
        acceptance_dispositions=[{'index':0, 'disposition':'retain', 'reason':'Still requested'}],
        deferred_considerations=[{'idea_id':'idea-1', 'source_hash':'candidate-version',
            'disposition':'include', 'reason':'Applies here', 'planned_application':'Read before writing'}],
        rationale='Review each substantive source and deferred choice')
    original = plan.model_dump()
    old = targets('task-plan', plan)
    new = task_plan_targets(plan)
    assert {r['id'] for r in old} - {r['id'] for r in new} == {
        'task-plan/source_hash', 'task-plan/deferred_index_hash'}
    assert new == [r for r in old if r['id'] not in {
        'task-plan/source_hash', 'task-plan/deferred_index_hash'}]
    assert plan.model_dump() == original
    # The same field names can be real choices outside the typed plan binding.
    assert {r['id'] for r in targets('document-choice', {
        'source_hash':'chosen version', 'deferred_index_hash':'report this value'})} == {
        'document-choice/source_hash', 'document-choice/deferred_index_hash'}


@pytest.mark.asyncio
async def test_plan_initialization_uses_typed_targets_and_resumes_saved_decisions(tmp_path, monkeypatch):
    from policy_harness.decisions import task_plan_targets, targets
    from policy_harness.models import TaskPlan
    store, engine, executor, gateway = runtime(tmp_path)
    invoked = []
    class PausedGraph:
        async def ainvoke(self, state, config):
            invoked.append(state['operation_id'])
    async def graph():return PausedGraph()
    monkeypatch.setattr(engine, '_graph', graph)
    try:
        task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
        await engine._initialize_task(task)
        current = store.get_task(task['id'])
        row = store.get_operation(current['state']['operation_id'])
        plan = TaskPlan.model_validate(row['operation']['args']['plan'])
        assert [d['id'] for d in row['operation']['decisions']] == [d['id'] for d in task_plan_targets(plan)]
        assert plan.deferred_index_hash == engine.knowledge.deferred_for_planning(task)['index_hash']
        assert {x['id'] for x in plan.source_coverage} == set(engine.policy.conditions) | set(engine.policy.rules)
        calls = len(gateway.calls)
        # A saved pre-change operation remains its exact original review unit.
        legacy_task = store.create_task('Retained plan', ['Retained outcome'])
        legacy = dict(row['operation'], id='legacy-plan', decisions=[
            {'id':d['id'], 'statement':d['statement'], 'rationale':plan.rationale}
            for d in targets('task-plan', plan)])
        store.save_operation(legacy_task['id'], legacy, policy_hash=engine.policy.hash)
        store.update_task(legacy_task['id'], state={'operation_id':'legacy-plan', 'cycle_id':'retained-cycle'})
        saved = store.get_operation('legacy-plan')
        await engine._initialize_task(store.get_task(legacy_task['id']))
        assert store.get_operation('legacy-plan') == saved
        assert invoked == [row['operation']['id'], 'legacy-plan']
        assert len(gateway.calls) == calls and not executor.calls
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_current_history_index_retains_original_hashes_and_observes_other_connection(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    other=None
    try:
        task=store.create_task('Read the exact current history',['retain unknown results'])
        operation=op('file_read',path='answer.txt').model_dump()
        store.save_operation(task['id'],operation)
        actual=OperationResult(operation_id=operation['id'],status='succeeded',
            effect='confirmed',data={'text':'結果の原文','measurement':0.0}).model_dump()
        store.update_operation(operation['id'],status='cycle_complete',result=actual)
        original=store.get_operation(operation['id'])
        index=store.operation_index(task['id'])
        assert index==engine._history_index(task['id'])
        assert index[0]['sha256']==digest(original)
        assert index[0]['read_args']['expected_hash']==digest([original])
        # Caller changes cannot alter a subsequent consumer's cached index.
        index[0]['read_args']['operation_ids'].clear()
        assert store.operation_index(task['id'])[0]['read_args']['operation_ids']==[operation['id']]
        assert engine._history_lookup(task['id'])['pending_count']==0
        other=Store(store.data_dir)
        unknown=OperationResult(operation_id=operation['id'],status='unknown',effect='unknown',
            data={'reason':'Actual response is unobserved'}).model_dump()
        other.update_operation(operation['id'],status='executing',result=unknown)
        revised=store.get_operation(operation['id'])
        current=store.operation_index(task['id'])
        assert current==engine._history_index(task['id'])
        assert current[0]['sha256']==digest(revised)!=digest(original)
        assert current[0]['read_args']['expected_hash']==digest([revised])
        assert engine._history_lookup(task['id'])['pending_ids']==[operation['id']]
        assert revised['result']==unknown
        assert not executor.calls
    finally:
        if other is not None:other.close()
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_parent_source_change_invalidates_an_issued_child_permit(tmp_path):
    store,engine,_,_=runtime(tmp_path)
    try:
        settings=configure(tmp_path/'settings');engine.gateway=ClosureGateway(engine.policy,store,settings)
        parent=store.create_task('Parent',['Parent outcome'])
        delegation=op('delegate',objective='Child',acceptance=['Child outcome'],independent_scope='Owned child answer.txt',
            integration_plan='Consume the exact child output',capability_requirements=['One owned file'],
            quality_requirements=['Keep the original parent scope'],cost_considerations='Configured fixture only',
            model_selections={r:selection(settings,r) for r in ('parent','worker','reviewer')})
        store.save_operation(parent['id'],delegation.model_dump())
        child=store.create_task('Child',['Child outcome'],parent_id=parent['id'])
        lease={'parent_source_hash':parent['source_hash'],'scope':'Original reviewed scope',
               'parent_operation_id':delegation.id,'model_leases':engine._delegate_models(parent,delegation.model_dump())}
        store.update_task(child['id'],state={'delegation_lease':lease})
        item=op('file_write',path='answer.txt',text='hello')
        state=await prepare_operation(engine,child,item)
        await engine._pre(state)
        row=store.get_operation(item.id)
        args=dict(task_id=child['id'],operation_id=item.id,action_hash=digest(item.model_dump()),
                  policy_hash=engine.policy.hash,bundle_hash=digest(row['pre_bundle']))
        permit=store.issue_permit(**args)
        before=store.record_get('permit_dependencies',permit)
        store.append_instruction(parent['id'],'Cancel the child action; retain its observations.',parent['source_hash'])
        with pytest.raises(PolicyError,match='Parent source changed'):
            store.consume_permit(permit,**args)
        assert store.get_operation(item.id)['status']=='reviewed'
        assert store.db.execute('SELECT consumed FROM permits WHERE id=?',(permit,)).fetchone()[0]==0
        assert store.record_get('permit_dependencies',permit)==before
        assert store.get_task(child['id'])['state']['delegation_lease']==lease
        assert not list(Path(child['workspace']).iterdir())
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_parent_change_during_final_async_inspection_prevents_child_effect(tmp_path):
    store,engine,_,_=runtime(tmp_path)
    try:
        class InspectionGateway(ClosureGateway):
            async def generate(self,role,phase,payload,schema,**kwargs):
                value,usage=await super().generate(role,phase,payload,schema,**kwargs)
                if schema is EngineeringScenarios:
                    # Explicit synthetic normal-path judgment. The actual
                    # source/lease and reviewer-call provenance remain real.
                    value=EngineeringScenarios(scenarios=[{
                        'id':'parent-cancellation',
                        'normal_path':'Inspect the prepared recipe and execute only under the current parent scope',
                        'boundary':'Final awaited candidate inspection to synchronous permit consumption',
                        'harm':'A child effect escapes a parent cancellation received during inspection',
                        'interactions':['Parent source, child lease, awaited inspection and workspace lock'],
                        'evidence_refs':['normal','workspace']}],
                        limitations=['Synthetic scenario for the inert inspection fixture; no live execution proof'])
                if schema is EngineeringScope:
                    value=value.model_copy(update={'engineering':True,
                        'rationale':'The explicit fixture source requires inspection of a prepared recipe and preservation of parent cancellation'})
                return value,usage
        settings=configure(tmp_path/'settings');engine.gateway=InspectionGateway(engine.policy,store,settings)
        parent=store.create_task('Parent',['Parent output'])
        child_objective='Child output after the prepared inert recipe is inspected; preserve parent cancellation'
        delegation=op('delegate',objective=child_objective,acceptance=['Child output'],independent_scope='Owned file',
            integration_plan='Consume actual result',capability_requirements=['One owned file'],quality_requirements=['Exact source'],
            cost_considerations='Configured fixture only',model_selections={r:selection(settings,r) for r in ('parent','worker','reviewer')})
        store.save_operation(parent['id'],delegation.model_dump())
        child=store.create_task(child_objective,['Child output'],parent_id=parent['id'])
        lease={'parent_operation_id':delegation.id,'parent_source_hash':parent['source_hash'],
               'model_leases':engine._delegate_models(parent,delegation.model_dump())}
        store.update_task(child['id'],state={'delegation_lease':lease})
        class AsyncInspection(FixtureExecutor):
            inspections=0
            async def describe_execution(self,workspace,args):
                self.inspections+=1
                if self.inspections==3:
                    await asyncio.sleep(0)
                    store.append_instruction(parent['id'],'Cancel the child output.',parent['source_hash'])
                return {'capability_id':args['capability_id'],'fixture_candidate':True,
                    'candidate':{'argv':['fixture-inert-recipe'],'image_id':'fixture-no-runtime',
                        'inputs':[],'environment':'Explicit in-memory inspection fixture',
                        'observation':'Count actual describe calls and assert no executor calls',
                        'comparison':'Cancel parent on final third inspection; no child result or file',
                        'declared_effects':['Candidate description only; stale execution must be refused'],
                        'recovery':'Retain the unconsumed operation and original child lease'}}
        executor=AsyncInspection();engine.executor=executor
        item=op('exec',capability_id='ASYNC-INSPECTION-FIXTURE')
        state=await prepare_operation(engine,child,item)
        await engine._pre(state)
        with pytest.raises(CycleHeld,match='Parent source changed'):await engine._execute(state)
        assert executor.inspections==3 and executor.calls==[]
        assert store.get_operation(item.id)['result'] is None
        assert not list(Path(child['workspace']).iterdir())
    finally:await engine.close();store.close()


@pytest.mark.parametrize('nested',[False,True])
def test_sqlite_full_retains_first_fault_and_next_small_transaction(tmp_path,nested):
    store=Store(tmp_path)
    try:
        pages=store.db.execute('PRAGMA page_count').fetchone()[0]
        store.db.execute('PRAGMA max_page_count='+str(pages+2))
        def write():return store.record('full','oversized',{'payload':'x'*500000})
        action=(lambda:store._transaction(write)) if nested else write
        with pytest.raises(sqlite3.DatabaseError) as captured:store._transaction(action)
        assert captured.value.sqlite_errorname=='SQLITE_FULL'
        assert 'no transaction is active' not in str(captured.value)
        assert store.record_get('full','oversized') is None
        store._transaction(lambda:store.record('recovery','small',{'observed':'small subsequent commit'}))
        assert store.record_get('recovery','small')['observed']=='small subsequent commit'
    finally:store.close()


@pytest.mark.asyncio
async def test_late_source_withdrawal_closes_old_cycle_then_reaches_fresh_reviewed_delivery(tmp_path):
    async with actual_runtime(tmp_path/'data') as (store,engine,executor,gateway):
        task,source=attach(store,store.create_task('Write hello in answer.txt',['answer.txt contains hello']))
        task=store.append_instruction(task['id'],'添付 opaque.bin を撤回します。元の hello ファイル作成は継続してください。',task['source_hash'])
        instruction=task['source_history'][-1]
        item=preparation(task,source,'withdraw',instruction_id=instruction['id'],source_quote=instruction['text'])
        store.save_operation(task['id'],item.model_dump(),policy_hash=engine.policy.hash)
        state={'task_id':task['id'],'operation_id':item.id}
        store.update_task(task['id'],state={'operation_id':item.id,'cycle_id':'late-source'})
        await engine._pre(state);await engine._execute(state)
        original=store.get_operation(item.id)
        current=store.append_instruction(task['id'],'Keep the withdrawal and all other original requirements.',task['source_hash'])
        await engine._post(state);await engine._learn(state);await engine._close_cycle(state)
        assert store.get_operation(item.id)['status']=='cycle_complete'
        assert not store.get_task(task['id'])['state'].get('operation_id')
        pending=store.record_get('source_preparation_confirmation_pending',item.id)
        assert pending['status']=='source_revalidation_required'
        assert next(s for s in store.get_task(task['id'])['source_history'] if s['id']==source['id'])['status']=='pending'
        assert store.record_get('source_preparation_confirmation',item.id) is None
        result=await asyncio.wait_for(engine.run_task(task['id']),120)
        assert result['task']['status']=='completed',result['events'][-3:]
        assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'hello'
        assert store.get_operation(item.id)['operation']==original['operation']
        assert store.get_operation(item.id)['result']==original['result']
        superseded=store.record_get('source_preparation_confirmation_pending',item.id)
        successor=store.get_operation(superseded['confirmation_operation_id'])
        assert superseded['status']=='superseded_by_reviewed_confirmation'
        assert successor['operation']['id']!=item.id and successor['status']=='cycle_complete'
        assert successor['operation']['args']['expected_source_hash']==current['source_hash']
        assert successor['pre_review'] and successor['post_review'] and successor['knowledge']
        assert result['task']['acceptance']==task['acceptance'] and len(result['task']['source_history'])==4


@pytest.mark.asyncio
async def test_recovered_stage_is_consumable_only_after_its_reviewed_recovery_cycle(tmp_path,monkeypatch):
    async with actual_runtime(tmp_path/'data') as (store,engine,executor,gateway):
        task,source=attach(store,store.create_task('Read attachment',['Preserve original']))
        item=preparation(task,source)
        store.save_operation(task['id'],item.model_dump(),policy_hash=engine.policy.hash)
        state={'task_id':task['id'],'operation_id':item.id}
        await engine._pre(state);real=executor.execute;calls=[]
        async def lose_result(workspace,operation):
            result=await real(workspace,operation);calls.append(operation.id)
            assert result.status=='succeeded',result
            raise RuntimeError('Observed write completed; injected lost return')
        monkeypatch.setattr(executor,'execute',lose_result)
        await engine._execute(state);monkeypatch.setattr(executor,'execute',real)
        unknown=store.get_operation(item.id)['result'];assert unknown['effect']=='unknown'
        await engine._post(state);await engine._learn(state);await engine._close_cycle(state)
        original=store.get_operation(item.id)
        recovery=op('reconcile',operation_id=item.id,reason='Consume the exact retained Executor receipt')
        store.save_operation(task['id'],recovery.model_dump(),policy_hash=engine.policy.hash)
        recovery_state={'task_id':task['id'],'operation_id':recovery.id}
        await engine._pre(recovery_state);await engine._execute(recovery_state);await engine._post(recovery_state)
        with pytest.raises(PolicyError):engine.source_preparation._protected(task['id'],item.id)
        await engine._learn(recovery_state)
        # An after-learning opinion on recovery also prevents early adoption.
        correction={'id':'recovery-opinion','task_id':task['id'],'operation_id':recovery.id,'status':'pending'}
        store.record('judgment_correction',correction['id'],correction)
        with pytest.raises(PolicyError,match='judgment correction'):engine.source_preparation._protected(task['id'],item.id)
        store.record('judgment_correction',correction['id'],{**correction,'status':'resolved'})
        await engine._close_cycle(recovery_state)
        recovered,protected=engine.source_preparation._protected(task['id'],item.id)
        assert recovered['result']['status']=='succeeded'
        assert store.record_get('source_preparation_confirmation',item.id)
        assert recovered['post_bundle']==original['post_bundle'] and recovered['post_bundle']['result']==unknown
        history=store.record_get('effect_reconciliation',recovery.id)
        assert history['original']['result']==unknown and history['reconciled']==recovered['result']
        assert len(calls)==1
        assert (Path(task['workspace'])/recovered['result']['data']['path']).read_bytes()==store.source_bytes(task['id'],source['id'])[0]
