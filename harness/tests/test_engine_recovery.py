"""Real local execution/recovery with explicit, non-network judgment fixtures.

These tests observe controller, SQLite, files and executor journals. They do not
prove model judgment quality, live provider behavior or complete policy adherence.
"""
import asyncio
import hashlib
import json
import shutil
from copy import deepcopy
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import (
    Assessment, Completion, ConfigurationRequired, Disposition, Operation, OperationResult,
    PolicyError,
)
from policy_harness.policy import PolicyCatalog
from policy_harness.store import Store, digest
from .test_core import FixtureGateway, FixtureWeb, POLICY
from .fixture_preparation import prepare_operation


def proposed(kind, args):
    return Operation(kind=kind, args=args, purpose='Complete the owned fixture goal',
                     expected_result='An observed, source-bound result',
                     decisions=[{'id': 'fixture-choice', 'statement': kind,
                                 'rationale': 'Explicit isolated test scenario'}])


class ScenarioGateway(FixtureGateway):
    """Only change scenario decisions; reuse the complete ordinary review route."""
    def __init__(self, policy, *, selector=None, completion_failure=None,
                 revise_learning=False):
        super().__init__(policy)
        self.selector = selector
        self.completion_failure = completion_failure
        self.completion_count = 0
        self.revise_learning = revise_learning
        self.revised = False

    async def generate(self, role, phase, payload, schema):
        if schema is Operation and self.selector is not None:
            selected = self.selector(payload)
            if selected is not None:
                self.calls.append((role, phase, payload))
                return selected, {'fixture': True, 'usage': None, 'cost': None}
        value, usage = await super().generate(role, phase, payload, schema)
        if schema is Completion:
            self.completion_count += 1
            if self.completion_count == 1 and self.completion_failure is not None:
                value = value.model_dump()
                value['acceptance'][0]['achieved'] = self.completion_failure
        if (schema is Disposition and self.revise_learning and not self.revised
                and phase == 'learning-outcome_disposition'
                and payload['operation']['kind'] == 'file_write'):
            self.revised = True
            value = value.model_copy(update={
                'verdict': 'revise',
                'rationale': 'Read the committed file through normal governed work, '
                             'then resolve this review with that subsequent evidence.'})
        return value, usage


def build_runtime(directory, *, policy_path=POLICY, gateway_factory=ScenarioGateway):
    store = Store(directory)
    policy = PolicyCatalog(policy_path)
    executor = Executor(directory)
    gateway = gateway_factory(policy)
    engine = Engine(store, policy, executor, gateway, FixtureWeb(), Knowledge(store))
    return store, engine, executor, gateway


@asynccontextmanager
async def local_runtime(directory, **kwargs):
    runtime = build_runtime(directory, **kwargs)
    try:
        yield runtime
    finally:
        await runtime[1].close()
        runtime[0].close()


async def bounded_run(engine, task_id, timeout=45):
    result = await asyncio.wait_for(engine.run_task(task_id), timeout=timeout)
    assert result['task']['status'] != 'stopped', 'Fixture exceeded bounded execution'
    return result


def completed(result):
    assert result['task']['status'] == 'completed', [
        (e['stage'], e['status'], e['detail']) for e in result['events'][-3:]]


def journal(executor):
    return [json.loads(p.read_text(encoding='utf-8'))
            for p in sorted(executor.journal.glob('*.json'))]


def receipt_bytes(executor, operation_id):
    matches = [p for p in executor.journal.glob('*.json')
               if json.loads(p.read_text(encoding='utf-8'))['operation']['id'] == operation_id]
    assert len(matches) == 1, 'One actual owned receipt must match the operation identity'
    return matches[0].read_bytes()


def operation_rows(result, kind):
    return [row for row in result['operations'] if row['operation']['kind'] == kind]


