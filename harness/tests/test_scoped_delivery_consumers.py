"""Existing delivery consumers under U34; models and HTTP are explicit fixtures.

These cases verify routing and preservation, not real model semantic quality,
full-policy compliance, provider latency or production artifact completion.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from policy_harness import models as m
from policy_harness.store import digest
from tests.test_core import FixtureGateway, runtime
from tests.test_knowledge import journal, learning, operation, seed_idea, seed_skill, observed_application
from tests.test_web_correction_flow import CorrectionGateway, open_runtime, original_record, run


def deferred_idea(identity='optional-formatting'):
    return {'id':identity,'target':'workflow','proposal':'Consider additional display formatting in a later task',
        'disposition':'investigate','rationale':'The exact requested bytes, readback and mandatory review do not depend on this formatting change.',
        'next_operation':{'defer':{'required_for_current_completion':False,
            'current_completion_impact':'All current file, evidence and review criteria remain achievable without changing formatting.',
            'next_trigger':'A later user task explicitly requests richer formatting.'}}}


class OptionalGateway(FixtureGateway):
    async def generate(self,role,phase,payload,schema):
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is m.Assessment:
            value=value.model_copy(update={'ideas':[m.Idea.model_validate(deferred_idea())]})
        return value,usage


@pytest.mark.asyncio
@pytest.mark.parametrize('required',[False,True])
async def test_paged_learning_preserves_actual_idea_disposition_at_shared_validator(tmp_path,required):
    schema=m.LearningApplications
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Preserve required output',['Observe exact output'])
    op=operation('file_read',args={'path':'answer.txt'})
    result=m.OperationResult(operation_id=op['id'],status='succeeded',data={'text':'hello'}).model_dump()
    idea=deferred_idea()
    idea['next_operation']['defer']['required_for_current_completion']=required
    value=m.LearningApplications(considered_skill_ids=[],applications=[],ideas=[idea])
    payload={'operation':op,'result':result,'pre':{'skills':{'selected':[]}},'required_ideas':[]}
    try:
        if required:
            with pytest.raises(m.PolicyError,match="Optional deferral needs"):
                engine._validate_generated(schema,value,payload,task_id=task['id'])
        else:
            engine._validate_generated(schema,value,payload,task_id=task['id'])
        assert not store.records('knowledge_application')
        assert store.get_task(task['id'])['status']!='completed'
    finally:await engine.close();store.close()


class RequiredChangeGateway(CorrectionGateway):
    async def generate(self,role,phase,payload,schema):
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is m.Learning and phase.startswith('web_acquisition_learning') and not self.response_store.records('skill'):
            value=value.model_copy(update={'skill_updates':[{'action':'create','title':'Explicit fixture procedure',
                'content':'Retain the observed original response.', 'applicability':'This exact mock source',
                'next_trigger':'A matching source is requested.'}]})
        return value,usage


@pytest.mark.asyncio
async def test_reasoned_optional_consideration_reaches_normal_artifact_and_finish(tmp_path):
    store,engine,executor,_=runtime(tmp_path)
    engine.policy.role_scoped_enabled=True  # Policy approval/version is tested separately.
    engine.gateway=OptionalGateway(engine.policy);engine.gateway.response_store=store
    task=store.create_task('Write hello in answer.txt',['answer.txt contains hello'])
    try:
        outcome=await engine.run_task(task['id'])
        assert outcome['task']['status']=='completed',outcome['events'][-1]
        assert (Path(task['workspace'])/'answer.txt').read_text()=='hello'
        assert [x['kind'] for x in executor.calls]==['file_write','file_read']
        assert not store.records('skill') and not store.records('knowledge_candidate')
        ideas=store.records('idea')
        assert ideas and all(i['status']=='deferred' and i['deferral']['reason'] for i in ideas)
        assert all(i['deferral_acceptance_hash']==digest(store.get_task(task['id'])['acceptance']) for i in ideas)
        episodes=store.records('episode')
        assert episodes and all(e['learning']['classifications'] for e in episodes)
        learned=[e for e in episodes if 'next_use_trigger' in e['learning']]
        assert learned and all(e['learning']['outcome_summary'] and e['learning']['next_use_trigger'] for e in learned)
        assert store.records('independent_review') and store.verify_events()
        assert not store.records('applied_knowledge_review')
        assert not any(phase.startswith(('learning-outcome','web-learning-outcome')) for _,phase,_ in engine.gateway.calls)
        after=[w for w in store.records('web_work') if w['stage']=='after']
        assert after and all(w['status']=='complete' and not w.get('applied_review_id') for w in after)
        assert all(w['learning_applied'] and w['learning_attempts'][-1]['disposition']['verdict']=='proceed' for w in after)
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_proposal_receives_authenticated_originals_and_shared_evidence_eligibility(tmp_path):
    requests=[]
    store,engine,_,gateway=open_runtime(tmp_path/'data',requests,pause='proposal',gateway_class=RequiredChangeGateway)
    engine.policy.role_scoped_enabled=True
    task=store.create_task('Write exact hello',['answer.txt contains hello'])
    try:
        paused=await run(engine,task['id'])
        assert paused['task']['status']=='configuration_required',paused['events'][-1]
        correction,=engine._pending_judgments(task['id'])
        _,after=original_record(store,task['id'])
        apps=deepcopy(store.records('knowledge_application'))
        source=engine._judgment_source(store.get_task(task['id']),correction)
        proposal=next(p for _,phase,p in gateway.calls if phase=='operation_proposal')
        context=proposal['web_correction_context'];binding,=context['correction_inputs']
        assert context['original_sources']=={digest(source):source}
        assert binding['original_source_key']==digest(source)
        assert source['learning']==after['applied_learning'] and source['selection']==after['selection']
        assert source['result']==after['acquisition_result']
        assert context['evidence_contract']['requires_primary_artifact_completion'] is False
        assert context['evidence_contract']['requires_specific_operation_kind'] is False
        # A fixture observed diagnostic is structurally eligible; it is not
        # fabricated semantic proof that the original objection is resolved.
        diagnostic=operation('file_list',args={})
        result=m.OperationResult(operation_id=diagnostic['id'],status='succeeded',data={'entries':[]}).model_dump()
        journal(store,task,diagnostic,result,learning())
        task=store.get_task(task['id'])
        current=engine._correction_context(task)
        eligible=current['correction_inputs'][0]['eligible_evidence']
        assert diagnostic['id'] in {e['operation_id'] for e in eligible}
        resolve=operation('judgment_resolve',args={'review_id':correction['id'],'expected_hash':correction['hash'],
            'reason':'Submit the exact original and observed diagnostic for ordinary semantic review.',
            'evidence_operation_ids':[diagnostic['id']]})
        described=await engine._describe_operation(task,resolve)
        assert described['original_source']==source
        assert described['evidence'][0]['result']==result
        store.update_operation(diagnostic['id'],result=dict(result,effect='unknown'))
        assert diagnostic['id'] not in {e['operation_id'] for e in engine._correction_context(task)['correction_inputs'][0]['eligible_evidence']}
        with pytest.raises(m.PolicyError,match='subsequent owned evidence'):
            await engine._describe_operation(task,resolve)
        assert store.records('knowledge_application')==apps and not (Path(task['workspace'])/'answer.txt').exists()
        assert [r['path'] for r in requests]==['/first']
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_existing_idea_deferral_has_original_review_input_and_preserves_history(tmp_path):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Complete current required output',['Exact output and review'])
    idea=seed_idea(store,engine.knowledge,task)
    decision={'id':idea['id'],'disposition':'defer','reason':'This proposed optional check is unnecessary for the current verified output.',
        'deferral':deferred_idea()['next_operation']['defer']}
    op=operation('knowledge_update',args={'decisions':[decision]})
    try:
        description=await engine._describe_operation(task,op)
        assert description['ideas']==[idea] and description['mandatory_acceptance']==task['acceptance']
        result=engine.knowledge.resolve_ideas(task['id'],[decision],op['id'])
        assert engine.knowledge.resolve_ideas(task['id'],[decision],op['id'])==result
        current=store.record_get('idea',idea['id'])
        assert current['status']=='deferred' and current['proposal']==idea['proposal']
        assert idea in store.records('idea_history')
        assert not engine.knowledge.deferred_for_planning(task)['candidates']
        # New requirements restore current applicability review without
        # deleting the old reasoned disposition or rewriting its source.
        store.update_task(task['id'],acceptance=[*task['acceptance'],'New relevant requirement'])
        assert any(x['idea_id']==idea['id'] for x in engine.knowledge.deferred_for_planning(task)['candidates'])
        assert engine.knowledge.finish(task['id'],[],True)['unverified_effects_explicit']
        assert store.get_task(task['id'])['status']!='completed'
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['unclassified','unclassified-task','required','missing-trigger','executed','unknown'])
async def test_deferral_cannot_hide_open_mandatory_or_adopted_effects(tmp_path,boundary):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Keep mandatory verification',['Observe the exact required output'])
    idea=seed_idea(store,engine.knowledge,task)
    if boundary=='unclassified-task':
        idea=dict(idea,target='task');store.record('idea',idea['id'],idea)
    decision={'id':idea['id'],'disposition':'defer','reason':'A reason does not waive required work.',
        'deferral':deepcopy(deferred_idea()['next_operation']['defer'])}
    if boundary=='required':decision['deferral']['required_for_current_completion']=True
    elif boundary=='missing-trigger':decision['deferral']['next_trigger']=''
    elif boundary in {'executed','unknown'}:
        op=operation('file_list',args={},improvement_bindings=[{'idea_id':idea['id']}])
        result=m.OperationResult(operation_id=op['id'],status='unknown' if boundary=='unknown' else 'succeeded',
            effect='unknown' if boundary=='unknown' else 'confirmed').model_dump()
        journal(store,task,op,result,learning())
    try:
        if boundary not in {'unclassified','unclassified-task'}:
            with pytest.raises(m.PolicyError):
                engine.knowledge.resolve_ideas(task['id'],[decision],'fixture-deferral')
        with pytest.raises(m.PolicyError,match='Unresolved improvements'):
            engine.knowledge.finish(task['id'],[],True)
        assert store.record_get('idea',idea['id'])==idea
        assert not store.records('idea_resolution')
        assert store.get_task(task['id'])['status']!='completed'
        if boundary=='unclassified-task':
            assert engine._mandatory_child_work(task)['task_ideas']==[idea['id']]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_u34_keeps_explicit_skill_update_and_reuses_old_application(tmp_path):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Retain adopted learning',['Keep real changes and their limitations'])
    op=operation('file_list',args={});result=m.OperationResult(operation_id=op['id'],status='succeeded').model_dump()
    learned=learning(skill_updates=[{'action':'create','title':'Explicit reviewed candidate','content':'Exact retained procedure.',
        'applicability':'This fixture only','next_trigger':'Next actual matching use'}])
    try:
        first=engine.knowledge.apply(task,op,learned,result,[])
        saved=deepcopy(store.records('knowledge_application'));skills=deepcopy(store.records('skill'))
        assert len(skills)==1 and skills[0]['needs_cleanup'] and skills[0]['effect']=='UNUSED_EFFECT_UNVERIFIED'
        assert engine.knowledge.apply(task,op,learned,result,[])==first
        assert store.records('knowledge_application')==saved and store.records('skill')==skills
        with pytest.raises(m.PolicyError,match='every created/updated Skill'):
            engine.knowledge.finish(task['id'],[],True)
    finally:await engine.close();store.close()


async def accepted_record_only(store,engine,task,*,unknown=False,updates=None,use=False,ideas=None):
    op=operation('file_list',args={})
    result=m.OperationResult(operation_id=op['id'],status='unknown' if unknown else 'succeeded',
        effect='unknown' if unknown else 'none').model_dump()
    learned=learning(skill_updates=updates or [],ideas=[deferred_idea()])
    selected=[]
    if use:
        skill=seed_skill(store,engine.knowledge,task)
        op=operation()
        result=(await engine.executor.execute(Path(task['workspace']),m.Operation.model_validate(op))).model_dump()
        op,result,learned,selected=observed_application(store,task,skill,op,result,
            updates=[{'action':'use','id':skill['id']}])
        learned=learned.model_copy(update={'ideas':[m.Idea.model_validate(i) for i in (ideas or [deferred_idea()])]})
    journal(store,task,op,result,learned,selected)
    store.update_operation(op['id'],policy_hash=engine.policy.hash)
    row=store.get_operation(op['id'])
    reviewed=await engine._review_bundle(task['id'],op,'post',row['post_bundle'],{'sources':[]})
    store.update_operation(op['id'],post_review=reviewed)
    outcome=engine.knowledge.apply(task,op,learned,result,[s['id'] for s in selected])
    app=store.record_get('knowledge_application',task['id']+':'+op['id'])
    context={'learning':learned.model_dump(),'selection':row['pre_bundle']['skills'],
        'accepted_learning_review':reviewed,'accepted_learning_disposition':reviewed['disposition'],
        'accepted_learning_provenance':{'bundle_sha256':digest(row['post_bundle']),
            'learning_sha256':digest(learned.model_dump()),'review_id':reviewed['id'],
            'application_id':app['id'],'application_input_hash':app['input_hash']}}
    consumer={'kind':'operation','operation_id':op['id'],'operation_sha256':digest(op)}
    return (task,op,'learning-outcome',outcome,result,context,consumer)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['skill-update','skill-use','child-export','unknown','partial','capture','existing-review'])
async def test_record_only_shortcut_preserves_effect_and_saved_judgment_routes(tmp_path,boundary,monkeypatch):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Retain accepted work',['Preserve effects and exact existing judgments'])
    update={'action':'create','title':'Required explicit update','content':'Original accepted procedure',
        'applicability':'Current fixture','next_trigger':'Next actual use'}
    if boundary=='partial':
        monkeypatch.setattr(engine.knowledge,'rebuild_projections',lambda:{'repaired':False,'conflicts':['fixture projection fault']})
    try:
        args=await accepted_record_only(store,engine,task,unknown=boundary=='unknown',
            updates=[update] if boundary=='skill-update' else None)
        task,op,phase,outcome,result,context,consumer=args
        if boundary=='skill-use':
            # Even a malformed asserted use must reach its ordinary validation,
            # never the record-only route; real bound-use cases live in test_knowledge.
            context['learning']['applications']=[{'skill_id':'not-an-observed-use'}]
        elif boundary=='child-export':
            outcome['candidate_ids']=['pending-parent-integration']
        elif boundary=='capture':
            from policy_harness import semantic_wire
            semantic_wire.capture(store,task,'reviewer',phase+'_review',
                {'operation':op,'bundle':{'accepted_input':context,'actual_result':outcome},'web':{'sources':[]}},m.Review)
        elif boundary=='existing-review':
            engine.policy.role_scoped_enabled=False
            await engine._judge_applied_knowledge(task,op,phase,[],outcome,result,{'sources':[]},context=context,consumer=consumer)
            engine.policy.role_scoped_enabled=True
        captures=deepcopy(store.records('semantic_wire_request'));reviews=deepcopy(store.records('applied_knowledge_review'))
        assert engine._record_only_learning(*args) is None
        assert store.records('semantic_wire_request')==captures and store.records('applied_knowledge_review')==reviews
        assert store.get_task(task['id'])['status']!='completed'
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('target',['episode','review','idea'])
async def test_record_only_confirmation_rejects_changed_originals(tmp_path,target):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Keep exact accepted storage',['No forged acceptance or lost original'])
    try:
        args=await accepted_record_only(store,engine,task)
        confirmed=engine._record_only_learning(*args)
        assert confirmed and 'review' not in confirmed and 'verdict' not in confirmed
        task,op,phase,outcome,result,context,consumer=args
        if target=='episode':
            row=store.record_get('episode',outcome['episode_id']);row['result']=dict(row['result'],stdout='changed')
            store.record('episode',row['id'],row)
        elif target=='review':
            row=store.record_get('review',context['accepted_learning_review']['id']);row['bundle_hash']='0'*64
            store.record('review',row['id'],row)
        else:
            row=store.records('idea')[0];row['proposal']='lost original'
            store.record('idea',row['id'],row)
        with pytest.raises(m.PolicyError,match='Control Learning'):
            engine._record_only_learning(*args)
        assert not store.records('applied_knowledge_review') and store.get_task(task['id'])['status']!='completed'
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_reviewed_file_use_confirms_storage_without_another_model_cycle(tmp_path):
    store,engine,executor,gateway=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Write exact fixture content',['Read actual file evidence'])
    try:
        args=await accepted_record_only(store,engine,task,use=True)
        task,op,phase,outcome,result,context,consumer=args
        calls=len(gateway.calls);applications=deepcopy(store.records('knowledge_application'))
        confirmed=await engine._judge_applied_knowledge(task,op,phase,[],outcome,result,{'sources':[]},context=context,consumer=consumer)
        assert confirmed['record_only']['application_id']==task['id']+':'+op['id']
        assert len(gateway.calls)==calls and store.records('knowledge_application')==applications
        assert not store.records('applied_knowledge_review')
        skill_id=outcome['observed_application_ids'][0]
        skill=store.record_get('skill',skill_id)
        assert len(skill['uses'])==1 and skill['uses'][0]['effect']=='UNVERIFIED'
        assert skill['needs_cleanup'] and store.get_task(task['id'])['status']!='completed'
        assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'hello'
        # A subsequent real use changes the current version, not the original
        # committed result. Historical confirmation must remain applicable.
        later=deepcopy(skill);later['sources'].append('later-source');later['revision']+=1
        engine.knowledge._save_skill(later)
        assert engine._record_only_learning(*args)==confirmed['record_only']
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_use_confirmation_keeps_adopted_but_unimplemented_ideas_pending(tmp_path):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Retain pending actual duties',['Do not infer execution from storage'])
    idea={'id':'annotation-needed','target':'workflow','proposal':'Add the accepted factual annotation to the procedure',
        'disposition':'adopt','rationale':'The annotation remains a separate unperformed content change.'}
    try:
        args=await accepted_record_only(store,engine,task,use=True,ideas=[idea])
        assert engine._record_only_learning(*args)
        stored=next(i for i in store.records('idea') if i['proposal']==idea['proposal'])
        assert stored['status']=='pending'
        assert stored['implementation_refs']==stored['verification_refs']==stored['actual_use_refs']==[]
        assert store.get_task(task['id'])['status']!='completed'
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('target',['proof','content','family'])
async def test_use_confirmation_rejects_missing_proof_or_unreviewed_content(tmp_path,target):
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Keep reviewed use bound',['No unreviewed procedure changes'])
    try:
        args=await accepted_record_only(store,engine,task,use=True)
        assert engine._record_only_learning(*args)
        task,op,phase,outcome,result,context,consumer=args
        if target=='proof':
            application=context['learning']['applications'][0]
            key=digest({'task':task['id'],'application':application})
            proof=store.record_get('skill_application',key);proof['effect']='VERIFIED'
            store.record('skill_application',key,proof)
        elif target=='family':
            key=digest({'kind':op['kind'],'classification':sorted(context['learning']['classifications'])})[:24]
            family=store.record_get('global_family',key);family['episode_refs']=[]
            store.record('global_family',key,family)
        else:
            # A internally hash-consistent but meaning-changing after-record is
            # still not the deterministic accepted use append.
            entry=outcome['skill_transitions']['entries'][0]
            skill=store.record_get('skill',entry['skill_id']);skill['content']='An unreviewed replacement procedure'
            skill['hash']=digest({k:v for k,v in skill.items() if k!='hash'})
            store.record('skill',skill['id'],skill)
            entry['after']={'hash':skill['hash'],'record_sha256':digest(skill)}
            key=task['id']+':'+op['id']
            application=store.record_get('knowledge_application',key);application['outcome']=deepcopy(outcome)
            projected=store.record_get('knowledge_projection_outcome',key)
            projected.update(outcome=deepcopy(outcome),semantic_outcome={k:v for k,v in outcome.items() if k!='projection_status'})
            store.record('knowledge_application',key,application);store.record('knowledge_projection_outcome',key,projected)
        with pytest.raises(m.PolicyError,match='Control Learning'):
            engine._record_only_learning(*args)
        assert not store.records('applied_knowledge_review')
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted',[False,True])
async def test_web_use_storage_reaches_consumer_without_replaying_http_or_reviews(tmp_path,monkeypatch,interrupted):
    from tests.test_web_skill_use import WebSkillGateway,skills
    from tests.test_web_recovery import collector,reopen,SimulatedProcessLoss
    store,engine,_,_=runtime(tmp_path);engine.policy.role_scoped_enabled=True
    task=store.create_task('Acquire evidence',['Keep the actual source and uncertainty'])
    applicable,_=skills(engine,task);engine.gateway=WebSkillGateway(engine.policy)
    requests=[];web=collector(store,requests);op=operation('file_list',args={})
    record=store.record
    def interrupt(kind,identity,body):
        if interrupted and kind=='web_work' and body.get('learning_applied'):
            raise SimulatedProcessLoss('Use application committed before consumer handoff')
        return record(kind,identity,body)
    monkeypatch.setattr(store,'record',interrupt)
    try:
        async def acquire():
            return await web.collect('https://example.com/allowed',phase='pre',task_id=task['id'],operation_id=op['id'],
                evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],op,stage,detail))
        if interrupted:
            with pytest.raises(SimulatedProcessLoss):await acquire()
        else:await acquire()
        committed=deepcopy(store.records('knowledge_application'));data_dir=store.data_dir;policy=engine.policy
        monkeypatch.setattr(store,'record',record)
        await engine.close();store.close()
        store,engine=reopen(data_dir,policy)
        calls=len(engine.gateway.calls)
        await engine.web_judgments.drain(task['id'])
        assert len(requests)==1 and store.records('knowledge_application')==committed
        assert len(engine.gateway.calls)==calls
        after=next(w for w in store.records('web_work') if w['stage']=='after')
        assert after['status']=='complete' and not after.get('applied_review_id')
        assert not store.records('applied_knowledge_review')
        skill=store.record_get('skill',applicable['id'])
        assert len(skill['uses'])==1 and skill['uses'][0]['effect']=='UNVERIFIED'
    finally:await engine.close();store.close()
