"""Normal Engine/SQLite correction routing; models and HTTP are explicit fixtures.

No corrective operation is inserted into Store by these tests. The ordinary
proposal, permit, real file executor, post review and Learning select/do it.
These cases do not measure live model correctness or full-policy compliance.
"""
import asyncio
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from policy_harness.engine import Engine, WebCorrectionNeeded
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.models import ConfigurationRequired, Disposition, Operation, ResearchQuery, TaskPlan
from policy_harness.policy import PolicyCatalog
from policy_harness.providers import WebCollector
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, POLICY
from tests.test_engine_recovery import proposed
from tests.test_progress_consumers import legacy_correction
from tests.test_web_recovery import Settings, collector


class CorrectionGateway(FixtureGateway):
    def __init__(self, policy, *, pause=None, nested=None, renew_review=False):
        super().__init__(policy)
        self.pause=pause;self.nested=nested;self.paused=False
        self.renew_review=renew_review

    def corrections(self):
        return self.response_store.records('judgment_correction')

    async def generate(self, role, phase, payload, schema):
        store=self.response_store
        if schema is Operation:
            self.calls.append((role,phase,payload))
            pending=payload['required_judgment_corrections']
            if pending:
                returned=[c for c in self.corrections() if c['status']=='resolved'
                          and store.get_operation(c['resolution_operation_id'])['status']=='cycle_complete']
                if self.pause=='first-return' and returned and not self.paused:
                    self.paused=True
                    raise ConfigurationRequired('Fixture pause after one reviewed cycle while its sibling remains pending')
                if self.pause=='proposal' and not self.paused:
                    self.paused=True
                    raise ConfigurationRequired('Fixture pause before the first actual corrective proposal')
                correction=pending[-1]
                path='correction-'+correction['id']+'.txt'
                rows=[r for r in payload['operations'] if r['operation']['args'].get('path')==path
                      and r.get('result') and r['result']['status']=='succeeded'
                      and r['result']['started_at']>=correction['created_at']]
                reads=[r for r in rows if r['operation']['kind']=='file_read']
                if reads:
                    return proposed('judgment_resolve',{'review_id':correction['id'],
                        'expected_hash':correction['hash'],
                        'reason':'The subsequently written and read owned correction record retains the exact applied outcome. The fixture review is now answered with actual evidence, not a prose-only change to committed Learning.',
                        'evidence_operation_ids':[r['id'] for r in rows]}),{'fixture':True}
                if rows:return proposed('file_read',{'path':path}),{'fixture':True}
                return proposed('file_write',{'path':path,'text':'Observed applied outcome '+digest(correction['actual_result'])}),{'fixture':True}
            # Correction files must not make the original answer look delivered.
            rows=[r for r in payload['operations'] if r['operation']['args'].get('path')=='answer.txt'
                  and r.get('result') and r['result']['status']=='succeeded']
            if not any(r['operation']['kind']=='file_write' for r in rows):
                return proposed('file_write',{'path':'answer.txt','text':'hello'}),{'fixture':True}
            if not any(r['operation']['kind']=='file_read' for r in rows):
                return proposed('file_read',{'path':'answer.txt'}),{'fixture':True}
            return proposed('finish',{'summary':'Original answer and required correction actually written/read'}),{'fixture':True}
        if schema is ResearchQuery:
            self.calls.append((role,phase,payload))
            op=payload['operation']
            if self.pause=='resolved-dispatch' and not self.paused and op['kind']=='judgment_resolve' and phase.startswith('post_'):
                self.paused=True
                raise ConfigurationRequired('Fixture pause after resolution dispatch, before its own post review')
            query=('https://example.com/first https://example.com/dependent'
                   if op['kind']=='plan_task' and phase.startswith('pre_') else 'https://example.com/diagnostic')
            return ResearchQuery(query=query,rationale='Explicit mock authoritative sources',private_data_excluded=True),{'fixture':True}
        value,usage=await super().generate(role,phase,payload,schema)
        if schema is TaskPlan:
            context=payload['source_context']
            value=value.model_copy(update={'source_hash':context['source_hash'],
                'source_dispositions':[{'source_id':identity,'classification':'clarify',
                    'reason':'Actual additional fixture context retains every original criterion'}
                    for identity in context['pending_ids']],
                'acceptance_dispositions':[{'index':item['index'],'old_hash':item['hash'],
                    'disposition':'retain','criterion':item['criterion'],
                    'reason':'Retain this exact original acceptance from the current input'}
                    for item in context['prior_acceptance']]})
        if schema is Disposition:
            corrections=self.corrections()
            parent=payload.get('bundle',{}).get('accepted_input',{}).get('parent_operation',{})
            web_phase=phase.startswith('web-learning-outcome') and phase.endswith('_disposition')
            root=web_phase and parent.get('kind')=='plan_task' and (not corrections or self.renew_review)
            if root:self.renew_review=False
            web_nested=(self.nested=='web' and web_phase and parent.get('kind')=='judgment_resolve'
                and not any(c.get('consumer',{}).get('kind')=='web_acquisition'
                    and store.get_operation(c['consumer']['parent_operation_id'])['operation']['kind']=='judgment_resolve'
                    for c in corrections))
            ordinary_nested=(self.nested=='learning' and phase=='learning-outcome_disposition'
                and payload['operation']['kind']=='judgment_resolve'
                and not any(c.get('consumer',{}).get('kind')=='operation' for c in corrections))
            if root or web_nested or ordinary_nested:
                value=value.model_copy(update={'verdict':'revise',
                    'rationale':'Record the exact applied-outcome digest in an owned correction file, read it through the governed executor and return that actual evidence to this review. Do not rewrite the earlier Learning.'})
        return value,usage


