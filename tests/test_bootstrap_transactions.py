"""Public installation state/ownership/recovery relations, beyond file counts."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap_under_test", ROOT / "tools/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / "tools"))
try:
    spec.loader.exec_module(bootstrap)
finally:
    sys.path.pop(0)


class BootstrapTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "project"

    def install(self):
        with redirect_stdout(io.StringIO()):
            bootstrap.install(self.target, "generic", "minimal", True)
        return next((self.target / bootstrap.BACKUPS).iterdir())

    def test_dry_run_does_not_even_create_destination(self):
        with redirect_stdout(io.StringIO()):
            bootstrap.install(self.target, "generic", "minimal", False)
        self.assertFalse(self.target.exists())

    def preview(self):
        output = io.StringIO()
        with redirect_stdout(output):
            bootstrap.install(self.target, "generic", "minimal", False)
        return re.search(r"plan-sha256: ([0-9a-f]{64})", output.getvalue()).group(1)

    def test_expected_plan_applies_exact_preview(self):
        plan = self.preview()
        with redirect_stdout(io.StringIO()):
            bootstrap.install(self.target, "generic", "minimal", True, plan)
        backup = next((self.target / bootstrap.BACKUPS).iterdir())
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertTrue(manifest["prior_preview_matched"])
        self.assertEqual(manifest["plan_sha256"], plan)

    def test_changed_destination_invalidates_preview_without_extra_writes(self):
        plan = self.preview()
        self.target.mkdir()
        original = self.target / "objective-integrity-adapter.md"
        original.write_bytes(b"New user instructions")
        with self.assertRaisesRegex(ValueError, "Preview plan differs"):
            bootstrap.install(self.target, "generic", "minimal", True, plan)
        self.assertEqual(list(self.target.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"New user instructions")

    def test_changed_source_or_member_set_invalidates_preview(self):
        plan = self.preview()
        neutral = self.root / "new-source.md"
        neutral.write_text("Changed synthetic input", encoding="utf-8")
        original = bootstrap.planned_files("generic", "minimal")
        for mapping in (dict(original, **{"objective-integrity-adapter.md": neutral}),
                        dict(original, **{"extra.md": neutral})):
            with patch.object(bootstrap, "planned_files", return_value=mapping):
                with self.assertRaisesRegex(ValueError, "Preview plan differs"):
                    bootstrap.install(self.target, "generic", "minimal", True, plan)
            self.assertFalse(self.target.exists())

    def test_existing_content_survives_apply_and_idempotent_rollback(self):
        self.target.mkdir()
        original = self.target / "objective-integrity-adapter.md"
        original.write_bytes(b"My original instructions\r\n")
        backup = self.install()
        self.assertNotEqual(original.read_bytes(), b"My original instructions\r\n")
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(backup)
            bootstrap.rollback(backup)
        self.assertEqual(original.read_bytes(), b"My original instructions\r\n")
        self.assertFalse((self.target / "objective-integrity/objective-contract.md").exists())

    def test_later_edit_blocks_entire_rollback_before_any_restoration(self):
        backup = self.install()
        first = self.target / "objective-integrity-adapter.md"
        preserved = first.read_bytes()
        edited = self.target / "objective-integrity/objective-contract.md"
        edited.write_text("An adopter's later objective", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Rollback conflict"):
            bootstrap.rollback(backup)
        self.assertEqual(first.read_bytes(), preserved)
        self.assertEqual(edited.read_text(), "An adopter's later objective")

    def test_partial_install_has_recoverable_inventory_and_first_fault(self):
        real_write = bootstrap.atomic_write
        def interrupted(path, data):
            if path == self.target / "objective-integrity/objective-contract.md":
                raise OSError("synthetic destination interruption")
            real_write(path, data)
        with patch.object(bootstrap, "atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "synthetic destination interruption"):
                self.install()
        backup = next((self.target / bootstrap.BACKUPS).iterdir())
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "INTERRUPTED")
        self.assertIn("synthetic destination interruption", manifest["first_fault"])
        self.assertTrue((self.target / "objective-integrity-adapter.md").exists())
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(backup)
        self.assertFalse((self.target / "objective-integrity-adapter.md").exists())

    def test_backup_corruption_does_not_touch_installed_file(self):
        self.target.mkdir()
        target = self.target / "objective-integrity-adapter.md"
        target.write_text("before", encoding="utf-8")
        backup = self.install()
        installed = target.read_bytes()
        (backup / "files/objective-integrity-adapter.md").write_text("corrupt", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Backup integrity mismatch"):
            bootstrap.rollback(backup)
        self.assertEqual(target.read_bytes(), installed)

    def test_interrupted_rollback_preserves_frontier_first_fault_and_retry(self):
        self.target.mkdir()
        target = self.target / "objective-integrity-adapter.md"
        target.write_bytes(b"original")
        backup = self.install()
        real_write = bootstrap.atomic_write
        def interrupted(path, data):
            if path == target and data == b"original":
                raise OSError("synthetic rollback interruption")
            real_write(path, data)
        with patch.object(bootstrap, "atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "synthetic rollback interruption"):
                bootstrap.rollback(backup)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["state"], "ROLLBACK_INTERRUPTED")
        attempt = manifest["rollback_attempts"][0]
        self.assertEqual(attempt["restored"], ["objective-integrity/objective-contract.md"])
        self.assertEqual(attempt["pending"], "objective-integrity-adapter.md")
        fault = manifest["rollback_first_fault"]
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(backup)
        final = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(final["rollback_first_fault"], fault)
        self.assertEqual(len(final["rollback_attempts"]), 2)
        self.assertEqual(final["rollback_attempts"][0], attempt)
        self.assertEqual(final["state"], "ROLLED_BACK")
        self.assertEqual(target.read_bytes(), b"original")

    def test_manifest_cannot_escape_or_retarget_destination(self):
        backup = self.install()
        path = backup / "manifest.json"
        original = json.loads(path.read_text())
        original["entries"][0]["path"] = "../outside.txt"
        path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid relative member"):
            bootstrap.rollback(backup)
        original["destination"] = str(self.root / "another-project")
        path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "destination binding"):
            bootstrap.rollback(backup)

    def test_parallel_install_lock_is_not_removed_by_rejected_contender(self):
        self.target.mkdir()
        lock = self.target / bootstrap.LOCK
        lock.write_text("another owner", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "lock exists"):
            self.install()
        self.assertEqual(lock.read_text(), "another owner")
        self.assertFalse((self.target / bootstrap.BACKUPS).exists())

    def test_destination_redirect_is_rejected_before_resolution(self):
        actual = self.root / "real-project"
        actual.mkdir()
        link = self.root / "linked-project"
        try:
            os.symlink(actual, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Host cannot create this synthetic symlink: {exc}")
        with self.assertRaisesRegex(ValueError, "Redirected"):
            bootstrap.absolute(link)
        self.assertEqual(list(actual.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
