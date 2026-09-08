import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import types

SCRIPT = Path(__file__).with_name('master_index.py')
m = types.ModuleType('master_index_under_test')
m.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_bytes(), str(SCRIPT), 'exec'), m.__dict__)


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.g = self.root / 'global.md'
        self.p = self.root / 'project.md'
        self.g.write_bytes('Original source\r\n## CURRENT CONTROL\r\ncurrent\r\n### G-EVT-X-1\r\nmatchshared\r\n## History\r\n### G-EVT-X-1\r\nmatchold\r\n'.encode())
        self.p.write_bytes(('Preamble\n## P-EVT-X-1\n' + 'matchlong text' * 160 + '\n').encode())

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        return m.build([self.g, self.p], self.root / 'cache')

    def test_exact_byte_partition_and_occurrences(self):
        result = self.build()
        idx = json.loads(Path(result['index']).read_text())
        for master in idx['masters']:
            raw = Path(master['backing']).read_bytes()
            self.assertEqual(b''.join(raw[s['start']:s['end']] for s in master['sections']), raw)
        self.assertEqual([o['id'] for o in idx['masters'][0]['id_occurrences']].count('G-EVT-X-1'), 2)

    def test_complete_pages_equal_original_matching_sections(self):
        result = self.build()
        cursor, actual = None, []
        while True:
            page = m.query(result['index'], ['match'], cursor, 256)
            actual.extend(page['items'])
            cursor = page['next_cursor']
            if not cursor:
                break
        idx = json.loads(Path(result['index']).read_text())
        expected = ''
        for master in idx['masters']:
            raw = Path(master['backing']).read_bytes()
            for sec in master['sections']:
                text = raw[sec['start']:sec['end']].decode()
                if 'match' in text:
                    expected += text
        self.assertEqual(''.join(i['text'] for i in actual), expected)
        self.assertFalse(page['all_knowledge_semantics_accounted'])

    def test_stale_source_does_not_reuse_cache(self):
        result = self.build()
        self.g.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'STALE_SOURCE'):
            m.query(result['index'], ['match'])

    def test_cursor_cannot_cross_query(self):
        result = self.build()
        page = m.query(result['index'], ['match'], page_chars=256)
        with self.assertRaisesRegex(ValueError, 'cursor/query'):
            m.query(result['index'], ['other'], page['next_cursor'], 256)

    def test_backing_tamper_rejected_and_source_unchanged(self):
        original = self.g.read_bytes()
        result = self.build()
        idx = json.loads(Path(result['index']).read_text())
        Path(idx['masters'][0]['backing']).write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'backing identity'):
            m.query(result['index'], ['match'])
        self.assertEqual(self.g.read_bytes(), original)

    def test_control_descendants_and_empty_query(self):
        result = self.build()
        page = m.query(result['index'], [], controls=True)
        text = ''.join(x['text'] for x in page['items'])
        self.assertIn('current', text)
        self.assertIn('shared', text)
        self.assertNotIn('old', text)
        empty = m.query(result['index'], ['absent'])
        self.assertEqual(empty['items'], [])
        self.assertTrue(empty['complete_for_explicit_query'])
        self.assertFalse(empty['all_knowledge_semantics_accounted'])

    def test_same_inputs_reuse_exact_index(self):
        first = self.build()
        self.assertEqual(first, self.build())

    def test_nested_control_roots_use_literal_source_oracle(self):
        selected = [
            '### WORKFLOW CURRENT CONTROL SUPPLEMENT — G-CONTROL-X-1\nactive3\n',
            '#### descendant\nactive4\n',
            '###### deeper\nactive6\n',
            '##### CURRENT CONTROL second\nactive5\n',
            '###### child\nactive-child\n',
            '## CURRENT CONTROL outer\nouter\n',
            '### CURRENT CONTROL inner\ninner\n',
            '### sibling within outer\nstill-active\n',
        ]
        excluded = ['# Master\npreamble\n## History\n',
                    '### neighboring history\nnot-active3\n',
                    '##### sibling history\nnot-active5\n',
                    '## End\nnot-active2\n']
        self.g.write_bytes((excluded[0] + ''.join(selected[:3]) + excluded[1]
                          + ''.join(selected[3:5]) + excluded[2]
                          + ''.join(selected[5:]) + excluded[3]).encode('utf8'))
        result = self.build()
        cursor, actual = None, []
        while True:
            page = m.query(result['index'], [], cursor, 256, controls=True)
            actual.extend(item['text'] for item in page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        # Expected text is predeclared independently of scan/index output.
        self.assertEqual(''.join(actual), ''.join(selected))


class ArchiveTest(unittest.TestCase):
    setUp = IndexTest.setUp
    tearDown = IndexTest.tearDown

    def marker(self, path, raw=None, **changes):
        raw = path.read_bytes() if raw is None else raw
        ref = {'path': str(path), 'sha256': m.sha(raw), 'bytes': len(raw)}
        ref.update(changes)
        return b'<!-- master-history-v1 ' + m.canonical(ref) + b' -->\n'

    def archive(self):
        history = self.root / 'history.md'
        history.write_bytes(b'Historical preamble\n## CURRENT CONTROL\nOLD-CONTROL\n## G-CONVERGENCE-X-1\nmatch cold\n')
        self.g.write_bytes(b'Live preamble\n## CURRENT CONTROL\nLIVE-CONTROL\n## References\n' + self.marker(history))
        return history

    def pages(self, index, terms, controls=False):
        cursor, chunks = None, []
        while True:
            page = m.query(index, terms, cursor, 256, controls)
            chunks.extend(page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                return chunks

    def test_live_and_cold_history_have_exact_roles_and_parent_lineage(self):
        history = self.archive()
        built = m.build([self.g], self.root / 'cache')
        index = json.loads(Path(built['index']).read_bytes())
        self.assertEqual(index['schema'], m.ARCHIVE_SCHEMA)
        self.assertEqual([node['role'] for node in index['masters']], ['live', 'history'])
        self.assertEqual(index['masters'][1]['parent_refs'][0]['parent_path'], str(self.g))
        self.assertEqual(index['masters'][1]['path'], str(history))
        chunks = self.pages(built['index'], ['match'])
        self.assertEqual(''.join(item['text'] for item in chunks), '## G-CONVERGENCE-X-1\nmatch cold\n')
        self.assertTrue(all(item['source_role'] == 'history' for item in chunks))

    def test_archived_controls_excluded_and_live_control_retained(self):
        self.archive()
        built = m.build([self.g], self.root / 'cache')
        chunks = self.pages(built['index'], [], controls=True)
        self.assertEqual(''.join(item['text'] for item in chunks), '## CURRENT CONTROL\nLIVE-CONTROL\n')
        self.assertTrue(all(item['source_role'] == 'live' for item in chunks))

    def test_nested_history_and_repeated_references_deduplicate_sources_not_edges(self):
        leaf = self.root / 'leaf.md'
        leaf.write_bytes(b'## P-EXAMPLE-X-1\nmatch deepest\n')
        mid = self.root / 'mid.md'
        mid.write_bytes(b'## Mid\n' + self.marker(leaf))
        self.g.write_bytes(b'## CURRENT CONTROL\ncurrent\n## Ref\n' + self.marker(mid) + self.marker(leaf) + self.marker(mid))
        graph = m.load_sources([self.g])
        self.assertEqual(len(graph['sources']), 3)
        self.assertEqual(len(graph['history_edges']), 4)
        self.assertEqual(len(graph['sources'][2]['parent_refs']), 2)
        built = m.build([self.g], self.root / 'cache')
        self.assertEqual(''.join(item['text'] for item in self.pages(built['index'], ['deepest'])),
            '## P-EXAMPLE-X-1\nmatch deepest\n')

    def test_all_matching_history_pages_preserve_every_character(self):
        history = self.archive()
        historical = ('## G-OLD-X-1\n' + 'matchUnicode sample\r\n' * 250).encode('utf8')
        history.write_bytes(historical)
        self.g.write_bytes(b'## CURRENT CONTROL\nmatch live\n## Ref\n' + self.marker(history))
        built = m.build([self.g], self.root / 'cache')
        chunks = self.pages(built['index'], ['match'])
        self.assertEqual(''.join(item['text'] for item in chunks),
            '## CURRENT CONTROL\nmatch live\n' + historical.decode('utf8'))
        self.assertGreater(len(chunks), 2)

    def test_matching_scan_cache_reused_but_hash_freshness_still_checked_each_page(self):
        history = self.archive()
        history.write_bytes(b'## G-OLD-X-1\n' + b'match ' * 200)
        self.g.write_bytes(b'## Ref\n' + self.marker(history))
        built = m.build([self.g], self.root / 'cache')
        m._matching_segments.cache_clear()
        first = m.query(built['index'], ['match'], page_chars=256)
        before = m._matching_segments.cache_info()
        m.query(built['index'], ['match'], first['next_cursor'], 256)
        self.assertGreater(m._matching_segments.cache_info().hits, before.hits)
        history.write_bytes(history.read_bytes() + b'changed')
        with self.assertRaises(ValueError):
            m.query(built['index'], ['match'], first['next_cursor'], 256)

    def test_missing_stale_and_wrong_size_history_hold_dependent_retrieval(self):
        history = self.archive()
        original = history.read_bytes()
        built = m.build([self.g], self.root / 'cache')
        history.unlink()
        with self.assertRaises(ValueError):
            m.query(built['index'], ['match'])
        history.write_bytes(original + b'tamper')
        with self.assertRaises(ValueError):
            m.query(built['index'], ['match'])
        history.write_bytes(original)
        self.g.write_bytes(self.marker(history, bytes=len(original) + 1))
        with self.assertRaisesRegex(ValueError, 'hash/size'):
            m.build([self.g], self.root / 'cache2')

    def test_malformed_duplicate_and_nonfinite_marker_json_rejected(self):
        history = self.archive()
        valid = self.marker(history)
        cases = [valid.replace(b' -->', b' -- >'),
            valid.replace(b'<!-- ', b'<!--'),
            valid.replace(b'"path":', b'"path":"duplicate","path":'),
            valid.replace(str(len(history.read_bytes())).encode(), b'1e999', 1),
            b'<!-- master-history-v1 {"path":"relative.md","sha256":"' + b'0' * 64 + b'","bytes":0} -->\n']
        for raw in cases:
            with self.subTest(raw=raw):
                self.g.write_bytes(raw)
                with self.assertRaises((ValueError, OSError)):
                    m.load_sources([self.g])

    def test_cycle_is_rejected_without_fabricating_hash_fixed_point(self):
        self.g.write_bytes(b'temporary fixture source')
        child = self.root / 'child.md'
        child.write_bytes(self.marker(self.g, sha256='0' * 64))
        self.g.write_bytes(self.marker(child))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            m.load_sources([self.g])

    def test_physical_alias_history_rejected(self):
        history = self.archive()
        alias = self.root / 'alias.md'
        os.link(history, alias)
        self.g.write_bytes(b'## Refs\n' + self.marker(history) + self.marker(alias))
        with self.assertRaisesRegex(ValueError, 'alias'):
            m.load_sources([self.g])

    def test_conflicting_repeated_reference_is_not_deduplicated_away(self):
        history = self.archive()
        self.g.write_bytes(self.marker(history) + self.marker(history, sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            m.load_sources([self.g])

    def test_real_symlink_rejected_if_available(self):
        history = self.archive()
        link = self.root / 'linked.md'
        try:
            link.symlink_to(history)
        except OSError as error:
            self.skipTest('host symlink creation unavailable: ' + str(error))
        self.g.write_bytes(self.marker(link))
        with self.assertRaisesRegex(ValueError, 'linked/reparse'):
            m.load_sources([self.g])

    def test_reparse_attribute_is_rejected_before_resolve(self):
        original = Path.lstat
        source = self.g
        def marked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == source:
                return types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with mock.patch.object(Path, 'lstat', marked), self.assertRaisesRegex(ValueError, 'reparse'):
            m.load_sources([self.g])

    def test_source_cache_and_history_cache_intersections_rejected_before_write(self):
        history = self.archive()
        with self.assertRaisesRegex(ValueError, 'contain'):
            m.build([self.g], self.root)
        cache = self.root / 'cache'
        cache.mkdir()
        cold = cache / 'cold.md'
        cold.write_bytes(history.read_bytes())
        self.g.write_bytes(self.marker(cold))
        with self.assertRaisesRegex(ValueError, 'contain'):
            m.build([self.g], cache)
        self.assertFalse((cache / 'backing').exists())

    def test_v1_ordinary_index_and_legacy_fence_grammar_remain_usable(self):
        self.g.write_bytes(b'## CURRENT CONTROL\ncurrent\n## History\n```text\n## G-OLD-X-1\nmatch code\n```\n')
        built = m.build([self.g], self.root / 'cache')
        index = json.loads(Path(built['index']).read_bytes())
        self.assertEqual(index['schema'], m.SCHEMA)
        sections, occurrences = m.scan(self.g.read_bytes(), markdown_fences=False)
        index['masters'][0].update(sections=sections, id_occurrences=occurrences)
        legacy = self.root / ('index-' + m.sha(m.canonical(index)) + '.json')
        legacy.write_bytes(m.canonical(index))
        chunks = self.pages(legacy, ['match'])
        self.assertIn('match code', ''.join(item['text'] for item in chunks))

    def test_v1_cannot_silently_omit_new_archive_reachability(self):
        self.archive()
        built = m.build([self.g], self.root / 'cache')
        index = json.loads(Path(built['index']).read_bytes())
        index['schema'] = m.SCHEMA
        index['masters'] = index['masters'][:1]
        index.pop('roots')
        index.pop('history_edges')
        incomplete = self.root / ('index-' + m.sha(m.canonical(index)) + '.json')
        incomplete.write_bytes(m.canonical(index))
        with self.assertRaisesRegex(ValueError, 'v1 index'):
            m.query(incomplete, ['match'])

    def test_v1_legacy_bom_preamble_spans_remain_readable(self):
        self.g.write_bytes(b'\xef\xbb\xbf## CURRENT CONTROL\nmatch BOM\n## G-OLD-X-1\nmatch old\n')
        built = m.build([self.g], self.root / 'cache')
        index = json.loads(Path(built['index']).read_bytes())
        sections, occurrences = m.scan(self.g.read_bytes(), markdown_fences=False)
        self.assertEqual(sections[0]['title'], 'PREAMBLE')
        index['masters'][0].update(sections=sections, id_occurrences=occurrences)
        legacy = self.root / ('index-' + m.sha(m.canonical(index)) + '.json')
        legacy.write_bytes(m.canonical(index))
        self.assertEqual(''.join(item['text'] for item in self.pages(legacy, ['match'])), self.g.read_bytes().decode('utf8'))

    def test_pure_organization_returns_exact_untouched_bytes_and_complete_old_backing(self):
        raw = b'Preamble\r\n## CURRENT CONTROL\r\nCURRENT\r\n## G-EVT-X-1\r\nremove only this\r\n## G-EVT-X-2\r\nkeep exact\r\n'
        self.g.write_bytes(raw)
        history = self.root / 'owned-history.md'
        sections, _ = m.scan(raw)
        chosen = [section['section_id'] for section in sections if section['title'] == 'G-EVT-X-1']
        before_files = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        result = m.propose_organization(self.g, history, chosen, m.sha(raw))
        replacement = base64.b64decode(result['replacement_base64'])
        backing = base64.b64decode(result['backing_base64'])
        self.assertEqual(backing, raw)
        self.assertEqual(self.g.read_bytes(), raw)
        self.assertFalse(history.exists())
        self.assertEqual(before_files, {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()})
        self.assertTrue(replacement.startswith(b'Preamble\r\n## CURRENT CONTROL\r\nCURRENT\r\n## G-EVT-X-2\r\nkeep exact\r\n'))
        self.assertNotIn(b'remove only this', replacement)
        self.assertIn('G-EVT-X-1', result['after_reachable_ids'])
        self.assertNotIn('G-EVT-X-1', result['after_current_ids'])
        self.assertFalse(result['permission_granted'])
        self.assertFalse(result['archive_reachability']['publication_observed'])
        # Owner-style fixture publication, not the pure proposal's side effect.
        history.write_bytes(backing)
        self.g.write_bytes(replacement)
        built = m.build([self.g], self.root / 'cache')
        chunks = self.pages(built['index'], ['remove only this'])
        self.assertEqual(''.join(item['text'] for item in chunks), '## G-EVT-X-1\r\nremove only this\r\n')

    def test_organization_refuses_stale_precondition_control_and_preamble(self):
        sections, _ = m.scan(self.g.read_bytes())
        for section in sections:
            if section['level'] == 0 or section['current_control_region']:
                with self.subTest(section=section['section_id']), self.assertRaises(ValueError):
                    m.propose_organization(self.g, self.root / 'cold.md', [section['section_id']], m.sha(self.g.read_bytes()))
        with self.assertRaisesRegex(ValueError, 'STALE_SOURCE'):
            m.propose_organization(self.g, self.root / 'cold.md', [len(sections) - 1], '0' * 64)

    def test_organization_refuses_pointer_section_and_partial_parent_subtree(self):
        self.archive()
        sections, _ = m.scan(self.g.read_bytes())
        pointer_id = next(section['section_id'] for section in sections if section['title'] == 'References')
        with self.assertRaisesRegex(ValueError, 'pointer'):
            m.propose_organization(self.g, self.root / 'next.md', [pointer_id], m.sha(self.g.read_bytes()))
        self.g.write_bytes(b'## History\ncontainer\n### G-EVT-X-1\nchild\n## Keep\nretained\n')
        with self.assertRaisesRegex(ValueError, 'subtree'):
            m.propose_organization(self.g, self.root / 'next.md', [0], m.sha(self.g.read_bytes()))
        result = m.propose_organization(self.g, self.root / 'next.md', [0, 1], m.sha(self.g.read_bytes()))
        self.assertTrue(base64.b64decode(result['replacement_base64']).startswith(b'## Keep\nretained\n'))

    def test_organization_refuses_output_alias_or_unknown_existing_backing(self):
        raw = self.g.read_bytes()
        sections, _ = m.scan(raw)
        selected = [sections[-1]['section_id']]
        with self.assertRaisesRegex(ValueError, 'contain'):
            m.propose_organization(self.g, self.g, selected, m.sha(raw))
        target = self.root / 'exists.md'
        target.write_bytes(b'unowned unrelated history')
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            m.propose_organization(self.g, target, selected, m.sha(raw))

    def test_fenced_code_headings_are_not_selectable_sections(self):
        raw = b'## G-EVT-X-1\n```text\n## CURRENT CONTROL\nfake inside code\n```\n## End\nkeep\n'
        self.g.write_bytes(raw)
        sections, _ = m.scan(raw)
        self.assertEqual([section['title'] for section in sections], ['G-EVT-X-1', 'End'])
        result = m.propose_organization(self.g, self.root / 'cold.md', [0], m.sha(raw))
        self.assertNotIn(b'fake inside code', base64.b64decode(result['replacement_base64']))


if __name__ == '__main__':
    unittest.main()
