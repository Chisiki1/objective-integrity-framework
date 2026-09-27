"""Real Store/settings consumers with synthetic HTTP; never paid API evidence."""
import base64
import hashlib
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from policy_harness.engine import Engine
from policy_harness.models import ConfigurationRequired, Disposition, Review
from policy_harness.providers import ModelGateway, ProviderError, WebCollector, WebAcquisitionError
from policy_harness.settings import SettingsManager
from policy_harness.store import Store
from .test_providers import Answer, FakePolicy, FakeSettings, envelope, public_resolver


class FixtureCipher:
    """Explicit reversible fixture, not a security or DPAPI claim."""
    def protect(self, value):
        return b'fixture-only:' + bytes(byte ^ 0xA5 for byte in value)

    def unprotect(self, value):
        assert value.startswith(b'fixture-only:')
        return bytes(byte ^ 0xA5 for byte in value[len(b'fixture-only:'):])


def configured(tmp_path, **changes):
    settings = SettingsManager(tmp_path / 'settings', protector=FixtureCipher())
    settings.update({'base_url': 'https://opencode.ai/zen/go/v1', 'model': 'deepseek-v4.1-flash',
                     'model_context_tokens': 100000, 'max_output_tokens': 1000,
                     'model_api_key': 'fixture-old-opaque-value',
                     'web_api_key': 'fixture-old-web-value', **changes})
    store = Store(tmp_path / 'control')
    task = store.create_task('Retain the actual response evidence', ['Read it without another request'])
    return settings, store, task


