"""Actual persistence/executor recovery; AI/Web fixtures, no live compliance claim."""
import asyncio
import json
from pathlib import Path

import pytest

from policy_harness.engine import CycleHeld, RevisionNeeded
from policy_harness.executor import Executor
from policy_harness.models import Learning, Operation, OperationResult, TaskPlan, PolicyError
from policy_harness.store import canonical, digest
from policy_harness.model_routing import resolve_lease
from tests.test_core import FixtureGateway, runtime
from tests.test_grouped_repairs import op
from tests.test_model_routing import configure, selection
from tests.fixture_preparation import prepare_operation


async def cycle(engine, task, operation):
    state=await prepare_operation(engine,task,operation)
    for method in (engine._pre,engine._execute,engine._post,engine._learn,engine._close_cycle):
        await method(state)
    return engine.store.get_operation(operation.id)


@pytest.mark.asyncio
async def test_history_pages_remain_readable_while_current_history_changes(tmp_path):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Read retained history',['history'])
    for i in range(4):
        item=op('file_read',path=str(i));store.save_operation(task['id'],item.model_dump())
        store.update_operation(item.id,status='cycle_complete',result=OperationResult(operation_id=item.id,status='failed',stderr='old fault '+str(i)).model_dump())
    first=await engine._dispatch(task,op('history_read',mode='index',limit=2))
    target=first.data['items'][0];before=store.get_operation(target['id'])
    store.update_operation(target['id'],result=OperationResult(operation_id=target['id'],status='succeeded').model_dump())
    extra=op();store.save_operation(task['id'],extra.model_dump())
    second=await engine._dispatch(task,op('history_read',**first.data['next_args']))
    assert second.data['index_hash']==first.data['index_hash'] and second.data['total']==4
    assert second.data['next_args'] is None
    chunks=[];args=dict(target['read_args'],max_chars=111)
    while args:
        read=await engine._dispatch(task,op('history_read',**args));chunks.append(read.data['json_range']);args=read.data['next_args']
    assert json.loads(''.join(chunks))==[before]
    assert store.get_operation(target['id'])!=before
    foreign=store.create_task('Other',['other'])
    with pytest.raises(PolicyError,match='foreign'):
        await engine._dispatch(foreign,op('history_read',**target['read_args']))
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', [' ', 'APIキーは', 'ASCIIkeyis'])
async def test_secret_crossing_source_read_boundary_never_enters_public_chunks(tmp_path, prefix):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Read text',['source'])
    secret='sk-' + 'dummyboundarysecret0000000000000001'
    raw=('x'*(65532-len(prefix.encode('utf-8')))+prefix+secret+'\n末尾').encode('utf-8')
    task=store.append_instruction(task['id'],'Attachment',task['source_hash'],attachment={'filename':'source.txt','bytes':raw})
    source=task['source_history'][-1];parts=[];offset=0
    while True:
        result=await engine._dispatch(task,op('source_read',source_id=source['id'],expected_hash=source['sha256'],offset=offset))
        parts.append(result.stdout)
        if result.data['next_offset'] is None:break
        offset=result.data['next_offset']
    visible=''.join(parts)
    assert secret not in visible and 'dummyboundary' not in visible and 'sk-' not in visible
    assert len(visible.encode('utf-8'))==len(raw)
    assert store.source_bytes(task['id'],source['id'])[0]==raw
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_revised_learning_keeps_all_old_ideas_and_draft_history(tmp_path):
    store,engine,executor,_=runtime(tmp_path)
    class Revise(FixtureGateway):
        revised=False
        async def generate(self,role,phase,payload,schema):
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='post_disposition' and not self.revised:
                self.revised=True;value=value.model_copy(update={'verdict':'revise','rationale':'Reassess exact knowledge choices; preserve old ideas'})
            return value,usage
    engine.gateway=Revise(engine.policy);task=store.create_task('Observe',['file']);item=op('file_write',path='answer.txt',text='hello')
    state=await prepare_operation(engine,task,item)
    await engine._pre(state);await engine._execute(state)
    with pytest.raises(RevisionNeeded):await engine._post(state)
    first=store.get_operation(item.id)['post_draft'];ids={i['id'] for i in first['learning']['ideas']}
    await engine._post(state)
    second=store.get_operation(item.id)['post_bundle'];assert ids<={i['id'] for i in second['learning']['ideas']}
    assert len(store.records('post_learning_attempt'))==2 and len(executor.calls)==1
    await engine._learn(state);await engine._close_cycle(state)
    assert store.get_operation(item.id)['status']=='cycle_complete'
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_applied_judgments_reassess_changed_source_and_reuse_exact_version(tmp_path):
    store,engine,_,gateway=runtime(tmp_path);task=store.create_task('A',['A']);item=op().model_dump()
    choices=[{'id':'choice','statement':'Organize actual evidence'}];outcome={'episode_id':'actual'};result={'status':'succeeded'};web={'sources':[]}
    inputs={'context':{'operation':item,'result':result,'learning':{'fixture':'accepted input for cache identity'}},
            'consumer':{'kind':'operation','task_id':task['id'],'operation_id':item['id'],'operation_sha256':digest(item)}}
    first=await engine._judge_applied_knowledge(task,item,'same',choices,outcome,result,web,**inputs)
    count=len(gateway.calls)
    assert (await engine._judge_applied_knowledge(task,item,'same',choices,outcome,result,web,**inputs))==first and len(gateway.calls)==count
    changed=store.append_instruction(task['id'],'Retain source and verify again',task['source_hash'])
    second=await engine._judge_applied_knowledge(changed,item,'same',choices,outcome,result,web,**inputs)
    assert second['id']!=first['id'] and len(store.records('judgment_result'))==2
    assert len(store.records('applied_knowledge_review'))==2
    await engine.close();store.close()


