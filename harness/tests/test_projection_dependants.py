"""Observed knowledge ownership and real filesystem races, with review fixtures."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from policy_harness.models import OperationResult, PolicyError
from policy_harness.store import Store, digest
from policy_harness.knowledge import Knowledge
from .test_cleanup_recovery import prepare_repair, accept_repair
from .test_knowledge import learning, operation, seed_skill


def test_parent_repair_closes_owned_descendant_failure_without_changing_original(tmp_path):
    store = Store(tmp_path / 'data'); knowledge = Knowledge(store)
    parent = store.create_task('Parent', ['exact source'])
    child = store.create_task('Child', ['exact source'], parent_id=parent['id'])
    foreign = store.create_task('Other root', ['separate authority'])
    skill = seed_skill(store, knowledge, parent)
    target = knowledge.root / 'skills' / skill['id'] / 'SKILL.md'
    target.write_bytes(b'Changed shared source, preserve this preimage')
    action = operation('file_read', args={'path': 'source.txt'})
    result = OperationResult(operation_id=action['id'], status='succeeded', data={'text': 'Observed'}).model_dump()
    outcome = knowledge.apply(child, action, learning(), result, [])
    failure = deepcopy(store.record_get('projection_failure', outcome['projection_status']['failure_id']))
    own = knowledge.pending_projection_failures(child)
    assert own and own[0]['unavailable'] and own[0]['repair_args'] == []
    queue = knowledge.projection_repair_queue(parent)
    assert queue[0]['task_id'] == child['id'] and queue[0]['repair_args'][0]['skill_id'] == skill['id']
    assert knowledge.projection_repair_queue(foreign) == []
    with pytest.raises(PolicyError, match='not the failure owner or ancestor'):
        knowledge.pending_projection_failures(child, repair_actor=foreign)
    repair, described = prepare_repair(store, knowledge, parent, skill['id'])
    actual = knowledge.repair_projection(parent, repair['id'], repair['args'])
    assert actual.status == 'succeeded' and knowledge.pending_projection_failures(child)
    accept_repair(store, parent, repair, described, actual)
    confirmed = knowledge.confirm_projection_repair(parent, repair['id'])
    assert confirmed['resolved_failure_ids'] == ['projection_failure:' + failure['id']]
    assert knowledge.pending_projection_failures(child) == []
    assert store.record_get('projection_failure', failure['id']) == failure
    assert knowledge.apply(child, action, learning(), result, []) == outcome
    decisions = [{'id': s['id'], 'disposition': 'retain', 'reason': 'Preserve unverified child knowledge'}
                 for s in knowledge.snapshot(child['id'])['skills'] if s['needs_cleanup']]
    assert knowledge.finish(child['id'], decisions, True)['unverified_effects_explicit']
    store.close()


@pytest.mark.parametrize('race', ['capture', 'new_creator', 'orphan_capture'])
def test_normal_renderer_never_clobbers_a_concurrent_original(tmp_path, monkeypatch, race):
    store = Store(tmp_path / 'data'); knowledge = Knowledge(store)
    task = store.create_task('Render source', ['Keep original'])
    skill = seed_skill(store, knowledge, task)
    target = knowledge.root / 'skills' / skill['id'] / 'SKILL.md'
    before = target.read_bytes(); external = b'External concurrent bytes must survive'
    if race == 'orphan_capture':
        store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('skill', skill['id']))
        store.db.commit()
    else:
        changed = dict(skill, content='New authoritative text')
        knowledge._save_skill(changed)
    rename, publish = Path.rename, knowledge._publish_projection
    if race in {'capture', 'orphan_capture'}:
        def capture(path, destination):
            if path == target: path.write_bytes(external)
            return rename(path, destination)
        monkeypatch.setattr(Path, 'rename', capture)
    else:
        def create(destination, content, before_publish=None):
            if destination == target: destination.write_bytes(external)
            return publish(destination, content, before_publish)
        monkeypatch.setattr(knowledge, '_publish_projection', create)
    with pytest.raises((PolicyError, OSError)) as error:
        knowledge.rebuild_projections()
    assert target.read_bytes() == external
    assert getattr(error.value, 'projection_skill_id') == skill['id']
    receipt = knowledge.root / error.value.projection_receipt_ref
    assert receipt.is_file()
    backup = receipt.parent / 'before.bin'
    assert backup.read_bytes() == (before if race == 'new_creator' else external)
    store.close()
