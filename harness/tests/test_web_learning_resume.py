"""Real temporary Store/Engine recovery; injected model faults and mock HTTP only."""
from collections import deque
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from policy_harness.bounded_judgments import BoundedJudgments
from policy_harness.models import AssessmentBatch, Disposition, Idea, Learning, PolicyError, Review
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.store import digest
from tests.test_bounded_judgments import PublicFixtureSettings
from tests.test_core import FixtureGateway, runtime
from tests.test_provider_evidence_recovery import FixtureCipher
from tests.test_web_recovery import collector, operation, reopen
from tests.fixture_response_transport import retained_reply


def assert_revision_facts(store, feedback, predecessor):
    """Compare the model's facts to actual originals, without using its builder."""
    if predecessor is None and feedback is None:
        return
    assert feedback['version']=='learning-current-v1'
    originals=[]
    for reference in feedback['sources']:
        assert reference['kind']=='learning_revision_source'
        record=store.record_get(reference['kind'],reference['id'])
        assert digest(record)==reference['sha256']
        assert record['id']==digest({k:v for k,v in record.items() if k!='id'})
        originals.append(record['value'])
    assert originals==([predecessor] if predecessor is not None else [])
    covered=[]
    for fact in feedback['facts']:
        assert fact['origins']
        for origin in fact['origins']:
            assert 0<=origin['source']<len(originals)
            path=origin['path']; value=originals[origin['source']]
            assert path.startswith('/')
            for token in path.lstrip('/').split('/'):
                token=token.replace('~1','/').replace('~0','~')
                value=value[int(token)] if isinstance(value,list) else value[token]
            if 'original_id' in origin:
                assert value['id']==origin['original_id']
            if isinstance(value,dict) and {'id','target','proposal','disposition','rationale'}<=set(value):
                value={k:v for k,v in value.items() if k!='id'}
            assert fact['value']==value
            assert not path.startswith(('/review_input','/disposition_input','/choices'))
            covered.append((origin['source'],path))
    def leaves(value,path):
        if isinstance(value,dict) and value:
            for key,item in value.items():yield from leaves(item,path+'/'+key)
        elif isinstance(value,list) and value:
            for index,item in enumerate(value):yield from leaves(item,path+'/'+str(index))
        else:yield path
    for index,original in enumerate(originals):
        # Every actual proposal, target assessment, opinion and disposition is
        # represented; a small summary or an original hash alone cannot pass.
        for field in ('learning','assessments','review','disposition',
                      'application_failure','proposal_failure','actual_application_failure'):
            if field not in original:continue
            value=original[field]
            if field.endswith('failure') and isinstance(value,dict):
                value={k:v for k,v in value.items() if k!='original'}
            for path in leaves(value,'/'+field):
                assert any(source==index and (path==prefix or path.startswith(prefix+'/'))
                           for source,prefix in covered),path


class ResumeGateway(FixtureGateway):
    def __init__(self, policy, *, fail_phase=None, fail_after_learning=1, verdicts=(), forbid_learning=False):
        super().__init__(policy)
        self.fail_phase=fail_phase
        self.fail_after_learning=fail_after_learning
        self.verdicts=deque(verdicts)
        self.forbid_learning=forbid_learning
        self.learning_count=0
        self.inputs=[]

    async def generate(self, role, phase, payload, schema):
        if schema in {AssessmentBatch, Review, Disposition, Learning}:
            self.inputs.append((phase,deepcopy(payload)))
        if phase=='web_acquisition_learning':
            assert not self.forbid_learning, 'A normal resume must reuse the saved Learning'
            self.learning_count+=1
        if phase==self.fail_phase and schema in {AssessmentBatch,Review,Disposition} and self.learning_count>=self.fail_after_learning:
            self.fail_phase=None
            await retained_reply(self,role,phase,payload,schema,None,status_code=500)
            raise AssertionError('The retained HTTP500 must fail')
        value,usage=await super().generate(role,phase,payload,schema)
        if phase=='web_learning_disposition' and self.verdicts:
            verdict=self.verdicts.popleft()
            value=value.model_copy(update={'verdict':verdict,'rationale':'Fixture actual '+verdict+' opinion disposition'})
        if schema is Disposition or (hasattr(self,'settings') and schema in {AssessmentBatch,Review}):
            return await retained_reply(self,role,phase,payload,schema,value,usage=usage)
        return value,usage


