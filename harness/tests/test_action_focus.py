"""Actual source/focus/permission consumers and browser observations."""
import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from policy_harness.access import PermissionRequired
from policy_harness.browser_observation import BrowserObserver, arguments, availability, browser_channels
from policy_harness.models import OperationResult, PolicyError
from policy_harness.practical_models import PracticalStep, ToolRequest, WorkRequirement
from policy_harness.providers import WebCollector
from policy_harness.task_focus import completion_evidence, current_focus, requirement_update, project_completion
from tests.test_practical_runtime import runtime, run, steps, tool, finish
from tests.test_providers import FakeSettings


@pytest.mark.asyncio
async def test_added_browser_instruction_cannot_disappear_into_objective(tmp_path):
    engine,store,gateway,_,task=runtime(tmp_path,[])
    pending=store.append_instruction(task['id'],'Observe actual browser traffic and storage.',task['source_hash'])
    gateway.replies.append(dict(objective='Observe browser traffic and storage',
        sources=[dict(source=1,classification='add',reason='New observation requested')],
        criteria=[dict(original=0,disposition='retain',criterion=task['acceptance'][0],reason='Keep old requirement')]))
    with pytest.raises(PolicyError,match='Every new instruction'):
        await engine._reconcile_sources(pending)
    assert store.get_task(task['id'])['source_history'][-1]['status']=='pending'
    assert not store.records('task_source_history')
    store.close()


@pytest.mark.asyncio
async def test_completed_answer_is_background_and_new_file_work_is_active(tmp_path):
    engine,store,gateway,_,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),finish('answer.txt')])
    await run(engine,store,task)
    previous=store.get_task(task['id'])
    gateway.replies.extend([
        dict(objective='Keep the original file and create note.txt',sources=[dict(source=1,classification='add',reason='New file')],
             criteria=[dict(original=0,disposition='retain',criterion=task['acceptance'][0],reason='Completed result retained'),
                       dict(original=None,disposition='add',criterion='Create note.txt with note',sources=[1],quote='Create note.txt with note',reason='New task',verification='artifact')]),
        steps(tool('file_write',path='note.txt',text='note')),
        dict(action='complete',message='note.txt is ready.',artifacts=['note.txt'],
             acceptance=[dict(criterion=1,evidence='The file tool saved and read back note.',operation_indices=[1])])])
    await engine.submit_instruction(task['id'],'Create note.txt with note',previous['source_hash'])
    await engine.running[task['id']]
    current=store.get_task(task['id'])
    assert current['status']=='completed'
    assert current['final']['summary']=='note.txt is ready.'
    assert len(current['final']['acceptance'])==2
    assert current['final']['acceptance'][0]['completed_source_hash']==previous['source_hash']
    payload=gateway.calls[-1][2]
    assert payload['current_request']=='Create note.txt with note'
    assert payload['work_focus']['active']==[1]
    assert payload['acceptance'][0]['status']=='completed_background'
    assert engine._generated_paths(current)=={'note.txt'}
    assert len(store.operations(task['id']))==2 and len(gateway.calls)==5
    assert len(store.records('task_final_history'))==1
    store.close()


@pytest.mark.asyncio
async def test_clarification_reopens_only_its_bound_completed_requirement(tmp_path):
    engine,store,gateway,_,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),finish('answer.txt')])
    await run(engine,store,task)
    current=store.get_task(task['id'])
    pending=store.append_instruction(task['id'],'Verify the saved bytes again.',current['source_hash'])
    gateway.replies.append(dict(objective=task['objective'],
        sources=[dict(source=1,classification='clarify',reason='Explicit recheck')],
        criteria=[dict(original=0,disposition='retain',criterion=task['acceptance'][0],sources=[1],
                       quote='Verify the saved bytes again.',reason='Reopen verification',verification='read')]))
    await engine._reconcile_sources(pending)
    focus=current_focus(store.get_task(task['id']))
    assert focus['active']==[0] and not focus['carried'] and focus['requirements']=={'0':'read'}
    store.close()


