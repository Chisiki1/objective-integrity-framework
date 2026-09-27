"""Synthetic HTTP over real Store/Engine/Web consumers; no live model claim.

Historical holds disable only the new partition classification while the real
old send, feedback, rejection, events and immutable receipts are produced.
"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from policy_harness import models as m
from policy_harness.learning_projection import current_input, groups, revision_feedback
from policy_harness.store import digest
from tests.test_core import runtime
from tests.test_knowledge import operation
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_providers import envelope
from tests.test_semantic_wire_contract import install, assert_wire_records
from tests.test_web_recovery import collector, operation as web_parent
from tests.test_partial_assessment_recovery import inputs as assessment_inputs, response_rows


def idea(identity, meaning=None, **changes):
    return m.Idea(id=identity, target='task', proposal='Preserve observed meaning ' + (meaning or identity),
        disposition='reject', rationale='No extra effect is justified by this synthetic observation',
        **changes).model_dump()


def learning_source():
    op = operation('file_list', args={})
    result = m.OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    source = proposal_input(op, result)
    values = [idea(str(i)) for i in range(6)]
    # Nonadjacent identities with every semantic field equal form one decision.
    # Same target/proposal with a different rationale remains a distinct one.
    values.insert(3, dict(values[0], id='alias'))
    values[-1] = dict(values[0], id='different', rationale='This contrary observation must be considered separately')
    source['required_ideas'] = values
    return current_input(source)


def wrong_count(call, extra=1):
    field = 'assessments' if call['schema'] is m.AssessmentBatch else 'existing_idea_decisions'
    output = deepcopy(call['output'])
    # Complete typed values; only the outer count is wrong.
    output[field] += [deepcopy(output[field][0])] * extra
    return output


def records_unchanged(store, records):
    for record in records:
        assert store.record_get('bounded_model_call', record['id']) == record


def partition_observations(store):
    values = [v for v in store.records('bounded_output_split') if 'cardinality' in v]
    assert values
    for split in values:
        count = split['cardinality']['logical_count']
        assert count > 1 and all(0 < n < count for n in split['child_logical_counts'])
        original = store.record_get('bounded_model_call', split['failed_call']['id'])
        assert split['failed_call'] == {'id': original['id'], 'key': original['key'], 'sha256': digest(original)}
        assert original['status'] == 'failed' and 'repeated' in original['first_fault']['message']
    return values


@pytest.mark.asyncio
async def test_saved_nine_target_one_row_and_extra_note_partitions_all_targets(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Judge nine exact targets', ['Every target needs its own assessment'])
    targets, context = assessment_inputs(9)
    original = []
    def one_row(call, number):
        if call['schema'] is m.AssessmentBatch and len(call['canonical']['targets']) == 9:
            body = response_rows(call)
            body['assessments'] = body['assessments'][:1]
            body['assessments_note'] = 'one exchange judged'
            original.append(deepcopy(body))
            return body
    first = install(store, engine, mutate=one_row)
    try:
        # Preserve the historical failed HTTP responses before this classifier existed.
        with monkeypatch.context() as historical:
            historical.setattr(engine.bounded_judgments, '_outer_cardinality', lambda *a, **kw: None)
            with pytest.raises(m.PolicyError, match='repeated|defect'):
                await engine._assess_targets(task['id'], 'nine-target-note', targets, context)
        assert len(original) == 2 and len(first.calls) == 2
        old_calls = deepcopy(store.records('bounded_model_call'))
        old_responses = deepcopy(store.records('model_response'))
        assert all(row['status'] == 'failed' for row in old_calls)
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        values = await engine._assess_targets(task['id'], 'nine-target-note', targets, context)
        assert [row['target_id'] for row in values] == [row['id'] for row in targets]
        assert all(len(call['canonical']['targets']) < 9 for call in after.calls if call['schema'] is m.AssessmentBatch)
        assert not any(row['assessment']['rationale'] == original[-1]['assessments'][0]['rationale'] for row in values)
        engine.bounded_judgments.authenticate_assessments(task['id'], 'nine-target-note', targets, context, values)
        assert await engine._assess_targets(task['id'], 'nine-target-note', targets, context) == values
        assert len(after.calls) == 3
        review = await engine.bounded_judgments.review_proposal(task['id'], context['operation'],
            'nine-target-review', dict(context, targets=targets, assessments=values))
        assert review and after.calls[-1]['schema'] is m.Review
        assert after.calls[-1]['canonical']['assessments'] == values
        splits = partition_observations(store)
        assert any(split['cardinality']['expected'] == 9 and split['cardinality']['returned'] == 1
                   and sum(split['child_logical_counts']) == 9 for split in splits)
        records_unchanged(store, old_calls)
        assert all(store.record_get('model_response', row['id']) == row for row in old_responses)
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_fresh_learning_count_failure_returns_smaller_groups_and_reuses_composition(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain every exact observation', ['A coherent reviewed proposal is still required'])
    source = learning_source(); roots = []
    new_idea = {k:v for k,v in idea('new', 'independent rejected-response candidate').items() if k != 'id'}
    def mismatch(call, number):
        if call['schema'] is m.Learning:
            roots.append(call)
            output = wrong_count(call, 3 - len(roots))
            if len(roots) == 2:output['new_ideas'] = [new_idea]
            return output
    before = install(store, engine, mutate=mismatch)
    try:
        learned = await engine.bounded_judgments.learning_proposal(task['id'], 'count-learning', source)
        assert len(roots) == 2
        assert [len(c['output']['existing_idea_decisions']) for c in roots] == [6, 6]
        by_id = {i.id:i.model_dump() for i in learned.ideas}
        assert all(by_id.get(i['id']) == i for i in source['required_ideas'])
        rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output']
        retained = rejected[-1]['detail']['response_idea_extraction']['ideas']
        assert len(retained) == 1 and by_id[retained[0]['id']] == retained[0]
        children = [c for c in before.calls if c['schema'] is m.LearningIdeas]
        assert children and all(0 < len(c['capture']['association']['required_idea_groups']) < 6 for c in children)
        assert all(('0' in [i['id'] for i in c['canonical']['required_ideas']]) ==
                   ('alias' in [i['id'] for i in c['canonical']['required_ideas']]) for c in children)
        partition_observations(store)
        old = deepcopy(store.records('bounded_model_call'))
        assert store.records('knowledge_application') == [] and executor.calls == []
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        assert await engine.bounded_judgments.learning_proposal(task['id'], 'count-learning', source) == learned
        assert after.calls == []
        records_unchanged(store, old)
        assert_wire_records(store, before.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('restart', [False, True])
async def test_web_review_revision_after_count_partition_retains_successes(tmp_path, restart):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Complete a reviewed correction after acquiring evidence', ['One GET and one reviewed Knowledge commit'])
    parent = web_parent(); requests = []; web = collector(store, requests)
    roots = []; dispositions = []; transports = []; retained = {}
    meanings = [{k:v for k,v in i.items() if k != 'id'} for i in learning_source()['required_ideas']]

    def mutate(call, number):
        if call['phase'] == 'web_acquisition_after' and call['schema'] is m.AssessmentBatch:
            output = deepcopy(call['output'])
            for index, assessment in enumerate(output['assessments']):assessment['ideas'] = meanings if index == 0 else []
            return output
        if call['phase'] == 'web_acquisition_learning' and call['schema'] is m.Learning:
            roots.append(call)
            if len(roots) <= 2:return wrong_count(call, len(roots))
            assert len(roots) == 3, 'The old failed root is not a new semantic revision'
            assert 'actual_format_feedback' not in call['canonical']
            assert call['canonical']['actual_revision_feedback']['sources']
            return dict(call['output'], outcome_summary='Revised using the independently reviewed observations')
        if call['phase'] == 'web_learning_disposition':
            dispositions.append(call)
            if len(dispositions) == 1:
                retained['calls'] = deepcopy([r for r in store.records('bounded_model_call') if r['status'] != 'started'])
                retained['exchanges'] = deepcopy(store.records('web_exchange'))
                if restart:engine.stop_requested.add(task['id'])
                from tests.test_semantic_wire_contract import source_choice
                return dict(call['output'], verdict='revise', revision_scope='learning',
                    revision_targets=[source_choice(call, '/learning/outcome_summary')],
                    rationale='Revise the proposal to address these actual review opinions')

    transports.append(install(store, engine, mutate=mutate))
    try:
        async def collect():
            return await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id=parent['id'],
                evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
        if restart:
            with pytest.raises(asyncio.CancelledError):await collect()
            saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
            attempt = saved['learning_attempts'][0]
            assert 'review' in attempt and 'disposition_input' in attempt and 'disposition' not in attempt
            decisions = [r for r in store.records('bounded_model_call') if r.get('phase') == 'web_learning_disposition']
            assert len(decisions) == 1 and decisions[0]['status'] == 'succeeded'
            assert decisions[0]['result']['verdict'] == 'revise'
            assert store.records('knowledge_application') == []
            assert len(dispositions) == 1 and len(roots) == 2
            retained['calls'] = deepcopy(store.records('bounded_model_call'))
            await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
            transports.append(install(store, engine, mutate=mutate))
            await engine.web_judgments.drain(task['id'])
        else:
            await collect()
        current = next(w for w in store.records('web_work') if w['stage'] == 'after')
        assert current['status'] == 'complete' and len(current['learning_attempts']) == 2
        assert len(roots) == 3 and len(dispositions) == 2 and len(requests) == 1
        assert current['learning_attempts'][0]['disposition']['verdict'] == 'revise'
        assert current['learning_attempts'][1]['disposition']['verdict'] == 'proceed'
        assert {i['id'] for i in roots[0]['canonical']['required_ideas']} <= {i['id'] for i in current['applied_learning']['ideas']}
        records_unchanged(store, retained['calls'])
        assert store.records('web_exchange') == retained['exchanges']
        partition_observations(store)
        applications = deepcopy(store.records('knowledge_application')); assert len(applications) == 1
        calls = sum(len(t.calls) for t in transports)
        await engine.web_judgments.drain(task['id'])
        assert sum(len(t.calls) for t in transports) == calls and store.records('knowledge_application') == applications
        assert executor.calls == [] and store.verify_events()
        assert_wire_records(store, [call for transport in transports for call in transport.calls])
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['same-revision', 'operation', 'result', 'pre', 'selection', 'required',
    'nonprefix', 'reordered', 'missing-source', 'facts', 'historical-response', 'ambiguous', 'unobserved'])
async def test_new_review_revision_does_not_hide_changed_or_unknown_inputs(tmp_path, monkeypatch, boundary):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve exact correction and operation ownership', ['No new send may hide a changed or unknown input'])
    source = learning_source(); op_id = source['operation']['id']
    attempts = [{'proposal_failure': {'reason': 'First distinct observed proposal failure'}}]
    source['actual_revision_feedback'] = revision_feedback(store, task['id'], op_id, attempts)
    first = install(store, engine, mutate=lambda call, n: wrong_count(call, n))
    try:
        with monkeypatch.context() as historical:
            historical.setattr(engine.bounded_judgments, '_outer_cardinality', lambda *a, **kw: None)
            with pytest.raises(m.PolicyError, match='repeated'):
                await engine.bounded_judgments.learning_proposal(task['id'], 'revision-boundary', source)
        assert len(first.calls) == 2
        failed = next(r for r in store.records('bounded_model_call') if r['status'] == 'failed')
        following = deepcopy(source)
        attempts.append({'proposal_failure': {'reason': 'A later distinct observed review correction'}})
        following['actual_revision_feedback'] = revision_feedback(store, task['id'], op_id, attempts)
        if boundary == 'same-revision':
            following['actual_revision_feedback'] = source['actual_revision_feedback']
            following['required_ideas'][0]['proposal'] = 'Changed original candidate'
        elif boundary == 'operation':following['operation']['args'] = {'path': 'different'}
        elif boundary == 'result':following['result']['data']['entries'] = ['different']
        elif boundary == 'pre':following['pre']['skills']['new_knowledge_needed'] = ['different']
        elif boundary == 'selection':following['selection_binding'] = {'foreign': True}
        elif boundary == 'required':following['required_ideas'][0]['proposal'] = 'Changed original candidate'
        elif boundary == 'nonprefix':following['actual_revision_feedback'] = revision_feedback(store, task['id'], op_id, attempts[1:])
        elif boundary == 'reordered':following['actual_revision_feedback'] = revision_feedback(store, task['id'], op_id, list(reversed(attempts)))
        elif boundary == 'missing-source':following['actual_revision_feedback']['sources'][-1]['id'] = 'missing-source'
        elif boundary == 'facts':following['actual_revision_feedback']['facts'][-1]['value'] = 'Changed fact'
        elif boundary == 'historical-response':
            rejection = failed['learning_trace']['rejected_events'][-1]
            event = next(e for e in store.events(task['id']) if {'seq': e['seq'], 'hash': e['hash']} == rejection)
            response = store.record_get('model_response', event['detail']['metadata']['response_record']['id'])
            store.record('model_response', response['id'], dict(response, messages_sha256='foreign-request'))
        elif boundary == 'ambiguous':
            other = deepcopy(failed); other['id'] = 'another-historical-call'; other['learning_trace'] = {'call_id': other['id']}
            store.record('bounded_model_call', other['id'], other)
        else:store.record('bounded_model_call', failed['id'], dict(failed, status='unobserved'))
        old_calls = deepcopy(store.records('bounded_model_call'))
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        with pytest.raises(m.PolicyError):
            await engine.bounded_judgments.learning_proposal(task['id'], 'revision-boundary', following)
        assert after.calls == [] and store.records('knowledge_application') == [] and executor.calls == []
        records_unchanged(store, old_calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('historical', [False, True])
async def test_assessment_count_partition_reopens_old_targets_and_keeps_successful_sibling(tmp_path, monkeypatch, historical):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Assess the exact pending choices', ['Keep each complete thinking-target assessment'])
    op = operation('file_list', args={})
    targets = [{'id': str(i), 'statement': 'Independent actual choice ' + str(i)} for i in range(4)]
    context = current_input({'operation': op, 'timing': 'before this exact choice'})
    root_count = 0; pause = True
    def mutate(call, number):
        nonlocal root_count, pause
        if call['schema'] is not m.AssessmentBatch:return
        if len(call['canonical']['targets']) == 4:
            root_count += 1
            return wrong_count(call, root_count)
        if pause:
            pause = False
            engine.stop_requested.add(task['id'])  # Complete this response, stop before the next dispatch.
    first = install(store, engine, mutate=mutate)
    try:
        if historical:
            with monkeypatch.context() as old:
                old.setattr(engine.bounded_judgments, '_outer_cardinality', lambda *a, **kw: None)
                with pytest.raises(m.PolicyError, match='repeated'):
                    await engine._assess_targets(task['id'], 'count-assessment', targets, context)
            held = deepcopy(store.records('bounded_model_call'))
            await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
            second = install(store, engine, mutate=mutate)
        else:
            second = first; held = []
        with pytest.raises(asyncio.CancelledError):
            await engine._assess_targets(task['id'], 'count-assessment', targets, context)
        assert root_count == 2
        records_unchanged(store, held)
        saved = deepcopy(store.records('bounded_model_call'))
        success = [r for r in saved if r['status'] == 'succeeded']
        assert len(success) == 1 and len(success[0]['payload']['targets']) == 2
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        # Migration supplies a different current view. The old authenticated
        # failed tree owns these pending choices, including its successful leaf.
        values = await engine._assess_targets(task['id'], 'count-assessment', targets[:1],
            dict(context, instruction='Current view cannot replace the old pending tree'), previous=(targets, context))
        assert [a['target_id'] for a in values] == [t['id'] for t in targets]
        assert all(set(a['assessment']['thinking_targets']) == engine.thinking_targets for a in values)
        assert len(after.calls) == 1 and after.calls[0]['canonical']['targets'] == targets[2:]
        assert values[:2] == success[0]['result']['assessments']
        records_unchanged(store, saved); partition_observations(store)
        assert await engine._assess_targets(task['id'], 'count-assessment', targets, context) == values
        assert len(after.calls) == 1 and executor.calls == [] and store.records('knowledge_application') == []
        assert_wire_records(store, first.calls + ([] if second is first else second.calls) + after.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_corrected_whole_assessment_success_is_reused_without_repartition(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Reuse the actual corrected assessment', ['No repeated successful request'])
    targets = [{'id':str(i), 'statement':'Preserved choice ' + str(i)} for i in range(4)]
    context = current_input({'operation':operation('file_list', args={}), 'timing':'before'})
    first = install(store, engine, mutate=lambda c,n: wrong_count(c, 2) if n == 1 else None)
    try:
        value = await engine._assess_targets(task['id'], 'corrected-whole', targets, context)
        assert len(first.calls) == 2 and first.calls[1]['canonical']['actual_format_feedback']
        old = deepcopy(store.records('bounded_model_call'))
        assert len(old) == 1 and old[0]['status'] == 'succeeded'
        assert store.records('bounded_output_split') == []
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        assert await engine._assess_targets(task['id'], 'corrected-whole', targets[:1],
            dict(context, instruction='Different proposed current view'), previous=(targets, context)) == value
        assert after.calls == [] and store.records('bounded_output_split') == []
        records_unchanged(store, old)
        assert executor.calls == [] and store.records('knowledge_application') == []
        assert_wire_records(store, first.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_historical_web_count_hold_nested_capacity_stop_and_reopen_apply_once(tmp_path, monkeypatch):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Finish the acquired evidence judgment', ['One GET and one actual Knowledge application'])
    parent = web_parent(); requests = []; web = collector(store, requests)
    roots = []; length_call = None; pause_call = None; count_focus = None; count_calls = []
    alias_members = frozenset()
    meanings = [{k:v for k,v in i.items() if k != 'id'} for i in learning_source()['required_ideas']]
    new_idea = {k:v for k,v in idea('new', 'retained independently at the failed root').items() if k != 'id'}
    def focus(call):
        return tuple(tuple(group['members']) for group in call['capture']['association']['required_idea_groups'])
    def members(key):
        return frozenset(member for group in key for member in group)
    def mutate(call, number):
        nonlocal length_call, pause_call, count_focus
        if call['phase'] == 'web_acquisition_after' and call['schema'] is m.AssessmentBatch:
            output = deepcopy(call['output'])
            for index, assessment in enumerate(output['assessments']):assessment['ideas'] = meanings if index == 0 else []
            return output
        if call['phase'] == 'web_acquisition_learning' and call['schema'] is m.Learning:
            roots.append(call); output = wrong_count(call, 3 - len(roots))
            if len(roots) == 2:output['new_ideas'] = [new_idea]
            return output
        if call['schema'] is m.LearningIdeas:
            key = focus(call); actual = members(key)
            if length_call is None and alias_members.issubset(actual):
                assert alias_members and len(key) > 1
                length_call = deepcopy(call)
                body = envelope(json.dumps(call['output'])); body['choices'][0]['finish_reason'] = 'length'
                return httpx.Response(200, json=body)
            if length_call is not None:
                length_members = members(focus(length_call))
                if pause_call is None and alias_members.issubset(actual) and actual < length_members:
                    # Return the valid response; the next send observes stop after this leaf is stored.
                    pause_call = deepcopy(call); engine.stop_requested.add(task['id'])
                elif pause_call is not None and actual < length_members and actual.isdisjoint(members(focus(pause_call))):
                    if count_focus is None and len(key) > 1:
                        count_focus = key
                    if key == count_focus:
                        assert len(count_calls) < 2, 'The exact rejected child must not be resent after its held pair'
                        count_calls.append(deepcopy(call))
                        return wrong_count(call, len(count_calls))
    first = install(store, engine, mutate=mutate)
    try:
        with monkeypatch.context() as old:
            old.setattr(engine.bounded_judgments, '_outer_cardinality', lambda *a, **kw: None)
            with pytest.raises(m.PolicyError, match='repeated'):
                await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id=parent['id'],
                    evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
        saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
        old = deepcopy(store.records('bounded_model_call')); exchanges = deepcopy(store.records('web_exchange'))
        rejections = [e for e in store.events(task['id']) if e['stage'] == 'web_acquisition_learning' and e['status'] == 'rejected_model_output']
        extra = rejections[-1]['detail']['response_idea_extraction']['ideas']
        assert len(extra) == 1 and len(roots) == 2 and len(requests) == 1
        original = roots[0]['canonical']['required_ideas']
        root_groups = roots[-1]['capture']['association']['required_idea_groups']
        root_limit = len(root_groups)
        assert root_limit == len(groups(original)) and members(focus(roots[-1])) == {i['id'] for i in original}
        normal_pre = [i for i in original if {k:v for k,v in i.items() if k != 'id'} not in meanings]
        assert normal_pre  # Keep the normal producer's independent Idea in the failed root.
        assert all(meaning in [{k:v for k,v in i.items() if k != 'id'} for i in original] for meaning in meanings)
        alias_groups = [group for group in root_groups if len(group['members']) > 1]
        assert len(alias_groups) == 1
        alias_members = frozenset(alias_groups[0]['members'])
        assert store.records('knowledge_application') == [] and saved['learning_attempts'] == []
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        second = install(store, engine, mutate=mutate)
        with pytest.raises(asyncio.CancelledError):await engine.web_judgments.drain(task['id'])
        successful = [deepcopy(r) for r in store.records('bounded_model_call')
                      if r.get('learning_schema') == 'LearningIdeas' and r['status'] == 'succeeded']
        assert length_call is not None and pause_call is not None
        stopped_leaf = [r for r in successful if r['wire_trace']['request_model_input'] == pause_call['canonical']]
        assert len(stopped_leaf) == 1
        assert {i['id'] for i in stopped_leaf[0]['result']['ideas']} == members(focus(pause_call))
        assert alias_members.issubset(members(focus(pause_call)))
        assert 0 < len(focus(pause_call)) < len(focus(length_call)) < root_limit
        assert count_calls == [] and len(requests) == 1 and store.records('knowledge_application') == []
        records_unchanged(store, old)
        stopped_calls = deepcopy(store.records('bounded_model_call'))
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        third = install(store, engine, mutate=mutate)
        await engine.web_judgments.drain(task['id'])
        current = store.record_get('web_work', saved['id'])
        assert current['status'] == 'complete' and len(current['learning_attempts']) == 1
        assert len(roots) == 2 and len(count_calls) == 2
        assert all(focus(call) == count_focus for call in count_calls)
        assert 1 < len(count_focus) < len(focus(length_call))
        assert members(count_focus) < members(focus(length_call))
        assert members(count_focus).isdisjoint(members(focus(pause_call)))
        assert not any(c['schema'] is m.Learning for c in second.calls + third.calls)
        assert not any(c['canonical'] == r['wire_trace']['request_model_input'] for c in third.calls for r in successful)
        records_unchanged(store, stopped_calls)
        applied = {i['id']:i for i in current['applied_learning']['ideas']}
        assert all(applied.get(i['id']) == i for i in original + extra)
        pages = [c for c in second.calls + third.calls if c['schema'] is m.LearningIdeas]
        for group in groups(original):
            for call in pages:
                actual = {i['id'] for i in call['canonical']['required_ideas']}
                assert not (actual.intersection(group['members']) and not actual.issuperset(group['members']))
        assert all(len(focus(call)) < root_limit for call in pages)
        assert sum(call['canonical'] == length_call['canonical'] for call in pages) == 1
        partition_observations(store)
        apps = deepcopy(store.records('knowledge_application')); assert len(apps) == 1
        assert current['acquisition_result'] == saved['acquisition_result'] and store.records('web_exchange') == exchanges
        count = len(third.calls); await engine.web_judgments.drain(task['id'])
        assert len(third.calls) == count and store.records('knowledge_application') == apps and len(requests) == 1
        assert executor.calls == [] and store.verify_events()
        assert_wire_records(store, first.calls + second.calls + third.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['zero', 'one', 'inner', 'foreign'])
async def test_nonpartitionable_or_mixed_wire_defect_stays_held_without_new_sends(tmp_path, fault):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep exact boundaries', ['No partial judgment or foreign choice is admitted'])
    source = learning_source()
    if fault == 'zero':source['required_ideas'] = []
    if fault == 'one':source['required_ideas'] = [source['required_ideas'][0], source['required_ideas'][3]]
    targets = [{'id': str(i), 'statement': 'choice ' + str(i)} for i in range(4)]
    async def invoke():
        if fault == 'inner':return await engine._assess_targets(task['id'], 'no-count-split', targets, source)
        return await engine.bounded_judgments.learning_proposal(task['id'], 'no-count-split', source)
    def invalid(call, number):
        if fault == 'zero':
            output = deepcopy(call['output'])
            output['existing_idea_decisions'] = [{'disposition':'reject', 'rationale':'No source slot exists', 'next_operation':None}]
            return output
        output = wrong_count(call, number)
        if fault == 'inner':output['assessments'][0]['thinking_values'].pop()
        if fault == 'foreign':output['applications'] = [{'skill_slot':number, 'evidence':[{'pointer':'/data/entries', 'explanation':'Observed listing'}]}]
        return output
    first = install(store, engine, mutate=invalid)
    try:
        with pytest.raises(m.PolicyError):await invoke()
        assert len(first.calls) == 2 and store.records('bounded_output_split') == []
        old = deepcopy(store.records('bounded_model_call'))
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        with pytest.raises(m.PolicyError):await invoke()
        assert after.calls == [] and store.records('bounded_output_split') == []
        records_unchanged(store, old)
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['capture', 'response', 'settings', 'source', 'stop'])
async def test_saved_learning_count_failure_authenticates_before_any_partition(tmp_path, monkeypatch, boundary):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Authenticate the failed source before a new decision', ['No corrupt or changed binding authorizes partition'])
    source = learning_source()
    first = install(store, engine, mutate=lambda call, n: wrong_count(call, n))
    with monkeypatch.context() as old:
        old.setattr(engine.bounded_judgments, '_outer_cardinality', lambda *a, **kw: None)
        with pytest.raises(m.PolicyError, match='repeated'):
            await engine.bounded_judgments.learning_proposal(task['id'], 'saved-count-binding', source)
    assert len(first.calls) == 2
    failures = deepcopy(store.records('bounded_model_call'))
    if boundary == 'capture':
        captured = first.calls[-1]['capture']
        store.record('semantic_wire_request', captured['id'], dict(captured, phase='foreign-phase'))
    elif boundary == 'response':
        rejected = [e for e in store.events(task['id']) if e['status'] == 'rejected_model_output'][-1]
        ref = rejected['detail']['metadata']['response_record']; response = store.record_get('model_response', ref['id'])
        store.record('model_response', ref['id'], dict(response, messages_sha256='foreign-request'))
    elif boundary == 'source':
        store.append_instruction(task['id'], 'New exact source boundary', task['source_hash'])
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    if boundary == 'settings':engine.gateway.settings.values['max_output_tokens'] -= 1
    if boundary == 'stop':engine.stop_requested.add(task['id'])
    try:
        with pytest.raises(asyncio.CancelledError if boundary == 'stop' else m.PolicyError):
            await engine.bounded_judgments.learning_proposal(task['id'], 'saved-count-binding', source)
        assert after.calls == [] and store.records('bounded_output_split') == []
        records_unchanged(store, failures)
        assert executor.calls == [] and store.records('knowledge_application') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_fresh_target_groups_share_stage_calls_and_resume_reuses_them(tmp_path):
    import hashlib as _hashlib
    from policy_harness import semantic_wire as _wire
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Judge grouped targets', ['Every target keeps its own assessment'])
    targets, context = assessment_inputs(5)
    body = ('Evidence about a target and its dependencies. ' * 620)
    context['result']['data']['response_analysis'] = {'source': {
        'id': 'grouped-source', 'url': 'https://example.test/source', 'text': body,
        'sha256': _hashlib.sha256(body.encode('utf-8')).hexdigest()}}
    def with_citations(call, _):
        if call['schema'] is m.AssessmentBatch:
            raw = deepcopy(call['output'])
            evidence = _wire._assessment_evidence(call['canonical'])
            if evidence:
                page = evidence['excerpts'][0]
                citation = {'source_id': evidence['source_id'], 'source_sha256': evidence['source_sha256'],
                            'start_byte': page['start_byte'], 'end_byte': min(page['start_byte'] + 24, page['end_byte'])}
                for row in raw['assessments']:
                    row['source_citations'] = [citation]
                    row['requested_source_ranges'] = []
            return raw
    first = install(store, engine, mutate=with_citations)
    values = await engine._assess_targets(task['id'], 'grouped-batch', targets, context)
    sizes = [len(call['canonical']['targets']) for call in first.calls if call['schema'] is m.AssessmentBatch]
    assert sizes == [3, 2]
    assert [row['target_id'] for row in values] == [row['id'] for row in targets]
    engine.bounded_judgments.authenticate_assessments(task['id'], 'grouped-batch', targets, context, values)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        assert await engine._assess_targets(task['id'], 'grouped-batch', targets, context) == values
        assert second.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_grouped_partial_correction_keeps_successful_sibling_on_resume(tmp_path):
    import hashlib as _hashlib
    from policy_harness import semantic_wire as _wire
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume grouped judgments', ['Keep completed sibling decisions'])
    targets, context = assessment_inputs(5)
    body = 'Evidence about a target and its dependencies. ' * 620
    context['result']['data']['response_analysis'] = {'source': {
        'id': 'grouped-source', 'url': 'https://example.test/source', 'text': body,
        'sha256': _hashlib.sha256(body.encode('utf-8')).hexdigest()}}
    first_group = True
    def partial_first_group(call, _):
        nonlocal first_group
        if call['schema'] is not m.AssessmentBatch:
            return None
        raw = deepcopy(call['output'])
        if call['capture']['wire_revision'] == _wire.ASSESSMENT_REFERENCE_SLOTS:
            for row in raw['assessments']:
                row['new_ideas'] = [{k: v for k, v in idea.items() if k != 'id'} for idea in row.pop('ideas')]
                row['existing_idea_decisions'] = []
                row['source_citation_slots'] = [0]
                row['requested_source_slots'] = []
            return raw
        evidence = _wire._assessment_evidence(call['canonical'])
        if evidence:
            page = evidence['excerpts'][0]
            citation = {'source_id': evidence['source_id'], 'source_sha256': evidence['source_sha256'],
                        'start_byte': page['start_byte'], 'end_byte': min(page['start_byte'] + 24, page['end_byte'])}
            for row in raw['assessments']:
                row['source_citations'] = [citation]
                row['requested_source_ranges'] = []
        if len(call['canonical']['targets']) == 3 and first_group:
            first_group = False
            raw['assessments'][0]['thinking_values'].pop()
        return raw
    first = install(store, engine, mutate=partial_first_group)
    values = await engine._assess_targets(task['id'], 'grouped-partial-resume', targets, context)
    assert [len(call['canonical']['targets']) for call in first.calls if call['schema'] is m.AssessmentBatch] == [3, 1, 2]
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        assert await engine._assess_targets(task['id'], 'grouped-partial-resume', targets, context) == values
        assert second.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_outer_count_with_foreign_source_does_not_partition(tmp_path):
    import hashlib as _hashlib
    from policy_harness import semantic_wire as _wire
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Hold mixed wire defects', ['Do not split a foreign source judgment'])
    targets, context = assessment_inputs(3)
    body = 'Evidence about a target and its dependencies. ' * 620
    context['result']['data']['response_analysis'] = {'source': {
        'id': 'grouped-source', 'url': 'https://example.test/source', 'text': body,
        'sha256': _hashlib.sha256(body.encode('utf-8')).hexdigest()}}
    def mixed(call, _):
        if call['schema'] is not m.AssessmentBatch:
            return None
        raw = deepcopy(call['output'])
        evidence = _wire._assessment_evidence(call['canonical'])
        page = evidence['excerpts'][0]
        raw['assessments'] = raw['assessments'][:1]
        raw['assessments'][0]['source_citations'] = [{
            'source_id': 'foreign-source', 'source_sha256': evidence['source_sha256'],
            'start_byte': page['start_byte'], 'end_byte': min(page['start_byte'] + 24, page['end_byte'])}]
        raw['assessments'][0]['requested_source_ranges'] = []
        return raw
    first = install(store, engine, mutate=mixed)
    try:
        with pytest.raises(m.PolicyError):
            await engine._assess_targets(task['id'], 'mixed-count-and-source', targets, context)
        assert [len(call['canonical']['targets']) for call in first.calls if call['schema'] is m.AssessmentBatch] == [3, 3]
        assert store.records('bounded_output_split') == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_legacy_empty_catalog_with_blank_candidate_judgment_does_not_partition(tmp_path, monkeypatch):
    from policy_harness import semantic_wire as wire
    from tests.test_partial_assessment_recovery import legacy_capture
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Hold mixed old judgment', ['A blank candidate judgment is not complete'])
    targets, context = assessment_inputs(4)
    context['learning']['ideas'] = []
    def mixed(call, _):
        if call['schema'] is not m.AssessmentBatch:
            return None
        raw = deepcopy(call['output'])
        raw['assessments'] = raw['assessments'][:1]
        raw['assessments'][0]['ideas'] = [{'candidate_slot': 0, 'consideration': '   '}]
        return raw
    first = install(store, engine, mutate=mixed)
    current_capture = wire.capture
    def old_capture(store, task, role, phase, payload, schema):
        if schema is m.AssessmentBatch:
            return legacy_capture(store, task, role, phase, payload, schema,
                                  revision=wire.REFERENCED_ASSESSMENT_DEFAULTS)
        return current_capture(store, task, role, phase, payload, schema)
    try:
        with monkeypatch.context() as old:
            old.setattr(wire, 'capture', old_capture)
            with pytest.raises(m.PolicyError):
                await engine._assess_targets(task['id'], 'legacy-blank-mixed', targets, context)
        assert [len(call['canonical']['targets']) for call in first.calls if call['schema'] is m.AssessmentBatch] == [4, 4]
        assert store.records('bounded_output_split') == []
    finally:
        await engine.close(); store.close()
