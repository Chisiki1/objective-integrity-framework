"""Bounded structural normal paths and bypasses; no live effect/semantic proof."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest


SCRIPT = Path(__file__).with_name('work_phase.py')
phase = types.ModuleType('work_phase_under_test')
phase.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_bytes(), str(SCRIPT), 'exec'), phase.__dict__)


class WorkPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='source-wide-engine-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.file('source.txt', 'Implement the entire source outcome, including all consumers.\n')
        self.candidate = self.file('candidate.txt', 'Owned baseline/planned manifest in BUILD; frozen candidate thereafter.\n')
        self.evidence = self.file('evidence.txt', 'A bounded synthetic observation; no real environment proof.\n')
        self.owner = self.file('owner-review.txt', 'Synthetic owner review reference.\n')
        self.independent = self.file('independent-review.txt', 'Synthetic distinct review reference, not independence proof.\n')
        self.scope = {
            'schema': 'source-wide-scope-v1', 'objective_id': 'O-test', 'owner_chat_id': 'owner-test',
            'source_ref': self.source,
            'requirements': [
                {'id': 'r1', 'source_clause': 'entire source outcome', 'description': 'Core implementation'},
                {'id': 'r2', 'source_clause': 'all consumers', 'description': 'All downstream consumers'}],
            'implementation_items': [
                {'id': 'core', 'requirement_ids': ['r1'], 'description': 'Implement core'},
                {'id': 'direct', 'requirement_ids': ['r2'], 'description': 'Connect owner action'},
                {'id': 'dispatch', 'requirement_ids': ['r2'], 'description': 'Connect child dispatch'}],
            'checks': [
                {'id': 'local-core', 'requirement_ids': ['r1'], 'stage': 'local', 'description': 'Core behavior'},
                {'id': 'local-consumers', 'requirement_ids': ['r2'], 'stage': 'local', 'description': 'Consumer integration'},
                {'id': 'future', 'requirement_ids': ['r1', 'r2'], 'stage': 'later', 'description': 'Later evidence retained'}],
            'coverage_review': {'owner_ref': None, 'independent_ref': None},
        }
        self.state = {
            'schema': 'source-wide-state-v1', 'objective_id': 'O-test', 'owner_chat_id': 'owner-test',
            'source_sha256': self.source['sha256'], 'completion_scope_ref': None,
            'phase': 'BUILD', 'current_stage': 'local', 'candidate_ref': self.candidate,
            'implementation': [{'id': key, 'status': 'pending', 'evidence_ref': None}
                for key in ('core', 'direct', 'dispatch')],
            'checks': [{'id': key, 'status': 'pending', 'evidence_ref': None, 'reason': ''}
                for key in ('local-core', 'local-consumers', 'future')],
            'findings': [], 'findings_closed': False,
        }

    def file(self, name, value):
        path = self.root / name
        raw = value.encode('utf-8')
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest().upper()}

    def json_file(self, name, value):
        return self.file(name, json.dumps(value, ensure_ascii=True, allow_nan=False))

    def binding(self, action='IMPLEMENT', *, items=None, checks=None, findings=None, feedback=None):
        scope_ref = self.json_file('scope.json', self.scope)
        self.state['completion_scope_ref'] = scope_ref
        state_ref = self.json_file('state.json', self.state)
        return {'state_ref': state_ref, 'completion_scope_ref': scope_ref, 'action': action,
            'item_ids': (['core'] if action == 'IMPLEMENT' else []) if items is None else items,
            'check_ids': [] if checks is None else checks,
            'finding_ids': [] if findings is None else findings, 'feedback': feedback}

    def evaluate(self, binding):
        return phase.evaluate(binding, objective_id='O-test',
            source_sha256=self.source['sha256'], owner_chat_id='owner-test')

    def complete_implementation(self):
        for item in self.state['implementation']:
            item.update(status='complete', evidence_ref=self.evidence)
        self.scope['coverage_review'] = {'owner_ref': self.owner, 'independent_ref': self.independent}

    def observed(self, identity, status, reason=''):
        check = next(check for check in self.state['checks'] if check['id'] == identity)
        check.update(status=status, reason=reason,
            evidence_ref=None if status == 'pending' else self.evidence)

    def finding(self, identity='f-core', check='local-core', cause='shared-cause',
            consumers=('core', 'direct'), classification='mandatory', status='open'):
        self.state['findings'].append({'id': identity, 'check_id': check,
            'classification': classification, 'basis': 'Source requirement or declared optional value',
            'root_cause': cause, 'consumer_item_ids': list(consumers), 'status': status,
            'evidence_ref': self.evidence})

    def closed_repair(self):
        self.complete_implementation()
        self.state.update(phase='REPAIR', findings_closed=True)
        self.observed('local-core', 'fail')
        self.observed('local-consumers', 'blocked', 'BLOCKED-BY-FIRST-FAULT: shared-cause')
        self.finding()
        self.finding('f-dispatch', 'local-consumers', consumers=('dispatch',))

    def feedback(self, **changes):
        result = {'decision': 'Choose between two internal representations with one semantic probe',
            'return_step': 'Implement remaining consumer connections',
            'cost_disposition': 'One bounded probe; no full suite or product build',
            'effects_disposition': 'known-bounded', 'effect_class': 'owned-local',
            'output_ownership': 'Owned temporary output; existing useful shared compiler cache retained',
            'applicability': 'construction'}
        result.update(changes)
        return result

    def test_normal_internal_subset_advances_without_whole_completion(self):
        result = self.evaluate(self.binding(items=['direct']))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['pending_implementation'], ['core', 'direct', 'dispatch'])
        self.assertFalse(result['source_outcome_completed'])
        self.assertFalse(result['permission_granted'])

    def test_build_baseline_and_null_reviews_are_normal(self):
        result = self.evaluate(self.binding())
        self.assertTrue(result['admitted'])
        self.assertEqual(result['candidate_ref'], self.candidate)

    def test_scope_review_is_available_before_implementation_and_audit(self):
        result = self.evaluate(self.binding('SCOPE_REVIEW'))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['next_actions'], ['IMPLEMENT'])

    def test_useful_shared_cache_and_semantic_feedback_are_allowed(self):
        result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))
        self.assertTrue(result['admitted'])
        self.assertIn('shared compiler cache retained', result['declared_feedback']['output_ownership'])
        self.assertEqual(result['pending_checks'], ['local-core', 'local-consumers'])

    def test_feedback_does_not_keyword_match_truthful_no_regression(self):
        result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback(
            decision='No accepted product regression; distinguish local representations')))
        self.assertTrue(result['admitted'])

    def test_feedback_unknown_harmful_external_effects_require_existing_route(self):
        for disposition in ('unknown', 'harmful', 'external'):
            with self.subTest(disposition=disposition):
                feedback = self.feedback(effects_disposition=disposition)
                result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=feedback))
                self.assertFalse(result['admitted'])
                self.assertTrue(result['dependent_route_required'])
                self.assertEqual(result['declared_feedback'], feedback)
                self.assertEqual(result['next_actions'], [])

    def test_product_regression_and_essential_design_failure_are_not_feedback(self):
        for applicability in ('accepted-product-regression', 'essential-design-failure'):
            with self.subTest(applicability=applicability):
                result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK',
                    feedback=self.feedback(applicability=applicability)))
                self.assertFalse(result['admitted'])
                self.assertTrue(result['dependent_route_required'])

    def test_nonowned_effect_class_cannot_enter_feedback_route(self):
        result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK',
            feedback=self.feedback(effect_class='uncontrolled-build-script')))
        self.assertFalse(result['admitted'])

    def test_feedback_requires_bounded_decision_return_cost_effect_and_ownership(self):
        for field in self.feedback():
            with self.subTest(field=field):
                feedback = self.feedback()
                del feedback[field]
                with self.assertRaises(ValueError):
                    self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=feedback))

    def test_early_formal_check_returns_to_whole_implementation(self):
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))
        self.assertFalse(result['admitted'])
        self.assertIn('SOURCE_WIDE_IMPLEMENTATION_INCOMPLETE', result['holds'])
        self.assertEqual(result['next_actions'], ['IMPLEMENT'])

    def test_finished_core_does_not_hide_pending_consumers(self):
        self.state['implementation'][0].update(status='complete', evidence_ref=self.evidence)
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))
        self.assertFalse(result['admitted'])
        self.assertEqual(result['pending_implementation'], ['direct', 'dispatch'])

    def test_full_state_sets_cannot_be_replaced_by_child_subset(self):
        for field in ('implementation', 'checks'):
            with self.subTest(field=field):
                original = self.state[field]
                self.state[field] = original[:1]
                with self.assertRaisesRegex(ValueError, 'complete scope'):
                    self.evaluate(self.binding())
                self.state[field] = original

    def test_unmapped_requirement_rejected_for_both_item_and_check_coverage(self):
        for field in ('implementation_items', 'checks'):
            with self.subTest(field=field):
                original = copy.deepcopy(self.scope[field])
                for record in self.scope[field]:
                    record['requirement_ids'] = ['r1']
                with self.assertRaisesRegex(ValueError, 'coverage'):
                    self.evaluate(self.binding())
                self.scope[field] = original

    def test_owner_and_source_expectations_cannot_be_swapped(self):
        binding = self.binding()
        for args in ({'owner_chat_id': 'child-owner'}, {'objective_id': 'child-objective'},
                {'source_sha256': '0' * 64}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                phase.evaluate(binding, **args)

    def test_scope_state_owner_and_objective_must_agree(self):
        for field in ('owner_chat_id', 'objective_id'):
            with self.subTest(field=field):
                original = self.state[field]
                self.state[field] = 'wrong'
                with self.assertRaises(ValueError):
                    self.evaluate(self.binding())
                self.state[field] = original

    def test_stale_state_source_and_candidate_files_rejected(self):
        for name in ('state.json', 'source.txt', 'candidate.txt'):
            with self.subTest(name=name):
                binding = self.binding()
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with self.assertRaisesRegex(ValueError, 'stale file hash'):
                    self.evaluate(binding)
                path.write_bytes(original)

    def test_source_change_with_new_hash_still_requires_state_and_expected_identity(self):
        new_source = self.file('new-source.txt', 'Different source amendment, requiring owner lineage.\n')
        self.scope['source_ref'] = new_source
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())
        self.state['source_sha256'] = new_source['sha256']
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())
        result = phase.evaluate(self.binding(), source_sha256=new_source['sha256'],
            owner_chat_id='owner-test', objective_id='O-test')
        self.assertTrue(result['admitted'])
        self.assertFalse(result['permission_granted'])

    def test_invalid_schemas_and_phases_rejected(self):
        for target, field in ((self.state, 'schema'), (self.scope, 'schema'), (self.state, 'phase')):
            with self.subTest(field=field):
                original = target[field]
                target[field] = 'invented'
                with self.assertRaises(ValueError):
                    self.evaluate(self.binding())
                target[field] = original

    def test_scope_complete_boolean_cannot_replace_mapping(self):
        self.scope['complete'] = True
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())

    def test_duplicate_record_ids_and_unknown_relations_rejected(self):
        self.scope['requirements'].append(copy.deepcopy(self.scope['requirements'][0]))
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())
        self.scope['requirements'].pop()
        self.scope['implementation_items'][0]['requirement_ids'] = ['absent']
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())

    def test_duplicate_json_and_nonfinite_json_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                phase._json(raw.encode())
        binding = self.binding()
        binding['feedback'] = {'value': float('nan')}
        with self.assertRaises(ValueError):
            self.evaluate(binding)

    def test_duplicate_fields_in_referenced_scope_rejected(self):
        binding = self.binding()
        raw = (self.root / 'scope.json').read_text()
        duplicate = raw.replace('"schema":', '"schema":"source-wide-scope-v1","schema":', 1)
        ref = self.file('scope.json', duplicate)
        binding['completion_scope_ref'] = ref
        self.state['completion_scope_ref'] = ref
        binding['state_ref'] = self.json_file('state.json', self.state)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            self.evaluate(binding)

    def test_relative_directory_and_missing_references_fail_closed_as_valueerror(self):
        for value in ('source.txt', str(self.root), str(self.root / 'missing')):
            with self.subTest(value=value):
                binding = self.binding()
                binding['state_ref']['path'] = value
                with self.assertRaises(ValueError):
                    self.evaluate(binding)

    def test_linked_paths_rejected_when_host_supports_symlinks(self):
        target = self.root / 'source-link.txt'
        try:
            target.symlink_to(self.root / 'source.txt')
        except OSError as error:
            self.skipTest('host cannot create symlink: ' + str(error))
        self.scope['source_ref'] = {**self.source, 'path': str(target)}
        with self.assertRaisesRegex(ValueError, 'linked/reparse'):
            self.evaluate(self.binding())

    def test_independent_review_cannot_alias_owner_by_path_or_hardlink(self):
        self.complete_implementation()
        self.state['phase'] = 'SWEEP'
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                ref = dict(self.owner)
                if hardlink:
                    alias = self.root / 'review-hardlink.txt'
                    os.link(self.owner['path'], alias)
                    ref['path'] = str(alias)
                self.scope['coverage_review']['independent_ref'] = ref
                with self.assertRaisesRegex(ValueError, 'alias'):
                    self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))

    def test_current_stage_must_exist_in_complete_scope(self):
        self.state['current_stage'] = 'child-only-stage'
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())

    def test_required_selections_are_nonempty_and_all_ids_known(self):
        for action in ('IMPLEMENT', 'FORMAL_CHECK', 'REPAIR_FINDINGS'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.evaluate(self.binding(action, items=[]))
        for selection in ({'items': ['missing']}, {'items': ['core', 'core']}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.evaluate(self.binding(**selection))

    def test_completed_item_cannot_be_selected_as_pending_implementation(self):
        self.state['implementation'][0].update(status='complete', evidence_ref=self.evidence)
        self.assertFalse(self.evaluate(self.binding())['admitted'])

    def test_complete_implementation_and_observed_checks_require_evidence(self):
        self.state['implementation'][0]['status'] = 'complete'
        with self.assertRaises(ValueError):
            self.evaluate(self.binding())
        self.state['implementation'][0]['status'] = 'pending'
        for status in ('pass', 'fail', 'blocked'):
            with self.subTest(status=status):
                self.state['checks'][0]['status'] = status
                with self.assertRaises(ValueError):
                    self.evaluate(self.binding())

    def test_nonbuild_requires_both_coverage_refs_but_not_a_complete_flag(self):
        self.complete_implementation()
        self.state['phase'] = 'SWEEP'
        self.scope['coverage_review']['independent_ref'] = None
        with self.assertRaises(ValueError):
            self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))

    def test_formal_entry_requires_all_connections_in_nonbuild_too(self):
        self.complete_implementation()
        self.state['phase'] = 'SWEEP'
        self.state['implementation'][-1].update(status='pending', evidence_ref=None)
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))
        self.assertFalse(result['admitted'])
        self.assertEqual(result['next_actions'], ['IMPLEMENT'])

    def test_normal_sweep_selects_pending_current_stage_only(self):
        self.complete_implementation()
        self.state['phase'] = 'SWEEP'
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core', 'local-consumers']))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['deferred_checks'], ['future'])
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['future']))
        self.assertFalse(result['admitted'])

    def test_first_finding_during_unfinished_sweep_cannot_start_repair(self):
        self.complete_implementation()
        self.state['phase'] = 'SWEEP'
        self.observed('local-core', 'fail')
        self.finding()
        result = self.evaluate(self.binding('REPAIR_FINDINGS',
            items=['core', 'direct'], findings=['f-core']))
        self.assertFalse(result['admitted'])
        self.assertEqual(result['next_actions'], ['FORMAL_CHECK'])
        self.assertTrue(self.evaluate(self.binding('COLLECT_FINDINGS'))['admitted'])

    def test_forced_repair_phase_does_not_bypass_unfinished_sweep(self):
        self.complete_implementation()
        self.state.update(phase='REPAIR', findings_closed=True)
        self.observed('local-core', 'fail')
        self.finding()
        result = self.evaluate(self.binding('REPAIR_FINDINGS', items=['core', 'direct'], findings=['f-core']))
        self.assertFalse(result['admitted'])
        self.assertIn('CURRENT_STAGE_COLLECTION_INCOMPLETE', result['holds'])

    def test_collected_current_stage_repairs_without_future_evidence(self):
        self.closed_repair()
        result = self.evaluate(self.binding('REPAIR_FINDINGS',
            items=['core', 'direct', 'dispatch'], findings=['f-core', 'f-dispatch']))
        self.assertTrue(result['admitted'])
        self.assertTrue(result['current_stage_collected'])
        self.assertEqual(result['deferred_checks'], ['future'])
        self.assertFalse(result['source_outcome_completed'])

    def test_repair_feedback_keeps_current_stage_collection_requirement(self):
        self.closed_repair()
        self.assertTrue(self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))['admitted'])
        self.state['findings_closed'] = False
        self.assertFalse(self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))['admitted'])

    def test_blocked_first_fault_requires_reason_and_does_not_become_pass(self):
        self.closed_repair()
        self.state['checks'][1]['reason'] = ''
        with self.assertRaises(ValueError):
            self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))
        self.state['checks'][1]['reason'] = 'BLOCKED-BY-FIRST-FAULT'
        self.state['phase'] = 'ACCEPT'
        result = self.evaluate(self.binding('ACCEPT'))
        self.assertFalse(result['admitted'])
        self.assertIn('local-consumers', result['incomplete_current_checks'])

    def test_pass_label_without_evidence_cannot_fabricate_observation(self):
        self.complete_implementation()
        self.state.update(phase='ACCEPT', findings_closed=True)
        for check in self.state['checks']:
            check['status'] = 'pass'
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.evaluate(self.binding('ACCEPT'))

    def test_grouped_repair_requires_all_same_cause_findings_and_consumers(self):
        self.closed_repair()
        for items, findings in ((['core', 'direct'], ['f-core']),
                (['core', 'direct'], ['f-core', 'f-dispatch']),
                (['core', 'direct', 'dispatch'], ['f-core'])):
            with self.subTest(items=items, findings=findings):
                result = self.evaluate(self.binding('REPAIR_FINDINGS', items=items, findings=findings))
                self.assertFalse(result['admitted'])

    def test_multiple_complete_cause_groups_can_share_one_repair_action(self):
        self.closed_repair()
        self.state['findings'][1]['root_cause'] = 'second-cause'
        result = self.evaluate(self.binding('REPAIR_FINDINGS',
            items=['core', 'direct', 'dispatch'], findings=['f-core', 'f-dispatch']))
        self.assertTrue(result['admitted'])
        result = self.evaluate(self.binding('REPAIR_FINDINGS', items=['core', 'direct'], findings=['f-core']))
        self.assertTrue(result['admitted'])

    def test_multiple_groups_still_reject_partial_group_coverage(self):
        self.closed_repair()
        self.finding('f-second-cause', 'local-core', cause='second-cause', consumers=('core',))
        result = self.evaluate(self.binding('REPAIR_FINDINGS',
            items=['core', 'direct', 'dispatch'], findings=['f-core', 'f-second-cause']))
        self.assertFalse(result['admitted'])

    def test_findings_closed_cannot_omit_observed_failure(self):
        self.closed_repair()
        self.state['findings'] = []
        with self.assertRaisesRegex(ValueError, 'omits a failed'):
            self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))

    def test_invalid_finding_basis_cause_consumer_or_evidence_rejected(self):
        self.closed_repair()
        for field, value in (('basis', ''), ('root_cause', ''), ('consumer_item_ids', []),
                ('consumer_item_ids', ['absent']), ('evidence_ref', None), ('check_id', 'absent')):
            with self.subTest(field=field):
                original = self.state['findings'][0][field]
                self.state['findings'][0][field] = value
                with self.assertRaises(ValueError):
                    self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))
                self.state['findings'][0][field] = original

    def test_optional_only_findings_do_not_create_mandatory_repair(self):
        self.complete_implementation()
        self.state.update(phase='ACCEPT', findings_closed=True)
        for check in self.state['checks']:
            self.observed(check['id'], 'pass')
        self.finding(classification='optional')
        result = self.evaluate(self.binding('ACCEPT'))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['mandatory_findings'], [])
        self.assertEqual(result['optional_findings'], ['f-core'])
        self.assertEqual(result['next_actions'], ['ACCEPT'])

    def test_optional_findings_cannot_be_selected_as_mandatory_repair(self):
        self.closed_repair()
        for finding in self.state['findings']:
            finding['classification'] = 'optional'
        result = self.evaluate(self.binding('REPAIR_FINDINGS',
            items=['core', 'direct', 'dispatch'], findings=['f-core', 'f-dispatch']))
        self.assertFalse(result['admitted'])
        self.assertNotIn('REPAIR_FINDINGS', result['next_actions'])

    def test_affected_resweep_preserves_resolved_findings_and_unaffected_passes(self):
        self.closed_repair()
        for finding in self.state['findings']:
            finding['status'] = 'resolved'
        self.state.update(phase='SWEEP', findings_closed=False)
        self.observed('local-core', 'pending')
        self.observed('local-consumers', 'pass')
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['local-core']))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['pending_checks'], ['local-core'])
        self.assertEqual(result['mandatory_findings'], [])
        self.assertFalse(self.evaluate(self.binding('FORMAL_CHECK', checks=['local-consumers']))['admitted'])

    def test_resolved_findings_recommend_affected_recheck_not_silent_stop(self):
        self.closed_repair()
        for finding in self.state['findings']:
            finding['status'] = 'resolved'
        result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))
        self.assertEqual(result['next_actions'], ['FORMAL_CHECK'])
        self.assertEqual(result['affected_recheck_ids'], ['local-core'])
        self.assertEqual(result['blocked_check_ids'], ['local-consumers'])
        self.assertIn('mark eligible affected checks pending', result['owner_state_transition_required'])
        self.assertEqual(result['blocked_dependencies'][0]['reason'], 'BLOCKED-BY-FIRST-FAULT: shared-cause')

    def test_blocked_only_requires_actual_dependency_change_not_invented_safe_check(self):
        self.closed_repair()
        for finding in self.state['findings']:
            finding['status'] = 'resolved'
        self.observed('local-core', 'pass')
        result = self.evaluate(self.binding('CONSTRUCTION_FEEDBACK', feedback=self.feedback()))
        self.assertEqual(result['next_actions'], [])
        self.assertIn('Resolve the actual dependent', result['owner_state_transition_required'])

    def test_input_refs_include_all_validated_bindings_for_consumer_ownership_checks(self):
        self.closed_repair()
        binding = self.binding('REPAIR_FINDINGS', items=['core', 'direct', 'dispatch'],
            findings=['f-core', 'f-dispatch'])
        result = self.evaluate(binding)
        self.assertEqual({ref['path'] for ref in result['input_refs']}, {
            binding['state_ref']['path'], binding['completion_scope_ref']['path'],
            self.source['path'], self.candidate['path'], self.owner['path'],
            self.independent['path'], self.evidence['path']})

    def test_later_stage_can_be_selected_without_rechecking_unaffected_stage(self):
        self.complete_implementation()
        self.state.update(phase='SWEEP', current_stage='later', findings_closed=False)
        self.observed('local-core', 'pass')
        self.observed('local-consumers', 'pass')
        result = self.evaluate(self.binding('FORMAL_CHECK', checks=['future']))
        self.assertTrue(result['admitted'])
        self.assertEqual(result['pending_checks'], ['future'])
        self.assertEqual(result['deferred_checks'], [])

    def test_acceptance_stays_incomplete_with_later_evidence_missing(self):
        self.complete_implementation()
        self.state.update(phase='ACCEPT', findings_closed=True)
        self.observed('local-core', 'pass')
        self.observed('local-consumers', 'pass')
        result = self.evaluate(self.binding('ACCEPT'))
        self.assertFalse(result['admitted'])
        self.assertEqual(result['deferred_checks'], ['future'])
        self.assertIn('REQUIRED_CHECKS_INCOMPLETE', result['holds'])

    def test_complete_normal_acceptance_has_only_structural_ceiling(self):
        self.complete_implementation()
        self.state.update(phase='ACCEPT', findings_closed=True)
        for check in self.state['checks']:
            self.observed(check['id'], 'pass')
        result = self.evaluate(self.binding('ACCEPT'))
        self.assertTrue(result['admitted'])
        self.assertFalse(result['source_outcome_completed'])
        self.assertIn('no semantic completeness', result['proof_ceiling'])
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_closed_collection_is_required_even_with_all_pass_labels(self):
        self.complete_implementation()
        self.state['phase'] = 'ACCEPT'
        for check in self.state['checks']:
            self.observed(check['id'], 'pass')
        result = self.evaluate(self.binding('ACCEPT'))
        self.assertFalse(result['admitted'])
        self.assertIn('FINDINGS_COLLECTION_NOT_CLOSED', result['holds'])

    def test_evaluation_does_not_write_or_mutate_inputs(self):
        binding = self.binding()
        original = copy.deepcopy(binding)
        files = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = self.evaluate(binding)
        self.assertTrue(result['admitted'])
        self.assertEqual(binding, original)
        self.assertEqual(files, {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_path_and_handle_ctime_semantics_do_not_reject_unchanged_file(self):
        # Synthetic metadata discriminator for the actual Windows first fault;
        # same-API ctime checks in _read_plain remain intact.
        path_info = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=3,
            st_mtime_ns=4, st_ctime_ns=5)
        handle_info = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=3,
            st_mtime_ns=4, st_ctime_ns=6)
        self.assertEqual(phase._stamp(path_info), phase._stamp(handle_info))
        actual, raw, _ = phase._read_plain(self.source['path'])
        self.assertEqual(actual, self.source)
        self.assertEqual(hashlib.sha256(raw).hexdigest().upper(), self.source['sha256'])

    def test_cli_emits_json_and_nonzero_for_held_or_invalid_input(self):
        for action, expected in (('IMPLEMENT', 0), ('FORMAL_CHECK', 2)):
            with self.subTest(action=action):
                binding = self.binding(action, checks=['local-core'] if action == 'FORMAL_CHECK' else [])
                ref = self.json_file('input.json', binding)
                run = subprocess.run([sys.executable, '-B', str(SCRIPT), '--input', ref['path']],
                    capture_output=True, text=True, check=False)
                self.assertEqual(run.returncode, expected, run.stderr)
                self.assertFalse(json.loads(run.stdout)['permission_granted'])
        ref = self.file('input.json', '{"action":"IMPLEMENT","action":"ACCEPT"}')
        run = subprocess.run([sys.executable, '-B', str(SCRIPT), '--input', ref['path']],
            capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 2)
        self.assertFalse(json.loads(run.stderr)['admitted'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
