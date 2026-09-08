"""Current/history compatibility fixtures; no semantic or live-activation proof."""
import base64
import json
from pathlib import Path
import tempfile
import types
import unittest


SCRIPT = Path(__file__).with_name('inventory_live_masters_v2.py')
m = types.ModuleType('inventory_live_masters_under_test')
m.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_bytes(), str(SCRIPT), 'exec'), m.__dict__)


class InventoryArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.master = self.root / 'GLOBAL_PROJECT_MASTER.md'
        self.project = self.root / 'PROJECT_MASTER.md'
        self.loader, self.loader_ref = m.source_loader()

    def marker(self, path):
        raw = path.read_bytes()
        return b'<!-- master-history-v1 ' + self.loader.canonical({
            'path': str(path), 'sha256': self.loader.sha(raw), 'bytes': len(raw)}) + b' -->\n'

    def test_unchanged_plain_master_keeps_existing_path_and_identity_contract(self):
        raw = b'# PM-EXAMPLE\n## CURRENT CONTROL\nCURRENT_CONTROL_ID: G-CONTROL-X-1\n## G-EVT-X-1\nPROJECT_EVENT_LINK: P-EVT-X-1\n'
        self.master.write_bytes(raw)
        result = m.inventory(self.master)
        self.assertEqual(result['path'], str(self.master.resolve()))
        self.assertEqual(result['sha256'], m.sha256_bytes(raw))
        self.assertIn('G-EVT-X-1', result['heading_ids'])
        self.assertIn('G-CONTROL-X-1', result['current_control_ids'])
        self.assertEqual(result['history_edges'], [])

    def test_nonlegacy_ids_equal_colon_multiple_links_and_supersession_are_retained(self):
        self.master.write_bytes(b'## CURRENT CONTROL G-CONTROL-X-2\nCURRENT_CONTROL_ID=G-CONTROL-X-2\nSUPERSEDES_CURRENT_CONTROL G-CONTROL-X-1\n## G-CONVERGENCE-X-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-ADAPT-X-1,P-KNOW-X-2; PROJECT_EVENT_LINK: P-EVT-X-3\n')
        self.project.write_bytes(b'## P-ADAPT-X-1\nfirst\n## P-KNOW-X-2\nsecond\n## P-EVT-X-3\nthird\n')
        result = m.inventory(self.master)
        self.assertIn('G-CONVERGENCE-X-1', result['heading_ids'])
        self.assertIn('G-CONTROL-X-1', result['later_superseded_control_ids'])
        self.assertEqual(result['sections'][-1]['project_event_links'], ['P-ADAPT-X-1', 'P-EVT-X-3', 'P-KNOW-X-2'])
        self.assertEqual(m.cross_layer(result, m.inventory(self.project))['invalid_project_event_links'], [])

    def test_supersession_supports_each_explicit_separator(self):
        for separator in (': ', '=', ' '):
            with self.subTest(separator=separator):
                self.master.write_text('## CURRENT CONTROL\nCURRENT_CONTROL_ID: G-CONTROL-X-1\nSUPERSEDES_CURRENT_CONTROL' + separator + 'G-CONTROL-X-1\n')
                result = m.inventory(self.master)
                self.assertEqual(result['stale_current_control_candidates'], ['G-CONTROL-X-1'])

    def test_archive_ids_are_inventoried_without_reviving_historical_controls(self):
        history = self.root / 'history.md'
        history.write_bytes(b'## CURRENT CONTROL G-CONTROL-OLD-1\nCURRENT_CONTROL_ID: G-CONTROL-OLD-1\n## G-ECAL-X-1\nold\n')
        self.master.write_bytes(b'## CURRENT CONTROL G-CONTROL-NEW-1\nCURRENT_CONTROL_ID: G-CONTROL-NEW-1\n## Ref\n' + self.marker(history))
        result = m.inventory(self.master)
        self.assertIn('G-ECAL-X-1', result['heading_ids'])
        self.assertIn('G-CONTROL-OLD-1', result['all_ids'])
        self.assertNotIn('G-CONTROL-OLD-1', result['current_control_ids'])
        self.assertTrue(all(heading['source_role'] == 'live' for heading in result['headings']
            if heading['role'] == 'current-control'))
        self.assertTrue(any(heading['role'] == 'archived-history' for heading in result['headings']))

    def test_nested_and_repeated_archive_inventory_uses_shared_complete_loader(self):
        leaf = self.root / 'leaf.md'
        leaf.write_bytes(b'## P-ADAPT-DEEP-1\ndeep\n')
        middle = self.root / 'middle.md'
        middle.write_bytes(b'## Mid\n' + self.marker(leaf))
        self.master.write_bytes(b'## CURRENT CONTROL\ncurrent\n## Ref\n' + self.marker(middle) + self.marker(leaf))
        result = m.inventory(self.master)
        self.assertEqual(len(result['sources']), 3)
        self.assertEqual(len(result['history_edges']), 3)
        self.assertEqual(result['heading_ids'].count('P-ADAPT-DEEP-1'), 1)
        self.assertEqual(result['source_loader_ref'], self.loader_ref)

    def test_stale_missing_and_mismatched_loader_fail_without_partial_inventory_claim(self):
        history = self.root / 'history.md'
        history.write_bytes(b'## P-OLD-X-1\nold\n')
        self.master.write_bytes(self.marker(history))
        history.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            m.inventory(self.master)
        history.unlink()
        with self.assertRaises(ValueError):
            m.inventory(self.master)
        with self.assertRaisesRegex(ValueError, 'loader hash'):
            m.inventory(self.master, source_loader_sha256='0' * 64)

    def test_real_replacement_retrieval_inventory_compare_preserves_prior_ids(self):
        raw = b'Preamble\n## CURRENT CONTROL\nCURRENT_CONTROL_ID: G-CONTROL-X-1\n## G-CONVERGENCE-X-1\nretire display only\n## Keep\nkeep\n'
        self.master.write_bytes(raw)
        before = m.inventory(self.master)
        chosen = [section['section_id'] for section in self.loader.scan(raw)[0]
            if section['title'] == 'G-CONVERGENCE-X-1']
        history = self.root / 'history.md'
        proposal = self.loader.propose_organization(self.master, history, chosen, self.loader.sha(raw))
        history.write_bytes(base64.b64decode(proposal['backing_base64']))
        self.master.write_bytes(base64.b64decode(proposal['replacement_base64']))
        after = m.inventory(self.master)
        comparison = m.compare(before, after)
        self.assertTrue(comparison['no_drop'])
        self.assertEqual(comparison['missing_heading_ids'], [])
        retired = [heading for heading in after['headings'] if 'G-CONVERGENCE-X-1' in heading['ids']]
        self.assertTrue(retired)
        self.assertTrue(all(heading['source_role'] == 'history' for heading in retired))
        self.assertEqual(after['current_control_ids'], before['current_control_ids'])

    def test_shorthand_link_expression_remains_visible_not_expanded_into_invented_ids(self):
        self.master.write_bytes(b'## G-CONVERGENCE-X-1\nPROJECT_EVENT_LINK=P-CONVERGENCE-004..007 in saved project\n')
        result = m.inventory(self.master)
        section = result['sections'][0]
        self.assertEqual(section['project_event_links'], ['P-CONVERGENCE-004'])
        self.assertIn('..007', section['project_event_link_expressions'][0])

    def test_archived_raw_reviewer_counterexample_is_diagnostic_not_current_blocker(self):
        history = self.root / 'old-global.md'
        history.write_bytes(b'## G-OLD-1\nRAW_EPISODE old misplaced episode; now retired from live display\n')
        self.project.write_bytes(b'## P-TEST-1\nRAW_EPISODE prior project detail\n')
        self.master.write_bytes(b'## CURRENT CONTROL\ncurrent\n## G-LIVE-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-TEST-1\n## History pointers\n' + self.marker(history))
        global_inventory, project_inventory = m.inventory(self.master), m.inventory(self.project)
        result = m.cross_layer(global_inventory, project_inventory)
        self.assertEqual(result['decision'], 'ELIGIBLE_WITH_SEMANTIC_AUDIT')
        self.assertEqual(result['global_raw_episode_lines'], [])
        historical = result['historical_diagnostics']
        self.assertFalse(historical['affects_current_write_decision'])
        self.assertEqual(historical['global_raw_episode_sections'][0]['source'], str(history))
        self.assertEqual(historical['global_raw_episode_sections'][0]['source_sha256'], m.sha256_bytes(history.read_bytes()))
        self.assertIn('G-OLD-1', global_inventory['heading_ids'])

    def test_all_historical_tag_link_diagnostics_retain_source_identity_without_blocking(self):
        old_global, old_project = self.root / 'old-global.md', self.root / 'old-project.md'
        old_global.write_bytes(b'## G-RAW-1\nRAW_EPISODE old\n## G-NOLINK-1\nSANITIZED_FAMILY old\n## G-BADLINK-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-MISSING-1\n')
        old_project.write_bytes(b'## P-OLD-1\nSANITIZED_FAMILY formerly misplaced\n')
        self.master.write_bytes(b'## G-LIVE-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-LIVE-1\n## References\n' + self.marker(old_global))
        self.project.write_bytes(b'## P-LIVE-1\nRAW_EPISODE current source\n## References\n' + self.marker(old_project))
        result = m.cross_layer(m.inventory(self.master), m.inventory(self.project))
        self.assertEqual(result['decision'], 'ELIGIBLE_WITH_SEMANTIC_AUDIT')
        historical = result['historical_diagnostics']
        expected = {
            'global_raw_episode_sections': old_global,
            'global_sanitized_without_project_link_sections': old_global,
            'invalid_project_event_links': old_global,
            'project_contains_sanitized_family_sections': old_project,
        }
        for key, source in expected.items():
            with self.subTest(key=key):
                self.assertTrue(historical[key])
                self.assertTrue(all(item['source'] == str(source) and item['source_role'] == 'history'
                    and item['source_sha256'] == m.sha256_bytes(source.read_bytes()) for item in historical[key]))
        self.assertEqual(historical['invalid_project_event_links'][0]['link'], 'P-MISSING-1')
        self.assertEqual(result['invalid_project_event_links'], [])
        self.assertEqual(result['project_contains_sanitized_family_lines'], [])

    def test_live_history_and_history_history_duplicates_remain_nonblocking_diagnostics(self):
        body = b'SANITIZED_FAMILY PROJECT_EVENT_LINK=P-LIVE-1\n'
        old_global, old_project = self.root / 'old-global.md', self.root / 'old-project.md'
        old_global.write_bytes(b'## G-OLD-1\n' + body)
        old_project.write_bytes(b'## P-OLD-1\n' + body)
        self.master.write_bytes(b'## G-LIVE-1\n' + body + b'## References\n' + self.marker(old_global))
        self.project.write_bytes(b'## P-LIVE-1\nRAW_EPISODE original project\n## References\n' + self.marker(old_project))
        result = m.cross_layer(m.inventory(self.master), m.inventory(self.project))
        self.assertEqual(result['decision'], 'ELIGIBLE_WITH_SEMANTIC_AUDIT')
        self.assertEqual(result['tagged_duplicate_blockers'], [])
        groups = result['historical_diagnostics']['normalized_cross_layer_duplicate_groups']
        tagged = [group for group in groups if group['tagged']]
        self.assertTrue(tagged)
        pairs = {(group['global_sections'][0]['source_role'], group['project_sections'][0]['source_role']) for group in tagged}
        self.assertIn(('live', 'history'), pairs)
        self.assertIn(('history', 'history'), pairs)
        self.assertTrue(all(section['source'] and section['source_sha256'] for group in tagged
            for section in group['global_sections'] + group['project_sections']))

    def test_current_bad_layout_still_blocks_across_each_existing_structural_consumer(self):
        cases = [
            (b'## G-BAD-1\nRAW_EPISODE current misplaced\n', b'## P-LIVE-1\nRAW_EPISODE valid project\n', 'global_raw_episode_lines'),
            (b'## G-BAD-1\nSANITIZED_FAMILY no link\n', b'## P-LIVE-1\nRAW_EPISODE valid project\n', 'global_sanitized_without_project_link_lines'),
            (b'## G-BAD-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-MISSING-1\n', b'## P-LIVE-1\nRAW_EPISODE valid project\n', 'invalid_project_event_links'),
            (b'## G-LIVE-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-BAD-1\n', b'## P-BAD-1\nSANITIZED_FAMILY current misplaced\n', 'project_contains_sanitized_family_lines'),
        ]
        for global_raw, project_raw, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                self.master.write_bytes(global_raw)
                self.project.write_bytes(project_raw)
                result = m.cross_layer(m.inventory(self.master), m.inventory(self.project))
                self.assertEqual(result['decision'], 'BLOCK_NEW_WRITE')
                self.assertTrue(result[diagnostic])

    def test_live_duplicate_occurrences_cannot_be_overwritten_by_later_history(self):
        body = b'SANITIZED_FAMILY PROJECT_EVENT_LINK=P-LIVE-1\n'
        old_global, old_project = self.root / 'old-global.md', self.root / 'old-project.md'
        old_global.write_bytes(b'## G-OLD-1\n' + body)
        old_project.write_bytes(b'## P-OLD-1\n' + body)
        self.master.write_bytes(b'## G-LIVE-1\n' + body + b'## G-LIVE-2\n' + body + b'## Ref\n' + self.marker(old_global))
        self.project.write_bytes(b'## P-LIVE-1\n' + body + b'## Ref\n' + self.marker(old_project))
        result = m.cross_layer(m.inventory(self.master), m.inventory(self.project))
        self.assertEqual(result['decision'], 'BLOCK_NEW_WRITE')
        self.assertTrue(result['tagged_duplicate_blockers'])
        groups = [group for group in result['current_diagnostics']['normalized_cross_layer_duplicate_groups'] if group['tagged']]
        self.assertEqual(len(groups[0]['global_sections']), 2)
        self.assertTrue(all(item['source_role'] == 'live' for group in groups
            for item in group['global_sections'] + group['project_sections']))
        self.assertTrue(result['historical_diagnostics']['normalized_cross_layer_duplicate_groups'])

    def test_current_global_link_can_resolve_to_preserved_archived_project_id(self):
        history = self.root / 'old-project.md'
        history.write_bytes(b'## P-ARCHIVED-1\nRAW_EPISODE exact historical source\n')
        self.project.write_bytes(b'## CURRENT CONTROL\ncurrent\n## Ref\n' + self.marker(history))
        self.master.write_bytes(b'## G-LIVE-1\nSANITIZED_FAMILY PROJECT_EVENT_LINK=P-ARCHIVED-1\n')
        project_inventory = m.inventory(self.project)
        result = m.cross_layer(m.inventory(self.master), project_inventory)
        self.assertIn('P-ARCHIVED-1', project_inventory['heading_ids'])
        self.assertEqual(result['invalid_project_event_links'], [])
        self.assertEqual(result['decision'], 'ELIGIBLE_WITH_SEMANTIC_AUDIT')

    def test_legacy_live_inventory_remains_checked_and_unknown_explicit_role_is_rejected(self):
        self.master.write_bytes(b'## G-BAD-1\nRAW_EPISODE live malformed\n')
        self.project.write_bytes(b'## P-LIVE-1\nRAW_EPISODE valid\n')
        global_inventory, project_inventory = m.inventory(self.master), m.inventory(self.project)
        for inventory in (global_inventory, project_inventory):
            for section in inventory['sections']:
                for key in ('source', 'source_sha256', 'source_role'):
                    section.pop(key)
        result = m.cross_layer(global_inventory, project_inventory)
        self.assertEqual(result['decision'], 'BLOCK_NEW_WRITE')
        diagnostic = result['current_diagnostics']['global_raw_episode_sections'][0]
        self.assertEqual(diagnostic['source'], str(self.master))
        self.assertEqual(diagnostic['source_sha256'], global_inventory['sha256'])
        global_inventory['sections'][0]['source_role'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'unknown section source role'):
            m.cross_layer(global_inventory, project_inventory)


if __name__ == '__main__':
    unittest.main()
