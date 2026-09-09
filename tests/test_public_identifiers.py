"""Public classifications never become whole-file or history-wide exclusions."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from public_identifiers import DECLARATIONS, PublicIdentifiers, parse


class PublicIdentifierBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oif-identifiers-")
        self.root = Path(self.temp.name)
        self.value = "8" * 64  # Generated synthetic fixture, never a secret.
        self.raw = ("Public fixture digest: " + self.value + "\n").encode()
        (self.root / "fixture.txt").write_bytes(self.raw)
        self.row = dict(path="fixture.txt", blob_sha256=hashlib.sha256(self.raw).hexdigest(),
                        kind="long_hex_identifier", value=self.value,
                        reason="Synthetic reviewed digest", provenance="Disposable test fixture")
        self.declare([self.row])

    def tearDown(self):
        def writable(function, path, _):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(self.root, onerror=writable)
        self.temp.cleanup()

    def declare(self, rows):
        path = self.root / DECLARATIONS
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(schema="oif-public-identifiers-v1", entries=rows)), encoding="utf8")

    def scan(self, tool, expected):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / (tool + ".py")), str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        result = subprocess.run(["git", "-C", str(self.root), "-c", "commit.gpgsign=false",
                                 "-c", "user.name=Fixture", "-c", "user.email=fixture", *args], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic identifier fixture")

    def test_exact_tree_and_history_then_changed_deleted_blob(self):
        self.scan("privacy_scan", 0)
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.commit()
        self.scan("history_scan", 0)
        (self.root / "fixture.txt").write_bytes(self.raw + b"changed\n")
        self.scan("privacy_scan", 1)
        self.commit()
        (self.root / "fixture.txt").unlink()
        self.commit()
        self.assertIn(b"fixture.txt: long_hex:", self.scan("history_scan", 1))

    def test_moved_or_substituted_value_remains_a_finding(self):
        (self.root / "fixture.txt").rename(self.root / "moved.txt")
        self.scan("privacy_scan", 1)
        (self.root / "moved.txt").unlink()
        (self.root / "fixture.txt").write_bytes(self.raw.replace(b"8", b"7"))
        self.scan("privacy_scan", 1)

    def test_no_whole_file_or_declaration_text_exclusion(self):
        token = "ghp" + "_" + "x" * 24
        private = "C:" + "\\Users\\" + "PrivateFixture\\data"
        email = "private" + "@" + "example.invalid"
        for residue in (token, private, email, "9" * 64):
            changed = (self.raw.decode() + residue).encode()
            (self.root / "fixture.txt").write_bytes(changed)
            # Even a correctly re-bound known digest cannot exempt another value.
            row = dict(self.row, blob_sha256=hashlib.sha256(changed).hexdigest())
            self.declare([row])
            self.scan("privacy_scan", 1)
            (self.root / "fixture.txt").write_bytes(self.raw)
            self.declare([dict(self.row, reason="Fixture " + residue)])
            self.scan("privacy_scan", 1)

    def test_strict_declarations_and_extra_literal_override(self):
        raw = (self.root / DECLARATIONS).read_bytes()
        value = json.loads(raw)
        value["extra"] = "unrecognized"
        with self.assertRaises(ValueError):
            parse(json.dumps(value).encode())
        for change in (dict(path="../escape"), dict(kind="token"), dict(blob_sha256="short")):
            self.declare([dict(self.row, **change)])
            self.scan("privacy_scan", 1)
        self.declare([self.row])
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/privacy_scan.py"), str(self.root),
                                 "--extra-literal", self.value], capture_output=True)
        self.assertEqual(result.returncode, 1)
        with self.assertRaises(ValueError):
            parse(b'{"schema":"one","schema":"two","entries":[]}')

    def test_decoded_free_text_is_not_an_exemption_in_tree_or_history(self):
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.commit()
        self.scan("history_scan", 0)
        private = "C:" + "\\Users\\" + "PrivateFixture\\data"
        for field in ("reason", "provenance"):
            for residue in (private, "ghp" + "_" + "x" * 24, self.value):
                with self.subTest(field=field, residue=residue):
                    self.declare([dict(self.row, **{field: residue})])
                    self.scan("privacy_scan", 1)
                    self.commit()
                    self.scan("history_scan", 1)
        self.declare([self.row])
        self.commit()
        self.scan("privacy_scan", 0)
        # Removing escaped residue from the current tree cannot erase history.
        self.assertIn(b"declaration_private_path", self.scan("history_scan", 1))

    def test_old_declarations_cannot_authorize_other_historical_blobs(self):
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.commit()
        self.declare([])
        (self.root / "fixture.txt").unlink()
        self.commit()
        self.assertIn(b"fixture.txt: long_hex:", self.scan("history_scan", 1))

    def test_actual_classifications_have_exact_current_artifacts(self):
        public = PublicIdentifiers(ROOT)
        for row in public.entries:
            raw = (ROOT / row["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["blob_sha256"].lower())
            self.assertIn(row["value"].encode(), raw)
            self.assertTrue(public.permits(row["path"], raw, row["kind"], row["value"]))


if __name__ == "__main__":
    unittest.main()
