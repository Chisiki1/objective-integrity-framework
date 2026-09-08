"""Behavioral normal/counterexamples, using only temporary local fixtures."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import capability_snapshot as cap


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hooks = self.root / 'hooks.json'
        self.journal = self.root / 'journal.jsonl'
        self.digest = 'A' * 64
        self.hooks.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': 'PRIVATE_COMMAND; never execute'}]}]}}))

    def capture(self):
        return {'seq': 7, 'event_type': 'user_prompt_source', 'payload': {
            'source_sha256': self.digest, 'prompt': 'PRIVATE_PROMPT', 'capture_route': 'not independently attested'}}

    def test_normal_file_and_privacy_no_writes(self):
        self.journal.write_text(json.dumps(self.capture())+'\n')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = cap.snapshot(hooks=self.hooks, journal=self.journal, source_sha256=self.digest)
        self.assertEqual(result['capture']['matching_source_records'], 1)
        self.assertEqual(result['capture']['last_matching_seq'], 7)
        self.assertEqual(result['hooks']['configured_events'][0]['event'], 'SessionStart')
        self.assertFalse(result['permission_granted'])
        self.assertEqual(result['host_delivery'], 'NOT_EVALUATED')
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_no_matching_capture_not_absence_of_host_delivery(self):
        self.journal.write_text(json.dumps(self.capture())+'\n')
        result = cap.snapshot(hooks=self.hooks, journal=self.journal, source_sha256='B'*64)
        self.assertEqual(result['capture']['matching_source_records'], 0)
        self.assertEqual(result['host_delivery'], 'NOT_EVALUATED')

    def test_partial_journal_keeps_observed_metadata(self):
        self.journal.write_bytes((json.dumps(self.capture())+'\n{"truncated":').encode())
        result = cap.capture_projection(self.journal, self.digest)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['observed_capture_records'], 1)
        self.assertEqual(result['malformed_records'], 1)

    def test_duplicate_json_is_invalid_not_successful_empty(self):
        self.hooks.write_text('{"hooks":{},"hooks":{}}')
        result = cap.hooks_projection(self.hooks)
        self.assertEqual(result['status'], 'INVALID')
        self.assertIsNone(result['configured_events'])

    def test_absent_input_remains_unavailable(self):
        result = cap.capture_projection(self.journal)
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIsNone(result['observed_capture_records'])

    def test_oversized_partition_is_not_empty_success(self):
        result, raw = cap.read_snapshot(self.hooks, limit=2)
        self.assertEqual(result['status'], 'LIMITED')
        self.assertIsNone(raw)
        self.assertIsNone(result['sha256'])

    def test_changed_read_remains_unresolved(self):
        with patch.object(cap, '_identity', side_effect=[('before',), ('after',)]):
            result, raw = cap.read_snapshot(self.hooks)
        self.assertEqual(result['status'], 'CHANGED_DURING_READ')
        self.assertIsNone(raw)

    def test_unknown_event_and_unsupported_handler_are_not_executed_or_echoed(self):
        self.hooks.write_text(json.dumps({'hooks': {'PRIVATE_EVENT': [{'hooks': [{'type': 'agent'}]}]}}))
        result = cap.hooks_projection(self.hooks)
        self.assertNotIn('PRIVATE_EVENT', json.dumps(result))
        self.assertEqual(result['configured_events'][0]['event'], 'UNKNOWN_EVENT')
        self.assertEqual(result['configured_events'][0]['declared_handlers']['other'], 1)

    def test_malformed_members_are_partial(self):
        self.hooks.write_text('{"hooks":{"Stop":{},"SessionStart":[{"hooks":[null]}]}}')
        result = cap.hooks_projection(self.hooks)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['malformed_members'], 2)

    def test_digest_requires_journal(self):
        with self.assertRaises(ValueError): cap.snapshot(hooks=self.hooks, source_sha256=self.digest)

    def test_relative_path_is_not_reinterpreted(self):
        result, raw = cap.read_snapshot('hooks.json')
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIsNone(raw)

    def test_nonfinite_record_is_retained_as_invalid_partition(self):
        self.journal.write_text('{"event_type":"user_prompt_source","payload":NaN}')
        result = cap.capture_projection(self.journal)
        self.assertEqual(result['status'], 'PARTIAL')

    def test_directory_is_not_read(self):
        result, raw = cap.read_snapshot(self.root)
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIsNone(raw)

    def test_future_envelope_is_not_empty_capture_success(self):
        self.journal.write_text('{"schema_version":"future","events":[]}')
        result = cap.capture_projection(self.journal)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['malformed_records'], 1)

    def test_mixed_unknown_envelope_preserves_valid_capture(self):
        self.journal.write_text(json.dumps(self.capture())+'\n{}\n')
        result = cap.capture_projection(self.journal, self.digest)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['matching_source_records'], 1)

    def test_well_shaped_unrelated_events_are_skippable(self):
        self.journal.write_text('{"event_type":"progress","payload":{}}\n')
        result = cap.capture_projection(self.journal)
        self.assertEqual(result['status'], 'OBSERVED')
        self.assertEqual(result['observed_capture_records'], 0)

    def test_nonstring_empty_or_whitespace_event_type_is_partial(self):
        for value in (None, 1, [], '', '  '):
            self.journal.write_text(json.dumps({'event_type': value}))
            self.assertEqual(cap.capture_projection(self.journal)['status'], 'PARTIAL')


if __name__ == '__main__': unittest.main()
