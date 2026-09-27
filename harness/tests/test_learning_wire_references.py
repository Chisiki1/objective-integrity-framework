"""Real captured HTTP/SQLite consumers; judgments are explicit test fixtures."""
from copy import deepcopy
import json
from uuid import uuid4

import httpx
import pytest

from policy_harness import models as m
from policy_harness import semantic_wire as wire
from policy_harness.decisions import targets as decision_targets
from policy_harness.learning_projection import current_input
from policy_harness.providers import ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import learning, operation
from tests.test_learning_convergence import idea
from tests.test_learning_update_contract import reopen
from tests.test_semantic_wire_contract import install, assert_wire_records, source_choice


def context():
    op = operation('file_list', args={})
    result = m.OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    return current_input({'operation': op, 'result': result,
        'pre': {'skills': {'selected': [{'application': 'Apply the exact per-URL discipline'}],
                           'rationale': 'fetch remains planned/unexecuted'}},
        'learning': learning(ideas=[idea('retained')]).model_dump()})


def legacy_capture(store, task, role, phase, payload, schema, *, revision=None):
    """The pre-binding writer, before any old response exists; never restamping."""
    identity = {'version': wire.VERSION, 'task_id': task['id'], 'source_hash': task['source_hash'],
        'policy_hash': store.policy_hash, 'role': role, 'phase': phase,
        'owner': task['actor'], 'parent_id': task['parent_id'],
        'lease': deepcopy(task['state'].get('delegation_lease')),
        'canonical_schema': schema.__name__, 'canonical_schema_sha256': digest(schema.model_json_schema()),
        'wire_schema_sha256': digest(wire.wire_schema(schema.__name__, payload, revision=revision).model_json_schema()),
        'model_input_sha256': digest(payload)}
    if revision is not None: identity['wire_revision'] = revision
    key = digest(identity)
    def create():
        saved = store.record_get('semantic_wire_request', key)
        if saved is None:
            saved = {'id': key, **identity, 'nonce': uuid4().hex, 'canonical_input': deepcopy(payload),
                     'association': wire.associations(payload, schema.__name__, store, revision=revision)}
            store.record('semantic_wire_request', key, saved)
        return {'id': key, 'sha256': digest(saved)}
    return store._transaction(create)


