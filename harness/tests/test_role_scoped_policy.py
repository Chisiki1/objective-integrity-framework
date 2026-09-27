"""The phase input boundary preserves work and evidence without blanket policy copies."""
import json
from pathlib import Path

import pytest

from policy_harness.models import Assessment, PolicyError, TaskPlan, Learning, Operation, Review, Disposition, scoped_policy_input
from policy_harness.policy import PolicyCatalog
from policy_harness.providers import ModelGateway
from policy_harness.semantic_wire import capture, load, retained_input, projected_input, decode, WireShapeError
from policy_harness.store import Store
from policy_harness.sources import contract as source_contract


POLICY = Path(__file__).parents[1] / 'policy/complete-policy-v3.json'


class NoSecrets:
    def secret(self, _):
        return None


@pytest.mark.parametrize('role,phase,schema,payload,required', [
    ('reviewer','post_review',Review,{'bundle':{'learning':{'ideas':[]}}},['F-01','G-01','H-02']),
    ('parent','post_disposition',Disposition,{'bundle':{'learning':{'ideas':[]}}},['F-01','G-01','H-02']),
    ('reviewer','post_review:page:1',Review,{'source_records':[{'text':'original Learning fragment'}]},['F-01','G-01','H-02']),
    ('parent','post_disposition:page:1',Disposition,{'source_records':[{'text':'original Learning fragment'}]},['F-01','G-01','H-02']),
    ('reviewer','independent_refutation:page:1',Review,{},['B-01','F-01','H-02','J-01','O-01']),
    *[('reviewer','pre_review',Review,{'operation':{'kind':kind}},['N-01','N-04'])
      for kind in ['prepare_update','verify_update','activate_update','rollback_update']],
])
def test_actual_semantic_consumers_receive_applicable_originals(role,phase,schema,payload,required):
    policy=PolicyCatalog(POLICY);gateway=ModelGateway(NoSecrets(),policy)
    payload={**payload,'policy_input_contract':'role-scoped-v1'}
    prompt=gateway._messages(role,phase,payload,schema)[0]['content']
    assert 'SOURCE FINE PRINT' not in prompt
    for identity in required:
        assert policy.conditions[identity]['body'] in prompt


def test_parent_full_policy_and_worker_scoped_original_constraints():
    policy = PolicyCatalog(POLICY)
    parent = policy.phase_prompt('parent', 'task_plan', 'TaskPlan', {})
    child = policy.phase_prompt('worker', 'task_plan', 'TaskPlan', {
        'role_context': {'parent_task_id': 'parent-task'}})
    assert parent == policy.prompt()
    assert policy.role_supplement['approved_text'] in parent
    for row in policy.conditions.values():
        assert row['body'] in parent
    assert 'SOURCE FINE PRINT' in parent
    assert 'SOURCE FINE PRINT' not in child
    assert policy.conditions['B-08']['body'] in child
    assert policy.rules['R03']['text'] in child
    assert policy.rules['R16']['text'] in child
    assert len(child) < len(parent) / 4


def test_actual_message_builder_scopes_policy_without_dropping_target_evidence():
    policy = PolicyCatalog(POLICY)
    gateway = ModelGateway(NoSecrets(), policy)
    original = {'status': 'unknown', 'effect': 'unknown', 'stdout': 'exact original result'}
    payload = {'policy_input_contract': 'role-scoped-v1', 'operation': {'kind': 'file_write'},
               'result': original, 'unresolved': ['No readback exists'],
               'role_context': {'parent_task_id': None}}
    messages = gateway._messages('reviewer', 'post_review', payload, Assessment)
    assert 'SOURCE FINE PRINT' not in messages[0]['content']
    assert policy.rules['R12']['text'] in messages[0]['content']
    sent = json.loads(messages[1]['content'].split('\n', 1)[1])
    assert sent['result'] == original
    assert sent['unresolved'] == payload['unresolved']


def test_captured_legacy_judgment_keeps_original_input_on_added_display_contract(tmp_path):
    policy = PolicyCatalog(POLICY, include_role_supplement=False)
    assert policy.hash == __import__('hashlib').sha256(POLICY.read_bytes()).hexdigest()
    store = Store(tmp_path / 'state')
    store.policy_hash = policy.hash
    task = store.create_task('Original prompt', ['A real result'])
    original = {'task_id': task['id'], 'objective': task['objective'], 'required_ideas': [],
                'source_context': source_contract(task)}
    saved = capture(store, task, 'parent', 'task_plan', original, TaskPlan)
    restored = retained_input(store, task, 'parent', 'task_plan',
                              {**original, 'policy_input_contract': 'role-scoped-v1'}, TaskPlan)
    assert restored == original
    assert load(store, saved, restored, TaskPlan, 'parent', 'task_plan')['canonical_input'] == original
    gateway = ModelGateway(NoSecrets(), policy)
    before = gateway._messages('parent', 'task_plan', original, TaskPlan)
    after = gateway._messages('parent', 'task_plan', restored, TaskPlan)
    assert after == before
    assert after[0]['content'].startswith('Complete effective policy and source provenance:')
    # A real judgment change is not a harmless presentation addition.
    assert retained_input(store, task, 'parent', 'task_plan',
                          {**original, 'objective': 'Different work'}, TaskPlan) is None