def test_action_completion_requires_actual_success_and_cannot_downgrade(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    focus=requirement_update(task,[WorkRequirement(criterion=0,verification='run_code')])
    task['state']['work_focus']=focus
    answer=PracticalStep(action='complete',message='Cannot run here.',acceptance=[dict(criterion=0,evidence='Network unavailable')])
    with pytest.raises(PolicyError,match='requires observed run_code'):
        completion_evidence(task,answer,[])
    with pytest.raises(PolicyError,match='Do not downgrade'):
        requirement_update(task,[WorkRequirement(criterion=0,verification='answer')])
    answer.acceptance[0].operation_indices=[0]
    rows=[dict(source_hash=task['source_hash'],operation=dict(kind='exec'),result=dict(status='failed',effect='confirmed'))]
    with pytest.raises(PolicyError,match='Failed or unknown'):
        completion_evidence(task,answer,rows)
    rows[0]['result']['status']='succeeded'
    assert completion_evidence(task,answer,rows)[0]['operation_indices']==[0]
    store.close()


def test_unclassified_action_and_historical_or_unrelated_artifact_cannot_complete(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    answer=PracticalStep(action='complete',message='Done',artifacts=['new.txt'],
        acceptance=[dict(criterion=0,evidence='Created',operation_indices=[0])])
    rows=[dict(source_hash='old-source',operation=dict(kind='file_write'),
        result=dict(status='succeeded',effect='confirmed',artifacts=[dict(path='old.txt')]))]
    with pytest.raises(PolicyError,match='Classify every active'):
        completion_evidence(task,answer,rows)
    task['state']['work_focus']=requirement_update(task,[WorkRequirement(criterion=0,verification='artifact')])
    with pytest.raises(PolicyError,match='current instruction'):
        completion_evidence(task,answer,rows)
    rows[0]['source_hash']=task['source_hash']
    with pytest.raises(PolicyError,match='declared deliverable'):
        completion_evidence(task,answer,rows)
    rows[0]['result']['artifacts'][0]['path']='new.txt'
    assert completion_evidence(task,answer,rows)[0]['operation_indices']==[0]
    store.close()


@pytest.mark.asyncio
async def test_native_created_artifact_requires_current_execution_and_subsequent_readback(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    engine.access.set(task['id'],'full',0,confirm_full_access=True)
    Path(task['workspace'],'generate.py').write_text('from pathlib import Path\nPath("summary.json").write_text(\'{"total":510}\')\n')
    executed=await engine._perform(task,ToolRequest(name='run_python',arguments={'entrypoint':'generate.py'},purpose='Create requested JSON'),'native-artifact')
    assert executed.status=='succeeded' and executed.data['execution']=='native',executed
    read=await engine._perform(task,ToolRequest(name='file_read',arguments={'path':'summary.json'},purpose='Verify generated JSON'),'native-artifact-read')
    assert read.status=='succeeded' and json.loads(Path(task['workspace'],'summary.json').read_text())=={'total':510}
    task['state']['work_focus']=requirement_update(task,[WorkRequirement(criterion=0,verification='artifact')])
    answer=PracticalStep(action='complete',message='Created JSON',artifacts=['summary.json'],
        acceptance=[dict(criterion=0,evidence='Native execution and verified JSON output',operation_indices=[0,1])])
    rows=store.operations(task['id'])
    assert completion_evidence(task,answer,rows)[0]['operation_indices']==[0,1]
    assert 'executable/code output' in engine._review_reasons(task,answer)
    assert engine._artifact_evidence(task,answer.artifacts)[0]['sha256']==read.artifacts[0]['sha256']
    answer.acceptance[0].operation_indices=[1]
    with pytest.raises(PolicyError,match='requires observed artifact'):completion_evidence(task,answer,rows)
    answer.acceptance[0].operation_indices=[0,1]
    rows[0]['source_hash']='old-source'
    with pytest.raises(PolicyError,match='current instruction'):completion_evidence(task,answer,rows)
    rows[0]['source_hash']=task['source_hash']
    with pytest.raises(PolicyError,match='declared deliverable'):completion_evidence(task,answer,list(reversed(rows)))
    store.close()


@pytest.mark.asyncio
async def test_omitted_output_list_and_write_reference_finish_without_repeating_actual_file_work(tmp_path):
    requests=steps(tool('file_write',path='ready.txt',text='ready'),tool('file_read',path='ready.txt'))
    requests['requirements']=[dict(criterion=0,verification='artifact')]
    answer=dict(action='complete',message='ready.txt is saved and verified.',artifacts=[],
                acceptance=[dict(criterion=0,evidence='Verified saved contents by reading',operation_indices=[1])])
    engine,store,gateway,_,task=runtime(tmp_path,[requests,answer])
    await run(engine,store,task)
    final=store.get_task(task['id'])
    assert final['status']=='completed' and Path(task['workspace'],'ready.txt').read_bytes()==b'ready'
    assert len(store.operations(task['id']))==2 and len(gateway.calls)==2
    assert final['final']['artifacts'][0]['relative_path']=='ready.txt'
    assert set(final['final']['acceptance'][0]['operation_indices'])=={0,1}
    projection=store.records('completion_projection')[0]
    assert projection['original']['artifacts']==[] and projection['original']['acceptance'][0]['operation_indices']==[1]
    assert projection['projected']['artifacts']==['ready.txt']
    store.close()


@pytest.mark.parametrize('variant',['old-source','different-file','different-hash','failed-write','unknown-write','read-before-write','no-indices','multiple-outputs'])
def test_completion_projection_never_invents_file_proof_or_selects_ambiguous_outputs(tmp_path,variant):
    engine,store,_,_,task=runtime(tmp_path,[])
    task['state']['work_focus']=requirement_update(task,[WorkRequirement(criterion=0,verification='artifact')])
    import copy
    write=dict(source_hash=task['source_hash'],operation=dict(kind='file_write'),result=dict(status='succeeded',effect='confirmed',artifacts=[dict(path='ready.txt',sha256='a'*64)]))
    read=copy.deepcopy(write);read['operation']['kind']='file_read';rows=[write,read]
    answer=PracticalStep(action='complete',message='Ready',acceptance=[dict(criterion=0,evidence='read',operation_indices=[1])])
    if variant=='old-source':write['source_hash']='old'
    elif variant=='different-file':write['result']['artifacts'][0]['path']='other.txt'
    elif variant=='different-hash':write['result']['artifacts'][0]['sha256']='b'*64
    elif variant=='failed-write':write['result']['status']='failed'
    elif variant=='unknown-write':write['result']['effect']='unknown'
    elif variant=='read-before-write':rows.reverse();answer.acceptance[0].operation_indices=[0]
    elif variant=='no-indices':answer.acceptance[0].operation_indices=[]
    elif variant=='multiple-outputs':
        other=copy.deepcopy(write);other['result']['artifacts'][0]['path']='other.txt';rows.append(other);answer.acceptance[0].operation_indices=[0,2]
    projected=project_completion(task,answer,rows)
    assert not projected.artifacts
    with pytest.raises(PolicyError):completion_evidence(task,projected,rows)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('variant',['unique','wrong-index','historical','ambiguous','paraphrase'])
async def test_missing_source_index_only_binds_one_exact_pending_addition(tmp_path,variant):
    engine,store,gateway,_,task=runtime(tmp_path,[])
    text='Create note.txt with note'
    pending=store.append_instruction(task['id'],text,task['source_hash'])
    if variant=='ambiguous':pending=store.append_instruction(task['id'],text,pending['source_hash'])
    gateway.replies.append(dict(objective=text,sources=[dict(source=i,classification='add',reason='New file')
        for i,s in enumerate(pending['source_history']) if s['status']=='pending'],
        criteria=[dict(original=0,disposition='retain',criterion=task['acceptance'][0],reason='Retain prior'),
        dict(original=None,disposition='add',criterion=text,quote=task['objective'] if variant=='historical' else 'Write a note' if variant=='paraphrase' else text,
             sources=[0] if variant=='wrong-index' else [],reason='New file',verification='artifact')]))
    if variant=='unique':
        await engine._reconcile_sources(pending)
        assert current_focus(store.get_task(task['id']))['requirements']['1']=='artifact'
    else:
        with pytest.raises(PolicyError):await engine._reconcile_sources(pending)
        assert store.get_task(task['id'])['source_history'][-1]['status']=='pending'
    store.close()


def test_capability_prompt_does_not_generalize_container_network_isolation(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    assert 'browser_inspect' in engine._payload(task)['tools']
    prompt=engine.policy.prompt()
    assert 'does NOT apply to web_fetch or browser_inspect' in prompt
    assert 'Return action=tools' in prompt
    store.close()


def test_browser_permissions_still_bind_source_access_and_operation(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    args={'url':'https://example.com'}
    engine.access.set(task['id'],'ask',0)
    with pytest.raises(PermissionRequired):engine.access.check(task,'browser_inspect',args,'inspect','Observe page')
    row=engine.access.pending(task['id'])[0]
    engine.access.resolve(row['id'],'approve',row['proposal_hash'],'Approved')
    engine.access.check(task,'browser_inspect',args,'inspect','Observe page')
    with pytest.raises(PermissionRequired):engine.access.check(task,'browser_inspect',{'url':'https://example.org'},'inspect','Changed page')
    store.close()


@pytest.mark.parametrize('args',[{},dict(url='https://example.com',path='index.html'),dict(path='index.html',wait_ms=20000),dict(path='index.html',timeout_seconds=1000)])
def test_browser_arguments_are_bounded(args):
    with pytest.raises(PolicyError):arguments(args)


def require_browser():
    if not availability()['driver_available']:
        pytest.skip('Playwright driver unavailable')
    if not (os.name=='nt' and browser_channels()[0]) and not os.environ.get('OIF_TEST_BROWSER'):
        pytest.skip('Set OIF_TEST_BROWSER=1 after installing Chromium for real browser tests')


@pytest.mark.asyncio
async def test_actual_browser_executes_task_html_without_host_or_external_access(tmp_path):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[])
    Path(task['workspace'],'index.html').write_text('''<title>Fixture</title><body>before<script>
      localStorage.setItem('answer','42');sessionStorage.setItem('session_token','do-not-print');
      document.cookie='color=blue; SameSite=Lax';document.body.firstChild.textContent='合計 '+(6*7);
      fetch('https://example.com/should-not-send').catch(()=>{});
      console.log('executed');</script></body>''',encoding='utf-8')
    request=ToolRequest(name='browser_inspect',arguments={'path':'index.html','wait_ms':100},purpose='Execute local page and inspect it')
    result=await engine._perform(task,request,'browser-local')
    assert result.status=='succeeded',result.stderr
    assert result.data['javascript_executed'] is True
    assert result.data['rendered_text']=='合計 42'
    storage=result.data['storage'][0]
    assert {'name':'answer','value':'42'} in storage['localStorage']
    assert storage['sessionStorage'][0]['value']=='[hidden session value]'
    assert any(x['name']=='color' and x['value']=='blue' for x in result.data['cookies'])
    assert any(x['url']=='https://example.com/should-not-send' and x['disposition']=='blocked' for x in result.data['network'])
    assert 'do-not-print' not in json.dumps(result.model_dump())
    recovered=engine.browser.recover(engine._operation('browser-local','browser_inspect',request.arguments,request.purpose))
    assert recovered.data==result.data
    reused=await engine._perform(task,request,'browser-local-reuse')
    assert reused.data['reused_from_operation']=='browser-local' and reused.effect=='none'
    Path(task['workspace'],'index.html').write_text('<body><script>document.body.textContent="更新後"</script></body>',encoding='utf-8')
    changed=await engine._perform(task,request,'browser-local-changed')
    assert changed.data['rendered_text']=='更新後' and 'reused_from_operation' not in changed.data
    store.close()


@pytest.mark.asyncio
async def test_actual_browser_brokers_public_js_get_and_records_blocked_posts(tmp_path):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[])
    requests=[]
    def transport(req):
        requests.append(req)
        assert req.url.host=='93.184.216.34'
        assert req.headers['host']=='site.example'
        assert not req.headers.get('authorization')
        if req.url.path=='/':
            return httpx.Response(200,headers={'content-type':'text/html','set-cookie':'theme=dark; SameSite=Lax'},
                text='<body>waiting<script src="/app.js"></script></body>')
        if req.url.path=='/app.js':return httpx.Response(200,headers={'content-type':'text/javascript'},text="fetch('/data.json').then(r=>r.json()).then(x=>{document.body.firstChild.textContent=x.answer;localStorage.setItem('observed',x.answer);fetch('/collect',{method:'POST',body:'test'}).catch(()=>{});fetch('https://127.0.0.1/private').catch(()=>{});})")
        return httpx.Response(200,headers={'content-type':'application/json'},json={'answer':'observed live JS'})
    async def resolver(host):return ['93.184.216.34']
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    result=await engine._perform(task,ToolRequest(name='browser_inspect',arguments={'url':'https://site.example/','wait_ms':400},purpose='Observe actual page JS'),'browser-public')
    assert result.status=='succeeded',result.stderr
    assert result.data['rendered_text']=='observed live JS'
    assert len(requests)==3 and all(r.method=='GET' for r in requests)
    assert any(x['method']=='POST' and x['disposition']=='blocked' for x in result.data['network'])
    assert any('127.0.0.1' in x['url'] and x['disposition']=='blocked' for x in result.data['network'])
    assert any(x['name']=='theme' for x in result.data['cookies'])
    assert result.data['sources']
    from policy_harness.practical_context import history_page
    page=history_page(store,task['id'],{'start':0,'view':'web_response','source':1},web=web)
    assert 'localStorage' in json.dumps(page)
    store.close()


@pytest.mark.asyncio
async def test_actual_browser_redirect_cannot_escape_public_broker(tmp_path):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[])
    calls=[]
    def transport(req):
        calls.append(str(req.url))
        if req.url.path=='/':return httpx.Response(302,headers={'location':'https://site.example/landing'})
        if req.url.path=='/landing':return httpx.Response(200,headers={'content-type':'text/html'},text='<body>redirected<script>fetch("/escape").catch(()=>{})</script></body>')
        if req.url.path=='/escape':return httpx.Response(302,headers={'location':'https://127.0.0.1/private'})
        raise AssertionError('Unexpected target '+str(req.url))
    async def resolver(host):return ['93.184.216.34']
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    result=await engine._perform(task,ToolRequest(name='browser_inspect',arguments={'url':'https://site.example/','wait_ms':200},purpose='Observe redirected page'),'browser-redirect')
    assert result.status=='succeeded',result.stderr
    assert result.data['rendered_text']=='redirected'
    assert len(calls)==3 and all('93.184.216.34' in x for x in calls)
    assert any('127.0.0.1' in str(x.get('redirect','')) and x['disposition']=='blocked' for x in result.data['network'])
    store.close()


@pytest.mark.asyncio
async def test_browser_stop_closes_observation_and_retains_partial_result(tmp_path):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[])
    Path(task['workspace'],'index.html').write_text('<body>start<script>setInterval(()=>localStorage.setItem("tick",String(Date.now())),50)</script></body>',encoding='utf-8')
    operation=engine._operation('cancel-browser','browser_inspect',dict(path='index.html',wait_ms=10000),'Observe then stop')
    entered=asyncio.Event();loop=asyncio.get_running_loop()
    handle=asyncio.create_task(engine.browser.inspect(task,operation,lambda:loop.call_soon_threadsafe(entered.set)))
    await entered.wait();await asyncio.sleep(.3);handle.cancel()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(handle,15)
    result=engine.browser.recover(operation)
    assert result.data['interrupted'] and result.status=='failed'
    assert result.data['finished_at'] and not any(x.startswith('cleanup:') for x in result.data['limits'])
    store.close()


@pytest.mark.asyncio
async def test_live_same_url_requests_keep_new_cookie_and_response(tmp_path):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[]);calls=[]
    def transport(req):
        calls.append((req.url.path,req.headers.get('cookie','')))
        if req.url.path=='/':
            return httpx.Response(200,headers={'content-type':'text/html'},text='''<body>waiting<script>
              (async()=>{await fetch('/state');let r=await fetch('/state');document.body.firstChild.textContent=await r.text()})()
              </script></body>''')
        return httpx.Response(200,headers={'content-type':'text/plain','set-cookie':'stage=second; SameSite=Lax'},
                              text='second response' if 'stage=second' in req.headers.get('cookie','') else 'first response')
    async def resolver(host):return ['93.184.216.34']
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    result=await engine._perform(task,ToolRequest(name='browser_inspect',arguments=dict(url='https://site.example/',wait_ms=300),purpose='Observe consecutive live requests'),'live-repeats')
    assert result.status=='succeeded',result.stderr
    assert result.data['rendered_text']=='second response' and len(calls)==3
    assert len({x['exchange_id'] for x in result.data['network'] if x.get('exchange_id')})==3
    assert engine.browser.recover(engine._operation('live-repeats','browser_inspect',{},'Recover saved')).data==result.data
    assert len(calls)==3
    store.close()


