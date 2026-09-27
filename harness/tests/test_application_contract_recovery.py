"""Actual Engine/Store/Web recovery; only external responses are synthetic."""
import asyncio
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from policy_harness.bounded_judgments import BoundedJudgments, InputCapacityError
from policy_harness.models import (Learning, LearningIdeas, LearningApplications, LearningSynthesis,
                                  SkillSelection, PolicyError, APPLICATION_OUTPUT_VERSION)
from policy_harness.providers import ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import learning
from tests.test_learning_update_contract import create_update
from tests.test_providers import envelope
from tests.test_response_recovery import LearningWireFixture
from tests.test_web_recovery import collector, operation as web_operation, reopen


def recorded_application(store, engine, task, value, *, persist=True):
    """Synthetic observation and review; no claim of actual Skill effectiveness."""
    from policy_harness.models import OperationResult
    from tests.test_knowledge import operation, journal
    op=operation('file_list',args={})
    result=OperationResult(operation_id=op['id'],status='succeeded',data={'recorded_absence':value,'unavailable':None}).model_dump()
    skill=engine.knowledge._new(task,'fixture-observation',{'title':'Keep observed absence separate',
        'content':'Inspect the recorded output and retain an empty list as absence at that time; do not infer completion or benefit.',
        'applicability':'Recorded result inspection','next_trigger':'Next recorded output'})
    selected=[{'id':skill['id'],'hash':skill['hash'],'application':'Inspect the exact recorded result',
        'procedure_clause':skill['content'],'procedure_sha256':hashlib.sha256(skill['content'].encode()).hexdigest()}]
    application={'skill_id':skill['id'],'skill_hash':skill['hash'],'procedure_clause':skill['content'],
        'procedure_sha256':selected[0]['procedure_sha256'],'operation_id':op['id'],'operation_sha256':digest(op),
        'result_sha256':digest(result),'evidence':[{'pointer':'/data/recorded_absence','sha256':digest(value),
            'explanation':'The supplied value records absence at this exact result; this value alone does not establish use or benefit.'}]}
    learned=learning(classifications=['use'],applications=[application])
    if persist:journal(store,task,op,result,learned,selected)
    return op,result,learned,selected


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[[],{},''])
async def test_recorded_empty_application_reaches_catalog_wire_and_knowledge(tmp_path,value):
    from policy_harness import semantic_wire as wire
    from policy_harness.knowledge import _pointer as other_evidence_pointer
    from tests.test_learning_update_contract import proposal_input
    from tests.test_semantic_wire_contract import install, semantic_fixture, assert_wire_records
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Retain observed absence',['Do not invent procedure use or benefit'])
    op,result,learned,selected=recorded_application(store,engine,task,value)
    def choose(call,_):
        if call['schema'] is Learning:return semantic_fixture(Learning,learned,call['canonical'],call['capture']['association'])
    transport=install(store,engine,mutate=choose)
    payload=proposal_input(op,result);payload['pre']['skills']['selected']=selected
    try:
        actual=await engine._call(task['id'],'observed-absence',payload,Learning)
        assert actual.applications==learned.applications
        call=next(c for c in transport.calls if c['schema'] is Learning)
        assert call['capture']['presentation_revision']==wire.APPLICATION_CONTRACT
        contract=call['wire']['application_contract']
        witness=next(row for row in contract['result_evidence'] if row['pointer']=='/data/recorded_absence')
        assert witness['sha256']==digest(value) and witness['observed_value']=='recorded-empty'
        assert any(row['pointer']=='/data/unavailable' for row in contract['unavailable_result_evidence'])
        assert call['wire']['result']==result and call['wire']['result']['data']['unavailable'] is None
        output=engine.knowledge.apply(task,op,actual,result,[selected[0]['id']])
        assert len(output['observed_application_ids'])==1
        assert engine.knowledge.apply(task,op,actual,result,[selected[0]['id']])==output
        with pytest.raises(PolicyError,match='no observed content'):
            other_evidence_pointer(result,'/data/recorded_absence')
        assert executor.calls==[]
        assert_wire_records(store,transport.calls)
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['missing','null','unknown-status','unknown-effect','foreign-pointer','changed-hash','changed-selection','unreviewed'])
async def test_application_absence_never_admits_unknown_or_unbound_witness(tmp_path,damage):
    from tests.test_knowledge import journal
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Reject unobserved application evidence',['Preserve original selection and review'])
    op,result,learned,selected=recorded_application(store,engine,task,[],persist=False)
    evidence=learned.applications[0]['evidence'][0]
    if damage=='missing':evidence['pointer']='/data/missing'
    elif damage=='null':evidence.update(pointer='/data/unavailable',sha256=digest(None))
    elif damage=='unknown-status':result['status']='unknown'
    elif damage=='unknown-effect':result['effect']='unknown'
    elif damage=='foreign-pointer':evidence.update(pointer='/status',sha256=digest(result['status']))
    elif damage=='changed-hash':evidence['sha256']='0'*64
    elif damage=='changed-selection':learned.applications[0]['skill_hash']='0'*64
    learned.applications[0]['result_sha256']=digest(result)
    journal(store,task,op,result,learned,selected)
    if damage=='unreviewed':store.update_operation(op['id'],post_review=None)
    try:
        with pytest.raises(PolicyError):engine.knowledge.apply(task,op,learned,result,[selected[0]['id']])
        assert not store.records('skill_application') and not store.records('knowledge_application')
    finally:await engine.close();store.close()


