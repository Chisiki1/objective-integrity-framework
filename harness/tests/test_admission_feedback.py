"""Actual Engine/SQLite/file consumers with explicit synthetic model responses.

These fixtures test structural feedback and provenance, not model judgment
quality. Historical setup is named and uses an actual captured request/event;
no corrective operation, permit, Web result or Knowledge application is forged.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

import policy_harness.engine as engine_module
from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import (ConfigurationRequired, EngineeringAdmission,
    EngineeringInvestigation, EngineeringScenarios, EngineeringScope, PolicyError, TaskPlan, LEARNING_SCHEMAS)
from policy_harness.policy import PolicyCatalog
from policy_harness.policy_admission import (AdmissionContractError, _binding, _judgment,
    _read, _record, admission_anchors, admission_input, admission_model_input, validate_engineering)
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, FixtureWeb, POLICY
from tests.test_engine_recovery import journal
from tests.test_policy_admission import ADMISSION_SCHEMAS, fixture_engineering_response


class FeedbackGateway(FixtureGateway):
    def __init__(self, policy, *, fault=EngineeringScope, repeat=False, pause=False,
                 on_feedback=None, on_rejection=None):
        super().__init__(policy)
        self.fault, self.repeat, self.pause = fault, repeat, pause
        self.on_feedback = on_feedback
        self.on_rejection = on_rejection
        self.rejected = False

    async def generate(self, role, phase, payload, schema, **kwargs):
        if schema in LEARNING_SCHEMAS and getattr(self, 'settings', None) is not None:
            from tests.test_bounded_judgments import PackedFixtureGateway
            self.calls.append((role, phase, deepcopy(payload)))
            packed = PackedFixtureGateway(self.policy)
            packed.settings, packed.response_store = self.settings, self.response_store
            return await packed.generate(role, phase, payload, schema, **kwargs)
        if schema in ADMISSION_SCHEMAS:
            self.calls.append((role, phase, deepcopy(payload)))
            if payload.get('actual_format_feedback'):
                if self.on_feedback:
                    self.on_feedback(self, payload)
                if self.pause:
                    raise ConfigurationRequired('Fixture pause after receiving the exact corrective input')
            value = fixture_engineering_response(payload, schema, store=self.response_store)
            # Derive the extra independent preparation through the real scope
            # response; no preparation record or protected operation is inserted.
            if schema is EngineeringScope and self.fault in {EngineeringScenarios, EngineeringInvestigation}:
                value = value.model_copy(update={'engineering': True,
                    'correction': self.fault is EngineeringInvestigation})
            if schema is self.fault and (not self.rejected or self.repeat):
                self.rejected = True
                data = value.model_dump()
                if schema is EngineeringScope:
                    data['source_refs'] = ['initial-source-hash-is-not-an-evidence-id']
                elif schema is EngineeringScenarios:
                    data['scenarios'].append(deepcopy(data['scenarios'][0]))
                elif schema is EngineeringInvestigation:
                    data['checks'] = data['checks'][:-1]
                else:
                    data['opinions'][0]['evidence_refs'] = ['foreign-opinion-source']
                value = schema.model_validate(data)
                if self.on_rejection:
                    await self.on_rejection(self, payload)
            usage = {'fixture': True, 'usage': None, 'cost': None}
        else:
            value, usage = await super().generate(role, phase, payload, schema)
            if schema is TaskPlan:
                context = payload['source_context']
                value = value.model_copy(update={'source_hash': context['source_hash'],
                    'source_dispositions': [{'source_id': identity, 'classification': 'clarify',
                        'reason': 'Retain the actual additional source without changing the original output'}
                        for identity in context['pending_ids']],
                    'acceptance_dispositions': [{'index': item['index'], 'old_hash': item['hash'],
                        'disposition': 'retain', 'criterion': item['criterion'],
                        'reason': 'Keep the original criterion from this exact input'}
                        for item in context['prior_acceptance']]})
        if getattr(self, 'settings', None) is not None:
            from tests.fixture_response_transport import retained_reply
            return await retained_reply(self, role, phase, payload, schema, value, usage=usage, **kwargs)
        return value, usage


class CountingWeb(FixtureWeb):
    def __init__(self):
        self.requests = []

    async def collect(self, query, **kwargs):
        self.requests.append((query, kwargs['phase'], kwargs['operation_id']))
        return await super().collect(query, **kwargs)


def open_runtime(directory, *, legacy_policy=False, **options):
    store = Store(directory)
    policy = PolicyCatalog(POLICY, include_role_supplement=not legacy_policy)
    gateway = FeedbackGateway(policy, **options)
    executor = Executor(directory)
    web = CountingWeb()
    engine = Engine(store, policy, executor, gateway, web, Knowledge(store))
    return store, engine, executor, gateway, web


def create_task(store):
    return store.create_task('Write hello in answer.txt and read the exact bytes back',
                             ['answer.txt contains hello', 'Read back the actual output'])


async def run(engine, task_id):
    return await asyncio.wait_for(engine.run_task(task_id), timeout=120)


def assert_delivered(result, task, executor):
    assert result['task']['status'] == 'completed', result['events'][-3:]
    assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
    receipts = journal(executor)
    assert sorted(r['operation']['kind'] for r in receipts) == ['file_read', 'file_write']
    assert result['task']['final']['completion']['achieved'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [EngineeringScope, EngineeringScenarios,
                                   EngineeringInvestigation, EngineeringAdmission])
async def test_normal_engine_corrects_structural_admission_once_then_delivers(tmp_path, schema):
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', fault=schema)
    task = create_task(store)
    try:
        result = await run(engine, task['id'])
        assert_delivered(result, task, executor)
        rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output'
                    and e['stage'].startswith('policy_admission_')]
        assert len(rejected) == 1
        phase = rejected[0]['stage']
        call = store.record_get('bounded_model_call', rejected[0]['detail']['admission_call_id'])
        original = admission_model_input(store.get_task(task['id']), engine.policy.hash, phase, call['payload'],
            policy_input_contract=call.get('admission_trace',{}).get('actual_model_input',{}).get('policy_input_contract'))
        corrected = call['admission_trace']['actual_model_input']
        inputs = [p for _, name, p in gateway.calls if name == phase
                  and digest(p) in {digest(original), digest(corrected)}]
        assert inputs == [original, corrected]
        assert corrected['actual_format_feedback']['rejected_response'] == rejected[0]['detail']['rejected_response']
        assert all(p['evidence_ids'] == sorted(p['evidence']) for p in inputs)
        rows = store.records('policy_admission_judgment')
        assert rows
        for row in rows:
            ref = {'kind': 'policy_admission_judgment', 'id': row['id'], 'sha256': digest(row)}
            _judgment(store, ref, _binding(store, task['id'], engine.policy.hash))
        trace = call['admission_trace']
        event = next(e for e in store.events(task['id']) if e['seq'] == trace['event']['seq'])
        assert event['detail']['admission_call_id'] == call['id']
        assert event['detail']['admission_actual_input_sha256'] == digest(trace['actual_model_input'])
        assert event['detail']['admission_actual_input_sha256'] != call['measurement']['model_input_sha256']
        assert store.verify_events()
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('pause', [False, True])
async def test_repeated_defect_or_paused_feedback_does_not_publish_or_execute(tmp_path, pause):
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', repeat=not pause, pause=pause)
    task = create_task(store)
    try:
        result = await run(engine, task['id'])
        assert result['task']['status'] in {'attention_required', 'configuration_required'}
        assert not (Path(task['workspace']) / 'answer.txt').exists()
        assert journal(executor) == []
        assert store.records('policy_admission_judgment') == []
        calls = [c for c in store.records('bounded_model_call') if c['phase'] == 'policy_admission_scope']
        assert len(calls) == 1 and calls[0]['status'] == 'failed'
        assert len([p for _, phase, p in gateway.calls if phase == 'policy_admission_scope']) == 2
        assert not any(c['phase'] == 'policy_admission_scope' for c in store.records('bounded_model_completed'))
    finally:
        await engine.close()
        store.close()


class CapturedOriginal(Exception):
    pass


async def historical_scope(store, engine, gateway, task, monkeypatch, *, indexed):
    """Explicit old-boundary fixture: a schema-valid response was saved before
    contextual reference validation. The model input and event are real Engine
    outputs; only the historical persistence shape is constructed here.
    """
    assert not engine.policy.role_scoped_enabled
    captured = {}
    async def capture(task_id, name, payload, schema):
        captured.update(name=name, payload=deepcopy(payload), schema=schema)
        raise CapturedOriginal()
    with monkeypatch.context() as patch:
        patch.setattr(engine.policy_admission, '_call', capture)
        with pytest.raises(CapturedOriginal):
            await engine.policy_admission.prepare(task['id'])
    phase = 'policy_admission_' + captured['name']
    payload, schema = captured['payload'], captured['schema']
    measured = engine.bounded_judgments.measure(task['id'], phase, payload, schema, role='reviewer')
    # Reproduce the pre-repair omission only while recording this old response.
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, 'validate_engineering', lambda *a, **kw: None)
        value = await engine._call(task['id'], phase, payload, schema, role='reviewer')
    event = store.events(task['id'])[-1]
    assert event['stage'] == phase and event['status'] == 'succeeded'
    assert value.source_refs == ['initial-source-hash-is-not-an-evidence-id']
    key = engine.bounded_judgments._receipt_key(store.get_task(task['id']), phase, payload, schema, 'reviewer', measured)
    call = {'id': uuid4().hex, 'key': key, 'task_id': task['id'], 'phase': phase,
        'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash,
        'payload': payload, 'measurement': measured, 'status': 'succeeded', 'result': value.model_dump()}
    store.record('bounded_model_call', call['id'], call)
    store.record('bounded_model_completed', key, call)
    original_index = None
    if indexed:
        binding = _binding(store, task['id'], engine.policy.hash)
        ref = _record(store, 'policy_admission_judgment', {'binding': binding, 'phase': phase,
            'schema': schema.__name__, 'original_input': payload, 'payload_sha256': digest(payload),
            'result': value.model_dump(), 'call_id': call['id'],
            'event': {'seq': event['seq'], 'hash': event['hash']}})
        index_key = digest({'binding': binding, 'phase': phase, 'payload': payload, 'schema': schema.__name__})
        original_index = (index_key, {'reference': ref})
        store.record('policy_admission_call_index', *original_index)
    return deepcopy(call), deepcopy(event), original_index


async def historical_compact_scope(store, engine, gateway, task, monkeypatch, *, indexed, invalid):
    """Reproduce candidate14's configured-capacity compact writer explicitly.

    The full packet and context pages are actual BoundedJudgments outputs, and
    the response event is an actual Engine output. Only the old omission of
    structural validation/anchors and its old persistence shape are emulated.
    """
    assert not engine.policy.role_scoped_enabled
    from tests.test_bounded_judgments import PublicFixtureSettings
    gateway.settings = PublicFixtureSettings()
    content = ('Retained original workspace facts; no execution authority.\n' * 800).encode()
    (Path(task['workspace']) / 'reference.txt').write_bytes(content)
    captured = {}
    async def capture(task_id, name, payload, schema):
        captured.update(name=name, payload=deepcopy(payload), schema=schema)
        raise CapturedOriginal()
    with monkeypatch.context() as patch:
        patch.setattr(engine.policy_admission, '_call', capture)
        with pytest.raises(CapturedOriginal):
            await engine.policy_admission.prepare(task['id'])
    original, schema = captured['payload'], captured['schema']
    phase = 'policy_admission_' + captured['name']
    base = {'instruction': original['instruction'], 'evidence_ids': sorted(original['evidence']),
            'work_items': original.get('work_items', [])}
    bounded = engine.bounded_judgments
    full = bounded.measure(task['id'], phase, original, schema, role='reviewer')
    empty = bounded.measure(task['id'], phase, dict(base, bounded_context={}), schema, role='reviewer')
    assert full['input_tokens_estimate'] > empty['input_tokens_estimate'] + 2000
    gateway.settings.values['model_context_tokens'] = (empty['input_tokens_estimate'] +
        empty['reserved_output_tokens'] + (full['input_tokens_estimate'] - empty['input_tokens_estimate']) // 2)
    assert not bounded._fits(task['id'], phase, original, schema, 'reviewer')
    context = await bounded.compact_context(task['id'], phase, original, base, schema, role='reviewer')
    assert context['packet_id'] and context['actual_source_fragment_count'] > 0
    payload = dict(base, bounded_context=context)
    measured = bounded.measure(task['id'], phase, payload, schema, role='reviewer')
    assert measured['fits'] and 'configuration' in measured
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, 'admission_anchors', lambda *a, **kw: None)
        patch.setattr(engine_module, 'validate_engineering', lambda *a, **kw: None)
        value = await engine._call(task['id'], phase, payload, schema, role='reviewer')
    event = store.events(task['id'])[-1]
    assert event['stage'] == phase and event['status'] == 'succeeded'
    assert value.source_refs == (['initial-source-hash-is-not-an-evidence-id'] if invalid else ['source'])
    key = bounded._receipt_key(store.get_task(task['id']), phase, payload, schema, 'reviewer', measured)
    call = {'id': uuid4().hex, 'key': key, 'task_id': task['id'], 'phase': phase,
        'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash,
        'payload': payload, 'measurement': measured, 'status': 'succeeded', 'result': value.model_dump()}
    store.record('bounded_model_call', call['id'], call)
    store.record('bounded_model_completed', key, call)
    index = None
    if indexed:
        binding = _binding(store, task['id'], engine.policy.hash)
        ref = _record(store, 'policy_admission_judgment', {'binding': binding, 'phase': phase,
            'schema': schema.__name__, 'original_input': original, 'payload_sha256': digest(payload),
            'result': value.model_dump(), 'call_id': call['id'],
            'event': {'seq': event['seq'], 'hash': event['hash']}})
        index = (digest({'binding': binding, 'phase': phase, 'payload': original, 'schema': schema.__name__}),
                 {'reference': ref})
        store.record('policy_admission_call_index', *index)
    return deepcopy({'call': call, 'event': event, 'index': index, 'original': original,
        'packet': store.record_get('bounded_input', context['packet_id']),
        'context': store.record_get('bounded_context', context['packet_id']),
        'events': store.events(task['id']), 'calls': store.records('bounded_model_call'),
        'settings': gateway.settings.public(), 'content': content})


def assert_historical_compact_unchanged(store, old):
    call = old['call']
    assert store.record_get('bounded_model_call', call['id']) == call
    assert store.record_get('bounded_model_completed', call['key']) == call
    assert store.record_get('bounded_input', old['packet']['id']) == old['packet']
    assert store.record_get('bounded_context', old['context']['id']) == old['context']
    assert store.events(call['task_id'])[:len(old['events'])] == old['events']
    for observed in old['calls']:
        assert store.record_get('bounded_model_call', observed['id']) == observed
    if old['index']:
        assert store.record_get('policy_admission_call_index', old['index'][0]) == old['index'][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('indexed', [False, True], ids=['bounded-only', 'indexed'])
@pytest.mark.parametrize('invalid', [False, True], ids=['valid', 'invalid'])
async def test_historical_compact_reopens_preparation_and_actual_permit_without_replay(tmp_path, monkeypatch, indexed, invalid):
    from tests.test_bounded_judgments import PublicFixtureSettings
    directory = tmp_path / 'data'
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True, fault=EngineeringScope if invalid else None)
    task = create_task(store)
    old = await historical_compact_scope(store, engine, gateway, task, monkeypatch, indexed=indexed, invalid=invalid)
    await engine.close()
    store.close()
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True, fault=None)
    gateway.settings = PublicFixtureSettings()
    gateway.settings.values = deepcopy(old['settings'])
    try:
        ref = await engine.policy_admission.prepare(task['id'])
        assert len(gateway.calls) == int(invalid), gateway.calls
        assert not any(':context:' in phase for _, phase, _ in gateway.calls)
        prep = _read(store, ref, 'policy_admission_preparation')
        accepted = _judgment(store, prep['scope'], _binding(store, task['id'], engine.policy.hash))
        assert accepted['original_input'] == old['original']
        assert accepted['result']['source_refs'] == ['source']
        terminal = store.record_get('bounded_model_call', accepted['call_id'])
        if invalid:
            _, phase, corrected = gateway.calls[0]
            assert phase == old['call']['phase']
            assert corrected['bounded_context'] == old['call']['payload']['bounded_context']
            assert corrected['admission_contract'] == admission_anchors(old['original'])
            assert corrected['evidence_ids'] == sorted(old['original']['evidence'])
            feedback = corrected['actual_format_feedback']
            assert feedback['rejected_response'] == old['call']['result']
            assert feedback['validation']['kind'] == 'proposal_contract'
            if indexed:
                assert feedback['source_judgment'] == old['index'][1]['reference']
                assert store.record_get('policy_admission_correction_index', old['index'][0])['reference'] == prep['scope']
            else:
                assert feedback['source_receipt'] == terminal['admission_parent'] == {
                    'id': old['call']['id'], 'key': old['call']['key'], 'sha256': digest(old['call'])}
            assert terminal['id'] != old['call']['id']
        else:
            assert terminal == old['call']
        assert_historical_compact_unchanged(store, old)
        before = deepcopy(gateway.calls)
        assert await engine.policy_admission.prepare(task['id']) == ref
        assert gateway.calls == before
        assert journal(executor) == [] and web.requests == []
        assert store.records('knowledge_application') == []
        # The normal proposal/review reaches issue_permit and consume_permit
        # inside _execute; no operation or admission record is inserted here.
        store.update_task(task['id'], state={'plan': {'objective': task['objective']}})
        selected = await engine._select({'task_id': task['id']})
        state = {'task_id': task['id'], **selected}
        await engine._pre(state)
        await engine._execute(state)
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        assert [r['operation']['kind'] for r in journal(executor)] == ['file_write']
        assert not any(phase.startswith('policy_admission_scope:context:') for _, phase, _ in gateway.calls)
        assert_historical_compact_unchanged(store, old)
        assert (Path(task['workspace']) / 'reference.txt').read_bytes() == old['content']
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['packet', 'context', 'ambiguous-event', 'settings'])
async def test_historical_compact_provenance_failure_holds_before_new_context_or_feedback(tmp_path, monkeypatch, fault):
    from tests.test_bounded_judgments import PublicFixtureSettings
    directory = tmp_path / 'data'
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True)
    task = create_task(store)
    old = await historical_compact_scope(store, engine, gateway, task, monkeypatch, indexed=False, invalid=True)
    await engine.close()
    store.close()
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True, fault=None)
    gateway.settings = PublicFixtureSettings()
    gateway.settings.values = deepcopy(old['settings'])
    try:
        if fault == 'packet':
            changed = deepcopy(old['packet'])
            changed['task_id'] = 'foreign-task'
            store.record('bounded_input', changed['id'], changed)
        elif fault == 'context':
            changed = deepcopy(old['context'])
            changed['source_records'] = []
            store.record('bounded_context', changed['id'], changed)
        elif fault == 'ambiguous-event':
            store.event(task['id'], old['call']['phase'], 'succeeded', deepcopy(old['event']['detail']))
        else:
            gateway.settings.values['max_output_tokens'] += 1
        snapshots = {kind: deepcopy(store.records(kind)) for kind in (
            'bounded_input', 'bounded_context', 'bounded_model_call', 'bounded_model_completed',
            'policy_admission_call_index', 'policy_admission_correction_index')}
        with pytest.raises(PolicyError, match='ADMISSION_.*PROVENANCE|Current reviewer settings'):
            await engine.policy_admission.prepare(task['id'])
        assert gateway.calls == [] and web.requests == [] and journal(executor) == []
        assert store.operations(task['id']) == []
        assert store.records('knowledge_application') == []
        assert {kind: store.records(kind) for kind in snapshots} == snapshots
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('indexed', [False, True])
async def test_saved_invalid_response_reopens_through_exact_feedback_without_replay(tmp_path, monkeypatch, indexed):
    directory = tmp_path / 'data'
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True)
    task = create_task(store)
    old, event, index = await historical_scope(store, engine, gateway, task, monkeypatch, indexed=indexed)
    await engine.close()
    store.close()
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True, fault=None)
    try:
        ref = await engine.policy_admission.prepare(task['id'])
        assert store.record_get('bounded_model_call', old['id']) == old
        assert store.record_get('bounded_model_completed', old['key']) == old
        assert next(e for e in store.events(task['id']) if e['seq'] == event['seq']) == event
        if index:
            assert store.record_get('policy_admission_call_index', index[0]) == index[1]
            recovery = store.record_get('policy_admission_correction_index', index[0])
            assert recovery['original'] == index[1]['reference']
        calls = [p for _, phase, p in gateway.calls if phase == 'policy_admission_scope']
        assert len(calls) == 1
        assert 'actual_format_feedback' in calls[0]
        assert calls[0]['evidence_ids'] == sorted(old['payload']['evidence'])
        prep = _read(store, ref, 'policy_admission_preparation')
        accepted = _judgment(store, prep['scope'], _binding(store, task['id'], engine.policy.hash))
        terminal = store.record_get('bounded_model_call', accepted['call_id'])
        assert terminal['id'] != old['id']
        assert terminal['payload'] != old['payload']
        assert accepted['original_input'] == old['payload']
        if not indexed:
            assert terminal['admission_parent']['id'] == old['id']
            assert accepted['request_key'] == old['key']
        counts = (len(gateway.calls), len(web.requests), len(store.records('knowledge_application')))
        assert await engine.policy_admission.prepare(task['id']) == ref
        assert counts == (len(gateway.calls), len(web.requests), len(store.records('knowledge_application')))
        result = await run(engine, task['id'])
        assert_delivered(result, task, executor)
        assert store.record_get('bounded_model_call', old['id']) == old
        assert store.verify_events()
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing_call', 'foreign_payload', 'foreign_role', 'actual_feedback', 'missing_original'])
async def test_corrupt_or_foreign_provenance_never_becomes_format_retry(tmp_path, monkeypatch, corruption):
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', legacy_policy=True)
    task = create_task(store)
    try:
        old, event, index = await historical_scope(store, engine, gateway, task, monkeypatch, indexed=True)
        changed = deepcopy(old)
        if corruption == 'missing_call':
            store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('bounded_model_call', old['id']))
        elif corruption == 'foreign_payload':
            changed['payload']['evidence']['source']['value'] = {'foreign': True}
            store.record('bounded_model_call', old['id'], changed)
        elif corruption == 'foreign_role':
            changed['key'] = engine.bounded_judgments._receipt_key(store.get_task(task['id']), old['phase'],
                old['payload'], EngineeringScope, 'author', old['measurement'])
            store.record('bounded_model_call', old['id'], changed)
        elif corruption == 'actual_feedback':
            changed['admission_trace'] = {'call_id': old['id'], 'event': {'seq': event['seq'], 'hash': event['hash']},
                                          'actual_model_input': {'forged': True}}
            store.record('bounded_model_call', old['id'], changed)
        else:
            original = _read(store, index[1]['reference'], 'policy_admission_judgment')
            original.pop('original_input')
            original.pop('id')
            bad_ref = _record(store, 'policy_admission_judgment', original)
            store.record('policy_admission_call_index', index[0], {'reference': bad_ref})
        before = len(gateway.calls)
        with pytest.raises(PolicyError, match='ADMISSION_'):
            await engine.policy_admission.prepare(task['id'])
        assert len(gateway.calls) == before
        assert journal(executor) == []
        assert not store.records('policy_admission_correction_index')
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_corrected_input_tamper_is_rejected_at_the_real_permit_boundary(tmp_path):
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', fault=EngineeringAdmission)
    task = create_task(store)
    try:
        # An ordinary reviewed plan is fixture setup. Operation selection,
        # proposal, review, permit and actual write remain normal consumers.
        store.update_task(task['id'], state={'plan': {'objective': task['objective']}})
        selected = await engine._select({'task_id': task['id']})
        state = {'task_id': task['id'], **selected}
        await engine._pre(state)
        row = store.get_operation(state['operation_id'])
        args = {'task_id': task['id'], 'operation_id': row['operation']['id'], 'action_hash': digest(row['operation']),
                'policy_hash': engine.policy.hash, 'bundle_hash': digest(row['pre_bundle'])}
        token = store.issue_permit(**args)
        receipt = _read(store, row['pre_bundle']['policy_admission']['receipt'], 'policy_admission_receipt')
        judgment = _read(store, receipt['judgment'], 'policy_admission_judgment')
        call = store.record_get('bounded_model_call', judgment['call_id'])
        changed = deepcopy(call)
        changed['admission_trace']['actual_model_input']['actual_format_feedback']['validation']['message'] = 'tampered'
        store.record('bounded_model_call', call['id'], changed)
        with pytest.raises(PolicyError, match='ADMISSION_CALL_PROVENANCE'):
            store.issue_permit(**args)
        with pytest.raises(PolicyError, match='ADMISSION_CALL_PROVENANCE'):
            store.consume_permit(token, **args)
        assert journal(executor) == []
        assert store.get_operation(row['operation']['id'])['result'] is None
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_compact_admission_keeps_exact_namespace_work_and_dependency_anchors(tmp_path, monkeypatch):
    from tests.test_bounded_judgments import PublicFixtureSettings
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', legacy_policy=True, fault=EngineeringInvestigation)
    gateway.settings = PublicFixtureSettings()
    task = create_task(store)
    large = Path(task['workspace']) / 'reference.txt'
    content = ('Complete retained reference facts; no execution or authority is implied.\n' * 800).encode()
    large.write_bytes(content)
    captured = {}
    async def capture(task_id, name, payload, schema):
        captured.update(payload=deepcopy(payload), schema=schema)
        raise CapturedOriginal()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(engine.policy_admission, '_call', capture)
            with pytest.raises(CapturedOriginal):
                await engine.policy_admission.prepare(task['id'])
        original = captured['payload']
        phase = 'policy_admission_scope'
        base = {'instruction': original['instruction'], 'evidence_ids': sorted(original['evidence']),
                'work_items': [], 'admission_contract': admission_anchors(original)}
        full = engine.bounded_judgments.measure(task['id'], phase, admission_input(original), EngineeringScope, role='reviewer')
        empty = engine.bounded_judgments.measure(task['id'], phase, dict(base, bounded_context={}), EngineeringScope, role='reviewer')
        assert full['input_tokens_estimate'] > empty['input_tokens_estimate'] + 2000
        gateway.settings.values['model_context_tokens'] = (empty['input_tokens_estimate'] +
            empty['reserved_output_tokens'] + (full['input_tokens_estimate'] - empty['input_tokens_estimate']) // 2)
        store.update_task(task['id'], state={'plan': {'objective': task['objective']}})
        selected = await engine._select({'task_id': task['id']})
        state = {'task_id': task['id'], **selected}
        await engine._pre(state)
        inputs = [(name, p) for _, name, p in gateway.calls if name in {
            'policy_admission_scope', 'policy_admission_blind_scenarios',
            'policy_admission_blind_investigation', 'policy_admission_applicability'}]
        assert {name for name, _ in inputs} == {
            'policy_admission_scope', 'policy_admission_blind_scenarios',
            'policy_admission_blind_investigation', 'policy_admission_applicability'}
        assert all('evidence' not in payload for _, payload in inputs)
        for name, payload in inputs:
            packet = store.record_get('bounded_input', payload['bounded_context']['packet_id'])
            actual = packet['value']
            assert payload['evidence_ids'] == sorted(actual['evidence'])
            assert payload['admission_contract'] == admission_anchors(actual)
            assert payload['work_items'] == actual.get('work_items', [])
        sweep = [p for name, p in inputs if name == 'policy_admission_blind_investigation']
        assert len(sweep) == 2 and len(sweep[0]['work_items']) == 2
        assert 'actual_format_feedback' in sweep[1]
        applicability = next(p for name, p in inputs if name == 'policy_admission_applicability')
        assert applicability['admission_contract']['prerequisite_ids'] == ['check:0', 'check:1']
        assert applicability['admission_contract']['finding_ids'] == ['empty-input']
        await engine._execute(state)
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        assert [r['operation']['kind'] for r in journal(executor)] == ['file_write']
        assert large.read_bytes() == content
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['settings', 'source'])
async def test_saved_invalid_admission_does_not_recover_under_changed_identity(tmp_path, monkeypatch, change):
    from tests.test_bounded_judgments import PublicFixtureSettings
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', legacy_policy=True)
    gateway.settings = PublicFixtureSettings()
    task = create_task(store)
    try:
        old, event, index = await historical_scope(store, engine, gateway, task, monkeypatch, indexed=True)
        # This exact reference is what a stored preparation/permit consumer sees.
        original = index[1]['reference']
        if change == 'settings':
            gateway.settings.values['max_output_tokens'] += 1
        elif change == 'source':
            store.append_instruction(task['id'], 'Retain the output and also preserve source identity', task['source_hash'])
        before = len(gateway.calls)
        if change == 'settings':
            with pytest.raises(PolicyError, match='settings|measured input'):
                await engine.policy_admission.prepare(task['id'])
        else:
            with pytest.raises(PolicyError, match='ADMISSION_FOREIGN'):
                _judgment(store, original, _binding(store, task['id'], engine.policy.hash))
        assert len(gateway.calls) == before
        assert store.record_get('bounded_model_call', old['id']) == old
        assert not store.records('policy_admission_correction_index')
        assert journal(executor) == []
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_saved_defect_repeat_is_held_after_one_corrective_send(tmp_path, monkeypatch):
    directory = tmp_path / 'data'
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True)
    task = create_task(store)
    old, event, index = await historical_scope(store, engine, gateway, task, monkeypatch, indexed=True)
    await engine.close()
    store.close()
    store, engine, executor, gateway, web = open_runtime(directory, legacy_policy=True, repeat=True)
    try:
        with pytest.raises(PolicyError, match='repeated the same proposal-contract defect'):
            await engine.policy_admission.prepare(task['id'])
        assert len(gateway.calls) == 1
        assert 'actual_format_feedback' in gateway.calls[0][2]
        assert store.record_get('bounded_model_call', old['id']) == old
        assert store.record_get('policy_admission_call_index', index[0]) == index[1]
        assert not store.records('policy_admission_correction_index')
        assert journal(executor) == []
    finally:
        await engine.close()
        store.close()


@pytest.mark.asyncio
async def test_actual_stop_after_rejected_output_prevents_corrective_send(tmp_path):
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data')
    task = create_task(store)
    async def stop_after_response(gateway, payload):
        await engine.stop_task(task['id'])
    gateway.on_rejection = stop_after_response
    try:
        with pytest.raises(asyncio.CancelledError):
            await engine.policy_admission.prepare(task['id'])
        assert store.get_task(task['id'])['status'] == 'stopped'
        assert len(gateway.calls) == 1
        assert any(e['status'] == 'stop_requested' for e in store.events(task['id']))
        assert any(e['status'] == 'rejected_model_output' for e in store.events(task['id']))
        calls = [c for c in store.records('bounded_model_call') if c['phase'] == 'policy_admission_scope']
        assert len(calls) == 1 and calls[0]['status'] == 'unobserved'
        assert not store.records('policy_admission_judgment')
        assert journal(executor) == [] and web.requests == []
    finally:
        await engine.close()
        store.close()
