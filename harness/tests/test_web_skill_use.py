"""Actual finite Web/Knowledge consumers with mock HTTP and explicit fixture reasoning."""
from copy import deepcopy

import pytest

from policy_harness.engine import RevisionNeeded
from policy_harness.models import Learning, LearningContractError, PolicyError, Review, SkillSelection
from policy_harness.store import digest
from tests.test_core import FixtureGateway, runtime
from tests.test_web_recovery import collector, operation, reopen, SimulatedProcessLoss


def skills(engine,task):
    applicable=engine.knowledge._new(task,'explicit-offline-source',{
        'title':'HTTPS source guard','content':'Refuse a /blocked URL before network dispatch. Acquire /allowed via HTTPS once and retain the actual response.',
        'applicability':'Public evidence acquisition','next_trigger':'Next public HTTPS exchange'})
    unrelated=engine.knowledge._new(task,'explicit-offline-source',{
        'title':'Image export','content':'Export the requested image with a transparent background.',
        'applicability':'Image editing only','next_trigger':'Next image request'})
    engine.knowledge.rebuild_projections()
    return applicable,unrelated


class WebSkillGateway(FixtureGateway):
    async def generate(self,role,phase,payload,schema):
        if schema is SkillSelection:
            chosen=[];rejected=[]
            for source in payload['knowledge']['skills']:
                if source['title']=='HTTPS source guard':
                    chosen.append({'id':source['id'],'hash':source['hash'],'application':'Screen the actual prepared URL and retain one acquired result.',
                        'reason':'The exact public HTTPS request falls within this source guard procedure.',
                        'procedure_clause':source['content']})
                else:rejected.append({'id':source['id'],'reason':'Image procedure does not apply to HTTP acquisition.'})
            return SkillSelection(selected=chosen,rejected=rejected,new_knowledge_needed=[],rationale='Fixture evaluates exact actual catalog'),{'fixture':True}
        if phase=='web_acquisition_review_before':
            selected=payload['pre']['skills']['selected']
            blocked=bool(selected and payload['acquisition']['url'].endswith('/blocked'))
            return Review(summary='Prepared request checked',opinions=[{'id':'blocked-source','observation':'Do not dispatch this blocked source.',
                'rationale':'Exact selected procedure requires refusal before HTTP.'}] if blocked else []),{'fixture':True}
        value,usage=await super().generate(role,phase,payload,schema)
        if phase=='web_acquisition_disposition_before' and payload['review']['opinions']:
            return value.model_copy(update={'verdict':'hold','rationale':'Apply the selected source guard before HTTP.'}),usage
        if schema is Learning and phase=='web_acquisition_learning':
            applications=[]
            for selected in payload['pre']['skills']['selected']:
                applications.append({'skill_id':selected['id'],'skill_hash':selected['hash'],
                    'procedure_clause':selected['procedure_clause'],'procedure_sha256':selected['procedure_sha256'],
                    'operation_id':payload['operation']['id'],'operation_sha256':digest(payload['operation']),
                    'result_sha256':digest(payload['result']),
                    'evidence':[{'pointer':'/data/url','sha256':digest(payload['result']['data']['url']),
                        'explanation':'The actual mock transport acquired the allowed HTTPS source; this is bounded fixture use evidence.'}]})
            return value.model_copy(update={'classifications':['use'] if applications else ['create'],'applications':applications}),usage
        return value,usage