@pytest.mark.asyncio
async def test_old_capture_reopens_without_resend_and_only_new_request_uses_references(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep original successful decisions', ['Every old receipt remains exact'])
    payload = context(); targets = decision_targets('learning', m.Learning.model_validate(payload['learning']), group_ideas=True)
    first = install(store, engine)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', legacy_capture)
        original = await engine._assess_targets(task['id'], 'old-owner', targets, payload)
    old_records = deepcopy(store.records('semantic_wire_request'))
    assert old_records and all('wire_revision' not in row for row in old_records)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        assert await engine._assess_targets(task['id'], 'old-owner', targets, payload) == original
        assert not second.calls
        assert store.records('semantic_wire_request') == old_records
        await engine._assess_targets(task['id'], 'new-owner', targets, payload)
        assert second.calls[0]['capture']['wire_revision'] == wire.REFERENCED_ASSESSMENT_DEFAULTS
        for saved in old_records: assert store.record_get('semantic_wire_request', saved['id']) == saved
        assert_wire_records(store, first.calls + second.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bundled', [False, True])
async def test_grounded_visible_indices_keep_duplicate_original_members(tmp_path, bundled):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Review the distinct third candidate', ['Exact original reference and all members'])
    payload = context()
    distinct = dict(idea('B'), proposal='Read the distinct saved artifact')
    payload['learning']['ideas'] = [idea('A1'), idea('A2'), distinct]
    if bundled:
        payload = current_input({'operation': payload['operation'], 'bundle': payload})
    prefix = '/bundle' if bundled else ''
    def choose(call, _):
        body = deepcopy(call['output'])
        visible = call['wire']['bundle'] if bundled else call['wire']
        assert [i['id'] for i in visible['learning']['ideas']] == ['A1', 'A2', 'B']
        body['opinions'][0]['evidence_refs'] = [source_choice(call, prefix + '/learning/ideas/2')]
        return body
    transport = install(store, engine, mutate=choose)
    try:
        result = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'original-index', payload)
        evidence = json.loads(result.opinions[0].evidence_refs[0])
        assert evidence['pointer'] == prefix + '/learning/ideas/2'
        assert evidence['value_sha256'] == digest(distinct)
        assert evidence['stage'] == 'current_uncommitted_learning' and evidence['editable']
        assert_wire_records(store, transport.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['succeeded', 'partial', 'unknown'])
async def test_pre_binding_disposition_pages_reopen_without_success_replay(tmp_path, monkeypatch, ending):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume the exact original judgment pages', ['Retain success and unknown effects'])
    payload = context()
    payload.update(review={'summary': 'Original independent opinions', 'opinions': [
        {'id': 'opinion-' + str(i), 'observation': 'Inspect the recorded stage ' + str(i),
         'rationale': 'Consider each opinion once', 'evidence_refs': []} for i in range(4)]}, sources=[])
    phase = 'old-disposition-pages'
    def force_pages(owner):
        fits = owner.bounded_judgments._fits
        def fit(task_id, actual, data, schema, role=None):
            if schema is m.Disposition and actual.startswith(phase):
                if 'bounded_context' not in data or len(data['review']['opinions']) > 1: return False
            return fits(task_id, actual, data, schema, role)
        monkeypatch.setattr(owner.bounded_judgments, '_fits', fit)
    force_pages(engine)
    def interrupt(call, count):
        if count == 2 and ending != 'succeeded':
            if ending == 'unknown': raise httpx.ReadTimeout('Original sent response was not observed')
            return httpx.Response(500, json={'error': 'Observed original page failure'})
    before = install(store, engine, mutate=interrupt)
    with monkeypatch.context() as historical:
        historical.setattr(wire, 'capture', legacy_capture)
        if ending == 'succeeded':
            original = await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], phase, payload)
        else:
            with pytest.raises(ProviderError):
                await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], phase, payload)
    saved = deepcopy(store.records('bounded_model_call'))
    old_outputs = deepcopy(store.records('semantic_wire_output'))
    old_captures = deepcopy(store.records('semantic_wire_request'))
    assert all('learning_reference_source' not in call['canonical'] for call in before.calls)
    assert all('wire_revision' not in row for row in old_captures)
    successes = [row for row in saved if row['status'] == 'succeeded']
    assert len(successes) == (4 if ending == 'succeeded' else 1)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    force_pages(engine); after = install(store, engine)
    try:
        if ending == 'unknown':
            with pytest.raises(m.PolicyError, match='unobserved|unknown|interrupted'):
                await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], phase, payload)
            assert not after.calls
            for row in saved: assert store.record_get('bounded_model_call', row['id']) == row
        else:
            result = await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], phase, payload,
                                                                        retained_only=ending == 'succeeded')
            assert result.verdict == 'proceed' and len(result.opinion_responses) == 4
            if ending == 'succeeded': assert result == original and not after.calls
            else: assert len(after.calls) == 3
            sent = len(after.calls)
            assert await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], phase, payload,
                                                                      retained_only=True) == result
            assert len(after.calls) == sent
        for row in successes: assert store.record_get('bounded_model_call', row['id']) == row
        for row in old_outputs: assert store.record_get('semantic_wire_output', row['id']) == row
        for row in old_captures: assert store.record_get('semantic_wire_request', row['id']) == row
        observed = before.calls + after.calls
        if ending == 'unknown':
            unobserved = before.calls[-1]
            retained = unobserved['capture']
            ref = {'id': retained['id'], 'sha256': digest(retained)}
            assert store.record_get('semantic_wire_request', retained['id']) == retained
            assert not any(row.get('semantic_wire_capture') == ref for row in store.records('model_response'))
            observed = before.calls[:-1]  # No response was observed, and no resend occurred.
        assert_wire_records(store, observed)
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_grounded_disposition_resolves_existing_authenticated_context_synopsis(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Correct through the original bounded context', ['Authenticate original operation and review'])
    payload = context(); payload.update(review={'summary': 'No extra opinions', 'opinions': []}, sources=[])
    phase = 'synopsis-disposition'
    bounded = engine.bounded_judgments; fits = bounded._fits; compact = bounded.compact_context
    def fit(task_id, actual, data, schema, role=None):
        if schema is m.Disposition and actual == phase and 'bounded_context' not in data: return False
        return fits(task_id, actual, data, schema, role)
    async def observed(task_id, actual, context, base, schema, **kw):
        if actual == phase: kw['force_observation'] = True
        return await compact(task_id, actual, context, base, schema, **kw)
    monkeypatch.setattr(bounded, '_fits', fit); monkeypatch.setattr(bounded, 'compact_context', observed)
    def correct(call, _):
        if call['schema'] is not m.Disposition: return
        assert call['canonical']['bounded_context']['packet_id']
        return dict(call['output'], verdict='revise', revision_scope='learning',
                    revision_targets=[source_choice(call, '/learning/outcome_summary')])
    transport = install(store, engine, mutate=correct)
    try:
        result = await bounded.disposition_proposal(task['id'], payload['operation'], phase, payload)
        evidence = json.loads(result.rationale.split('\nCorrection references: ', 1)[1])[0]
        assert result.verdict == 'revise' and evidence['editable']
        request = next(c for c in transport.calls if c['schema'] is m.Disposition)['capture']
        original = wire._reference_source(request['canonical_input'], store)
        assert original['learning'] == payload['learning']
        assert evidence['source_sha256'] == digest(original)
        changed = deepcopy(request['canonical_input'])
        changed['bounded_context']['source_sha256'] = 'foreign'
        with pytest.raises(m.PolicyError, match='source differs'): wire._reference_source(changed, store)
        assert_wire_records(store, transport.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', [
    {'candidate_slot': 8, 'consideration': 'A foreign candidate cannot be supplied'},
    {'candidate_slot': -1, 'consideration': 'Negative slots are not membership'},
    {'candidate_slot': True, 'consideration': 'Boolean is not a slot'},
    {'candidate_slot': 0, 'consideration': '   '},
    {'candidate_slot': 0, 'consideration': 'Concrete thought', 'id': 'invented'},
    None,
])
async def test_invalid_candidate_reference_never_reaches_knowledge_or_executor(tmp_path, choice):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain actual concrete consideration', ['No fabricated or empty candidates'])
    payload = context(); targets = decision_targets('learning', m.Learning.model_validate(payload['learning']), group_ideas=True)
    def invalid(call, _):
        body = deepcopy(call['output'])
        for assessment in body['assessments']:
            assessment['ideas'] = [] if choice is None else [choice]
        return body
    transport = install(store, engine, mutate=invalid)
    try:
        with pytest.raises(m.PolicyError, match='repeated'):
            await engine._assess_targets(task['id'], 'invalid-reference', targets, payload)
        assert len(transport.calls) == 2
        assert not store.records('knowledge_application') and executor.calls == []
        assert any(e['status'] == 'rejected_model_output' for e in store.events(task['id']))
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('paged', [False, True])
async def test_selected_review_and_editable_correction_use_same_captured_source(tmp_path, monkeypatch, paged):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Correct the current proposal without rewriting history', ['Preserve immutable before facts'])
    payload = context(); calls = {'review': 0, 'dispose': 0}
    if paged:
        fits = engine.bounded_judgments._fits
        def fit(task_id, phase, data, schema, role=None):
            if schema is m.Review and phase == 'grounded-review' and 'source_records' not in data: return False
            if schema is m.Disposition and phase == 'grounded-dispose' and 'bounded_context' not in data: return False
            return fits(task_id, phase, data, schema, role)
        monkeypatch.setattr(engine.bounded_judgments, '_fits', fit)
    def producer(call, _):
        body = deepcopy(call['output'])
        if call['schema'] is m.Review and call['phase'] == 'grounded-review':
            calls['review'] += 1
            body['opinions'][0]['evidence_refs'] = ([{'source_slot': len(call['capture']['association']['source_slots'])}]
                if calls['review'] == 1 else [source_choice(call, '/pre/skills/rationale')])
            body['opinions'][0]['observation'] = 'This is a before-selection statement, not an after-result claim'
        elif call['schema'] is m.Disposition and call['phase'] == 'grounded-dispose':
            calls['dispose'] += 1
            body.update(verdict='revise', revision_scope='learning',
                        rationale='Clarify the uncommitted summary while preserving the immutable selection')
            body['revision_targets'] = [source_choice(call, '/pre/skills/rationale'
                if calls['dispose'] == 1 else '/learning/outcome_summary')]
        return body
    transport = install(store, engine, mutate=producer)
    try:
        review = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'grounded-review', payload)
        evidence = json.loads(review.opinions[0].evidence_refs[0])
        assert evidence['pointer'] == '/pre/skills/rationale' and evidence['stage'] == 'before_snapshot'
        assert evidence['editable'] is False
        source = deepcopy(payload)
        source.update(review=review.model_dump(), sources=[])
        disposition = await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], 'grounded-dispose', source)
        assert calls == {'review': 2, 'dispose': 2}
        assert disposition.verdict == 'revise'
        refs = json.loads(disposition.rationale.split('\nCorrection references: ', 1)[1])
        assert refs[0]['pointer'] == '/learning/outcome_summary'
        assert refs[0]['editable'] and refs[0]['stage'] == 'current_uncommitted_learning'
        assert payload['pre']['skills']['rationale'] == 'fetch remains planned/unexecuted'
        rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output']
        assert len(rejected) == 2
        assert_wire_records(store, transport.calls)
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_parent_can_reject_wrong_opinion_and_reopen_without_regeneration(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Reason about every independent opinion', ['Reject mistaken criticism with evidence'])
    payload = context()
    payload.update(review={'summary': 'Preserved historical opinion', 'opinions': [
        {'id': 'historical-opinion', 'observation': 'Rewrite the before selection',
         'rationale': 'Mistakenly reads before as after', 'evidence_refs': []}]}, sources=[])
    def reject(call, _):
        return dict(call['output'], opinion_decisions=[{'disposition': 'reject',
            'rationale': 'The exact selection is a before snapshot and the completed exchange is separate'}])
    before = install(store, engine, mutate=reject)
    original = await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], 'reject-wrong-opinion', payload)
    assert original.verdict == 'proceed' and original.opinion_responses[0].disposition == 'reject'
    saved = deepcopy(store.records('semantic_wire_output'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    try:
        restored = await engine.bounded_judgments.disposition_proposal(task['id'], payload['operation'], 'reject-wrong-opinion', payload, retained_only=True)
        assert restored == original and not after.calls
        assert store.records('semantic_wire_output') == saved
        assert_wire_records(store, before.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('family', ['Review', 'Disposition'])
async def test_saved_binding_codec_reopens_without_resend_and_new_phase_uses_slots(tmp_path, monkeypatch, family):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve the issued reference format', ['No successful response regeneration'])
    payload = context(); payload.update(review={'summary': 'Retain the actual judgment', 'opinions': []}, sources=[])
    schema = getattr(m, family)
    async def judge(owner, phase):
        method = owner.bounded_judgments.review_proposal if family == 'Review' else owner.bounded_judgments.disposition_proposal
        return await method(task['id'], payload['operation'], phase, payload)
    def binding_capture(*args): return legacy_capture(*args, revision=wire.LEARNING_BINDINGS)
    before = install(store, engine)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', binding_capture)
        result = await judge(engine, 'saved-binding-codec')
    saved = deepcopy(store.records('semantic_wire_request'))
    outputs = deepcopy(store.records('semantic_wire_output'))
    messages = deepcopy(before.calls[0]['wire'])
    assert saved and all(row['wire_revision'] == wire.LEARNING_BINDINGS for row in saved)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    try:
        assert await judge(engine, 'saved-binding-codec') == result and not after.calls
        for row in saved:
            assert wire.projected_input(store.record_get('semantic_wire_request', row['id'])) == messages
        await judge(engine, 'new-target-codec')
        assert after.calls[0]['capture']['wire_revision'] == wire.LEARNING_TARGETS
        for row in saved: assert store.record_get('semantic_wire_request', row['id']) == row
        for row in outputs: assert store.record_get('semantic_wire_output', row['id']) == row
        assert_wire_records(store, before.calls + after.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [None, wire.LEARNING_BINDINGS, wire.LEARNING_TARGETS])
async def test_saved_format_integrity_is_separate_from_current_source_applicability(tmp_path, monkeypatch, revision):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Read historical judgments', ['Do not authorize stale execution'])
    payload = context(); phase = 'historical-integrity'
    transport = install(store, engine)
    def writer(*args): return legacy_capture(*args, revision=revision)
    try:
        with monkeypatch.context() as saved_format:
            saved_format.setattr(wire, 'capture', writer)
            original = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], phase, payload)
        captured = transport.calls[0]['capture']; ref = {'id': captured['id'], 'sha256': digest(captured)}
        saved = deepcopy(store.records('semantic_wire_output'))
        store.append_instruction(task['id'], 'Add a new current instruction without erasing history', task['source_hash'])
        canonical = captured['canonical_input']
        with monkeypatch.context() as historical:
            def no_current_task(*args): raise AssertionError('Historical restore must not require current task state')
            historical.setattr(store, 'get_task', no_current_task)
            restored = wire.load(store, ref, canonical, m.Review, captured['role'], phase, current=False)
        assert restored == captured
        assert wire.decode(store, restored, m.Review, saved[0]['raw']) == original
        with pytest.raises(m.PolicyError, match='current source/policy/owner/parent/lease'):
            wire.load(store, ref, canonical, m.Review, captured['role'], phase)
        assert store.records('semantic_wire_output') == saved and len(transport.calls) == 1
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', [
    {'source_slot': -1}, {'source_slot': True}, {'source_slot': 1000000},
    {'source_slot': 0, 'pointer': '/learning'}, {'pointer': '/learning', 'quote': 'invented'},
])
async def test_invalid_source_slot_cannot_reach_a_canonical_judgment(tmp_path, choice):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Select only a code-owned source', ['Reject invented or transcribed identity'])
    payload = context()
    def invalid(call, _):
        body = deepcopy(call['output']); body['opinions'][0]['evidence_refs'] = [choice]; return body
    transport = install(store, engine, mutate=invalid)
    try:
        with pytest.raises(m.PolicyError, match='repeated'):
            await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'invalid-source-slot', payload)
        assert len(transport.calls) == 2 and not store.records('semantic_wire_output')
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()
