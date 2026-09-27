"""Ordinary stop/resume over retained mock HTTP; no external model/tool use.

The cycle entry selects one real judgment. This verifies that boundary, not a
complete user artifact task or the semantic quality of fixture judgments.
"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from policy_harness import models as m
from policy_harness.bounded_judgments import ContextObservation
from policy_harness.engine import Quiesced
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_disposition_contract_recovery import payload as disposition_payload
from tests.test_knowledge import journal, learning, operation
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_semantic_wire_contract import install


PHASE='explicit-model-resume'


def selected_judgment(engine, monkeypatch, task, source, schema, results):
    async def enter(current):
        results.append(await engine.bounded_judgments._call(task['id'],PHASE,source,schema))
        raise Quiesced('Fixture boundary: judgment returned; actual artifact remains pending')
    monkeypatch.setattr(engine,'_initialize_task',enter)


async def stopped_request(tmp_path, monkeypatch, family='context', *, complete_response=False, corrective=False):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Finish one stopped judgment',['Retain effects and complete the artifact later'])
    if family=='context':
        schema=ContextObservation;source={'source_records':[{'id':'source','value':'Original source'}]}
        op=operation('file_list',args={});store.save_operation(task['id'],op)
    elif family=='disposition':
        schema=m.Disposition;source=disposition_payload(1);op=source['operation']
        store.save_operation(task['id'],op)
    else:
        schema=m.LearningIdeas;op=operation('file_list',args={})
        result=m.OperationResult(operation_id=op['id'],status='succeeded',data={'entries':[]}).model_dump()
        source=proposal_input(op,result)
        journal(store,task,op,result,learning(),[],status='result_recorded')
    results=[];selected_judgment(engine,monkeypatch,task,source,schema,results)
    def reject_first(call,count):
        if corrective and count==1:
            value=deepcopy(call['output'])
            value['new_ideas'].append({'target':'task','proposal':'Preserve this actual rejected candidate',
                'disposition':'reject','rationale':'Its content is retained; no new work is adopted.',
                'next_operation':None})
            value['unexpected_field']=True
            return value
    first=install(store,engine,mutate=reject_first)
    entered=asyncio.Event()
    if complete_response:
        generate=engine.gateway.generate
        async def retain_then_wait(*args,**kwargs):
            value=await generate(*args,**kwargs)
            if args[1]==PHASE:
                entered.set();await asyncio.Event().wait()
            return value
        monkeypatch.setattr(engine.gateway,'generate',retain_then_wait)
    else:
        sent=0
        async def wait_for_stop(request):
            nonlocal sent
            assert 'tools' not in json.loads(request.content)
            sent+=1
            if not corrective or sent==2:
                entered.set();await asyncio.Event().wait()
            return await first(request)
        engine.gateway._transport=httpx.MockTransport(wait_for_stop)
    handle=engine.start_task(task['id'])
    await asyncio.wait_for(entered.wait(),30)
    await engine.stop_task(task['id']);await handle
    assert store.get_task(task['id'])['status']=='stopped' and not results
    old=next(r for r in store.records('bounded_model_call') if r['phase']==PHASE)
    assert old['status']=='unobserved' and old['first_fault']['type']=='CancelledError'
    captures=deepcopy(store.records('semantic_wire_request'));events=deepcopy(store.events(task['id']))
    rows=deepcopy(store.operations(task['id']));responses=deepcopy(store.records('model_response'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor)
    selected_judgment(engine,monkeypatch,task,source,schema,results)
    second=install(store,engine)
    return store,engine,executor,task,source,schema,results,old,captures,events,rows,responses,first,second


@pytest.mark.asyncio
@pytest.mark.parametrize('family',['context','disposition','ideas'])
@pytest.mark.parametrize('complete_response',[False,True])
async def test_normal_resume_recovers_response_or_only_missing_judgment(tmp_path,monkeypatch,family,complete_response):
    (store,engine,executor,task,source,schema,results,old,captures,events,rows,responses,first,second)=await stopped_request(
        tmp_path,monkeypatch,family,complete_response=complete_response)
    try:
        handle=engine.resume_task(task['id'])
        assert engine.resume_task(task['id']) is handle
        await asyncio.wait_for(handle,30)
        assert len(results)==1,store.events(task['id'])[-1]
        assert len(second.calls)==(0 if complete_response else 1)
        if not complete_response:
            assert second.calls[0]['canonical']['interrupted_model_resume']['source_call']['id']==old['id']
        assert store.record_get('bounded_model_call',old['id'])==old
        assert all(store.record_get('semantic_wire_request',r['id'])==r for r in captures)
        assert store.events(task['id'])[:len(events)]==events
        assert store.operations(task['id'])==rows and executor.calls==[]
        assert all(store.record_get('model_response',r['id'])==r for r in responses)
        before=len(second.calls)
        assert await engine.bounded_judgments._call(task['id'],PHASE,source,schema)==results[0]
        assert len(second.calls)==before
        assert len([e for e in store.events(task['id']) if e['status']=='resume_requested'])==1
        assert store.verify_events()
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',[None,'owner','resume'])
async def test_normal_resume_preserves_rejected_ideas_and_actual_pending_corrective_send(tmp_path,monkeypatch,damage):
    (store,engine,executor,task,source,schema,results,old,captures,events,rows,responses,first,second)=await stopped_request(
        tmp_path,monkeypatch,'ideas',corrective=True)
    old_actual=old['learning_trace']['actual_model_input']
    assert old_actual['required_ideas'] and old_actual['actual_format_feedback']
    assert len(responses)==1 and responses[0]['semantic_wire_capture']!=old['wire_trace']['actual_capture']
    try:
        await asyncio.wait_for(engine.resume_task(task['id']),30)
        assert len(results)==1,store.events(task['id'])[-1]
        assert len(second.calls)==1
        sent=second.calls[0]['canonical']
        assert sent['required_ideas']==old_actual['required_ideas']
        assert sent['actual_format_feedback']==old_actual['actual_format_feedback']
        assert sent['interrupted_model_resume']['source_call']['id']==old['id']
        assert {i.id for i in results[0].ideas}=={i['id'] for i in old_actual['required_ideas']}
        assert store.record_get('bounded_model_call',old['id'])==old and store.operations(task['id'])==rows
        assert executor.calls==[]
        assert await engine.bounded_judgments._call(task['id'],PHASE,source,schema)==results[0]
        assert len(second.calls)==1
        await engine.close();store.close();store,engine=reopen(store,engine,executor)
        reopened=install(store,engine)
        assert await engine.bounded_judgments._call(task['id'],PHASE,source,schema)==results[0]
        assert reopened.calls==[] and store.record_get('bounded_model_call',old['id'])==old
        if damage:
            saved=next(r for r in store.records('bounded_model_call') if r['status']=='succeeded')
            if damage=='owner':saved['learning_owner']='another-focus'
            else:saved['learning_trace']['interrupted_resume']['resume_event']['hash']='0'*64
            store.record('bounded_model_call',saved['id'],saved)
            store.record('bounded_model_completed',saved['key'],saved)
            with pytest.raises(m.PolicyError):
                await engine.bounded_judgments._call(task['id'],PHASE,source,schema)
            assert reopened.calls==[] and executor.calls==[] and store.operations(task['id'])==rows
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['no-explicit-resume','settings','lease','event','unknown-operation','later-dependent-effect','unknown-peer'])
async def test_normal_resume_holds_invalid_binding_and_unknown_dependent_effects(tmp_path,monkeypatch,damage):
    (store,engine,executor,task,source,schema,results,old,captures,events,rows,responses,first,second)=await stopped_request(tmp_path,monkeypatch)
    if damage=='settings':engine.gateway.settings.values['max_output_tokens']-=1
    elif damage=='lease':
        current=store.get_task(task['id'])
        store.update_task(task['id'],state=dict(current['state'],delegation_lease={'changed':True}))
    elif damage=='event':
        ref=old['feedback_trace']['failure_event']
        changed=next(e for e in events if e['seq']==ref['seq'])
        store.db.execute('UPDATE events SET detail=? WHERE seq=?',(json.dumps(dict(changed['detail'],result='changed')),ref['seq']))
        store.db.commit()
    elif damage=='unknown-operation':
        store.update_operation(rows[0]['operation']['id'],status='result_recorded',
            result=m.OperationResult(operation_id=rows[0]['operation']['id'],status='unknown',effect='unknown').model_dump())
    elif damage=='later-dependent-effect':
        # Even a now-known result cannot hide dispatch after the unaccepted
        # judgment; the existing executor writes this event before acting.
        identity=rows[0]['operation']['id']
        store.event(task['id'],'execution','started',{'operation_id':identity})
        store.update_operation(identity,status='result_recorded',
            result=m.OperationResult(operation_id=identity,status='succeeded',data={'entries':[]}).model_dump())
    elif damage=='unknown-peer':
        peer=deepcopy(old);peer.update(id='incomparable-unknown',key='unknown-peer')
        store.record('bounded_model_call',peer['id'],peer)
    try:
        if damage=='no-explicit-resume':
            with pytest.raises(m.PolicyError):
                await engine.bounded_judgments._call(task['id'],PHASE,source,schema)
        else:
            await asyncio.wait_for(engine.resume_task(task['id']),30)
        assert not results and second.calls==[] and executor.calls==[]
        assert store.record_get('bounded_model_call',old['id'])==old
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_each_further_interruption_requires_a_later_explicit_resume(tmp_path,monkeypatch):
    (store,engine,executor,task,source,schema,results,old,captures,events,rows,responses,first,second)=await stopped_request(tmp_path,monkeypatch)
    entered=asyncio.Event()
    async def wait_again(request):
        entered.set();await asyncio.Event().wait()
    engine.gateway._transport=httpx.MockTransport(wait_again)
    handle=engine.resume_task(task['id'])
    await asyncio.wait_for(entered.wait(),30)
    await engine.stop_task(task['id']);await handle
    interrupted=deepcopy(store.records('bounded_model_call'))
    assert len(interrupted)==2 and all(r['status']=='unobserved' for r in interrupted)
    # Removing only the in-memory stop does not create another resume decision.
    engine.stop_requested.discard(task['id'])
    with pytest.raises(m.PolicyError):
        await engine.bounded_judgments._call(task['id'],PHASE,source,schema)
    third=install(store,engine)
    try:
        await asyncio.wait_for(engine.resume_task(task['id']),30)
        assert len(results)==1 and len(third.calls)==1
        assert all(store.record_get('bounded_model_call',r['id'])==r for r in interrupted)
        assert executor.calls==[] and store.operations(task['id'])==rows
    finally:await engine.close();store.close()