NEW_IDEAS = [
    {'id': 'representation-refusal', 'target': 'workflow', 'proposal': 'Preserve a failed representation separately from a retry.',
     'disposition': 'investigate', 'rationale': 'The exact refusal is observed; an alternative representation is untested.', 'next_operation': None},
    {'id': 'content-bearing-application', 'target': 'workflow', 'proposal': 'Require content-bearing output before claiming Skill application.',
     'disposition': 'reject', 'rationale': 'The current exact evidence check already requires observed output.', 'next_operation': None}]


class ApplicationWireFixture(LearningWireFixture):
    async def generate(self, role, phase, payload, schema, **kwargs):
        value, usage = await super().generate(role, phase, payload, schema, **kwargs)
        if schema is SkillSelection:
            available = {s['id'] for s in payload.get('knowledge', {}).get('skills', [])}
            # The selection consumer supplies record pages; never invent an ID
            # that the actual SkillSelection caller has not exposed.
            selected = []
            for skill in self.state['selected_skills']:
                if skill['id'] not in available:
                    continue
                clause = skill['content']
                selected.append({'id': skill['id'], 'hash': skill['hash'], 'reason': 'Inspect the retained exchange',
                    'application': 'Consider the actual exchange output', 'procedure_clause': clause,
                    'procedure_sha256': hashlib.sha256(clause.encode()).hexdigest()})
            value = value.model_copy(update={'selected': selected,
                'rejected': [r for r in value.rejected if r['id'] not in {s['id'] for s in selected}]})
        return value, usage

    def respond(self, request):
        state = self.state; schema = self.current_schema
        if schema is LearningSynthesis:
            return super().respond(request)
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'].split('\n', 1)[1])
        state.setdefault('sent', []).append({'schema': schema.__name__, 'phase': self.current_phase,
            'payload': deepcopy(payload), 'body': deepcopy(body)})
        required = payload.get('required_ideas', [])
        def length():
            response = envelope('')
            response['choices'][0]['finish_reason'] = 'length'
            response['usage'] = {'completion_tokens': body['max_tokens'],
                'completion_tokens_details': {'reasoning_tokens': body['max_tokens']}}
            return httpx.Response(200, json=response)
        if schema is Learning:
            if not payload.get('source_packet_id'):
                return length()
            # Exact older full-Learning writer, including a formal corrected
            # first leaf. The ordinary controller creates every event/receipt.
            ideas = required if payload.get('actual_format_feedback') else payload['bounded_context']['required_ideas']
            value = learning(ideas=ideas, skill_updates=[create_update()]).model_dump()
        elif schema is LearningIdeas:
            value = {'ideas': required}
        else:
            assert schema is LearningApplications
            selected = [s['id'] for s in payload['pre']['skills']['selected']]
            if body['max_tokens'] == 32768:
                assert 'application_output_contract' not in payload
                if len(selected) > 1:
                    return length()
                if not payload.get('actual_format_feedback'):
                    state['rejected_value'] = {'considered_skill_ids': [s['id'] for s in payload['bounded_context']['pre']['skills']['selected']],
                        'applications': [], 'ideas': [*payload['bounded_context']['required_ideas'], *deepcopy(NEW_IDEAS)]}
                    return httpx.Response(200, json=envelope(json.dumps(state['rejected_value'])))
                assert payload['actual_format_feedback']['rejected_response'] == state['rejected_value']
                state['failed_input'] = deepcopy(payload)
                return length()
            contract = payload['application_output_contract']
            assert contract['schema'] == APPLICATION_OUTPUT_VERSION
            assert contract['selected_skill_ids'] == selected and len(selected) == 1
            assert contract['required_idea_ids'] == [i['id'] for i in required]
            assert contract['allowed_output_fields'] == ['considered_skill_ids', 'applications', 'ideas']
            assert not set(contract['already_owned_idea_ids']).intersection(contract['required_idea_ids'])
            assert 'skill_update_contract' not in payload and 'idea_disposition_contract' not in payload
            assert 'Current Application output ownership:' in body['messages'][0]['content']
            state.setdefault('current_applications', []).append(deepcopy(payload))
            if payload.get('actual_format_feedback'):
                assert payload['actual_format_feedback'] == state['failed_input']['actual_format_feedback']
                assert required == NEW_IDEAS
            value = {'considered_skill_ids': selected, 'applications': [], 'ideas': required}
            if state.get('repeat_foreign'):
                value = deepcopy(state['rejected_value'])
            if state.get('stop_after_response'):
                state['engine'].stop_requested.add(payload['task_id'])
                # The second live388 violation in isolation: Ideas are valid,
                # but one foreign sibling Skill is included in this output.
                value['considered_skill_ids'] = list(state['rejected_value']['considered_skill_ids'])
        return httpx.Response(200, json=envelope(json.dumps(value)))


