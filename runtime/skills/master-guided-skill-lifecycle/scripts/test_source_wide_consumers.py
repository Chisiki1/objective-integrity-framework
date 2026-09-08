#!/usr/bin/env python3
"""Exercise the three real consumers on one scope; no product or host actions."""
from pathlib import Path
import copy, hashlib, json, os, subprocess, sys, tempfile, types, unittest

HERE=Path(__file__).resolve().parent
SKILLS=HERE.parents[1]
def load(path,name):
    m=types.ModuleType(name);m.__file__=str(path)
    exec(compile(path.read_bytes(),str(path),'exec'),m.__dict__)
    return m
work=load(HERE/'work_io.py','consumer_work')

class Consumers(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.r=Path(self.tmp.name)
        self.source=self.file('source.txt','Implement both a producer and its real consumer; verify and preserve later evidence.')
        self.owner=self.file('owner.md','Owner source-to-full-scope reading; fixture only.')
        self.reviewer=self.file('review.md','Independent source coverage fixture, not a real independent audit.')
        self.candidate=self.file('candidate.txt','Frozen fixture candidate.')
        self.evidence=self.file('evidence.txt','Fixture observation with a bounded proof ceiling.')
        self.scope={'schema':'source-wide-scope-v1','objective_id':'O','owner_chat_id':'owner','source_ref':self.source,
            'requirements':[{'id':'R','source_clause':'source paragraph','description':'Producer and real consumer'}],
            'implementation_items':[{'id':i,'requirement_ids':['R'],'description':i} for i in ['producer','consumer']],
            'checks':[{'id':i,'requirement_ids':['R'],'stage':stage,'description':i} for i,stage in [('local-a','local'),('local-b','local'),('later','runtime')]],
            'coverage_review':{'owner_ref':self.owner,'independent_ref':self.reviewer}}
        self.scope_ref=self.file('scope.json',self.scope)
        self.state={'schema':'source-wide-state-v1','objective_id':'O','owner_chat_id':'owner','source_sha256':self.source['sha256'],
            'completion_scope_ref':self.scope_ref,'phase':'BUILD','current_stage':'local','candidate_ref':self.candidate,
            'implementation':[{'id':'producer','status':'complete','evidence_ref':self.evidence},{'id':'consumer','status':'pending','evidence_ref':None}],
            'checks':[{'id':i,'status':'pending','evidence_ref':None,'reason':''} for i in ['local-a','local-b','later']],
            'findings':[],'findings_closed':False}
        self.state_ref=self.file('state.json',self.state)
    def file(self,name,data):
        path=self.r/name;path.write_text(json.dumps(data) if isinstance(data,(dict,list)) else data,encoding='utf8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest().upper()}
    def binding(self,action='IMPLEMENT',**kw):
        v={'state_ref':self.state_ref,'completion_scope_ref':self.scope_ref,'action':action,
            'item_ids':['consumer'] if action=='IMPLEMENT' else [],'check_ids':[],'finding_ids':[],'feedback':None}
        v.update(kw);return v
    def spec(self,binding=None):
        masters=[dict(self.file(role+'.md',role+' fixture master'),role=role,read='Entire tiny fixture.',apply='Preserve parent full scope and read-only owner evidence.') for role in ['global','project']]
        return {'schema':'work-spec-v2','unit_id':'U','owner_chat_id':'owner','target':'/root/fixture_worker',
            'objective':'Implement the required consumer','objective_id':'O','work_phase':binding or self.binding(),
            'instructions':'Use the parent whole scope; write only declared artifact/result.','source':self.source,
            'inputs':[],'methods':[],'masters':masters,'scope':'Declared consumer artifact only.',
            'prohibitions':['No other task or external action.'],
            'outputs':[{'id':'out','path':str(self.r/'output.txt'),'before_sha256':None,'acceptance':'Required consumer artifact.'}],
            'result_path':str(self.r/'result.json'),'consumer':{'id':'C','description':'Parent integration','acceptance':'Original request, not child completion.',
                'destinations':[{'output_id':'out','path':str(self.r/'destination.txt')}]}}
    def prepared(self,spec):
        ref=self.file('spec.json',spec);prepared=work.prepare(ref['path']);self.file('prepared.json',prepared);return prepared
    def cli(self,script,value):
        ref=self.file('cli.json',value)
        cp=subprocess.run([sys.executable,'-B',str(script),'--input',ref['path']],capture_output=True,check=False)
        return cp,json.loads(cp.stdout)
    def event(self,binding):
        return dict(schema_version='objective-supervisor-control-v2',event_id='E',objective_id='O',source_sha256=self.source['sha256'],owner_chat_id='owner',
            work_phase=binding,decision='ACT',objective_evidence_delta='Deliver required full connection.',omission_consequence='Consumer remains absent.',
            shorter_alternative='Use existing implementation evidence.',induced_rework='No component acceptance loop.',return_step='Finish original requested scope.',
            later_effect='Observe actual integration separately.',fixed_polling=False,status_only=False,progress_status='progressing',decision_window=None)
    def transition(self,binding):
        return dict(schema_version='workflow-transition-admission-v3',receipt_id='T',objective_id='O',source_sha256=self.source['sha256'],owner_role='COORDINATED-WORK',
            action_class='material_diagnosis',requested_transition='ADVISORY',material_candidate_action=False,released_external_action=False,
            correction_admission=dict(applicable=False,authority_mode='',authority_ref='',fcr_status='',fcr_ref='',scenario_status='',scenario_ref='',interaction_ids=[],impact_status='',impact_ref='',omission_consequence='',expected_objective_delta='',cost_rework_counterfactual='',return_step=''),
            known_cause=dict(applicable=False,global_master_search_ref='',project_master_search_ref='',signature='',disposition='',prior_family_ref='',changed_premise='',discriminating_prediction=''),
            skill_effect=dict(applicable=False,action_finalized=False,selected_ref='',bytes_ref='',script_applicable=False,script_ref='',application_ref='',effect_ref='',repeated_mechanical_family=False,constrained_representation=False,phase='PRE_ACTION',outcome_capture_ref=''),
            freshness=dict(required=False,observed_at='',state_ref='',dependent_claim_ids=[],baseline_status='NOT_APPLICABLE'),
            monitoring=dict(applicable=False,decision_window_ref='',cursor_status='NOT_APPLICABLE',objective_evidence_delta='',unchanged_reuse=True,progress_status='NOT_APPLICABLE',wait_value_ref='',fixed_polling=False,recovery_disposition=''),
            stage_allocation=dict(job_shape_changed=False,view_ref='',disposition='',view_sha256='',result_ref='',result_file_sha256='',selected_configuration_id='',allocator_script_ref='',allocator_script_sha256=''),
            value=dict(auxiliary=False,mandatory_outcome_unproven_if_omitted='',expected_time_saved='',retirement_condition=''),
            topology=dict(read_only_auditor=False,implementing_owner_role='COORDINATED-WORK',owner_task_id='owner',owner_lease_id='L',chat_id='owner',parent_lease_ref='',resource_claims_ref='fixture scope',recovery_mode='NORMAL'),
            scenario_recomposition=dict(applicable=False,semantic_lock_ref='',relation_ids=[],witness_ref='',unresolved_relation_ids=[]),
            consumer=dict(required=False,outcome_claim_ids=[],oracle_ref='',evidence_delta='',status='NOT_APPLICABLE'),proof_ceiling='Fixture selection only.',job_id='J',job_shape_sha256='A'*64,work_phase=binding)
    def test_dispatch_selects_remaining_real_connection(self):
        prepared=self.prepared(self.spec())
        result=work.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])
        self.assertTrue(result['work_phase']['admitted']);self.assertEqual(result['work_phase']['pending_implementation'],['consumer'])
        self.assertIn('work-spec-v2',result['dispatch']['message'])
    def test_premature_formal_dispatch_rejected(self):
        with self.assertRaises(ValueError):self.prepared(self.spec(self.binding('FORMAL_CHECK',check_ids=['local-a'])))
    def test_verify_rechecks_live_scope_alias_for_output(self):
        spec=self.spec();output=Path(spec['outputs'][0]['path']);scope=Path(self.scope_ref['path'])
        output.write_bytes(scope.read_bytes());spec['outputs'][0]['before_sha256']=self.scope_ref['sha256']
        prepared=self.prepared(spec)
        self.assertTrue(work.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])['work_phase']['admitted'])
        output.unlink();os.link(scope,output)
        with self.assertRaisesRegex(ValueError,'aliases a read-only binding'):
            work.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])
    def test_verify_rechecks_live_scope_alias_for_result(self):
        spec=self.spec();prepared=self.prepared(spec)
        os.link(self.scope_ref['path'],spec['result_path'])
        with self.assertRaisesRegex(ValueError,'aliases a read-only binding'):
            work.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])
    def test_child_cannot_own_scope_or_state_or_source_proof(self):
        for protected in [self.scope_ref,self.state_ref,self.owner,self.reviewer,self.candidate]:
            with self.subTest(path=protected['path']):
                spec=self.spec();spec['outputs'][0].update(path=protected['path'],before_sha256=protected['sha256'])
                with self.assertRaises(ValueError):work._spec(spec)
    def consume_after_drift(self,kind):
        prepared=self.prepared(self.spec());output=self.file('output.txt','Actual partial fixture artifact')
        self.file('result.json',dict(schema='work-result-v1',request_id=prepared['request_id'],unit_id='U',status='partial',effect_state='unknown',outputs=[dict(output,id='out')],unresolved=['Remaining owner integration.'],notes='Preserve partial and unknown effects.',first_fault='Interrupted fixture'))
        if kind=='state':self.file('state.json',dict(self.state,findings_closed=True))
        elif kind=='scope':self.file('scope.json',dict(self.scope,owner_chat_id='changed-owner'))
        elif kind=='source':self.file('source.txt','A later source whose old effects must remain visible.')
        elif kind=='missing-state':(self.r/'state.json').unlink()
        else:raise AssertionError(kind)
        result=work.consume(self.r/'prepared.json',self.r/'result.json',expected_request_id=prepared['request_id'])
        self.assertTrue(result['raw_result']['base64']);self.assertEqual(result['reported_result']['effect_state'],'unknown')
        self.assertTrue(result['effect_requires_reconciliation']);self.assertEqual(len(result['artifacts']),1)
        self.assertTrue(result['freshness_issues']['required']);self.assertFalse(result['source_outcome_completed'])
        # Readback can observe an already integrated artifact without authorizing
        # a new stale-scope mutation or clearing the child's unknown effect.
        destination=self.file('destination.txt','Actual partial fixture artifact')
        self.file('integration.json',dict(schema='work-integration-v1',request_id=prepared['request_id'],unit_id='U',consumer_id='C',parent_observation='Fixture reads an existing destination, not fresh permission.',outputs=[dict(output_id='out',destination=destination['path'],sha256=destination['sha256'])]))
        integrated=work.integrate(self.r/'prepared.json',self.r/'result.json',self.r/'integration.json',expected_request_id=prepared['request_id'])
        self.assertEqual(len(integrated['integration_readback']),1);self.assertTrue(integrated['requires_reconciliation'])
        self.assertTrue(integrated['effect_requires_reconciliation']);self.assertFalse(integrated['source_outcome_completed'])
    def test_phase_drift_does_not_erase_partial_returned_effect(self):self.consume_after_drift('state')
    def test_scope_drift_does_not_erase_partial_returned_effect(self):self.consume_after_drift('scope')
    def test_source_drift_does_not_erase_partial_returned_effect(self):self.consume_after_drift('source')
    def test_missing_state_does_not_erase_partial_returned_effect(self):self.consume_after_drift('missing-state')
    def test_legacy_spec_does_not_claim_new_protection(self):
        spec=self.spec();spec['schema']='work-spec-v1';spec.pop('work_phase');spec.pop('objective_id')
        prepared=self.prepared(spec);result=work.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])
        self.assertFalse(result['work_phase']['source_wide_evaluated'])
    def test_parent_control_redirects_premature_formal_work(self):
        cp,result=self.cli(SKILLS/'objective-supervisor-control/scripts/supervisor_control.py',self.event(self.binding('FORMAL_CHECK',check_ids=['local-a'])))
        self.assertEqual(cp.returncode,3);self.assertEqual(result['decision'],'HOLD');self.assertEqual(result['work_phase']['next_actions'],['IMPLEMENT'])
    def test_parent_control_normal_implementation(self):
        cp,result=self.cli(SKILLS/'objective-supervisor-control/scripts/supervisor_control.py',self.event(self.binding()))
        self.assertEqual(cp.returncode,0);self.assertEqual(result['decision'],'ADMIT')
    def test_transition_cannot_bypass_phase_by_trivial_label(self):
        value=self.transition(self.binding('FORMAL_CHECK',check_ids=['local-a']));value['action_class']='trivial_read_only'
        cp,result=self.cli(SKILLS/'workflow-transition-admission/scripts/transition_admission.py',value)
        self.assertEqual(cp.returncode,3);self.assertIn('SOURCE_WIDE_PHASE_HOLD',result['holds'])
    def test_transition_normal_source_scope(self):
        cp,result=self.cli(SKILLS/'workflow-transition-admission/scripts/transition_admission.py',self.transition(self.binding()))
        self.assertEqual(cp.returncode,0);self.assertTrue(result['work_phase']['admitted'])
    def test_parent_identity_mismatch_is_held(self):
        value=self.event(self.binding());value['owner_chat_id']='child'
        cp,result=self.cli(SKILLS/'objective-supervisor-control/scripts/supervisor_control.py',value)
        self.assertEqual(cp.returncode,3);self.assertEqual(result['decision'],'HOLD')
    def test_feedback_cannot_relabel_external_action(self):
        feedback=dict(decision='Resolve local type connection.',return_step='Implement consumer.',cost_disposition='Cheap bounded parse.',output_ownership='No candidate mutation.',effect_class='owned-local',effects_disposition='known-bounded',applicability='construction')
        value=self.transition(self.binding('CONSTRUCTION_FEEDBACK',feedback=feedback));value['released_external_action']=True
        cp,result=self.cli(SKILLS/'workflow-transition-admission/scripts/transition_admission.py',value)
        self.assertNotEqual(cp.returncode,0)
        self.assertIn('CONSTRUCTION_FEEDBACK_CANNOT_RECLASSIFY_EXTERNAL_OR_REGRESSION_ACTION',result['holds'])

if __name__=='__main__':unittest.main()
