"""Package behavior and normal/negative paths, not model or directory approval."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("plugin_package", ROOT / "tools/plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class PluginPackage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oif-plugin-")
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "source"
        for directory in ("packaging", ".agents/skills/objective-integrity", "docs/assets", "evals", "tools"):
            shutil.copytree(ROOT / directory, self.root / directory)
        for name in ("LICENSE", "PRIVACY.md"):
            shutil.copyfile(ROOT / name, self.root / name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_preview_is_read_only_and_exact_build_is_self_contained(self):
        dest = self.parent / "build"
        preview = plugin.build(dest, root=self.root)
        self.assertFalse(preview["written"])
        self.assertFalse(dest.exists())
        made = plugin.build(dest, preview["plan_sha256"], True, self.root)
        self.assertFalse(made["host_installed"])
        self.assertFalse(made["directory_submitted"])
        directory = Path(made["package"])
        self.assertTrue(plugin.validate_directory(directory)["valid"])
        self.assertTrue((directory / "skills/objective-integrity/references/in-work-learning.md").is_file())
        with zipfile.ZipFile(made["archive"]) as archive:
            names = archive.namelist()
            self.assertTrue(all(n.startswith("objective-integrity/") for n in names))
            self.assertEqual(len(names), len(set(names)))
            self.assertFalse(any(n.endswith(("mcp.json", ".app.json")) or "/hooks/" in n for n in names))
            for name in names:
                self.assertEqual(archive.read(name), (dest / name).read_bytes())
        copied = self.parent / "relocated/objective-integrity"
        shutil.copytree(directory, copied)
        self.assertTrue(plugin.validate_directory(copied)["valid"])

    def test_stale_source_or_destination_never_overwrites(self):
        dest = self.parent / "build"
        preview = plugin.build(dest, root=self.root)
        page = self.root / "packaging/README.md"
        page.write_text(page.read_text() + "\nChanged after preview.\n", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "unchanged preview"):
            plugin.build(dest, preview["plan_sha256"], True, self.root)
        self.assertFalse(dest.exists())
        dest.mkdir()
        marker = dest / "user.txt"
        marker.write_text("preserve", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "absent"):
            plugin.build(dest, root=self.root)
        self.assertEqual(marker.read_text(), "preserve")

    def test_apply_without_preview_and_source_overlap_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "expect-plan"):
            plugin.build(self.parent / "build", apply=True, root=self.root)
        for destination in (self.root, self.root / "generated", self.parent):
            with self.assertRaises(ValueError):
                plugin.build(destination, root=self.root)

    def test_reproducible_archives_and_retained_previous_build(self):
        values = []
        for name in ("old-build", "new-build"):
            dest = self.parent / name
            preview = plugin.build(dest, root=self.root)
            made = plugin.build(dest, preview["plan_sha256"], True, self.root)
            values.append(Path(made["archive"]).read_bytes())
        self.assertEqual(values[0], values[1])
        self.assertEqual(hashlib.sha256(values[0]).hexdigest(), hashlib.sha256(values[1]).hexdigest())

    def test_metadata_and_required_resources(self):
        manifest, members = plugin.payload(self.root)
        mutations = [
            lambda m: m["extensions"]["com.openai"]["interface"].update(shortDescription="x" * 31),
            lambda m: m["extensions"]["com.openai"]["interface"].update(defaultPrompt=["same", " same "]),
            lambda m: m["extensions"]["com.openai"].update(apps="./.app.json"),
            lambda m: m["extensions"]["com.openai"]["interface"].update(screenshots=[]),
            lambda m: m["author"].update(name="unmatched"),
            lambda m: m.update(homepage="https://user:credential@127.0.0.1/")]
        for change in mutations:
            changed = copy.deepcopy(manifest)
            change(changed)
            with self.subTest(change=change), self.assertRaises(ValueError):
                plugin.validate_metadata(changed)
        required_pages = ("standard-workflow.md", "objective-continuity.md", "high-assurance.md", "skill-book.md", "in-work-learning.md")
        for missing in ("assets/icon.png", *("skills/objective-integrity/references/" + n for n in required_pages)):
            incomplete = dict(members)
            del incomplete[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                plugin.validate_members(incomplete, manifest)
        for name in ("../escape", "skills\\escape", "assets/ICON.png"):
            changed = dict(members)
            changed[name] = b"bad"
            with self.subTest(name=name), self.assertRaises(ValueError):
                plugin.validate_members(changed, manifest)

    def test_every_required_reference_is_checked_before_build_and_after_relocation(self):
        pages = ("standard-workflow.md", "objective-continuity.md", "high-assurance.md", "skill-book.md", "in-work-learning.md")
        view = plugin.build(self.parent / "complete", root=self.root)
        made = plugin.build(self.parent / "complete", view["plan_sha256"], True, self.root)
        for name in pages:
            for base in (self.root / ".agents/skills/objective-integrity", Path(made["package"]) / "skills/objective-integrity"):
                page = base / "references" / name
                raw = page.read_bytes()
                page.unlink()
                try:
                    with self.subTest(page=name, base=base.name), self.assertRaisesRegex(ValueError, "Missing bundled skill reference"):
                        if base == self.root / ".agents/skills/objective-integrity":
                            plugin.build(self.parent / "must-not-exist", root=self.root)
                        else:
                            plugin.validate_directory(Path(made["package"]))
                    self.assertFalse((self.parent / "must-not-exist").exists())
                finally:
                    page.write_bytes(raw)

    def test_fresh_process_preview_binds_each_executable_helper(self):
        for name in ("bootstrap.py", "path_identity.py"):
            dest = self.parent / (name + "-build")
            argv = [sys.executable, "-B", str(self.root / "tools/plugin.py"), "build", "--destination", str(dest)]
            before = subprocess.run(argv, capture_output=True)
            self.assertEqual(before.returncode, 0, (before.stdout, before.stderr))
            view = json.loads(before.stdout)
            helper = self.root / "tools" / name
            raw = helper.read_bytes()
            helper.write_bytes(raw + b"\n# Same behavior, different reviewed helper bytes.\n")
            try:
                stale = subprocess.run([*argv, "--expect-plan", view["plan_sha256"], "--apply"], capture_output=True)
                self.assertEqual(stale.returncode, 2, (stale.stdout, stale.stderr))
                self.assertIn(b"unchanged preview", stale.stderr)
                self.assertFalse(dest.exists())
            finally:
                helper.write_bytes(raw)

    def test_scenarios_remain_specs_not_fabricated_observations(self):
        cases = json.loads((self.root / "evals/plugin-scenarios.json").read_bytes())
        self.assertEqual(cases["observation"]["status"], "NOT-RUN")
        self.assertEqual({c["id"] for c in cases["cases"]}, {"P01", "P02", "P03", "P04", "P05", "N01", "N02", "N03"})
        self.assertEqual(sum(c["should_activate"] for c in cases["cases"]), 5)
        for case in cases["cases"]:
            self.assertTrue(case["turns"] and case["required"] and case["forbidden"])
        from eval_runner import plugin_scenario_errors
        self.assertEqual(plugin_scenario_errors(cases), [])
        for mutate in (lambda c: c["cases"].pop(),
                       lambda c: c["cases"][0].update(id="N01"),
                       lambda c: c["observation"].update(status="PASS"),
                       lambda c: c["cases"][0].update(should_activate=False)):
            bad = copy.deepcopy(cases)
            mutate(bad)
            self.assertTrue(plugin_scenario_errors(bad))

    def test_redirected_source_is_rejected_when_links_are_available(self):
        page = self.root / "packaging/README.md"
        before = page.read_bytes()
        page.unlink()
        external = self.parent / "external.md"
        external.write_bytes(before)
        try:
            page.symlink_to(external)
        except OSError:
            page.write_bytes(before)
            self.skipTest("Host does not permit this symlink test")
        with self.assertRaises(ValueError):
            plugin.payload(self.root)

    def test_scenario_specification_cannot_hide_missing_normative_fixtures(self):
        for rel in ("framework/core-invariants.md", "framework/normative-reference.md",
                    "docs/evaluation.md", "docs/skill-book.md"):
            destination = self.root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, destination)
        for fixture in (self.root / "evals").glob("*.json"):
            if fixture.name != "plugin-scenarios.json":
                fixture.unlink()
        result = subprocess.run([sys.executable, "-B", str(self.root / "tools/eval_runner.py"), str(self.root)],
                                capture_output=True)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(b"required evaluation fixtures are missing", result.stdout)
        self.assertNotIn(b"eval_runner: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