class ClosureGateway(FixtureGateway):
    supports_model_leases=True
    def __init__(self,policy,store,settings):
        super().__init__(policy);self.store=store;self.response_store=store;self.settings=settings;self.lease_calls=[]
    async def generate(self,role,phase,payload,schema,*,model_lease=None,job_context=None):
        if model_lease:
            resolve_lease(self.settings,model_lease,role=role,job_context=job_context)
            self.lease_calls.append(job_context)
        task=self.store.get_task(payload['task_id'])
        if schema is Operation and task['state'].get('delegation_lease',{}).get('settlement_only'):
            rows=self.store.operations(task['id'])
            if not any(o['operation']['kind']=='file_read' and o['status']=='cycle_complete' for o in rows):
                return op('file_read',path='answer.txt'),{'fixture':True}
            return op('finish',summary='Closed actual child result and cleanup'),{'fixture':True}
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is TaskPlan and task['source_history'][-1]['kind']=='delegation_update' and task['source_history'][-1]['status']=='pending':
            source=task['source_history'][-1];settlement=json.loads(source['text'])['settlement']
            value=value.model_copy(update={**settlement,'source_hash':task['source_hash'],
                'source_dispositions':[{'source_id':source['id'],'classification':'replace','reason':'Reviewed parent cancelled deliverable; all mandatory result closure retained'}],
                'acceptance_dispositions':[
                    *[{'index':i,'old_hash':digest(c),'disposition':'withdraw','criterion':None,'source_ids':[source['id']],
                       'source_quote':source['text'],'reason':'Exact reviewed parent scope replacement'} for i,c in enumerate(task['acceptance'])],
                    *[{'index':None,'disposition':'add','criterion':c,'source_ids':[source['id']],'source_quote':c,
                       'reason':'Mandatory closure instead of cancelled delivery'} for c in settlement['acceptance']]]})
        return value,usage


