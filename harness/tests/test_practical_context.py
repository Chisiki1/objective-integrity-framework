import asyncio
import json
from pathlib import Path

import pytest

from policy_harness.models import OperationResult, PolicyError
from policy_harness.practical_context import bounded_result, history_page
from policy_harness.practical_models import PracticalStep
from policy_harness.providers import ProviderError, ModelGateway
from policy_harness.store import Store, canonical
from tests.test_practical_runtime import runtime, steps, tool, finish, run
from tests.test_usability_learning import lesson


def test_large_web_source_does_not_hide_later_pages_and_original_is_unchanged():
    raw = {'status': 'succeeded', 'effect': 'confirmed', 'data': {'acquisitions': [],
        'sources': [{'url': 'https://example.org/sitemap', 'text': 'a' * 440000},
                    {'url': 'https://example.org/terms', 'text': 'Personal data retention terms'}]}}
    view = bounded_result(raw, tool='web_fetch')
    sources = view['data']['sources']
    assert sources[0]['total_text_chars'] == 440000 and len(sources[0]['text']) < 10000
    assert sources[1]['text'] == 'Personal data retention terms'
    assert len(raw['data']['sources'][0]['text']) == 440000
    assert len(canonical(view)) < 12000


@pytest.mark.asyncio
async def test_raw_web_history_reaches_model_without_refetch(tmp_path):
    import httpx
    from policy_harness.providers import WebCollector
    from tests.test_providers import FakeSettings, public_resolver
    calls = []
    html = '<title>Page</title><script>const contactEndpoint="/contact/send";</script>'
    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html'}, text=html)
    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('web_fetch', query='https://example.org/contact')),
        steps(tool('history_read', start=0, view='web_response', query='contactEndpoint')),
        finish()])
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(handle), resolver=public_resolver)
    web.acquisition_store = store; engine.web = web
    await run(engine, store, task)
    record = store.operations(task['id'])[1]['result']
    assert record['status'] == 'succeeded' and '/contact/send' in record['data']['text']
    assert len(calls) == 1
    original = store.operations(task['id'])[0]['result']['data']['sources'][0]
    assert 'contactEndpoint' not in original['text']
    with pytest.raises(PolicyError):
        history_page(store, task['id'], {'start': 1, 'view': 'web_response'}, web=web)
    store.close()


@pytest.mark.asyncio
async def test_large_js_prefix_is_searchable_and_later_urls_continue(tmp_path):
    import httpx
    from policy_harness.providers import WebCollector
    from tests.test_providers import FakeSettings, public_resolver
    requests = []
    prefix = b'fetch("/api");navigator.sendBeacon("/metrics");localStorage.getItem("theme");'
    script = prefix + '日'.encode() * 50
    limit = len(prefix) + 4  # One complete Japanese character and one partial.
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/javascript'},
                              content=script if request.url.path == '/app.js' else b'const later="read";')
    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('web_fetch', query=['https://example.org/app.js', 'https://example.org/other.js'])),
        steps(tool('history_read', start=0, view='web_response', query=['fetch(', 'sendBeacon', 'localStorage'])),
        finish(), {'verdict': 'accept', 'findings': [], 'rationale': 'Reports partial source and observed matches only'}])
    web = WebCollector(FakeSettings(web_max_response_bytes=limit), transport=httpx.MockTransport(handle), resolver=public_resolver)
    web.acquisition_store = store; engine.web = web
    await run(engine, store, task)
    assert store.get_task(task['id'])['status'] == 'completed'
    rows = store.operations(task['id'])
    result = rows[0]['result']
    assert result['status'] == 'failed' and result['effect'] == 'confirmed'
    sources = result['data']['sources']
    assert len(requests) == 2 and len(sources) == 2
    assert sources[0]['truncated'] and sources[0]['body_complete'] is False
    assert sources[0]['text'].endswith('日') and sources[0]['received_bytes'] == limit
    assert not sources[1]['truncated'] and 'later=' in sources[1]['text']
    history = rows[1]['result']['data']
    assert history['search_mode'].startswith('case-insensitive')
    assert history['source_coverage'][0]['body_complete'] is False
    assert {m['query'] for m in json.loads(history['text'])['matches']} == {'fetch(', 'sendBeacon', 'localStorage'}
    projection = bounded_result(result, tool='web_fetch')
    assert projection['data']['sources'][0]['truncated'] is True
    assert 'absence' in projection['data']['sources'][0]['coverage_note']
    assert gateway.calls[2][2]['history'][1]['result']['data']['source_coverage'][0]['body_complete'] is False
    store.close()