@pytest.mark.asyncio
async def test_selected_web_procedure_controls_dispatch_and_binds_actual_learning(tmp_path):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Acquire public evidence',['Source content observed'])
    applicable,unrelated=skills(engine,task);engine.gateway=WebSkillGateway(engine.policy)
    requests=[];web=collector(store,requests);op=operation()
    kwargs=dict(phase='pre',task_id=task['id'],operation_id=op['id'],
        evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    with pytest.raises(RevisionNeeded,match='source guard'):
        await web.collect('https://example.com/blocked',**kwargs)
    assert requests==[]
    result=await web.collect('https://example.com/allowed',**kwargs)
    assert len(requests)==1 and requests[0].url.path=='/allowed'
    assert result['sources']
    after=next(x for x in store.records('web_work') if x['stage']=='after')
    assert [s['id'] for s in after['selection']['selected']]==[applicable['id']]
    assert [s['id'] for s in after['selection']['rejected']]==[unrelated['id']]
    episode=store.record_get('episode',after['learning_applied']['episode_id'])
    assert episode['selected_skills']==[applicable['id']]
    application=episode['learning']['applications'][0]
    assert application['operation_sha256']==digest(after['acquisition_operation'])
    assert application['result_sha256']==digest(after['acquisition_result'])
    assert application['evidence'][0]['sha256']==digest('https://example.com/allowed')
    # Neither a modified after-selection nor an unbound procedure may borrow
    # the real result to manufacture an application.
    changed=deepcopy(after);changed['selection']['selected']=[]
    store.record('web_work',after['id'],changed)
    with pytest.raises(LearningContractError,match='before/after source identity'):
        engine.knowledge.validate_learning_proposal(task,after['acquisition_operation'],
            Learning.model_validate(episode['learning']),after['acquisition_result'],[applicable['id']])
    store.record('web_work',after['id'],after)
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_selected_web_commit_recovers_original_input_without_http_or_application_replay(tmp_path,monkeypatch):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Acquire public evidence',['Source content observed'])
    applicable,_=skills(engine,task);engine.gateway=WebSkillGateway(engine.policy)
    requests=[];web=collector(store,requests);op=operation();actual_record=store.record
    def crash(kind,identity,body):
        if kind=='web_work' and body.get('learning_applied'):
            raise SimulatedProcessLoss('Committed actual selected Skill use; response record not saved')
        return actual_record(kind,identity,body)
    monkeypatch.setattr(store,'record',crash)
    with pytest.raises(SimulatedProcessLoss):
        await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    committed=deepcopy(store.records('knowledge_application'));data_dir=store.data_dir;policy=engine.policy
    monkeypatch.setattr(store,'record',actual_record)
    await engine.close();store.close()
    store,engine=reopen(data_dir,policy)
    def no_apply(*args,**kwargs):
        raise AssertionError('Committed selected Web input must not be applied twice')
    monkeypatch.setattr(engine.knowledge,'apply',no_apply)
    await engine.web_judgments.drain(task['id'])
    assert len(requests)==1 and store.records('knowledge_application')==committed
    after=next(x for x in store.records('web_work') if x['stage']=='after')
    assert after['application_recovery']['application_replayed'] is False
    assert [s['id'] for s in after['selection']['selected']]==[applicable['id']]
    assert after['status']=='complete'
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['revise','retire'])
async def test_web_selection_changed_during_review_is_held_before_dns_and_http(tmp_path,change):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Acquire public evidence',['Source content observed'])
    applicable,_=skills(engine,task);requests=[];dns=[];web=collector(store,requests);op=operation()
    resolver=web._resolver
    async def counted(host):dns.append(host);return await resolver(host)
    web._resolver=counted
    class ChangingGateway(WebSkillGateway):
        changed=False
        async def generate(self,role,phase,payload,schema):
            value,usage=await super().generate(role,phase,payload,schema)
            if phase=='web_acquisition_disposition_before' and not self.changed:
                self.changed=True;current=store.record_get('skill',applicable['id'])
                if change=='retire':current['status']='retired'
                else:current.update(content=current['content']+' Retain transport status.',hash='f'*64)
                store.record('skill',current['id'],current)
            return value,usage
    engine.gateway=ChangingGateway(engine.policy)
    with pytest.raises(RevisionNeeded,match='Selected Web Skill changed'):
        await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    assert requests==[] and dns==[]
    assert store.records('web_exchange')[0]['stage']=='prepared'
    assert not store.records('knowledge_application')
    before=deepcopy(next(x for x in store.records('web_work') if x['stage']=='before'))
    exchange=deepcopy(store.records('web_exchange')[0])
    data_dir=store.data_dir;policy=engine.policy
    await engine.close();store.close()
    store,engine=reopen(data_dir,policy,gateway=WebSkillGateway(policy))
    web=collector(store,requests);resolver=web._resolver
    async def resumed_dns(host):dns.append(host);return await resolver(host)
    web._resolver=resumed_dns
    result=await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
        evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    assert result['sources'] and len(requests)==1 and len(dns)==1
    assert any(x==before for x in store.records('web_work_history'))
    current=store.records('web_exchange')[0]
    assert current['id']==exchange['id'] and current['record']['id']==exchange['record']['id']
    assert current['stage']=='response'
    after=next(x for x in store.records('web_work') if x['stage']=='after')
    selected=after['selection']['selected']
    assert len(selected)==(0 if change=='retire' else 1)
    if selected:assert selected[0]['hash']=='f'*64
    assert after['status']=='complete' and len(store.records('knowledge_application'))==1
    # Repeat the same acquisition from its stored bytes; do not reselect, send,
    # or apply the original knowledge input a second time.
    applications=deepcopy(store.records('knowledge_application'))
    await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
        evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    assert len(requests)==1 and len(dns)==1
    assert store.records('knowledge_application')==applications
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('committed',[False,True])
@pytest.mark.parametrize('change',['source','policy'])
async def test_selected_web_recovery_rejudges_current_source_without_reselecting_or_refetching(tmp_path,monkeypatch,committed,change):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Acquire public evidence',['Source content observed'])
    applicable,_=skills(engine,task);engine.gateway=WebSkillGateway(engine.policy)
    requests=[];web=collector(store,requests);op=operation();record=store.record;apply=engine.knowledge.apply
    def lose_record(kind,identity,body):
        if kind=='web_work' and body.get('learning_applied'):raise SimulatedProcessLoss('Commit exists before receipt save')
        return record(kind,identity,body)
    def lose_before_commit(*args,**kwargs):raise SimulatedProcessLoss('Accepted input saved before application')
    if committed:monkeypatch.setattr(store,'record',lose_record)
    else:monkeypatch.setattr(engine.knowledge,'apply',lose_before_commit)
    with pytest.raises(SimulatedProcessLoss):
        await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
    monkeypatch.setattr(store,'record',record);monkeypatch.setattr(engine.knowledge,'apply',apply)
    before=next(x for x in store.records('web_work') if x['stage']=='before')
    original_selection=deepcopy(before['selection']);applications=deepcopy(store.records('knowledge_application'))
    data_dir=store.data_dir;policy=engine.policy;await engine.close();store.close()
    class RecoveryGateway(WebSkillGateway):
        new_learning=0
        async def generate(self,role,phase,payload,schema):
            if schema is SkillSelection:raise AssertionError('Acquired responses retain the original selected version')
            if phase=='web_acquisition_learning':
                assert not committed,'Committed Learning must not be recreated'
                self.new_learning+=1
            return await super().generate(role,phase,payload,schema)
    gateway=RecoveryGateway(policy);store,engine=reopen(data_dir,policy,gateway=gateway)
    if change=='source':store.append_instruction(task['id'],'Retain the original transport status.',task['source_hash'])
    else:engine.policy.hash='e'*64
    await engine.web_judgments.drain(task['id'])
    after=next(x for x in store.records('web_work') if x['stage']=='after')
    assert len(requests)==1 and after['status']=='complete'
    assert after['selection']==original_selection and after['selection_binding']==before['selection_binding']
    assert len(store.records('knowledge_application'))==1
    if committed:assert store.records('knowledge_application')==applications
    else:assert gateway.new_learning==1
    episode=store.record_get('episode',after['learning_applied']['episode_id'])
    assert episode['selected_skills']==[applicable['id']]
    assert episode['learning']['applications'][0]['skill_hash']==original_selection['selected'][0]['hash']
    assert store.records('web_work_history')
    applications=deepcopy(store.records('knowledge_application'))
    completed_after=deepcopy(after)
    await engine.close();store.close()
    store,engine=reopen(data_dir,policy)
    await engine.web_judgments.drain(task['id'])
    assert len(requests)==1 and store.records('knowledge_application')==applications
    after=next(x for x in store.records('web_work') if x['stage']=='after')
    assert after==completed_after
    if committed:
        assert after['application_recovery']['application_replayed'] is False
    assert [s['id'] for s in after['selection']['selected']]==[applicable['id']]
    assert after['status']=='complete'
    await engine.close();store.close()