@pytest.mark.asyncio
@pytest.mark.parametrize('committed_learning_gap', [False, True])
async def test_parent_recovers_stale_child_unknown_then_settles_mandatory_cycles(tmp_path, committed_learning_gap):
    store,engine,_,_=runtime(tmp_path);engine.executor=Executor(store.data_dir)
    settings=configure(tmp_path/'settings');engine.gateway=ClosureGateway(engine.policy,store,settings)
    parent=store.create_task('Produce a file',['parent file']);delegation=op('delegate',objective='Old child delivery',acceptance=['Old child criterion'],
        independent_scope='Owned child file',integration_plan='Parent consumes file',
        capability_requirements=['Read and review one owned text file'],quality_requirements=['Keep original effects and unknown observations'],
        cost_considerations='Keep configured contract and minimize rework; real cost is unknown',model_selections={r:selection(settings,r) for r in ('parent','worker','reviewer')})
    store.save_operation(parent['id'],delegation.model_dump());child=store.create_task('Old child delivery',['Old child criterion'],parent_id=parent['id'])
    lease={'parent_operation_id':delegation.id,'parent_source_hash':parent['source_hash'],
        'model_leases':engine._delegate_models(parent,delegation.model_dump())}
    store.update_task(child['id'],state={'delegation_lease':lease});child=store.get_task(child['id'])
    original=op('file_write',path='answer.txt',text='hello')
    state=await prepare_operation(engine,child,original);await engine._pre(state);await engine._execute(state)
    actual=store.get_operation(original.id)['result']
    unknown=OperationResult(operation_id=original.id,status='unknown',effect='unknown',stderr='Injected loss of controller observation after actual executor write').model_dump()
    store.update_operation(original.id,result=unknown,status='result_recorded')
    store.update_task(child['id'],state={'delegation_lease':lease,'operation_id':original.id,'cycle_id':'retained'},status='stopped')
    committed=None
    if committed_learning_gap:
        await engine._post(state)
        old=store.get_operation(original.id)
        engine.knowledge.apply(child,old['operation'],Learning.model_validate(old['post_bundle']['learning']),old['result'],
                               [s['id'] for s in old['pre_bundle']['skills']['selected']])
        committed=store.record_get('knowledge_application',child['id']+':'+original.id)
    parent=store.append_instruction(parent['id'],'Cancel old child delivery and close its actual work',parent['source_hash']);source=parent['source_history'][-1]
    scope=dict(child_id=child['id'],expected_lease_hash=digest(lease),parent_source_hash=parent['source_hash'],
        reason='Exact cancellation with mandatory closure',source_id=source['id'],source_quote=source['text'])
    with pytest.raises(PolicyError,match='Reconcile child effects'):
        await engine._describe_operation(parent,op('child_scope',**scope,disposition='retire').model_dump())
    recovery=op('child_reconcile',child_id=child['id'],operation_id=original.id,expected_lease_hash=digest(lease),
        expected_result_hash=digest(unknown),reason='Observe retained executor journal before changing scope')
    row=await cycle(engine,parent,recovery)
    assert row['status']=='cycle_complete' and store.get_operation(original.id)['result']==actual
    history=store.record_get('effect_reconciliation',recovery.id)
    assert history['original']['result']==unknown and (Path(child['workspace'])/'answer.txt').read_bytes()==b'hello'
    with pytest.raises(PolicyError,match='Required child result cycles'):
        await engine._describe_operation(parent,op('child_scope',**scope,disposition='retire').model_dump())
    settle=op('child_scope',**scope,disposition='settle',settlement={'objective':'Close cancelled child actual work',
        'acceptance':['All actual child results reviewed and answer.txt observed']})
    await cycle(engine,parent,settle)
    result=await asyncio.wait_for(asyncio.shield(engine.running[child['id']]),60)
    assert result['task']['status']=='completed',result['events'][-2:]
    assert result['task']['acceptance']==['All actual child results reviewed and answer.txt observed']
    assert result['task']['source_history'][0]['acceptance']==['Old child criterion']
    assert result['task']['source_history'][-1]['kind']=='delegation_update'
    assert all(o['status']=='cycle_complete' for o in store.operations(child['id']))
    assert not any(engine._mandatory_child_work(result['task']).values())
    assert len([o for o in store.operations(child['id']) if o['operation']['kind']=='file_write'])==1
    assert any(c['objective']=='Close cancelled child actual work' for c in engine.gateway.lease_calls)
    if committed is not None:
        assert store.record_get('knowledge_application',child['id']+':'+original.id)==committed
        assert store.record_get('episode',committed['outcome']['episode_id'])['result']==unknown
    await engine.close();store.close()