@pytest.mark.asyncio
async def test_real_file_delivery_and_actual_cleanup_have_before_after_judgments(tmp_path):
    async with local_runtime(tmp_path / 'data') as (store, engine, executor, gateway):
        task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
        result = await bounded_run(engine, task['id'])
        completed(result)
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        assert len(journal(executor)) == 2
        assert all(x['status'] == 'cycle_complete' for x in result['operations'])
        final = result['task']['final']
        targets = {x['id'] for x in final['individual_choices']}
        assert targets == {x['target_id'] for x in final['pre_assessments']}
        assert targets == {x['target_id'] for x in final['post_assessments']}
        assert any(x.startswith('completion/') for x in targets)
        assert any(x.startswith('cleanup/') for x in targets)
        assert not any(x['needs_cleanup'] for x in result['knowledge']['skills'])
        assert all(x['effect'] == 'UNUSED_EFFECT_UNVERIFIED'
                   for x in result['knowledge']['skills'])
        events = result['events']
        before = max(e['seq'] for e in events
                     if e['stage'].startswith('completion_choices_before') and e['status'] == 'succeeded')
        cleanup = next(e['seq'] for e in events if e['stage'] == 'cleanup')
        after = min(e['seq'] for e in events
                    if e['stage'].startswith('completion_choices_after') and e['status'] == 'started')
        review = next(e['seq'] for e in events
                      if e['stage'] == 'completion_outcome_disposition' and e['status'] == 'succeeded')
        publication = next(e['seq'] for e in events if e['stage'] == 'final')
        assert before < cleanup < after < review < publication
        assert store.verify_events()


@pytest.mark.asyncio
async def test_string_false_acceptance_is_rejected_without_publishing(tmp_path):
    factory = lambda policy: ScenarioGateway(policy, completion_failure='false')
    async with local_runtime(tmp_path / 'data', gateway_factory=factory) as (store, engine, executor, _):
        task = store.create_task('Write hello', ['answer.txt contains hello'])
        result = await bounded_run(engine, task['id'])
        assert result['task']['status'] == 'attention_required'
        assert result['task']['final'] is None
        assert not any(e['stage'] == 'final' for e in result['events'])
        assert any(e['stage'] == 'completion_proposal' and e['status'] == 'failed'
                   and e['detail']['error_type'] == 'ValidationError' for e in result['events'])
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        assert len(journal(executor)) == 2


@pytest.mark.asyncio
async def test_false_acceptance_returns_to_real_work_before_later_completion(tmp_path):
    def selector(payload):
        kinds = [r['operation']['kind'] for r in payload['operations']]
        if kinds.count('finish') == 1 and kinds.count('file_read') == 1:
            return proposed('file_read', {'path': 'answer.txt'})
    factory = lambda policy: ScenarioGateway(policy, selector=selector, completion_failure=False)
    async with local_runtime(tmp_path / 'data', gateway_factory=factory) as (store, engine, executor, gateway):
        task = store.create_task('Write hello', ['answer.txt contains hello'])
        result = await bounded_run(engine, task['id'])
        completed(result)
        assert gateway.completion_count == 2
        finishes = operation_rows(result, 'finish')
        assert len(finishes) == 2
        assert 'Completion evidence' in finishes[0]['completion_deferred']
        kinds = [r['operation']['kind'] for r in result['operations']]
        assert kinds[-3:] == ['finish', 'file_read', 'finish']
        assert len(journal(executor)) == 3
        assert sum(e['stage'] == 'final' for e in result['events']) == 1


class SimulatedControllerLoss(BaseException):
    """Injected after real Executor return, before controller result persistence."""


