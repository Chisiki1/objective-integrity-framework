"""Original support-to-work boundaries using real Store/Engine and offline models/HTTP."""
from copy import deepcopy
from pathlib import Path

import pytest

from policy_harness.engine import CycleHeld, RevisionNeeded, WebCorrectionNeeded
from policy_harness.executor import Executor
from policy_harness.models import (Learning, LearningContractError, Operation, OperationResult,
    PolicyError, ResearchQuery, learning_applications_schema, application_evidence_pointer)
from policy_harness.store import digest
from tests.test_core import FixtureGateway, runtime
from tests.test_knowledge import learning, operation as file_operation, seed_skill, observed_application, journal
from tests.test_web_recovery import collector, operation, reopen
from tests.fixture_preparation import prepare_operation


@pytest.mark.asyncio
@pytest.mark.parametrize('shape',[229,232,233,'identity-pointer','missing-explanation'])
async def test_advertised_application_contract_reaches_strict_atomic_consumer(tmp_path,shape):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Write exact temporary hello',['Exact bytes and retained evidence'])
    skill=seed_skill(store,engine.knowledge,task)
    op=file_operation()
    result=await Executor(store.data_dir).execute(Path(task['workspace']),Operation.model_validate(op))
    op,result,valid,selected=observed_application(store,task,skill,op,result.model_dump())
    journal(store,task,op,result,valid,selected)
    payload={'operation':op,'result':result,'pre':{'skills':{'selected':selected}},'required_ideas':[]}
    supplied=engine._model_input(task['id'],'learning_proposal',payload,Learning)['application_contract']
    assert supplied['applications']==learning_applications_schema()
    assert Learning.model_json_schema()['properties']['applications']['items']==supplied['applications']['items']
    assert supplied['operation_sha256']==digest(op) and supplied['result_sha256']==digest(result)
    assert supplied['result_evidence'] and all(application_evidence_pointer(p['pointer']) for p in supplied['result_evidence'])
    assert not any(p['pointer'] in {'/operation_id','/status','/effect'} for p in supplied['result_evidence'])
    bad=deepcopy(valid.model_dump());app=bad['applications'][0]
    if shape==229:
        app.pop('procedure_clause');app['evidence_pointers']=app.pop('evidence');app.update(application_result='observed',observed_state='used')
    elif shape==232:
        for name in ('procedure_clause','operation_id','operation_sha256','result_sha256'):app.pop(name)
        app['application']='claimed'
    elif shape==233:
        app['id']=app.pop('operation_id');app.update(hash=app['skill_hash'],application='claimed')
    elif shape=='identity-pointer':
        app['evidence'][0].update(pointer='/operation_id',sha256=digest(op['id']))
    else:app['evidence'][0].pop('explanation')
    retained=Learning.model_validate(bad)
    assert retained.model_dump()==bad  # Historical dictionaries are not rewritten.
    before=deepcopy(store.records('knowledge_application'));skills_before=deepcopy(store.records('skill'))
    with pytest.raises(LearningContractError):
        engine.knowledge.validate_learning_proposal(task,op,retained,result,[skill['id']])
    row=store.get_operation(op['id']);bundle=dict(row['post_bundle'],learning=bad)
    store.update_operation(op['id'],post_bundle=bundle,post_review={'bundle_hash':digest(bundle),'disposition':{'verdict':'proceed'}})
    with pytest.raises(PolicyError):engine.knowledge.apply(task,op,retained,result,[skill['id']])
    assert store.records('knowledge_application')==before and store.records('skill')==skills_before
    store.update_operation(op['id'],post_bundle=row['post_bundle'],post_review=row['post_review'])
    actual=engine.knowledge.apply(task,op,valid,result,[skill['id']])
    assert actual['observed_application_ids'] and len(store.records('skill_application'))==1
    assert Path(task['workspace'],'answer.txt').read_bytes()==b'hello'
    assert executor.calls==[]  # The awaited real Executor wrote the temporary file above.
    await engine.close();store.close()


