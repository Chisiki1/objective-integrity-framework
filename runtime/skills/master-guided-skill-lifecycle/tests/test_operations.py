"""Connected ordinary-result, next-consumer and hook cases; no live effects."""
from pathlib import Path
import json
import copy
import subprocess
import sys
import tempfile
import unittest

S=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(S))
import operation_io as o
import in_work_hooks as h
import work_io as w


class Operations(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name)
        for name in ['source','method','global','project']:(self.r/name).write_text(name)
        self.spec=dict(schema='work-operation-v1',operation_id='one',owner_chat_id='chat',source=o.ref(self.r/'source'),
            purpose='Necessary local operation',argv=[sys.executable,'-c','print("actual")'],cwd=str(self.r),timeout_seconds=5,
            accepted_exit_codes=[0],methods=[dict(id='method',**o.ref(self.r/'method'))],next_consumer='review',effect_scope='read-only')
    def tearDown(self):self.t.cleanup()
    def execute(self,code=None):
        if code is not None:self.spec['argv']=[sys.executable,'-c',code]
        o.put(self.r/'spec.json',self.spec);o.execute(self.r/'spec.json',self.r/'operation')
        return o.consume(self.r/'operation/result.json')
    def next_spec(self):
        return dict(schema='work-spec-v1',unit_id='review',owner_chat_id='chat',target='internal:reviewer',objective='Read actual result',instructions='Inspect result, never replay it.',source=self.spec['source'],inputs=[],methods=[],masters=[dict(role=k,read='Current own control',apply='Keep limits',**o.ref(self.r/k)) for k in ['global','project']],scope='One read-only report',prohibitions=['No replay or external action'],outputs=[dict(id='report',path=str(self.r/'report.txt'),before_sha256=None,acceptance='Actual independent conclusion')],result_path=str(self.r/'review-result.json'),consumer=dict(id='review',description='Consume actual result',acceptance='Known effects and limits retained',destinations=[dict(output_id='report',path=str(self.r/'integrated.txt'))]))
    def next(self):
        o.put(self.r/'next.json',self.next_spec())
        o.put(self.r/'meaning.json',dict(disposition='reuse',cause='reuse-opportunity',reason='A required independent review consumes this result.',expected_change='No manual result identity copying',alternatives=[],effect_reconciliation='Keep pending effects; read-only investigation only, never replay.',return_trigger=None))
        return w.build_next_operation(self.r/'next.json',self.r/'operation/result.json',self.r/'meaning.json',output_root=self.r/'generated')
    def test_actual_empty_nonzero_and_no_replay(self):
        value=self.execute('raise SystemExit(7)')
        self.assertEqual(value['operation']['status'],'failed');self.assertEqual(value['operation']['exit_code'],7)
        self.assertEqual(value['operation']['streams']['stdout']['bytes'],0)
        self.assertEqual(value['in_work_learning']['first_fault']['exit_code'],7)
        with self.assertRaises(ValueError):o.execute(self.r/'spec.json',self.r/'operation')
        nxt=self.next();self.assertIsNotNone(nxt['prepared']);self.assertFalse(nxt['dispatch_performed'])
    def test_accepted_nonzero_and_no_skill(self):
        self.spec.update(accepted_exit_codes=[7],methods=[])
        value=self.execute('raise SystemExit(7)');self.assertEqual(value['operation']['status'],'succeeded')
        self.assertIn('no-selected-skill-does-not-exclude-a-useful-new-operation',value['in_work_learning']['signals'])
    def test_timeout_then_method_retirement_keeps_effect_and_raw_result(self):
        self.spec['timeout_seconds']=.04
        self.execute('import time; time.sleep(1)')
        (self.r/'method').unlink()
        value=o.consume(self.r/'operation/result.json')
        self.assertEqual(value['operation']['status'],'unknown');self.assertTrue(value['effect_requires_reconciliation'])
        self.assertTrue(value['freshness_issues']);self.assertEqual(value['operation']['first_fault']['kind'],'timeout')
        nxt=self.next();self.assertIsNone(nxt['prepared']);self.assertTrue(nxt['handoff']['raw_result_ref'])
    def test_invalid_missing_and_tampered_results_never_success(self):
        self.assertIsNone(o.consume(self.r/'missing')['operation'])
        self.execute();path=self.r/'operation/result.json';value=json.loads(path.read_bytes())
        value['exit_code']=7;path.write_text(json.dumps(value))
        bad=o.consume(path);self.assertIsNone(bad['operation']);self.assertTrue(bad['validation_issues'])
        self.assertIsNone(self.next()['prepared'])
    def test_invalid_shape_cli_keeps_raw_unknown_handoff(self):
        self.execute();original=json.loads((self.r/'operation/result.json').read_bytes())
        cases=[[],None,1,'scalar']
        for key,values in {'request_ref':[[],None,1], 'spec_ref':[[]], 'streams':[[],None,{'stdout':None}], 'first_fault':[[],1], 'changed_bindings':[None,[None]],'elapsed_ms':[None,True,'1']}.items():
            for value in values:
                altered=copy.deepcopy(original);altered[key]=value;cases.append(altered)
        for number,value in enumerate(cases):
            with self.subTest(value=value):
                path=self.r/f'bad-{number}.json';path.write_text(json.dumps(value))
                cp=subprocess.run([sys.executable,'-B',str(S/'operation_io.py'),'consume','--result',str(path)],capture_output=True)
                self.assertEqual(cp.returncode,0);out=json.loads(cp.stdout)
                self.assertIsNone(out['operation']);self.assertTrue(out['effect_requires_reconciliation'])
                self.assertEqual(out['raw_result_ref'],o.ref(path));self.assertEqual(cp.stderr,b'')
    def test_actual_next_prepare_and_full_package_input(self):
        self.execute();nxt=self.next();prepared=nxt['prepared']
        self.assertEqual(len(prepared['spec']['inputs']),2)
        o.put(self.r/'prepared.json',prepared)
        self.assertFalse(w.verify(self.r/'prepared.json',expected_request_id=prepared['request_id'])['permission_granted'])
        root=self.r/'package';root.mkdir();(root/'SKILL.md').write_text('---\nname: example\ndescription: Bounded example\n---\nExample')
        (root/'script.py').write_text('print("resource")')
        value=o.candidate_input(self.r/'operation/result.json',root,'example','CREATE')
        self.assertEqual(len(value['candidate']['package']['members']),2)
        with self.assertRaises(ValueError):o.candidate_input(self.r/'operation/result.json',root,'example','REVISE')
        revised=o.candidate_input(self.r/'operation/result.json',root,'example','REVISE',prior_candidate_id='example-old')
        self.assertEqual(revised['candidate']['prior_candidate_id'],'example-old')
    def test_stale_command_shell_duplicate_and_owned_effect(self):
        self.spec['methods'].append(self.spec['methods'][0])
        with self.assertRaises(ValueError):o.validate(self.spec)
        self.spec['methods']=[];self.spec['argv'][0]=str(self.r/'powershell.exe')
        with self.assertRaises(ValueError):o.validate(self.spec,current=False)
        self.spec['effect_scope']='owned-local'
        value=self.execute('print("owned output")');self.assertTrue(value['effect_requires_reconciliation'])


