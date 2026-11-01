"""Local model connections preserve remote HTTPS and credential boundaries."""
import json

import httpx
from pydantic import BaseModel
import pytest

from policy_harness.models import ConfigurationRequired
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.settings import SettingsManager, SettingsError


class Protector:
    def protect(self, raw):
        return b'fixture:' + raw

    def unprotect(self, raw):
        assert raw.startswith(b'fixture:')
        return raw[8:]


class Policy:
    hash = 'fixture-policy'

    def prompt(self):
        return 'Return the requested JSON answer.'


class Answer(BaseModel):
    answer: str


def configured(tmp_path, endpoint):
    settings = SettingsManager(tmp_path, protector=Protector())
    settings.update(dict(base_url=endpoint, model='local-qwen', model_context_tokens=65536,
                         max_output_tokens=4096))
    return settings


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:11434/v1/', 'http://localhost:1234/v1',
                                     'http://[::1]:8080/v1'])
@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
async def test_local_provider_without_key_reaches_both_request_routes(tmp_path, endpoint, mode):
    settings = configured(tmp_path, endpoint)
    settings.update({'api_mode': mode})
    requests = []

    def handle(request):
        requests.append(request)
        assert 'authorization' not in request.headers
        assert str(request.url) == endpoint.rstrip('/') + '/chat/completions'
        assert json.loads(request.content)['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={'model': 'local-qwen', 'choices': [
            {'message': {'role': 'assistant', 'content': '{"answer":"local"}'}, 'finish_reason': 'stop'}]})

    gateway = ModelGateway(settings, Policy(), transport=httpx.MockTransport(handle))
    assert (await gateway.health())['configured']
    answer, metadata = await gateway.generate('parent', 'assessment', {'task_id': 'local-check'}, Answer)
    assert answer.answer == 'local' and len(requests) == 1
    assert metadata['actual_model'] == 'local-qwen'


@pytest.mark.parametrize('endpoint', ['http://example.com/v1', 'http://192.168.1.2/v1',
    'http://127.0.0.1.example/v1', 'http://localhost@192.0.2.1/v1',
    'http://user:password@localhost/v1', 'http://127.0.0.1:0/v1',
    'http://127.0.0.1:99999/v1', 'http://127.0.0.1/v1?key=x',
    'http://127.0.0.1/v1#fragment', 'http://localhost\\@192.0.2.1/v1'])
def test_only_explicit_loopback_http_is_allowed(tmp_path, endpoint):
    settings = SettingsManager(tmp_path)
    with pytest.raises(SettingsError):
        settings.update({'base_url': endpoint})
    assert not settings.path.exists()


def test_cloud_profile_survives_local_switch_without_transferring_its_key(tmp_path):
    settings = configured(tmp_path, 'https://provider.example/v1')
    settings.update({'model_api_key': 'CLOUD_CREDENTIAL_FIXTURE'})
    cloud = settings.save_profile('Cloud')
    cloud_before = settings._load()['model_profiles'][cloud]
    settings.update({'base_url': 'http://127.0.0.1:11434/v1', 'model': 'qwen'})
    assert settings.secret('model_api_key') is None
    assert settings._load()['model_profiles'][cloud] == cloud_before
    local = settings.save_profile('Local Qwen')
    with pytest.raises(SettingsError):
        settings.update({'base_url': 'https://other.example/v1'})
    settings.activate_profile(cloud)
    assert settings.secret('model_api_key') == 'CLOUD_CREDENTIAL_FIXTURE'
    settings.activate_profile(local)
    assert settings.secret('model_api_key') is None
    assert 'CLOUD_CREDENTIAL_FIXTURE' in settings.redaction_secrets()[0]


async def test_remote_model_still_requires_authentication(tmp_path):
    settings = configured(tmp_path, 'https://provider.example/v1')
    gateway = ModelGateway(settings, Policy())
    assert 'model_api_key' in (await gateway.health())['missing']
    with pytest.raises(ConfigurationRequired, match='API key'):
        await gateway.generate('parent', 'assessment', {'task_id': 'remote-check'}, Answer)


async def test_local_authentication_and_redirect_are_not_silently_changed(tmp_path):
    settings = configured(tmp_path, 'http://127.0.0.1:8080/v1')
    settings.update({'model_api_key': 'LOCAL_SERVER_CREDENTIAL_FIXTURE'})
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer LOCAL_SERVER_CREDENTIAL_FIXTURE'
        return httpx.Response(307, headers={'location': 'https://other.example/collect'})

    gateway = ModelGateway(settings, Policy(), transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError, match='HTTP 307'):
        await gateway.generate('parent', 'assessment', {'task_id': 'redirect-check'}, Answer)
    assert len(requests) == 1


def test_web_provider_still_requires_https(tmp_path):
    settings = SettingsManager(tmp_path)
    with pytest.raises(SettingsError):
        settings.update({'web_base_url': 'http://127.0.0.1:8080'})


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:11434/v1', '  http://127.0.0.1:11434/v1  '])
async def test_partial_setup_key_does_not_follow_a_new_local_origin(tmp_path, endpoint):
    settings = SettingsManager(tmp_path, protector=Protector())
    settings.update({'model_api_key': 'UNASSIGNED_CREDENTIAL_FIXTURE'})
    assert not settings.get()['base_url']
    settings.update({'base_url': endpoint, 'model': 'qwen', 'model_context_tokens': 65536, 'max_output_tokens': 4096})
    assert settings.secret('model_api_key') is None
    assert 'UNASSIGNED_CREDENTIAL_FIXTURE' in settings.redaction_secrets()[0]
    requests = []
    def handle(request):
        requests.append(request)
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={'choices': [
            {'message': {'content': '{"answer":"local"}'}, 'finish_reason': 'stop'}]})
    answer, _ = await ModelGateway(settings, Policy(), transport=httpx.MockTransport(handle)).generate(
        'parent', 'assessment', {'task_id': 'partial-setup'}, Answer)
    assert answer.answer == 'local' and len(requests) == 1