@pytest.mark.asyncio
async def test_old_size_failure_reconciles_without_replay_and_keeps_original(tmp_path):
    import copy
    import httpx
    from policy_harness.providers import WebCollector
    from policy_harness.practical_engine import Held
    from tests.test_providers import FakeSettings, public_resolver
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'application/javascript'}, text='fetch("/api");' * 20)
    engine, store, _, _, task = runtime(tmp_path, [
        steps(tool('web_fetch', query='https://example.org/app.js')), finish(),
        {'verdict': 'accept', 'findings': [], 'rationale': 'Known incomplete source is explicit'}])
    web = WebCollector(FakeSettings(web_max_response_bytes=55), transport=httpx.MockTransport(handle), resolver=public_resolver)
    web.acquisition_store = store; engine.web = web
    await run(engine, store, task)
    row = store.operations(task['id'])[0]; identity = row['operation']['id']
    saved = store.web_operation_exchanges(task['id'], identity)[0]
    for field in ('read_stopped', 'response_limit_bytes'):
        saved['record'].pop(field, None)  # Older failed exchanges have no new markers.
    store.record('web_exchange', saved['id'], saved)
    original = copy.deepcopy(row['result'])
    original.update(status='unknown', effect='unknown',
                    data={'observation': {'acquisitions': [saved['record']]}, 'sources': []})
    store.update_operation(identity, result=original, status='recovery_required')
    recovered = await engine._recover_operation(task, store.get_operation(identity))
    assert recovered.status == 'failed' and recovered.effect == 'confirmed'
    assert recovered.data['replayed'] is False and len(calls) == 1
    assert 'fetch(' in history_page(store, task['id'], {'start': 0, 'view': 'web_response', 'query': 'fetch'}, web=web)['text']
    assert store.records('practical_result_history')[0]['result'] == original
    assert store.record_get('web_exchange', saved['id']) == saved
    # A matching error string alone is insufficient: changed saved bytes stay held.
    damaged = copy.deepcopy(saved); damaged['partial_base64'] = 'eA=='
    store.record('web_exchange', saved['id'], damaged)
    store.update_operation(identity, result=original, status='recovery_required')
    with pytest.raises(Held, match='still unknown'):
        await engine._recover_operation(task, store.get_operation(identity))
    assert len(calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_explicit_js_read_budget_is_bounded_and_does_not_change_settings(tmp_path):
    import httpx
    from policy_harness.providers import WebCollector, ConfigurationRequired
    from tests.test_providers import FakeSettings, public_resolver
    settings = FakeSettings(web_max_response_bytes=16)
    requests = []
    script = 'const important="tail";'
    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'application/javascript'}, text=script)
    web = WebCollector(settings, transport=httpx.MockTransport(handle), resolver=public_resolver)
    web.acquisition_store = Store(tmp_path)
    result = await web.collect('https://example.org/app.js', phase='task_research', task_id='t', operation_id='op', max_bytes=128)
    assert result['sources'][0]['text'] == script and result['sources'][0]['truncated'] is False
    assert result['sources'][0]['received_bytes'] == len(script) and result['sources'][0]['response_limit_bytes'] == 128
    assert settings.get()['web_max_response_bytes'] == 16 and len(requests) == 1
    for maximum in (0, True, 8000001, '8000000'):
        with pytest.raises(ConfigurationRequired, match='max_bytes'):
            await web.collect('https://example.org/app.js', phase='task_research', task_id='t', operation_id='invalid', max_bytes=maximum)
    with pytest.raises(ConfigurationRequired, match='max_bytes'):
        await web.collect('https://example.org/app.js', phase='pre', task_id='t', operation_id='invalid', max_bytes=128)
    assert len(requests) == 1
    web.acquisition_store.close()


