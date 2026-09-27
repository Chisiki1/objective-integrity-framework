"""Actual SQLite/Knowledge recovery with fixture AI and mock HTTP, never live APIs."""
import base64
from copy import deepcopy
import hashlib

import httpx
import pytest

from policy_harness.engine import Engine
from policy_harness.knowledge import Knowledge
from policy_harness.models import Learning, Operation, PolicyError
from policy_harness.providers import WebCollector, WebAcquisitionError
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, FixtureExecutor, FixtureWeb, runtime


class SimulatedProcessLoss(BaseException):
    pass


class Settings:
    def secret(self, name):
        return None

    def get(self):
        return {'web_provider': 'public_url', 'user_agent': 'Recovery-Fixture',
                'request_timeout_seconds': 5, 'web_max_response_bytes': 10000}


class NoNewLearning(FixtureGateway):
    async def generate(self, role, phase, payload, schema):
        if phase == 'web_acquisition_learning':
            raise AssertionError('A committed or accepted Learning must not be generated again')
        return await super().generate(role, phase, payload, schema)


def operation():
    return Operation(kind='web_fetch', args={'url': 'https://example.com/a'},
        purpose='Acquire declared public evidence', expected_result='Exact response',
        decisions=[{'id': 'source', 'statement': 'Fetch the declared URL', 'rationale': 'Fixture evidence'}]).model_dump()


def detail(identity='acquisition'):
    return {'id': identity, 'url': 'https://example.com/a', 'status': 'succeeded',
            'http_status': 200, 'bytes': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest(),
            'started_at': '2026-09-14T00:00:00+00:00', 'finished_at': '2026-09-14T00:00:01+00:00',
            'elapsed_seconds': 1, 'fixture': True}


def collector(store, requests, status=200):
    async def resolver(host):
        return ['93.184.216.34']

    async def transport(request):
        requests.append(request)
        return httpx.Response(status, headers={'content-type': 'text/plain'}, content=b'exact mock response')

    web = WebCollector(Settings(), transport=httpx.MockTransport(transport), resolver=resolver)
    web.acquisition_store = store
    return web


def reopen(data_dir, policy, *, gateway=None, web=None):
    store = Store(data_dir)
    engine = Engine(store, policy, FixtureExecutor(), gateway or NoNewLearning(policy),
                    web or FixtureWeb(), Knowledge(store))
    return store, engine


