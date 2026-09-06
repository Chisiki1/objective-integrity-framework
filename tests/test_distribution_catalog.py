"""Join the generated public catalog to the actual shipped Skill members."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DistributionCatalog(unittest.TestCase):
    def test_catalog_binds_required_references_and_validates_at_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user = root / "empty-user-root"
            user.mkdir()
            registry = root / "registry.json"
            made = subprocess.run([sys.executable, "-B", str(ROOT / "tools/catalog.py"), "--output", str(registry)],
                                  text=True, capture_output=True, check=False)
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            data = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(len(data["entries"]), 8)
            lifecycle = next(item for item in data["entries"] if item["name"] == "master-guided-skill-lifecycle")
            members = {item["path"]: item["sha256"] for item in lifecycle["files"]}
            for relative in ("references/learning-loop.md", "references/condition-stewardship.md"):
                actual = ROOT / "runtime/skills/master-guided-skill-lifecycle" / relative
                self.assertEqual(members[relative], hashlib.sha256(actual.read_bytes()).hexdigest().upper())
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "runtime/skills/master-guided-skill-resolver/scripts/validate_registry.py"),
                 "--registry", str(registry), "--user-root", str(user), "--project-root", str(ROOT / "runtime/skills"),
                 "--master", str(ROOT / "templates/distribution-catalog.md")],
                text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["valid"])
            before = registry.read_bytes()
            duplicate = subprocess.run([sys.executable, "-B", str(ROOT / "tools/catalog.py"), "--output", str(registry)],
                                       text=True, capture_output=True, check=False)
            self.assertEqual(duplicate.returncode, 2)
            self.assertEqual(registry.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
