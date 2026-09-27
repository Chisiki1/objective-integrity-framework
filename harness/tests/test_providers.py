"""Synthetic HTTP fixtures plus native DPAPI checks, not paid/live model evidence."""
import json
import os
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict
import pytest

from policy_harness.models import ConfigurationRequired
from policy_harness.providers import ModelGateway, WebCollector, ProviderError, WebAcquisitionError, session_header
from policy_harness.settings import SettingsManager, SettingsError, DEFAULTS


class FakeSettings:
    def __init__(self, **overrides):
        self.values = {**DEFAULTS, 'base_url': 'https://opencode.ai/zen/go/v1', 'model': 'deepseek-v4.1-flash', 'model_context_tokens': 100000, 'max_output_tokens': 1000, 'web_provider': 'public_url', **overrides}
        self.keys = {'model_api_key': 'SYNTHETIC_MODEL_CREDENTIAL', 'web_api_key': 'SYNTHETIC_WEB_CREDENTIAL'}

    def get(self):
        return {**self.values, **{key + '_configured': bool(value) for key, value in self.keys.items()}}

    def secret(self, name):
        return self.keys.get(name)


class FakePolicy:
    hash = 'SOURCE_POLICY_HASH'
    def prompt(self):
        return 'ALL_CURRENT_RULES_AND_SOURCE_FINE_PRINT_MARKER'


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str


def envelope(content='{"summary":"completed assessment"}', **changes):
    value = {'id': 'fixture-response', 'model': 'deepseek-v4.1-flash', 'choices': [{'message': {'content': content, 'role': 'assistant'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 341, 'completion_tokens': 12, 'total_tokens': 353}}
    value.update(changes)
    return value


@pytest.mark.skipif(os.name != 'nt', reason='This verifies real Windows DPAPI, not a substitute cipher.')
def test_dpapi_roundtrip_write_only_atomic_validation(tmp_path):
    manager = SettingsManager(tmp_path)
    public = manager.update({'model_api_key': 'SYNTHETIC_NOT_A_REAL_KEY', 'model': 'chosen-model'})
    assert public['model_api_key_configured'] is True
    assert 'SYNTHETIC_NOT_A_REAL_KEY' not in json.dumps(public)
    assert 'SYNTHETIC_NOT_A_REAL_KEY' not in manager.path.read_text()
    assert SettingsManager(tmp_path).secret('model_api_key') == 'SYNTHETIC_NOT_A_REAL_KEY'
    before = manager.path.read_bytes()
    with pytest.raises(SettingsError):
        manager.update({'api_mode': 'unknown', 'model_api_key': 'SHOULD_NOT_BE_WRITTEN'})
    assert manager.path.read_bytes() == before
    assert manager.public() == manager.get()
    manager.update({'model_api_key': None})
    assert manager.secret('model_api_key') is None


def test_no_paid_defaults_or_secret_in_public(tmp_path):
    manager = SettingsManager(tmp_path)
    assert manager.public()['model'] == ''
    assert manager.public()['base_url'] == ''
    assert manager.public()['cost_limit'] is None
    with pytest.raises(SettingsError):
        manager.update({'base_url': 'https://user:password@example.org/'})
    with pytest.raises(SettingsError):
        manager.secret('another-key')


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
async def test_model_schema_headers_whole_policy_and_real_usage(mode):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())
    gateway = ModelGateway(FakeSettings(api_mode=mode), FakePolicy(), transport=httpx.MockTransport(handle))
    result, meta = await gateway.generate('reviewer', 'pre-review', {'task': {'id': 'task-one'}, 'evidence': 'provided only'}, Answer)
    assert result.summary == 'completed assessment'
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert request.url == 'https://opencode.ai/zen/go/v1/chat/completions'
    assert request.headers['x-opencode-session'] == session_header('task-one')
    assert request.headers['user-agent'] == 'hermes-policy-harness/0.1.0'
    assert 'ALL_CURRENT_RULES_AND_SOURCE_FINE_PRINT_MARKER' in body['messages'][0]['content']
    assert 'SOURCE_POLICY_HASH' in body['messages'][0]['content']
    assert body['messages'][0]['content'].index(FakePolicy().prompt()) < body['messages'][0]['content'].index('Role: reviewer')
    assert not body.get('tools')
    assert meta['usage'] == {'prompt_tokens': 341, 'completion_tokens': 12, 'total_tokens': 353}
    assert meta['cost'] is None
    assert session_header('task-one') != session_header('task-two')


@pytest.mark.parametrize('content', ['```json\n{"summary":"no"}\n```', '{"summary":"x","extra":1}', '{"summary":1}', '{"summary":"a","summary":"b"}'])
async def test_malformed_schema_is_not_repaired_or_retried(content):
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=envelope(content))
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError, match='required JSON schema') as caught:
        await gateway.generate('parent', 'assessment', {'task_id': 'task-one'}, Answer)
    assert count == 1
    meta = caught.value.metadata
    assert meta['finish_reason'] == 'stop'
    assert meta['response_diagnostic']['diagnostic_truncated'] is False
    assert meta['validation_diagnostic']['sanitized_response']
    assert meta['response_sha256']