@pytest.mark.asyncio
async def test_real_write_receipt_recovers_after_lost_controller_result_without_replay(tmp_path):
    directory = tmp_path / 'data'
    store, engine, executor, _ = build_runtime(directory)
    task = store.create_task('Write hello', ['answer.txt contains hello'])
    original_update = store.update_operation
    lost = []

    def lose_one_write_result(identity, **changes):
        if (not lost and changes.get('status') == 'result_recorded'
                and store.get_operation(identity)['operation']['kind'] == 'file_write'):
            lost.append(identity)
            raise SimulatedControllerLoss('Actual write finished; controller result not committed')
        return original_update(identity, **changes)

    store.update_operation = lose_one_write_result
    try:
        with pytest.raises(SimulatedControllerLoss):
            await bounded_run(engine, task['id'])
        assert len(lost) == 1
        assert store.get_operation(lost[0])['status'] == 'executing'
        assert store.get_operation(lost[0])['result'] is None
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        assert len(journal(executor)) == 1
        before = receipt_bytes(executor, lost[0])
    finally:
        await engine.close()
        store.close()

    def selector(payload):
        unknown = next((r for r in payload['operations']
                        if (r['result'] or {}).get('effect') == 'unknown'), None)
        if unknown:
            return proposed('reconcile', {'operation_id': unknown['id']})
    factory = lambda policy: ScenarioGateway(policy, selector=selector)
    async with local_runtime(directory, gateway_factory=factory) as (store, engine, executor, _):
        assert store.get_task(task['id'])['status'] == 'recovery_required'
        assert store.get_operation(lost[0])['result']['effect'] == 'unknown'
        result = await bounded_run(engine, task['id'])
        completed(result)
        write = store.get_operation(lost[0])
        assert write['result']['status'] == 'succeeded'
        assert write['result']['effect'] == 'confirmed'
        history = store.record_get('effect_reconciliation', write['reconciliation_ref'])
        assert history['original']['result']['effect'] == 'unknown'
        assert history['reconciled']['operation_id'] == lost[0]
        assert len(operation_rows(result, 'file_write')) == 1
        assert len(operation_rows(result, 'reconcile')) == 1
        assert len(journal(executor)) == 2
        assert receipt_bytes(executor, lost[0]) == before
        assert store.verify_events()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,args', [
    ('file_write', {'path': 'unobserved.txt', 'text': 'unobserved'}),
    ('delegate', {'objective': 'Unobserved child', 'acceptance': ['unobserved']}),
    ('web_fetch', {'url': 'https://fixture.invalid/unobserved'}),
])
async def test_missing_domain_receipt_stays_unknown_after_full_reconciliation_cycle(tmp_path, kind, args):
    async with local_runtime(tmp_path / 'data') as (store, engine, executor, _):
        task = store.create_task('Recover explicitly unknown fixture state', ['Preserve unknown evidence'])
        # This is seeded lost-state coverage, not a claim that a live crash occurred.
        original = proposed(kind, args)
        store.save_operation(task['id'], original.model_dump(), policy_hash=engine.policy.hash)
        unknown = OperationResult(operation_id=original.id, status='unknown', effect='unknown',
                                  stderr='Seeded fixture: original result and domain journal unavailable')
        store.update_operation(original.id, status='cycle_complete', result=unknown.model_dump())
        reconcile = proposed('reconcile', {'operation_id': original.id})
        store.save_operation(task['id'], reconcile.model_dump(), policy_hash=engine.policy.hash)
        cycle = uuid4().hex
        store.update_task(task['id'], state={'operation_id': reconcile.id, 'cycle_id': cycle})
        graph = await engine._graph()
        await asyncio.wait_for(graph.ainvoke({'task_id': task['id'], 'operation_id': reconcile.id},
            {'configurable': {'thread_id': task['id'] + ':' + cycle}}), 20)
        recovered = store.get_operation(reconcile.id)
        assert recovered['status'] == 'cycle_complete'
        assert recovered['result']['data']['reconciliation']['effect'] == 'unknown'
        assert store.get_operation(original.id)['result'] == unknown.model_dump()
        assert store.records('effect_reconciliation') == []
        assert len(store.list_tasks()) == 1
        assert journal(executor) == []
        assert not (Path(task['workspace']) / 'unobserved.txt').exists()