@pytest.mark.asyncio
async def test_concurrent_browser_fetches_share_total_budget(tmp_path,monkeypatch):
    require_browser()
    import policy_harness.browser_observation as module
    monkeypatch.setattr(module,'MAX_TOTAL_BYTES',2500);monkeypatch.setattr(module,'MAX_BODY_BYTES',1600)
    engine,store,_,_,task=runtime(tmp_path,[]);calls=[]
    async def transport(req):
        calls.append(req.url.path)
        if req.url.path=='/':return httpx.Response(200,headers={'content-type':'text/html'},text='<body>bounded<script>for(let i=0;i<8;i++)fetch("/data?i="+i).catch(()=>{})</script></body>')
        await asyncio.sleep(.03)
        return httpx.Response(200,headers={'content-type':'text/plain'},content=b'x'*1400)
    async def resolver(host):return ['93.184.216.34']
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    result=await engine._perform(task,ToolRequest(name='browser_inspect',arguments=dict(url='https://site.example/',wait_ms=400),purpose='Observe bounded concurrent fetches'),'bounded-concurrent')
    assert result.data['received_bytes']<=2500 and len(calls)==3
    assert 'total_response_bytes' in result.data['limits']
    assert any(x.get('partial') for x in result.data['acquisitions'])
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['preflight','subresource'])
async def test_cancelled_http_retains_unknown_even_when_browser_closes(tmp_path,stage):
    require_browser()
    engine,store,_,_,task=runtime(tmp_path,[])
    entered=asyncio.Event();loop=asyncio.get_running_loop();calls=[]
    async def transport(req):
        calls.append(str(req.url))
        if stage=='subresource' and req.url.path=='/':
            return httpx.Response(200,headers={'content-type':'text/html'},text='<body>waiting<script>fetch("/slow").catch(()=>{})</script></body>')
        loop.call_soon_threadsafe(entered.set)
        await asyncio.sleep(30)
        return httpx.Response(200,text='late')
    async def resolver(host):return ['93.184.216.34']
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    operation=engine._operation('cancel-http','browser_inspect',dict(url='https://site.example/',wait_ms=10000),'Stop an in-flight read')
    handle=asyncio.create_task(engine.browser.inspect(task,operation,lambda:None))
    await asyncio.wait_for(entered.wait(),15);handle.cancel()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(handle,15)
    result=engine.browser.recover(operation)
    assert result.status=='unknown' and result.effect=='unknown'
    assert any(x['status']=='unknown' and x.get('http_dispatched') for x in result.data['acquisitions'])
    assert len(calls)==(1 if stage=='preflight' else 2)
    if stage=='preflight':assert not result.data['cleanup']  # No browser started yet.
    else:assert result.data['cleanup']['browser']=='closed' and result.data['cleanup']['driver']=='closed',result.data['limits']
    store.close()