async def interrupted(tmp_path, phase, *, prior_revision=False):
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Retain the exact acquired source',['Original source, one actual application'])
    parent=operation();requests=[]
    gateway=ResumeGateway(engine.policy,fail_phase=phase,fail_after_learning=2 if prior_revision else 1,
                          verdicts=['revise'] if prior_revision else [])
    gateway.response_store=store;engine.gateway=gateway
    web=collector(store,requests)
    with pytest.raises(ProviderError,match='HTTP 500'):
        await web.collect('https://example.com/a',phase='pre',task_id=task['id'],operation_id=parent['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    saved=next(w for w in store.records('web_work') if w['stage']=='after')
    assert len(saved['learning_attempts'])==(2 if prior_revision else 1)
    assert len(requests)==1 and not store.records('knowledge_application')
    original=deepcopy(saved);exchanges=deepcopy(store.records('web_exchange'))
    failed_input=next(p for ph,p in reversed(gateway.inputs) if ph==phase)
    data_dir=store.data_dir;policy=engine.policy
    await engine.close();store.close()
    return task,original,exchanges,requests,failed_input,data_dir,policy


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['web_learning_choices_before','web_learning_review','web_learning_disposition'])
@pytest.mark.parametrize('prior_revision',[False,True])
async def test_provider_fault_resumes_saved_attempt_with_actual_predecessor(tmp_path,phase,prior_revision):
    task,original,exchanges,requests,failed_input,data_dir,policy=await interrupted(tmp_path,phase,prior_revision=prior_revision)
    gateway=ResumeGateway(policy,forbid_learning=True)
    store,engine=reopen(data_dir,policy,gateway=gateway)
    await engine.web_judgments.drain(task['id'])
    current=store.record_get('web_work',original['id'])
    assert current['status']=='complete'
    assert len(current['learning_attempts'])==len(original['learning_attempts'])
    assert current['acquisition_operation']==original['acquisition_operation']
    assert current['acquisition_result']==original['acquisition_result']
    assert store.records('web_exchange')==exchanges and len(requests)==1
    resumed_input=next(p for ph,p in gateway.inputs if ph==phase)
    assert resumed_input==failed_input
    predecessor=original['learning_attempts'][-2] if prior_revision else None
    if phase!='web_learning_disposition':
        assert_revision_facts(store,resumed_input['actual_revision_feedback'],predecessor)
    assert_revision_facts(store,current['learning_attempts'][-1]['review_input']['actual_revision_feedback'],predecessor)
    for key,value in original['learning_attempts'][-1].items():
        if key!='learning':assert current['learning_attempts'][-1][key]==value
    assert len(store.records('knowledge_application'))==1
    applications=deepcopy(store.records('knowledge_application'))
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application')==applications and len(requests)==1
    assert store.verify_events()
    await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['web_learning_choices_before','web_learning_review','web_learning_disposition'])
