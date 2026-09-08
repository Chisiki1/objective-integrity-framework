"""Preserve adopter-owned state while updating and recovering a known package."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import bootstrap
finally:
    sys.path.pop(0)


class PackageUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "project"
        self.first = self.root / "first.md"
        self.first.write_text("first package", encoding="utf8")
        self.second = self.root / "second.md"
        self.second.write_text("updated package", encoding="utf8")
        self.mapping = {"objective-integrity-adapter.md": self.first,
                        "objective-integrity/objective-contract.md": self.first,
                        ".oif/README.md": self.first, ".oif/old.md": self.first}
        self.mock = patch.object(bootstrap, "planned_files", side_effect=lambda *a: dict(self.mapping))
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.call(apply=True)
        self.original_backup = self.latest()
        (self.target / "objective-integrity-adapter.md").write_text("my settings", encoding="utf8")
        (self.target / "objective-integrity/objective-contract.md").write_text("my actual objective", encoding="utf8")
        (self.target / ".oif/unowned.md").write_text("my unrelated file", encoding="utf8")
        self.mapping.pop(".oif/old.md")
        self.mapping[".oif/README.md"] = self.second
        self.mapping[".oif/new.md"] = self.second

    def latest(self):
        return sorted((self.target / bootstrap.BACKUPS).iterdir())[-1]

    def call(self, apply=False, update=False, expected=None):
        out = io.StringIO()
        with redirect_stdout(out):
            bootstrap.install(self.target, "generic", "complete", apply, expected, update)
        return out.getvalue()

    def snapshot(self):
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in self.target.rglob("*") if p.is_file()}

    def assert_personal_state(self):
        self.assertEqual((self.target / "objective-integrity-adapter.md").read_text(), "my settings")
        self.assertEqual((self.target / "objective-integrity/objective-contract.md").read_text(), "my actual objective")
        self.assertEqual((self.target / ".oif/unowned.md").read_text(), "my unrelated file")

    def test_preview_update_deletion_and_recovery_preserve_personal_state(self):
        before = self.snapshot()
        preview = self.call(update=True)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("removed obsolete owned members: ['.oif/old.md']", preview)
        plan = re.search(r"plan-sha256: ([0-9a-f]{64})", preview).group(1)
        self.call(apply=True, update=True, expected=plan)
        updated_backup = self.latest()
        self.assertFalse((self.target / ".oif/old.md").exists())
        self.assertEqual((self.target / ".oif/new.md").read_text(), "updated package")
        self.assert_personal_state()
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(updated_backup)
            bootstrap.rollback(updated_backup)
        self.assertEqual((self.target / ".oif/old.md").read_text(), "first package")
        self.assertEqual((self.target / ".oif/README.md").read_text(), "first package")
        self.assertFalse((self.target / ".oif/new.md").exists())
        self.assert_personal_state()
        self.call(apply=True, update=True)
        self.assert_personal_state()

    def test_modified_owned_file_rejects_before_any_write(self):
        (self.target / ".oif/README.md").write_text("local package customization", encoding="utf8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Update conflict"):
            self.call(apply=True, update=True)
        self.assertEqual(self.snapshot(), before)

    def test_new_member_cannot_replace_unowned_file(self):
        (self.target / ".oif/new.md").write_text("user file", encoding="utf8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "unowned file"):
            self.call(apply=True, update=True)
        self.assertEqual(self.snapshot(), before)

    def test_stale_plan_and_changed_manifest_preserve_destination(self):
        preview = self.call(update=True)
        plan = re.search(r"plan-sha256: ([0-9a-f]{64})", preview).group(1)
        manifest = self.original_backup / "manifest.json"
        data = json.loads(manifest.read_text())
        data["annotation"] = "changed history input"
        manifest.write_text(json.dumps(data), encoding="utf8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Preview plan differs"):
            self.call(apply=True, update=True, expected=plan)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_v2_manifest_can_update_and_rollback(self):
        manifest = self.original_backup / "manifest.json"
        data = json.loads(manifest.read_text())
        data["schema_version"] = "oif-install-v2"
        data.pop("update_from", None)
        manifest.write_text(json.dumps(data), encoding="utf8")
        self.call(apply=True, update=True)
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(self.latest())
        self.assert_personal_state()

    def test_interrupted_update_retains_first_fault_and_requires_recovery(self):
        real_write = bootstrap.atomic_write
        def fail(path, data):
            if path == self.target / ".oif/new.md":
                raise OSError("synthetic update interruption")
            return real_write(path, data)
        with patch.object(bootstrap, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(OSError, "synthetic update interruption"):
                self.call(apply=True, update=True)
        backup = self.latest()
        original = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(original["state"], "INTERRUPTED")
        self.assertIn("synthetic update interruption", original["first_fault"])
        with self.assertRaisesRegex(ValueError, "unresolved effects"):
            self.call(apply=True, update=True)
        with redirect_stdout(io.StringIO()):
            bootstrap.rollback(backup)
        final = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(final["first_fault"], original["first_fault"])
        self.assert_personal_state()

    def test_later_edit_holds_entire_update_rollback(self):
        self.call(apply=True, update=True)
        (self.target / ".oif/new.md").write_text("later edit", encoding="utf8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Rollback conflict"):
            bootstrap.rollback(self.latest())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.target / ".oif/old.md").exists())

    def test_no_history_and_minimal_mode_do_not_infer_update_ownership(self):
        empty = self.root / "empty"
        with self.assertRaisesRegex(ValueError, "prior complete"):
            bootstrap.install(empty, "generic", "complete", True, update=True)
        with self.assertRaisesRegex(ValueError, "requires --mode complete"):
            bootstrap.install(self.target, "generic", "minimal", True, update=True)
        self.assertFalse(empty.exists())


if __name__ == "__main__":
    unittest.main()
