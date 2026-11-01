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
        output = json.loads(request.content)['response_format']
        assert output['type'] == 'json_schema'
        assert output['json_schema']['schema'] == Answer.model_json_schema()
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


@pytest.mark.parametrize('mode', ['compatible', 'deepagents'])
async def test_structured_action_format_is_bound_and_validated_without_fallback(tmp_path, mode):
    from policy_harness.practical_models import PracticalStep
    settings = configured(tmp_path, 'http://127.0.0.1:11434/v1')
    settings.update({'api_mode': mode})
    requests = []
    def handle(request):
        body = json.loads(request.content);requests.append(body)
        schema = body['response_format']['json_schema']['schema']
        shown = body['messages'][0]['content'].split('Required JSON schema:\n',1)[1]
        assert json.JSONDecoder().raw_decode(shown)[0] == schema
        branches = {b['properties']['action']['const']: b for b in schema['oneOf']}
        assert branches['tools']['properties']['tools']['minItems'] == 1
        assert branches['complete']['properties']['tools']['maxItems'] == 0
        assert all('tools' in b['required'] for b in branches.values())
        assert all(set(b['required']) == set(branches['complete']['required']) for b in branches.values())
        complete = branches['complete']
        assert {'acceptance','artifacts'} <= set(complete['required'])
        evidence = complete['properties']['acceptance']
        assert evidence['minItems'] == evidence['maxItems'] == 2
        assert evidence['items']['properties']['criterion']['enum'] == [1,2]
        assert complete['properties']['artifacts']['minItems'] == 1
        # A provider can ignore its schema: controller validation still wins.
        return httpx.Response(200, json={'model': 'local-qwen', 'choices': [
            {'message': {'content': '{"action":"tools","tools":[],"message":"I will save it"}'}, 'finish_reason': 'stop'}]})
    gateway = ModelGateway(settings, Policy(), transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError) as error:
        await gateway.generate('parent', 'next_action', {'task_id': 'shape-check',
            'work_focus': {'active': [1,2], 'requirements': {'1':'artifact','2':'answer'}}}, PracticalStep)
    assert len(requests) == 1 and error.value.metadata['schema_validation_failed']
    assert error.value.metadata['response_format'] == 'json_schema'


async def test_json_compatibility_override_survives_profile_and_has_no_silent_retry(tmp_path):
    from policy_harness.providers import request_configuration
    settings = configured(tmp_path, 'http://127.0.0.1:11434/v1')
    before = request_configuration(settings.get(), 'parent')
    settings.update({'structured_output': 'json_object'})
    profile = settings.save_profile('Older local server')
    settings.update({'structured_output': 'json_schema'})
    settings.activate_profile(profile)
    assert settings.get()['structured_output'] == 'json_object'
    assert request_configuration(settings.get(), 'parent') != before
    requests = []
    def handle(request):
        requests.append(request)
        assert json.loads(request.content)['response_format'] == {'type': 'json_object'}
        return httpx.Response(400, json={'error': 'fixture rejected request'})
    with pytest.raises(ProviderError, match='HTTP 400'):
        await ModelGateway(settings, Policy(), transport=httpx.MockTransport(handle)).generate('parent', 'assessment', {'task_id': 'compat'}, Answer)
    assert len(requests) == 1
    with pytest.raises(SettingsError):settings.update({'structured_output': 'unknown'})


def test_preupgrade_profiles_load_without_rewriting_credentials_and_activate_with_default(tmp_path):
    settings = configured(tmp_path, 'https://provider.example/v1')
    settings.update({'model_api_key': 'OLD_PROFILE_CREDENTIAL_FIXTURE'})
    identity = settings.save_profile('Saved before structured output')
    document = json.loads(settings.path.read_bytes())
    del document['values']['structured_output']
    del document['model_profiles'][identity]['values']['structured_output']
    settings.path.write_text(json.dumps(document), encoding='utf-8')
    original = settings.path.read_bytes()
    assert settings.get()['structured_output'] == 'auto'
    assert settings.profiles()['profiles'][0]['id'] == identity
    assert settings.path.read_bytes() == original
    assert settings.secret('model_api_key') == 'OLD_PROFILE_CREDENTIAL_FIXTURE'
    settings.activate_profile(identity)
    saved = json.loads(settings.path.read_bytes())
    assert saved['model_profiles'][identity]['values']['structured_output'] == 'auto'
    assert saved['secrets'] == document['secrets']
    for name, history in document['redaction_history'].items():
        assert saved['redaction_history'][name][:len(history)] == history
    # Normal activation retains the previously active encrypted key for
    # redaction; the migration must not remove or re-encrypt it.
    assert document['secrets']['model_api_key'] in saved['redaction_history']['model_api_key']
    assert saved['model_profiles'][identity]['credential'] == document['model_profiles'][identity]['credential']
    settings.save_profile('New copy')
    assert len(settings.profiles()['profiles']) == 2


