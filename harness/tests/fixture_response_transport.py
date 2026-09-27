"""Explicit old-wire fixture replies through the real retained HTTP boundary.

Only tests import this adapter. It never supplies a product fallback or creates
response metadata by hand. Existing fixture judgments and output limits remain
the producer; ModelGateway observes and retains the synthetic HTTP exchange.
"""
import json

import httpx

from policy_harness.providers import ModelGateway, ProviderError
from tests.test_provider_evidence_recovery import FixtureCipher
from tests.test_providers import FakeSettings, envelope


class _FixtureCredentials:
    """Keep actual route/lease settings; supply only a missing synthetic key."""
    def __init__(self, settings):
        self.settings = settings

    def __getattr__(self, name):
        return getattr(self.settings, name)

    def secret(self, name):
        value = self.settings.secret(name)
        if value is None and name == 'model_api_key':
            return 'SYNTHETIC_OFFLINE_RESPONSE_CREDENTIAL'
        return value


async def retained_reply(gateway, role, phase, payload, schema, value, *,
                         usage=None, content=None, finish_reason='stop', status_code=200,
                         **arguments):
    store = getattr(gateway, 'response_store', None) or getattr(gateway, 'store', None)
    assert store is not None, 'The fixture must bind its actual temporary response Store'
    sent = []
    async def reply(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body['messages'] == observed._messages(role, phase, payload, schema)
        if status_code != 200:
            return httpx.Response(status_code, json={'error': 'Explicit synthetic interrupted model response'})
        response = envelope(value.model_dump_json() if content is None else content,
                            model=body['model'])
        response['choices'][0]['finish_reason'] = finish_reason
        return httpx.Response(200, json=response)
    settings = getattr(gateway, 'settings', None) or FakeSettings(
        base_url='https://fixture.invalid/v1', model='fixture-model',
        model_context_tokens=10_000_000, max_output_tokens=32768)
    observed = ModelGateway(_FixtureCredentials(settings), gateway.policy,
        transport=httpx.MockTransport(reply), response_store=store,
        response_protector=FixtureCipher())
    observed.supports_semantic_wire = False
    # Keep the actual fixture's packer, including an explicit historical order.
    if hasattr(gateway, '_messages'):
        observed._messages = gateway._messages
    # These fixture producers intentionally emit the original canonical schema.
    assert arguments.get('semantic_wire') is None
    try:
        result, metadata = await observed.generate(role, phase, payload, schema, **arguments)
    except ProviderError:
        assert len(sent) == 1
        raise
    assert len(sent) == 1 and result.model_dump() == value.model_dump()
    return result, {**(usage or {}), **metadata, 'fixture': True}
