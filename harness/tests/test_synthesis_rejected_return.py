"""Actual Store/Engine/HTTP recovery with synthetic model judgments only."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from policy_harness import models as m
from policy_harness.executor import Executor
from policy_harness.providers import ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import journal, learning, observed_application, operation, seed_skill
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_providers import envelope
from tests.test_semantic_wire_contract import install
from tests.test_wire_cardinality_partition import idea, records_unchanged


def length_response():
    body = envelope('{"unfinished":')
    body['choices'][0]['finish_reason'] = 'length'
    return httpx.Response(200, json=body)


async def historical_failure(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve the actual file result and finish its learning', ['One application and update per Skill'])
    skills = [seed_skill(store, engine.knowledge, task) for _ in range(3)]
    op = operation()
    result = (await Executor(store.data_dir).execute(Path(task['workspace']), m.Operation.model_validate(op))).model_dump()
    applications = []; selected = []
    for skill in skills:
        _, _, observed, selection = observed_application(store, task, skill, op, result)
        applications.extend(observed.applications); selected.extend(selection)
    updates = [{'action': 'use', 'id': s['id']} for s in skills]
    updates.append({'action': 'improve', 'id': skills[0]['id'], 'expected_hash': skills[0]['hash'],
                    'content': skills[0]['content'] + ' Preserve the first fault and the successful result.'})
    journal(store, task, op, result, learning(), selected, status='result_recorded')
    source = proposal_input(op, result, selected)
    source['required_ideas'] = [idea('one'), idea('two')]
    synth = []
    def respond(call, _):
        if call['schema'] is m.Learning:
            return length_response()  # Actual bounded output path creates the page plan.
        output = deepcopy(call['output'])
        if call['schema'] is m.LearningApplications:
            output['applications'] = [{'skill_slot': i, 'evidence': [{k:v for k,v in e.items() if k != 'sha256'}
                for e in application['evidence']]} for i, application in enumerate(applications)]
            return output
        if call['schema'] is m.LearningSynthesis:
            synth.append(call)
            if len(synth) == 2:
                return length_response()
            output['skill_updates'] = [{k:v for k,v in update.items() if k != 'expected_hash'} for update in updates]
            output['classifications'] = ['use', 'improve']
            return output
    transport = install(store, engine, mutate=respond)
    validate = engine.bounded_judgments.validate_learning_unit
    def old_conflict(task_id, schema, value, payload, **kw):
        validate(task_id, schema, value, payload, **kw)
        if schema is m.LearningSynthesis:
            ids = [u.get('id') for u in value.skill_updates if u.get('id')]
            if len(ids) != len(set(ids)):
                raise m.LearningContractError('Coherent Learning contains competing updates to one exact Skill')
    with monkeypatch.context() as historical:
        historical.setattr(engine.bounded_judgments, 'validate_learning_unit', old_conflict)
        with pytest.raises(ProviderError):
            await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source)
    assert len(synth) == 2
    failed = next(r for r in store.records('bounded_model_call') if r.get('learning_schema') == 'LearningSynthesis')
    assert failed['status'] == 'failed' and failed['metadata']['finish_reason'] == 'length'
    assert len(failed['learning_trace']['rejected_events']) == 1
    old_calls = deepcopy(store.records('bounded_model_call'))
    assert {r.get('learning_schema') for r in old_calls if r['status'] == 'succeeded'} >= {'LearningIdeas', 'LearningApplications'}
    old_events = deepcopy(store.events(task['id']))
    old_captures = deepcopy(store.records('semantic_wire_request'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    return store, engine, executor, task, source, skills, updates, failed, old_calls, old_events, old_captures, after


@pytest.mark.asyncio
async def test_complete_rejected_synthesis_reopens_without_success_or_provider_replay(tmp_path, monkeypatch):
    store, engine, executor, task, source, skills, updates, failed, old_calls, old_events, old_captures, after = await historical_failure(tmp_path, monkeypatch)
    try:
        value = await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source)
        assert value.skill_updates == updates
        assert after.calls == [] and executor.calls == []
        records_unchanged(store, old_calls)
        assert store.events(task['id']) == old_events  # No invented provider success.
        for old in old_captures:
            assert store.record_get('semantic_wire_request', old['id']) == old
        recovered = [r for r in store.records('bounded_model_call') if r['status'] == 'revalidated']
        assert len(recovered) == 1 and recovered[0]['recovered_from'] == engine.bounded_judgments._call_ref(failed)
        assert not store.record_get('bounded_model_completed', failed['key'])
        # Reopen again before final review/application: the return stays bound.
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        again = install(store, engine)
        assert await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source) == value
        assert again.calls == []
        # Fixture review admits the exact complete result; atomic Knowledge is
        # exercised here, while real semantic review remains a live-task claim.
        op, result = source['operation'], source['result']
        selected = source['pre']['skills']['selected']
        with pytest.raises(m.PolicyError):
            engine.knowledge.apply(task, op, value, result, [s['id'] for s in skills])
        post = {'operation': op, 'result': result, 'learning': value.model_dump()}
        store.update_operation(op['id'], status='post_reviewed', post_bundle=post,
            post_review={'bundle_hash': digest(post), 'disposition': {'verdict': 'proceed'}})
        first = engine.knowledge.apply(task, op, value, result, [s['id'] for s in skills])
        assert engine.knowledge.apply(task, op, value, result, [s['id'] for s in skills]) == first
        assert len(store.records('skill_application')) == 3
        for old in skills:
            actual = store.record_get('skill', old['id'])
            assert len(actual['uses']) == 1 and actual['uses'][0]['skill_hash'] == old['hash']
        assert store.record_get('skill', skills[0]['id'])['content'] == updates[-1]['content']
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['call', 'event', 'wire-output', 'response', 'source', 'lease', 'settings', 'stale-skill', 'branch', 'truncated-proposal'])
async def test_rejected_synthesis_corruption_or_staleness_holds_without_effect(tmp_path, monkeypatch, damage):
    store, engine, executor, task, source, skills, updates, failed, old_calls, old_events, old_captures, after = await historical_failure(tmp_path, monkeypatch)
    before = deepcopy(store.records('skill')); applications = deepcopy(store.records('knowledge_application'))
    event = next(e for e in old_events if e['seq'] == failed['learning_trace']['rejected_events'][0]['seq'])
    usage = event['detail']['usage']
    if damage == 'call':
        changed = deepcopy(failed); changed['actual_model_input_sha256'] = '0' * 64
        store.record('bounded_model_call', changed['id'], changed)
    elif damage == 'event':
        detail = deepcopy(event['detail']); detail['rejected_response']['outcome_summary'] = 'tampered'
        with store.lock:
            store.db.execute('UPDATE events SET detail=? WHERE seq=?', (json.dumps(detail), event['seq'])); store.db.commit()
    elif damage == 'wire-output':
        ref = usage['semantic_wire_output']; changed = store.record_get('semantic_wire_output', ref['id'])
        changed['raw']['outcome_summary'] = 'tampered'; store.record('semantic_wire_output', ref['id'], changed)
    elif damage == 'response':
        ref = usage['response_record']; changed = store.record_get('model_response', ref['id'])
        changed['ciphertext_base64'] = 'corrupt'; store.record('model_response', ref['id'], changed)
    elif damage == 'source':
        changed = store.get_task(task['id']); changed['source_hash'] = '0' * 64
        # Deliberately corrupt only this owned fixture; the public update API
        # correctly forbids rewriting source identity.
        with store.lock:
            store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(changed), task['id'])); store.db.commit()
    elif damage == 'lease':
        current = store.get_task(task['id']); store.update_task(task['id'], state=dict(current['state'], delegation_lease={'changed': True}))
    elif damage == 'settings':
        engine.gateway.settings.values['max_output_tokens'] -= 1
    elif damage == 'stale-skill':
        changed = deepcopy(skills[0]); changed['hash'] = '0' * 64; store.record('skill', changed['id'], changed)
        before = deepcopy(store.records('skill'))
    elif damage == 'branch':
        changed = deepcopy(failed); changed['id'] = 'ambiguous-other-call'; store.record('bounded_model_call', changed['id'], changed)
    elif damage == 'truncated-proposal':
        detail = deepcopy(event['detail']); detail['usage']['finish_reason'] = 'length'
        with store.lock:
            store.db.execute('UPDATE events SET detail=? WHERE seq=?', (json.dumps(detail), event['seq'])); store.db.commit()
    try:
        with pytest.raises((m.PolicyError, ProviderError)):
            if damage in {'source', 'lease', 'settings', 'stale-skill'}:
                # A public request under changed conditions may legitimately
                # make a fresh judgment. This checks exact historical adoption.
                engine.bounded_judgments._recover_rejected_synthesis(task['id'], failed)
            else:
                await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source)
        assert after.calls == [] and executor.calls == []
        assert store.records('knowledge_application') == applications
        assert store.records('skill') == before
        assert not any(r['status'] == 'revalidated' for r in store.records('bounded_model_call'))
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['unknown', 'started'])
@pytest.mark.parametrize('boundary', ['reopen', 'retention'])
async def test_late_unresolved_peer_holds_reuse_and_atomic_retention(tmp_path, monkeypatch, status, boundary):
    store, engine, executor, task, source, skills, updates, failed, old_calls, old_events, old_captures, after = await historical_failure(tmp_path, monkeypatch)
    peer = dict(deepcopy(failed), id='later-unresolved-call', status=status)
    try:
        if boundary == 'reopen':
            await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source)
            await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
            after = install(store, engine)
            store.record('bounded_model_call', peer['id'], peer)
        else:
            transaction = store._transaction
            inserted = False
            def concurrent_peer(fn):
                nonlocal inserted
                if not inserted and fn.__qualname__.endswith('_recover_rejected_synthesis.<locals>.retain'):
                    inserted = True
                    store.record('bounded_model_call', peer['id'], peer)
                return transaction(fn)
            monkeypatch.setattr(store, '_transaction', concurrent_peer)
        before_calls = deepcopy(store.records('bounded_model_call'))
        before_returns = deepcopy(store.records('bounded_learning_return'))
        before_skills = deepcopy(store.records('skill'))
        before_applied = deepcopy(store.records('knowledge_application'))
        with pytest.raises(m.PolicyError, match='return branch is ambiguous'):
            await engine.bounded_judgments.learning_proposal(task['id'], 'fixture-learning', source)
        assert store.record_get('bounded_model_call', peer['id']) == peer
        records_unchanged(store, before_calls)
        assert {r['id'] for r in store.records('bounded_model_call')} == {r['id'] for r in before_calls} | {peer['id']}
        assert store.records('bounded_learning_return') == before_returns
        assert store.records('skill') == before_skills
        assert store.records('knowledge_application') == before_applied
        assert after.calls == [] and executor.calls == []
        assert store.events(task['id']) == old_events
    finally:
        await engine.close(); store.close()


@pytest.mark.parametrize('updates', [
    [{'action':'improve','id':'a','expected_hash':'a'*64,'content':'first'}, {'action':'retire','id':'a','expected_hash':'a'*64,'reason':'second'}],
    [{'action':'merge','id':'a','expected_hash':'a'*64,'merge_ids':['b'],'merge_hashes':{'b':'b'*64},'content':'merged'}, {'action':'improve','id':'b','expected_hash':'b'*64,'content':'other write'}],
    [{'action':'use','id':'a'}, {'action':'use','id':'a'}],
])
def test_shared_update_guard_preserves_real_mutation_and_duplicate_use_rejection(updates):
    with pytest.raises(m.LearningContractError):
        m.validate_learning_updates(updates)
