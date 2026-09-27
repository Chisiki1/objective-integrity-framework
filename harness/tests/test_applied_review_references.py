"""Real applied-Knowledge consumers; external observations/judgments are fixtures."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from policy_harness import models as m
from policy_harness import semantic_wire as wire
from policy_harness.engine import Quiesced
from policy_harness.store import digest
from tests.test_application_contract_recovery import recorded_application
from tests.test_core import runtime
from tests.test_knowledge import journal, operation
from tests.test_learning_update_contract import reopen
from tests.test_learning_wire_references import legacy_capture
from tests.test_semantic_wire_contract import install, source_choice, assert_wire_records


PHASE = 'applied-reference-outcome'
EXPECTED = '/bundle/accepted_input/accepted_learning/skill_updates/0/expected_hash'
BEFORE = '/bundle/actual_result/skill_transitions/entries/0/before/hash'
AFTER = '/bundle/actual_result/skill_transitions/entries/0/after/hash'
ASSESSMENT = '/bundle/assessments/0/assessment/rationale'
PARENT = '/bundle/accepted_input/parent_operation/args/text'


def committed_inputs(store, engine, task, *, choice_count=1):
    op, result, learned, selected = recorded_application(store, engine, task, [], persist=False)
    if choice_count != 1:
        op['decisions'] = [dict(op['decisions'][0], id=f'committed-choice-{i}')
                           for i in range(choice_count)]
        # Bind the observed Skill use to the same operation that is committed.
        learned = learned.model_copy(update={'applications': [
            dict(item, operation_sha256=digest(op)) for item in learned.applications]})
    learned = learned.model_copy(update={'classifications': ['use', 'improve'], 'skill_updates': [{
        'action': 'improve', 'id': selected[0]['id'], 'expected_hash': selected[0]['hash'],
        'content': selected[0]['procedure_clause'] + ' Preserve the original observation time.'}]})
    journal(store, task, op, result, learned, selected)
    outcome = engine.knowledge.apply(task, op, learned, result, [selected[0]['id']])
    context = {'accepted_learning': learned.model_dump(), 'pre': {'skills': {'selected': selected}},
               'parent_operation': operation(), 'accepted_review': {'disposition': {'verdict': 'proceed'}}}
    args = (task, op, PHASE, op['decisions'], outcome, result, {'sources': []})
    kwargs = {'context': context, 'consumer': {'kind': 'operation', 'operation_id': op['id']}}
    return args, kwargs


def partition_reviews(engine, monkeypatch):
    fits = engine.bounded_judgments._fits
    def bounded(task_id, phase, payload, schema, role=None):
        if schema is m.Review and 'source_records' not in payload and 'bounded_context' not in payload:
            return False
        if schema is m.Disposition and 'bounded_context' not in payload:
            return False
        return fits(task_id, phase, payload, schema, role)
    monkeypatch.setattr(engine.bounded_judgments, '_fits', bounded)


@pytest.mark.asyncio
@pytest.mark.parametrize('paged', [False, True])
async def test_applied_current_targets_retain_originals_and_ground_real_correction(tmp_path, monkeypatch, paged):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Review the actual committed effect', ['The parent artifact remains required'])
    args, kwargs = committed_inputs(store, engine, task)
    application = deepcopy(store.records('knowledge_application'))
    accepted = deepcopy(kwargs['context'])
    if paged: partition_reviews(engine, monkeypatch)
    def critique(call, _):
        body = deepcopy(call['output'])
        if call['schema'] is m.AssessmentBatch:
            body['assessments'][0]['rationale'] = 'This Knowledge write proves the parent file was delivered.'
            return body
        if call['schema'] is m.Review:
            body['opinions'][0].update(
                observation='The current after-assessment overstates the committed Knowledge effect.',
                rationale='Its rationale claims the pending parent file is delivered; a Knowledge commit does not show that.',
                evidence_refs=[source_choice(call, p) for p in (ASSESSMENT, EXPECTED, BEFORE, AFTER, PARENT)])
            return body
        if call['schema'] is m.Disposition:
            body.update(verdict='revise', rationale='Correct the overclaim through governed work; retain the committed Knowledge.',
                        revision_targets=[source_choice(call, ASSESSMENT)])
            body['opinion_decisions'][0].update(disposition='accept', rationale='The exact assessment rationale exceeds the observed effect.')
            return body
    transport = install(store, engine, mutate=critique)
    try:
        saved = await engine._judge_applied_knowledge(*args, **kwargs)
        reviews = [call for call in transport.calls if call['schema'] in {m.Review, m.Disposition}]
        assert reviews and all(call['capture']['wire_revision'] == wire.APPLIED_TARGETS for call in reviews)
        assert all(call['capture']['presentation_revision'] == wire.REFERENCE_TREE for call in reviews)
        reference = json.loads(saved['review']['review']['opinions'][0]['evidence_refs'][0])
        assert reference['pointer'] == ASSESSMENT
        assert reference['stage'] == 'current_after_assessment'
        assert reference['target_id'] == args[3][0]['id'] and reference['target_pointer'] == '/bundle/choices/0'
        chosen = {json.loads(item)['pointer']: json.loads(item)
                  for item in saved['review']['review']['opinions'][0]['evidence_refs']}
        assert all(not item['editable'] for item in chosen.values())
        assert chosen[EXPECTED]['stage'] == 'immutable_accepted_input'
        assert chosen[BEFORE]['stage'] == 'committed_transition_before'
        assert chosen[AFTER]['stage'] == 'committed_transition_after'
        assert chosen[PARENT]['stage'] == 'parent_operation_requirements'
        before = accepted['accepted_learning']['skill_updates'][0]['expected_hash']
        transition = args[4]['skill_transitions']['entries'][0]
        assert before == transition['before']['hash'] != transition['after']['hash']
        assert chosen[EXPECTED]['value_sha256'] == chosen[BEFORE]['value_sha256'] == digest(before)
        assert chosen[AFTER]['value_sha256'] == digest(transition['after']['hash'])
        assert chosen[PARENT]['value_sha256'] == digest(accepted['parent_operation']['args']['text'])
        assert saved['review']['disposition']['verdict'] == 'revise'
        correction = store.record_get('judgment_correction', saved['review']['id'])
        assert correction['status'] == 'pending' and correction['actual_result'] == args[4]
        assert 'Correction references:' in correction['review']['disposition']['rationale']
        assert 'current_after_assessment' in correction['review']['disposition']['rationale']
        assert kwargs['context'] == accepted and store.records('knowledge_application') == application
        before_calls = len(transport.calls)
        assert await engine._judge_applied_knowledge(*args, **kwargs) == saved
        assert len(transport.calls) == before_calls and executor.calls == []
        assert not (Path(task['workspace']) / 'answer.txt').exists()
        assert_wire_records(store, transport.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_normal_resume_keeps_committed_effect_and_43_assessments_after_review_stop(tmp_path, monkeypatch):
    """Real stop/run/resume and model recovery at a seeded applied-review boundary.

    The earlier application is a fixture; this is not an end-to-end artifact or
    external provider test. Only the selected cycle entry is narrowed below.
    """
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Finish the pending review', ['Keep the parent artifact pending'])
    args, kwargs = committed_inputs(store, engine, task, choice_count=43)
    applications = deepcopy(store.records('knowledge_application'))
    operations = deepcopy(store.operations(task['id']))
    completed = []

    def select_pending_cycle(target):
        async def enter(current):
            completed.append(await target._judge_applied_knowledge(current, *args[1:], **kwargs))
            raise Quiesced('Fixture boundary: applied review returned; parent artifact is still pending')
        monkeypatch.setattr(target, '_initialize_task', enter)

    select_pending_cycle(engine)
    entered = asyncio.Event()
    first = install(store, engine)

    async def interrupted_transport(request):
        body = json.loads(request.content)
        if f'Phase: {PHASE}_review\n' in body['messages'][0]['content']:
            entered.set()
            await asyncio.Event().wait()
        return await first(request)

    engine.gateway._transport = httpx.MockTransport(interrupted_transport)
    try:
        with monkeypatch.context() as old:
            old.setattr(wire, 'capture', legacy_capture)
            handle = engine.start_task(task['id'])
            await asyncio.wait_for(entered.wait(), timeout=30)
            await engine.stop_task(task['id'])
            await handle
        assert store.get_task(task['id'])['status'] == 'stopped'
        assert not completed and not store.records('applied_knowledge_review')
        prior_calls = deepcopy(store.records('bounded_model_call'))
        prior_captures = deepcopy(store.records('semantic_wire_request'))
        prior_events = deepcopy(store.events(task['id']))
        assessments = [r for r in prior_calls
                       if r['phase'] == PHASE + '_choices_after' and r['status'] == 'succeeded']
        assert len(assessments) == 1 and len(assessments[0]['result']['assessments']) == 43
        interrupted = [r for r in prior_calls if r['phase'] == PHASE + '_review']
        assert len(interrupted) == 1 and interrupted[0]['status'] == 'unobserved'
        assert any(c['canonical_schema'] == 'Review' and 'wire_revision' not in c for c in prior_captures)
        assert store.records('knowledge_application') == applications
        await engine.close(); store.close()
        store, engine = reopen(store, engine, executor)
        select_pending_cycle(engine)
        second = install(store, engine)
        resumed = engine.resume_task(task['id'])
        assert engine.resume_task(task['id']) is resumed
        await asyncio.wait_for(resumed, timeout=30)
        assert len(completed) == 1, store.events(task['id'])[-1]
        assert len(completed[0]['assessments']) == 43
        assert [c['schema'] for c in second.calls] == [m.Review, m.Disposition]
        assert second.calls[0]['capture']['wire_revision'] == wire.APPLIED_TARGETS
        assert completed[0]['review']['disposition']['verdict'] == 'proceed'
        assert all(store.record_get('bounded_model_call', r['id']) == r for r in prior_calls)
        assert all(store.record_get('semantic_wire_request', r['id']) == r for r in prior_captures)
        assert store.events(task['id'])[:len(prior_events)] == prior_events
        assert len([e for e in store.events(task['id']) if e['status'] == 'resume_requested']) == 1
        assert store.records('knowledge_application') == applications
        assert store.operations(task['id']) == operations and executor.calls == []
        before = len(second.calls)
        assert await engine._judge_applied_knowledge(store.get_task(task['id']), *args[1:], **kwargs) == completed[0]
        assert len(second.calls) == before
        assert not (Path(task['workspace']) / 'answer.txt').exists()
        assert store.get_task(task['id'])['status'] != 'completed'
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['review-path', 'review-slot', 'disposition-slot'])
async def test_applied_nonexistent_references_do_not_create_a_completed_review(tmp_path, invalid):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Reject nonexistent references', ['Keep the already committed effect'])
    args, kwargs = committed_inputs(store, engine, task)
    application = deepcopy(store.records('knowledge_application'))
    def invalid_reference(call, _):
        body = deepcopy(call['output'])
        if call['schema'] is m.Review and invalid.startswith('review'):
            body['opinions'][0]['evidence_refs'] = (
                ['bundle.accepted_input.web.sources'] if invalid == 'review-path' else [{'source_slot': 10**9}])
            return body
        if call['schema'] is m.Disposition and invalid == 'disposition-slot':
            body.update(verdict='revise', revision_targets=[{'source_slot': 10**9}])
            return body
    transport = install(store, engine, mutate=invalid_reference)
    try:
        with pytest.raises(m.PolicyError):
            await engine._judge_applied_knowledge(*args, **kwargs)
        assert not store.records('applied_knowledge_review') and not store.records('judgment_correction')
        assert store.records('knowledge_application') == application and executor.calls == []
        rejected = [event for event in store.events(task['id']) if event.get('status') == 'rejected_model_output']
        assert rejected
        assert any(call['capture']['wire_revision'] == wire.APPLIED_TARGETS for call in transport.calls
                   if call['schema'] in {m.Review, m.Disposition})
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['review', 'completed'])
async def test_old_applied_schema_reopens_through_normal_judgment_without_replay(tmp_path, monkeypatch, cut):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve original applied review', ['Do not replace a completed old call'])
    args, kwargs = committed_inputs(store, engine, task)
    application = deepcopy(store.records('knowledge_application'))
    first = install(store, engine)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', legacy_capture)
        if cut == 'review':
            async def stop_before_disposition(*args, **kwargs):
                raise RuntimeError('Synthetic stop after the saved original review')
            old.setattr(engine.bounded_judgments, '_dispose', stop_before_disposition)
            with pytest.raises(RuntimeError, match='saved original review'):
                await engine._judge_applied_knowledge(*args, **kwargs)
        else:
            completed = await engine._judge_applied_knowledge(*args, **kwargs)
    captures = deepcopy(store.records('semantic_wire_request'))
    calls = deepcopy(store.records('bounded_model_call'))
    assert all('wire_revision' not in capture for capture in captures)
    assert any(call['canonical_schema'] == 'Review' for call in captures)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        saved = await engine._judge_applied_knowledge(*args, **kwargs)
        if cut == 'completed': assert saved == completed and second.calls == []
        else:
            assert [call['schema'] for call in second.calls] == [m.Disposition]
            assert second.calls[0]['capture']['wire_revision'] == wire.APPLIED_TARGETS
        assert all(store.record_get('semantic_wire_request', row['id']) == row for row in captures)
        assert all(store.record_get('bounded_model_call', row['id']) == row for row in calls)
        assert store.records('knowledge_application') == application and executor.calls == []
        assert_wire_records(store, first.calls + second.calls)
    finally:
        await engine.close(); store.close()