def open_runtime(directory, requests, *, pause=None, nested=None, policy=None, renew_review=False,
                 gateway_class=CorrectionGateway, **gateway_options):
    store=Store(directory);policy=policy or PolicyCatalog(POLICY)
    gateway=gateway_class(policy,pause=pause,nested=nested,renew_review=renew_review,**gateway_options);executor=Executor(directory)
    async def resolver(host):return ['93.184.216.34']
    async def transport(request):
        if request.url.path=='/dependent':
            # This is the next actual dependent GET, not a displayed status.
            roots=[]
            for correction in store.records('judgment_correction'):
                work=store.record_get('web_work',correction['operation_id']+':after')
                if work and work['operation']['kind']=='plan_task':roots.append(correction)
            assert roots
            for correction in roots:
                receipt=store.record_get('web_judgment_resolution',correction['id'])
                assert correction['status']=='resolved' and receipt
                assert store.get_operation(receipt['resolution_operation_id'])['status']=='cycle_complete'
            assert not any(c['status']=='pending' for c in store.records('judgment_correction'))
        requests.append({'path':request.url.path,
                         'pending':[c['id'] for c in store.records('judgment_correction') if c['status']=='pending']})
        return httpx.Response(200,headers={'content-type':'text/plain'},content=b'exact mock source')
    web=WebCollector(Settings(),transport=httpx.MockTransport(transport),resolver=resolver)
    engine=Engine(store,policy,executor,gateway,web,Knowledge(store))
    return store,engine,executor,gateway


async def run(engine,task_id,*,timeout=120):
    return await asyncio.wait_for(engine.run_task(task_id),timeout=timeout)


def original_record(store,task_id):
    parents=[r for r in store.operations(task_id) if r['operation']['kind']=='plan_task']
    assert len(parents)==1
    parent=parents[0]
    after=next(w for w in store.records('web_work') if w['stage']=='after'
               and w['operation']['id']==parent['operation']['id'] and w['detail']['url'].endswith('/first'))
    return parent,after


def assert_original_preserved(store,task_id,parent,after,app,requests):
    assert Path(store.get_task(task_id)['workspace'],'answer.txt').read_bytes()==b'hello'
    assert store.get_operation(parent['operation']['id'])['operation']==parent['operation']
    current=store.record_get('web_work',after['id'])
    for field in ('detail','acquisition_operation','acquisition_result','applied_learning','learning_applied'):
        assert current[field]==after[field]
    assert store.record_get('knowledge_application',app['id'])==app
    assert [r['path'] for r in requests].count('/first')==1
    assert [r['path'] for r in requests].count('/dependent')==1
    assert not store.get_task(task_id)['state'].get('web_correction_returns')
    assert not any(c['status']=='pending' for c in store.records('judgment_correction'))
    assert all(r['result'] and r['status']=='cycle_complete' for r in store.operations(task_id))
    for correction in store.records('judgment_correction'):
        resolution=store.get_operation(correction['resolution_operation_id'])
        evidence=[store.get_operation(i) for i in resolution['operation']['args']['evidence_operation_ids']]
        assert {'file_write','file_read'} <= {r['operation']['kind'] for r in evidence}
        for row in evidence:
            assert row['result']['started_at']>=correction['created_at']
            assert row['pre_review']['disposition']['verdict']=='proceed'
            assert row['post_review']['disposition']['verdict']=='proceed'
            assert row['knowledge']['episode_id']
        text=Path(store.get_task(task_id)['workspace'],'correction-'+correction['id']+'.txt').read_text()
        assert text=='Observed applied outcome '+digest(correction['actual_result'])
        assert any(r['operation']['kind']=='file_read' and r['result']['stdout']==text for r in evidence)
    assert store.verify_events()


