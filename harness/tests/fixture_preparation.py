"""Real prerequisite callers for isolated recovery scenarios.

Only the model's scenario decisions are synthetic. PolicyAdmission and Engine
create their own source-bound records and call events; no permit or admission
receipt is fabricated here. These fixtures do not certify model judgment.
"""
from copy import deepcopy

from policy_harness.models import Operation
from policy_harness.model_routing import resolve_lease
from policy_harness.policy_admission import GOVERNED_KINDS
from policy_harness.store import digest


async def prepare_operation(engine, task, operation):
    """Retain a scenario's operation ID through the real author/binding route."""
    expected = Operation.model_validate(operation).model_dump()
    task_id = task['id']
    if expected['kind'] not in GOVERNED_KINDS:
        # Existing isolated non-mutation cycles do not need a new fixture
        # admission route; retain their original direct setup and consumers.
        engine.store.save_operation(task_id, expected, policy_hash=engine.policy.hash)
        return {'task_id': task_id, 'operation_id': expected['id']}
    gateway = engine.gateway
    # Engine connects this at construction. A scenario may replace its gateway
    # afterward; bounded fixture decoding must read the actual same Store.
    if hasattr(gateway, 'response_store'):
        gateway.response_store = engine.store
    preparation = await engine.policy_admission.prepare(task_id)

    class ScenarioProposal:
        def __getattr__(self, name):
            return getattr(gateway, name)

        async def generate(self, role, phase, payload, schema, **kwargs):
            assert phase == 'operation_proposal' and schema is Operation
            assert payload['task_id'] == task_id
            if kwargs.get('model_lease'):
                resolve_lease(gateway.settings, kwargs['model_lease'], role=role,
                              job_context=kwargs['job_context'])
            return Operation.model_validate(deepcopy(expected)), {
                'fixture': True, 'usage': None, 'cost': None,
                'scope': 'Explicit scenario proposal; no model-semantic claim'}

    # The actual Engine call validates the response and records its provenance.
    # Unlike _select, this fixture retains the caller-owned operation ID used by
    # the downstream receipt, recovery and no-replay assertions.
    engine.gateway = ScenarioProposal()
    try:
        proposed = await engine._call(task_id, 'operation_proposal', {
            'engineering_preparation': engine.policy_admission.context(preparation),
            'fixture_scenario_operation': expected,
            'instruction': 'Return the one explicitly declared isolated scenario operation. '
                           'All judgments are fixtures; actual effects remain unobserved.'}, Operation)
    finally:
        engine.gateway = gateway
    assert proposed.model_dump() == expected
    binding = engine.policy_admission.bind_proposal(task_id, preparation, expected)
    engine.store.save_operation(task_id, expected, policy_hash=engine.policy.hash,
                                admission_proposal=binding)
    return {'task_id': task_id, 'operation_id': expected['id']}


def bind_cleanup_evidence(engine, task, row, draft):
    """Prepare a deterministic upstream semantic fixture before cleanup faults.

    The real immutable history snapshot and exact original records are supplied
    to the unchanged finalization validator. Acceptance judgments are explicit
    setup for the isolated transaction branch, not an original-task result or
    an assertion that a live independent reviewer reached this conclusion.
    """
    store = engine.store
    task = store.get_task(task['id'])
    raw = engine._final_evidence(task, [])
    identity = 'fixture-cleanup-review:' + row['operation']['id']
    independent = {
        'id': identity, 'task_id': task['id'], 'source_hash': task['source_hash'],
        'acceptance_hash': digest(task['acceptance']), 'policy_hash': engine.policy.hash,
        'raw_evidence': raw, 'raw_evidence_hash': digest(raw),
        'history_snapshot_id': raw['snapshot_id'],
        'review': deepcopy(draft['pre_review']['review']),
        'proof_ceiling': 'Deterministic upstream semantic fixture over these exact '
                         'isolated records; only the downstream cleanup is under test.'}
    store.record('independent_review', identity, independent)
    draft = deepcopy(draft)
    evidence_ids = [row['operation']['id']]
    draft['completion'].update(
        acceptance=[{'criterion': criterion, 'achieved': True,
                     'evidence_refs': evidence_ids, 'independent_refs': [identity]}
                    for criterion in task['acceptance']], evidence_refs=evidence_ids)
    draft.update(source_hash=task['source_hash'], acceptance_hash=digest(task['acceptance']),
                 policy_hash=engine.policy.hash, independent_review_id=identity,
                 independent_review_sha256=digest(independent),
                 frozen_evidence_sha256=digest(raw), history_snapshot_id=raw['snapshot_id'])
    store.update_operation(row['operation']['id'], finalization=draft)
    bound = store.get_operation(row['operation']['id'])
    assert engine._assert_finalization_evidence(task['id'], bound, draft) == raw
    return bound