class HookShapes(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name)
        self.config=dict(observation_root=str(self.r),skills_root=str(self.r/'active'),additional_skill_roots=[str(self.r/'native')],instruction_path='existing-route')
        self.payload=dict(hook_event_name='PostToolUse',session_id='test',tool_use_id='one',tool_name='Bash',tool_input=dict(command='# work_io.py'),tool_response='')
    def tearDown(self):self.t.cleanup()
    def test_filename_and_empty_output_are_not_usage(self):
        self.assertEqual(h.handle(self.payload,self.config),{})
        self.payload['tool_response']='ordinary text';self.assertEqual(h.handle(self.payload,self.config),{})
    def test_actual_string_envelope_and_accepted_exit(self):
        value=dict(schema='work-operation-summary-v1',result_ref=dict(path=str(self.r/'result'),sha256='A'*64),status='failed',exit_code=7)
        self.payload['tool_response']=json.dumps(value);h.handle(self.payload,self.config)
        record=json.loads(next(self.r.glob('*/history/*.json')).read_bytes());self.assertEqual(record['reason'],'first-fault-result')
        value['status']='succeeded';self.payload['tool_response']=json.dumps(value);h.handle(self.payload,self.config)
        self.assertIn('ordinary-procedure-result',[json.loads(p.read_bytes())['reason'] for p in self.r.glob('*/history/*.json')])
    def test_patch_body_reference_not_destination(self):
        self.payload.update(tool_name='apply_patch',tool_input=dict(command='*** Update File: '+str(self.r/'report.md')+'\n+Read '+str(self.r/'active/SKILL.md')))
        self.assertEqual(h.handle(self.payload,self.config),{})
        self.payload['tool_input']['command']='*** Add File: '+str(self.r/'native/example/SKILL.md')+'\n+content'
        self.assertTrue(h.handle(self.payload,self.config))


if __name__=='__main__':unittest.main()
