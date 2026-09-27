"""Actual packed HTTP responses and Store/Engine recovery, with synthetic judgments.

These fixtures do not claim live model quality. Old-state setup is explicit;
no missing request or response evidence is invented by a recovery helper.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from policy_harness import models as m
from policy_harness.bounded_judgments import ContextObservation
from policy_harness.disposition_contracts import recurrence
from policy_harness.providers import ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import operation, seed_skill
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_semantic_wire_contract import install, assert_wire_records


def context(identity='pending'):
    return {'source_records':[{'id':identity,'value':'Actual retained source '+identity}]}


async def invoke(engine,task,source,*,direct=False,role=None):
    caller=engine._call if direct else engine.bounded_judgments._call
    return await caller(task['id'],'wire-recovery-context',source,ContextObservation,role=role)


def rejected(store,task):
    return [e for e in store.events(task['id']) if e['status']=='rejected_model_output']


def failed_record(store):
    return next(r for r in store.records('bounded_model_call') if r.get('feedback_schema')=='ContextObservation'
                and r['status'] in {'failed','unobserved'})


def failure_mutator(engine,task,interrupt,*,schema=ContextObservation):
    count=0
    def mutate(call,number):
        nonlocal count
        if call['schema'] is not schema:return None
        if schema is ContextObservation and call['canonical']['source_records'][0]['id']!='pending':return None
        count+=1
        if count==1:
            if interrupt=='stop':engine.stop_requested.add(task['id'])
            field='rationale' if schema is m.EngineeringAdmission else 'summary'
            return dict(call['output'],**{field:17})
        if interrupt=='unknown':raise httpx.ReadTimeout('Synthetic response unobserved')
        return httpx.Response(500,json={'error':'Observed synthetic failed corrected request'})
    return mutate


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['context','disposition'])
@pytest.mark.parametrize('fault',['http500','unknown','changed_response'])
async def test_initial_failed_send_resumes_only_with_authenticated_observed_response(tmp_path,kind,fault):
    from tests.test_disposition_contract_recovery import payload as disposition_payload
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Resume the retained initial request',['No unknown or completed request replay'])
    schema=ContextObservation if kind=='context' else m.Disposition
    source=context() if kind=='context' else disposition_payload(1)
    phase='initial-response-recovery'
    def fail(call,number):
        if fault=='unknown':raise httpx.ReadTimeout('Synthetic initial response unobserved')
        return httpx.Response(500,json={'error':'Observed failure before any format correction'})
    first=install(store,engine,mutate=fail)
    with pytest.raises(ProviderError):
        await engine._call(task['id'],phase,source,schema)
    old=deepcopy(store.records('bounded_model_call'));events=deepcopy(store.events(task['id']))
    assert len(first.calls)==1 and len(old)==1 and rejected(store,task)==[]
    trace=old[0]['feedback_trace' if kind=='context' else 'disposition_trace']
    expected=trace['request_model_input']
    assert expected==trace['root_model_input'] and 'actual_format_feedback' not in expected
    if fault=='changed_response':
        response,=store.records('model_response')
        store.record('model_response',response['id'],dict(response,http_status=200))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        if fault=='http500':
            value=await engine.bounded_judgments._call(task['id'],phase,source,schema)
            assert len(second.calls)==1 and second.calls[0]['canonical']==expected
            linked=next(r for r in store.records('bounded_model_call') if r.get('parent',{}).get('id')==old[0]['id'])
            assert linked['status']=='succeeded' and linked['measurement']==old[0]['measurement']
            assert await engine._call(task['id'],phase,source,schema)==value
            assert len(second.calls)==1
        else:
            with pytest.raises(m.PolicyError,match='PROVENANCE'):
                await engine.bounded_judgments._call(task['id'],phase,source,schema)
            assert second.calls==[] and store.records('bounded_model_call')==old
        assert store.events(task['id'])[:len(events)]==events
        assert store.record_get('bounded_model_call',old[0]['id'])==old[0]
        assert executor.calls==[] and store.records('web_exchange')==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('direct,interrupt',[(True,'http500'),(False,'http500'),(False,'stop')])
async def test_generic_direct_and_bounded_resume_exact_correction_and_keep_successful_sibling(tmp_path,direct,interrupt):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Retain one exact source judgment',['No completed work is repeated'])
    first=install(store,engine,mutate=failure_mutator(engine,task,interrupt))
    sibling=await invoke(engine,task,context('completed'),direct=direct)
    expected_error=asyncio.CancelledError if interrupt=='stop' else ProviderError
    with pytest.raises(expected_error):await invoke(engine,task,context(),direct=direct)
    old=deepcopy(store.records('bounded_model_call'));events=deepcopy(store.events(task['id']))
    failed=failed_record(store);expected=failed['feedback_trace']['next_model_input']
    assert len(first.calls)==(2 if interrupt=='stop' else 3)
    assert failed['status']==('unobserved' if interrupt=='stop' else 'failed')
    assert failed['measurement']['model_input_sha256']==digest(failed['feedback_trace']['root_model_input'])
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        value=await invoke(engine,task,context(),direct=not direct)
        assert value.coverage_ids==['pending'] and len(second.calls)==1
        assert second.calls[0]['canonical']==expected
        assert second.calls[0]['canonical']['actual_format_feedback']['validation']
        completed=next(r for r in store.records('bounded_model_call') if r.get('parent',{}).get('id')==failed['id'])
        assert completed['feedback_trace']['request_model_input']==expected
        assert completed['feedback_trace']['request_measurement']['messages_sha256']==digest(second.calls[0]['messages'])
        assert completed['measurement']==failed['measurement']  # initial request stays initial
        assert store.events(task['id'])[:len(events)]==events
        for record in old:assert store.record_get('bounded_model_call',record['id'])==record
        snapshot=deepcopy(store.records('bounded_model_call'))
        assert await invoke(engine,task,context('completed'),direct=not direct)==sibling
        assert await invoke(engine,task,context(),direct=direct)==value
        assert len(second.calls)==1 and store.records('bounded_model_call')==snapshot
        assert executor.calls==[] and store.records('web_exchange')==[] and store.records('knowledge_application')==[]
        assert_wire_records(store,first.calls+second.calls)
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_normal_skill_selection_changing_wrong_counts_is_one_durable_defect(tmp_path):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Select only useful actual procedures',['Preserve every candidate'])
    skill=seed_skill(store,engine.knowledge,task);op=operation('file_list',args={})
    knowledge=deepcopy(store.records('knowledge_application'))
    def wrong(call,number):
        assert call['schema'] is m.SkillSelection
        return dict(call['output'],decisions=call['output']['decisions']+[call['output']['decisions'][0]]*number)
    first=install(store,engine,mutate=wrong)
    with pytest.raises(m.PolicyError,match='repeated'):
        await engine._select_skills(store.get_task(task['id']),op)
    assert len(first.calls)==2
    diagnostics=[e['detail']['metadata']['validation_diagnostic'] for e in rejected(store,task)]
    assert diagnostics[0]!=diagnostics[1]
    assert recurrence(diagnostics[0],schema=m.SkillSelection)==recurrence(diagnostics[1],schema=m.SkillSelection)
    calls=deepcopy(store.records('bounded_model_call'));responses=deepcopy(store.records('model_response'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        with pytest.raises(m.PolicyError,match='repeated'):
            await engine._select_skills(store.get_task(task['id']),op)
        assert second.calls==[] and store.records('bounded_model_call')==calls
        assert store.records('model_response')==responses and store.record_get('skill',skill['id'])==skill
        assert executor.calls==[] and store.records('knowledge_application')==knowledge
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_distinct_wire_fields_receive_distinct_feedback_and_then_reuse_the_return(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Keep separate actual defects',['One valid return'])
    def different(call,number):
        if number==1:return dict(call['output'],summary=31)
        if number==2:return dict(call['output'],limitations=42)
    first=install(store,engine,mutate=different)
    value=await invoke(engine,task,context(),direct=True)
    assert len(first.calls)==3
    diagnostics=[e['detail']['metadata']['validation_diagnostic'] for e in rejected(store,task)]
    assert recurrence(diagnostics[0],schema=ContextObservation)!=recurrence(diagnostics[1],schema=ContextObservation)
    assert all(c['canonical'].get('actual_format_feedback') for c in first.calls[1:])
    # An incomplete diagnostic is not reconstructed from its displayed prefix.
    incomplete=dict(diagnostics[0],diagnostic_truncated=True)
    assert recurrence(incomplete,schema=ContextObservation)==incomplete
    assert recurrence(diagnostics[0],schema=m.Review)!=recurrence(diagnostics[0],schema=ContextObservation)
    old=deepcopy(store.records('bounded_model_call'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        assert await invoke(engine,task,context())==value
        assert second.calls==[] and store.records('bounded_model_call')==old
        assert executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_learning_foreign_selector_variants_hold_after_reopen_and_keep_rejected_ideas(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Retain every observed proposal',['No unreviewed application'])
    op=operation('file_list',args={});result=m.OperationResult(operation_id=op['id'],status='succeeded',data={'entries':[]}).model_dump()
    source=proposal_input(op,result)
    proposal={'target':'task','proposal':'A real retained candidate from the rejected response',
              'disposition':'reject','rationale':'No new task scope','next_operation':None}
    def foreign(call,number):
        assert call['schema'] is m.LearningApplications
        return dict(call['output'],new_ideas=[proposal],applications=[{'skill_slot':number,
            'evidence':[{'pointer':'/data/entries','explanation':'Exact observed listing'}]}])
    first=install(store,engine,mutate=foreign)
    with pytest.raises(m.PolicyError,match='repeated'):
        await engine.bounded_judgments._call(task['id'],'wire-learning-repeat',source,m.LearningApplications)
    assert len(first.calls)==2
    events=rejected(store,task)
    diagnostics=[e['detail']['metadata']['validation_diagnostic'] for e in events]
    assert diagnostics[0]!=diagnostics[1]
    assert recurrence(diagnostics[0],schema=m.LearningApplications)==recurrence(diagnostics[1],schema=m.LearningApplications)
    retained={i['id']:i for e in events for i in e['detail']['response_idea_extraction']['ideas']}
    assert retained and all(i['proposal']==proposal['proposal'] for i in retained.values())
    old=deepcopy(store.records('bounded_model_call'));response_records=deepcopy(store.records('model_response'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        history=engine._learning_response_history(task['id'],op['id'])
        by_id={i['id']:i for i in history['ideas']}
        assert all(by_id.get(identity)==idea for identity,idea in retained.items())
        with pytest.raises(m.PolicyError,match='repeated'):
            await engine.bounded_judgments._call(task['id'],'wire-learning-repeat',source,m.LearningApplications)
        assert second.calls==[] and store.records('model_response')==response_records
        for record in old:assert store.record_get('bounded_model_call',record['id'])==record
        assert executor.calls==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['source','policy','owner','role','lease','settings','stop','call','response','ambiguous','unknown'])
async def test_saved_generic_correction_keeps_binding_and_unknown_effect_holds(tmp_path,boundary):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Keep the original request boundary',['Do not replay an unknown send'])
    interrupt='unknown' if boundary=='unknown' else 'http500'
    first=install(store,engine,mutate=failure_mutator(engine,task,interrupt))
    with pytest.raises(ProviderError):await invoke(engine,task,context())
    old=deepcopy(failed_record(store));assert len(first.calls)==2
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        role=None
        if boundary in {'source','owner','lease'}:
            changed=store.get_task(task['id'])
            if boundary=='source':changed['source_hash']='changed-source'
            elif boundary=='owner':changed['actor']='worker'
            else:changed['state']['delegation_lease']={'foreign':'unbound-lease'}
            store.db.execute('UPDATE tasks SET body=? WHERE id=?',(json.dumps(changed),task['id']))
        elif boundary=='policy':engine.policy.hash='changed-policy'
        elif boundary=='role':role='reviewer'
        elif boundary=='settings':engine.gateway.settings.values['model']='changed-model'
        elif boundary=='stop':engine.stop_requested.add(task['id'])
        elif boundary=='call':
            changed=deepcopy(old);changed['feedback_trace']['next_model_input']['source_records'][0]['value']='changed after capture'
            store.record('bounded_model_call',changed['id'],changed)
        elif boundary=='response':
            response=store.record_get('model_response',old['metadata']['response_record']['id'])
            response['http_status']=200;store.record('model_response',response['id'],response)
        elif boundary=='ambiguous':
            store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('wire_feedback_request',old['request_key']))
            extra=deepcopy(old);extra['id']=uuid4().hex
            store.record('bounded_model_call',extra['id'],extra)
        error=asyncio.CancelledError if boundary=='stop' else m.PolicyError
        with pytest.raises(error):await invoke(engine,task,context(),direct=True,role=role)
        assert second.calls==[] and executor.calls==[]
        assert store.records('knowledge_application')==[] and store.records('web_exchange')==[]
        if boundary!='call':assert store.record_get('bounded_model_call',old['id'])==old
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt',['http500','stop'])
@pytest.mark.parametrize('direct',[False,True])
async def test_normal_engine_admission_reopens_feedback_then_reaches_single_execution_permit(tmp_path,interrupt,direct):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Write hello in answer.txt',['Actual bytes'])
    first=install(store,engine,mutate=failure_mutator(engine,task,interrupt,schema=m.EngineeringAdmission))
    store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    selected=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**selected}
    error=asyncio.CancelledError if interrupt=='stop' else ProviderError
    with pytest.raises(error):await engine._pre(state)
    assert executor.calls==[] and store.get_operation(selected['operation_id'])['result'] is None
    failed=next(r for r in store.records('bounded_model_call') if r.get('feedback_schema')=='EngineeringAdmission'
                and r['status'] in {'failed','unobserved'})
    expected=deepcopy(failed['feedback_trace']['next_model_input'])
    original_records=deepcopy(store.records('bounded_model_call'));events=deepcopy(store.events(task['id']))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        if direct:
            # A direct Engineering caller consumes the same exact retained
            # request; the subsequent ordinary admission still authenticates it.
            await engine._call(task['id'],failed['phase'],failed['payload'],m.EngineeringAdmission,role='reviewer')
        await engine._pre(state)
        resumed=[c for c in second.calls if c['phase']==failed['phase']]
        assert len(resumed)==1 and resumed[0]['canonical']==expected
        completed=next(r for r in store.records('bounded_model_call') if r.get('parent',{}).get('id')==failed['id'])
        assert completed['measurement']==failed['measurement']
        assert completed['admission_trace']['actual_model_input']==expected
        assert completed['feedback_trace']['request_measurement']['messages_sha256']==digest(resumed[0]['messages'])
        assert store.get_operation(selected['operation_id'])['status']=='reviewed'
        for record in original_records:assert store.record_get('bounded_model_call',record['id'])==record
        assert store.events(task['id'])[:len(events)]==events
        # The actual ordinary execution consumes the saved Engineering judgment
        # through issue/consume_permit; the model does not supply a token.
        await engine._execute(state)
        assert len(executor.calls)==1 and (Path(task['workspace'])/'answer.txt').read_text(encoding='utf-8')=='hello'
        row=deepcopy(store.get_operation(selected['operation_id']))
        knowledge=deepcopy(store.records('knowledge_application'));calls=len(second.calls)
        await engine._pre(state);await engine._execute(state)
        assert len(executor.calls)==1 and len(second.calls)==calls
        assert store.get_operation(selected['operation_id'])==row and store.records('knowledge_application')==knowledge
        assert_wire_records(store,first.calls+second.calls)
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_historical_failure_without_captured_continuation_stays_unadmitted(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Retain incomplete historical proof',['No invented old request'])
    first=install(store,engine,mutate=failure_mutator(engine,task,'http500'))
    with pytest.raises(ProviderError):await invoke(engine,task,context())
    current=failed_record(store)
    # Explicit old bounded writer shape: actual response evidence survives,
    # but no ordered controller continuation or request index existed.
    old={k:deepcopy(current[k]) for k in ('id','key','task_id','phase','source_hash','policy_hash','payload',
                                        'measurement','status','first_fault','metadata','wire_trace')}
    old['actual_model_input']=deepcopy(current['feedback_trace']['actual_model_input'])
    old['actual_model_input_sha256']=digest(old['actual_model_input'])
    store.record('bounded_model_call',old['id'],old)
    store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('wire_feedback_request',current['request_key']))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        with pytest.raises(m.PolicyError,match='historical failed request'):
            await invoke(engine,task,context(),direct=True)
        assert second.calls==[] and store.record_get('bounded_model_call',old['id'])==old
        assert executor.calls==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()
