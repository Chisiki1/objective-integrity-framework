"""Real retained-response, SQLite and Engine consumers; all remote data is synthetic."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from policy_harness.engine import Engine, Quiesced
from policy_harness.knowledge import Knowledge
from policy_harness.models import ConfigurationRequired, Idea, Learning, OperationResult, PolicyError
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.settings import SettingsError
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, FixtureWeb, runtime
from tests.test_knowledge import journal, learning, operation
from tests.test_learning_update_contract import create_update, proposal_input, reopen
from tests.test_learning_update_contract import CorrectingGateway
from tests.test_provider_evidence_recovery import FixtureCipher, read_all
from tests.test_providers import FakeSettings, envelope
from tests.test_web_recovery import SimulatedProcessLoss, collector, operation as web_operation


NEW_IDEA = Idea(id='schema-new-idea', target='task', proposal='Retain the observed schema-failure example for later work',
                 disposition='reject', rationale='Preserve it as an actual considered candidate without adopting it').model_dump()


async def whole_rejected_frontier(tmp_path, monkeypatch, *, schema_tail=False):
    """Produce actual three-response history, then repair only its validator."""
    from tests.test_semantic_wire_contract import install
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Return the final complete judgment', ['No send or accepted-result replay'])
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    source = proposal_input(op, result); source['required_ideas'] = [NEW_IDEA]
    journal(store, task, op, result, learning(), [], status='result_recorded')
    def respond(call, count):
        body = deepcopy(call['output'])
        body['new_ideas'].append({k: v for k, v in dict(NEW_IDEA,
            proposal=f'Preserve independently considered proposal {count}').items() if k != 'id'})
        if count == 2 or (schema_tail and count == 3):body['unexpected_wire_field'] = True
        return body
    transport = install(store, engine, mutate=respond)
    validate = engine._validate_learning_proposal
    def previous_contract(task_id, value, payload):
        validate(task_id, value, payload)
        raise PolicyError('Historical duplicated evidence contract rejects an observed empty result')
    with monkeypatch.context() as old:
        old.setattr(engine, '_validate_learning_proposal', previous_contract)
        with pytest.raises(PolicyError, match='Model repeated the same'):
            await engine.bounded_judgments.learning_proposal(task['id'], 'whole-learning', source)
    assert len(transport.calls) == 3
    failed = next(r for r in store.records('bounded_model_call') if r['status'] == 'failed')
    originals = deepcopy(store.records('bounded_model_call'))
    responses = deepcopy(store.records('model_response')); events = deepcopy(store.events(task['id']))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    return store, engine, executor, task, source, failed, originals, responses, events, after


@pytest.mark.asyncio
async def test_last_complete_whole_learning_returns_after_contract_repair_without_resend(tmp_path, monkeypatch):
    from tests.test_semantic_wire_contract import install
    store, engine, executor, task, source, failed, originals, responses, events, after = await whole_rejected_frontier(tmp_path, monkeypatch)
    try:
        operation_before = deepcopy(store.get_operation(source['operation']['id']))
        final = next(e for e in events if e['seq'] == failed['learning_trace']['rejected_events'][-1]['seq'])
        value = await engine.bounded_judgments.learning_proposal(task['id'], 'whole-learning', source)
        assert value.model_dump() == final['detail']['rejected_response']
        assert {i.id for i in value.ideas} == {i['id'] for i in final['detail']['retained_ideas']}
        assert len(value.ideas) == 4
        assert after.calls == [] and executor.calls == []
        assert store.events(task['id']) == events and store.records('model_response') == responses
        for row in originals:assert store.record_get('bounded_model_call', row['id']) == row
        returned = [r for r in store.records('bounded_model_call') if r['status'] == 'revalidated']
        assert len(returned) == 1 and returned[0]['recovered_from'] == engine.bounded_judgments._call_ref(failed)
        assert not store.record_get('bounded_model_completed', failed['key'])
        assert store.records('knowledge_application') == []
        # Returning a proposal does not perform its normal review/application.
        assert store.get_operation(source['operation']['id']) == operation_before
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        again = install(store, engine)
        assert await engine.bounded_judgments.learning_proposal(task['id'], 'whole-learning', source) == value
        assert again.calls == [] and store.events(task['id']) == events
        assert store.records('knowledge_application') == []
        assert store.get_operation(source['operation']['id']) == operation_before
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['still-invalid', 'schema-tail', 'later-start', 'unknown-peer', 'atomic-peer',
    'event', 'wire-output', 'response', 'role', 'settings', 'lease'])
async def test_complete_whole_learning_keeps_unresolved_or_corrupt_return_held(tmp_path, monkeypatch, damage):
    store, engine, executor, task, source, failed, originals, responses, events, after = await whole_rejected_frontier(
        tmp_path, monkeypatch, schema_tail=damage == 'schema-tail')
    final = next(e for e in events if e['seq'] == failed['learning_trace']['rejected_events'][-1]['seq'])
    if damage == 'still-invalid':
        def invalid(*args, **kw):raise PolicyError('The actual proposal defect is still present')
        monkeypatch.setattr(engine, '_validate_learning_proposal', invalid)
    elif damage == 'later-start':
        store.event(task['id'], failed['phase'], 'started', {'actor': task['actor'], 'target': source['operation']['id'], 'policy_hash': engine.policy.hash})
    elif damage in {'unknown-peer', 'atomic-peer'}:
        peer = dict(deepcopy(failed), id='later-unknown', key='different-request', status='unobserved')
        peer['payload']['new_context'] = 'A later unresolved send cannot be treated as not executed'
        if damage == 'unknown-peer':store.record('bounded_model_call', peer['id'], peer)
        else:
            transaction = store._transaction
            inserted = False
            def race(fn):
                nonlocal inserted
                if not inserted and fn.__qualname__.endswith('_recover_rejected_synthesis.<locals>.retain'):
                    inserted = True
                    store.record('bounded_model_call', peer['id'], peer)
                return transaction(fn)
            monkeypatch.setattr(store, '_transaction', race)
    elif damage == 'event':
        detail = deepcopy(final['detail']); detail['rejected_response']['ideas'] = []
        store.db.execute('UPDATE events SET detail=? WHERE seq=?', (json.dumps(detail), final['seq'])); store.db.commit()
    elif damage in {'wire-output', 'response'}:
        key = 'semantic_wire_output' if damage == 'wire-output' else 'response_record'
        kind = 'semantic_wire_output' if damage == 'wire-output' else 'model_response'
        ref = final['detail']['usage'][key]; row = store.record_get(kind, ref['id'])
        if damage == 'wire-output':row['raw']['outcome_summary'] = 'changed after response'
        else:row['ciphertext_base64'] = 'corrupt'
        store.record(kind, row['id'], row)
    elif damage == 'settings':engine.gateway.settings.values['max_output_tokens'] -= 1
    elif damage == 'role':
        current = store.get_task(task['id']); current['actor'] = 'reviewer'
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(current), task['id'])); store.db.commit()
    elif damage == 'lease':
        current = store.get_task(task['id']); store.update_task(task['id'], state=dict(current['state'], delegation_lease={'changed': True}))
    before_calls = deepcopy(store.records('bounded_model_call')); before_events = deepcopy(store.events(task['id']))
    try:
        with pytest.raises(PolicyError):
            await engine.bounded_judgments.learning_proposal(task['id'], 'whole-learning', source)
        assert after.calls == [] and executor.calls == []
        expected = before_calls + ([peer] if damage == 'atomic-peer' else [])
        assert {r['id']: r for r in store.records('bounded_model_call')} == {r['id']: r for r in expected}
        assert store.events(task['id']) == before_events and store.records('knowledge_application') == []
        assert store.records('bounded_learning_return') == []
    finally:
        await engine.close(); store.close()


class RecoverableCipher(FixtureCipher):
    unavailable = False

    def unprotect(self, value):
        if self.unavailable:
            raise SettingsError('Synthetic protection context unavailable')
        return super().unprotect(value)


def wire_gateway(store, policy, calls, *, invalid=True, cipher=None, malformed=False, repeat_invalid=False):
    def transport(request):
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'].split('\n', 1)[1])
        calls.append(deepcopy(payload))
        value = learning(ideas=payload.get('required_ideas', [])).model_dump()
        if (len(calls) == 1 or repeat_invalid) and invalid:
            # Put the valid Idea in the omitted middle of the bounded preview.
            value = {'outcome_summary': 'a' * 80000, 'ideas': [NEW_IDEA, {'id': 'invalid-shape'}],
                     **{k: v for k, v in value.items() if k not in {'outcome_summary', 'ideas'}}}
            value['classifications'] = ['unsupported-classification']
            value['next_use_trigger'] = 'z' * 80000
        content = '{"ideas": [broken' if malformed else json.dumps(value)
        response = envelope(content)
        response['choices'][0]['message']['reasoning_content'] = 'PRIVATE_SYNTHETIC_REASONING'
        return httpx.Response(200, json=response)
    gateway = ModelGateway(FakeSettings(model_context_tokens=2000000, max_output_tokens=262144), policy,
                        transport=httpx.MockTransport(transport), response_store=store,
                        response_protector=cipher or FixtureCipher())
    # Preserve the historical canonical response and original fault exactly.
    gateway.supports_semantic_wire = False
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_schema_invalid_complete_response_retains_middle_idea_through_bounded_and_restart(tmp_path, monkeypatch, restart):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain exact rejected proposal', ['No Idea disappears'])
    op = operation('file_list', args={}); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    source = proposal_input(op, result); calls = []
    engine.gateway = wire_gateway(store, engine.policy, calls)
    if restart:
        event = store.event
        def interrupt(*args, **kwargs):
            actual = event(*args, **kwargs)
            if args[2] == 'rejected_model_output':
                raise SimulatedProcessLoss('After durable rejected response, before corrected request')
            return actual
        with monkeypatch.context() as patch:
            patch.setattr(store, 'event', interrupt)
            with pytest.raises(SimulatedProcessLoss):
                await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
        assert len(calls) == 1
        retained = deepcopy(store.records('model_response'))
        interrupted = deepcopy(store.records('bounded_model_call')[0])
        await engine.close(); store.close()
        store, engine = reopen(store, engine, executor)
        engine.gateway = wire_gateway(store, engine.policy, calls)
        assert store.records('model_response') == retained
    value = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert len(calls) == 2 and calls[1]['required_ideas'] == [NEW_IDEA]
    assert [i.model_dump() for i in value.ideas] == [NEW_IDEA]
    rejected = next(e for e in store.events(task['id']) if e['status'] == 'rejected_model_output')
    detail = rejected['detail']; extraction = detail['response_idea_extraction']
    assert calls[1]['actual_format_feedback']['validation'] == detail['metadata']['validation_diagnostic']
    assert calls[1]['actual_format_feedback']['rejected_response'] == detail['metadata']['response_diagnostic']
    if restart:
        completed = next(r for r in store.records('bounded_model_call') if r['status'] == 'succeeded')
        assert completed['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(interrupted)
        assert completed['learning_trace']['actual_model_input']['actual_format_feedback'] == calls[1]['actual_format_feedback']
        assert store.record_get('bounded_model_call', interrupted['id']) == interrupted
    assert extraction['status'] == 'partial' and extraction['invalid_idea_indexes'] == [1]
    assert extraction['ideas'] == [NEW_IDEA]
    preview = detail['metadata']['response_diagnostic']
    assert preview['diagnostic_truncated'] is True
    assert NEW_IDEA['proposal'] not in preview['sanitized_response']
    full, _ = read_all(engine.gateway, extraction['response_ref'], task['id'])
    assert NEW_IDEA['proposal'] in full and 'PRIVATE_SYNTHETIC_REASONING' not in full
    assert detail['metadata']['finish_reason'] == 'stop'
    assert store.records('episode') == [] and store.records('knowledge_application') == [] and executor.calls == []
    row = {'task_id': task['id'], 'operation': op, 'pre_bundle': {}}
    assert engine._post_required_ideas(row, []) == [NEW_IDEA]
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['last-input', 'response-binding', 'trace-role'])
async def test_observed_rejection_cannot_authenticate_changed_last_send(tmp_path, monkeypatch, changed):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep unknown or corrupt sends held', ['No replacement model call'])
    op = operation('file_list', args={}); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    source = proposal_input(op, result); calls = []
    engine.gateway = wire_gateway(store, engine.policy, calls)
    event = store.event
    def interrupt(*args, **kwargs):
        actual = event(*args, **kwargs)
        if args[2] == 'rejected_model_output':
            raise SimulatedProcessLoss('After durable rejected response, before corrected request')
        return actual
    with monkeypatch.context() as patch:
        patch.setattr(store, 'event', interrupt)
        with pytest.raises(SimulatedProcessLoss):
            await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert len(calls) == 1
    record = store.records('bounded_model_call')[0]
    if changed == 'last-input':
        record['learning_trace']['actual_model_input']['instruction'] = 'A different last send has no observed response'
        store.record('bounded_model_call', record['id'], record)
    elif changed == 'trace-role':
        record['learning_trace'].pop('role')
        store.record('bounded_model_call', record['id'], record)
    else:
        response = store.records('model_response')[0]
        response['messages_sha256'] = '0' * 64
        store.record('model_response', response['id'], response)
    originals = deepcopy(store.records('bounded_model_call')); responses = deepcopy(store.records('model_response'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    engine.gateway = wire_gateway(store, engine.policy, calls)
    expected = {'last-input': 'actual failed send does not follow', 'response-binding': 'saved correction response/request binding differs',
        'trace-role': 'initial request/role/lease changed'}[changed]
    with pytest.raises(PolicyError, match=expected):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert len(calls) == 1 and store.records('bounded_model_call') == originals
    assert store.records('model_response') == responses and store.records('knowledge_application') == [] and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_interrupted_rejection_keeps_repeated_defect_held_after_reopen(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain observed rejection and its defect history', ['No third request for the same failed correction'])
    op = operation('file_list', args={}); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    source = proposal_input(op, result); calls = []
    engine.gateway = wire_gateway(store, engine.policy, calls, repeat_invalid=True)
    event = store.event
    def interrupt(*args, **kwargs):
        actual = event(*args, **kwargs)
        if args[2] == 'rejected_model_output':raise SimulatedProcessLoss('After durable rejection, before correction')
        return actual
    with monkeypatch.context() as patch:
        patch.setattr(store, 'event', interrupt)
        with pytest.raises(SimulatedProcessLoss):
            await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    engine.gateway = wire_gateway(store, engine.policy, calls, repeat_invalid=True)
    with pytest.raises(PolicyError, match='repeated the same schema defect'):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert len(calls) == 2 and calls[1]['actual_format_feedback']['validation']
    originals = deepcopy(store.records('bounded_model_call')); responses = deepcopy(store.records('model_response'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    engine.gateway = wire_gateway(store, engine.policy, calls, repeat_invalid=True)
    with pytest.raises(PolicyError, match='saved correction repeated a known defect'):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert len(calls) == 2 and store.records('model_response') == responses and executor.calls == []
    assert store.records('knowledge_application') == []
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['protection', 'history'])
async def test_protected_response_recovers_before_next_request_and_web_idea_consumer(tmp_path, unavailable):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Recover protected prior response', ['Same response and Idea'])
    op = operation('web_fetch', args={'url': 'https://example.com'})
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    source = proposal_input(op, result); calls = []; cipher = RecoverableCipher(); cipher.unavailable = unavailable == 'protection'
    engine.gateway = wire_gateway(store, engine.policy, calls, cipher=cipher)
    if unavailable == 'history':
        settings = engine.gateway.settings; settings.history_unavailable = False
        settings.redaction_secrets = lambda: (tuple(settings.keys.values()), ('history:fixture',) if settings.history_unavailable else ())
        generate = engine.gateway.generate
        async def history_becomes_unavailable(*args, **kwargs):
            try: return await generate(*args, **kwargs)
            except ProviderError:
                settings.history_unavailable = True
                raise
        engine.gateway.generate = history_becomes_unavailable
    with pytest.raises(ConfigurationRequired, match='protection context|Credential history'):
        await engine.bounded_judgments.learning_proposal(task['id'], 'web_acquisition_learning', source)
    assert len(calls) == 1
    rejected = next(e for e in store.events(task['id']) if e['status'] == 'rejected_model_output')
    assert rejected['detail']['idea_extraction_pending'] is True
    with pytest.raises(ConfigurationRequired):
        engine.gateway.read_response(rejected['detail']['metadata']['response_record'], task_id=task['id'])
    original = deepcopy(store.records('model_response'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    cipher.unavailable = False
    engine.gateway = wire_gateway(store, engine.policy, calls, cipher=cipher)
    value = await engine.bounded_judgments.learning_proposal(task['id'], 'web_acquisition_learning', source)
    assert [i.model_dump() for i in value.ideas] == [NEW_IDEA]
    assert len(calls) == 2 and calls[1]['required_ideas'] == [NEW_IDEA]
    assert store.records('model_response')[0] == original[0]
    recovery = store.records('learning_response_recovery')[0]
    assert recovery['source_event'] == {'seq': rejected['seq'], 'hash': rejected['hash']}
    assert NEW_IDEA in engine.web_judgments._observed_ideas(task['id'], op['id'])
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_complete_response_binding_and_malformed_json_do_not_invent_ideas(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain malformed evidence', ['Explicit uncertainty'])
    calls = []; gateway = wire_gateway(store, engine.policy, calls, malformed=True)
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'learning_proposal', {'task_id': task['id']}, Learning)
    reference = caught.value.metadata['response_record']
    args = dict(task_id=task['id'], role='parent', phase='learning_proposal', policy_hash=engine.policy.hash, source_hash=task['source_hash'])
    extracted = gateway.learning_response_ideas(reference, **args)
    assert extracted['status'] == 'unavailable' and extracted['ideas'] == []
    assert extracted['response_ref'] == reference and extracted['view_sha256']
    for changed in [dict(reference, retained_bytes=0), dict(reference, record_sha256='0'*64)]:
        with pytest.raises(ProviderError): gateway.learning_response_ideas(changed, **args)
    with pytest.raises(ProviderError, match='binding'):
        gateway.learning_response_ideas(reference, **dict(args, phase='different-phase'))
    assert len(calls) == 1 and store.records('knowledge_application') == []
    await engine.close(); store.close()


def control_episode(engine, task, op, *, legacy=False):
    assessments = [{'target_id': 'prior-judgment', 'assessment': {'ideas': [NEW_IDEA]}}]
    engine._journal_judgment_result(task, op, 'web-before-unexecuted', assessments,
                                    {'status': 'not-dispatched', 'effect': 'none'}, {'actual_opinion': 'Do not dispatch yet'})
    episode = next(e for e in engine.store.records('episode') if e['operation_id'] == op['id'])
    if legacy:
        record = engine.store.record_get('judgment_result', episode['id'])
        for name in ('task_id', 'operation_id', 'phase', 'episode_sha256'): record.pop(name)
        engine.store.record('judgment_result', episode['id'], record)
    return episode


@pytest.mark.parametrize('legacy', [False, True])
def test_verified_control_journal_is_not_an_uncommitted_semantic_effect(tmp_path, legacy):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Use legitimate prior control', ['Keep all original records'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    prior = control_episode(engine, task, op, legacy=legacy)
    value = learning(skill_updates=[create_update()])
    observation = engine.knowledge.application_state(task, op, value, result, [])
    assert observation['state'] == 'not_committed' and observation['episodes'] == []
    assert observation['control_episodes'][0]['sha256'] == digest(prior)
    original_ideas = deepcopy(store.records('idea'))
    actual = engine.knowledge.apply(task, op, value, result, [])
    assert actual['episode_id'] != prior['id'] and store.record_get('episode', prior['id']) == prior
    assert all(store.record_get('idea', i['id']) == i for i in original_ideas)
    assert engine.knowledge.application_state(task, op, value, result, [])['state'] == 'committed'
    store.close()


@pytest.mark.parametrize('tamper', ['missing-journal', 'input', 'episode-result', 'episode-idea', 'receipt-hash'])
def test_unverified_control_lookalike_remains_unknown_and_cannot_apply(tmp_path, tamper):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Preserve an inconsistent original', ['No additional effect'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    prior = control_episode(engine, task, op)
    if tamper == 'missing-journal':
        # An arbitrary episode has no matching producer receipt.
        prior['id'] = 'orphan-control-lookalike'; store.record('episode', prior['id'], prior)
    elif tamper.startswith('episode'):
        if tamper == 'episode-result': prior['result']['effect'] = 'confirmed'
        else: prior['learning']['ideas'][0]['proposal'] = 'Changed meaning'
        store.record('episode', prior['id'], prior)
    else:
        receipt = store.record_get('judgment_result', prior['id'])
        receipt['input_hash' if tamper == 'input' else 'episode_sha256'] = '0'*64
        store.record('judgment_result', prior['id'], receipt)
    value = learning(skill_updates=[create_update()]); before = deepcopy(store.records('episode'))
    assert engine.knowledge.application_state(task, op, value, result, [])['state'] == 'unknown'
    with pytest.raises(PolicyError, match='atomic application marker'):
        engine.knowledge.apply(task, op, value, result, [])
    assert store.records('episode') == before and store.records('skill') == [] and store.records('knowledge_application') == []
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, True])
async def test_legacy_normal_orphan_is_classified_before_regeneration(tmp_path, invalid):
    store, engine, executor, gateway = runtime(tmp_path)
    task = store.create_task('Retain inconsistent original input', ['No regeneration'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    value = learning(skill_updates=[{'action': 'create-provisional'}] if invalid else [create_update()])
    journal(store, task, op, result, value)
    orphan = {'id': 'orphan', 'task_id': task['id'], 'operation_id': op['id'], 'operation': op,
              'learning': value.model_dump(), 'result': result, 'selected_skills': []}
    store.record('episode', 'orphan', orphan); original = deepcopy(store.get_operation(op['id']))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    with pytest.raises(PolicyError, match='atomic application marker'):
        await engine._learn({'task_id': task['id'], 'operation_id': op['id']})
    assert store.get_operation(op['id']) == original and store.records('learning_contract_failure') == []
    assert store.record_get('episode', 'orphan') == orphan and store.records('knowledge_application') == []
    assert engine.gateway.calls == [] and executor.calls == []
    await engine.close(); store.close()


def test_atomic_application_rechecks_orphan_after_clean_observation(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Close the final apply race', ['No duplicate semantic effect'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    value = learning(skill_updates=[create_update()])
    assert engine.knowledge.application_state(task, op, value, result, [])['state'] == 'not_committed'
    other = Store(store.data_dir)
    orphan = {'id': 'intervening-orphan', 'task_id': task['id'], 'operation_id': op['id']}
    other.record('episode', orphan['id'], orphan); other.close()
    with pytest.raises(PolicyError, match='atomic application marker'):
        engine.knowledge.apply(task, op, value, result, [])
    assert store.records('skill') == [] and store.records('knowledge_application') == []
    assert store.record_get('episode', orphan['id']) == orphan
    store.close()


@pytest.mark.asyncio
async def test_invalid_regeneration_rechecks_orphan_at_atomic_transition(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Protect regeneration against an intervening effect', ['Retain original row'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    value = learning(skill_updates=[{'action': 'create-provisional'}])
    journal(store, task, op, result, value); original = deepcopy(store.get_operation(op['id']))
    validate = engine.knowledge.validate_learning_proposal
    def intervening(*args, **kwargs):
        try: return validate(*args, **kwargs)
        finally: store.record('episode', 'raced', {'id': 'raced', 'task_id': task['id'], 'operation_id': op['id']})
    monkeypatch.setattr(engine.knowledge, 'validate_learning_proposal', intervening)
    with pytest.raises(PolicyError, match='atomic application marker'):
        await engine._learn({'task_id': task['id'], 'operation_id': op['id']})
    assert store.get_operation(op['id']) == original and store.records('learning_contract_failure') == []
    assert store.records('knowledge_application') == [] and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('control,invalid', [(False, False), (False, True), (True, False), (True, True)])
async def test_saved_web_result_recovery_distinguishes_control_from_semantic_orphan(tmp_path, monkeypatch, control, invalid):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Recover exact acquired result', ['No repeated acquisition'])
    op = web_operation(); requests = []; engine.web = collector(store, requests)
    if invalid: engine.gateway = CorrectingGateway(engine.policy, [{'action': 'create-provisional'}])
    with monkeypatch.context() as old:
        if invalid: old.setattr(engine.knowledge, 'validate_learning_proposal', lambda *a, **k: None)
        def interrupted(*args, **kwargs): raise PolicyError('Synthetic known original failure before semantic commit')
        old.setattr(engine.knowledge, 'apply', interrupted)
        with pytest.raises(PolicyError, match='original failure'):
            await engine._research(task['id'], op, 'pre', {})
    saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
    original = deepcopy(saved); exchanges = deepcopy(store.records('web_exchange'))
    acquisition = saved['acquisition_operation']
    if control: prior = control_episode(engine, task, acquisition)
    else:
        prior = {'id': 'web-semantic-orphan', 'task_id': task['id'], 'operation_id': acquisition['id'],
                 'operation': acquisition, 'result': saved['acquisition_result'],
                 'learning': saved['learning_attempts'][-1]['learning'], 'selected_skills': []}
        store.record('episode', prior['id'], prior)
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor); engine.web = collector(store, requests)
    if control:
        actual = await engine._research(task['id'], op, 'pre', {})
        current = store.record_get('web_work', saved['id'])
        assert current['status'] == 'complete' and actual['sources'][0]['text'] == 'exact mock response'
        assert len(current['learning_attempts']) == (2 if invalid else 1)
        assert bool([p for _, phase, p in engine.gateway.calls if phase == 'web_acquisition_learning']) is invalid
        assert current['acquisition_result'] == original['acquisition_result']
        assert current['application_failures'] == original['application_failures']
    else:
        with pytest.raises(PolicyError, match='inconsistent'):
            await engine._research(task['id'], op, 'pre', {})
        assert store.record_get('web_work', saved['id'])['learning_attempts'] == original['learning_attempts']
        assert store.records('knowledge_application') == []
        assert not any(phase == 'web_acquisition_learning' for _, phase, _ in engine.gateway.calls)
    assert store.record_get('episode', prior['id']) == prior
    assert store.records('web_exchange') == exchanges and len(requests) == 1 and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [asyncio.CancelledError, ConfigurationRequired, RuntimeError, Quiesced])
async def test_late_interruption_after_real_final_commit_preserves_completion(tmp_path, monkeypatch, fault):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
    graph = await engine._graph()
    class LateGraph:
        async def ainvoke(self, *args, **kwargs):
            actual = await graph.ainvoke(*args, **kwargs)
            if store.get_task(task['id'])['status'] == 'completed':
                raise fault('Synthetic late boundary interruption after actual final commit')
            return actual
    engine.graph = LateGraph()
    actual = await engine.run_task(task['id'])
    assert actual['task']['status'] == 'completed', actual['events'][-1]
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    assert [o['kind'] for o in executor.calls] == ['file_write', 'file_read']
    final_event = next(e for e in actual['events'] if e['stage'] == 'final' and e['status'] == 'completed')
    assert final_event['detail'] == actual['task']['final']
    tail = [e for e in actual['events'] if e['seq'] > final_event['seq']]
    assert tail[-1]['status'] == 'terminal_preserved' and all(e['status'] != 'stopped' for e in tail)
    assert tail[-1]['detail']['final_sha256'] == digest(actual['task']['final'])
    assert (await engine.run_task(task['id']))['task']['final'] == actual['task']['final']
    assert store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_incomplete_cancellation_preserves_unknown_effect_without_final(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve an incomplete action', ['No fake final'])
    op = operation(); unknown = OperationResult(operation_id=op['id'], status='unknown', effect='unknown').model_dump()
    store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    store.update_operation(op['id'], result=unknown, status='result_recorded')
    async def interrupted(task):
        raise asyncio.CancelledError()
    engine._initialize_task = interrupted
    actual = await engine.run_task(task['id'])
    assert actual['task']['status'] == 'stopped' and actual['task']['final'] is None
    assert actual['events'][-1]['detail']['unknown_effects'] == [op['id']]
    assert store.get_operation(op['id'])['result'] == unknown and executor.calls == []
    await engine.close(); store.close()


class LearningWireFixture(ModelGateway):
    """Real configured packing/response retention for Learning; explicit other judgments."""
    # This historical fixture writes canonical Learning JSON, including faults.
    supports_semantic_wire = False

    def __init__(self, store, policy, state):
        from policy_harness.models import LEARNING_SCHEMAS
        self.state = state
        self.offline = FixtureGateway(policy); self.offline.response_store = store
        self.current_schema = None
        self.current_phase = None
        super().__init__(FakeSettings(api_mode='compatible', model_context_tokens=2_000_000, max_output_tokens=32768),
            policy, transport=httpx.MockTransport(self.respond), response_store=store, response_protector=FixtureCipher())

    async def generate(self, role, phase, payload, schema, **kwargs):
        from policy_harness.models import LEARNING_SCHEMAS, SkillSelection, Assessment, AssessmentBatch
        if schema not in LEARNING_SCHEMAS:
            value, usage = await self.offline.generate(role, phase, payload, schema)
            if schema in {Assessment, AssessmentBatch}:
                # Ordinary before/after assessments create enough real candidate
                # obligations to leave pending work after a successful leaf.
                def candidates(assessment):
                    return assessment.model_copy(update={'ideas': [*assessment.ideas,
                    Idea(id='retain-transport', target='task', proposal='Retain the acquired transport result before interpreting it.',
                         disposition='reject', rationale='The existing exchange journal already preserves this evidence.'),
                    Idea(id='compare-owner', target='workflow', proposal='Compare the current task owner before resuming this acquisition.',
                         disposition='reject', rationale='Use the existing exact source binding without an extra operation.')]})
                if schema is Assessment:
                    value = candidates(value)
                else:
                    value = value.model_copy(update={'assessments': [item.model_copy(update={'assessment': candidates(item.assessment)})
                        for item in value.assessments]})
            if schema is SkillSelection and self.state.get('selected_skill'):
                skill = self.state['selected_skill']
                clause = skill['content']
                selected = {'id': skill['id'], 'hash': skill['hash'], 'reason': 'Controlled actual exchange check',
                    'application': 'Inspect the actual exchange data', 'procedure_clause': clause,
                    'procedure_sha256': hashlib.sha256(clause.encode()).hexdigest()}
                value = value.model_copy(update={'selected': [selected],
                    'rejected': [x for x in value.rejected if x['id'] != skill['id']]})
            if schema.__name__ == 'Disposition':
                from tests.fixture_response_transport import retained_reply
                return await retained_reply(self, role, phase, payload, schema, value, usage=usage, **kwargs)
            return value, usage
        self.current_schema = schema; self.current_phase = phase
        return await super().generate(role, phase, payload, schema, **kwargs)

    def respond(self, request):
        from policy_harness.models import LearningIdeas, LearningApplications, LearningSynthesis
        state = self.state; schema = self.current_schema
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'].split('\n', 1)[1])
        state.setdefault('sent', []).append({'schema': schema.__name__, 'phase': self.current_phase, 'payload': deepcopy(payload)})
        def length():
            response = envelope('{"partial":')
            response['choices'][0]['finish_reason'] = 'length'
            return httpx.Response(200, json=response)
        required = payload.get('required_ideas', [])
        if schema is Learning:
            if not payload.get('source_packet_id'):
                return length()
            if state.get('seed_fail'):
                state['seed_fail'] = False
                return httpx.Response(503, text='Synthetic actual later endpoint unavailable')
            returned = required
            if not payload.get('actual_format_feedback') and not state.get('first_legacy_rejected'):
                state['first_legacy_rejected'] = True
                returned = payload['bounded_context']['required_ideas']
            update = deepcopy(state.get('page_update', create_update()))
            value = learning(ideas=returned, skill_updates=[update]).model_dump()
            selected = payload.get('pre', {}).get('skills', {}).get('selected', [])
            if selected:
                chosen = selected[0]; contract = payload['application_contract']
                value['applications'] = [{'skill_id': chosen['id'], 'skill_hash': chosen['hash'],
                    'procedure_clause': chosen['procedure_clause'], 'procedure_sha256': chosen['procedure_sha256'],
                    'operation_id': payload['operation']['id'], 'operation_sha256': contract['operation_sha256'],
                    'result_sha256': contract['result_sha256'], 'evidence': [{'pointer': '/data',
                        'sha256': digest(payload['result']['data']), 'explanation': 'Controlled observation of the acquired exchange data'}]}]
        elif schema is LearningIdeas:
            if state.get('fresh') and not payload.get('actual_format_feedback'):
                if state.get('first_fresh_completed') and not state.get('later_fault'):
                    state['later_fault'] = True
                    return httpx.Response(503, text='Synthetic fault after corrected leaf')
                if len(required) > 2:
                    return length()
                if not state.get('new_rejected'):
                    state['new_rejected'] = True
                    value = {'ideas': [*required, NEW_IDEA], 'wrong_shape': 'Retain the complete Idea in a rejected response'}
                    return httpx.Response(200, json=envelope(json.dumps(value)))
            value = {'ideas': required}
            if state.get('fresh') and payload.get('actual_format_feedback'):
                state['first_fresh_completed'] = True
        elif schema is LearningApplications:
            value = {'considered_skill_ids': [x['id'] for x in payload['pre']['skills']['selected']],
                     'applications': [], 'ideas': required}
        else:
            assert schema is LearningSynthesis
            if state.get('synthesis_length'):
                return length()  # Actual synthetic HTTP length, not input-count inference.
            value = {k: v for k, v in learning(ideas=required, skill_updates=[deepcopy(state.get('coherent_update', create_update()))]).model_dump().items() if k != 'applications'}
            if state.get('fresh') and not state.get('foreign_idea_rejected'):
                state['foreign_idea_rejected'] = True
                value['ideas'] = [NEW_IDEA]
            if state.get('competing') and not state.get('composition_rejected'):
                state['composition_rejected'] = True
                value['skill_updates'] = [deepcopy(state['first_update']), deepcopy(state['other_update'])]
            state.setdefault('synthesis_inputs', []).append(deepcopy(payload))
        if schema.__name__ == state.get('new_owner') and not state.get('owned_new_idea_sent'):
            state['owned_new_idea_sent'] = True
            value['ideas'] = [*value['ideas'], deepcopy(NEW_IDEA)]
        return httpx.Response(200, json=envelope(json.dumps(value)))


async def learning_web_frontier(tmp_path, *, historical=False, fresh=False, competing=False, new_owner=None, synthesis_length=False):
    """Ordinary Web acquisition; historical setup only replaces the old page producer."""
    from policy_harness.bounded_judgments import BoundedJudgments
    store, engine, executor, _ = runtime(tmp_path)
    if historical:
        # Produce the real pre-controller-context request before measurement,
        # capture and send; removing its trace later must not mix two formats.
        model_input = engine._model_input
        def historical_input(*args, **kwargs):
            return model_input(*args, **dict(kwargs, installed_context=None))
        engine._model_input = historical_input
    task = store.create_task('Retain every acquired result and considered candidate', ['One actual Knowledge application'])
    parent = web_operation(); requests = []; state = {'fresh': fresh, 'competing': competing,
        'new_owner': new_owner, 'synthesis_length': synthesis_length}
    if competing or new_owner == 'LearningApplications':
        skill = engine.knowledge._new(task, 'fixture-seed', {'title': 'Exact exchange observation',
            'content': 'Inspect actual acquired exchange data.', 'applicability': 'Public acquisitions', 'next_trigger': 'Next actual exchange'})
        state['selected_skill'] = skill
        state['page_update'] = {'action': 'improve', 'id': skill['id'], 'expected_hash': skill['hash'], 'content': 'Preserve transport failure.'}
        state['first_update'] = deepcopy(state['page_update'])
        state['other_update'] = dict(state['page_update'], content='Retain exact original request identity.')
        state['coherent_update'] = dict(state['page_update'], content='Preserve transport failure and retain exact original request identity.')
    engine.gateway = LearningWireFixture(store, engine.policy, state)
    engine.bounded_judgments = BoundedJudgments(engine)
    bounded = engine.bounded_judgments
    if not fresh and not new_owner and not synthesis_length:
        # Explicit candidate16 producer-order setup. Canonical Store reload
        # changes object order, but the old pointer list survives in failed
        # actual_model_input and (unless historical) the successful trace.
        def old_pointers(value, prefix=''):
            rows = [{'pointer': prefix, 'sha256': digest(value)}] if prefix else []
            if isinstance(value, dict):
                for key, child in value.items():
                    rows.extend(old_pointers(child, prefix + '/' + str(key).replace('~', '~0').replace('/', '~1')))
            elif isinstance(value, list):
                for index, child in enumerate(value):rows.extend(old_pointers(child, prefix + '/' + str(index)))
            return rows
        engine._evidence_pointers = old_pointers
        if historical:
            async def old_correction(task_id, phase, record, revised, schema, role, rejected):
                # Candidate16's exact old recursive transport: no parent/link field.
                return await bounded._call(task_id, phase, revised, schema, role=role, _rejected_shapes=rejected)
            bounded._correct_learning = old_correction
        async def old_pages(task_id, phase, payload):
            assert phase == 'web_acquisition_learning' and len(payload['required_ideas']) > 3
            # Reproduce an actual old full-output exhaustion, followed by the
            # old full Learning focus writer. No corrective operation is injected.
            with pytest.raises(ProviderError) as fault:
                await bounded._call(task_id, phase, payload, Learning)
            assert fault.value.metadata['truncated'] is True
            packet = bounded._packet(task_id, phase + ':learning', payload)
            def focus(ideas, selected=()):
                return {'operation': payload['operation'], 'result': payload['result'], 'source_packet_id': packet['id'],
                    'required_ideas': ideas, 'bounded_context': deepcopy(payload),
                    'pre': {'skills': {'selected': list(selected), 'rejected': [], 'new_knowledge_needed': [], 'rationale': 'Old exact source focus'}},
                    'instruction': 'Old full Learning page: only this focus may be disposed; later whole review remains mandatory.'}
            first = await bounded._call(task_id, phase, focus(payload['required_ideas'][:2], payload['pre']['skills']['selected']), Learning)
            state['first_leaf_ids'] = [i.id for i in first.ideas]
            if competing:
                state['page_update'] = state['other_update']
                await bounded._call(task_id, phase, focus(payload['required_ideas'][2:4]), Learning)
            state['seed_fail'] = True
            await bounded._call(task_id, phase, focus(payload['required_ideas'][4:] or payload['required_ideas'][2:]), Learning)
            raise AssertionError('The actual later 503 should preserve this incomplete source')
        bounded.learning_proposal = old_pages
    if new_owner:
        call = bounded._call
        async def pause_after_success(task_id, phase, payload, schema, **kwargs):
            value = await call(task_id, phase, payload, schema, **kwargs)
            if schema.__name__ == new_owner and not state.get('owner_paused'):
                state['owner_paused'] = True
                raise asyncio.CancelledError('Controlled interruption after the exact owner success was retained')
            return value
        bounded._call = pause_after_success
    web = collector(store, requests)
    expected = (pytest.raises(asyncio.CancelledError, match='exact owner success') if new_owner
                else pytest.raises(ProviderError) if synthesis_length
                else pytest.raises(ProviderError, match='HTTP 503'))
    with expected as stopped:
        await web.collect('https://example.com/actual', phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    if synthesis_length:
        assert stopped.value.metadata['truncated'] is True and stopped.value.metadata['finish_reason'] == 'length'
    after = next(w for w in store.records('web_work') if w['stage'] == 'after')
    assert after['learning_attempts'] == [] and not store.records('knowledge_application') and len(requests) == 1
    if historical:
        # Explicit old-format persisted-state setup. Exact observed request,
        # response and event bytes remain; only fields absent in candidate16 and
        # its then-nonexistent return index are absent in this isolated fixture.
        for record in store.records('bounded_model_call'):
            if record['phase'] != 'web_acquisition_learning': continue
            for key in ('learning_trace', 'learning_parent', 'learning_schema', 'learning_owner'): record.pop(key, None)
            store.record('bounded_model_call', record['id'], record)
            if record['status'] == 'succeeded': store.record('bounded_model_completed', record['key'], record)
            if record.get('metadata', {}).get('truncated'): store.record('bounded_model_truncated', record['key'], record)
        store.db.execute("DELETE FROM records WHERE kind='bounded_learning_return'")
    originals = deepcopy(store.records('bounded_model_call'))
    responses = deepcopy(store.records('model_response'))
    exchanges = deepcopy(store.records('web_exchange'))
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    return task, parent, after, originals, responses, exchanges, requests, data_dir, policy, state


@pytest.mark.asyncio
@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('competing', [False, True])
async def test_web_reopen_reuses_corrected_leaf_and_reviews_one_coherent_application(tmp_path, historical, competing):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, parent, after, originals, responses, exchanges, requests, data_dir, policy, state = await learning_web_frontier(
        tmp_path, historical=historical, competing=competing)
    count = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = LearningWireFixture(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    first_success = next(r for r in originals if r['status'] == 'succeeded' and r['phase'] == 'web_acquisition_learning')
    retained, measured = engine.bounded_judgments._retained_learning_request(task['id'], first_success, Learning, task['actor'])
    current_input = engine._model_input(task['id'], first_success['phase'], first_success['payload'], Learning,
        installed_context=retained.get('installed_controller_context'))
    assert measured == first_success['measurement'] and digest(retained) == measured['model_input_sha256']
    assert current_input != retained and engine.bounded_judgments._same_evidence_input(current_input, retained)
    assert sorted(current_input['application_contract']['result_evidence'], key=lambda x: x['pointer']) == sorted(
        retained['application_contract']['result_evidence'], key=lambda x: x['pointer'])
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', after['id'])
    assert current['status'] == 'complete' and len(current['learning_attempts']) == 1
    assert all(x['schema'] != 'Learning' for x in state['sent'][count:]), 'Ordinary reconstruction must not resend old full Learning or its leaf'
    assert current['acquisition_operation'] == after['acquisition_operation'] and current['acquisition_result'] == after['acquisition_result']
    actual = current['applied_learning']; ids = [i['id'] for i in actual['ideas']]
    assert len(ids) == len(set(ids)) and set(state['first_leaf_ids']) <= set(ids)
    review_input = current['learning_attempts'][0]['review_input']
    composition = review_input['learning_composition']
    assert composition['legacy_proposals'] and all(x['source_receipt'] for x in composition['legacy_proposals'])
    if competing:
        assert state['composition_rejected'] and len(state['synthesis_inputs']) == 2
        assert state['synthesis_inputs'][1]['actual_format_feedback']['rejected_response']['skill_updates']
        assert actual['skill_updates'] == [state['coherent_update']]
        assert store.record_get('skill', state['selected_skill']['id'])['content'] == state['coherent_update']['content']
        assert len(store.records('skill_application')) == 1
    applications = deepcopy(store.records('knowledge_application'))
    assert len(applications) == 1
    episode = store.record_get('episode', applications[0]['outcome']['episode_id'])
    assert episode['learning'] == actual and episode['result'] == after['acquisition_result']
    for old in originals: assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses: assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    sent = len(state['sent']); await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == sent and store.records('knowledge_application') == applications
    assert store.verify_events()
    await engine.close(); store.close()
    store, engine = web_reopen(data_dir, policy)
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application') == applications and store.record_get('episode', episode['id']) == episode
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_same_phase_reopen_keeps_new_response_idea_owned_and_corrects_synthesis_echo(tmp_path):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, _, after, originals, responses, exchanges, requests, data_dir, policy, state = await learning_web_frontier(tmp_path, fresh=True)
    success = next(r for r in originals if r.get('learning_schema') == 'LearningIdeas' and r['status'] == 'succeeded')
    assert NEW_IDEA in success['result']['ideas']
    assert NEW_IDEA not in success['payload']['required_ideas'], 'N first existed inside this request correction, not its original prompt'
    before = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = LearningWireFixture(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', after['id']); actual = current['applied_learning']
    assert sum(i['id'] == NEW_IDEA['id'] for i in actual['ideas']) == 1
    assert next(i for i in actual['ideas'] if i['id'] == NEW_IDEA['id']) == NEW_IDEA
    assert state['foreign_idea_rejected'] and len(state['synthesis_inputs']) == 2
    corrected = state['synthesis_inputs'][1]
    assert corrected['required_ideas'] == [] and corrected['actual_format_feedback']['rejected_response']['ideas'] == [NEW_IDEA]
    assert not any(x['schema'] == 'LearningIdeas' and x['payload']['learning_focus'] == success['payload']['learning_focus'] for x in state['sent'][before:])
    assert store.record_get('bounded_model_call', success['id']) == success
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    assert len(store.records('knowledge_application')) == 1
    applications = deepcopy(store.records('knowledge_application')); sent = len(state['sent'])
    await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == sent and store.records('knowledge_application') == applications
    assert store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['receipt', 'response', 'configuration', 'actor', 'history', 'missing_order'])
async def test_historical_corrected_leaf_provenance_change_holds_without_model_or_effect_replay(tmp_path, changed):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, _, after, originals, responses, exchanges, requests, data_dir, policy, state = await learning_web_frontier(tmp_path, historical=True)
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = LearningWireFixture(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    success = next(r for r in originals if r['status'] == 'succeeded' and r['phase'] == 'web_acquisition_learning')
    if changed == 'receipt':
        corrupt = deepcopy(success); corrupt['measurement']['model_input_sha256'] = '0'*64
        store.record('bounded_model_call', corrupt['id'], corrupt)
        store.record('bounded_model_completed', corrupt['key'], corrupt)
    elif changed == 'response':
        event = next(e for e in store.events(task['id']) if e['status'] == 'succeeded' and e['detail'].get('result') == success['raw_result'])
        reference = event['detail']['measurement']['usage']['response_record']
        corrupt = store.record_get('model_response', reference['id']); corrupt['messages_sha256'] = '0'*64
        store.record('model_response', corrupt['id'], corrupt)
    elif changed == 'configuration':
        engine.gateway.settings.values['max_output_tokens'] += 1
    elif changed == 'actor':
        # Deliberate persisted-owner corruption in this isolated fixture. The
        # real Store has id/body columns and intentionally no owner update API.
        corrupt = store.get_task(task['id']); corrupt['actor'] = 'reviewer'
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(corrupt, sort_keys=True), task['id']))
    elif changed == 'history':
        # A real additional proposed row changes history_lookup. An evidence
        # ordering exception cannot excuse this independent source difference.
        store.save_operation(task['id'], web_operation())
    else:
        # Pre-trace old success has no representation left after these retained
        # failed-input witnesses are removed. A digest cannot reconstruct it.
        for record in store.records('bounded_model_call'):
            if record.get('actual_model_input'):
                record.pop('actual_model_input'); record.pop('actual_model_input_sha256')
                store.record('bounded_model_call', record['id'], record)
    sent = len(state['sent'])
    with pytest.raises(PolicyError, match='LEARNING_PROVENANCE'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == sent and store.records('knowledge_application') == []
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    assert store.record_get('web_work', after['id'])['acquisition_result'] == after['acquisition_result']
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['LearningIdeas', 'LearningApplications', 'LearningSynthesis'])
async def test_each_successful_output_owner_keeps_its_new_idea_through_web_reopen(tmp_path, owner):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, _, after, originals, responses, exchanges, requests, data_dir, policy, state = await learning_web_frontier(
        tmp_path, new_owner=owner)
    success = next(r for r in originals if r.get('learning_schema') == owner and r['status'] == 'succeeded')
    mapping = next(m for m in success['idea_namespaces'] if m['original_id'] == NEW_IDEA['id'])
    owned_id = mapping['controller_id']
    assert owned_id != NEW_IDEA['id'] and owned_id.startswith('bounded-idea:')
    original_idea = next(i for i in success['result']['ideas'] if i['id'] == owned_id)
    count = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = LearningWireFixture(store, policy, state)
    engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', after['id'])
    assert current['status'] == 'complete'
    actual = current['applied_learning']['ideas']
    assert [i for i in actual if i['id'] == owned_id] == [original_idea]
    assert not any(s['schema'] == owner and s['payload'].get('learning_focus') == success['payload']['learning_focus']
                   for s in state['sent'][count:]), 'The authenticated successful owner must not be regenerated'
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    applications = deepcopy(store.records('knowledge_application'))
    assert len(applications) == 1
    episode = store.record_get('episode', applications[0]['outcome']['episode_id'])
    assert episode['learning'] == current['applied_learning'] and episode['result'] == after['acquisition_result']
    await engine.close(); store.close()
    store, engine = web_reopen(data_dir, policy)
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application') == applications
    assert store.record_get('episode', episode['id']) == episode and store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_actual_global_synthesis_length_remains_incomplete_without_unchanged_replay(tmp_path):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, _, after, originals, responses, exchanges, requests, data_dir, policy, state = await learning_web_frontier(
        tmp_path, synthesis_length=True)
    failed = next(r for r in originals if r.get('learning_schema') == 'LearningSynthesis' and r['status'] == 'failed')
    assert failed['metadata']['truncated'] is True and failed['metadata']['finish_reason'] == 'length'
    assert failed['actual_model_input_sha256'] == digest(failed['actual_model_input'])
    assert failed['metadata']['response_record']['id'] in {r['id'] for r in responses}
    count = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = LearningWireFixture(store, policy, state)
    engine.bounded_judgments = BoundedJudgments(engine)
    with pytest.raises(ProviderError) as held:
        await engine.web_judgments.drain(task['id'])
    assert held.value.metadata == failed['metadata']
    assert held.value.bounded_call_ref == {'id': failed['id'], 'key': failed['key'], 'sha256': digest(failed)}
    assert len(state['sent']) == count and store.records('knowledge_application') == []
    assert store.record_get('web_work', after['id'])['learning_attempts'] == []
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1 and store.verify_events()
    await engine.close(); store.close()


class FeedbackTransportGateway(LearningWireFixture):
    """One completed owner, then a real formal rejection and corrected HTTP 500."""
    def respond(self, request):
        from policy_harness.models import LearningIdeas
        if self.current_schema is not LearningIdeas:
            return super().respond(request)
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'].split('\n', 1)[1])
        state = self.state
        state.setdefault('sent', []).append({'schema': 'LearningIdeas', 'phase': self.current_phase,
                                             'payload': deepcopy(payload)})
        required = payload['required_ideas']
        if not state.get('completed_ideas'):
            if len(required) > 2:
                response = envelope('{"partial":')
                response['choices'][0]['finish_reason'] = 'length'
                return httpx.Response(200, json=response)
            state['completed_ideas'] = deepcopy(required)
            return httpx.Response(200, json=envelope(json.dumps({'ideas': required})))
        if not state.get('rejected_focus'):
            state['rejected_focus'] = deepcopy(payload['learning_focus'])
            ideas = [*required, *state['completed_ideas']]
            if state.get('new_idea'):ideas.append(deepcopy(NEW_IDEA))
            state['rejected_value'] = {'ideas': deepcopy(ideas)}
            return httpx.Response(200, json=envelope(json.dumps({'ideas': ideas})))
        if not state.get('http_failed'):
            assert payload['actual_format_feedback']['rejected_response'] == state['rejected_value']
            assert payload['learning_focus'] == state['rejected_focus']
            state['http_failed'] = True
            state['failed_input'] = deepcopy(payload)
            if state.get('unobserved'):
                raise httpx.ReadTimeout('Synthetic lost corrected response', request=request)
            return httpx.Response(500, text='Internal server error')
        ideas = required
        if state.get('repeat_defect'):
            ideas = [*required, *state['completed_ideas']]
        return httpx.Response(200, json=envelope(json.dumps({'ideas': ideas})))


async def feedback_transport_frontier(tmp_path, *, historical=False, new_idea=False, unobserved=False):
    """The original failure is produced through the normal Web/Engine graph."""
    from policy_harness.bounded_judgments import BoundedJudgments
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Resume the exact corrected pending Web decision', ['Retain all Ideas and apply once'])
    parent = web_operation(); requests = []; state = {'new_idea': new_idea, 'unobserved': unobserved}
    engine.gateway = FeedbackTransportGateway(store, engine.policy, state)
    engine.bounded_judgments = BoundedJudgments(engine)
    web = collector(store, requests)
    with pytest.raises(ProviderError, match='delivery and usage may be unknown' if unobserved else 'HTTP 500') as fault:
        await web.collect('https://example.com/actual', phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    if unobserved:
        assert fault.value.metadata['request_effect'] == 'response-unobserved'
    else:
        assert fault.value.metadata['http_status'] == 500 and not fault.value.metadata.get('truncated')
    after = next(w for w in store.records('web_work') if w['stage'] == 'after')
    failed = next(r for r in store.records('bounded_model_call') if
        (r.get('metadata', {}).get('request_effect') == 'response-unobserved' if unobserved
         else r.get('metadata', {}).get('http_status') == 500))
    succeeded = [r for r in store.records('bounded_model_call') if r.get('learning_schema') == 'LearningIdeas' and r['status'] == 'succeeded']
    assert len(succeeded) == 1
    assert succeeded[0]['result']['ideas'] == state['completed_ideas']
    assert failed['actual_model_input'] == state['failed_input']
    assert failed['actual_model_input_sha256'] == digest(state['failed_input'])
    assert after['learning_attempts'] == [] and store.records('knowledge_application') == []
    assert len(requests) == 1
    if historical:
        # Explicit candidate19 persistence shape. The request/measurement,
        # protected responses and exact Engine events remain untouched.
        failed['learning_trace'] = {'call_id': failed['id']}
        store.record('bounded_model_call', failed['id'], failed)
    originals = deepcopy(store.records('bounded_model_call'))
    responses = deepcopy(store.records('model_response'))
    events = deepcopy(store.events(task['id'])); exchanges = deepcopy(store.records('web_exchange'))
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    return task, after, failed, succeeded[0], originals, responses, events, exchanges, requests, data_dir, policy, state


@pytest.mark.asyncio
@pytest.mark.parametrize('unobserved', [False, True])
async def test_failed_model_restart_requires_observed_unadmitted_request(tmp_path, unobserved):
    from policy_harness.models import LearningIdeas, PolicyError
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, success, originals, responses, events, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(
        tmp_path, unobserved=unobserved)
    store, engine = web_reopen(data_dir, policy)
    engine.gateway=FeedbackTransportGateway(store,policy,state)
    try:
        if unobserved:
            with pytest.raises(PolicyError,match='observed retryable'):
                engine.bounded_judgments.failed_learning_restart(task['id'],failed,schema=LearningIdeas)
        else:
            restart=engine.bounded_judgments.failed_learning_restart(task['id'],failed,schema=LearningIdeas)
            assert restart['source_receipt']==engine.bounded_judgments._call_ref(failed)
            assert restart['required_ideas']==failed['actual_model_input']['required_ideas']
            assert restart['target_effect']=='none; proposal not admitted'
            assert restart['usage'] is None and restart['billing']=='unknown'
            changed=deepcopy(failed);changed['status']='succeeded'
            with pytest.raises(PolicyError,match='LEARNING_PROVENANCE'):
                engine.bounded_judgments.failed_learning_restart(task['id'],changed,schema=LearningIdeas)
        assert store.records('web_exchange')==exchanges and len(requests)==1
        assert store.record_get('web_work',after['id'])==after
        assert all(store.record_get('bounded_model_call',r['id'])==r for r in originals)
    finally:
        await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('new_idea', [False, True])
async def test_web_reopen_restores_actual_feedback_after_http500_without_completed_replay(tmp_path, historical, new_idea):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, success, originals, responses, events, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(
        tmp_path, historical=historical, new_idea=new_idea)
    before = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    sent = state['sent'][before:]
    assert sent and sent[0]['schema'] == 'LearningIdeas'
    resumed = sent[0]['payload']
    assert resumed['actual_format_feedback'] == failed['actual_model_input']['actual_format_feedback']
    assert resumed['learning_focus'] == failed['payload']['learning_focus']
    assert resumed['required_ideas'] == failed['actual_model_input']['required_ideas']
    completed_ids = {i['id'] for i in success['result']['ideas']}
    assert not completed_ids.intersection(i['id'] for i in resumed['required_ideas'])
    assert not any(s['schema'] == 'LearningIdeas' and s['payload']['learning_focus'] == success['payload']['learning_focus'] for s in sent)
    current = store.record_get('web_work', after['id'])
    assert current['status'] == 'complete' and len(current['learning_attempts']) == 1
    actual = current['applied_learning']; actual_ids = [i['id'] for i in actual['ideas']]
    assert len(actual_ids) == len(set(actual_ids)) and completed_ids <= set(actual_ids)
    for original in failed['actual_model_input']['required_ideas']:
        assert [i for i in actual['ideas'] if i['id'] == original['id']] == [original]
    if new_idea:
        assert [i for i in resumed['required_ideas'] if i['id'] == NEW_IDEA['id']] == [NEW_IDEA]
        assert [i for i in actual['ideas'] if i['id'] == NEW_IDEA['id']] == [NEW_IDEA]
    recovered = next(r for r in store.records('bounded_model_call')
        if r.get('learning_trace', {}).get('feedback_recovery', {}).get('id') == failed['id'] and r['status'] == 'succeeded')
    assert recovered['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(failed)
    assert recovered['learning_trace']['actual_model_input'] == resumed
    assert current['acquisition_result'] == after['acquisition_result']
    applications = deepcopy(store.records('knowledge_application')); assert len(applications) == 1
    episode = store.record_get('episode', applications[0]['outcome']['episode_id'])
    assert episode['learning'] == actual and episode['result'] == after['acquisition_result']
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.events(task['id'])[:len(events)] == events
    assert store.records('web_exchange') == exchanges and len(requests) == 1 and store.verify_events()
    count = len(state['sent']); await engine.close(); store.close()
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == count and store.records('knowledge_application') == applications
    assert store.record_get('episode', episode['id']) == episode
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_restored_feedback_repeated_defect_stays_held_across_reopen(tmp_path):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, _, originals, responses, _, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(tmp_path)
    state['repeat_defect'] = True
    before = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    with pytest.raises(PolicyError, match='repeated the same proposal-contract defect'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == before + 1
    assert state['sent'][-1]['payload']['actual_format_feedback'] == failed['actual_model_input']['actual_format_feedback']
    repeated = next(r for r in store.records('bounded_model_call') if r.get('first_fault', {}).get('message', '').startswith('Model repeated the same'))
    assert repeated['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(failed)
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    await engine.close(); store.close()
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    with pytest.raises(PolicyError, match='saved correction repeated a known defect'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == before + 1 and store.records('knowledge_application') == []
    assert store.record_get('bounded_model_call', repeated['id']) == repeated
    assert store.record_get('web_work', after['id'])['learning_attempts'] == []
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['feedback', 'focus', 'operation', 'response', 'event', 'metadata', 'ambiguous', 'settings', 'role'])
async def test_saved_transport_feedback_provenance_change_holds_before_next_send(tmp_path, changed):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, _, originals, _, events, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(tmp_path, historical=True, new_idea=changed in {'settings', 'role'})
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    if changed in {'feedback', 'focus', 'operation', 'metadata'}:
        corrupt = deepcopy(failed)
        if changed == 'feedback':
            corrupt['actual_model_input']['actual_format_feedback']['instruction'] = 'Silently discard the rejected Ideas'
            corrupt['actual_model_input_sha256'] = digest(corrupt['actual_model_input'])
        elif changed == 'focus':
            corrupt['payload']['learning_focus']['identities'][0]['proposal'] = 'Different original focus'
        elif changed == 'operation':
            corrupt['payload']['operation']['id'] = 'different-operation'
        else:
            corrupt['metadata'] = {}  # The exact HTTP terminal must still prevent a cache miss/retry.
        store.record('bounded_model_call', corrupt['id'], corrupt)
    elif changed == 'response':
        corrupt = store.record_get('model_response', failed['metadata']['response_record']['id'])
        corrupt['messages_sha256'] = '0' * 64
        store.record('model_response', corrupt['id'], corrupt)
    elif changed == 'event':
        rejected = next(e for e in events if e['status'] == 'rejected_model_output' and e['stage'] == failed['phase'])
        corrupt = deepcopy(rejected); corrupt['detail']['rejected_response']['ideas'] = []
        store.db.execute('UPDATE events SET detail=? WHERE seq=?', (json.dumps(corrupt['detail'], sort_keys=True), rejected['seq']))
    elif changed == 'settings':
        engine.gateway.settings.values['max_output_tokens'] += 1
    elif changed == 'ambiguous':
        # One actual terminal cannot authenticate two independent historical
        # attempts; a copied result/hash is not a unique request identity.
        corrupt = deepcopy(failed); corrupt['id'] = 'different-historical-call'
        corrupt['learning_trace'] = {'call_id': corrupt['id']}
        store.record('bounded_model_call', corrupt['id'], corrupt)
    else:
        corrupt = store.get_task(task['id']); corrupt['actor'] = 'reviewer'
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(corrupt, sort_keys=True), task['id']))
    if changed in {'settings', 'role'}:
        from policy_harness.models import LearningIdeas
        count = len(state['sent'])
        previous_responses = deepcopy(store.records('model_response'))
        # Exact stale-request reuse must stop before sending. A separate normal
        # current-configuration plan remains eligible for fresh judgments.
        with pytest.raises(PolicyError, match='LEARNING_PROVENANCE'):
            await engine.bounded_judgments._call(task['id'], failed['phase'], failed['payload'], LearningIdeas)
        assert len(state['sent']) == count and store.records('knowledge_application') == []
        old_ids = {row['id'] for row in store.records('bounded_model_call')}
        await engine.web_judgments.drain(task['id'])
        assert len(state['sent']) > count
        current_role = store.get_task(task['id'])['actor']
        current_output = engine.gateway.settings.values['max_output_tokens']
        fresh = [row for row in store.records('bounded_model_call')
                 if row['id'] not in old_ids and row.get('learning_schema') and row['status'] == 'succeeded']
        assert {row['learning_schema'] for row in fresh} == {'LearningIdeas', 'LearningSynthesis'}
        for row in fresh:
            assert row['learning_trace']['role'] == current_role
            assert row['measurement']['reserved_output_tokens'] == current_output
            assert not row['learning_trace'].get('feedback_recovery')
            assert not row['learning_trace']['actual_model_input'].get('actual_format_feedback')
            assert row['payload']['source_packet_id'] != failed['payload']['source_packet_id']
        packet = store.record_get('bounded_input', failed['payload']['source_packet_id'])
        required = deepcopy(packet['value']['required_ideas'])
        for original in failed['actual_model_input']['required_ideas']:
            if original['id'] not in {idea['id'] for idea in required}:
                required.append(original)
        assert [idea for idea in required if idea['id'] == NEW_IDEA['id']] == [NEW_IDEA]
        fresh_ideas = [row for row in fresh if row['learning_schema'] == 'LearningIdeas']
        assert len(fresh_ideas) == 1 and fresh_ideas[0]['payload']['required_ideas'] == required
        current = store.record_get('web_work', after['id'])
        assert current['status'] == 'complete' and len(current['learning_attempts']) == 1
        assert current['acquisition_result'] == after['acquisition_result']
        actual = current['applied_learning']
        # Policy-required choice assessments add Ideas before final review.
        attempt = current['learning_attempts'][0]
        assessed = [idea for assessment in attempt['assessments']
                    for idea in assessment['assessment']['ideas']]
        assert assessed
        expected = [*required, *assessed]
        assert len(expected) == len({idea['id'] for idea in expected})
        assert actual['ideas'] == expected
        assert attempt['review_input']['learning'] == actual
        for original in expected:
            assert [idea for idea in actual['ideas'] if idea['id'] == original['id']] == [original]
        applications = deepcopy(store.records('knowledge_application'))
        assert len(applications) == 1
        episode = store.record_get('episode', applications[0]['outcome']['episode_id'])
        assert episode['learning'] == actual and episode['result'] == after['acquisition_result']
        for original in originals:
            assert store.record_get('bounded_model_call', original['id']) == original
        for original in previous_responses:
            assert store.record_get('model_response', original['id']) == original
        assert store.events(task['id'])[:len(events)] == events and store.verify_events()
        assert store.records('web_exchange') == exchanges and len(requests) == 1
        count = len(state['sent'])
        await engine.close(); store.close()
        store, engine = web_reopen(data_dir, policy)
        engine.gateway = FeedbackTransportGateway(store, policy, state)
        engine.gateway.settings.values['max_output_tokens'] = current_output
        engine.bounded_judgments = BoundedJudgments(engine)
        assert store.get_task(task['id'])['actor'] == current_role
        await engine.web_judgments.drain(task['id'])
        assert len(state['sent']) == count and store.records('knowledge_application') == applications
        assert store.record_get('episode', episode['id']) == episode
        assert store.records('web_exchange') == exchanges and len(requests) == 1
        await engine.close(); store.close()
        return

    count = len(state['sent'])
    with pytest.raises(PolicyError, match='LEARNING_PROVENANCE'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == count and store.records('knowledge_application') == []
    assert store.record_get('web_work', after['id'])['acquisition_result'] == after['acquisition_result']
    assert store.record_get('web_work', after['id'])['learning_attempts'] == []
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    for old in originals:
        if old['id'] != failed['id']:assert store.record_get('bounded_model_call', old['id']) == old
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['source', 'policy', 'stop'])
async def test_pending_transport_request_keeps_source_policy_and_stop_guards(tmp_path, monkeypatch, changed):
    from policy_harness.bounded_judgments import BoundedJudgments
    from policy_harness.models import LearningIdeas
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, _, originals, responses, _, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(tmp_path)
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    if changed == 'source':
        store.append_instruction(task['id'], 'Reassess the old proposal under this new source before continuing.', task['source_hash'])
    elif changed == 'policy':
        monkeypatch.setattr(policy, 'hash', digest({'fixture_policy': 'different-current-policy'}))
    else:
        engine.stop_requested.add(task['id'])
    count = len(state['sent'])
    # The exact next pending Engine caller cannot apply old feedback to a new
    # source/policy. Current-source Web reassessment is a separate existing route.
    with pytest.raises(asyncio.CancelledError if changed == 'stop' else PolicyError):
        await engine.bounded_judgments._call(task['id'], failed['phase'], failed['payload'], LearningIdeas)
    assert len(state['sent']) == count and store.records('knowledge_application') == []
    assert store.record_get('web_work', after['id'])['learning_attempts'] == []
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_second_http_failure_retains_feedback_lineage_until_normal_resume(tmp_path):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, _, originals, _, _, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(tmp_path)
    state['http_failed'] = False
    count = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    with pytest.raises(ProviderError, match='HTTP 500'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == count + 1 and store.records('knowledge_application') == []
    second = next(r for r in store.records('bounded_model_call')
        if r.get('metadata', {}).get('http_status') == 500 and r['id'] != failed['id'])
    assert second['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(failed)
    assert second['actual_model_input']['actual_format_feedback'] == failed['actual_model_input']['actual_format_feedback']
    responses = deepcopy(store.records('model_response'))
    await engine.close(); store.close()
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    await engine.web_judgments.drain(task['id'])
    returned = next(r for r in store.records('bounded_model_call')
        if r.get('learning_trace', {}).get('feedback_recovery', {}).get('id') == second['id'] and r['status'] == 'succeeded')
    assert returned['learning_trace']['feedback_recovery'] == engine.bounded_judgments._call_ref(second)
    assert store.record_get('web_work', after['id'])['status'] == 'complete'
    assert len(store.records('knowledge_application')) == 1
    for old in [*originals, second]:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1 and store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_unobserved_corrected_transport_holds_without_inventing_http_completion(tmp_path):
    from policy_harness.bounded_judgments import BoundedJudgments
    from tests.test_web_recovery import reopen as web_reopen
    task, after, failed, _, originals, responses, _, exchanges, requests, data_dir, policy, state = await feedback_transport_frontier(tmp_path, unobserved=True)
    count = len(state['sent'])
    store, engine = web_reopen(data_dir, policy)
    engine.gateway = FeedbackTransportGateway(store, policy, state); engine.bounded_judgments = BoundedJudgments(engine)
    with pytest.raises(PolicyError, match='unobserved saved Learning request; no resend'):
        await engine.web_judgments.drain(task['id'])
    assert len(state['sent']) == count and failed['metadata']['request_effect'] == 'response-unobserved'
    assert store.records('knowledge_application') == [] and store.record_get('web_work', after['id'])['learning_attempts'] == []
    for old in originals:assert store.record_get('bounded_model_call', old['id']) == old
    for old in responses:assert store.record_get('model_response', old['id']) == old
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    await engine.close(); store.close()
