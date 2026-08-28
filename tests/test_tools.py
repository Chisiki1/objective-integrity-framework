from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ToolTests(unittest.TestCase):
    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *args], cwd=ROOT, text=True, capture_output=True)

    def test_privacy_scan_passes_repository(self) -> None:
        result = self.run_tool("tools/privacy_scan.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_drop_check_passes(self) -> None:
        result = self.run_tool("tools/no_drop_check.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_scan_passes_repository(self) -> None:
        result = self.run_tool("tools/history_scan.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_scan_fails_without_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True, capture_output=True)
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "history_scan.py"), str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing HEAD", result.stdout)

    def test_link_check_passes_repository(self) -> None:
        result = self.run_tool("tools/link_check.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bootstrap_dry_run_and_apply_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            dry = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "generic")
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertFalse((target / "objective-integrity-adapter.md").exists())
            applied = self.run_tool("tools/bootstrap.py", "--destination", str(target), "--adapter", "generic", "--apply")
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertTrue((target / "objective-integrity-adapter.md").exists())
            backups = sorted((target / ".objective-integrity-backups").iterdir())
            rolled = self.run_tool("tools/bootstrap.py", "--rollback", str(backups[-1]))
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            self.assertFalse((target / "objective-integrity-adapter.md").exists())


if __name__ == "__main__":
    unittest.main()
