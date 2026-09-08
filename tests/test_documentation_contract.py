"""Keep optional translation aligned without creating a privacy exclusion."""
import importlib.util
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oif_document_privacy", ROOT / "tools/privacy_scan.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class DocumentationContract(unittest.TestCase):
    def scan(self, root):
        with redirect_stdout(StringIO()):
            return privacy.main([str(root)])

    def test_optional_language_is_not_a_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "docs/ja/README.md"
            page.parent.mkdir(parents=True)
            page.write_text("\u65e5\u672c\u8a9e\n", encoding="utf-8")
            self.assertEqual(self.scan(root), 0)
            page.write_text("\u65e5\u672c\u8a9e\n" + "sk-" + "x" * 24, encoding="utf-8")
            self.assertEqual(self.scan(root), 1)
            page.write_text("\u65e5\u672c\u8a9e\n", encoding="utf-8")
            (root / "README.md").write_text("\u65e5\u672c\u8a9e\n", encoding="utf-8")
            self.assertEqual(self.scan(root), 1)

    def test_edition_commands_and_credits_match(self):
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        japanese = (ROOT / "docs/ja/README.md").read_text(encoding="utf-8")
        adoption = (ROOT / "docs/adoption.md").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"\d{4}-\d{2}-\d{2}", english).group(),
                         re.search(r"\d{4}-\d{2}-\d{2}", japanese).group())
        self.assertIn("[Japanese guide](docs/ja/README.md)", english)
        self.assertIn("[English](../../README.md)", japanese)
        commands = re.findall(r"^python (?:tools/bootstrap\.py|\.\./sample-project/\.oif/tools/check\.py).+$", adoption, re.M)
        self.assertGreaterEqual(len(commands), 6)
        for command in commands:
            self.assertIn(command, japanese)
        credits = set(re.findall(r"https://x\.com/[A-Za-z0-9_]+", english))
        self.assertEqual(len(credits), 2)
        self.assertEqual(credits, set(re.findall(r"https://x\.com/[A-Za-z0-9_]+", japanese)))
        self.assertIn(".objective-integrity-backups", adoption)
        self.assertIn(".objective-integrity-backups", japanese)


if __name__ == "__main__":
    unittest.main()
