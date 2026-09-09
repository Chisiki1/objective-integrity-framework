"""Behavioral tests for exact grouped retrieval; run after whole-candidate freeze.

Uses owned TemporaryDirectory fixtures. Real CLI tests deliberately invoke
default Python without -B/PYTHONDONTWRITEBYTECODE; no live master is a fixture.
"""
import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'master_index.py'
if not SCRIPT.exists():
    SCRIPT = HERE.parent / 'scripts' / 'master_index.py'
MI = runpy.run_path(str(SCRIPT))
GLOBALS = MI['query_compact'].__globals__


class CompactQueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='master-query-')
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def source(self, name, text):
        path = self.root / name
        path.write_bytes(text.encode('utf8') if isinstance(text, str) else text)
        return path

    def marker(self, path):
        raw = path.read_bytes()
        ref = {'path': str(path), 'sha256': MI['sha'](raw), 'bytes': len(raw)}
        return '<!-- master-history-v1 ' + MI['canonical'](ref).decode() + ' -->\n'

    def build(self, sources):
        return Path(MI['build'](sources, self.root / 'cache')['index'])

    def indexed(self, index, change, name='modified'):
        data = MI['strict_json'](index.read_bytes())
        change(data)
        raw = MI['canonical'](data)
        target = self.root / name
        target.mkdir()
        path = target / ('index-' + MI['sha'](raw) + '.json')
        path.write_bytes(raw)
        return path

    def shared_history(self):
        body = '## Claim\nshared needle: preserve every origin.\n'
        history = self.source('history.md', '# History\n' + body + '## End\nold\n')
        global_ = self.source('global.md', '# Global\n' + body + '## Archive\n' + self.marker(history))
        project = self.source('project.md', '# Project\n' + body + '## Archive\n' + self.marker(history))
        return self.build([global_, project]), body, history, global_, project

    def pages(self, index, terms, budget=1800, controls=False):
        cursor, seen, groups = None, set(), {}
        for _ in range(1000):  # Fixture runaway discriminator, not a production retry rule.
            page = MI['query_compact'](index, terms, cursor, budget, controls)
            self.assertLessEqual(len(MI['serialize_page'](page)), budget)
            self.assertFalse(page['origins_complete'])
            for item in page['items']:
                content = groups.setdefault(item['group_id'], '')
                self.assertEqual(item['character_offset'], len(content))
                self.assertTrue(item['text'])
                groups[item['group_id']] += item['text']
            cursor = page['next_cursor']
            if cursor is None:
                self.assertTrue(page['complete_for_explicit_query'])
                return groups, page
            self.assertFalse(page['complete_for_explicit_query'])
            self.assertNotIn(cursor, seen)
            seen.add(cursor)
        self.fail('fixture did not reach a finite content frontier')

    def origins(self, index, terms, group_id, budget=1300, controls=False):
        cursor, text, seen = None, '', set()
        for _ in range(1000):
            page = MI['query_origins'](index, terms, group_id, cursor, budget, controls)
            self.assertLessEqual(len(MI['serialize_page'](page)), budget)
            self.assertEqual(page['character_offset'], len(text))
            self.assertTrue(page['origin_json'])
            text += page['origin_json']
            cursor = page['next_cursor']
            if cursor is None:
                self.assertTrue(page['origins_complete'])
                self.assertEqual(len(text), page['total_characters'])
                return json.loads(text)
            self.assertFalse(page['origins_complete'])
            self.assertNotIn(cursor, seen)
            seen.add(cursor)
        self.fail('fixture did not reach a finite provenance frontier')

    def cli(self, script, *arguments):
        env = dict(os.environ)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.pop('PYTHONPYCACHEPREFIX', None)
        return subprocess.run([sys.executable, str(script), *map(str, arguments)],
                              capture_output=True, env=env, timeout=30)

    def test_exact_duplicate_text_has_all_sources_and_graph_origins(self):
        index, body, history, global_, project = self.shared_history()
        groups, page = self.pages(index, ['shared needle'])
        self.assertEqual(list(groups.values()), [body])
        item = page['items'][0]
        self.assertEqual(item['origin_count'], 3)
        self.assertEqual(item['matching_origin_count'], 3)
        self.assertNotIn('parent_refs', item)
        document = self.origins(index, ['shared needle'], next(iter(groups)))
        self.assertEqual(len(document['origins']), 3)
        self.assertEqual({s['path'] for s in document['sources']},
                         {str(history), str(global_), str(project)})
        self.assertEqual({s['role'] for s in document['sources']}, {'live', 'history'})
        self.assertEqual(len(document['history_edges']), 2)
        source_map = {s['source']: s for s in document['sources']}
        for origin in document['origins']:
            raw = Path(source_map[origin['source']]['path']).read_bytes()
            self.assertEqual(raw[origin['byte_start']:origin['byte_end']], body.encode())

    def test_near_and_contradictory_claims_stay_distinct(self):
        first = self.source('one.md', '## Rule\npolicy decision: allow\n')
        second = self.source('two.md', '## Rule\npolicy decision: deny\n')
        third = self.source('three.md', b'## Rule\r\npolicy decision: allow\r\n')
        groups, _ = self.pages(self.build([first, second, third]), ['policy decision'])
        self.assertEqual(len(groups), 3)
        self.assertEqual(set(t.encode() for t in groups.values()),
                         {p.read_bytes() for p in (first, second, third)})

    def test_unicode_long_title_and_escaping_are_lossless_and_bounded(self):
        text = '## ' + '\u9577\U0001f600' * 500 + '\nneedle ' + '\u65e5\u672c\U0001f600"\\\t' * 350 + '\r\n'
        path = self.source('unicode.md', text)
        groups, _ = self.pages(self.build([path]), ['needle'], budget=1600)
        self.assertEqual(list(groups.values()), [text])

    def test_controls_only_select_live_text_but_keep_identical_old_origins(self):
        current = '## CURRENT CONTROL\ncurrent needle\n'
        history = self.source('history.md', '# H\n' + current + '## Old\nobsolete\n')
        root = self.source('root.md', '# R\n' + current + '# Archive\n' + self.marker(history))
        index = self.build([root])
        groups, page = self.pages(index, [], controls=True)
        self.assertEqual(list(groups.values()), [current])
        self.assertEqual(page['items'][0]['matching_origin_count'], 1)
        document = self.origins(index, [], next(iter(groups)), controls=True)
        self.assertEqual(sorted(o['matched_by_query'] for o in document['origins']), [False, True])

    def test_archived_obsolete_control_is_not_a_live_control(self):
        history = self.source('history.md', '## CURRENT CONTROL\nobsolete-only\n')
        root = self.source('root.md', '## CURRENT CONTROL\nlive-only\n# Archive\n' + self.marker(history))
        groups, _ = self.pages(self.build([root]), [], controls=True)
        self.assertEqual(list(groups.values()), ['## CURRENT CONTROL\nlive-only\n'])

    def test_cursor_is_bound_to_mode_query_group_and_index(self):
        first = self.source('first.md', '## First\nneedle ' + 'x' * 8000 + '\n')
        second = self.source('second.md', '## Second\nneedle other\n')
        index = self.build([first, second])
        page = MI['query_compact'](index, ['needle'], page_chars=1400)
        cursor = page['next_cursor']
        self.assertIsNotNone(cursor)
        with self.assertRaises(ValueError):
            MI['query_compact'](index, ['other'], cursor, 1400)
        same = MI['query_compact'](index, ['NEEDLE'], cursor, 1800)
        self.assertGreater(same['items'][0]['character_offset'], 0)
        group = page['items'][0]['group_id']
        origin = MI['query_origins'](index, ['needle'], group, page_chars=1100)
        with self.assertRaises(ValueError):
            MI['query_origins'](index, ['needle'], group, cursor, 1400)
        if origin['next_cursor'] is not None:
            with self.assertRaises(ValueError):
                MI['query_compact'](index, ['needle'], origin['next_cursor'], 1400)
            with self.assertRaises(ValueError):
                MI['query_origins'](index, ['needle'], MI['sha'](second.read_bytes()),
                                    origin['next_cursor'], 1400)
        changed = self.indexed(index, lambda d: d.update(proof_ceiling='different exact index'))
        with self.assertRaises(ValueError):
            MI['query_compact'](changed, ['needle'], cursor, 1400)

    def test_malformed_and_past_frontier_cursors_fail(self):
        path = self.source('one.md', '## A\nneedle\n')
        index = self.build([path])
        page = MI['query_compact'](index, ['needle'])
        binding = page['query_binding']
        for cursor in ('', 'C1:' + binding + ':00:0', 'C1:' + binding + ':0:-1',
                       'C1:' + binding + ':999:0', 'C1:' + binding + ':0:999', 5):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                MI['query_compact'](index, ['needle'], cursor)

    def test_too_small_budget_fails_instead_of_zero_progress(self):
        path = self.source('one.md', '## A\nneedle\n')
        index = self.build([path])
        with self.assertRaisesRegex(ValueError, 'page_chars too small'):
            MI['query_compact'](index, ['needle'], page_chars=256)
        result = self.cli(SCRIPT, 'query', '--index', index, '--term', 'needle', '--page-chars', 256)
        self.assertEqual(result.returncode, 2)
        self.assertLessEqual(len(result.stdout), 256)
        self.assertEqual(json.loads(result.stdout)['status'], 'ERROR')

    def test_no_match_reports_empty_complete_content_not_semantic_completion(self):
        index = self.build([self.source('one.md', '## A\nnormal\n')])
        result = MI['query_compact'](index, ['missing'])
        self.assertEqual(result['items'], [])
        self.assertTrue(result['complete_for_explicit_query'])
        self.assertFalse(result['all_knowledge_semantics_accounted'])
        with self.assertRaises(ValueError):
            MI['query_origins'](index, ['missing'], 'A' * 64)

    def test_equal_size_and_mtime_source_change_is_still_stale(self):
        path = self.source('one.md', '## A\nneedle allow\n')
        index = self.build([path])
        before = path.stat()
        path.write_bytes(path.read_bytes().replace(b'allow', b'denyy'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'STALE_SOURCE'):
            MI['query_compact'](index, ['needle'])

    def test_missing_history_and_changed_cache_are_errors(self):
        index, _, history, _, _ = self.shared_history()
        old = history.read_bytes()
        history.unlink()
        with self.assertRaises(ValueError):
            MI['query_compact'](index, ['shared'])
        history.write_bytes(old)
        data = MI['strict_json'](index.read_bytes())
        Path(data['masters'][0]['backing']).write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'backing identity mismatch'):
            MI['query_compact'](index, ['shared'])

    def test_role_and_coverage_tampering_are_rejected(self):
        index, _, _, _, _ = self.shared_history()
        def role(data):
            next(m for m in data['masters'] if m['role'] == 'history')['role'] = 'live'
        bad_role = self.indexed(index, role, 'role')
        with self.assertRaisesRegex(ValueError, 'role or lineage'):
            MI['query_compact'](bad_role, ['shared'])
        bad_coverage = self.indexed(index, lambda d: d['masters'][0]['sections'].pop(), 'coverage')
        with self.assertRaisesRegex(ValueError, 'coverage mismatch'):
            MI['query_compact'](bad_coverage, ['shared'])
        # Python considers False == 0; exact index coverage must not do so.
        bad_type = self.indexed(index, lambda d: d['masters'][0]['sections'][0].update(section_id=False), 'type')
        with self.assertRaisesRegex(ValueError, 'coverage mismatch'):
            MI['query_compact'](bad_type, ['shared'])

    def test_malformed_index_and_duplicate_json_fields_fail(self):
        index = self.build([self.source('one.md', '## A\nneedle\n')])
        bad = self.indexed(index, lambda d: d.update(masters=[None]))
        with self.assertRaises(ValueError):
            MI['query_compact'](bad, ['needle'])
        duplicate = self.source('duplicate.json', '{"schema":"one","schema":"two"}')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
            MI['query_compact'](duplicate, ['needle'])

    def test_legacy_api_and_explicit_cli_response_still_match(self):
        index, _, _, _, _ = self.shared_history()
        legacy = MI['query'](index, ['shared'])
        self.assertEqual(legacy['total_segments'], 3)
        self.assertIn('parent_refs', legacy['items'][0])
        result = self.cli(SCRIPT, 'query', '--index', index, '--term', 'shared', '--legacy-output')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), legacy)
        compact = self.cli(SCRIPT, 'query', '--index', index, '--term', 'shared')
        self.assertEqual(compact.returncode, 0, compact.stderr)
        self.assertEqual(json.loads(compact.stdout)['total_groups'], 1)
        self.assertTrue(compact.stdout.endswith(b'\n'))
        self.assertFalse(compact.stdout.endswith(b'\r\n'))

    def test_legacy_v1_fence_coverage_remains_supported(self):
        path = self.source('one.md', '## Outside\n~~~\n## Inside\nneedle\n~~~\n')
        index = self.build([path])
        def legacy_scan(data):
            sections, occurrences = MI['scan'](path.read_bytes(), markdown_fences=False)
            data['masters'][0]['sections'] = sections
            data['masters'][0]['id_occurrences'] = occurrences
        old_index = self.indexed(index, legacy_scan)
        old = MI['query'](old_index, ['needle'])
        grouped, _ = self.pages(old_index, ['needle'])
        self.assertEqual(list(grouped.values()), [old['items'][0]['text']])

    def test_large_provenance_paths_are_data_not_unbounded_metadata(self):
        index, _, _, _, _ = self.shared_history()
        terms = ('shared',)
        digest, binding, graph, groups = MI['_compact_data'](index, terms, False)
        graph = copy.deepcopy(graph)
        names = {s['path']: s['path'] + '/\u9577\U0001f600' * 900 for s in graph['sources']}
        for source in graph['sources']:
            source['path'] = names[source['path']]
        for edge in graph['history_edges']:
            edge['path'] = names[edge['path']]
            edge['parent_path'] = names[edge['parent_path']]
        # Serialization boundary test, not a claim that the host supports a
        # physical filename of this length. Freshness is exercised separately.
        with mock.patch.dict(GLOBALS, {'_compact_data': lambda *a: (digest, binding, graph, groups)}):
            document = self.origins(index, ['shared'], groups[0]['group_id'], budget=1400)
        self.assertEqual({s['path'] for s in document['sources']}, set(names.values()))

    def test_default_python_cli_does_not_generate_package_bytecode(self):
        script = self.root / 'master_index.py'
        script.write_bytes(SCRIPT.read_bytes())
        index = self.build([self.source('one.md', '## A\nneedle\n')])
        result = self.cli(script, 'query', '--index', index, '--term', 'needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.root.rglob('*.pyc')), [])

    def test_oversized_failure_stays_bounded_and_explicitly_partial(self):
        error = ValueError('missing source/backing: ' + '\u9577\U0001f600/' * 5000)
        result = MI['_compact_error'](error, 512)
        self.assertLessEqual(len(MI['serialize_page'](result)), 512)
        self.assertFalse(result['error_complete'])
        self.assertEqual(result['error_sha256'], MI['sha'](str(error).encode()))


if __name__ == '__main__':
    unittest.main()
