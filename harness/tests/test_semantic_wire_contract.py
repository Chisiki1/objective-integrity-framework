"""Real Engine/Store/HTTP packing consumers with explicit synthetic judgments.

No live model quality is asserted. The fixture translator below only supplies
test judgments in the new wire shape; production has no canonical fallback.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from policy_harness import models as m
from policy_harness import semantic_wire as wire
from policy_harness.models import PolicyError
from policy_harness.bounded_judgments import ContextObservation
from policy_harness.engine import Engine
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.policy_admission import _binding, _judgment, _read
from policy_harness.providers import ModelGateway, ProviderError
from policy_harness.semantic_wire import WIRE_SCHEMAS, decode, load, projected_input, wire_schema
from policy_harness.store import Store, digest
from tests.test_core import FixtureGateway, FixtureWeb, runtime
from tests.test_knowledge import learning, operation, seed_skill, observed_application
from tests.test_learning_update_contract import proposal_input, reopen
from tests.test_provider_evidence_recovery import FixtureCipher, read_all
from tests.test_providers import FakeSettings, envelope
from tests.test_policy_admission import fixture_engineering_response


@pytest.mark.parametrize('referenced', [False, True])
@pytest.mark.parametrize('family', ['Assessment', 'AssessmentBatch'])
def test_assessment_optional_refs_default_is_versioned(referenced, family):
    from pydantic import ValidationError
    prior = wire.LEARNING_BINDINGS if referenced else None
    current = wire.REFERENCED_ASSESSMENT_DEFAULTS if referenced else wire.ASSESSMENT_DEFAULTS
    row = {'objective_link': 'Current target', 'rationale': 'Current meaning needs review',
        'success': 'expected', 'mistakes': [], 'recurrence': 'no',
        'efficiency': 'No repeated work', 'interactions': 'Keep the complete peer context',
        'thinking_values': ['A concrete consideration'],
        'ideas': [{'target': 'workflow', 'proposal': 'Check the current artifact',
            'disposition': 'investigate', 'rationale': 'Needs ordinary judgment', 'next_operation': None}]}
    def body(value): return {'assessments': [value]} if family == 'AssessmentBatch' else value
    old_schema = wire_schema(family, {}, revision=prior)
    new_schema = wire_schema(family, {}, revision=current)
    old_hash = digest(old_schema.model_json_schema())
    with pytest.raises(ValidationError): old_schema.model_validate(body(row), strict=True)
    accepted = new_schema.model_validate(body(row), strict=True).model_dump()
    assert (accepted['assessments'][0] if family == 'AssessmentBatch' else accepted)['evidence_refs'] == []
    canonical = dict(row, thinking_targets={'target': row['thinking_values'][0]})
    canonical.pop('thinking_values'); canonical['ideas'] = [dict(row['ideas'][0], id='code-owned')]
    assert m.Assessment.model_validate(canonical, strict=True).evidence_refs == []
    for invalid in (None, 'evidence', [1], {}):
        with pytest.raises(ValidationError):
            new_schema.model_validate(body(dict(row, evidence_refs=invalid)), strict=True)
    explicit = new_schema.model_validate(body(dict(row, evidence_refs=['exact-evidence'])), strict=True).model_dump()
    assert (explicit['assessments'][0] if family == 'AssessmentBatch' else explicit)['evidence_refs'] == ['exact-evidence']
    assert digest(old_schema.model_json_schema()) == old_hash != digest(new_schema.model_json_schema())
    assert old_schema.model_validate(body(dict(row, evidence_refs=[])), strict=True)


def without_id(value):
    return {k:v for k,v in value.items() if k!='id'}


def displayed_source_slots(call):
    """Read the documented display, independently of the production decoder."""
    reference = call['wire']['semantic_response']['reference_source']
    if 'slots' in reference:
        return reference['slots']
    assert reference['revision'] == wire.REFERENCE_TREE
    slots = []
    def visit(node, pointer, root):
        if type(node) is int:
            slots.append({'slot': node, 'pointer': pointer,
                          'editable': root['editable'], 'stage': root['stage']})
        else:
            assert isinstance(node, dict) and node
            for token, child in node.items():
                visit(child, pointer + '/' + token, root)
    for root in reference['roots']:
        visit(root['tree'], root['pointer'], root)
    slots.sort(key=lambda item: item['slot'])
    assert [item['slot'] for item in slots] == list(range(len(slots)))
    return slots


def source_choice(call, pointer):
    """Select an explicit fixture judgment from the actually displayed table."""
    slots = displayed_source_slots(call)
    return {'source_slot': next(row['slot'] for row in slots if row['pointer'] == pointer)}


def admission_choice(call, kind, identity):
    slots = call['wire']['semantic_response']['admission_references']['slots']
    return {'reference_slot': next(row['slot'] for row in slots if row['kind'] == kind and row['id'] == identity)}


def admission_fixture_references(value, references):
    """Synthetic producer selects the table; production never translates model IDs."""
    if isinstance(value, list): return [admission_fixture_references(item, references) for item in value]
    if not isinstance(value, dict): return value
    output = {}
    for key, item in value.items():
        def selected(identity, kind):
            return {'reference_slot': next(i for i, ref in enumerate(references) if ref == {'kind': kind, 'id': identity})}
        if key in {'source_refs', 'evidence_refs', 'addressed_findings', 'finding_ids'}:
            kind = 'evidence' if key in {'source_refs', 'evidence_refs'} else 'finding'
            output[key] = [selected(identity, kind) for identity in item]
        elif key == 'prerequisite_id': output[key] = selected(item, 'prerequisite')
        else: output[key] = admission_fixture_references(item, references)
    return output


def semantic_fixture(schema, value, source, association=None):
    """Explicit fixture-only producer. It changes no real controller record."""
    data = deepcopy(value.model_dump()); name = schema.__name__
    if name == 'Disposition':
        output={'verdict':data['verdict'],'rationale':data['rationale'],
                'opinion_decisions':[{k:v for k,v in x.items() if k!='opinion_id'} for x in data['opinion_responses']]}
        if source.get('learning_projection')=='learning-current-v1' and source.get('learning_correction_scope'):
            output['revision_scope']='learning' if data['verdict']=='revise' else 'none'
        if (association or {}).get('reference_source_sha256'):
            output['revision_targets']=[]
        return output
    if name == 'Assessment':
        data['thinking_values'] = [data['thinking_targets'][k] for k in source['required_thinking_targets']]
        data.pop('thinking_targets');data['ideas'] = [without_id(x) for x in data['ideas']]
    elif name == 'AssessmentBatch':
        data['assessments'] = [semantic_fixture(m.Assessment,m.Assessment.model_validate(x['assessment']),source) for x in data['assessments']]
    elif name == 'SkillSelection':
        selected={x['id']:x for x in data['selected']};rejected={x['id']:x for x in data['rejected']}
        return {'decisions':[{'choice':'select' if s['id'] in selected else 'reject',
            'reason':(selected.get(s['id']) or rejected[s['id']])['reason'],
            'application':selected.get(s['id'],{}).get('application'),
            'procedure_clause':selected.get(s['id'],{}).get('procedure_clause')} for s in source['knowledge']['skills']],
            'new_knowledge_needed':data['new_knowledge_needed'],'rationale':data['rationale']}
    elif name == 'Cleanup':data['decisions']=[without_id(x) for x in data['decisions']]
    elif name in {'Learning','LearningIdeas','LearningApplications','LearningSynthesis'}:
        required={x['id'] for x in source.get('required_ideas',[])}
        by_id={x['id']:x for x in data.pop('ideas')}
        slots=source.get('required_ideas',[])
        if source.get('learning_projection')=='learning-current-v1':
            groups=(association or {})['required_idea_groups']
            assert {identity for group in groups for identity in group['members']}==required
            original={x['id']:x for x in slots}
            for group in groups:
                assert all(without_id(original[identity])==without_id(group['idea']) for identity in group['members'])
                assert all(without_id(by_id[identity])==without_id(by_id[group['idea']['id']]) for identity in group['members'])
            slots=[group['idea'] for group in groups]
        data['existing_idea_decisions']=[{k:by_id[x['id']][k] for k in ('disposition','rationale','next_operation')} for x in slots]
        data['new_ideas']=[without_id(x) for key,x in by_id.items() if key not in required]
        selected=source.get('pre',{}).get('skills',{}).get('selected',[])
        if 'applications' in data:
            data['applications']=[{'skill_slot':[s['id'] for s in selected].index(a['skill_id']),
                'evidence':[{k:v for k,v in e.items() if k!='sha256'} for e in a['evidence']]} for a in data['applications']]
        data.pop('considered_skill_ids',None)
        for u in data.get('skill_updates',[]):u.pop('expected_hash',None);u.pop('merge_hashes',None)
    elif name == 'Completion':data['acceptance']=[{k:v for k,v in a.items() if k not in {'criterion','independent_refs'}} for a in data['acceptance']]
    elif name == 'TaskPlan':
        data={k:v for k,v in data.items() if k not in {'acceptance','source_coverage','source_hash','source_dispositions','acceptance_dispositions','deferred_index_hash','deferred_considerations'}}
        data.update(source_decisions=[{'classification':'clarify','reason':'Fixture retains original requested output'} for _ in source['source_context']['pending_ids']],
            prior_acceptance_decisions=[{'disposition':'retain','criterion':None,'source_ids':[],'source_quote':'','reason':'Keep actual original criterion'} for _ in source['source_context']['prior_acceptance']],
            added_acceptance=[],deferred_decisions=[{'disposition':'exclude','reason':'No new work is needed for this exact fixture','planned_application':None} for _ in source.get('deferred_for_planning',{}).get('candidates',[])])
    elif name == 'ContextObservation':data.pop('coverage_ids')
    elif name == 'EngineeringInvestigation':
        data['checks']=[{k:v for k,v in c.items() if k!='work_item'} for c in data['checks']]
        data['findings']=[without_id(x) for x in data['findings']]
    elif name == 'EngineeringScenarios':data['scenarios']=[without_id(x) for x in data['scenarios']]
    elif name in {'Review','EngineeringAdmission'}:
        data['opinions']=[without_id(x) for x in data['opinions']]
        if (association or {}).get('reference_source_sha256'):
            # Synthetic review only: bind the fixture operation it discusses.
            for opinion in data['opinions']:
                slots = association.get('source_slots')
                opinion['evidence_refs'] = ([{'source_slot': next(i for i,row in enumerate(slots) if row['pointer'] == '/operation')}]
                    if slots is not None else [{'pointer':'/operation/kind','quote':source['operation']['kind']}])
    elif name == 'Operation':data.pop('id');data['decisions']=[without_id(x) for x in data['decisions']]
    if 'admission_references' in (association or {}):
        data = admission_fixture_references(data, association['admission_references'])
    return data


class WireTransport:
    def __init__(self,store,policy,*,mutate=None,engineering=False):
        self.store=store;self.policy=policy;self.calls=[];self.mutate=mutate;self.engineering=engineering
        self.fixture=FixtureGateway(policy);self.fixture.response_store=store

    async def __call__(self,request):
        body=json.loads(request.content);wire_input=json.loads(body['messages'][1]['content'].split('\n',1)[1])
        system=body['messages'][0]['content'];role=system.split('Role: ',1)[1].split('\n',1)[0]
        phase=system.split('Phase: ',1)[1].split('\n',1)[0]
        family=wire_input.get('semantic_response',{}).get('family')
        if family:
            # Apply the same exact predicates before loading unrelated captures.
            # There is still one authenticated input match; no synthetic cache.
            with self.store.lock:
                candidates = [json.loads(row['body']) for row in self.store.db.execute(
                    "SELECT body FROM records WHERE kind=? AND json_extract(body,'$.canonical_schema')=? "
                    "AND json_extract(body,'$.role')=? AND json_extract(body,'$.phase')=? ORDER BY rowid",
                    ('semantic_wire_request', family, role, phase))]
            matches=[r for r in candidates if projected_input(r)==wire_input]
            assert len(matches)==1,'Actual sent input must have exactly one pre-measured capture'
            captured=matches[0];source=captured['canonical_input'];role=captured['role'];phase=captured['phase']
            schema=ContextObservation if family=='ContextObservation' else getattr(m,family)
        else:
            # ResearchQuery and historical pre-wire Scope use canonical schemas.
            source=wire_input;captured=None
            schema=m.EngineeringScope if phase.startswith('policy_admission_scope') else m.ResearchQuery
        if schema is m.EngineeringScope and self.engineering:
            value=fixture_engineering_response(source,schema,store=self.store).model_copy(update={'engineering':True,'correction':True})
        elif schema in {m.LearningIdeas,m.LearningApplications,m.LearningSynthesis}:
            base=learning(ideas=source.get('required_ideas',[])).model_dump()
            values={k:v for k,v in base.items() if k in schema.model_fields}
            if schema is m.LearningApplications:values['considered_skill_ids']=[s['id'] for s in source.get('pre',{}).get('skills',{}).get('selected',[])]
            value=schema.model_validate(values)
        else:value,_=await self.fixture.generate(role,phase,source,schema)
        output=semantic_fixture(schema,value,source,captured['association']) if family else value.model_dump()
        call={'schema':schema,'phase':phase,'role':role,'canonical':deepcopy(source),'wire':deepcopy(wire_input),
              'messages':deepcopy(body['messages']),'capture':deepcopy(captured),'output':deepcopy(output)}
        self.calls.append(call)
        if self.mutate is not None:
            changed=self.mutate(call,len(self.calls))
            if isinstance(changed,httpx.Response):return changed
            if changed is not None:output=changed
        return httpx.Response(200,json=envelope(json.dumps(output)))


def install(store,engine,*,mutate=None,engineering=False):
    transport=WireTransport(store,engine.policy,mutate=mutate,engineering=engineering)
    gateway=ModelGateway(FakeSettings(model_context_tokens=4000000,max_output_tokens=262144),engine.policy,
        transport=httpx.MockTransport(transport),response_store=store,response_protector=FixtureCipher())
    assert gateway.supports_semantic_wire is True
    engine.gateway=gateway
    return transport


def assert_wire_records(store,calls):
    responses=store.records('model_response')
    for call in [c for c in calls if c['capture'] is not None]:
        record=call['capture'];ref={'id':record['id'],'sha256':digest(record)}
        assert any(r.get('semantic_wire_capture')==ref and r['messages_sha256']==digest(call['messages']) for r in responses)
        assert projected_input(record)==call['wire']
        assert record['canonical_input']==call['canonical']
        schema=wire_schema(call['schema'].__name__,call['canonical'],revision=record.get('wire_revision'))
        assert record['wire_schema_sha256']==digest(schema.model_json_schema())
        if call['canonical'].get('learning_projection')!='learning-current-v1' and not record.get('wire_revision'):
            assert schema is WIRE_SCHEMAS[call['schema'].__name__]
    for output in store.records('semantic_wire_output'):
        captured=store.record_get('semantic_wire_request',output['capture']['id'])
        schema=ContextObservation if output['schema']=='ContextObservation' else getattr(m,output['schema'])
        assert decode(store,captured,schema,output['raw']).model_dump()==output['canonical']
    for call in store.records('bounded_model_call'):
        if not call.get('measurement',{}).get('semantic_wire'):continue
        captured=store.record_get('semantic_wire_request',call['measurement']['semantic_wire']['id'])
        sent=[c for c in calls if c['capture']==captured]
        if sent:assert call['measurement']['messages_sha256']==digest(sent[0]['messages'])


@pytest.mark.asyncio
async def test_normal_wire_engine_delivers_with_actual_web_knowledge_permits_and_final(tmp_path, monkeypatch):
    from tests.test_web_recovery import collector
    from tests.test_engine_recovery import journal as execution_journal
    store,initial,_,_=runtime(tmp_path);await initial.close()
    executor=Executor(store.data_dir);requests=[]
    engine=Engine(store,initial.policy,executor,FixtureGateway(initial.policy),collector(store,requests),Knowledge(store))
    fits = engine.bounded_judgments._fits
    def paged_final(task_id, phase, payload, schema, role=None):
        if schema is m.Review and phase == 'independent_refutation' and 'source_records' not in payload: return False
        return fits(task_id, phase, payload, schema, role)
    monkeypatch.setattr(engine.bounded_judgments, '_fits', paged_final)
    transport=install(store,engine)
    task=store.create_task('Write hello in answer.txt and read it back',['answer.txt contains hello','Read actual bytes'])
    try:
        # Candidate26 made finite progress through write/read and finish learned
        # at120s; retain that failure and allow the remaining final assertions.
        # This guards this synthetic graph only, not product latency or retries.
        result=await asyncio.wait_for(engine.run_task(task['id']),300)
        assert result['task']['status']=='completed',result['events'][-3:]
        assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'hello'
        final_pages = [c for c in transport.calls if c['phase'] == 'independent_refutation' and c['schema'] is m.Review]
        assert final_pages and all(c['canonical']['source_records'] for c in final_pages)
        for call in final_pages:
            original = store.record_get('bounded_input', call['canonical']['source_packet_id'])['value']
            assert 'operation' not in original and 'wire_revision' not in call['capture']
        assert sorted(x['operation']['kind'] for x in execution_journal(executor))==['file_read','file_write']
        assert requests and store.records('web_exchange')
        assert all(w['status']=='complete' for w in store.records('web_work'))
        assert result['task']['final']['completion']['achieved'] is True
        assert store.records('knowledge_application')
        for app in store.records('knowledge_application'):assert app['outcome']['episode_id'] and app['input_hash']
        assert not any(s['needs_cleanup'] for s in result['knowledge']['skills'])
        assert {'TaskPlan','Operation','AssessmentBatch','Review','Disposition','Learning','Completion','Cleanup','EngineeringAdmission'}<={c['schema'].__name__ for c in transport.calls}
        selection_pages=store.records('skill_selection_page')
        selection_calls=[c for c in transport.calls if c['schema'] is m.SkillSelection]
        assert len(selection_calls)==len(selection_pages)
        if not selection_pages:
            bundles=[row['pre_bundle'] for row in store.operations(task['id']) if row.get('pre_bundle')]
            assert bundles
            assert all(bundle['skills']['selected']==[] and bundle['skills']['rejected']==[]
                and bundle['skills']['rationale']=='No active Skill in the complete exact current catalog'
                for bundle in bundles)
        assert_wire_records(store,transport.calls)
        # Every fixed field is absent from the response schema; the actual
        # canonical consumer still has the exact original membership and facts.
        for call in transport.calls:
            if call['schema'] is m.Disposition:
                assert 'web_refs' not in call['output'] and 'opinion_responses' not in call['output']
            if call['schema'] is m.Operation:assert 'id' not in call['output'] and all('id' not in d for d in call['output']['decisions'])
        assert store.verify_events()
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bundled', [False, True])
async def test_reference_tree_preserves_every_original_slot_value_and_permission(tmp_path, bundled):
    from policy_harness.learning_projection import current_input
    from tests.test_learning_wire_references import context
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Read exact original evidence', ['Keep paths, values and change authority'])
    payload = context()
    payload['result']['data'] = {'escape/~': [True, {}, [], {'id': 'atomic', 'text': 'original/~'}],
                               '': {'~1': ''}}
    payload['root/value~'] = None
    op = payload['operation']
    if bundled:
        payload = current_input({'operation': op, 'bundle': payload})
    prefix = '/bundle' if bundled else ''
    pointers = [prefix + '/result/data/escape~1~0/' + str(i) for i in range(4)]
    pointers += [prefix + '/result/data//~01', prefix + '/root~1value~0', prefix + '/learning/ideas/0']
    def choose(call, _):
        returned = deepcopy(call['output'])
        returned['opinions'][0]['evidence_refs'] = [source_choice(call, pointer) for pointer in pointers]
        return returned
    transport = install(store, engine, mutate=choose)
    try:
        reviewed = await engine.bounded_judgments.review_proposal(task['id'], op, 'tree-originals', payload)
        assert len(transport.calls) == 1
        call = transport.calls[0]; captured = call['capture']
        assert captured['presentation_revision'] == wire.REFERENCE_TREE
        assert engine.policy.prompt() in call['messages'][0]['content']
        reference = call['wire']['semantic_response']['reference_source']
        assert 'slots' not in reference
        displayed = displayed_source_slots(call)
        expected = [dict(slot=i, **{k:v for k,v in slot.items() if k != 'value_sha256'})
                    for i, slot in enumerate(captured['association']['source_slots'])]
        assert displayed == expected
        original = wire._reference_source(captured['canonical_input'], store)
        for slot in displayed:
            value = original
            for token in slot['pointer'][1:].split('/'):
                token = token.replace('~1', '/').replace('~0', '~')
                value = value[int(token)] if isinstance(value, list) else value[token]
            decoded = wire._source_reference(captured, store, {'source_slot': slot['slot']})
            assert decoded['pointer'] == slot['pointer'] and decoded['value_sha256'] == digest(value)
            assert (decoded['editable'], decoded['stage']) == (slot['editable'], slot['stage'])
        refs = [json.loads(item) for item in reviewed.opinions[0].evidence_refs]
        assert [item['pointer'] for item in refs] == pointers
        assert [item['editable'] for item in refs] == [False] * 6 + [True]
        visible = call['wire']['bundle'] if bundled else call['wire']
        canonical = original['bundle'] if bundled else original
        assert visible['result'] == canonical['result']
        assert visible['root/value~'] is None
        assert len(json.dumps(reference['roots'])) < len(json.dumps(expected))
        assert_wire_records(store, transport.calls)
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['complete', 'partial', 'unknown'])
async def test_reference_tree_restores_flat_review_messages_before_new_requests(tmp_path, monkeypatch, cut):
    from uuid import uuid4
    from policy_harness.learning_projection import revision_feedback
    from tests.test_learning_wire_references import context
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Resume original review work', ['Keep successful and unknown sends unchanged'])
    payload = context(); completed = None
    # The historical evidence presentation was eligible only with stored
    # current-frontier feedback. Use its real producer, not an impossible tag.
    payload['actual_revision_feedback'] = revision_feedback(
        store, task['id'], payload['operation']['id'],
        [{'learning': deepcopy(payload['learning'])}])
    def old_capture(store, task, role, phase, supplied, schema):
        # The old writer stores its flat presentation before any response exists.
        binding = wire._task_binding(task, store.policy_hash)
        identity = wire._identity(binding, role, phase, supplied, schema, wire.LEARNING_TARGETS,
                                  presentation_revision=wire.LEARNING_EVIDENCE)
        key = digest(identity)
        saved = store.record_get('semantic_wire_request', key)
        if saved is None:
            saved = {'id': key, **identity, 'nonce': uuid4().hex, 'canonical_input': deepcopy(supplied),
                     'association': wire.associations(supplied, schema.__name__, store, revision=wire.LEARNING_TARGETS)}
            store.record('semantic_wire_request', key, saved)
        return {'id': key, 'sha256': digest(saved)}
    def interrupt(call, _):
        if call['phase'] == 'flat-review-pending': raise asyncio.CancelledError()
    first = install(store, engine, mutate=interrupt)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', old_capture)
        if cut != 'unknown':
            completed = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'flat-review-complete', payload)
        if cut != 'complete':
            with pytest.raises(asyncio.CancelledError):
                await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'flat-review-pending', payload)
    captures = deepcopy(store.records('semantic_wire_request'))
    calls = deepcopy(store.records('bounded_model_call'))
    outputs = deepcopy(store.records('semantic_wire_output'))
    assert all(row['presentation_revision'] == wire.LEARNING_EVIDENCE for row in captures)
    assert all('slots' in call['wire']['semantic_response']['reference_source'] for call in first.calls)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        if completed is not None:
            assert await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'flat-review-complete', payload) == completed
        if cut != 'complete':
            with pytest.raises(PolicyError, match='unobserved|interrupted'):
                await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'flat-review-pending', payload)
        assert second.calls == [] and store.records('semantic_wire_request') == captures
        assert store.records('bounded_model_call') == calls and store.records('semantic_wire_output') == outputs
        for call in first.calls:
            ref = {'id': call['capture']['id'], 'sha256': digest(call['capture'])}
            assert engine.gateway._messages(call['role'], call['phase'], call['canonical'], m.Review,
                                            semantic_wire=ref) == call['messages']
        await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'tree-review-new', payload)
        assert len(second.calls) == 1 and second.calls[0]['capture']['presentation_revision'] == wire.REFERENCE_TREE
        for captured in captures: assert store.record_get('semantic_wire_request', captured['id']) == captured
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign',['none','skill','pointer'])
async def test_nonempty_application_selection_binds_exact_observed_bytes_and_knowledge_once(tmp_path,foreign):
    store,engine,_,_=runtime(tmp_path);task=store.create_task('Apply the exact write procedure',['Actual bytes and one application'])
    skill=seed_skill(store,engine.knowledge,task)
    op=operation();executor=Executor(store.data_dir)
    result=(await executor.execute(Path(task['workspace']),m.Operation.model_validate(op))).model_dump()
    op,result,expected,selected=observed_application(store,task,skill,op=op,result=result)
    # Explicit synthetic selection review over the real executor result. This
    # focused case reaches the existing atomic application guard; the separate
    # full graph case establishes normal selection/permit/execution ordering.
    before={'operation':op,'skills':{'selected':selected,'rejected':[]},'candidate':None}
    store.save_operation(task['id'],op)
    store.update_operation(op['id'],status='executed',result=result,pre_bundle=before,
        pre_review={'bundle_hash':digest(before),'disposition':{'verdict':'proceed'}},
        started_at=result['started_at'])
    source=proposal_input(op,result,selected)
    source['knowledge']={'skills':[skill]}
    def application(call,count):
        if call['schema'] is not m.LearningApplications:return None
        body=deepcopy(call['output']);slot=0;pointer='/data/sha256'
        if 'actual_format_feedback' not in call['canonical']:
            if foreign=='skill':slot=1
            if foreign=='pointer':pointer='/data/nonexistent'
        body['applications']=[{'skill_slot':slot,'evidence':[{'pointer':pointer,'explanation':'The actual written file hash records the requested bytes.'}]}]
        return body
    transport=install(store,engine,mutate=application)
    try:
        value=await engine.bounded_judgments._call(task['id'],'wire-application',source,m.LearningApplications)
        assert value.considered_skill_ids==[skill['id']]
        assert value.applications==expected.applications
        assert len(transport.calls)==(1 if foreign=='none' else 2)
        if foreign!='none':
            assert transport.calls[1]['canonical']['actual_format_feedback']['validation']
            assert all('skill_hash' not in a and 'operation_sha256' not in a for a in transport.calls[1]['output']['applications'])
        learned=learning(classifications=['use'],applications=value.applications,ideas=[x.model_dump() for x in value.ideas])
        after={'operation':op,'result':result,'learning':learned.model_dump()}
        store.update_operation(op['id'],status='post_reviewed',post_bundle=after,
            post_review={'bundle_hash':digest(after),'disposition':{'verdict':'proceed'}})
        first=engine.knowledge.apply(task,op,learned,result,[skill['id']])
        original=deepcopy(store.records('knowledge_application'))
        episode=deepcopy(store.record_get('episode',first['episode_id']))
        assert len(store.record_get('skill',skill['id'])['uses'])==1
        data_dir=store.data_dir;policy=engine.policy
        await engine.close();store.close();store=Store(data_dir)
        engine=Engine(store,policy,executor,FixtureGateway(policy),FixtureWeb(),Knowledge(store));transport=install(store,engine)
        again=engine.knowledge.apply(store.get_task(task['id']),op,learned,result,[skill['id']])
        assert again['episode_id']==first['episode_id']
        assert store.records('knowledge_application')==original
        assert store.record_get('episode',first['episode_id'])==episode
        assert len(store.record_get('skill',skill['id'])['uses'])==1 and transport.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_real_web_reconstruction_reuses_successful_wire_siblings_after_store_reopen(tmp_path):
    from tests.test_web_recovery import collector, operation as acquisition_parent
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Preserve the acquired source',['One GET and one Knowledge commit'])
    parent=acquisition_parent();requests=[];web=collector(store,requests)
    def interrupt(call,count):
        if call['phase']=='web_learning_review':return httpx.Response(500,json={'error':'Synthetic failure after retained Learning'})
    before=install(store,engine,mutate=interrupt)
    with pytest.raises(ProviderError):
        await web.collect('https://example.com/a',phase='pre',task_id=task['id'],operation_id=parent['id'],
            evaluate=lambda stage,detail:engine._web_acquisition_evaluate(task['id'],parent,stage,detail))
    saved=next(w for w in store.records('web_work') if w['stage']=='after')
    assert saved['learning_attempts'][-1]['learning']
    successes=deepcopy([r for r in store.records('bounded_model_call') if r['status']=='succeeded'])
    exchanges=deepcopy(store.records('web_exchange'));assert len(requests)==1
    await engine.close();store.close();store,engine=reopen(store,engine,executor);after=install(store,engine)
    try:
        await engine.web_judgments.drain(task['id'])
        current=store.record_get('web_work',saved['id'])
        assert current['status']=='complete'
        assert current['learning_attempts'][0]['learning']==saved['learning_attempts'][0]['learning']
        assert not any(c['phase']=='web_acquisition_learning' for c in after.calls)
        for old in successes:assert store.record_get('bounded_model_call',old['id'])==old
        assert store.records('web_exchange')==exchanges and len(requests)==1
        applications=deepcopy(store.records('knowledge_application'));assert len(applications)==1
        calls=len(after.calls);await engine.web_judgments.drain(task['id'])
        assert store.records('knowledge_application')==applications and len(after.calls)==calls
        assert executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_new_plan_still_rejects_foreign_source_quote_and_preserves_original_acceptance(tmp_path):
    store,engine,executor,_=runtime(tmp_path);task=store.create_task('Keep the source outcome',['Original acceptance'])
    store.append_instruction(task['id'],'Clarify that the original requested output stays unchanged.',task['source_hash'])
    def invalid_quote(call,count):
        if call['schema'] is m.TaskPlan and 'actual_format_feedback' not in call['canonical']:
            body=deepcopy(call['output'])
            body['added_acceptance']=[{'criterion':'Invented requirement','source_ids':[call['canonical']['source_context']['pending_ids'][0]],
                'source_quote':'A quotation that is absent from the actual source.','reason':'A semantically invalid choice'}]
            return body
    transport=install(store,engine,mutate=invalid_quote)
    try:
        value=await engine._call(task['id'],'wire-plan',{'source_task_id':task['id']},m.TaskPlan)
        assert value.acceptance==task['acceptance'] and len(transport.calls)==2
        assert 'exact quote' in transport.calls[1]['canonical']['actual_format_feedback']['validation']['message']
        assert value.acceptance_dispositions[0]['old_hash']==digest(task['acceptance'][0])
        assert store.get_task(task['id'])['acceptance']==task['acceptance'] and executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[0,1,3])
async def test_ordered_skills_and_targets_reach_real_strict_consumers(tmp_path,count):
    store,engine,executor,_=runtime(tmp_path);transport=install(store,engine)
    task=store.create_task('Inspect exact fixed memberships',['Original outcome'])
    skills=[{'id':f's-{i}','hash':digest(i),'content':'Read the actual output.','needs_cleanup':True} for i in range(count)]
    try:
        selected=await engine.bounded_judgments._call(task['id'],'wire-skill',{'operation':{'id':'op'},'knowledge':{'skills':skills}},m.SkillSelection)
        assert selected.selected==[] and [x['id'] for x in selected.rejected]==[x['id'] for x in skills]
        cleanup=await engine._call(task['id'],'wire-cleanup',{'knowledge':{'skills':skills}},m.Cleanup)
        assert [x['id'] for x in cleanup.decisions]==[x['id'] for x in skills]
        if count:
            targets=[{'id':f't-{i}','description':f'Actual target {i}'} for i in range(count)]
            values=await engine.bounded_judgments.assess_targets(task['id'],'wire-assessment',targets,{'operation':{'id':'op'}})
            assert [a['target_id'] for a in values]==[x['id'] for x in targets]
            assert all(set(v['assessment']['thinking_targets'])==engine.thinking_targets for v in values)
            ids=[idea['id'] for v in values for idea in v['assessment']['ideas']]
            assert len(ids)==len(set(ids)) and all(i.startswith('wire-') for i in ids)
        assert_wire_records(store,transport.calls);assert executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
async def test_source_investigation_and_context_coverage_remain_real_judgments(tmp_path):
    store,engine,executor,_=runtime(tmp_path);transport=install(store,engine,engineering=True)
    task=store.create_task('Inspect a concrete existing boundary',['First source item','Second source item'])
    try:
        ref=await engine.policy_admission.prepare(task['id'])
        prep=_read(store,ref,'policy_admission_preparation')
        investigation=_judgment(store,prep['investigation'],_binding(store,task['id'],engine.policy.hash))
        assert [c['work_item'] for c in investigation['result']['checks']]==task['acceptance']
        assert investigation['result']['status']=='ready'
        assert investigation['result']['findings'][0]['id'].startswith('wire-')
        # Canonical classes are intentionally unchanged; inspect by family name.
        engineering_calls = [c for c in transport.calls if c['schema'].__name__ in wire.ADMISSION_WIRE_SCHEMAS]
        assert {c['schema'] for c in engineering_calls} == {m.EngineeringScope, m.EngineeringScenarios, m.EngineeringInvestigation}
        assert all(c['capture']['wire_revision'] == wire.ADMISSION_REFERENCES for c in engineering_calls)
        original=[{'id':'part-0','value':'A'},{'id':'part-1','value':'B'}]
        observed=await engine.bounded_judgments._call(task['id'],'wire-context',{'source_records':original},ContextObservation)
        assert observed.coverage_ids==['part-0','part-1'] and observed.limitations
        assert_wire_records(store,transport.calls);assert executor.calls==[]
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper',['association','raw','canonical'])
async def test_saved_admission_permit_rejects_wire_tamper_on_issue_and_consume(tmp_path,tamper):
    store,engine,executor,_=runtime(tmp_path);transport=install(store,engine)
    task=store.create_task('Write hello in answer.txt',['Exact output'])
    try:
        store.update_task(task['id'],state={'plan':{'objective':task['objective']}})
        selected=await engine._select({'task_id':task['id']});state={'task_id':task['id'],**selected}
        await engine._pre(state)
        row=store.get_operation(selected['operation_id']);op=row['operation']
        action_hash=digest(op);bundle_hash=digest(row['pre_bundle'])
        token=store.issue_permit(task['id'],op['id'],action_hash,engine.policy.hash,bundle_hash)
        before_calls=len(transport.calls)
        output=next(o for o in store.records('semantic_wire_output') if o['schema']=='EngineeringAdmission')
        kind='semantic_wire_request' if tamper=='association' else 'semantic_wire_output'
        target=store.record_get(kind,output['capture']['id']) if tamper=='association' else output
        changed=deepcopy(target)
        if tamper=='association':changed['association']['foreign']='not captured'
        elif tamper=='raw':changed['raw']['rationale']='changed after response'
        else:changed['canonical']['rationale']='changed after decoding'
        store.record(kind,changed['id'],changed)
        with pytest.raises(PolicyError,match='WIRE_PROVENANCE'):
            store.issue_permit(task['id'],op['id'],action_hash,engine.policy.hash,bundle_hash)
        with pytest.raises(PolicyError,match='WIRE_PROVENANCE'):
            store.consume_permit(token,task_id=task['id'],operation_id=op['id'],action_hash=action_hash,policy_hash=engine.policy.hash,bundle_hash=bundle_hash)
        assert len(transport.calls)==before_calls and executor.calls==[]
        assert store.get_operation(op['id'])['result'] is None
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['none','source','owner','lease','settings','stop'])
async def test_successful_wire_receipt_reopens_or_holds_without_resending(tmp_path,boundary):
    store,engine,executor,_=runtime(tmp_path);transport=install(store,engine)
    task=store.create_task('Retain exact source result',['No effect replay'])
    payload={'source_records':[{'id':'source-part','value':'Observed original'}]}
    value=await engine.bounded_judgments._call(task['id'],'wire-context',payload,ContextObservation)
    record=deepcopy(store.records('bounded_model_call')[-1]);old_outputs=deepcopy(store.records('semantic_wire_output'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        if boundary=='none':
            again=await engine.bounded_judgments._call(task['id'],'wire-context',payload,ContextObservation)
            assert again==value and transport.calls==[]
        elif boundary=='stop':
            engine.stop_requested.add(task['id'])
            with pytest.raises(asyncio.CancelledError):await engine.bounded_judgments._call(task['id'],'wire-context',payload,ContextObservation)
        elif boundary=='settings':
            engine.gateway.settings.values['model']='changed-model'
            measured=record['measurement'];current=engine.bounded_judgments._configuration(task['id'],'parent')
            from policy_harness.providers import request_configuration
            assert request_configuration(current,'parent')['configuration']!=measured['configuration']
            with pytest.raises(m.ConfigurationRequired):
                await engine._call(task['id'],'wire-context',payload,ContextObservation,
                    expected_request_configuration={k:measured[k] for k in ('configuration','context_tokens','reserved_output_tokens')})
        else:
            # Explicit isolated-store corruption/source transition. No product
            # owner-update API is added; this reaches the saved receipt guard.
            row=store.get_task(task['id'])
            if boundary=='source':row['source_hash']='changed-source'
            elif boundary=='owner':row['actor']='worker'
            else:row['state']['delegation_lease']={'foreign':'lease'}
            store.db.execute('UPDATE tasks SET body=? WHERE id=?',(json.dumps(row),task['id']))
            with pytest.raises(PolicyError,match='WIRE_PROVENANCE'):engine.bounded_judgments._wire_event(record,ContextObservation)
        assert transport.calls==[] and executor.calls==[]
        assert store.records('semantic_wire_output')==old_outputs
        assert store.record_get('bounded_model_call',record['id'])==record
    finally:await engine.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [m.TaskPlan, m.Operation])
@pytest.mark.parametrize('legacy', [False, True])
async def test_supplied_operation_guide_keeps_saved_input_format_without_resend(tmp_path, schema, legacy):
    from policy_harness.policy import PolicyCatalog
    from tests.test_core import POLICY
    store, engine, executor, _ = runtime(tmp_path)
    if legacy:
        engine.policy = PolicyCatalog(POLICY, include_role_supplement=False)
        store.policy_hash = engine.policy.hash
    transport = install(store, engine)
    task = store.create_task('Write and read answer.txt', ['Preserve the exact received judgment'])
    payload = {'available_operations': engine._controller_guide(), 'executor_capabilities': {},
               'source_task_id': task['id'], 'operations': []}
    phase = 'task_plan' if schema is m.TaskPlan else 'propose'
    try:
        value = await engine._call(task['id'], phase, payload, schema)
        assert len(transport.calls) == 1
        captured = transport.calls[0]['canonical']
        assert 'installed_controller_context' not in captured
        assert captured.get('policy_input_contract') == (None if legacy else 'role-scoped-v1')
        original = deepcopy(store.records('bounded_model_call'))
        events = deepcopy(store.events(task['id']))
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        after = install(store, engine)
        assert await engine._call(task['id'], phase, payload, schema) == value
        assert after.calls == [] and executor.calls == []
        assert store.records('bounded_model_call') == original
        assert store.events(task['id']) == events
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['none', 'event', 'response'])
async def test_received_success_survives_later_local_authentication_failure(tmp_path, monkeypatch, damage):
    store, engine, executor, _ = runtime(tmp_path); transport = install(store, engine)
    task = store.create_task('Write and read answer.txt', ['Retain success without sending again'])
    payload = {'available_operations': engine._controller_guide(), 'executor_capabilities': {},
               'source_task_id': task['id']}
    recovery = engine._wire_recovery(m.TaskPlan)
    authenticate = recovery._authenticate
    def old_consumer(record, *args, **kwargs):
        if record.get('result') and record.get('status') == 'succeeded':
            raise PolicyError('WIRE_RECOVERY_PROVENANCE: captured source envelope differs')
        return authenticate(record, *args, **kwargs)
    with monkeypatch.context() as old:
        old.setattr(recovery, '_authenticate', old_consumer)
        with pytest.raises(PolicyError, match='captured source envelope differs'):
            await engine._call(task['id'], 'task_plan', payload, m.TaskPlan)
    original = deepcopy(store.records('bounded_model_call')[-1])
    assert original['status'] == 'failed' and original['feedback_trace']['send_state'] == 'succeeded'
    assert 'failure_event' not in original['feedback_trace'] and len(transport.calls) == 1
    events = deepcopy(store.events(task['id'])); responses = deepcopy(store.records('model_response'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    after = install(store, engine)
    try:
        event = next(e for e in events if e['seq'] == original['feedback_trace']['event']['seq'])
        if damage == 'event':
            detail = deepcopy(event['detail']); detail['result']['rationale'] = 'changed after receipt'
            store.db.execute('UPDATE events SET detail=? WHERE seq=?', (json.dumps(detail), event['seq']))
            store.db.commit()
        elif damage == 'response':
            response = deepcopy(responses[0]); response['ciphertext_base64'] = 'corrupt'
            store.record('model_response', response['id'], response)
        if damage == 'none':
            value = await engine._call(task['id'], 'task_plan', payload, m.TaskPlan)
            assert value.model_dump() == original['result']
            assert store.events(task['id']) == events and store.records('model_response') == responses
        else:
            with pytest.raises(PolicyError):
                await engine._call(task['id'], 'task_plan', payload, m.TaskPlan)
        assert after.calls == [] and executor.calls == []
        assert store.record_get('bounded_model_call', original['id']) == original
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_rejected_wire_learning_keeps_valid_middle_idea_across_store_reopen(tmp_path):
    store,engine,executor,_=runtime(tmp_path)
    task=store.create_task('Retain actual rejected proposal',['No Idea disappears'])
    op=operation('file_list',args={});result=m.OperationResult(operation_id=op['id'],status='succeeded').model_dump()
    source=proposal_input(op,result)
    new={'target':'task','proposal':'Actual new middle candidate','disposition':'reject','rationale':'Keep original scope','next_operation':None}
    def first(call,count):
        if call['schema'] is m.Learning and 'actual_format_feedback' not in call['canonical']:
            body=deepcopy(call['output']);body.update(outcome_summary='a'*80000,next_use_trigger='z'*80000,
                new_ideas=[new,{'proposal':'invalid independently'}],classifications=['unsupported'])
            engine.stop_requested.add(task['id'])
            return body
    transport=install(store,engine,mutate=first)
    with pytest.raises(asyncio.CancelledError):await engine.bounded_judgments.learning_proposal(task['id'],'learning_proposal',source)
    assert len(transport.calls)==1
    rejected=next(e for e in store.events(task['id']) if e['status']=='rejected_model_output')
    extraction=rejected['detail']['response_idea_extraction'];retained=extraction['ideas']
    assert len(retained)==1 and without_id(retained[0])==new
    assert extraction['status']=='partial' and extraction['invalid_idea_indexes']==['new_ideas/1']
    assert retained[0]['id'].startswith('wire-')
    responses=deepcopy(store.records('model_response'))
    await engine.close();store.close();store,engine=reopen(store,engine,executor);transport=install(store,engine)
    try:
        # The normal history reader carries the retained proposal into the next
        # required set before Web/Knowledge consumers; it never admits old raw.
        assert engine.web_judgments._observed_ideas(task['id'],op['id'])==retained
        value=await engine.bounded_judgments.learning_proposal(task['id'],'learning_proposal',source)
        assert [i.model_dump() for i in value.ideas]==retained
        assert len(transport.calls)==1 and transport.calls[0]['canonical']['required_ideas']==retained
        assert store.records('model_response')[:len(responses)]==responses
        assert engine._post_required_ideas({'task_id':task['id'],'operation':op,'pre_bundle':{}},[])==retained
        assert executor.calls==[] and store.records('knowledge_application')==[]
        assert_wire_records(store,transport.calls)
    finally:await engine.close();store.close()


def admission_reference_input(task):
    """Original evidence names deliberately overlap across reference namespaces."""
    finding = {'id': 'source', 'observation': 'Preserve this original counterexample',
               'rationale': 'A valid reference is not evidence of success', 'evidence_refs': ['workspace']}
    check = {'work_item': task['acceptance'][0], 'state': 'inspected', 'evidence_refs': ['source'],
             'reason': 'Only current source was inspected', 'next_step': 'Read the produced artifact'}
    investigation = {'findings': [finding], 'checks': [check], 'missing_evidence': ['Unobserved later readback']}
    evidence = {key: {'value': value} for key, value in {
        'workspace': {'files': []},
        'source': {'prior_acceptance': [{'criterion': task['acceptance'][0]}]},
        'normal': {'work_items': task['acceptance']},
        'operation': {'kind': 'file_write', 'args': {'path': 'answer.txt', 'text': 'hello'}},
        'check:0': check, 'finding:source': finding, 'missing:0': 'Unobserved later readback',
    }.items()}
    return {'evidence': evidence, 'work_items': task['acceptance'], 'preparation': {'investigation': investigation}}


def admission_reference_response(call):
    body = deepcopy(call['output'])
    if call['schema'] is m.EngineeringAdmission:
        choose = lambda kind, identity: admission_choice(call, kind, identity)
        body['addressed_findings'] = [choose('finding', 'source')]
        body['dependencies'] = [{'prerequisite_id': choose('prerequisite', 'check:0'), 'relation': 'required',
            'finding_ids': [choose('finding', 'source')], 'evidence_refs': [choose('evidence', 'check:0')],
            'reason': 'Current inspection is required; the original counterexample remains relevant'}]
        body['readiness'] = [{'aspect': 'observation', 'state': 'missing', 'evidence_refs': [choose('evidence', 'workspace')],
            'reason': 'Actual artifact readback remains unobserved'}]
        body['required_preparation'] = ['Read actual artifact bytes after the write']
    return body


@pytest.mark.asyncio
async def test_all_admission_reference_families_preserve_judgments_through_real_response_storage(tmp_path):
    from policy_harness.policy_admission import admission_anchors, validate_engineering
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Bind existing evidence without rewriting it', ['Retain original meaning'])
    payload = admission_reference_input(task)
    transport = install(store, engine, mutate=lambda call, _: admission_reference_response(call))
    try:
        values = {}
        for name in wire.ADMISSION_WIRE_SCHEMAS:
            schema = getattr(m, name)
            value = await engine.bounded_judgments._call(task['id'], 'policy_admission_references_' + name,
                                                        payload, schema, role='reviewer')
            values[name] = value.model_dump()
            validate_engineering(schema, value, payload)
            call = transport.calls[-1]
            assert call['capture']['wire_revision'] == wire.ADMISSION_REFERENCES
            assert call['canonical']['evidence'] == payload['evidence']
            assert call['wire']['evidence'] == payload['evidence']
            assert call['capture']['canonical_schema_sha256'] == digest(schema.model_json_schema())
            # A bounded input uses the same authoritative namespace, not another codec.
            compact = {'admission_contract': admission_anchors(payload)}
            assert wire.associations(compact, name, revision=wire.ADMISSION_REFERENCES) == call['capture']['association']
        assert len(transport.calls) == 4
        assert values['EngineeringScope']['source_refs'] == ['source']
        assert values['EngineeringScenarios']['scenarios'][0]['evidence_refs'] == ['normal', 'workspace']
        investigation = values['EngineeringInvestigation']
        assert investigation['evidence_refs'] == ['source', 'workspace']
        assert investigation['findings'][0]['evidence_refs'] == ['source']
        assert investigation['checks'][0]['evidence_refs'] == ['source', 'workspace']
        admission = values['EngineeringAdmission']
        assert admission['addressed_findings'] == ['source']
        assert admission['dependencies'][0] == {'prerequisite_id': 'check:0', 'relation': 'required',
            'finding_ids': ['source'], 'evidence_refs': ['check:0'],
            'reason': 'Current inspection is required; the original counterexample remains relevant'}
        assert admission['readiness'][0] == {'aspect': 'observation', 'state': 'missing', 'evidence_refs': ['workspace'],
            'reason': 'Actual artifact readback remains unobserved'}
        assert admission['opinions'][0]['evidence_refs'] == ['source', 'operation']
        assert admission['required_preparation'] == ['Read actual artifact bytes after the write']
        assert_wire_records(store, transport.calls)
        assert executor.calls == [] and not store.records('knowledge_application')
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [True, False])
async def test_admission_saved_formats_reopen_without_resend_and_stale_sources_remain_history(tmp_path, monkeypatch, legacy):
    from tests.test_learning_wire_references import legacy_capture
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain each successful admission response', ['No successful response is replayed'])
    payload = admission_reference_input(task); first = install(store, engine)
    def old_capture(*args):
        return None if args[-1] is m.EngineeringScope else legacy_capture(*args)
    def phase(name, kind): return 'policy_admission_' + name.removeprefix('Engineering').lower() + '_' + kind
    values = {}
    with monkeypatch.context() as version:
        if legacy: version.setattr(wire, 'capture', old_capture)
        for name in wire.ADMISSION_WIRE_SCHEMAS:
            values[name] = await engine.bounded_judgments._call(task['id'], phase(name, 'saved'),
                payload, getattr(m, name), role='reviewer')
    responses = deepcopy(store.records('model_response'))
    outputs = deepcopy(store.records('semantic_wire_output'))
    captures = deepcopy(store.records('semantic_wire_request'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        for name, original in values.items():
            assert await engine.bounded_judgments._call(task['id'], phase(name, 'saved'),
                payload, getattr(m, name), role='reviewer') == original
        assert second.calls == []
        assert store.records('model_response') == responses and store.records('semantic_wire_output') == outputs
        for record in captures: assert store.record_get('semantic_wire_request', record['id']) == record
        # Only a genuinely new request selects the new transport revision.
        for name in values:
            await engine.bounded_judgments._call(task['id'], phase(name, 'new'),
                payload, getattr(m, name), role='reviewer')
        assert len(second.calls) == 4
        assert all(c['capture']['wire_revision'] == wire.ADMISSION_REFERENCES for c in second.calls)
        assert_wire_records(store, first.calls + second.calls)
        store.append_instruction(task['id'], 'Current source changed; preserve past facts', task['source_hash'])
        for output in outputs:
            captured = store.record_get('semantic_wire_request', output['capture']['id'])
            schema = getattr(m, output['schema']); source = captured['canonical_input']
            with monkeypatch.context() as historical:
                def no_current_task(*args): raise AssertionError('Historical decoding must not consult current task state')
                historical.setattr(store, 'get_task', no_current_task)
                restored = load(store, output['capture'], source, schema, captured['role'], captured['phase'], current=False)
                assert decode(store, restored, schema, output['raw']).model_dump() == output['canonical']
            with pytest.raises(PolicyError, match='current source/policy/owner/parent/lease'):
                load(store, output['capture'], source, schema, captured['role'], captured['phase'])
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('schema_name,path,choice', [
    ('EngineeringScope', ('source_refs', 0), {'reference_slot': -1}),
    ('EngineeringScenarios', ('scenarios', 0, 'evidence_refs', 0), {'reference_slot': True}),
    ('EngineeringInvestigation', ('evidence_refs', 0), 'source'),
    ('EngineeringInvestigation', ('findings', 0, 'evidence_refs', 0), {'reference_slot': 1000000}),
    ('EngineeringInvestigation', ('checks', 0, 'evidence_refs', 0), 'wrong-kind'),
    ('EngineeringAdmission', ('addressed_findings', 0), 'wrong-kind'),
    ('EngineeringAdmission', ('dependencies', 0, 'prerequisite_id'), 'wrong-kind'),
    ('EngineeringAdmission', ('dependencies', 0, 'finding_ids', 0), 'wrong-kind'),
    ('EngineeringAdmission', ('readiness', 0, 'evidence_refs', 0), 'wrong-kind'),
    ('EngineeringAdmission', ('opinions', 0, 'evidence_refs', 0), {'reference_slot': 0, 'id': 'source'}),
])
async def test_invalid_admission_selections_cannot_create_canonical_success(tmp_path, schema_name, path, choice):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Reject foreign admission selections', ['Do not admit invalid evidence'])
    payload = admission_reference_input(task)
    def invalid(call, _):
        body = admission_reference_response(call); target = body
        for part in path[:-1]: target = target[part]
        selected = choice
        if choice == 'wrong-kind':
            selected = admission_choice(call, 'finding' if 'evidence_refs' in path else 'evidence', 'source')
        target[path[-1]] = selected
        return body
    transport = install(store, engine, mutate=invalid)
    try:
        with pytest.raises(PolicyError, match='repeated'):
            await engine.bounded_judgments._call(task['id'], 'policy_admission_invalid_' + schema_name,
                payload, getattr(m, schema_name), role='reviewer')
        assert len(transport.calls) == 2 and not store.records('semantic_wire_output')
        assert not store.records('bounded_model_completed') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['assessment', 'review', 'empty-review', 'page', 'unknown-review', 'empty-unknown'])
async def test_partial_v2_applied_judgment_reopens_at_original_phase_without_resend(tmp_path, monkeypatch, cut):
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Preserve partially successful applied judgment', ['No successful judgment is regenerated'])
    op = operation(); store.save_operation(task['id'], op, policy_hash=engine.policy.hash)
    choices = [] if cut.startswith('empty') else deepcopy(op['decisions'])
    if cut == 'page': choices.append(dict(choices[0], id='second-choice'))
    phase = 'learning-outcome'
    # Historical v2 caller input, before controller_facts existed. These are
    # explicit fixture observations, not a claimed real Knowledge/file effect.
    outcome = {'skills': [], 'skill_transitions': [], 'fixture': 'retained observation'}
    result = {'status': 'succeeded', 'data': {'fixture': 'original observation'}}
    context = {'learning': learning().model_dump()}
    consumer = {'kind': 'operation', 'operation_id': op['id']}
    web = {'sources': []}
    old = {'contract': 'applied-knowledge-review-v2', 'operation': op,
        'actual_result': outcome, 'original_operation_result': result,
        'accepted_input': context, 'choices': choices, 'consumer': consumer,
        'history': engine._history_lookup(task['id']),
        'timing': 'after the actual committed knowledge application',
        'result_semantics': 'Judge the committed Knowledge outcome against the accepted Learning and exact original operation/result. Transport is not task-artifact delivery. outcome.skills includes newly created Skills; skill_transitions describes changes to preexisting selected/update targets only. Empty transitions do not erase a created Skill, and projection/application receipts do not establish verified benefit.'}

    def partition(owner):
        fits = owner.bounded_judgments._fits
        def width(task_id, current_phase, payload, schema, role=None):
            if schema is m.AssessmentBatch and len(payload.get('targets', [])) > 1: return False
            return fits(task_id, current_phase, payload, schema, role)
        monkeypatch.setattr(owner.bounded_judgments, '_fits', width)

    def interrupted(call, _):
        if cut.endswith('unknown') or cut == 'unknown-review':
            if call['schema'] is m.Review: raise asyncio.CancelledError()
    first = install(store, engine, mutate=interrupted)
    try:
        if cut == 'page':
            partition(engine); actual_call = engine.bounded_judgments._call
            async def cut_after_success(*args, **kwargs):
                value = await actual_call(*args, **kwargs)
                if args[3] is m.AssessmentBatch: raise RuntimeError('Fixture cut after authentic successful page')
                return value
            with monkeypatch.context() as staged:
                staged.setattr(engine.bounded_judgments, '_call', cut_after_success)
                with pytest.raises(RuntimeError, match='Fixture cut'):
                    await engine._assess_targets(task['id'], phase + '_choices_after', choices, old)
        else:
            assessed = await engine._assess_targets(task['id'], phase + '_choices_after', choices, old)
            if cut != 'assessment':
                payload = engine.bounded_judgments.review_bundle_input(op, dict(old, assessments=assessed), web)
                if 'unknown' in cut:
                    with pytest.raises(asyncio.CancelledError):
                        await engine.bounded_judgments.review_proposal(task['id'], op, phase + '_review', payload)
                else:
                    await engine.bounded_judgments.review_proposal(task['id'], op, phase + '_review', payload)
        retained = deepcopy([r for r in store.records('bounded_model_call') if r.get('status') == 'succeeded'])
        assert not store.records('applied_knowledge_review')
        if cut != 'empty-unknown': assert retained
        await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
        second = install(store, engine)
        if cut == 'page': partition(engine)
        if 'unknown' in cut:
            with pytest.raises(PolicyError, match='unobserved|interrupted'):
                await engine._judge_applied_knowledge(task, op, phase, choices, outcome, result, web,
                                                     context=context, consumer=consumer)
            assert second.calls == [] and not store.records('applied_knowledge_review')
        else:
            saved = await engine._judge_applied_knowledge(task, op, phase, choices, outcome, result, web,
                                                        context=context, consumer=consumer)
            assert saved['input_contract'] == 'applied-knowledge-review-v2'
            new_assessments = [c for c in second.calls if c['schema'] is m.AssessmentBatch]
            assert len(new_assessments) == (1 if cut == 'page' else 0)
            if new_assessments: assert [t['id'] for t in new_assessments[0]['canonical']['targets']] == ['second-choice']
            assert len([c for c in second.calls if c['schema'] is m.Review]) == (0 if 'review' in cut else 1)
            before = len(second.calls)
            assert await engine._judge_applied_knowledge(task, op, phase, choices, outcome, result, web,
                                                         context=context, consumer=consumer) == saved
            assert len(second.calls) == before
        assert all(store.record_get('bounded_model_call',r['id']) == r for r in retained)
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()


def repeated_plan_context():
    rationale = 'Preserve every substantive choice; read actual bytes before claiming completion. '
    binding = 'Complete current mandatory source; applicability and evidence remain subject to its exact text, never a PASS label'
    plan = m.TaskPlan(task_kind='document', objective='Write and read answer.txt',
        acceptance=['Exact requested bytes'], deliverables=['answer.txt'], constraints=['Owned workspace'],
        preservation=['Original source and unknown effects'], needed_capabilities=['file tools'],
        missing_capabilities=[], phases=['write', 'read', 'finish'],
        source_hash='captured-source', deferred_index_hash='captured-index',
        source_coverage=[{'id': 'source-' + str(i), 'binding': binding} for i in range(254)],
        source_dispositions=[{'source_id': 'instruction-1', 'classification': 'clarify', 'reason': 'Preserve exact text'}],
        deferred_considerations=[{'idea_id': 'existing-idea', 'source_hash': 'candidate-version',
            'disposition': 'exclude', 'reason': 'Outside this required output', 'planned_application': None}],
        rationale=rationale)
    op = operation('plan_task', args={'plan': plan.model_dump()})
    op['decisions'] = [{'id': 'task-plan/choice-' + str(i), 'statement': 'Actual source choice ' + str(i),
                       'rationale': rationale, 'evidence_refs': ['original-instruction']} for i in range(19)]
    return {'parent_operation': op, 'source_records': [{'id': 'original-instruction', 'value': 'Create, read and show the requested artifact'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('context_kind', ['shared', 'distinct', 'non-plan'])
async def test_controller_context_factoring_is_lossless_and_keeps_full_policy(tmp_path, context_kind):
    store, engine, executor, _ = runtime(tmp_path); transport = install(store, engine)
    task = store.create_task('Retain all source and semantic meaning', ['No hidden or dropped obligation'])
    payload = repeated_plan_context(); op = payload['parent_operation']
    if context_kind == 'distinct':
        op['args']['plan']['source_coverage'][1]['binding'] = 'Different applicability; retain this counterexample in full'
        op['decisions'][1]['rationale'] = 'A distinct decision-specific reason must remain explicit'
    elif context_kind == 'non-plan': op['kind'] = 'file_write'
    original = deepcopy(payload)
    try:
        await engine.bounded_judgments._call(task['id'], 'controller-context', payload, ContextObservation)
        assert len(transport.calls) == 1
        call = transport.calls[0]; record = call['capture']
        assert payload == original and call['canonical']['parent_operation'] == original['parent_operation']
        assert engine.policy.prompt() in call['messages'][0]['content']
        projected = deepcopy(call['wire'])
        if context_kind == 'shared':
            assert record['presentation_revision'] == wire.CONTROLLER_CONTEXT
            fields = projected['semantic_response'].pop('inherited_fields')
            assert {(item['array_pointer'], item['field']) for item in fields['arrays']} == {
                ('/parent_operation/args/plan/source_coverage', 'binding'), ('/parent_operation/decisions', 'rationale')}
            # Consume the documented inheritance format independently of the producer.
            for item in fields['arrays']:
                rows = projected
                for token in item['array_pointer'].split('/')[1:]: rows = rows[token]
                assert all(item['field'] not in row for row in rows)
                for row in rows: row[item['field']] = item['value']
            assert projected['parent_operation'] == original['parent_operation']
            assert len(call['wire']['parent_operation']['args']['plan']['source_coverage']) == 254
            assert len(call['wire']['parent_operation']['decisions']) == 19
            legacy = dict(record); legacy.pop('presentation_revision')
            assert projected == projected_input(legacy)
            assert len(json.dumps(call['wire'], ensure_ascii=False).encode('utf-8')) < len(json.dumps(projected, ensure_ascii=False).encode('utf-8'))
        else:
            assert 'presentation_revision' not in record
            assert 'inherited_fields' not in projected['semantic_response']
            assert projected['parent_operation'] == original['parent_operation']
        assert_wire_records(store, transport.calls)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['complete', 'partial', 'unknown'])
async def test_prefactoring_complete_partial_and_unknown_captures_keep_original_messages(tmp_path, monkeypatch, cut):
    from tests.test_learning_wire_references import legacy_capture
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Restore saved model work exactly', ['Do not replay success or unknown requests'])
    payload = repeated_plan_context(); completed = None
    def interrupt(call, _):
        if call['phase'] == 'old-context-pending': raise asyncio.CancelledError()
    first = install(store, engine, mutate=interrupt)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', legacy_capture)
        if cut != 'unknown':
            completed = await engine.bounded_judgments._call(task['id'], 'old-context-complete', payload, ContextObservation)
        if cut != 'complete':
            with pytest.raises(asyncio.CancelledError):
                await engine.bounded_judgments._call(task['id'], 'old-context-pending', payload, ContextObservation)
    captures = deepcopy(store.records('semantic_wire_request'))
    responses = deepcopy(store.records('model_response'))
    outputs = deepcopy(store.records('semantic_wire_output'))
    bounded_calls = deepcopy(store.records('bounded_model_call'))
    assert captures and all('presentation_revision' not in row for row in captures)
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine)
    try:
        if completed is not None:
            assert await engine.bounded_judgments._call(task['id'], 'old-context-complete', payload, ContextObservation) == completed
        if cut != 'complete':
            with pytest.raises(PolicyError, match='unobserved|interrupted'):
                await engine.bounded_judgments._call(task['id'], 'old-context-pending', payload, ContextObservation)
        assert second.calls == []
        assert store.records('semantic_wire_request') == captures
        assert store.records('model_response') == responses and store.records('semantic_wire_output') == outputs
        assert store.records('bounded_model_call') == bounded_calls
        for call in first.calls:
            ref = {'id': call['capture']['id'], 'sha256': digest(call['capture'])}
            assert engine.gateway._messages(call['role'], call['phase'], call['canonical'], ContextObservation,
                semantic_wire=ref) == call['messages']
        if cut == 'complete':
            await engine.bounded_judgments._call(task['id'], 'new-context', payload, ContextObservation)
            assert len(second.calls) == 1
            assert second.calls[0]['capture']['presentation_revision'] == wire.CONTROLLER_CONTEXT
        pending = [call for call in first.calls if call['phase'] == 'old-context-pending']
        assert len(pending) == (0 if cut == 'complete' else 1)
        for call in pending:
            captured = call['capture']; ref = {'id': captured['id'], 'sha256': digest(captured)}
            saved = [row for row in bounded_calls if row['phase'] == 'old-context-pending']
            assert len(saved) == 1 and saved[0]['status'] == 'unobserved'
            assert saved[0]['measurement']['semantic_wire'] == ref
            assert saved[0]['measurement']['messages_sha256'] == digest(call['messages'])
            assert store.record_get('semantic_wire_request', captured['id']) == captured
            assert projected_input(captured) == call['wire']
            assert captured['canonical_input'] == call['canonical']
            schema = wire_schema('ContextObservation', call['canonical'], revision=captured.get('wire_revision'))
            assert captured['wire_schema_sha256'] == digest(schema.model_json_schema())
            assert schema is WIRE_SCHEMAS['ContextObservation']
            assert not any(row.get('semantic_wire_capture') == ref for row in responses)
            assert not any(row['capture'] == ref for row in outputs)
        # The injected cancellation happens before the transport returns a response.
        observed = [call for call in first.calls + second.calls if call['phase'] != 'old-context-pending']
        assert_wire_records(store, observed)
        assert executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
async def test_factored_learning_reference_keeps_original_value_and_old_target_capture(tmp_path, monkeypatch):
    from tests.test_learning_wire_references import context, legacy_capture
    store, engine, executor, _ = runtime(tmp_path)
    task = store.create_task('Retain exact reviewed original context', ['A display factor cannot alter source ownership'])
    payload = context(); payload.update(repeated_plan_context()); payload.pop('source_records')
    def select_parent(call, _):
        body = deepcopy(call['output'])
        body['opinions'][0]['evidence_refs'] = [source_choice(call, '/parent_operation')]
        return body
    first = install(store, engine, mutate=select_parent)
    with monkeypatch.context() as old:
        old.setattr(wire, 'capture', lambda *args: legacy_capture(*args, revision=wire.LEARNING_TARGETS))
        before = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'old-plan-review', payload)
    old_captures = deepcopy(store.records('semantic_wire_request'))
    old_outputs = deepcopy(store.records('semantic_wire_output'))
    await engine.close(); store.close(); store, engine = reopen(store, engine, executor)
    second = install(store, engine, mutate=select_parent)
    try:
        assert await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'old-plan-review', payload) == before
        assert second.calls == [] and store.records('semantic_wire_request') == old_captures
        assert store.records('semantic_wire_output') == old_outputs
        after = await engine.bounded_judgments.review_proposal(task['id'], payload['operation'], 'new-plan-review', payload)
        assert len(second.calls) == 1
        call = second.calls[0]
        assert call['capture']['wire_revision'] == wire.LEARNING_TARGETS
        assert call['capture']['presentation_revision'] == wire.REFERENCE_TREE
        assert 'binding' not in call['wire']['parent_operation']['args']['plan']['source_coverage'][0]
        reference = json.loads(after.opinions[0].evidence_refs[0])
        assert reference['pointer'] == '/parent_operation'
        assert reference['value_sha256'] == digest(payload['parent_operation'])
        assert reference['editable'] is False and reference['stage'] == 'immutable_captured_context'
        for captured in old_captures: assert store.record_get('semantic_wire_request', captured['id']) == captured
        assert_wire_records(store, first.calls + second.calls)
        assert not store.records('knowledge_application') and executor.calls == []
    finally:
        await engine.close(); store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damaged_prior_hash',[False,True])
async def test_current_application_message_uses_wire_schema_and_preserves_saved_canonical_history(tmp_path,damaged_prior_hash):
    from tests.test_application_contract_recovery import recorded_application
    from tests.test_learning_wire_references import legacy_capture
    store,engine,_,_=runtime(tmp_path)
    task=store.create_task('Keep the real response contract and history',['Fixed hashes belong to code'])
    op,result,learned,selected=recorded_application(store,engine,task,[])
    install(store,engine)
    prior=learned.model_dump()
    if damaged_prior_hash:prior['applications'][0]['evidence'][0]['sha256']='0'*64
    detail={'validation':{'kind':'proposal_contract','message':'Retained original rejection'},'rejected_response':prior}
    payload=dict(proposal_input(op,result,selected),actual_format_feedback=engine._feedback_from_rejection(detail))
    try:
        old_input=engine._model_input(task['id'],'saved-application-contract',payload,m.Learning)
        old_ref=legacy_capture(store,task,task['actor'],'saved-application-contract',old_input,m.Learning)
        old_capture=deepcopy(store.record_get('semantic_wire_request',old_ref['id']))
        old_messages=engine.gateway._messages(task['actor'],'saved-application-contract',old_input,m.Learning,semantic_wire=old_ref)
        new_input=engine._model_input(task['id'],'current-application-contract',payload,m.Learning)
        new_ref=engine._wire_capture(task['id'],'current-application-contract',new_input,m.Learning,task['actor'])
        captured=store.record_get('semantic_wire_request',new_ref['id'])
        messages=engine.gateway._messages(task['actor'],'current-application-contract',new_input,m.Learning,semantic_wire=new_ref)
        sent=json.loads(messages[1]['content'].split('\n',1)[1])
        assert captured['presentation_revision']==wire.APPLICATION_CONTRACT
        application=sent['application_contract']['applications']['items']
        assert set(application['properties'])=={'skill_slot','evidence'}
        assert set(application['properties']['evidence']['items']['properties'])=={'pointer','explanation'}
        assert application['properties']['evidence']['items']['additionalProperties'] is False
        assert sent['application_contract']['allowed_output_fields']==list(wire_schema('Learning',new_input).model_json_schema()['properties'])
        feedback=sent['actual_format_feedback']
        assert feedback['canonical_or_diagnostic_history']==prior
        assert feedback['validation']==detail['validation']
        assert 'rejected_response' not in feedback
        raw=semantic_fixture(m.Learning,learned,new_input,captured['association'])
        assert decode(store,captured,m.Learning,raw).applications==learned.applications
        copied=deepcopy(raw);copied['applications'][0]['evidence'][0]['sha256']=prior['applications'][0]['evidence'][0]['sha256']
        with pytest.raises(ValueError):decode(store,captured,m.Learning,copied)
        assert captured['canonical_input']['actual_format_feedback']['rejected_response']==prior
        assert wire.capture(store,store.get_task(task['id']),task['actor'],'saved-application-contract',old_input,m.Learning)==old_ref
        assert store.record_get('semantic_wire_request',old_ref['id'])==old_capture
        assert engine.gateway._messages(task['actor'],'saved-application-contract',old_input,m.Learning,semantic_wire=old_ref)==old_messages
        old_sent=json.loads(old_messages[1]['content'].split('\n',1)[1])
        assert old_sent['actual_format_feedback']['rejected_response']==prior
        assert 'sha256' in old_sent['application_contract']['applications']['items']['properties']['evidence']['items']['properties']
    finally:await engine.close();store.close()