async def test_first_real_revise_after_resume_produces_new_reviewed_attempt(tmp_path,phase):
    task,original,exchanges,requests,_,data_dir,policy=await interrupted(tmp_path,phase)
    gateway=ResumeGateway(policy,verdicts=['revise','proceed'])
    store,engine=reopen(data_dir,policy,gateway=gateway)
    await engine.web_judgments.drain(task['id'])
    current=store.record_get('web_work',original['id'])
    assert current['status']=='complete' and len(current['learning_attempts'])==2
    previous=current['learning_attempts'][0]
    assert previous['disposition']['verdict']=='revise'
    proposals=[p for ph,p in gateway.inputs if ph=='web_acquisition_learning']
    assert len(proposals)==1
    assert_revision_facts(store,proposals[0]['actual_revision_feedback'],previous)
    required={i['id']:(i['proposal'],i['target']) for i in previous['learning']['ideas']}
    retained={i['id']:(i['proposal'],i['target']) for i in current['applied_learning']['ideas']}
    assert required.items()<=retained.items()
    assert_revision_facts(store,current['learning_attempts'][1]['review_input']['actual_revision_feedback'],previous)
    assert current['acquisition_result']==original['acquisition_result']
    assert store.records('web_exchange')==exchanges and len(requests)==1
    assert len(store.records('knowledge_application'))==1
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_genuine_hold_after_resume_preserves_uncommitted_attempt(tmp_path):
    task,original,exchanges,requests,_,data_dir,policy=await interrupted(tmp_path,'web_learning_disposition')
    gateway=ResumeGateway(policy,verdicts=['hold'],forbid_learning=True)
    store,engine=reopen(data_dir,policy,gateway=gateway)
    with pytest.raises(PolicyError,match='Web learning held'):
        await engine.web_judgments.drain(task['id'])
    current=store.record_get('web_work',original['id'])
    assert current['learning_attempts'][0]['disposition']['verdict']=='hold'
    assert not store.records('knowledge_application')
    assert store.records('web_exchange')==exchanges and len(requests)==1
    with pytest.raises(PolicyError,match='Web learning held'):
        await engine.web_judgments.drain(task['id'])
    assert len([ph for ph,_ in gateway.inputs if ph=='web_learning_disposition'])==1
    await engine.close();store.close()


