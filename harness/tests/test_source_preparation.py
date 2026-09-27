"""Actual source/Executor/Engine consumers with explicit AI/Web fixtures.

The optional Docker case executes one controlled UTF-16 decoder. It does not
claim arbitrary format support, semantic model obedience, or whole acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path

import pytest

from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import Operation, PolicyError
from policy_harness.policy import PolicyCatalog
from policy_harness.source_preparation import prepared_source_texts
from policy_harness.store import Store, digest
from tests.test_core import FixtureWeb, POLICY
from tests.test_core_recovery_closure import cycle
from tests.test_executor import recipe
from tests.test_model_routing import configure
from tests.test_source_integration import SourceGateway, proposed


class PreparationGateway(SourceGateway):
    async def generate(self, role, phase, payload, schema, **kwargs):
        if schema is Operation and phase == 'source_preparation_proposal':
            preparation = payload['source_preparation']
            item = preparation['pending'][0]
            task = self.store.get_task(payload['task_id'])
            args = {name: item[name] for name in ('source_id', 'expected_hash', 'expected_source_hash')}
            args['reason'] = 'Fixture uses one actual task-owned source and preserves every other obligation'
            withdrawals = [s for s in task['source_history'] if s['kind'] == 'instruction' and
                           s['text'] == '添付 opaque.bin を撤回します。元の hello ファイル作成は継続してください。']
            if withdrawals:
                source = withdrawals[-1]
                return proposed('source_prepare', mode='withdraw', instruction_id=source['id'],
                                source_quote=source['text'], **args), {'fixture': True}
            assert not item['preparations'], 'Fixture reaches one stage boundary; no invented extractor is supplied'
            return proposed('source_prepare', mode='stage', **args), {'fixture': True}
        return await super().generate(role, phase, payload, schema, **kwargs)


@asynccontextmanager
async def actual_runtime(data):
    store = Store(data)
    policy = PolicyCatalog(POLICY)
    executor = Executor(data)
    settings = configure(data)
    gateway = PreparationGateway(policy, store, settings)
    engine = Engine(store, policy, executor, gateway, FixtureWeb(), Knowledge(store))
    try:
        yield store, engine, executor, gateway
    finally:
        await engine.close()
        store.close()


def attach(store, task, raw=b'\xff\xfe\x00\x00opaque\xff', filename='opaque.bin'):
    task = store.append_instruction(task['id'], 'Attachment: ' + filename, task['source_hash'],
                                    attachment={'filename': filename, 'bytes': raw})
    return task, task['source_history'][-1]


def preparation(task, source, mode='stage', **extra):
    return proposed('source_prepare', mode=mode, source_id=source['id'], expected_hash=source['sha256'],
                    expected_source_hash=task['source_hash'], reason='Explicit fixture preparation', **extra)


@pytest.mark.asyncio
async def test_opaque_source_reaches_governed_stage_with_original_bytes_preserved(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
        raw = b'\xff\x00PRIVATE-DUMMY-BINARY\x82\xa0'
        task, source = attach(store, task, raw)
        await engine._initialize_task(task)
        assert store.record_get('source_read_progress', source['id'])['complete']
        assert not store.record_get('source_read_progress', source['id'])['text_decoded']
        await engine._initialize_task(store.get_task(task['id']))
        row = next(r for r in store.operations(task['id']) if r['operation']['kind'] == 'source_prepare')
        assert row['status'] == 'cycle_complete' and row['result']['status'] == 'succeeded'
        assert row['pre_review']['disposition']['verdict'] == row['post_review']['disposition']['verdict'] == 'proceed'
        assert (Path(task['workspace']) / row['result']['data']['path']).read_bytes() == raw
        assert store.source_bytes(task['id'], source['id'], source['sha256'])[0] == raw
        assert source['id'] not in prepared_source_texts(store, store.get_task(task['id']))
        assert 'PRIVATE-DUMMY-BINARY' not in json.dumps(row)
        assert 'base64' not in row['operation']['args'] and not row['result']['artifacts']
        protected = executor._read_record(row['result']['data']['executor_operation_id'])
        assert protected['operation']['kind'] == 'file_write' and protected['phase'] == 'finished'
        assert engine.source_preparation.pending(store.get_task(task['id']))['pending']


@pytest.mark.asyncio
async def test_later_exact_withdrawal_resumes_same_task_and_retains_source_history(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
        raw = b'\xff\xfe\x00\x00opaque-original'
        task, source = attach(store, task, raw)
        await engine._initialize_task(task)
        await engine._initialize_task(store.get_task(task['id']))
        previous = store.get_task(task['id'])
        task = store.append_instruction(task['id'], '添付 opaque.bin を撤回します。元の hello ファイル作成は継続してください。', previous['source_hash'])
        instruction = task['source_history'][-1]
        result = await asyncio.wait_for(engine.run_task(task['id']), 120)
        assert result['task']['id'] == task['id'] and result['task']['status'] == 'completed', result['events'][-3:]
        withdrawn = next(s for s in result['task']['source_history'] if s['id'] == source['id'])
        assert withdrawn['status'] == 'withdrawn' and withdrawn['classification'] == 'withdraw'
        assert withdrawn['disposition']['instruction_id'] == instruction['id']
        assert withdrawn['disposition']['source_quote'] == instruction['text']
        assert store.source_bytes(task['id'], source['id'])[0] == raw
        assert result['task']['acceptance'] == previous['acceptance']
        assert result['task']['source_history'][0] == previous['source_history'][0]
        assert (Path(task['workspace']) / 'answer.txt').read_text('utf-8') == 'hello'
        assert len([r for r in result['operations'] if r['operation']['kind'] == 'source_prepare' and r['operation']['args']['mode'] == 'withdraw']) == 1
        assert not engine.source_preparation.pending(result['task'])['pending']
        assert store.verify_events()


@pytest.mark.asyncio
async def test_stage_confirmation_keeps_observed_effect_across_later_source_update(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task, source = attach(store, store.create_task('Read attachment', ['source']))
        action = preparation(task, source)
        store.save_operation(task['id'], action.model_dump(), policy_hash=engine.policy.hash)
        state = {'task_id': task['id'], 'operation_id': action.id}
        await engine._pre(state); await engine._execute(state)
        actual = store.get_operation(action.id)['result']
        assert actual['status'] == 'succeeded'
        current = store.append_instruction(task['id'], 'Keep all prior requirements; this is additional context.', task['source_hash'])
        assert current['source_hash'] != task['source_hash']
        await engine._post(state); await engine._learn(state); await engine._close_cycle(state)
        row = store.get_operation(action.id)
        assert row['status'] == 'cycle_complete' and row['result'] == actual
        assert store.record_get('source_preparation_confirmation', action.id)
        assert store.source_bytes(task['id'], source['id'])[0] == (Path(task['workspace']) / actual['data']['path']).read_bytes()
        final = store.get_task(task['id'])
        assert all(s['status'] == 'pending' for s in final['source_history'][1:])
        assert final['acceptance'] == task['acceptance'] and not prepared_source_texts(store, final)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['foreign-source', 'stale-set', 'wrong-origin', 'foreign-quote', 'fabricated-quote', 'earlier-instruction'])
async def test_preparation_rejects_foreign_stale_or_invented_authority_without_staging(tmp_path, fault):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task = store.create_task('Preserve task', ['source'])
        task = store.append_instruction(task['id'], 'old instruction', task['source_hash'])
        earlier = task['source_history'][-1]
        task, source = attach(store, task)
        other = store.create_task('Other', ['other'])
        other, foreign = attach(store, other)
        task = store.append_instruction(task['id'], 'Withdraw opaque.bin', task['source_hash'])
        later = task['source_history'][-1]
        action = preparation(task, source)
        if fault == 'foreign-source':
            action.args.update(source_id=foreign['id'], expected_hash=foreign['sha256'])
        elif fault == 'stale-set':
            action.args['expected_source_hash'] = '0' * 64
        elif fault == 'wrong-origin':
            action.args['expected_hash'] = '0' * 64
        else:
            action = preparation(task, source, 'withdraw', instruction_id=later['id'], source_quote=later['text'])
            if fault == 'foreign-quote': action.args['instruction_id'] = foreign['id']
            if fault == 'fabricated-quote': action.args['source_quote'] = 'This was never supplied'
            if fault == 'earlier-instruction': action.args.update(instruction_id=earlier['id'], source_quote=earlier['text'])
        with pytest.raises(PolicyError):
            engine.source_preparation.describe(task, action.args)
        assert list(Path(task['workspace']).iterdir()) == []
        assert not store.records('source_preparation')


@pytest.mark.asyncio
async def test_stage_result_gap_is_reconciled_from_actual_executor_without_second_write(tmp_path, monkeypatch):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task, source = attach(store, store.create_task('Read attachment', ['source']))
        action = preparation(task, source)
        store.save_operation(task['id'], action.model_dump(), policy_hash=engine.policy.hash)
        state = {'task_id': task['id'], 'operation_id': action.id}
        await engine._pre(state)
        real = executor.execute
        calls = []
        async def lose_result(workspace, operation):
            calls.append(operation.id)
            actual = await real(workspace, operation)
            assert actual.status == 'succeeded' and actual.effect == 'confirmed', actual
            raise RuntimeError('Injected loss after actual file write before preparation result capture')
        monkeypatch.setattr(executor, 'execute', lose_result)
        await engine._execute(state)
        first = store.get_operation(action.id)['result']
        assert first['effect'] == 'unknown' and len(calls) == 1
        observed = await engine.source_preparation.reconcile(task, action)
        assert observed.status == 'succeeded' and observed.effect == 'confirmed'
        again = await engine.source_preparation.reconcile(task, action)
        assert again == observed and len(calls) == 1
        assert store.get_operation(action.id)['result'] == first
        assert (Path(task['workspace']) / observed.data['path']).read_bytes() == store.source_bytes(task['id'], source['id'])[0]


@pytest.mark.asyncio
async def test_staged_copy_and_unreviewed_generated_file_cannot_be_adopted_as_extraction(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task, source = attach(store, store.create_task('Read attachment', ['source']))
        stage = preparation(task, source)
        row = await cycle(engine, task, stage)
        arbitrary = await cycle(engine, task, proposed('file_write', path='invented.txt', text='Unrelated generated text'))
        accept = preparation(task, source, 'accept_extraction', stage_operation_id=stage.id,
            extraction_operation_id=arbitrary['operation']['id'], output_path='invented.txt',
            expected_output_sha256=hashlib.sha256(b'Unrelated generated text').hexdigest(), method='Claim without actual extractor')
        with pytest.raises(PolicyError, match='Executor operation'):
            engine.source_preparation.describe(task, accept.args)
        assert not prepared_source_texts(store, task)
        assert row['result']['data']['source_semantics_adopted'] is False


@pytest.mark.asyncio
async def test_withdrawal_confirmation_waits_for_actual_post_review_and_learning(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task, source = attach(store, store.create_task('Read attachment', ['source']))
        task = store.append_instruction(task['id'], 'Withdraw opaque.bin', task['source_hash'])
        instruction = task['source_history'][-1]
        action = preparation(task, source, 'withdraw', instruction_id=instruction['id'], source_quote=instruction['text'])
        store.save_operation(task['id'], action.model_dump(), policy_hash=engine.policy.hash)
        state = {'task_id': task['id'], 'operation_id': action.id}
        await engine._pre(state); await engine._execute(state)
        with pytest.raises(PolicyError): engine.source_preparation.confirm(task, action.id)
        assert next(s for s in store.get_task(task['id'])['source_history'] if s['id'] == source['id'])['status'] == 'pending'
        await engine._post(state); await engine._learn(state); await engine._close_cycle(state)
        assert next(s for s in store.get_task(task['id'])['source_history'] if s['id'] == source['id'])['status'] == 'withdrawn'


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('HARNESS_RUN_SOURCE_PREPARATION_DOCKER_TESTS') != '1', reason='Explicit actual Docker source-preparation opt-in')
async def test_actual_isolated_decoder_to_reviewed_derivative_to_same_task_plan(tmp_path):
    async with actual_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        setup = await executor.prepare_environment(include_pytest=False)
        assert setup['status'] == 'succeeded', setup
        text = '添付の原文です。\nKeep the hello task.\n'
        raw = text.encode('utf-16')
        task, source = attach(store, store.create_task('Write hello in answer.txt', ['answer.txt contains hello']), raw, 'utf16.txt')
        await engine._initialize_task(task)
        stage = preparation(task, source)
        staged = await cycle(engine, task, stage)
        input_path = staged['result']['data']['path']
        code = ('from pathlib import Path\n'
                f'raw = Path({input_path!r}).read_bytes()\n'
                "assert raw[:2] in (b'\\xff\\xfe', b'\\xfe\\xff')\n"
                "Path('extracted.txt').write_bytes(raw.decode('utf-16').encode('utf-8'))\n")
        await cycle(engine, task, proposed('file_write', path='extractor.py', text=code, expected_sha256=None))
        registration = await cycle(engine, task, proposed('capability_request', **recipe(code.encode('utf-8'), entrypoint='extractor.py')))
        extraction = await cycle(engine, task, proposed('exec', capability_id=registration['result']['data']['capability_id']))
        assert extraction['result']['status'] == 'succeeded', extraction['result']
        action = preparation(task, source, 'accept_extraction', stage_operation_id=stage.id,
            extraction_operation_id=extraction['operation']['id'], output_path='extracted.txt',
            expected_output_sha256=hashlib.sha256(text.encode()).hexdigest(), method='Lossless BOM-selected UTF-16 decode; exact input and output verified')
        candidate = engine.source_preparation.describe(task, action.args)
        assert candidate['extraction']['text'] == text
        wrong = action.model_copy(deep=True)
        wrong.args['expected_output_sha256'] = '0' * 64
        with pytest.raises(PolicyError): engine.source_preparation.describe(task, wrong.args)
        accepted = await cycle(engine, task, action)
        assert accepted['status'] == 'cycle_complete'
        assert prepared_source_texts(store, store.get_task(task['id']))[source['id']] == text
        assert store.source_bytes(task['id'], source['id'])[0] == raw
        derivative = store.record_get('source_extraction', source['id'])
        assert derivative['source_sha256'] != derivative['extracted_sha256']
        assert store.source_bytes(task['id'], derivative['raw_source_ref'])[0] == text.encode()
        # The normal source-planning consumer uses the confirmed original meaning.
        await engine._initialize_task(store.get_task(task['id']))
        current = store.get_task(task['id'])
        assert current['state']['plan'] and next(s for s in current['source_history'] if s['id'] == source['id'])['status'] == 'applied'
        source_context = engine._source_context(current)
        assert source_context['attachment_sources'][0]['text'] == text
        assert current['acceptance'] == task['acceptance']
