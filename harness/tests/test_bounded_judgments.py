"""Real controller/storage/packing with explicit offline judgment fixtures."""
from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from policy_harness.bounded_judgments import BoundedJudgments, ContextObservation, InputCapacityError
from policy_harness.engine import Engine
from policy_harness.knowledge import Knowledge
from policy_harness.models import (AssessmentBatch, ConfigurationRequired, Disposition, Learning, LEARNING_SCHEMAS, LearningIdeas, LearningApplications, LearningSynthesis, Operation,
                                  OperationResult, PolicyError, Review, SkillSelection)
from policy_harness.policy import PolicyCatalog
from policy_harness.providers import ModelGateway, ProviderError, _token_estimate
from policy_harness.store import Store, digest
from tests.fixture_preparation import prepare_operation


POLICY = Path(__file__).parents[1] / 'policy/complete-policy-v3.json'


class PublicFixtureSettings:
    def __init__(self):
        from tests.test_providers import FakeSettings
        self.values = FakeSettings(base_url='https://fixture.invalid/v1', model='fixture-capacity-model',
            api_mode='compatible', model_context_tokens=10_000_000, max_output_tokens=8192).get()

    def public(self):
        return dict(self.values)

    def get(self):
        return self.public()

    def secret(self, name):
        return 'SYNTHETIC_CAPACITY_CREDENTIAL' if name == 'model_api_key' else None


class PackedFixtureGateway(ModelGateway):
    # This writer intentionally emits the retained canonical/legacy contract.
    supports_semantic_wire = False

    def __init__(self, policy, fault=None):
        super().__init__(PublicFixtureSettings(), policy)
        self.calls = []
        self.capacity_rejections = []
        self.output_truncations = []
        self.completed_outputs = []
        self.fault = fault

    async def generate(self, role, phase, payload, schema, **kwargs):
        messages = self._messages(role, phase, payload, schema)
        tokens, method = _token_estimate(json.dumps(messages, ensure_ascii=False))
        if tokens + self.settings.values['max_output_tokens'] > self.settings.values['model_context_tokens']:
            self.capacity_rejections.append({'phase': phase, 'tokens': tokens, 'request_sent': False})
            raise ConfigurationRequired('Complete policy and input exceed configured context estimate; fixture request was not sent')
        self.calls.append({'phase': phase, 'role': role, 'schema': schema.__name__, 'payload': deepcopy(payload),
                           'tokens': tokens, 'method': method, 'policy_full': self.policy.prompt() in messages[0]['content']})
        from tests.test_policy_admission import ADMISSION_SCHEMAS, fixture_engineering_response
        if schema in ADMISSION_SCHEMAS:
            value = fixture_engineering_response(payload, schema, store=self.response_store)
        elif schema is ContextObservation:
            ids = [r['id'] for r in payload['source_records']]
            value = ContextObservation(coverage_ids=ids[1:] if self.fault == 'coverage' else ids,
                summary='Offline source-page interpretation: retain every original identity, actual result and uncertainty; no semantic completeness claim.',
                limitations=['Controlled fixture interpretation; actual AI fidelity unobserved.'])
        elif schema is AssessmentBatch:
            targets = payload['targets'][1:] if self.fault == 'target' else payload['targets']
            value = AssessmentBatch(assessments=[{'target_id': target['id'], 'assessment': {
                'objective_link': 'Exact owned output', 'rationale': 'Controlled per-target assessment',
                'success': 'succeeded' if 'post' in phase or 'after' in phase else 'expected',
                'mistakes': [], 'recurrence': 'unknown', 'efficiency': 'Measured transport only',
                'interactions': 'Other choices remain separately accounted',
                'thinking_targets': {key: 'Fixture examines ' + key for key in payload['required_thinking_targets']},
                'ideas': [{'id': digest(target)[:16], 'target': 'task', 'proposal': 'Consider a future exact-output example',
                           'disposition': 'reject', 'rationale': 'No present source change is needed'}], 'evidence_refs': []}}
                for target in targets])
        elif schema is SkillSelection:
            selected = []; rejected = []
            for skill in payload['knowledge']['skills']:
                if int(skill['title'].split()[-1]) % 2 == 0:
                    selected.append({'id': skill['id'], 'hash': skill['hash'], 'reason': 'Applicable exact output procedure',
                                     'application': 'Write the exact bytes and observe the file hash', 'procedure_clause': skill['content']})
                else:
                    rejected.append({'id': skill['id'], 'reason': 'Other source applicability; retain without claimed use'})
            value = SkillSelection(selected=selected, rejected=rejected, new_knowledge_needed=['Record current actual output'], rationale='Offline exact catalog disposition')
        elif schema is Review:
            value = Review(summary='Offline adversarial source-page review', opinions=[{
                'id': digest({'phase': phase, 'payload': payload})[:24], 'observation': 'Preserve actual source and unknowns',
                'rationale': 'This bounded fixture does not prove semantic correctness'}])
        elif schema is Disposition:
            opinions = payload['review']['opinions']
            value = Disposition(verdict='hold' if any(o['observation'] == 'hold this' for o in opinions) else 'proceed',
                rationale='Offline reasoned disposition of this exact unchanged source packet',
                opinion_responses=[] if self.fault == 'opinion' else [{'opinion_id': o['id'], 'disposition': 'accept', 'rationale': 'Compared exact supplied source'} for o in opinions],
                web_refs=[s['id'] for s in payload.get('web', {}).get('sources', payload.get('sources', []))])
        elif schema in LEARNING_SCHEMAS:
            required = payload.get('required_ideas', [])
            applications = []
            for chosen in (payload.get('pre', {}).get('skills', {}).get('selected', []) if schema is not LearningSynthesis else []):
                result = payload['result']; operation = payload['operation']; clause = chosen['procedure_clause']
                contract = payload['application_contract']
                evidence = ({'pointer': '/data', 'sha256': next(r['sha256'] for r in contract['result_evidence'] if r['pointer'] == '/data'),
                             'explanation': 'Original fixture result data was supplied in complete source pages; this is a controlled use assertion'}
                            if 'learning_anchor_ref' in payload else
                            {'pointer': '/data/sha256', 'sha256': digest(result['data']['sha256']),
                             'explanation': 'Actual fixture executor file hash after the selected exact write'})
                applications.append({'skill_id': chosen['id'], 'skill_hash': chosen['hash'], 'procedure_clause': clause,
                    'procedure_sha256': hashlib.sha256(clause.encode()).hexdigest(), 'operation_id': operation['id'],
                    'operation_sha256': contract['operation_sha256'], 'result_sha256': contract['result_sha256'],
                    'evidence': [evidence]})
            returned_ideas = required[:-1] if self.fault == 'idea' else required
            if schema in (Learning,LearningIdeas) and getattr(self,'extra_idea',None) is not None:
                returned_ideas=[*returned_ideas,self.extra_idea]
                self.extra_idea=None  # One actual proposal, later carried by its receipt.
            if self.fault == 'new_ideas':
                returned_ideas = [*returned_ideas, {**idea('new'), 'id': 'idea1'}]
            value = Learning(outcome_summary='Actual fixture output, separately reviewed', classifications=['use'] if applications else ['organize'],
                skill_updates=[], recurrence='unknown', next_use_trigger='Next applicable exact file operation',
                ideas=returned_ideas, applications=applications)
            if schema is LearningIdeas:
                value = LearningIdeas(ideas=value.ideas)
            elif schema is LearningApplications:
                value = LearningApplications(considered_skill_ids=[x['id'] for x in payload['pre']['skills']['selected']],
                    applications=applications, ideas=value.ideas)
            elif schema is LearningSynthesis:
                value = LearningSynthesis(**{k: v for k, v in value.model_dump().items() if k != 'applications'})
        else:
            raise AssertionError(schema)
        serialized = value.model_dump_json()
        output = _token_estimate(serialized)[0]
        reserve = self.settings.values['max_output_tokens']
        if output > reserve:
            # This is an explicit synthetic response, with the same length-stop
            # contract as ModelGateway. Never return the oversized typed value.
            raw = serialized[:min(len(serialized) - 1, reserve)]
            metadata = {'fixture': True, 'truncated': True, 'finish_reason': 'length',
                        'usage': None, 'input_sha256': digest(payload),
                        'input_tokens_estimate': tokens, 'output_reserve': reserve,
                        'fixture_complete_output_tokens_estimate': output,
                        'fixture_complete_candidate_sha256': hashlib.sha256(serialized.encode()).hexdigest(),
                        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                        'response_diagnostic': {'sanitized_response': raw, 'diagnostic_truncated': False}}
            self.output_truncations.append({'phase': phase, 'schema': schema.__name__, 'metadata': deepcopy(metadata)})
            if schema is Disposition:
                from tests.fixture_response_transport import retained_reply
                try:
                    await retained_reply(self, role, phase, payload, schema, value,
                        content=raw, finish_reason='length', **kwargs)
                except ProviderError as error:
                    error.metadata = {**metadata, **error.metadata, 'fixture': True}
                    raise
                raise AssertionError('The actual length response must remain rejected')
            raise ProviderError('Observed synthetic finish_reason=length at the configured output reserve', metadata=metadata)
        assert output <= self.settings.values['max_output_tokens'], (phase, 'fixture output exceeds actual reserve', output)
        self.completed_outputs.append({'phase': phase, 'tokens': output, 'reserve': reserve})
        usage = {'fixture': True, 'input_tokens_estimate': tokens, 'output_tokens_estimate': output, 'usage': None}
        if schema is Disposition or schema in LEARNING_SCHEMAS:
            from tests.fixture_response_transport import retained_reply
            return await retained_reply(self, role, phase, payload, schema, value, usage=usage, **kwargs)
        return value, usage


