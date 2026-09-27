"""Finite cleanup recovery consumers. AI/Web review responses are explicit fixtures."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from policy_harness.engine import CycleHeld, RevisionNeeded
from policy_harness.models import Learning, OperationResult, PolicyError, SkillSelection
from policy_harness.store import digest
from tests.test_cleanup_recovery import frozen_cleanup, prepare_repair, accept_repair
from tests.test_core import runtime
from tests.test_core_recovery_closure import cycle
from tests.test_grouped_repairs import op
from tests.test_knowledge import journal, learning, operation, seed_skill
from tests.test_whole2_core_repairs import CommitAckFault
from tests.fixture_preparation import bind_cleanup_evidence


def empty_cleanup(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Finish the observed empty cleanup scope', ['Keep original evidence'])
    action = operation('finish', args={'summary': 'Empty original scope fixture'})
    journal(store, task, action, OperationResult(operation_id=action['id'], status='succeeded').model_dump(),
            learning(), status='learned')
    review = {'id': 'empty-frozen-review', 'review': {'summary': 'Fixture', 'opinions': []},
              'disposition': {'verdict': 'proceed', 'rationale': 'Controlled empty cleanup', 'opinion_responses': []}}
    draft = {'completion': {'achieved': True, 'acceptance': [], 'evidence_refs': [], 'unresolved': [], 'summary': 'Fixture'},
             'cleanup': {'decisions': [], 'rationale': 'No original Skill records'}, 'choices': [],
             'pre_assessments': [], 'pre_review': review, 'web': {'sources': []},
             'structural_evidence_complete': True, 'policy_hash': engine.policy.hash}
    row = bind_cleanup_evidence(engine, task, store.get_operation(action['id']), draft)
    return store, engine, task, row, []


async def committed_unknown(tmp_path, monkeypatch, *, empty=False, orphan=False, learning_ready=False):
    store, engine, task, row, skills = empty_cleanup(tmp_path) if empty else frozen_cleanup(tmp_path)
    if learning_ready:
        # Explicit original fixture sources, before the real cleanup intent and
        # commit: these retained procedures apply to the later actual readback.
        for skill in skills:
            skill.update(content='Read each original Skill source and projection, compare hashes, and preserve the original cleanup failure.',
                         applicability='Readback of registered Skill projections after a cleanup fault',
                         revision=skill['revision'] + 1)
            engine.knowledge._save_skill(skill)
        engine.knowledge._render_skills()
        skills = [store.record_get('skill', s['id']) for s in skills]
    if orphan:
        source = seed_skill(store, engine.knowledge, task)
        store.record('fixture_original_orphan', source['id'], source)
        with store.lock:
            store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('skill', source['id']))
    connection, get = store.db, store.record_get
    proxy = CommitAckFault(connection, task['id'] + ':' + row['operation']['id'])
    draft = deepcopy(row['finalization'])
    if learning_ready:
        draft['cleanup']['decisions'] = [{'id': s['id'], 'disposition': 'retain',
                                         'reason': 'Retain the procedure for the next actual readback'} for s in skills]
    draft.pop('post_review', None)
    draft.pop('post_assessments', None)
    store.update_operation(row['operation']['id'], finalization=draft)
    async def assess(*args, **kwargs): return []
    async def review(*args, **kwargs): return draft['pre_review']
    def readback(kind, identity):
        if proxy.commits and kind == 'cleanup_result':
            raise OSError('ACTUAL_COMMIT_READBACK_TEMPORARILY_UNAVAILABLE')
        return get(kind, identity)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'db', proxy)
        patch.setattr(store, 'record_get', readback)
        patch.setattr(engine, '_assess_targets', assess)
        patch.setattr(engine, '_review_bundle', review)
        with pytest.raises(RevisionNeeded):
            await engine._close_cycle({'task_id': task['id'], 'operation_id': row['operation']['id']})
    assert store.db is connection and proxy.commits == 1
    key = task['id'] + ':' + row['operation']['id']
    failure, result = get('cleanup_failure', key), get('cleanup_result', key)
    assert failure['effect'] == 'unknown' and failure['transaction_rolled_back'] is False
    assert failure['first_fault']['message'] == 'FIRST_FAULT_AFTER_ACTUAL_COMMIT'
    assert result is not None
    assert store.get_operation(row['operation']['id'])['status'] == 'cycle_complete'
    return store, engine, task, skills, failure, result


def accepted_repair_learning(store, engine, task, identity):
    action, candidate = prepare_repair(store, engine.knowledge, task, identity)
    result = engine.knowledge.repair_projection(task, action['id'], action['args'])
    assert result.status == 'succeeded'
    accept_repair(store, task, action, candidate, result)
    applied = engine.knowledge.apply(task, action, learning(), result.model_dump(), [])
    store.update_operation(action['id'], status='learned', knowledge_applied=applied)
    engine._confirm_reviewed_outcomes(task, store.get_operation(action['id']))
    return applied


@pytest.mark.asyncio
async def test_two_original_repairs_and_full_learning_reach_next_normal_finish(tmp_path, monkeypatch):
    store, engine, task, skills, failure, committed = await committed_unknown(tmp_path, monkeypatch)
    try:
        original_ids = {s['id'] for s in skills}
        original_failure, original_result = deepcopy(failure), deepcopy(committed)
        initial = engine.knowledge.pending_projection_failures(task)[0]
        assert set(initial['skill_ids']) == original_ids
        for skill in skills:
            pending = engine.knowledge.pending_projection_failures(task)[0]
            assert set(pending['skill_ids']) == original_ids
            args = next(a for a in pending['repair_args'] if a['skill_id'] == skill['id'])
            row = await cycle(engine, task, op('knowledge_projection_repair', **args))
            assert row['status'] == 'cycle_complete' and row['result']['status'] == 'succeeded'
        assert len(store.records('skill')) > len(skills)
        assert engine.knowledge.pending_projection_failures(task) == []
        resolution = store.record_get('projection_failure_resolution', 'cleanup_failure:' + failure['id'])
        assert resolution['complete'] and set(resolution['repaired_skill_ids']) == original_ids
        intent = store.record_get('cleanup_intent', failure['cleanup_intent_ref']['id'])
        store.record('cleanup_intent', intent['id'], dict(intent, unexpected_source_change=True))
        stale = engine.knowledge.pending_projection_failures(task)[0]
        assert not stale['original_scope']['known']
        with pytest.raises(CycleHeld, match='projection failures'):
            await engine._finalize(store.get_task(task['id']), store.get_operation(failure['operation_id']))
        store.record('cleanup_intent', intent['id'], intent)
        assert engine.knowledge.pending_projection_failures(task) == []
        result = await engine.run_task(task['id'])
        assert result['task']['status'] == 'completed', result['events'][-1]
        assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
        assert store.record_get('cleanup_failure', failure['id']) == original_failure
        assert store.record_get('cleanup_result', failure['id']) == original_result
        assert not any(s['needs_cleanup'] for s in engine.knowledge.snapshot(task['id'])['skills'])
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['empty', 'legacy', 'empty_interrupted', 'orphan'])
async def test_explicit_cleanup_readback_closes_empty_and_legacy_scope(tmp_path, monkeypatch, origin):
    if origin == 'empty_interrupted':
        store, engine, task, original, skills = empty_cleanup(tmp_path)
        intent = engine.knowledge.capture_cleanup_intent(task, original['operation'], [])
        with pytest.raises(RevisionNeeded):
            await engine._close_cycle({'task_id': task['id'], 'operation_id': original['operation']['id']})
        failure, committed = store.record_get('cleanup_failure', intent['id']), None
        assert failure['effect'] == 'unknown'
    else:
        store, engine, task, skills, failure, committed = await committed_unknown(
            tmp_path, monkeypatch, empty=origin in {'empty', 'orphan'}, orphan=origin == 'orphan')
    try:
        if origin == 'legacy':
            # Exact old shape in this owned fixture; preserve its new-format source too.
            store.record('fixture_legacy_source', failure['id'], {'failure': failure, 'result': committed})
            failure = {k: v for k, v in failure.items() if k != 'cleanup_intent_ref'}
            committed = {k: v for k, v in committed.items() if k != 'cleanup_intent_sha256'}
            store.record('cleanup_failure', failure['id'], failure)
            store.record('cleanup_result', failure['id'], committed)
        pending = engine.knowledge.pending_projection_failures(task)[0]
        assert pending['original_scope']['known']
        assert set(pending['skill_ids']) == {s['id'] for s in skills}
        args = pending['cleanup_reconcile_args']
        if origin == 'empty_interrupted':
            action = op('knowledge_projection_repair', **args)
            store.save_operation(task['id'], action.model_dump(), policy_hash=engine.policy.hash)
            state = {'task_id': task['id'], 'operation_id': action.id}
            for method in (engine._pre, engine._execute, engine._post, engine._learn): await method(state)
            get = store.record_get
            def unavailable(kind, key):
                if kind == 'cleanup_result': raise OSError('EMPTY_SCOPE_CONFIRMATION_READBACK_UNAVAILABLE')
                return get(kind, key)
            with monkeypatch.context() as patch:
                patch.setattr(store, 'record_get', unavailable)
                with pytest.raises(PolicyError, match='no longer established'): await engine._close_cycle(state)
            assert engine.knowledge.pending_projection_failures(task)
            await engine._close_cycle(state)
            row = store.get_operation(action.id)
            assert row['result']['data']['basis'] == 'empty-original-scope-and-unchanged-source'
        else:
            row = await cycle(engine, task, op('knowledge_projection_repair', **args))
            assert row['result']['data']['committed_readback']
        assert row['result']['status'] == 'succeeded'
        if origin == 'orphan':
            assert len(row['result']['data']['original_scope']['orphan_projections']) == 1
            assert any(x.get('orphan_id') and x['matches'] for x in row['result']['data']['projections'])
        assert engine.knowledge.pending_projection_failures(task) == []
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert len(store.records('skill')) > len(skills)  # Ordinary Web/post Learning ran.
        final = await engine.run_task(task['id'])
        assert final['task']['status'] == 'completed', final['events'][-1]
        assert store.record_get('cleanup_failure', failure['id']) == failure
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_new_conflict_remains_separate_when_original_scope_is_repaired(tmp_path, monkeypatch):
    store, engine, task, skills, failure, _ = await committed_unknown(tmp_path, monkeypatch)
    try:
        first = accepted_repair_learning(store, engine, task, skills[0]['id'])
        later_id = first['skills'][0]
        target = engine.knowledge.root / 'skills' / later_id / 'SKILL.md'
        target.write_bytes(b'independent later conflict must survive\n')
        action = operation('file_read', args={'path': 'observed.txt'})
        result = OperationResult(operation_id=action['id'], status='succeeded', data={'text': 'fixture actual-result boundary'})
        outcome = engine.knowledge.apply(task, action, learning(), result.model_dump(), [])
        new_failure = outcome['projection_status']['failure_id']
        accepted_repair_learning(store, engine, task, skills[1]['id'])
        pending = engine.knowledge.pending_projection_failures(task)
        assert failure['id'] not in {f['id'] for f in pending}
        assert new_failure in {f['id'] for f in pending}
        assert any(later_id in f['skill_ids'] for f in pending)
        assert target.read_bytes() == b'independent later conflict must survive\n'
        assert store.record_get('cleanup_failure', failure['id']) == failure
        with pytest.raises(CycleHeld, match='projection failures'):
            await engine._finalize(store.get_task(task['id']), store.get_operation(failure['operation_id']))
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_cleanup_reconciliation_preserves_unknown_readback_and_requires_exact_review(tmp_path, monkeypatch):
    store, engine, task, _, failure, _ = await committed_unknown(tmp_path, monkeypatch)
    try:
        args = engine.knowledge.pending_projection_failures(task)[0]['cleanup_reconcile_args']
        with pytest.raises(PolicyError):
            engine.knowledge.describe_projection_repair(task, dict(args, expected_failure_sha256='0' * 64))
        foreign = store.create_task('Another owner', ['Keep its state'])
        with pytest.raises(PolicyError): engine.knowledge.describe_projection_repair(foreign, args)
        get = store.record_get
        def missing(kind, key):
            if kind == 'cleanup_result': raise OSError('READBACK_REMAINS_UNAVAILABLE')
            return get(kind, key)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'record_get', missing)
            candidate = engine.knowledge.describe_projection_repair(task, args)
            action = operation('knowledge_projection_repair', args=args)
            journal(store, task, action, OperationResult(operation_id=action['id'], status='pending').model_dump(),
                    learning(), candidate=candidate, status='executing')
            unknown = engine.knowledge.repair_projection(task, action['id'], args)
        assert unknown.status == 'unknown' and unknown.effect == 'unknown'
        accept_repair(store, task, action, candidate, unknown)
        store.update_operation(action['id'], status='learned')
        engine._confirm_reviewed_outcomes(task, store.get_operation(action['id']))
        assert engine.knowledge.pending_projection_failures(task)
        successor = await cycle(engine, task, op('knowledge_projection_repair', **args))
        assert successor['result']['status'] == 'succeeded'
        recovered = await cycle(engine, task, op('reconcile', operation_id=action['id']))
        assert recovered['result']['status'] == 'succeeded'
        original = store.get_operation(action['id'])
        assert original['result']['effect'] == 'confirmed'
        assert original['result']['data']['original_effect'] == 'unknown'
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert engine.knowledge.pending_projection_failures(task) == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_legacy_missing_original_scope_is_explicitly_unknown(tmp_path, monkeypatch):
    store, engine, task, _, failure, committed = await committed_unknown(tmp_path, monkeypatch)
    try:
        original = {'failure': failure, 'result': committed}
        store.record('fixture_legacy_source', failure['id'], original)
        failure = {k: v for k, v in failure.items() if k != 'cleanup_intent_ref'}
        failure['projection_recovery'] = {'scope_unknown': True, 'conflicts': []}
        store.record('cleanup_failure', failure['id'], failure)
        with store.lock:
            store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('cleanup_result', failure['id']))
        later = seed_skill(store, engine.knowledge, task)
        pending = engine.knowledge.pending_projection_failures(task)[0]
        assert not pending['original_scope']['known'] and pending['skill_ids'] == []
        assert later['id'] not in pending['skill_ids']
        args = pending['cleanup_reconcile_args']
        candidate = engine.knowledge.describe_projection_repair(task, args)
        assert not candidate['established']
        assert candidate['basis'] == 'unresolved'
        assert store.record_get('cleanup_failure', failure['id']) == failure
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_interrupted_intent_is_retained_without_replaying_cleanup(tmp_path, monkeypatch):
    store, engine, task, row, skills = frozen_cleanup(tmp_path)
    try:
        intent = engine.knowledge.capture_cleanup_intent(task, row['operation'], row['finalization']['cleanup']['decisions'])
        calls = []
        def forbidden(*args): calls.append(args); raise AssertionError('CLEANUP_MUST_NOT_REPLAY')
        monkeypatch.setattr(engine.knowledge, 'finish', forbidden)
        with pytest.raises(CycleHeld): await engine._finalize(task, row)
        failure = store.record_get('cleanup_failure', intent['id'])
        assert calls == [] and failure['effect'] == 'unknown'
        assert failure['cleanup_intent_ref']['sha256'] == digest(intent)
        assert {s['id'] for s in skills} == set(engine.knowledge.pending_projection_failures(task)[0]['skill_ids'])
    finally:
        await engine.close(); store.close()


async def original_skill_learning(tmp_path, monkeypatch, mode, *, apply=True, initialized=False):
    store, engine, task, skills, failure, committed = await committed_unknown(
        tmp_path, monkeypatch, learning_ready=True)
    if initialized:
        await engine._initialize_task(store.get_task(task['id']))
    skill = store.record_get('skill', skills[0]['id'])
    args = engine.knowledge.pending_projection_failures(task)[0]['cleanup_reconcile_args']
    action = op('knowledge_projection_repair', **args)
    generate = engine.gateway.generate

    async def fixture(role, phase, payload, schema):
        value, metadata = await generate(role, phase, payload, schema)
        if payload.get('operation', {}).get('id') != action.id:
            return value, metadata
        if schema is SkillSelection and any(s['id'] == skill['id'] for s in payload['knowledge']['skills']):
            value.selected = [{'id': skill['id'], 'hash': skill['hash'],
                               'reason': 'This retained original procedure describes the same cleanup readback being executed',
                               'application': 'Compare original Skill DB sources with actual projection bytes',
                               'procedure_clause': skill['content'],
                               'procedure_sha256': hashlib.sha256(skill['content'].encode()).hexdigest()}]
            value.rejected = [s for s in value.rejected if s['id'] != skill['id']]
        if schema is Learning and phase == 'learning_proposal':
            row = store.get_operation(action.id)
            applications = []
            if mode in {'use', 'use_improve'}:
                selected = row['pre_bundle']['skills']['selected'][0]
                applications = [{'skill_id': skill['id'], 'skill_hash': selected['hash'],
                                 'procedure_clause': selected['procedure_clause'], 'procedure_sha256': selected['procedure_sha256'],
                                 'operation_id': action.id, 'operation_sha256': digest(row['operation']),
                                 'result_sha256': digest(row['result']),
                                 'evidence': [{'pointer': '/data/projections', 'sha256': digest(row['result']['data']['projections']),
                                               'explanation': 'The real readback compared the original DB Skill hashes with actual projection bytes.'}]}]
            updates = ([{'id': skill['id'], 'action': 'improve', 'expected_hash': skill['hash'],
                         'content': skill['content'] + '\nUse the separate Learning transition to retain exact pre/post versions.'}]
                       if mode in {'improve', 'use_improve'} else [])
            value = value.model_copy(update={'classifications': ['use', 'improve'] if mode == 'use_improve' else [mode],
                                             'applications': applications, 'skill_updates': updates})
        return value, metadata

    monkeypatch.setattr(engine.gateway, 'generate', fixture)
    store.save_operation(task['id'], action.model_dump(), policy_hash=engine.policy.hash)
    if initialized:
        store.update_task(task['id'], state=dict(store.get_task(task['id'])['state'], operation_id=action.id))
    state = {'task_id': task['id'], 'operation_id': action.id}
    try:
        for method in (engine._pre, engine._execute, engine._post): await method(state)
        assert store.get_operation(action.id)['status'] == 'post_reviewed'
        if apply: await engine._learn(state)
    except BaseException:
        await engine.close(); store.close()
        raise
    return store, engine, task, state, skill, failure, committed


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['use', 'improve', 'use_improve'])
async def test_original_skill_learning_closes_readback_and_next_normal_finish(tmp_path, monkeypatch, mode):
    store, engine, task, state, before, failure, committed = await original_skill_learning(tmp_path, monkeypatch, mode)
    try:
        identity = state['operation_id']
        row = store.get_operation(identity)
        saved = deepcopy(store.record_get('cleanup_reconciliation', identity))
        after = store.record_get('skill', before['id'])
        assert row['status'] == 'learned' and after['hash'] != before['hash']
        application = store.record_get('knowledge_application', task['id'] + ':' + identity)
        transition = application['outcome']['skill_transitions']
        assert transition['entries'] == [{'skill_id': before['id'],
                                         'before': {'hash': before['hash'], 'record_sha256': digest(before)},
                                         'after': {'hash': after['hash'], 'record_sha256': digest(after)}}]
        assert bool(after['uses']) == (mode in {'use', 'use_improve'})
        assert bool(row['knowledge']['observed_application_ids']) == (mode in {'use', 'use_improve'})
        if mode in {'use', 'use_improve'}:
            assert after['uses'][-1]['operation_id'] == identity
            assert after['uses'][-1]['result_sha256'] == digest(row['result'])
        # Same input is an immutable replay of the stored outcome, without a
        # second mutation or an extra readback/learning operation.
        repeated = engine.knowledge.apply(task, row['operation'], Learning.model_validate(row['post_bundle']['learning']),
                                          row['result'], [before['id']])
        assert repeated == row['knowledge'] == application['outcome']
        assert store.record_get('skill', before['id']) == after
        await engine._close_cycle(state)
        assert store.get_operation(identity)['status'] == 'cycle_complete'
        assert store.get_task(task['id'])['state'].get('operation_id') is None
        assert store.record_get('cleanup_reconciliation', identity) == saved
        assert store.get_operation(identity)['result'] == row['result']
        resolution = store.record_get('projection_failure_resolution', 'cleanup_failure:' + failure['id'])
        assert resolution['learning_transition']['transition_sha256'] == digest(transition)
        assert resolution['learning_transition']['outcome_sha256'] == digest(row['knowledge'])
        assert resolution['original_effect'] == 'unknown'
        assert engine.knowledge.pending_projection_failures(task) == []
        result = await engine.run_task(task['id'])
        assert result['task']['status'] == 'completed', result['events'][-1]
        assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
        assert [x['kind'] for x in engine.executor.calls] == ['file_write', 'file_read']
        assert len([x for x in store.operations(task['id']) if x['operation']['args'].get('mode') == 'cleanup_reconcile']) == 1
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert store.record_get('cleanup_result', failure['id']) == committed
        assert not any(s['needs_cleanup'] for s in engine.knowledge.snapshot(task['id'])['skills'])
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing_transition', 'altered_transition', 'other_operation', 'missing_version', 'altered_version', 'external_projection'])
async def test_original_skill_transition_requires_exact_same_operation_and_actual_bytes(tmp_path, monkeypatch, fault):
    store, engine, task, state, before, failure, committed = await original_skill_learning(tmp_path, monkeypatch, 'improve')
    try:
        identity = state['operation_id']; row = store.get_operation(identity)
        application_id = task['id'] + ':' + identity
        original = deepcopy(store.record_get('knowledge_application', application_id))
        saved = deepcopy(store.record_get('cleanup_reconciliation', identity))
        version_id = before['id'] + ':' + before['hash']
        source = store.record_get('skill_history', version_id)
        target = engine.knowledge.root / 'skills' / before['id'] / 'SKILL.md'
        actual_bytes = target.read_bytes()
        if fault in {'missing_transition', 'altered_transition'}:
            changed = deepcopy(original)
            if fault == 'missing_transition': changed['outcome'].pop('skill_transitions')
            else: changed['outcome']['skill_transitions']['entries'][0]['after']['record_sha256'] = '0' * 64
            store.record('knowledge_application', application_id, changed)
        elif fault == 'other_operation':
            action = operation('knowledge_read', args={'kind': 'skill', 'id': before['id']})
            current = store.record_get('skill', before['id'])
            learned = learning(classifications=['improve'], skill_updates=[{'id': before['id'], 'action': 'improve',
                                'expected_hash': current['hash'], 'content': current['content'] + '\nA separate operation changed this source.'}])
            actual = OperationResult(operation_id=action['id'], status='succeeded', effect='confirmed', data={'source': current}).model_dump()
            journal(store, task, action, actual, learned)
            engine.knowledge.apply(task, action, learned, actual, [])
        elif fault == 'missing_version':
            with store.lock: store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('skill_history', version_id))
        elif fault == 'altered_version':
            store.record('skill_history', version_id, dict(source, content='Tampered source retaining an old hash field'))
        else:
            target.write_bytes(b'Unrelated external bytes must remain visible\n')
        with pytest.raises(PolicyError): await engine._close_cycle(state)
        assert store.get_operation(identity)['status'] == 'learned'
        assert store.record_get('projection_failure_resolution', 'cleanup_failure:' + failure['id']) is None
        assert any(x['id'] == failure['id'] for x in engine.knowledge.pending_projection_failures(task))
        assert store.record_get('cleanup_reconciliation', identity) == saved
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert store.record_get('cleanup_result', failure['id']) == committed
        if fault == 'external_projection':
            assert target.read_bytes() == b'Unrelated external bytes must remain visible\n'
        # Restore only this fixture's removed/corrupted source evidence. Re-enter
        # the same close consumer; it must not replay the semantic Learning.
        if fault != 'other_operation':
            store.record('knowledge_application', application_id, original)
            store.record('skill_history', version_id, source)
            target.write_bytes(actual_bytes)
            await engine._close_cycle(state)
            assert store.get_operation(identity)['status'] == 'cycle_complete'
            assert store.get_operation(identity)['knowledge'] == row['knowledge']
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_original_skill_transition_and_semantic_application_rollback_together(tmp_path, monkeypatch):
    store, engine, task, state, before, failure, committed = await original_skill_learning(tmp_path, monkeypatch, 'use_improve', apply=False)
    try:
        identity = state['operation_id']; row = store.get_operation(identity)
        snapshot = {kind: deepcopy(store.records(kind)) for kind in ('skill', 'skill_history', 'skill_application', 'episode', 'knowledge_application')}
        record = store.record
        def interrupt(kind, key, value):
            if kind == 'knowledge_application' and key == task['id'] + ':' + identity:
                raise OSError('FIRST_FAULT_BEFORE_LEARNING_APPLICATION_COMMIT')
            return record(kind, key, value)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'record', interrupt)
            with pytest.raises(OSError, match='FIRST_FAULT_BEFORE_LEARNING_APPLICATION_COMMIT'):
                await engine._learn(state)
        assert {kind: store.records(kind) for kind in snapshot} == snapshot
        assert store.get_operation(identity)['status'] == 'post_reviewed'
        assert store.record_get('skill', before['id']) == before
        await engine._learn(state)
        assert store.get_operation(identity)['status'] == 'learned'
        await engine._close_cycle(state)
        assert store.get_operation(identity)['status'] == 'cycle_complete'
        assert len(store.record_get('skill', before['id'])['uses']) == 1
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert store.record_get('cleanup_result', failure['id']) == committed
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_original_skill_learned_pointer_resumes_without_replaying_learning(tmp_path, monkeypatch):
    store, engine, task, state, before, failure, committed = await original_skill_learning(
        tmp_path, monkeypatch, 'use_improve', initialized=True)
    try:
        identity = state['operation_id']
        row = deepcopy(store.get_operation(identity))
        saved = deepcopy(store.record_get('cleanup_reconciliation', identity))
        application = deepcopy(store.record_get('knowledge_application', task['id'] + ':' + identity))
        assert row['status'] == 'learned' and store.get_task(task['id'])['state']['operation_id'] == identity
        apply = engine.knowledge.apply
        def no_replay(task_value, action, learned, result, selected):
            assert action['id'] != identity, 'A learned operation must resume at confirmation without replaying Learning'
            return apply(task_value, action, learned, result, selected)
        monkeypatch.setattr(engine.knowledge, 'apply', no_replay)
        store.update_task(task['id'], status='stopped')
        result = await engine.run_task(task['id'])
        assert result['task']['status'] == 'completed', result['events'][-1]
        actual = store.get_operation(identity)
        assert actual['status'] == 'cycle_complete' and actual['knowledge'] == row['knowledge']
        assert store.record_get('knowledge_application', task['id'] + ':' + identity) == application
        assert store.record_get('cleanup_reconciliation', identity) == saved
        assert [x['kind'] for x in engine.executor.calls] == ['file_write', 'file_read']
        assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
        assert engine.knowledge.pending_projection_failures(task) == []
        assert store.record_get('cleanup_failure', failure['id']) == failure
        assert store.record_get('cleanup_result', failure['id']) == committed
    finally:
        await engine.close(); store.close()
