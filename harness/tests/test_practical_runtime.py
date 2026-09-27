"""Real Store/Executor/API integration with scripted judgments, not model quality proof."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import OperationResult, PolicyError
from policy_harness.practical_engine import PracticalEngine, RUNTIME
from policy_harness.practical_policy import PracticalPolicy
from policy_harness.server import create_app, create_runtime
from policy_harness.store import Store
from tests.test_server import Settings

ROOT = Path(__file__).resolve().parents[1]


def tool(name, **arguments):
    return {'name': name, 'arguments': arguments, 'purpose': 'Fulfill the requested artifact'}


def steps(*tools):
    return {'action': 'tools', 'tools': list(tools), 'message': 'Working on the requested output'}


def finish(*paths, criteria=1):
    return {'action': 'complete', 'message': 'Requested output is ready', 'artifacts': list(paths),
            'acceptance': [{'criterion': i, 'evidence': 'Tool result and output bytes verified'} for i in range(criteria)]}


class Gateway:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.entered = asyncio.Event()

    async def generate(self, role, phase, payload, schema):
        self.calls.append((role, phase, payload))
        self.entered.set()
        if not self.replies:
            raise AssertionError('Unexpected model call')
        value = self.replies.pop(0)
        if callable(value):
            value = await value(payload)
        return schema.model_validate(value), {'elapsed_seconds': 0.0, 'fixture': True}

    async def health(self):
        return {'ready': True, 'fixture': True}


class Web:
    def __init__(self):
        self.calls = []

    async def collect(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return {'sources': [{'url': 'https://example.com/', 'text': 'Public fact', 'sha256': '1' * 64}]}


def runtime(tmp_path, replies):
    store = Store(tmp_path)
    policy = PracticalPolicy(ROOT / 'policy/complete-policy-v3.json')
    gateway, web = Gateway(replies), Web()
    engine = PracticalEngine(store, policy, Executor(tmp_path), gateway, web, Knowledge(store))
    task = store.create_task('Create answer.txt containing hello', ['The exact requested artifact exists'])
    return engine, store, gateway, web, task


async def run(engine, store, task):
    await engine.start_task(task['id'])
    return store.get_task(task['id'])


@pytest.mark.asyncio
async def test_file_delivery_two_model_calls_without_web_learning_or_fake_reviews(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    assert len(gateway.calls) == 2 and web.calls == []
    operation = store.operations(task['id'])[0]
    assert operation['status'] == 'result_recorded'
    assert operation['receipts'] == {}
    assert result['final']['artifacts'][0]['sha256'] == hashlib.sha256(b'hello').hexdigest()
    assert result['state']['metrics']['learning_model_calls'] == 0
    store.close()


@pytest.mark.asyncio
async def test_existing_edit_requires_read_and_retains_preimage(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [
        steps(tool('file_write', path='answer.txt', text='new')),
        steps(tool('file_read', path='answer.txt')),
        steps(tool('file_write', path='answer.txt', text='new')), finish('answer.txt')])
    Path(task['workspace'], 'answer.txt').write_text('old', encoding='utf-8')
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    rows = store.operations(task['id'])
    assert 'READ_BEFORE_WRITE' in rows[0]['result']['stderr']
    assert rows[-1]['operation']['args']['expected_sha256'] == hashlib.sha256(b'old').hexdigest()
    backup = list((engine.executor.control / 'preimages').glob('*.json'))
    assert len(backup) == 1 and json.loads(backup[0].read_text())['base64'] == 'b2xk'
    store.close()


@pytest.mark.asyncio
async def test_traversal_is_rejected_before_effect_and_no_blind_failure_loop(tmp_path):
    request = steps(tool('file_write', path='../escaped.txt', text='bad'))
    engine, store, gateway, web, task = runtime(tmp_path, [request, request])
    result = await run(engine, store, task)
    assert result['status'] == 'held'
    assert not Path(task['workspace']).parent.joinpath('escaped.txt').exists()
    assert len(store.operations(task['id'])) == 1
    assert len(gateway.calls) == 2
    store.close()


@pytest.mark.asyncio
async def test_stop_during_model_then_resume_does_not_rewrite_success(tmp_path):
    waiting = asyncio.Event()
    async def wait(payload):
        waiting.set()
        await asyncio.Event().wait()
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), wait, finish('answer.txt')])
    engine.start_task(task['id'])
    await asyncio.wait_for(waiting.wait(), 3)
    await engine.stop_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'stopped'
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'completed'
    assert len(store.operations(task['id'])) == 1
    assert any(r['status'] == 'interrupted' for r in store.records('practical_call'))
    store.close()


@pytest.mark.asyncio
async def test_process_restart_reuses_saved_response_and_operation_identity(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [])
    store.update_task(task['id'], state=dict(task['state'], runtime=RUNTIME), status='running')
    task = store.get_task(task['id'])
    reply = steps(tool('file_write', path='answer.txt', text='hello'))
    store.record('practical_call', 'saved-call', {'id': 'saved-call', 'task_id': task['id'], 'source_hash': task['source_hash'],
                                                'phase': 'next_action', 'status': 'responded', 'consumed': False, 'response': reply})
    from uuid import uuid5, NAMESPACE_URL
    identity = uuid5(NAMESPACE_URL, 'saved-call:tool:0').hex
    op = engine._operation(identity, 'file_write', {'path': 'answer.txt', 'text': 'hello', 'expected_sha256': None}, 'Create')
    store.save_operation(task['id'], op.model_dump())
    store.update_operation(identity, status='executing', started_at='before-crash')
    executed = await engine.executor.execute(Path(task['workspace']), op)
    assert executed.status == 'succeeded'
    store.close()
    reopened = Store(tmp_path)
    second = PracticalEngine(reopened, engine.policy, Executor(tmp_path), Gateway([finish('answer.txt')]), Web(), Knowledge(reopened))
    assert reopened.get_task(task['id'])['status'] == 'recovery_required'
    await second.resume_task(task['id'])
    assert reopened.get_task(task['id'])['status'] == 'completed'
    assert len(reopened.operations(task['id'])) == 1
    assert len(second.gateway.calls) == 1
    reopened.close()


@pytest.mark.asyncio
async def test_unknown_effect_is_preserved_and_never_replayed(tmp_path, monkeypatch):
    engine, store, gateway, web, task = runtime(tmp_path, [])
    op = engine._operation('unknown-operation', 'file_write', {'path': 'answer.txt', 'text': 'hello'}, 'Create')
    store.update_task(task['id'], state={'runtime': RUNTIME})
    store.save_operation(task['id'], op.model_dump())
    store.update_operation(op.id, status='executing', started_at='before-crash')
    async def unknown(*args, **kwargs):
        return OperationResult(operation_id=op.id, status='unknown', effect='unknown')
    monkeypatch.setattr(engine.executor, 'reconcile', unknown)
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'held'
    assert store.get_operation(op.id)['result']['effect'] == 'unknown'
    assert not gateway.calls
    assert not Path(task['workspace'], 'answer.txt').exists()
    store.close()


@pytest.mark.asyncio
async def test_additional_instruction_preserves_previous_acceptance_and_final(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    await run(engine, store, task)
    original = store.get_task(task['id'])
    gateway.replies.extend([
        {'objective': 'Create both requested files', 'sources': [
            {'source': 0, 'classification': 'clarify', 'reason': 'Already applied; historical commentary'},
            {'source': 1, 'classification': 'add', 'reason': 'Additional output'}],
         'criteria': [{'original': 0, 'disposition': 'retain', 'criterion': task['acceptance'][0], 'reason': 'Still required'},
                      {'original': None, 'disposition': 'add', 'criterion': 'notes.txt contains note', 'sources': [1],
                       'quote': 'notes.txt contains note', 'reason': 'Direct instruction'}]},
        steps(tool('file_write', path='notes.txt', text='note')), finish('answer.txt', 'notes.txt', criteria=2)])
    await engine.submit_instruction(task['id'], 'Also ensure notes.txt contains note', original['source_hash'])
    await engine.running[task['id']]
    result = store.get_task(task['id'])
    assert result['status'] == 'completed'
    assert task['acceptance'][0] in result['acceptance']
    assert len(store.records('task_final_history')) == 1
    assert len(store.operations(task['id'])) == 2
    store.close()


@pytest.mark.asyncio
async def test_required_web_is_a_single_tool_without_recursive_model_reviews(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('web_fetch', query='https://example.com/')), finish(),
        {'verdict': 'accept', 'findings': [], 'rationale': 'The text answer uses the retrieved source'}])
    await run(engine, store, task)
    assert store.get_task(task['id'])['status'] == 'completed'
    assert len(web.calls) == 1 and len(gateway.calls) == 3
    store.close()


@pytest.mark.asyncio
async def test_code_final_review_can_require_a_real_correction(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [
        steps(tool('file_write', path='answer.py', text='print(1)')), finish('answer.py'),
        {'verdict': 'revise', 'findings': ['User requested output 2'], 'rationale': 'Wrong literal'},
        {'findings': [{'finding': 0, 'decision': 'accept', 'reason': 'The requested literal is 2'}], 'rationale': 'Correct the actual output'},
        steps(tool('file_write', path='answer.py', text='print(2)')), finish('answer.py'),
        {'verdict': 'accept', 'findings': [], 'rationale': 'Output matches requested source'}])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert Path(task['workspace'], 'answer.py').read_text() == 'print(2)'
    assert [r for r, _, _ in gateway.calls].count('reviewer') == 2
    assert len(store.records('practical_review')) == 2
    store.close()


@pytest.mark.asyncio
async def test_completion_rejects_unobserved_artifact_and_preserves_hold(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [finish('missing.txt'), finish('missing.txt')])
    result = await run(engine, store, task)
    assert result['status'] == 'held' and result['final'] is None
    assert 'no successful tool result' in result['state']['last_hold']['reason']
    store.close()


def test_factory_uses_practical_policy_and_preserves_legacy_tasks(tmp_path):
    legacy = Store(tmp_path)
    legacy.policy_hash = 'legacy-policy'
    task = legacy.create_task('Old task', ['Preserve unknown effects'])
    before = legacy.get_task(task['id'])
    legacy.close()
    store, settings, policy, engine = create_runtime(tmp_path)
    assert isinstance(engine, PracticalEngine)
    assert store.get_task(task['id']) == before
    with pytest.raises(PolicyError, match='Legacy'):
        engine.start_task(task['id'])
    assert policy.summary()['approved_practical']['id'] == 'practical-runtime-v1'
    assert len(policy.prompt()) < 8000  # Bounded execution guide, not old chat/fine-print duplication.
    parent = policy.phase_prompt('parent', 'next_action', 'PracticalStep', {})
    assert all(row['body'] in parent for row in policy.baseline.conditions.values())
    assert all(row['text'] in parent for row in policy.baseline.rules.values())
    store.close()


def test_existing_ui_api_creates_and_downloads_verified_artifact(tmp_path):
    engine, store, gateway, web, _ = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    app = create_app(tmp_path, engine=engine, settings=Settings(), store=store)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41234)) as client:
        assert '何を完成させたいですか' in client.get('/').text
        client.headers['X-CSRF-Token'] = client.get('/api/session').json()['csrf_token']
        response = client.post('/api/tasks', json={'objective': 'Write hello into answer.txt', 'acceptance': ['answer.txt contains hello']})
        assert response.status_code == 201
        task_id = response.json()['task']['id']
        async def await_task():
            await engine.running[task_id]
        client.portal.call(await_task)
        snapshot = client.get('/api/tasks/' + task_id).json()
        assert snapshot['task']['status'] == 'completed'
        artifact = snapshot['operations'][0]['result']['artifacts'][0]
        assert client.get(artifact['download_url']).content == b'hello'
    store.close()


@pytest.mark.asyncio
async def test_empty_artifact_claim_cannot_bypass_independent_outcome_check(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [
        finish(), {'verdict': 'revise', 'findings': ['No requested file was created'], 'rationale': 'No execution evidence'},
        {'findings': [{'finding': 0, 'decision': 'accept', 'reason': 'No file was produced'}], 'rationale': 'Create the missing requested file'},
        steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert [r for r, _, _ in gateway.calls] == ['parent', 'reviewer', 'parent', 'parent', 'parent']
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    store.close()


@pytest.mark.asyncio
async def test_identical_rejected_completion_is_not_reviewed_again(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [
        steps(tool('file_write', path='answer.py', text='print(1)')), finish('answer.py'),
        {'verdict': 'revise', 'findings': ['Required calculation is missing'], 'rationale': 'Concrete defect'},
        {'findings': [{'finding': 0, 'decision': 'accept', 'reason': 'The calculation is required'}], 'rationale': 'Repair the code'}, finish('answer.py')])
    result = await run(engine, store, task)
    assert result['status'] == 'held'
    assert 'already rejected' in result['state']['last_hold']['reason']
    assert [role for role, _, _ in gateway.calls].count('reviewer') == 1
    store.close()


@pytest.mark.asyncio
async def test_large_single_text_gets_independent_review(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [
        steps(tool('file_write', path='report.md', text='a' * 8192)), finish('report.md'),
        {'verdict': 'accept', 'findings': [], 'rationale': 'Verified requested large output'}])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert any('8192' in reason for reason in result['final']['review_applicability'])
    store.close()


@pytest.mark.asyncio
async def test_knowledge_search_cannot_forward_other_task_sources(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('knowledge_read', query='')),
        {'action': 'blocked', 'message': 'No earlier knowledge in this task'}])
    store.record('practical_outcome', 'private-other-task', {'task_id': 'another-task', 'objective': 'private-client-plan',
                                                         'summary': 'Do not forward', 'artifacts': []})
    await run(engine, store, task)
    assert store.operations(task['id'])[0]['result']['data']['matches'] == []
    assert 'private-client-plan' not in json.dumps(gateway.calls)
    store.close()


@pytest.mark.asyncio
async def test_cancelled_dispatched_web_remains_unknown_on_resume(tmp_path):
    engine, store, gateway, web, task = runtime(tmp_path, [steps(tool('web_fetch', query='https://example.com/'))])
    dispatched = asyncio.Event()
    async def collect(query, **kwargs):
        record = {'id': 'exchange-1', 'task_id': task['id'], 'operation_id': kwargs['operation_id'],
                  'network_dispatched': True, 'http_dispatched': True, 'status': 'started'}
        store.record('web_exchange', 'exchange-1', {'record': record, 'stage': 'started'})
        dispatched.set()
        await asyncio.Event().wait()
    web.collect = collect
    engine.start_task(task['id'])
    await asyncio.wait_for(dispatched.wait(), 3)
    await engine.stop_task(task['id'])
    row = store.operations(task['id'])[0]
    assert row['result']['effect'] == 'unknown'
    assert row['result']['data']['external_request_effect'] == 'unknown'
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'held'
    assert len(gateway.calls) == 1
    assert store.records('practical_result_history')
    store.close()


@pytest.mark.asyncio
async def test_reviewer_sees_program_generated_files_omitted_from_final_list(tmp_path):
    async def inspect_review(payload):
        assert {v['path'] for v in payload['artifact_contents']} == {'answer.txt', 'extra.txt'}
        assert any('omitted' in v for v in payload['review_reasons'])
        return {'verdict': 'accept', 'findings': [], 'rationale': 'Original request and both observed outputs checked'}
    engine, store, gateway, web, task = runtime(tmp_path, [finish('answer.txt'), inspect_review])
    store.update_task(task['id'], state={'runtime': RUNTIME})
    artifacts = []
    for name, content in [('answer.txt', b'hello'), ('extra.txt', b'program-created output')]:
        path = Path(task['workspace'], name)
        path.write_bytes(content)
        artifacts.append({'path': str(path), 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    op = engine._operation('recorded-program-result', 'exec', {'capability_id': 'fixture'}, 'Generate requested output')
    store.save_operation(task['id'], op.model_dump())
    store.update_operation(op.id, status='result_recorded', result=OperationResult(
        operation_id=op.id, status='succeeded', effect='confirmed', artifacts=artifacts,
        data={'changed_paths': ['answer.txt', 'extra.txt']}).model_dump())
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert len(gateway.calls) == 2
    store.close()
