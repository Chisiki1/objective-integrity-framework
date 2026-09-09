"""Actual clean-source asset building plus negative release boundaries."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release


class ReleaseAssets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oif-release-")
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "repo"
        self.root.mkdir()
        for directory in ("tools", "packaging", ".agents", "docs/assets", "evals"):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("LICENSE", "NOTICE", "VERSION", "PRIVACY.md", "README.md"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.commit()

    def tearDown(self):
        # Git creates read-only object files on Windows.
        def writable(function, path, _):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(self.parent, onerror=writable)
        self.temp.cleanup()

    def git(self, *args):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        result = subprocess.run(["git", "-C", str(self.root), "-c", "commit.gpgsign=false", "-c", "user.name=OIF Test",
                                 "-c", "user.email=test@example.invalid", *args],
                                capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result.stdout

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic release fixture")

    def test_preview_exact_assets_repeatability_and_no_overwrite(self):
        builds = []
        for name in ("first", "second"):
            dest = self.parent / name
            preview = release.build(dest, root=self.root)
            self.assertFalse(preview["written"])
            self.assertFalse(dest.exists())
            result = release.build(dest, preview["plan_sha256"], True, self.root)
            self.assertFalse(result["published"])
            self.assertFalse(result["host_installed"])
            files = {n: (dest / n).read_bytes() for n in result["assets"]}
            builds.append(files)
            for line in files["SHA256SUMS.txt"].decode().splitlines():
                sha, filename = line.split("  ")
                self.assertEqual(hashlib.sha256(files[filename]).hexdigest(), sha)
            record = json.loads(files["release.json"])
            self.assertEqual(record["source_commit"], self.git("rev-parse", "HEAD").decode().strip())
            self.assertNotIn(str(self.parent), files["release.json"].decode())
            for asset in record["artifacts"]:
                with zipfile.ZipFile(dest / asset["name"]) as z:
                    license_name = next(n for n in z.namelist() if n.endswith("/LICENSE"))
                    self.assertEqual(z.read(license_name), (self.root / "LICENSE").read_bytes())
                    self.assertTrue(any(n.endswith("/NOTICE") for n in z.namelist()))
            with self.assertRaisesRegex(ValueError, "absent"):
                release.build(dest, root=self.root)
        self.assertEqual(builds[0], builds[1])

    def test_dirty_untracked_version_mismatch_and_stale_preview(self):
        dest = self.parent / "must-not-exist"
        preview = release.build(dest, root=self.root)
        page = self.root / "README.md"
        original = page.read_bytes()
        page.write_bytes(original + b"\nchange\n")
        with self.assertRaisesRegex(ValueError, "clean committed"):
            release.build(dest, root=self.root)
        self.commit()
        with self.assertRaisesRegex(ValueError, "unchanged preview"):
            release.build(dest, preview["plan_sha256"], True, self.root)
        extra = self.root / "untracked.txt"
        extra.write_text("private fixture", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "clean committed"):
            release.build(dest, root=self.root)
        extra.unlink()
        (self.root / "VERSION").write_text("0.1.2\n", encoding="utf8")
        self.commit()
        with self.assertRaisesRegex(ValueError, "must agree"):
            release.build(dest, root=self.root)
        self.assertFalse(dest.exists())

    def test_missing_preview_and_source_overlap_are_rejected(self):
        dest = self.parent / "absent"
        with self.assertRaisesRegex(ValueError, "expect-plan"):
            release.build(dest, apply=True, root=self.root)
        for target in (self.root, self.root / "output", self.parent):
            with self.assertRaisesRegex(ValueError, "separate"):
                release.build(target, root=self.root)
        self.assertFalse(dest.exists())

    def test_mid_build_source_change_never_becomes_a_mixed_release(self):
        dest = self.parent / "retained-partial"
        preview = release.build(dest, root=self.root)
        real_build = release.plugin.build
        def changed_before_plugin(*args, **kwargs):
            page = self.root / "packaging/README.md"
            page.write_bytes(page.read_bytes() + b"\nConcurrent fixture change\n")
            return real_build(*args, **kwargs)
        with patch.object(release.plugin, "build", side_effect=changed_before_plugin):
            with self.assertRaisesRegex(ValueError, "changed during"):
                release.build(dest, preview["plan_sha256"], True, self.root)
        self.assertTrue(dest.exists())
        self.assertEqual(list(dest.iterdir()), [])

    def test_published_license_and_version_contract(self):
        expected = (ROOT / "VERSION").read_text().strip()
        manifest = json.loads((ROOT / "packaging/plugin.json").read_bytes())
        self.assertEqual(manifest["version"], expected)
        self.assertEqual(manifest["license"], "Apache-2.0")
        for page in ("README.md", "docs/ja/README.md", "docs/releases.md"):
            self.assertIn(expected, (ROOT / page).read_text(encoding="utf8"))
        for page in ("packaging/README.md", "docs/plugin.md"):
            text = (ROOT / page).read_text(encoding="utf8")
            self.assertNotIn("MIT licens", text)
            self.assertIn("Apache", text)


if __name__ == "__main__":
    unittest.main()