@pytest.mark.asyncio
async def test_incomplete_network_response_still_holds(tmp_path):
    import httpx
    from policy_harness.providers import WebCollector
    from tests.test_providers import FakeSettings, public_resolver
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'const prefix=1;'
            raise httpx.ReadTimeout('No response continuation')
    engine, store, _, _, task = runtime(tmp_path, [steps(tool('web_fetch', query='https://example.org/app.js'))])
    web = WebCollector(FakeSettings(), transport=httpx.MockTransport(lambda r:
        httpx.Response(200, headers={'content-type': 'application/javascript'}, stream=Interrupted())), resolver=public_resolver)
    web.acquisition_store = store; engine.web = web
    await run(engine, store, task)
    result = store.operations(task['id'])[0]['result']
    assert result['status'] == result['effect'] == 'unknown'
    assert store.get_task(task['id'])['status'] == 'held'
    assert result['data']['known_response_limits'] == []
    store.close()


@pytest.mark.asyncio
async def test_oversized_http_error_is_known_and_does_not_become_a_page(tmp_path):
    import httpx
    from policy_harness.providers import WebCollector
    from tests.test_providers import FakeSettings, public_resolver
    engine, store, _, _, task = runtime(tmp_path, [
        steps(tool('web_fetch', query=['https://example.org/missing', 'https://example.org/good'])),
        finish(), {'verdict': 'accept', 'findings': [], 'rationale': 'Only successful page is used'}])
    web = WebCollector(FakeSettings(web_max_response_bytes=16),
        transport=httpx.MockTransport(lambda r: httpx.Response(404 if r.url.path == '/missing' else 200,
            headers={'content-type': 'text/plain'}, text='not found ' * 100 if r.url.path == '/missing' else 'actual page')),
        resolver=public_resolver)
    web.acquisition_store = store; engine.web = web
    await run(engine, store, task)
    result = store.operations(task['id'])[0]['result']
    assert result['status'] == 'failed' and result['effect'] == 'confirmed'
    assert [s['text'] for s in result['data']['sources']] == ['actual page']
    assert store.get_task(task['id'])['status'] == 'completed'
    store.close()


def measured_gateway(gateway, threshold=1200):
    # Deterministic pressure fixture, independent of any network tokenizer.
    gateway.measure_input = lambda role, phase, payload, schema: {
        'input_tokens': len(canonical(payload.get('history', []))) + len(canonical(payload.get('working_memory'))),
        'context_tokens': threshold + 500, 'output_tokens': 500}


def summary():
    return {'summary': 'Earlier input: target code is amber. The original result is operation 0. Output is unfinished.',
            'next_action': 'Write the requested exact output using amber.', 'evidence_indices': [0]}


@pytest.mark.asyncio
async def test_compaction_continues_with_real_original_and_saved_memory(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('file_read', path='input.txt')), summary(),
        steps(tool('file_write', path='answer.txt', text='amber')), finish('answer.txt')])
    Path(task['workspace'], 'input.txt').write_text('target code is amber\n' + 'original evidence\n' * 1000)
    measured_gateway(gateway, 2500)
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert Path(task['workspace'], 'answer.txt').read_bytes() == b'amber'
    assert [c[1] for c in gateway.calls].count('context_compaction') == 1
    consumer = gateway.calls[2][2]
    assert consumer['working_memory']['summary'] == summary()['summary']
    assert consumer['sources'][0]['text'] == task['objective']
    assert consumer['acceptance'][0]['criterion'] == task['acceptance'][0]
    assert store.operations(task['id'])[0]['result']['stdout'].startswith('target code is amber')
    assert result['state']['metrics']['context_compactions'] == 1
    store.close()


