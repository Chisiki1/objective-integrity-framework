"""Explicit generic condition inventories resolve without installing any policy."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import workflow_governance as governance


def digest(raw):
    return hashlib.sha256(raw).hexdigest().upper()


class CurrentConditionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = self.root / "conditions.txt"
        self.index_path = self.root / "index.json"
        self.bodies = {"01.": "Preserve the requested outcome.",
                       "105.": "Require separate authority for external actions."}
        self.write_document()
        self.index = {
            "schema_version": "workflow-condition-index-v1",
            "current_condition_document": {"path": str(self.document),
                                           "sha256": digest(self.document.read_bytes())},
            "conditions": [
                {"id": f"condition-{index}", "locator": locator,
                 "current_body_sha256": digest(body.encode("utf-8"))}
                for index, (locator, body) in enumerate(self.bodies.items(), 1)
            ],
        }

    def write_document(self):
        content = "Context is not itself a numbered condition.\r\n" + "".join(
            f"{locator} {body}\r\n" for locator, body in self.bodies.items())
        self.document.write_bytes(content.encode("utf-8"))

    def resolve(self, index=None):
        self.index_path.write_text(json.dumps(self.index if index is None else index), encoding="utf-8")
        return governance.current_conditions(str(self.index_path))

    def test_arbitrary_nonempty_inventory_has_no_private_count_or_authority(self):
        self.resolve()
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        result = governance.current_conditions(str(self.index_path))
        self.assertEqual("REUSE_EXACT_CURRENT_CONDITIONS", result["route"])
        self.assertEqual(2, result["condition_count"])
        self.assertEqual(["condition-1", "condition-2"], result["condition_ids"])
        self.assertFalse(result["authority_granted"])
        self.assertFalse(result["version_created"])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.iterdir()})

    def test_missing_extra_duplicate_or_wrong_body_is_rejected(self):
        variants = []
        missing = copy.deepcopy(self.index)
        missing["conditions"].pop()
        variants.append(missing)
        duplicate = copy.deepcopy(self.index)
        duplicate["conditions"].append(copy.deepcopy(duplicate["conditions"][0]))
        variants.append(duplicate)
        wrong = copy.deepcopy(self.index)
        wrong["conditions"][0]["current_body_sha256"] = "A" * 64
        variants.append(wrong)
        missing_locator = copy.deepcopy(self.index)
        missing_locator["conditions"][0]["locator"] = "2."
        variants.append(missing_locator)
        empty = copy.deepcopy(self.index)
        empty["conditions"] = []
        variants.append(empty)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve(value)

    def test_duplicate_document_locator_cannot_be_overwritten_by_dictionary(self):
        self.document.write_bytes(self.document.read_bytes() + b"01. Another body.\r\n")
        self.index["current_condition_document"]["sha256"] = digest(self.document.read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicate numbered"):
            self.resolve()

    def test_changed_document_and_duplicate_json_do_not_resolve(self):
        self.document.write_bytes(self.document.read_bytes() + b"Changed context.\n")
        with self.assertRaisesRegex(ValueError, "document changed"):
            self.resolve()
        self.index_path.write_bytes(b'{"schema_version":"workflow-condition-index-v1","schema_version":"other"}')
        with self.assertRaises(ValueError):
            governance.current_conditions(str(self.index_path))

    def test_cli_is_a_real_read_only_consumer(self):
        self.resolve()
        command = [sys.executable, "-B", str(Path(governance.__file__)),
                   "--current-conditions", str(self.index_path)]
        result = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, json.loads(result.stdout)["condition_count"])
        self.document.write_bytes(b"changed")
        result = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(2, result.returncode)
        self.assertEqual("INVALID_REQUEST", json.loads(result.stdout)["route"])


if __name__ == "__main__":
    unittest.main()