@pytest.mark.asyncio
async def test_document_anchors_share_exact_exchange_and_reviewed_reopen(tmp_path):
    store, engine, _, gateway = runtime(tmp_path)
    task = store.create_task('Read both documented sections', ['Keep both original targets'])
    op = operation(); requests = []; web = collector(store, requests)
    targets = ['https://example.com/a#write_text', 'https://example.com/a#read_text']
    query = ' '.join(targets)
    choice = {'query': query, 'private_data_excluded': True, 'rationale': 'Both sections of one document'}
    evaluate = lambda stage, record: engine._web_acquisition_evaluate(task['id'], op, stage, record, choice)
    first = await web.collect(query, phase='pre', task_id=task['id'], operation_id=op['id'], evaluate=evaluate)
    assert len(requests) == 1 and not requests[0].url.fragment
    assert len(first['sources']) == 1
    source = first['sources'][0]
    assert source['requested_url'] == targets[0] and source['url'] == 'https://example.com/a'
    assert first['query'] == query and first['collection_context']['planned_urls'] == targets
    assert first['collection_context']['remaining_urls'] == []
    assert first['collection_context']['completed_targets'] == [
        {'requested_url': url, 'final_url': source['url'], 'source_id': source['id']} for url in targets]
    saved = {kind: deepcopy(store.records(kind)) for kind in ('web_exchange', 'web_work', 'knowledge_application')}
    assert len(saved['knowledge_application']) == 1
    assert all(row['status'] == 'complete' for row in saved['web_work'])
    data_dir, policy = store.data_dir, engine.policy
    await engine.close(); store.close()
    store, engine = reopen(data_dir, policy); web = collector(store, requests)
    evaluate = lambda stage, record: engine._web_acquisition_evaluate(task['id'], op, stage, record, choice)
    second = await web.collect(query, phase='pre', task_id=task['id'], operation_id=op['id'], evaluate=evaluate)
    assert second['sources'] == first['sources']
    assert second['collection_context'] == first['collection_context']
    assert len(requests) == 1 and engine.gateway.calls == []
    assert all(store.records(kind) == rows for kind, rows in saved.items())
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [404, 503])
async def test_cached_failed_response_keeps_failure_body_and_measurement_without_http_replay(tmp_path, status):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available'])
    op = operation(); requests = []; web = collector(store, requests, status)
    evaluate = lambda stage, record: engine._web_acquisition_evaluate(task['id'], op, stage, record)
    kwargs = dict(phase='pre', task_id=task['id'], operation_id=op['id'], evaluate=evaluate)
    with pytest.raises(WebAcquisitionError, match=f'HTTP {status}') as first:
        await web.collect('https://example.com/a', **kwargs)
    original = store.records('web_exchange')[0]
    assert base64.b64decode(original['raw_base64']) == b'exact mock response'
    assert first.value.metadata['sources'] == []
    app = deepcopy(store.records('knowledge_application'))
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    store, engine = reopen(data_dir, policy)
    web = collector(store, requests, status)
    kwargs['evaluate'] = lambda stage, record: engine._web_acquisition_evaluate(task['id'], op, stage, record)
    with pytest.raises(WebAcquisitionError, match=f'HTTP {status}') as resumed:
        await web.collect('https://example.com/a', **kwargs)
    assert len(requests) == 1
    assert resumed.value.metadata['sources'] == []
    assert resumed.value.metadata['acquisitions'][0] == original['record']
    assert store.records('web_exchange')[0] == original
    assert store.records('knowledge_application') == app
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['policy', 'source'])
async def test_completed_acquisition_is_rejudged_after_binding_change_without_new_learning(tmp_path, monkeypatch, change):
    store, engine, _, gateway = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available']); op = operation(); record = detail()
    first = await engine.web_judgments.evaluate(task['id'], op, 'after', record)
    original = store.record_get('web_work', record['id'] + ':after')
    applications = deepcopy(store.records('knowledge_application'))
    calls_before = len(gateway.calls)
    assert await engine.web_judgments.evaluate(task['id'], op, 'after', record) == first
    assert len(gateway.calls) == calls_before
    if change == 'policy':
        # This fixture changes only the in-memory test policy identity, not user policy files.
        monkeypatch.setattr(engine.policy, 'hash', digest({'fixture_policy': 'next'}))
    else:
        store.append_instruction(task['id'], 'Include the new explicit source condition.', task['source_hash'])
    engine.gateway = NoNewLearning(engine.policy)
    def no_apply(*args, **kwargs):
        raise AssertionError('Policy/source rejudgment must not replay Knowledge.apply')
    monkeypatch.setattr(engine.knowledge, 'apply', no_apply)
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', record['id'] + ':after')
    assert current['status'] == 'complete'
    assert current['policy_hash'] == engine.policy.hash
    assert current['source_hash'] == store.get_task(task['id'])['source_hash']
    assert current['acquisition_result'] == original['acquisition_result']
    assert current['learning_applied'] == original['learning_applied']
    assert current['learning_attempts'] == original['learning_attempts']
    assert any(x['outcome'] == first for x in store.records('web_work_history'))
    phases = [phase for _, phase, _ in engine.gateway.calls]
    assert 'web_acquisition_review_after' in phases
    assert any(phase.startswith('web-learning-outcome') and phase.endswith('choices_after') for phase in phases)
    assert store.records('knowledge_application') == applications
    assert any(x['id'].startswith(record['id'] + ':web-acquisition-reassessment:') for x in store.records('judgment_result'))
    await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_commit_before_web_work_save_recovers_exact_episode_without_apply_or_learning(tmp_path, monkeypatch, legacy):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available']); op = operation(); requests = []
    web = collector(store, requests); engine.web = web
    actual_record = store.record
    def crash_before_web_outcome(kind, identity, body):
        if kind == 'web_work' and body.get('learning_applied'):
            raise SimulatedProcessLoss('Actual Knowledge.apply returned; web_work outcome not committed')
        return actual_record(kind, identity, body)
    monkeypatch.setattr(store, 'record', crash_before_web_outcome)
    with pytest.raises(SimulatedProcessLoss):
        await engine._research(task['id'], op, 'pre', {})
    monkeypatch.setattr(store, 'record', actual_record)
    saved = next(x for x in store.records('web_work') if x['stage'] == 'after')
    app = deepcopy(store.records('knowledge_application')[0])
    episode = deepcopy(store.record_get('episode', app['outcome']['episode_id']))
    assert 'learning_applied' not in saved
    assert saved['application_intent']['input_hash'] == app['input_hash']
    assert saved['learning_attempts'][-1]['disposition']['verdict'] == 'proceed'
    assert saved['acquisition_result'] == episode['result']
    assert saved['acquisition_result']['started_at'] == saved['detail']['started_at']
    if legacy:
        # Existing incomplete web_work records used to lack all durable input fields.
        for name in ('application_intent', 'acquisition_operation', 'acquisition_result', 'source_hash'):
            saved.pop(name, None)
        saved['learning_attempts'][0]['legacy_event_refs'] = [{'seq': 7, 'hash': 'fixture-original-event'}]
        actual_record('web_work', saved['id'], saved)
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    store, engine = reopen(data_dir, policy)
    def no_apply(*args, **kwargs):
        raise AssertionError('The committed application must be recovered from the original episode')
    monkeypatch.setattr(engine.knowledge, 'apply', no_apply)
    await engine.web_judgments.drain(task['id'])
    restored = store.record_get('web_work', saved['id'])
    assert restored['status'] == 'complete'
    assert restored['acquisition_operation'] == episode['operation']
    assert restored['acquisition_result'] == episode['result']
    assert restored['applied_learning'] == episode['learning']
    assert restored['learning_applied'] == app['outcome']
    assert restored['learning_attempts'] == saved['learning_attempts']
    assert store.record_get('episode', episode['id']) == episode
    assert store.records('knowledge_application') == [app]
    assert len(requests) == 1
    assert restored['application_recovery']['application_replayed'] is False
    recovered = [x for x in store.events(task['id']) if x['stage']=='web_knowledge_application' and
                 x['detail'].get('application_id')==app['id']]
    assert len(recovered)==1 and recovered[0]['detail']['outcome']==app['outcome']
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_accepted_learning_before_apply_is_resumed_with_exact_durable_input(tmp_path, monkeypatch):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available']); op = operation(); record = detail()
    def crash_before_apply(*args, **kwargs):
        raise SimulatedProcessLoss('Accepted input persisted; application has not started')
    monkeypatch.setattr(engine.knowledge, 'apply', crash_before_apply)
    with pytest.raises(SimulatedProcessLoss):
        await engine.web_judgments.evaluate(task['id'], op, 'after', record)
    saved = store.record_get('web_work', record['id'] + ':after')
    assert store.records('knowledge_application') == []
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()
    store, engine = reopen(data_dir, policy)
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', saved['id'])
    app = store.records('knowledge_application')[0]
    assert current['status'] == 'complete'
    assert app['input_hash'] == saved['application_intent']['input_hash']
    assert current['learning_attempts'] == saved['learning_attempts']
    assert current['acquisition_result'] == saved['acquisition_result']
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_interrupted_learning_review_resumes_saved_proposal_without_new_learning(tmp_path):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available']); op = operation(); record = detail()
    class InterruptedReview(FixtureGateway):
        async def generate(self, role, phase, payload, schema):
            if phase=='web_learning_review':raise SimulatedProcessLoss('Learning and its assessments are durable; reviewer disconnected')
            return await super().generate(role,phase,payload,schema)
    engine.gateway=InterruptedReview(engine.policy)
    with pytest.raises(SimulatedProcessLoss):
        await engine.web_judgments.evaluate(task['id'],op,'after',record)
    saved=store.record_get('web_work',record['id']+':after')
    assert len(saved['learning_attempts'])==1 and 'review' not in saved['learning_attempts'][0]
    draft=deepcopy(saved['learning_attempts'][0])
    data_dir=store.data_dir;policy=engine.policy
    await engine.close();store.close()
    store,engine=reopen(data_dir,policy)
    await engine.web_judgments.drain(task['id'])
    current=store.record_get('web_work',saved['id'])
    assert current['status']=='complete' and len(current['learning_attempts'])==1
    assert current['learning_attempts'][0]['learning']==draft['learning']
    assert current['learning_attempts'][0]['assessments']==draft['assessments']
    assert len(store.records('knowledge_application'))==1
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_source_change_before_uncommitted_application_requires_current_learning_review(tmp_path,monkeypatch):
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Acquire evidence',['source is available']);op=operation();record=detail()
    real_apply=engine.knowledge.apply
    def interrupted(*args,**kwargs):raise SimulatedProcessLoss('Accepted under the old source; not committed')
    monkeypatch.setattr(engine.knowledge,'apply',interrupted)
    with pytest.raises(SimulatedProcessLoss):
        await engine.web_judgments.evaluate(task['id'],op,'after',record)
    previous=store.record_get('web_work',record['id']+':after')
    store.append_instruction(task['id'],'Use the newly explicit condition.',task['source_hash'])
    engine.gateway=FixtureGateway(engine.policy)
    monkeypatch.setattr(engine.knowledge,'apply',real_apply)
    await engine.web_judgments.drain(task['id'])
    current=store.record_get('web_work',previous['id'])
    assert current['status']=='complete' and len(current['learning_attempts'])==2
    assert current['learning_attempts'][0]==previous['learning_attempts'][0]
    assert current['learning_attempts'][1]['binding']['source_hash']==store.get_task(task['id'])['source_hash']
    assert any(phase=='web_acquisition_learning' for _,phase,_ in engine.gateway.calls)
    assert len(store.records('knowledge_application'))==1
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_committed_input_mismatch_is_not_relaxed_or_reapplied(tmp_path, monkeypatch):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Acquire evidence', ['source is available']); op = operation(); record = detail()
    await engine.web_judgments.evaluate(task['id'], op, 'after', record)
    app = store.records('knowledge_application')[0]
    app['input_hash'] = 'f' * 64
    store.record('knowledge_application', app['id'], app)
    with pytest.raises(PolicyError, match='input hash'):
        await engine.web_judgments.evaluate(task['id'], op, 'after', record)
    assert store.records('knowledge_application') == [app]
    await engine.close(); store.close()


