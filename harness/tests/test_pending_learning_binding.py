"""W6A-01 continuation consumers with real protected storage and synthetic wires.

These tests have no live API/Docker dependency. Controller/Store/Knowledge and
ModelGateway response retention are real; semantic judgments and HTTP are fixtures.
"""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from policy_harness.models import (ConfigurationRequired, Disposition, Learning, OperationResult, PolicyError,
    LEARNING_SCHEMAS, LearningIdeas, LearningApplications, LearningSynthesis)
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.store import digest
from tests.test_core import FixtureGateway, runtime
from tests.test_knowledge import learning, operation
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_providers import FakeSettings, envelope
from tests.test_response_recovery import RecoverableCipher
from tests.test_web_recovery import collector, operation as web_operation
from tests.fixture_preparation import prepare_operation


WITHDRAWAL = 'WITHDRAW_PENDING_CANDIDATE: do not adopt the earlier response candidate.'
PENDING_IDEA = {
    'id': 'pending-original-candidate',
    'target': 'task',
    'proposal': 'Consider retaining the observed failure as an example for later work.',
    'disposition': 'investigate',
    'rationale': 'The original source has not decided whether to adopt this candidate.',
}


class ProtectedLearningGateway(FixtureGateway):
    """Route only the tested Learning phase through the real wire/response reader."""

    def __init__(self, store, policy, wire_calls, cipher, control, *, phase):
        super().__init__(policy)
        self.wire_calls = wire_calls
        self.control = control
        self.phase = phase
        self.current_schema = Learning
        self.extractions = []
        self.settings = FakeSettings(model_context_tokens=2_000_000, max_output_tokens=262144)
        self.settings.redaction_secrets = lambda: (
            tuple(self.settings.keys.values()),
            ('history:fixture',) if control.get('history_unavailable') else (),
        )

        def transport(request):
            body = json.loads(request.content)
            payload = json.loads(body['messages'][1]['content'].split('\n', 1)[1])
            wire_calls.append(deepcopy(payload))
            value = learning(ideas=deepcopy(payload.get('required_ideas', []))).model_dump()
            if len(wire_calls) == 1:
                # A valid independent Idea in a genuinely schema-invalid response.
                value['ideas'] = [deepcopy(PENDING_IDEA), {'id': 'invalid-shape'}]
                value['classifications'] = ['unsupported-classification']
            elif WITHDRAWAL in json.dumps(payload.get('source_context', {})):
                for idea in value['ideas']:
                    if idea['id'] == PENDING_IDEA['id']:
                        idea.update(disposition='reject', rationale=WITHDRAWAL)
            if self.current_schema is LearningIdeas:
                value = {'ideas': value['ideas']}
            elif self.current_schema is LearningApplications:
                value = {'considered_skill_ids': [s['id'] for s in payload['pre']['skills']['selected']],
                         'applications': [], 'ideas': value['ideas']}
            elif self.current_schema is LearningSynthesis:
                value = {k: v for k, v in value.items() if k != 'applications'}
            return httpx.Response(200, json=envelope(json.dumps(value)))

        self.provider = ModelGateway(
            self.settings, policy, transport=httpx.MockTransport(transport),
            response_store=store, response_protector=cipher,
        )

    async def generate(self, role, phase, payload, schema, **kwargs):
        if schema in LEARNING_SCHEMAS and phase.split(':page:', 1)[0] == self.phase:
            self.current_schema = schema
            try:
                return await self.provider.generate(role, phase, payload, schema, **kwargs)
            except ProviderError:
                if self.control.get('fault') == 'history' and len(self.wire_calls) == 1:
                    self.control['history_unavailable'] = True
                raise
        value,usage=await super().generate(role, phase, payload, schema)
        if schema is Disposition:
            from tests.fixture_response_transport import retained_reply
            return await retained_reply(self,role,phase,payload,schema,value,usage=usage)
        return value,usage

    def learning_response_ideas(self, reference, **binding):
        self.extractions.append({'reference': deepcopy(reference), 'binding': dict(binding)})
        return self.provider.learning_response_ideas(reference, **binding)


