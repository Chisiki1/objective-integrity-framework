"""Real builder/compiler/resolver to selected Python consumer; no empirical-generalization claim."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / 'skill_application_bridge.py'
def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
def digest(v):
    return hashlib.sha256(v).hexdigest().upper()

class BridgeTests(unittest.TestCase):
    def test_real_selection_to_consumer_and_counterexamples(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill = root / 'selected'
            skill.mkdir()
            script = skill / 'action.py'
            script.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text("consumer-result", encoding="utf-8")\nsys.stdout.buffer.write(bytes([255,254]))\n', encoding='utf-8')
            (skill/'SKILL.md').write_text('---\nname: selected\ndescription: Execute the bounded selected Python consumer.\n---\nUse action.py with the exact output path.\n', encoding='utf-8')
            marker = root / 'consumer.txt'
            def write(name, data):
                p = root / name
                p.write_bytes(canonical(data))
                return {'path': str(p), 'sha256': digest(p.read_bytes())}
            def call(file, *args, codes=(0,)):
                r = subprocess.run([sys.executable, '-B', str(HERE/file), *args], capture_output=True)
                self.assertIn(r.returncode, codes, (r.stdout,r.stderr))
                if not r.stdout and '--output' in args:
                    return json.loads(Path(args[args.index('--output')+1]).read_bytes())
                self.assertTrue(r.stdout, (file, r.returncode, r.stderr))
                return json.loads(r.stdout)
            files = [{'path': x.relative_to(root).as_posix(), 'sha256': digest(x.read_bytes()), 'size': x.stat().st_size} for x in sorted(skill.iterdir())]
            cut_hash = digest(canonical(files))
            manifest = write('manifest.json', dict(files=files, member_set_sha256=cut_hash, managed_roots=['selected']))
            entry = {
                'skill_id': 'SKILL-TEST-BOUND-SCRIPT-001',
                'name': 'selected',
                'version': '1.0.0',
                'origin': 'user',
                'relative_path': 'selected',
                'status': 'active-bounded',
                'disclosure_class': 'blind_safe_mechanical',
                'match_clauses': [{'job': ['bounded-script-consumer']}],
                'required_authority': ['explicit local test authority'],
                'required_inputs': ['finalized typed payload'],
                'resource_claims': ['write one temporary marker'],
                'expected_delta': 'create the requested temporary marker',
                'proof_ceiling': 'synthetic selected-script process result only',
                'cost': 'one bounded local process',
                'revalidation': ['payload or selected file change'],
                'rollback': 'remove the temporary fixture root',
                'master_links': ['G-EVT-TEST-SKILLBOOK-001'],
                'files': [
                    {'path': x.name, 'sha256': digest(x.read_bytes())}
                    for x in sorted(skill.iterdir())
                ],
            }
            registry = write('registry.json', {'schema_version':'mgskill-registry-v1','registry_id':'TEST-BOUND-SCRIPT','entries':[entry]})
            source = write('source.json', {'clauses':[{'id':'SRC-1','text':'Run a bounded selected script to create the required local consumer artifact.'}]})
            payload = dict(facts={'job':['bounded-script-consumer']}, script_path=str(script), arguments=[str(marker)], execute=True)
            schema = dict(status='available', identity='bound-script-payload-v1', schema=dict(type='object',properties=dict(facts=dict(type='object',properties=dict(job=dict(type='array',items=dict(type='string'))),required=['job'],additionalProperties=False),script_path=dict(type='string'),arguments=dict(type='array',items=dict(type='string')),execute=dict(type='boolean')),required=['facts','script_path','arguments','execute'],additionalProperties=False))
            plan = dict(schema_version='mgskill-fact-builder-plan-v1',objective_id='objective',current_authority={'message':'Isolated verification of user-authorized workflow migration.'},source_document={'path':source['path']},source_claims=['SRC-1'],blind_phase='none',source_records=[dict(id='SRC-1',disposition='no-selection-fact',classification='primary',intent='produce local artifact',mechanism='selected bounded process',evidence='exact fixture source',reason='source is outcome, not tool selection',facts={},excluded_facts={})],action_records=[dict(id='ACT-1',disposition='fact-bearing',evidence='final payload',facts=payload['facts'],excluded_facts={},finality='finalized',final_payload=payload,tool_schema=schema)],negative_selection_challenge=dict(status='completed',countermodel='An unrelated job must not select this script; user outcome text alone does not select it.'))
            def compile_plan(p):
                pp = write('plan.json',p)
                call('build_skill_fact_input.py','--plan',pp['path'],'--registry',registry['path'],'--output',str(root/'built.json'))
                comp = call('compile_skill_facts.py','--source',str(root/'built.json'),'--registry',registry['path'],'--output',str(root/'compiler.json'),'--resolver-input-output',str(root/'input.json'))
                return comp
            compiler = compile_plan(plan)
            selected = call('resolve_skills.py','--input',str(root/'input.json'),'--registry',registry['path'],'--user-root',str(root))
            self.assertEqual(selected['decision'],'selected')
            selection_spec = write('selection.json',selected)
            def spec(p): return {'path':str(p),'sha256':digest(p.read_bytes())}
            bundle = dict(schema_version='skill-application-bundle-v1',candidate_cut=dict(root=str(root),manifest=manifest['path'],manifest_sha256=manifest['sha256'],member_set_sha256=cut_hash),envelope=dict(schema_version='final-action-envelope-v1',objective_id='objective',source_claims=['SRC-1'],action_id='ACT-1',candidate_member_set_sha256=cut_hash,lane=dict(role='COORDINATED-WORK',task_id='chat',chat_id='chat',lease_id='lease',source_sha256=source['sha256']),final_payload=payload,final_payload_sha256=digest(canonical(payload)),selection_snapshot_sha256=selected['selection_snapshot_sha256'],tool_schema=schema),compiler_receipt=spec(root/'compiler.json'),resolver_input=spec(root/'input.json'),selection_receipt=selection_spec,required_read_receipt=write('read.json',dict(schema_version='required-read-receipt-v1',status='HASH_ONLY',selection_snapshot_sha256=selected['selection_snapshot_sha256'],comprehension='UNPROVEN')),script_bindings=[dict(skill_id=entry['skill_id'],relative_path='action.py',sha256=digest(script.read_bytes()),role='action-script')])
            def execute(v):
                p = write('bundle.json',v)
                return call('skill_application_bridge.py','run-script','--bundle',p['path'],codes=(0,1,2))
            actual = execute(bundle)
            self.assertEqual(actual['decision'],'EXECUTED')
            self.assertEqual(marker.read_text(),'consumer-result')
            self.assertEqual(actual['stdout_base64'],'//4=')
            for mutation in (lambda b:b['envelope']['lane'].update(chat_id='other'),lambda b:b['envelope'].update(action_id='OTHER'),lambda b:b['envelope'].update(final_payload_sha256='0'*64)):
                changed = copy.deepcopy(bundle)
                mutation(changed)
                self.assertNotEqual(execute(changed)['decision'],'EXECUTED')
            changed = copy.deepcopy(bundle)
            changed['envelope']['final_payload']['arguments'] = [str(root/'wrong-consumer.txt')]
            changed['envelope']['final_payload_sha256'] = digest(canonical(changed['envelope']['final_payload']))
            self.assertNotEqual(execute(changed)['decision'],'EXECUTED')
            self.assertFalse((root/'wrong-consumer.txt').exists())
            not_final = copy.deepcopy(plan)
            not_final['action_records'][0]['finality']='not-finalized'
            compile_plan(not_final)
            changed = copy.deepcopy(bundle)
            changed['compiler_receipt']=spec(root/'compiler.json')
            self.assertNotEqual(execute(changed)['decision'],'EXECUTED')

if __name__ == '__main__':
    unittest.main()
