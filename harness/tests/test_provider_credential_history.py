"""Synthetic keys/cipher/HTTP with actual settings, response and Web consumers."""
import base64
import asyncio
from copy import deepcopy
import hashlib
import json
from urllib.parse import quote

import httpx
import pytest
from pydantic import BaseModel, model_validator

from policy_harness.engine import Engine
from policy_harness.knowledge import Knowledge
from policy_harness.models import ConfigurationRequired
from policy_harness.providers import ModelGateway, ProviderError, WebCollector, WebAcquisitionError
from policy_harness.settings import SettingsManager
from policy_harness.store import Store
from tests.test_providers import Answer, FakePolicy, envelope, public_resolver
from tests.test_provider_evidence_recovery import read_all
from tests.test_whole_ui_repairs import CipherFixture
from tests.test_core import FixtureExecutor, FixtureGateway, runtime
from tests.test_web_recovery import operation


OLD = 'synthetic-obsolete-value/history+"quoted"'
CURRENT = 'synthetic-current-model-value'
OLD_WEB = 'synthetic-obsolete-web-value'
CURRENT_WEB = 'synthetic-current-web-value'
AFTER = 'synthetic-new-unreadable-value'


def configured(tmp_path, **changes):
    cipher = CipherFixture()
    settings = SettingsManager(tmp_path / 'settings', protector=cipher)
    settings.update({'base_url': 'https://opencode.ai/zen/go/v1', 'model': 'deepseek-v4.1-flash',
        'model_context_tokens': 100000, 'max_output_tokens': 1000, 'web_provider': 'public_url',
        'model_api_key': OLD, 'web_api_key': OLD_WEB, **changes})
    old_cipher = base64.b64decode(json.loads(settings.path.read_bytes())['secrets']['model_api_key'])
    settings.update({'model_api_key': CURRENT, 'web_api_key': CURRENT_WEB})
    settings = SettingsManager(settings.data_dir, protector=cipher)
    return settings, cipher, old_cipher


def make_unreadable(settings, cipher):
    settings.update({'web_api_key': AFTER})
    raw = json.loads(settings.path.read_bytes())['secrets']['web_api_key']
    cipher.unreadable.add(base64.b64decode(raw))


def model(tmp_path, settings, handler):
    store = Store(tmp_path / 'control')
    task = store.create_task('Retain synthetic provider evidence', ['No request replay'])
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(handler),
                           response_store=store, response_protector=CipherFixture())
    return gateway, store, task


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
@pytest.mark.parametrize('content', [OLD, quote(OLD, safe=''), OLD_WEB])
async def test_history_in_actual_model_input_blocks_before_http(tmp_path, mode, content):
    settings, _, _ = configured(tmp_path, api_mode=mode)
    calls = []
    gateway, store, task = model(tmp_path, settings, lambda r: calls.append(r))
    with pytest.raises(ProviderError, match='credential'):
        await gateway.generate('parent', 'proposal', {'task_id': task['id'], 'text': content}, Answer)
    assert calls == [] and store.records('model_response') == []
    store.close()


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
async def test_normal_rotated_model_route_and_full_policy_are_unchanged(tmp_path, mode):
    settings, _, _ = configured(tmp_path, api_mode=mode)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer ' + CURRENT
        assert json.loads(request.content)['messages'][0]['content'].startswith('Complete effective policy and source provenance:\n' + FakePolicy().prompt())
        return httpx.Response(200, json=envelope())
    gateway, store, task = model(tmp_path, settings, handler)
    value, metadata = await gateway.generate('reviewer', 'review', {'task_id': task['id']}, Answer)
    assert value.summary == 'completed assessment' and len(calls) == 1
    assert metadata['usage']['total_tokens'] == 353 and metadata['finish_reason'] == 'stop'
    assert read_all(gateway, metadata['response_record'], task['id'])[0]
    store.close()


