import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('master_index', Path(__file__).with_name('master_index.py'))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


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


if __name__ == '__main__':
    unittest.main()