@pytest.mark.asyncio
async def test_failed_summary_keeps_context_and_does_not_retry_each_decision(tmp_path):
    async def fail(_):
        raise ProviderError('summary service unavailable')
    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('file_read', path='input.txt')), fail,
        steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    Path(task['workspace'], 'input.txt').write_text('fact\n' * 250)
    gateway.measure_input = lambda role, phase, payload, schema: {
        'input_tokens': 1800 + 100 * len(payload['history']) if payload['history'] else 200,
        'context_tokens': 2900, 'output_tokens': 500}
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert sum(c[1] == 'context_compaction' for c in gateway.calls) == 1
    assert gateway.calls[2][2]['history'][0]['result']['file_content'].startswith('fact')
    assert not store.task_records('practical_context', task['id'])
    assert store.task_records('practical_call', task['id'])[1]['status'] == 'failed'
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('second_failure', [False, True])
async def test_explicit_resume_retries_failed_overflow_once_preserving_failure(tmp_path, second_failure):
    async def fail(_):raise ProviderError('temporary summary connection failure')
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_read', path='input.txt')), fail])
    Path(task['workspace'], 'input.txt').write_text('target code is amber\n' + 'original\n' * 1000)
    measured_gateway(gateway, 2500)
    result = await run(engine, store, task)
    assert result['status'] == 'held'
    failure = store.record_get('practical_context_failure', task['id'])
    first = store.operations(task['id'])[0]
    gateway.replies += [fail] if second_failure else [summary(), steps(tool('file_write', path='answer.txt', text='amber')), finish('answer.txt')]
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == ('held' if second_failure else 'completed')
    assert sum(c[1] == 'context_compaction' for c in gateway.calls) == 2
    assert store.operations(task['id'])[0] == first
    assert store.records('practical_context_failure_history') == [failure]
    assert len([c for c in store.task_records('practical_call', task['id']) if c['status']=='failed']) == (2 if second_failure else 1)
    store.close()


@pytest.mark.asyncio
async def test_source_change_during_compaction_does_not_commit_stale_memory(tmp_path):
    async def changed(_):
        store.append_instruction(task['id'], 'New condition', store.get_task(task['id'])['source_hash'])
        return summary()
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_read', path='input.txt')), changed])
    Path(task['workspace'], 'input.txt').write_text('fact\n' * 1000)
    measured_gateway(gateway, 2500)
    result = await run(engine, store, task)
    assert result['status'] in {'held', 'recovery_required'}
    assert not store.task_records('practical_context', task['id'])
    assert len(store.operations(task['id'])) == 1
    assert store.get_task(task['id'])['source_history'][-1]['status'] == 'pending'
    store.close()