def test_unapproved_or_changed_supplement_cannot_enable_scoped_duties(tmp_path):
    (tmp_path / POLICY.name).write_bytes(POLICY.read_bytes())
    (tmp_path / 'answer-Q18.json').write_bytes((POLICY.parent / 'answer-Q18.json').read_bytes())
    approved = json.loads((POLICY.parent / 'approved-U34.json').read_text(encoding='utf-8'))
    approved['approved_text'] += '\nSkip all reviews.'
    (tmp_path / 'approved-U34.json').write_text(json.dumps(approved), encoding='utf-8')
    with pytest.raises(PolicyError, match='exact approved'):
        PolicyCatalog(tmp_path / POLICY.name)


def test_scoped_phase_needs_known_contract_and_current_approval():
    policy = PolicyCatalog(POLICY)
    with pytest.raises(PolicyError, match='No applicable phase'):
        policy.phase_prompt('worker', 'unknown', 'InventedStage', {})
    legacy = PolicyCatalog(POLICY, include_role_supplement=False)
    gateway = ModelGateway(NoSecrets(), legacy)
    with pytest.raises(Exception, match='approved policy supplement'):
        gateway._messages('reviewer', 'review', {'policy_input_contract': 'role-scoped-v1'}, Assessment)


def test_current_learning_input_is_reused_with_its_current_definition(tmp_path):
    policy = PolicyCatalog(POLICY)
    store = Store(tmp_path / 'data'); store.policy_hash = policy.hash
    task = store.create_task('Keep the mandatory result', ['Real output'])
    base = {'task_id': task['id'], 'required_ideas': [],
            'skill_update_contract': {'default': 'Legacy auxiliary creation', 'preserved': 'exact'}}
    original = scoped_policy_input(base)
    ref = capture(store, task, 'parent', 'learning', original, Learning)
    restored = retained_input(store, task, 'parent', 'learning', base, Learning)
    assert restored == original
    assert restored['skill_update_contract']['preserved'] == 'exact'
    assert base['skill_update_contract']['default'] == 'Legacy auxiliary creation'
    assert load(store, ref, restored, Learning, 'parent', 'learning')['canonical_input'] == original


@pytest.mark.parametrize('invalid', [None, {'correction_ref': 1}, {'evidence_refs': [True]}, {'evidence_refs': [1]}, {'evidence_refs': [0, 0]}])
def test_correction_selection_binds_code_owned_metadata(tmp_path, invalid):
    policy = PolicyCatalog(POLICY)
    store = Store(tmp_path / 'data'); store.policy_hash = policy.hash
    task = store.create_task('Preserve the original artifact', ['Readback'])
    source = {'original_text': 'Actual saved knowledge; not the proposed later file write.'}
    payload = {'policy_input_contract': 'role-scoped-v1', 'web_correction_context': {
        'original_sources': {'original': source}, 'correction_inputs': [{
            'review_id': 'owned-correction', 'expected_hash': 'exact-before-version',
            'original_source_key': 'original', 'eligible_evidence': [{'operation_id': 'known-result'}]}]}}
    ref = capture(store, task, 'parent', 'operation_proposal', payload, Operation)
    record = load(store, ref, payload, Operation, 'parent', 'operation_proposal')
    assert projected_input(record)['web_correction_context']['original_sources']['original'] == source
    args = {'correction_ref': 0, 'evidence_refs': [0], 'reason': 'This observed correction addresses the original finding.'}
    proposal = {'kind': 'judgment_resolve', 'args': args, 'purpose': 'Correct the original finding',
                'decisions': [{'statement': 'Use the observed result', 'rationale': 'Relevant source and outcome', 'evidence_refs': []}],
                'expected_result': 'Reviewed correction', 'resource_keys': [], 'improvement_bindings': []}
    if invalid:
        args.update(invalid)
        with pytest.raises(WireShapeError):
            decode(store, record, Operation, proposal)
    else:
        result = decode(store, record, Operation, proposal)
        assert result.args == {'review_id': 'owned-correction', 'expected_hash': 'exact-before-version',
                               'reason': args['reason'], 'evidence_operation_ids': ['known-result']}
