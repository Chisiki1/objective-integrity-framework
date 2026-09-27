"""Deterministic controller tests. These are NOT live AI/Web compliance evidence."""
import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from policy_harness.engine import Engine
from policy_harness.knowledge import Knowledge
from policy_harness.models import (Assessment,AssessmentBatch,Cleanup,Completion,Disposition,Learning,
    Operation,OperationResult,PolicyError,ResearchQuery,Review,SkillSelection,TaskPlan)
from policy_harness.policy import PolicyCatalog
from policy_harness.store import Store,digest
from policy_harness.bounded_judgments import ContextObservation

POLICY=Path(__file__).parents[1]/'policy/complete-policy-v3.json'


class FixtureGateway:
    def __init__(self,policy,*,omit_opinion=False):
        self.policy=policy;self.calls=[];self.omit_opinion=omit_opinion;self.response_store=None

    async def generate(self,role,phase,payload,schema):
        self.calls.append((role,phase,payload))
        from .test_policy_admission import ADMISSION_SCHEMAS, fixture_engineering_response
        if schema in ADMISSION_SCHEMAS:
            return fixture_engineering_response(payload,schema,store=self.response_store),{'fixture':True,'usage':None,'cost':None}
        if schema is TaskPlan:
            value=TaskPlan(task_kind='document',objective=payload['objective'],acceptance=payload['acceptance'],deliverables=['answer.txt'],constraints=['owned workspace'],preservation=['original task'],needed_capabilities=['file tools'],missing_capabilities=[],phases=['write','read','finish'],source_coverage=[{'id':i,'method':'Fixture coverage only; no live semantic claim'} for i in [*self.policy.conditions,*self.policy.rules]],rationale='Fixture task extraction')
        elif schema is Operation:
            kinds=[x['operation']['kind'] for x in payload['operations']]
            if 'file_write' not in kinds:kind,args='file_write',{'path':'answer.txt','text':'hello'}
            elif 'file_read' not in kinds:kind,args='file_read',{'path':'answer.txt'}
            else:kind,args='finish',{'summary':'Fixture document produced'}
            value=Operation(kind=kind,args=args,purpose='Deliver the requested fixture file',expected_result='Observed result',decisions=[{'id':'D1','statement':'Use owned answer.txt','rationale':'Fixture source scope'},{'id':'D2','statement':'Keep requested content','rationale':'Fixture original acceptance'}])
        elif schema is ContextObservation:
            value=ContextObservation(coverage_ids=[r['id'] for r in payload['source_records']],
                summary='Fixture supplied source fragments retain original observations, ownership and unresolved limits.',
                limitations=['Synthetic interpretation; actual model fidelity is not observed.'])
        elif schema is Assessment:
            value=Assessment(objective_link=payload['objective'],rationale='Fixture target assessment',success='expected' if 'pre' in phase or 'before' in phase else 'succeeded',mistakes=[],recurrence='unknown',efficiency='Fixture measurement; no improvement claim',interactions='Owned target only',thinking_targets={x:'Fixture consideration for '+x for x in payload['required_thinking_targets']},ideas=[{'id':'proposal','target':'task','proposal':'Consider clearer wording for a future similar document','disposition':'reject','rationale':'Keep the exact requested content in this fixture'}])
        elif schema is AssessmentBatch:
            values=[]
            for target in payload['targets']:
                assessment,_=await self.generate(role,phase,{**payload,'target':target},Assessment)
                values.append({'target_id':target['id'],'assessment':assessment.model_dump()})
            value=AssessmentBatch(assessments=values)
        elif schema is ResearchQuery:
            value=ResearchQuery(query='https://fixture.invalid/documentation',rationale='Explicit fixture only',private_data_excluded=True)
        elif schema is Review:
            value=Review(summary='Fixture independent response; no real semantic validation',opinions=[{'id':phase+':opinion','observation':'Check original acceptance','rationale':'Fixture invariant'}])
        elif schema is Disposition:
            opinions=payload['review']['opinions']
            sources=payload.get('web',{}).get('sources',payload.get('sources',[]))
            value=Disposition(verdict='proceed',rationale='Fixture controlled consumption',opinion_responses=[] if self.omit_opinion else [{'opinion_id':o['id'],'disposition':'accept','rationale':'Compared with fixture source'} for o in opinions],web_refs=[x['id'] for x in sources])
        elif schema is SkillSelection:
            value=SkillSelection(selected=[],rejected=[{'id':x['id'],'reason':'Fixture does not claim actual Skill application'} for x in payload['knowledge']['skills']],new_knowledge_needed=['provisional actual outcome'],rationale='Fixture full candidate disposition')
        elif schema is Learning:
            value=Learning(outcome_summary='Fixture observed outcome '+phase,classifications=['create'],skill_updates=[],recurrence='unknown',next_use_trigger='Next task with the same concrete source needs',ideas=payload.get('required_ideas',[]),evidence_refs=[])
        elif schema is Completion:
            ops=[x for x in payload['operations'] if x['operation']['kind']=='file_read']
            evidence=[x['operation']['id'] for x in ops]
            independent=payload['independent_refutation']['id']
            value=Completion(achieved=True,acceptance=[{'criterion':x,'achieved':True,'evidence_refs':evidence,'independent_refs':[independent]} for x in payload['acceptance']],evidence_refs=evidence,unresolved=[],summary='Fixture file written and read; not live AI evidence')
        elif schema is Cleanup:
            value=Cleanup(decisions=[{'id':s['id'],'disposition':'retain','reason':'Retain unused provisional source with next trigger'} for s in payload['knowledge']['skills'] if s['needs_cleanup']],rationale='Fixture exact cleanup inventory')
        else:raise AssertionError(schema)
        return value,{'fixture':True,'usage':None,'cost':None}