@pytest.mark.asyncio
@pytest.mark.parametrize('restart',[False,True])
async def test_preplan_applied_review_routes_real_correction_before_dependent_get(tmp_path,restart):
    requests=[];directory=tmp_path/'data'
    store,engine,executor,gateway=open_runtime(directory,requests,pause='proposal')
    task=store.create_task('Write exact hello and retain actual correction evidence in this workspace',['answer.txt contains hello'])
    paused=await run(engine,task['id'])
    assert paused['task']['status']=='configuration_required'
    assert not paused['task']['state'].get('plan')
    frame,=paused['task']['state']['web_correction_returns']
    parent,after=original_record(store,task['id'])
    assert parent['status']=='proposed' and parent['result'] is None
    assert frame['operation_id']==parent['operation']['id']
    assert [r['path'] for r in requests]==['/first']
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    if restart:
        await engine.close();store.close()
        store,engine,executor,gateway=open_runtime(directory,requests)
    result=await run(engine,task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    returned=store.record_get('web_correction_continuation',frame['id'])
    assert returned['operation_id']==frame['operation_id'] and returned['cycle_id']==frame['cycle_id']
    assert returned['status']=='returned'
    assert_original_preserved(store,task['id'],parent,after,app,requests)
    proposals=[p for _,phase,p in gateway.calls if phase=='operation_proposal' and p.get('web_correction_context',{}).get('pending_returns')]
    assert proposals and all(p['plan'] is None for p in proposals)
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_resolved_dispatch_reopens_into_own_review_before_original_return(tmp_path):
    requests=[];directory=tmp_path/'data'
    store,engine,executor,gateway=open_runtime(directory,requests,pause='resolved-dispatch')
    task=store.create_task('Write hello and retain correction evidence',['answer.txt contains hello'])
    paused=await run(engine,task['id'])
    assert paused['task']['status']=='configuration_required'
    correction,=store.records('judgment_correction')
    assert correction['status']=='resolved' and not store.records('web_judgment_resolution')
    resolution=store.get_operation(correction['resolution_operation_id'])
    assert resolution['status']=='result_recorded'
    effect=deepcopy(store.record_get('controller_effect',resolution['operation']['id']))
    assert [r['path'] for r in requests].count('/dependent')==0
    parent,after=original_record(store,task['id'])
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    await engine.close();store.close()
    store,engine,executor,gateway=open_runtime(directory,requests)
    result=await run(engine,task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert store.record_get('controller_effect',resolution['operation']['id'])==effect
    assert_original_preserved(store,task['id'],parent,after,app,requests)
    await engine.close();store.close()


class PreparationCorrectionGateway(CorrectionGateway):
    """Bounded model faults and source updates; operations use the real graph."""
    def __init__(self,policy,*,attachment_kind='opaque',reject=False,**kwargs):
        super().__init__(policy,**kwargs)
        self.attachment_kind=attachment_kind;self.reject=reject
        self.excluded=None;self.rejection_feedback=None

    async def generate(self,role,phase,payload,schema):
        store=self.response_store;task=store.get_task(payload['task_id'])
        attachment=next((s for s in task['source_history'] if s['kind']=='attachment'),None)
        if schema is ResearchQuery and attachment:
            op=payload['operation'];marker='Retain the original hello requirement; recheck the pending withdrawal against this newer context.'
            if (self.attachment_kind=='decoded-confirmation' and op['kind']=='source_prepare'
                    and op['args']['mode']=='withdraw' and phase.startswith('post_')
                    and not any(s.get('text')==marker for s in task['source_history'])):
                assert store.get_operation(op['id'])['result']['status']=='succeeded'
                store.append_instruction(task['id'],marker,task['source_hash'])
        if schema is Operation and attachment:
            preparation=payload.get('source_preparation',{})
            rows=store.operations(task['id'])
            if (self.attachment_kind=='decoded-confirmation'
                    and not any(r['operation']['kind']=='source_prepare' for r in rows)):
                # A normal withdrawal capture followed by an actual later user
                # source produces the pending confirmation; no record is forged.
                instruction=next(s for s in task['source_history'] if s.get('text')=='Withdraw note.txt; keep the original hello deliverable.')
                self.calls.append((role,phase,payload))
                return proposed('source_prepare',{'mode':'withdraw','source_id':attachment['id'],
                    'expected_hash':attachment['sha256'],'expected_source_hash':task['source_hash'],
                    'instruction_id':instruction['id'],'source_quote':instruction['text'],
                    'reason':'Exact fixture instruction requests only this attachment withdrawal'}),{'fixture':True}
            if preparation.get('pending') and self.reject:
                self.calls.append((role,phase,payload))
                if self.excluded is None:
                    self.excluded=proposed('delegate',{'objective':'Forbidden at this pending-source boundary'})
                    assert self.excluded.kind not in preparation['allowed_preparation_operations']
                    return self.excluded,{'fixture':True}
                self.rejection_feedback=payload.get('actual_format_feedback')
                assert self.rejection_feedback
                raise ConfigurationRequired('Fixture pause after actual rejection, before a replacement proposal')
            if phase=='source_preparation_proposal':
                self.calls.append((role,phase,payload))
                item=preparation['pending'][0]
                if item['preparations'] or item['confirmation_pending']:
                    raise ConfigurationRequired('Fixture leaves source meaning/confirmation pending after allowed correction progress')
                return proposed('source_prepare',{'mode':'stage',
                    **{k:item[k] for k in ('source_id','expected_hash','expected_source_hash')},
                    'reason':'Preserve exact opaque bytes through the normal preparation route'}),{'fixture':True}
        return await super().generate(role,phase,payload,schema)


@pytest.mark.asyncio
@pytest.mark.parametrize('restart',[False,True])
@pytest.mark.parametrize('attachment_kind',['opaque','decoded-confirmation'])
async def test_pending_source_restriction_composes_with_normal_correction(tmp_path,restart,attachment_kind):
    requests=[];directory=tmp_path/'data'
    options={'gateway_class':PreparationCorrectionGateway,'attachment_kind':attachment_kind}
    store,engine,executor,gateway=open_runtime(directory,requests,pause='proposal',reject=True,**options)
    task=store.create_task('Write hello and retain actual correction evidence',['answer.txt contains hello'])
    paused=await run(engine,task['id']);assert paused['task']['status']=='configuration_required'
    parent,after=original_record(store,task['id'])
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    raw=b'\xff\x00exact opaque fixture\xfe' if attachment_kind=='opaque' else b'Actual decoded attachment, not an accepted source disposition.'
    filename='opaque.bin' if attachment_kind=='opaque' else 'note.txt'
    task=store.append_instruction(task['id'],'Read this attachment while preserving the hello deliverable.',
        task['source_hash'],attachment={'filename':filename,'bytes':raw})
    source=task['source_history'][-1]
    if attachment_kind=='decoded-confirmation':
        task=store.append_instruction(task['id'],'Withdraw note.txt; keep the original hello deliverable.',task['source_hash'])
    rejected=await run(engine,task['id'])
    assert rejected['task']['status']=='configuration_required',rejected['events'][-1]
    assert gateway.excluded and gateway.rejection_feedback
    with pytest.raises(KeyError):store.get_operation(gateway.excluded.id)
    assert not any(r['operation']['kind'] in {'delegate','child_integrate'} for r in store.operations(task['id']))
    assert not any(t['parent_id']==task['id'] for t in store.list_tasks())
    progress=deepcopy(store.record_get('source_read_progress',source['id']))
    assert progress['complete'] and progress['text_decoded']==(attachment_kind=='decoded-confirmation')
    context=engine.source_preparation.pending(store.get_task(task['id']))
    item,=context['pending'];assert item['source_id']==source['id'] and not item['source_semantics_adopted']
    assert bool(item['confirmation_pending'])==(attachment_kind=='decoded-confirmation')
    if attachment_kind=='decoded-confirmation':
        withdrawal,=[r for r in store.operations(task['id']) if r['operation']['kind']=='source_prepare']
        assert withdrawal['status']=='cycle_complete' and withdrawal['result']['status']=='succeeded'
        assert item['confirmation_pending'][0]['id']==withdrawal['operation']['id']
        assert item['confirmation_pending'][0]['reviewed_source_hash']!=store.get_task(task['id'])['source_hash']
    before_source=deepcopy(store.get_task(task['id'])['source_history'])
    before_hash=store.get_task(task['id'])['source_hash']
    committed=deepcopy(store.records('knowledge_application'))
    if restart:
        await engine.close();store.close()
        store,engine,executor,gateway=open_runtime(directory,requests,**options)
    else:gateway.reject=False
    advanced=await run(engine,task['id'])
    assert advanced['task']['status']=='configuration_required',advanced['events'][-1]
    assert not advanced['task']['state'].get('plan') and advanced['task']['final'] is None
    correction,=store.records('judgment_correction')
    assert correction['status']=='resolved' and store.record_get('web_judgment_resolution',correction['id'])
    resolution=store.get_operation(correction['resolution_operation_id'])
    assert resolution['status']=='cycle_complete'
    evidence=[store.get_operation(i) for i in resolution['operation']['args']['evidence_operation_ids']]
    text='Observed applied outcome '+digest(correction['actual_result'])
    assert Path(advanced['task']['workspace'],'correction-'+correction['id']+'.txt').read_text()==text
    assert any(r['operation']['kind']=='file_read' and r['result']['stdout']==text for r in evidence)
    assert all(r['status']=='cycle_complete' and r['knowledge']['episode_id'] for r in evidence)
    assert advanced['task']['source_hash']==before_hash and advanced['task']['source_history']==before_source
    assert advanced['task']['acceptance']==task['acceptance']
    assert store.source_bytes(task['id'],source['id'])[0]==raw
    assert store.record_get('source_read_progress',source['id'])==progress
    assert len([r for r in store.operations(task['id']) if r['operation']['kind']=='source_read'])==1
    assert store.record_get('knowledge_application',app['id'])==app
    for application in committed:assert store.record_get('knowledge_application',application['id'])==application
    assert [r['path'] for r in requests].count('/first')==1 and not any(r['path']=='/dependent' for r in requests)
    assert engine.source_preparation.pending(advanced['task'])['pending']
    assert store.get_operation(parent['operation']['id'])['status']=='superseded'
    assert not engine.web_judgments.correction_frontier(task['id'])
    if attachment_kind=='opaque':
        stage,=[r for r in store.operations(task['id']) if r['operation']['kind']=='source_prepare']
        assert stage['status']=='cycle_complete' and stage['result']['status']=='succeeded'
        assert Path(advanced['task']['workspace'],stage['result']['data']['path']).read_bytes()==raw
    assert store.verify_events()
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('nested',['web','learning'])
async def test_nested_review_keeps_original_parent_and_independent_correction_progress(tmp_path,nested):
    requests=[];store,engine,executor,gateway=open_runtime(tmp_path/'data',requests,pause='proposal',nested=nested)
    task=store.create_task('Write hello and retain correction evidence',['answer.txt contains hello'])
    paused=await run(engine,task['id'])
    assert paused['task']['status']=='configuration_required'
    parent,after=original_record(store,task['id'])
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    result=await run(engine,task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert len(store.records('judgment_correction'))==2
    frames=store.records('web_correction_continuation')
    assert len(frames)==(2 if nested=='web' else 1)
    assert len({f['operation_id'] for f in frames})==len(frames)
    assert any(r['path']=='/diagnostic' and r['pending'] for r in requests)
    assert_original_preserved(store,task['id'],parent,after,app,requests)
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_legacy_complete_after_review_is_routed_before_restart_drain(tmp_path):
    requests=[];directory=tmp_path/'data'
    store,engine,executor,gateway=open_runtime(directory,requests,pause='proposal')
    task=store.create_task('Write hello and retain correction evidence',['answer.txt contains hello'])
    paused=await run(engine,task['id']);assert paused['task']['status']=='configuration_required'
    frame,=paused['task']['state']['web_correction_returns']
    parent,after=original_record(store,task['id'])
    old=legacy_correction(store,after,store.records('judgment_correction')[0])
    # Reproduce the prior caller's already acquired later FAQ, with its actual
    # after-stage interrupted before Learning. This intentionally uses that old
    # callback shape only to build the historical input; recovery uses run_task.
    faq_requests=[];old_web=collector(store,faq_requests);actual_generate=gateway.generate
    async def interrupt_faq_after(role,phase,payload,schema):
        if phase=='web_acquisition_after':
            raise ConfigurationRequired('Fixture retains an acquired FAQ with unfinished after-work')
        return await actual_generate(role,phase,payload,schema)
    gateway.generate=interrupt_faq_after
    faq_query=ResearchQuery(query='https://example.com/queued-faq',rationale='Actual historical mock response',private_data_excluded=True)
    with pytest.raises(ConfigurationRequired):
        await old_web.collect(faq_query.query,phase='pre',task_id=task['id'],operation_id=parent['operation']['id'],
            evaluate=lambda stage,detail:engine.web_judgments.evaluate(task['id'],parent['operation'],stage,detail,faq_query.model_dump()))
    gateway.generate=actual_generate
    faq=next(w for w in store.records('web_work') if w['stage']=='after' and w['detail']['url'].endswith('/queued-faq'))
    assert len(faq_requests)==1 and faq['status']=='pending' and not faq.get('learning_applied')
    # Model the old product's persisted owner shape, not a manually inserted
    # corrective operation. Its complete after-work and actual commit stay exact.
    state=dict(store.get_task(task['id'])['state']);state.pop('web_correction_returns')
    state.update(operation_id=frame['operation_id'],cycle_id=frame['cycle_id'])
    store.update_task(task['id'],state=state,status='stopped')
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    await engine.close();store.close()
    store,engine,executor,gateway=open_runtime(directory,requests)
    first=[];actual=engine.web_judgments.drain
    async def observed_drain(identity):
        first_call=not first
        if first_call:
            first.append(deepcopy(store.get_task(identity)['state']))
        value=await actual(identity)
        if first_call:
            assert store.record_get('web_work',faq['id'])==faq
            assert store.record_get('judgment_correction',old['id'])==old
        return value
    engine.web_judgments.drain=observed_drain
    result=await run(engine,task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    assert first[0]['web_correction_returns'][0]['operation_id']==parent['operation']['id']
    assert first[0].get('operation_id') is None
    retained=store.record_get('judgment_correction_history',old['id']+':'+old['hash'])
    assert retained==old and 'consumer' not in retained
    assert store.record_get('web_work',faq['id'])['status']=='complete'
    assert len(faq_requests)==1
    assert_original_preserved(store,task['id'],parent,after,app,requests)
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['source','policy'])
async def test_new_binding_and_review_preserve_old_effect_and_replace_unstarted_parent(tmp_path,change):
    requests=[];directory=tmp_path/'data'
    store,engine,executor,gateway=open_runtime(directory,requests,pause='proposal')
    task=store.create_task('Write hello and retain correction evidence',['answer.txt contains hello'])
    paused=await run(engine,task['id']);assert paused['task']['status']=='configuration_required'
    parent,after=original_record(store,task['id'])
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    first_review=deepcopy(store.records('judgment_correction')[0])
    policy=None
    if change=='source':
        current=store.get_task(task['id'])
        store.append_instruction(task['id'],'Also preserve each exact correction record; the original hello remains required.',current['source_hash'])
    else:
        # A new catalog byte identity with exactly the same semantic policy and
        # approved Q18. This fixture exercises normal adoption, not an exemption.
        source=tmp_path/'policy';source.mkdir()
        (source/POLICY.name).write_bytes(POLICY.read_bytes()+b'\n')
        (source/'answer-Q18.json').write_bytes((POLICY.parent/'answer-Q18.json').read_bytes())
        policy=PolicyCatalog(source/POLICY.name)
        assert policy.hash!=engine.policy.hash
        assert policy.conditions==engine.policy.conditions and policy.rules==engine.policy.rules
    await engine.close();store.close()
    store,engine,executor,gateway=open_runtime(directory,requests,policy=policy,renew_review=True)
    result=await run(engine,task['id'])
    assert result['task']['status']=='completed',result['events'][-1]
    old=store.get_operation(parent['operation']['id'])
    assert old['status']=='superseded' and old['result'] is None and old['operation']==parent['operation']
    assert Path(result['task']['workspace'],'answer.txt').read_bytes()==b'hello'
    assert store.record_get('knowledge_application',app['id'])==app
    assert store.record_get('web_work',after['id'])['acquisition_result']==after['acquisition_result']
    reviews=[c for c in store.records('judgment_correction') if c['operation_id']==after['detail']['id']]
    assert len(reviews)==2 and {c['status'] for c in reviews}=={'resolved'}
    assert store.record_get('judgment_correction_history',first_review['id']+':'+first_review['hash'])==first_review
    assert all(store.record_get('web_judgment_resolution',c['id']) for c in reviews)
    assert not engine.web_judgments.correction_frontier(task['id'])
    assert sum(r['operation']['kind']=='plan_task' and r['status']=='cycle_complete' for r in store.operations(task['id']))==1
    if change=='policy':
        adoption=[r for r in store.operations(task['id']) if r['operation']['kind']=='adopt_policy']
        assert len(adoption)==1 and adoption[0]['status']=='cycle_complete'
        assert result['task']['policy_hash']==engine.policy.hash
    assert store.verify_events()
    await engine.close();store.close()


async def second_actual_review(store,engine,gateway,task_id,after):
    """An actual second reviewer/parent disposition of the same committed input."""
    first=next(r for r in store.records('applied_knowledge_review')
               if r['id'].startswith(after['detail']['id']+':web-learning-outcome'))
    bundle=next(p['bundle'] for _,phase,p in gateway.calls if phase=='web-learning-outcome_review')
    context=deepcopy(bundle['accepted_input'])
    context['prior_applied_reviews']=[deepcopy(first)]
    task=store.get_task(task_id)
    source=engine.web_judgments.applied_source(task,after['detail']['id'])
    gateway.renew_review=True
    reviewed=await engine._judge_applied_knowledge(task,source['operation'],'web-learning-outcome',
        after['applied_choices'],source['outcome'],source['result'],{'sources':[]},
        context=context,consumer=source['consumer'])
    assert reviewed['review']['id']!=first['review']['id']
    corrections=engine._pending_judgments(task_id)
    assert len(corrections)==2 and {c['operation_id'] for c in corrections}=={after['detail']['id']}
    assert all(engine.web_judgments.correction_source(task,c)['consumer']==source['consumer'] for c in corrections)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['ordinary','reopen','historical','historical-nested','historical-learning'])
async def test_same_acquisition_reviews_return_separately_without_replay(tmp_path,mode):
    requests=[];directory=tmp_path/'data'
    store,engine,executor,gateway=open_runtime(directory,requests,pause='proposal')
    task=store.create_task('Write hello and retain each reviewed correction',['answer.txt contains hello'])
    paused=await run(engine,task['id']);assert paused['task']['status']=='configuration_required'
    parent,after=original_record(store,task['id'])
    app=deepcopy(store.record_get('knowledge_application',task['id']+':'+after['detail']['id']))
    await second_actual_review(store,engine,gateway,task['id'],after)
    gateway.pause='first-return';gateway.paused=False
    confirm=engine.web_judgments.confirm_correction
    if mode.startswith('historical'):
        # Historical producer defect: a sibling used to suppress this receipt,
        # although the actual governed resolution finished all review/Learning.
        # No corrective operation, accepted review or effect is inserted by hand.
        def old_sibling_hold(owner,correction,resolution):
            if any(c['operation_id']==correction['operation_id'] for c in engine._pending_judgments(owner['id'])):return
            return confirm(owner,correction,resolution)
        engine.web_judgments.confirm_correction=old_sibling_hold
    paused=await run(engine,task['id']);assert paused['task']['status']=='configuration_required'
    first,=[c for c in store.records('judgment_correction') if c['status']=='resolved']
    resolution=store.get_operation(first['resolution_operation_id'])
    assert resolution['status']=='cycle_complete' and resolution['result']['status']=='succeeded'
    receipt=store.record_get('web_judgment_resolution',first['id'])
    assert bool(receipt)==(not mode.startswith('historical'))
    assert len(engine._pending_judgments(task['id']))==1
    assert not any(r['path']=='/dependent' for r in requests)
    engine.web_judgments.confirm_correction=confirm
    if mode=='historical-nested':
        # Explicit historical input: the already completed resolution has an
        # additional actual acquired/reviewed Web result still needing correction.
        gateway.nested='web'
        query=ResearchQuery(query='https://example.com/late-resolution-review',
            rationale='Retained historical follow-up on this exact resolution',private_data_excluded=True)
        with pytest.raises(WebCorrectionNeeded):
            await engine.web.collect(query.query,phase='post',task_id=task['id'],operation_id=resolution['operation']['id'],
                evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],resolution['operation'],stage,detail,query.model_dump()))
        own=[c for c in engine.web_judgments.correction_frontier(task['id'])
             if c['consumer']['parent_operation_id']==resolution['operation']['id']]
        assert len(own)==1
        effects=deepcopy(store.records('knowledge_application'));network_count=len(requests)
        engine._web_correction_boundary(task['id'])
        assert store.record_get('web_judgment_resolution',first['id']) is None
        assert store.records('knowledge_application')==effects and len(requests)==network_count
    elif mode=='historical-learning':
        gateway.nested='learning'
        await engine._judge_applied_knowledge(store.get_task(task['id']),resolution['operation'],
            'learning-outcome',resolution['post_bundle']['learning_choices'],resolution['knowledge'],
            resolution['result'],resolution['post_web'],
            context={'learning':resolution['post_bundle']['learning'],'selection':resolution['pre_bundle']['skills'],
                'before_review':resolution['pre_review'],'accepted_learning_review':resolution['post_review'],
                'prior_applied_reviews':[r for r in store.records('applied_knowledge_review')
                    if r['id'].startswith(resolution['operation']['id']+':learning-outcome')]},
            consumer={'kind':'operation','operation_id':resolution['operation']['id'],
                'operation_sha256':digest(resolution['operation'])})
        own,=[c for c in engine._pending_judgments(task['id']) if c['operation_id']==resolution['operation']['id']]
        gateway.pause='resolved-dispatch';gateway.paused=False
        pending_return=await run(engine,task['id'])
        assert pending_return['task']['status']=='configuration_required'
        own=store.record_get('judgment_correction',own['id'])
        assert own['status']=='resolved'
        own_resolution=store.get_operation(own['resolution_operation_id'])
        assert own_resolution['status']=='result_recorded'
        effects=deepcopy(store.records('knowledge_application'));network_count=len(requests)
        engine._web_correction_boundary(task['id'])
        assert store.record_get('web_judgment_resolution',first['id']) is None
        assert engine.web_judgments.has_pending_return(task['id'],resolution['operation']['id'])
        assert store.records('knowledge_application')==effects and len(requests)==network_count
    committed=deepcopy(store.records('knowledge_application'))
    actual_results={r['operation']['id']:deepcopy(r['result']) for r in store.operations(task['id']) if r['result']}
    effect=deepcopy(store.record_get('controller_effect',resolution['operation']['id']))
    if mode!='ordinary':
        await engine.close();store.close()
        store,engine,executor,gateway=open_runtime(directory,requests)
    else:gateway.pause=None
    # This historical nested graph reached finish/post_reviewed at the original
    # 120-second guard. Keep a finite allowance only for its final invocation;
    # completion, preserved effects and every once-only tail oracle still apply.
    result=await run(engine,task['id'],timeout=600 if mode=='historical-nested' else 120)
    assert result['task']['status']=='completed',result['events'][-1]
    returned=store.record_get('web_judgment_resolution',first['id'])
    assert returned['resolution_operation_id']==resolution['operation']['id']
    if receipt:assert returned==receipt
    assert store.record_get('controller_effect',resolution['operation']['id'])==effect
    for identity,actual in actual_results.items():assert store.get_operation(identity)['result']==actual
    for application in committed:assert store.record_get('knowledge_application',application['id'])==application
    roots=[c for c in store.records('judgment_correction') if c['operation_id']==after['detail']['id']]
    assert len(roots)==2 and all(store.record_get('web_judgment_resolution',c['id']) for c in roots)
    assert not engine.web_judgments.correction_frontier(task['id'])
    assert_original_preserved(store,task['id'],parent,after,app,requests)
    await engine.close();store.close()
