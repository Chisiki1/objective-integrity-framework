"""Real Engine/Knowledge/SQLite consumers; synthetic model, Web and crash inputs.

The two legacy action names and first fault come from live7, whose immutable
source hashes are in the job report. Fixture judgments are not live AI evidence.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from policy_harness.engine import Engine, RevisionNeeded
from policy_harness.knowledge import Knowledge
from policy_harness.models import (Learning, LearningContractError, OperationResult,
                                  PolicyError, learning_update_contract, learning_updates_schema)
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, FixtureWeb, runtime
from tests.test_knowledge import learning, operation, seed_skill, observed_application, journal
from tests.test_web_recovery import collector, operation as web_operation, SimulatedProcessLoss
from tests.fixture_preparation import prepare_operation


def create_update():
    return {'action': 'create', 'title': 'Observed knowledge', 'content': 'Preserve the actual output and unknown benefit.',
            'applicability': 'Matching exact-output work', 'next_trigger': 'Next matching file operation'}


class CorrectingGateway(FixtureGateway):
    def __init__(self, policy, update=None, *, application=None, phase=None):
        super().__init__(policy)
        self.update = update
        self.application = application
        self.target_phase = phase
        self.learning_inputs = []
        self.target_calls = 0

    async def generate(self, role, phase, payload, schema):
        value, usage = await super().generate(role, phase, payload, schema)
        if schema is Learning:
            self.learning_inputs.append(deepcopy(payload))
            if self.target_phase is None or phase == self.target_phase:
                self.target_calls += 1
                if self.target_calls == 1:
                    value = value.model_copy(update={'skill_updates': deepcopy(self.update or []),
                                                    'applications': deepcopy(self.application or [])})
        return value, usage


def proposal_input(op, result, selected=None):
    return {'operation': op, 'result': result, 'required_ideas': [],
            'pre': {'skills': {'selected': selected or [], 'rejected': [], 'new_knowledge_needed': []}}}


def reopen(store, engine, executor, gateway=None):
    policy = engine.policy
    fresh_store = Store(store.data_dir)
    fresh_engine = Engine(fresh_store, policy, executor, gateway or FixtureGateway(policy), FixtureWeb(), Knowledge(fresh_store))
    return fresh_store, fresh_engine


@pytest.mark.asyncio
@pytest.mark.parametrize('update,diagnostic', [
    ({'action': 'create-provisional'}, 'unsupported'),
    ({'action': 'record-only (no skill, no rule)'}, 'unsupported'),
    ({'action': 'create', 'title': 'No body'}, 'missing fields'),
    ({**create_update(), 'next_use_trigger': 'wrong wire name'}, 'unsupported fields'),
    ({**create_update(), 'content': ' '}, 'nonempty string'),
    ({'action': 'improve', 'id': 'unobserved'}, 'expected_hash'),
    ({'action': 'retire', 'id': 'unobserved', 'expected_hash': 'a'*64}, 'reason'),
    ({'action': 'merge', 'id': 'a', 'expected_hash': 'a'*64, 'merge_ids': ['b', 'b'],
      'merge_hashes': {'b': 'b'*64}, 'content': 'merged'}, 'distinct other ids'),
    ({'action': 'merge', 'id': 'a', 'expected_hash': 'a'*64, 'merge_ids': ['b'],
      'merge_hashes': {}, 'content': 'merged'}, 'matching merge_hashes'),
    ({'action': 'organize', 'id': 'a', 'expected_hash': 3}, 'nonempty string'),
])
async def test_new_invalid_wire_gets_exact_feedback_before_any_choice_or_effect(tmp_path, update, diagnostic):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Record an observed result', ['Source remains exact'])
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded', data={'entries': ['a']}).model_dump()
    gateway = CorrectingGateway(engine.policy, [update]); engine.gateway = gateway
    value = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', proposal_input(op, result))
    assert value.skill_updates == []
    assert len(gateway.learning_inputs) == 2
    feedback = gateway.learning_inputs[1]['actual_format_feedback']
    assert feedback['rejected_response']['skill_updates'] == [update]
    assert diagnostic in feedback['validation']['message']
    assert all(p['skill_update_contract'] == learning_update_contract() for p in gateway.learning_inputs)
    actual_schema = Learning.model_json_schema()['properties']['skill_updates']
    assert actual_schema['items'] == learning_updates_schema()['items']
    assert store.records('knowledge_application') == [] and store.records('episode') == []
    assert executor.calls == []
    assert not any('choices' in phase for _, phase, _ in gateway.calls)
    rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output']
    assert len(rejected) == 1 and rejected[0]['detail']['rejected_response']['skill_updates'] == [update]
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing', 'foreign', 'stale', 'merge-stale', 'use-without-application'])
async def test_new_target_and_version_failures_return_feedback_without_mutation(tmp_path, kind):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep exact existing knowledge', ['No fabricated application'])
    skill = seed_skill(store, engine.knowledge, task)
    update = {'action': 'improve', 'id': skill['id'], 'expected_hash': skill['hash'], 'content': 'Reviewed candidate'}
    if kind == 'missing': update['id'] = 'missing'
    elif kind == 'foreign':
        child = store.create_task('Private child', ['Own scope'], parent_id=task['id'])
        private = seed_skill(store, engine.knowledge, child)
        update.update(id=private['id'], expected_hash=private['hash'])
    elif kind == 'stale': update['expected_hash'] = '0'*64
    elif kind == 'merge-stale':
        other = seed_skill(store, engine.knowledge, task)
        update.update(action='merge', merge_ids=[other['id']], merge_hashes={other['id']: '0'*64})
    else: update = {'action': 'use', 'id': skill['id']}
    before = deepcopy(store.records('skill')); episodes = deepcopy(store.records('episode'))
    op = operation('file_list', args={}); result = OperationResult(operation_id=op['id'], status='succeeded', data={'entries': ['a']}).model_dump()
    gateway = CorrectingGateway(engine.policy, [update]); engine.gateway = gateway
    value = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', proposal_input(op, result))
    assert value.skill_updates == [] and len(gateway.learning_inputs) == 2
    assert gateway.learning_inputs[1]['actual_format_feedback']['rejected_response']['skill_updates'] == [update]
    assert store.records('skill') == before and store.records('episode') == episodes
    assert executor.calls == []
    await engine.close(); store.close()


def test_final_atomic_consumer_keeps_default_provisional_and_rolls_back_invalid_updates(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Keep the original knowledge', ['No unsupported mutation'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    invalid = learning(skill_updates=[create_update(), {'action': 'create-provisional'}])
    # Historical response parsing does not rewrite or reject the original source.
    assert Learning.model_validate(invalid.model_dump()).model_dump() == invalid.model_dump()
    with pytest.raises(LearningContractError, match='unsupported'):
        engine.knowledge.apply(task, op, invalid, result, [])
    assert store.records('episode') == [] and store.records('skill') == []
    assert store.records('knowledge_application') == []
    good = learning()
    outcome = engine.knowledge.apply(task, op, good, result, [])
    skill = store.record_get('skill', outcome['skills'][0])
    assert skill['status'] == 'provisional' and skill['effect'] == 'UNUSED_EFFECT_UNVERIFIED'
    assert skill['content'] == good.outcome_summary and skill['next_trigger'] == good.next_use_trigger
    assert skill['uses'] == [] and skill['needs_cleanup'] and skill['sources'] == [outcome['episode_id']]
    assert engine.knowledge.apply(task, op, good, result, []) == outcome
    assert len(store.records('episode')) == 1 and len(store.records('skill')) == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['unknown-result', 'missing-selection', 'wrong-evidence', 'wrong-procedure'])
async def test_application_evidence_is_checked_before_review_against_original_journal(tmp_path, fault):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Apply an actual procedure', ['Exact evidence'])
    skill = seed_skill(store, engine.knowledge, task)
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded', effect='confirmed', data={'sha256': 'a'*64}).model_dump()
    op, result, value, selected = observed_application(store, task, skill, op=op, result=result)
    if fault == 'unknown-result': result['effect'] = 'unknown'
    if fault == 'wrong-evidence': value.applications[0]['evidence'][0]['sha256'] = '0'*64
    if fault == 'wrong-procedure': value.applications[0]['procedure_clause'] = 'Not selected'
    journal(store, task, op, result, value, selected)
    gateway = CorrectingGateway(engine.policy, [{'action': 'use', 'id': skill['id']}], application=value.applications)
    engine.gateway = gateway
    before = deepcopy(store.records('skill'))
    source = proposal_input(op, result, [] if fault == 'missing-selection' else selected)
    actual = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert actual.applications == [] and len(gateway.learning_inputs) == 2
    assert gateway.learning_inputs[1]['actual_format_feedback']['rejected_response']['applications'] == value.applications
    assert store.records('skill') == before and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_normal_task_correction_still_delivers_file_through_complete_fixture_cycle(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    gateway = CorrectingGateway(engine.policy, [{'action': 'create-provisional'}]); engine.gateway = gateway
    task = store.create_task('Write hello in answer.txt', ['answer.txt contains hello'])
    result = await engine.run_task(task['id'])
    assert result['task']['status'] == 'completed', result['events'][-1]
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    assert [x['kind'] for x in executor.calls] == ['file_write', 'file_read']
    assert all(s['effect'] == 'UNUSED_EFFECT_UNVERIFIED' for s in result['knowledge']['skills'])
    rejection = next(e for e in result['events'] if e['status'] == 'rejected_model_output')
    choice = next(e for e in result['events'] if e['stage'] == 'web_learning_choices_before')
    assert rejection['seq'] < choice['seq']
    assert all('skill_update_contract' in p for p in gateway.learning_inputs)
    assert store.verify_events()
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_old_normal_reviewed_invalid_learning_resumes_same_result_after_restart(tmp_path, monkeypatch):
    store, engine, executor, gateway = runtime(tmp_path)
    task = store.create_task('Write exact hello', ['The five bytes are verified'])
    op = operation()
    state = await prepare_operation(engine, task, op)
    await engine._pre(state); await engine._execute(state)
    invalid = [{'action': 'create-provisional'}, {'action': 'record-only (no skill, no rule)'}]
    engine.gateway = CorrectingGateway(engine.policy, invalid, phase='learning_proposal')
    # Reproduce the old missing proposal validation at its actual producer.
    # Calls, attempts, independent review and the operation keep one consistent
    # returned proposal. Restore current validation before the real consumer.
    with monkeypatch.context() as old:
        old.setattr(engine.knowledge, 'validate_learning_proposal', lambda *a, **k: None)
        await engine._post(state)
    row = store.get_operation(op['id']); original_result = deepcopy(row['result'])
    assert row['post_bundle']['learning']['skill_updates'] == invalid
    prior_attempt, = [a for a in store.records('post_learning_attempt') if a['operation_id'] == op['id']]
    assert prior_attempt['learning'] == row['post_bundle']['learning']
    assert prior_attempt['review'] == row['post_review']
    legacy = deepcopy(store.get_operation(op['id']))
    with pytest.raises(RevisionNeeded, match='Stored Learning'):
        await engine._learn(state)
    failure = store.records('learning_contract_failure')[0]
    assert failure['original'] == legacy
    assert store.get_operation(op['id'])['result'] == original_result
    assert store.get_operation(op['id'])['status'] == 'result_recorded'
    assert store.record_get('knowledge_application', task['id'] + ':' + op['id']) is None
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    await engine._pre(state); await engine._execute(state)
    await engine._post(state); await engine._learn(state)
    current = store.get_operation(op['id'])
    assert current['status'] == 'learned' and current['result'] == original_result
    assert executor.calls == [op]
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    assert store.record_get('learning_contract_failure', failure['id']) == failure
    before_ideas = {i['id']: (i['proposal'], i['target']) for i in legacy['post_bundle']['learning']['ideas']}
    after_ideas = {i['id']: (i['proposal'], i['target']) for i in current['post_bundle']['learning']['ideas']}
    assert before_ideas.items() <= after_ideas.items()
    calls = engine.gateway.calls
    regenerated = next(p for _, phase, p in calls if phase == 'learning_proposal')
    feedback=regenerated['actual_revision_feedback']
    assert feedback['version']=='learning-current-v1'
    originals=[]
    for reference in feedback['sources']:
        assert reference['kind']=='learning_revision_source'
        record=store.record_get(reference['kind'],reference['id'])
        assert digest(record)==reference['sha256']
        originals.append(record['value'])
    assert any(original.get('actual_application_failure')==failure for original in originals)
    for key,value in failure.items():
        if key=='original':continue  # Exact original already retained and compared above.
        assert any(fact['field']=='actual_application_failure.'+key and fact['value']==value
                   for fact in feedback['facts']),key
    assert any(phase == 'learning_choices_before' for _, phase, _ in calls)
    assert any(phase == 'post_review' for _, phase, _ in calls)
    assert not any(phase == 'research_query' for _, phase, _ in calls)
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_normal_commit_before_status_write_is_recovered_without_reapplying(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Record observed output', ['Preserve commit'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded', data={'observed': 'hello'}).model_dump()
    value = learning(skill_updates=[create_update()])
    journal(store, task, op, result, value)
    # Model a process loss after the actual atomic commit but before row status.
    outcome = engine.knowledge.apply(task, op, value, result, [])
    sources = deepcopy(store.records('episode')); skills = deepcopy(store.records('skill'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    def no_apply(*args, **kwargs):
        raise AssertionError('A committed application must not execute its semantic body again')
    monkeypatch.setattr(engine.knowledge, '_apply', no_apply)
    monkeypatch.setattr(engine.knowledge, 'validate_learning_proposal', no_apply)
    await engine._learn({'task_id': task['id'], 'operation_id': op['id']})
    assert store.get_operation(op['id'])['knowledge'] == outcome
    assert store.records('episode') == sources and store.records('skill') == skills
    assert engine.gateway.calls == [] and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_observed', [True, False])
async def test_legacy_web_failure_corrects_saved_acquisition_without_refetch(tmp_path, monkeypatch, failure_observed):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Acquire exact source', ['Source retained'])
    op = web_operation(); requests = []
    web = collector(store, requests); engine.web = web
    gateway = CorrectingGateway(engine.policy, [{'action': 'create-provisional'}, {'action': 'record-only (no skill, no rule)'}])
    engine.gateway = gateway
    # Reproduce ONLY the old admission boundary in this isolated fixture, letting
    # real Web/Engine journaling retain the legacy accepted input and first fault.
    with monkeypatch.context() as old:
        old.setattr(engine.knowledge, 'validate_learning_proposal', lambda *a, **k: None)
        def original_failure(*args, **kwargs):
            if not failure_observed:
                raise SimulatedProcessLoss('Accepted legacy proposal saved; apply result unobserved')
            raise PolicyError('Skill update target is unavailable')
        old.setattr(engine.knowledge, 'apply', original_failure)
        with pytest.raises(PolicyError if failure_observed else SimulatedProcessLoss):
            await engine._research(task['id'], op, 'pre', {})
    saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
    original = deepcopy(saved); exchange = deepcopy(store.records('web_exchange'))
    assert len(requests) == 1 and len(saved['learning_attempts']) == 1
    assert saved['learning_attempts'][0]['disposition']['verdict'] == 'proceed'
    if failure_observed:
        assert saved['application_failures'][0]['message'] == 'Skill update target is unavailable'
    assert store.records('knowledge_application') == []
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests)
    actual = await engine._research(task['id'], op, 'pre', {})
    current = store.record_get('web_work', saved['id'])
    assert current['status'] == 'complete' and len(current['learning_attempts']) == 2
    old_attempt = deepcopy(current['learning_attempts'][0])
    if not failure_observed:
        assert old_attempt.pop('proposal_failure')['target_effect'] == 'none; pure validation'
    assert old_attempt == original['learning_attempts'][0]
    assert current.get('application_failures') == original.get('application_failures')
    assert current['acquisition_operation'] == original['acquisition_operation']
    assert current['acquisition_result'] == original['acquisition_result']
    assert store.records('web_exchange') == exchange and len(requests) == 1
    assert actual['sources'][0]['text'] == 'exact mock response'
    inputs = [p for _, phase, p in engine.gateway.calls if phase == 'web_acquisition_learning']
    assert len(inputs) == 1
    from tests.test_web_learning_resume import assert_revision_facts
    assert_revision_facts(store,inputs[0]['actual_revision_feedback'],current['learning_attempts'][0])
    assert inputs[0]['skill_update_contract'] == learning_update_contract()
    required = {i['id']: (i['proposal'], i['target']) for i in original['learning_attempts'][0]['learning']['ideas']}
    retained = {i['id']: (i['proposal'], i['target']) for i in current['applied_learning']['ideas']}
    assert required.items() <= retained.items()
    applications = deepcopy(store.records('knowledge_application'))
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application') == applications and len(requests) == 1
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_order', [False, True])
async def test_cached_proposal_rechecks_current_cas_and_preserves_old_receipt(tmp_path, legacy_order):
    store, engine, executor, gateway = runtime(tmp_path)
    task = store.create_task('Keep actual Skill revisions', ['No stale mutation'])
    skill = seed_skill(store, engine.knowledge, task)
    update = {'action': 'improve', 'id': skill['id'], 'expected_hash': skill['hash'], 'content': 'Candidate body'}
    op = operation('file_list', args={})
    result = OperationResult(operation_id=op['id'], status='succeeded', data={'z': {'last': 2, 'first': 1}, 'a': [3, 4]}).model_dump()
    if legacy_order:
        def original_pointers(value, prefix=''):
            rows = [{'pointer': prefix, 'sha256': digest(value)}] if prefix else []
            if isinstance(value, dict):
                for key, child in value.items():
                    rows.extend(original_pointers(child, prefix+'/'+str(key).replace('~', '~0').replace('/', '~1')))
            elif isinstance(value, list):
                for index, child in enumerate(value):rows.extend(original_pointers(child, prefix+'/'+str(index)))
            return rows
        engine._evidence_pointers = original_pointers
    gateway = CorrectingGateway(engine.policy, [update]); engine.gateway = gateway
    source = proposal_input(op, result)
    first = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert first.skill_updates == [update]
    cached = deepcopy(store.records('bounded_model_completed')[0])
    # A different real knowledge operation changes the current version.
    other_op = operation('file_list', args={})
    other = learning(skill_updates=[{**update, 'content': 'Other reviewed actual result'}])
    engine.knowledge.apply(task, other_op, other, OperationResult(operation_id=other_op['id'], status='succeeded').model_dump(), [])
    before = deepcopy(store.record_get('skill', skill['id']))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor, gateway)
    source = deepcopy(cached['payload'])
    current_input = engine._model_input(task['id'], 'learning_proposal', source, Learning)
    retained_input = cached['learning_trace']['request_model_input']
    assert (current_input != retained_input) is legacy_order
    assert engine.bounded_judgments._same_evidence_input(current_input, retained_input)
    revised = await engine.bounded_judgments.learning_proposal(task['id'], 'learning_proposal', source)
    assert revised.skill_updates == [] and len(gateway.learning_inputs) == 2
    assert gateway.learning_inputs[-1]['actual_format_feedback']['rejected_response'] == cached['result']
    assert store.record_get('bounded_model_completed', cached['key']) == cached
    assert store.records('bounded_learning_rejection')[0]['source_receipt']['sha256'] == digest(cached)
    assert store.record_get('skill', skill['id']) == before and executor.calls == []
    await engine.close(); store.close()


def test_final_consumer_rechecks_cas_after_successful_proposal_validation(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Protect an exact version', ['CAS remains required'])
    skill = seed_skill(store, engine.knowledge, task)
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    update = {'action': 'improve', 'id': skill['id'], 'expected_hash': skill['hash'], 'content': 'Proposed body'}
    value = learning(skill_updates=[create_update(), update])
    engine.knowledge.validate_learning_proposal(task, op, value, result, [])
    other_op = operation(); other_result = OperationResult(operation_id=other_op['id'], status='succeeded').model_dump()
    engine.knowledge.apply(task, other_op, learning(skill_updates=[{**update, 'content': 'Intervening actual revision'}]), other_result, [])
    before = deepcopy(store.records('skill')); episodes = deepcopy(store.records('episode'))
    with pytest.raises(PolicyError, match='changed'):
        engine.knowledge.apply(task, op, value, result, [])
    assert store.records('skill') == before and store.records('episode') == episodes
    assert store.record_get('knowledge_application', task['id'] + ':' + op['id']) is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('restart_after_repeated_defect', [False, True])
async def test_new_idea_in_invalid_generated_learning_is_retained_in_feedback_and_resume(tmp_path, restart_after_repeated_defect):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve each actual candidate', ['No idea is silently dropped'])
    op = operation('file_list', args={}); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    source = proposal_input(op, result)
    idea = {'id': 'observed-in-invalid-response', 'target': 'task', 'proposal': 'Compare a future original-result example',
            'disposition': 'investigate', 'rationale': 'Actual benefit has not been verified', 'next_operation': None}
    class Gateway(FixtureGateway):
        count = 0
        async def generate(self, role, phase, payload, schema):
            value, usage = await super().generate(role, phase, payload, schema)
            if schema is Learning:
                self.count += 1
                if self.count <= (2 if restart_after_repeated_defect else 1):
                    ids = {i.id for i in value.ideas}
                    items = [i.model_dump() for i in value.ideas] + ([] if idea['id'] in ids else [idea])
                    value = Learning.model_validate({**value.model_dump(), 'ideas': items, 'skill_updates': [{'action': 'create-provisional'}]})
            return value, usage
    engine.gateway = Gateway(engine.policy)
    if restart_after_repeated_defect:
        with pytest.raises(PolicyError, match='repeated the same proposal-contract defect'):
            await engine._call(task['id'], 'learning_proposal', source, Learning)
        rejected = deepcopy([e for e in store.events(task['id']) if e['status'] == 'rejected_model_output'])
        assert len(rejected) == 2
        await engine.close(); store.close()
        store, engine = reopen(store, engine, executor)
        actual = await engine._call(task['id'], 'learning_proposal', source, Learning)
        assert [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output'] == rejected
    else:
        actual = await engine._call(task['id'], 'learning_proposal', source, Learning)
        assert len(engine.gateway.calls) == 2
    carried = next(i for i in actual.ideas if i.id == idea['id'])
    assert (carried.proposal, carried.target) == (idea['proposal'], idea['target'])
    assert any(idea in p['required_ideas'] for _, _, p in engine.gateway.calls)
    rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output']
    assert rejected[0]['detail']['rejected_response']['skill_updates'] == [{'action': 'create-provisional'}]
    assert idea in rejected[0]['detail']['retained_ideas']
    assert store.records('knowledge_application') == [] and executor.calls == []
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('web', [False, True])
async def test_valid_application_failure_resumes_original_after_actual_noncommit_observation(tmp_path, monkeypatch, web):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve an unknown application outcome', ['No replay of unknown effects'])
    def fail(*args, **kwargs): raise OSError('Synthetic storage outcome unavailable')
    if web:
        op = web_operation(); requests = []; engine.web = collector(store, requests)
        with monkeypatch.context() as broken:
            broken.setattr(engine.knowledge, 'apply', fail)
            with pytest.raises(OSError, match='outcome unavailable'):
                await engine._research(task['id'], op, 'pre', {})
        previous = deepcopy(store.records('web_work'))
    else:
        op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
        journal(store, task, op, result, learning())
        with monkeypatch.context() as broken:
            broken.setattr(engine.knowledge, 'apply', fail)
            with pytest.raises(OSError, match='outcome unavailable'):
                await engine._learn({'task_id': task['id'], 'operation_id': op['id']})
        previous = deepcopy(store.records('knowledge_application_failure'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    applied = []
    actual_apply = engine.knowledge._apply
    def count_apply(*args, **kwargs):
        applied.append(deepcopy(args[2].model_dump()))
        return actual_apply(*args, **kwargs)
    monkeypatch.setattr(engine.knowledge, '_apply', count_apply)
    if web:
        await engine.web_judgments.drain(task['id'])
        await engine.web_judgments.drain(task['id'])
        old = next(w for w in previous if w['stage'] == 'after')
        current = store.record_get('web_work', old['id'])
        assert current['status'] == 'complete'
        assert current['learning_attempts'] == old['learning_attempts']
        assert current['application_failures'] == old['application_failures']
        assert current['acquisition_result'] == old['acquisition_result']
        assert applied == [old['learning_attempts'][0]['learning']]
        assert len(requests) == 1
    else:
        state = {'task_id': task['id'], 'operation_id': op['id']}
        await engine._learn(state); await engine._learn(state)
        assert store.get_operation(op['id'])['status'] == 'learned'
        assert store.records('knowledge_application_failure') == previous
        assert applied == [previous[0]['original']['post_bundle']['learning']]
    assert len(store.records('knowledge_application')) == 1 and executor.calls == []
    assert not any('learning_proposal' in phase or phase == 'web_acquisition_learning' for _, phase, _ in engine.gateway.calls)
    observations = store.records('knowledge_application_reconciliation')
    assert len(observations) == 1
    assert observations[0]['observation']['state'] == 'not_committed'
    assert observations[0]['observation']['application'] is None and observations[0]['observation']['episodes'] == []
    await engine.close(); store.close()


def test_atomic_application_observation_does_not_treat_orphan_episode_as_noncommit(tmp_path, monkeypatch):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Keep inconsistent state visible', ['Do not invent non-commit'])
    op = operation(); result = OperationResult(operation_id=op['id'], status='succeeded').model_dump()
    value = learning(); engine.knowledge.apply(task, op, value, result, [])
    read = store.record_get
    # Read fault injection only: original committed records are never edited.
    monkeypatch.setattr(store, 'record_get', lambda kind, identity: None if kind == 'knowledge_application' else read(kind, identity))
    observed = engine.knowledge.application_state(task, op, value, result, [])
    assert observed['state'] == 'unknown' and len(observed['episodes']) == 1
    store.close()


@pytest.mark.asyncio
async def test_unobserved_web_commit_is_reconciled_before_contract_regeneration(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain exact committed acquisition', ['No replay']); op = web_operation(); requests = []
    engine.web = collector(store, requests)
    record = store.record
    def crash(kind, identity, body):
        if kind == 'web_work' and body.get('learning_applied'):
            raise SimulatedProcessLoss('Commit observed, caller outcome save not observed')
        return record(kind, identity, body)
    monkeypatch.setattr(store, 'record', crash)
    with pytest.raises(SimulatedProcessLoss): await engine._research(task['id'], op, 'pre', {})
    monkeypatch.setattr(store, 'record', record)
    applications = deepcopy(store.records('knowledge_application'))
    await engine.close(); store.close()
    store, engine = reopen(store, engine, executor)
    def no_new(*a, **k): raise AssertionError('Original committed acquisition must win')
    monkeypatch.setattr(engine.knowledge, 'apply', no_new)
    monkeypatch.setattr(engine.knowledge, 'validate_learning_proposal', no_new)
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application') == applications and len(requests) == 1
    saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
    assert saved['application_recovery']['application_replayed'] is False
    assert not any(phase == 'web_acquisition_learning' for _, phase, _ in engine.gateway.calls)
    await engine.close(); store.close()
