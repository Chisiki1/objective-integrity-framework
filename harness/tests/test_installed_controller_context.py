"""Installed facts at real input/retention boundaries; synthetic model responses."""
import asyncio
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from policy_harness import models as m
from policy_harness import semantic_wire as wire
from policy_harness.bounded_judgments import ContextObservation
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import operation
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_semantic_wire_contract import install, repeated_plan_context
from tests.test_web_recovery import collector
from policy_harness.providers import ProviderError


def assert_installed_facts(call, engine):
    facts = call['canonical']['installed_controller_context']
    assert facts['version'] == wire.INSTALLED_CONTEXT
    guide = engine._controller_guide()
    for kind in ('file_write', 'file_read', 'file_list'):
        assert facts['operation_contracts'][kind] == guide[kind]
    assert 'No automatic trailing newline' in facts['operation_contracts']['file_write']
    assert 'result.stdout' in facts['operation_contracts']['file_read']
    assert 'total_bytes' in facts['operation_contracts']['file_read']
    assert facts['executor_capabilities'] == engine.executor.catalog(
        workspace=Path(engine.store.get_task(call['canonical']['task_id'])['workspace']))
    assert 'Availability is not task permission' in facts['meaning']
    assert facts['web_execution']['order'].index('safe_code_analysis') < facts['web_execution']['order'].index('after_judgment_and_independent_review')
    assert facts['idea_revision']['fixed_existing_fields'] == ['id', 'target', 'proposal']
    assert facts['parent_progression']['pre_collection_return'][-1] == 'execute_if_authorized'
    assert call['wire']['installed_controller_context'] == facts
    expected_policy = (engine.policy.phase_prompt(call['role'], call['phase'], call['schema'].__name__, call['wire'])
                       if call['wire'].get('policy_input_contract') == 'role-scoped-v1' else engine.policy.prompt())
    assert expected_policy in call['messages'][0]['content']


