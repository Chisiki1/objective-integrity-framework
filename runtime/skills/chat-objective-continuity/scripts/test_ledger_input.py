"""Isolated normal-consumer tests; pass the exact runtime/hash and temp root."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import ledger_input


RUNTIME = None
RUNTIME_SHA = None
TEMP_ROOT = None


class LedgerInputTests(unittest.TestCase):
    def setUp(self):
        if RUNTIME is None or TEMP_ROOT is None:
            raise RuntimeError("run this test script with explicit --runtime, --runtime-sha256 and --temp-root")
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime_path = self.root / "runtime.py"
        self.runtime_path.write_bytes(Path(RUNTIME).read_bytes())
        self.runtime = ledger_input.load_runtime(self.runtime_path, RUNTIME_SHA)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"schema": self.runtime.CONFIG_SCHEMA,
            "namespace": "fixture-owner", "implicit_session_ledgers": True,
            "session_bindings": {}, "data_root": str(self.root / "data")}), encoding="utf-8")
        self.config = self.runtime.select_ledger_config(self.runtime.load_config(self.config_path), "fixture-chat")
        ingested = self.runtime.ingest_source(self.config, "fixture-chat", "fixture-source-1",
            b"Deliver two outcomes. Retain source meaning.\n", "bootstrap_source", "manual_bootstrap")
        sid = ingested["source_event_id"]; source = ingested["state"]["sources"][sid]
        self.source_path = self.runtime.ledger_dir(self.config) / source["source_ref"]
        self.runtime.append_event(self.config, "source_classified", "CLASSIFY-1", {
            "source_event_id": sid, "disposition": "INITIAL", "classification_note": "fixture owner review",
            "contract": {"contract_id": "fixture-contract", "primary_objective": "Deliver both outcomes",
                "clauses": [{"clause_id": "C1", "source_event_id": sid, "source_ref": source["source_ref"],
                             "source_sha256": source["source_sha256"], "locator": "whole fixture source"}],
                "open_outcomes": [{"outcome_id": "O1", "status": "OPEN", "description": "first outcome"},
                                  {"outcome_id": "O2", "status": "OPEN", "description": "second outcome"}]}},
            "root", ingested["state"]["head_hash"])

    def state(self):
        records, _ = self.runtime.scan_journal(self.config, repair_partial=False)
        return self.runtime.attach_runtime_frontiers(self.config, self.runtime.replay(self.config, records))

    def context(self, **overrides):
        args = dict(runtime_path=self.runtime_path, runtime_sha256=RUNTIME_SHA,
                    config_path=self.config_path, logical_chat_id="fixture-chat", expected_head=self.state()["head_hash"])
        args.update(overrides)
        return ledger_input.LedgerInputs(**args)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def apply(self, command, payload, event_id):
        return self.runtime.append_event(self.config, ledger_input.EVENT_TYPES[command], event_id,
                                        payload, "root", self.state()["head_hash"])

    def start(self, action_id="ACTION-1"):
        payload = self.context().action_start(action_id=action_id, description="bounded fixture work",
            outcome_ids=["O1"], source_clause_ids=["C1"], notes="no external authority")
        self.apply("action-start", payload, action_id + "-START")
        return payload

    def test_progress_delta_and_unchanged_real_runtime_consumer(self):
        self.apply("progress", {"contract_id": "fixture-contract", "outcome_updates": [
            {"outcome_id": "O1", "status": "OPEN", "evidence_refs": ["old-evidence"]}],
            "notes": "old note", "evidence_refs": ["old-top-level"]}, "OLD-PROGRESS")
        context = self.context(); before = self.snapshot()
        payload = context.progress(outcome_updates=[{"outcome_id": "O1", "status": "OPEN", "evidence_refs": ["new-evidence"]}],
                                   next_eligible_work="connect result to caller", evidence_refs=["new-top-level"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(["new-evidence"], payload["outcome_updates"][0]["evidence_refs"])
        self.assertNotIn("notes", payload)
        self.apply("progress", payload, "NEW-PROGRESS")
        state = self.state()
        self.assertEqual(["old-evidence", "new-evidence"], state["open_outcomes"]["O1"]["evidence_refs"])
        self.assertEqual("OPEN", state["open_outcomes"]["O2"]["status"])
        self.assertEqual(["new-top-level"], state["latest_progress"]["evidence_refs"])

    def test_action_start_preserves_explicit_values_and_input_only(self):
        before = self.snapshot()
        payload = self.context().action_start(action_id="ACTION-1", description="normal action",
            outcome_ids=["O1"], source_clause_ids=["C1"], action_input_sha256="A" * 64,
            proof_ceiling="input only", notes={"effect": "unobserved"})
        self.assertEqual(before, self.snapshot())
        self.assertEqual("fixture-contract", payload["contract_id"])
        self.assertEqual({"effect": "unobserved"}, payload["notes"])
        self.assertNotIn("ACTION-1", self.state()["actions"])
        self.apply("action-start", payload, "START-1")
        self.assertEqual("PENDING", self.state()["actions"]["ACTION-1"]["status"])

    def test_unknown_and_partial_effects_are_preserved(self):
        self.start()
        original = {"action_id": "ACTION-1", "status": "UNKNOWN_EFFECT", "effect_state": "UNKNOWN_EFFECT",
                    "first_fault": {"stage": "consumer result unavailable"}, "evidence_refs": ["raw-output"]}
        before = self.snapshot(); payload = self.context().action_outcome(**original)
        self.assertEqual(original, payload); self.assertEqual(before, self.snapshot())
        self.apply("action-outcome", payload, "UNKNOWN-1")
        self.assertEqual(["ACTION-1"], self.state()["unknown_effect_action_ids"])
        self.start("RETRY-1")
        partial = self.context().action_outcome(action_id="RETRY-1", status="FAILED", effect_state="EFFECT_CONFIRMED_PARTIAL")
        self.apply("action-outcome", partial, "PARTIAL-1")
        self.assertEqual(["ACTION-1"], self.state()["unknown_effect_action_ids"])

    def test_action_outcome_conservation_with_unclassified_and_tampered_source(self):
        self.start()
        self.runtime.ingest_source(self.config, "fixture-chat", "new-source", b"An owner update pending review.\n",
                                   "user_prompt_source", "manual_bootstrap")
        self.source_path.write_bytes(b"modified fixture source")
        context = self.context(); before = self.snapshot()
        payload = context.action_outcome(action_id="ACTION-1", status="CANCELLED", effect_state="UNKNOWN_EFFECT",
                                         first_fault="effect not observed", evidence_refs=["captured-attempt"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(context.observations["unclassified_source_ids"])
        self.assertTrue(context.observations["unproven_source_ids"])
        self.apply("action-outcome", payload, "CONSERVE-1")
        self.assertEqual(["ACTION-1"], self.state()["unknown_effect_action_ids"])

    def test_invalid_fields_ids_and_false_terminal_labels_rejected_without_writes(self):
        self.start(); context = self.context()
        attempts = [("progress", {"outcome_updates": [{"outcome_id": "NEW", "status": "OPEN"}]}),
                    ("progress", {"outcome_updates": [{"outcome_id": "O1", "status": "COMPLETED"}]}),
                    ("progress", {"outcome_updates": [{"outcome_id": "O1", "status": "OPEN", "description": "changed"}]}),
                    ("action-start", {"action_id": "NEW", "description": "work", "outcome_ids": ["O1"], "source_clause_ids": ["C1"], "scope_ref": "invented"}),
                    ("action-outcome", {"action_id": "ACTION-1", "status": "COMPLETED", "effect_state": "EFFECT_CONFIRMED_SUCCEEDED"}),
                    ("action-outcome", {"action_id": "ACTION-1", "status": "SUCCEEDED", "effect_state": "UNKNOWN_EFFECT"}),
                    ("action-outcome", {"action_id": "ACTION-1", "status": "FAILED", "effect_state": "EFFECT_CONFIRMED_FAILED", "proof_ceiling": "unsupported"})]
        for command, fields in attempts:
            with self.subTest(command=command, fields=fields):
                before = self.snapshot()
                with self.assertRaises((ValueError, self.runtime.LedgerError, context.runtime.LedgerError)):
                    context.build(command, fields)
                self.assertEqual(before, self.snapshot())

    def test_explicit_satisfaction_allowed_but_reopen_is_runtime_rejected(self):
        self.apply("progress", self.context().progress(outcome_updates=[{"outcome_id": "O1", "status": "SATISFIED", "evidence_refs": ["owner-result"]}]), "SATISFY")
        context = self.context(); before = self.snapshot()
        with self.assertRaises(context.runtime.LedgerError):
            context.progress(outcome_updates=[{"outcome_id": "O1", "status": "OPEN"}])
        self.assertEqual(before, self.snapshot())

    def test_source_tamper_holds_dependent_progress_and_start(self):
        self.source_path.write_bytes(b"modified")
        context = self.context(); before = self.snapshot()
        for command, fields in [("progress", {}), ("action-start", {"action_id": "A", "description": "work", "outcome_ids": ["O1"], "source_clause_ids": ["C1"]})]:
            with self.subTest(command=command), self.assertRaises(context.runtime.LedgerError):
                context.build(command, fields)
        self.assertEqual(before, self.snapshot())

    def test_stale_head_and_cross_chat_do_not_create_or_modify_ledgers(self):
        context = self.context(); self.apply("progress", {"contract_id": "fixture-contract"}, "OTHER-PROGRESS")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "stale expected_head"):
            context.progress(notes="stale")
        with self.assertRaises(FileNotFoundError):
            self.context(logical_chat_id="other-chat")
        self.assertEqual(before, self.snapshot())

    def test_identity_file_mismatch_and_wrong_contract_rejected(self):
        context = self.context()
        with self.assertRaises(context.runtime.LedgerError):
            context.progress(contract_id="other-contract")
        identity = context.ledger_root / "identity.json"
        data = json.loads(identity.read_bytes()); data["logical_chat_id"] = "other-chat"
        identity.write_text(json.dumps(data), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "identity"):
            context.progress()
        self.assertEqual(before, self.snapshot())

    def test_runtime_and_config_changes_never_silently_refresh(self):
        context = self.context()
        self.runtime_path.write_bytes(self.runtime_path.read_bytes() + b"\n# fixture change\n")
        with self.assertRaisesRegex(ValueError, "runtime changed"):
            context.progress()
        with self.assertRaisesRegex(ValueError, "runtime hash"):
            self.context()
        self.runtime_path.write_bytes(Path(RUNTIME).read_bytes())
        context = self.context(); self.config_path.write_bytes(self.config_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "config changed"):
            context.progress()

    def test_partial_journal_is_not_repaired(self):
        journal = self.runtime.journal_path(self.config)
        journal.write_bytes(journal.read_bytes() + b'{"partial":')
        before = self.snapshot()
        with self.assertRaises(Exception):
            ledger_input.LedgerInputs(runtime_path=self.runtime_path, runtime_sha256=RUNTIME_SHA,
                config_path=self.config_path, logical_chat_id="fixture-chat", expected_head="A" * 64)
        self.assertEqual(before, self.snapshot())

    def test_output_is_new_explicit_input_not_ledger_mutation(self):
        context = self.context(); payload = context.progress(evidence_refs=["new-evidence"])
        with self.assertRaisesRegex(ValueError, "unchanged last validated input"):
            context.write_input(self.root / "unvalidated.json", {**payload, "proof_ceiling": "invented"})
        self.assertFalse((self.root / "unvalidated.json").exists())
        path = self.root / "prepared.json"; context.write_input(path, payload)
        self.assertEqual(payload, json.loads(path.read_bytes()))
        with self.assertRaises(FileExistsError): context.write_input(path, payload)
        with self.assertRaisesRegex(ValueError, "inside the ledger"):
            context.write_input(context.ledger_root / "new-input.json", payload)

    def test_cli_output_is_directly_accepted_by_unchanged_runtime_cli(self):
        context = self.context(); before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", str(Path(ledger_input.__file__)), "progress",
            "--runtime", str(self.runtime_path), "--runtime-sha256", RUNTIME_SHA,
            "--config", str(self.config_path), "--logical-chat-id", "fixture-chat",
            "--expected-head", context.expected_head], input=json.dumps({"notes": "owner normal result", "evidence_refs": ["E1"]}).encode(), capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.snapshot())
        applied = subprocess.run([sys.executable, "-B", str(self.runtime_path), "progress", "--config", str(self.config_path),
            "--logical-chat-id", "fixture-chat", "--expected-head", context.expected_head,
            "--input", "-", "--event-id", "CLI-CONSUMER"], input=result.stdout, capture_output=True)
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertEqual("owner normal result", self.state()["latest_progress"]["notes"])


    def test_response_check_returns_disposition_dict(self):
        # Regression: the CLI prints json.dumps(response_check(...)); a dropped
        # return would silently emit "null" (audit finding 0.2.0-high-1).
        binding_dir = self.root / "bindings"; binding_dir.mkdir()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["host_binding"] = {"session_env": "FIXTURE_SESSION", "bindings_dir": "bindings"}
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (binding_dir / (hashlib.sha256(b"fixture-chat").hexdigest() + ".json")).write_text(
            json.dumps({"role": "root", "session_id": "fixture-chat"}), encoding="utf-8")
        os.environ["FIXTURE_SESSION"] = "fixture-chat"
        self.addCleanup(os.environ.pop, "FIXTURE_SESSION", None)
        result = self.context().response_check(request_outcome_ids=["O1", "O2"], excluded_active={},
            source_clause_ids=["C1"], purpose="completion",
            next_action={"eligible": False, "description": "fixture: no further eligible work",
                         "evidence_refs": ["fixture evidence"]})
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("schema"), "ledger-response-disposition-v1")
        self.assertIn("decision", result)
        self.assertIn("final_allowed", result)
        self.assertEqual(result.get("request_outcome_ids"), ["O1", "O2"])
        self.assertFalse(result.get("permission_granted"))
        self.assertNotEqual(json.dumps(result, ensure_ascii=True), "null")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--temp-root", required=True)
    args, rest = parser.parse_known_args()
    RUNTIME = Path(args.runtime).resolve(strict=True); RUNTIME_SHA = args.runtime_sha256
    TEMP_ROOT = Path(args.temp_root).resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *rest])
