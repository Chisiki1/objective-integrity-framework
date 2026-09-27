"""Normal governed consumers with real SQLite, ModelGateway and file execution.

Only model/HTTP responses are synthetic. These cases establish routing and
preservation, not live model judgment quality or universal policy obedience.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import (ConfigurationRequired, Disposition, EngineeringAdmission,
    Learning, Operation, PolicyError, Review)
from policy_harness.operation_contracts import file_write_input
from policy_harness.policy import PolicyCatalog
from policy_harness.providers import ModelGateway, WebCollector
from policy_harness.store import Store, digest
from tests.test_core import POLICY
from tests.test_engine_recovery import proposed
from tests.test_learning_update_contract import create_update
from tests.test_provider_evidence_recovery import FixtureCipher
from tests.test_providers import FakeSettings, envelope
from tests.test_web_correction_flow import CorrectionGateway
from tests.test_web_recovery import Settings


class ProgressBehavior(CorrectionGateway):
    def __init__(self, policy, state):
        super().__init__(policy,nested=state.get('nested'))
        self.state=state

    async def generate(self,role,phase,payload,schema):
        state=self.state;store=self.response_store
        if schema is Operation:
            pending=payload.get('required_judgment_corrections',[])
            if pending and state.get('clone') and not state.get('clone_sent'):
                context=payload['web_correction_context'];state['clone_context']=deepcopy(context)
                parent=next(x['operation'] for x in context['parent_operations']
                            if x['operation']['kind']=='file_write' and x['operation']['args']['path']=='answer.txt')
                clone=deepcopy(parent);clone['id']=uuid4().hex
                state['clone_sent']=clone
                return Operation.model_validate(clone),{'fixture':True}
            if pending and state.get('same_path_diagnostic') and not state.get('diagnostic_sent'):
                state['diagnostic_sent']=True
                return proposed('file_read',{'path':'answer.txt'}),{'fixture':True}
            if state.get('bad_args') and (not state.get('bad_sent') or state.get('repeat_bad')):
                state['bad_sent']=True
                return proposed('file_write',{}),{'fixture':True}
            read=any(r['operation']['kind']=='file_read' and r['operation']['args'].get('path')=='input.txt'
                     and r.get('result',{}).get('status')=='succeeded' for r in payload['operations'] if r.get('result'))
            if state.get('prerequisite') and payload.get('proposal_revision_feedback') and not read:
                state['preparation_feedback']=deepcopy(payload['proposal_revision_feedback'])
                return proposed('file_read',{'path':'input.txt'}),{'fixture':True}
            if not pending:
                rows=store.operations(payload['task_id'])
                writes=[r for r in rows if r['operation']['kind']=='file_write' and r['operation']['args'].get('path')=='answer.txt'
                        and (r.get('result') or {}).get('status')=='succeeded']
                if writes and not any(r['operation']['kind']=='file_read' and r['operation']['args'].get('path')=='answer.txt'
                        and r.get('result') and r['result']['started_at']>=writes[-1]['result']['finished_at'] for r in rows):
                    return proposed('file_read',{'path':'answer.txt'}),{'fixture':True}
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is EngineeringAdmission and state.get('prerequisite'):
            op=payload['evidence']['operation']['value']
            read=any(r['operation']['kind']=='file_read' and r['operation']['args'].get('path')=='input.txt'
                     and (r.get('result') or {}).get('status')=='succeeded' for r in store.operations(payload['task_id']))
            if op['kind']=='file_write' and op['args']['path']=='answer.txt' and not read:
                value=value.model_copy(update={'required_preparation':['Read input.txt through governed file_read before this write.']})
        if schema is Learning and state.get('revise_learning'):
            identity=payload['operation']['id']
            state.setdefault('learning_target',identity)
            if identity==state['learning_target']:
                revised=bool(payload.get('actual_revision_feedback'))
                content='Observed transport; cause remains unknown.' if revised else 'Unsupported inferred transport cause.'
                value=value.model_copy(update={'outcome_summary':content,
                    'skill_updates':[dict(create_update(),content=content)]})
                state.setdefault('learning_drafts',[]).append(deepcopy(value.model_dump()))
        if schema is Review and phase.startswith('web_learning_review') and state.get('revise_learning'):
            value=Review(summary='Check the actual proposed reusable cause.',opinions=[
                {'id':'cause','observation':'The transport observation cannot establish a cause.',
                 'rationale':'Change the operative Skill content to retain unknown cause.'},
                {'id':'preserve','observation':'Preserve the original acquired result and every Idea.',
                 'rationale':'A revised proposal cannot replay the HTTP exchange.'}])
        if schema is Disposition:
            parent=payload.get('bundle',{}).get('accepted_input',{}).get('parent_operation',{})
            if parent.get('kind')=='plan_task':value=value.model_copy(update={'verdict':'proceed'})
            if (state.get('correction') and phase.startswith('web-learning-outcome')
                    and parent.get('kind')=='file_write' and parent['args'].get('path')=='answer.txt'
                    and not state.get('root_correction')):
                state['root_correction']=True
                value=value.model_copy(update={'verdict':'revise',
                    'rationale':'Write and read a separate correction record preserving this exact applied result, then resolve this review. The dependent answer write remains parked.'})
            if phase.startswith('web_learning_disposition') and state.get('revise_learning'):
                if payload['operation']['id']==state['learning_target']:
                    # In a page, the exact original has been transported into
                    # bounded_context; the fixture can inspect the retained
                    # source packet instead of inventing omitted full content.
                    original=payload
                    if 'learning' not in original:
                        original=payload['bounded_context']
                        if 'packet_id' in original:
                            original=store.record_get('bounded_input',original['packet_id'])['value']
                    revised=original['learning']['outcome_summary']=='Observed transport; cause remains unknown.'
                    verdict='proceed' if revised else 'revise'
                    value=value.model_copy(update={'verdict':verdict,
                        'rationale':'Apply this unchanged corrected Learning.' if revised else
                                    'Accept the requested operative correction; obtain and review a new complete Learning before applying.'})
        return value,usage


class WireGateway(ModelGateway):
    # ProgressBehavior returns the original canonical JSON and rejection cases.
    supports_semantic_wire = False

    def __init__(self,store,policy,state):
        self.state=state;self.behavior=ProgressBehavior(policy,state);self.behavior.response_store=store
        self.active=None
        super().__init__(FakeSettings(api_mode='compatible',model_context_tokens=2_000_000,max_output_tokens=65536),
            policy,transport=httpx.MockTransport(self.respond),response_store=store,response_protector=FixtureCipher())

    async def generate(self,role,phase,payload,schema,**kwargs):
        if (self.state.get('pause_after_clone') and schema is Operation
                and payload.get('actual_format_feedback') and self.state.get('clone_sent') and not self.state.get('paused')):
            self.state['paused']=True
            raise ConfigurationRequired('Synthetic pause with the exact parent parked after a rejected recreated effect')
        if self.state.get('pause_phase') and phase.startswith(self.state['pause_phase']) and not self.state.get('paused'):
            self.state['paused']=True
            raise ConfigurationRequired('Synthetic safe pause at '+phase)
        self.active=(role,phase,schema)
        return await super().generate(role,phase,payload,schema,**kwargs)

    async def respond(self,request):
        role,phase,schema=self.active
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'].split('\n',1)[1])
        self.state.setdefault('sent',[]).append({'role':role,'phase':phase,'schema':schema.__name__,
            'input':deepcopy(payload),'messages_sha256':digest(body['messages'])})
        value,_=await self.behavior.generate(role,phase,payload,schema)
        return httpx.Response(200,json=envelope(json.dumps(value.model_dump())))


def open_runtime(directory,state):
    store=Store(directory);policy=PolicyCatalog(POLICY);gateway=WireGateway(store,policy,state)
    executor=Executor(directory)
    async def resolver(host):return ['93.184.216.34']
    async def respond(request):
        # The actual transport observation records current controller ownership.
        state.setdefault('gets',[]).append({'url':str(request.url),
            'owner':deepcopy(store.get_task(state['task_id'])['state'].get('operation_id'))})
        return httpx.Response(200,headers={'content-type':'text/plain'},content=b'Exact mocked public source')
    web=WebCollector(Settings(),transport=httpx.MockTransport(respond),resolver=resolver)
    knowledge=Knowledge(store);apply=knowledge.apply
    def counted(task,operation,*args,**kwargs):
        state.setdefault('applications',[]).append(task['id']+':'+operation['id'])
        return apply(task,operation,*args,**kwargs)
    knowledge.apply=counted
    engine=Engine(store,policy,executor,gateway,web,knowledge)
    return store,engine,gateway


async def run(engine,identity):
    # Synthetic ordinary graphs only. This is a stuck-graph guard, not a product
    # timeout, live latency claim or permission to skip unresolved work.
    return await asyncio.wait_for(engine.run_task(identity),120)


def task_for(store,state):
    task=store.create_task('Write exactly hello in answer.txt and read it back; preserve corrections and inspect input.txt when needed.',
        ['answer.txt has the five UTF-8 bytes hello','Governed file_read confirms the actual five bytes'])
    state['task_id']=task['id']
    return task


def assert_delivered(store,task):
    rows=store.operations(task['id'])
    assert store.get_task(task['id'])['status']=='completed'
    assert Path(task['workspace'],'answer.txt').read_bytes()==b'hello'
    writes=[r for r in rows if r['operation']['kind']=='file_write' and r['operation']['args'].get('path')=='answer.txt' and r['result']]
    assert len(writes)==1 and writes[0]['result']['data']['bytes']==5
    reads=[r for r in rows if r['operation']['kind']=='file_read' and r['operation']['args']['path']=='answer.txt'
           and r['result'] and r['result']['started_at']>=writes[0]['result']['finished_at']]
    assert len(reads)==1 and reads[0]['result']['stdout']=='hello' and reads[0]['result']['data']['total_bytes']==5
    assert all(r['pre_review']['disposition']['verdict']=='proceed' and r['post_review']['disposition']['verdict']=='proceed'
               for r in [*writes,*reads])
    assert store.verify_events()


@pytest.mark.asyncio
async def test_invalid_arguments_receive_actual_feedback_before_dependent_web_then_normal_write(tmp_path):
    state={'bad_args':True};store,engine,gateway=open_runtime(tmp_path/'data',state);task=task_for(store,state)
    result=await run(engine,task['id']);assert_delivered(store,task)
    rejected=[e for e in result['events'] if e['stage']=='operation_proposal' and e['status']=='rejected_model_output']
    assert len(rejected)==1 and rejected[0]['detail']['rejected_response']['args']=={}
    assert 'WRITE_CONTENT' in rejected[0]['detail']['validation']['message']
    corrected=next(s for s in state['sent'] if s['phase']=='operation_proposal' and s['input'].get('actual_format_feedback'))
    assert corrected['input']['actual_format_feedback']['rejected_response']==rejected[0]['detail']['rejected_response']
    assert not any(r['operation']['kind']=='file_write' and not r['operation']['args'] for r in result['operations'])
    assert store.records('model_response') and store.records('knowledge_application')
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_repeated_invalid_arguments_hold_without_target_or_dependent_exchange(tmp_path):
    state={'bad_args':True,'repeat_bad':True};store,engine,_=open_runtime(tmp_path/'data',state);task=task_for(store,state)
    result=await run(engine,task['id'])
    assert result['task']['status']=='attention_required'
    rejected=[e for e in result['events'] if e['stage']=='operation_proposal' and e['status']=='rejected_model_output']
    assert len(rejected)==2 and not Path(task['workspace'],'answer.txt').exists()
    assert not any(r['operation']['kind']=='file_write' for r in result['operations'])
    assert all(store.get_operation(g['owner'])['operation']['kind']=='plan_task' for g in state['gets'])
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_saved_empty_arguments_reopen_preserves_original_and_revises_normally(tmp_path,monkeypatch):
    state={'bad_args':True};directory=tmp_path/'data';store,engine,_=open_runtime(directory,state);task=task_for(store,state)
    await engine._initialize_task(engine._current_cycle(task['id']))
    current=engine._validate_generated
    # Explicit historical producer setup: old Operation validation did not
    # inspect file arguments. Actual preparation/call/event/proposal are retained.
    with monkeypatch.context() as patch:
        patch.setattr(engine,'_validate_generated',lambda schema,*a,**kw: None if schema is Operation else current(schema,*a,**kw))
        patch.setattr(engine,'_require_operation_independence',lambda *a:None)
        await engine._select({'task_id':task['id']})
    old=deepcopy(store.get_operation(store.get_task(task['id'])['state']['operation_id']))
    assert old['operation']['args']=={} and old['result'] is None
    await engine.close();store.close();store,engine,_=open_runtime(directory,state)
    await run(engine,task['id']);assert_delivered(store,task)
    retained=store.get_operation(old['operation']['id'])
    assert retained['operation']==old['operation'] and retained['result'] is None
    assert retained['status']=='revision_required' and 'WRITE_CONTENT' in retained['revision_reason']
    assert not any(g['owner']==old['operation']['id'] for g in state['gets'])
    assert any((s['input'].get('proposal_revision_feedback') or {}).get('operation_sha256')==digest(old['operation']) for s in state['sent'])
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_required_preparation_returns_before_research_and_preserves_opinion_responses(tmp_path):
    state={'prerequisite':True};store,engine,_=open_runtime(tmp_path/'data',state);task=task_for(store,state)
    Path(task['workspace'],'input.txt').write_bytes(b'hello')
    # This complete preparation/read/write/final fixture was still advancing at
    # the ordinary 120s cancellation guard. Keep a bounded integration guard;
    # actual completion below is required, and no product timeout is changed.
    await asyncio.wait_for(engine.run_task(task['id']),300);assert_delivered(store,task)
    held=next(r for r in store.operations(task['id']) if r.get('preparation_review'))
    assert held['result'] is None and held['status']=='revision_required'
    # The semantic claim is "before this write". Independent governed Web/pre
    # review can proceed, but the exact write remains unexecuted until the read.
    assert held.get('pre_web') and held['result'] is None
    review=held['preparation_review'];assert review['admission']['missing']
    assert {o['id'] for o in review['review']['opinions']}=={o['opinion_id'] for o in review['disposition']['opinion_responses']}
    assert state['preparation_feedback']['preparation_review']==review
    diagnostic=next(r for r in store.operations(task['id']) if r['operation']['kind']=='file_read' and r['operation']['args']['path']=='input.txt')
    assert diagnostic['status']=='cycle_complete' and diagnostic['result']['stdout']=='hello'
    await engine.close();store.close()


@pytest.mark.parametrize('args',[
    {},{'path':'answer.txt','text':5},{'path':4,'text':'hello'},
    {'path':'../answer.txt','text':'hello'},{'path':'answer.txt','base64':'%%%'} ,
    {'path':'answer.txt','base64':True},{'path':'answer.txt','text':'hello','base64':'aA=='},
    {'path':'answer.txt','text':'hello','expected_sha256':7}])
def test_shared_final_file_argument_contract_rejects_before_any_mutation(tmp_path,args):
    with pytest.raises(PolicyError):file_write_input(tmp_path,args)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('paged',[False,True])
async def test_accepted_change_revises_actual_learning_and_passes_exact_disposition_to_after_review(tmp_path,monkeypatch,paged):
    state={'revise_learning':True};store,engine,_=open_runtime(tmp_path/'data',state);task=task_for(store,state)
    if paged:
        fits=engine.bounded_judgments._fits
        def bounded(task_id,phase,payload,schema,role=None):
            if schema is Disposition and len(payload.get('review',{}).get('opinions',[]))>1:return False
            return fits(task_id,phase,payload,schema,role)
        monkeypatch.setattr(engine.bounded_judgments,'_fits',bounded)
    await run(engine,task['id']);assert_delivered(store,task)
    work=store.record_get('web_work',state['learning_target']+':after')
    assert len(work['learning_attempts'])==2
    first,last=work['learning_attempts']
    assert first['disposition']['verdict']=='revise' and last['disposition']['verdict']=='proceed'
    assert first['learning']['skill_updates'][0]['content']=='Unsupported inferred transport cause.'
    assert work['applied_learning']==last['learning']
    assert work['applied_learning']['skill_updates'][0]['content']=='Observed transport; cause remains unknown.'
    assert {i['id'] for i in first['learning']['ideas']}<={i['id'] for i in last['learning']['ideas']}
    app=store.record_get('knowledge_application',task['id']+':'+work['detail']['id'])
    episode=store.record_get('episode',app['outcome']['episode_id']);assert episode['learning']==last['learning']
    assert state['applications'].count(app['id'])==1
    after=next(s['input']['accepted_input'] for s in state['sent']
        if s['phase'].startswith('web-learning-outcome_choices_after') and s['input']['operation']['id']==work['detail']['id'])
    assert after['accepted_learning_disposition']==last['disposition'] and after['accepted_learning_review']==last['review']
    assert after['accepted_learning_provenance']['application_intent']['input_hash']==app['input_hash']
    assert after['identity_and_stage']['parent_operation_id']==work['operation']['id']
    assert after['identity_and_stage']['acquisition_operation_id']==work['detail']['id']!=work['operation']['id']
    factual=next(s['input']['controller_facts'] for s in state['sent']
        if s['phase'].startswith('web-learning-outcome_choices_after') and s['input']['operation']['id']==work['detail']['id'])
    assert factual['review_lineage']['predecessor_review']==first['review']
    assert factual['review_lineage']['accepting_review_sha256']==digest(last['review'])
    assert factual['review_lineage']['proposal_created_at']==last['created_at']
    assert factual['next_use_trigger']['dispatches_operations_or_urls'] is False
    assert factual['application']['application_input_hash']==app['input_hash']
    assert all(x['stage']=='response' and x['response_sha256'] for x in factual['retained_exchanges'])
    calls=[s for s in state['sent'] if s['schema']=='Disposition' and s['phase'].startswith('web_learning_disposition')]
    assert calls and all(s['input']['operative_disposition_contract']['version']=='unchanged-reviewed-payload-v1' for s in calls)
    assert any('bounded_context' in s['input'] for s in calls)==paged
    if paged:
        target=[s for s in calls if s['input']['operation']['id']==state['learning_target']]
        assert len(target)==4 and all(len(s['input']['review']['opinions'])==1 for s in target)
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy',[False,True])
async def test_interrupted_acquired_learning_reopens_without_refetch_or_regenerating_success(tmp_path,monkeypatch,legacy):
    state={'pause_phase':'web_learning_choices_before'};directory=tmp_path/'data'
    store,engine,_=open_runtime(directory,state);task=task_for(store,state)
    if legacy:
        save=engine.web_judgments._save
        def old_save(value):
            # Explicit historical v1 writer: the context has no new semantic
            # labels and its actual wire messages/measurement remain original.
            value.pop('context_contract_version',None);save(value)
        monkeypatch.setattr(engine.web_judgments,'_save',old_save)
    with monkeypatch.context() as historical:
        if legacy:
            from policy_harness import web_judgments as web_module
            historical.setattr(web_module,'current_input',lambda value,**kw:deepcopy(value))
        result=await run(engine,task['id'])
    assert result['task']['status']=='configuration_required'
    work=next(w for w in store.records('web_work') if w['stage']=='after')
    old_learning=deepcopy(work['learning_attempts'][0]['learning'])
    receipts=deepcopy([r for r in store.records('bounded_model_call') if r.get('status')=='succeeded' and r['phase']=='web_acquisition_learning'])
    assert receipts and len(state['gets'])==1 and not store.records('knowledge_application')
    before_calls=len([s for s in state['sent'] if s['phase']=='web_acquisition_learning'])
    await engine.close();store.close();store,engine,_=open_runtime(directory,state)
    await run(engine,task['id']);assert_delivered(store,task)
    current=store.record_get('web_work',work['id'])
    assert current['detail']==work['detail'] and current['learning_attempts'][0]['learning']['outcome_summary']==old_learning['outcome_summary']
    assert all(store.record_get('bounded_model_call',r['id'])==r for r in receipts)
    target_calls=[s for s in state['sent'] if s['phase']=='web_acquisition_learning' and s['input']['operation']['id']==work['detail']['id']]
    assert len(target_calls)==before_calls==1
    assert ('identity_and_stage' not in target_calls[0]['input'])==legacy
    app_id=task['id']+':'+work['detail']['id'];assert state['applications'].count(app_id)==1
    exchanges=[x for x in store.records('web_exchange') if x['record']['id']==work['detail']['id']]
    assert len(exchanges)==1
    assert len(state['gets'])==len(store.records('web_exchange'))
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_changed_acquisition_identity_holds_reopened_after_result_before_any_send(tmp_path):
    state={'pause_phase':'web_learning_choices_before'};directory=tmp_path/'data'
    store,engine,_=open_runtime(directory,state);task=task_for(store,state)
    await run(engine,task['id']);work=next(w for w in store.records('web_work') if w['stage']=='after')
    # Deliberate corruption in this isolated Store, not a recovery API.
    work['acquisition_result']['operation_id']='foreign-acquisition';store.record('web_work',work['id'],work)
    sent=len(state['sent']);gets=len(state['gets'])
    await engine.close();store.close();store,engine,_=open_runtime(directory,state)
    result=await run(engine,task['id'])
    assert result['task']['status']=='attention_required' and 'identity mismatch' in result['events'][-1]['detail']['message']
    assert len(state['sent'])==sent and len(state['gets'])==gets and not store.records('knowledge_application')
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_accepted_disposition_tamper_after_commit_holds_without_reapplying_knowledge(tmp_path):
    state={'pause_phase':'web-learning-outcome_choices_after'};directory=tmp_path/'data'
    store,engine,_=open_runtime(directory,state);task=task_for(store,state)
    result=await run(engine,task['id']);assert result['task']['status']=='configuration_required'
    work=next(w for w in store.records('web_work') if w.get('learning_applied'))
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+work['detail']['id']))
    index=work['application_intent']['accepted_attempt_index']
    work['learning_attempts'][index]['disposition']['rationale']='Foreign replacement of the accepted response'
    store.record('web_work',work['id'],work)
    sent=len(state['sent']);gets=len(state['gets']);calls=list(state['applications'])
    await engine.close();store.close();store,engine,_=open_runtime(directory,state)
    result=await run(engine,task['id'])
    assert result['task']['status']=='attention_required'
    assert 'immutable application intent' in result['events'][-1]['detail']['message']
    assert len(state['sent'])==sent and len(state['gets'])==gets and state['applications']==calls
    assert store.record_get('knowledge_application',app['id'])==app
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('restart',[False,True])
async def test_fresh_uuid_cannot_recreate_parked_write_but_real_nested_correction_returns(tmp_path,restart):
    state={'correction':True,'clone':True,'nested':'web','same_path_diagnostic':True};directory=tmp_path/'data'
    if restart:state['pause_after_clone']=True
    store,engine,_=open_runtime(directory,state);task=task_for(store,state)
    Path(task['workspace'],'answer.txt').write_bytes(b'hello')  # Existing target is valid read-only diagnostic evidence.
    if restart:
        result=await run(engine,task['id']);assert result['task']['status']=='configuration_required'
        frame=deepcopy(result['task']['state']['web_correction_returns'][-1])
        assert store.get_operation(frame['operation_id'])['operation']['args']['path']=='answer.txt'
        acquired=[deepcopy(w) for w in store.records('web_work') if w.get('learning_applied')]
        applications=deepcopy(store.records('knowledge_application'))
        await engine.close();store.close();store,engine,_=open_runtime(directory,state)
    # This finite nested path includes diagnosis, two correction write/read/resolve
    # cycles and the original write/read/final through real wire and durable storage.
    # Its observed advancing graph exceeded 120 seconds. Keep this test-only hang
    # guard local; unrelated fixtures and all product timeouts remain unchanged.
    await asyncio.wait_for(engine.run_task(task['id']),600);assert_delivered(store,task)
    rejected=[e for e in store.events(task['id']) if e['stage']=='operation_proposal' and e['status']=='rejected_model_output']
    assert any(e['detail']['rejected_response']==state['clone_sent'] and 'PENDING_CORRECTION_DEPENDENCY' in e['detail']['validation']['message'] for e in rejected)
    context=state['clone_context'];parent=next(p for p in context['parent_operations'] if p['operation']['args'].get('path')=='answer.txt')
    assert state['clone_sent']['id']!=parent['operation']['id']
    assert store.get_operation(parent['operation']['id'])['status']=='cycle_complete'
    diagnostic=next(r for r in store.operations(task['id']) if r['operation']['kind']=='file_read'
        and r['operation']['args']['path']=='answer.txt' and r['result']['started_at']<store.get_operation(parent['operation']['id'])['result']['started_at'])
    assert diagnostic['status']=='cycle_complete' and diagnostic['result']['stdout']=='hello'
    assert not engine.web_judgments.correction_frontier(task['id'])
    corrections=store.records('judgment_correction');assert len(corrections)>=2
    for correction in corrections:
        resolution=store.get_operation(correction['resolution_operation_id'])
        assert resolution['status']=='cycle_complete'
        evidence=[store.get_operation(i) for i in resolution['operation']['args']['evidence_operation_ids']]
        assert {'file_write','file_read'}<={r['operation']['kind'] for r in evidence}
        assert all(r['result']['started_at']>=correction['created_at'] for r in evidence)
    assert len(state['applications'])==len(set(state['applications']))
    assert len(state['gets'])==len(store.records('web_exchange'))
    if restart:
        assert store.get_operation(frame['operation_id'])['operation']==next(p['operation'] for p in context['parent_operations'] if p['operation']['id']==frame['operation_id'])
        assert all(store.record_get('knowledge_application',a['id'])==a for a in applications)
        for old in acquired:
            current=store.record_get('web_work',old['id'])
            assert all(current[k]==old[k] for k in ('detail','acquisition_operation','acquisition_result','applied_learning','learning_applied'))
    await engine.close();store.close()