def test_fixed_policy_prefix_across_role_phase_schema():
    gateway = ModelGateway(FakeSettings(), FakePolicy())
    first = gateway._messages('parent', 'proposal', {'task_id': 'task'}, Answer)[0]['content']
    second = gateway._messages('reviewer', 'assessment', {'task_id': 'task'}, Answer)[0]['content']
    assert first.partition('Role:')[0] == second.partition('Role:')[0]
    assert first.startswith('Complete effective policy and source provenance:\n' + FakePolicy().prompt())


async def test_error_diagnostic_redacts_credentials_and_bounds_only_capture():
    payload = {'summary': 12, 'secret': 'SYNTHETIC_MODEL_CREDENTIAL', 'web_api_key': 'SYNTHETIC_WEB_CREDENTIAL', 'extra': 'x' * 90000}
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope(json.dumps(payload)))))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': 'task-one'}, Answer)
    meta = caught.value.metadata
    for key in FakeSettings().keys.values():
        assert key not in json.dumps(meta)
    diagnostic = meta['response_diagnostic']
    assert diagnostic['diagnostic_truncated'] is True
    assert diagnostic['omitted_chars'] > 0
    details = json.loads(meta['validation_diagnostic']['sanitized_response'])
    assert any(item['type'] == 'string_type' and item['loc'] == ['summary'] for item in details)
    assert all('input' not in item for item in details)
    assert meta['truncated'] is False  # The model response, unlike its display, was complete.


@pytest.mark.parametrize('choices', [[], [{'message': 'invalid', 'finish_reason': 'stop'}], [{'message': {'content': '{"summary":"partial"}'}, 'finish_reason': 'length'}]])
async def test_envelope_or_incomplete_response_keeps_safe_diagnostic(choices):
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope(choices=choices))))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': 'task-one'}, Answer)
    meta = caught.value.metadata
    assert meta['response_diagnostic']['sanitized_response']
    if choices and choices[0]['finish_reason'] == 'length':
        assert meta['finish_reason'] == 'length'
        assert meta['truncated'] is True


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
async def test_tool_call_is_rejected_before_any_execution(mode, tmp_path):
    output = tmp_path / 'MUST_NOT_EXIST.txt'
    malicious = envelope(choices=[{'message': {'content': '', 'tool_calls': [{'id': 't', 'type': 'function', 'function': {'name': 'write_file', 'arguments': json.dumps({'file_path': str(output), 'content': 'bad'})}}]}, 'finish_reason': 'tool_calls'}])
    gateway = ModelGateway(FakeSettings(api_mode=mode), FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=malicious)))
    with pytest.raises(ProviderError, match='tool call'):
        await gateway.generate('reviewer', 'review', {'task_id': 'task-one'}, Answer)
    assert not output.exists()


