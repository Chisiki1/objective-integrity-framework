"""Optional learning cannot discard a valid next action or relax its safeguards."""
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from policy_harness.access import Access
from policy_harness.models import PolicyError
from policy_harness.practical_models import PracticalStep, parse_practical_step
from policy_harness.providers import ModelGateway, ProviderError
from tests.test_practical_runtime import runtime, steps, tool, finish, run
from tests.test_providers import FakeSettings, FakePolicy, envelope
from tests.test_usability_learning import lesson


def incomplete_step():
    answer = steps(tool('file_write', path='second.txt', text='world'))
    note = lesson()
    note['operation_indices'] = []  # The actual user's first failure.
    answer.update(learning=[note], skill_uses=[{
        'new_lesson': 0, 'tool_index': 0, 'adaptation': 'Use the proposed method'}])
    return answer


@pytest.mark.asyncio
async def test_missing_evidence_continues_and_can_be_corrected_on_next_real_step(tmp_path):
    done = finish('first.txt', 'second.txt')
    done['learning'] = [lesson()]
    engine, store, gateway, web, task = runtime(tmp_path, [
        steps(tool('file_write', path='first.txt', text='hello')), incomplete_step(), done])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert Path(task['workspace'], 'second.txt').read_bytes() == b'world'
    assert len(gateway.calls) == 3
    rejected = store.records('practical_learning_rejection')
    assert len(rejected) == 1 and 'operation_indices' in rejected[0]['reason']
    assert rejected[0]['proposal']['learning'][0]['operation_indices'] == []
    assert gateway.calls[-1][2]['learning_context']['deferred_proposals']
    knowledge = engine.learning.snapshot(task['id'])
    assert len(knowledge['skills']) == 1 and knowledge['skills'][0]['use_count'] == 0
    assert not knowledge['applications']
    assert result['state']['metrics']['learning_model_calls'] == 0
    store.close()


@pytest.mark.asyncio
async def test_repeated_incomplete_metadata_does_not_hold_or_purchase_repair(tmp_path):
    done = finish('second.txt')
    done['learning'] = incomplete_step()['learning']
    engine, store, gateway, _, task = runtime(tmp_path, [incomplete_step(), done])
    result = await run(engine, store, task)
    assert result['status'] == 'completed' and len(gateway.calls) == 2
    assert len(store.records('practical_learning_rejection')) == 2
    assert not store.records('practical_skill')
    store.close()


@pytest.mark.asyncio
async def test_permission_pause_resumes_same_deferred_bundle_exactly_once(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [incomplete_step(), finish('second.txt')])
    Access(store).set(task['id'], 'ask', 0)
    result = await run(engine, store, task)
    assert result['status'] == 'awaiting_user'
    assert not Path(task['workspace'], 'second.txt').exists()
    approval = engine.pending_approvals()[0]
    engine.resolve_approval(approval['id'], 'approve', approval['proposal_hash'], 'Exact fixture write')
    await engine.running[task['id']]
    assert store.get_task(task['id'])['status'] == 'completed'
    assert len(store.operations(task['id'])) == 1 and len(gateway.calls) == 2
    assert len(store.records('practical_learning_rejection')) == 1
    assert len([e for e in store.events(task['id']) if e['stage'] == 'learning']) == 1
    store.close()


def test_source_changes_and_storage_errors_are_not_optional(tmp_path, monkeypatch):
    engine, store, _, _, task = runtime(tmp_path, [])
    answer = PracticalStep.model_validate(incomplete_step())
    stale = dict(task, source_hash='f' * 64)
    with pytest.raises(PolicyError, match='古い学習判断'):
        engine.learning.apply_optional(stale, answer, 'old', [])
    def unavailable(*args, **kwargs):
        raise OSError('database unavailable')
    monkeypatch.setattr(engine.learning, 'apply', unavailable)
    with pytest.raises(OSError, match='database unavailable'):
        engine.learning.apply_optional(task, answer, 'current', [])
    assert not store.records('practical_learning_rejection')
    store.close()


@pytest.mark.parametrize('field,bad', [('learning', [{'unexpected': 'bad'}]),
    ('skill_uses', [{'new_lesson': 0, 'skill': 0, 'tool_index': 0, 'adaptation': 'ambiguous'}]),
    ('learning_assessments', [{'operation_index': 'wrong'}])])
def test_optional_shape_is_retained_and_core_is_still_strict(field, bad):
    raw = steps(tool('file_list', path='.'))
    raw[field] = bad
    answer, deferred = parse_practical_step(raw)
    assert answer.tools[0].name == 'file_list' and not answer.learning
    assert deferred['proposal'][field] == bad and raw[field] == bad
    raw['tools'][0]['name'] = 'invented_tool'
    with pytest.raises(ValidationError):
        parse_practical_step(raw)


@pytest.mark.asyncio
async def test_real_gateway_retains_optional_error_but_rejects_credentials():
    value = steps(tool('file_list', path='.'))
    value['learning'] = [{'unexpected': 'bad'}]
    async def handler(request):
        return httpx.Response(200, json=envelope(json.dumps(value)))
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=httpx.MockTransport(handler))
    answer, metadata = await gateway.generate('parent', 'next_action', {'task_id': 'optional-fixture'}, PracticalStep)
    assert answer.action == 'tools' and metadata['optional_learning_rejection']['proposal']['learning'] == value['learning']
    value['learning'][0]['unexpected'] = 'SYNTHETIC_MODEL_CREDENTIAL'
    with pytest.raises(ProviderError, match='credential'):
        await gateway.generate('parent', 'next_action', {'task_id': 'optional-fixture'}, PracticalStep)


def test_web_guide_matches_active_provider_without_global_mutation(tmp_path):
    engine, store, _, web, task = runtime(tmp_path, [])
    web.settings = FakeSettings(web_provider='public_url')
    assert 'does NOT accept keyword' in engine._payload(task)['tools']['web_fetch']['query']
    web.settings.values['web_provider'] = 'brave'
    assert 'search query' in engine._payload(task)['tools']['web_fetch']['query']
    web.settings.values['web_provider'] = 'none'
    assert 'unavailable' in engine._payload(task)['tools']['web_fetch']
    store.close()


@pytest.mark.asyncio
async def test_oversized_rejected_metadata_stays_saved_without_filling_next_context(tmp_path):
    async def next_required_step(payload):
        deferred = payload['learning_context']['deferred_proposals']
        assert len(deferred) == 2
        assert len(json.dumps(deferred, ensure_ascii=False)) < 6500
        assert all(v['excerpt_truncated'] and v['call_id'] for v in deferred)
        return steps(tool('file_write', path='answer.txt', text='hello'))
    engine, store, gateway, _, task = runtime(tmp_path, [next_required_step, finish('answer.txt')])
    for i in range(3):
        raw = steps(tool('file_list', path='.'))
        raw['learning'] = [{'unexpected': 'x' * 100000}]
        answer, rejected = parse_practical_step(raw)
        rejected['reason'] += 'x' * 100000
        engine.learning.apply_optional(task, answer, f'oversized-{i}', [], rejected)
    result = await run(engine, store, task)
    assert result['status'] == 'completed' and len(gateway.calls) == 2
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    originals = store.records('practical_learning_rejection')
    assert len(originals) == 3
    assert all(r['proposal']['learning'][0]['unexpected'] == 'x' * 100000 for r in originals)
    assert not store.records('practical_skill')
    store.close()
