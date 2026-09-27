"""Real source/API/controller integration with explicit AI and Web fixtures.

No model, network, browser or production runtime is used. Fault injectors pause
only specified boundaries; Store/source persistence and execution are real.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.model_routing import resolve_lease
from policy_harness.models import Operation, OperationResult, PolicyError, TaskPlan
from policy_harness.policy import PolicyCatalog
from policy_harness.server import create_app
from policy_harness.store import Store, canonical, digest
from tests.test_core import FixtureGateway, FixtureWeb, POLICY
from tests.test_model_routing import configure, selection


FIXTURE_SECRET = 'sk-' + 'localfixturesecret0000000000000001'
BASE_URL = 'http://127.0.0.1:8787'
# Whole5's complete governed consumers exceeded the former 60/30-second
# deadlines. Allow 3x the observed 60-second boundary; retain every outcome
# assertion and keep task cancellation defects independently visible.
GOVERNED_TASK_TIMEOUT_SECONDS = 180


def proposed(operation_kind, **args):
    return Operation(kind=operation_kind, args=args, purpose='Observe the exact source-bound local consumer',
        expected_result='Actual result without semantic or live-model claims',
        decisions=[{'id':'source-consumer', 'statement':'Use one owned operation with the current source',
                    'rationale':'Explicit bounded integration fixture'}])


class SourceGateway(FixtureGateway):
    """AI output fixture; actual model lease validation remains in this path."""
    supports_model_leases = True

    def __init__(self, policy, store, settings):
        super().__init__(policy)
        self.store = store
        self.settings = settings
        self.plan_gates = {}
        self.scope_choices = {}
        self.scope_quotes = {}
        self.outputs = {}
        self.lease_calls = []

    async def generate(self, role, phase, payload, schema, *, model_lease=None, job_context=None):
        if model_lease is not None:
            resolved = resolve_lease(self.settings, model_lease, role=role, job_context=job_context)
            self.lease_calls.append({'task_id':payload['task_id'], 'role':role,
                'lease':model_lease, 'context':job_context, 'resolved':resolved})
        identity = payload['task_id']
        task = self.store.get_task(identity)
        if schema is TaskPlan:
            if identity in self.plan_gates:
                entered, released = self.plan_gates[identity]
                entered.set()
                await released.wait()
            value, usage = await super().generate(role, phase, payload, schema)
            pending = payload['source_context']['pending_ids']
            if pending:
                value = value.model_copy(update={
                    'source_hash':task['source_hash'],
                    'source_dispositions':[{'source_id':source_id, 'classification':'clarify',
                        'reason':'Fixture preserves every prior criterion; source is additional context'} for source_id in pending],
                    'acceptance_dispositions':[{'index':i, 'old_hash':digest(criterion),
                        'disposition':'retain', 'criterion':criterion,
                        'reason':'Exact existing criterion retained by this fixture'}
                        for i, criterion in enumerate(task['acceptance'])]})
            return value, usage
        if schema is Operation:
            rows = [row for row in self.store.operations(identity) if row.get('source_hash') == task['source_hash']]
            kinds = [row['operation']['kind'] for row in rows]
            children = [child for child in self.store.list_tasks() if child['parent_id'] == identity]
            if identity in self.scope_choices:
                stale = [child for child in children if child['state'].get('delegation_lease',{}).get('parent_source_hash') != task['source_hash']]
                if stale:
                    child = stale[0]
                    source = task['source_history'][-1]
                    return proposed('child_scope', child_id=child['id'],
                        expected_lease_hash=digest(child['state']['delegation_lease']),
                        parent_source_hash=task['source_hash'], disposition=self.scope_choices[identity],
                        reason='Explicit fixture source disposition preserves history and original child scope',
                        source_id=source['id'], source_quote=self.scope_quotes.get(identity,source['text'])), {'fixture':True}
            if 'file_write' not in kinds:
                content = self.outputs.get((identity,task['source_version']), 'hello')
                return proposed('file_write', path='answer.txt', text=content), {'fixture':True}
            if 'file_read' not in kinds:
                return proposed('file_read', path='answer.txt'), {'fixture':True}
            retained = [child['id'] for child in children if child['status']!='retired']
            if retained and 'wait_children' not in kinds:
                return proposed('wait_children', child_ids=retained,
                    why_no_independent_work='Own artifact is observed; remaining dependency is the resumed child'), {'fixture':True}
            if not task['parent_id'] and payload['knowledge']['pending']['pending_candidates']:
                # Consume the actual paginated API. A model-output fixture must
                # not assume that a compact navigation view contains full data.
                prior = [row for row in rows if row['status']=='cycle_complete' and
                    (row['operation']['kind']=='candidate_disposition' or
                     (row['operation']['kind'] in {'knowledge_index','knowledge_read'} and
                      row['operation']['args'].get('kind')=='candidate'))]
                last = prior[-1] if prior else None
                if last is None or last['operation']['kind']=='candidate_disposition':
                    return proposed('knowledge_index', kind='candidate', limit=20), {'fixture':True}
                data = last['result']['data']
                if last['operation']['kind']=='knowledge_index':
                    candidates = [item for item in data['items'] if item['status']=='pending']
                    if not candidates:
                        assert data['next_cursor'],'Pending census has no reachable candidate in this exact complete index.'
                        return proposed('knowledge_index',kind='candidate',cursor=data['next_cursor'],limit=20), {'fixture':True}
                    return proposed('knowledge_read',**candidates[0]['read_args'],max_chars=65536), {'fixture':True}
                assert data.get('complete_visible_value'), 'Fixture needs the remaining actual source ranges before disposition.'
                candidate = data['value']
                return proposed('candidate_disposition', candidate_id=candidate['candidate_id'],
                    expected_hash=candidate['candidate_hash'], expected_state_hash=candidate['state_hash'],
                    disposition='reject', reason='Retain actual child history; this fixture adopts no shared procedure change'), {'fixture':True}
            return proposed('finish', summary='Observed owned artifact and explicit child/source dispositions'), {'fixture':True}
        value, usage = await super().generate(role, phase, payload, schema)
        if schema.__name__ == 'Disposition':
            from tests.fixture_response_transport import retained_reply
            return await retained_reply(self, role, phase, payload, schema, value, usage=usage,
                model_lease=model_lease, job_context=job_context)
        return value, usage


@asynccontextmanager
async def runtime(data_dir, *, executor_type=Executor):
    store = Store(data_dir)
    policy = PolicyCatalog(POLICY)
    settings = configure(data_dir)
    executor = executor_type(data_dir)
    gateway = SourceGateway(policy, store, settings)
    engine = Engine(store, policy, executor, gateway, FixtureWeb(), Knowledge(store))
    try:
        yield store, engine, executor, gateway, settings
    finally:
        await engine.close()
        store.close()


@asynccontextmanager
async def http_client(data_dir, store, engine, settings):
    app = create_app(data_dir, engine=engine, settings=settings, store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
                headers={'Origin':BASE_URL}) as client:
            opened = await client.get('/')
            assert opened.status_code == 200
            session = await client.get('/api/session')
            assert session.status_code == 200
            client.headers['X-CSRF-Token'] = session.json()['csrf_token']
            yield client


async def finished(engine, identity):
    handle = engine.running[identity]
    return await asyncio.wait_for(asyncio.shield(handle), GOVERNED_TASK_TIMEOUT_SECONDS)


def assert_completed(result):
    assert result['task']['status']=='completed', json.dumps(result['events'][-3:],ensure_ascii=False)


async def observed_boundary(event,handle,*,timeout=60):
    """Wake on the boundary or actual early task termination, without polling."""
    waiting=asyncio.create_task(event.wait())
    try:
        ready,_=await asyncio.wait([waiting,handle],timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
        assert ready,'Neither execution boundary nor task termination was observed before the bounded deadline.'
        if not event.is_set():
            actual=await handle
            raise AssertionError('Task ended before requested boundary: '+json.dumps(actual['events'][-3:],ensure_ascii=False))
    finally:
        if not waiting.done():waiting.cancel()
        await asyncio.gather(waiting,return_exceptions=True)


async def child_result_consumed(store,parent_id,child_id):
    """Durable event wait; this proves result consumption, not parent finish."""
    stream=store.subscribe(parent_id)
    try:
        async for event in stream:
            if event['stage']=='task' and event['status'] in {'attention_required','configuration_required','failed','stopped'}:
                raise AssertionError({'task_id':parent_id,'stage':event['stage'],'status':event['status'],'detail':event['detail']})
            if event['stage']!='cycle' or event['status']!='completed':continue
            rows=[row for row in store.operations(parent_id) if row['operation']['kind']=='wait_children'
                  and row['status']=='cycle_complete']
            for row in rows:
                child=next((value for value in row['result']['data']['children'] if value['task']['id']==child_id),None)
                if child is not None:
                    assert child['task']['status']=='completed',child['task']['status']
                    return row
    finally:await stream.aclose()


def source_rows(result):
    return [row for row in result['operations'] if row['operation']['kind']=='source_read']


@pytest.mark.asyncio
async def test_http_text_reconciles_real_source_and_downloads_real_executor_artifact(tmp_path):
    data_dir = tmp_path/'data'
    async with runtime(data_dir) as (store, engine, executor, gateway, settings):
        task = store.create_task('  Write hello in answer.txt\n', ['answer.txt contains hello'])
        text = '\nKeep the same required output. Private fixture: '+FIXTURE_SECRET+'  \n'
        async with http_client(data_dir,store,engine,settings) as client:
            response = await client.post('/api/tasks/'+task['id']+'/instructions',
                json={'text':text,'expected_source_hash':task['source_hash']})
            assert response.status_code==200,response.text
            accepted = response.json()['task']
            source = accepted['source_history'][-1]
            assert accepted['id']==task['id'] and len(store.list_tasks())==1
            assert store.source_bytes(task['id'],source['id'])[0]==text.encode()
            assert FIXTURE_SECRET not in response.text and '[REDACTED]' in response.text
            result = await finished(engine,task['id'])
            assert_completed(result)
            assert result['task']['acceptance']==task['acceptance']
            assert result['task']['source_history'][-1]['status']=='applied'
            assert store.records('task_source_history')
            snapshot = await client.get('/api/tasks/'+task['id'])
            assert FIXTURE_SECRET not in snapshot.text
            write = next(row for row in snapshot.json()['operations'] if row['operation']['kind']=='file_write')
            artifact, = write['result']['artifacts']
            assert Path(artifact['path']).is_absolute() and artifact['relative_path']=='answer.txt'
            downloaded = await client.get(artifact['download_url'])
            assert downloaded.status_code==200 and downloaded.content==b'hello'
            assert store.verify_events()


@pytest.mark.asyncio
async def test_http_initial_input_preserves_exact_original_blob_before_projection(tmp_path):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        body={'objective':'  Write hello\r\n'+FIXTURE_SECRET+'  ', 'acceptance':['  answer.txt contains hello  ']}
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks',json=body)
            assert response.status_code==201,response.text
            task=response.json()['task']
            raw,_=store.source_bytes(task['id'],task['source_history'][0]['id'])
            assert raw==canonical(body).encode()
            assert FIXTURE_SECRET not in response.text
            result=await finished(engine,task['id'])
            assert_completed(result)


@pytest.mark.asyncio
async def test_http_attachment_is_immutable_and_governed_ranges_precede_real_plan(tmp_path):
    data_dir=tmp_path/'data'
    raw=('Keep the original criterion. '+FIXTURE_SECRET+'\n').encode()+b'A'*70000
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        task=store.create_task('Write hello',['answer.txt contains hello'])
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+task['id']+'/attachments',json={
                'filename':' additional source.txt ', 'base64':base64.b64encode(raw).decode(),
                'expected_source_hash':task['source_hash']})
            assert response.status_code==200,response.text
            source=response.json()['task']['source_history'][-1]
            assert source['filename']==' additional source.txt ' and source['bytes']==len(raw)
            assert source['sha256']==hashlib.sha256(raw).hexdigest()
            assert store.source_bytes(task['id'],source['id'])[0]==raw
            result=await finished(engine,task['id'])
            assert_completed(result)
            reads=source_rows(result)
            assert [row['result']['data']['offset'] for row in reads]==[0,65536]
            assert sum(row['result']['data']['returned_bytes'] for row in reads)==len(raw)
            assert all(row['status']=='cycle_complete' and row['pre_review'] and row['post_review'] for row in reads)
            progress=store.record_get('source_read_progress',source['id'])
            assert progress['complete'] and progress['source_sha256']==source['sha256']
            plan=next(row for row in result['operations'] if row['operation']['kind']=='plan_task')
            assert result['operations'].index(reads[-1]) < result['operations'].index(plan)
            assert [path.name for path in Path(task['workspace']).iterdir()]==['answer.txt']
            snapshot=await client.get('/api/tasks/'+task['id'])
            assert FIXTURE_SECRET not in snapshot.text
            assert '*'*len(FIXTURE_SECRET) in snapshot.text
            assert reads[0]['result']['data']['redacted'] is True
            assert reads[1]['result']['data']['redacted'] is False
    reopened=Store(data_dir)
    try:
        assert reopened.source_bytes(task['id'],source['id'],source['sha256'])[0]==raw
        assert reopened.get_task(task['id'])['source_hash']==result['task']['source_hash']
    finally:reopened.close()


@pytest.mark.asyncio
async def test_two_http_submissions_that_pass_precheck_have_one_cas_winner(tmp_path,monkeypatch):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        task=store.create_task('Write hello',['answer.txt contains hello'])
        original=engine.submit_instruction
        arrived=asyncio.Event();release=asyncio.Event();count=0
        async def at_engine_boundary(identity,text,expected):
            nonlocal count
            count+=1
            if count==2:arrived.set()
            await release.wait()
            return await original(identity,text,expected)
        monkeypatch.setattr(engine,'submit_instruction',at_engine_boundary)
        async with http_client(data_dir,store,engine,settings) as client:
            tasks=[asyncio.create_task(client.post('/api/tasks/'+task['id']+'/instructions',json={
                'text':text,'expected_source_hash':task['source_hash']})) for text in ['Keep hello. Source A','Keep hello. Source B']]
            try:
                await asyncio.wait_for(arrived.wait(),5)
                assert store.get_task(task['id'])['source_hash']==task['source_hash']
                release.set()
                responses=await asyncio.gather(*tasks)
            finally:
                release.set()
                for pending in tasks:
                    if not pending.done():pending.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            assert sorted(response.status_code for response in responses)==[200,409]
            assert 'SOURCE_HASH_CONFLICT' in next(response.text for response in responses if response.status_code==409)
            current=store.get_task(task['id'])
            assert current['source_version']==2 and len(current['source_history'])==2
            assert len([e for e in store.events(task['id']) if e['stage']=='source' and e['status']=='received'])==1
            assert_completed(await finished(engine,task['id']))


def crash_after_capture(data_dir,task_id,expected_hash):
    """Actual subprocess exits only after real append transaction committed."""
    async def child():
        store=Store(Path(data_dir));policy=PolicyCatalog(POLICY)
        settings=configure(Path(data_dir));gateway=SourceGateway(policy,store,settings)
        engine=Engine(store,policy,Executor(Path(data_dir)),gateway,FixtureWeb(),Knowledge(store))
        async def crash_before_stop(identity):
            print(json.dumps({'pid':os.getpid(),'task_id':identity,
                'source_hash':store.get_task(identity)['source_hash'],'boundary':'source-committed-before-stop'}),flush=True)
            os._exit(73)
        engine.stop_task=crash_before_stop
        await engine.submit_instruction(task_id,'Keep hello after captured-source crash.\n',expected_hash)
        raise AssertionError('Injected crash boundary was not reached')
    asyncio.run(child())


@pytest.mark.asyncio
async def test_fresh_process_recovers_captured_source_and_supersedes_old_unexecuted_choice(tmp_path):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        task=store.create_task('Write hello',['answer.txt contains hello'])
        old=proposed('file_write',path='old-unexecuted.txt',text='must not run')
        store.save_operation(task['id'],old.model_dump(),policy_hash=engine.policy.hash)
        store.update_operation(old.id,status='reviewed')
        store.update_task(task['id'],state={'plan':{'fixture':'prior saved plan'},'operation_id':old.id,'cycle_id':uuid4().hex})
    code='from tests.test_source_integration import crash_after_capture; import sys; crash_after_capture(*sys.argv[1:])'
    command=[sys.executable,'-B','-c',code,str(data_dir),task['id'],task['source_hash']]
    actual=await asyncio.to_thread(subprocess.run,command,cwd=Path(__file__).parents[1],capture_output=True,timeout=20)
    assert actual.returncode==73,(actual.stdout.decode(errors='replace'),actual.stderr.decode(errors='replace'))
    (data_dir/'fixture-crash-process.json').write_bytes(actual.stdout)
    crashed=json.loads(actual.stdout)
    assert crashed['pid']!=os.getpid() and crashed['task_id']==task['id']
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        captured=store.get_task(task['id'])
        assert captured['source_hash']==crashed['source_hash']
        assert captured['source_version']==2 and captured['state']['operation_id']==old.id
        assert captured['source_history'][-1]['status']=='pending'
        assert store.source_bytes(task['id'],captured['source_history'][-1]['id'])[0]==b'Keep hello after captured-source crash.\n'
        result=await asyncio.wait_for(engine.run_task(task['id']),GOVERNED_TASK_TIMEOUT_SECONDS)
        assert_completed(result)
        assert store.get_operation(old.id)['status']=='superseded'
        assert store.get_operation(old.id)['result'] is None
        assert not (Path(task['workspace'])/'old-unexecuted.txt').exists()
        assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'hello'
        assert result['task']['source_history'][-1]['status']=='applied'
        assert store.verify_events()


class LostReceiptExecutor(Executor):
    """Real file write/journal, then explicit cancellation before Engine gets it."""
    def __init__(self,data_dir):
        super().__init__(data_dir)
        self.effect_observed=asyncio.Event()
        self.release=asyncio.Event()
        self.lost_operation=None

    async def execute(self,workspace,operation):
        result=await super().execute(workspace,operation)
        if operation.kind=='file_write' and self.lost_operation is None:
            self.lost_operation=operation.id
            self.effect_observed.set()
            await self.release.wait()
        return result


@pytest.mark.asyncio
async def test_http_instruction_interrupt_preserves_unknown_actual_effect_without_replay(tmp_path):
    data_dir=tmp_path/'data'
    async with runtime(data_dir,executor_type=LostReceiptExecutor) as (store,engine,executor,gateway,settings):
        task=store.create_task('Write observed content',['answer.txt is readable'])
        gateway.outputs[(task['id'],1)]='original actual effect'
        gateway.outputs[(task['id'],2)]='must wait for reconciliation'
        engine.start_task(task['id'])
        await observed_boundary(executor.effect_observed,engine.running[task['id']])
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+task['id']+'/instructions',json={
                'text':'Keep prior effects; apply this context after reconciliation.', 'expected_source_hash':task['source_hash']})
            assert response.status_code==200,response.text
            result=await finished(engine,task['id'])
        original=store.get_operation(executor.lost_operation)
        assert original['result']['status']=='unknown' and original['result']['effect']=='unknown'
        assert result['task']['status']!='completed'
        assert (Path(task['workspace'])/'answer.txt').read_text()=='original actual effect'
        record=executor._read_record(executor.lost_operation)
        assert record['result']['effect']=='confirmed'
        writes=[row for row in result['operations'] if row['operation']['kind']=='file_write']
        assert sum(row['result'] is not None for row in writes)==1
        assert len(list(executor.journal.glob('*.json')))==1
        assert result['task']['source_version']==2 and result['task']['source_history'][-1]['text'].startswith('Keep prior effects')
        assert any('unresolved effect' in str(event['detail']) for event in result['events'])


def seed_child(store,engine,gateway,parent):
    """Persist explicit preexisting delegation state; source/model binding is real."""
    args={'objective':'Child: write hello','acceptance':['answer.txt contains hello'],
        'independent_scope':'Only child answer.txt in its own workspace',
        'integration_plan':'Parent observes child completion while keeping its own source outcome',
        'capability_requirements':['Structured fixture output','Owned file write and read'],
        'quality_requirements':['Preserve parent source and exact role/model lease'],
        'cost_considerations':'Bounded local fixture; no paid calls; preserve operation evidence',
        'model_selections':{role:selection(gateway.settings,role) for role in ['worker','reviewer','parent']}}
    delegated=proposed('delegate',**args)
    store.save_operation(parent['id'],delegated.model_dump(),policy_hash=engine.policy.hash)
    leases=engine._delegate_models(parent,delegated.model_dump())
    child=store.create_task(args['objective'],args['acceptance'],parent_id=parent['id'])
    lease={'parent_id':parent['id'],'parent_operation_id':delegated.id,
        'parent_objective':parent['objective'],'parent_acceptance':parent['acceptance'],
        'parent_source_hash':parent['source_hash'],'policy_hash':engine.policy.hash,
        'constraints':[],'preservation':[],'independent_scope':args['independent_scope'],
        'integration_plan':args['integration_plan'],'model_leases':leases,
        'shared_knowledge_write':'candidate-only; parent consumes shared changes','resources':{}}
    store.update_task(child['id'],state={'delegation_lease':lease},status='stopped')
    result=OperationResult(operation_id=delegated.id,status='succeeded',effect='confirmed',data={'child_id':child['id'],'fixture_seed':True})
    store.update_operation(delegated.id,status='cycle_complete',result=result.model_dump())
    return store.get_task(child['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition',['resume','retire'])
async def test_changed_parent_holds_child_then_governed_scope_disposition_preserves_lineage(tmp_path,disposition):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        parent=store.create_task('Write hello',['answer.txt contains hello'])
        child=seed_child(store,engine,gateway,parent)
        old_lease=child['state']['delegation_lease']
        entered,released=asyncio.Event(),asyncio.Event()
        gateway.plan_gates[parent['id']]=(entered,released)
        gateway.scope_choices[parent['id']]=disposition
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+parent['id']+'/instructions',json={
                'text':'Keep parent output; '+('retain and resume the original child scope.' if disposition=='resume' else 'retire the child while preserving its recorded work.'),
                'expected_source_hash':parent['source_hash']})
            assert response.status_code==200,response.text
            await asyncio.wait_for(entered.wait(),10)
            held=await asyncio.wait_for(engine.run_task(child['id']),10)
            assert held['task']['status']=='attention_required'
            assert not list(Path(child['workspace']).iterdir())
            assert not any(call[2]['task_id']==child['id'] for call in gateway.calls)
            assert any('Parent source changed' in str(event['detail']) for event in held['events'])
            released.set()
            if disposition=='resume':
                # Root accepted this bounded source-consumer endpoint. Remaining
                # parent Knowledge candidates are explicitly preserved; their
                # final closure remains a separate mandatory whole-task check.
                # Observe the complete source-page/post-review consumer. The
                # previous 120s fixture deadline interrupted useful work;
                # this remains an event wait with the same actual endpoint.
                consumed=await asyncio.wait_for(child_result_consumed(store,parent['id'],child['id']),600)
                assert consumed['post_review'] and consumed['status']=='cycle_complete'
                result=await engine.stop_task(parent['id'])
                assert result['task']['status']=='stopped'
                assert not any(row['status']=='executing' for row in result['operations'])
                assert result['knowledge']['candidates']
            else:
                result=await finished(engine,parent['id'])
                assert_completed(result)
        current=store.get_task(child['id'])
        assert current['objective']==child['objective'] and current['acceptance']==child['acceptance']
        history,=store.records('child_scope_history')
        assert history['before']['state']['delegation_lease']==old_lease
        assert current['state']['delegation_lease']['parent_source_hash']==result['task']['source_hash']
        scope=next(row for row in result['operations'] if row['operation']['kind']=='child_scope')
        assert scope['status']=='cycle_complete' and scope['pre_review'] and scope['post_review']
        assert scope['result']['effect']=='confirmed'
        if disposition=='resume':
            assert current['status']=='completed'
            assert (Path(current['workspace'])/'answer.txt').read_bytes()==b'hello'
            assert gateway.lease_calls
            # Routing validates all context fields and canonicalizes SHA256 to
            # uppercase; Engine's digest emits lowercase for the same bytes.
            assert all(bytes.fromhex(call['context']['source_hash'])==bytes.fromhex(call['lease']['job_context']['source_hash'])
                       for call in gateway.lease_calls)
            (data_dir/'source-consumer-outcome.json').write_text(json.dumps({
                'proof':'child completed under renewed lease, parent consumed reviewed result, then safely stopped',
                'parent_id':parent['id'],'parent_status':result['task']['status'],
                'child_id':current['id'],'child_status':current['status'],
                'parent_source_hash':result['task']['source_hash'],
                'old_lease_hash':digest(old_lease),'new_lease_hash':digest(current['state']['delegation_lease']),
                'wait_operation_id':consumed['operation']['id'],'wait_status':consumed['status'],
                'pending_candidates':sum(value['status']=='pending' for value in result['knowledge']['candidates']),
                'lease_calls':[{'task_id':call['task_id'],'role':call['role'],
                    'context_source_hash':call['context']['source_hash'],
                    'lease_source_hash':call['lease']['job_context']['source_hash'],
                    'resolved_lease_hash':call['resolved']['model_selection']['lease_hash']}
                    for call in gateway.lease_calls],
                'unresolved':'Parent Knowledge candidate closure and whole task completion remain mandatory and unproven here.'},indent=2),'utf-8')
        else:
            assert current['status']=='retired' and not list(Path(current['workspace']).iterdir())
            assert not gateway.lease_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('attachment',[False,True])
async def test_http_user_source_cannot_rewrite_a_child(tmp_path,attachment):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        parent=store.create_task('Write hello',['answer.txt contains hello'])
        child=seed_child(store,engine,gateway,parent)
        route='attachments' if attachment else 'instructions'
        body={'expected_source_hash':child['source_hash']}
        body.update({'filename':'source.txt','base64':base64.b64encode(b'foreign child input').decode()} if attachment else {'text':'foreign child input'})
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+child['id']+'/'+route,json=body)
            assert response.status_code==422,response.text
            assert 'primary task' in response.text
        assert store.get_task(child['id'])['source_hash']==child['source_hash']
        assert store.get_task(parent['id'])['source_hash']==parent['source_hash']
        assert len(store.get_task(child['id'])['source_history'])==1


@pytest.mark.asyncio
async def test_child_retirement_rejects_existing_unknown_effect_and_preserves_it(tmp_path):
    data_dir=tmp_path/'data'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        parent=store.create_task('Write hello',['answer.txt contains hello'])
        child=seed_child(store,engine,gateway,parent)
        unknown=proposed('file_write',path='unobserved.txt',text='unobserved')
        store.save_operation(child['id'],unknown.model_dump(),policy_hash=engine.policy.hash)
        result=OperationResult(operation_id=unknown.id,status='unknown',effect='unknown',stderr='Explicit preexisting lost-result fixture')
        store.update_operation(unknown.id,status='cycle_complete',result=result.model_dump())
        gateway.scope_choices[parent['id']]='retire'
        await engine.submit_instruction(parent['id'],'Retire the child only after all effects are reconciled.',parent['source_hash'])
        actual=await finished(engine,parent['id'])
        assert actual['task']['status']=='attention_required'
        assert store.get_task(child['id'])['status']!='retired'
        assert store.get_operation(unknown.id)['result']['effect']=='unknown'
        assert not store.records('child_scope_history')
        assert any('Reconcile child effects' in str(event['detail']) for event in actual['events'])


@pytest.mark.asyncio
async def test_binary_source_public_projection_does_not_disclose_private_encoded_secret(tmp_path):
    data_dir=tmp_path/'data'
    raw=b'\xffBinary fixture\x00'+FIXTURE_SECRET.encode()
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        task=store.create_task('Write hello',['answer.txt contains hello'])
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+task['id']+'/attachments',json={
                'filename':'private-input.bin','base64':base64.b64encode(raw).decode(),
                'expected_source_hash':task['source_hash']})
            assert response.status_code==200,response.text
            source=response.json()['task']['source_history'][-1]
            assert store.source_bytes(task['id'],source['id'])[0]==raw
            await finished(engine,task['id'])
            snapshot=(await client.get('/api/tasks/'+task['id'])).json()
            encoded=[row['result']['data'].get('base64') for row in snapshot['operations']
                     if row['operation']['kind']=='source_read' and row.get('result')]
            disclosed=[value for value in encoded if value and FIXTURE_SECRET.encode() in base64.b64decode(value)]
            assert not disclosed,'Private immutable source bytes were exposed as reversible base64 in the public operation result.'


@pytest.mark.asyncio
async def test_parent_stop_preserves_preexisting_retired_child_terminal_state(tmp_path):
    # A persisted terminal state is an explicit fixture. The actual stop route
    # must preserve it; no proposal or model call is needed for this boundary.
    async with runtime(tmp_path/'data') as (store,engine,executor,gateway,settings):
        parent=store.create_task('Write hello',['answer.txt contains hello'])
        child=seed_child(store,engine,gateway,parent)
        store.update_task(child['id'],status='retired')
        before=store.get_task(child['id'])
        await engine.stop_task(parent['id'])
        current=store.get_task(child['id'])
        assert current['status']=='retired','Stopping a parent reopened an already retired child as '+current['status']
        assert current['source_hash']==before['source_hash']
        assert current['state']['delegation_lease']==before['state']['delegation_lease']
        assert not gateway.calls


@pytest.mark.asyncio
async def test_child_scope_can_quote_the_actual_governed_attachment_content(tmp_path):
    data_dir=tmp_path/'data'
    raw=b'Keep parent output. Retire the child and preserve its existing history.'
    async with runtime(data_dir) as (store,engine,executor,gateway,settings):
        parent=store.create_task('Write hello',['answer.txt contains hello'])
        child=seed_child(store,engine,gateway,parent)
        gateway.scope_choices[parent['id']]='retire'
        gateway.scope_quotes[parent['id']]=raw.decode()
        async with http_client(data_dir,store,engine,settings) as client:
            response=await client.post('/api/tasks/'+parent['id']+'/attachments',json={
                'filename':'child-instructions.txt','base64':base64.b64encode(raw).decode(),
                'expected_source_hash':parent['source_hash']})
            assert response.status_code==200,response.text
            result=await finished(engine,parent['id'])
        assert_completed(result)
        assert store.get_task(child['id'])['status']=='retired'
        read,=source_rows(result)
        assert read['status']=='cycle_complete' and read['result']['stdout']==raw.decode()
        scope=next(row for row in result['operations'] if row['operation']['kind']=='child_scope')
        assert scope['operation']['args']['source_quote']==raw.decode()
        assert result['operations'].index(read) < result['operations'].index(scope)