@pytest.mark.asyncio
async def test_summary_response_reused_after_commit_interruption(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_read', path='input.txt')), summary()])
    Path(task['workspace'], 'input.txt').write_text('target code is amber\n' + 'text\n' * 1000)
    measured_gateway(gateway, 2500)
    original_record = store.record
    def interrupt(kind, identity, body):
        if kind == 'practical_context_current':
            raise OSError('simulated disk error before pointer commit')
        return original_record(kind, identity, body)
    store.record = interrupt
    result = await run(engine, store, task)
    assert result['status'] == 'held'
    assert not store.task_records('practical_context', task['id'])
    assert not store.task_records('practical_call', task['id'])[-1]['consumed']
    store.record = original_record
    gateway.replies += [steps(tool('file_write', path='answer.txt', text='amber')), finish('answer.txt')]
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'completed'
    assert sum(c[1] == 'context_compaction' for c in gateway.calls) == 1
    assert len(store.operations(task['id'])) == 2
    store.close()


@pytest.mark.asyncio
async def test_targeted_history_no_recursive_record_payload_and_exact_paging(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    await run(engine, store, task)
    row = store.operations(task['id'])[0]
    result = dict(row['result'], stdout='preamble\n' + 'noise\n' * 4000 + 'needle=amber\nend')
    store.update_operation(row['operation']['id'], result=result)
    match = history_page(store, task['id'], {'start': 0, 'field': 'stdout', 'query': 'needle='})
    assert 'needle=amber' in match['text'] and len(match['text']) < 2000
    page = history_page(store, task['id'], {'start': 0, 'field': 'stdout', 'max_chars': 12000})
    assert page['text'] == result['stdout'][:12000] and page['next_offset'] == 12000
    second = history_page(store, task['id'], {'start': 0, 'field': 'stdout', 'offset': page['next_offset']})
    assert second['text'] == result['stdout'][12000:24000]
    # Reading data.text keeps original characters, not a JSON string escaped again.
    store.update_operation(row['operation']['id'], result=dict(result, data={'text': '"quote"\n'*1200, 'next_offset': 8400}))
    projected = bounded_result(store.get_operation(row['operation']['id'])['result'], 50000)
    assert projected['data']['next_offset'] == 8400
    original = history_page(store, task['id'], {'start': 0, 'view': 'record'})
    assert original['total_chars'] == len(canonical(store.get_operation(row['operation']['id'])))
    with pytest.raises(PolicyError):
        history_page(store, task['id'], {'start': 0, 'field': '__class__.__bases__'})
    store.close()


@pytest.mark.asyncio
async def test_read_content_is_explicit_in_model_input_and_original_remains_exact(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_read', path='input.txt')),
        steps(tool('file_write', path='answer.txt', text='CONFIRMED plum=17')), finish('answer.txt')])
    Path(task['workspace'], 'input.txt').write_bytes(b'CONFIRMED plum=17')
    await run(engine, store, task)
    sent = gateway.calls[1][2]['history'][0]['result']
    assert sent['file_content'] == 'CONFIRMED plum=17' and 'stdout' not in sent
    assert not sent['data']['truncated'] and not sent['data']['utf8_lossy']
    assert store.operations(task['id'])[0]['result']['stdout'] == sent['file_content']
    assert history_page(store, task['id'], {'start':0,'field':'stdout'})['text'] == sent['file_content']
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('content,args', [
    (b'original text\n' * 2500, {}),
    (b'', {}),
    (b'\xff\x00', {'encoding': 'base64'}),
    (b'\xff\x00', {}),
    (b'partial file', {'max_bytes': 3}),
], ids=['long-text', 'empty-text', 'binary', 'lossy-text', 'partial-text'])
async def test_file_read_projection_keeps_text_range_and_encoding_distinct(tmp_path, content, args):
    import base64
    engine, store, gateway, _, task = runtime(tmp_path, [
        steps(tool('file_read', path='input.txt', **args)),
        steps(tool('file_write', path='answer.txt', text='done')), finish('answer.txt')])
    Path(task['workspace'], 'input.txt').write_bytes(content)
    await run(engine, store, task)
    original = store.operations(task['id'])[0]['result']
    sent = gateway.calls[1][2]['history'][0]['result']
    if args.get('encoding') == 'base64':
        assert 'file_content' not in sent and 'file_content_truncated' not in sent
        assert base64.b64decode(sent['data']['base64']) == content
        assert 'Binary read' in sent['content_note']
    else:
        returned = content[:args.get('max_bytes', len(content))]
        text = returned.decode('utf-8', errors='replace')
        assert original['stdout'] == text
        assert sent['file_content'] == text[:22000]
        assert sent['file_content_truncated'] == (len(text) > 22000)
        assert sent['data']['truncated'] == ('max_bytes' in args)
        assert sent['data']['utf8_lossy'] == (text.encode() != returned)
        # Reconstruct the exact returned text from original history, without a new file read.
        offset, restored = 0, ''
        while True:
            page = history_page(store, task['id'], {'start': 0, 'field': 'stdout', 'offset': offset})
            restored += page['text']
            if page['next_offset'] is None:break
            offset = page['next_offset']
        assert restored == text
    assert sent['data']['encoding'] == args.get('encoding', 'utf-8')
    assert store.operations(task['id'])[0]['result'] == original
    store.close()


@pytest.mark.asyncio
async def test_general_lesson_reaches_another_task_without_original_evidence(tmp_path):
    done = finish('answer.txt')
    done['learning'] = [lesson(share_scope='general', sharing_reason='Generic verified text-writing procedure, with no case details')]
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), done])
    await run(engine, store, task)
    other = store.create_task('Write a short UTF-8 text file', ['Exact text'])
    context = engine.learning.context(other)
    assert len(context['skills']) == 1 and context['skills'][0]['scope'] == 'general'
    assert task['id'] not in canonical(context) and 'evidence' not in context['skills'][0]
    step = steps(tool('file_write', path='next.txt', text='world'))
    step['skill_uses'] = [{'skill': 0, 'tool_index': 0, 'adaptation': 'Reuse exact write and byte readback'}]
    complete = finish('next.txt')
    complete['learning_assessments'] = [{'operation_index': 0, 'judgment': 'helpful', 'reason': 'Actual bytes met the condition directly'}]
    gateway.replies += [step, complete]
    await engine.start_task(other['id'])
    assert store.get_task(other['id'])['status'] == 'completed'
    assert engine.learning.snapshot(other['id'])['skills'][0]['use_count'] == 1
    shared = engine.learning.snapshot(other['id'])['skills'][0]
    assert not {'owner_task_id', 'source_hash', 'evidence', 'touched_tasks'} & shared.keys()
    assert task['id'] not in canonical(engine.learning.snapshot(other['id']))
    assert store.operations(task['id'])[0]['operation']['id'] not in canonical(engine.learning.snapshot(other['id']))
    store.close()