def request_runtime(tmp_path, handler, mode='compatible'):
    """Real Settings/Store/Engine/gateway; only HTTP and judgments are fixtures."""
    from policy_harness.settings import SettingsManager
    from tests.test_core import runtime
    from tests.test_provider_evidence_recovery import FixtureCipher
    store, engine, executor, _ = runtime(tmp_path / 'runtime')
    settings = SettingsManager(tmp_path / 'settings', protector=FixtureCipher())
    settings.update({'base_url': 'https://fixture-a.invalid/v1', 'model': 'model-a',
                     'api_mode': mode, 'model_context_tokens': 1_000_000,
                     'max_output_tokens': 32768, 'model_api_key': 'fixture-current-key-a'})
    gateway = ModelGateway(settings, engine.policy, transport=httpx.MockTransport(handler),
                           response_store=store, response_protector=FixtureCipher())
    # These handlers replay the original JSON contract and its exact faults.
    gateway.supports_semantic_wire = False
    engine.gateway = gateway
    engine.bounded_judgments = BoundedJudgments(engine)
    task = store.create_task('Preserve the exact request and accepted result', ['No differently configured result under the prior key'])
    return store, engine, engine.bounded_judgments, task, settings, gateway, executor


def request_review_response(body, defect=None):
    from tests.test_providers import envelope
    opinion = {'id': 'fixture-opinion', 'observation': 'Synthetic judgment only',
               'rationale': 'No real model quality or target effect is inferred'}
    value = {'summary': body['model'], 'opinions': [opinion]}
    if defect == 'schema':
        value['summary'] = 17
    elif defect == 'proposal':
        value['opinions'].append(deepcopy(opinion))
    return httpx.Response(200, json=envelope(json.dumps(value), model=body['model']))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
@pytest.mark.parametrize('defect', ['schema', 'proposal'])
async def test_actual_regeneration_holds_changed_configuration_before_corrective_dispatch(tmp_path, mode, defect):
    sent = []

    async def handle(request):
        body = json.loads(request.content); sent.append(body)
        if len(sent) == 1:
            settings.update({'model': 'model-b'})
            return request_review_response(body, defect)
        # This is the A -> B -> A counterexample to a final settings check:
        # an incorrectly sent B correction restores A before returning B data.
        # With per-dispatch binding, this branch is reached only by the later
        # explicit B call below, never by the failed A logical judgment.
        if body['model'] == 'model-b':
            settings.update({'model': 'model-a'})
        return request_review_response(body)

    store, engine, bounded, task, settings, gateway, executor = request_runtime(tmp_path, handle, mode)
    phase = 'configuration-bound-review'; payload = {'target': {'id': 'owned-review'}}
    with pytest.raises(ConfigurationRequired, match='settings changed'):
        await bounded._call(task['id'], phase, payload, Review)
    assert [body['model'] for body in sent] == ['model-a']
    assert not store.records('bounded_model_completed') and not store.records('bounded_model_truncated')
    rejected = [event for event in store.events(task['id']) if event['status'] == 'rejected_model_output']
    assert len(rejected) == 1
    failed = store.records('bounded_model_call')[0]
    assert failed['status'] == 'failed' and failed['measurement']['configuration']['model'] == 'model-a'
    responses = deepcopy(store.records('model_response'))
    assert len(responses) == 1 and executor.calls == []

    # A separately measured B request may finish after the operator restores A;
    # the actual request was still B, so only the B key may own that result.
    result_b = await bounded._call(task['id'], phase, payload, Review)
    assert result_b.summary == 'model-b' and settings.get()['model'] == 'model-a'
    result_a = await bounded._call(task['id'], phase, payload, Review)
    assert result_a.summary == 'model-a'
    completed = store.records('bounded_model_completed')
    assert len(completed) == 2 and len({row['key'] for row in completed}) == 2
    assert {row['measurement']['configuration']['model']: row['result']['summary'] for row in completed} == {
        'model-a': 'model-a', 'model-b': 'model-b'}
    assert [body['model'] for body in sent] == ['model-a', 'model-b', 'model-a']
    assert store.record_get('bounded_model_call', failed['id']) == failed
    assert store.record_get('model_response', responses[0]['id']) == responses[0]

    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    store = Store(data_dir)
    gateway.response_store = store
    engine = Engine(store, policy, executor, gateway, NoNetworkWeb(), Knowledge(store))
    restored = await engine.bounded_judgments._call(task['id'], phase, payload, Review)
    assert restored.model_dump() == result_a.model_dump() and len(sent) == 3
    assert store.records('bounded_model_completed') == completed and executor.calls == []
    assert store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unchanged', 'a-b-a'])
async def test_actual_regeneration_keeps_stable_settings_through_schema_feedback(tmp_path, change):
    sent = []

    async def handle(request):
        body = json.loads(request.content); sent.append(body)
        if len(sent) == 1:
            if change == 'a-b-a':
                settings.update({'model': 'model-b'})
                settings.update({'model': 'model-a'})
            return request_review_response(body, 'schema')
        return request_review_response(body)

    store, engine, bounded, task, settings, _, executor = request_runtime(tmp_path, handle)
    value = await bounded._call(task['id'], 'same-configuration-review', {'target': {'id': 'same'}}, Review)
    assert value.summary == 'model-a' and [body['model'] for body in sent] == ['model-a', 'model-a']
    assert 'actual_format_feedback' not in sent[0]['messages'][1]['content']
    assert 'actual_format_feedback' in sent[1]['messages'][1]['content']
    assert len(store.records('model_response')) == 2 and len(store.records('bounded_model_completed')) == 1
    assert executor.calls == [] and store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
@pytest.mark.parametrize('change_endpoint', [False, True])
async def test_actual_regeneration_reads_current_credentials_without_retargeting_old_request(tmp_path, mode, change_endpoint):
    sent = []

    async def handle(request):
        body = json.loads(request.content)
        sent.append((str(request.url), request.headers['authorization']))
        if len(sent) == 1:
            settings.update({'model_api_key': 'fixture-current-key-b',
                             **({'base_url': 'https://fixture-b.invalid/v1'} if change_endpoint else {})})
            return request_review_response(body, 'schema')
        return request_review_response(body)

    store, engine, bounded, task, settings, _, executor = request_runtime(tmp_path, handle, mode)
    call = bounded._call(task['id'], 'credential-bound-review', {'target': {'id': 'same'}}, Review)
    if change_endpoint:
        with pytest.raises(ConfigurationRequired, match='settings changed'):
            await call
        assert len(sent) == 1 and not store.records('bounded_model_completed')
    else:
        assert (await call).summary == 'model-a'
        assert sent[1] == ('https://fixture-a.invalid/v1/chat/completions', 'Bearer fixture-current-key-b')
        assert len(store.records('bounded_model_completed')) == 1
    assert sent[0] == ('https://fixture-a.invalid/v1/chat/completions', 'Bearer fixture-current-key-a')
    assert len(sent) == (1 if change_endpoint else 2)
    retained = json.dumps([store.events(task['id']), store.records('model_response'),
                           store.records('bounded_model_call')], ensure_ascii=False)
    assert all(secret not in retained for secret in ('fixture-current-key-a', 'fixture-current-key-b'))
    assert executor.calls == [] and store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_actual_request_bindings_stay_local_across_concurrent_tasks_and_roles(tmp_path):
    entered = asyncio.Event(); release = asyncio.Event(); sent = []

    async def handle(request):
        body = json.loads(request.content); sent.append(body)
        if len(sent) == 1:
            entered.set()
            await release.wait()
            return request_review_response(body, 'schema')
        return request_review_response(body)

    store, engine, bounded, task, settings, _, executor = request_runtime(tmp_path, handle)
    settings.update({'review_model': 'review-model-a'})
    other = store.create_task('An independent worker judgment', ['Keep its own measured model'])
    reviewer = asyncio.create_task(bounded._call(task['id'], 'parallel-review', {'target': {'id': 'review'}}, Review, role='reviewer'))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        settings.update({'model': 'model-b'})
        worker = await bounded._call(other['id'], 'parallel-review', {'target': {'id': 'worker'}}, Review, role='worker')
        settings.update({'model': 'model-a'})
        release.set()
        reviewed = await reviewer
    finally:
        release.set()
        await asyncio.gather(reviewer, return_exceptions=True)
    assert worker.summary == 'model-b' and reviewed.summary == 'review-model-a'
    assert [body['model'] for body in sent] == ['review-model-a', 'model-b', 'review-model-a']
    completed = store.records('bounded_model_completed')
    assert {row['task_id']: (row['measurement']['configuration']['model'], row['result']['summary']) for row in completed} == {
        task['id']: ('review-model-a', 'review-model-a'), other['id']: ('model-b', 'model-b')}
    assert executor.calls == [] and store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [False, True])
