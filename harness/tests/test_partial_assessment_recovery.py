"""Real protected HTTP/SQLite composition; model judgments are synthetic fixtures."""
import asyncio
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4, uuid5

import httpx
import pytest

from policy_harness import models as m
from policy_harness import semantic_wire as wire
from policy_harness.learning_projection import current_input
from policy_harness.providers import ProviderError
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import learning, operation
from tests.test_learning_convergence import idea
from tests.test_learning_update_contract import reopen
from tests.test_providers import envelope
from tests.test_semantic_wire_contract import install, source_choice, assert_wire_records
from tests.test_learning_wire_references import legacy_capture


PHASE = 'partial-assessment-before'
FAULTS = [4, 5, 16, 18, 26, 27, 28, 30, 31, 34]


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback_new', [False, True])
@pytest.mark.parametrize('old_evidence', [False, True])
async def test_held_old_assessment_gets_one_new_presentation_and_reuses_it(tmp_path, monkeypatch, feedback_new, old_evidence):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain source bound assessment', ['Independent review remains required'])
    targets, context = inputs(1)
    body = ('Evidence about target-0 and its dependencies. ' * 620)
    context['result']['data']['response_analysis'] = {'source': {
        'id': 'saved-source', 'url': 'https://example.test/source', 'text': body,
        'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}}
    real_evidence = wire._assessment_evidence
    old_revision = (wire.ASSESSMENT_EVIDENCE if old_evidence
                    else wire.REFERENCED_ASSESSMENT_DEFAULTS)
    old_presentation = wire.ASSESSMENT_EVIDENCE if old_evidence else None
    def old_capture(saved_store, saved_task, role, phase, payload, schema):
        # Persist the exact historical capture at its original send. The
        # current fresh-singleton selector must not fabricate its revision.
        binding = wire._task_binding(saved_task, saved_store.policy_hash)
        identity = wire._identity(binding, role, phase, payload, schema, old_revision,
                                  presentation_revision=old_presentation)
        key = digest(identity)
        def create():
            saved = saved_store.record_get('semantic_wire_request', key)
            if saved is None:
                saved = {'id': key, **identity, 'nonce': uuid4().hex,
                         'canonical_input': deepcopy(payload),
                         'association': wire.associations(payload, schema.__name__, saved_store,
                             revision=old_revision, presentation_revision=old_presentation,
                             binding=binding)}
                saved_store.record('semantic_wire_request', key, saved)
            return {'id': key, 'sha256': digest(saved)}
        return saved_store._transaction(create)
    notes = ['one exchange judged', 'Review needs another source']
    def old_defect(call, _):
        if call['schema'] is m.AssessmentBatch:
            raw = response_rows(call)
            proposed = raw['assessments'][0]['ideas'][1]
            raw['assessments'][0]['ideas'].extend(
                dict(proposed, proposal=f'Distinct assessed improvement {i}') for i in range(21))
            raw['assessments_note'] = notes.pop(0)
            return raw
    first = install(store, engine, mutate=old_defect)
    with monkeypatch.context() as historical:
        historical.setattr(wire, 'capture', old_capture)
        with pytest.raises(m.PolicyError, match='repeated|defect'):
            await engine._assess_targets(task['id'], PHASE, targets, context)
    old_calls = deepcopy(store.records('bounded_model_call'))
    old_responses = deepcopy(store.records('model_response'))
    assert len(first.calls) == 2 and len(old_calls) == 1
    assert old_calls[0]['feedback_trace']['send_state'] == 'held'
    old_capture = store.record_get('semantic_wire_request', old_calls[0]['wire_trace']['initial_capture']['id'])
    assert old_capture['wire_revision'] == (wire.ASSESSMENT_EVIDENCE if old_evidence
                                            else wire.REFERENCED_ASSESSMENT_DEFAULTS)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    def new_response(call, _):
        if call['schema'] is m.AssessmentBatch:
            raw = deepcopy(call['output'])
            evidence = real_evidence(call['canonical'])
            first_page = evidence['excerpts'][0]
            citation = {'source_id': evidence['source_id'],
                        'source_sha256': evidence['source_sha256'],
                        'start_byte': first_page['start_byte'],
                        'end_byte': min(first_page['start_byte'] + 24, first_page['end_byte'])}
            for row in raw['assessments']:
                row['source_citations'] = ([] if feedback_new and len(second.calls) == 1 else [citation])
                row['requested_source_ranges'] = []
            return raw
    second = install(store, engine, mutate=new_response)
    try:
        result = await engine._assess_targets(task['id'], PHASE, targets, context)
        engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        new_calls = [row for row in store.records('bounded_model_call') if row['id'] != old_calls[0]['id']]
        assert len(new_calls) == 1
        assert any(row['status'] == 'succeeded' for row in new_calls)
        assert new_calls[0]['request_key'] != old_calls[0]['request_key']
        assert new_calls[0]['payload']['assessment_revision_transition']['parent_call']['id'] == old_calls[0]['id']
        capture = store.record_get('semantic_wire_request', new_calls[0]['wire_trace']['initial_capture']['id'])
        assert capture['wire_revision'] == wire.ASSESSMENT_COMPACT
        assert len(second.calls) == (2 if feedback_new else 1)
        again = await engine._assess_targets(task['id'], PHASE, targets, context)
        assert again == result and len(second.calls) == (2 if feedback_new else 1)
        assert store.records('bounded_model_call')[0] == old_calls[0]
        for item in old_responses:
            assert store.record_get('model_response', item['id']) == item
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('last_note,recovers', [('', True), ('one exchange judged', True),
    ('Review needs another source', False)])
async def test_saved_outer_note_recovers_only_complete_bound_rows(tmp_path, last_note, recovers):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain the protected judgment', ['Independent review remains required'])
    targets, context = inputs(1)
    notes = ['one exchange judged', last_note]
    def extra(call, _):
        if call['schema'] is m.AssessmentBatch:
            body = response_rows(call)
            proposed = body['assessments'][0]['ideas'][1]
            body['assessments'][0]['ideas'].extend(
                dict(proposed, proposal=f'Distinct assessed improvement {index}') for index in range(21))
            assert len(body['assessments'][0]['ideas']) == 23
            body['assessments_note'] = notes.pop(0)
            return body
    first = install(store, engine, mutate=extra)
    with pytest.raises(m.PolicyError, match='repeated|defect'):
        await engine._assess_targets(task['id'], PHASE, targets, context)
    assert len(first.calls) == 2 and executor.calls == []
    saved_calls = deepcopy(store.records('bounded_model_call'))
    saved_events = deepcopy(store.events(task['id']))
    saved_responses = deepcopy(store.records('model_response'))
    assert all(row['status'] == 'failed' for row in saved_calls)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        if recovers:
            result = await engine._assess_targets(task['id'], PHASE, targets, context)
            assert len(result) == 1 and result[0]['target_id'] == targets[0]['id']
            engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
            retained = next(row for row in store.records('bounded_model_call') if row.get('feedback_trace', {}).get('rejected_events'))
            # The saved call is still a failed call; its latest exact response
            # supplies a separate retained subset, not an accepted old result.
            assert retained['status'] == 'failed'
            assert store.record_get('bounded_model_completed', retained['key']) is None
            review_input = dict(context, targets=targets, assessments=result)
            assert not store.records('bounded_review_responses')
            await engine.bounded_judgments.review_proposal(task['id'], context['operation'],
                'outer-note-review', review_input)
            assert store.records('bounded_review_responses')
            assert len(second.calls) == 1 and second.calls[0]['schema'] is m.Review
        else:
            with pytest.raises(m.PolicyError, match='repeated|defect'):
                await engine._assess_targets(task['id'], PHASE, targets, context)
            assert second.calls == []
        assert all(store.record_get('model_response', row['id']) == row for row in saved_responses)
        assert store.events(task['id'])[:len(saved_events)] == saved_events
        assert all(store.record_get('bounded_model_call', row['id']) == row for row in saved_calls)
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()


def inputs(count=36):
    op = operation('file_list', args={})
    result = m.OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    context = current_input({'operation': op, 'result': result,
        'learning': learning(ideas=[idea('known-candidate')]).model_dump(),
        'timing': 'Assess this exact current proposal with all peer dependencies.'})
    targets = [{'id': f'target-{i}', 'value': f'Full original target {i}'} for i in range(count)]
    return targets, context


def response_rows(call):
    body = deepcopy(call['output'])
    for target, row in zip(call['canonical']['targets'], body['assessments']):
        row['rationale'] = 'Original judgment for ' + target['id']
        proposed = deepcopy(row['ideas'][0])
        proposed['proposal'] = 'A distinct proposed improvement for ' + target['id']
        row['ideas'] = [{'candidate_slot': 0, 'consideration': 'Apply the saved candidate to ' + target['id']}, proposed]
    return body


def corrupt_rows(call, faulty):
    body = response_rows(call)
    for index in faulty: body['assessments'][index]['recurrence'] = 'yes; explanation belongs in the rationale'
    return body


def failed_subset(store):
    return next(row for row in store.records('bounded_model_call')
        if row.get('feedback_trace', {}).get('partial_assessments'))


def force_correction_pages(engine, monkeypatch):
    fits = engine.bounded_judgments._fits
    def bounded(task_id, phase, payload, schema, role=None):
        if schema is m.AssessmentBatch and 'assessment_correction' in payload and len(payload['targets']) > 1:
            return False
        return fits(task_id, phase, payload, schema, role)
    monkeypatch.setattr(engine.bounded_judgments, '_fits', bounded)


@pytest.mark.asyncio
async def test_mixed_rows_preserve_raw_ids_and_reach_complete_independent_review(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep successful row judgments', ['Full independent review remains required'])
    targets, context = inputs(); raw = {}; review_inputs = []
    def mutate(call, _):
        if call['schema'] is m.AssessmentBatch:
            if 'assessment_correction' not in call['canonical']:
                raw.update(corrupt_rows(call, FAULTS)); return deepcopy(raw)
            return response_rows(call)
        if call['schema'] is m.Review:
            review_inputs.append(deepcopy(call['canonical']))
            body = deepcopy(call['output'])
            body['opinions'][0].update(observation='A retained peer judgment still needs substantive correction',
                evidence_refs=[source_choice(call, '/assessments/0/assessment/rationale')])
            return body
        if call['schema'] is m.Disposition:
            body = deepcopy(call['output'])
            body.update(verdict='revise', revision_scope='governed',
                revision_targets=[source_choice(call, '/assessments/0/assessment/rationale')])
            return body
    transport = install(store, engine, mutate=mutate)
    try:
        result = await engine._assess_targets(task['id'], PHASE, targets, context)
        batches = [call for call in transport.calls if call['schema'] is m.AssessmentBatch]
        assert [len(call['canonical']['targets']) for call in batches] == [36, 10]
        assert [row['target_id'] for row in result] == [target['id'] for target in targets]
        original = batches[0]['capture']; failed = failed_subset(store)
        subset = failed['feedback_trace']['partial_assessments']
        assert failed['status'] == 'failed' and store.record_get('bounded_model_completed', failed['key']) is None
        assert subset['raw_sha256'] == digest(raw)
        assert [row['index'] for row in subset['unresolved']] == FAULTS
        assert [row['raw'] for row in subset['unresolved']] == [raw['assessments'][i] for i in FAULTS]
        retained = [i for i in range(36) if i not in FAULTS]
        for index in retained:
            row = result[index]['assessment']; original_row = raw['assessments'][index]
            assert list(row['thinking_targets'].values()) == original_row['thinking_values']
            assert row['rationale'].startswith(original_row['rationale'] + '\nCandidate known-candidate:')
            expected = 'wire-' + uuid5(UUID(original['nonce']), digest(raw) + f':assessments/{index}/ideas/1').hex
            assert row['ideas'][1]['id'] == expected
            assert row['ideas'][0]['id'] == 'known-candidate'
        correction = batches[1]['canonical']['assessment_correction']
        assert correction['original_targets'] == targets
        assert correction['retained_assessments'] == [result[i] for i in retained]
        assert [t['id'] for t in batches[1]['canonical']['targets']] == [targets[i]['id'] for i in FAULTS]
        assert not any(output['capture']['id'] == original['id'] for output in store.records('semantic_wire_output'))
        engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        recovered_targets, recovered_context, _ = engine.bounded_judgments._retained_assessment_parts(
            task['id'], PHASE, result, context=context)
        assert (recovered_targets, recovered_context) == (targets, context)
        reviewed_input = dict(context, targets=targets, assessments=result)
        review = await engine.bounded_judgments.review_proposal(task['id'], context['operation'], 'partial-review', reviewed_input)
        assert review_inputs[0]['assessments'] == result and review_inputs[0]['targets'] == targets
        disposition = await engine.bounded_judgments.disposition_proposal(task['id'], context['operation'],
            'partial-disposition', dict(reviewed_input, review=review.model_dump(), sources=[]))
        assert disposition.verdict == 'revise'
        assert next(call for call in transport.calls if call['schema'] is m.Review)['role'] == 'reviewer'
        assert len(disposition.opinion_responses) == len(review.opinions)
        assert executor.calls == [] and store.records('knowledge_application') == []
        assert_wire_records(store, transport.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['before_correction', 'after_successful_page', 'unknown_correction'])
async def test_partial_rows_reopen_and_preserve_success_or_hold_unknown(tmp_path, monkeypatch, cut):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume the exact unresolved rows', ['No successful or unknown send replay'])
    targets, context = inputs(4)
    if cut == 'after_successful_page': force_correction_pages(engine, monkeypatch)
    def interrupt(call, _):
        if call['schema'] is not m.AssessmentBatch: return None
        if 'assessment_correction' not in call['canonical']:
            if cut == 'before_correction': engine.stop_requested.add(task['id'])
            return corrupt_rows(call, [1, 3])
        if cut == 'unknown_correction': raise httpx.ReadTimeout('Synthetic unobserved correction')
        if cut != 'after_successful_page': engine.stop_requested.add(task['id'])
        return response_rows(call)
    first = install(store, engine, mutate=interrupt)
    if cut == 'after_successful_page':
        saved_record = store.record
        def stop_after_completed_page(kind, identity, body):
            result = saved_record(kind, identity, body)
            if kind == 'bounded_model_completed' and 'assessment_correction' in body.get('payload', {}):
                engine.stop_requested.add(task['id'])
            return result
        monkeypatch.setattr(store, 'record', stop_after_completed_page)
    with pytest.raises(ProviderError if cut == 'unknown_correction' else asyncio.CancelledError):
        await engine._assess_targets(task['id'], PHASE, targets, context)
    saved = deepcopy(store.records('bounded_model_call')); events = deepcopy(store.events(task['id']))
    partial = deepcopy(failed_subset(store))
    assert len(first.calls) == (1 if cut == 'before_correction' else 2)
    completed_page = None
    if cut == 'after_successful_page':
        completed_page = next(row for row in saved if 'assessment_correction' in row['payload'])
        assert completed_page['status'] == 'succeeded'
        assert completed_page['feedback_trace']['send_state'] == 'succeeded'
        assert 'first_fault' not in completed_page
        assert store.record_get('bounded_model_completed', completed_page['key']) == completed_page
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine, mutate=lambda call, _: response_rows(call) if call['schema'] is m.AssessmentBatch else None)
    if cut == 'after_successful_page': force_correction_pages(engine, monkeypatch)
    try:
        if cut == 'unknown_correction':
            with pytest.raises(m.PolicyError, match='PROVENANCE'):
                await engine._assess_targets(task['id'], PHASE, targets, context)
            assert second.calls == []
        else:
            result = await engine._assess_targets(task['id'], PHASE, targets, context)
            assert len(second.calls) == 1
            expected_ids = ['target-3'] if cut == 'after_successful_page' else ['target-1', 'target-3']
            assert [t['id'] for t in second.calls[0]['canonical']['targets']] == expected_ids
            assert [row['value'] for row in partial['feedback_trace']['partial_assessments']['retained']] == [result[0], result[2]]
            if completed_page is not None:
                assert completed_page['result']['assessments'] == [result[1]]
            assert await engine._assess_targets(task['id'], PHASE, targets, context) == result
            assert len(second.calls) == 1
            engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        assert all(store.record_get('bounded_model_call', row['id']) == row for row in saved)
        assert store.events(task['id'])[:len(events)] == events
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['source', 'policy', 'settings', 'lease', 'role', 'response', 'semantic_context'])
async def test_retained_subset_applicability_is_not_historical_parse_validity(tmp_path, boundary):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve applicable rows only', ['Changed dependencies are new judgments'])
    targets, context = inputs(4)
    def interrupt(call, _):
        engine.stop_requested.add(task['id']); return corrupt_rows(call, [1, 3])
    install(store, engine, mutate=interrupt)
    with pytest.raises(asyncio.CancelledError): await engine._assess_targets(task['id'], PHASE, targets, context)
    saved = deepcopy(failed_subset(store))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine, mutate=lambda call, _: response_rows(call)
                     if call['schema'] is m.AssessmentBatch else None)
    role = None
    if boundary in {'source', 'lease'}:
        changed = store.get_task(task['id'])
        if boundary == 'source': changed['source_hash'] = 'changed-source'
        else: changed['state']['delegation_lease'] = {'foreign': 'changed lease'}
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(changed), task['id']))
    elif boundary == 'policy': engine.policy.hash = 'changed-policy'
    elif boundary == 'settings': engine.gateway.settings.values['model'] = 'changed-model'
    elif boundary == 'role': role = 'reviewer'
    elif boundary == 'response':
        ref = saved['feedback_trace']['partial_assessments']['response']
        response = store.record_get('model_response', ref['id'])
        store.record('model_response', ref['id'], dict(response, http_status=500))
    try:
        if boundary == 'semantic_context':
            changed = dict(context, timing='A changed semantic dependency requires every target to be reconsidered')
            result = await engine._assess_targets(task['id'], PHASE, targets, changed)
            assert len(second.calls) == 1 and second.calls[0]['canonical']['targets'] == targets
            assert 'assessment_correction' not in second.calls[0]['canonical']
            assert result[0]['assessment']['ideas'][1]['id'] != saved['feedback_trace']['partial_assessments']['retained'][0]['value']['assessment']['ideas'][1]['id']
        else:
            with pytest.raises(m.PolicyError, match='PROVENANCE|changed|differs'):
                if boundary == 'role':
                    await engine.bounded_judgments._call(task['id'], PHASE, saved['payload'], m.AssessmentBatch, role=role)
                else: await engine._assess_targets(task['id'], PHASE, targets, context)
            assert second.calls == []
        assert store.record_get('bounded_model_call', saved['id']) == saved and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['outer_count', 'duplicate_json', 'foreign_selector', 'duplicate_selector', 'truncated', 'unknown'])
