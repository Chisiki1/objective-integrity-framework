"""Exercise the public entrypoint and the demo's actual connected file consumer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OIF = ROOT / "tools" / "oif.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class CurrentWorkflowConsumerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Reuse one immutable successful run for read-only assertions. The demo
        # itself still requires an explicit new/empty destination and runs every
        # structural tool against its actual owned inputs, not mocked responses.
        parent = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
        cls.temporary = tempfile.TemporaryDirectory(prefix="oif-current-", dir=parent)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.destination = cls.root / "demo"
        process_temp = cls.root / "process-temp"
        process_temp.mkdir()
        cls.environment = os.environ.copy()
        cls.environment.update({"PYTHONDONTWRITEBYTECODE": "1", "TEMP": str(process_temp),
                                "TMP": str(process_temp), "TMPDIR": str(process_temp)})
        result = subprocess.run(
            [sys.executable, "-B", str(OIF), "demo", "--directory", str(cls.destination)],
            cwd=ROOT, env=cls.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            # Preserve the first failing child output in the test failure before
            # temporary cleanup. Dependent assertions are not marked successful.
            evidence = [result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")]
            record_path = cls.destination / "run-record.json"
            if record_path.is_file():
                record = json.loads(record_path.read_bytes())
                evidence.append(json.dumps(record, indent=2))
                for step in record.get("steps", [])[-1:]:
                    for stream in ("stdout", "stderr"):
                        evidence.append((cls.destination / step[stream]).read_text(encoding="utf-8", errors="replace"))
            raise AssertionError("Demo first fault; dependent observations NOT-OBSERVED:\n" + "\n".join(evidence))
        cls.summary = json.loads((cls.destination / "demo-result.json").read_bytes())
        cls.current = cls.summary["current_workflow"]
        cls.workflow = cls.destination / "current-workflow"
        cls.record = json.loads((cls.destination / "run-record.json").read_bytes())

    def cli(self, *arguments: str, expected: int = 0) -> dict:
        result = subprocess.run([sys.executable, "-B", str(OIF), *arguments],
                                cwd=ROOT, env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, expected,
                         result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    def output(self, name: str) -> dict:
        matches = [step for step in self.record["steps"] if step["name"] == name]
        self.assertEqual(len(matches), 1)
        return json.loads((self.destination / matches[0]["stdout"]).read_bytes())

    def test_whole_scope_repair_reaches_both_actual_consumers(self) -> None:
        self.assertEqual(self.current["schema"], "oif-current-workflow-demo-v1")
        self.assertEqual(self.current["phase_sequence"], ["BUILD", "SWEEP", "REPAIR", "SWEEP", "ACCEPT"])
        label = (self.workflow / "label.txt").read_text(encoding="utf-8").strip()
        index = json.loads((self.workflow / "index.json").read_bytes())
        self.assertEqual(label, "Demo Project")
        self.assertEqual(index, {"label": "label.txt", "display_name": "Demo Project"})
        self.assertEqual((self.workflow / index["label"]).read_text(encoding="utf-8").strip(), label)
        self.assertEqual([row["status"] for row in self.current["initial_findings"]], ["fail", "fail"])
        self.assertEqual([row["status"] for row in self.current["final_observations"]], ["pass", "pass"])
        repair = json.loads((self.workflow / "repair-binding.json").read_bytes())
        repair_state = json.loads(Path(repair["state_ref"]["path"]).read_bytes())
        self.assertEqual(set(repair["item_ids"]), {"label", "index"})
        self.assertEqual(set(repair["finding_ids"]), {"label-name", "index-name"})
        self.assertEqual({finding["root_cause"] for finding in repair_state["findings"]}, {"shared-display-name"})
        final = json.loads((self.workflow / "accept-state.json").read_bytes())
        self.assertTrue(self.current["original_findings_preserved"])
        self.assertEqual(len(final["findings"]), 2)
        for finding in final["findings"]:
            self.assertEqual(finding["status"], "resolved")
            first = Path(finding["evidence_ref"]["path"])
            self.assertEqual(digest(first), finding["evidence_ref"]["sha256"])
            self.assertEqual(json.loads(first.read_bytes()), self.current["initial_findings"])

    def test_current_consumers_admit_structure_without_manufacturing_authority(self) -> None:
        for phase in self.current["phase_observations"]:
            self.assertEqual(phase["owner_control"], "ADMIT")
            self.assertFalse(phase["permission_granted"])
        self.assertFalse(self.current["permission_granted"])
        self.assertFalse(self.current["source_outcome_completed"])
        self.assertIn("Synthetic structural fixtures", self.current["review_evidence"])
        for name in ("build", "sweep", "repair", "resweep", "accept"):
            transition = self.output("current-" + name + "-transition")
            self.assertTrue(transition["work_phase"]["admitted"])
            self.assertFalse(transition["work_phase"]["source_outcome_completed"])
        progress = self.output("status-card-owner-input")
        self.assertEqual(progress["contract_id"], "demo-contract-v1")
        self.assertEqual(progress["outcome_updates"][0]["outcome_id"], "DEMO-O1")
        self.assertEqual(self.summary["outcomes"]["DEMO-O1"]["status"], "SATISFIED")

    def test_typed_allocation_consumes_fresh_result_without_dispatch(self) -> None:
        evidence = [json.loads(line) for line in (self.workflow / "allocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["record_type"] for row in evidence], ["raw_process", "consumption"])
        raw, consumed = evidence
        self.assertTrue(raw["process_started"])
        self.assertEqual(raw["returncode"], 0)
        result = json.loads(base64.b64decode(raw["stdout_base64"]))
        self.assertEqual(consumed["result_sha256"], result["result_sha256"])
        self.assertEqual(consumed["input_sha256"], digest(self.workflow / "allocation.json"))
        self.assertEqual(consumed["decision"], "RETAIN_CURRENT")
        self.assertTrue(consumed["selection_ready"])
        self.assertFalse(consumed["permission_granted"])
        self.assertFalse(consumed["dispatch_performed"])

    def test_current_history_organization_keeps_exact_backing_retrievable(self) -> None:
        history = self.current["history"]
        current = (self.destination / history["current"]).read_bytes()
        backing = (self.destination / history["backing"]).read_bytes()
        self.assertTrue(history["complete_backing_preserved"])
        self.assertTrue(history["archived_episode_retrieved"])
        self.assertEqual(len(backing), history["before_bytes"])
        self.assertEqual(len(current), history["current_bytes"])
        self.assertLess(len(current), len(backing))
        self.assertNotIn(b"Both consumers repaired together", current)
        self.assertIn(b"Both consumers repaired together", backing)
        index = self.output("current-index-after")["index"]
        query = self.cli("index", "query", "--index", index, "--term", "shared-display-name")
        self.assertTrue(query["complete_for_explicit_query"])
        self.assertTrue(any(item["source_role"] == "history" for item in query["items"]))

    def test_premature_formal_check_is_held_by_real_phase_cli(self) -> None:
        binding = json.loads((self.workflow / "build-binding.json").read_bytes())
        binding.update(action="FORMAL_CHECK", item_ids=[], check_ids=["label-check", "index-check"])
        proposal = self.root / "premature-formal-check.json"
        proposal.write_text(json.dumps(binding), encoding="utf-8")
        held = self.cli("work-phase", "--input", str(proposal), expected=2)
        self.assertFalse(held["admitted"])
        self.assertEqual(set(held["pending_implementation"]), {"label", "index"})
        self.assertIn("SOURCE_WIDE_IMPLEMENTATION_INCOMPLETE", held["holds"])
        self.assertFalse(held["permission_granted"])

    def test_catalog_hashes_the_current_helpers_and_cli_exposes_their_contracts(self) -> None:
        catalog = self.cli("catalog")
        required = {
            "chat-objective-continuity": ["scripts/ledger_input.py"],
            "master-guided-skill-lifecycle": ["scripts/allocation_io.py", "scripts/work_io.py",
                                             "scripts/work_phase.py", "scripts/capability_snapshot.py"],
        }
        for name, paths in required.items():
            entry = next(item for item in catalog["entries"] if item["name"] == name)
            members = {item["path"]: item["sha256"] for item in entry["files"]}
            for path in paths:
                self.assertEqual(members[path], digest(ROOT / "runtime" / "skills" / name / path))
        for command in ("objective-input", "allocation-io", "work", "work-phase", "capabilities"):
            result = subprocess.run([sys.executable, "-B", str(OIF), command, "--help"],
                                    cwd=ROOT, env=self.environment, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertIn(b"usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
