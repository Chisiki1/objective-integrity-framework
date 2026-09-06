"""Physical alias protection and the preserved safe extended-path consumer."""
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location("alias_test_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


identity = load_tool("path_identity")
bootstrap = load_tool("bootstrap")
checker = load_tool("check")
demo = load_tool("demo")
catalog = load_tool("catalog")


def extended(path):
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:]) if value.startswith("\\\\") else Path("\\\\?\\" + value)


class PathIdentity(unittest.TestCase):
    def test_ordinary_existing_ancestor_and_new_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(identity.within(root / "new" / "leaf", root))
            self.assertFalse(identity.within(root, root / "new"))
            self.assertEqual(identity.comparison_identity(root / "new" / ".." / "leaf"), identity.comparison_identity(root / "leaf"))

    @unittest.skipUnless(os.name == "nt", "Native Windows extended-path alias relation")
    def test_extended_aliases_cannot_change_containment_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "runtime").mkdir()
            self.assertTrue(os.path.samefile(source, extended(source)))
            self.assertEqual(identity.comparison_identity(source), identity.comparison_identity(extended(source)))
            self.assertEqual(identity.comparison_identity(source / "new"), identity.comparison_identity(extended(source / "new")))
            with patch.object(bootstrap, "ROOT", source), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                for target in (source, source / "new", root):
                    self.assertEqual(bootstrap.main(["--destination", str(extended(target))]), 2)
            with patch.object(checker, "ROOT", source):
                for target in (source, source / "runtime", root):
                    with self.assertRaisesRegex(ValueError, "separate"):
                        checker.prepare_temporary_parent(str(extended(target)))
            with patch.object(demo, "ROOT", source):
                for target in (source, source / "new", root):
                    with self.assertRaisesRegex(ValueError, "separate"):
                        demo.safe_destination(str(extended(target)))
            output = source / "runtime" / "forbidden-registry.json"
            with patch.object(catalog, "ROOT", source), patch.object(catalog, "build", return_value={}), \
                 patch.object(sys, "argv", ["catalog.py", "--output", str(extended(output))]), redirect_stderr(io.StringIO()):
                self.assertEqual(catalog.main(), 2)
            self.assertFalse(output.exists())
            self.assertFalse((source / "new").exists())
            self.assertEqual(set(source.iterdir()), {source / "runtime"})
            self.assertTrue(bootstrap.is_global_like(extended(Path.home())))
            home_is_source_ancestor = Path.home().resolve() in (ROOT, *ROOT.parents)
            expected_reason = "separate" if home_is_source_ancestor else "global"
            with self.assertRaisesRegex(ValueError, expected_reason):
                checker.prepare_temporary_parent(str(extended(Path.home())))
            with self.assertRaisesRegex(ValueError, "global"):
                demo.safe_destination(str(extended(Path.home() / ".agents" / "unused-demo")))

    @unittest.skipUnless(os.name == "nt", "Native Windows extended-path I/O")
    def test_safe_sibling_preserves_preview_apply_and_normal_spelling_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "sibling"
            for relative in ("adapters/generic/system-developer-adapter.md", "templates/objective-contract.md"):
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            stdout = io.StringIO()
            with patch.object(bootstrap, "ROOT", source), redirect_stdout(stdout):
                self.assertEqual(bootstrap.main(["--destination", str(target)]), 0)
                plan = re.search(r"plan-sha256: ([0-9a-f]{64})", stdout.getvalue()).group(1)
                self.assertEqual(bootstrap.main(["--destination", str(extended(target)), "--expect-plan", plan, "--apply"]), 0)
            self.assertTrue((target / "objective-integrity-adapter.md").is_file())
            backup = next((target / bootstrap.BACKUPS).iterdir())
            # Drop the extended spelling to challenge recovery's physical binding.
            normal_backup = identity.comparison_identity(backup)
            with redirect_stdout(io.StringIO()):
                bootstrap.rollback(normal_backup)
            self.assertFalse((target / "objective-integrity-adapter.md").exists())
            with patch.object(checker, "ROOT", source):
                self.assertTrue(os.path.samefile(checker.prepare_temporary_parent(str(extended(target))), target))
            with patch.object(demo, "ROOT", source):
                self.assertEqual(identity.comparison_identity(demo.safe_destination(str(extended(root / "new-demo")))), identity.comparison_identity(root / "new-demo"))


if __name__ == "__main__":
    unittest.main()