@pytest.mark.parametrize('failure', ['http', 'envelope', 'schema', 'content'])
async def test_historical_response_values_are_excluded_and_never_become_proposals(tmp_path, failure):
    settings, _, _ = configured(tmp_path)
    raw_content = json.dumps({'summary': OLD, 'extra': OLD_WEB})
    if failure == 'content':raw_content = json.dumps({'summary': OLD})
    raw = ('error ' + OLD + ' ' + OLD_WEB).encode() if failure in {'http', 'envelope'} else json.dumps(envelope(raw_content)).encode()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400 if failure == 'http' else 200, content=raw)
    gateway, store, task = model(tmp_path, settings, handler)
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    assert reference['source_sha256'] == hashlib.sha256(raw).hexdigest()
    text, _ = read_all(gateway, reference, task['id'])
    for secret in (OLD, OLD_WEB, CURRENT, CURRENT_WEB):
        assert secret not in text and secret not in json.dumps(caught.value.metadata)
    assert '[REDACTED]' in text and len(calls) == 1
    store.close()


async def test_unreadable_history_holds_messages_diagnostics_and_existing_ranges(tmp_path):
    settings, cipher, old_cipher = configured(tmp_path)
    gateway, store, task = model(tmp_path, settings, lambda r: httpx.Response(400, text='old ' + OLD_WEB))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    cipher.unreadable.add(old_cipher)
    with pytest.raises(ConfigurationRequired, match='history'):
        gateway._messages('parent', 'proposal', {'task_id': task['id']}, Answer)
    assert gateway._diagnostic('unknown original text')['records_withheld'] is True
    with pytest.raises(ConfigurationRequired):
        gateway.read_response(reference, task_id=task['id'], limit=1)
    cipher.unreadable.clear()
    restored = ModelGateway(SettingsManager(settings.data_dir, protector=cipher), FakePolicy(),
                           response_store=store, response_protector=CipherFixture())
    assert OLD_WEB not in read_all(restored, reference, task['id'])[0]
    store.close()


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
@pytest.mark.parametrize('status', [200, 503])
async def test_unreadable_after_model_response_preserves_observation_and_recovers_without_http(tmp_path, mode, status):
    settings, cipher, _ = configured(tmp_path, api_mode=mode)
    raw = json.dumps(envelope(json.dumps({'summary': AFTER}))).encode()
    calls = []
    def handler(request):
        calls.append(request)
        make_unreadable(settings, cipher)
        return httpx.Response(status, content=raw)
    gateway, store, task = model(tmp_path, settings, handler)
    with pytest.raises(ProviderError, match='history') as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    metadata = caught.value.metadata
    assert metadata['http_status'] == status and metadata['request_effect'] == 'response-observed'
    reference = metadata['response_record']
    assert reference['redaction_pending'] and reference['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert AFTER not in json.dumps(metadata) and AFTER not in json.dumps(store.records('model_response'))
    with pytest.raises(ConfigurationRequired):gateway.read_response(reference, task_id=task['id'])
    settings.update({'web_api_key': 'synthetic-recovered-current-web'})
    with pytest.raises(ConfigurationRequired):gateway.read_response(reference, task_id=task['id'])
    cipher.unreadable.clear()
    restored = ModelGateway(SettingsManager(settings.data_dir, protector=cipher), FakePolicy(),
                           response_store=store, response_protector=CipherFixture())
    text, page = read_all(restored, reference, task['id'])
    assert AFTER not in text and '[REDACTED]' in text and page['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert len(calls) == 1 and len(store.records('model_response')) == 1
    store.close()


def web(tmp_path, settings, handler, resolver=public_resolver):
    store = Store(tmp_path / 'web-control')
    collector = WebCollector(settings, transport=httpx.MockTransport(handler), resolver=resolver, response_protector=CipherFixture())
    collector.acquisition_store = store
    return collector, store


@pytest.mark.parametrize('provider', ['public_url', 'brave', 'tavily'])
@pytest.mark.parametrize('query', [OLD_WEB, 'https://example.com/' + quote(OLD, safe=''),
    'https://example.com/docs#' + quote(OLD, safe=''), 'https://example.com/docs#' + CURRENT_WEB])
async def test_web_history_query_or_url_blocks_before_dns_or_http(tmp_path, provider, query):
    settings, _, _ = configured(tmp_path, web_provider=provider)
    calls = []
    async def resolver(host):calls.append(('dns', host)); return ['93.184.216.34']
    collector, store = web(tmp_path, settings, lambda r: calls.append(('http', r)), resolver)
    with pytest.raises(WebAcquisitionError, match='credential'):
        await collector.collect(query, phase='pre', task_id='task', operation_id='operation')
    assert not calls and not store.records('web_exchange')
    store.close()


@pytest.mark.parametrize('origin', ['page', 'redirect', 'search'])
async def test_web_response_history_cannot_become_page_or_followup_url(tmp_path, origin):
    settings, _, _ = configured(tmp_path, web_provider='brave' if origin == 'search' else 'public_url')
    calls, evaluations = [], []
    def handler(request):
        calls.append(request)
        if origin == 'redirect':return httpx.Response(302, headers={'location': '/next/' + quote(OLD, safe='')})
        if origin == 'search':return httpx.Response(200, json={'web': {'results': [{'url': 'https://example.com/' + OLD_WEB}]}})
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='actual page ' + OLD_WEB)
    async def evaluate(stage, record):evaluations.append((stage, record))
    collector, store = web(tmp_path, settings, handler)
    query = 'search term' if origin == 'search' else 'https://example.com/a'
    kwargs = dict(phase='pre', task_id='task', operation_id='operation', evaluate=evaluate)
    with pytest.raises(WebAcquisitionError, match='credential') as caught:await collector.collect(query, **kwargs)
    original = deepcopy(store.records('web_exchange'))
    assert len(calls) == 1 and [s for s, _ in evaluations] == ['before', 'after']
    assert original[0]['protected_exchange'] and 'raw_base64' not in original[0]
    assert OLD_WEB not in json.dumps(original) and OLD_WEB not in json.dumps(caught.value.metadata)
    with pytest.raises(WebAcquisitionError, match='credential'):await collector.collect(query, **kwargs)
    assert len(calls) == 1 and store.records('web_exchange') == original
    store.close()


@pytest.mark.parametrize('provider', ['brave', 'tavily'])
async def test_normal_search_and_redirect_reopen_from_exact_protected_exchanges(tmp_path, provider):
    settings, _, _ = configured(tmp_path, web_provider=provider)
    calls, evaluations = [], []
    def handler(request):
        calls.append(request)
        if 'search' in request.url.path:
            assert request.headers.get('x-subscription-token', request.headers.get('authorization')) in (CURRENT_WEB, 'Bearer ' + CURRENT_WEB)
            rows = [{'url': 'https://example.com/start'}]
            return httpx.Response(200, json={'web': {'results': rows}, 'results': rows, 'usage': {'credits': 1}})
        if request.url.path == '/start':return httpx.Response(302, headers={'location': '/final'})
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='complete authoritative fixture text')
    async def evaluate(stage, record):evaluations.append((stage, record))
    collector, store = web(tmp_path, settings, handler)
    kwargs = dict(phase='pre', task_id='task', operation_id='operation', evaluate=evaluate)
    first = await collector.collect('search phrase', **kwargs)
    original = deepcopy(store.records('web_exchange')); data_dir = store.data_dir; store.close()
    reopened = Store(data_dir)
    collector = WebCollector(SettingsManager(settings.data_dir, protector=settings._protector), transport=httpx.MockTransport(handler), resolver=public_resolver, response_protector=CipherFixture())
    collector.acquisition_store = reopened
    second = await collector.collect('search phrase', **kwargs)
    assert first['sources'] == second['sources'] and len(calls) == 3
    assert [s for s, _ in evaluations] == ['before', 'after'] * 3 + ['after'] * 3
    assert reopened.records('web_exchange') == original
    reopened.close()


