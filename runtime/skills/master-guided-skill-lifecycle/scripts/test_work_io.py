"""Behavioral tests use only new temporary roots beneath the explicit --temp-root."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import work_io as work

TEMP_ROOT = None


class WorkIOTests(unittest.TestCase):
    def setUp(self):
        if TEMP_ROOT is None: raise RuntimeError('supply explicit --temp-root')
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        self.source = self.write('source.txt', b'Deliver the bounded documented method.')
        self.input = self.write('input.txt', b'Current implementation contract.')
        self.method = self.write('method.md', b'Use the returned input in the real caller.')
        self.global_master = self.write('global.md', b'Global source and current scoped control.')
        self.project_master = self.write('project.md', b'Project original failure, current control and consumer.')
        self.output = self.root/'draft.md'; self.result_path = self.root/'result.json'
        self.destination = self.root/'integrated.md'
        self.spec = {'schema':'work-spec-v1', 'unit_id':'DOC-1', 'owner_chat_id':'owner-chat',
            'target':'/root/documentation', 'objective':'Deliver current method instructions',
            'instructions':'Produce a usable reference from the supplied exact implementation.',
            'source':self.ref(self.source), 'inputs':[dict(self.ref(self.input), id='contract')],
            'methods':[dict(self.ref(self.method), id='consumer-first', apply='Bind examples to the real supplied API.')],
            'masters':[dict(self.ref(self.global_master), role='global', read='current and applicable history', apply='preserve source scope'),
                       dict(self.ref(self.project_master), role='project', read='current and applicable history', apply='address the real caller gap')],
            'scope':'This isolated fixture only', 'prohibitions':['No external or shared semantic writes'],
            'outputs':[{'id':'reference', 'path':str(self.output), 'before_sha256':None, 'acceptance':'Accurate reference for current API'}],
            'result_path':str(self.result_path), 'consumer':{'id':'candidate-doc', 'description':'Actual candidate reference',
                'acceptance':'Parent judges usable instructions', 'destinations':[{'output_id':'reference','path':str(self.destination)}]}}
        self.spec_path = self.root/'spec.json'; self.prepared_path = self.root/'prepared.json'

    def write(self, name, raw):
        path = self.root/name; path.write_bytes(raw); return path

    def ref(self, path):
        return {'path':str(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest().upper()}

    def save(self, path, value): path.write_text(json.dumps(value), encoding='utf-8')

    def prepare(self):
        self.save(self.spec_path, self.spec)
        request = work.prepare(self.spec_path); self.save(self.prepared_path, request)
        return request

    def result(self, request, status='succeeded', effect='confirmed', outputs=True):
        if outputs: self.output.write_bytes(b'Exact API reference generated for the real consumer.\n')
        result = {'schema':'work-result-v1', 'request_id':request['request_id'], 'unit_id':self.spec['unit_id'],
            'status':status, 'effect_state':effect, 'outputs':[dict(self.ref(self.output),id='reference')] if outputs else [],
            'unresolved':[] if status=='succeeded' else ['Remaining owner decision'],
            'notes':'Actual child observation', 'first_fault':None if status=='succeeded' else {'stage':'partial work'}}
        self.save(self.result_path, result); return result

    def consume(self, request):
        return work.consume(self.prepared_path, self.result_path, expected_request_id=request['request_id'])

    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_provider_neutral_internal_target_is_preserved_without_dispatch(self):
        self.spec['target'] = 'internal:documentation-worker.1'
        request = self.prepare()
        before = self.snapshot()
        verified = work.verify(self.prepared_path, expected_request_id=request['request_id'])
        self.assertEqual('internal:documentation-worker.1', verified['dispatch']['target'])
        self.assertFalse(verified['dispatch_performed'])
        self.assertFalse(verified['permission_granted'])
        self.assertEqual(before, self.snapshot())

    def test_external_and_ambiguous_target_identifiers_are_rejected(self):
        for target in ('https://example.org/task', 'internal:', 'internal:../other', 'other-task'):
            with self.subTest(target=target):
                self.spec['target'] = target
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_normal_document_request_result_and_actual_integration(self):
        request = self.prepare(); before = self.snapshot()
        verified = work.verify(self.prepared_path, expected_request_id=request['request_id'])
        self.assertEqual(request['dispatch'], verified['dispatch']); self.assertEqual(before,self.snapshot())
        self.assertEqual('/root/documentation',verified['dispatch']['target'])
        self.result(request); before = self.snapshot(); handoff = self.consume(request)
        self.assertEqual(before,self.snapshot()); self.assertEqual([],handoff['validation_issues'])
        self.assertEqual(self.ref(self.output)['sha256'],handoff['artifacts'][0]['sha256'])
        self.assertFalse(handoff['source_outcome_completed']); self.assertFalse(handoff['permission_granted'])
        self.destination.write_bytes(self.output.read_bytes())
        observation = {'schema':'work-integration-v1','request_id':request['request_id'],'unit_id':'DOC-1',
            'consumer_id':'candidate-doc','parent_observation':'Parent copied reviewed draft into actual candidate.',
            'outputs':[{'output_id':'reference','destination':str(self.destination),'sha256':self.ref(self.output)['sha256']}]}
        observation_path=self.root/'observation.json'; self.save(observation_path,observation); before=self.snapshot()
        integrated=work.integrate(self.prepared_path,self.result_path,observation_path,expected_request_id=request['request_id'])
        self.assertEqual(before,self.snapshot()); self.assertEqual([],integrated['validation_issues'])
        self.assertEqual(1,len(integrated['integration_readback'])); self.assertFalse(integrated['source_outcome_completed'])
        self.assertTrue(integrated['structural_integration_complete']); self.assertFalse(integrated['requires_reconciliation'])
        self.destination.write_bytes(b'Other content')
        self.assertTrue(work.integrate(self.prepared_path,self.result_path,observation_path,expected_request_id=request['request_id'])['validation_issues'])

    def test_normal_implementation_handoff_is_not_completion(self):
        self.spec['unit_id']='IMPL-1'; self.output=self.root/'component.py'; self.spec['outputs'][0]['path']=str(self.output)
        request=self.prepare(); self.result(request); self.output.write_bytes(b'def entry(value):\n    return value + 1\n')
        result=json.loads(self.result_path.read_bytes()); result['outputs'][0]=dict(self.ref(self.output),id='reference'); self.save(self.result_path,result)
        handoff=self.consume(request)
        self.assertEqual([],handoff['validation_issues']); self.assertEqual(str(self.output),handoff['artifacts'][0]['path'])
        self.assertTrue(handoff['unresolved']); self.assertFalse(handoff['source_outcome_completed'])

    def test_failed_partial_and_unknown_effects_remain_exact(self):
        request=self.prepare()
        for status,effect,outputs in [('failed','none',False),('partial','partial',True),('unknown','unknown',True)]:
            with self.subTest(status=status):
                result=self.result(request,status,effect,outputs); raw=self.result_path.read_bytes(); before=self.snapshot()
                handoff=self.consume(request)
                self.assertEqual(result,handoff['reported_result']); self.assertEqual(raw,base64.b64decode(handoff['raw_result']['base64']))
                self.assertEqual(before,self.snapshot()); self.assertFalse(handoff['source_outcome_completed'])

    def test_changed_source_master_and_spec_after_result_do_not_erase_effects(self):
        request=self.prepare(); result=self.result(request,'partial','unknown'); raw=self.result_path.read_bytes()
        self.source.write_bytes(b'New source'); self.global_master.write_bytes(b'Unrelated append'); self.spec_path.write_bytes(b'new owner work')
        handoff=self.consume(request)
        self.assertEqual(result,handoff['reported_result']); self.assertEqual(1,len(handoff['artifacts']))
        self.assertEqual(raw,base64.b64decode(handoff['raw_result']['base64'])); self.assertEqual(3,len(handoff['freshness_issues']['required']))
        self.assertTrue(handoff['requires_reconciliation'])

    def test_stale_input_blocks_only_new_dependent_dispatch(self):
        request=self.prepare(); self.input.write_bytes(b'changed'); before=self.snapshot()
        with self.assertRaises(ValueError): work.verify(self.prepared_path,expected_request_id=request['request_id'])
        self.assertEqual(before,self.snapshot())

    def test_optional_missing_input_has_no_fake_digest_or_global_hold(self):
        self.spec['inputs'].append({'id':'unrelated','path':str(self.root/'absent.txt'),'sha256':None,'required':False})
        request=self.prepare(); verified=work.verify(self.prepared_path,expected_request_id=request['request_id'])
        self.assertEqual(1,len(verified['optional_input_issues'])); self.assertIsNone(request['spec']['inputs'][1]['sha256'])
        self.result(request); handoff=self.consume(request)
        self.assertFalse(handoff['requires_reconciliation']); self.assertEqual(1,len(handoff['artifacts']))

    def test_wrong_unit_preserves_raw_but_does_not_admit_artifacts(self):
        request=self.prepare(); result=self.result(request); result['unit_id']='OTHER'; self.save(self.result_path,result)
        handoff=self.consume(request)
        self.assertTrue(handoff['validation_issues']); self.assertFalse(handoff['result_identity_matches'])
        self.assertEqual([],handoff['artifacts']); self.assertEqual(result,handoff['reported_result'])

    def test_missing_wrong_hash_and_unexpected_output_path(self):
        request=self.prepare()
        for variant in ['missing','hash','unexpected']:
            with self.subTest(variant=variant):
                result=self.result(request)
                if variant=='missing': self.output.unlink()
                elif variant=='hash': self.output.write_bytes(b'different')
                else: result['outputs'][0]['path']=str(self.input); self.save(self.result_path,result)
                before=self.snapshot(); handoff=self.consume(request)
                self.assertTrue(handoff['validation_issues']); self.assertFalse(handoff['artifacts']); self.assertEqual(before,self.snapshot())

    def test_tampered_request_or_wrong_expected_identity_retains_raw_result(self):
        request=self.prepare(); self.result(request)
        wrong=work.consume(self.prepared_path,self.result_path,expected_request_id='0'*64)
        self.assertTrue(wrong['validation_issues']); self.assertFalse(wrong['result_identity_matches'])
        self.assertEqual([],wrong['artifacts']); self.assertTrue(wrong['raw_result']['base64'])
        request['dispatch']['target']='/root/other'; self.save(self.prepared_path,request)
        handoff=self.consume(request); self.assertTrue(handoff['validation_issues']); self.assertTrue(handoff['raw_result']['base64'])
        self.assertEqual([],handoff['artifacts'])

    def test_unknown_or_incomplete_effect_needs_reconciliation_without_losing_artifacts(self):
        request=self.prepare()
        for status,effect in [('unknown','confirmed'),('partial','confirmed'),('succeeded','unknown'),('succeeded','partial')]:
            with self.subTest(status=status,effect=effect):
                result=self.result(request,status,effect); handoff=self.consume(request)
                self.assertEqual(result,handoff['reported_result']); self.assertEqual(1,len(handoff['artifacts']))
                self.assertTrue(handoff['effect_requires_reconciliation']); self.assertTrue(handoff['requires_reconciliation'])
        self.result(request,'failed','none'); known_failure=self.consume(request)
        self.assertFalse(known_failure['effect_requires_reconciliation'])
        self.assertFalse(known_failure['requires_reconciliation'])

    def test_empty_subset_and_full_integration_keep_missing_obligations(self):
        other_output=self.root/'second-draft.md'; other_destination=self.root/'second-integrated.md'
        self.spec['outputs'].append({'id':'second','path':str(other_output),'before_sha256':None,'acceptance':'Second real output'})
        self.spec['consumer']['destinations'].append({'output_id':'second','path':str(other_destination)})
        request=self.prepare(); result=self.result(request); other_output.write_bytes(b'Second output')
        result['outputs'].append(dict(self.ref(other_output),id='second')); self.save(self.result_path,result)
        self.destination.write_bytes(self.output.read_bytes()); other_destination.write_bytes(other_output.read_bytes())
        rows=[{'output_id':'reference','destination':str(self.destination),'sha256':self.ref(self.output)['sha256']},
              {'output_id':'second','destination':str(other_destination),'sha256':self.ref(other_output)['sha256']}]
        observation_path=self.root/'integration.json'
        for count,missing in [(0,['reference','second']),(1,['second']),(2,[])]:
            with self.subTest(count=count):
                self.save(observation_path,{'schema':'work-integration-v1','request_id':request['request_id'],'unit_id':'DOC-1',
                    'consumer_id':'candidate-doc','parent_observation':'Actual parent incorporation','outputs':rows[:count]})
                before=self.snapshot()
                handoff=work.integrate(self.prepared_path,self.result_path,observation_path,expected_request_id=request['request_id'])
                self.assertEqual(before,self.snapshot()); self.assertEqual(missing,handoff['missing_integration_output_ids'])
                self.assertEqual(count,len(handoff['integration_readback'])); self.assertEqual(count==2,handoff['structural_integration_complete'])
                self.assertEqual(count!=2,handoff['requires_reconciliation']); self.assertFalse(handoff['source_outcome_completed'])
        result['status']='unknown'; result['effect_state']='unknown'; self.save(self.result_path,result)
        unknown=work.integrate(self.prepared_path,self.result_path,observation_path,expected_request_id=request['request_id'])
        self.assertTrue(unknown['structural_integration_complete']); self.assertTrue(unknown['requires_reconciliation'])

    def test_invalid_json_and_missing_result_remain_observations(self):
        request=self.prepare(); self.result_path.write_bytes(b'{broken')
        handoff=self.consume(request); self.assertEqual(b'{broken',base64.b64decode(handoff['raw_result']['base64']))
        self.assertTrue(handoff['validation_issues']); self.result_path.unlink()
        handoff=self.consume(request); self.assertIn('read_error',handoff['raw_result']); self.assertTrue(handoff['validation_issues'])

    def test_declared_writes_cannot_alias_sources_or_other_chats(self):
        self.spec['outputs'][0]['path']=str(self.source)
        with self.assertRaises(ValueError): self.prepare()
        self.spec['outputs'][0]['path']=str(self.output); self.spec['target']='codex://threads/other'
        with self.assertRaises(ValueError): self.prepare()

    def test_output_baseline_and_result_collision_prevent_resend_inference(self):
        request=self.prepare(); self.result_path.write_bytes(b'prior unknown result')
        with self.assertRaises(ValueError): work.verify(self.prepared_path,expected_request_id=request['request_id'])
        self.result_path.unlink(); self.output.write_bytes(b'unknown newer output')
        with self.assertRaises(ValueError): work.verify(self.prepared_path,expected_request_id=request['request_id'])

    def test_cli_produces_exact_tool_payload_without_writes(self):
        self.save(self.spec_path,self.spec); before=self.snapshot()
        result=subprocess.run([sys.executable,'-B',str(Path(work.__file__)),'prepare','--spec',str(self.spec_path)],capture_output=True)
        self.assertEqual(0,result.returncode,result.stderr); prepared=json.loads(result.stdout)
        self.assertEqual(before,self.snapshot()); self.assertEqual({'target','message'},set(prepared['dispatch']))
        self.assertEqual(work.prepare(self.spec_path),prepared)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--temp-root',required=True); args,rest=parser.parse_known_args()
    TEMP_ROOT=Path(args.temp_root).resolve(strict=True)
    unittest.main(argv=[sys.argv[0],*rest])