def read_all(gateway, reference, task_id):
    result, offset, expected = [], 0, None
    while True:
        page = gateway.read_response(reference, task_id=task_id, offset=offset,
                                     limit=4096, expected_view_hash=expected)
        result.append(page['text'])
        assert page['next_offset'] > offset or page['complete']
        if page['complete']:
            return ''.join(result), page
        offset, expected = page['next_offset'], page['view_sha256']


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
@pytest.mark.parametrize('failure', ['http', 'envelope', 'schema'])
async def test_rotation_uses_outgoing_and_current_keys_in_diagnostics_and_store(tmp_path, mode, failure):
    settings, store, task = configured(tmp_path, api_mode=mode)
    old = [settings.secret('model_api_key'), settings.secret('web_api_key')]
    newer = ['fixture-new-opaque-value', 'fixture-new-web-value']
    calls = []
    def transport(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer ' + old[0]
        settings.update(dict(zip(('model_api_key', 'web_api_key'), newer)))
        reflected = ' / '.join([*old, *newer])
        if failure == 'http':
            return httpx.Response(400, text='original error: ' + reflected)
        if failure == 'envelope':
            return httpx.Response(200, text='malformed envelope: ' + reflected)
        value = envelope(json.dumps({'summary': 12, 'extra': reflected}))
        value['id'] = old[0]
        return httpx.Response(200, json=value)
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(transport),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('reviewer', 'review', {'task_id': task['id']}, Answer)
    assert len(calls) == 1
    metadata = caught.value.metadata
    for secret in [*old, *newer]:
        assert secret not in json.dumps(metadata, ensure_ascii=False)
    reference = metadata['response_record']
    assert metadata['response_diagnostic']['source_ref'] == reference
    assert reference['source_hash'] == task['source_hash']
    assert reference['phase'] == 'review'
    assert reference['confidential_exclusions_applied'] is True
    if failure == 'schema':
        assert metadata['validation_diagnostic']['sanitized_response']
    store.event(task['id'], 'review', 'error', metadata)
    store.close()
    reopened = Store(tmp_path / 'control')
    restored = ModelGateway(settings, FakePolicy(), response_store=reopened, response_protector=FixtureCipher())
    text, _ = read_all(restored, reference, task['id'])
    event_text = json.dumps(reopened.events(task['id']), ensure_ascii=False)
    raw_record = reopened.record_get('model_response', reference['id'])
    retained = FixtureCipher().unprotect(base64.b64decode(raw_record['ciphertext_base64'])).decode()
    for secret in [*old, *newer]:
        assert secret not in text and secret not in event_text and secret not in retained
    assert len(reopened.records('model_response')) == 1
    reopened.close()


@pytest.mark.parametrize('failure', ['http', 'envelope', 'schema'])
async def test_complete_middle_evidence_reopens_by_bound_range_without_replay(tmp_path, failure):
    settings, store, task = configured(tmp_path)
    middle = 'EXACT_MIDDLE_EVIDENCE_MUST_SURVIVE'
    evidence = 'a' * 45000 + middle + 'z' * 45000
    if failure == 'http':
        raw = evidence.encode()
        status = 503
    elif failure == 'envelope':
        # Duplicate fields are themselves fault evidence; serialization must not erase them.
        raw = ('{"duplicate":1,"duplicate":2,"evidence":' + json.dumps(evidence) + '}').encode()
        status = 200
    else:
        raw = json.dumps(envelope(json.dumps({'summary': 12, 'extra': evidence}))).encode()
        status = 200
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(status, content=raw, headers={'content-type': 'application/json; charset=utf-8'})
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(transport),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'assessment', {'task_id': task['id']}, Answer)
    meta = caught.value.metadata
    assert middle not in meta['response_diagnostic']['sanitized_response']
    assert meta['response_diagnostic']['diagnostic_truncated'] is True
    reference = meta['response_record']
    assert reference['observed_bytes'] == len(raw)
    assert reference['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert reference['truncated'] is False
    assert reference['confidential_exclusions_applied'] is False
    store.event(task['id'], 'assessment', 'error', meta)
    store.close()
    reopened = Store(tmp_path / 'control')
    gateway = ModelGateway(settings, FakePolicy(), response_store=reopened, response_protector=FixtureCipher())
    text, page = read_all(gateway, reference, task['id'])
    assert text.encode() == raw
    assert middle in text
    assert page['retained_sha256'] == hashlib.sha256(raw).hexdigest()
    assert len(calls) == 1
    if failure == 'envelope':
        assert text.count('"duplicate"') == 2
    with pytest.raises(ProviderError, match='different task'):
        gateway.read_response(reference, task_id='foreign')
    altered = {**reference, 'record_sha256': '0' * 64}
    with pytest.raises(ProviderError, match='record changed'):
        gateway.read_response(altered, task_id=task['id'])
    with pytest.raises(ProviderError, match='source binding'):
        gateway.read_response({**reference, 'source_sha256': '0' * 64}, task_id=task['id'])
    reopened.close()


async def test_excludes_private_reasoning_and_escaped_credentials_without_saving_them(tmp_path):
    settings, store, task = configured(tmp_path)
    key = settings.secret('model_api_key')
    escaped = ''.join('\\u%04x' % ord(char) for char in key)
    raw = ('{"reasoning_content":"PRIVATE_THOUGHT_FIXTURE",'
           '"\\u0074hinking_content":"PRIVATE_SECOND_FIXTURE",'
           '"reflected":"' + escaped + '","evidence":"retained"}').encode()
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw)),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    record = store.record_get('model_response', reference['id'])
    saved = FixtureCipher().unprotect(base64.b64decode(record['ciphertext_base64'])).decode()
    for excluded in [key, escaped, 'PRIVATE_THOUGHT_FIXTURE', 'PRIVATE_SECOND_FIXTURE']:
        assert excluded not in saved
    assert 'retained' in saved
    assert reference['source_sha256'] != reference['retained_sha256']
    text, _ = read_all(gateway, reference, task['id'])
    assert text == saved
    store.close()


async def test_success_retains_evidence_and_reflected_secret_cannot_become_proposal(tmp_path):
    settings, store, task = configured(tmp_path)
    responses = [envelope(), envelope(json.dumps({'summary': settings.secret('model_api_key')}))]
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=responses.pop(0))),
                           response_store=store, response_protector=FixtureCipher())
    result, metadata = await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    assert result.summary == 'completed assessment'
    assert metadata['response_record']['status'] == 'retained'
    assert 'response_diagnostic' not in metadata
    with pytest.raises(ProviderError, match='credential content'):
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    assert len(store.records('model_response')) == 2
    store.close()