class FixtureWeb:
    async def collect(self,query,*,phase,task_id,operation_id,evaluate=None):
        identity=uuid4().hex
        record={'id':identity,'url':'https://fixture.invalid/documentation','status':'started','fixture':True}
        if evaluate:await evaluate('before',record)
        record=dict(record,status='succeeded',elapsed_seconds=0.001)
        if evaluate:await evaluate('after',record)
        return {'sources':[{'id':identity,'url':record['url'],'title':'FIXTURE','text':'Fixture evidence, not an acquired Web page','sha256':hashlib.sha256(b'fixture').hexdigest()}],'fixture':True,'acquisitions':[record]}


class FixtureExecutor:
    def __init__(self):self.calls=[]
    def catalog(self,workspace=None):return {'fixture':True}
    async def health(self):return {'fixture':True}
    async def execute(self,workspace,operation):
        self.calls.append(operation.model_dump())
        path=workspace/operation.args['path']
        if operation.kind=='file_write':
            path.write_text(operation.args['text'],encoding='utf-8')
            return OperationResult(operation_id=operation.id,status='succeeded',effect='confirmed',data={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        if operation.kind=='file_read':
            return OperationResult(operation_id=operation.id,status='succeeded',data={'path':str(path),'text':path.read_text(encoding='utf-8'),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        raise AssertionError(operation.kind)


def runtime(tmp_path,**kwargs):
    store=Store(tmp_path/'data');policy=PolicyCatalog(POLICY);executor=FixtureExecutor();gateway=FixtureGateway(policy,**kwargs)
    engine=Engine(store,policy,executor,gateway,FixtureWeb(),Knowledge(store))
    return store,engine,executor,gateway


@pytest.mark.asyncio
async def test_complete_controlled_fixture_preserves_each_decision_and_cleanup(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    task=store.create_task('Write hello in answer.txt',['answer.txt contains hello'])
    result=await engine.run_task(task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert (Path(task['workspace'])/'answer.txt').read_text()=='hello'
    assert [x['kind'] for x in executor.calls]==['file_write','file_read']
    write=next(o for o in result['operations'] if o['operation']['kind']=='file_write')
    assert {a['target_id'] for a in write['pre_bundle']['assessments']}=={x['id'] for x in write['pre_bundle']['targets']}
    assert {write['operation']['id'],'D1','D2','arguments/args/text'}<={a['target_id'] for a in write['pre_bundle']['assessments']}
    assert {a['target_id'] for a in write['post_bundle']['assessments']}=={x['id'] for x in write['pre_bundle']['targets']}
    assert all(not x['needs_cleanup'] for x in result['knowledge']['skills'])
    assert all(x['effect']=='UNUSED_EFFECT_UNVERIFIED' for x in result['knowledge']['skills'])
    assert all(x['status']=='rejected' for x in result['knowledge']['ideas'])
    assert store.verify_events()
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_unconsumed_actual_opinion_prevents_any_execution(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path,omit_opinion=True)
    task=store.create_task('Write hello',['file exists'])
    result=await engine.run_task(task['id'])
    assert result['task']['status']=='attention_required'
    assert executor.calls==[]
    assert not (Path(task['workspace'])/'answer.txt').exists()
    assert any('opinion' in str(e['detail']).lower() for e in result['events'])
    await engine.close();store.close()


def test_single_use_exact_permit_cannot_be_replayed_or_retargeted(tmp_path):
    store=Store(tmp_path)
    task=store.create_task('x',['x'])
    op=Operation(kind='file_list',purpose='inventory',expected_result='files',decisions=[{'id':'D','statement':'Inspect','rationale':'Need current state'}]).model_dump()
    store.save_operation(task['id'],op);store.update_operation(op['id'],status='reviewed')
    token=store.issue_permit(task['id'],op['id'],digest(op),'policy','bundle')
    with pytest.raises(PolicyError):store.consume_permit(token,task_id=task['id'],operation_id=op['id'],action_hash='changed',policy_hash='policy',bundle_hash='bundle')
    store.consume_permit(token,task_id=task['id'],operation_id=op['id'],action_hash=digest(op),policy_hash='policy',bundle_hash='bundle')
    with pytest.raises(PolicyError):store.consume_permit(token,task_id=task['id'],operation_id=op['id'],action_hash=digest(op),policy_hash='policy',bundle_hash='bundle')
    with pytest.raises(PolicyError):store.update_operation(op['id'],operation=dict(op,purpose='altered'))
    store.close()


def test_restart_marks_started_action_unknown_without_replay(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path)
    task=store.create_task('x',['x']);store.update_task(task['id'],status='running')
    op=Operation(kind='file_write',args={'path':'possibly-written.txt','content':'x'},purpose='x',expected_result='x',decisions=[{'id':'D','statement':'x','rationale':'x'}]).model_dump()
    store.save_operation(task['id'],op);store.update_operation(op['id'],status='executing')
    reopened=Engine(store,engine.policy,executor,gateway,FixtureWeb(),engine.knowledge)
    result=store.get_operation(op['id'])['result']
    assert result['status']=='unknown' and result['effect']=='unknown'
    assert store.get_task(task['id'])['status']=='recovery_required'
    assert executor.calls==[]
    store.close()


@pytest.mark.asyncio
async def test_event_subscription_replays_and_wakes_on_new_event(tmp_path):
    store=Store(tmp_path);task=store.create_task('x',['x'])
    stream=store.subscribe(task['id'])
    initial=await anext(stream)
    waiting=asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    event=store.event(task['id'],'unit','succeeded',{'observed':True})
    assert (await asyncio.wait_for(waiting,2))['seq']==event['seq']>initial['seq']
    await stream.aclose();store.close()