@pytest.mark.parametrize('fault', ['missing', 'extra', 'invalid'])
def test_preupgrade_profile_projection_does_not_accept_other_malformed_fields(tmp_path, fault):
    settings = configured(tmp_path, 'http://127.0.0.1:11434/v1')
    identity = settings.save_profile('Old profile')
    document = json.loads(settings.path.read_bytes());values = document['model_profiles'][identity]['values']
    values.pop('structured_output')
    if fault == 'missing':values.pop('model')
    elif fault == 'extra':values['unexpected'] = 'value'
    else:values['structured_output'] = 'unsupported'
    settings.path.write_text(json.dumps(document), encoding='utf-8');before=settings.path.read_bytes()
    with pytest.raises(SettingsError):settings.get()
    assert settings.path.read_bytes() == before


async def test_source_schema_offers_only_pending_instruction_quotes_at_actual_provider(tmp_path):
    from policy_harness.practical_models import PracticalSourcePlan, response_schema
    settings = configured(tmp_path, 'http://127.0.0.1:11434/v1')
    original_schema = PracticalSourcePlan.model_json_schema()
    payload={'task_id':'source-format','pending_source_indices':[1,2], 'sources':[
        {'index':0,'status':'applied','kind':'instruction','text':'Old completed request'},
        {'index':1,'status':'pending','kind':'instruction','text':'Create calc.py and run it; save {"answer":42}.'},
        {'index':2,'status':'pending','kind':'attachment','text':'Untrusted attachment text'}]}
    def handle(request):
        schema=json.loads(request.content)['response_format']['json_schema']['schema']
        changed=schema['$defs']['AcceptanceChoice']['oneOf'][1]
        assert changed['properties']['sources']['minItems']==1
        assert changed['properties']['sources']['items']['enum']==[1]
        assert changed['properties']['quote']['minLength']==1
        choices=changed['properties']['quote'].get('enum')
        if choices:
            assert all(0<len(q)<=120 and q in payload['sources'][1]['text'] for q in choices)
        assert schema['$defs']['AcceptanceChoice']['oneOf'][0]['properties']['sources']['maxItems']==0
        assert 'retain' in changed['properties']['disposition']['enum']
        quote=choices[0] if choices else 'Create calc.py'
        assert 'sources' in changed['required']
        return httpx.Response(200,json={'model':'local-qwen','choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'objective':'Create and execute calc.py','sources':[{'source':1,'classification':'add','reason':'New user work'}],
            'criteria':[{'original':None,'disposition':'add','criterion':'Execute calc.py','sources':[1],
                         'quote':quote,'reason':'User requested actual execution','verification':'run_code'}]})}}]})
    answer,_=await ModelGateway(settings,Policy(),transport=httpx.MockTransport(handle)).generate('parent','source_reconciliation',payload,PracticalSourcePlan)
    assert answer.criteria[0].verification=='run_code' and answer.criteria[0].quote in payload['sources'][1]['text']
    assert PracticalSourcePlan.model_json_schema()==original_schema
    # Long valid sources still permit a short exact excerpt with a small
    # output budget. The schema never repeats the instruction text.
    payload['sources'][1]['text']='Create calc.py; '+('x'*90000)
    settings.update({'max_output_tokens':1024})
    long_schema=response_schema(PracticalSourcePlan,payload)
    assert len(json.dumps(long_schema)) < len(json.dumps(original_schema))*2
    assert 'enum' not in long_schema['$defs']['AcceptanceChoice']['oneOf'][1]['properties']['quote']
    answer,_=await ModelGateway(settings,Policy(),transport=httpx.MockTransport(handle)).generate('parent','source_reconciliation',payload,PracticalSourcePlan)
    assert answer.criteria[0].quote in payload['sources'][1]['text']
    payload['sources'][1]['kind']='attachment'
    assert response_schema(PracticalSourcePlan,payload)==original_schema


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