async def test_context_and_missing_config_stop_before_request():
    def never(request):
        pytest.fail('No HTTP request is allowed in this case')
    settings = FakeSettings(model_context_tokens=10, max_output_tokens=5)
    gateway = ModelGateway(settings, FakePolicy(), transport=httpx.MockTransport(never))
    with pytest.raises(ConfigurationRequired, match='No content was truncated'):
        await gateway.generate('worker', 'proposal', {'task_id': 'task-one'}, Answer)
    settings.values['model'] = ''
    assert (await gateway.health())['configured'] is False
    with pytest.raises(ConfigurationRequired):
        await gateway.generate('worker', 'proposal', {'task_id': 'task-one'}, Answer)


async def test_error_preserves_status_not_sensitive_provider_body():
    secret = 'SYNTHETIC_MODEL_CREDENTIAL'
    gateway = ModelGateway(FakeSettings(), FakePolicy(), transport=httpx.MockTransport(lambda r: httpx.Response(402, text='echo ' + secret)))
    with pytest.raises(ProviderError) as caught:
        await gateway.generate('parent', 'proposal', {'task_id': 'task-one'}, Answer)
    assert caught.value.metadata['http_status'] == 402
    assert secret not in str(caught.value)
    assert secret not in json.dumps(caught.value.metadata)


async def public_resolver(host):
    return ['93.184.216.34']


@pytest.mark.parametrize('fragment', ['', '#open'])
async def test_public_url_real_text_before_after_and_dns_pinning(fragment):
    stages, requests = [], []
    async def evaluate(stage, record):
        stages.append((stage, record.copy()))
    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html; charset=utf-8'}, text='<title>Official title</title><script>discarded script</script><p>Actual page evidence.</p>')
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(handle), resolver=public_resolver)
    result = await web.collect('Compare https://example.org/docs' + fragment, phase='pre', task_id='task', operation_id='op', evaluate=evaluate)
    assert [stage for stage, _ in stages] == ['before', 'after']
    assert result['search_performed'] is False
    assert result['sources'][0]['text'] == 'Official title\nActual page evidence.'
    assert result['sources'][0]['title'] == 'Official title'
    assert requests[0].url.host == '93.184.216.34'
    assert requests[0].headers['host'] == 'example.org'
    assert requests[0].extensions['sni_hostname'] == 'example.org'
    assert 'authorization' not in requests[0].headers
    assert not requests[0].url.fragment
    assert result['sources'][0]['requested_url'] == 'https://example.org/docs' + fragment
    assert result['sources'][0]['url'] == 'https://example.org/docs'


@pytest.mark.parametrize('query', ['https://127.0.0.1/x', 'https://example.org/?api_key=abc', 'https://user:pass@example.org', 'https://localhost/', 'Bearer abcdef', ('C' + ':' + '\\Users\\Private\\notes https://example.org'), 'person@example.org https://example.org'])
async def test_private_targets_and_queries_are_refused(query):
    def never(request):
        pytest.fail('Private data must not reach transport')
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(never), resolver=public_resolver)
    with pytest.raises(WebAcquisitionError):
        await web.collect(query, phase='pre', task_id='task', operation_id='op')


async def test_dns_rebinding_and_unsafe_redirect_cannot_reach_private_host():
    order = []
    async def evaluate(stage, detail):
        order.append(stage)
    async def private_resolver(host):
        order.append('dns')
        return ['127.0.0.1']
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(lambda r: pytest.fail('Private DNS must not connect')), resolver=private_resolver)
    with pytest.raises(WebAcquisitionError, match='Unsafe DNS'):
        await web.collect('https://example.org', phase='pre', task_id='task', operation_id='op', evaluate=evaluate)
    assert order == ['before', 'dns', 'after']
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://169.254.169.254/metadata'})
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(redirect), resolver=public_resolver)
    with pytest.raises(WebAcquisitionError):
        await web.collect('https://example.org', phase='pre', task_id='task', operation_id='op')
    assert len(calls) == 1


async def test_oversize_response_preserves_failure_and_post_evaluation():
    stages = []
    async def evaluate(stage, detail):
        stages.append((stage, detail))
    web = WebCollector(FakeSettings(web_max_response_bytes=5), transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={'content-type': 'text/plain'}, text='long response')), resolver=public_resolver)
    with pytest.raises(WebAcquisitionError) as caught:
        await web.collect('https://example.org', phase='post', task_id='task', operation_id='op', evaluate=evaluate)
    assert [stage for stage, _ in stages] == ['before', 'after']
    assert caught.value.metadata['acquisitions'][0]['partial'] is True
    assert stages[-1][1]['status'] == 'failed'