@pytest.mark.parametrize('timing', ['before', 'dns'])
async def test_web_unreadable_between_judgment_and_dispatch_stops_http(tmp_path, timing):
    settings, cipher, old_cipher = configured(tmp_path)
    calls = []
    async def resolver(host):
        calls.append('dns')
        if timing == 'dns':cipher.unreadable.add(old_cipher)
        return ['93.184.216.34']
    async def evaluate(stage, record):
        if stage == 'before' and timing == 'before':cipher.unreadable.add(old_cipher)
    collector, store = web(tmp_path, settings, lambda r: calls.append('http'), resolver)
    with pytest.raises(WebAcquisitionError, match='history'):
        await collector.collect('https://example.com/a', phase='pre', task_id='task', operation_id='operation', evaluate=evaluate)
    assert calls == ([] if timing == 'before' else ['dns'])
    if timing == 'dns':assert store.records('web_exchange')[0]['record']['http_dispatched'] is False
    store.close()


async def test_web_unknown_history_recovers_into_real_judgment_and_learning_without_refetch(tmp_path):
    settings, cipher, _ = configured(tmp_path)
    store, engine, _, gateway = runtime(tmp_path / 'runtime')
    task = store.create_task('Acquire one original fixture response', ['Complete original learning'])
    op = operation(); calls = []
    raw = b'original complete public evidence'
    def handler(request):
        calls.append(request)
        make_unreadable(settings, cipher)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=raw)
    collector = WebCollector(settings, transport=httpx.MockTransport(handler), resolver=public_resolver, response_protector=CipherFixture())
    collector.acquisition_store = store
    async def evaluate(stage, record):return await engine._web_acquisition_evaluate(task['id'], op, stage, record)
    kwargs = dict(phase='pre', task_id=task['id'], operation_id=op['id'], evaluate=evaluate)
    with pytest.raises(WebAcquisitionError, match='history') as caught:await collector.collect(op['args']['url'], **kwargs)
    original = deepcopy(store.records('web_exchange'))
    assert caught.value.metadata['acquisitions'][0]['sha256'] == hashlib.sha256(raw).hexdigest()
    assert original[0]['stage'] == 'response' and original[0]['record']['records_withheld']
    assert [x['stage'] for x in store.records('web_work')] == ['before']
    policy, data_dir = engine.policy, store.data_dir
    await engine.close(); store.close()
    cipher.unreadable.clear()
    store = Store(data_dir)
    gateway = FixtureGateway(policy)
    collector = WebCollector(SettingsManager(settings.data_dir, protector=cipher), transport=httpx.MockTransport(handler), resolver=public_resolver, response_protector=CipherFixture())
    engine = Engine(store, policy, FixtureExecutor(), gateway, collector, Knowledge(store))
    result = await collector.collect(op['args']['url'], **kwargs)
    assert len(calls) == 1 and result['sources'][0]['text'] == raw.decode()
    after = next(x for x in store.records('web_work') if x['stage'] == 'after')
    assert after['status'] == 'complete' and after['learning_applied'] and after['learning_attempts']
    assert after['detail']['id'] == original[0]['record']['id']
    assert after['detail']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert any(phase == 'web_acquisition_learning' for _, phase, _ in gateway.calls)
    assert store.records('web_exchange') == original
    await engine.close(); store.close()