async def test_ambiguous_or_unobserved_batch_cannot_create_retained_rows(tmp_path, fault):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('No invented row associations', ['Only complete exact rows may be retained'])
    targets, context = inputs(4)
    def invalid(call, _):
        body = corrupt_rows(call, [1])
        if fault == 'outer_count': body = response_rows(call); body['assessments'].pop()
        elif fault == 'foreign_selector': body['assessments'][1]['ideas'][0]['candidate_slot'] = 500
        elif fault == 'duplicate_selector': body['assessments'][1]['ideas'].append(deepcopy(body['assessments'][1]['ideas'][0]))
        elif fault == 'duplicate_json':
            content = '{"assessments":' + json.dumps(body['assessments']) + ',"assessments":[]}'
            return httpx.Response(200, json=envelope(content))
        elif fault == 'truncated':
            response = envelope(json.dumps(body)); response['choices'][0]['finish_reason'] = 'length'
            return httpx.Response(200, json=response)
        elif fault == 'unknown': raise httpx.ReadTimeout('Synthetic unknown original batch')
        return body
    transport = install(store, engine, mutate=invalid)
    payload = dict(context, targets=targets, target={'ids': [t['id'] for t in targets]})
    try:
        with pytest.raises((m.PolicyError, ProviderError)):
            await engine._call(task['id'], PHASE, payload, m.AssessmentBatch)
        assert all('partial_assessments' not in row.get('feedback_trace', {}) for row in store.records('bounded_model_call'))
        assert store.records('bounded_assessment_result') == []
        assert all(call['canonical']['targets'] == targets for call in transport.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_duplicate_target_identity_is_held_before_any_response(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Bind every row exactly once', ['Repeated targets are not independent rows'])
    targets, context = inputs(2); targets[1]['id'] = targets[0]['id']
    transport = install(store, engine)
    try:
        with pytest.raises(m.PolicyError): await engine._assess_targets(task['id'], PHASE, targets, context)
        assert transport.calls == [] and store.records('bounded_assessment_result') == [] and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_already_successful_whole_correction_is_never_replaced_by_old_subset(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain the already accepted later whole result', ['Do not roll back to the earlier rejection'])
    targets, context = inputs(4)
    count = 0
    def old_response(call, _):
        nonlocal count
        count += 1
        return corrupt_rows(call, [1]) if count == 1 else response_rows(call)
    first = install(store, engine, mutate=old_response)
    # Reproduce the prior writer at the original send, never restamp a saved call.
    recovery = engine._wire_recovery(m.AssessmentBatch)
    monkeypatch.setattr(recovery, '_partial_assessments', lambda *args: None)
    result = await engine._assess_targets(task['id'], PHASE, targets, context)
    old = deepcopy(store.records('bounded_model_call')); outputs = deepcopy(store.records('semantic_wire_output'))
    assert len(first.calls) == 2 and all(len(call['canonical']['targets']) == 4 for call in first.calls)
    assert all('partial_assessments' not in row.get('feedback_trace', {}) for row in old)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        assert await engine._assess_targets(task['id'], PHASE, targets, context) == result
        assert second.calls == []
        assert store.records('bounded_model_call') == old and store.records('semantic_wire_output') == outputs
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('progress', [True, False])
async def test_repeated_field_defect_requires_strictly_smaller_unresolved_rows(tmp_path, progress):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Carry actual partial progress forward', ['No same unresolved-page retry loop'])
    targets, context = inputs(4)
    def response(call, _):
        count = len(call['canonical']['targets'])
        if count == 4: return corrupt_rows(call, [1, 2, 3])
        if count == 3: return corrupt_rows(call, [1, 2])
        return response_rows(call) if progress else corrupt_rows(call, [0, 1])
    transport = install(store, engine, mutate=response)
    try:
        if progress:
            result = await engine._assess_targets(task['id'], PHASE, targets, context)
            assert [row['target_id'] for row in result] == [target['id'] for target in targets]
            engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        else:
            with pytest.raises(m.PolicyError, match='repeated'):
                await engine._assess_targets(task['id'], PHASE, targets, context)
            with pytest.raises(m.PolicyError, match='repeated'):
                await engine._assess_targets(task['id'], PHASE, targets, context)
            assert store.records('bounded_assessment_result') == []
        assert [len(call['canonical']['targets']) for call in transport.calls] == [4, 3, 2]
        partials = [row for row in store.records('bounded_model_call') if row.get('feedback_trace', {}).get('partial_assessments')]
        assert len(partials) == 2 and all(row['status'] == 'failed' for row in partials)
        assert [len(call['canonical']['assessment_correction']['retained_assessments']) for call in transport.calls[1:]] == [1, 2]
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_repeated_one_row_mixed_candidate_reopens_as_distinct_correction(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Complete the original artifact after source assessment',
                             ['Keep the rejected judgment unadmitted'])
    targets, context = inputs(1)

    def mixed_response(call, _):
        body = response_rows(call)
        row = body['assessments'][0]
        row['ideas'] = [dict(row['ideas'][1], candidate_slot=0,
                             consideration='Retain the old rejection while considering the changed proposal')]
        return body

    first = install(store, engine, mutate=mixed_response)
    recovery = engine._wire_recovery(m.AssessmentBatch)
    # This stored predecessor reached its repeated-defect terminal before the
    # unresolved-only classifier existed. Resume must consume its saved bytes.
    with monkeypatch.context() as prior:
        prior.setattr(recovery, '_partial_assessments', lambda *args, **kwargs: None)
        with pytest.raises(m.PolicyError, match='repeated'):
            await engine._assess_targets(task['id'], PHASE, targets, context)
    assert len(first.calls) == 2
    old_calls = deepcopy(store.records('bounded_model_call'))
    old_responses = deepcopy(store.records('model_response'))
    old_captures = deepcopy(store.records('semantic_wire_request'))
    assert old_calls[-1]['feedback_trace']['send_state'] == 'held'
    assert store.records('bounded_assessment_result') == []
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)

    second = install(store, engine, mutate=lambda call, _: response_rows(call))
    try:
        result = await engine._assess_targets(task['id'], PHASE, targets, context)
        assert len(second.calls) == 1
        correction = second.calls[0]['canonical']['assessment_correction']
        assert correction['retained_assessments'] == []
        assert [row['index'] for row in correction['unresolved_rows']] == [0]
        assert correction['unresolved_rows'][0]['raw']['ideas'][0]['candidate_slot'] == 0
        assert 'proposal' in correction['unresolved_rows'][0]['raw']['ideas'][0]
        assert 'separate ideas' in correction['instruction']
        assert result[0]['assessment']['ideas'][0]['id'] == 'known-candidate'
        assert len(result[0]['assessment']['ideas']) == 2
        assert store.records('model_response')[:len(old_responses)] == old_responses
        assert store.records('semantic_wire_request')[:len(old_captures)] == old_captures
        assert store.records('bounded_model_call')[0] == old_calls[0]
        engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        assert store.records('bounded_assessment_result')
        assert store.records('bounded_review_result') == []
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('observed_shape', ['1456-missing-target', '1457-override'])
async def test_saved_mixed_idea_recovery_keeps_valid_peer_and_corrects_only_unknown(
        tmp_path, monkeypatch, observed_shape):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve the valid idea from the rejected assessment',
                             ['Judge the mixed candidate separately'])
    targets, context = inputs(1)

    def mixed_response(call, _):
        body = response_rows(call)
        row = body['assessments'][0]
        valid = row['ideas'][1]
        if observed_shape == '1456-missing-target':
            mixed = {key: value for key, value in valid.items() if key != 'target'}
            mixed.update(candidate_slot=0, consideration='Old selection and changed text both matter')
        else:
            mixed = {'candidate_slot': 0, 'consideration': 'Old selection may apply',
                     'consideration_override': 'Its current use needs a changed reason'}
        row['ideas'] = [valid, mixed]
        return body

    first = install(store, engine, mutate=mixed_response)
    recovery = engine._wire_recovery(m.AssessmentBatch)
    with monkeypatch.context() as prior:
        prior.setattr(recovery, '_partial_assessments', lambda *args, **kwargs: None)
        with pytest.raises(m.PolicyError, match='repeated'):
            await engine._assess_targets(task['id'], PHASE, targets, context)
    assert len(first.calls) == 2
    saved_calls = deepcopy(store.records('bounded_model_call'))
    saved_responses = deepcopy(store.records('model_response'))
    assert saved_calls[-1]['feedback_trace']['send_state'] == 'held'
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)

    def corrected(call, _):
        body = response_rows(call)
        if call['schema'] is m.AssessmentBatch:
            assert 'retained_idea_rows' in call['canonical']['assessment_correction']
            body['assessments'][0]['ideas'] = [body['assessments'][0]['ideas'][0]]
        return body

    second = install(store, engine, mutate=corrected)
    try:
        result = await engine._assess_targets(task['id'], PHASE, targets, context)
        assert len(second.calls) == 1
        marker = second.calls[0]['canonical']['assessment_correction']
        assert marker['retained_idea_rows'][0]['unresolved_idea_indexes'] == [1]
        assert marker['retained_idea_rows'][0]['retained_ideas'][0]['index'] == 0
        assert marker['unresolved_rows'][0]['raw']['ideas'][1] == mixed_response(first.calls[-1], None)['assessments'][0]['ideas'][1]
        assert len(result[0]['assessment']['ideas']) == 2
        assert result[0]['assessment']['ideas'][0]['proposal'].startswith('A distinct proposed improvement')
        assert store.records('model_response')[:len(saved_responses)] == saved_responses
        assert store.records('bounded_model_call')[0] == saved_calls[0]
        engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_empty_catalog_repeated_selector_restores_old_capture_and_partitions(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Finish all nine fresh judgments', ['Old invalid rows remain unadmitted'])
    targets, context = inputs(9)
    context['learning']['ideas'] = []
    sent = []

    def response(call, _):
        if call['schema'] is not m.AssessmentBatch: return None
        sent.append(call)
        body = deepcopy(call['output'])
        if len(call['canonical']['targets']) == 9:
            if len(sent) == 1:
                body['assessments'] = body['assessments'][:1]
            for index, row in enumerate(body['assessments']):
                row['ideas'] = [{'candidate_slot': index, 'consideration': 'This target has no saved Idea'}]
        return body

    transport = install(store, engine, mutate=response)
    current_capture = wire.capture

    def prior_capture(store, task, role, phase, payload, schema):
        if schema is m.AssessmentBatch and len(payload.get('targets', [])) == 9:
            return legacy_capture(store, task, role, phase, payload, schema,
                                  revision=wire.REFERENCED_ASSESSMENT_DEFAULTS)
        return current_capture(store, task, role, phase, payload, schema)

    with monkeypatch.context() as historical:
        historical.setattr(wire, 'capture', prior_capture)
        result = await engine._assess_targets(task['id'], PHASE, targets, context)
    try:
        assert [row['target_id'] for row in result] == [target['id'] for target in targets]
        assert len(sent) > 3 and [len(call['canonical']['targets']) for call in sent[:3]] == [9, 9, 9]
        old_captures = [call['capture'] for call in sent[:3]]
        assert all(capture['wire_revision'] == wire.REFERENCED_ASSESSMENT_DEFAULTS
                   and capture['association']['known_ideas'] == [] for capture in old_captures)
        assert wire.capture(store, task, sent[0]['role'], PHASE, sent[0]['canonical'], m.AssessmentBatch) == {
            'id': old_captures[0]['id'], 'sha256': digest(old_captures[0])}
        assert all(call['capture']['wire_revision'] == wire.ASSESSMENT_DEFAULTS for call in sent[3:])
        assert all(call['capture']['association']['known_ideas'] == [] for call in sent[3:])
        assert 'candidate_slot' not in json.dumps(wire.wire_schema('AssessmentBatch', sent[3]['canonical'],
                                                                    revision=wire.ASSESSMENT_DEFAULTS).model_json_schema())
        old_call = next(row for row in store.records('bounded_model_call')
                        if len(row.get('payload', {}).get('targets', [])) == 9)
        assert old_call['status'] == 'failed' and len(old_call['feedback_trace']['rejected_events']) == 3
        assert store.record_get('bounded_model_completed', old_call['key']) is None
        assert not any(output['capture']['id'] in {capture['id'] for capture in old_captures}
                       for output in store.records('semantic_wire_output'))
        assert all(row['assessment']['ideas'] and 'candidate_slot' not in row['assessment']['ideas'][0]
                   for row in result)
        assert_wire_records(store, transport.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['current', 'source', 'response', 'unknown'])
async def test_legacy_held_optional_refs_reuses_final_valid_rows(tmp_path, monkeypatch, boundary):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Complete the pending nine assessments', ['Preserve valid rows and independently review the whole'])
    targets, context = inputs(9); originals = []
    def legacy_response(call, count):
        if boundary == 'unknown' and count == 2:
            raise httpx.ReadTimeout('Synthetic unobserved last assessment response')
        body = response_rows(call)
        for index, row in enumerate(body['assessments']):
            row.pop('evidence_refs')
            if index < 2:
                row['ideas'][1].update(disposition='investigate',
                    next_operation={'defer': {'current_completion_impact': 'Claimed optional improvement; review this claim',
                        'next_trigger': 'After the original artifact is completed'}})
            elif count == 1:
                # This earlier complete envelope also has unaccepted short
                # thinking rows. Only the authenticated final response is used.
                row['thinking_values'] = row['thinking_values'][:3]
        originals.append(deepcopy(body))
        return body
    first = install(store, engine, mutate=legacy_response)
    def old_capture(*args): return legacy_capture(*args, revision=wire.LEARNING_BINDINGS)
    with monkeypatch.context() as prior:
        prior.setattr(wire, 'capture', old_capture)
        prior.setattr(engine._wire_recovery(m.AssessmentBatch), '_partial_assessments', lambda *args, **kwargs: None)
        with pytest.raises((m.PolicyError, ProviderError)):
            await engine._assess_targets(task['id'], PHASE, targets, context)
    assert len(first.calls) == 2
    old = deepcopy(store.records('bounded_model_call'))
    old_captures = deepcopy(store.records('semantic_wire_request'))
    events = deepcopy(store.events(task['id']))
    failed = old[-1]
    assert 'partial_assessments' not in failed['feedback_trace']
    assert failed['feedback_trace']['send_state'] == ('dispatching' if boundary == 'unknown' else 'held')
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    def corrected(call, _):
        if call['schema'] is not m.AssessmentBatch: return None
        body = response_rows(call)
        for row in body['assessments']:
            row.pop('evidence_refs')
            row['ideas'][1].update(disposition='investigate', next_operation={'defer': {
                'required_for_current_completion': False,
                'current_completion_impact': 'The exact mandatory artifact does not depend on this optional improvement',
                'next_trigger': 'After the mandatory artifact is complete'}})
        return body
    second = install(store, engine, mutate=corrected)
    if boundary == 'source':
        changed = store.get_task(task['id']); changed['source_hash'] = 'changed-source'
        store.db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(changed), task['id']))
    elif boundary == 'response':
        last = next(event for event in events if event['seq'] == failed['feedback_trace']['rejected_events'][-1]['seq'])
        ref = last['detail']['metadata']['response_record']
        response = store.record_get('model_response', ref['id'])
        store.record('model_response', ref['id'], dict(response, http_status=500))
    try:
        if boundary != 'current':
            with pytest.raises((m.PolicyError, ProviderError)):
                await engine._assess_targets(task['id'], PHASE, targets, context)
            assert second.calls == [] and store.records('bounded_assessment_result') == []
        else:
            result = await engine._assess_targets(task['id'], PHASE, targets, context)
            assert len(second.calls) == 1
            call = second.calls[0]; correction = call['canonical']['assessment_correction']
            assert call['capture']['wire_revision'] == wire.REFERENCED_ASSESSMENT_DEFAULTS
            assert [target['id'] for target in call['canonical']['targets']] == ['target-0', 'target-1']
            assert correction['raw_sha256'] == digest(originals[-1]) != digest(originals[0])
            assert [row['index'] for row in correction['unresolved_rows']] == [0, 1]
            for row in correction['unresolved_rows']:
                assert 'required_for_current_completion' not in row['raw']['ideas'][1]['next_operation']['defer']
            assert correction['retained_assessments'] == result[2:]
            assert [row['target_id'] for row in result] == [target['id'] for target in targets]
            final_capture = first.calls[-1]['capture']
            for index in range(2, 9):
                value = result[index]['assessment']
                assert value['evidence_refs'] == []
                assert list(value['thinking_targets'].values()) == originals[-1]['assessments'][index]['thinking_values']
                expected = 'wire-' + uuid5(UUID(final_capture['nonce']), digest(originals[-1]) + f':assessments/{index}/ideas/1').hex
                assert value['ideas'][1]['id'] == expected
            for row in result[:2]:
                assert m.idea_deferral(row['assessment']['ideas'][1])['required_for_current_completion'] is False
            engine.bounded_judgments.authenticate_assessments(task['id'], PHASE, targets, context, result)
            assert await engine._assess_targets(task['id'], PHASE, targets, context) == result
            assert len(second.calls) == 1
            reviewed = dict(context, targets=targets, assessments=result)
            review = await engine.bounded_judgments.review_proposal(task['id'], context['operation'], 'retained-default-review', reviewed)
            disposition = await engine.bounded_judgments.disposition_proposal(task['id'], context['operation'],
                'retained-default-disposition', dict(reviewed, review=review.model_dump(), sources=[]))
            assert second.calls[1]['role'] == 'reviewer' and second.calls[1]['canonical']['assessments'] == result
            assert len(disposition.opinion_responses) == len(review.opinions)
        for record in old: assert store.record_get('bounded_model_call', record['id']) == record
        for capture in old_captures: assert store.record_get('semantic_wire_request', capture['id']) == capture
        assert store.events(task['id'])[:len(events)] == events
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()