async def test_later_configured_key_is_scrubbed_before_range_splitting(tmp_path):
    settings, store, task = configured(tmp_path)
    later_key = 'previously-public-later-credential'
    raw = ('prefix ' + later_key + ' suffix').encode()
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(400, content=raw)),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    original = gateway.read_response(reference, task_id=task['id'], limit=10)
    settings.update({'model_api_key': later_key})
    with pytest.raises(ProviderError, match='display changed'):
        gateway.read_response(reference, task_id=task['id'], offset=10, expected_view_hash=original['view_sha256'])
    text, _ = read_all(gateway, reference, task['id'])
    assert later_key not in text and '[REDACTED]' in text
    store.close()


async def test_invalid_http_bytes_remain_retained_with_explicit_safe_display(tmp_path):
    settings, store, task = configured(tmp_path)
    raw = b'actual invalid byte: \xff tail'
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(400, content=raw)),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    assert reference['retained_sha256'] == hashlib.sha256(raw).hexdigest()
    text, page = read_all(gateway, reference, task['id'])
    assert r'\xff' in text and 'explicit backslash escapes' in page['decoding']
    store.close()


async def test_transport_unknown_and_retention_fault_are_not_replayed(tmp_path, monkeypatch):
    settings, store, task = configured(tmp_path)
    calls = []
    def unknown(request):
        calls.append(request)
        raise httpx.ReadError('transport fixture')
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(unknown),
                           response_store=store, response_protector=FixtureCipher())
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    assert caught.value.metadata['request_effect'] == 'response-unobserved'
    assert len(calls) == 1 and not store.records('model_response')
    def observed(request):
        calls.append(request)
        return httpx.Response(503, text='actual observed error')
    gateway._transport = httpx.MockTransport(observed)
    def fail_record(*args):
        raise OSError('injected write fault; not replayable')
    monkeypatch.setattr(store, 'record', fail_record)
    with pytest.raises(ProviderError, match='retention failed') as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    assert caught.value.metadata['evidence_state'] == 'response-observed-retention-failed'
    assert caught.value.metadata['observed_response_bytes'] == len(b'actual observed error')
    assert len(calls) == 2
    store.close()


async def test_unbound_nonfixture_transport_cannot_dispatch():
    class NeverTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            pytest.fail('No real transport may send before durable evidence is bound')
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=NeverTransport())
    with pytest.raises(ConfigurationRequired, match='response Store'):
        await gateway.generate('parent', 'proposal', {'task_id': 'task'}, Answer)


async def test_diagnostic_failure_keeps_already_retained_source(tmp_path, monkeypatch):
    settings, store, task = configured(tmp_path)
    raw = b'actual response before a diagnostic projection failure'
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(400, content=raw)),
                           response_store=store, response_protector=FixtureCipher())
    def fail_display(*args, **kwargs):
        raise RuntimeError('injected display failure')
    monkeypatch.setattr(gateway, '_diagnostic', fail_display)
    with pytest.raises(ProviderError, match='response processing failed') as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    restored = ModelGateway(settings, FakePolicy(), response_store=store, response_protector=FixtureCipher())
    assert read_all(restored, reference, task['id'])[0].encode() == raw
    store.close()


@pytest.mark.skipif(os.name != 'nt', reason='Actual DPAPI requires Windows; other evidence tests use an explicit cipher fixture.')
async def test_native_dpapi_protected_response_reopens(tmp_path):
    settings, store, task = configured(tmp_path)
    marker = 'PRIVATE_EVIDENCE_ENCRYPTION_ROUNDTRIP'
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(400, text=marker)),
                           response_store=store)
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': task['id']}, Answer)
    reference = caught.value.metadata['response_record']
    assert reference['protection'] == 'windows-dpapi-current-user'
    assert marker not in json.dumps(store.records('model_response'))
    store.close()
    reopened = Store(tmp_path / 'control')
    restored = ModelGateway(settings, FakePolicy(), response_store=reopened)
    assert read_all(restored, reference, task['id'])[0] == marker
    reopened.close()