@pytest.mark.asyncio
async def test_unconsumed_exact_permit_survives_reopen_and_only_executes_once(tmp_path):
    directory = tmp_path / 'data'
    store, engine, executor, _ = build_runtime(directory)
    task = store.create_task('Write once', ['one exact write'])
    operation = proposed('file_write', {'path': 'answer.txt', 'text': 'hello'})
    state = await prepare_operation(engine, task, operation)
    try:
        await engine._pre(state)
        row = store.get_operation(operation.id)
        bindings = (task['id'], operation.id, digest(operation.model_dump()),
                    engine.policy.hash, digest(row['pre_bundle']))
        token = store.issue_permit(*bindings)
        with pytest.raises(PolicyError):
            store.issue_permit(*bindings[:-1], 'changed-bundle')
        assert journal(executor) == []
    finally:
        await engine.close()
        store.close()
    async with local_runtime(directory) as (store, engine, executor, _):
        assert store.issue_permit(*bindings) == token
        await engine._execute(state)
        receipt = receipt_bytes(executor, operation.id)
        await engine._execute(state)
        assert len(journal(executor)) == 1
        assert receipt_bytes(executor, operation.id) == receipt
        permit = store.db.execute('SELECT * FROM permits WHERE operation_id=?', (operation.id,)).fetchone()
        assert permit['id'] == token and permit['consumed'] == 1
        assert store.get_operation(operation.id)['result']['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_exact_fixture_amendment_reloads_and_same_task_adopts_with_q18(tmp_path):
    policy_dir = tmp_path / 'isolated-policy'
    policy_dir.mkdir()
    policy_path = policy_dir / POLICY.name
    shutil.copyfile(POLICY, policy_path)
    shutil.copyfile(POLICY.parent / 'answer-Q18.json', policy_dir / 'answer-Q18.json')
    original_bytes = policy_path.read_bytes()
    directory = tmp_path / 'data'

    def gateway_factory(policy):
        identity = 'A-01'
        old = policy.conditions[identity]['body']
        def selector(payload):
            if any(r['operation']['kind'] == 'policy_amendment' for r in payload['operations']):
                raise ConfigurationRequired('Fixture boundary: isolated exact user answer required')
            return proposed('policy_amendment', {
                'condition_id': identity, 'old_text': old,
                'new_text': old + '\nFixture-only clarification for this temporary policy copy.',
                'reason': 'Exercise exact approved amendment persistence',
                'impact': 'This disposable fixture policy only',
                'recovery': 'Discard this disposable fixture policy and database'})
        return ScenarioGateway(policy, selector=selector)

    store, engine, _, _ = build_runtime(directory, policy_path=policy_path,
                                        gateway_factory=gateway_factory)
    task = store.create_task('Write hello with approved current policy', ['answer.txt contains hello'])
    try:
        original_hash = engine.policy.hash
        result = await bounded_run(engine, task['id'])
        assert result['task']['status'] == 'configuration_required', result['events'][-3:]
        approval, = engine.pending_approvals(task['id'])
        with pytest.raises(PolicyError):
            engine.resolve_approval(approval['id'], 'approve', 'stale-fixture-hash')
        assert engine.policy.hash == original_hash
        engine.resolve_approval(approval['id'], 'approve', approval['proposal_hash'],
                                'Explicit isolated fixture user response; no production approval')
        approved_hash = engine.policy.hash
        assert approved_hash != original_hash
    finally:
        await engine.close()
        store.close()
    async with local_runtime(directory, policy_path=policy_path) as (store, engine, executor, _):
        assert engine.policy.hash == approved_hash
        assert len(engine.policy.conditions) == 236 and len(engine.policy.rules) == 18
        assert 'R18' in engine.policy.rules
        result = await bounded_run(engine, task['id'])
        completed(result)
        adopted, = operation_rows(result, 'adopt_policy')
        assert adopted['operation']['args']['old_policy_hash'] == original_hash
        assert adopted['operation']['args']['new_policy_hash'] == approved_hash
        assert adopted['operation']['args']['original_acceptance'] == task['acceptance']
        assert result['task']['id'] == task['id']
        assert result['task']['objective'] == task['objective']
        assert result['task']['policy_hash'] == result['task']['final']['policy_hash'] == approved_hash
        assert policy_path.read_bytes() == original_bytes
        assert len(journal(executor)) == 2
        assert store.verify_events()


@pytest.mark.asyncio
async def test_after_learning_revision_uses_new_real_evidence_without_reapplying_write(tmp_path):
    def selector(payload):
        corrections = payload['required_judgment_corrections']
        if not corrections:
            return None
        correction = corrections[0]
        evidence = [r for r in payload['operations']
                    if r['operation']['kind'] == 'file_read' and r['result']
                    and r['result']['started_at'] >= correction['created_at']]
        if not evidence:
            return proposed('file_read', {'path': 'answer.txt'})
        return proposed('judgment_resolve', {
            'review_id': correction['id'], 'expected_hash': correction['hash'],
            'reason': 'Actual subsequent file read confirmed the committed requested contents',
            'evidence_operation_ids': [evidence[-1]['id']]})
    factory = lambda policy: ScenarioGateway(policy, selector=selector, revise_learning=True)
    async with local_runtime(tmp_path / 'data', gateway_factory=factory) as (store, engine, executor, gateway):
        task = store.create_task('Write hello', ['answer.txt contains hello'])
        result = await bounded_run(engine, task['id'])
        completed(result)
        assert gateway.revised
        correction, = store.records('judgment_correction')
        assert correction['status'] == 'resolved'
        resolution, = operation_rows(result, 'judgment_resolve')
        assert correction['resolution_operation_id'] == resolution['operation']['id']
        original, = operation_rows(result, 'file_write')
        assert correction['operation_id'] == original['operation']['id']
        history, = store.records('judgment_correction_history')
        assert history['status'] == 'pending' and history['id'] == correction['id']
        assert len(journal(executor)) == 2
        applied, = [x for x in store.records('applied_knowledge_review')
                    if x['id'].startswith(original['operation']['id'] + ':learning-outcome:')]
        assert applied['id']==original['operation']['id']+':learning-outcome:'+applied['input_hash']
        assert applied['review']['id']==correction['id']
        assert applied['outcome_hash']==digest(correction['actual_result'])
        assert all(r['result']['effect'] != 'unknown' for r in result['operations'])
        # An ordinary operation's post-learning correction remains ordinary
        # work. The Web-specific continuation does not park unrelated cycles.
        assert not result['task']['state'].get('web_correction_returns')
        assert not store.records('web_correction_continuation')
        assert not engine.web_judgments.correction_frontier(task['id'])


@pytest.mark.asyncio
async def test_real_child_result_integrates_and_every_child_prompt_carries_lease(tmp_path):
    from policy_harness.model_routing import resolve_lease
    from tests.test_model_routing import configure, selection
    settings=configure(tmp_path/'lease-settings')
    def selector(payload):
        if payload['delegation_lease']:
            return None
        # This scenario fixture chooses from real retained observations; the
        # compact model navigation view is deliberately not a full data record.
        # Model history reading is covered separately, not certified here.
        rows = store.operations(payload['task_id'])
        kinds = [r['operation']['kind'] for r in rows]
        if 'delegate' not in kinds:
            return proposed('delegate', {
                'objective': 'Child: write hello in answer.txt',
                'acceptance': ['answer.txt contains hello'],
                'independent_scope': 'Only the separate child workspace answer.txt',
                'integration_plan': 'Parent consumes the completed child file by exact hash',
                'capability_requirements':['Produce and verify one file in a separate owned child workspace'],
                'quality_requirements':['Retain original task scope and all actual result reviews'],
                'cost_considerations':'Use the configured fixture roles; real provider cost is unobserved',
                'model_selections':{r:selection(settings,r) for r in ('parent','worker','reviewer')}})
        delegated = next(r for r in rows if r['operation']['kind'] == 'delegate')
        child_id = delegated['result']['data']['child_id']
        if 'wait_children' not in kinds:
            return proposed('wait_children', {
                'child_ids': [child_id],
                'why_no_independent_work': 'The one required child artifact is the remaining dependency'})
        if 'child_integrate' not in kinds:
            waited = next(r for r in rows if r['operation']['kind'] == 'wait_children')
            child = waited['result']['data']['children'][0]
            observed = next(r for r in child['operations'] if r['operation']['kind'] == 'file_read')
            return proposed('child_integrate', {
                'child_id': child_id, 'source_path': 'answer.txt', 'destination_path': 'answer.txt',
                'sha256': observed['result']['data']['sha256'], 'expected_sha256': None})
        if 'file_read' not in kinds:
            return proposed('file_read', {'path': 'answer.txt'})
        pending = [c for c in engine.knowledge.retrieve(store.get_task(payload['task_id']))['candidates'] if c['status'] == 'pending']
        if pending:
            candidate = pending[0]
            return proposed('candidate_disposition', {
                'candidate_id': candidate['candidate_id'], 'expected_hash': candidate['candidate_hash'],
                'expected_state_hash': candidate['state_hash'], 'disposition': 'reject',
                'reason': 'Fixture retains original child knowledge history but does not adopt additional shared knowledge changes.'})
        return proposed('finish', {'summary': 'Observed the integrated child file'})
    class LeaseScenario(ScenarioGateway):
        supports_model_leases=True
        async def generate(self,role,phase,payload,schema,*,model_lease=None,job_context=None):
            if model_lease:
                resolve_lease(self.settings,model_lease,role=role,job_context=job_context)
            # Keep the mandatory nonempty assessment and every required idea.
            return await super().generate(role,phase,payload,schema)
    def factory(policy):
        gateway=LeaseScenario(policy,selector=selector);gateway.settings=settings
        return gateway
    async with local_runtime(tmp_path / 'data', gateway_factory=factory) as (store, engine, executor, gateway):
        task = store.create_task('Integrate a child-produced hello file', ['answer.txt contains hello'])
        # Full child history now has actual paged readers and post-review
        # consumers. The prior 120s observation ended after wait_children's
        # post-review, before integration; final assertions remain mandatory.
        # The retained 600s run reached real child completion/integration and
        # eight candidate dispositions with 7250 ideas. Give that observed
        # finite full consumer additional time; do not remove required ideas or
        # weaken the parent finalization/cleanup endpoint to fit the old bound.
        result = await bounded_run(engine, task['id'], timeout=1800)
        completed(result)
        child, = result['children']
        assert child['status'] == 'completed' and child['parent_id'] == task['id']
        assert child['workspace'] != task['workspace']
        assert (Path(task['workspace']) / 'answer.txt').read_bytes() == b'hello'
        integrated, = operation_rows(result, 'child_integrate')
        assert integrated['result']['data']['integration_source'] == {
            'child_id': child['id'], 'source_path': 'answer.txt',
            'sha256': hashlib.sha256(b'hello').hexdigest()}
        child_calls = [(phase,payload) for _, phase, payload in gateway.calls if payload['task_id'] == child['id']]
        assert child_calls
        lease = child['state']['delegation_lease']
        blind_lease=deepcopy(lease)
        blind_lease.pop('job_operation',None);blind_lease.pop('integration_plan',None)
        for model in blind_lease['model_leases'].values():
            model.pop('model_reason');model.pop('reasoning_reason')
        for phase,payload in child_calls:
            if phase.startswith('policy_admission_') and phase!='policy_admission_applicability':
                assert payload['delegation_lease']==blind_lease
                assert payload['withheld_parent_context']['lease_sha256']==digest(lease)
            else:assert payload['delegation_lease']==lease
        assert lease['parent_id'] == task['id']
        assert lease['parent_objective'] == task['objective']
        assert lease['parent_acceptance'] == task['acceptance']
        assert lease['constraints'] == result['task']['state']['plan']['constraints']
        assert 'candidate-only' in lease['shared_knowledge_write']
        assessments=[a for row in result['operations'] for key in ('pre_bundle','post_bundle')
                     for a in (row.get(key) or {}).get('assessments',[])]
        assert assessments and all(a['assessment']['ideas'] for a in assessments)
        assert len(journal(executor)) == 4
        assert store.verify_events()
