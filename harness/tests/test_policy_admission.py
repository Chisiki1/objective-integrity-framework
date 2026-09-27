"""Actual controller/Store/file consumers with explicitly synthetic judgments.

These tests establish admission, source retention and call ordering. They do not
assert that a live model will always classify risk, infer causes or cover every
scenario correctly. No real API, shell, Docker or credentials are used.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from policy_harness.engine import RevisionNeeded
from policy_harness.models import (Completion, EngineeringAdmission, EngineeringInvestigation,
    EngineeringScenarios, EngineeringScope, Disposition, Operation, OperationResult, PolicyError)
from policy_harness.policy_admission import (READINESS_ASPECTS, PolicyAdmission,
    _judgment, _read, _record, validate_admission)
from policy_harness.store import Store, digest
from .test_core import FixtureGateway, runtime


ADMISSION_SCHEMAS = {EngineeringScope, EngineeringScenarios,
                     EngineeringInvestigation, EngineeringAdmission}
BROKEN = "def normalize(items):\n    return items[0].strip()\n"
FIXED = "def normalize(items):\n    return items[0].strip() if items else ''\n"


def fixture_admission_input(payload, store=None):
    if not payload.get('evidence') and store is not None:
        packet_id = payload.get('bounded_context', {}).get('packet_id')
        packet = store.record_get('bounded_input', packet_id) if packet_id else None
        if packet:
            # Exact synthetic transport reader; not a production ID lookup claim.
            payload = packet['value']
    if not payload.get('evidence'):
        raise AssertionError('This fixture requires exact original evidence, not an unseen synopsis')
    return payload


def fixture_engineering_response(payload, schema, *, correction=False, route=None,
                                 needs_evidence=False, missing_aspect=None, store=None):
    """Reference judgments over the actual supplied fixture facts.

    Reused by test_core's fixture only. There is no production fallback and no
    dummy protected record: Engine/BoundedJudgments create the real records.
    Engineering cases below fix their exact source/cause/oracle explicitly.
    """
    payload = fixture_admission_input(payload, store)
    evidence = payload['evidence']
    values = {key: entry['value'] for key, entry in evidence.items()}
    if schema is EngineeringScope:
        source = values['source']
        outcomes = [r['criterion'] for r in source['prior_acceptance']]
        return EngineeringScope(engineering=correction, correction=correction,
            rationale='Fixture source describes an existing empty-input defect' if correction else
                      'Fixture source is an ordinary scoped output request; actual mutations are classified again',
            source_refs=['source'], work_items=outcomes or ['Preserve exact source'],
            normal_success=outcomes or ['Complete requested output'],
            preservation=['Original source, existing contents, actual effects and unchanged normal behavior'])
    if schema is EngineeringScenarios:
        normal = values['normal']
        return EngineeringScenarios(scenarios=[{'id': 'empty-input',
            'normal_path': 'Caller receives the requested result for every accepted input',
            'boundary': 'Caller input to normalization to returned output',
            'harm': 'An accepted empty input raises before the normal consumer receives output',
            'interactions': ['Input cardinality, output meaning and caller recovery'],
            'evidence_refs': ['normal', 'workspace']}],
            limitations=['Synthetic normal-path derivation over supplied ' + str(len(normal['work_items'])) + ' source items; runtime unobserved'])
    if schema is EngineeringInvestigation:
        code = values.get('file:program.py', {}).get('text')
        if correction and code != BROKEN:
            raise AssertionError('Causal fixture is valid only for the exact observed broken preimage')
        return EngineeringInvestigation(status='needs_evidence' if needs_evidence else 'ready',
            first_fault='The supplied source indexes items[0] without handling an accepted empty list',
            causal_chain=['Caller accepts an empty list', 'Indexing element zero prevents the empty result'],
            alternative_causes=['A stricter caller contract would make this input invalid; exact source explicitly accepts it'],
            falsifiable_predictions=['Calling the baseline with [] raises before returning; nonempty input still strips whitespace'],
            evidence_refs=['source', 'file:program.py'] if correction else ['source', 'workspace'],
            findings=[{'id': 'empty-input', 'observation': 'Empty input cannot reach the required output',
                       'rationale': 'The actual source has an unconditional element access',
                       'evidence_refs': ['file:program.py'] if correction else ['source']}],
            checks=[{'work_item': item, 'state': 'blocked' if needs_evidence else 'inspected',
                     'evidence_refs': ['source', 'workspace'],
                     'reason': 'Current source and boundary inspected; no runtime pass claimed',
                     'next_step': 'Run empty and nonempty inputs on the eventual fixed candidate'}
                    for item in payload['work_items']],
            missing_evidence=['Read the actual caller contract before correction'] if needs_evidence else [],
            limitations=['Static fixture causality; later runtime acceptance remains unobserved'])
    if schema is EngineeringAdmission:
        operation = values['operation']
        # This bounded fixture distinguishes source construction from plain
        # output. The production classifier receives the actual meanings and
        # effects; it does not use this fixture's file-extension convention.
        path = operation['args'].get('path', operation['args'].get('destination_path', ''))
        source_construction = Path(path).suffix in {'.py', '.js', '.ts', '.rs', '.go', '.c', '.cpp'}
        selected = route or ('verification' if operation['kind'] == 'exec' else
                            'material_correction' if correction else
                            'construction' if source_construction else 'routine')
        if correction and operation['kind'] == 'file_write':
            assert operation['args']['path'] == 'program.py'
            assert operation['args']['text'] == FIXED
        ready = []
        if operation['kind'] == 'exec':
            execution = (values['candidate'] or {}).get('candidate', {})
            available = bool(execution.get('argv') and execution.get('image_id'))
            for aspect in sorted(READINESS_ASPECTS):
                ready.append({'aspect': aspect, 'state': 'observed' if available and aspect != missing_aspect else 'missing',
                    'evidence_refs': ['candidate', 'source'] if available and aspect != missing_aspect else [],
                    'reason': 'Exact fixture argv, candidate sources/effects/resources and source oracle are supplied; this is preparation, not a test result'})
        dependencies = []
        if selected == 'material_correction':
            for key in evidence:
                if key.startswith(('check:', 'missing:')):
                    findings = ['empty-input'] if key == 'check:0' and 'finding:empty-input' in evidence else []
                    dependencies.append({'prerequisite_id': key, 'relation': 'required',
                        'finding_ids': findings,
                        'evidence_refs': ['source', 'operation', 'preservation', key,
                                          *['finding:' + f for f in findings]],
                        'reason': 'The exact empty-input correction fixture requires its original causal and preservation checks; no unknown fact is waived'})
        return EngineeringAdmission(route=selected,
            phase={'material_correction': 'REPAIR', 'construction': 'BUILD', 'verification': 'SWEEP'}.get(selected, 'NONE'),
            rationale='Compare this exact source/operation/preimage and preserve original outcome and actual effects',
            evidence_refs=['source', 'operation', 'workspace'],
            preservation=['Keep normal nonempty text unchanged and preserve failure/recovery evidence'],
            interactions=['Input boundaries, shared workspace ownership and actual result consumers'],
            addressed_findings=['empty-input'] if selected == 'material_correction' else [],
            dependencies=dependencies,
            readiness=ready, opinions=[{'id': 'normal-preservation',
                'observation': 'Verify empty and nonempty consumers on the fixed candidate',
                'rationale': 'A source correction is not a runtime acceptance result',
                'evidence_refs': ['source', 'operation']}])
    raise AssertionError(schema)


class AdmissionGateway(FixtureGateway):
    def __init__(self, policy, *, correction=True, route=None, needs_evidence=False, missing_aspect=None):
        super().__init__(policy)
        self.correction, self.route = correction, route
        self.needs_evidence, self.missing_aspect = needs_evidence, missing_aspect
        self.next_operation = None

    async def generate(self, role, phase, payload, schema):
        if schema in ADMISSION_SCHEMAS:
            self.calls.append((role, phase, deepcopy(payload)))
            return fixture_engineering_response(payload, schema, correction=self.correction,
                route=self.route, needs_evidence=self.needs_evidence,
                missing_aspect=self.missing_aspect, store=self.response_store), {'fixture': True, 'usage': None, 'cost': None}
        if schema is Operation and self.next_operation is not None:
            self.calls.append((role, phase, deepcopy(payload)))
            return self.next_operation.model_copy(deep=True), {'fixture': True}
        return await super().generate(role, phase, payload, schema)


def engineering_runtime(tmp_path, **options):
    store, engine, executor, _ = runtime(tmp_path)
    gateway = AdmissionGateway(engine.policy, **options)
    engine.gateway = gateway
    gateway.response_store = store
    # This assertion makes a missing real Engine integration fail visibly.
    assert isinstance(engine.policy_admission, PolicyAdmission)
    task = store.create_task('Correct normalize: [] returns an empty string and nonempty text is stripped',
                             ['Accept empty input', 'Preserve normal nonempty text'])
    (Path(task['workspace']) / 'program.py').write_bytes(BROKEN.encode('utf-8'))
    assert (Path(task['workspace']) / 'program.py').read_bytes() == BROKEN.encode('utf-8')
    # An existing reviewed plan is fixture setup; actual prerequisite calls,
    # proposal/pre-review/permit and target mutation below use the real Engine.
    store.update_task(task['id'], state={'plan': {'objective': task['objective']}})
    gateway.next_operation = Operation(kind='file_write', args={'path': 'program.py', 'text': FIXED},
        purpose='Correct the source-required empty input result', expected_result='Actual file bytes change',
        decisions=[{'id': 'empty-case', 'statement': 'Return an empty result for empty input',
                    'rationale': 'Preserve the exact caller contract and nonempty behavior'}])
    return store, engine, executor, gateway, task


async def reviewed_write(engine, task):
    selected = await engine._select({'task_id': task['id']})
    state = {'task_id': task['id'], **selected}
    await engine._pre(state)
    return state


def future_preparation(gateway, *, response='reject'):
    """Independent future-duty mistake and an explicit parent timing response."""
    original = gateway.generate
    async def generate(role, phase, payload, schema, **kwargs):
        value, usage = await original(role, phase, payload, schema, **kwargs)
        if schema is EngineeringAdmission:
            value = value.model_copy(update={'required_preparation': [
                'After writing, read the saved bytes and compare with the original requirement.',
                'If the write effect is unknown, inspect it before any retry.',
                'Preserve the unchanged source and do not write extra files.']})
        if schema is Disposition:
            required = {o['id'] for o in payload['review']['opinions']
                        if o['observation'].startswith('This must be completed BEFORE')}
            value = value.model_copy(update={'opinion_responses': [
                r.model_copy(update={'disposition': response,
                    'rationale': 'These are later/conditional duties or unchanged invariants, not unmet prerequisites of this write.'})
                if r.opinion_id in required else r for r in value.opinion_responses]})
        return value, usage
    gateway.generate = generate


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['accept', 'investigate'])
async def test_true_or_unresolved_preparation_still_blocks_parent_proceed(tmp_path, response):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    future_preparation(gateway, response=response)
    try:
        with pytest.raises(RevisionNeeded, match='After writing'):
            await reviewed_write(engine, task)
        assert not executor.calls
        assert (Path(task['workspace']) / 'program.py').read_bytes() == BROKEN.encode()
        assert not store.get_task(task['id']).get('final')
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_refuted_timing_claim_reaches_permit_and_exact_write_once(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    future_preparation(gateway)
    try:
        state = await reviewed_write(engine, task)
        row = store.get_operation(state['operation_id'])
        admission = row['pre_bundle']['policy_admission']
        assert len(admission['decision']['required_preparation']) == 3
        assert admission['mechanical_missing'] == []
        assert 'admission_preparation_disposition' not in [p for _, p, _ in gateway.calls]
        token, args = permit(store, engine, task, state)
        # A reopening validates original judgment + the exact saved parent
        # responses; it neither regenerates admission nor re-executes the write.
        reopened = Store(tmp_path / 'data')
        try:
            validate_admission(reopened, task['id'], reopened.get_operation(state['operation_id']), engine.policy.hash)
        finally:
            reopened.close()
        await engine._execute(state)
        await engine._execute(state)
        assert len(executor.calls) == 1
        # This fixture's write_text follows host newline rules. Exact production
        # bytes are checked by the real Executor ordinary-task test below.
        assert (Path(task['workspace']) / 'program.py').read_text(encoding='utf-8') == FIXED
        assert store.record_get('policy_admission_pending', task['id'])['missing'] == []
        assert not store.get_task(task['id']).get('final')
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_preparation_refutation_cannot_be_forged_at_permit_consumer(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    future_preparation(gateway)
    try:
        state = await reviewed_write(engine, task)
        token, args = permit(store, engine, task, state)
        row = store.get_operation(state['operation_id'])
        reviewed = deepcopy(row['pre_review'])
        reviewed['disposition']['opinion_responses'] = []
        store.update_operation(state['operation_id'], pre_review=reviewed)
        with pytest.raises(PolicyError, match='ADMISSION_DISPOSITION'):
            store.consume_permit(token, **args)
        assert not executor.calls
        assert (Path(task['workspace']) / 'program.py').read_bytes() == BROKEN.encode()
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_future_duties_reach_post_review_and_completion_without_reproposal(tmp_path):
    from .test_admission_feedback import open_runtime, create_task, assert_delivered
    store, engine, executor, gateway, web = open_runtime(tmp_path / 'data', fault=None)
    future_preparation(gateway)
    task = create_task(store)
    try:
        result = await engine.run_task(task['id'])
        assert_delivered(result, task, executor)
        write = next(r for r in result['operations'] if r['operation']['kind'] == 'file_write')
        retained = write['post_bundle']['admission_obligations']
        assert retained['admission'] == write['pre_bundle']['policy_admission']
        assert retained['before_review'] == write['pre_review']
        assert sum(r['operation']['kind'] == 'file_write' for r in result['operations']) == 1
        final = engine._final_evidence(store.get_task(task['id']), [])
        original = next(r for r in final['operations'] if r['id'] == write['operation']['id'])
        assert original['post_bundle']['admission_obligations'] == retained
        assert not [e for e in store.events(task['id']) if e['stage'] == 'policy_admission_reprepare']
    finally:
        await engine.close(); store.close()


def permit(store, engine, task, state):
    row = store.get_operation(state['operation_id'])
    args = {'task_id': task['id'], 'operation_id': state['operation_id'],
            'action_hash': digest(row['operation']), 'policy_hash': engine.policy.hash,
            'bundle_hash': digest(row['pre_bundle'])}
    return store.issue_permit(**args), args


@pytest.mark.asyncio
async def test_material_correction_uses_actual_blind_calls_and_preserves_proof_ceiling(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    state = await reviewed_write(engine, task)
    phases = [phase for _, phase, _ in gateway.calls]
    assert phases.index('policy_admission_blind_scenarios') < phases.index('operation_proposal')
    assert phases.index('policy_admission_blind_investigation') < phases.index('operation_proposal')
    scenario = next(payload for _, phase, payload in gateway.calls if phase == 'policy_admission_blind_scenarios')
    assert not {'objective', 'source_context', 'history_lookup', 'plan', 'operation'} & scenario.keys()
    assert set(scenario['evidence']) == {'normal', 'workspace', 'file:program.py'}
    investigation = next(payload for _, phase, payload in gateway.calls if phase == 'policy_admission_blind_investigation')
    assert not {'plan', 'operation', 'assessments'} & investigation.keys()
    await engine._execute(state)
    assert (Path(task['workspace']) / 'program.py').read_text('utf-8') == FIXED
    assert len(executor.calls) == 1
    row = store.get_operation(state['operation_id'])
    assert row['result']['effect'] == 'confirmed'
    assert row['pre_bundle']['policy_admission']['decision']['phase'] == 'REPAIR'
    assert any('runtime' in str(opinion).lower() for opinion in row['pre_review']['review']['opinions'])
    assert not store.get_task(task['id']).get('final')
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['scope', 'scenarios', 'investigation'])
async def test_missing_real_prerequisite_prevents_issue_and_consume(tmp_path, name):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    state = await reviewed_write(engine, task)
    token, args = permit(store, engine, task, state)
    row = store.get_operation(state['operation_id'])
    receipt = _read(store, row['pre_bundle']['policy_admission']['receipt'], 'policy_admission_receipt')
    prep = _read(store, receipt['preparation'], 'policy_admission_preparation')
    ref = prep[name]
    store.db.execute('DELETE FROM records WHERE kind=? AND id=?', (ref['kind'], ref['id']))
    with pytest.raises(PolicyError, match='ADMISSION_EVIDENCE_CHANGED'):
        store.issue_permit(**args)
    with pytest.raises(PolicyError, match='ADMISSION_EVIDENCE_CHANGED'):
        store.consume_permit(token, **args)
    assert store.get_operation(state['operation_id'])['status'] == 'reviewed'
    assert executor.calls == []
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_causal_evidence_returns_to_preparation_without_mutation(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path, needs_evidence=True)
    with pytest.raises(RevisionNeeded):
        await reviewed_write(engine, task)
    pending = store.record_get('policy_admission_pending', task['id'])
    assert pending['need_investigation']
    assert executor.calls == []
    assert (Path(task['workspace']) / 'program.py').read_text('utf-8') == BROKEN
    # The same task can select an actual safe read; missing repair evidence is
    # not an unconditional task-wide stop or a user-maintained completion form.
    gateway.next_operation = Operation(kind='file_read', args={'path': 'program.py'},
        purpose='Read actual caller evidence', expected_result='Original evidence',
        decisions=[{'id': 'read-original', 'statement': 'Read the unchanged original', 'rationale': 'Resolve missing causal evidence'}])
    state = await reviewed_write(engine, task)
    await engine._execute(state)
    assert executor.calls[-1]['kind'] == 'file_read'
    assert (Path(task['workspace']) / 'program.py').read_text('utf-8') == BROKEN
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_status', ['proposed', 'reviewed'])
async def test_unstarted_legacy_proposal_is_retained_and_reenters_actual_preparation(tmp_path, legacy_status):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    legacy = gateway.next_operation.model_copy(deep=True).model_dump()
    store.save_operation(task['id'], legacy, policy_hash=engine.policy.hash)
    old_permit = None
    if legacy_status == 'reviewed':
        legacy_bundle = {'operation': legacy, 'legacy_fixture': 'Pre-admission generic review'}
        legacy_review = {'disposition': {'verdict': 'proceed',
            'rationale': 'Persisted pre-admission generic fixture review'}}
        store.update_operation(legacy['id'], status='reviewed',
            pre_bundle=legacy_bundle, pre_review=legacy_review)
        # Seed an actual persisted old-version unconsumed permit. The current
        # issuer is deliberately not used to fabricate missing admission.
        store.db.execute('INSERT INTO permits VALUES(?,?,?,?,?,?,0)',
            ('legacy-permit', task['id'], legacy['id'], digest(legacy),
             engine.policy.hash, digest(legacy_bundle)))
        store.record('permit_dependencies', 'legacy-permit',
            store._permit_dependencies(task['id'], store.get_operation(legacy['id'])))
        old_permit = dict(store.db.execute('SELECT * FROM permits WHERE id=?',
                         ('legacy-permit',)).fetchone())
    original = deepcopy(store.get_operation(legacy['id']))
    existing = store.get_task(task['id'])['state']
    store.update_task(task['id'], state={**existing, 'operation_id': legacy['id'],
        'cycle_id': 'legacy-cycle', 'retained_context': 'Original task remains active'})
    # This is the real persisted legacy path: selection first reuses its pending
    # row, then pre-review discovers that no pre-author preparation exists.
    selected = await engine._select({'task_id': task['id']})
    assert selected['operation_id'] == legacy['id']
    assert gateway.calls == []
    with pytest.raises(RevisionNeeded, match='legacy.*proposal'):
        await engine._pre({'task_id': task['id'], **selected})
    retained = store.get_operation(legacy['id'])
    current = store.get_task(task['id'])
    assert retained['operation'] == original['operation']
    assert retained['source_hash'] == original['source_hash']
    assert retained['result'] is None and retained['status'] == 'revision_required'
    if old_permit:
        assert retained['pre_bundle'] == original['pre_bundle']
        assert retained['pre_review'] == original['pre_review']
        assert dict(store.db.execute('SELECT * FROM permits WHERE id=?',
                    ('legacy-permit',)).fetchone()) == old_permit
        with pytest.raises(PolicyError):
            store.consume_permit('legacy-permit', task_id=task['id'], operation_id=legacy['id'],
                action_hash=old_permit['action_hash'], policy_hash=old_permit['policy_hash'],
                bundle_hash=old_permit['bundle_hash'])
    assert retained['admission_revision']['prior_status'] == legacy_status
    assert retained['admission_revision']['proposal_sha256'] == digest(original['operation'])
    assert 'legacy' in retained['admission_revision']['reason'] and 'proposal' in retained['admission_revision']['reason']
    assert 'operation_id' not in current['state'] and 'cycle_id' not in current['state']
    assert current['state']['plan'] == existing['plan']
    assert current['state']['retained_context'] == 'Original task remains active'
    assert current['source_hash'] == task['source_hash']
    assert executor.calls == [] and (Path(task['workspace']) / 'program.py').read_text('utf-8') == BROKEN
    # A second normal selection must perform the actual blind calls and create
    # a newly ordered author event; merely deleting a pointer is insufficient.
    selected = await engine._select({'task_id': task['id']})
    assert selected['operation_id'] != legacy['id']
    phases = [phase for _, phase, _ in gateway.calls]
    for phase in ('policy_admission_scope', 'policy_admission_blind_scenarios',
                  'policy_admission_blind_investigation'):
        assert phases.index(phase) < phases.index('operation_proposal')
    new_row = store.get_operation(selected['operation_id'])
    proposal = _read(store, new_row['admission_proposal'], 'policy_admission_proposal')
    preparation = _read(store, proposal['preparation'], 'policy_admission_preparation')
    assert preparation['binding']['source_hash'] == task['source_hash']
    await engine._pre({'task_id': task['id'], **selected})
    assert store.get_operation(selected['operation_id'])['status'] == 'reviewed'
    assert store.get_operation(legacy['id']) == retained
    if old_permit:
        assert dict(store.db.execute('SELECT * FROM permits WHERE id=?',
                    ('legacy-permit',)).fetchone()) == old_permit
    assert executor.calls == []
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_unchanged_repair_group_reuses_investigation_after_one_file_write(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    state = await reviewed_write(engine, task)
    before = await engine.policy_admission.prepare(task['id'])
    calls = len([1 for _, phase, _ in gateway.calls if phase == 'policy_admission_blind_investigation'])
    await engine._execute(state)
    after = await engine.policy_admission.prepare(task['id'])
    assert after == before
    assert len([1 for _, phase, _ in gateway.calls if phase == 'policy_admission_blind_investigation']) == calls == 1
    # Reuse is not permission to replay the old mutation on the new candidate.
    with pytest.raises(PolicyError, match='ADMISSION_CANDIDATE_STALE'):
        validate_admission(store, task['id'], store.get_operation(state['operation_id']), engine.policy.hash)
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['workspace', 'source', 'policy', 'task'])
async def test_current_identity_is_checked_at_actual_permit_boundary(tmp_path, change):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    state = await reviewed_write(engine, task)
    token, args = permit(store, engine, task, state)
    row = store.get_operation(state['operation_id'])
    target_id, policy_hash = task['id'], engine.policy.hash
    if change == 'workspace':
        (Path(task['workspace']) / 'other.txt').write_text('Concurrent unreviewed change', encoding='utf-8')
    elif change == 'source':
        store.append_instruction(task['id'], 'Preserve the caller signature too', task['source_hash'])
    elif change == 'policy':
        policy_hash = 'another-effective-policy'
    else:
        target_id = store.create_task('Another source', ['Other result'])['id']
    with pytest.raises(PolicyError, match='ADMISSION_'):
        validate_admission(store, target_id, row, policy_hash)
    if change in {'workspace', 'source'}:
        with pytest.raises(PolicyError):
            store.consume_permit(token, **args)
    assert executor.calls == []
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_real_but_late_independent_call_is_not_retroactive_blind_evidence(tmp_path):
    store, engine, executor, gateway, task = engineering_runtime(tmp_path)
    selected = await engine._select({'task_id': task['id']})
    row = store.get_operation(selected['operation_id'])
    proposal = _read(store, row['admission_proposal'], 'policy_admission_proposal')
    prep = _read(store, proposal['preparation'], 'policy_admission_preparation')
    old = _judgment(store, prep['investigation'], prep['binding'])
    payload = deepcopy(old['original_input'])
    payload['instruction'] += ' This is a genuinely later separate call in the ordering fixture.'
    late = await engine.policy_admission._call(task['id'], 'blind_investigation', payload, EngineeringInvestigation)
    changed = {k: v for k, v in prep.items() if k != 'id'}
    changed['investigation'] = late
    ref = _record(store, 'policy_admission_preparation', changed)
    with pytest.raises(PolicyError, match='ADMISSION_BLIND_ORDER'):
        engine.policy_admission.bind_proposal(task['id'], ref, row['operation'])
    assert executor.calls == []
    await engine.close()
    store.close()


def test_reviewed_flag_and_author_payload_cannot_create_a_mutation_permit(tmp_path):
    store = Store(tmp_path / 'data')
    task = store.create_task('Write one file', ['Actual file'])
    op = Operation(kind='file_write', args={'path': 'answer.txt', 'text': 'hello',
        'policy_admission': {'ready': True, 'blind': True}}, purpose='Deliver file',
        expected_result='Actual file', decisions=[{'id': 'd', 'statement': 'Write', 'rationale': 'Task'}]).model_dump()
    store.save_operation(task['id'], op)
    store.update_operation(op['id'], status='reviewed', pre_bundle={'operation': op})
    with pytest.raises(PolicyError, match='ADMISSION_MISSING'):
        store.issue_permit(task['id'], op['id'], digest(op), 'policy', 'bundle')
    assert not (Path(task['workspace']) / 'answer.txt').exists()
    store.close()


@pytest.mark.asyncio
async def test_ordinary_document_completes_without_causal_or_formal_sweep(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    gateway = AdmissionGateway(engine.policy, correction=False)
    engine.gateway = gateway
    gateway.response_store = store
    task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
    result = await engine.run_task(task['id'])
    assert result['task']['status'] == 'completed', result['events'][-1]
    assert (Path(task['workspace']) / 'answer.txt').read_text('utf-8') == 'hello'
    assert not any(phase in {'policy_admission_blind_scenarios', 'policy_admission_blind_investigation'}
                   for _, phase, _ in gateway.calls)
    assert result['task']['acceptance'] == ['answer.txt contains hello']
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_unavailable_unrelated_member_does_not_block_ordinary_output(tmp_path, monkeypatch):
    import policy_harness.policy_admission as admission_module
    store, engine, executor, _ = runtime(tmp_path)
    gateway = AdmissionGateway(engine.policy, correction=False)
    gateway.response_store = store
    engine.gateway = gateway
    task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
    (Path(task['workspace']) / 'unavailable.bin').write_bytes(b'Unrelated fixture')
    original = admission_module.check_node
    def bounded_unavailable(path):
        if Path(path).name == 'unavailable.bin':
            raise PolicyError('PATH_LINK: fixture unavailable member')
        return original(path)
    monkeypatch.setattr(admission_module, 'check_node', bounded_unavailable)
    result = await engine.run_task(task['id'])
    assert result['task']['status'] == 'completed', result['events'][-1]
    assert (Path(task['workspace']) / 'answer.txt').read_text('utf-8') == 'hello'
    receipt = next(r for r in store.records('policy_admission_receipt') if r['binding']['task_id'] == task['id'])
    assert receipt['workspace']['unavailable.bin']['type'] == 'unavailable'
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ready', 'missing_comparison', 'changed_argv'])
async def test_exec_readiness_uses_bound_real_recipe_description(tmp_path, case):
    missing_aspect = 'comparison' if case == 'missing_comparison' else None
    store, engine, executor, gateway, task = engineering_runtime(tmp_path, route='verification', missing_aspect=missing_aspect)
    raw_sha = hashlib.sha256(BROKEN.encode()).hexdigest()
    description = {'candidate': {'workspace': task['workspace'], 'image_id': 'fixture-prepared-image',
        'executable': '/usr/local/bin/python', 'argv': ['-I', '-B', '/workspace/program.py'],
        'files': {'program.py': raw_sha}, 'declared_effects': ['owned report only'],
        'resources': {'timeout_seconds': 5, 'rationale': 'Explicit bounded fixture'}},
        'sources': [{'path': 'program.py', 'sha256': raw_sha, 'text': BROKEN}]}
    executor.describe_execution = lambda workspace, args: deepcopy(description)
    gateway.next_operation = Operation(kind='exec', args={'capability_id': 'prepared-fixture'},
        purpose='Check empty and nonempty inputs on the bound candidate', expected_result='Actual outcomes; no predeclared pass',
        decisions=[{'id': 'check-inputs', 'statement': 'Compare original source-required outcomes', 'rationale': 'Refute both the defect and regression'}])
    selected = await engine._select({'task_id': task['id']})
    state = {'task_id': task['id'], **selected}
    if missing_aspect:
        with pytest.raises(RevisionNeeded):
            await engine._pre(state)
        assert executor.calls == []
    else:
        await engine._pre(state)
        if case == 'changed_argv':
            token, args = permit(store, engine, task, state)
            description['candidate']['argv'].append('--changed')
            # Engine re-describes the actual executable before invoking Store.
            with pytest.raises(PolicyError, match='source changed|Candidate changed'):
                await engine._execute(state)
            assert executor.calls == []
        else:
            async def execute(workspace, operation):
                executor.calls.append(operation.model_dump())
                marker = workspace / 'fixture-executor-observed.txt'
                marker.write_text('Actual fixture executor reached', encoding='utf-8')
                return OperationResult(operation_id=operation.id, status='succeeded', effect='confirmed',
                    data={'fixture': True, 'marker': str(marker), 'runtime_test_semantics': 'NOT-OBSERVED'})
            executor.execute = execute
            await engine._execute(state)
            assert len(executor.calls) == 1
            assert (Path(task['workspace']) / 'fixture-executor-observed.txt').exists()
    await engine.close()
    store.close()


LOOKUP_BROKEN = "def lookup(cache, key):\n    return cache[key]\n"
LOOKUP_GUESS = "def lookup(cache, key):\n    return cache.get(key)\n"
A_OUTCOME = 'A: normalize accepts empty input and preserves normal text'
B_OUTCOME = 'B: lookup follows its required external missing-key contract'
B_MISSING = 'The required B missing-key contract is absent; its return, exception and recovery semantics are unobserved'


class ScopedAdmissionGateway(AdmissionGateway):
    """Two independent modules, with an actually absent required B source.

    The final Completion response is intentionally false. That exercises the
    real known-obligation gate, not a fixture that politely reports unfinished.
    """
    async def generate(self, role, phase, payload, schema):
        if schema in {EngineeringScenarios, EngineeringInvestigation, EngineeringAdmission}:
            self.calls.append((role, phase, deepcopy(payload)))
            original = fixture_admission_input(payload, self.response_store)
            evidence = original['evidence']
            values = {key: entry['value'] for key, entry in evidence.items()}
            if schema is EngineeringScenarios:
                value = fixture_engineering_response(original, schema, correction=True).model_dump()
                value['scenarios'].append({'id': 'lookup-contract',
                    'normal_path': 'The lookup caller receives the value or failure required by its original contract',
                    'boundary': 'Independent lookup module to its own caller and recovery',
                    'harm': 'A guessed missing-key fallback silently replaces a required exception',
                    'interactions': ['Cache membership, caller error handling and contract ownership'],
                    'evidence_refs': ['normal', 'workspace', 'file:lookup.py']})
                return EngineeringScenarios.model_validate(value), {'fixture': True}
            if schema is EngineeringInvestigation:
                assert values['file:program.py']['text'] == BROKEN
                assert values['file:lookup.py']['text'] == LOOKUP_BROKEN
                assert 'file:lookup-contract.json' not in evidence
                value = fixture_engineering_response(original, schema, correction=True).model_dump()
                assert [c['work_item'] for c in value['checks']] == [A_OUTCOME, B_OUTCOME]
                value['status'] = 'needs_evidence'
                value['checks'][0].update(evidence_refs=['source', 'file:program.py'],
                    reason='The original A source accepts []; its unconditional items[0] prevents return. The standalone function has no B import, state or caller dependency.')
                value['checks'][1].update(state='blocked', evidence_refs=['source', 'workspace', 'file:lookup.py'],
                    reason=B_MISSING, next_step='Obtain the required original B contract through governed source preparation')
                value['findings'].append({'id': 'lookup-contract', 'observation': B_MISSING,
                    'rationale': 'The same work unit retains B as mandatory; current local lookup code alone cannot resolve its required contract',
                    'evidence_refs': ['source', 'workspace', 'file:lookup.py']})
                value['missing_evidence'] = [B_MISSING]
                value['alternative_causes'].append('B may require KeyError or another fallback; absent contract cannot establish either')
                return EngineeringInvestigation.model_validate(value), {'fixture': True}
            operation = values['operation']
            path = operation['args']['path']
            assert operation['kind'] == 'file_write' and path in {'program.py', 'lookup.py'}
            safe_a = path == 'program.py'
            assert operation['args']['text'] == (FIXED if safe_a else LOOKUP_GUESS)
            assert values['file:lookup.py']['text'] == LOOKUP_BROKEN
            value = fixture_engineering_response(original, schema, route='material_correction').model_dump()
            value['addressed_findings'] = ['empty-input' if safe_a else 'lookup-contract']
            value['dependencies'] = []
            assert {key for key in evidence if key.startswith(('check:', 'missing:'))} == {'check:0', 'check:1', 'missing:0'}
            for key in ('check:0', 'check:1', 'missing:0'):
                required = (key == 'check:0') if safe_a else (key != 'check:0')
                findings = value['addressed_findings'] if required and key.startswith('check:') else []
                value['dependencies'].append({'prerequisite_id': key,
                    'relation': 'required' if required else 'independent', 'finding_ids': findings,
                    'evidence_refs': ['source', 'operation', 'preservation', key, 'sweep_candidate',
                        'file:program.py', 'file:lookup.py', *['finding:' + f for f in findings]],
                    'reason': ('A changes only its standalone normalization body; original/current bytes show no shared import, state, return value or error path with B. A causal and normal-path evidence is observed.'
                               if safe_a else
                               'The proposed lookup fallback changes exactly the missing-key behavior whose original B contract is unavailable; A evidence cannot resolve or authorize it.')})
            if getattr(self, 'omit_dependency', None):
                value['dependencies'] = [d for d in value['dependencies']
                                         if d['prerequisite_id'] != self.omit_dependency]
            return EngineeringAdmission.model_validate(value), {'fixture': True}
        if schema is Completion:
            self.calls.append((role, phase, deepcopy(payload)))
            writes = [row for row in payload['operations']
                if row['operation']['kind'] == 'file_write' and (row.get('result') or {}).get('effect') == 'confirmed']
            assert writes and all(row['operation']['args']['path'] == 'program.py' for row in writes)
            refs = [writes[0]['operation']['id']]
            independent_id = payload['independent_refutation']['id']
            value = Completion(achieved=True,
                acceptance=[{'criterion': criterion, 'achieved': True, 'evidence_refs': refs,
                             'independent_refs': [independent_id]} for criterion in payload['acceptance']],
                evidence_refs=refs, unresolved=[],
                summary='Intentionally false fixture claim: a real A write is incorrectly cited for B completion')
            return value, {'fixture': True}
        return await super().generate(role, phase, payload, schema)


def scoped_runtime(tmp_path, repair='A'):
    store, engine, executor, _ = runtime(tmp_path)
    gateway = ScopedAdmissionGateway(engine.policy)
    gateway.response_store = store
    engine.gateway = gateway
    task = store.create_task('Repair independent A normalize and B lookup. A accepts [] and preserves stripped text. '
        'B requires its original missing-key contract, which has not been supplied. Keep B mandatory and unresolved.',
        [A_OUTCOME, B_OUTCOME])
    root = Path(task['workspace'])
    (root / 'program.py').write_bytes(BROKEN.encode('utf-8'))
    (root / 'lookup.py').write_bytes(LOOKUP_BROKEN.encode('utf-8'))
    store.update_task(task['id'], state={'plan': {'objective': task['objective'], 'acceptance': task['acceptance']}})
    gateway.next_operation = Operation(kind='file_write',
        args={'path': 'program.py' if repair == 'A' else 'lookup.py',
              'text': FIXED if repair == 'A' else LOOKUP_GUESS},
        purpose='Repair the selected source-bound module and preserve the other mandatory outcome',
        expected_result='Actual file bytes; separate full acceptance remains unobserved',
        decisions=[{'id': 'repair-' + repair, 'statement': 'Change only the selected function body',
                    'rationale': 'Use original source and independently collected findings'}])
    return store, engine, executor, gateway, task


@pytest.mark.asyncio
@pytest.mark.parametrize('repair', ['A', 'B'])
async def test_actual_permits_scope_blocker_and_false_finish_keeps_whole_outcome(tmp_path, repair):
    store, engine, executor, gateway, task = scoped_runtime(tmp_path, repair)
    selected = await engine._select({'task_id': task['id']})
    state = {'task_id': task['id'], **selected}
    proposal = _read(store, store.get_operation(selected['operation_id'])['admission_proposal'], 'policy_admission_proposal')
    before = engine.policy_admission.context(proposal['preparation'])['investigation']
    assert before['status'] == 'needs_evidence' and before['checks'][1]['state'] == 'blocked'
    assert before['missing_evidence'] == [B_MISSING]
    if repair == 'B':
        with pytest.raises(RevisionNeeded, match='dependent'):
            await engine._pre(state)
        row = store.get_operation(selected['operation_id'])
        preparation = row['preparation_review']
        assert any('dependent' in text for text in preparation['admission']['missing'])
        assert row['status'] == 'revision_required' and row['result'] is None
        assert 'pre_bundle' not in row and 'pre_web' not in row
        # Clear only claimed missing flags and status, preserving the actual
        # independent B response. Both boundaries must rederive its dependency
        # hold even with a fabricated legacy permit. Original records remain.
        candidate = await engine._describe_operation(task, row['operation'])
        bundle = {'operation': deepcopy(row['operation']), 'candidate': candidate,
                  'policy_admission': deepcopy(preparation['admission'])}
        receipt = _read(store, bundle['policy_admission']['receipt'], 'policy_admission_receipt')
        assert receipt['candidate_hash'] == digest(candidate)
        counterfeit = {key: value for key, value in receipt.items() if key != 'id'}
        counterfeit['missing'] = []
        bundle['policy_admission']['receipt'] = _record(store, 'policy_admission_receipt', counterfeit)
        bundle['policy_admission']['missing'] = []
        store.update_operation(selected['operation_id'], status='reviewed', pre_bundle=bundle)
        args = {'task_id': task['id'], 'operation_id': selected['operation_id'],
            'action_hash': digest(row['operation']), 'policy_hash': engine.policy.hash,
            'bundle_hash': digest(bundle)}
        with pytest.raises(PolicyError, match='ADMISSION_DECISION: recorded preparation differs'):
            store.issue_permit(**args)
        store.db.execute('INSERT INTO permits VALUES(?,?,?,?,?,?,0)',
            ('old-dependent-permit', task['id'], selected['operation_id'],
             args['action_hash'], args['policy_hash'], args['bundle_hash']))
        store.record('permit_dependencies', 'old-dependent-permit', store._permit_dependencies(task['id'], row))
        with pytest.raises(PolicyError, match='ADMISSION_DECISION: recorded preparation differs'):
            store.consume_permit('old-dependent-permit', **args)
        assert store.db.execute('SELECT consumed FROM permits WHERE id=?', ('old-dependent-permit',)).fetchone()[0] == 0
        assert executor.calls == []
    else:
        await engine._pre(state)
        token, _ = permit(store, engine, task, state)
        await engine._execute(state)
        assert store.db.execute('SELECT consumed FROM permits WHERE id=?', (token,)).fetchone()[0] == 1
        assert (Path(task['workspace']) / 'program.py').read_text('utf-8') == FIXED
        await engine._post(state)
        await engine._learn(state)
        await engine._close_cycle(state)
        # A successful file repair is not new B diagnostic evidence and must not
        # launch another whole sweep merely because B remains blocked.
        assert await engine.policy_admission.prepare(task['id']) == proposal['preparation']
        assert engine.policy_admission.context(proposal['preparation'])['investigation'] == before
        gateway.next_operation = Operation(kind='finish', args={'summary': 'Attempt false whole completion'},
            purpose='Exercise mandatory whole completion against the unchanged original source',
            expected_result='B remains unfinished', decisions=[{'id': 'false-finish',
                'statement': 'Request final completion', 'rationale': 'The controller must reject the known B gap'}])
        finish = {'task_id': task['id'], **await engine._select({'task_id': task['id']})}
        await engine._pre(finish)
        await engine._execute(finish)
        await engine._post(finish)
        await engine._learn(finish)
        with pytest.raises(RevisionNeeded):
            await engine._close_cycle(finish)
        final_row = store.get_operation(finish['operation_id'])
        assert final_row['finalization']['completion']['achieved'] is True
        assert final_row['finalization']['structural_evidence_complete'] is True
        assert final_row['completion_deferred']
        independent = store.record_get('independent_review', final_row['finalization']['independent_review_id'])
        original_a = next(row for row in independent['raw_evidence']['operations'] if row['operation']['id'] == state['operation_id'])
        retained = original_a['pre_bundle']['policy_admission']['source_wide_preparation']['investigation']
        assert retained == before
        assert not store.records('cleanup_result')
    assert (Path(task['workspace']) / 'lookup.py').read_bytes() == LOOKUP_BROKEN.encode('utf-8')
    assert store.get_task(task['id'])['acceptance'] == [A_OUTCOME, B_OUTCOME]
    assert store.get_task(task['id'])['status'] != 'completed'
    assert store.get_task(task['id'])['final'] is None
    outstanding = engine.policy_admission.completion_obligations(task['id'])
    assert any(item['kind'] == 'check' and item['value']['work_item'] == B_OUTCOME
               and item['value']['state'] == 'blocked' for item in outstanding)
    assert any(item['kind'] == 'missing' and item['value'] == B_MISSING for item in outstanding)
    await engine.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('omitted', ['check:1', 'missing:0'])
async def test_independent_review_must_classify_each_original_blocker(tmp_path, omitted):
    store, engine, executor, gateway, task = scoped_runtime(tmp_path)
    gateway.omit_dependency = omitted
    selected = await engine._select({'task_id': task['id']})
    with pytest.raises(RevisionNeeded, match='every original whole check and missing fact'):
        await engine._pre({'task_id': task['id'], **selected})
    assert (Path(task['workspace']) / 'program.py').read_bytes() == BROKEN.encode('utf-8')
    assert executor.calls == []
    assert store.get_task(task['id'])['acceptance'] == [A_OUTCOME, B_OUTCOME]
    await engine.close()
    store.close()


@pytest.mark.asyncio
async def test_obsolete_reviewer_context_retains_missing_evidence_until_real_reprepare(tmp_path, monkeypatch):
    import policy_harness.engine as engine_module
    import policy_harness.policy_admission as admission_module
    store, engine, executor, gateway, task = scoped_runtime(tmp_path)
    def original_context(task, policy_hash):
        return {'task_id': task['id'], 'source_hash': task['source_hash'], 'policy_hash': policy_hash,
            'role_context': {'actual_role': 'reviewer', 'reviewer_tools': [], 'review_call_is_real': True}}
    # Obtain real bound calls under the old actual wrapper, then restore the new
    # authoritative wrapper; an arbitrary version string is not the cache key.
    with monkeypatch.context() as legacy:
        legacy.setattr(admission_module, 'independent_context', original_context)
        legacy.setattr(engine_module, 'independent_context', original_context)
        prior_ref = await engine.policy_admission.prepare(task['id'])
        original = deepcopy(_read(store, prior_ref, 'policy_admission_preparation'))
    outstanding = engine.policy_admission.completion_obligations(task['id'])
    assert any(item['kind'] == 'reprepare' and prior_ref in item['prior_preparations'] for item in outstanding)
    assert _read(store, prior_ref, 'policy_admission_preparation') == original
    selected = await engine._select({'task_id': task['id']})
    row = store.get_operation(selected['operation_id'])
    proposal = _read(store, row['admission_proposal'], 'policy_admission_proposal')
    current = _read(store, proposal['preparation'], 'policy_admission_preparation')
    assert current['binding']['reviewer_context_sha256'] != original['binding']['reviewer_context_sha256']
    assert current['binding']['source_hash'] == original['binding']['source_hash']
    assert sum(phase == 'policy_admission_scope' for _, phase, _ in gateway.calls) == 2
    assert any(item['kind'] == 'missing' and item['value'] == B_MISSING
               for item in engine.policy_admission.completion_obligations(task['id']))
    assert _read(store, prior_ref, 'policy_admission_preparation') == original
    assert executor.calls == [] and store.get_task(task['id'])['final'] is None
    await engine.close()
    store.close()


class LaterVerificationGateway(AdmissionGateway):
    async def generate(self, role, phase, payload, schema):
        if schema is EngineeringInvestigation:
            self.calls.append((role, phase, deepcopy(payload)))
            original = fixture_admission_input(payload, self.response_store)
            value = fixture_engineering_response(original, schema, correction=True).model_dump()
            observations = [(key, entry['value']) for key, entry in original['evidence'].items()
                if key.startswith('result:') and entry['value']['kind'] == 'exec'
                and entry['value']['result'].get('data', {}).get('source_sha256') == hashlib.sha256(BROKEN.encode()).hexdigest()
                and entry['value']['result']['data'].get('observed') == 'x'
                and entry['value']['result']['data'].get('expected') == 'x']
            value['checks'][-1].update(state='inspected' if observations else 'later',
                reason='Actual bound Python result observes normal text preservation' if observations else
                       'Static normal-path checks are collected; the later prepared executable invocation must observe the actual normal result',
                evidence_refs=['source', 'file:program.py', *[key for key, _ in observations]],
                next_step='Retain the observed normal result and verify the empty-input correction separately' if observations else
                          'Run the prepared normal-input verification and consume its original result')
            return EngineeringInvestigation.model_validate(value), {'fixture': True}
        return await super().generate(role, phase, payload, schema)


@pytest.mark.asyncio
async def test_actual_later_verification_refreshes_ready_investigation_without_erasing_history(tmp_path):
    store, engine, executor, _, task = engineering_runtime(tmp_path)
    gateway = LaterVerificationGateway(engine.policy, route='verification')
    gateway.response_store = store
    engine.gateway = gateway
    source_hash = hashlib.sha256(BROKEN.encode()).hexdigest()
    description = {'candidate': {'workspace': task['workspace'], 'image_id': 'pytest-python-fixture',
        'argv': ['python', 'program.py'], 'files': {'program.py': source_hash},
        'declared_effects': ['No file mutation; one in-process fixture function call'],
        'resources': {'timeout_seconds': 5, 'rationale': 'One explicitly authored normal input'}},
        'sources': [{'path': 'program.py', 'sha256': source_hash, 'text': BROKEN}]}
    executor.describe_execution = lambda workspace, args: deepcopy(description)
    async def verify_normal(workspace, operation):
        executor.calls.append(operation.model_dump())
        raw = (workspace / 'program.py').read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source_hash
        namespace = {}
        # Actual local Python behavior in this pytest fixture. It is not a
        # production container/environment assertion or an invented test pass.
        exec(compile(raw, str(workspace / 'program.py'), 'exec'), namespace)
        observed = namespace['normalize']([' x '])
        return OperationResult(operation_id=operation.id, status='succeeded', effect='none',
            data={'source_sha256': source_hash, 'observed': observed, 'expected': 'x',
                  'scope': 'Actual Python function in the pytest process; native container unobserved'})
    executor.execute = verify_normal
    gateway.next_operation = Operation(kind='exec', args={'capability_id': 'normal-input-fixture'},
        purpose='Observe the mandatory later normal-input result', expected_result='Compare actual x with required x',
        decisions=[{'id': 'observe-normal', 'statement': 'Run the exact current normal input',
                    'rationale': 'Close only the still-unobserved normal result'}])
    selected = await engine._select({'task_id': task['id']})
    state = {'task_id': task['id'], **selected}
    row = store.get_operation(selected['operation_id'])
    prior_ref = _read(store, row['admission_proposal'], 'policy_admission_proposal')['preparation']
    prior = _read(store, prior_ref, 'policy_admission_preparation')
    original = deepcopy(_judgment(store, prior['investigation'], prior['binding']))
    assert original['result']['status'] == 'ready' and original['result']['checks'][-1]['state'] == 'later'
    assert engine.policy_admission.completion_obligations(task['id'])
    await engine._pre(state)
    await engine._execute(state)
    actual_result = store.get_operation(selected['operation_id'])['result']
    assert actual_result['data']['observed'] == actual_result['data']['expected'] == 'x'
    refreshed_ref = await engine.policy_admission.prepare(task['id'])
    refreshed = _read(store, refreshed_ref, 'policy_admission_preparation')
    current = _judgment(store, refreshed['investigation'], refreshed['binding'])['result']
    assert refreshed['investigation'] != prior['investigation']
    assert current['checks'][-1]['state'] == 'inspected'
    assert 'result:' + selected['operation_id'] in current['checks'][-1]['evidence_refs']
    assert _judgment(store, prior['investigation'], prior['binding']) == original
    assert engine.policy_admission.completion_obligations(task['id']) == []
    assert store.get_task(task['id'])['final'] is None  # No whole acceptance was attempted or proved.
    await engine.close()
    store.close()
