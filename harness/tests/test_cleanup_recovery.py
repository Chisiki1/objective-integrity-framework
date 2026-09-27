"""Actual cleanup/projection faults with frozen review fixtures, never live AI proof."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from policy_harness.engine import CycleHeld
from policy_harness.knowledge import Knowledge
from policy_harness.models import OperationResult, PolicyError
from policy_harness.store import digest
from .test_core import runtime
from .test_knowledge import journal, learning, operation, seed_skill
from .fixture_preparation import bind_cleanup_evidence


def frozen_cleanup(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Controlled cleanup failure recovery', ['Keep exact knowledge source'])
    skills = [seed_skill(store, engine.knowledge, task), seed_skill(store, engine.knowledge, task)]
    skills.sort(key=lambda s: s['id'])
    action = operation('finish', args={'summary': 'Frozen cleanup fixture'})
    result = OperationResult(operation_id=action['id'], status='succeeded').model_dump()
    journal(store, task, action, result, learning(), status='learned')
    review = {'id': 'fixture-frozen-review', 'review': {'summary': 'Controlled fixture', 'opinions': []},
              'disposition': {'verdict': 'proceed', 'rationale': 'Fixture source', 'opinion_responses': []}}
    # This fixture isolates the real transaction/render/readback branch. It does
    # not certify upstream semantic assessments or the user's primary outcome.
    draft = {'completion': {'achieved': True, 'acceptance': [], 'evidence_refs': [],
                            'unresolved': [], 'summary': 'Controlled cleanup only'},
             'cleanup': {'decisions': [{'id': s['id'], 'disposition': 'retire', 'reason': 'Fixture cleanup choice'} for s in skills],
                         'rationale': 'Freeze exact cleanup inputs'},
             'choices': [], 'pre_assessments': [], 'pre_review': review, 'web': {'sources': []},
             'structural_evidence_complete': True, 'policy_hash': engine.policy.hash,
             'post_assessments': [], 'post_review': review}
    row = bind_cleanup_evidence(engine, task, store.get_operation(action['id']), draft)
    return store, engine, task, row, skills


def prepare_repair(store, knowledge, task, skill_id):
    skill = store.record_get('skill', skill_id)
    target = knowledge.root / 'skills' / skill_id / 'SKILL.md'
    args = {'skill_id': skill_id, 'expected_skill_hash': skill['hash'],
            'expected_file_sha256': hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None,
            'reason': 'Repair this exact registered projection and preserve its original bytes'}
    description = knowledge.describe_projection_repair(task, args)
    op = operation('knowledge_projection_repair', args=args)
    journal(store, task, op, OperationResult(operation_id=op['id'], status='pending').model_dump(),
            learning(), candidate=description, status='executing')
    return op, description


def accept_repair(store, task, op, description, result):
    row = store.get_operation(op['id'])
    assert row['task_id'] == task['id'] and row['pre_bundle']['candidate'] == description
    after = {'operation': op, 'result': result.model_dump(), 'learning': learning().model_dump()}
    store.update_operation(op['id'], result=result.model_dump(), status='post_reviewed', post_bundle=after,
                           post_review={'bundle_hash': digest(after), 'disposition': {'verdict': 'proceed'}})


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['later_failure', 'external_conflict', 'recovery_io'])
async def test_actual_engine_cleanup_partial_write_rollback_and_first_fault(tmp_path, monkeypatch, fault):
    store, engine, task, row, skills = frozen_cleanup(tmp_path)
    knowledge = engine.knowledge
    targets = [knowledge.root / 'skills' / s['id'] / 'SKILL.md' for s in skills]
    original_bytes = [p.read_bytes() for p in targets]
    original_history = store.records('skill_history')
    publish = knowledge._publish_projection
    calls = {'failed': False, 'partial': None, 'count': 0}

    def fail_later(target, content, before_publish=None):
        target = Path(target)
        if target.name == 'SKILL.md':
            calls['count'] += 1
            if target == targets[1] and not calls['failed']:
                calls['partial'] = targets[0].read_bytes()
                assert calls['partial'] != original_bytes[0]
                calls['failed'] = True
                if fault == 'external_conflict': targets[0].write_bytes(b'external source edited during cleanup\n')
                raise OSError('FIRST_FAULT_LATER_PROJECTION_REPLACE')
            if calls['failed'] and fault == 'recovery_io':
                raise OSError('SECOND_FAULT_RECOVERY_IO_DENIED')
        return publish(target, content, before_publish)

    monkeypatch.setattr(knowledge, '_publish_projection', fail_later)
    with pytest.raises(CycleHeld):
        await engine._finalize(task, row)
    failure_id = task['id'] + ':' + row['operation']['id']
    failure = store.record_get('cleanup_failure', failure_id)
    assert failure['first_fault']['message'] == 'FIRST_FAULT_LATER_PROJECTION_REPLACE'
    assert failure['transaction_rolled_back'] is True
    assert store.record_get('cleanup_result', failure_id) is None
    assert [store.record_get('skill', s['id']) for s in skills] == skills
    assert store.records('skill_history') == original_history
    assert store.get_task(task['id'])['status'] != 'completed'
    if fault == 'later_failure':
        assert [p.read_bytes() for p in targets] == original_bytes
        assert failure['effect'] == 'none' and failure['status'] == 'failed'
        assert knowledge.pending_projection_failures(task) == []
    else:
        assert failure['effect'] == 'unknown' and failure['status'] == 'unknown'
        assert knowledge.pending_projection_failures(task)[0]['failure_kind'] == 'cleanup_failure'
        if fault == 'external_conflict':
            assert targets[0].read_bytes() == b'external source edited during cleanup\n'
        else:
            assert not targets[0].exists()
            retained = [p.read_bytes() for p in (knowledge.root / 'projection-preimages').glob('*/before.bin')]
            assert calls['partial'] in retained and original_bytes[0] in retained
            assert failure['projection_recovery']['error']['message'] == 'SECOND_FAULT_RECOVERY_IO_DENIED'
    attempts = calls['count']
    with pytest.raises(CycleHeld):
        await engine._finalize(store.get_task(task['id']), store.get_operation(row['operation']['id']))
    assert calls['count'] == attempts
    assert store.record_get('cleanup_failure', failure_id) == failure
    if fault == 'external_conflict':
        monkeypatch.setattr(knowledge, '_publish_projection', publish)
        op, description = prepare_repair(store, knowledge, task, skills[0]['id'])
        result = knowledge.repair_projection(task, op['id'], op['args'])
        assert result.status == 'succeeded' and result.effect == 'confirmed'
        assert targets[0].read_bytes() == original_bytes[0]
        assert knowledge.pending_projection_failures(task)
        accept_repair(store, task, op, description, result)
        confirmation = knowledge.confirm_projection_repair(task, op['id'])
        assert confirmation['resolved_failure_ids'] == ['cleanup_failure:' + failure_id]
        assert knowledge.pending_projection_failures(task) == []
        assert store.record_get('cleanup_failure', failure_id) == failure
    store.close()


def test_existing_conflict_learning_returns_durable_partial_then_reviewed_repair(tmp_path):
    store, engine, task, _, skills = frozen_cleanup(tmp_path)
    knowledge = engine.knowledge
    target = knowledge.root / 'skills' / skills[0]['id'] / 'SKILL.md'
    original = target.read_bytes()
    target.write_bytes(b'Unrecognized original text that must not be lost\n')
    restart = Knowledge(store)
    assert restart.startup_projection_status['state'] == 'pending'
    op = operation('file_read', args={'path': 'requested.txt'})
    actual = OperationResult(operation_id=op['id'], status='succeeded', data={'text': 'actual fixture read'}).model_dump()
    learned = learning()
    outcome = restart.apply(task, op, learned, actual, [])
    assert outcome['projection_status']['state'] == 'pending'
    assert outcome['projection_status']['first_fault']['type'] == 'ProjectionConflict'
    saved_application = store.record_get('knowledge_application', task['id'] + ':' + op['id'])
    assert saved_application['outcome'] == outcome
    episode_count = len(store.records('episode'))
    assert restart.apply(task, op, learned, actual, []) == outcome
    assert len(store.records('episode')) == episode_count
    assert target.read_bytes() == b'Unrecognized original text that must not be lost\n'
    pending_before = restart.pending_projection_failures(task)
    assert pending_before and pending_before[0]['repair_args'][0]['expected_file_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(PolicyError, match='Projection failures'):
        restart.finish(task['id'], [], True)
    repair, description = prepare_repair(store, restart, task, skills[0]['id'])
    result = restart.repair_projection(task, repair['id'], repair['args'])
    assert result.status == 'succeeded' and target.read_bytes() == original
    assert (restart.root / result.data['backup_ref']).read_bytes() == b'Unrecognized original text that must not be lost\n'
    assert store.record_get('skill', skills[0]['id']) == skills[0]
    assert restart.repair_projection(task, repair['id'], repair['args']).model_dump() == result.model_dump()
    assert restart.pending_projection_failures(task)
    with pytest.raises(PolicyError):
        restart.confirm_projection_repair(task, repair['id'])
    accept_repair(store, task, repair, description, result)
    confirmed = restart.confirm_projection_repair(task, repair['id'])
    assert confirmed['resolved_failure_ids']
    assert restart.pending_projection_failures(task) == []
    assert restart.apply(task, op, learned, actual, []) == outcome
    assert store.record_get('knowledge_application', saved_application['id']) == saved_application
    assert restart.confirm_projection_repair(task, repair['id']) == confirmed
    store.close()


@pytest.mark.parametrize('race', ['capture', 'competing_creator', 'io_and_restore'])
def test_repair_preserves_raced_or_failed_original_and_requires_new_review(tmp_path, monkeypatch, race):
    store, engine, task, _, skills = frozen_cleanup(tmp_path)
    knowledge = engine.knowledge
    target = knowledge.root / 'skills' / skills[0]['id'] / 'SKILL.md'
    target.write_bytes(b'Exact reviewed conflicting source\n')
    before = target.read_bytes()
    op, description = prepare_repair(store, knowledge, task, skills[0]['id'])
    rename = Path.rename
    publish = knowledge._publish_projection
    if race == 'capture':
        def raced_rename(path, destination):
            if path == target: path.write_bytes(b'External bytes changed after final CAS\n')
            return rename(path, destination)
        monkeypatch.setattr(Path, 'rename', raced_rename)
    elif race == 'competing_creator':
        def competing(destination, content, before_publish=None):
            if before_publish: destination.write_bytes(b'Another writer created this exact target\n')
            return publish(destination, content, before_publish)
        monkeypatch.setattr(knowledge, '_publish_projection', competing)
    else:
        def denied(*args, **kwargs): raise OSError('REPAIR_PUBLICATION_AND_RESTORE_IO_DENIED')
        monkeypatch.setattr(knowledge, '_publish_projection', denied)
    result = knowledge.repair_projection(task, op['id'], op['args'])
    assert result.status == 'failed' and result.effect == 'unknown'
    assert result.data['first_fault']
    if race == 'capture':
        assert target.read_bytes() == b'External bytes changed after final CAS\n'
        assert (knowledge.root / result.data['backup_ref']).read_bytes() == target.read_bytes()
    elif race == 'competing_creator':
        assert target.read_bytes() == b'Another writer created this exact target\n'
        assert (knowledge.root / result.data['backup_ref']).read_bytes() == before
    else:
        assert not target.exists()
        assert (knowledge.root / result.data['backup_ref']).read_bytes() == before
        assert result.data['preimage_restoration']['first_fault']['message'] == 'REPAIR_PUBLICATION_AND_RESTORE_IO_DENIED'
    monkeypatch.setattr(Path, 'rename', rename)
    monkeypatch.setattr(knowledge, '_publish_projection', publish)
    accept_repair(store, task, op, description, result)
    assert knowledge.reconcile_projection_repair(task, op['id']).model_dump() == result.model_dump()
    successor, new_description = prepare_repair(store, knowledge, task, skills[0]['id'])
    repaired = knowledge.repair_projection(task, successor['id'], successor['args'])
    assert repaired.status == 'succeeded'
    accept_repair(store, task, successor, new_description, repaired)
    confirmation = knowledge.confirm_projection_repair(task, successor['id'])
    assert op['id'] in confirmation['reconcilable_operation_ids']
    reconciled = knowledge.reconcile_projection_repair(task, op['id'])
    assert reconciled.status == 'failed' and reconciled.effect == 'confirmed'
    assert reconciled.data['original_effect'] == 'unknown'
    assert reconciled.data['first_fault'] == result.data['first_fault']
    assert knowledge._repair_record(task, op['id'])[2]['result'] == result.model_dump()
    assert target.read_bytes() == knowledge._skill_markdown(store.record_get('skill', skills[0]['id']))
    store.close()


def test_repair_rejects_changed_version_review_and_nonregular_target(tmp_path):
    store, engine, task, _, skills = frozen_cleanup(tmp_path)
    knowledge = engine.knowledge
    target = knowledge.root / 'skills' / skills[0]['id'] / 'SKILL.md'
    target.write_bytes(b'Explicitly reviewed bytes\n')
    op, _ = prepare_repair(store, knowledge, task, skills[0]['id'])
    target.write_bytes(b'Changed after review\n')
    with pytest.raises(PolicyError, match='changed'):
        knowledge.repair_projection(task, op['id'], op['args'])
    assert target.read_bytes() == b'Changed after review\n'
    child = store.create_task('Foreign child', ['Own source only'], parent_id=task['id'])
    with pytest.raises(PolicyError, match='ownership'):
        knowledge.describe_projection_repair(child, op['args'])
    link = target.parent / 'hardlink.txt'
    os.link(target, link)
    try:
        with pytest.raises(PolicyError, match='HARDLINK'):
            knowledge.describe_projection_repair(task, dict(op['args'], expected_file_sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
    finally:
        link.unlink()
    store.close()


def test_registered_first_projection_failure_can_create_only_exactly_absent_target(tmp_path, monkeypatch):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('First knowledge write failed', ['Retain and repair the committed source'])
    knowledge = engine.knowledge
    render = knowledge.rebuild_projections
    def fail_before_index(): raise OSError('FIRST_RENDER_FAILED_BEFORE_INDEX')
    monkeypatch.setattr(knowledge, 'rebuild_projections', fail_before_index)
    skill = seed_skill(store, knowledge, task)
    monkeypatch.setattr(knowledge, 'rebuild_projections', render)
    target = knowledge.root / 'skills' / skill['id'] / 'SKILL.md'
    assert not target.exists() and not (knowledge.root / 'projection-index.json').exists()
    op, description = prepare_repair(store, knowledge, task, skill['id'])
    assert description['projection_index_entry'] is None
    assert description['before']['absent'] and op['args']['expected_file_sha256'] is None
    result = knowledge.repair_projection(task, op['id'], op['args'])
    assert result.status == 'succeeded' and result.effect == 'confirmed'
    assert target.read_bytes() == knowledge._skill_markdown(skill)
    accept_repair(store, task, op, description, result)
    knowledge.confirm_projection_repair(task, op['id'])
    assert knowledge.pending_projection_failures(task) == []
    # Existing unindexed files cannot receive that absence-only permission.
    index_path = knowledge.root / 'projection-index.json'
    index = json.loads(index_path.read_text('utf-8'))
    index['entries'].pop(skill['id'])
    index_path.write_text(json.dumps(index), encoding='utf-8')
    with pytest.raises(PolicyError, match='no registered'):
        prepare_repair(store, knowledge, task, skill['id'])
    assert target.read_bytes() == knowledge._skill_markdown(skill)
    store.close()


@pytest.mark.parametrize('substitute_same_bytes', [False, True])
def test_interrupted_publication_readback_is_read_only_and_needs_owned_file_identity(tmp_path, monkeypatch, substitute_same_bytes):
    import policy_harness.knowledge as knowledge_module
    store, engine, task, _, skills = frozen_cleanup(tmp_path)
    knowledge = engine.knowledge
    target = knowledge.root / 'skills' / skills[0]['id'] / 'SKILL.md'
    target.write_bytes(b'Exact original before a simulated process interruption\n')
    op, _ = prepare_repair(store, knowledge, task, skills[0]['id'])
    atomic = knowledge_module.atomic_json
    class SimulatedProcessInterruption(BaseException):
        pass
    def interrupted(path, value, **kwargs):
        if path.name == 'record.json' and value.get('phase') == 'complete':
            raise SimulatedProcessInterruption('Published file; final receipt commit not reached')
        return atomic(path, value, **kwargs)
    monkeypatch.setattr(knowledge_module, 'atomic_json', interrupted)
    with pytest.raises(SimulatedProcessInterruption):
        knowledge.repair_projection(task, op['id'], op['args'])
    monkeypatch.setattr(knowledge_module, 'atomic_json', atomic)
    assert target.read_bytes() == knowledge._skill_markdown(skills[0])
    restarted = Knowledge(store)
    if substitute_same_bytes:
        replacement = target.parent / 'external-replacement.tmp'
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
    files_before = {str(p.relative_to(knowledge.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in knowledge.root.rglob('*') if p.is_file()}
    result = restarted.reconcile_projection_repair(task, op['id'])
    files_after = {str(p.relative_to(knowledge.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in knowledge.root.rglob('*') if p.is_file()}
    assert files_after == files_before
    receipt = restarted._repair_record(task, op['id'])[2]
    assert receipt['phase'] == 'publication-prepared' and receipt['result'] is None
    if substitute_same_bytes:
        assert result.status == 'failed' and result.effect == 'unknown'
        assert result.data['first_fault']['type'] == 'InterruptedProjectionRepair'
    else:
        assert result.status == 'succeeded' and result.effect == 'confirmed'
    store.close()