@pytest.mark.asyncio
async def test_resume_retains_genuine_prior_acquisition_without_current_self(tmp_path):
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Keep distinct acquired evidence',['Retain genuine prior evidence across restart'])
    requests=[];web=collector(store,requests);first=operation()
    await web.collect('https://example.com/earlier',phase='pre',task_id=task['id'],operation_id=first['id'],
        evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],first,stage,detail))
    prior=next(w for w in store.records('web_work') if w['stage']=='after')
    assert prior['status']=='complete' and len(store.records('knowledge_application'))==1
    prior_applications=deepcopy(store.records('knowledge_application'))
    # Both exchanges belong to the same collection owner. Unrelated task
    # history remains stored but is not an input dependency of this judgment.
    parent=first;gateway=ResumeGateway(engine.policy,fail_phase='web_learning_review')
    gateway.response_store=store;engine.gateway=gateway
    with pytest.raises(ProviderError,match='HTTP 500'):
        await web.collect('https://example.com/current',phase='pre',task_id=task['id'],operation_id=parent['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    original=next(w for w in store.records('web_work') if w['stage']=='after' and w['id']!=prior['id'])
    exchanges=deepcopy(store.records('web_exchange'))
    failed_input=next(p for ph,p in gateway.inputs if ph=='web_learning_review')
    evidence,=failed_input['prior_acquisition_evidence']
    assert evidence['id']==prior['id'] and evidence['disposition']==prior['disposition']
    assert evidence['acquisition']['id']==prior['detail']['id']
    reused=evidence['reused_judgment']
    assert reused['work_id']==prior['id'] and reused['work_sha256']==digest(prior)
    assert reused['acquisition_sha256']==digest(prior['detail'])
    assert reused['source_hash']==task['source_hash'] and reused['policy_hash']==engine.policy.hash
    expected_prior=deepcopy(failed_input['prior_acquisition_evidence'])
    assert len(requests)==2 and store.records('knowledge_application')==prior_applications
    data_dir=store.data_dir;policy=engine.policy
    await engine.close();store.close()
    resumed=ResumeGateway(policy,forbid_learning=True);store,engine=reopen(data_dir,policy,gateway=resumed)
    await engine.web_judgments.drain(task['id'])
    actual_input=next(p for ph,p in resumed.inputs if ph=='web_learning_review')
    assert actual_input==failed_input
    assert actual_input['prior_acquisition_evidence']==expected_prior
    current=store.record_get('web_work',original['id'])
    assert current['status']=='complete' and len(current['learning_attempts'])==1
    assert current['learning_attempts'][0]['learning']==original['learning_attempts'][0]['learning']
    assert current['acquisition_result']==original['acquisition_result']
    assert store.record_get('web_work',prior['id'])==prior
    assert store.records('web_exchange')==exchanges and len(requests)==2
    applications=store.records('knowledge_application')
    assert len(applications)==2 and all(a in applications for a in prior_applications)
    await engine.web_judgments.drain(task['id'])
    assert store.records('knowledge_application')==applications and len(requests)==2
    assert store.verify_events()
    await engine.close();store.close()


class PackedResumeGateway(ResumeGateway):
    """Configured production packing/storage, explicit synthetic model replies.

    legacy_order recreates the former packer at the actual call boundary. Only
    the fixture response is synthetic; Store, Knowledge and Web recovery run
    their normal paths. No real model or paid request is used.
    """
    after_phase = 'web-learning-outcome_choices_after'

    def __init__(self, policy, *, legacy_order=False, stop_count=None, forbid_learning=False):
        super().__init__(policy, forbid_learning=forbid_learning)
        self.settings = PublicFixtureSettings()
        self.settings.values.update(base_url='https://fixture.invalid/v1', model='fixture-model',
            review_model='', api_mode='compatible', reasoning_effort=None,
            user_agent='cache-recovery-fixture', max_output_tokens=32768)
        self.packer = ModelGateway(self.settings, policy, response_protector=FixtureCipher())
        self.legacy_order = legacy_order
        self.stop_count = stop_count
        self.after_calls = []
        self.after_inputs = []

    def _messages(self, role, phase, payload, schema):
        messages = self.packer._messages(role, phase, payload, schema)
        if self.legacy_order:
            messages[1]['content'] = 'PHASE INPUT (data):\n' + json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return messages

    async def failure(self, role, phase, payload, schema, *, truncated):
        await retained_reply(self, role, phase, payload, schema, None,
            content='' if truncated else None, finish_reason='length' if truncated else 'stop',
            status_code=200 if truncated else 503)
        raise AssertionError('The retained interrupted response must fail')

    async def generate(self, role, phase, payload, schema):
        # Pack every actual judgment, so this cannot pass through the unconfigured
        # fixture branch whose messages_sha256 is None.
        self._messages(role, phase, payload, schema)
        if schema is AssessmentBatch and phase == self.after_phase:
            ids = [target['id'] for target in payload['targets']]
            self.after_calls.append(ids)
            self.after_inputs.append(deepcopy(payload))
            if len(ids) == 29:
                await self.failure(role, phase, payload, schema, truncated=True)
            if len(ids) == self.stop_count:
                self.stop_count = None
                await self.failure(role, phase, payload, schema, truncated=False)
        value, usage = await super().generate(role, phase, payload, schema)
        if schema is Learning and phase == 'web_acquisition_learning':
            # Preserve every original candidate while supplying 25 distinct
            # meanings. Repeated IDs for one meaning must not inflate the
            # 29-choice/14+15-page capacity and recovery case.
            meanings = {json.dumps({k: v for k, v in idea.model_dump().items() if k != 'id'},
                                   sort_keys=True) for idea in value.ideas}
            assert len(meanings) <= 25
            additions = [Idea(id='cache-idea-' + str(index), target='task',
                proposal='Consider this separately retained fixture idea ' + str(index),
                disposition='investigate', rationale='Synthetic proposal; use and benefit remain unobserved')
                for index in range(25 - len(meanings))]
            value = value.model_copy(update={'ideas': [*value.ideas, *additions]})
            return await retained_reply(self,role,phase,payload,schema,value,usage=usage)
        return value, usage


def legacy_after_receipts(store, task, policy):
    """Represent the prior on-disk format using captured old-packer requests.

    This fixture removes only the newly introduced configuration field and
    computes the former documented key. It never invents wire hashes, response
    bytes, settings, a typed success, Knowledge, or application effects.
    """
    replacements = {}
    for record in store.records('bounded_model_call'):
        if record['phase'] != PackedResumeGateway.after_phase:
            continue
        old_key = record['key']
        record['measurement'].pop('configuration')
        record['key'] = digest({'task_id': task['id'], 'source_hash': task['source_hash'],
            'policy_hash': policy.hash, 'phase': record['phase'], 'role': task['actor'],
            'schema': AssessmentBatch.model_json_schema(), 'payload': record['payload'],
            'measurement': record['measurement']})
        store.record('bounded_model_call', record['id'], record)
        for kind in ('bounded_model_truncated', 'bounded_model_completed'):
            if store.record_get(kind, old_key):
                store.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, old_key))
                store.record(kind, record['key'], record)
        replacements[record['id']] = {'id': record['id'], 'key': record['key'], 'sha256': digest(record)}
    for split in store.records('bounded_output_split'):
        ref = replacements.get((split.get('failed_call') or {}).get('id'))
        if ref:
            old_id = split.pop('id')
            split['failed_call'] = ref
            split['id'] = digest(split)
            store.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('bounded_output_split', old_id))
            store.record('bounded_output_split', split['id'], split)


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy,stop_count', [(False, 14), (False, 15), (True, 14)])
async def test_configured_after_application_resume_keeps_29_choices_and_pending_pages(tmp_path, legacy, stop_count):
    store, engine, _, _ = runtime(tmp_path)
    task = store.create_task('Retain the original acquired evidence', ['Complete every post-application choice once'])
    gateway = PackedResumeGateway(engine.policy, legacy_order=legacy, stop_count=stop_count)
    gateway.response_store = store
    engine.gateway = gateway
    engine.bounded_judgments = BoundedJudgments(engine)
    parent = operation(); requests = []; web = collector(store, requests)
    with pytest.raises(ProviderError, match='HTTP 503'):
        await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id=parent['id'],
            evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    original = next(work for work in store.records('web_work') if work['stage'] == 'after')
    assert len(original['applied_choices']) == 29
    before_choice_input = next(payload for phase, payload in gateway.inputs if phase == 'web_learning_choices_before')
    attempt = original['learning_attempts'][original['application_intent']['accepted_attempt_index']]
    assert before_choice_input['targets'] == original['applied_choices']
    assert [row['target_id'] for row in attempt['assessments']] == [target['id'] for target in original['applied_choices']]
    expected_learning = deepcopy(before_choice_input['learning'])
    assert len({json.dumps({k:v for k,v in idea.items() if k!='id'},sort_keys=True)
                for idea in expected_learning['ideas']}) == 25
    required_ideas=[*expected_learning['ideas'],*[idea for row in attempt['assessments'] for idea in row['assessment']['ideas']]]
    actual_ideas=original['applied_learning']['ideas']
    assert len({idea['id'] for idea in actual_ideas})==len(actual_ideas)
    assert {idea['id']:idea for idea in actual_ideas}=={idea['id']:idea for idea in required_ideas}
    assert all(idea in actual_ideas for idea in required_ideas)
    expected_learning['ideas']=deepcopy(actual_ideas)
    assert original['applied_learning'] == expected_learning == attempt['learning']
    episode = store.record_get('episode', original['learning_applied']['episode_id'])
    assert episode['learning'] == expected_learning
    idea_records = {}
    for idea in expected_learning['ideas']:
        identity = digest({'task': task['id'], 'idea': idea})[:32]
        record = store.record_get('idea', identity)
        assert record['source_episode'] == episode['id'] and record['task_id'] == task['id']
        assert {field: record[field] for field in idea if field != 'id'} == {field: value for field, value in idea.items() if field != 'id'}
        idea_records[identity] = deepcopy(record)
    assert len(store.records('knowledge_application')) == len(requests) == 1
    assert list(gateway.after_inputs[0]['actual_result']) != sorted(gateway.after_inputs[0]['actual_result'])
    if legacy:
        legacy_after_receipts(store, task, engine.policy)
    failed = next(record for record in store.records('bounded_model_truncated') if record['phase'] == gateway.after_phase)
    assert failed['measurement']['messages_sha256']
    retained = {kind: deepcopy(store.records(kind)) for kind in
                ('knowledge_application', 'web_exchange', 'bounded_model_call', 'model_response', 'bounded_output_split')}
    completed = [record for record in store.records('bounded_model_completed') if record['phase'] == gateway.after_phase]
    assert len(completed) == (1 if stop_count == 15 else 0)
    before_input = gateway.after_inputs[0]
    data_dir = store.data_dir; policy = engine.policy
    await engine.close(); store.close()

    resumed = PackedResumeGateway(policy, forbid_learning=True)
    store, engine = reopen(data_dir, policy, gateway=resumed)
    await engine.web_judgments.drain(task['id'])
    current = store.record_get('web_work', original['id'])
    assert current['status'] == 'complete'
    assert current['applied_learning'] == original['applied_learning']
    assert current['learning_applied'] == original['learning_applied']
    assert current['learning_attempts'] == original['learning_attempts']
    assert current['application_intent'] == original['application_intent']
    assert current['acquisition_result'] == original['acquisition_result']
    expected = original['applied_choices']
    expected_pending = [expected[14:]] if stop_count == 15 else [expected[:14], expected[14:]]
    assert resumed.after_calls == [[target['id'] for target in page] for page in expected_pending]
    assert all(len(page) != 29 for page in resumed.after_calls)
    # Compare full caller context, not just a semantic helper's digest.
    for actual in resumed.after_inputs:
        assert {key: value for key, value in actual.items() if key not in {'targets', 'target'}} == {
            key: value for key, value in before_input.items() if key not in {'targets', 'target'}}
    reviews = [record for record in store.records('applied_knowledge_review') if record['consumer'].get('kind') == 'web_acquisition']
    review = next(record for record in reviews if record['outcome_hash'] == digest(original['learning_applied']))
    assert [row['target_id'] for row in review['assessments']] == [target['id'] for target in expected]
    for record in completed:
        for saved_assessment in record['result']['assessments']:
            actual = next(row for row in review['assessments'] if row['target_id'] == saved_assessment['target_id'])
            assert actual == saved_assessment
    assert store.record_get('episode', episode['id']) == episode
    assert {identity: store.record_get('idea', identity) for identity in idea_records} == idea_records
    assert store.records('knowledge_application') == retained['knowledge_application']
    assert store.records('web_exchange') == retained['web_exchange'] and len(requests) == 1
    for kind in ('bounded_model_call', 'model_response', 'bounded_output_split'):
        for record in retained[kind]:
            assert store.record_get(kind, record['id']) == record
    assert store.record_get('bounded_model_truncated', failed['key']) == failed
    if legacy:
        split = next(record for record in store.records('bounded_output_split') if record.get('basis') == 'legacy-partition-only')
        assert split['failed_call']['key'] == failed['key']
        assert split['current_measurement']['model_input_sha256'] == failed['measurement']['model_input_sha256']
        assert split['current_measurement']['messages_sha256'] != failed['measurement']['messages_sha256']
        assert split['wire_equality_claimed'] is False and split['old_configuration_complete'] is False
    calls = deepcopy(resumed.after_calls)
    await engine.web_judgments.drain(task['id'])
    assert resumed.after_calls == calls and store.records('knowledge_application') == retained['knowledge_application']
    assert store.verify_events()
    await engine.close(); store.close()
