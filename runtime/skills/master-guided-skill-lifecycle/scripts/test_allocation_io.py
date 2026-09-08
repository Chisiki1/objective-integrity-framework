"""Behavioral regression coverage for typed input and fresh allocation consumption."""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import allocation_io as io


def state(**updates):
    values = dict(selected_model="model-a", selected_reasoning="high", injectable=False,
                  requested_model="model-a", requested_reasoning="high", accepted="unavailable",
                  fallback="retain observed context; effective fields unavailable")
    values.update(updates)
    return io.build_orchestrator_state(**values)


def fixture():
    return {
        "schema_version": "stage-allocation-v2", "objective_id": "fixture-objective",
        "source_sha256": "A" * 64, "stage_id": "fixture-stage", "job_id": "fixture-job",
        "job_shape_sha256": "B" * 64, "trigger": "JOB_SHAPE_CHANGED",
        "current_lease_id": "fixture-lease",
        "owner": {"lane": "COORDINATED-WORK", "task_id": "fixture-task", "lease_id": "fixture-lease"},
        "context_reuse": {"benefit_positive": True, "evidence": "fixture retained context",
                          "replacement_cost": "unavailable", "current_configuration_id": "config-a"},
        "selection_authority": {"mode": "measured-comparison", "source_clause": "fixture-source",
                                "configuration_id": None, "evidence": "fixture comparison"},
        "candidates": [{"configuration_id": "config-a",
                        "capability_needs": [{"need": "fixture capability", "met": True, "evidence": "fixture witness"}],
                        "representative_eval_version": "fixture-v1",
                        "risk_adjusted_total_cost": {"status": "estimated", "value": 20, "evidence": "fixture estimate"},
                        "lower_configuration_failure_prediction": "not assessed",
                        "orchestrator_state": state()}],
        "proof_ceiling": "isolated fixture, no model quality claim",
    }


class AllocationIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input.json"
        self.evidence = self.root / "evidence.jsonl"
        self.data = fixture()

    def write(self):
        self.source.write_bytes(io._canonical(self.data))

    def run_input(self):
        self.write()
        return io.run_allocation(self.source, self.evidence)

    def records(self):
        return [json.loads(line) for line in self.evidence.read_text().splitlines()]

    def test_selected_can_reuse_context(self):
        result = self.run_input()
        self.assertTrue(result["selection_ready"])
        self.assertEqual("SELECTED", result["decision"])
        self.assertTrue(result["reuse_current_conversation"])
        self.assertTrue(result["selected_matches_current_configuration"])
        self.assertFalse(result["permission_granted"])
        self.assertFalse(result["dispatch_performed"])
        raw, consumed = self.records()
        self.assertEqual("raw_process", raw["record_type"])
        self.assertEqual("consumption", consumed["record_type"])
        self.assertEqual(0, raw["returncode"])
        self.assertIsNone(result["selected_orchestrator_state"]["effective_model"])

    def test_retain_current_unknown_cost(self):
        self.data["candidates"][0]["risk_adjusted_total_cost"] = {"status": "unavailable", "reason": "not measured"}
        result = self.run_input()
        self.assertTrue(result["selection_ready"])
        self.assertEqual("RETAIN_CURRENT", result["decision"])

    def test_source_selected(self):
        self.data["selection_authority"].update(mode="exact-source-selection", configuration_id="config-a")
        self.data["candidates"][0]["risk_adjusted_total_cost"] = {"status": "source_selected", "reason": "fixture exact source"}
        result = self.run_input()
        self.assertTrue(result["selection_ready"])
        self.assertEqual("SOURCE_SELECTED", result["decision"])

    def test_configuration_change_does_not_mean_discard_context(self):
        self.data["candidates"][0]["configuration_id"] = "config-b"
        self.data["candidates"][0]["orchestrator_state"] = state(selected_model="model-b", requested_model="model-b")
        result = self.run_input()
        self.assertTrue(result["selection_ready"])
        self.assertTrue(result["reuse_current_conversation"])
        self.assertFalse(result["selected_matches_current_configuration"])
        self.assertEqual("model-b", result["selected_orchestrator_state"]["selected_model"])

    def test_held_is_not_success_or_permission(self):
        self.data["context_reuse"]["benefit_positive"] = False
        self.data["candidates"][0]["risk_adjusted_total_cost"] = {"status": "unavailable", "reason": "not measured"}
        result = self.run_input()
        self.assertFalse(result["selection_ready"])
        self.assertFalse(result["permission_granted"])
        self.assertEqual(3, result["returncode"])
        raw = self.records()[0]
        original = json.loads(base64.b64decode(raw["stdout_base64"]))
        self.assertEqual("USER-DECISION", original["decision"])

    def test_no_capability_is_not_success(self):
        self.data["candidates"][0]["capability_needs"][0]["met"] = False
        self.assertFalse(self.run_input()["selection_ready"])

    def test_invalid_enum_preserves_allocator_first_fault(self):
        self.data["candidates"][0]["orchestrator_state"]["accepted"] = "not-yet-submitted"
        result = self.run_input()
        self.assertFalse(result["selection_ready"])
        raw = self.records()[0]
        self.assertEqual(2, raw["returncode"])
        original = json.loads(base64.b64decode(raw["stderr_base64"]))
        self.assertEqual("orchestrator_state injectable/accepted is invalid", original["error"])

    def test_builder_rejects_enum_aliases_and_non_boolean(self):
        for accepted in ("not-yet-submitted", "Accepted", True, None):
            with self.subTest(accepted=accepted), self.assertRaises(ValueError):
                state(accepted=accepted)
        for injectable in ("false", "true", 0, 1, None):
            with self.subTest(injectable=injectable), self.assertRaises(ValueError):
                state(injectable=injectable)

    def test_builder_preserves_explicit_differences_and_unknowns(self):
        result = state(selected_model="model-b", requested_model="model-a")
        self.assertEqual("model-b", result["selected_model"])
        self.assertEqual("model-a", result["requested_model"])
        self.assertIsNone(result["effective_model"])
        self.assertIsNone(result["effective_reasoning"])

    def test_expected_current_identity_prevents_stale_input_launch(self):
        self.write()
        expected = {field: copy.deepcopy(self.data[field]) for field in io.IDENTITY_FIELDS}
        expected["job_id"] = "new-job"
        with patch.object(io.subprocess, "Popen") as launch:
            result = io.run_allocation(self.source, self.evidence, expected_identity=expected)
        launch.assert_not_called()
        self.assertFalse(result["selection_ready"])
        self.assertEqual("NOT-STARTED", result["process_effect_status"])

    def test_result_tamper_and_rehashed_misbinding(self):
        self.run_input()
        raw = base64.b64decode(self.records()[0]["stdout_base64"])
        original = json.loads(raw)
        changes = ({"job_id": "stale-job"}, {"source_sha256": "C" * 64},
                   {"input_sha256": "D" * 64}, {"selected_configuration_id": "other"},
                   {"selected_orchestrator_state": state(selected_model="wrong")},
                   {"reuse_current_conversation": False}, {"decision": "HELD"})
        for delta in changes:
            with self.subTest(delta=delta):
                changed = {**original, **delta}
                with self.assertRaises(ValueError):
                    io._consume_result(io._canonical(changed), self.data, io._sha(self.source.read_bytes()))
                changed.pop("result_sha256")
                changed["result_sha256"] = io._sha(io._canonical(changed))
                with self.assertRaises(ValueError):
                    io._consume_result(io._canonical(changed), self.data, io._sha(self.source.read_bytes()))

    def test_duplicate_candidate_identity_rejected(self):
        self.data["candidates"].append(copy.deepcopy(self.data["candidates"][0]))
        self.assertFalse(self.run_input()["selection_ready"])

    def test_rehashed_but_more_expensive_selection_is_rejected(self):
        other = copy.deepcopy(self.data["candidates"][0])
        other["configuration_id"] = "config-b"
        other["risk_adjusted_total_cost"]["value"] = 30
        self.data["candidates"].append(other)
        self.run_input()
        result = json.loads(base64.b64decode(self.records()[0]["stdout_base64"]))
        result["selected_configuration_id"] = "config-b"
        result.pop("result_sha256")
        result["result_sha256"] = io._sha(io._canonical(result))
        with self.assertRaisesRegex(ValueError, "deterministic v2 selection"):
            io._consume_result(io._canonical(result), self.data, io._sha(self.source.read_bytes()))

    def test_projection_matches_real_allocator_across_cost_and_context_branches(self):
        cases = [
            ("observed", 10, "estimated", 20, False, "config-b", "SELECTED"),
            ("estimated", 10, "observed", 10, True, "config-b", "RETAIN_CURRENT"),
            ("observed", 10, "observed", 10, False, "config-b", "USER-DECISION"),
            ("observed", 10, "unavailable", None, True, "config-a", "RETAIN_CURRENT"),
            ("observed", 10, "unavailable", None, False, "config-a", "USER-DECISION"),
            ("unavailable", None, "unavailable", None, True, "missing", "USER-DECISION"),
        ]
        for index, (s1, n1, s2, n2, reuse, current, decision) in enumerate(cases):
            with self.subTest(index=index):
                self.data = fixture()
                other = copy.deepcopy(self.data["candidates"][0])
                other["configuration_id"] = "config-b"
                self.data["candidates"].append(other)
                for item, status, value in zip(self.data["candidates"], (s1, s2), (n1, n2)):
                    item["risk_adjusted_total_cost"] = (
                        {"status": status, "reason": "fixture unknown"} if status == "unavailable"
                        else {"status": status, "value": value, "evidence": "fixture estimate"})
                self.data["context_reuse"].update(benefit_positive=reuse, current_configuration_id=current)
                self.evidence = self.root / f"branch-{index}.jsonl"
                normalized = self.run_input()
                actual = json.loads(base64.b64decode(self.records()[0]["stdout_base64"]))
                expected = io._selection_projection(self.data)
                self.assertEqual(decision, actual["decision"])
                self.assertTrue(all(actual[key] == value for key, value in expected.items()))
                self.assertEqual(decision in io.SUCCESS_DECISIONS, normalized["selection_ready"])

    def test_input_mutation_during_run_is_unconsumed(self):
        real_popen = subprocess.Popen
        source = self.source
        class Mutator:
            def __init__(self, *args, **kwargs):
                self.process = real_popen(*args, **kwargs)
            def communicate(self):
                out = self.process.communicate()
                source.write_bytes(source.read_bytes() + b" ")
                self.returncode = self.process.returncode
                return out
        with patch.object(io.subprocess, "Popen", Mutator):
            result = self.run_input()
        self.assertFalse(result["selection_ready"])
        self.assertIn("changed during execution", result["error"])
        self.assertTrue(self.records()[0]["stdout_base64"])

    def test_allocator_mutation_during_run_is_unconsumed(self):
        allocator = self.root / "stage_allocation.py"
        allocator.write_bytes(io.ALLOCATOR.read_bytes())
        real_popen = subprocess.Popen
        class Mutator:
            def __init__(self, *args, **kwargs):
                self.process = real_popen(*args, **kwargs)
            def communicate(self):
                out = self.process.communicate()
                allocator.write_bytes(allocator.read_bytes() + b"\n# changed\n")
                self.returncode = self.process.returncode
                return out
        with patch.object(io, "ALLOCATOR", allocator), patch.object(io.subprocess, "Popen", Mutator):
            result = self.run_input()
        self.assertFalse(result["selection_ready"])
        self.assertIn("changed during execution", result["error"])

    def test_existing_evidence_and_aliases_never_launch_or_overwrite(self):
        self.write()
        self.evidence.write_bytes(b"original evidence")
        for destination in (self.evidence, self.source, io.ALLOCATOR):
            with self.subTest(destination=destination), patch.object(io.subprocess, "Popen") as launch:
                with self.assertRaises(FileExistsError):
                    io.run_allocation(self.source, destination)
                launch.assert_not_called()
        self.assertEqual(b"original evidence", self.evidence.read_bytes())

    def test_reparse_parent_is_rejected(self):
        self.write()
        link = self.root / "linked"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"host cannot create symlink: {error}")
        with patch.object(io.subprocess, "Popen") as launch:
            with self.assertRaises(ValueError):
                io.run_allocation(self.source, link / "new.jsonl")
            launch.assert_not_called()

    def test_windows_reparse_attribute_rejected_before_launch(self):
        self.write()
        original = Path.lstat
        root = self.root
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == root:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", reparse), patch.object(io.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "reparse"):
                io.run_allocation(self.source, self.evidence)
            launch.assert_not_called()
        self.assertFalse(self.evidence.exists())

    def test_spawn_failure_remains_raw_and_not_started(self):
        with patch.object(io.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            result = self.run_input()
        self.assertFalse(result["selection_ready"])
        self.assertEqual("NOT-STARTED", result["process_effect_status"])
        self.assertIn("fixture spawn failure", self.records()[0]["error"])

    def test_raw_evidence_is_durable_before_result_interpretation(self):
        consume = io._consume_result
        def checked(*args):
            records = self.records()
            self.assertEqual(1, len(records))
            self.assertEqual("raw_process", records[0]["record_type"])
            return consume(*args)
        with patch.object(io, "_consume_result", checked):
            self.assertTrue(self.run_input()["selection_ready"])

    def test_cli_state_and_run(self):
        command = [sys.executable, "-B", str(Path(io.__file__)), "state", "--selected-model", "model-a",
                   "--selected-reasoning", "high", "--requested-model", "model-a", "--requested-reasoning", "high",
                   "--injectable", "false", "--accepted", "unavailable", "--fallback", "unavailable"]
        completed = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(json.loads(completed.stdout)["injectable"])
        self.write()
        completed = subprocess.run([sys.executable, "-B", str(Path(io.__file__)), "run", "--input", str(self.source),
                                    "--evidence", str(self.evidence)], capture_output=True, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["selection_ready"])


if __name__ == "__main__":
    unittest.main()