def attach_gateway(store, engine, wire_calls, cipher, control, phase):
    gateway = ProtectedLearningGateway(store, engine.policy, wire_calls, cipher, control, phase=phase)
    engine.gateway = gateway
    gateway.response_store = store
    return gateway


def pending_original(store, task_id):
    matches = [e for e in store.events(task_id)
               if e['status'] == 'rejected_model_output' and e['detail'].get('idea_extraction_pending')]
    assert len(matches) == 1
    event = deepcopy(matches[0])
    reference = event['detail']['metadata']['response_record']
    record = deepcopy(store.record_get('model_response', reference['id']))
    assert record and record['task_id'] == task_id
    assert PENDING_IDEA['id'] not in {i['id'] for i in event['detail']['retained_ideas']}
    return event, record


def assert_original_unchanged(store, task_id, event, response):
    actual = next(e for e in store.events(task_id) if e['seq'] == event['seq'])
    assert actual == event
    assert store.record_get('model_response', response['id']) == response
    assert store.verify_events()


def assert_candidate(ideas, *, withdrawn=False):
    found = [i for i in ideas if i['id'] == PENDING_IDEA['id']]
    assert len(found) == 1
    assert {k: found[0][k] for k in ('id', 'target', 'proposal')} == {
        k: PENDING_IDEA[k] for k in ('id', 'target', 'proposal')}
    if withdrawn:
        assert found[0]['disposition'] == 'reject'
        assert found[0]['rationale'] == WITHDRAWAL


def change_binding(store, engine, task_id, change, monkeypatch):
    if change == 'source':
        task = store.get_task(task_id)
        store.append_instruction(task_id, WITHDRAWAL, task['source_hash'])
    elif change == 'policy':
        # Existing approved policy bytes remain untouched; only this test identity changes.
        monkeypatch.setattr(engine.policy, 'hash', digest({'fixture_policy': 'changed-after-response'}))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'policy', 'payload'])
