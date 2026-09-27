"""Exact semantic Disposition transport and retained legacy failure recovery.

Historical setup explicitly reproduces the old writer with real retained mock
HTTP responses and Engine events. It never claims an unavailable old trace.
"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from policy_harness import models as m
from policy_harness.disposition_contracts import contract, recurrence, sources
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import operation
from tests.test_learning_update_contract import reopen
from tests.test_provider_evidence_recovery import FixtureCipher
from tests.test_providers import FakeSettings, envelope
from tests.test_semantic_wire_contract import install, assert_wire_records


def payload(count=3):
    return {'operation':operation('file_list',args={}),
        'review':{'summary':'Exact retained review','opinions':[{'id':f'opinion-{i}',
            'observation':f'Observed issue {i}','rationale':'Compare the original outcome','evidence_refs':[]} for i in range(count)]},
        'sources':[{'id':f'source-{i}','url':f'https://example.com/{i}','text':f'Actual supplied source {i}'} for i in range(count)]}


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[0,1,3])
@pytest.mark.parametrize('nested',[False,True])
async def test_direct_fixed_references_are_code_owned_and_semantic_hold_is_preserved(tmp_path,count,nested):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Consider every original opinion',['Preserve judgments'])
    source=payload(count)
    if nested:source['web']={'sources':source.pop('sources')}
    def held(call,number):
        if call['schema'] is m.Disposition:return dict(call['output'],verdict='hold',rationale='The source leaves a real question unresolved')
    transport=install(store,engine,mutate=held)
    try:
        value=await engine.bounded_judgments.disposition_proposal(task['id'],source['operation'],'wire-disposition',source)
        assert value.verdict=='hold' and value.rationale=='The source leaves a real question unresolved'
        assert [r.opinion_id for r in value.opinion_responses]==[o['id'] for o in source['review']['opinions']]
        assert value.web_refs==[s['id'] for s in sources(source)]
        assert len(transport.calls)==1
        assert set(transport.calls[0]['output'])=={'verdict','rationale','opinion_decisions'}
        assert_wire_records(store,transport.calls);assert executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_conflicting_source_aliases_hold_before_measure_or_send(tmp_path):
    store,engine,executor,_=runtime(tmp_path);transport=install(store,engine)
    task=store.create_task('Keep the exact acquired sources',['No foreign aliases']);source=payload(1)
    source['web']={'sources':[dict(source['sources'][0],text='Conflicting source body')]}
    try:
        with pytest.raises(m.PolicyError,match='conflicting'):
            await engine.bounded_judgments.disposition_proposal(task['id'],source['operation'],'wire-disposition',source)
        assert transport.calls==[] and store.records('semantic_wire_request')==[] and executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_actual_truncation_pages_consume_every_source_and_keep_worst_verdict_on_reopen(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Page the complete review',['Every real source survives'])
    source=payload(5);truncated=False
    def pages(call,number):
        nonlocal truncated
        if call['schema'] is not m.Disposition:return None
        if not truncated:
            truncated=True;reply=envelope(json.dumps(call['output']));reply['choices'][0]['finish_reason']='length'
            return httpx.Response(200,json=reply)
        verdict='hold' if any(o['id']=='opinion-0' for o in call['canonical']['review']['opinions']) else 'proceed'
        return dict(call['output'],verdict=verdict,rationale='Preserve the actual page judgment '+verdict)
    transport=install(store,engine,mutate=pages)
    result=await engine.bounded_judgments.disposition_proposal(task['id'],source['operation'],'wire-pages',source)
    assert result.verdict=='hold'
    assert sorted(result.web_refs)==sorted(s['id'] for s in source['sources'])
    assert sorted(r.opinion_id for r in result.opinion_responses)==sorted(o['id'] for o in source['review']['opinions'])
    assert len([c for c in transport.calls if c['schema'] is m.Disposition])>=3
    records=deepcopy(store.records('bounded_model_call'));splits=deepcopy(store.records('bounded_output_split'))
    assert splits and any(r['status']=='failed' for r in records)
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        repeated=await engine.bounded_judgments.disposition_proposal(task['id'],source['operation'],'wire-pages',source)
        assert repeated==result and transport.calls==[]
        assert store.records('bounded_model_call')==records and store.records('bounded_output_split')==splits
        assert executor.calls==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt',['http500','stop'])
async def test_corrected_request_survives_transport_or_pre_send_stop_then_returns_once(tmp_path,interrupt):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Preserve rejected and corrected inputs',['One accepted return'])
    source=payload(2)
    def fail(call,number):
        if number==1:
            if interrupt=='stop':engine.stop_requested.add(task['id'])
            return dict(call['output'],opinion_decisions=call['output']['opinion_decisions'][:-1])
        return httpx.Response(500,json={'error':'Synthetic known failed corrective request'})
    first=install(store,engine,mutate=fail)
    fault=asyncio.CancelledError if interrupt=='stop' else ProviderError
    with pytest.raises(fault):await engine.bounded_judgments._call(task['id'],'wire-return',source,m.Disposition)
    assert len(first.calls)==(1 if interrupt=='stop' else 2)
    old=deepcopy(store.records('bounded_model_call'));events=deepcopy(store.events(task['id']))
    expected=old[-1]['disposition_trace']['next_model_input']
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    try:
        value=await engine.bounded_judgments._call(task['id'],'wire-return',source,m.Disposition)
        assert value.verdict=='proceed' and len(second.calls)==1
        assert second.calls[0]['canonical']==expected
        assert second.calls[0]['canonical']['actual_format_feedback']['validation']
        assert store.events(task['id'])[:len(events)]==events
        for record in old:assert store.record_get('bounded_model_call',record['id'])==record
        saved=deepcopy(store.records('bounded_model_call'))
        assert await engine.bounded_judgments._call(task['id'],'wire-return',source,m.Disposition)==value
        assert len(second.calls)==1 and store.records('bounded_model_call')==saved
        assert executor.calls==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_wrong_slot_count_variants_are_one_durable_defect_through_real_gateway(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Do not loop on one structural defect',['Retain first failure'])
    source=payload(2)
    def repeated(call,number):return dict(call['output'],opinion_decisions=call['output']['opinion_decisions']+[call['output']['opinion_decisions'][0]]*number)
    transport=install(store,engine,mutate=repeated)
    with pytest.raises(m.PolicyError,match='repeated'):
        await engine.bounded_judgments._call(task['id'],'wire-repeat',source,m.Disposition)
    assert len(transport.calls)==2
    rejected=[e['detail']['metadata']['validation_diagnostic'] for e in store.events(task['id']) if e['status']=='rejected_model_output']
    assert rejected[0]!=rejected[1] and recurrence(rejected[0])==recurrence(rejected[1])
    records=deepcopy(store.records('bounded_model_call'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        with pytest.raises(m.PolicyError,match='repeated'):
            await engine.bounded_judgments._call(task['id'],'wire-repeat',source,m.Disposition)
        assert transport.calls==[] and store.records('bounded_model_call')==records and executor.calls==[]
    finally:await engine.close();store.close()


async def write_legacy(store,engine,task,source,*,invalid):
    """Old writer shape: actual legacy pack/response/events, no invented trace."""
    phase='wire-legacy';root=engine._model_input(task['id'],phase,source,m.Disposition,role='parent')
    root.pop('exact_response_contract',None)
    value=m.Disposition(verdict='proceed',rationale='Historical actual response',
        opinion_responses=[{'opinion_id':o['id'],'disposition':'accept','rationale':'Actual considered opinion'} for o in source['review']['opinions']],
        web_refs=['wrong-response-domain'] if invalid else [s['id'] for s in source['sources']])
    gateway=ModelGateway(FakeSettings(model_context_tokens=4000000,max_output_tokens=262144),engine.policy,
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json=envelope(json.dumps(value.model_dump())))),
        response_store=store,response_protector=FixtureCipher())
    engine.gateway=gateway
    measured=engine.bounded_judgments._measure_input(phase,root,m.Disposition,'parent',engine.bounded_judgments._configuration(task['id'],'parent'))
    record={'id':uuid4().hex,'task_id':task['id'],'source_hash':task['source_hash'],'policy_hash':engine.policy.hash,
        'phase':phase,'payload':deepcopy(source),'measurement':measured}
    record['key']=engine.bounded_judgments._receipt_key(task,phase,source,m.Disposition,'parent',measured)
    store.event(task['id'],phase,'started',{'actor':'parent','target':source['operation']['id'],'policy_hash':engine.policy.hash})
    actual,usage=await gateway.generate('parent',phase,root,m.Disposition)
    if invalid:
        store.event(task['id'],phase,'rejected_model_output',{'validation':{'kind':'proposal_contract','message':'Historical incorrect acquired references'},
            'rejected_response':actual.model_dump(),'usage':usage,'target_effect':'none; proposal not admitted'})
        record.update(status='failed',first_fault={'type':'PolicyError','message':'Historical repeated structural defect'})
        store.event(task['id'],phase,'failed',{'error_type':'PolicyError','message':record['first_fault']['message'],'metadata':{},'first_fault_preserved':True})
    else:
        record.update(status='succeeded',result=actual.model_dump())
        store.event(task['id'],phase,'succeeded',{'actor':'parent','result':actual.model_dump(),'measurement':{'usage':usage}})
        store.record('bounded_model_completed',record['key'],record)
    store.record('bounded_model_call',record['id'],record)
    assert 'disposition_trace' not in record and 'actual_model_input' not in record
    return deepcopy(record)


@pytest.mark.asyncio
async def test_old_success_is_authenticated_as_old_without_new_wire_or_model_replay(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Reuse the retained successful judgment',['No old output regeneration'])
    source=payload(2);old=await write_legacy(store,engine,task,source,invalid=False)
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        value=await engine.bounded_judgments._call(task['id'],'wire-legacy',source,m.Disposition)
        assert value.model_dump()==old['result'] and transport.calls==[]
        assert store.record_get('bounded_model_call',old['id'])==old
        assert store.records('semantic_wire_request')==[] and store.records('semantic_wire_output')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_legacy_failure_transition_freezes_new_root_across_later_history_and_reopen(tmp_path,monkeypatch):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Correct an observed old failure',['Old evidence remains immutable'])
    source=payload(2);old=await write_legacy(store,engine,task,source,invalid=True)
    first=install(store,engine,mutate=lambda call,number:httpx.Response(500,json={'error':'Synthetic corrected-request interruption'}))
    with pytest.raises(ProviderError):await engine.bounded_judgments._call(task['id'],'wire-legacy',source,m.Disposition)
    assert len(first.calls)==1
    corrected=first.calls[0]['canonical'];transition=corrected['actual_format_feedback']['contract_transition']
    assert transition['source_receipt']=={'id':old['id'],'key':old['key'],'sha256':digest(old)}
    assert 'not authenticated' in transition['proof_limit']
    children=deepcopy([r for r in store.records('bounded_model_call') if r['id']!=old['id']])
    await engine.close();store.close();store,engine=reopen(store,engine,executor);second=install(store,engine)
    # Only the current navigation representation changes. This models later
    # unrelated history without fabricating a tool or a completed operation.
    original_lookup=engine._history_lookup
    monkeypatch.setattr(engine,'_history_lookup',lambda identity:{**original_lookup(identity),'later_navigation_observation':'new-history'})
    try:
        value=await engine.bounded_judgments._call(task['id'],'wire-legacy',source,m.Disposition)
        assert value.verdict=='proceed' and len(second.calls)==1
        assert second.calls[0]['canonical']==corrected
        assert store.record_get('bounded_model_call',old['id'])==old
        for child in children:assert store.record_get('bounded_model_call',child['id'])==child
        assert executor.calls==[] and store.records('web_exchange')==[] and store.records('knowledge_application')==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['source','role','settings','lease'])
async def test_disposition_pending_binding_change_holds_before_model(tmp_path,change):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Keep the failed request owner',['No foreign resume']);source=payload(1)
    def fail(call,number):
        if number==1:return dict(call['output'],opinion_decisions=[])
        return httpx.Response(500,json={'error':'Synthetic known failure'})
    install(store,engine,mutate=fail)
    with pytest.raises(ProviderError):await engine.bounded_judgments._call(task['id'],'wire-binding',source,m.Disposition)
    records=deepcopy(store.records('bounded_model_call'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        role=None
        if change=='settings':engine.gateway.settings.values['model']='different-model'
        elif change=='role':role='reviewer'
        else:
            row=store.get_task(task['id'])
            if change=='source':row['source_hash']='different-source'
            else:row['state']['delegation_lease']={'changed':'lease'}
            store.db.execute('UPDATE tasks SET body=? WHERE id=?',(json.dumps(row),task['id']))
        with pytest.raises(m.PolicyError,match='DISPOSITION_PROVENANCE'):
            await engine.bounded_judgments._call(task['id'],'wire-binding',source,m.Disposition,role=role)
        assert transport.calls==[] and store.records('bounded_model_call')==records and executor.calls==[]
    finally:await engine.close();store.close()
