"""Whole route fixtures; real host delivery and ordinary-task benefit are separate."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

S = Path(__file__).resolve().parents[1] / 'scripts'


def module(name):
    path = S / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


w = module('work_io')
l = module('in_work_learning')
h = module('in_work_hooks')


def digest(raw): return hashlib.sha256(raw).hexdigest().upper()
def ref(p): return {'path': str(p), 'sha256': digest(p.read_bytes())}
def put(p, value): p.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8'); return p


class InWork(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.r = Path(self.temp.name)
        for name in ['source', 'global', 'project', 'method', 'action']:
            (self.r / name).write_text('original ' + name)
        self.spec = {'schema': 'work-spec-v1', 'unit_id': 'one', 'owner_chat_id': 'chat',
            'target': 'internal:worker', 'objective': 'required report', 'instructions': 'Create the requested report only.',
            'source': ref(self.r / 'source'), 'inputs': [],
            'methods': [{'id': 'procedure', 'apply': 'Use its operation.', **ref(self.r / 'method')}],
            'masters': [{'role': role, 'read': 'Current relevant control.', 'apply': 'Preserve scope.', **ref(self.r / role)} for role in ['global', 'project']],
            'scope': 'one report', 'prohibitions': ['No external effects'],
            'outputs': [{'id': 'report', 'path': str(self.r / 'report'), 'before_sha256': None, 'acceptance': 'Requested report'}],
            'result_path': str(self.r / 'result.json'),
            'consumer': {'id': 'final-report', 'description': 'Read report', 'acceptance': 'Semantic acceptance',
                'destinations': [{'output_id': 'report', 'path': str(self.r / 'integrated')}]}}
        self.spec_path = put(self.r / 'spec.json', self.spec)
        self.request = w.prepare(self.spec_path)
        self.prepared = put(self.r / 'prepared.json', self.request)
        (self.r / 'report').write_text('Requested content')
        self.result = {'schema': 'work-result-v1', 'request_id': self.request['request_id'], 'unit_id': 'one',
            'status': 'succeeded', 'effect_state': 'confirmed', 'outputs': [{'id': 'report', **ref(self.r / 'report')}],
            'unresolved': [], 'notes': 'Actual report produced; semantic acceptance separate.', 'first_fault': None}
        self.result_path = put(self.r / 'result.json', self.result)

    def tearDown(self): self.temp.cleanup()
    def consume(self): return w.consume(self.prepared, self.result_path, expected_request_id=self.request['request_id'])

    def test_normal_legacy_result_and_learning_entry(self):
        value = self.consume()
        self.assertTrue(value['result_identity_matches']); self.assertFalse(value['requires_reconciliation'])
        self.assertEqual(value['in_work_learning']['status'], 'succeeded')
        self.assertIn('method-application-not-observed-not-assumed-unused', value['in_work_learning']['signals'])

    def test_malformed_result_keeps_raw_feedback(self):
        for body in [['bad'], {'wrong': True}, {**self.result, 'method_applications': None}]:
            put(self.result_path, body); value = self.consume()
            self.assertTrue(value['validation_issues']); self.assertEqual(value['raw_result']['sha256'], ref(self.result_path)['sha256'])
            self.assertIn('in_work_learning', value)

    def test_unknown_and_stale_result_not_lost(self):
        self.result.update(status='unknown', effect_state='unknown', first_fault='original failure')
        put(self.result_path, self.result); (self.r / 'method').write_text('updated')
        value = self.consume()
        self.assertTrue(value['effect_requires_reconciliation']); self.assertTrue(value['freshness_issues']['required'])
        self.assertEqual(value['in_work_learning']['first_fault'], 'original failure')
        self.assertTrue(value['artifacts'])

    def test_actual_method_report_binding(self):
        self.result['method_applications'] = [{'id': 'procedure', 'reader': 'internal:worker', 'sha256': ref(self.r / 'method')['sha256'],
            'state': 'script-executed', 'action_ref': ref(self.r / 'action'), 'notes': 'Isolated execution evidence.'}]
        put(self.result_path, self.result); self.assertFalse(self.consume()['validation_issues'])
        self.result['method_applications'][0]['reader'] = 'parent'
        put(self.result_path, self.result); self.assertTrue(self.consume()['validation_issues'])

    def test_current_child_and_parent_boundaries(self):
        for actor in ['internal:worker', 'COORDINATED-WORK:chat']:
            value = w.boundary(self.prepared, expected_request_id=self.request['request_id'], actor=actor, read_refs=[], effect_state='known-bounded')
            self.assertEqual(value['next_action'], 'reuse-own-unchanged-coverage')
        (self.r / 'method').write_text('new method')
        value = w.boundary(self.prepared, expected_request_id=self.request['request_id'], actor='internal:worker', read_refs=[], effect_state='in-flight')
        self.assertTrue(value['in_flight_version_preserved']); self.assertTrue(value['changed_reads_missing'])
        value = w.boundary(self.prepared, expected_request_id=self.request['request_id'], actor='internal:worker', read_refs=[ref(self.r / 'method')], effect_state='none')
        self.assertFalse(value['changed_reads_missing']); self.assertTrue(value['reconciliation_required'])
        (self.r / 'global').write_text('unrelated current episode')
        value = w.boundary(self.prepared, expected_request_id=self.request['request_id'], actor='internal:worker', read_refs=[], effect_state='none')
        self.assertFalse(value['execution_performed']); self.assertEqual(value['next_action'], 'reconcile-only-dependent-work')

    def test_next_request_generated_from_real_result(self):
        nxt = json.loads(json.dumps(self.spec)); nxt['unit_id'] = 'two'; nxt['outputs'][0]['path'] = str(self.r / 'report-two')
        nxt['result_path'] = str(self.r / 'result-two.json'); nxt['consumer']['id'] = 'next-consumer'
        put(self.r / 'next.json', nxt)
        meaning = {'disposition': 'reuse', 'cause': 'reuse-opportunity', 'reason': 'Required second report shares the useful method.',
            'expected_change': 'Generate identities from the original result.', 'alternatives': [],
            'effect_reconciliation': 'Prior report is known bounded; no action replay.', 'return_trigger': None}
        put(self.r / 'meaning.json', meaning)
        value = w.build_next(self.r / 'next.json', self.prepared, self.result_path, self.r / 'meaning.json',
            expected_request_id=self.request['request_id'], output_root=self.r / 'generated')
        self.assertEqual(value['disposition']['disposition'], 'reuse')
        self.assertEqual(len(value['prepared']['spec']['inputs']), 3)
        generated = put(self.r / 'next-prepared.json', value['prepared'])
        self.assertFalse(w.verify(generated, expected_request_id=value['prepared']['request_id'])['permission_granted'])
        with self.assertRaises(ValueError):
            w.build_next(self.r / 'next.json', self.prepared, self.result_path, self.r / 'meaning.json',
                expected_request_id=self.request['request_id'], output_root=self.r / 'generated')

    def test_no_skill_and_concrete_new_or_no_change_disposition(self):
        ctx = l.result_context(self.consume(), [])
        self.assertIn('no-selected-skill-does-not-exclude-a-useful-new-operation', ctx['signals'])
        meaning = {'disposition': 'create', 'cause': 'reuse-opportunity', 'reason': 'A needed operation has no suitable current procedure.',
            'expected_change': 'Useful typed operation for next work', 'alternatives': ['No current applicable generator; compare instruction-only.'],
            'effect_reconciliation': 'Known bounded', 'return_trigger': None}
        value = l.build_decision(ctx, self.spec, meaning); self.assertEqual(value['disposition'], 'create')
        value['disposition'] = 'defer'
        with self.assertRaises(ValueError): l.validate_decision(value, context=ctx, owner_chat_id='chat', next_source_sha256=self.spec['source']['sha256'])

    def test_relation_opportunity_preserves_exclusion(self):
        episode = {'id': 'one', 'family_keys': ['assembly'], 'evidence_ref': ref(self.result_path),
            'next_consumer': 'report', 'applied_ids': [], 'excluded_ids': []}
        procedure = {'id': 'generator', 'family_keys': ['assembly'], 'evidence_ref': ref(self.r / 'method')}
        self.assertEqual(len(l.relation_gaps([episode], [procedure])), 1)
        episode['excluded_ids'] = ['generator']; self.assertEqual(l.relation_gaps([episode], [procedure]), [])


class Hooks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.r = Path(self.temp.name)
        self.checker = self.r / 'checker.ps1'; self.checker.write_text('# known checker')
        self.config = {'observation_root': str(self.r), 'default_shell': 'powershell', 'powershell_executable': 'powershell',
            'powershell_checker': ref(self.checker), 'skills_root': 'C:/owned/skills', 'instruction_path': 'C:/owned/skills/lifecycle/references/in-work.md'}
        self.payload = {'hook_event_name': 'PreToolUse', 'session_id': 'shared', 'turn_id': 'one', 'tool_use_id': 'tool',
            'tool_name': 'Bash', 'tool_input': {'command': 'Get-Date'}}
    def tearDown(self): self.temp.cleanup()

    def test_normal_and_uncovered_calls_do_not_execute_checker(self):
        with mock.patch.object(h.subprocess, 'run') as run:
            self.assertEqual(h.handle(self.payload, self.config), {})
            self.payload['tool_input'] = {'command': 'foreach (x) {}', 'shell': 'bash'}
            self.assertEqual(h.handle(self.payload, self.config), {}); run.assert_not_called()

    def test_exact_known_deny_not_rewrite(self):
        self.payload['tool_input']['command'] = 'foreach ($x in 1) { $x } | Sort-Object'
        response = {'decision': 'BLOCK', 'members': [{'findings': [{'constraint_id': 'PS-FOREACH-PIPE-001'}]}]}
        with mock.patch.object(h.subprocess, 'run', return_value=mock.Mock(returncode=2, stdout=json.dumps(response).encode())):
            value = h.handle(self.payload, self.config)
        self.assertEqual(value['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertNotIn('updatedInput', value['hookSpecificOutput'])
        records = list(self.r.glob('*/history/*.json')); self.assertEqual(len(records), 1)
        self.assertNotIn('$x', records[0].read_text()); self.assertIn('unverified-shared-session', records[0].read_text())

    def test_post_failure_preserves_original_and_no_ordinary_noise(self):
        self.payload.update(hook_event_name='PostToolUse', tool_response={'exit_code': 0, 'output': 'secret text'})
        self.assertEqual(h.handle(self.payload, self.config), {})
        self.payload['tool_response']['exit_code'] = 1
        original = json.loads(json.dumps(self.payload)); value = h.handle(self.payload, self.config)
        self.assertEqual(self.payload, original); self.assertNotIn('decision', value); self.assertNotIn('continue', value)
        self.assertNotIn('secret text', list(self.r.glob('*/history/*.json'))[0].read_text())

    def test_resume_child_context_never_claims_actor_authority(self):
        for event in ['SessionStart', 'SubagentStart']:
            self.payload['hook_event_name'] = event
            self.payload['source'] = 'compact'
            self.payload['tool_response'] = {'private': 'never-store-this-body'}
            value = h.handle(self.payload, self.config)
            self.assertIn('Parent reading does not certify child reading', value['hookSpecificOutput']['additionalContext'])
            self.assertNotIn('continue', value)
        records = list(self.r.glob('*/history/*.json'))
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertNotIn('never-store-this-body', record.read_text())
            body = json.loads(record.read_bytes())
            self.assertEqual(body['actor'], 'unverified-shared-session')
            self.assertEqual(body['semantic_effect'], 'not-observed')
        self.payload['hook_event_name'] = 'PostCompact'
        self.assertEqual(h.handle(self.payload, self.config), {})


if __name__ == '__main__': unittest.main()
