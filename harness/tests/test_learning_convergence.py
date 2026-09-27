"""Configured HTTP fixtures over real Engine/SQLite/Web/Knowledge consumers.

Historical setup below deliberately uses the old unmarked input writer. It
does not claim to reproduce the live model or explain its HTTP500 response.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from policy_harness import models as m
from policy_harness import web_judgments as web_module
from policy_harness.bounded_judgments import BoundedJudgments, ContextObservation
from policy_harness.decisions import targets as decision_targets, operation_targets
from policy_harness.engine import Engine, RevisionNeeded
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.learning_projection import VERSION, current_input, groups, merge_ideas, revision_feedback
from policy_harness.providers import ProviderError, WebCollector
from policy_harness.semantic_wire import projected_input, wire_schema
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, runtime
from tests.test_knowledge import learning, operation
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_semantic_wire_contract import install, assert_wire_records, source_choice
from tests.test_web_correction_flow import CorrectionGateway
from tests.test_web_recovery import collector, operation as web_parent, Settings


class Historical26Bounded(BoundedJudgments):
    """The candidate26 successful/failing writer, before generic feedback_trace.

    Writes the original measured call, actual Engine wire trace and transport
    event. No modern receipt is manufactured and then stripped of its binding.
    The isolated fixture disables only the later generic Engine router while
    this old writer invokes the otherwise unchanged real send/response path.
    """
    async def _call(self,task_id,phase,payload,schema,*,role=None,**kw):
        if schema not in (m.Review,m.AssessmentBatch,ContextObservation):
            return await super()._call(task_id,phase,payload,schema,role=role,**kw)
        measured=self.measure(task_id,phase,payload,schema,role=role)
        task=self.e.store.get_task(task_id)
        key=self._receipt_key(task,phase,payload,schema,role or task['actor'],measured)
        saved=self.e.store.record_get('bounded_model_completed',key)
        if saved:return schema.model_validate(saved['result'])
        record={'id':uuid4().hex,'key':key,'task_id':task_id,'phase':phase,'source_hash':task['source_hash'],
            'policy_hash':self.e.policy.hash,'payload':deepcopy(payload),'measurement':measured,'status':'started'}
        self.e.store.record('bounded_model_call',record['id'],record)
        record['wire_trace']={'initial_capture':measured['semantic_wire']}
        router=self.e._wire_recovery
        self.e._wire_recovery=lambda selected:None if selected is schema else router(selected)
        try:
            value=await self.raw_call(task_id,phase,payload,schema,role=role,wire_trace=record['wire_trace'],
                expected_request_configuration={k:measured[k] for k in ('configuration','context_tokens','reserved_output_tokens')})
        except BaseException as error:
            record.update(status='failed' if isinstance(error,Exception) else 'unobserved',
                first_fault={'type':type(error).__name__,'message':str(error)})
            if isinstance(error,ProviderError):record['metadata']=deepcopy(error.metadata)
            actual=getattr(error,'controller_model_input',None)
            if actual is not None:record.update(actual_model_input=deepcopy(actual),actual_model_input_sha256=digest(actual))
            self.e.store.record('bounded_model_call',record['id'],record)
            if self._output_truncated(error):
                self.e.store.record('bounded_model_truncated',key,record);error.bounded_call_ref=self._call_ref(record)
            raise
        finally:self.e._wire_recovery=router
        self._validate(schema,value,payload,task_id=task_id)
        record.update(status='succeeded',result=value.model_dump())
        self._wire_event(record,schema)
        self.e.store.record('bounded_model_call',record['id'],record)
        self.e.store.record('bounded_model_completed',key,record)
        return value

    def _retain_assessments(self,*args):
        # This aggregate association did not exist in the historical writer.
        pass


async def historical28_post(engine,state):
    """Candidate28 ordinary producer shape, with real downstream calls/records.

    Kept local to the fixture: the current runtime never disables provenance.
    Its actual old bundle lacks later contexts, targets and proposal receipts.
    """
    store=engine.store;row=store.get_operation(state['operation_id']);task=store.get_task(state['task_id'])
    op=row['operation'];feedback=engine._learning_revision_feedback(row)
    web=await engine._research(task['id'],op,'post',{'result':row['result'],'decisions':op['decisions'],'pre':row['pre_bundle']})
    targets=row['pre_bundle'].get('targets') or operation_targets(op,row['pre_bundle']['skills'])
    assessments=await engine._assess_targets(task['id'],'post_assessment',targets,{'operation':op,'pre':row['pre_bundle'],'result':row['result'],'web':web,
        'previous_post_draft':row.get('post_draft'),'actual_revision_feedback':feedback,'timing':'after the actual operation result; compare expected versus observed, preserve unknowns'})
    attempt={'id':uuid4().hex,'task_id':task['id'],'operation_id':op['id'],'policy_hash':engine.policy.hash,
        'source_hash':task['source_hash'],'created_at':m.now(),'assessments':assessments,'status':'assessed'}
    store.record('post_learning_attempt',attempt['id'],attempt)
    required=engine._post_required_ideas(row,assessments)
    value=await engine.bounded_judgments.learning_proposal(task['id'],'learning_proposal',{'operation':op,'result':row['result'],'pre':row['pre_bundle'],'post':assessments,
        'actual_revision_feedback':feedback,'required_ideas':required,'knowledge':engine._knowledge_context(task,op),'web':web,
        'instruction':'Classify every actual result into real create/use/improve/organize/merge/retire Skill work. Propose concrete updates with source, applicability and next trigger. Unknown recurrence produces provisional knowledge. Do not claim use of an unselected Skill or verified benefit from registration. Assess/dispose all pre/post ideas; adopted feasible workflow changes need an actual next operation and verification/use, not a promise.'})
    engine._validate_ideas(required,value)
    attempt.update(learning=value.model_dump(),status='learning_proposed');store.record('post_learning_attempt',attempt['id'],attempt)
    choices=decision_targets('learning',value)
    assessed=await engine._assess_targets(task['id'],'learning_choices_before',choices,{'operation':op,'result':row['result'],'learning':value.model_dump(),'timing':'before actual knowledge mutation'})
    value.ideas.extend(m.Idea.model_validate(i) for a in assessed for i in a['assessment']['ideas'])
    bundle={'operation':op,'result':row['result'],'assessments':assessments,'learning':value.model_dump(),'learning_choices':choices,'choice_assessments':assessed}
    composition=engine.bounded_judgments.learning_review_context(task['id'],op['id'],value)
    if composition:bundle['learning_composition']=composition
    attempt.update(learning=value.model_dump(),choice_assessments=assessed,status='awaiting_review');store.record('post_learning_attempt',attempt['id'],attempt)
    reviewed=await engine._review_bundle(task['id'],op,'post',bundle,web)
    attempt.update(review=reviewed,status='reviewed');store.record('post_learning_attempt',attempt['id'],attempt)
    if reviewed['disposition']['verdict']!='proceed':
        store.update_operation(op['id'],post_draft=bundle,post_review=reviewed)
        raise RevisionNeeded('Historical post verdict requires revision')
    store.update_operation(op['id'],status='post_reviewed',post_bundle=bundle,post_web=web,post_review=reviewed)


def idea(identity, **changes):
    return m.Idea(id=identity, target='task', proposal='Check the actual retained result before another effect',
        disposition='reject', rationale='The requested result is already preserved; no extra effect is needed',
        **changes).model_dump()


def at(value, pointer):
    for part in pointer.strip('/').split('/') if pointer else []:
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


@pytest.mark.asyncio
async def test_current_frontier_keeps_exact_opinion_evidence_without_old_assessment_replay(tmp_path):
    from policy_harness import semantic_wire as wire
    from policy_harness.learning_projection import validate_feedback, _historical_revision_feedback, _frontier_v1_revision_feedback
    store, engine, _, _ = runtime(tmp_path)
    task=store.create_task('Use current result and preserve every original', ['No repeated historical judgment'])
    op=operation('file_list',args={})
    result=m.OperationResult(operation_id=op['id'],status='succeeded',data={'entries':[]}).model_dump()
    old_learning=learning().model_dump();old_learning['outcome_summary']='Original result requires this exact correction'
    current_learning=deepcopy(old_learning);current_learning['outcome_summary']='Current proposal, still awaiting review'
    try:
        install(store,engine)
        original=engine._model_input(task['id'],'frontier-review',current_input(
            {'operation':op,'result':result,'learning':old_learning}),m.Review,role='reviewer')
        ref=engine._wire_capture(task['id'],'frontier-review',original,m.Review,'reviewer')
        captured=store.record_get('semantic_wire_request',ref['id'])
        slot=next(i for i,row in enumerate(captured['association']['source_slots']) if row['pointer']=='/learning/outcome_summary')
        evidence=wire._source_reference(captured,store,{'source_slot':slot})
        opinion={'id':'exact-opinion','observation':'The old wording conflicts with the actual result',
            'rationale':'Reconsider this specified original, not unrelated past assessments',
            'evidence_refs':[json.dumps(evidence)]}
        prior={'learning':old_learning,'assessments':[{'target_id':'old-target',
            'assessment':{'rationale':'OLD-ALREADY-REVIEWED-ASSESSMENT'}}],
            'review':{'summary':'Independent finding','opinions':[opinion], 'additional_context':'Unique stage observation'},
            'disposition':{'verdict':'revise','rationale':'Repair the captured issue\nCorrection references: '+json.dumps([evidence]),
                'opinion_responses':[{'opinion_id':'exact-opinion','disposition':'accept','rationale':'The identified correction is required'}],
                'web_refs':['retained-acquisition']}}
        current={'learning':current_learning,'assessments':[{'target_id':'new-target',
            'assessment':{'rationale':'CURRENT-ASSESSMENT'}}]}
        old=_historical_revision_feedback(store,task['id'],op['id'],[prior,current])
        validate_feedback(store,task['id'],op['id'],old)
        v1=_frontier_v1_revision_feedback(store,task['id'],op['id'],[prior,current])
        validate_feedback(store,task['id'],op['id'],v1)
        old_input=engine._model_input(task['id'],'retained-v1',current_input(
            dict(proposal_input(op,result), actual_revision_feedback=v1)),m.Learning)
        old_ref=engine._wire_capture(task['id'],'retained-v1',old_input,m.Learning,task['actor'])
        old_capture=deepcopy(store.record_get('semantic_wire_request',old_ref['id']))
        old_message=wire.projected_input(old_capture)
        feedback=revision_feedback(store,task['id'],op['id'],[prior,current])
        validate_feedback(store,task['id'],op['id'],feedback)
        assert 'OLD-ALREADY-REVIEWED-ASSESSMENT' not in json.dumps(feedback)
        assert 'CURRENT-ASSESSMENT' in json.dumps(feedback)
        assert feedback['opinion_frontier'][0]['state']=='unresolved_or_requires_changed_proposal_review'
        assert feedback['fact_projection']=='current-frontier-v2'
        headers={row['field']:row['value'] for row in feedback['stage_judgments']}
        assert headers['review']=={k:v for k,v in prior['review'].items() if k!='opinions'}
        assert headers['disposition']=={k:v for k,v in prior['disposition'].items() if k!='opinion_responses'}
        assert not any(f['field'] in {'review','disposition'} for f in feedback['facts'])
        assert next(iter(feedback['opinion_evidence'].values()))['value']==old_learning['outcome_summary']
        assert [store.record_get(r['kind'],r['id'])['value'] for r in feedback['sources']]==[prior,current]
        tampered=deepcopy(feedback);next(iter(tampered['opinion_evidence'].values()))['value']='different original'
        with pytest.raises(m.PolicyError,match='facts or lineage changed'):
            validate_feedback(store,task['id'],op['id'],tampered)
        assert engine.bounded_judgments._later_learning_revision(
            current_input({'operation':op,'actual_revision_feedback':old}),
            current_input({'operation':op,'actual_revision_feedback':feedback}))
        assert engine.bounded_judgments._later_learning_revision(
            current_input({'operation':op,'actual_revision_feedback':v1}),
            current_input({'operation':op,'actual_revision_feedback':feedback}))
        assert not engine.bounded_judgments._later_learning_revision(
            current_input({'operation':op,'actual_revision_feedback':feedback}),
            current_input({'operation':op,'actual_revision_feedback':v1}))
        validate_feedback(store,task['id'],op['id'],v1)
        assert engine._wire_capture(task['id'],'retained-v1',old_input,m.Learning,task['actor'])==old_ref
        assert store.record_get('semantic_wire_request',old_ref['id'])==old_capture
        assert wire.projected_input(wire.load(store,old_ref,old_input,m.Learning,task['actor'],'retained-v1'))==old_message
    finally:
        await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('same_current',[True,False])
async def test_current_frontier_wire_supplies_equal_original_once_with_all_aliases(tmp_path,same_current):
    from policy_harness import semantic_wire as wire
    from policy_harness.learning_projection import validate_feedback
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Read the current result and exact historical evidence',['Keep all distinct scopes and values'])
    op=operation('file_list',args={})
    result=m.OperationResult(operation_id=op['id'],status='succeeded',data={'entries':[]}).model_dump()
    parent=dict(deepcopy(op),rationale='Exact original parent meaning. '*40)
    attempts=[];refs=[]
    try:
        install(store,engine)
        for phase in ('first-original','second-original'):
            original=engine._model_input(task['id'],phase,current_input(
                {'operation':op,'result':result,'parent_operation':parent,'learning':learning().model_dump()}),m.Review,role='reviewer')
            ref=engine._wire_capture(task['id'],phase,original,m.Review,'reviewer')
            captured=store.record_get('semantic_wire_request',ref['id'])
            slot=next(i for i,row in enumerate(captured['association']['source_slots']) if row['pointer']=='/parent_operation')
            evidence=wire._source_reference(captured,store,{'source_slot':slot});refs.append(evidence)
            attempts.append({'learning':learning().model_dump(),
                'review':{'summary':'Review at '+phase,'opinions':[{'id':phase,'observation':'Inspect original parent',
                    'rationale':'Same value, separate captured scope','evidence_refs':[json.dumps(evidence)]}]},
                'disposition':{'verdict':'revise','rationale':'Correction references: '+json.dumps([evidence]),
                    'opinion_responses':[{'opinion_id':phase,'disposition':'accept','rationale':'Requires review of the changed proposal'}],
                    'web_refs':[phase]}})
        feedback=revision_feedback(store,task['id'],op['id'],attempts)
        validate_feedback(store,task['id'],op['id'],feedback)
        evidence_key,evidence=next(iter(feedback['opinion_evidence'].items()))
        assert len(feedback['opinion_evidence'])==1 and evidence['references']==refs and evidence['value']==parent
        assert all(row['evidence_keys']==[evidence_key] for row in feedback['opinion_frontier'])
        actual_parent=parent if same_current else dict(parent,rationale='Different current parent meaning')
        payload=engine._model_input(task['id'],'current-frontier-wire',current_input(dict(
            proposal_input(op,result),parent_operation=actual_parent,actual_revision_feedback=feedback)),m.Learning)
        ref=engine._wire_capture(task['id'],'current-frontier-wire',payload,m.Learning,task['actor'])
        captured=store.record_get('semantic_wire_request',ref['id'])
        assert captured['presentation_revision']==wire.APPLICATION_CONTRACT
        sent=wire.projected_input(captured);expanded=deepcopy(sent)
        value=sent['actual_revision_feedback']['opinion_evidence'][evidence_key]['value']
        if same_current:
            assert value=={'delivered_value_ref':'/parent_operation','value_sha256':digest(parent)}
        else:
            assert value==parent and sent['parent_operation']==actual_parent
        for reference in sent['semantic_response']['shared_learning_values']['references']:
            original=at(sent,reference['value_pointer'])
            assert digest(original)==reference['value_sha256']
            assert at(sent,reference['pointer'])=={'delivered_value_ref':reference['value_pointer'],
                                                  'value_sha256':reference['value_sha256']}
            container,key=reference['pointer'].rsplit('/',1)
            key=key.replace('~1','/').replace('~0','~')
            target=at(expanded,container)
            target[int(key) if isinstance(target,list) else key]=deepcopy(original)
        assert expanded['actual_revision_feedback']==feedback
        assert store.record_get('semantic_wire_request',ref['id'])['canonical_input']==payload
        assert all(row['state']=='unresolved_or_requires_changed_proposal_review' for row in feedback['opinion_frontier'])
    finally:
        await engine.close();store.close()


@pytest.mark.parametrize('metadata,expected',[
    ({'http_status':500},True),({'http_status':599},True),({'http_status':429},True),
    ({'http_status':499},False),({'http_status':600},False),({'http_status':200},False),
    ({'http_status':True},False),({'http_status':'500'},False),
    ({'http_status':500,'truncated':True},False),
    ({'http_status':500,'request_effect':'response-unobserved'},False),({},False),(None,False)])
def test_observed_retryable_failure_is_only_exact_http_eligibility(metadata,expected):
    assert BoundedJudgments.observed_retryable_failure({'metadata':metadata}) is expected


@pytest.mark.asyncio
async def test_current_projection_preserves_all_meanings_lineage_and_rejects_tamper_at_real_call(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Keep actual findings without recursive prompt history', ['Every original decision remains reachable'])
    op = operation('file_list', args={})
    result = m.OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    a = idea('a'); b = dict(a, id='b'); conflict = dict(a, id='c', rationale='A different unresolved observation')
    first = {'learning': learning(ideas=[a, b, conflict]).model_dump(),
        'review': {'summary': 'Real fixture observation', 'opinions': [{'id': 'opinion', 'observation': 'Keep evidence', 'rationale': 'Exact bytes'}]},
        'disposition': {'verdict': 'revise', 'rationale': 'Improve the proposed summary'},
        'review_input': {'self_container': 'not a new observation'},
        'proposal_failure': {'message': 'First actual failure', 'original': {'prior_container': 'retained only as source'}}}
    second = deepcopy(first)
    second['learning']['outcome_summary'] = 'A distinct revised observation'
    second['review_input'] = {'actual_revision_feedback': deepcopy(first)}
    try:
        assert revision_feedback(store, task['id'], op['id'], []) is None
        feedback = revision_feedback(store, task['id'], op['id'], [first, second])
        assert len(feedback['sources']) == 2
        for source, original in zip(feedback['sources'], [first, second]):
            record = store.record_get(source['kind'], source['id'])
            assert record['value'] == original and digest(record) == source['sha256']
        for fact in feedback['facts']:
            for origin in fact['origins']:
                original = [first, second][origin['source']]
                value = at(original, origin['path'])
                if origin.get('original_id') and {'target', 'proposal', 'disposition', 'rationale'} <= set(value):
                    assert value['id'] == origin['original_id']; value = {k:v for k,v in value.items() if k != 'id'}
                assert value == fact['value']
        facts = json.dumps(feedback['facts'])
        assert 'self_container' not in facts and 'prior_container' not in facts and 'actual_revision_feedback' not in facts
        assert 'First actual failure' in facts and 'A different unresolved observation' in facts
        same = [f for f in feedback['facts'] if f['field'] == 'learning.ideas' and f['value']['rationale'] == a['rationale']]
        assert len(same) == 1 and {x['original_id'] for x in same[0]['origins']} == {'a', 'b'}
        transport = install(store, engine)
        payload = current_input(dict(proposal_input(op, result), required_ideas=[a, b, conflict], actual_revision_feedback=feedback))
        value = await engine.bounded_judgments.learning_proposal(task['id'], 'projection-proposal', payload)
        assert [x.id for x in value.ideas] == ['a', 'b', 'c']
        sent = next(c for c in transport.calls if c['schema'] is m.Learning)
        assert len(sent['capture']['association']['required_idea_groups']) == 2
        assert sent['wire']['required_ideas'][0]['original_ids'] == ['a', 'b']
        count = len(transport.calls)
        tampered = deepcopy(payload); tampered['actual_revision_feedback']['facts'][0]['value'] = 'Changed after observation'
        with pytest.raises(m.PolicyError, match='facts or lineage changed'):
            await engine.bounded_judgments.learning_proposal(task['id'], 'projection-tampered', tampered)
        assert len(transport.calls) == count and executor.calls == []
        assert_wire_records(store, transport.calls)
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['none','repeated'])
async def test_old_review_owner_survives_current_projection_and_reopen(tmp_path,fault):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Use the exact observed review before changing its input',['No repeated successful or held review'])
    op=operation('file_list',args={})
    old={'operation':op,'bundle':{'operation':op,'observation':'Actual retained fixture source'},
         'instruction':'Review this exact source; a new projection does not replace the old outcome.'}
    def invalid(call,count):
        if fault=='repeated' and call['schema'] is m.Review:
            return dict(call['output'],unexpected_controller_field=count)
    first=install(store,engine,mutate=invalid)
    if fault=='repeated':
        with pytest.raises(m.PolicyError,match='repeated'):
            await engine.bounded_judgments.review_proposal(task['id'],op,'old-review-owner',old)
        assert len(first.calls)>=2
        value=None
    else:value=await engine.bounded_judgments.review_proposal(task['id'],op,'old-review-owner',old)
    calls=deepcopy(store.records('bounded_model_call'));captures=deepcopy(store.records('semantic_wire_request'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        if fault=='repeated':
            with pytest.raises(m.PolicyError,match='repeated'):
                await engine.bounded_judgments.review_proposal(task['id'],op,'old-review-owner',current_input(old),previous=old)
        else:
            returned=await engine.bounded_judgments.review_proposal(task['id'],op,'old-review-owner',current_input(old),previous=old)
            assert returned==value
            assert engine.bounded_judgments.review_input(task['id'],'old-review-owner',returned.model_dump())==old
        assert after.calls==[] and executor.calls==[] and store.records('knowledge_application')==[]
        assert store.records('bounded_model_call')==calls and store.records('semantic_wire_request')==captures
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['assessment','learning','missing-leaf','duplicate-leaf'])
async def test_web_saved_assessments_require_actual_leaves_and_exact_learning_composition(tmp_path,damage):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Keep the acquired response and all exact review inputs',['No unbound saved-array adoption'])
    parent=web_parent();requests=[];web=collector(store,requests)
    def fail(call,count):
        if call['phase']=='web_learning_review':return httpx.Response(500,json={'error':'Review has not completed'})
    install(store,engine,mutate=fail)
    with pytest.raises(ProviderError):
        await web.collect('https://example.com/a',phase='pre',task_id=task['id'],operation_id=parent['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    work=next(w for w in store.records('web_work') if w['stage']=='after')
    attempt=work['learning_attempts'][-1]
    assert attempt['assessments'] and 'review' not in attempt and attempt['choice_context']
    calls=deepcopy(store.records('bounded_model_call'));exchanges=deepcopy(store.records('web_exchange'))
    binding=next(r for r in store.records('bounded_assessment_result') if r['phase']=='web_learning_choices_before')
    if damage=='assessment':
        attempt['assessments'][0]['assessment']['rationale']='Altered without an actual returned assessment'
        store.record('web_work',work['id'],work)
    elif damage=='learning':
        attempt['learning']['outcome_summary']='Altered after the actual Learning response'
        store.record('web_work',work['id'],work)
    elif damage=='missing-leaf':
        store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('bounded_model_call',binding['calls'][0]['id']))
    else:
        binding['calls'].append(deepcopy(binding['calls'][0]));store.record('bounded_assessment_result',binding['id'],binding)
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        with pytest.raises(m.PolicyError):await engine.web_judgments.drain(task['id'])
        assert after.calls==[] and len(requests)==1 and executor.calls==[]
        assert store.records('knowledge_application')==[] and store.records('web_exchange')==exchanges
        assert store.record_get('web_work',work['id'])['status']!='complete'
        for call in calls:
            if damage=='missing-leaf' and call['id']==binding['calls'][0]['id']:continue
            assert store.record_get('bounded_model_call',call['id'])==call
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',['proceed','hold','governed'])
async def test_saved_post_verdict_reopens_before_promotion_without_regeneration(tmp_path,monkeypatch,verdict):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Write hello in answer.txt',['Preserve actual bytes and the exact reviewed outcome'])
    engine.web=collector(store,[])
    def decide(call,count):
        if call['phase']=='post_disposition':
            changed=dict(call['output'],verdict='revise' if verdict=='governed' else verdict,
                revision_scope='governed' if verdict=='governed' else 'none',rationale='Retained actual post verdict')
            if verdict=='governed':
                target=source_choice(call,'/operation')
                from tests.test_semantic_wire_contract import displayed_source_slots
                assert not displayed_source_slots(call)[target['source_slot']]['editable']
                changed['revision_targets']=[target]
            return changed
    install(store,engine,mutate=decide)
    store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    selected=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**selected}
    await engine._pre(state);await engine._execute(state)
    original=deepcopy(store.get_operation(state['operation_id']))
    class OutcomeSaved(RuntimeError):pass
    record=store.record
    def interrupt(kind,identity,value):
        returned=record(kind,identity,value)
        if kind=='post_learning_attempt' and value.get('status')=='reviewed':
            raise OutcomeSaved('Real Review and Disposition persisted before operation promotion')
        return returned
    with monkeypatch.context() as cut:
        cut.setattr(store,'record',interrupt)
        with pytest.raises(OutcomeSaved):await engine._post(state)
    attempt=next(a for a in store.records('post_learning_attempt') if a['operation_id']==state['operation_id'])
    assert attempt['status']=='reviewed' and not store.get_operation(state['operation_id']).get('post_review')
    # Explicit historical metadata shape, keeping the actual request/events/
    # response intact. A later promoted self-review is not its predecessor.
    attempt.pop('proposal_receipt',None);attempt.pop('learning_composition',None)
    store.record('post_learning_attempt',attempt['id'],attempt)
    calls=deepcopy(store.records('bounded_model_call'));review=deepcopy(attempt['review'])
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        for _ in range(2):
            if verdict=='proceed':await engine._post(state)
            else:
                expected=web_module.WebCorrectionNeeded if verdict=='governed' else m.PolicyError
                with pytest.raises(expected):await engine._post(state)
        row=store.get_operation(state['operation_id'])
        assert row['result']==original['result'] and row['post_review']==review
        assert after.calls==[] and len(executor.calls)==1 and store.records('bounded_model_call')==calls
        assert Path(task['workspace'],'answer.txt').read_bytes()==b'hello' and store.records('knowledge_application')
        # Only pre/post Web acquisitions may already have committed Knowledge;
        # the file operation's uncommitted Learning must not be applied here.
        assert store.record_get('knowledge_application',task['id']+':'+state['operation_id']) is None
        if verdict=='proceed':assert row['status']=='post_reviewed'
        elif verdict=='governed':
            correction,=store.records('learning_correction')
            assert row['learning_governed_correction']==correction['id']
            assert store.record_get('judgment_correction',correction['id'])['status']=='pending'
            assert store.record_get('web_judgment_resolution',correction['id']) is None
        else:assert row['status']==original['status']
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',['same-task','other-task','absent'])
async def test_real_graph_wrapped_cancellation_requires_matching_stop_and_keeps_effect(tmp_path,monkeypatch,stop):
    from langgraph.graph import StateGraph,START,END
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Preserve the actual result across a graph interruption',['No executor replay'])
    engine.web=collector(store,[]);install(store,engine)
    store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    selected=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**selected}
    await engine._pre(state);await engine._execute(state)
    before=deepcopy(store.get_operation(state['operation_id']));calls=deepcopy(store.records('bounded_model_call'))
    async def cancelled(actual):
        assert actual['task_id']==task['id']
        if stop!='absent':engine.stop_requested.add(task['id'] if stop=='same-task' else 'another-task')
        raise asyncio.CancelledError('Actual node interruption after the retained result')
    builder=StateGraph(dict);builder.add_node('cancelled',cancelled);builder.add_edge(START,'cancelled');builder.add_edge('cancelled',END)
    async def graph():return builder.compile()
    monkeypatch.setattr(engine,'_graph',graph)
    try:
        result=await engine.run_task(task['id'])
        expected='stopped' if stop=='same-task' else 'attention_required'
        assert result['task']['status']==expected
        terminal=[e for e in result['events'] if e['stage']=='task' and e['status']==expected][-1]
        assert terminal['detail']['type']=='NodeCancelledError' and terminal['detail']['node']=='cancelled'
        assert (terminal['detail'].get('stop_task_id')==task['id'])==(stop=='same-task')
        assert store.get_operation(state['operation_id'])==before and len(executor.calls)==1
        assert store.records('bounded_model_call')==calls and Path(task['workspace'],'answer.txt').read_bytes()==b'hello'
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_repeated_assessment_candidate_reuses_identity_and_complete_thinking_then_knowledge(tmp_path):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve all observations without duplicating one improvement', ['Keep every distinct judgment'])
    op = operation('file_list', args={}); result = m.OperationResult(operation_id=op['id'], status='succeeded', data={'entries': []}).model_dump()
    originals = [idea('a'), idea('b')]
    originals.append(dict(originals[0], id='conflicting', disposition='investigate', rationale='Different evidence remains unresolved'))
    observed = learning(ideas=originals)
    def repeat(call, count):
        if call['schema'] is m.AssessmentBatch:
            body = deepcopy(call['output'])
            for index,assessment in enumerate(body['assessments']):
                assessment['ideas'] = [{'candidate_slot':0,'consideration':
                    'For target '+call['canonical']['targets'][index]['id']+', compare retained bytes with a needless second effect; '
                    'the same result-preservation candidate applies and the other unresolved candidate remains separate.'}]
            return body
    transport = install(store, engine, mutate=repeat)
    try:
        choices = decision_targets('learning', observed, group_ideas=True)
        idea_choices = [c for c in choices if '/ideas/' in c['id']]
        assert len(idea_choices) == 2
        assert {x['id'] for x in idea_choices[0]['source_lineage']} == {'a', 'b'}
        context = current_input({'operation': op, 'result': result, 'learning': observed.model_dump()})
        first = await engine._assess_targets(task['id'], 'current-choice', choices, context)
        second = await engine._assess_targets(task['id'], 'current-choice-next', choices, context)
        for batch in (first, second):
            assert [a['target_id'] for a in batch] == [c['id'] for c in choices]
            assert all(set(a['assessment']['thinking_targets']) == engine.thinking_targets for a in batch)
            assert all(a['assessment']['ideas'][0]['id'] == 'a' for a in batch)
            assert all([i['id'] for i in a['assessment']['ideas']] == ['a','b'] for a in batch)
            assert all(a['target_id'] in a['assessment']['rationale'] for a in batch)
        merged = merge_ideas(originals, [i for a in first + second for i in a['assessment']['ideas']])
        assert merged == originals and len(groups(merged)) == 2
        value = learning(ideas=merged)
        outcome = engine.knowledge.apply(task, op, value, result, [])
        episode = deepcopy(store.record_get('episode', outcome['episode_id']))
        assert {i['id'] for i in episode['learning']['ideas']} == {'a', 'b', 'conflicting'}
        apps = deepcopy(store.records('knowledge_application'))
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        assert engine.knowledge.apply(store.get_task(task['id']), op, value, result, [])['episode_id'] == outcome['episode_id']
        assert store.record_get('episode', outcome['episode_id']) == episode and store.records('knowledge_application') == apps
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy,failed_phase,unknown', [(False,'web_learning_choices_before',False),
    (True,'web_learning_choices_before',False),(False,'web_learning_review',False),
    (True,'web_learning_review',False),(True,'web_learning_choices_before',True),
    (True,'web_learning_choices_before:page:1',False),(True,'web_learning_review',True),
    (True,'web_learning_review:partial',False)])
async def test_real_web_reopen_reuses_learning_and_completed_siblings_then_applies_once(tmp_path, monkeypatch, legacy, failed_phase, unknown):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain the acquired result and finish its pending judgment', ['One acquisition and one Knowledge commit'])
    parent = web_parent(); requests = []; web = collector(store, requests)
    partial_review=failed_phase=='web_learning_review:partial';review_calls=[]
    original_fits=BoundedJudgments._fits
    if partial_review:
        def finite_review(self,task_id,phase,payload,schema,role=None):
            if phase=='web_learning_review' and schema is m.Review:
                if 'source_records' not in payload:return False
                parts=payload['source_records']
                if len(parts)>1 or any(r['path']=='' for r in parts):return False
            return original_fits(self,task_id,phase,payload,schema,role)
        monkeypatch.setattr(BoundedJudgments,'_fits',finite_review)
    if ':page:' in failed_phase:
        fits=BoundedJudgments._fits;compact=BoundedJudgments.compact_context
        def finite_page(self,task_id,phase,payload,schema,role=None):
            if phase.startswith('web_learning_choices_before') and schema is m.AssessmentBatch and len(payload.get('targets',[]))>2:return False
            return fits(self,task_id,phase,payload,schema,role)
        async def observed_context(self,task_id,phase,context,base,schema,**kw):
            if phase=='web_learning_choices_before':kw['force_observation']=True
            return await compact(self,task_id,phase,context,base,schema,**kw)
        monkeypatch.setattr(BoundedJudgments,'_fits',finite_page)
        monkeypatch.setattr(BoundedJudgments,'compact_context',observed_context)
    def fail(call, count):
        if partial_review and call['phase']=='web_learning_review':review_calls.append(call)
        if call['phase'] == failed_phase or (partial_review and call['phase']=='web_learning_review' and len(review_calls)==2):
            if unknown:raise httpx.ReadTimeout('Unknown response after the actual request was sent')
            return httpx.Response(500, json={'error': 'Observed fixture HTTP500 after retained Learning'})
    before = install(store, engine, mutate=fail)
    with monkeypatch.context() as historical:
        if legacy:
            # Explicit old unmarked writer, using the real gateway and retained
            # messages/response/event. Only the next request uses the new view.
            historical.setattr(web_module, 'current_input', lambda value, **kw: deepcopy(value))
            historical.setattr(web_module, 'revision_feedback', lambda store, task_id, operation_id, attempts: deepcopy(attempts[-1]) if attempts else None)
            historical.setattr(web_module, 'decision_targets', lambda label, value, **kw: decision_targets(label, value))
        with pytest.raises(ProviderError):
            await web.collect('https://example.com/a', phase='pre', task_id=task['id'], operation_id=parent['id'],
                evaluate=lambda stage, detail: engine._web_acquisition_evaluate(task['id'], parent, stage, detail))
    saved = next(w for w in store.records('web_work') if w['stage'] == 'after')
    if legacy:
        # The old persisted attempt had no direct call reference or version.
        # This is explicit historical fixture setup, not a production migration.
        saved['learning_attempts'][-1].pop('projection_version')
        saved['learning_attempts'][-1].pop('proposal_receipt', None)
        store.record('web_work', saved['id'], saved)
    original = deepcopy(saved); old_calls = deepcopy(store.records('bounded_model_call'))
    old_captures = deepcopy(store.records('semantic_wire_request')); exchanges = deepcopy(store.records('web_exchange'))
    successful_proposals = [r for r in old_calls if r['phase'] == 'web_acquisition_learning' and r['status'] == 'succeeded']
    assert successful_proposals and len(requests) == 1 and store.records('knowledge_application') == []
    if partial_review:
        assert len(review_calls)==2 and all(c['canonical']['source_records'] for c in review_calls)
        assert any(c['phase']=='web_learning_review' and c['status']=='succeeded' for c in old_calls)
        # A larger new capacity must not replace the authenticated old tree.
        monkeypatch.setattr(BoundedJudgments,'_fits',original_fits)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    try:
        if unknown:
            with pytest.raises(m.PolicyError):await engine.web_judgments.drain(task['id'])
            assert after.calls == [] and store.records('knowledge_application') == []
            assert store.records('web_exchange') == exchanges and len(requests) == 1
            for call in old_calls:assert store.record_get('bounded_model_call', call['id']) == call
            return
        await engine.web_judgments.drain(task['id'])
        current = store.record_get('web_work', saved['id'])
        assert current['status'] == 'complete' and len(current['learning_attempts']) == 1
        old_ideas = {i['id']: i for i in original['learning_attempts'][0]['learning']['ideas']}
        current_ideas = {i['id']: i for i in current['applied_learning']['ideas']}
        assert all(current_ideas.get(identity) == value for identity, value in old_ideas.items())
        assert not any(c['phase'] == 'web_acquisition_learning' for c in after.calls)
        if failed_phase == 'web_learning_review':assert not any(c['phase'] == 'web_learning_choices_before' for c in after.calls)
        if partial_review:
            assert not any(c['phase'].startswith('web_learning_choices_before') for c in after.calls)
            continued=[c for c in after.calls if c['phase']=='web_learning_review']
            assert continued and continued[0]['canonical']==review_calls[1]['canonical']
            assert not any(c['canonical']==review_calls[0]['canonical'] for c in continued)
            assert all('learning_projection' not in c['canonical'] for c in continued)
        if ':page:' in failed_phase:
            assert any(r['phase']=='web_learning_choices_before:page:0' and r['status']=='succeeded' for r in old_calls)
            assert not any(c['phase']=='web_learning_choices_before:page:0' for c in after.calls)
            continued=[c for c in after.calls if c['phase']==failed_phase]
            assert len(continued)==1 and 'learning_projection' not in continued[0]['canonical']
        for call in old_calls:assert store.record_get('bounded_model_call', call['id']) == call
        for captured in old_captures:
            assert store.record_get('semantic_wire_request', captured['id']) == captured
            sent = [c for c in before.calls if c['capture'] == captured]
            if sent:assert projected_input(captured) == sent[0]['wire']
            assert captured['wire_schema_sha256'] == digest(wire_schema(captured['canonical_schema'], captured['canonical_input'],revision=captured.get('wire_revision')).model_json_schema())
        if legacy and failed_phase == 'web_learning_choices_before':
            sent = next(c for c in after.calls if c['phase'] == failed_phase)
            assert sent['canonical']['learning_projection'] == VERSION
            transition = sent['canonical']['previous_assessment_request']
            rejected = next(r for r in old_calls if r['phase'] == failed_phase and r['status'] == 'failed')
            assert transition['call']['id'] == rejected['id'] and transition['call']['sha256'] == digest(rejected)
        assert current['acquisition_result'] == original['acquisition_result']
        assert store.records('web_exchange') == exchanges and len(requests) == 1
        apps = deepcopy(store.records('knowledge_application')); assert len(apps) == 1
        calls = len(after.calls); await engine.web_judgments.drain(task['id'])
        assert len(after.calls) == calls and store.records('knowledge_application') == apps and executor.calls == []
        assert store.verify_events()
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_phase,boundary', [('learning_choices_before','none'),('post_review','none'),
    ('post_review','settings'),('post_review','assessment'),('post_review','historical-pages')])
async def test_normal_post_reopen_reuses_proposal_after_failed_choice_without_executor_replay(tmp_path,monkeypatch,failed_phase,boundary):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Write hello in answer.txt', ['Actual bytes retained'])
    requests = []; engine.web = collector(store, requests)
    def fail(call, count):
        if call['phase'] == failed_phase:return httpx.Response(500, json={'error': 'Fixture failed after ordinary Learning success'})
    install(store, engine, mutate=fail)
    if boundary=='historical-pages':
        fits=BoundedJudgments._fits;compact=BoundedJudgments.compact_context
        def finite_page(self,task_id,phase,payload,schema,role=None):
            if phase.startswith('learning_choices_before') and schema is m.AssessmentBatch and len(payload.get('targets',[]))>2:return False
            return fits(self,task_id,phase,payload,schema,role)
        async def observed_context(self,task_id,phase,context,base,schema,**kw):
            if phase=='learning_choices_before':kw['force_observation']=True
            return await compact(self,task_id,phase,context,base,schema,**kw)
        monkeypatch.setattr(BoundedJudgments,'_fits',finite_page)
        monkeypatch.setattr(BoundedJudgments,'compact_context',observed_context)
    # Normal selection/preparation/permit/execution; no hand-inserted operation.
    store.update_task(task['id'], state={'plan': {'objective': task['objective']}})
    selected = await engine._select({'task_id': task['id']}); state = {'task_id': task['id'], **selected}
    await engine._pre(state); await engine._execute(state)
    with pytest.raises(ProviderError):await engine._post(state)
    row = deepcopy(store.get_operation(state['operation_id'])); old_calls = deepcopy(store.records('bounded_model_call'))
    assert row['result'] and len(executor.calls) == 1
    pending = next(a for a in store.records('post_learning_attempt') if a['operation_id'] == state['operation_id'])
    assert pending['status'] == ('learning_proposed' if failed_phase=='learning_choices_before' else 'awaiting_review')
    if boundary=='historical-pages':
        # Explicit old stage metadata shape. Actual packet, completed leaves,
        # model receipts and immutable events are neither fabricated nor edited.
        assert len([r for r in old_calls if r['phase'].startswith('learning_choices_before:page:') and r['status']=='succeeded'])>1
        pending.pop('choices');pending.pop('choice_context')
        store.record('post_learning_attempt',pending['id'],pending)
        for record in store.records('bounded_assessment_result'):
            if record['phase']=='learning_choices_before':store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('bounded_assessment_result',record['id']))
        # Move actual immutable rows into reverse database order. This changes
        # retrieval order only; their inputs/events/results must remain exact.
        pages=[r for r in old_calls if r['phase'].startswith('learning_choices_before:page:')]
        for record in pages:store.db.execute('DELETE FROM records WHERE kind=? AND id=?',('bounded_model_call',record['id']))
        for record in reversed(pages):store.record('bounded_model_call',record['id'],record)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    engine.web = collector(store, requests); after = install(store, engine)
    try:
        if boundary=='settings':engine.gateway.settings.values['model']='different-model'
        elif boundary=='assessment':
            changed=deepcopy(pending);changed['choice_assessments'][0]['assessment']['rationale']='Changed saved array without its actual response'
            store.record('post_learning_attempt',changed['id'],changed)
        if boundary in {'settings','assessment'}:
            with pytest.raises(m.PolicyError):await engine._post(state)
            assert not any(c['phase'] in {'post_assessment','learning_proposal','learning_choices_before','post_review'} for c in after.calls)
            assert len(executor.calls)==1 and store.get_operation(state['operation_id'])['result']==row['result']
            return
        await engine._post(state)
        current = store.get_operation(state['operation_id'])
        assert current['status'] == 'post_reviewed' and current['result'] == row['result']
        assert not any(c['phase'] in {'post_assessment', 'learning_proposal'} for c in after.calls)
        if boundary=='historical-pages':assert not any(c['phase'].startswith('learning_choices_before') for c in after.calls)
        for call in old_calls:assert store.record_get('bounded_model_call', call['id']) == call
        assert len(executor.calls) == 1 and Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_ordinary_post_hold_reopens_without_learning_or_model_regeneration(tmp_path):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Write hello in answer.txt',['Exact observed bytes'])
    engine.web=collector(store,[])
    def hold(call,count):
        if call['phase']=='post_disposition':
            return dict(call['output'],verdict='hold',revision_scope='none',rationale='A real unresolved fixture observation remains held')
    install(store,engine,mutate=hold)
    store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    chosen=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**chosen}
    await engine._pre(state);await engine._execute(state)
    with pytest.raises(m.PolicyError,match='remains held'):await engine._post(state)
    row=deepcopy(store.get_operation(state['operation_id']));records=deepcopy(store.records('bounded_model_call'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        with pytest.raises(m.PolicyError,match='remains held'):await engine._post(state)
        assert after.calls==[] and len(executor.calls)==1
        assert store.get_operation(state['operation_id'])==row and store.records('bounded_model_call')==records
    finally:await engine.close();store.close()


class GovernedFixture(CorrectionGateway):
    async def generate(self, role, phase, payload, schema):
        value, usage = await super().generate(role, phase, payload, schema)
        # This fixture isolates an uncommitted Learning review, not the older
        # independent post-application correction cases in the shared fixture.
        if schema is m.Disposition and phase.startswith('web-learning-outcome'):
            value = value.model_copy(update={'verdict': 'proceed'})
        return value, usage


@pytest.mark.asyncio
@pytest.mark.parametrize('cut',['attached-stop','saved-verdict'])
async def test_governed_wrong_target_revision_parks_then_real_correction_returns_after_reopen(tmp_path,monkeypatch,cut):
    store, initial, _, _ = runtime(tmp_path); await initial.close()
    policy = initial.policy; directory = store.data_dir; requests = []; pause = [cut=='attached-stop']
    executor = Executor(directory)
    engine = None
    async def resolve(host):return ['93.184.216.34']
    async def fetch(request):
        requests.append(request.url.path)
        if request.url.path == '/dependent':
            roots = [c for c in store.records('judgment_correction') if c.get('consumer', {}).get('stage') == 'uncommitted_learning']
            assert roots
            for correction in roots:
                receipt = store.record_get('web_judgment_resolution', correction['id'])
                assert receipt and store.get_operation(receipt['resolution_operation_id'])['status'] == 'cycle_complete'
        if request.url.path == '/diagnostic' and pause[0]:
            pause[0] = False; engine.stop_requested.add(task['id'])
        return httpx.Response(200, headers={'content-type': 'text/plain'}, content=b'Exact governed fixture source')
    def build():
        web = WebCollector(Settings(), transport=httpx.MockTransport(fetch), resolver=resolve)
        return Engine(store, policy, executor, FixtureGateway(policy), web, Knowledge(store))
    engine = build(); task = store.create_task('Write hello and retain the actual reviewed correction', ['answer.txt contains hello'])
    def governed(call, count):
        if call['phase'] == 'web_learning_disposition' and call['canonical'].get('parent_operation', {}).get('kind') == 'plan_task' and not store.records('learning_correction'):
            body = deepcopy(call['output'])
            body.update(verdict='revise', revision_scope='governed', rationale='The next owned correction must write and read the observed result digest; Learning cannot rewrite the immutable parent or acquisition.')
            from tests.test_semantic_wire_contract import source_choice
            body['revision_targets']=[source_choice(call, '/parent_operation')]
            return body
    def wire():
        transport = install(store, engine, mutate=governed)
        transport.fixture = GovernedFixture(policy); transport.fixture.response_store = store
        return transport
    before = wire()
    class VerdictSaved(RuntimeError):pass
    saved_cut=[];record=store.record
    def interrupt(kind,identity,value):
        returned=record(kind,identity,value)
        if cut=='saved-verdict' and kind=='web_work' and not saved_cut:
            attempt=(value.get('learning_attempts') or [{}])[-1]
            if attempt.get('disposition',{}).get('verdict')=='revise' and not attempt.get('governed_correction'):
                saved_cut.append(deepcopy(value));raise VerdictSaved('Actual verdict saved before correction pointer attachment')
        return returned
    monkeypatch.setattr(store,'record',interrupt)
    try:
        paused = await asyncio.wait_for(engine.run_task(task['id']), 300)
        assert paused['task']['status'] == ('stopped' if cut=='attached-stop' else 'attention_required')
        roots=store.records('learning_correction')
        if cut=='attached-stop':
            root,=roots
            assert store.record_get('judgment_correction',root['id'])['status']=='pending'
            saved=deepcopy(store.record_get('web_work',root['consumer']['web_work_id']))
            assert store.get_task(task['id'])['state']['web_correction_returns']
        else:
            assert not roots and len(saved_cut)==1
            saved=saved_cut[0];root=None
        assert not saved.get('learning_applied') and requests.count('/first') == 1 and '/dependent' not in requests
        old_calls = deepcopy([c for c in store.records('bounded_model_call') if c['status'] == 'succeeded'])
        await engine.close(); store.close(); store = Store(directory); engine = build(); after = wire()
        # The full governed return reached the primary read's reviewed result
        # at the old guard. Allow its remaining learn/finish and retain all tails.
        result = await asyncio.wait_for(engine.run_task(task['id']), 600)
        assert result['task']['status'] == 'completed', result['events'][-3:]
        assert Path(task['workspace'], 'answer.txt').read_bytes() == b'hello'
        if root is None:
            root,=store.records('learning_correction')
            assert root['consumer']['web_work_id']==saved['id']
        assert store.record_get('learning_correction', root['id']) == root
        correction = store.record_get('judgment_correction', root['id'])
        receipt = store.record_get('web_judgment_resolution', root['id'])
        assert correction['status'] == 'resolved' and receipt
        resolution = store.get_operation(receipt['resolution_operation_id'])
        assert resolution['status'] == 'cycle_complete'
        evidence = [store.get_operation(i) for i in resolution['operation']['args']['evidence_operation_ids']]
        assert {'file_write', 'file_read'} <= {r['operation']['kind'] for r in evidence}
        assert all(r['post_review']['disposition']['verdict'] == 'proceed' and r['knowledge']['episode_id'] for r in evidence)
        assert requests.count('/first') == requests.count('/dependent') == 1
        current = store.record_get('web_work', saved['id'])
        assert current['status'] == 'complete' and len(current['learning_attempts']) == 2
        old_attempt=saved['learning_attempts'][0];consumed=current['learning_attempts'][0]
        assert all(consumed[k]==v for k,v in old_attempt.items())
        assert consumed['revision_consumed'] is True
        proposed = [c for c in after.calls if c['phase'] == 'web_acquisition_learning' and c['canonical']['operation']['id'] == saved['acquisition_operation']['id']]
        assert len(proposed) == 1
        assert proposed[0]['canonical']['governed_correction_return']['receipt'] == receipt
        assert all(store.record_get('bounded_model_call', old['id']) == old for old in old_calls)
        application = store.record_get('knowledge_application', task['id'] + ':' + saved['acquisition_operation']['id'])
        assert application and application['outcome']['episode_id']
        assert result['task']['final']['completion']['achieved'] is True and store.verify_events()
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('shape',['full-review','partial-review','assessment-pages','failed-review'])
async def test_candidate26_actual_writer_returns_after_reopen_without_feedback_trace(tmp_path,monkeypatch,shape):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Keep genuine old successful judgments',['Original receipt and event order retained'])
    op=operation('file_list',args={})
    old={'operation':op,'bundle':{'operation':op,'first_observation':'first exact fact','other_observation':'second exact fact'},'instruction':'Review every supplied original fact'}
    if shape=='assessment-pages':old.pop('instruction')  # assess_targets owns its exact instruction.
    first=install(store,engine,mutate=(lambda call,count:httpx.Response(500,json={'error':'Old observed failure'}) if call['schema'] is m.Review else None) if shape=='failed-review' else None)
    engine.bounded_judgments=Historical26Bounded(engine)
    class UnsentPage(RuntimeError):pass
    sent=[];old_call=Historical26Bounded._call;fits=BoundedJudgments._fits
    def finite(self,task_id,phase,payload,schema,role=None):
        if shape=='partial-review' and phase=='historical-review' and schema is m.Review:
            if 'source_records' not in payload or len(payload['source_records'])>1 or any(r['path']=='' for r in payload['source_records']):return False
        if shape=='assessment-pages' and phase.startswith('historical-assessment') and schema is m.AssessmentBatch and len(payload.get('targets',[]))>1:return False
        return fits(self,task_id,phase,payload,schema,role)
    async def before_second(self,task_id,phase,payload,schema,**kw):
        if shape=='partial-review' and phase=='historical-review' and schema is m.Review:
            sent.append(deepcopy(payload))
            if len(sent)==2:raise UnsentPage('Old writer interrupted before a new model dispatch')
        return await old_call(self,task_id,phase,payload,schema,**kw)
    with monkeypatch.context() as setup:
        setup.setattr(BoundedJudgments,'_fits',finite);setup.setattr(Historical26Bounded,'_call',before_second)
        if shape=='assessment-pages':
            targets=[{'id':'target-'+str(i),'kind':'actual fixture fact','value':{'fact':i}} for i in range(3)]
            result=await engine.bounded_judgments.assess_targets(task['id'],'historical-assessment',targets,old)
        elif shape in {'partial-review','failed-review'}:
            with pytest.raises(UnsentPage if shape=='partial-review' else ProviderError):
                await engine.bounded_judgments.review_proposal(task['id'],op,'historical-review',old)
        else:result=await engine.bounded_judgments.review_proposal(task['id'],op,'historical-review',old)
    records=deepcopy(store.records('bounded_model_call'));captures=deepcopy(store.records('semantic_wire_request'))
    actual=[r for r in records if r['phase'].startswith('historical-')]
    assert actual and all('feedback_trace' not in r and 'request_key' not in r for r in actual)
    assert all(r.get('wire_trace',{}).get('initial_capture') for r in actual)
    if shape=='partial-review':assert len([r for r in actual if r['status']=='succeeded' and r['phase']=='historical-review'])==1
    if shape=='assessment-pages':assert len([r for r in actual if r['phase'].startswith('historical-assessment:page:')])==3
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        if shape=='assessment-pages':monkeypatch.setattr(BoundedJudgments,'_fits',finite)
        if shape=='failed-review':
            with pytest.raises(m.PolicyError,match='historical failed request'):
                await engine.bounded_judgments.review_proposal(task['id'],op,'historical-review',current_input(old),previous=old)
            assert after.calls==[]
        elif shape=='assessment-pages':
            returned=await engine.bounded_judgments.assess_targets(task['id'],'historical-assessment',targets,current_input(old),previous=(targets,old))
            assert returned==result and after.calls==[]
            assert [x['target_id'] for x in returned]==[t['id'] for t in targets]
        else:
            returned=await engine.bounded_judgments.review_proposal(task['id'],op,'historical-review',current_input(old),previous=old)
            if shape=='full-review':assert returned==result and after.calls==[]
            else:
                assert after.calls and not any(c['canonical'].get('source_records')==sent[0]['source_records'] for c in after.calls)
                packet=next(p for p in store.records('bounded_input') if p['phase']=='historical-review:review-source')
                coverage=store.record_get('bounded_review_coverage',packet['id'])
                assert engine.bounded_judgments._remaining_review_records(old,coverage['source_records'])==[]
        assert all(store.record_get('bounded_model_call',r['id'])==r for r in records)
        assert all(store.record_get('semantic_wire_request',r['id'])==r for r in captures)
        assert executor.calls==[] and store.records('knowledge_application')==[] and store.verify_events()
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut,verdict',[('awaiting_review','proceed'),('reviewed','proceed'),('reviewed','hold'),('promoted','hold')])
async def test_candidate28_original_post_producer_adopts_exact_stages_across_two_reopens(tmp_path,monkeypatch,cut,verdict):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Write hello in answer.txt',['One exact original result'])
    requests=[];engine.web=collector(store,requests)
    def decide(call,count):
        if call['phase']=='post_disposition':return dict(call['output'],verdict=verdict,rationale='Exact historical ordinary verdict')
    install(store,engine,mutate=decide)
    store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    chosen=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**chosen}
    await engine._pre(state);await engine._execute(state)
    class SavedStage(RuntimeError):pass
    record=store.record
    def interrupt(kind,identity,value):
        value_returned=record(kind,identity,value)
        if kind=='post_learning_attempt' and value.get('status')==cut:raise SavedStage('Exact old producer state saved')
        return value_returned
    with monkeypatch.context() as setup:
        setup.setattr(store,'record',interrupt)
        with pytest.raises(RevisionNeeded if cut=='promoted' else SavedStage):await historical28_post(engine,state)
    attempt=next(a for a in store.records('post_learning_attempt') if a['operation_id']==state['operation_id'])
    assert not set(attempt)&{'assessment_context','learning_input','choices','choice_context','proposal_receipt','result_sha256'}
    rows=deepcopy(store.records('bounded_model_call'));original=deepcopy(store.get_operation(state['operation_id']))
    exchanges=deepcopy(store.records('web_exchange'));apps=deepcopy(store.records('knowledge_application'))
    first_resumed=[]
    try:
        for index in range(2):
            await engine.close();store.close();store,engine=reopen(store,engine,executor)
            engine.web=collector(store,requests);after=install(store,engine,mutate=decide)
            if verdict=='hold':
                with pytest.raises(m.PolicyError,match='remains held'):await engine._post(state)
            else:await engine._post(state)
            if index==0:first_resumed=deepcopy(after.calls)
            else:assert after.calls==[]
            assert not any(c['phase'] in {'post_assessment','learning_proposal','learning_choices_before'} for c in after.calls)
            if cut!='awaiting_review':assert after.calls==[]
            assert store.get_operation(state['operation_id'])['result']==original['result']
            assert len(executor.calls)==1 and Path(task['workspace'],'answer.txt').read_bytes()==b'hello'
            assert store.records('web_exchange')==exchanges and store.records('knowledge_application')==apps
            assert all(store.record_get('bounded_model_call',r['id'])==r for r in rows)
        if cut=='awaiting_review':assert {c['phase'] for c in first_resumed}=={'post_review','post_disposition'}
        assert store.get_operation(state['operation_id'])['status']==('post_reviewed' if verdict=='proceed' else original['status'])
        assert store.verify_events()
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_captured_learning_history_tamper_holds_before_saved_post_adoption(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Write hello in answer.txt',['One exact result'])
    engine.web=collector(store,[])
    def pause(call,count):
        if call['phase']=='post_review':return httpx.Response(500,json={'error':'Review pending'})
    install(store,engine,mutate=pause);store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    chosen=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**chosen}
    await engine._pre(state);await engine._execute(state)
    with pytest.raises(ProviderError):await engine._post(state)
    record=next(r for r in store.records('bounded_model_call') if r['phase']=='learning_proposal' and r['status']=='succeeded')
    changed=deepcopy(record);changed['learning_trace']['request_model_input']['history_lookup']['operation_count']+=1
    # Consistent mutable index tampering still cannot alter the captured request,
    # packed messages, immutable terminal event or actual retained response.
    store.record('bounded_model_call',record['id'],changed);store.record('bounded_model_completed',record['key'],changed)
    attempt=next(a for a in store.records('post_learning_attempt') if a['operation_id']==state['operation_id'])
    attempt['proposal_receipt']=engine.bounded_judgments._call_ref(changed);store.record('post_learning_attempt',attempt['id'],attempt)
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        with pytest.raises(m.PolicyError,match='LEARNING_PROVENANCE'):await engine._post(state)
        assert after.calls==[] and len(executor.calls)==1
        assert store.record_get('bounded_model_call',record['id'])==changed
        assert store.record_get('knowledge_application',task['id']+':'+state['operation_id']) is None
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_new_post_observation_reaches_next_assessment_once_and_preserves_old_verdict(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Write hello in answer.txt',['Preserve every actual observation'])
    requests=[];engine.web=collector(store,requests)
    def hold(call,count):
        if call['phase']=='post_disposition':return dict(call['output'],verdict='hold',revision_scope='none',rationale='Pending exact observation')
    install(store,engine,mutate=hold);store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
    chosen=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**chosen}
    await engine._pre(state);await engine._execute(state)
    with pytest.raises(m.PolicyError,match='remains held'):await engine._post(state)
    row=deepcopy(store.get_operation(state['operation_id']));old_attempts=deepcopy(store.records('post_learning_attempt'))
    old_calls=deepcopy(store.records('bounded_model_call'));exchanges=deepcopy(store.records('web_exchange'))
    observation={'measurement':'A new independent observation needs assessment before the original result can advance','unchanged_effect':'The five original bytes remain hello'}
    store.update_operation(state['operation_id'],post_draft=dict(row['post_draft'],new_observation=observation))
    await engine.close();store.close();store,engine=reopen(store,engine,executor)
    engine.web=collector(store,requests);after=install(store,engine)
    try:
        await engine._post(state)
        sent=[c for c in after.calls if c['phase']=='post_assessment']
        assert len(sent)==1
        feedback=sent[0]['canonical']['actual_revision_feedback']
        assert feedback['observation_projection']=='post-observations-v1'
        facts=[f for f in feedback['facts'] if f['field']=='observation.new_observation']
        assert len(facts)==1 and facts[0]['value']==observation
        assert observation['measurement'] in json.dumps(sent[0]['wire'],ensure_ascii=False)
        for origin in facts[0]['origins']:
            ref=feedback['sources'][origin['source']];source=store.record_get(ref['kind'],ref['id'])
            assert digest(source)==ref['sha256'] and at(source['value'],origin['path'])==observation
        assert all(store.record_get('post_learning_attempt',a['id'])==a for a in old_attempts)
        assert all(store.record_get('bounded_model_call',r['id'])==r for r in old_calls)
        assert store.records('web_exchange')==exchanges and len(executor.calls)==1
        assert store.get_operation(state['operation_id'])['result']==row['result']
        await engine._learn(state)
        application=deepcopy(store.record_get('knowledge_application',task['id']+':'+state['operation_id']))
        assert application and store.get_operation(state['operation_id'])['status']=='learned'
        await engine.close();store.close();store,engine=reopen(store,engine,executor);again=install(store,engine)
        await engine._post(state);await engine._learn(state)
        assert again.calls==[] and store.record_get('knowledge_application',task['id']+':'+state['operation_id'])==application
        assert len(executor.calls)==1 and store.records('web_exchange')==exchanges
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_candidate26_web_receipts_reopen_to_review_and_knowledge_without_replay(tmp_path,monkeypatch):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Finish the original acquisition judgment',['One GET and one Knowledge application'])
    parent=web_parent();requests=[];web=collector(store,requests);install(store,engine)
    engine.bounded_judgments=Historical26Bounded(engine)
    old_call=Historical26Bounded._call
    class UnsentReview(RuntimeError):pass
    async def before_review(self,task_id,phase,payload,schema,**kw):
        if phase=='web_learning_review':raise UnsentReview('The old producer has not sent this pending review')
        return await old_call(self,task_id,phase,payload,schema,**kw)
    with monkeypatch.context() as historical:
        historical.setattr(Historical26Bounded,'_call',before_review)
        historical.setattr(web_module,'current_input',lambda value,**kw:deepcopy(value))
        historical.setattr(web_module,'revision_feedback',lambda store,task_id,operation_id,attempts:deepcopy(attempts[-1]) if attempts else None)
        historical.setattr(web_module,'decision_targets',lambda label,value,**kw:decision_targets(label,value))
        with pytest.raises(UnsentReview):
            await web.collect('https://example.com/a',phase='pre',task_id=task['id'],operation_id=parent['id'],
                evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    saved=next(w for w in store.records('web_work') if w['stage']=='after')
    assert saved['learning_attempts'][-1]['assessments'] and 'review' not in saved['learning_attempts'][-1]
    calls=deepcopy(store.records('bounded_model_call'));exchanges=deepcopy(store.records('web_exchange'))
    assert any(r['phase']=='web_learning_choices_before' and 'feedback_trace' not in r for r in calls)
    assert len(requests)==1 and store.records('knowledge_application')==[]
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        await engine.web_judgments.drain(task['id'])
        current=store.record_get('web_work',saved['id'])
        assert current['status']=='complete' and current['learning_applied']
        assert not any(c['phase'] in {'web_acquisition_learning','web_learning_choices_before'} for c in after.calls)
        assert all(store.record_get('bounded_model_call',r['id'])==r for r in calls)
        assert len(requests)==1 and store.records('web_exchange')==exchanges and executor.calls==[]
        applications=deepcopy(store.records('knowledge_application'));assert len(applications)==1
        await engine.close();store.close();store,engine=reopen(store,engine,executor);again=install(store,engine)
        await engine.web_judgments.drain(task['id'])
        assert again.calls==[] and store.records('knowledge_application')==applications and len(requests)==1
    finally:await engine.close();store.close()