@pytest.mark.asyncio
async def test_late_route_during_cleanup_cannot_start_http_or_change_saved_observation(tmp_path,monkeypatch):
    require_browser()
    from playwright.async_api import BrowserContext
    from types import SimpleNamespace
    engine,store,_,_,task=runtime(tmp_path,[])
    calls=[];aborted=[];handlers={}
    original_route=BrowserContext.route;original_close=BrowserContext.close
    async def install_route(context,url,handler,**kwargs):
        handlers[context]=handler
        return await original_route(context,url,handler,**kwargs)
    async def close_with_queued_route(context,*args,**kwargs):
        async def abort():aborted.append('late')
        async def headers():return {}
        async def fulfill(**kwargs):raise AssertionError('A closing observation must not deliver a new response')
        request=SimpleNamespace(url='https://site.example/late',method='GET',resource_type='fetch',all_headers=headers)
        await handlers[context](SimpleNamespace(abort=abort,fulfill=fulfill),request)
        return await original_close(context,*args,**kwargs)
    monkeypatch.setattr(BrowserContext,'route',install_route)
    monkeypatch.setattr(BrowserContext,'close',close_with_queued_route)
    async def resolver(host):return ['93.184.216.34']
    def transport(req):
        calls.append(req.url.path)
        return httpx.Response(200,headers={'content-type':'text/html'},text='<body>observed</body>')
    web=WebCollector(FakeSettings(),transport=httpx.MockTransport(transport),resolver=resolver)
    web.acquisition_store=store;engine.web=web;engine.browser=BrowserObserver(store,web)
    op=engine._operation('late-route','browser_inspect',dict(url='https://site.example/',wait_ms=0),'Observe and close')
    result=await engine.browser.inspect(task,op,lambda:None)
    assert result.status=='succeeded' and aborted==['late'] and calls==['/'],result
    assert not any(x['url'].endswith('/late') for x in result.data['network'])
    saved=engine.browser.recover(op).model_dump()
    await asyncio.sleep(.1)
    assert engine.browser.recover(op).model_dump()==saved==result.model_dump()
    store.close()
