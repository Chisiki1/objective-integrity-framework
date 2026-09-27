"""Actual bounded admission calls retain child ownership; judgments are fixtures."""
from copy import deepcopy
from pathlib import Path

import pytest

from policy_harness.model_routing import resolve_lease
from policy_harness.models import Assessment, AssessmentBatch, EngineeringScope, Operation
from policy_harness.store import canonical, digest
from tests.test_core import runtime
from tests.test_model_routing import configure, selection
from tests.test_policy_admission import AdmissionGateway, BROKEN, FIXED, reviewed_write


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', ['resume', 'settle'])
async def test_blind_admission_transports_and_measures_actual_child_lease(tmp_path,monkeypatch,disposition):
    store,engine,executor,_=runtime(tmp_path/'runtime')
    settings=configure(tmp_path/'settings')
    class LeaseGateway(AdmissionGateway):
        supports_model_leases=True
        batch_depth=0
        async def generate(self,role,phase,payload,schema,*,model_lease=None,job_context=None):
            # FixtureGateway expands a batch through self.generate internally.
            # Only that pure Assessment construction is not a new model call.
            if schema is Assessment and self.batch_depth:
                return await super().generate(role,phase,payload,schema)
            assert model_lease is not None, 'Missing lease at actual model boundary'
            resolve_lease(self.settings,model_lease,role=role,job_context=job_context)
            self.batch_depth+=int(schema is AssessmentBatch)
            try:
                value,usage=await super().generate(role,phase,payload,schema)
                if schema.__name__=='Disposition':
                    from tests.fixture_response_transport import retained_reply
                    return await retained_reply(self,role,phase,payload,schema,value,usage=usage,
                        model_lease=model_lease,job_context=job_context)
                return value,usage
            finally:self.batch_depth-=int(schema is AssessmentBatch)
    gateway=LeaseGateway(engine.policy);gateway.settings=settings;gateway.response_store=store
    engine.gateway=gateway
    parent=store.create_task('Preserve the complete parent output',['Keep normal behavior'])
    child=store.create_task('Correct normalize empty input',['Accept empty input','Preserve normal nonempty text'],parent_id=parent['id'])
    author_marker='PARENT_CAUSAL_CONCLUSION_AND_PROPOSED_FIX'
    selection_marker='PARENT_MODEL_SELECTION_RATIONALE'
    integration_marker='PARENT_INTEGRATION_PROPOSAL'
    job=Operation(kind='delegate',args={'objective':child['objective'],'acceptance':child['acceptance'],
        'independent_scope':'Only the owned child program.py','integration_plan':integration_marker,
        'capability_requirements':['Read and correct Python source'],'quality_requirements':['Preserve normal output'],
        'cost_considerations':'Configured local fixture route; no API cost observation',
        'model_selections':{role:dict(selection(settings,role),model_reason=selection_marker)
                            for role in ('worker','parent','reviewer')}},
        purpose=author_marker,expected_result='Candidate returned to parent',
        decisions=[{'id':'scope','statement':'Only child workspace','rationale':author_marker}]).model_dump()
    store.save_operation(parent['id'],job,policy_hash=engine.policy.hash)
    lease={'parent_id':parent['id'],'parent_operation_id':job['id'],'parent_objective':parent['objective'],
        'parent_acceptance':parent['acceptance'],'parent_source_hash':parent['source_hash'],
        'constraints':['Never write parent workspace'],'preservation':['Retain original failure and normal result'],
        'policy_hash':engine.policy.hash,'independent_scope':job['args']['independent_scope'],
        'integration_plan':job['args']['integration_plan'],'resources':{'owned':'child workspace only'},
        'shared_knowledge_write':'candidate-only; primary parent applies',
        'model_leases':engine._delegate_models(parent,job)}
    # Explicit upstream fixture state. Exercise the real scope description and
    # transactional lease construction; the complete governed source/API route
    # is covered separately by test_source_integration and recovery closure.
    store.update_task(child['id'],state={'delegation_lease':deepcopy(lease)},status='stopped')
    store.append_instruction(parent['id'],'Continue the owned child scope after this clarification.',parent['source_hash'])
    parent=store.get_task(parent['id'])
    store.update_task(parent['id'],state={'plan':{'constraints':lease['constraints'],'preservation':lease['preservation']}})
    parent=store.get_task(parent['id']);source=parent['source_history'][-1]
    args={'child_id':child['id'],'expected_lease_hash':digest(lease),
          'parent_source_hash':parent['source_hash'],'disposition':disposition,
          'source_id':source['id'],'source_quote':source['text'],'reason':'Preserve owned effects and sources'}
    if disposition=='settle':
        args['settlement']={'objective':child['objective'],'acceptance':child['acceptance']}
    scope=Operation(kind='child_scope',args=args,purpose='Apply source-bound child scope',
                    expected_result='Renewed owned lease',decisions=[{'id':'scope','statement':disposition,'rationale':'Exact parent source'}])
    store.save_operation(parent['id'],scope.model_dump(),policy_hash=engine.policy.hash)
    starts=[];monkeypatch.setattr(engine,'start_task',starts.append)
    changed=await engine._dispatch(parent,scope)
    assert changed.status=='succeeded' and changed.effect=='confirmed' and starts==[child['id']]
    child=store.get_task(child['id']);lease=deepcopy(child['state']['delegation_lease'])
    assert author_marker in canonical(lease['job_operation'])
    assert store.record_get('child_scope_history',scope.id)['before']['state']['delegation_lease']['parent_source_hash']!=parent['source_hash']
    marker='AUTHOR_DIAGNOSIS_MUST_NOT_ENTER_BLIND_PREPARATION'
    store.update_task(child['id'],state={**child['state'],'plan':{'objective':child['objective'],'diagnosis':marker}})
    (Path(child['workspace'])/'program.py').write_bytes(BROKEN.encode('utf-8'))
    gateway.next_operation=Operation(kind='file_write',args={'path':'program.py','text':FIXED},
        purpose='Restore accepted empty result',expected_result='Corrected owned source',
        decisions=[{'id':'empty','statement':'Preserve empty and nonempty results','rationale':'Exact source acceptance'}])
    await reviewed_write(engine,child)
    phases={'policy_admission_scope','policy_admission_blind_scenarios',
            'policy_admission_blind_investigation','policy_admission_applicability'}
    calls=[(phase,payload) for role,phase,payload in gateway.calls if phase in phases]
    assert {phase for phase,_ in calls}==phases
    for phase,payload in calls:
        assert payload['role_context']['parent_task_id']==parent['id']
        assert payload['role_context']['task_owner_role']==store.get_task(child['id'])['actor']
        assert payload['role_context']['actual_role']=='reviewer'
        if phase!='policy_admission_applicability':
            assert not {'plan','operation','assessments'} & payload.keys()
            assert all(text not in canonical(payload) for text in [marker,author_marker,selection_marker,integration_marker])
            boundary=payload['delegation_lease']
            assert {k:v for k,v in boundary.items() if k!='model_leases'}=={
                k:v for k,v in lease.items() if k not in {'job_operation','integration_plan','model_leases'}}
            for role,model in boundary['model_leases'].items():
                assert model=={k:v for k,v in lease['model_leases'][role].items() if k not in {'model_reason','reasoning_reason'}}
            withheld=payload['withheld_parent_context']
            assert withheld['lease_sha256']==digest(lease)
            assert withheld['fields']['job_operation']=={'id':lease['job_operation']['id'],
                'kind':'delegate','sha256':digest(lease['job_operation'])}
        else:
            assert payload['delegation_lease']==lease
            assert author_marker in canonical(payload) and selection_marker in canonical(payload)
        retained=[r for r in store.records('bounded_model_call') if r['phase']==phase and r['status']=='succeeded']
        assert retained and retained[-1]['measurement']['model_input_sha256']==digest(payload)
    assert store.get_task(child['id'])['state']['delegation_lease']==lease
    assert any(payload['delegation_lease']==lease for _,phase,payload in gateway.calls if phase=='operation_proposal')
    with pytest.raises(AssertionError,match='Missing lease at actual model boundary'):
        await gateway.generate('reviewer','policy_admission_scope',{'task_id':child['id']},EngineeringScope)
    assert executor.calls==[]
    assert store.verify_events()
    await engine.close();store.close()