async def test_actual_child_regeneration_checks_the_lease_and_measured_settings(tmp_path, change):
    from policy_harness.model_routing import StaleModelLease
    from tests.test_model_routing import selection
    sent = []

    async def handle(request):
        body = json.loads(request.content); sent.append(body)
        if len(sent) == 1:
            if change:
                settings.update({'model': 'model-b'})
            return request_review_response(body, 'schema')
        return request_review_response(body)

    store, engine, bounded, parent, settings, _, executor = request_runtime(tmp_path, handle)
    child = store.create_task('Review the owned child result', ['Retain its assigned model'], parent_id=parent['id'])
    job = Operation(kind='delegate', args={'objective': child['objective'], 'acceptance': child['acceptance'],
        'independent_scope': 'Only the owned child result', 'integration_plan': 'Parent reads the retained child result',
        'capability_requirements': ['Structured JSON review'], 'quality_requirements': ['Exact model lease'],
        'cost_considerations': 'Synthetic transport; real capability and cost remain unobserved',
        'model_selections': {role: selection(settings, role) for role in ('worker', 'parent', 'reviewer')}},
        purpose='Owned child review', expected_result='Retained proposal with its actual request',
        decisions=[{'id': 'owned', 'statement': 'Read the child result', 'rationale': 'Parent source'}]).model_dump()
    leases = engine._delegate_models(parent, job)
    # Explicit fixture ownership state; the caller, lease resolution, settings,
    # actual request and durable outcome paths remain the real implementations.
    store.update_task(child['id'], state={'delegation_lease': {
        'parent_id': parent['id'], 'parent_source_hash': parent['source_hash'],
        'parent_operation_id': job['id'], 'job_operation': job, 'model_leases': leases}})
    call = bounded._call(child['id'], 'child-request-review', {'target': {'id': 'owned'}}, Review, role='reviewer')
    if change:
        with pytest.raises(StaleModelLease):
            await call
        assert len(sent) == 1 and not store.records('bounded_model_completed')
    else:
        assert (await call).summary == leases['reviewer']['model']
        completed = store.records('bounded_model_completed')
        assert len(completed) == 1
        assert completed[0]['measurement']['configuration']['model_selection']['lease_hash'] == leases['reviewer']['lease_hash']
        assert [body['model'] for body in sent] == [leases['reviewer']['model']] * 2
    assert len(store.records('model_response')) == len(sent) and executor.calls == []
    assert store.verify_events()
    await engine.close(); store.close()


class FixtureExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, workspace, operation):
        self.calls.append(operation.id)
        path = workspace / operation.args['path']
        path.write_text(operation.args['text'], encoding='utf-8')
        return OperationResult(operation_id=operation.id, status='succeeded', effect='confirmed',
                               data={'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})


class NoNetworkWeb:
    pass


def make_runtime(tmp_path, fault=None, slack=6000):
    store = Store(tmp_path / 'data'); policy = PolicyCatalog(POLICY)
    gateway = PackedFixtureGateway(policy, fault); executor = FixtureExecutor(); knowledge = Knowledge(store)
    engine = Engine(store, policy, executor, gateway, NoNetworkWeb(), knowledge)
    bounded = BoundedJudgments(engine)
    # Exercise Engine's integrated consumers. An obsolete global _call wrapper
    # would wrap Learning again when a resumed adapter captures the real caller.
    engine.bounded_judgments = bounded

    async def research(*args, **kwargs):
        return {'sources': [{'id': 'offline-source', 'url': 'https://fixture.invalid/reference',
                            'text': 'Controlled source fixture; no real Web acquisition', 'sha256': digest('fixture')}], 'fixture': True}

    engine._research = research
    task = store.create_task('Write hello to answer.txt', ['Actual file contains hello'])
    operation = Operation(kind='file_write', args={'path': 'answer.txt', 'text': 'hello'},
                          purpose='Write exact requested bytes', expected_result='Actual exact output',
                          decisions=[{'id': 'write-choice', 'statement': 'Use the requested file', 'rationale': 'Owned source'}]).model_dump()
    probe = {'operation': operation, 'result': OperationResult(operation_id=operation['id'], status='succeeded').model_dump(),
             'required_ideas': [], 'review': {'summary': 'probe', 'opinions': []}, 'web': {'sources': []}, 'targets': [{'id': 'probe'}]}
    overhead = max(bounded.measure(task['id'], 'budget-probe', probe, schema)['input_tokens_estimate']
                   for schema in (Learning, Review, Disposition, AssessmentBatch, ContextObservation, SkillSelection))
    gateway.settings.values['model_context_tokens'] = overhead + gateway.settings.values['max_output_tokens'] + slack
    return store, engine, bounded, task, operation, gateway, executor


def add_skills(store, knowledge, task, count, history=0):
    values = []
    for index in range(count):
        skill = knowledge._new(task, 'controlled-fixture-seed', {'title': 'Fixture Skill ' + str(index),
            'content': 'Write the exact requested text and verify the resulting file hash. ' * 6,
            'applicability': 'Requested exact text file ' + str(index), 'next_trigger': 'Next exact write'})
        if history and index == 0:
            skill['uses'] = [{'fixture_history': n, 'observation': 'Past bounded observation with its own source and retained unknowns'} for n in range(history)]
            knowledge._save_skill(skill)
            skill = store.record_get('skill', skill['id'])
        values.append(skill)
    knowledge.rebuild_projections()
    return values


def idea(index):
    return {'id': 'required-' + str(index), 'target': 'task', 'proposal': 'Preserve exact earlier candidate ' + str(index),
            'disposition': 'reject', 'rationale': 'Current source needs no additional change'}


def force_learning_pages(bounded, task_id, operation, result, count=32):
    payload = {'operation': operation, 'result': result, 'required_ideas': [idea(i) for i in range(count)]}
    while bounded._fits(task_id, 'learning_proposal', payload, Learning):
        count *= 2
        payload['required_ideas'] = [idea(i) for i in range(count)]
    return payload


@pytest.mark.asyncio
async def test_actual_cycle_large_selection_pre_post_learning_and_exact_application(tmp_path,monkeypatch):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    skills = add_skills(store, engine.knowledge, task, 28)
    state = await prepare_operation(engine, task, op)
    await engine._pre(state)
    before = store.get_operation(op['id'])
    assert len(before['pre_bundle']['skills']['selected']) + len(before['pre_bundle']['skills']['rejected']) == len(skills)
    assert {a['target_id'] for a in before['pre_bundle']['assessments']} == {t['id'] for t in before['pre_bundle']['targets']}
    await engine._execute(state)
    proposed_prior=idea('previous-attempt');gateway.extra_idea=proposed_prior
    class ProposalSaved(RuntimeError):pass
    original_record=store.record
    def interrupt_after_proposal(kind,identity,value):
        original_record(kind,identity,value)
        if kind=='post_learning_attempt' and value.get('status')=='learning_proposed':
            raise ProposalSaved('Controlled interruption after the actual durable producer')
    with monkeypatch.context() as interrupted:
        interrupted.setattr(store,'record',interrupt_after_proposal)
        with pytest.raises(ProposalSaved):await engine._post(state)
    pending,=[a for a in store.records('post_learning_attempt') if a['operation_id']==op['id']]
    assert pending['created_at'] and pending['source_hash']==task['source_hash'] and pending['status']=='learning_proposed'
    prior,=[i for i in pending['learning']['ideas'] if i['proposal']==proposed_prior['proposal']]
    completed_before=deepcopy(store.records('bounded_model_completed'))
    await engine._post(state)
    for call in completed_before:assert store.record_get('bounded_model_completed',call['key'])==call
    post = store.get_operation(op['id'])
    required = engine._post_required_ideas(post, post['post_bundle']['assessments'])
    actual = post['post_bundle']['learning']
    assert {i['id'] for i in required} <= {i['id'] for i in actual['ideas']}
    assert prior['id'] in {i['id'] for i in actual['ideas']}
    assert len(actual['applications']) == len(before['pre_bundle']['skills']['selected'])
    await engine._learn(state)
    assert executor.calls == [op['id']]
    assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
    assert len(store.records('skill_application')) == len(actual['applications'])
    assert store.records('bounded_learning')
    assert store.records('bounded_review_coverage')
    assert all(c['policy_full'] for c in gateway.calls)
    # Narrow output ownership can remove the former oversized global echo.
    # The separate observed-truncation case forces the output-failure route.
    assert any(c['schema'] == 'LearningApplications' for c in gateway.calls)
    assert sum(c['schema'] == 'LearningSynthesis' for c in gateway.calls) == 1
    assert all(c['metadata']['finish_reason'] == 'length' for c in gateway.output_truncations)
    assert all(c['tokens'] <= c['reserve'] for c in gateway.completed_outputs)
    assert all(c['tokens'] + gateway.settings.values['max_output_tokens'] <= gateway.settings.values['model_context_tokens'] for c in gateway.calls)
    for saved in store.records('review'):
        assert saved['bundle_hash']
        assert sorted(o['id'] for o in saved['review']['opinions']) == sorted(r['opinion_id'] for r in saved['disposition']['opinion_responses'])
    assert len(store.get_operation(op['id'])['post_bundle']['learning']['ideas']) == len(actual['ideas'])
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['target', 'opinion', 'idea', 'coverage'])
async def test_missing_mandatory_content_is_rejected_without_execution(tmp_path, fault):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path, fault)
    with pytest.raises((PolicyError, ValueError)):
        if fault == 'target':
            await bounded.assess_targets(task['id'], 'pre_assessment', [{'id': 'a', 'statement': 'Exact A'}, {'id': 'b', 'statement': 'Exact B'}], {'operation': op})
        elif fault == 'opinion':
            review = Review(summary='Actual opinions', opinions=[{'id': 'o' + str(i), 'observation': 'Actual concern ' * 20, 'rationale': 'Source-bound'} for i in range(90)])
            await bounded._dispose(task['id'], op, 'pre', {'decisions': op['decisions']}, {'sources': []}, review)
        elif fault == 'idea':
            await bounded.learning_proposal(task['id'], 'learning_proposal', {'operation': op,
                'result': OperationResult(operation_id=op['id'], status='succeeded').model_dump(), 'required_ideas': [idea(i) for i in range(150)]})
        else:
            await bounded.compact_context(task['id'], 'review_context', {'history': ['actual evidence ' * 100 for _ in range(100)]},
                                          {'operation': op}, Review, role='reviewer')
    assert executor.calls == []
    assert store.records('bounded_model_call')
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_history_metadata_bounded_but_selection_observes_full_record(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    skill = add_skills(store, engine.knowledge, task, 1, history=700)[0]
    index = engine.knowledge.index(task, 'skill')
    item = index['items'][0]
    assert item['uses_count'] == 700 and 'uses' not in item
    assert len(json.dumps(item)) < 2500
    assert engine.knowledge.read(task, **item['read_args'])['value']['uses'] == skill['uses']
    selection, ids = await bounded.select_skills(task, op)
    assert ids == {skill['id']} and selection.selected[0]['hash'] == skill['hash']
    packets = store.records('bounded_input')
    assert any(p['value'].get('full_skill') == skill for p in packets)
    coverage = store.records('bounded_context')
    assert coverage and all(c['source_records'] for c in coverage)
    assert all(c['tokens'] + gateway.settings.values['max_output_tokens'] <= gateway.settings.values['model_context_tokens'] for c in gateway.calls)
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_fragmented_web_source_and_all_opinions_keep_parent_hold(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    source = {'id': 'web-large', 'url': 'https://fixture.invalid/full', 'text': 'Source evidence with original uncertainty. ' * 3000, 'sha256': digest('source')}
    extra = Review(summary='Prior actual adversarial concerns', opinions=[{'id': 'previous-hold', 'observation': 'hold this', 'rationale': 'Retain this exact unresolved source concern'}])
    result = await bounded.review_bundle(task['id'], op, 'pre', {'choices': [idea(i) for i in range(100)]}, {'sources': [source]}, [extra])
    assert result['disposition']['verdict'] == 'hold'
    assert result['disposition']['web_refs'] == ['web-large']
    original_ids = [item for r in store.records('bounded_review_responses') for response in r['responses'] for item in response['opinion_ids']]
    hold_id = next(item['controller_id'] for item in original_ids if item['original_id'] == 'previous-hold')
    assert hold_id in [x['opinion_id'] for x in result['disposition']['opinion_responses']]
    parent_calls = [c for c in gateway.calls if c['schema'] == 'Disposition']
    assert len(parent_calls) > 1
    assert any(s.get('source_fragments') for c in parent_calls for s in c['payload']['web']['sources'])
    assert any(p['value'].get('web', {}).get('sources') == [source] for p in store.records('bounded_input'))
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_large_learning_result_is_fully_observed_and_keeps_original_anchor_hashes(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    huge = {'operation': op, 'result': OperationResult(operation_id=op['id'], status='succeeded', stdout='unique output data ' * 20000).model_dump(),
            'required_ideas': [idea(1)]}
    learned = await bounded.learning_proposal(task['id'], 'learning_proposal', huge)
    assert {i.id for i in learned.ideas} == {idea(1)['id']}
    assert any(p['value'] == huge for p in store.records('bounded_input'))
    anchor = store.records('bounded_learning_anchors')[0]
    observed = store.record_get('bounded_context', anchor['observed_context_id'])
    fragments = [r for r in observed['source_records'] if r['path'] == '/result/stdout']
    assert fragments and all(r['fragment'] for r in fragments)
    assert json.loads(''.join(r['value'] for r in sorted(fragments, key=lambda r: r['start']))) == huge['result']['stdout']
    call = next(c for c in gateway.calls if c['schema'] in {'LearningIdeas', 'LearningApplications', 'LearningSynthesis'} and c['payload'].get('learning_anchor_ref'))
    assert call['payload']['result']['source_sha256'] == digest(huge['result'])
    assert call['payload']['operation']['id'] == op['id']
    assert call['payload']['application_contract']['result_sha256'] == digest(huge['result'])
    assert next(r['sha256'] for r in call['payload']['application_contract']['result_evidence'] if r['pointer'] == '/stdout') == digest(huge['result']['stdout'])
    assert len(call['payload']['application_contract']['result_evidence']) == 4
    from policy_harness.models import learning_applications_schema
    assert call['payload']['application_contract']['applications'] == learning_applications_schema()
    assert Learning.model_json_schema()['properties']['applications']['items'] == learning_applications_schema()['items']
    assert all(c['tokens'] + gateway.settings.values['max_output_tokens'] <= gateway.settings.values['model_context_tokens'] for c in gateway.calls)
    assert executor.calls == []
    await bounded.assess_targets(task['id'], 'independent_pre', [{'id': 'independent', 'statement': 'Safe independent target'}], {'operation': op})
    assert gateway.calls
    await engine.close(); store.close()


def test_actual_policy_schema_and_source_envelope_are_in_capacity_measure(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    measured = bounded.measure(task['id'], 'probe', {'operation': op}, Review)
    assert measured['input_tokens_estimate'] > _token_estimate(json.dumps(op))[0] * 10
    model_input = engine._model_input(task['id'], 'probe', {'operation': op}, Review)
    messages = gateway._messages('parent', 'probe', model_input, Review)
    assert measured['messages_sha256'] == digest(messages)
    misses = bounded.token_cache_misses
    assert bounded.measure(task['id'], 'probe', {'operation': op}, Review) == measured
    assert bounded.token_cache_misses == misses and bounded.token_cache_hits > 0
    gateway.settings.values['model_context_tokens'] = measured['input_tokens_estimate'] + gateway.settings.values['max_output_tokens'] - 1
    assert not bounded.measure(task['id'], 'probe', {'operation': op}, Review)['fits']
    store.close()


@pytest.mark.asyncio
async def test_bad_coverage_is_never_cached_and_corrected_resume_can_progress(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path, 'coverage')
    context = {'choices': [idea(i) for i in range(180)]}
    base = {'operation': op}
    # The production schema changes the reserved envelope. Derive the fault
    # input from its real capacity instead of assuming 180 choices still page.
    while bounded._fits(task['id'], 'context-resume', {**base, 'bounded_context': context}, Review, 'reviewer'):
        context['choices'] = [idea(i) for i in range(len(context['choices']) * 2)]
    assert not bounded.measure(task['id'], 'context-resume', {**base, 'bounded_context': context}, Review, role='reviewer')['fits']
    with pytest.raises(PolicyError):
        await bounded.compact_context(task['id'], 'context-resume', context, base, Review, role='reviewer')
    rejected_observations = sum(c['schema'] == 'ContextObservation' for c in gateway.calls)
    assert rejected_observations > 0, 'The invalid source-coverage response must actually be consumed.'
    assert not store.records('bounded_model_completed')
    assert any(r['status'] == 'rejected' for r in store.records('bounded_model_call'))
    gateway.fault = None
    result = await bounded.compact_context(task['id'], 'context-resume', context, base, Review, role='reviewer')
    assert sum(c['schema'] == 'ContextObservation' for c in gateway.calls) > rejected_observations
    assert result['source_sha256'] == digest(context)
    assert store.records('bounded_model_completed')
    assert all(r['status'] == 'succeeded' for r in store.records('bounded_model_completed'))
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_fit_assessment_keeps_normal_phase_and_repeated_ids_are_namespaced(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    await bounded.assess_targets(task['id'], 'pre_assessment', [{'id': 'one', 'statement': 'Exact target'}], {'operation': op})
    assert [c['phase'] for c in gateway.calls if c['schema'] == 'AssessmentBatch'] == ['pre_assessment']
    repeated = [Review(summary='Independent response ' + str(n), opinions=[{'id': 'o1', 'observation': 'Actual concern', 'rationale': 'Unchanged source'}]) for n in range(2)]
    result = await bounded.review_bundle(task['id'], op, 'pre', {'operation': op}, {'sources': []}, repeated)
    actual = [r['opinion_id'] for r in result['disposition']['opinion_responses']]
    assert len(actual) == len(set(actual)) == 3
    mappings = [response for record in store.records('bounded_review_responses') for response in record['responses']]
    assert sum(m['original_id'] == 'o1' for response in mappings for m in response['opinion_ids']) == 2
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_web_review_saved_separately_and_resumed_disposition_never_regenerates_it(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    sources = [{'id': 'actual-web', 'url': 'https://fixture.invalid/source', 'text': 'Original acquired evidence; controlled fixture'}]
    payload = {'operation': op, 'choices': [idea(i) for i in range(120)], 'sources': sources,
               'instruction': 'Review the exact acquired result and each learning choice'}
    while bounded._fits(task['id'], 'web_learning_review', payload, Review, 'reviewer'):
        payload['choices'] = [idea(i) for i in range(len(payload['choices']) * 2)]
    review = await bounded.review_proposal(task['id'], op, 'web_learning_review', payload)
    store.record('web_fixture_saved_review', op['id'], {'id': op['id'], 'review': review.model_dump()})
    reviews_before = sum(c['schema'] == 'Review' for c in gateway.calls)
    saved = store.record_get('web_fixture_saved_review', op['id'])['review']
    resumed = BoundedJudgments(engine)
    actual = {'operation': op, 'review': saved, 'sources': sources, 'actual_proposal': payload,
              'instruction': 'Consume the stored exact review; do not regenerate the acquisition or the review'}
    disposition = await resumed.disposition_proposal(task['id'], op, 'web_learning_disposition', actual)
    assert store.record_get('web_fixture_saved_review', op['id'])['review'] == saved
    assert sum(c['schema'] == 'Review' for c in gateway.calls) == reviews_before
    assert sorted(r.opinion_id for r in disposition.opinion_responses) == sorted(o['id'] for o in saved['opinions'])
    assert disposition.web_refs == ['actual-web']
    assert all(c['phase'] == 'web_learning_review' for c in gateway.calls if c['schema'] == 'Review')
    assert all(c['phase'] == 'web_learning_disposition' for c in gateway.calls if c['schema'] == 'Disposition')
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_normal_web_stage_keeps_exact_payload_and_new_learning_ids_are_scoped(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path, 'new_ideas')
    payload = {'operation': op, 'sources': [], 'instruction': 'Review actual request preparation', 'actual_detail': {'status': 'prepared'}}
    review = await bounded.review_proposal(task['id'], op, 'web_acquisition_review_before', payload)
    call = next(c for c in gateway.calls if c['schema'] == 'Review')
    assert call['phase'] == 'web_acquisition_review_before'
    assert all(call['payload'][k] == value for k, value in payload.items())
    actual = {'operation': op, 'review': review.model_dump(), 'sources': [], 'original_detail': payload['actual_detail']}
    await bounded.disposition_proposal(task['id'], op, 'web_acquisition_disposition_before', actual)
    call = next(c for c in gateway.calls if c['schema'] == 'Disposition')
    assert call['phase'] == 'web_acquisition_disposition_before'
    assert all(call['payload'][k] == value for k, value in actual.items())
    source = force_learning_pages(bounded, task['id'], op, OperationResult(operation_id=op['id'], status='succeeded').model_dump())
    required = source['required_ideas']
    value = await bounded.learning_proposal(task['id'], 'learning_proposal', source)
    record = store.records('bounded_learning')[0]
    assert len(record['parts']) > 1
    assert sum(i['id'] == 'idea1' for part in record['raw_parts'] for i in part['ideas']) == len(record['parts'])
    assert len({i.id for i in value.ideas}) == len(required) + len(record['parts'])
    assert {i['id'] for i in required} <= {i.id for i in value.ideas}
    synthesis = next(c for c in gateway.calls if c['schema'] == 'LearningSynthesis')
    assert synthesis['payload']['composition_context']
    assert synthesis['payload']['bounded_context']
    for call in gateway.calls:
        if call['schema'] == 'LearningIdeas':
            assert set(LearningIdeas.model_json_schema()['properties']) == {'ideas'}
    assert len(record['parts']) == len(record['part_receipts'])
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_interrupted_learning_keeps_first_successful_page_ids_for_parent_resume(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path, 'new_ideas')
    state = await prepare_operation(engine, task, op)
    await engine._pre(state)
    await engine._execute(state)
    row = store.get_operation(op['id'])
    payload = force_learning_pages(bounded, task['id'], op, row['result'])
    generated = gateway.generate
    observed = 0

    async def interrupt(role, phase, supplied, schema, **kwargs):
        nonlocal observed
        if schema in LEARNING_SCHEMAS:
            observed += 1
            if observed == 2:
                raise asyncio.CancelledError('Controlled interruption after one durable Learning page')
        return await generated(role, phase, supplied, schema, **kwargs)

    gateway.generate = interrupt
    with pytest.raises(asyncio.CancelledError):
        await bounded.learning_proposal(task['id'], 'learning_proposal', payload)
    successful = [r for r in store.records('bounded_model_call') if r['phase'].split(':page:')[0] == 'learning_proposal' and r['status'] == 'succeeded']
    assert len(successful) == 1
    partial = successful[0]
    assert partial['task_id'] == task['id'] and partial['operation_id'] == op['id']
    assert any(i['id'] == 'idea1' for i in partial['raw_result']['ideas'])
    stable = {i['id'] for i in partial['result']['ideas']}
    assert 'idea1' not in stable
    assert any(r['status'] == 'unobserved' for r in store.records('bounded_model_call') if r['phase'].startswith('learning_proposal'))
    recovered = engine._post_required_ideas(store.get_operation(op['id']), [])
    assert stable <= {i['id'] for i in recovered}
    gateway.generate = generated
    required = {i['id']: i for i in payload['required_ideas']}
    required.update({i['id']: i for i in recovered})
    payload['required_ideas'] = list(required.values())
    resumed = BoundedJudgments(engine)
    result = await resumed.learning_proposal(task['id'], 'learning_proposal', payload)
    assert stable <= {i.id for i in result.ideas}
    assert store.record_get('bounded_model_call', partial['id']) == partial
    assert executor.calls == [op['id']]
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_identical_learning_pages_preserve_each_exact_source_receipt(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    source = {'operation': op, 'result': OperationResult(operation_id=op['id'], status='succeeded').model_dump(),
              'required_ideas': [], 'pre': {'skills': {'selected': [], 'rejected': [], 'new_knowledge_needed': []}}}
    packet = bounded._packet(task['id'], 'learning_proposal:learning', source)
    receipts = []; outputs = []
    for label in ('first actual source decision', 'second distinct source decision'):
        payload = dict(source, source_packet_id=packet['id'], instruction=label,
                       learning_focus={'kind': 'ideas', 'source_decision': label})
        value = await bounded._call(task['id'], 'learning_proposal:page:ideas', payload, LearningIdeas)
        outputs.append(value.model_dump())
        receipts.append(deepcopy(bounded._learning_receipts[digest(value.model_dump())]))
    assert outputs[0] == outputs[1] == {'ideas': []}
    assert receipts[0]['key'] != receipts[1]['key'] and receipts[0]['id'] != receipts[1]['id']
    assert receipts[0]['payload'] != receipts[1]['payload']
    assert receipts[0]['learning_trace']['event'] != receipts[1]['learning_trace']['event']
    for receipt in receipts:
        assert store.record_get('bounded_model_call', receipt['id']) == receipt
        assert bounded._learning_call(task['id'], receipt, LearningIdeas).model_dump() == {'ideas': []}
    assert executor.calls == [] and store.records('knowledge_application') == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_research_query_receives_bounded_actual_source_context(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    payload = {'operation': op, 'target': {'pre': {'assessments': [idea(i) for i in range(250)]}},
               'instruction': 'Choose public authoritative HTTPS sources; exclude private details',
               'previous_acquisition_reviews': [{'id': 'original-failed-response', 'observation': 'Actual acquired redirect had no body'}]}
    prepared = await bounded.research_payload(task['id'], 'post_research_query', payload)
    assert prepared['instruction'] == payload['instruction']
    assert prepared['bounded_context']['source_sha256'] == digest(payload)
    assert any(p['value'] == payload for p in store.records('bounded_input'))
    assert prepared['target']['source_sha256'] == digest(payload)
    await engine.close(); store.close()


def install_output_truncation(gateway, limits):
    """Observed synthetic provider responses; no network or real billed usage."""
    original = gateway.generate
    attempts = []

    async def generate(role, phase, payload, schema, **kwargs):
        if schema is AssessmentBatch:
            count = len(payload['targets'])
        elif schema is SkillSelection:
            count = len(payload['knowledge']['skills'])
        elif schema is LearningIdeas:
            count = len(payload.get('required_ideas', []))
        elif schema is LearningApplications:
            count = len(payload.get('required_ideas', [])) + len(payload['pre']['skills']['selected'])
        elif schema is LearningSynthesis:
            count = 1 + len(payload.get('required_ideas', []))  # Lifecycle plus any owned correction Ideas.
        elif schema is Learning:
            count = 1 + len(payload.get('required_ideas', [])) + len(payload.get('pre', {}).get('skills', {}).get('selected', []))
        elif schema is Disposition:
            count = len(payload['review']['opinions']) + len(payload.get('web', {}).get('sources', payload.get('sources', [])))
        else:
            count = len(payload.get('source_records', []))
        limit = limits.get(schema, limits.get(Learning) if schema in LEARNING_SCHEMAS else None)
        if limit is not None:
            entry = {'schema': schema.__name__, 'phase': phase, 'count': count,
                     'input_sha256': digest(payload), 'truncated': count > limit}
            attempts.append(entry)
            if entry['truncated']:
                raw = '{"fixture_incomplete":' + json.dumps({'source': digest(payload)})
                metadata = {'fixture': True, 'truncated': True, 'finish_reason': 'length',
                            'usage': {'prompt_tokens': 123, 'completion_tokens': 456},
                            'input_sha256': digest(payload), 'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                            'response_diagnostic': {'sanitized_response': raw, 'diagnostic_truncated': False}}
                entry['metadata'] = deepcopy(metadata)
                raise ProviderError('Observed synthetic finish_reason=length', metadata=metadata)
        return await original(role, phase, payload, schema, **kwargs)

    gateway.generate = generate
    return attempts


@pytest.mark.asyncio
async def test_observed_truncation_reaches_complete_real_cycle_without_effect_replay(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    skills = add_skills(store, engine.knowledge, task, 6)
    attempts = install_output_truncation(gateway, {AssessmentBatch: 2, SkillSelection: 2, Learning: 2, Disposition: 2})
    state = await prepare_operation(engine, task, op)
    await engine._pre(state)
    pre = store.get_operation(op['id'])['pre_bundle']
    assert {a['target_id'] for a in pre['assessments']} == {t['id'] for t in pre['targets']}
    assert {s['id'] for s in pre['skills']['selected'] + pre['skills']['rejected']} == {s['id'] for s in skills}
    await engine._execute(state)
    await engine._post(state)
    post = store.get_operation(op['id'])['post_bundle']
    assert {a['target_id'] for a in post['assessments']} == {t['id'] for t in pre['targets']}
    required = engine._post_required_ideas(store.get_operation(op['id']), post['assessments'])
    assert {i['id'] for i in required} <= {i['id'] for i in post['learning']['ideas']}
    await engine._learn(state)
    assert executor.calls == [op['id']]
    assert (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
    assert len(store.records('skill_application')) == len(pre['skills']['selected'])
    failures = store.records('bounded_model_truncated')
    assert failures and all(f['status'] == 'failed' for f in failures)
    actual_failed = [a for a in attempts if a['truncated']]
    assert {digest(f['metadata']) for f in failures} == {digest(a['metadata']) for a in actual_failed}
    assert all(f['payload'] and f['measurement']['messages_sha256'] for f in failures)
    assert not {f['key'] for f in failures} & {c['key'] for c in store.records('bounded_model_completed')}
    for split in store.records('bounded_output_split'):
        saved = store.record_get('bounded_model_call', split['failed_call']['id'])
        assert digest(saved) == split['failed_call']['sha256']
        assert saved['metadata']['truncated'] is True
        assert len(split['children']) >= 2
    for saved in store.records('review'):
        assert {o['id'] for o in saved['review']['opinions']} == {r['opinion_id'] for r in saved['disposition']['opinion_responses']}
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_truncated_learning_resume_reuses_successful_leaf_and_stable_idea_ids(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path, 'new_ideas')
    attempts = install_output_truncation(gateway, {Learning: 2})
    payload = {'operation': op, 'result': OperationResult(operation_id=op['id'], status='succeeded').model_dump(),
               'required_ideas': [idea(i) for i in range(4)]}
    pages = bounded._page_calls

    async def pause_after_leaf(*args, **kwargs):
        async for item in pages(*args, **kwargs):
            yield item
            raise asyncio.CancelledError('Controlled stop between durable leaves, before the next model request')

    bounded._page_calls = pause_after_leaf
    with pytest.raises(asyncio.CancelledError):
        await bounded.learning_proposal(task['id'], 'learning_proposal', payload)
    completed = [r for r in store.records('bounded_model_completed') if r['phase'].startswith('learning_proposal:page:ideas')]
    assert len(completed) == 1
    first = deepcopy(completed[0]); stable_ids = {i['id'] for i in first['result']['ideas']}
    assert attempts[0]['truncated'] and attempts[-1]['schema'] == 'LearningIdeas' and not attempts[-1]['truncated']
    first_input = next(a['input_sha256'] for a in attempts if not a['truncated'])
    count_before = sum(a['input_sha256'] == first_input for a in attempts)
    resumed = BoundedJudgments(engine)
    value = await resumed.learning_proposal(task['id'], 'learning_proposal', payload)
    assert stable_ids <= {i.id for i in value.ideas}
    assert {i['id'] for i in payload['required_ideas']} <= {i.id for i in value.ideas}
    assert sum(a['input_sha256'] == first_input for a in attempts) == count_before
    assert attempts[-1]['schema'] == 'LearningSynthesis' and not attempts[-1]['truncated']
    assert store.record_get('bounded_model_call', first['id']) == first
    learning = store.records('bounded_learning')[0]
    assert len(learning['part_receipts']) == 3
    assert learning['part_receipts'][0]['key'] == first['key']
    for receipt in learning['part_receipts']:
        assert digest(store.record_get('bounded_model_call', receipt['id'])) == receipt['sha256']
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_truncated_context_and_review_preserve_exact_source_coverage_and_final_hold(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    attempts = install_output_truncation(gateway, {ContextObservation: 2, Review: 2, Disposition: 2})
    context = {'choices': [idea(i) for i in range(120)]}
    while bounded._fits(task['id'], 'truncated-context', {'operation': op, 'bounded_context': context}, Review, 'reviewer'):
        context['choices'] += [idea(i) for i in range(len(context['choices']), len(context['choices']) * 2)]
    compact = await bounded.compact_context(task['id'], 'truncated-context', context, {'operation': op}, Review, role='reviewer')
    assert compact['source_sha256'] == digest(context)
    coverage = store.record_get('bounded_context', compact['packet_id'])['source_records']
    assert {r['id'] for r in coverage} == {bounded._record('/choices/' + str(i), value)['id'] for i, value in enumerate(context['choices'])}
    payload = {'operation': op, **context}
    stored_review = await bounded.review_proposal(task['id'], op, 'web_learning_review', payload)
    review_count = sum(a['schema'] == 'Review' for a in attempts)
    review = stored_review.model_copy(update={'opinions': [*stored_review.opinions,
        *Review(summary='Separate concerns', opinions=[{'id': 'hold-' + str(i), 'observation': 'hold this', 'rationale': 'Retain this exact concern'} for i in range(3)]).opinions]})
    sources = [{'id': 'retained-source', 'text': 'Actual fixture evidence; no network'}]
    disposition = await bounded.disposition_proposal(task['id'], op, 'web_learning_disposition',
        {'operation': op, 'review': review.model_dump(), 'sources': sources, 'actual_proposal': payload})
    assert disposition.verdict == 'hold'
    assert {r.opinion_id for r in disposition.opinion_responses} == {o.id for o in review.opinions}
    assert disposition.web_refs == ['retained-source']
    assert sum(a['schema'] == 'Review' for a in attempts) == review_count
    assert {'ContextObservation', 'Review', 'Disposition'} <= {a['schema'] for a in attempts if a['truncated']}
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_indivisible_truncation_is_a_durable_capacity_boundary_until_capacity_changes(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    limits = {AssessmentBatch: 0}
    attempts = install_output_truncation(gateway, limits)
    targets = [{'id': 'only-target', 'statement': 'An indivisible full assessment'}]
    for adapter in (bounded, BoundedJudgments(engine)):
        with pytest.raises(InputCapacityError, match='output was truncated.*indivisible') as caught:
            await adapter.assess_targets(task['id'], 'one-item', targets, {'operation': op})
        assert caught.value.metadata['usage']['completion_tokens'] == 456
    assert len(attempts) == 1 and not store.records('bounded_model_completed')
    saved = deepcopy(store.records('bounded_model_truncated')[0])
    # Explicitly configured fixture change; this test does not change an actual
    # contracted model's capacity or claim the setting enlarges real capability.
    gateway.settings.values['max_output_tokens'] += 8192
    gateway.settings.values['model_context_tokens'] += 8192
    limits[AssessmentBatch] = 1
    actual = await BoundedJudgments(engine).assess_targets(task['id'], 'one-item', targets, {'operation': op})
    assert [a['target_id'] for a in actual] == ['only-target']
    assert len(attempts) == 2 and store.record_get('bounded_model_call', saved['id']) == saved
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['provider', 'model', 'reasoning', 'api_mode', 'context', 'output',
                                  'role', 'schema', 'phase', 'source', 'policy', 'payload', 'model_input'])
async def test_completed_configured_judgment_never_crosses_changed_binding(tmp_path, monkeypatch, change):
    store, engine, bounded, task, op, gateway, _ = make_runtime(tmp_path)
    gateway.settings.values.update(base_url='https://fixture.invalid/v1', model='fixture-model',
        review_model='fixture-reviewer', api_mode='compatible', reasoning_effort=None,
        user_agent='configured-receipt-fixture')
    payload = {'source_records': [{'id': 'source-a', 'value': {'last': 'fact', 'first': 'original'}}]}
    await bounded._call(task['id'], 'configuration-recovery', payload, ContextObservation)
    original = deepcopy(store.records('bounded_model_completed')[-1])
    calls_before = len(gateway.calls)
    # The unchanged result has an actual configured receipt and is reused.
    await bounded._call(task['id'], 'configuration-recovery', json.loads(json.dumps(payload, sort_keys=True)), ContextObservation)
    assert len(gateway.calls) == calls_before
    phase = 'configuration-recovery'; role = None; schema = ContextObservation
    if change in {'provider', 'model', 'reasoning', 'api_mode'}:
        field = {'provider': 'base_url', 'model': 'model', 'reasoning': 'reasoning_effort', 'api_mode': 'api_mode'}[change]
        gateway.settings.values[field] = {'provider': 'https://other.invalid/v1', 'model': 'other-model',
                                         'reasoning': 'high', 'api_mode': 'deepagents'}[change]
    elif change == 'context': gateway.settings.values['model_context_tokens'] += 2048
    elif change == 'output': gateway.settings.values['max_output_tokens'] += 1024
    elif change == 'role': role = 'reviewer'
    elif change == 'schema': schema = Review
    elif change == 'phase': phase += '-changed'
    elif change == 'source': store.append_instruction(task['id'], 'A new exact source condition.', task['source_hash'])
    elif change == 'policy': monkeypatch.setattr(engine.policy, 'hash', digest('changed test policy identity'))
    elif change == 'payload': payload['source_records'][0]['value']['first'] = 'changed'
    elif change == 'model_input':
        # A real newly saved operation changes the controller's history input
        # while leaving this payload and task source untouched.
        store.save_operation(task['id'], op)
    value = await bounded._call(task['id'], phase, payload, schema, role=role)
    assert value is not None and len(gateway.calls) == calls_before + 1
    assert store.record_get('bounded_model_call', original['id']) == original
    assert store.record_get('bounded_model_completed', original['key']) == original
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_legacy_completed_result_with_missing_provider_binding_is_not_resent(tmp_path):
    store, engine, bounded, task, _, gateway, _ = make_runtime(tmp_path)
    gateway.settings.values.update(base_url='https://fixture.invalid/v1', model='fixture-model',
        api_mode='compatible', reasoning_effort=None, user_agent='configured-receipt-fixture')
    payload = {'source_records': [{'id': 'source-a', 'value': {'last': 'fact', 'first': 'original'}}]}
    phase = 'legacy-completed-result'
    await bounded._call(task['id'], phase, payload, ContextObservation)
    original = deepcopy(store.records('bounded_model_completed')[-1])
    old = deepcopy(original)
    old['measurement'].pop('configuration')
    old['key'] = digest({'task_id': task['id'], 'source_hash': task['source_hash'], 'policy_hash': engine.policy.hash,
        'phase': phase, 'role': task['actor'], 'schema': ContextObservation.model_json_schema(),
        'payload': payload, 'measurement': old['measurement']})
    # Prior-format fixture. The actual successful fixture event deliberately
    # lacks retained provider/model metadata, so the old binding is unknown.
    store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('bounded_model_completed', original['key']))
    store.record('bounded_model_call', old['id'], old)
    store.record('bounded_model_completed', old['key'], old)
    calls = deepcopy(gateway.calls)
    records = deepcopy(store.records('bounded_model_call'))
    with pytest.raises(ConfigurationRequired, match='saved bounded result predates.*configuration binding'):
        await BoundedJudgments(engine)._call(task['id'], phase, payload, ContextObservation)
    assert gateway.calls == calls
    assert store.records('bounded_model_call') == records
    assert store.record_get('bounded_model_completed', old['key']) == old
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['provider', 'model', 'context', 'output', 'role', 'schema', 'phase',
                                  'source', 'policy', 'payload', 'model_input', 'partition', 'response_binding'])
async def test_legacy_partition_cannot_match_changed_source_or_request(tmp_path, monkeypatch, change):
    from tests.test_web_learning_resume import PackedResumeGateway, legacy_after_receipts

    store, engine, _, task, op, _, _ = make_runtime(tmp_path)
    first = PackedResumeGateway(engine.policy, legacy_order=True, stop_count=14)
    first.response_store = store; engine.gateway = first
    bounded = BoundedJudgments(engine); engine.bounded_judgments = bounded
    targets = [{'id': 'target-' + str(index), 'statement': 'Retain choice ' + str(index)} for index in range(29)]
    with pytest.raises(ProviderError, match='Synthetic smaller request interruption'):
        await bounded.assess_targets(task['id'], first.after_phase, targets,
                                     {'actual_result': {'last': {'z': 1, 'a': 2}, 'first': 'original'}})
    legacy_after_receipts(store, task, engine.policy)
    failed = deepcopy(next(record for record in store.records('bounded_model_truncated') if record['phase'] == first.after_phase))
    original_splits = deepcopy(store.records('bounded_output_split'))
    payload = deepcopy(failed['payload'])
    phase = first.after_phase; role = None; schema = AssessmentBatch
    resumed = PackedResumeGateway(engine.policy)
    resumed.response_store = store; engine.gateway = resumed
    bounded = BoundedJudgments(engine); engine.bounded_judgments = bounded
    if change in {'provider', 'model'}:
        resumed.settings.values['base_url' if change == 'provider' else 'model'] = 'https://other.invalid/v1' if change == 'provider' else 'other-model'
    elif change == 'context': resumed.settings.values['model_context_tokens'] += 2048
    elif change == 'output': resumed.settings.values['max_output_tokens'] += 1024
    elif change == 'role': role = 'reviewer'
    elif change == 'schema': schema = Review
    elif change == 'phase': phase += '-changed'
    elif change == 'source': store.append_instruction(task['id'], 'A new exact source condition.', task['source_hash'])
    elif change == 'policy': monkeypatch.setattr(engine.policy, 'hash', digest('changed test policy identity'))
    elif change == 'payload': payload['actual_result']['first'] = 'changed'
    elif change == 'model_input': store.save_operation(task['id'], op)
    elif change == 'partition':
        # A self-consistent record hash cannot conceal a changed partition.
        split = deepcopy(original_splits[0]); old_id = split.pop('id')
        split['children'][0]['items_sha256'] = digest('different child')
        split['id'] = digest(split)
        store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('bounded_output_split', old_id))
        store.record('bounded_output_split', split['id'], split)
    elif change == 'response_binding':
        changed = deepcopy(failed)
        changed['metadata']['response_record']['messages_sha256'] = digest('different original wire')
        store.record('bounded_model_call', changed['id'], changed)
        store.record('bounded_model_truncated', changed['key'], changed)
        split = deepcopy(original_splits[0]); old_id = split.pop('id')
        split['failed_call']['sha256'] = digest(changed); split['id'] = digest(split)
        store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('bounded_output_split', old_id))
        store.record('bounded_output_split', split['id'], split)
    build = lambda part: dict(payload, targets=part, target={'ids': [target['id'] for target in part]})
    result = [item async for item in bounded._complete_page(task['id'], phase, targets, build, schema, role=role)]
    assert result
    if phase == first.after_phase and schema is AssessmentBatch:
        assert resumed.after_calls[0] == [target['id'] for target in targets]
    else:
        assert resumed.inputs[0][0] == phase and len(resumed.inputs[0][1]['targets']) == 29
    assert not any(record.get('basis') == 'legacy-partition-only' for record in store.records('bounded_output_split'))
    if change != 'response_binding':
        assert store.record_get('bounded_model_call', failed['id']) == failed
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [
    {'truncated': False, 'finish_reason': 'refusal'},
    {'request_effect': 'response-unobserved', 'usage': None},
    {'truncated': 'true', 'finish_reason': 'length'},
])
async def test_other_provider_failures_never_enter_automatic_output_recovery(tmp_path, metadata):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(True)
        raise ProviderError('Controlled non-recoverable response', metadata=deepcopy(metadata))

    gateway.generate = fail
    with pytest.raises(ProviderError, match='non-recoverable'):
        await bounded.assess_targets(task['id'], 'other-fault', [{'id': str(i)} for i in range(4)], {'operation': op})
    assert len(calls) == 1
    assert store.records('bounded_model_call')[0]['metadata'] == metadata
    assert not store.records('bounded_output_split') and not store.records('bounded_model_truncated')
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_schema_feedback_then_truncation_retains_the_actual_final_request(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    normal = gateway.generate
    targets = [{'id': 'feedback-' + str(i), 'statement': 'Exact target ' + str(i)} for i in range(4)]
    sent = []

    async def generate(role, phase, payload, schema, **kwargs):
        sent.append(deepcopy(payload))
        if schema is AssessmentBatch and len(payload['targets']) == 4:
            if 'actual_format_feedback' not in payload:
                raise ProviderError('Observed synthetic JSON schema defect', metadata={
                    'validation_diagnostic': {'kind': 'synthetic-schema', 'message': 'Missing assessment field'},
                    'response_diagnostic': {'sanitized_response': '{"fixture_schema_error":true}'},
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 12}})
            raise ProviderError('Observed synthetic output after schema feedback is incomplete', metadata={
                'truncated': True, 'finish_reason': 'length',
                'usage': {'prompt_tokens': 21, 'completion_tokens': 22},
                'response_diagnostic': {'sanitized_response': '{"fixture_truncated":'}})
        return await normal(role, phase, payload, schema, **kwargs)

    gateway.generate = generate
    actual = await bounded.assess_targets(task['id'], 'feedback-before-truncation', targets, {'operation': op})
    assert [item['target_id'] for item in actual] == [target['id'] for target in targets]
    assert [len(item['targets']) for item in sent] == [4, 4, 2, 2]
    failure = store.records('bounded_model_truncated')[0]
    assert failure['actual_model_input'] == sent[1]
    assert failure['actual_model_input_sha256'] == digest(sent[1])
    assert failure['measurement']['model_input_sha256'] == digest(sent[0])
    assert 'actual_format_feedback' not in failure['payload']
    assert failure['metadata']['usage']['completion_tokens'] == 22
    assert any(e['status'] == 'rejected_model_output' for e in store.events(task['id']))
    assert executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_large_result_anchor_consumes_original_knowledge_application_and_rejects_changed_refs(tmp_path):
    store, engine, bounded, task, op, gateway, executor = make_runtime(tmp_path)
    skills = add_skills(store, engine.knowledge, task, 1)
    execute = executor.execute

    async def large_result(workspace, operation):
        result = await execute(workspace, operation)
        return result.model_copy(update={'data': {**result.data, 'children': [
            {'id': 'fixture-child-' + str(i), 'operations': [{'observed': 'Owned synthetic child-result content ' * 1200}],
             'knowledge': {'verification': 'Explicit fixture history; no live child claim'}} for i in range(2)]}})

    executor.execute = large_result
    state = await prepare_operation(engine, task, op)
    await engine._pre(state)
    await engine._execute(state)
    original = deepcopy(store.get_operation(op['id'])['result'])
    await engine._post(state)
    await engine._learn(state)
    row = store.get_operation(op['id'])
    assert row['status'] == 'learned' and row['result'] == original
    assert executor.calls == [op['id']] and (Path(task['workspace']) / 'answer.txt').read_text() == 'hello'
    application = row['post_bundle']['learning']['applications'][0]
    assert application['skill_id'] == skills[0]['id']
    assert application['result_sha256'] == digest(original)
    assert application['evidence'][0]['pointer'] == '/data'
    assert application['evidence'][0]['sha256'] == digest(original['data'])
    assert len(store.records('skill_application')) == 1
    leaf = next(r for r in store.records('bounded_model_completed') if r['payload'].get('learning_anchor_ref'))
    valid = deepcopy(leaf['payload'])
    contract = bounded.learning_application_contract(task['id'], valid)
    assert contract['result_sha256'] == digest(original)
    from policy_harness.models import learning_applications_schema
    sent = engine._model_input(task['id'], 'learning_proposal', valid, Learning)
    assert sent['application_contract']['applications'] == learning_applications_schema()
    call_count = len(gateway.calls)
    for reference in (None, {}, {'id': [], 'sha256': '0' * 64}, {'id': '0' * 64, 'sha256': '0' * 64}):
        with pytest.raises(PolicyError):
            engine._model_input(task['id'], 'learning_proposal', {**valid, 'learning_anchor_ref': reference}, Learning)
    with pytest.raises(PolicyError, match='view differs'):
        engine._model_input(task['id'], 'learning_proposal', {**valid, 'result': {**valid['result'], 'effect': 'unknown'}}, Learning)
    foreign = store.create_task('Other explicit fixture task', ['No shared source authority'])
    with pytest.raises(PolicyError, match='another task'):
        engine._model_input(foreign['id'], 'learning_proposal', valid, Learning)
    anchor = store.record_get('bounded_learning_anchors', valid['learning_anchor_ref']['id'])
    packet = store.record_get('bounded_input', anchor['source_packet_id'])
    store.record('bounded_input', packet['id'], {**packet, 'value': {**packet['value'], 'result': {}}})
    with pytest.raises(PolicyError, match='packet changed'):
        engine._model_input(task['id'], 'learning_proposal', valid, Learning)
    store.record('bounded_input', packet['id'], packet)
    observation = store.record_get('bounded_context', anchor['observed_context_id'])
    store.record('bounded_context', observation['id'], {**observation, 'source_records': []})
    with pytest.raises(PolicyError, match='source observation'):
        engine._model_input(task['id'], 'learning_proposal', valid, Learning)
    store.record('bounded_context', observation['id'], observation)
    store.append_instruction(task['id'], 'New exact fixture instruction', store.get_task(task['id'])['source_hash'])
    with pytest.raises(PolicyError, match='stale'):
        engine._model_input(task['id'], 'learning_proposal', valid, Learning)
    assert len(gateway.calls) == call_count
    await engine.close(); store.close()