@pytest.mark.parametrize('status', [200, 302, 503])
async def test_web_history_loss_during_post_evaluation_retains_original_response(tmp_path, status):
    settings, cipher, old_cipher = configured(tmp_path)
    calls, evaluations = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'content-type': 'text/plain', 'location': '/final'}, content=b'original response')
    async def evaluate(stage, record):
        evaluations.append((stage, record))
        if stage == 'after':cipher.unreadable.add(old_cipher)
    collector, store = web(tmp_path, settings, handler)
    with pytest.raises(WebAcquisitionError, match='history') as caught:
        await collector.collect('https://example.com/a', phase='pre', task_id='task', operation_id='operation', evaluate=evaluate)
    record = caught.value.metadata['acquisitions'][0]
    assert record['http_status'] == status and record['sha256'] == hashlib.sha256(b'original response').hexdigest()
    assert len(calls) == 1 and [s for s, _ in evaluations] == ['before', 'after']
    assert store.records('web_exchange')[0]['stage'] == 'response'
    store.close()


@pytest.mark.parametrize('fault', ['read_error', 'cancelled'])
async def test_partial_web_failure_preserves_protected_bytes_and_never_replays(tmp_path, fault):
    settings, cipher, _ = configured(tmp_path)
    calls = []
    class PartialStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('actual partial ' + AFTER).encode()
            make_unreadable(settings, cipher)
            if fault == 'cancelled':raise asyncio.CancelledError()
            raise httpx.ReadError('synthetic interruption ' + AFTER)
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, stream=PartialStream())
    collector, store = web(tmp_path, settings, handler)
    kwargs = dict(phase='pre', task_id='task', operation_id='operation')
    with pytest.raises(asyncio.CancelledError if fault == 'cancelled' else WebAcquisitionError):
        await collector.collect('https://example.com/a', **kwargs)
    saved = deepcopy(store.records('web_exchange')[0])
    assert saved['stage'] == 'failed' and saved['record']['partial']
    assert AFTER not in json.dumps(saved)
    cipher.unreadable.clear()
    payload = collector._load_exchange(saved)
    assert base64.b64decode(payload['partial_base64']) == ('actual partial ' + AFTER).encode()
    assert payload['record']['status'] == ('unknown' if fault == 'cancelled' else 'failed')
    with pytest.raises(WebAcquisitionError):await collector.collect('https://example.com/a', **kwargs)
    assert len(calls) == 1 and store.records('web_exchange')[0] == saved
    store.close()