async def test_keyword_query_is_not_fabricated_as_public_url_search():
    web = WebCollector(FakeSettings())
    with pytest.raises(ConfigurationRequired, match='cannot perform a keyword search'):
        await web.collect('latest API information', phase='pre', task_id='task', operation_id='op')


@pytest.mark.parametrize('provider', ['brave', 'tavily'])
@pytest.mark.parametrize('fragment', ['', '#section'])
async def test_search_and_page_fetch_both_have_evaluations(provider, fragment):
    stages = []
    target = 'https://example.org/docs' + fragment
    async def evaluate(stage, detail):
        stages.append((stage, detail['kind']))
    def handle(request):
        if request.headers['host'] == 'api.search.brave.com':
            assert request.headers['x-subscription-token'] == 'SYNTHETIC_WEB_CREDENTIAL'
            return httpx.Response(200, json={'web': {'results': [{'url': target}]}})
        if request.headers['host'] == 'api.tavily.com':
            assert request.headers['authorization'] == 'Bearer SYNTHETIC_WEB_CREDENTIAL'
            assert json.loads(request.content)['include_usage'] is True
            return httpx.Response(200, json={'results': [{'url': target}], 'usage': {'credits': 1}})
        assert 'authorization' not in request.headers and 'x-subscription-token' not in request.headers
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='Fetched authoritative candidate')
    web = WebCollector(FakeSettings(web_provider=provider), transport=httpx.MockTransport(handle), resolver=public_resolver)
    result = await web.collect('public API guide', phase='pre', task_id='task', operation_id='op', evaluate=evaluate)
    assert result['search_performed'] is True
    assert result['sources'][0]['requested_url'] == target
    assert result['sources'][0]['url'] == 'https://example.org/docs'
    assert stages == [('before', 'search'), ('after', 'search'), ('before', 'fetch'), ('after', 'fetch')]
    if provider == 'tavily':
        assert result['acquisitions'][0]['provider_usage'] == {'credits': 1}


@pytest.mark.parametrize('target', [
    'https://127.0.0.1/x#open', 'https://example.org/?token=opaque#open',
    'https://example.org/#token=opaque', 'https://user:pass@example.org/#open',
    'https://example.org:8443/x#open', 'https://private.internal/x#open',
])
async def test_document_fragments_do_not_hide_refused_targets(target):
    async def never_dns(host):
        pytest.fail('Rejected original URL must not reach DNS')
    web = WebCollector(FakeSettings(), resolver=never_dns)
    with pytest.raises(WebAcquisitionError):
        await web.collect(target, phase='pre', task_id='task', operation_id='op')


@pytest.mark.parametrize('location,match', [
    ('#next', 'redirect cycle'),
    ('https://169.254.169.254/metadata#next', 'public HTTPS'),
    ('https://user:pass@example.org/docs#next', 'public HTTPS|Private'),
])
async def test_fragment_redirect_is_normalized_before_cycle_or_private_dispatch(location, match):
    requests, resolved = [], []
    async def resolver(host):
        resolved.append(host)
        return ['93.184.216.34']
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': location})
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(redirect), resolver=resolver)
    with pytest.raises(WebAcquisitionError, match=match):
        await web.collect('https://example.org/docs#first', phase='pre', task_id='task', operation_id='op')
    assert len(requests) == 1 and resolved == ['example.org']
    assert requests[0].url.path == '/docs' and not requests[0].url.fragment


async def test_fragment_normalization_keeps_escaped_path_and_query_bytes():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/plain'}, text='whole document')
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(handle), resolver=public_resolver)
    target = 'https://example.org/a%23b?q=%23open#section'
    result = await web.collect(target, phase='pre', task_id='task', operation_id='op')
    assert requests[0].url.raw_path == b'/a%23b?q=%23open'
    assert result['sources'][0]['requested_url'] == target
    assert result['sources'][0]['url'] == target.partition('#')[0]