async def application_frontier(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume the retained Application decision', ['All Ideas remain and Knowledge applies once'])
    state = {'selected_skills': [engine.knowledge._new(task, 'fixture-seed', {
        'title': title, 'content': content, 'applicability': 'Public acquisition', 'next_trigger': 'Next exchange'})
        for title, content in [('Preserve transport', 'Keep each transport result separate.'),
                               ('Observe content', 'Inspect content before asserting an application.')]]}
    engine.gateway = ApplicationWireFixture(store, engine.policy, state)
    engine.bounded_judgments = BoundedJudgments(engine)
    bounded = engine.bounded_judgments
    current_input = engine._model_input
    def old_input(task_id, phase, payload, schema, **kwargs):
        # Explicit candidate22 producer, used only to construct historical
        # persisted evidence. Current production never chooses this for sends.
        if schema is LearningApplications:
            kwargs['legacy_application'] = True
        return current_input(task_id, phase, payload, schema, **kwargs)
    engine._model_input = old_input
    normal_proposal = bounded.learning_proposal
    async def historical_first_leaf(task_id, phase, payload):
        assert len(payload['required_ideas']) > 3
        assert len(payload['pre']['skills']['selected']) == 2
        with pytest.raises(ProviderError) as exhausted:
            await bounded._call(task_id, phase, payload, Learning)
        assert exhausted.value.metadata['truncated']
        packet = bounded._packet(task_id, phase + ':learning', payload)
        focus = {'operation': payload['operation'], 'result': payload['result'], 'source_packet_id': packet['id'],
            'required_ideas': payload['required_ideas'][:3], 'bounded_context': deepcopy(payload),
            'pre': {'skills': {'selected': [], 'rejected': [], 'new_knowledge_needed': [], 'rationale': 'Historical first Idea focus'}},
            'instruction': 'Old full Learning page: only this Idea focus; later whole review remains mandatory.'}
        await bounded._call(task_id, phase, focus, Learning)
        return await normal_proposal(task_id, phase, payload)
    bounded.learning_proposal = historical_first_leaf
    requests = []; parent = web_operation()
    with pytest.raises(InputCapacityError, match='indivisible'):
        await collector(store, requests).collect('https://example.com/actual', phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    after = next(w for w in store.records('web_work') if w['stage'] == 'after')
    failed = next(r for r in store.records('bounded_model_call') if r.get('learning_schema') == 'LearningApplications'
        and r.get('actual_model_input', {}).get('actual_format_feedback'))
    successes = [r for r in store.records('bounded_model_call') if r.get('status') == 'succeeded' and r.get('learning_schema')]
    assert [r['learning_schema'] for r in successes] == ['Learning', 'LearningIdeas']
    assert len(successes[0]['result']['ideas']) == 3 and len(successes[1]['result']['ideas']) > 0
    source = store.record_get('bounded_input', failed['payload']['source_packet_id'])['value']
    assert [i for r in successes for i in r['result']['ideas']] == source['required_ideas']
    assert all(r['measurement']['reserved_output_tokens'] == 32768 for r in successes)
    assert failed['actual_model_input']['required_ideas'] == NEW_IDEAS
    assert failed['actual_model_input'] == state['failed_input']
    assert failed['metadata']['http_status'] == 200 and failed['metadata']['finish_reason'] == 'length'
    assert failed['metadata']['truncated'] is True and failed['metadata']['usage']['completion_tokens'] == 32768
    assert failed['metadata']['response_record']['request_sha256']
    assert len(failed['learning_trace']['rejected_events']) == 1
    assert len(requests) == 1 and executor.calls == [] and store.records('knowledge_application') == []
    originals = {kind: deepcopy(store.records(kind)) for kind in (
        'bounded_model_call', 'bounded_model_completed', 'bounded_model_truncated', 'bounded_learning_plan',
        'bounded_input', 'bounded_output_split', 'model_response', 'web_exchange')}
    events = deepcopy(store.events(task['id']))
    engine._model_input = current_input
    bounded.learning_proposal = normal_proposal
    return store, engine, task, state, after, failed, successes, originals, events, requests


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_application_output_transition_preserves_old_returns_and_restores_feedback_through_web(tmp_path, restart):
    store, engine, task, state, after, failed, successes, originals, events, requests = await application_frontier(tmp_path)
    directory, policy = store.data_dir, engine.policy
    if restart:
        await engine.close(); store.close()
        store, engine = reopen(directory, policy)
    engine.gateway = ApplicationWireFixture(store, policy, state)
    engine.gateway.settings.values['max_output_tokens'] = 65536
    engine.bounded_judgments = BoundedJudgments(engine)
    start = len(state['sent'])
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', after['id'])
    assert current['status'] == 'complete' and current['acquisition_result'] == after['acquisition_result']
    assert [r['schema'] for r in state['sent'][start:]] == ['LearningApplications', 'LearningApplications', 'LearningSynthesis']
    assert state['current_applications'][0]['actual_format_feedback'] == failed['actual_model_input']['actual_format_feedback']
    assert state['current_applications'][0]['required_ideas'] == NEW_IDEAS
    assert state['current_applications'][1]['required_ideas'] == []
    old_ids = {i['id'] for r in successes for i in r['result']['ideas']}
    assert set(state['current_applications'][0]['application_output_contract']['already_owned_idea_ids']) == old_ids
    assert set(state['current_applications'][1]['application_output_contract']['already_owned_idea_ids']) == old_ids | {i['id'] for i in NEW_IDEAS}
    for payload in state['current_applications']:
        assert payload['bounded_context'] == failed['payload']['bounded_context']
        assert payload['source_packet_id'] == failed['payload']['source_packet_id']
    completed = next(r for r in store.records('bounded_learning') if r.get('composition_ref'))
    composition = store.record_get('bounded_learning_composition', completed['composition_ref']['id'])
    assert all(engine.bounded_judgments._call_ref(r) in composition['part_receipts'] for r in successes)
    expected = [i for r in successes for i in r['result']['ideas']] + NEW_IDEAS
    assert composition['idea_decisions'] == expected and len({i['id'] for i in expected}) == len(expected)
    assert completed['result']['ideas'] == expected
    assert composition['legacy_proposals'][0]['proposal'] == successes[0]['result']
    actual = current['applied_learning']
    attempt = current['learning_attempts'][0]
    assessed = [i for a in attempt['assessments'] for i in a['assessment']['ideas']]
    assert assessed and actual['ideas'] == [*expected, *assessed]
    assert attempt['review_input']['learning'] == actual
    applications = deepcopy(store.records('knowledge_application'))
    assert len(applications) == 1
    episode = store.record_get('episode', applications[0]['outcome']['episode_id'])
    assert episode['learning'] == actual and episode['result'] == after['acquisition_result']
    for kind, records in originals.items():
        for record in records:
            identity = record['key'] if kind in {'bounded_model_completed', 'bounded_model_truncated'} else record['id']
            assert store.record_get(kind, identity) == record
    assert store.events(task['id'])[:len(events)] == events and store.verify_events()
    assert store.records('web_exchange') == originals['web_exchange'] and len(requests) == 1
    latest = [r for r in store.records('bounded_model_call') if r['id'] not in {x['id'] for x in originals['bounded_model_call']}
              and r.get('learning_schema') and r['status'] == 'succeeded']
    corrected = next(r for r in latest if r['learning_trace'].get('feedback_recovery'))
    assert corrected['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(failed)
    assert all(r['measurement']['reserved_output_tokens'] == 65536 for r in latest)
    count = len(state['sent'])
    await engine.close(); store.close()
    store, engine = reopen(directory, policy)
    engine.gateway = ApplicationWireFixture(store, policy, state)
    engine.gateway.settings.values['max_output_tokens'] = 65536
    engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == count and store.records('knowledge_application') == applications
    assert store.record_get('episode', episode['id']) == episode and len(requests) == 1
    assert store.records('web_exchange') == originals['web_exchange']
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['provider', 'model', 'reasoning', 'context', 'actor', 'feedback', 'request', 'output'])
async def test_application_history_does_not_turn_other_drift_or_corruption_into_retry(tmp_path, changed):
    store, engine, task, state, after, failed, _, originals, _, requests = await application_frontier(tmp_path)
    directory, policy = store.data_dir, engine.policy
    await engine.close(); store.close()
    store, engine = reopen(directory, policy)
    engine.gateway = ApplicationWireFixture(store, policy, state)
    engine.gateway.settings.values['max_output_tokens'] = 65536
    engine.bounded_judgments = BoundedJudgments(engine)
    names = {'provider': ('base_url', 'https://different.invalid/v1'), 'model': ('model', 'different-model'),
             'reasoning': ('reasoning_effort', 'low'), 'context': ('model_context_tokens', 2_000_001),
             'output': ('max_output_tokens', 65535)}
    if changed in names:
        key, value = names[changed]; engine.gateway.settings.values[key] = value
    elif changed == 'actor':
        corrupt = store.get_task(task['id']); corrupt['actor'] = 'reviewer'
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(corrupt, sort_keys=True), task['id']))
    elif changed == 'feedback':
        corrupt = deepcopy(failed); corrupt['actual_model_input']['actual_format_feedback']['validation']['message'] = 'Invented history'
        corrupt['actual_model_input_sha256'] = digest(corrupt['actual_model_input'])
        store.record('bounded_model_call', corrupt['id'], corrupt)
    else:
        reference = failed['metadata']['response_record']; response = store.record_get('model_response', reference['id'])
        response['request_sha256'] = 'f' * 64
        store.record('model_response', response['id'], response)
    count = len(state['sent'])
    with pytest.raises(PolicyError, match='LEARNING_PROVENANCE'):
        await engine.bounded_judgments._call(task['id'], failed['phase'], failed['payload'], LearningApplications)
    assert len(state['sent']) == count and store.records('knowledge_application') == []
    assert store.record_get('web_work', after['id'])['acquisition_result'] == after['acquisition_result']
    assert store.records('web_exchange') == originals['web_exchange'] and len(requests) == 1
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True])
async def test_changed_application_contract_keeps_repeat_defect_and_stop_guards(tmp_path, stop):
    store, engine, task, state, _, _, _, _, _, requests = await application_frontier(tmp_path)
    engine.gateway.settings.values['max_output_tokens'] = 65536
    state['engine'] = engine
    state['stop_after_response' if stop else 'repeat_foreign'] = True
    before = len(state['sent'])
    expected = pytest.raises(asyncio.CancelledError) if stop else pytest.raises(PolicyError, match='repeated the same')
    with expected:
        await engine.web_judgments.drain(task['id'])
    # The retained defect signature is carried to the real next call. A stop
    # raised while receiving that rejection cannot cause another send either.
    assert len(state['sent']) == before + 1 and len(requests) == 1
    assert store.records('knowledge_application') == []
    if stop:
        rejection = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output'][-1]['detail']
        assert rejection['validation']['message'] == 'Learning application decisions omitted, duplicated or invented an identity'
        assert rejection['rejected_response']['ideas'] == NEW_IDEAS
        assert len(rejection['rejected_response']['considered_skill_ids']) == 2
        assert len(state['sent'][-1]['payload']['pre']['skills']['selected']) == 1
    await engine.close(); store.close()
