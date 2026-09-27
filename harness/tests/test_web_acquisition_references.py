"""Acquisition consumers with real wire/SQLite paths and explicit fixture judgments."""
from copy import deepcopy
import json

import httpx
import pytest

from policy_harness import models as m
from policy_harness.providers import ProviderError
from tests.test_core import runtime
from tests.test_learning_update_contract import reopen
from tests.test_semantic_wire_contract import install, assert_wire_records, source_choice
from tests.test_web_recovery import collector, operation


@pytest.mark.asyncio
@pytest.mark.parametrize('paged', [False, True])
async def test_acquisition_reuses_authenticated_candidates_and_immutable_source_slots(tmp_path, monkeypatch, paged):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Read the declared source', ['Keep the acquired bytes and original judgments'])
    parent = operation(); requests = []; web = collector(store, requests)
    counts = {'review': 0, 'correction': 0}
    if paged:
        fits = engine.bounded_judgments._fits
        def fit(task_id, phase, payload, schema, role=None):
            if schema is m.AssessmentBatch and phase.startswith('web_acquisition_after') and len(payload['targets']) > 1:
                return False
            if schema is m.Review and phase == 'web_acquisition_review_after' and 'source_records' not in payload:
                return False
            if schema is m.Disposition and phase == 'acquisition-correction-test' and 'bounded_context' not in payload:
                return False
            return fits(task_id, phase, payload, schema, role)
        monkeypatch.setattr(engine.bounded_judgments, '_fits', fit)

    def choose(call, _):
        body = deepcopy(call['output'])
        if call['schema'] is m.AssessmentBatch and call['phase'].startswith('web_acquisition_after'):
            assert call['capture']['association']['known_ideas']
            for row in body['assessments']:
                row['ideas'] = [{'candidate_slot': 0,
                    'consideration': 'The exact before candidate still applies to these measured bytes; no new or changed action is justified.'}]
        elif call['phase'] == 'web_acquisition_review_after':
            counts['review'] += 1
            body['opinions'][0]['evidence_refs'] = ([{'source_slot': len(call['capture']['association']['source_slots'])}]
                if counts['review'] == 1 else [source_choice(call, '/research_choice/rationale')])
        elif call['phase'] == 'acquisition-correction-test':
            counts['correction'] += 1
            body.update(verdict='revise', revision_scope='learning' if counts['correction'] == 1 else 'governed',
                rationale='A changed research choice requires its governed route; immutable acquisition facts cannot be rewritten.',
                revision_targets=[source_choice(call, '/research_choice/rationale')])
        return body

    transport = install(store, engine, mutate=choose)
    choice = {'query': 'https://example.com/a', 'private_data_excluded': True,
              'rationale': 'Read the one declared evidence source'}
    try:
        await web.collect(choice['query'], phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail, choice))
        before = next(row for row in store.records('web_work') if row['stage'] == 'before')
        after = next(row for row in store.records('web_work') if row['stage'] == 'after')
        assert before['context_contract_version'] == after['context_contract_version'] == 'web-identity-v4'
        originals = [idea for row in before['assessments'] for idea in row['assessment']['ideas']]
        meanings = [{k: v for k, v in idea.items() if k != 'id'} for idea in originals]
        assert len({idea['id'] for idea in originals}) == 3
        assert all(meaning == meanings[0] for meaning in meanings)
        assert all(row['assessment']['ideas'] == originals for row in after['assessments'])
        assert all('measured bytes' in row['assessment']['rationale'] for row in after['assessments'])
        evidence = json.loads(after['review']['opinions'][0]['evidence_refs'][0])
        assert evidence['pointer'] == '/research_choice/rationale' and evidence['editable'] is False
        assert len(requests) == len(store.records('knowledge_application')) == 1
        assert all(row['status'] == 'complete' for row in (before, after))
        acquisitions = [c for c in transport.calls if c['phase'].startswith('web_acquisition') and c['schema'] in {m.Review, m.Disposition}]
        assert acquisitions and all(not row['editable'] for call in acquisitions for row in call['capture']['association']['source_slots'])

        source = {**after['review_input'], 'review': after['review'], 'sources': []}
        disposition = await engine.bounded_judgments.disposition_proposal(task['id'], after['acquisition_operation'],
            'acquisition-correction-test', source)
        route = engine.bounded_judgments.disposition_route(task['id'], 'acquisition-correction-test', source, disposition.model_dump())
        assert route['scope'] == 'governed' and disposition.verdict == 'revise'
        assert counts == {'review': 2, 'correction': 2}
        assert len([e for e in store.events(task['id']) if e['status'] == 'rejected_model_output']) == 2
        assert len(requests) == 1 and executor.calls == []
        assert_wire_records(store, transport.calls)

        # A tampered earlier value is not an authenticated reusable candidate.
        altered = deepcopy(before)
        altered['assessments'][0]['assessment']['ideas'][0]['proposal'] = 'An invented different decision'
        store.record('web_work', before['id'], altered)
        with pytest.raises(m.PolicyError, match='authenticated original'):
            engine.web_judgments._context(after)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [None, 'web-identity-v2'])
async def test_old_acquisition_context_resumes_failed_disposition_without_success_replay(tmp_path, monkeypatch, version):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume original acquired evidence', ['Do not repeat success or HTTP effects'])
    parent = operation(); requests = []; web = collector(store, requests)
    save = engine.web_judgments._save
    def old_writer(saved):
        if 'assessments' not in saved and saved.get('context_contract_version') == 'web-identity-v4':
            if version is None: saved.pop('context_contract_version')
            else: saved['context_contract_version'] = version
        save(saved)
    monkeypatch.setattr(engine.web_judgments, '_save', old_writer)
    def interrupt(call, _):
        if call['phase'] == 'web_learning_disposition':
            return httpx.Response(503, json={'error': 'Synthetic observed service failure at the retained disposition'})
    first = install(store, engine, mutate=interrupt)
    with pytest.raises(ProviderError):
        await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    saved = next(row for row in store.records('web_work') if row['stage'] == 'after')
    failed_input = next(c['canonical'] for c in first.calls if c['phase'] == 'web_learning_disposition')
    successes = deepcopy([r for r in store.records('bounded_model_call') if r['status'] == 'succeeded'])
    exchanges = deepcopy(store.records('web_exchange'))
    assert len(requests) == 1 and saved.get('context_contract_version') == version
    assert all('learning_projection' not in c['canonical'] for c in first.calls
               if c['phase'].startswith(('web_acquisition_before', 'web_acquisition_after', 'web_acquisition_review', 'web_acquisition_disposition')))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        await engine.web_judgments.drain(task['id'])
        current = store.record_get('web_work', saved['id'])
        assert current['status'] == 'complete' and current.get('context_contract_version') == version
        assert next(c['canonical'] for c in second.calls if c['phase'] == 'web_learning_disposition') == failed_input
        assert not any(c['phase'] in {'web_acquisition_before', 'web_acquisition_after', 'web_acquisition_learning',
                                     'web_learning_choices_before', 'web_learning_review'} for c in second.calls)
        for original in successes: assert store.record_get('bounded_model_call', original['id']) == original
        assert store.records('web_exchange') == exchanges and len(requests) == 1
        assert len(store.records('knowledge_application')) == 1 and executor.calls == []
        assert_wire_records(store, first.calls + second.calls)
    finally:
        await engine.close(); store.close()