async def test_normal_post_recovers_pending_original_after_binding_change(tmp_path, monkeypatch, change):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Write exact hello', ['answer.txt has the requested five bytes'])
    op = operation()
    state = await prepare_operation(engine, task, op)
    await engine._pre(state)
    await engine._execute(state)
    original_result = deepcopy(store.get_operation(op['id'])['result'])
    wire_calls = []
    cipher = RecoverableCipher()
    cipher.unavailable = True
    control = {'fault': 'protection'}
    attach_gateway(store, engine, wire_calls, cipher, control, 'learning_proposal')

    with pytest.raises(ConfigurationRequired):
        await engine._post(state)
    event, response = pending_original(store, task['id'])
    assert len(wire_calls) == 1 and executor.calls == [op]
    original_binding = {k: response[k] for k in ('task_id', 'role', 'phase', 'policy_hash', 'source_hash')}
    await engine.close()
    store.close()
    store, engine = reopen(store, engine, executor)
    change_binding(store, engine, task['id'], change, monkeypatch)
    if change == 'payload':
        # A newly observed post draft changes the real next _post input.
        store.update_operation(op['id'], post_draft={'new_observation': 'The resumed result requires this additional comparison.'})
    cipher.unavailable = False
    gateway = attach_gateway(store, engine, wire_calls, cipher, control, 'learning_proposal')

    await engine._post(state)
    current = store.get_operation(op['id'])
    assert current['status'] == 'post_reviewed' and current['result'] == original_result
    assert len(wire_calls) == 2 and wire_calls[0] != wire_calls[1]
    if change == 'policy':
        assert wire_calls[1]['policy_hash'] != response['policy_hash']
    elif change == 'payload':
        assert wire_calls[1]['policy_hash'] == response['policy_hash']
        assert store.get_task(task['id'])['source_hash'] == response['source_hash']
        # The current projection may place this fact differently, but its full
        # meaning must reach the actual next assessment, not just an archive ID.
        observation = 'The resumed result requires this additional comparison.'
        assert any(observation in json.dumps(p, ensure_ascii=False)
                   for _, phase, p in gateway.calls if phase == 'post_assessment')
    assert_candidate(wire_calls[1]['required_ideas'])
    assert_candidate(current['post_bundle']['learning']['ideas'], withdrawn=change == 'source')
    assert gateway.extractions == [{'reference': event['detail']['metadata']['response_record'],
                                    'binding': original_binding}]
    history = wire_calls[1]['historical_response_evidence']['originals']
    assert history[0]['source_event'] == {'seq': event['seq'], 'hash': event['hash']}
    assert history[0]['original_binding'] == original_binding
    assert len(store.records('learning_response_recovery')) == 1
    assert_original_unchanged(store, task['id'], event, response)

    # Consume the recovered candidate in real Knowledge, then re-enter the normal caller.
    await engine._learn(state)
    application = store.record_get('knowledge_application', task['id'] + ':' + op['id'])
    assert application is not None
    episode = store.record_get('episode', application['outcome']['episode_id'])
    assert_candidate(episode['learning']['ideas'], withdrawn=change == 'source')
    before_calls, before_reads = len(wire_calls), len(gateway.extractions)
    await engine._post(state)
    assert (len(wire_calls), len(gateway.extractions)) == (before_calls, before_reads)
    assert executor.calls == [op]
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    assert_original_unchanged(store, task['id'], event, response)
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'policy', 'payload'])
async def test_web_drain_recovers_pending_response_without_repeating_http(tmp_path, monkeypatch, change):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Collect the declared source', ['The original acquisition remains exact'])
    op = web_operation()
    requests, wire_calls = [], []
    engine.web = collector(store, requests)
    cipher = RecoverableCipher()
    cipher.unavailable = True
    control = {'fault': 'protection'}
    attach_gateway(store, engine, wire_calls, cipher, control, 'web_acquisition_learning')
    with pytest.raises(ConfigurationRequired):
        await engine._research(task['id'], op, 'pre', {})
    event, response = pending_original(store, task['id'])
    saved = deepcopy(next(w for w in store.records('web_work') if w['stage'] == 'after'))
    exchanges = deepcopy(store.records('web_exchange'))
    assert len(requests) == 1 and len(wire_calls) == 1
    await engine.close()
    store.close()
    store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests)
    change_binding(store, engine, task['id'], change, monkeypatch)
    if change == 'payload':
        # New legitimate knowledge affects the next complete Learning input.
        from tests.test_knowledge import seed_skill
        seed_skill(store, engine.knowledge, store.get_task(task['id']))
    cipher.unavailable = False
    gateway = attach_gateway(store, engine, wire_calls, cipher, control, 'web_acquisition_learning')

    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', saved['id'])
    assert current['status'] == 'complete'
    assert current['acquisition_result'] == saved['acquisition_result']
    assert current['acquisition_operation'] == saved['acquisition_operation']
    assert current['detail'] == saved['detail']
    assert_candidate(current['applied_learning']['ideas'], withdrawn=change == 'source')
    assert len(wire_calls) == 2 and len(gateway.extractions) == 1
    if change == 'policy':
        assert wire_calls[1]['policy_hash'] != response['policy_hash']
    elif change == 'payload':
        assert store.get_task(task['id'])['source_hash'] == response['source_hash']
        assert wire_calls[1]['policy_hash'] == response['policy_hash']
        assert wire_calls[0]['knowledge'] != wire_calls[1]['knowledge']
    assert gateway.extractions[0]['binding']['source_hash'] == response['source_hash']
    assert gateway.extractions[0]['binding']['policy_hash'] == response['policy_hash']
    assert store.records('web_exchange') == exchanges and len(requests) == 1
    assert_original_unchanged(store, task['id'], event, response)

    # Both the normal drain and actual collector reuse their durable original outcome.
    await engine.web_judgments.drain(task['id'])
    acquired = await engine._research(task['id'], op, 'pre', {})
    assert acquired['sources'][0]['text'] == 'exact mock response'
    assert len(requests) == 1 and len(wire_calls) == 2 and len(gateway.extractions) == 1
    assert store.records('web_exchange') == exchanges and executor.calls == []
    assert_original_unchanged(store, task['id'], event, response)
    committed = deepcopy(store.records('knowledge_application'))
    recoveries = deepcopy(store.records('learning_response_recovery'))
    await engine.close()
    store.close()
    store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests)
    current_task = store.get_task(task['id'])
    store.append_instruction(task['id'], 'Keep the acquired result while rechecking this current instruction.',
                             current_task['source_hash'])
    gateway = attach_gateway(store, engine, wire_calls, cipher, control, 'web_acquisition_learning')
    # Current-source rejudgment enters the real history consumer after another reconstruction.
    await engine.web_judgments.drain(task['id'])
    assert gateway.extractions == [] and len(wire_calls) == 2 and len(requests) == 1
    assert store.records('learning_response_recovery') == recoveries
    assert store.records('knowledge_application') == committed
    assert store.records('web_exchange') == exchanges
    assert_original_unchanged(store, task['id'], event, response)
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['protection', 'history', 'foreign-record', 'changed-record'])
async def test_unread_original_holds_changed_replacement_learning(tmp_path, fault):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve original response', ['No replacement while original is unread'])
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    payload = proposal_input(op, result)
    calls = []
    cipher = RecoverableCipher()
    cipher.unavailable = fault != 'history'
    control = {'fault': 'history' if fault == 'history' else 'protection'}
    gateway = attach_gateway(store, engine, calls, cipher, control, 'learning_proposal')
    with pytest.raises(ConfigurationRequired):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', payload)
    event, response = pending_original(store, task['id'])
    store.append_instruction(task['id'], 'A new instruction changes the pending request binding.', task['source_hash'])
    observed_response = response
    if fault in {'foreign-record', 'changed-record'}:
        cipher.unavailable = False
        # Corrupt only isolated fixture storage; the original event/reference stays immutable.
        observed_response = deepcopy(response)
        if fault == 'foreign-record':
            other = store.create_task('Other source owner', ['Keep separate'])
            observed_response['task_id'] = other['id']
        else:
            observed_response['retained_sha256'] = '0' * 64
        store.record('model_response', response['id'], observed_response)
    with pytest.raises(PolicyError):
        await engine.bounded_judgments.learning_proposal(
            task['id'], 'learning_proposal', dict(payload, current_observation='changed payload'))
    assert len(calls) == 1
    assert store.records('learning_response_recovery') == []
    assert store.records('learning_response_recovery_failure')
    assert store.record_get('model_response', response['id']) == observed_response
    assert next(e for e in store.events(task['id']) if e['seq'] == event['seq']) == event
    assert store.records('knowledge_application') == [] and executor.calls == []
    if fault in {'protection', 'history'}:
        cipher.unavailable = False
        control['history_unavailable'] = False
        restored = await engine.bounded_judgments.learning_proposal(
            task['id'], 'learning_proposal', dict(payload, current_observation='changed payload'))
        assert_candidate([i.model_dump() for i in restored.ideas])
        assert len(calls) == 2 and len(store.records('learning_response_recovery')) == 1
        assert_original_unchanged(store, task['id'], event, response)
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('isolation', ['operation', 'task'])
async def test_pending_original_does_not_block_or_leak_to_other_work(tmp_path, isolation):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Original owner', ['Retain original'])
    op = operation('file_list', args={})
    payload = proposal_input(op, OperationResult(operation_id=op['id'], status='succeeded').model_dump())
    calls = []
    cipher = RecoverableCipher()
    cipher.unavailable = True
    control = {'fault': 'protection'}
    gateway = attach_gateway(store, engine, calls, cipher, control, 'learning_proposal')
    with pytest.raises(ConfigurationRequired):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', payload)
    event, response = pending_original(store, task['id'])
    reads = len(gateway.extractions)
    other_task = store.create_task('Independent owner', ['No foreign candidates']) if isolation == 'task' else task
    other_op = deepcopy(op) if isolation == 'task' else operation('file_list', args={})
    other_payload = proposal_input(
        other_op, OperationResult(operation_id=other_op['id'], status='succeeded').model_dump())
    value = await engine.bounded_judgments.learning_proposal(
        other_task['id'], 'learning_proposal', other_payload)
    assert PENDING_IDEA['id'] not in {i.id for i in value.ideas}
    assert calls[-1]['required_ideas'] == []
    assert len(calls) == 2 and len(gateway.extractions) == reads
    assert store.records('learning_response_recovery') == []
    assert_original_unchanged(store, task['id'], event, response)
    assert executor.calls == []
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_restored_candidate_keeps_focus_page_ownership_and_current_withdrawal(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Process all candidate identities', ['No omission or cross-page adoption'])
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    calls = []
    cipher = RecoverableCipher()
    cipher.unavailable = True
    control = {'fault': 'protection'}
    gateway = attach_gateway(store, engine, calls, cipher, control, 'learning_proposal')
    with pytest.raises(ConfigurationRequired):
        await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', proposal_input(op, result))
    event, response = pending_original(store, task['id'])
    store.append_instruction(task['id'], WITHDRAWAL, task['source_hash'])
    cipher.unavailable = False
    bounded = engine.bounded_judgments
    gateway.settings.values['max_output_tokens'] = 2000
    probe = bounded.measure(task['id'], 'learning_proposal', proposal_input(op, result), Learning)
    gateway.settings.values['model_context_tokens'] = probe['input_tokens_estimate'] + 2000 + 6000
    from tests.test_bounded_judgments import idea
    payload = proposal_input(op, result)
    for count in (32, 64, 128, 256, 512):
        payload['required_ideas'] = [idea(i) for i in range(count)]
        if not bounded._fits(task['id'], 'learning_proposal', payload, Learning):
            break
    else:
        pytest.fail('The measured fixture never reached a real paging boundary')
    value = await bounded.learning_proposal(task['id'], 'learning_proposal', payload)
    actual = [i.model_dump() for i in value.ideas]
    expected = {i['id'] for i in payload['required_ideas']} | {PENDING_IDEA['id']}
    assert {i['id'] for i in actual} == expected and len(actual) == len(expected)
    assert_candidate(actual, withdrawn=True)
    leaves = [r for r in store.records('bounded_model_call')
              if r['task_id'] == task['id'] and r['phase'].startswith('learning_proposal:page:')
              and r.get('learning_schema') == 'LearningIdeas'
              and r['status'] == 'succeeded']
    assert len(leaves) > 1
    memberships = [{i['id'] for i in r['payload']['required_ideas']} for r in leaves]
    assert set().union(*memberships) == expected
    assert sum(len(ids) for ids in memberships) == len(expected)
    for leaf, own_ids in zip(leaves, memberships):
        assert {i['id'] for i in leaf['result']['ideas']} == own_ids
        assert leaf['payload']['source_packet_id']
    successful_reads = [r for r in store.records('learning_response_recovery')
                        if r.get('source_event', {}).get('seq') == event['seq']]
    assert len(successful_reads) == 1
    # One unsuccessful initial read plus one successful durable recovery; pages reuse it.
    assert len(gateway.extractions) == 2
    assert_original_unchanged(store, task['id'], event, response)
    assert store.records('knowledge_application') == [] and executor.calls == []
    await engine.close()
    store.close()
