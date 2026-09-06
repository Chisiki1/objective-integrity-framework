"""Runner setup and installed-input boundaries, independent of product behavior."""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_under_test", ROOT / "tools/check.py")
checker = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / "tools"))
try:
    spec.loader.exec_module(checker)
finally:
    sys.path.pop(0)


class CheckBoundaries(unittest.TestCase):
    def test_runtime_requires_explicit_parent(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            checker.prepare_temporary_parent(None)

    def test_source_and_global_locations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            checker.prepare_temporary_parent(str(ROOT))
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / ".agents" / "state"
            parent.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "global"):
                checker.prepare_temporary_parent(str(parent))

    def test_parent_is_read_only_until_owned_child_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            sentinel = parent / "another-owner.txt"
            sentinel.write_bytes(b"keep")
            self.assertEqual(checker.prepare_temporary_parent(str(parent)), parent.resolve())
            self.assertEqual(list(parent.iterdir()), [sentinel])

    def test_redirected_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual"
            actual.mkdir()
            link = root / "link"
            try:
                os.symlink(actual, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Host cannot create a synthetic symlink: {exc}")
            with self.assertRaisesRegex(ValueError, "redirected"):
                checker.prepare_temporary_parent(str(link))

    def test_required_evaluation_fixture_absence_is_not_pass(self):
        spec = importlib.util.spec_from_file_location("evaluation_under_test", ROOT / "tools/eval_runner.py")
        evaluation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evaluation)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("framework/core-invariants.md", "framework/normative-reference.md", "docs/evaluation.md", "docs/skill-book.md"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(evaluation.main(str(root)), 1)
            self.assertIn("required evaluation fixtures are missing", output.getvalue())


if __name__ == "__main__":
    unittest.main()
