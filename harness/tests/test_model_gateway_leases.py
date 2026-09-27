"""Real gateway routing with synthetic transport; never live-provider evidence."""
import json
import threading
import httpx
import pytest

from policy_harness.model_routing import bind_selection,StaleModelLease
from policy_harness.providers import ModelGateway,session_header
from .test_model_routing import selection,context
from .test_providers import FakeSettings,FakePolicy,Answer,envelope


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['compatible','deepagents'])
async def test_selected_job_role_reaches_actual_request_in_both_adapters(mode):
    settings=FakeSettings(api_mode=mode,model_context_tokens=100000,review_model='explicit-fixture-review-model',reasoning_effort='configured-fixture-effort')
    settings._lock=threading.RLock()
    requests=[]
    async def transport(request):
        body=json.loads(request.content);requests.append((request,body))
        return httpx.Response(200,json=envelope(model=body['model']))
    gateway=ModelGateway(settings,FakePolicy(),transport=httpx.MockTransport(transport))
    for role in ['worker','reviewer']:
        job=context('one-child:'+role,role);lease=bind_selection(settings,selection(settings,role),job)
        result,metadata=await gateway.generate(role,'bounded-assessment',{'task_id':'actual-child-id'},Answer,model_lease=lease,job_context=job)
        request,body=requests[-1]
        assert body['model']==lease['model']
        assert body['reasoning_effort']==lease['reasoning']['effort']
        assert request.headers['x-opencode-session']==session_header('actual-child-id')
        assert metadata['model_selection']['lease_hash']==lease['lease_hash']
        assert metadata['requested_model']==lease['model'] and result.summary=='completed assessment'
    assert len(requests)==2 and requests[0][1]['model']!=requests[1][1]['model']


@pytest.mark.asyncio
async def test_stale_model_lease_refused_before_any_key_read_or_transport():
    settings=FakeSettings();job=context();lease=bind_selection(settings,selection(settings),job)
    settings.values['model']='operator-changed-model'
    def secret(name):raise AssertionError('A stale lease must not read credentials')
    settings.secret=secret
    def transport(request):raise AssertionError('A stale lease must not dispatch')
    gateway=ModelGateway(settings,FakePolicy(),transport=httpx.MockTransport(transport))
    with pytest.raises(StaleModelLease):await gateway.generate('worker','assessment',{'task_id':'child'},Answer,model_lease=lease,job_context=job)