@pytest.mark.asyncio
async def test_private_lesson_is_not_promoted_and_does_not_block_task(tmp_path):
    note = lesson(share_scope='general', sharing_reason='reuse'); note['procedure'] = ('Read C' + ':' + '\\Users\\Alice\\private.txt')
    done = finish('answer.txt'); done['learning'] = [note]
    engine, store, _, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), done])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert store.records('practical_skill')[0]['scope'] == 'task:' + task['id']
    assert store.task_records('practical_sharing_review', task['id'])
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('private,proposal', [
    ('Customer Alice at Acme needs help.', 'For Customer Alice at Acme, check the saved bytes.'),
    ('client: acme', 'The client acme requires a readback.'),
    ('customer alice at acme needs the document tomorrow', 'Always send the saved document to acme.'),
    ('顧客: 山田太郎', '山田太郎のために出力を確認する'),
    ('Project amount: 57319', 'Reuse the amount 57319 for the next file.'),
    ('Internal host: build.corp', 'Read from build.corp.'),
])
async def test_case_facts_stay_local_and_task_completes(tmp_path, private, proposal):
    done = finish('answer.txt')
    note = lesson(share_scope='general', sharing_reason='reusable')
    note['procedure'] = proposal
    done['learning'] = [note]
    engine, store, _, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text=private)), done])
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    skill = store.records('practical_skill')[0]
    assert skill['scope'] == 'task:' + task['id']
    other = store.create_task('Other independent task', ['Complete'])
    assert engine.learning.context(other)['skills'] == []
    assert engine.learning.snapshot(task['id'])['skills'][0]['procedure'] == proposal
    store.close()