class OutcomeGateway(FixtureGateway):
    def __init__(self,policy):super().__init__(policy);self.revise=True
    async def generate(self,role,phase,payload,schema):
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is Learning and phase == 'web_acquisition_learning':
            # This test exercises a material knowledge change. An empty optional
            # learning bundle now correctly avoids a redundant outcome review.
            from tests.test_learning_update_contract import create_update
            value = value.model_copy(update={'skill_updates': [create_update()]})
        if phase=='web-learning-outcome_disposition' and self.revise:
            self.revise=False
            value=value.model_copy(update={'verdict':'revise','rationale':'Fixture requires subsequent evidence for this exact committed outcome.'})
        return value,usage


async def acquired_with_correction(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Acquire and use bounded evidence',['Original result remains exact'])
    engine.gateway=OutcomeGateway(engine.policy);requests=[];web=collector(store,requests);parent=operation()
    store.save_operation(task['id'],parent,policy_hash=engine.policy.hash)
    with pytest.raises(WebCorrectionNeeded):
        await web.collect('https://example.com/a',phase='pre',task_id=task['id'],operation_id=parent['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    after=next(w for w in store.records('web_work') if w['stage']=='after')
    correction,=engine._pending_judgments(task['id'])
    return store,engine,task,parent,after,correction,requests


@pytest.mark.asyncio
async def test_committed_web_review_receives_actual_input_and_typed_history(tmp_path):
    store,engine,task,parent,after,correction,requests=await acquired_with_correction(tmp_path)
    old=deepcopy(store.records('knowledge_application'))
    call_count=len(engine.gateway.calls)
    current=await engine.web_judgments.evaluate(task['id'],parent,'after',after['detail'],after['research_choice'])
    assert current['disposition']['verdict']=='proceed'
    assert [c['review_id'] for c in current['pending_applied_reviews']]==[correction['id']]
    await engine.web_judgments.drain(task['id'])
    assert call_count==27 and len(engine.gateway.calls)==call_count
    retained=store.record_get('web_work',after['id'])
    assert {key:retained[key] for key in after}==after
    assert set(retained)-set(after)=={'application_recovery'}
    application=store.record_get('knowledge_application',task['id']+':'+after['detail']['id'])
    episode=store.record_get('episode',application['outcome']['episode_id'])
    accepted=[i for i,attempt in enumerate(after['learning_attempts'])
              if attempt['learning']==episode['learning'] and attempt.get('disposition',{}).get('verdict')=='proceed']
    assert accepted
    assert retained['application_recovery']=={
        'application_id':application['id'],'input_hash':application['input_hash'],
        'episode_id':episode['id'],'episode_sha256':digest(episode),
        'accepted_attempt_index':accepted[-1],'review_reconstructed':False,'application_replayed':False}
    assert store.records('knowledge_application')==old and len(requests)==1
    calls=[p for _,phase,p in engine.gateway.calls if phase=='web-learning-outcome_choices_after']
    assert calls
    for p in calls:
        assert p['original_operation_result']==after['acquisition_result']
        assert p['actual_result']==after['learning_applied']
        assert p['accepted_input']['learning']==after['applied_learning']
        assert p['accepted_input']['pre']['skills']==after['selection']
        assert p['accepted_input']['parent_operation']==parent
        assert p['history']['pending_operations'][0]['status']=='proposed'
        assert p['history']['pending_operations'][0]['effect'] is None
        assert p['source_context']['pending_ids']==[]
    bundle=next(p['bundle'] for _,phase,p in engine.gateway.calls if phase=='web-learning-outcome_review')
    assert bundle['original_operation_result']==after['acquisition_result'] and bundle['accepted_input']['learning']==after['applied_learning']
    kwargs=dict(context=bundle['accepted_input'],consumer=bundle['consumer'])
    previous=store.records('applied_knowledge_review')[-1]
    repeated=await engine._judge_applied_knowledge(task,after['acquisition_operation'],'web-learning-outcome',after['applied_choices'],
        after['learning_applied'],after['acquisition_result'],{'sources':[]},**kwargs)
    assert repeated==previous
    changed=deepcopy(kwargs);changed['context']['prior_acquisition_evidence'].append({'id':'retained-first-fault','status':'failed','fixture':True})
    renewed=await engine._judge_applied_knowledge(task,after['acquisition_operation'],'web-learning-outcome',after['applied_choices'],
        after['learning_applied'],after['acquisition_result'],{'sources':[]},**changed)
    assert renewed['input_hash']!=previous['input_hash']
    assert store.record_get('judgment_correction',correction['id'])==correction
    assert store.records('knowledge_application')==old and len(requests)==1
    await engine.close();store.close()


def legacy_correction(store,after,correction):
    old=deepcopy(correction);old.pop('consumer');old.pop('hash');old['hash']=digest(old)
    store.record('judgment_correction',old['id'],old)
    applied=next(r for r in store.records('applied_knowledge_review') if r['review']['id']==old['id'])
    applied.pop('consumer');applied.pop('input_contract')
    applied['input_hash']=digest({'choices':after['applied_choices'],'outcome':after['learning_applied'],
        'result':after['acquisition_result'],'web':{'sources':[]},'policy_hash':applied['policy_hash'],'source_hash':applied['source_hash']})
    # Keep the actual review identity and record key; only this fixture's old input-contract shape differs.
    store.record('applied_knowledge_review',applied['id'],applied)
    return old


async def evidence_and_resolution(store,engine,task,correction):
    evidence=Operation(kind='file_write',args={'path':'observed.txt','text':'subsequent owned fixture evidence'},
        purpose='Record required subsequent evidence',expected_result='Actual temporary file',
        decisions=[{'id':'evidence','statement':'Record observed correction evidence','rationale':'Fixture exact correction'}])
    state=await prepare_operation(engine,task,evidence.model_dump())
    for action in (engine._pre,engine._execute,engine._post,engine._learn,engine._close_cycle):await action(state)
    resolution=Operation(kind='judgment_resolve',args={'review_id':correction['id'],'expected_hash':correction['hash'],
        'reason':'Use the subsequent actual fixture evidence without replaying the acquisition.', 'evidence_operation_ids':[evidence.id]},
        purpose='Resolve this exact reviewed Web outcome',expected_result='Return resolution to its original consumer',
        decisions=[{'id':'resolve','statement':'Consume actual reviewed evidence','rationale':'Same owned correction'}])
    return resolution


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy',[False,True])
async def test_web_resolution_returns_after_committed_result_and_restart_without_replay(tmp_path,legacy):
    store,engine,task,parent,after,correction,requests=await acquired_with_correction(tmp_path)
    if legacy:correction=legacy_correction(store,after,correction)
    applications=deepcopy(store.records('knowledge_application'))
    resolution=await evidence_and_resolution(store,engine,task,correction)
    state=await prepare_operation(engine,task,resolution.model_dump())
    for action in (engine._pre,engine._execute,engine._post,engine._learn):await action(state)
    committed=deepcopy(store.record_get('controller_effect',resolution.id))
    assert committed['result']['status']=='succeeded'
    assert not store.records('web_judgment_resolution')
    directory=store.data_dir;policy=engine.policy
    await engine.close();store.close()
    store,engine=reopen(directory,policy)
    def no_replay(*args,**kwargs):raise AssertionError('No dispatch or application during completion recovery')
    engine._dispatch=no_replay;engine.knowledge.apply=no_replay
    await engine._close_cycle(state)
    await engine._close_cycle(state)
    assert store.record_get('controller_effect',resolution.id)==committed
    returned,=store.records('web_judgment_resolution')
    assert returned['consumer']['web_work_id']==after['id']
    assert returned['resolution_operation_id']==resolution.id
    assert store.get_operation(resolution.id)['status']=='cycle_complete'
    assert store.record_get('web_work',after['id'])['judgment_resolutions']==[correction['id']]
    assert all(x in store.records('knowledge_application') for x in applications) and len(requests)==1
    with pytest.raises(KeyError):store.get_operation(after['detail']['id'])
    assert store.verify_events()
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',['foreign','missing','result','application','review','collision'])
async def test_invalid_web_resolution_source_is_held_before_commit(tmp_path,bad):
    store,engine,task,parent,after,correction,requests=await acquired_with_correction(tmp_path)
    resolution=await evidence_and_resolution(store,engine,task,correction)
    if bad=='foreign':
        current=dict(after,task_id=store.create_task('Other task',['No access'])['id']);store.record('web_work',after['id'],current)
    elif bad=='missing':store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('web_work',after['id']))
    elif bad=='result':
        current=deepcopy(after);current['acquisition_result']['data']['bytes']+=1;store.record('web_work',after['id'],current)
    elif bad=='application':
        key=task['id']+':'+after['detail']['id'];app=store.record_get('knowledge_application',key)
        store.record('knowledge_application',key,dict(app,input_hash='0'*64))
    elif bad=='review':
        actual=store.record_get('review',correction['id']);store.record('review',correction['id'],dict(actual,task_id='foreign'))
    else:store.save_operation(task['id'],dict(file_operation(),id=after['detail']['id']))
    with pytest.raises(PolicyError):await engine._describe_operation(task,resolution.model_dump())
    assert store.record_get('judgment_correction',correction['id'])==correction
    assert store.record_get('controller_effect',resolution.id) is None and not store.records('web_judgment_resolution')
    assert len(requests)==1
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition',['revise','hold','source','policy'])
async def test_local_web_revision_preserves_parent_and_actual_source_guards(tmp_path,disposition):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Use one public source',['Keep the original operation'])
    parent=operation();store.save_operation(task['id'],parent,policy_hash=engine.policy.hash)
    requests=[];engine.web=collector(store,requests)
    class LocalGateway(FixtureGateway):
        queries=0;changed=False
        async def generate(self,role,phase,payload,schema):
            if schema is ResearchQuery:
                self.queries+=1
                assert payload['operation']==parent
                if self.queries>1:assert payload['local_revision_feedback']['parent_operation_id']==parent['id']
                return ResearchQuery(query='https://example.com/'+('rejected' if self.queries==1 else 'accepted'),rationale='Fixture public URL',private_data_excluded=True),{'fixture':True}
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='web_acquisition_disposition_before' and payload['acquisition']['url'].endswith('/rejected'):
                if disposition=='source' and not self.changed:
                    self.changed=True;store.append_instruction(task['id'],'Preserve an additional actual source',task['source_hash'])
                elif disposition=='policy' and not self.changed:
                    self.changed=True;engine.policy.hash='f'*64
                value=value.model_copy(update={'verdict':'hold' if disposition=='hold' else 'revise','rationale':'Select the appropriate local source without rewriting the task.'})
            return value,usage
    gateway=LocalGateway(engine.policy);engine.gateway=gateway
    if disposition=='revise':
        result=await engine._research(task['id'],parent,'pre',{'operation':parent})
        assert result['sources'] and len(requests)==1 and requests[0].url.path=='/accepted'
        assert gateway.queries==2
    else:
        with pytest.raises(PolicyError):await engine._research(task['id'],parent,'pre',{'operation':parent})
        assert not requests
    assert store.get_operation(parent['id'])['operation']==parent
    assert store.get_operation(parent['id'])['status']=='proposed'
    assert len(store.operations(task['id']))==1 and not executor.calls
    assert not any(phase=='task_plan' for _,phase,_ in gateway.calls)
    await engine.close();store.close()