@pytest.mark.asyncio
async def test_started_exchange_remains_unknown_after_reconstruction_without_replay(tmp_path):
    store = Store(tmp_path); task = store.create_task('Acquire evidence', ['source is available']); requests = []
    args = dict(phase='pre', task_id=task['id'], operation_id='operation')
    key = digest({**args, 'method':'GET', 'url':'https://example.com/a', 'query':None, 'body':None})
    record = {'id':'unknown-acquisition', **args, 'url':'https://example.com/a', 'status':'started', 'network_dispatched':True}
    store.record('web_exchange', key, {'id':key, 'stage':'started', 'record':record})
    observed = []
    async def evaluate(stage, value):
        observed.append((stage, deepcopy(value)))
    with pytest.raises(WebAcquisitionError) as resumed:
        await collector(store, requests).collect('https://example.com/a', evaluate=evaluate, **args)
    assert requests == []
    assert observed[0][0] == 'after' and observed[0][1]['status'] == 'unknown'
    assert resumed.value.metadata['acquisitions'][0]['status'] == 'unknown'
    assert store.record_get('web_exchange', key)['record']['status'] == 'unknown'
    store.close()


@pytest.mark.asyncio
async def test_saved_analysis_precedes_after_judgment_and_resumes_without_http_or_reparse(tmp_path, monkeypatch):
    store = Store(tmp_path); task = store.create_task('Read exact evidence', ['Review the extracted result'])
    requests = []; web = collector(store, requests); observed = []
    analyze = web._analyze_response
    def after_durable_response(record, raw, headers):
        saved = store.record_get('web_exchange', record['exchange_id'])
        assert saved['stage'] == 'response' and base64.b64decode(saved['raw_base64']) == raw
        assert 'response_analysis' not in saved['record']
        return analyze(record, raw, headers)
    monkeypatch.setattr(web, '_analyze_response', after_durable_response)
    async def interrupted(stage, record):
        if stage == 'after':
            observed.append(deepcopy(record))
            assert record['response_analysis']['source']['text'] == 'exact mock response'
            assert record['extraction_status'] == 'succeeded'
            assert record['response_analysis']['content_adopted'] is False
            assert record['body_accepted_as_source'] is False
            assert record['collection_context']['completed_targets'] == []
            raise SimulatedProcessLoss('After-judgment interrupted; source was not handed off')
    args = dict(phase='pre', task_id=task['id'], operation_id='parent')
    try:
        with pytest.raises(SimulatedProcessLoss):
            await web.collect('https://example.com/a', evaluate=interrupted, **args)
        retained = deepcopy(store.records('web_exchange'))
        def no_reparse(*args): raise AssertionError('The actual saved extraction must be reused')
        monkeypatch.setattr(web, '_analyze_response', no_reparse)
        async def accepted(stage, record):
            assert stage == 'after' and record == observed[0]
        returned = await web.collect('https://example.com/a', evaluate=accepted, **args)
        assert len(requests) == 1 and store.records('web_exchange') == retained
        source, = returned['sources']
        assert source['text'] == 'exact mock response'
        assert source['response_sha256'] == observed[0]['sha256']
        assert returned['collection_context']['completed_targets'][0]['source_id'] == source['id']
        assert observed[0]['collection_context']['completed_targets'] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_redirect_review_and_next_before_gate_precede_destination_communication(tmp_path):
    store = Store(tmp_path); task = store.create_task('Read redirected evidence', ['Review both exchanges'])
    events = []; release = False
    async def resolver(host): return ['93.184.216.34']
    async def transport(request):
        events.append(('http', request.url.path))
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/final?session=private#section',
                'content-type': 'text/plain', 'set-cookie': 'private-cookie'}, content=b'redirect')
        assert release
        return httpx.Response(200, headers={'content-type': 'text/html'}, content=b'<title>Source</title><p>Observed page</p>')
    web = WebCollector(Settings(), transport=httpx.MockTransport(transport), resolver=resolver)
    web.acquisition_store = store
    async def evaluate(stage, record):
        path = httpx.URL(record['url']).path
        events.append((stage, path))
        if stage == 'after' and path == '/start':
            analysis = record['response_analysis']
            assert analysis['state'] == 'redirect_pending' and 'source' not in analysis
            assert analysis['response_representation'] == {'content_type': 'text/plain', 'location': '/final',
                'location_private_components_omitted': True}
            assert 'private-cookie' not in str(record) and 'session=private' not in str(record)
            if not release: raise PolicyError('Independent judgment holds this redirect')
        elif stage == 'after':
            assert record['response_analysis']['source']['text'].endswith('Observed page')
            assert record['response_analysis']['content_adopted'] is False
    args = dict(phase='pre', task_id=task['id'], operation_id='parent', evaluate=evaluate)
    try:
        with pytest.raises(PolicyError, match='holds this redirect'):
            await web.collect('https://example.com/start', **args)
        assert events == [('before', '/start'), ('http', '/start'), ('after', '/start')]
        release = True
        returned = await web.collect('https://example.com/start', **args)
        assert events[3:] == [('after', '/start'), ('before', '/final'), ('http', '/final'), ('after', '/final')]
        assert returned['sources'][0]['url'] == 'https://example.com/final?session=private'
        assert len(store.records('web_exchange')) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_extraction_is_reviewed_as_failure_and_never_delivered(tmp_path):
    store = Store(tmp_path); task = store.create_task('Read valid text', ['No silent decode replacement'])
    observed = []; requests = []
    async def resolver(host): return ['93.184.216.34']
    async def transport(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'\xff')
    web = WebCollector(Settings(), transport=httpx.MockTransport(transport), resolver=resolver); web.acquisition_store = store
    async def evaluate(stage, record):
        if stage == 'after': observed.append(deepcopy(record))
    try:
        for _ in range(2):
            with pytest.raises(WebAcquisitionError, match='encoding needs explicit handling') as failed:
                await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id='parent', evaluate=evaluate)
            assert failed.value.metadata['sources'] == []
        assert len(requests) == 1 and observed[0] == observed[1]
        assert observed[0]['status'] == 'succeeded'  # HTTP result, not extraction.
        assert observed[0]['response_analysis']['state'] == 'failed'
        assert observed[0]['response_analysis']['content_adopted'] is False
        assert 'source' not in observed[0]['response_analysis']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_response_keeps_exact_pre_extraction_callback(tmp_path):
    store = Store(tmp_path); task = store.create_task('Resume old saved evidence', ['Preserve historical meaning'])
    requests = []; web = collector(store, requests); raw = b'legacy exact text'
    args = dict(phase='pre', task_id=task['id'], operation_id='parent')
    key = digest({**args, 'method': 'GET', 'url': 'https://example.com/a', 'query': None, 'body': None})
    record = {**detail('legacy'), **args, 'exchange_id': key, 'method': 'GET', 'kind': 'fetch',
        'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'extraction_status': 'not_yet_performed',
        'body_accepted_as_source': False}
    saved = {'id': key, 'stage': 'response', 'record': record, 'raw_base64': base64.b64encode(raw).decode(),
        'headers': {'content-type': 'text/plain'}}
    store.record('web_exchange', key, saved)
    async def evaluate(stage, value):
        assert stage == 'after' and value == record
        assert 'response_analysis' not in value
    try:
        result = await web.collect('https://example.com/a', evaluate=evaluate, **args)
        assert requests == [] and result['sources'][0]['text'] == raw.decode()
        assert store.record_get('web_exchange', key) == saved
    finally:
        store.close()


@pytest.mark.parametrize('provider,body', [('brave', b'{"web":{"results":[{"url":"https://example.com/a"}]}}'),
    ('tavily', b'{"results":[{"url":"https://example.com/a"}]}')])
def test_search_analysis_retains_observed_urls_without_destination_acquisition(provider, body):
    record = {**detail(), 'kind': 'search', 'web_provider': provider, 'sha256': hashlib.sha256(body).hexdigest()}
    analysis = WebCollector._analyze_response(record, body, {'content-type': 'application/json'})
    assert analysis['state'] == 'succeeded' and analysis['urls'] == ['https://example.com/a']
    assert analysis['kind'] == 'search_results' and 'source' not in analysis
    assert analysis['content_adopted'] is False and analysis['response_sha256'] == record['sha256']