@pytest.mark.asyncio
async def test_headers_do_not_parse_old_bodies_and_track_rollback_other_connection(tmp_path):
    import sqlite3
    engine, store, _, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    await run(engine, store, task)
    row = store.operations(task['id'])[0]
    identity = row['operation']['id']
    store.update_operation(identity, result=dict(row['result'], stdout='large old result\n' * 100000))
    other = Store(store.data_dir)
    def deny_original(action, table, column, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'operations' and column == 'body' else sqlite3.SQLITE_OK
    store.db.set_authorizer(deny_original)
    assert store.operation_headers(task['id'])[0]['result']['status'] == 'succeeded'
    assert store.unresolved_operations(task['id']) == []
    saved = dict(id='saved', task_id=task['id'], policy_hash=task['policy_hash'], source_hash=task['source_hash'],
                 through=1, summary='Output contains hello.', next_action='Continue from the new instruction.', evidence_indices=[0])
    store.record('practical_context', 'saved', saved)
    store.record('practical_context_current', task['id'], {'id': 'saved'})
    assert engine._payload(store.get_task(task['id']))['working_memory']['summary'] == saved['summary']
    store.db.set_authorizer(None)
    unknown = dict(row['result'], status='unknown', effect='unknown')
    other.update_operation(identity, result=unknown)
    assert store.unresolved_operations(task['id'])[0][0] == 0
    def failed_update():
        store.update_operation(identity, result=row['result'])
        raise RuntimeError('rollback')
    with pytest.raises(RuntimeError):store._transaction(failed_update)
    assert store.operation_headers(task['id'])[0]['result']['effect'] == 'unknown'
    assert store.get_operation(identity)['result'] == unknown
    other.close();store.close()


@pytest.mark.asyncio
async def test_new_instruction_gets_compaction_before_source_reconciliation(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    await run(engine, store, task)
    row = store.operations(task['id'])[0]
    store.update_operation(row['operation']['id'], result=dict(row['result'], stdout='Original observed text: hello\n' * 1000))
    task = store.append_instruction(task['id'], 'Keep the same output and verify the saved result.', store.get_task(task['id'])['source_hash'])
    gateway.replies += [dict(summary='Output contains hello. Original operation 0 has the observed bytes.', next_action='Reconcile the latest instruction.', evidence_indices=[0]),
                        {'objective': task['objective'], 'sources': [{'source': 1, 'classification': 'clarify', 'reason': 'Retains the output requirement'}],
                         'criteria': [{'original': 0, 'disposition': 'retain', 'criterion': task['acceptance'][0], 'reason': 'Unchanged'}]}, finish('answer.txt')]
    measured_gateway(gateway, 2500)
    await engine.resume_task(task['id'])
    assert store.get_task(task['id'])['status'] == 'completed'
    assert [c[1] for c in gateway.calls[2:4]] == ['context_compaction', 'source_reconciliation']
    assert gateway.calls[3][2]['sources'][1]['text'] == task['source_history'][1]['text']
    assert gateway.calls[3][2]['working_memory'] is not None
    store.close()


@pytest.mark.asyncio
async def test_verification_failure_is_visible_after_hundreds_of_passing_cases(tmp_path):
    from policy_harness.practical_context import bounded_result, history_page
    engine, store, _, _, task = runtime(tmp_path, [steps(tool('file_write', path='answer.txt', text='hello')), finish('answer.txt')])
    await run(engine, store, task)
    row = store.operations(task['id'])[0]
    cases = [dict(node_id=f'test_{i}', outcome='passed', detail='') for i in range(450)]
    cases.append(dict(node_id='test_actual_fault', outcome='failed', detail='Original first fault: missing candidate static file'))
    result = dict(row['result'], status='failed', stdout='Passing output\n' * 4000,
                  data={'test_report': {'tests': 451, 'failures': 1, 'errors': 0, 'skipped': 0, 'cases': cases}})
    store.update_operation(row['operation']['id'], result=result)
    view = bounded_result(result, tool='harness_verify')
    assert view['data']['test_report']['failed_cases'][0]['node_id'] == 'test_actual_fault'
    assert 'missing candidate static file' in view['data']['test_report']['failed_cases'][0]['detail']
    assert len(str(view)) < 1500
    assert history_page(store, task['id'], {'field': 'data.test_report.cases.450.detail'})['text'] == cases[-1]['detail']
    assert store.get_operation(row['operation']['id'])['result'] == result
    store.close()
