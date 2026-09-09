"""Reproduce the public byte metric and check its non-improvement controls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QueryBenchmark(unittest.TestCase):
    def test_real_cli_comparison_preserves_content_and_negative_controls(self):
        with tempfile.TemporaryDirectory(prefix="oif-metric-") as temporary:
            parent = Path(temporary).resolve()
            output = parent / "results.json"
            argv = [sys.executable, "-B", str(ROOT / "tools/benchmark_query.py"),
                    "--script", str(ROOT / "runtime/skills/master-guided-skill-lifecycle/scripts/master_index.py"),
                    "--scratch", str(parent / "scratch"), "--output", str(output)]
            result = subprocess.run(argv, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            data = json.loads(output.read_bytes())
            self.assertEqual(data["status"], "verified")
            self.assertTrue(data["runtime"]["master_index_unchanged"])
            rows = data["cases"]
            self.assertEqual(rows["duplicate-rich"]["source_occurrences"], 10)
            self.assertGreater(rows["duplicate-rich"]["full_retrieval_reduction_percent"], 0)
            self.assertLess(rows["no-duplicates"]["full_retrieval_reduction_percent"], 0)
            self.assertEqual(rows["near-contradictions"]["unique_exact_sections"], 3)
            self.assertEqual(rows["no-match"]["source_occurrences"], 0)
            for row in rows["stale-source"]["checks"].values():
                self.assertEqual(row["exit_code"], 2)
            for name in ("duplicate-rich", "no-duplicates", "near-contradictions", "no-match"):
                self.assertTrue(rows[name]["exact_content_preserved"])
                self.assertTrue(rows[name]["all_matching_origins_and_ancestor_edges_preserved"])
            self.assertNotIn(str(parent), output.read_text())
            original = output.read_bytes()
            repeated = subprocess.run(argv, capture_output=True, timeout=30)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_recorded_percentages_follow_the_denominators(self):
        data = json.loads((ROOT / "docs/benchmarks/query-response-0.1.1.json").read_bytes())
        for row in data["cases"].values():
            if "full_retrieval_reduction_percent" in row:
                old = row["legacy_content_and_provenance_bytes"]
                new = row["compact_content_and_provenance_bytes"]
                self.assertEqual(round(100 * (old - new) / old, 2), row["full_retrieval_reduction_percent"])


if __name__ == "__main__":
    unittest.main()
