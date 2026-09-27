"""Real task/learning consumers with deterministic gateway decisions."""
import asyncio
from pathlib import Path

import pytest

from policy_harness.models import PolicyError
from policy_harness.practical_models import PracticalStep
from tests.test_practical_runtime import runtime, steps, tool, finish, run
from tests.test_usability_learning import lesson


@pytest.mark.asyncio
async def test_completion_review_uses_proposal_catalog_after_its_own_learning(tmp_path):
    create = steps(tool('file_read', path='answer.txt'))
    create['learning'] = [lesson()]
    old_catalog = []

    async def propose(payload):
        old_catalog.extend(payload['learning_context']['skills'])
        assert old_catalog[0]['revision'] == 1
        note = lesson()
        note.update(action='improve', skill=0, procedure='Read the exact saved byte facts once.')
        return dict(finish(), message='The supplied procedure is revision 1; it can be refined.', learning=[note])

    async def review(payload):
        live = store.record_get('practical_skill', old_catalog[0]['id'])
        assert live['revision'] == 2
        assert payload['learning_context']['skills'] == old_catalog
        assert payload['learned_procedures']['skills'] == old_catalog
        applied = payload['completion_learning']
        assert applied['status'] == 'applied'
        assert applied['record']['skill_changes'][0]['revision'] == 2
        assert applied['record']['skill_changes'][0]['id'] == old_catalog[0]['id']
        assert payload['proposed_completion']['learning'][0]['skill'] == 0
        return {'verdict':'accept','findings':[],'rationale':'The as-of-input description and actual refinement are consistent.'}

    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('file_write',path='answer.txt',text='hello')), create, propose, review])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert len(gateway.calls) == 4
    assert len(store.records('practical_skill_history')) == 1
    # Recovery sees the original call catalog and never repeats its refinement.
    call = next(c for c in store.records('practical_call') if c.get('response',{}).get('action') == 'complete')
    engine.learning.apply_optional(result, PracticalStep.model_validate(call['response']), call['id'], call['skill_catalog'])
    assert store.record_get('practical_skill', old_catalog[0]['id'])['revision'] == 2
    store.close()


@pytest.mark.asyncio
async def test_review_cannot_borrow_another_chat_catalog(tmp_path):
    engine, store, _, _, task = runtime(tmp_path, [])
    store.record('practical_call','foreign',{'task_id':'another-chat','source_hash':task['source_hash'],'skill_catalog':[]})
    with pytest.raises(PolicyError, match='回答作成時'):
        engine._completion_review_payload(task,PracticalStep.model_validate(finish()),[],['text'],completion_call='foreign')
    store.close()


@pytest.mark.asyncio
async def test_two_chats_run_and_stop_resume_independently_without_rewriting(tmp_path):
    first_waiting, second_waiting, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first_wait(payload):
        first_waiting.set()
        await asyncio.Event().wait()

    async def second_wait(payload):
        second_waiting.set()
        await release_second.wait()
        return finish('answer.txt')

    engine, store, gateway, _, first = runtime(tmp_path, [
        steps(tool('file_write',path='answer.txt',text='first')), first_wait,
        steps(tool('file_write',path='answer.txt',text='second')), second_wait,
        finish('answer.txt')])
    second = store.create_task('Create answer.txt containing second', ['The exact requested artifact exists'])
    try:
        handle1 = engine.start_task(first['id'])
        await asyncio.wait_for(first_waiting.wait(), 5)
        handle2 = engine.start_task(second['id'])
        await asyncio.wait_for(second_waiting.wait(), 5)
        assert not handle1.done() and not handle2.done()
        assert store.get_task(first['id'])['status'] == store.get_task(second['id'])['status'] == 'running'
        assert engine.start_task(second['id']) is handle2
        await engine.stop_task(first['id'])
        assert store.get_task(first['id'])['status'] == 'stopped'
        assert not handle2.done() and store.get_task(second['id'])['status'] == 'running'
        release_second.set()
        await asyncio.wait_for(handle2, 5)
        await asyncio.wait_for(engine.resume_task(first['id']), 5)
        for task, text in [(first,'first'),(second,'second')]:
            assert store.get_task(task['id'])['status'] == 'completed'
            assert Path(task['workspace'],'answer.txt').read_text() == text
            assert len(store.operations(task['id'])) == 1
        assert len(gateway.calls) == 5
    finally:
        await engine.close()
        store.close()