async def test_redirect_target_identity_survives_shared_destination_and_store_reopen(tmp_path):
    from tests.test_core import runtime
    settings = FakeSettings()
    store, host, _, _ = runtime(tmp_path)
    task = store.create_task('Collect all declared targets', ['Account for every actual source in parent disposition'])
    calls, before = [], []
    async def evaluate(stage, record):
        if stage == 'before':
            before.append(record)
    def transport(request):
        calls.append((request.headers['host'], request.url.path))
        if request.url.path in ['/start-a', '/start-b']:
            return httpx.Response(302, headers={'location': 'https://first.example/landing'})
        return httpx.Response(200, text='Actual source ' + request.url.path, headers={'content-type': 'text/plain'})
    query = 'https://first.example/start-a https://first.example/start-b https://second.example/end'
    web = WebCollector(settings, transport=httpx.MockTransport(transport), resolver=public_resolver)
    web.acquisition_store = store
    result = await web.collect(query, phase='before', task_id=task['id'], operation_id='op', evaluate=evaluate)
    # Exercise the real Engine input contract with its installed dependencies.
    review = Review(summary='Explicit test review', opinions=[])
    model_input = Engine._model_input(host, task['id'], 'web-parent-disposition',
                                     {'review': review.model_dump(), 'web': result}, Disposition)
    disposition = Disposition(verdict='proceed', rationale='Consume the supplied exact references', opinion_responses=[],
                              web_refs=model_input['exact_response_contract']['web_refs'])
    Engine._validate_disposition(review, disposition, result['sources'])
    second_start = next(row for row in before if row['url'].endswith('/start-b'))
    assert second_start['collection_context']['remaining_urls'] == ['https://first.example/start-b', 'https://second.example/end']
    third = next(row for row in before if row['url'].endswith('/end'))
    assert third['collection_context']['remaining_urls'] == ['https://second.example/end']
    assert len(third['collection_context']['completed_targets']) == 2
    assert len(result['sources']) == len({source['id'] for source in result['sources']}) == 2
    completed = result['collection_context']['completed_targets']
    assert completed[0]['final_url'] == completed[1]['final_url']
    assert completed[0]['source_id'] == completed[1]['source_id']
    assert completed[0]['requested_url'] != completed[1]['requested_url']
    assert result['collection_context']['remaining_urls'] == []
    assert len(calls) == 4  # Shared landing consumes its existing exact exchange.
    original = store.records('web_exchange')
    data_dir = store.data_dir
    await host.close()
    store.close()
    reopened = Store(data_dir)
    web = WebCollector(settings, transport=httpx.MockTransport(lambda r: pytest.fail('No saved exchange is resent')), resolver=public_resolver)
    web.acquisition_store = reopened
    resumed = await web.collect(query, phase='before', task_id=task['id'], operation_id='op', evaluate=evaluate)
    assert resumed['sources'] == result['sources']
    assert resumed['collection_context']['remaining_urls'] == []
    assert reopened.records('web_exchange') == original
    reopened.close()


async def test_redirect_completion_remains_complete_when_later_url_failed(tmp_path):
    store = Store(tmp_path)
    def transport(request):
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/landing'})
        if request.url.path == '/landing':
            return httpx.Response(200, text='Complete first source', headers={'content-type': 'text/plain'})
        return httpx.Response(503, text='Actual later failure')
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(transport), resolver=public_resolver)
    web.acquisition_store = store
    query = 'https://first.example/start https://second.example/failure'
    with pytest.raises(WebAcquisitionError) as caught:
        await web.collect(query, phase='pre', task_id='task', operation_id='op')
    assert caught.value.metadata['collection_context']['remaining_urls'] == ['https://second.example/failure']
    assert caught.value.metadata['sources'][0]['requested_url'] == 'https://first.example/start'
    store.close()