async def test_changed_protected_web_record_is_held_without_replay(tmp_path):
    settings, _, _ = configured(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='original page')
    collector, store = web(tmp_path, settings, handler)
    kwargs = dict(phase='pre', task_id='task', operation_id='operation')
    await collector.collect('https://example.com/a', **kwargs)
    saved = store.records('web_exchange')[0]
    saved['protected_exchange']['ciphertext_sha256'] = '0' * 64
    store.record('web_exchange', saved['id'], saved)
    with pytest.raises(WebAcquisitionError, match='validated'):
        await collector.collect('https://example.com/a', **kwargs)
    assert len(calls) == 1
    store.close()


@pytest.mark.parametrize('encoding', ['lowercase-percent', 'double-percent'])
async def test_encoded_history_in_diagnostics_and_retained_ranges_is_excluded(tmp_path, encoding):
    settings, _, _ = configured(tmp_path)
    encoded = quote(OLD, safe='')
    if encoding == 'lowercase-percent':encoded = encoded.replace('%2F', '%2f').replace('%2B', '%2b')
    else:encoded = quote(encoded, safe='')
    raw = ('prefix ' + encoded + ' suffix').encode()
    gateway, store, task = model(tmp_path, settings, lambda r: httpx.Response(400, content=raw))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    metadata = caught.value.metadata
    text, _ = read_all(gateway, metadata['response_record'], task['id'])
    assert encoded not in json.dumps(metadata) and encoded not in text
    assert text == 'prefix [REDACTED] suffix'
    assert metadata['response_sha256'] == hashlib.sha256(raw).hexdigest()
    store.close()


async def test_history_loss_during_schema_validation_withholds_previous_safe_preview(tmp_path):
    settings, cipher, _ = configured(tmp_path)
    class RotatingValidation(BaseModel):
        summary: str
        @model_validator(mode='after')
        def reject_after_settings_change(self):
            make_unreadable(settings, cipher)
            raise ValueError('synthetic invalid schema value')
    raw = json.dumps(envelope(json.dumps({'summary': AFTER}))).encode()
    gateway, store, task = model(tmp_path, settings, lambda r: httpx.Response(200, content=raw))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, RotatingValidation)
    metadata = caught.value.metadata
    assert metadata['response_diagnostic']['records_withheld'] and AFTER not in json.dumps(metadata)
    reference = metadata['response_record']
    with pytest.raises(ConfigurationRequired):gateway.read_response(reference, task_id=task['id'])
    cipher.unreadable.clear()
    assert AFTER not in read_all(gateway, reference, task['id'])[0]
    assert reference['source_sha256'] == hashlib.sha256(raw).hexdigest()
    store.close()