@pytest.mark.asyncio
async def test_new_query_acquisition_and_independent_reviews_receive_installed_facts(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Write exactly hello and confirm its bytes', ['Exact file content'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    requests = []; engine.web = collector(store, requests)
    transport = install(store, engine)
    try:
        result = await engine._research(task['id'], op, 'pre', {'operation': op})
        assert result['sources'] and len(requests) == 1
        selected = [call for call in transport.calls if call['schema'] is m.ResearchQuery
                    or call['phase'].startswith(('web_acquisition_before', 'web_acquisition_after',
                                                  'web_acquisition_review_before', 'web_acquisition_review_after'))]
        assert any(call['schema'] is m.ResearchQuery and call['capture'] is None for call in selected)
        for stage in ('before', 'after'):
            assert any(call['phase'].startswith('web_acquisition_' + stage) and call['schema'] is m.AssessmentBatch for call in selected)
            assert any(call['phase'].startswith('web_acquisition_review_' + stage) and call['schema'] is m.Review for call in selected)
        for call in selected:
            assert_installed_facts(call, engine)
            if call['schema'] is m.Review:
                assert call['role'] == 'reviewer'
                assert call['canonical']['role_context']['reviewer_tools'] == []
        assert executor.calls == [] and not Path(task['workspace'], 'answer.txt').exists()
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['complete', 'partial', 'unknown'])
async def test_precontext_complete_partial_unknown_keep_original_messages_without_resend(tmp_path, monkeypatch, cut):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve original requests', ['Do not replay saved or unknown work'])
    payload = repeated_plan_context(); completed = None
    def interrupted(call, _):
        if call['phase'] == 'pending-context': raise asyncio.CancelledError()
    first = install(store, engine, mutate=interrupted)
    with monkeypatch.context() as old:
        old.setattr(engine, '_installed_controller_context', lambda *args: None)
        if cut != 'unknown':
            completed = await engine.bounded_judgments._call(task['id'], 'complete-context', payload, ContextObservation)
        if cut != 'complete':
            with pytest.raises(asyncio.CancelledError):
                await engine.bounded_judgments._call(task['id'], 'pending-context', payload, ContextObservation)
    kinds = ('semantic_wire_request', 'model_response', 'semantic_wire_output', 'bounded_model_call')
    saved = {kind: deepcopy(store.records(kind)) for kind in kinds}
    assert all('installed_controller_context' not in row['canonical_input'] for row in saved['semantic_wire_request'])
    # The existing consumer permits this operation preview to age, while the
    # original source/payload/role/lease and response messages stay bound.
    store.save_operation(task['id'], operation(), policy_hash=engine.policy.hash)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    def no_current_context(*args):
        raise AssertionError('An old return must be selected before constructing current facts')
    monkeypatch.setattr(engine, '_installed_controller_context', no_current_context)
    try:
        if completed is not None:
            assert await engine.bounded_judgments._call(task['id'], 'complete-context', payload, ContextObservation) == completed
        if cut != 'complete':
            with pytest.raises(m.PolicyError, match='unobserved|interrupted'):
                await engine.bounded_judgments._call(task['id'], 'pending-context', payload, ContextObservation)
        assert second.calls == []
        assert all(store.records(kind) == value for kind, value in saved.items())
        for call in first.calls:
            ref = {'id': call['capture']['id'], 'sha256': digest(call['capture'])}
            assert engine.gateway._messages(call['role'], call['phase'], call['canonical'], ContextObservation,
                semantic_wire=ref) == call['messages']
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_learning_restores_its_saved_context_format_after_catalog_change(tmp_path, monkeypatch, legacy):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain actual learning', ['Saved response is not regenerated'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    result = m.OperationResult(operation_id=op['id'], status='succeeded', stdout='fixture observed bytes').model_dump()
    payload = proposal_input(op, result)
    first = install(store, engine)
    with monkeypatch.context() as old:
        if legacy: old.setattr(engine, '_installed_controller_context', lambda *args: None)
        learned = await engine.bounded_judgments._call(task['id'], 'learning_proposal', payload, m.Learning)
    captures = deepcopy(store.records('semantic_wire_request'))
    calls = deepcopy(store.records('bounded_model_call'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    monkeypatch.setattr(executor, 'catalog', lambda workspace=None: {'fixture': True, 'changed_environment': True})
    try:
        assert await engine.bounded_judgments._call(task['id'], 'learning_proposal', payload, m.Learning) == learned
        assert second.calls == [] and store.records('semantic_wire_request') == captures
        assert store.records('bounded_model_call') == calls
        for call in first.calls:
            ref = {'id': call['capture']['id'], 'sha256': digest(call['capture'])}
            assert engine.gateway._messages(call['role'], call['phase'], call['canonical'], m.Learning,
                semantic_wire=ref) == call['messages']
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['source', 'lease', 'settings'])
async def test_saved_context_snapshot_does_not_waive_current_applicability(tmp_path, monkeypatch, changed):
    store, engine, executor, _ = runtime(tmp_path); transport = install(store, engine)
    task = store.create_task('Use only applicable saved work', ['All current binding checks remain'])
    payload = repeated_plan_context()
    try:
        returned = await engine.bounded_judgments._call(task['id'], 'saved-context', payload, ContextObservation)
        call, = transport.calls
        monkeypatch.setattr(executor, 'catalog', lambda workspace=None: {'fixture': True, 'new_environment': True})
        restored = engine._model_input(task['id'], 'saved-context', payload, ContextObservation)
        assert restored == call['canonical']
        assert await engine.bounded_judgments._call(task['id'], 'saved-context', payload, ContextObservation) == returned
        fresh = engine._model_input(task['id'], 'new-context', payload, ContextObservation)
        assert fresh['installed_controller_context']['executor_capabilities']['new_environment'] is True
        if changed == 'source':
            store.append_instruction(task['id'], 'Also preserve the later instruction', task['source_hash'])
        elif changed == 'lease':
            store.update_task(task['id'], state={**task['state'], 'delegation_lease': {'fixture': 'changed lease'}})
        else:
            engine.gateway.settings.values['max_output_tokens'] -= 1
        ref = {'id': call['capture']['id'], 'sha256': digest(call['capture'])}
        # Original bytes remain readable even when current applicability fails.
        assert wire.load(store, ref, call['canonical'], ContextObservation, call['role'], call['phase'], current=False) == call['capture']
        with pytest.raises(m.PolicyError, match='binding|configuration|settings|source|lease'):
            await engine.bounded_judgments._call(task['id'], 'saved-context', payload, ContextObservation)
        assert len(transport.calls) == 1 and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_plain_research_return_keeps_original_query_and_complete_acquisition(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Continue the exact saved Web query', ['No query or acquisition replay'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    requests = []; engine.web = collector(store, requests)
    first = install(store, engine)
    with monkeypatch.context() as old:
        old.setattr(engine, '_installed_controller_context', lambda *args: None)
        before = await engine._research(task['id'], op, 'pre', {'operation': op})
    query_call, = [call for call in first.calls if call['schema'] is m.ResearchQuery]
    assert query_call['capture'] is None and 'installed_controller_context' not in query_call['canonical']
    # This is the existing typed parked-return shape, bound to the actual query
    # and completed exchange above; it is not a semantic-wire capture.
    retained = {'id': 'fixture-parked-return', 'operation_sha256': digest(op),
        'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash, 'query': query_call['output']}
    current = store.get_task(task['id'])
    store.update_task(task['id'], state={**current['state'], 'web_research_returns': {op['id'] + ':pre': retained}})
    kinds = ('model_response', 'semantic_wire_request', 'semantic_wire_output', 'bounded_model_call', 'web_exchange', 'web_work')
    saved = {kind: deepcopy(store.records(kind)) for kind in kinds}
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests); second = install(store, engine)
    try:
        after = await engine._research(task['id'], op, 'pre', {'operation': op})
        assert after['sources'] == before['sources'] and len(requests) == 1
        assert second.calls == []
        for kind, value in saved.items():
            if kind == 'web_work':
                assert len(store.records(kind)) == len(value)
                for work in value: assert_restored_work(store, work)
            else: assert store.records(kind) == value
        assert store.record_get('web_research_return', retained['id'])['status'] == 'consumed'
        assert engine.gateway._messages(query_call['role'], query_call['phase'], query_call['canonical'], m.ResearchQuery) == query_call['messages']
        assert not store.get_task(task['id'])['state'].get('web_research_returns') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_installed_facts_cannot_supply_permission_or_actual_file_results(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Require real reviewed file work', ['The model cannot grant execution'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    try:
        model_input = engine._model_input(task['id'], 'review', {'operation': op,
            'installed_controller_context': {'version': 'untrusted', 'permission': True}}, m.Review, role='reviewer')
        facts = model_input['installed_controller_context']
        assert facts['version'] == wire.INSTALLED_CONTEXT and 'permission' not in facts
        assert model_input['role_context']['reviewer_tools'] == []
        assert store.get_operation(op['id'])['result'] is None
        with pytest.raises(m.PolicyError, match='pre-execution cycle is incomplete'):
            await engine._execute({'task_id': task['id'], 'operation_id': op['id']})
        assert executor.calls == [] and not Path(task['workspace'], 'answer.txt').exists()
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('old_writer', [False, True])
async def test_ordinary_research_resume_keeps_query_and_first_source(tmp_path, old_writer):
    from tests.test_knowledge import seed_skill
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Finish the existing collection', ['Reuse the successful first source'])
    seed_skill(store, engine.knowledge, task)
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    requests = []; engine.web = collector(store, requests)
    count = 0
    def fail_second(call, _):
        nonlocal count
        if call['schema'] is m.ResearchQuery:
            return dict(call['output'], query='https://example.com/a https://example.com/b')
        if call['schema'] is m.SkillSelection:
            count += 1
            if count == 2: return httpx.Response(500, json={'error': 'Observed provider failure'})
    first = install(store, engine, mutate=fail_second)
    with pytest.raises(ProviderError):
        await engine._research(task['id'], op, 'pre', {'operation': op})
    assert len(requests) == 1
    complete = [deepcopy(w) for w in store.records('web_work') if w['status'] == 'complete']
    assert len(complete) == 2
    applications = deepcopy(store.records('knowledge_application'))
    failed = deepcopy([r for r in store.records('bounded_model_call') if r['status'] == 'failed'])
    assert len(failed) == 1
    if old_writer:
        # Simulate the historical writer only: real query/work/results above
        # remain, but that writer did not persist the ordinary return slot.
        current = store.get_task(task['id']); state = deepcopy(current['state'])
        state.pop('web_research_returns'); store.update_task(task['id'], state=state)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests); second = install(store, engine)
    try:
        result = await engine._research(task['id'], op, 'pre', {'operation': op})
        assert len(result['sources']) == 2 and len(requests) == 2
        assert not any(call['schema'] is m.ResearchQuery for call in second.calls)
        for work in complete: assert_restored_work(store, work)
        assert store.records('knowledge_application')[:len(applications)] == applications
        assert store.record_get('bounded_model_call', failed[0]['id']) == failed[0]
        assert not store.get_task(task['id'])['state'].get('web_research_returns')
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


def assert_restored_work(store, original):
    restored = deepcopy(store.record_get('web_work', original['id']))
    recovery = restored.pop('application_recovery', None)
    if recovery is not None:
        application = store.record_get('knowledge_application', original['task_id'] + ':' + original['detail']['id'])
        episode = store.record_get('episode', application['outcome']['episode_id'])
        accepted = [i for i, attempt in enumerate(original['learning_attempts'])
                    if attempt['learning'] == episode['learning'] and attempt.get('disposition', {}).get('verdict') == 'proceed']
        assert recovery == {'application_id': application['id'], 'input_hash': application['input_hash'],
            'episode_id': episode['id'], 'episode_sha256': digest(episode),
            'accepted_attempt_index': accepted[-1] if accepted else None,
            'review_reconstructed': False, 'application_replayed': False}
    assert restored == {k: v for k, v in original.items() if k != 'application_recovery'}
    if 'application_recovery' in original: assert recovery == original['application_recovery']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['choice', 'binding', 'revision'])
async def test_historical_query_requires_exact_collection_and_current_binding(tmp_path, invalid):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Check stored query applicability', ['Do not guess a historical choice'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    query = m.ResearchQuery(query='https://example.com/a', rationale='Exact original reason', private_data_excluded=True).model_dump()
    work = {'id': 'source:before', 'task_id': task['id'], 'operation': op,
        'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash,
        'detail': {'phase': 'pre', 'collection_context': {'query': query['query']}}, 'research_choice': query}
    store.record('web_work', work['id'], work)
    try:
        if invalid == 'binding':
            store.append_instruction(task['id'], 'A changed source requires current judgment', task['source_hash'])
            assert engine._restore_research_choice(store.get_task(task['id']), op, 'pre') is None
        elif invalid == 'choice':
            other = deepcopy(work); other['id'] = 'other:before'; other['research_choice']['rationale'] = 'Different semantic choice'
            store.record('web_work', other['id'], other)
            with pytest.raises(m.PolicyError, match='ambiguous'): engine._restore_research_choice(task, op, 'pre')
        else:
            store.event(task['id'], 'web_revision', 'required', {'parent_operation_id': op['id']})
            with pytest.raises(m.PolicyError, match='revision'): engine._restore_research_choice(task, op, 'pre')
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', ['other-phase', 'same-phase', 'missing', 'wrong-query', 'wrong-owner'])
async def test_historical_query_scopes_revision_to_exact_acquisition(tmp_path, revision):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Continue only the unaffected saved collection', ['Preserve unresolved revisions'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    query = m.ResearchQuery(query='https://example.com/a', rationale='Exact saved choice', private_data_excluded=True).model_dump()
    work = {'id': 'current:before', 'task_id': task['id'], 'operation': op,
        'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash,
        'detail': {'id': 'current', 'phase': 'post', 'collection_context': {'query': query['query']}}, 'research_choice': query}
    store.record('web_work', work['id'], work)
    previous = deepcopy(work); previous['id'] = 'previous:after'
    previous['detail'].update(id='previous', phase='post' if revision == 'same-phase' else 'pre')
    if revision == 'wrong-owner': previous['task_id'] = 'another-task'
    if revision != 'missing': store.record('web_work', previous['id'], previous)
    store.event(task['id'], 'web_revision', 'required', {'parent_operation_id': op['id'],
        'acquisition_id': 'previous', 'stage': 'after',
        'query': 'https://example.com/wrong' if revision == 'wrong-query' else query['query']})
    saved = deepcopy(store.records('web_work'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    transport = install(store, engine)
    try:
        if revision == 'other-phase': assert engine._restore_research_choice(task, op, 'post').model_dump() == query
        else:
            with pytest.raises(m.PolicyError, match='revision'): engine._restore_research_choice(task, op, 'post')
        assert store.records('web_work') == saved and transport.calls == [] and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_precontext_canonical_disposition_success_keeps_original_messages(tmp_path, monkeypatch):
    from tests.test_disposition_contract_recovery import payload, write_legacy
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain the original canonical return', ['No historical regeneration'])
    source = payload(2)
    with monkeypatch.context() as old:
        old.setattr(engine, '_installed_controller_context', lambda *args: None)
        saved = await write_legacy(store, engine, task, source, invalid=False)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        value = await engine.bounded_judgments._call(task['id'], 'wire-legacy', source, m.Disposition)
        assert value.model_dump() == saved['result'] and second.calls == []
        assert store.record_get('bounded_model_call', saved['id']) == saved
        assert store.records('semantic_wire_request') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_current_web_facts_distinguish_prior_snapshot_and_later_completed_source(tmp_path):
    from tests.test_web_recovery import detail
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Use both actual sources', ['Keep temporal evidence distinct']); op = operation()
    first = {**detail('first'), 'extraction_status': 'not_yet_performed', 'body_accepted_as_source': False}
    prior = {'id': 'first:after', 'task_id': task['id'], 'stage': 'after', 'acquisition': first,
        'disposition': {'verdict': 'proceed'}}
    store.record('web_review', prior['id'], prior)
    completed = {'requested_url': first['url'], 'final_url': first['url'], 'source_id': 'first'}
    second = {**detail('second'), 'phase': 'pre', 'collection_context': {'completed_targets': [completed]},
        'response_analysis': {'state': 'succeeded', 'content_adopted': False}}
    work = {'id': 'second:after', 'task_id': task['id'], 'operation': op, 'stage': 'after', 'detail': second,
        'research_choice': {}, 'selection': {'selected': [], 'rejected': []}, 'selection_binding': {},
        'context_contract_version': 'web-identity-v4', **engine.web_judgments._binding(task)}
    try:
        engine.web_judgments._ensure_acquisition_input(work)
        current = engine.web_judgments._context(work)
        facts = current['controller_facts']
        assert facts['current_exchange']['analysis_state'] == 'succeeded'
        assert facts['current_exchange']['content_adopted'] is False
        assert facts['next_parent_stage'] == 'pre_collection_return'
        previous, = facts['prior_snapshot_interpretation']
        assert previous['snapshot_sha256'] == digest(first)
        assert previous['subsequent_completed_targets'] == [completed]
        assert current['prior_acquisition_evidence'][0]['acquisition'] == first
        # A saved v3 definition is still built with its original fields/prose.
        legacy = deepcopy(work); legacy['context_contract_version'] = 'web-identity-v3'
        old = engine.web_judgments._context(legacy)
        assert 'controller_facts' not in old
        assert 'not yet extracted semantic page content' in old['phase_contract']
        assert old['result'] == current['result'] and store.record_get('web_review', prior['id']) == prior
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_fixed_idea_proposal_correction_reaches_wire_and_independent_review(tmp_path):
    from policy_harness.learning_projection import current_input
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Correct the actual proposal', ['Retain old idea and review its replacement'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    result = m.OperationResult(operation_id=op['id'], status='succeeded', stdout='observed').model_dump()
    original = m.Idea(id='original', target='workflow', proposal='Repeat the unchanged failed request',
        disposition='investigate', rationale='Needs an actual recovery condition', next_operation=None).model_dump()
    payload = dict(proposal_input(op, result), required_ideas=[original])
    def correct(call, number):
        if call['schema'] is m.LearningIdeas:
            facts = call['wire']['installed_controller_context']['idea_revision']
            assert set(facts['fixed_existing_fields']) == {'id', 'target', 'proposal'}
            assert facts['changed_proposal_route']['changed_idea'] == 'new_ideas'
            returned = deepcopy(call['output'])
            returned['existing_idea_decisions'] = [{'disposition': 'reject',
                'rationale': 'Unchanged retry has no new applicable evidence', 'next_operation': None}]
            returned['new_ideas'] = [{'target': 'workflow', 'proposal': 'Resume only the identified failed stage after its effect check',
                'disposition': 'investigate', 'rationale': 'Preserve successful and unknown work while checking the actual failed call',
                'next_operation': None}]
            return returned
    transport = install(store, engine, mutate=correct)
    try:
        learned = await engine.bounded_judgments._call(task['id'], 'idea-correction', payload, m.LearningIdeas)
        retained, replacement = [idea.model_dump() for idea in learned.ideas]
        assert all(retained[key] == original[key] for key in ('id', 'proposal', 'target'))
        assert retained['disposition'] == 'reject' and replacement['id'] != original['id']
        assert replacement['proposal'] != original['proposal'] and payload['required_ideas'] == [original]
        review_input = current_input({'operation': op, 'result': result, 'learning': learned.model_dump()})
        await engine.bounded_judgments.review_proposal(task['id'], op, 'idea-correction-review', review_input)
        review_call = next(call for call in transport.calls if call['schema'] is m.Review)
        assert review_call['role'] == 'reviewer'
        assert review_call['wire']['installed_controller_context']['idea_revision']['mutable_existing_fields'] == ['disposition', 'rationale', 'next_operation']
        from tests.test_semantic_wire_contract import displayed_source_slots
        slots = displayed_source_slots(review_call)
        assert any(slot['pointer'] == '/learning/ideas/0' and slot['editable'] for slot in slots)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['http500', 'unobserved', 'revise', 'proposal', 'target'])
async def test_pending_legacy_after_uses_retained_response_and_keeps_failed_call_history(tmp_path, monkeypatch, failure):
    import base64
    import hashlib
    from tests.test_web_recovery import detail
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Finish the retained page judgment', ['No HTTP or unknown-call replay'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    requests = []; engine.web = collector(store, requests)
    raw = b'previously retained exact source text'
    args = {'phase': 'pre', 'task_id': task['id'], 'operation_id': op['id']}
    key = digest({**args, 'method': 'GET', 'url': 'https://example.com/a', 'query': None, 'body': None})
    record = {**detail('legacy-pending'), **args, 'exchange_id': key, 'method': 'GET', 'kind': 'fetch',
        'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'extraction_status': 'not_yet_performed',
        'body_accepted_as_source': False}
    exchange = {'id': key, 'stage': 'response', 'record': record, 'raw_base64': base64.b64encode(raw).decode(),
        'headers': {'content-type': 'text/plain'}}
    store.record('web_exchange', key, exchange)
    def fail_learning(call, number):
        if call['phase'] == 'web_acquisition_learning':
            if failure == 'unobserved': raise asyncio.CancelledError()
            return httpx.Response(500, json={'error': {'message': 'Observed fixture model failure'}})
    original_save = engine.web_judgments._save
    def old_writer(saved):
        saved['context_contract_version'] = 'web-identity-v3'
        original_save(saved)
    first = install(store, engine, mutate=fail_learning)
    evaluate = lambda stage, value: engine.web_judgments.evaluate(task['id'], op, stage, value)
    try:
        with monkeypatch.context() as old:
            old.setattr(engine.web_judgments, '_save', old_writer)
            prepared = {key:value for key,value in record.items() if key not in
                {'http_status', 'sha256', 'bytes', 'started_at', 'finished_at', 'elapsed_seconds', 'extraction_status'}}
            prepared.update(status='prepared', network_dispatched=False)
            await evaluate('before', prepared)
            error = asyncio.CancelledError if failure == 'unobserved' else ProviderError
            with pytest.raises(error):
                await engine.web.collect('https://example.com/a', evaluate=evaluate, **args)
        prior = deepcopy(store.record_get('web_work', record['id'] + ':after'))
        before = deepcopy(store.record_get('web_work', record['id'] + ':before'))
        calls = deepcopy(store.records('bounded_model_call'))
        corrective = failure in {'revise', 'proposal', 'target'}
        progress = {'proposals': 0, 'dispositions': 0}
        def revise_once(call, number):
            if not corrective: return
            if call['phase'] == 'web_acquisition_learning':
                progress['proposals'] += 1
                value = deepcopy(call['output'])
                assert value['existing_idea_decisions']
                for decision in value['existing_idea_decisions']:
                    decision.update(disposition='reject', rationale='Current result shows the existing mechanism suffices; retain the original proposal.', next_operation=None)
                return value
            if call['phase'] == 'web_learning_disposition':
                progress['dispositions'] += 1
                if progress['dispositions'] == 1:
                    from tests.test_semantic_wire_contract import source_choice
                    value = deepcopy(call['output'])
                    value.update(verdict='revise', rationale='Clarify the current explanation using the existing result.',
                        revision_scope='learning', revision_targets=[source_choice(call, '/learning/outcome_summary')])
                    return value
        second = install(store, engine, mutate=revise_once)
        if failure == 'unobserved':
            with pytest.raises(m.PolicyError, match='unobserved saved Learning request'):
                await engine.web_judgments.drain(task['id'])
            assert store.record_get('web_work', prior['id']) == prior
            assert second.calls == [] and store.records('bounded_model_call') == calls
            return
        if corrective:
            class BeforeCorrection(RuntimeError): pass
            original_proposal = engine.bounded_judgments.learning_proposal
            async def stop_before_correction(*args, **kwargs):
                if progress['proposals']:
                    raise BeforeCorrection('No next model call dispatched')
                return await original_proposal(*args, **kwargs)
            with monkeypatch.context() as pause:
                pause.setattr(engine.bounded_judgments, 'learning_proposal', stop_before_correction)
                with pytest.raises(BeforeCorrection):
                    await engine.web_judgments.drain(task['id'])
            pending = deepcopy(store.record_get('web_work', prior['id']))
            latest = pending['learning_attempts'][-1]
            assert latest['revision_consumed'] and latest['assessments'] and latest['review'] and latest['disposition']
            old = {i['id']:i for i in pending['analysis_transition']['failed_restart']['required_ideas']}
            current_ideas = {i['id']:i for i in latest['learning']['ideas']}
            assert old and all((current_ideas[k]['target'],current_ideas[k]['proposal']) == (v['target'],v['proposal']) for k,v in old.items())
            assert any(current_ideas[k] != v for k,v in old.items())
            retained_calls = deepcopy(store.records('bounded_model_call'))
            retained_captures = deepcopy(store.records('semantic_wire_request'))
            earlier_sent = list(second.calls)
            await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
            engine.web = collector(store, requests)
            second = install(store, engine)
            evaluate = lambda stage, value: engine.web_judgments.evaluate(task['id'], op, stage, value)
            if failure in {'proposal', 'target'}:
                corrupted = deepcopy(pending)
                changed = corrupted['learning_attempts'][-1]['learning']['ideas'][0]
                changed[failure] = ('workflow' if changed['target'] == 'task' else 'task') if failure == 'target' else 'different immutable meaning'
                store.record('web_work', pending['id'], corrupted)
                with pytest.raises(m.PolicyError, match='different meaning'):
                    await engine.web_judgments.drain(task['id'])
                assert second.calls == [] and store.records('knowledge_application') == []
                assert all(store.record_get('bounded_model_call', r['id']) == r for r in retained_calls)
                return
        await engine.web_judgments.drain(task['id'])
        if corrective:
            assert second.calls[0]['phase'] == 'web_acquisition_learning'
            supplied = {i['id']:i for i in second.calls[0]['canonical']['required_ideas']}
            assert all(supplied[k] == v for k,v in current_ideas.items())
            assert all(store.record_get('bounded_model_call', r['id']) == r for r in retained_calls)
            assert all(store.record_get('semantic_wire_request', r['id']) == r for r in retained_captures)
            assert store.record_get('web_work', prior['id'])['learning_attempts'][0] == latest
            assert len(store.records('knowledge_application')) == 1
            for call in second.calls:
                if call['phase'] in {'web_learning_review', 'web_learning_disposition'}:
                    contract = call['canonical']['installed_controller_context']['learning_applications']
                    assert all(contract[k] == v for k,v in m.learning_applications_contract().items())
                    assert call['wire']['installed_controller_context']['learning_applications'] == contract
                    assert 'status' not in contract['applications']['items']['properties']
            second.calls[:0] = earlier_sent
        current = store.record_get('web_work', prior['id'])
        transition = current['analysis_transition']
        history = store.record_get('web_work_history', transition['original_work_ref']['id'])
        assert history == prior and transition['original_work_ref']['sha256'] == digest(prior)
        assert current['status'] == 'complete'
        assert current['detail']['response_analysis']['source']['text'] == raw.decode()
        assert current['detail']['response_analysis']['content_adopted'] is False
        assert store.record_get('web_work', before['id']) == before
        assert all(store.record_get('bounded_model_call', call['id']) == call for call in calls)
        assert store.record_get('web_exchange', key) == exchange and requests == []
        contexts = [call for call in second.calls if call['phase'] == 'web_acquisition_learning']
        assert contexts and contexts[-1]['canonical']['result']['data']['response_analysis']['source']['text'] == raw.decode()
        current_payload = contexts[-1]['canonical']
        failed_ref = transition['failed_restart']['source_receipt']
        assert engine.web_judgments.validate_analysis_transition(task['id'], current_payload, failed_ref)
        damaged = deepcopy(current_payload); damaged['analysis_transition']['current_result_sha256'] = '0' * 64
        with pytest.raises(m.PolicyError, match='analysis transition'):
            engine.web_judgments.validate_analysis_transition(task['id'], damaged, failed_ref)
        # Even mutually consistent replacement hashes cannot fabricate an
        # extraction different from the actual retained response bytes.
        fabricated = deepcopy(current)
        fabricated['detail']['response_analysis']['source']['text'] = 'invented source'
        fabricated['detail']['response_analysis']['source']['sha256'] = hashlib.sha256(b'invented source').hexdigest()
        fabricated['acquisition_result']['data'] = deepcopy(fabricated['detail'])
        fabricated['analysis_transition']['current_detail_sha256'] = digest(fabricated['detail'])
        fabricated['analysis_transition']['current_result_sha256'] = digest(fabricated['acquisition_result'])
        store.record('web_work', fabricated['id'], fabricated)
        damaged = dict(current_payload, result=fabricated['acquisition_result'], analysis_transition=fabricated['analysis_transition'])
        try:
            with pytest.raises(m.PolicyError, match='analysis transition'):
                engine.web_judgments.validate_analysis_transition(task['id'], damaged, failed_ref)
        finally:
            store.record('web_work', current['id'], current)
        call_count = len(second.calls)
        delivered = await engine.web.collect('https://example.com/a', evaluate=evaluate, **args)
        assert delivered['sources'][0]['text'] == raw.decode()
        assert len(second.calls) == call_count and requests == [] and executor.calls == []
    finally:
        await engine.close(); store.close()
