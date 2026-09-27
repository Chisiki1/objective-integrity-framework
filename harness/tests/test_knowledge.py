"""Focused knowledge consumer checks; fixture reviews are not live model evidence."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import Learning, Operation, OperationResult, PolicyError
from policy_harness.store import Store, digest


def learning(**changes):
    fields = dict(outcome_summary='Observed file operation', classifications=['create'],
                  skill_updates=[], recurrence='unknown', next_use_trigger='Next matching file task',
                  ideas=[], applications=[])
    fields.update(changes)
    return Learning(**fields)


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'data')
    knowledge = Knowledge(store)
    task = store.create_task('Write and verify the requested file', ['The file has the exact requested bytes'])
    yield store, knowledge, task
    store.close()


def operation(kind='file_write', **changes):
    fields = dict(kind=kind, args={'path': 'answer.txt', 'text': 'hello', 'expected_sha256': None},
                  purpose='Produce the requested file', expected_result='Exact file bytes',
                  decisions=[{'id': 'file-choice', 'statement': 'Use answer.txt', 'rationale': 'Requested output'}])
    fields.update(changes)
    return Operation(**fields).model_dump()


def journal(store, task, op, result, learned, selected=None, candidate=None, status='post_reviewed'):
    before = {'operation': op, 'skills': {'selected': selected or [], 'rejected': []}, 'candidate': candidate}
    after = {'operation': op, 'result': result, 'learning': learned.model_dump()}
    review = lambda b: {'bundle_hash': digest(b), 'disposition': {'verdict': 'proceed'}}
    store.save_operation(task['id'], op)
    store.update_operation(op['id'], status=status, result=result,
                         pre_bundle=before, pre_review=review(before),
                         post_bundle=after, post_review=review(after),
                         started_at=result['started_at'])
    return op


def seed_skill(store, knowledge, task):
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    learned = learning(skill_updates=[{
        'action': 'create', 'title': 'Write exact content',
        'content': 'Write the exact requested text and record the resulting file hash.',
        'applicability': 'Writing a requested plain text file', 'next_trigger': 'Next exact file write'}])
    outcome = knowledge.apply(task, op, learned, result, [])
    return store.record_get('skill', outcome['skills'][0])


def observed_application(store, task, skill, op=None, result=None, updates=None):
    op = op or operation()
    if result is None:
        executor = Executor(store.data_dir)
        result = asyncio.run(executor.execute(Path(task['workspace']), Operation.model_validate(op))).model_dump()
    clause = skill['content']
    clause_hash = hashlib.sha256(clause.encode()).hexdigest()
    selected = [{'id': skill['id'], 'hash': skill['hash'], 'application': 'Apply the exact write procedure',
                 'procedure_clause': clause, 'procedure_sha256': clause_hash}]
    application = dict(skill_id=skill['id'], skill_hash=skill['hash'], procedure_clause=clause,
                       procedure_sha256=clause_hash, operation_id=op['id'],
                       operation_sha256=digest(op), result_sha256=digest(result),
                       evidence=[{'pointer': '/data/sha256', 'sha256': digest(result['data']['sha256']),
                                  'explanation': 'The actual written file hash records the requested bytes.'}])
    learned = learning(classifications=['use'], applications=[application], skill_updates=updates or [])
    return op, result, learned, selected


def integration_operation(store, knowledge, parent, candidate_id):
    description = knowledge.describe_candidate(parent, candidate_id)
    op = operation('knowledge_integrate', args={'candidate_id': candidate_id, 'expected_hash': description['candidate_hash']})
    result = OperationResult(operation_id=op['id'], status='pending').model_dump()
    journal(store, parent, op, result, learning(), candidate=description, status='executing')
    return op, description


def test_selection_does_not_change_skill_use_or_version(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    outcome = knowledge.apply(task, op, learning(), result, [skill['id']])
    unchanged = store.record_get('skill', skill['id'])
    assert unchanged['uses'] == []
    assert unchanged['hash'] == skill['hash']
    assert outcome['observed_application_ids'] == []
    assert len(outcome['skills']) == 1  # New knowledge is still explicitly classified.


def test_actual_file_application_is_bound_and_replay_is_idempotent(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    op, result, learned, selected = observed_application(store, task, skill)
    assert result['status'] == 'succeeded'
    assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
    journal(store, task, op, result, learned, selected)
    first = knowledge.apply(task, op, learned, result, [skill['id']])
    second = knowledge.apply(task, op, learned, result, [skill['id']])
    assert first == second
    actual = store.record_get('skill', skill['id'])
    assert len(actual['uses']) == 1
    assert actual['uses'][0]['skill_hash'] == skill['hash']
    assert actual['uses'][0]['operation_id'] == op['id']
    assert actual['uses'][0]['result_sha256'] == digest(result)
    assert knowledge._skill_version(skill['id'], skill['hash']) == skill
    assert len(store.records('skill_application')) == 1


@pytest.mark.parametrize('bad', ['result_hash', 'operation_hash', 'clause', 'pointer_hash', 'unselected_clause', 'unreviewed'])
def test_forged_application_has_no_partial_semantic_effect(runtime, bad):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    op, result, learned, selected = observed_application(store, task, skill)
    raw = learned.model_dump()
    app = raw['applications'][0]
    if bad == 'result_hash':
        app['result_sha256'] = '0' * 64
    elif bad == 'operation_hash':
        app['operation_sha256'] = '0' * 64
    elif bad == 'clause':
        app['procedure_clause'] = 'A different procedure'
        app['procedure_sha256'] = hashlib.sha256(app['procedure_clause'].encode()).hexdigest()
    elif bad == 'pointer_hash':
        app['evidence'][0]['sha256'] = '0' * 64
    elif bad == 'unselected_clause':
        selected[0]['procedure_clause'] = 'Some other selection'
    learned = Learning.model_validate(raw)
    journal(store, task, op, result, learned, selected)
    if bad == 'unreviewed':
        store.update_operation(op['id'], post_review={'bundle_hash': '0' * 64, 'disposition': {'verdict': 'proceed'}})
    before = len(store.records('episode'))
    with pytest.raises(PolicyError):
        knowledge.apply(task, op, learned, result, [skill['id']])
    assert len(store.records('episode')) == before
    assert store.record_get('skill', skill['id']) == skill
    assert store.records('skill_application') == []


def test_use_declaration_without_observation_is_rejected(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    with pytest.raises(PolicyError, match=r'skill_updates\[0\]\.use requires its exact evidenced application; selection is not use'):
        knowledge.apply(task, op, learning(classifications=['use'],
                        skill_updates=[{'action': 'use', 'id': skill['id']}]), result, [skill['id']])
    assert len(store.records('episode')) == 1
    assert store.record_get('skill', skill['id']) == skill


def test_application_and_improvement_share_original_cas_without_false_staleness(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    op, result, learned, selected = observed_application(store, task, skill, updates=[{
        'action': 'improve', 'id': skill['id'], 'expected_hash': skill['hash'],
        'content': skill['content'] + ' Preserve the original failure if writing fails.'}])
    journal(store, task, op, result, learned, selected)
    knowledge.apply(task, op, learned, result, [skill['id']])
    actual = store.record_get('skill', skill['id'])
    assert len(actual['uses']) == 1
    assert 'original failure' in actual['content']
    assert knowledge._skill_version(skill['id'], skill['hash']) == skill


def test_late_invalid_learning_rolls_back_all_writes(runtime):
    store, knowledge, task = runtime
    op = operation()
    result = OperationResult(operation_id=op['id'], status='failed').model_dump()
    create = {'action': 'create', 'title': 'Partial attempt', 'content': 'Observed failure',
              'applicability': 'Failure diagnosis', 'next_trigger': 'Next similar operation'}
    invalid = learning(skill_updates=[create, {'action': 'unclassified'}])
    for _ in range(2):
        with pytest.raises(PolicyError):
            knowledge.apply(task, op, invalid, result, [])
    assert store.records('episode') == []
    assert store.records('skill') == []
    assert store.records('knowledge_application') == []


def test_projection_failure_is_not_semantic_replay(runtime, monkeypatch):
    store, knowledge, task = runtime
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    render = knowledge.rebuild_projections
    def fail():
        raise OSError('fixture projection failure after transaction commit')
    monkeypatch.setattr(knowledge, 'rebuild_projections', fail)
    outcome = knowledge.apply(task, op, learning(), result, [])
    assert outcome['projection_status']['state'] == 'pending'
    assert outcome['projection_status']['first_fault']['message'] == 'fixture projection failure after transaction commit'
    assert len(store.records('episode')) == 1
    assert len(store.records('skill')) == 1
    monkeypatch.setattr(knowledge, 'rebuild_projections', render)
    assert knowledge.apply(task, op, learning(), result, []) == outcome
    assert len(store.records('episode')) == 1
    assert len(store.records('skill')) == 1
    assert not (knowledge.root / 'skills' / outcome['skills'][0] / 'SKILL.md').exists()
    assert knowledge.pending_projection_failures(task)
    # Readback repair is separate from the immutable prior learning result.
    assert knowledge.rebuild_projections()['repaired'] is True
    assert knowledge.apply(task, op, learning(), result, []) == outcome
    assert knowledge.pending_projection_failures(task)


def test_child_use_is_local_until_exact_parent_integration(runtime):
    store, knowledge, parent = runtime
    shared = seed_skill(store, knowledge, parent)
    before_global = deepcopy(store.records('global_family'))
    child = store.create_task('Bounded file subtask', ['Exact child output'], parent['id'])
    op, result, learned, selected = observed_application(store, child, shared)
    journal(store, child, op, result, learned, selected)
    applied = knowledge.apply(child, op, learned, result, [shared['id']])
    assert store.record_get('skill', shared['id']) == shared
    assert store.records('global_family') == before_global
    assert len(knowledge.snapshot(child['id'])['applications']) == 1
    candidate_id = applied['candidate_ids'][0]
    outsider = store.create_task('Other root task', ['Unrelated'])
    with pytest.raises(PolicyError, match='owning primary'):
        knowledge.describe_candidate(outsider, candidate_id)
    with pytest.raises(PolicyError, match='owning primary'):
        knowledge.describe_candidate(child, candidate_id)
    integration, described = integration_operation(store, knowledge, parent, candidate_id)
    integrated = knowledge.integrate_candidate(parent, integration['id'], candidate_id, described['candidate_hash'])
    assert knowledge.integrate_candidate(parent, integration['id'], candidate_id, described['candidate_hash']) == integrated
    actual = store.record_get('skill', shared['id'])
    assert len(actual['uses']) == 1
    assert actual['uses'][0]['task_id'] == child['id']
    assert actual['uses'][0]['operation_id'] == op['id']
    assert integrated['parent_application_not_inferred'] is True
    assert knowledge.describe_candidate(parent, candidate_id)['status'] == 'integrated'
    assert knowledge.snapshot(child['id'])['skills'] == []


def test_child_shared_change_is_candidate_and_stale_cas_is_atomic(runtime):
    store, knowledge, parent = runtime
    shared = seed_skill(store, knowledge, parent)
    child = store.create_task('Improve a reusable procedure', ['Reviewed improvement candidate'], parent['id'])
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    proposed = learning(classifications=['improve'], skill_updates=[{
        'action': 'improve', 'id': shared['id'], 'expected_hash': shared['hash'],
        'content': shared['content'] + ' Keep exact source provenance.'}])
    produced = knowledge.apply(child, op, proposed, result, [])
    assert store.record_get('skill', shared['id']) == shared
    candidate_id = produced['candidate_ids'][0]
    # A real parent mutation makes the reviewed child's original CAS stale.
    changed = deepcopy(shared)
    changed['content'] += ' Concurrent reviewed change.'
    changed['revision'] += 1
    store._transaction(lambda: knowledge._save_skill(changed))
    integration, description = integration_operation(store, knowledge, parent, candidate_id)
    with pytest.raises(PolicyError, match='changed after update'):
        knowledge.integrate_candidate(parent, integration['id'], candidate_id, description['candidate_hash'])
    assert knowledge.describe_candidate(parent, candidate_id)['status'] == 'pending'
    assert store.records('knowledge_integration') == []
    assert 'Concurrent reviewed change' in store.record_get('skill', shared['id'])['content']


def test_child_new_skill_visibility_and_parent_import_preserve_source(runtime):
    store, knowledge, parent = runtime
    child = store.create_task('Independent knowledge work', ['Source-linked result'], parent['id'])
    sibling = store.create_task('Different scope', ['Other result'], parent['id'])
    local = seed_skill(store, knowledge, child)
    assert local['visibility'] == 'task'
    assert local['id'] not in {s['id'] for s in knowledge.retrieve(parent)['skills']}
    assert local['id'] not in {s['id'] for s in knowledge.retrieve(sibling)['skills']}
    candidate_id = knowledge.snapshot(child['id'])['candidates'][0]['candidate_id']
    op, description = integration_operation(store, knowledge, parent, candidate_id)
    outcome = knowledge.integrate_candidate(parent, op['id'], candidate_id, description['candidate_hash'])
    imported = store.record_get('skill', outcome['skills'][0])
    assert imported['id'] != local['id']
    assert imported['owner_task_id'] == parent['id']
    assert imported['visibility'] == 'shared'
    assert imported['sources'] == local['sources']
    assert imported['imported_from']['skill_hash'] == local['hash']
    assert imported['uses'] == []
    assert store.record_get('skill', local['id']) == local


def seed_idea(store, knowledge, task):
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    learned = learning(ideas=[{'id': 'input-idea', 'target': 'workflow', 'proposal': 'Verify the written hash',
                              'disposition': 'adopt', 'rationale': 'Detect content mismatch'}])
    knowledge.apply(task, op, learned, result, [])
    return store.records('idea')[0]


def improvement_operation(store, task, idea, candidate, role, start, *, binding=True, observed_hash=None, expected_status='succeeded'):
    bindings = [{'idea_id': idea['id'], 'candidate_sha256': candidate, 'role': role,
                 'source_pointer': '/data/sha256', 'source_sha256': candidate,
                 'result_pointer': '/stdout', 'claim': 'Exact candidate checked by the fixture oracle',
                 'expected_status': expected_status}] if binding else []
    op = operation('file_write' if role == 'implementation' else 'exec',
                   improvement_bindings=bindings)
    result = OperationResult(operation_id=op['id'], status=expected_status, effect='confirmed',
                             stdout='Observed exact candidate output',
                             data={'sha256': observed_hash or candidate},
                             started_at=start.isoformat(), finished_at=(start + timedelta(seconds=1)).isoformat()).model_dump()
    journal(store, task, op, result, learning())
    return op['id']


def resolution(idea, candidate, implementation, verification, use):
    return [{'id': idea['id'], 'disposition': 'verified', 'reason': 'Bound candidate implemented, checked and used',
             'candidate_sha256': candidate, 'implementation_operation_ids': [implementation],
             'verification_operation_ids': [verification], 'actual_use_operation_ids': [use]}]


@pytest.mark.parametrize('bad', ['before_idea', 'missing_binding', 'wrong_candidate', 'before_implementation'])
def test_unrelated_old_or_misbound_success_cannot_verify_improvement(runtime, bad):
    store, knowledge, task = runtime
    idea = seed_idea(store, knowledge, task)
    candidate = hashlib.sha256(b'improved procedure').hexdigest()
    baseline = datetime.fromisoformat(idea['created_at']) + timedelta(seconds=2)
    impl = improvement_operation(store, task, idea, candidate, 'implementation', baseline)
    verify_start = baseline + timedelta(seconds=2)
    if bad == 'before_idea':
        verify_start = baseline - timedelta(seconds=10)
    if bad == 'before_implementation':
        verify_start = baseline
    verify = improvement_operation(store, task, idea, candidate, 'verification', verify_start,
                                   binding=bad != 'missing_binding',
                                   observed_hash='0' * 64 if bad == 'wrong_candidate' else None)
    use = improvement_operation(store, task, idea, candidate, 'use', baseline + timedelta(seconds=4))
    with pytest.raises(PolicyError):
        knowledge.resolve_ideas(task['id'], resolution(idea, candidate, impl, verify, use), 'resolution')
    assert store.record_get('idea', idea['id'])['status'] == 'pending'
    assert store.records('idea_resolution') == []


def test_bound_candidate_roles_and_negative_verification_are_preserved(runtime):
    store, knowledge, task = runtime
    idea = seed_idea(store, knowledge, task)
    candidate = hashlib.sha256(b'improved procedure').hexdigest()
    baseline = datetime.fromisoformat(idea['created_at']) + timedelta(seconds=2)
    impl = improvement_operation(store, task, idea, candidate, 'implementation', baseline)
    verify = improvement_operation(store, task, idea, candidate, 'verification', baseline + timedelta(seconds=2),
                                   expected_status='failed')
    use = improvement_operation(store, task, idea, candidate, 'use', baseline + timedelta(seconds=4))
    decisions = resolution(idea, candidate, impl, verify, use)
    first = knowledge.resolve_ideas(task['id'], decisions, 'resolution')
    assert knowledge.resolve_ideas(task['id'], decisions, 'resolution') == first
    actual = store.record_get('idea', idea['id'])
    assert actual['status'] == 'verified'
    assert actual['candidate_sha256'] == candidate
    assert actual['evidence']['verification'][0]['result_sha256']
    assert actual['semantic_effect'] == 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'
    assert len(store.records('idea_resolution')) == 1

def test_legacy_committed_learning_reuses_source_without_reapplying(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    episode = store.records('episode')[0]
    key = task['id'] + ':' + episode['operation_id']
    current = store.record_get('knowledge_application', key)
    legacy = digest({'learning': episode['learning'], 'result': episode['result'],
                     'selected': episode['selected_skills']})
    store.record('knowledge_application', key, dict(current, input_hash=legacy))
    outcome = knowledge.apply(task, episode['operation'], Learning.model_validate(episode['learning']),
                              episode['result'], episode['selected_skills'])
    assert outcome == current['outcome']
    assert len(store.records('episode')) == 1
    assert store.record_get('skill', skill['id']) == skill
    assert len(store.records('knowledge_application_history')) == 1


def test_legacy_skill_origin_survives_later_actor_sources(runtime):
    store, knowledge, task = runtime
    skill = seed_skill(store, knowledge, task)
    other = store.create_task('Later related work', ['Reuse existing knowledge'])
    other_skill = seed_skill(store, knowledge, other)
    legacy = deepcopy(skill)
    legacy.pop('owner_task_id')
    legacy.pop('visibility')
    legacy['sources'] += other_skill['sources']
    legacy['hash'] = digest({k: v for k, v in legacy.items() if k != 'hash'})
    store.record('skill', legacy['id'], legacy)
    assert knowledge._owner(legacy) == task['id']
    store._transaction(lambda: knowledge._save_skill(legacy))
    saved = store.record_get('skill', legacy['id'])
    assert saved['owner_task_id'] == task['id']
    assert saved['visibility'] == 'shared'
    assert saved['sources'] == legacy['sources']
    assert knowledge._skill_version(legacy['id'], legacy['hash']) == legacy


def deferred_idea(store, knowledge, task, proposal):
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    knowledge.apply(task, op, learning(ideas=[{'id': 'carryover', 'target': 'task', 'proposal': proposal,
                                             'disposition': 'adopt', 'rationale': 'Useful related follow-up'}]), result, [])
    cleanup = [{'id': s['id'], 'disposition': 'retain', 'reason': 'Keep source and next-use condition'}
               for s in knowledge.snapshot(task['id'])['skills'] if s['needs_cleanup']]
    knowledge.finish(task['id'], cleanup, main_achieved=True)
    return next(i for i in store.records('idea') if i['task_id'] == task['id'])


def planning_operation(store, knowledge, task, *, transform=None):
    index = knowledge.deferred_for_planning(task)
    decisions = [{'idea_id': c['idea_id'], 'source_hash': c['source_hash'],
                  'disposition': 'include' if number == 0 else 'exclude',
                  'reason': 'Related objective' if number == 0 else 'Different task outcome',
                  'planned_application': 'Apply to the next requested output' if number == 0 else ''}
                 for number, c in enumerate(index['candidates'])]
    if transform:
        transform(decisions)
    op = operation('plan_task', args={'plan': {'deferred_index_hash': index['index_hash'],
                                             'deferred_considerations': decisions}})
    result = OperationResult(operation_id=op['id'], status='pending').model_dump()
    journal(store, task, op, result, learning(), status='executing')
    return op, index, decisions


def test_next_task_accounts_for_all_visible_carryovers_without_changing_origin(runtime):
    store, knowledge, original = runtime
    first = deferred_idea(store, knowledge, original, 'Reuse source verification in the next report')
    unrelated = store.create_task('Completely different vocabulary', ['Unrelated output'])
    second = deferred_idea(store, knowledge, unrelated, 'A different improvement to consider explicitly')
    later = store.create_task('Create another report', ['Exact requested report'])
    original_before, unrelated_before = store.get_task(original['id']), store.get_task(unrelated['id'])
    op, index, decisions = planning_operation(store, knowledge, later)
    assert {c['idea_id'] for c in index['candidates']} == {first['id'], second['id']}
    assert knowledge.retrieve(later)['deferred_planning'] == index
    outcome = knowledge.record_deferred_selection(later, op['id'], decisions, index['index_hash'])
    assert knowledge.record_deferred_selection(later, op['id'], decisions, index['index_hash']) == outcome
    assert len(outcome['included_idea_ids']) == 1
    assert outcome['actual_use_not_inferred'] is True
    assert store.record_get('idea', first['id']) == first
    assert store.record_get('idea', second['id']) == second
    assert store.get_task(original['id']) == original_before
    assert store.get_task(unrelated['id']) == unrelated_before
    assert len(store.records('deferred_consideration')) == 1


@pytest.mark.parametrize('bad', ['omitted', 'empty_reason', 'empty_application', 'changed_source', 'stale_index', 'unreviewed'])
def test_deferred_selection_rejects_missing_or_stale_sources_atomically(runtime, bad):
    store, knowledge, original = runtime
    idea = deferred_idea(store, knowledge, original, 'Carry exact source context')
    later = store.create_task('Next related work', ['Source-bound result'])
    def tamper(decisions):
        if bad == 'omitted': decisions.clear()
        elif bad == 'empty_reason': decisions[0]['reason'] = ''
        elif bad == 'empty_application': decisions[0]['planned_application'] = ''
        elif bad == 'changed_source': decisions[0]['source_hash'] = '0' * 64
    op, index, decisions = planning_operation(store, knowledge, later, transform=tamper)
    if bad == 'stale_index':
        updated = dict(idea, next_trigger='A newly clarified next-use condition')
        store.record('idea', idea['id'], updated)
    elif bad == 'unreviewed':
        store.update_operation(op['id'], pre_review={})
    with pytest.raises(PolicyError):
        knowledge.record_deferred_selection(later, op['id'], decisions, index['index_hash'])
    assert store.records('deferred_consideration') == []
    assert store.record_get('idea', idea['id'])['task_id'] == original['id']


def test_full_episode_read_is_source_bound_and_explicit_private_is_enforced(runtime):
    store, knowledge, original = runtime
    skill = seed_skill(store, knowledge, original)
    episode_id = skill['sources'][0]
    episode = store.record_get('episode', episode_id)
    later = store.create_task('Related knowledge read', ['Original experience'])
    read = knowledge.describe_episode(later, episode_id, digest(episode))
    assert read['episode'] == episode
    assert read['operation_sha256'] == digest(episode['operation'])
    assert read['result_sha256'] == digest(episode['result'])
    with pytest.raises(PolicyError, match='changed'):
        knowledge.describe_episode(later, episode_id, '0' * 64)
    private = dict(episode, visibility='private')
    store.record('episode', episode_id, private)
    assert knowledge.describe_episode(original, episode_id)['episode'] == private
    with pytest.raises(PolicyError, match='visible source'):
        knowledge.describe_episode(later, episode_id)
    assert not any(episode_id in f['episode_refs'] for f in knowledge.retrieve(later)['global'])


def test_child_episode_requires_exact_parent_integration_before_shared_read(runtime):
    store, knowledge, parent = runtime
    child = store.create_task('Produce child knowledge', ['Parent-reviewed source'], parent['id'])
    sibling = store.create_task('Read relevant sources', ['Permitted original experience'], parent['id'])
    local = seed_skill(store, knowledge, child)
    episode_id = local['sources'][0]
    with pytest.raises(PolicyError, match='visible source'):
        knowledge.describe_episode(sibling, episode_id)
    candidate_id = knowledge.snapshot(child['id'])['candidates'][0]['candidate_id']
    op, described = integration_operation(store, knowledge, parent, candidate_id)
    assert described['source_episode'] == store.record_get('episode', episode_id)
    knowledge.integrate_candidate(parent, op['id'], candidate_id, described['candidate_hash'])
    assert knowledge.describe_episode(sibling, episode_id)['episode_id'] == episode_id
    grant = store.record_get('episode_access', episode_id)
    store.record('episode_access', episode_id, dict(grant, operation_id='guessed-unrelated-operation'))
    with pytest.raises(PolicyError, match='visible source'):
        knowledge.describe_episode(sibling, episode_id)


def candidate_disposition_operation(store, knowledge, parent, candidate_id, disposition, **changes):
    candidate = knowledge.describe_candidate(parent, candidate_id)
    args = {'candidate_id': candidate_id, 'expected_hash': candidate['candidate_hash'],
            'expected_state_hash': candidate['state_hash'], 'disposition': disposition,
            'reason': 'The parent considered the complete candidate and current targets', **changes}
    described = knowledge.describe_candidate_disposition(parent, args)
    op = operation('candidate_disposition', args=args)
    result = OperationResult(operation_id=op['id'], status='pending').model_dump()
    journal(store, parent, op, result, learning(), candidate=described, status='executing')
    return op, described


def test_parent_candidate_defer_and_reject_keep_full_history_and_cas(runtime):
    store, knowledge, parent = runtime
    child = store.create_task('Candidate contribution', ['Source-backed proposal'], parent['id'])
    local = seed_skill(store, knowledge, child)
    candidate_id = knowledge.snapshot(child['id'])['candidates'][0]['candidate_id']
    original = store.record_get('knowledge_candidate', candidate_id)
    op, _ = candidate_disposition_operation(store, knowledge, parent, candidate_id, 'defer',
                                           next_trigger='Next related file operation')
    first = knowledge.dispose_candidate(parent, op['id'], op['args'])
    assert knowledge.dispose_candidate(parent, op['id'], op['args']) == first
    assert first['status'] == 'deferred' and not first['shared_change_applied']
    with pytest.raises(PolicyError, match='disposition changed'):
        knowledge.describe_candidate_disposition(parent, op['args'])
    reject, _ = candidate_disposition_operation(store, knowledge, parent, candidate_id, 'reject')
    outcome = knowledge.dispose_candidate(parent, reject['id'], reject['args'])
    assert outcome['status'] == 'rejected'
    assert original in store.records('knowledge_candidate_history')
    assert len(store.records('knowledge_candidate_history')) == 2
    assert store.record_get('skill', local['id']) == local
    assert len(store.records('skill')) == 1


def stale_shared_candidate(store, knowledge, parent):
    shared = seed_skill(store, knowledge, parent)
    child = store.create_task('Improve shared procedure', ['Exact proposal'], parent['id'])
    op = operation()
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    produced = knowledge.apply(child, op, learning(classifications=['improve'], skill_updates=[{
        'action': 'improve', 'id': shared['id'], 'expected_hash': shared['hash'],
        'content': shared['content'] + ' Preserve child provenance.'}]), result, [])
    changed = dict(shared, content=shared['content'] + ' Retain the intervening parent change.', revision=shared['revision'] + 1)
    store._transaction(lambda: knowledge._save_skill(changed))
    return produced['candidate_ids'][0], store.record_get('skill', shared['id'])


def test_parent_rebase_reviews_complete_current_bytes_then_integrates_new_candidate(runtime):
    store, knowledge, parent = runtime
    candidate_id, current = stale_shared_candidate(store, knowledge, parent)
    original = knowledge.describe_candidate(parent, candidate_id)
    replacement = deepcopy(original['payload'])
    replacement['shared_updates'][0].update(expected_hash=current['hash'],
                                             content=current['content'] + ' Preserve child provenance.')
    op, described = candidate_disposition_operation(store, knowledge, parent, candidate_id, 'rebase',
                                                    replacement_payload=replacement)
    assert described['candidate']['current_targets'][current['id']] == current
    outcome = knowledge.dispose_candidate(parent, op['id'], op['args'])
    next_id = outcome['replacement_candidate_id']
    assert next_id != candidate_id
    assert knowledge.describe_candidate(parent, candidate_id)['status'] == 'rebased'
    assert store.record_get('skill', current['id']) == current  # Rebase itself changes no Skill.
    imported, next_description = integration_operation(store, knowledge, parent, next_id)
    knowledge.integrate_candidate(parent, imported['id'], next_id, next_description['candidate_hash'])
    actual = store.record_get('skill', current['id'])
    assert 'intervening parent change' in actual['content'] and 'child provenance' in actual['content']
    assert next_description['payload']['source_episode_sha256'] == original['payload']['source_episode_sha256']
    assert next_description['payload']['rebase']['prior_candidate_hash'] == original['candidate_hash']


@pytest.mark.parametrize('bad', ['source', 'drop_target', 'stale_target', 'unreviewed'])
def test_rebase_rejects_source_rewrite_or_unreviewed_current_cut(runtime, bad):
    store, knowledge, parent = runtime
    candidate_id, current = stale_shared_candidate(store, knowledge, parent)
    original = knowledge.describe_candidate(parent, candidate_id)
    replacement = deepcopy(original['payload'])
    replacement['shared_updates'][0]['expected_hash'] = current['hash']
    if bad == 'source': replacement['source_episode_sha256'] = '0' * 64
    elif bad == 'drop_target': replacement['shared_updates'] = []
    elif bad == 'stale_target': replacement['shared_updates'][0]['expected_hash'] = '0' * 64
    with pytest.raises(PolicyError):
        op, _ = candidate_disposition_operation(store, knowledge, parent, candidate_id, 'rebase', replacement_payload=replacement)
        store.update_operation(op['id'], pre_bundle={})
        knowledge.dispose_candidate(parent, op['id'], op['args'])
    assert knowledge.describe_candidate(parent, candidate_id) == original
    assert store.record_get('skill', current['id']) == current
    assert store.records('knowledge_candidate_disposition') == []


def test_projection_repair_restores_outer_rollback_and_removes_only_managed_orphan(runtime):
    store, knowledge, task = runtime
    original = seed_skill(store, knowledge, task)
    target = knowledge.root / 'skills' / original['id'] / 'SKILL.md'
    original_bytes = target.read_bytes()
    observed = {}
    def fail_outer_transaction():
        changed = dict(original, content='Rolled-back content', revision=original['revision'] + 1)
        knowledge._save_skill(changed)
        created = seed_skill(store, knowledge, task)
        observed['id'] = created['id']
        assert b'Rolled-back content' in target.read_bytes()
        raise RuntimeError('fixture failure after nested transaction and projections')
    with pytest.raises(RuntimeError, match='nested transaction'):
        store._transaction(fail_outer_transaction)
    assert store.record_get('skill', original['id']) == original
    assert store.record_get('skill', observed['id']) is None
    orphan = knowledge.root / 'skills' / observed['id'] / 'SKILL.md'
    assert orphan.is_file()
    unrelated = orphan.parent / 'keep.txt'
    unrelated.write_text('Unowned adjacent content', encoding='utf-8')
    repaired = knowledge.rebuild_projections()
    assert repaired['repaired'] is True
    assert repaired['removed_orphan_ids'] == [observed['id']]
    assert target.read_bytes() == original_bytes
    assert not orphan.exists() and unrelated.read_text('utf-8') == 'Unowned adjacent content'
    assert repaired['semantic_writes_replayed'] is False


def test_projection_repair_preserves_changed_or_unmanaged_orphans(runtime):
    store, knowledge, task = runtime
    observed = {}
    def fail_outer_transaction():
        observed['id'] = seed_skill(store, knowledge, task)['id']
        raise RuntimeError('rollback fixture')
    with pytest.raises(RuntimeError):
        store._transaction(fail_outer_transaction)
    target = knowledge.root / 'skills' / observed['id'] / 'SKILL.md'
    target.write_text('Unknown new content', encoding='utf-8')
    unknown = knowledge.root / 'skills' / 'unmanaged' / 'SKILL.md'
    unknown.parent.mkdir()
    unknown.write_text('Unowned complete file', encoding='utf-8')
    result = knowledge.rebuild_projections()
    assert result['repaired'] is False
    assert set(result['conflicts']) == {observed['id'] + ':changed-orphan', 'unmanaged:unmanaged-orphan'}
    assert target.read_text('utf-8') == 'Unknown new content'
    assert unknown.read_text('utf-8') == 'Unowned complete file'
    assert store.records('skill') == []


def controller_evidence_fixture(store, knowledge, task, *, bad=None, use_kind='file_read', use_effect='confirmed'):
    """Trusted record service is a fixture; real process attestation is tested by Updates."""
    idea = seed_idea(store, knowledge, task)
    old_source, new_source = hashlib.sha256(b'old source').hexdigest(), hashlib.sha256(b'new source').hexdigest()
    description = {'schema': 'controller-update-v1', 'workspace': task['workspace'], 'policy_hash': task['policy_hash'],
                   'rationale': 'Improve the normal controller operation', 'before_version': old_source,
                   'after_version': new_source, 'changes': [{'path': 'src/policy_harness/proposal.py',
                                                           'source': 'proposal.py', 'old': {'absent': True},
                                                           'new': {'absent': False, 'base64': 'eD0xCg=='}}]}
    candidate = digest(description)
    description['candidate_sha256'] = candidate
    start = datetime.fromisoformat(idea['created_at']) + timedelta(seconds=2)
    records = {}
    binding = {'activation_candidate_id': candidate, 'candidate_sha256': candidate,
               'expected_source_version': new_source, 'activation_process_id': 'old-process',
               'activation_pid': 101, 'activation_sha256': hashlib.sha256(b'activation').hexdigest(),
               'activation_status': 'activated'}

    def observed(kind, role, offset, *, runtime, args, data=None):
        source = '/controller_runtime/candidate_sha256' if role == 'use' else (
            '/data/candidate_sha256' if kind == 'prepare_update' else '/data/candidate_id')
        if role == 'use' and bad == 'executor_source': source = '/data/candidate_id'
        bindings = [{'idea_id': idea['id'], 'candidate_sha256': candidate, 'role': role,
                     'source_pointer': source, 'source_sha256': candidate,
                     'result_pointer': '/stdout', 'claim': 'Observed output is the intended fixture effect'}] if role else []
        op = operation(kind, args=args, improvement_bindings=bindings)
        at = start + timedelta(seconds=offset)
        result = OperationResult(operation_id=op['id'], status='succeeded', effect=use_effect if role == 'use' else 'confirmed',
                                 stdout='Actual fixture output content', data=data or {'candidate_id': candidate},
                                 started_at=at.isoformat(), finished_at=(at + timedelta(seconds=1)).isoformat()).model_dump()
        envelope = dict(runtime, record_id=task['id'] + ':' + op['id'], operation_sha256=digest(op),
                        result_sha256=digest({k: v for k, v in result.items() if k != 'controller_runtime'}))
        result['controller_runtime'] = envelope
        record = {'task_id': task['id'], 'operation_id': op['id'], 'status': 'recorded',
                  'record_id': envelope['record_id'], 'operation_sha256': digest(op),
                  'result_sha256': envelope['result_sha256'], 'controller_runtime': deepcopy(envelope),
                  'normal_use': {'status': envelope.get('normal_use', 'not-established')}}
        records[(task['id'], op['id'])] = record
        if role == 'use' and bad == 'forged_envelope': result['controller_runtime']['process_id'] = 'invented-process'
        if role == 'use' and bad == 'raw_hash_mismatch': result['stdout'] += ' changed after attestation'
        if role == 'use' and bad == 'foreign_record': record['task_id'] = 'some-other-task'
        reviewed = deepcopy(description) if kind == 'prepare_update' else None
        if kind == 'prepare_update':
            if bad in {'stage_missing_candidate', 'stage_forged_caller_hash'}: reviewed = None
            elif bad == 'stage_candidate_hash': reviewed['candidate_sha256'] = '0' * 64
            elif bad == 'stage_candidate_bytes': reviewed['rationale'] += ' Changed after description'
            elif bad == 'stage_runtime_mismatch': record['result_sha256'] = '0' * 64
        journal(store, task, op, result, learning(), candidate=reviewed)
        if kind == 'prepare_update' and bad == 'stage_unreviewed_candidate':
            saved = store.get_operation(op['id'])
            saved['pre_bundle']['candidate']['rationale'] += ' Not reviewed'
            store.update_operation(op['id'], pre_bundle=saved['pre_bundle'])
        return op['id']

    before = {'loaded_source_version': old_source, 'pid': 101, 'process_id': 'old-process', 'normal_use': 'not-established'}
    public_args = {'policy_hash': task['policy_hash'], 'rationale': description['rationale'],
                   'changes': [{'path': 'src/policy_harness/proposal.py', 'source': 'proposal.py',
                                'expected_sha256': None, 'sha256': hashlib.sha256(b'x=1\n').hexdigest()}]}
    if bad == 'stage_forged_caller_hash': public_args['expected_candidate_sha256'] = candidate
    staged = {'candidate_id': '0' * 64 if bad == 'stage_result_mismatch' else candidate,
              'candidate_sha256': candidate, 'staged': True, 'after_version': new_source}
    impl = observed('prepare_update', 'implementation', 0, runtime=before, args=public_args, data=staged)
    verify = observed('verify_update', 'verification', 2, runtime=before, args={'candidate_id': candidate})
    activation = observed('activate_update', None, 4, runtime={**before, **binding}, args={'candidate_id': candidate})
    after = {**binding, 'loaded_source_version': new_source, 'pid': 202, 'process_id': 'new-process', 'normal_use': 'observed'}
    if bad == 'same_process': after.update(pid=101, process_id='old-process')
    elif bad == 'same_pid': after['pid'] = 101
    elif bad == 'old_source': after['loaded_source_version'] = old_source
    elif bad == 'wrong_activation': after['activation_sha256'] = '0' * 64
    elif bad == 'wrong_candidate': after['candidate_sha256'] = '0' * 64
    elif bad == 'not_observed': after['normal_use'] = 'not-established'
    elif bad == 'rolled_back': after['activation_status'] = 'rolled_back'
    use_kind = 'activate_update' if bad == 'activation_as_use' else use_kind
    use = observed(use_kind, 'use', 3 if bad == 'before_activation' else 6, runtime=after,
                   args={'path': 'requested-output.txt'})
    decisions = resolution(idea, candidate, impl, verify, use)
    if bad != 'missing_activation': decisions[0]['activation_operation_id'] = activation
    knowledge.runtime_evidence = (lambda task_id, op_id: deepcopy(records.get((task_id, op_id)))) if bad != 'missing_service' else None
    return idea, decisions, records


def test_controller_verified_use_requires_protected_new_process_normal_result(runtime):
    store, knowledge, task = runtime
    idea, decisions, records = controller_evidence_fixture(store, knowledge, task)
    outcome = knowledge.resolve_ideas(task['id'], decisions, 'controller-resolution')
    assert outcome['resolved_idea_ids'] == [idea['id']]
    actual = store.record_get('idea', idea['id'])
    evidence = actual['evidence']['controller_update']
    preparation = store.get_operation(decisions[0]['implementation_operation_ids'][0])
    assert 'expected_candidate_sha256' not in preparation['operation']['args']
    assert preparation['pre_bundle']['candidate']['candidate_sha256'] == decisions[0]['candidate_sha256']
    assert evidence['activation_operation_id'] == decisions[0]['activation_operation_id']
    assert evidence['actual_use'][0]['pid'] == 202
    assert evidence['actual_use'][0]['process_id'] == 'new-process'
    assert actual['semantic_effect'] == 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'
    assert knowledge.resolve_ideas(task['id'], decisions, 'controller-resolution') == outcome


@pytest.mark.parametrize('bad', ['same_process', 'same_pid', 'old_source', 'wrong_activation', 'wrong_candidate',
                                 'not_observed', 'rolled_back', 'activation_as_use', 'before_activation',
                                 'missing_activation', 'missing_service', 'executor_source', 'forged_envelope',
                                 'raw_hash_mismatch', 'foreign_record', 'stage_missing_candidate', 'stage_candidate_hash',
                                 'stage_candidate_bytes', 'stage_result_mismatch', 'stage_unreviewed_candidate',
                                 'stage_forged_caller_hash', 'stage_runtime_mismatch'])
def test_controller_false_use_has_no_partial_verified_effect(runtime, bad):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task, bad=bad)
    before_history = store.records('idea_history')
    with pytest.raises(PolicyError):
        knowledge.resolve_ideas(task['id'], decisions, 'controller-resolution')
    assert store.record_get('idea', idea['id']) == idea
    assert store.records('idea_resolution') == []
    assert store.records('idea_history') == before_history


def test_controller_candidate_cannot_hide_stage_by_substituting_generic_old_success(runtime):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task)
    decision = decisions[0]
    candidate = decision['candidate_sha256']
    baseline = datetime.fromisoformat(idea['created_at']) + timedelta(seconds=20)
    decision.pop('activation_operation_id')
    for offset, (role, field) in enumerate([('implementation', 'implementation_operation_ids'),
                                           ('verification', 'verification_operation_ids'), ('use', 'actual_use_operation_ids')]):
        decision[field] = [improvement_operation(store, task, idea, candidate, role, baseline + timedelta(seconds=offset * 2))]
    with pytest.raises(PolicyError, match='activation_operation_id'):
        knowledge.resolve_ideas(task['id'], decisions, 'hidden-controller-resolution')
    assert store.record_get('idea', idea['id'])['status'] == 'pending'


def test_controller_legacy_idea_uses_exact_episode_time_without_rewriting_history(runtime):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task)
    episode = store.record_get('episode', idea['source_episode'])
    del idea['created_at']
    store.record('idea', idea['id'], idea)
    assert knowledge.resolve_ideas(task['id'], decisions)['resolved_idea_ids'] == [idea['id']]
    assert store.record_get('episode', episode['id']) == episode
    assert 'created_at' not in store.record_get('idea', idea['id'])


@pytest.mark.parametrize('bad', ['missing', 'future', 'foreign'])
def test_controller_legacy_time_is_never_invented(runtime, bad):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task)
    del idea['created_at']
    store.record('idea', idea['id'], idea)
    episode = store.record_get('episode', idea['source_episode'])
    if bad == 'missing': episode.pop('created_at')
    elif bad == 'future': episode['created_at'] = '2999-01-01T00:00:00+00:00'
    else: episode['task_id'] = store.create_task('Different source', ['Unrelated'])['id']
    store.record('episode', episode['id'], episode)
    with pytest.raises(PolicyError):
        knowledge.resolve_ideas(task['id'], decisions)
    assert store.record_get('idea', idea['id']) == idea


@pytest.mark.parametrize('kind', ['file_read', 'file_list', 'history_read', 'knowledge_read'])
def test_protected_successful_read_without_modification_can_close_update_use(runtime, kind):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task, use_kind=kind, use_effect='none')
    assert knowledge.resolve_ideas(task['id'], decisions)['resolved_idea_ids'] == [idea['id']]
    actual = store.get_operation(decisions[0]['actual_use_operation_ids'][0])['result']
    assert actual['effect'] == 'none'


@pytest.mark.parametrize('kind,effect', [('file_write', 'none'), ('file_read', 'unknown'), ('knowledge_update', 'none')])
def test_read_only_exception_does_not_admit_unknown_or_bookkeeping(runtime, kind, effect):
    store, knowledge, task = runtime
    idea, decisions, _ = controller_evidence_fixture(store, knowledge, task, use_kind=kind, use_effect=effect)
    with pytest.raises(PolicyError):
        knowledge.resolve_ideas(task['id'], decisions)
    assert store.record_get('idea', idea['id']) == idea


def test_large_knowledge_context_is_bounded_and_full_sources_remain_retrievable(runtime):
    store, knowledge, task = runtime
    first = seed_skill(store, knowledge, task)
    for number in range(80):
        value = deepcopy(first)
        value.update(id='bulk-' + str(number).zfill(3), title='Procedure ' + str(number),
                     content='Complete original procedure; ' * 3000)
        knowledge._save_skill(value)
    idea = seed_idea(store, knowledge, task)
    before = knowledge.retrieve(task)
    context = knowledge.context(task, max_chars=12000)
    assert len(json.dumps(context, ensure_ascii=False)) <= 12000
    assert context['projection_only'] is True and context['full_history_read'] is False
    assert context['pending']['workflow_ideas'] == 1
    # The idea's learning episode also creates its mandatory provisional Skill.
    assert context['pending']['skills_cleanup'] == 82
    assert context['indices']['skill']['total'] == 82
    assert 'Complete original procedure;' not in json.dumps(context)
    cursor, seen = None, []
    while True:
        page = knowledge.index(task, 'skill', cursor=cursor, limit=13)
        assert len(page['items']) <= 13
        for item in page['items']:
            read = knowledge.read(task, **item['read_args'])
            assert digest(read['value']) == item['source_sha256']
            assert read['value']['hash'] == item['hash']
            seen.append(item['id'])
        cursor = page['next_cursor']
        if cursor is None: break
    assert len(seen) == len(set(seen)) == 82
    assert knowledge.retrieve(task) == before
    # A small displayed page never replaces the complete mandatory cleanup/idea gate.
    decisions = [{'id': item['id'], 'disposition': 'retain', 'reason': 'Keep exact source for next use'}
                 for item in before['skills']]
    with pytest.raises(PolicyError, match='Unresolved improvements'):
        knowledge.finish(task['id'], decisions, True)
    assert store.record_get('idea', idea['id'])['status'] == 'pending'
    assert all(s['needs_cleanup'] for s in knowledge.retrieve(task)['skills'])


def test_index_cursor_and_full_read_reject_changed_source_or_query(runtime):
    store, knowledge, task = runtime
    first = seed_skill(store, knowledge, task)
    other = dict(deepcopy(first), id='second', title='Second procedure')
    knowledge._save_skill(other)
    page = knowledge.index(task, 'skill', limit=1)
    item, cursor = page['items'][0], page['next_cursor']
    with pytest.raises(PolicyError, match='cursor'):
        knowledge.index(task, 'skill', query='different query', cursor=cursor)
    changed = store.record_get('skill', first['id'])
    changed['content'] += ' Updated procedure.'
    knowledge._save_skill(changed)
    with pytest.raises(PolicyError, match='changed'):
        knowledge.index(task, 'skill', cursor=cursor)
    with pytest.raises(PolicyError, match='changed'):
        knowledge.read(task, **item['read_args'])
    with pytest.raises(PolicyError):
        knowledge.index(task, 'skill', cursor='not-a-valid-cursor')


def test_empty_query_and_private_child_indices_keep_exact_access_scope(runtime):
    store, knowledge, parent = runtime
    empty = knowledge.context(parent)
    assert all(page['total'] == 0 and page['next_cursor'] is None for page in empty['indices'].values())
    child = store.create_task('Private child source', ['Exact child output'], parent_id=parent['id'])
    skill = seed_skill(store, knowledge, child)
    episode_id = skill['sources'][0]
    assert knowledge.index(child, 'skill', query='write exact')['matched'] == 1
    assert knowledge.index(parent, 'skill')['items'] == []
    assert knowledge.index(parent, 'episode')['items'] == []
    for kind, identity in [('skill', skill['id']), ('episode', episode_id)]:
        with pytest.raises(PolicyError, match='visible'):
            knowledge.read(parent, kind, identity)
    # Passing a forged task dictionary does not alter persisted ownership.
    assert knowledge.context(dict(parent, id=child['id'], parent_id=None))['task_id'] == child['id']
    with pytest.raises(PolicyError, match='budget'):
        knowledge.context(parent, max_chars=100)


def test_family_deferred_and_candidate_lookup_preserve_sources_and_pending_census(runtime):
    store, knowledge, parent = runtime
    skill = seed_skill(store, knowledge, parent)
    idea = deferred_idea(store, knowledge, parent, 'Retain a source-bound future task improvement')
    child = store.create_task('Private child detail', ['Child output'], parent_id=parent['id'])
    private = seed_skill(store, knowledge, child)
    family = {'id': 'family-source', 'summary': 'Reusable public procedure',
              'episode_refs': [skill['sources'][0], private['sources'][0]],
              'skill_refs': [skill['id'], private['id']]}
    store.record('global_family', family['id'], family)
    payload = {'source_task_id': child['id'], 'primary_task_id': parent['id'], 'source_objective': 'Bounded child candidate'}
    store.record('knowledge_candidate', 'candidate-source',
                 {'id': 'candidate-source', 'payload': payload, 'hash': digest(payload), 'status': 'pending'})
    original = knowledge.retrieve(parent)
    for kind in ('family', 'deferred', 'candidate'):
        page = knowledge.index(parent, kind)
        item = next(i for i in page['items'] if i['id'] == {'family': family['id'], 'deferred': idea['id'], 'candidate': 'candidate-source'}[kind])
        read = knowledge.read(parent, **item['read_args'])
        assert read['source_sha256'] == digest(read['value']) == item['source_sha256']
        if kind == 'family':
            assert read['source_scope'] == 'visible-family-projection'
            assert read['value']['episode_refs'] == [skill['sources'][0]]
            assert read['value']['skill_refs'] == [skill['id']]
        elif kind == 'deferred':
            assert read['value']['idea'] == store.record_get('idea', idea['id'])
        else:
            assert read['value']['payload'] == payload
    context = knowledge.context(parent, query='a-nonmatching-query')
    assert all(page['matched'] == 0 for page in context['indices'].values())
    assert context['pending']['pending_candidates'] >= 1
    assert context['pending']['deferred_candidates'] == 1
    assert knowledge.retrieve(parent) == original
    unrelated = store.create_task('Unrelated parent', ['Separate data'])
    with pytest.raises(PolicyError, match='visible'):
        knowledge.read(unrelated, 'candidate', 'candidate-source')
    cursor = knowledge.index(parent, 'skill', limit=1)['next_cursor']
    # Cursors from a different task cannot disclose any of its index entries.
    if cursor:
        with pytest.raises(PolicyError, match='cursor'):
            knowledge.index(unrelated, 'skill', cursor=cursor)
