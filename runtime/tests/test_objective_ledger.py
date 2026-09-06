from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "objective_ledger.py"
ROOT_SESSION = "synthetic-root-session"


class LedgerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.data = self.root / "data"
        self.config_path = self.root / "config.json"
        self.config = {
            "schema": "chat-objective-continuity-config-v1",
            "namespace": "test-user",
            "session_bindings": {
                ROOT_SESSION: {"role": "root", "logical_chat_id": "logical-chat-1"},
                "child-1": {"role": "subagent"},
            },
            "implicit_session_ledgers": True,
            "data_root": str(self.data),
            "lock_timeout_seconds": 5,
            "max_hook_input_bytes": 1048576,
            "max_prompt_bytes": 262144,
            "tool_classes": {"read": "read_only", "write": "mutating", "shell": "mixed"},
        }
        self.write_json(self.config_path, self.config)
        self.source_file = self.root / "source.txt"
        self.source_file.write_text("Do the primary outcome without substituting tests.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def write_json(path: pathlib.Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def cli(self, *args: str, stdin: dict | None = None, expected: int = 0) -> dict:
        command = [
            sys.executable,
            "-B",
            str(SCRIPT),
            *args,
            "--config",
            str(self.config_path),
            "--logical-chat-id",
            "logical-chat-1",
        ]
        result = subprocess.run(
            command,
            input=json.dumps(stdin, ensure_ascii=False).encode("utf-8") if stdin is not None else None,
            capture_output=True,
            check=False,
        )
        stdout = result.stdout.decode("ascii")
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, expected, stdout + stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else {}

    def bootstrap(self) -> dict:
        return self.cli(
            "bootstrap",
            "--session-id",
            ROOT_SESSION,
            "--source-file",
            str(self.source_file),
            "--delivery-id",
            "initial",
        )

    def current_state(self) -> dict:
        return self.cli("status")["state"]

    @staticmethod
    def load_runtime():
        spec = importlib.util.spec_from_file_location("candidate_chat_objective_ledger", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def classify_initial(self, source_event_id: str) -> dict:
        state = self.current_state()
        source = state["sources"][source_event_id]
        payload = {
            "source_event_id": source_event_id,
            "disposition": "INITIAL",
            "classification_note": "test root classification",
            "contract": {
                "contract_id": "contract-v1",
                "primary_objective": "Deliver the primary outcome.",
                "mandatory_acceptance": ["observable outcome is delivered"],
                "scope": {"included": ["candidate runtime"], "excluded": ["live product"]},
                "authority": {"semantic_writer": "root source review"},
                "constraints": ["same chat"],
                "prohibited_substitutes": ["tests without the outcome"],
                "clauses": [
                    {
                        "clause_id": "C1",
                        "source_event_id": source_event_id,
                        "source_ref": source["source_ref"],
                        "source_sha256": source["source_sha256"],
                        "locator": "line 1",
                    }
                ],
                "open_outcomes": [{"outcome_id": "O1", "status": "OPEN", "description": "observable outcome"}],
            },
        }
        path = self.root / "classify.json"
        self.write_json(path, payload)
        return self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-1", "--expected-head", state["head_hash"])

    def test_create_if_absent_and_identical_bootstrap_is_idempotent(self) -> None:
        first = self.bootstrap()
        second = self.bootstrap()
        self.assertEqual(first["source_event_id"], second["source_event_id"])
        self.assertEqual(second["status"], "duplicate")
        state = self.current_state()
        self.assertEqual(state["revision"], 1)
        projection = pathlib.Path(self.cli("status")["projection"])
        text = projection.read_text(encoding="utf-8")
        self.assertIn(first["source_event_id"], text)
        self.assertIn("UNCLASSIFIED", text)

    def test_same_prompt_hook_duplicate_is_idempotent_and_never_classifies(self) -> None:
        event = {"session_id": ROOT_SESSION, "turn_id": "turn-1", "prompt": "new user source"}
        first = self.cli("hook", "--event", "UserPromptSubmit", stdin=event)
        second = self.cli("hook", "--event", "UserPromptSubmit", stdin=event)
        self.assertEqual(first["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(second["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        state = self.current_state()
        self.assertIsNone(state["current_contract_id"])
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(state["unclassified_source_ids"]), 1)
        source = state["sources"][state["unclassified_source_ids"][0]]
        self.assertEqual(source["capture_route"], "HOST_USER_PROMPT_SESSION_BOUND_UNVERIFIED_ACTOR")

    def test_same_trusted_delivery_id_with_different_content_is_collision_not_new_authority(self) -> None:
        first = {"session_id": ROOT_SESSION, "turn_id": "trusted-turn", "prompt": "first bytes"}
        second = {**first, "prompt": "different bytes"}
        self.cli("hook", "--event", "UserPromptSubmit", stdin=first)
        warning = self.cli("hook", "--event", "UserPromptSubmit", stdin=second)
        self.assertTrue(warning["continue"])
        self.assertIn("systemMessage", warning)
        state = self.current_state()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(state["unclassified_source_ids"]), 1)

    def test_equal_prompts_without_trusted_delivery_id_are_distinct_occurrences(self) -> None:
        event = {"session_id": ROOT_SESSION, "prompt": "same content, two deliveries"}
        self.cli("hook", "--event", "UserPromptSubmit", stdin=event)
        self.cli("hook", "--event", "UserPromptSubmit", stdin=event)
        state = self.current_state()
        self.assertEqual(state["revision"], 2)
        self.assertEqual(len(state["unclassified_source_ids"]), 2)

    def test_new_prompt_holds_only_configured_mutation_until_classified(self) -> None:
        boot = self.bootstrap()
        read = self.cli("preflight", "--tool-name", "read")
        write = self.cli("preflight", "--tool-name", "write")
        mixed = self.cli("preflight", "--tool-name", "shell")
        self.assertEqual(read["decision"], "allow")
        self.assertEqual(write["decision"], "hold")
        self.assertEqual(mixed["decision"], "allow")
        self.assertIn("not claimed protected", mixed["proof_ceiling"])
        self.classify_initial(boot["source_event_id"])
        self.assertEqual(self.cli("preflight", "--tool-name", "write")["decision"], "allow")

    def test_progress_cannot_change_objective_semantics(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        bad = self.root / "bad-progress.json"
        self.write_json(bad, {"primary_objective": "silently changed", "outcome_updates": []})
        result = self.cli("progress", "--input", str(bad), "--event-id", "PROGRESS-BAD", expected=2)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(self.current_state()["contracts"]["contract-v1"]["primary_objective"], "Deliver the primary outcome.")

    def test_rejected_transition_leaves_journal_and_head_unchanged_with_evidence(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        journal = next(self.data.rglob("journal.jsonl"))
        before_bytes = journal.read_bytes()
        before_head = self.current_state()["head_hash"]
        result = self.cli(
            "progress",
            "--input",
            "-",
            "--event-id",
            "REJECTED-PROGRESS",
            stdin={"primary_objective": "poison", "outcome_updates": []},
            expected=2,
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(journal.read_bytes(), before_bytes)
        self.assertEqual(self.current_state()["head_hash"], before_head)
        rejections = list(self.data.rglob("rejections/*.json"))
        self.assertEqual(len(rejections), 1)
        evidence = json.loads(rejections[0].read_text(encoding="ascii"))
        self.assertFalse(evidence["semantic_journal_changed"])
        self.assertEqual(evidence["observed_head"], before_head)

    def test_progress_updates_only_existing_open_outcome(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        good = self.root / "progress.json"
        self.write_json(
            good,
            {
                "contract_id": "contract-v1",
                "outcome_updates": [{"outcome_id": "O1", "status": "SATISFIED", "evidence_refs": ["local:test"]}],
                "next_eligible_work": "return to source outcome",
                "proof_ceiling": "local test",
            },
        )
        self.cli("progress", "--input", str(good), "--event-id", "PROGRESS-1")
        self.assertEqual(self.current_state()["open_outcomes"]["O1"]["status"], "SATISFIED")

    def test_add_preserves_progressed_status_and_evidence_from_current_state(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("progress", "--input", "-", "--event-id", "PROGRESS-BEFORE-ADD", stdin={"contract_id": "contract-v1", "outcome_updates": [{"outcome_id": "O1", "status": "SATISFIED", "evidence_refs": ["proof:add"]}]})
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "add-preserve-turn", "prompt": "add one outcome"})
        state = self.current_state()
        source_id = state["unclassified_source_ids"][0]
        source = state["sources"][source_id]
        payload = {
            "source_event_id": source_id,
            "disposition": "ADD",
            "contract": {
                "contract_id": "contract-v2",
                "parent_contract_id": "contract-v1",
                "primary_objective": "Deliver the primary outcome.",
                "clauses": [state["contracts"]["contract-v1"]["clauses"][0], {"clause_id": "C2", "source_event_id": source_id, "source_ref": source["source_ref"], "source_sha256": source["source_sha256"]}],
                "open_outcomes": [
                    {"outcome_id": "O1", "status": "OPEN", "description": "observable outcome"},
                    {"outcome_id": "O2", "status": "OPEN", "description": "added outcome"},
                ],
            },
        }
        self.cli("classify-source", "--input", "-", "--event-id", "CLASSIFY-ADD-PRESERVE", stdin=payload)
        current = self.current_state()["open_outcomes"]
        self.assertEqual(current["O1"]["status"], "SATISFIED")
        self.assertEqual(current["O1"]["evidence_refs"], ["proof:add"])
        contract = self.current_state()["contracts"]["contract-v2"]
        self.assertEqual(contract["mandatory_acceptance"], ["observable outcome is delivered"])
        self.assertEqual(contract["scope"]["excluded"], ["live product"])
        self.assertEqual(contract["authority"], {"semantic_writer": "root source review"})
        self.assertEqual(contract["constraints"], ["same chat"])
        self.assertEqual(contract["prohibited_substitutes"], ["tests without the outcome"])

    def test_correct_uses_new_id_and_reopen_uses_explicit_source_lineage(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli(
            "progress",
            "--input",
            "-",
            "--event-id",
            "PROGRESS-SAT",
            stdin={"contract_id": "contract-v1", "outcome_updates": [{"outcome_id": "O1", "status": "SATISFIED", "evidence_refs": ["proof:one"]}]},
        )
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "correct-turn", "prompt": "correct the outcome faithfully"})
        state = self.current_state()
        source_id = state["unclassified_source_ids"][0]
        source = state["sources"][source_id]
        parent_clause = state["contracts"]["contract-v1"]["clauses"][0]
        correction = {
            "source_event_id": source_id,
            "disposition": "CORRECT",
            "normative_transitions": [
                {"field": "primary_objective", "operation": "CORRECT", "source_clause_ids": ["C2"]},
                {"field": "constraints", "operation": "CORRECT", "source_clause_ids": ["C2"]},
            ],
            "contract": {
                "contract_id": "contract-v2",
                "parent_contract_id": "contract-v1",
                "primary_objective": "Deliver the primary outcome faithfully.",
                "constraints": ["same chat", "faithful correction"],
                "clauses": [
                    parent_clause,
                    {"clause_id": "C2", "source_event_id": source_id, "source_ref": source["source_ref"], "source_sha256": source["source_sha256"]},
                ],
                "open_outcomes": [
                    {"outcome_id": "O1", "status": "OPEN", "description": "observable outcome"},
                    {"outcome_id": "O2", "status": "OPEN", "description": "faithfully corrected observable outcome", "corrects_outcome_id": "O1"},
                ],
                "outcome_transitions": [
                    {"outcome_id": "O1", "operation": "SUPERSEDE", "from_status": "SATISFIED", "source_clause_ids": ["C2"]}
                ],
            },
        }
        self.cli("classify-source", "--input", "-", "--event-id", "CLASSIFY-CORRECT", stdin=correction)
        current = self.current_state()["open_outcomes"]
        self.assertEqual(current["O1"]["status"], "SUPERSEDED")
        self.assertEqual(current["O1"]["evidence_refs"], ["proof:one"])
        self.assertEqual(current["O2"]["corrects_outcome_id"], "O1")
        self.assertEqual(self.current_state()["contracts"]["contract-v2"]["constraints"], ["same chat", "faithful correction"])
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "reopen-turn", "prompt": "reopen the prior outcome"})
        state = self.current_state()
        reopen_source_id = state["unclassified_source_ids"][0]
        reopen_source = state["sources"][reopen_source_id]
        reopened = {
            "source_event_id": reopen_source_id,
            "disposition": "ADD",
            "contract": {
                "contract_id": "contract-v3",
                "parent_contract_id": "contract-v2",
                "primary_objective": "Deliver the primary outcome faithfully.",
                "clauses": [
                    *state["contracts"]["contract-v2"]["clauses"],
                    {"clause_id": "C3", "source_event_id": reopen_source_id, "source_ref": reopen_source["source_ref"], "source_sha256": reopen_source["source_sha256"]},
                ],
                "open_outcomes": [
                    {"outcome_id": "O1", "status": "SUPERSEDED", "description": "observable outcome"},
                    {"outcome_id": "O2", "status": "OPEN", "description": "faithfully corrected observable outcome", "corrects_outcome_id": "O1"},
                ],
                "outcome_transitions": [{"outcome_id": "O1", "operation": "REOPEN", "from_status": "SUPERSEDED", "source_clause_ids": ["C3"]}],
            },
        }
        self.cli("classify-source", "--input", "-", "--event-id", "CLASSIFY-REOPEN", stdin=reopened)
        reopened_state = self.current_state()["open_outcomes"]
        self.assertEqual(reopened_state["O1"]["status"], "OPEN")
        self.assertEqual(reopened_state["O1"]["evidence_refs"], ["proof:one"])

    def test_explicit_withdraw_requires_source_and_closes_current_contract(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("progress", "--input", "-", "--event-id", "PROGRESS-BEFORE-WITHDRAW", stdin={"contract_id": "contract-v1", "outcome_updates": [], "next_eligible_work": "must disappear on withdrawal"})
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "withdraw-turn", "prompt": "withdraw the objective"})
        state = self.current_state()
        source_id = state["unclassified_source_ids"][0]
        source = state["sources"][source_id]
        payload = {
            "source_event_id": source_id,
            "disposition": "WITHDRAW",
            "contract": {
                "contract_id": "contract-withdrawn-v2",
                "withdraws_contract_id": "contract-v1",
                "clauses": [{"clause_id": "CW", "source_event_id": source_id, "source_ref": source["source_ref"], "source_sha256": source["source_sha256"]}],
                "open_outcomes": [],
            },
        }
        self.cli("classify-source", "--input", "-", "--event-id", "CLASSIFY-WITHDRAW", stdin=payload)
        current = self.current_state()
        self.assertIsNone(current["current_contract_id"])
        self.assertEqual(current["open_outcomes"], {})
        self.assertIsNone(current["latest_progress"])
        projection = next(self.data.rglob("objective.txt")).read_text(encoding="utf-8")
        self.assertNotIn("must disappear on withdrawal", projection)
        self.assertIn("NONE (NO ACTIVE CONTRACT)", projection)

    def test_replace_and_withdraw_require_exact_lineage(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("progress", "--input", "-", "--event-id", "PROGRESS-BEFORE-REPLACE", stdin={"contract_id": "contract-v1", "outcome_updates": [], "next_eligible_work": "stale old-contract advice"})
        self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": ROOT_SESSION, "turn_id": "replace-turn", "prompt": "replace the objective"},
        )
        state = self.current_state()
        replacement_source_id = state["unclassified_source_ids"][0]
        source = state["sources"][replacement_source_id]
        bad = {
            "source_event_id": replacement_source_id,
            "disposition": "REPLACE",
            "contract": {
                "contract_id": "contract-v2",
                "replaces_contract_id": "wrong",
                "primary_objective": "replacement",
                "clauses": [{"clause_id": "C2", "source_event_id": replacement_source_id, "source_ref": source["source_ref"], "source_sha256": source["source_sha256"]}],
                "open_outcomes": [],
            },
        }
        path = self.root / "replace.json"
        self.write_json(path, bad)
        result = self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-2", expected=2)
        self.assertEqual(result["status"], "ERROR")
        bad["contract"]["replaces_contract_id"] = "contract-v1"
        self.write_json(path, bad)
        self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-2")
        replaced = self.current_state()
        self.assertEqual(replaced["current_contract_id"], "contract-v2")
        self.assertIsNone(replaced["latest_progress"])
        self.assertNotIn("scope", replaced["contracts"]["contract-v2"])
        projection = next(self.data.rglob("objective.txt")).read_text(encoding="utf-8")
        self.assertNotIn("stale old-contract advice", projection)

    def test_compare_and_swap_rejects_stale_writer(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        stale = self.current_state()["head_hash"]
        progress = self.root / "progress.json"
        self.write_json(progress, {"contract_id": "contract-v1", "outcome_updates": [], "next_eligible_work": "x"})
        self.cli("progress", "--input", str(progress), "--event-id", "PROGRESS-A", "--expected-head", stale)
        result = self.cli("progress", "--input", str(progress), "--event-id", "PROGRESS-B", "--expected-head", stale, expected=3)
        self.assertEqual(result["status"], "CAS_MISMATCH")

    def test_partial_last_line_is_preserved_and_repaired(self) -> None:
        self.bootstrap()
        journal = next(self.data.rglob("journal.jsonl"))
        with journal.open("ab") as stream:
            stream.write(b'{"partial":')
        result = self.cli("verify")
        self.assertEqual(result["status"], "STRUCTURE_PASS")
        recovery = list(self.data.rglob("partial-*.bin"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), b'{"partial":')
        self.assertTrue(journal.read_bytes().endswith(b"\n"))

    def test_hash_bound_dead_lock_recovery_preserves_lock_and_repairs_partial_tail(self) -> None:
        self.bootstrap()
        runtime = self.load_runtime()
        base = runtime.load_config(self.config_path)
        config = runtime.select_ledger_config(base, "logical-chat-1")
        root = runtime.ledger_dir(config)
        journal = runtime.journal_path(config)
        lock = root / "ledger.lock"
        owner = {
            "schema": "chat-objective-continuity-lock-v1",
            "namespace": config["namespace"],
            "logical_chat_id": config["logical_chat_id"],
            "operation": "append_event",
            "pid": 2147483647,
            "token": "dead-owner-token",
            "created_utc": "2026-09-05T00:00:00Z",
        }
        raw = (runtime.canonical_json(owner) + "\n").encode("ascii")
        before_head = runtime.read_state(config)[0]["head_hash"]
        lock.write_bytes(raw)
        with mock.patch.object(runtime, "owner_alive", return_value=False):
            prewrite = runtime.recover_dead_owner_lock(config, runtime.sha256_bytes(raw))
        self.assertEqual(prewrite["status"], "LOCK_RECOVERED")
        self.assertEqual(prewrite["head_hash"], before_head)
        journal.write_bytes(journal.read_bytes() + b'{"partial":')
        lock.write_bytes(raw)
        with mock.patch.object(runtime, "owner_alive", return_value=False):
            result = runtime.recover_dead_owner_lock(config, runtime.sha256_bytes(raw))
        self.assertEqual(result["status"], "LOCK_RECOVERED")
        self.assertFalse(lock.exists())
        self.assertEqual(len(list(root.rglob("dead-lock-*.json"))), 1)
        self.assertEqual(len(list(root.rglob("partial-*.bin"))), 1)
        self.assertTrue(journal.read_bytes().endswith(b"\n"))

    def test_live_or_unknown_lock_owner_is_never_evicted(self) -> None:
        self.bootstrap()
        runtime = self.load_runtime()
        base = runtime.load_config(self.config_path)
        config = runtime.select_ledger_config(base, "logical-chat-1")
        lock = runtime.ledger_dir(config) / "ledger.lock"
        owner = {
            "schema": "chat-objective-continuity-lock-v1",
            "namespace": config["namespace"],
            "logical_chat_id": config["logical_chat_id"],
            "operation": "append_event",
            "pid": os.getpid(),
            "token": "owner-token",
            "created_utc": "2026-09-05T00:00:00Z",
        }
        raw = (runtime.canonical_json(owner) + "\n").encode("ascii")
        for liveness in (True, None):
            lock.write_bytes(raw)
            with mock.patch.object(runtime, "owner_alive", return_value=liveness):
                with self.assertRaises(runtime.LedgerError):
                    runtime.recover_dead_owner_lock(config, runtime.sha256_bytes(raw))
            self.assertEqual(lock.read_bytes(), raw)
            lock.unlink()

    def test_postcommit_before_projection_death_is_replayed_by_exact_dead_lock_recovery(self) -> None:
        self.bootstrap()
        runtime = self.load_runtime()
        base = runtime.load_config(self.config_path)
        config = runtime.select_ledger_config(base, "logical-chat-1")
        before_projection = json.loads((runtime.ledger_dir(config) / "current.json").read_text(encoding="utf-8"))
        with mock.patch.object(runtime, "write_projection", side_effect=OSError("simulated projection death")):
            with self.assertRaises(runtime.PostCommitError):
                runtime.ingest_source(config, ROOT_SESSION, "postcommit-turn", "later source".encode("utf-8"), "user_prompt_source", "HOST_USER_PROMPT_SESSION_BOUND_UNVERIFIED_ACTOR", "host_session_bound_unverified")
        self.assertEqual(before_projection["revision"], 1)
        self.assertEqual(json.loads((runtime.ledger_dir(config) / "current.json").read_text(encoding="utf-8"))["revision"], 1)
        lock = runtime.ledger_dir(config) / "ledger.lock"
        owner = {"schema": "chat-objective-continuity-lock-v1", "namespace": config["namespace"], "logical_chat_id": config["logical_chat_id"], "operation": "postcommit_projection", "pid": 2147483647, "token": "postcommit-dead", "created_utc": "2026-09-05T00:00:00Z"}
        raw = (runtime.canonical_json(owner) + "\n").encode("ascii")
        lock.write_bytes(raw)
        with mock.patch.object(runtime, "owner_alive", return_value=False):
            result = runtime.recover_dead_owner_lock(config, runtime.sha256_bytes(raw))
        self.assertEqual(result["status"], "LOCK_RECOVERED")
        repaired = json.loads((runtime.ledger_dir(config) / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(repaired["revision"], 2)

    def test_lock_release_failure_after_commit_reports_executed_effect_before_main_assignment(self) -> None:
        runtime = self.load_runtime()
        real_unlink = pathlib.Path.unlink

        def fail_only_ledger_lock(path: pathlib.Path, missing_ok: bool = False):
            if path.name == "ledger.lock":
                raise OSError("simulated lock release failure")
            return real_unlink(path, missing_ok=missing_ok)

        argv = [
            "bootstrap",
            "--config",
            str(self.config_path),
            "--logical-chat-id",
            "logical-chat-1",
            "--session-id",
            ROOT_SESSION,
            "--source-file",
            str(self.source_file),
            "--delivery-id",
            "lock-release-main",
        ]
        receipts: list[dict] = []
        with mock.patch.object(pathlib.Path, "unlink", fail_only_ledger_lock), mock.patch.object(runtime, "output", side_effect=receipts.append):
            code = runtime.main(argv)
        self.assertEqual(code, 4)
        self.assertEqual(receipts[-1]["status"], "COMMIT_EFFECT_REQUIRES_REPLAY")
        self.assertTrue(receipts[-1]["executed"])
        self.assertIn("LOCK_RELEASE_UNPROVEN", receipts[-1]["effect"])
        self.assertIn("simulated lock release failure", receipts[-1]["cleanup_fault"])
        lock = next(self.data.rglob("ledger.lock"))
        real_unlink(lock)
        self.assertEqual(self.current_state()["revision"], 1)

    def test_projection_first_fault_survives_additional_lock_cleanup_failure(self) -> None:
        self.bootstrap()
        runtime = self.load_runtime()
        base = runtime.load_config(self.config_path)
        config = runtime.select_ledger_config(base, "logical-chat-1")
        real_unlink = pathlib.Path.unlink

        def fail_only_ledger_lock(path: pathlib.Path, missing_ok: bool = False):
            if path.name == "ledger.lock":
                raise OSError("secondary cleanup failure")
            return real_unlink(path, missing_ok=missing_ok)

        with mock.patch.object(runtime, "write_projection", side_effect=OSError("primary projection failure")), mock.patch.object(pathlib.Path, "unlink", fail_only_ledger_lock):
            with self.assertRaises(runtime.PostCommitError) as caught:
                runtime.ingest_source(config, ROOT_SESSION, "projection-plus-cleanup", b"later source", "user_prompt_source", "HOST_USER_PROMPT_SESSION_BOUND_UNVERIFIED_ACTOR", "host_session_bound_unverified")
        self.assertIn("primary projection failure", caught.exception.primary_message)
        self.assertIn("secondary cleanup failure", caught.exception.cleanup_fault)
        self.assertEqual(caught.exception.event_id, runtime.source_event_id(ROOT_SESSION, "projection-plus-cleanup"))
        lock = runtime.ledger_dir(config) / "ledger.lock"
        real_unlink(lock)
        self.assertEqual(self.current_state()["revision"], 2)

    def test_complete_malformed_last_line_is_not_repaired(self) -> None:
        self.bootstrap()
        journal = next(self.data.rglob("journal.jsonl"))
        with journal.open("ab") as stream:
            stream.write(b'{"malformed":}\n')
        result = self.cli("verify", expected=2)
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(journal.read_bytes().endswith(b'{"malformed":}\n'))
        self.assertEqual(list(self.data.rglob("partial-*.bin")), [])

    def test_classified_source_cannot_be_consumed_twice(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        state = self.current_state()
        source = state["sources"][boot["source_event_id"]]
        payload = {
            "source_event_id": boot["source_event_id"],
            "disposition": "ADD",
            "contract": {
                "contract_id": "contract-v2",
                "parent_contract_id": "contract-v1",
                "primary_objective": "Deliver the primary outcome.",
                "clauses": [{"clause_id": "C1", "source_event_id": boot["source_event_id"], "source_ref": source["source_ref"], "source_sha256": source["source_sha256"], "locator": "line 1"}],
                "open_outcomes": [{"outcome_id": "O1", "status": "OPEN", "description": "observable outcome"}],
            },
        }
        path = self.root / "reuse-source.json"
        self.write_json(path, payload)
        result = self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-REUSE", expected=2)
        self.assertEqual(result["status"], "ERROR")

    def test_add_cannot_drop_parent_outcome_or_rewrite_primary(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "add-turn", "prompt": "add one requirement"})
        state = self.current_state()
        source_id = state["unclassified_source_ids"][0]
        source = state["sources"][source_id]
        parent_clause = state["contracts"]["contract-v1"]["clauses"][0]
        payload = {
            "source_event_id": source_id,
            "disposition": "ADD",
            "contract": {
                "contract_id": "contract-v2",
                "parent_contract_id": "contract-v1",
                "primary_objective": "rewritten primary",
                "clauses": [parent_clause, {"clause_id": "C2", "source_event_id": source_id, "source_ref": source["source_ref"], "source_sha256": source["source_sha256"]}],
                "open_outcomes": [],
            },
        }
        path = self.root / "bad-add.json"
        self.write_json(path, payload)
        result = self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-ADD", expected=2)
        self.assertEqual(result["status"], "ERROR")
        payload["contract"]["primary_objective"] = "Deliver the primary outcome."
        payload["contract"]["open_outcomes"] = [{"outcome_id": "O1", "status": "OPEN", "description": "observable outcome"}]
        payload["contract"]["constraints"] = ["silently changed"]
        self.write_json(path, payload)
        no_transition = self.cli("classify-source", "--input", str(path), "--event-id", "CLASSIFY-ADD-NORMATIVE", expected=2)
        self.assertEqual(no_transition["status"], "ERROR")

    def test_human_projection_exposes_current_policy_without_machine_snapshot_duplication(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        projection = next(self.data.rglob("objective.txt")).read_text(encoding="utf-8")
        self.assertIn("CURRENT POLICY / ACCEPTANCE / AUTHORITY", projection)
        self.assertIn("mandatory_acceptance", projection)
        self.assertIn("prohibited_substitutes", projection)
        self.assertIn("current machine state: current.json", projection)
        self.assertIn("append-only history: journal.jsonl", projection)
        self.assertNotIn("MACHINE SNAPSHOT", projection)
        self.assertNotIn('\"contracts\":', projection)

    def test_human_projection_unicode_display_preserves_machine_and_transport_boundaries(self) -> None:
        boot = self.bootstrap()
        state = self.current_state()
        source = state["sources"][boot["source_event_id"]]
        policy_value = {
            "display": "Unicode café 🙂",
            "syntax": "quote \" slash \\ control\nline",
            "lone": "\ud800",
        }
        classification = {
            "source_event_id": boot["source_event_id"],
            "disposition": "INITIAL",
            "contract": {
                "contract_id": "contract-v1",
                "primary_objective": "Primary objective 🙂",
                "constraints": [policy_value],
                "clauses": [
                    {
                        "clause_id": "C1",
                        "source_event_id": boot["source_event_id"],
                        "source_ref": source["source_ref"],
                        "source_sha256": source["source_sha256"],
                    }
                ],
                "open_outcomes": [
                    {"outcome_id": "O1", "status": "OPEN", "description": "Open outcome 🙂"}
                ],
            },
        }
        classification_path = self.root / "unicode-classification.json"
        classification_path.write_bytes(json.dumps(classification, ensure_ascii=True).encode("ascii"))
        self.cli(
            "classify-source",
            "--input",
            str(classification_path),
            "--event-id",
            "CLASSIFY-UNICODE-DISPLAY",
        )
        next_value = {
            "display": "Next work 🙂",
            "syntax": "quote \" slash \\ tab\tend",
            "lone": "\udfff",
        }
        progress_path = self.root / "unicode-progress.json"
        progress_path.write_bytes(
            json.dumps(
                {
                    "contract_id": "contract-v1",
                    "outcome_updates": [],
                    "next_eligible_work": next_value,
                    "blockers": [{"display": "No blocker 🙂"}],
                    "return_step": "Return to objective 🙂",
                    "proof_ceiling": "Display boundary only 🙂",
                },
                ensure_ascii=True,
            ).encode("ascii")
        )
        self.cli("progress", "--input", str(progress_path), "--event-id", "PROGRESS-UNICODE-DISPLAY")
        root = next(self.data.rglob("identity.json")).parent
        journal = root / "journal.jsonl"
        machine = root / "current.json"
        projection_path = root / "objective.txt"
        source_path = root / source["source_ref"]
        before = {
            "journal": journal.read_bytes(),
            "machine": machine.read_bytes(),
            "source": source_path.read_bytes(),
            "head": json.loads(machine.read_text(encoding="utf-8"))["head_hash"],
        }
        projection = projection_path.read_text(encoding="utf-8")
        self.assertIn("Unicode café 🙂", projection)
        self.assertIn("Next work 🙂", projection)
        self.assertNotIn("\\u00e9", projection)
        self.assertNotIn("\\ud83d\\ude42", projection)
        constraints_line = next(line for line in projection.splitlines() if line.startswith("- constraints: "))
        next_line = next(line for line in projection.splitlines() if line.startswith("- next_eligible_work: "))
        self.assertEqual(json.loads(constraints_line.split(": ", 1)[1]), [policy_value])
        self.assertEqual(json.loads(next_line.split(": ", 1)[1]), next_value)
        self.assertIn("\\ud800", constraints_line)
        self.assertIn("\\udfff", next_line)
        rebuilt = self.cli("status")["state"]
        self.assertEqual(journal.read_bytes(), before["journal"])
        self.assertEqual(machine.read_bytes(), before["machine"])
        self.assertEqual(source_path.read_bytes(), before["source"])
        self.assertEqual(rebuilt["head_hash"], before["head"])
        self.assertEqual(rebuilt["contracts"]["contract-v1"]["constraints"], [policy_value])
        self.assertEqual(rebuilt["latest_progress"]["next_eligible_work"], next_value)
        runtime = self.load_runtime()
        protocol_json = runtime.canonical_json(rebuilt)
        protocol_json.encode("ascii")
        self.assertIn("\\u00e9", protocol_json)
        self.assertIn("\\ud83d\\ude42", protocol_json)

    def test_dispose_source_keeps_raw_bytes_and_cannot_change_objective(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "internal-turn", "prompt": "internal continuation text"})
        before = self.current_state()
        source_id = before["unclassified_source_ids"][0]
        source_path = next(self.data.rglob(before["sources"][source_id]["source_ref"]))
        raw = source_path.read_bytes()
        self.cli(
            "dispose-source",
            "--input",
            "-",
            "--event-id",
            "DISPOSE-INTERNAL",
            stdin={
                "source_event_id": source_id,
                "disposition": "INTERNAL_CONTINUATION",
                "review_ground": "owner reviewed this captured occurrence as internal and non-authoritative",
            },
        )
        after = self.current_state()
        self.assertEqual(after["current_contract_id"], before["current_contract_id"])
        self.assertEqual(after["contracts"], before["contracts"])
        self.assertEqual(source_path.read_bytes(), raw)
        self.assertFalse(after["disposed_sources"][source_id]["semantic_authority"])
        self.assertFalse(after["disposed_sources"][source_id]["objective_changed"])

    def test_dispose_already_incorporated_duplicate_requires_current_contract_links(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "duplicate-capture", "prompt": "already reviewed and incorporated text"})
        state = self.current_state()
        source_id = state["unclassified_source_ids"][0]
        self.cli(
            "dispose-source",
            "--input",
            "-",
            "--event-id",
            "DISPOSE-DUPLICATE",
            stdin={
                "source_event_id": source_id,
                "disposition": "ALREADY_INCORPORATED_DUPLICATE",
                "review_ground": "root reviewed this occurrence and linked its already-incorporated meaning without granting new authority",
                "incorporated_contract_id": "contract-v1",
                "incorporated_clause_ids": ["C1"],
            },
        )
        after = self.current_state()
        self.assertEqual(after["current_contract_id"], "contract-v1")
        self.assertEqual(after["disposed_sources"][source_id]["incorporated_clause_ids"], ["C1"])

    def test_concurrent_prompt_appends_retain_one_chain(self) -> None:
        self.bootstrap()
        processes = []
        for index in range(12):
            command = [
                sys.executable,
                "-B",
                str(SCRIPT),
                "hook",
                "--event",
                "UserPromptSubmit",
                "--config",
                str(self.config_path),
            ]
            payload = json.dumps({"session_id": ROOT_SESSION, "turn_id": f"turn-{index}", "prompt": f"source-{index}"}, ensure_ascii=False).encode("utf-8")
            processes.append(subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            processes[-1]._test_payload = payload
        outputs = [process.communicate(process._test_payload, timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in outputs), outputs)
        result = self.cli("verify")
        self.assertEqual(result["revision"], 13)

    def test_compaction_resume_and_incidental_fields_reuse_identity(self) -> None:
        self.bootstrap()
        for event_name, event_id in (("PreCompact", "pre-1"), ("PostCompact", "post-1"), ("SessionStart", "resume-1")):
            self.cli(
                "hook",
                "--event",
                event_name,
                stdin={
                    "session_id": ROOT_SESSION,
                    "event_id": event_id,
                    "source": "compact" if "Compact" in event_name else "resume",
                    "cwd": "D:/changed-workspace",
                    "model": "changed-model",
                    "session_name": "changed-name",
                },
            )
        state = self.current_state()
        self.assertEqual(state["identity"]["logical_chat_id"], "logical-chat-1")
        self.assertEqual(state["revision"], 1)

    def test_recovery_context_begins_with_pointer_and_stays_within_hook_bound(self) -> None:
        self.bootstrap()
        for index in range(12):
            self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": f"long-id-{index}-" + "x" * 80, "prompt": f"source {index}"})
        result = self.cli("hook", "--event", "SessionStart", stdin={"session_id": ROOT_SESSION, "source": "compact"})
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("READ FIRST:"))
        self.assertLessEqual(len(context.encode("utf-8")), 600)

    def test_new_session_is_distinct_until_explicit_same_chat_binding(self) -> None:
        self.bootstrap()
        before = self.current_state()["head_hash"]
        no_ledger = self.cli(
            "hook",
            "--event",
            "SessionStart",
            stdin={"session_id": "fork-1", "event_id": "fork-start", "forked_from": ROOT_SESSION},
        )
        self.assertEqual(no_ledger["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(len(list(self.data.rglob("identity.json"))), 1)
        self.assertEqual(self.current_state()["head_hash"], before)
        self.config["session_bindings"]["fork-1"] = {"role": "root", "logical_chat_id": "logical-chat-1"}
        self.write_json(self.config_path, self.config)
        accepted = self.cli("hook", "--event", "SessionStart", stdin={"session_id": "fork-1", "event_id": "fork-start"})
        self.assertEqual(accepted["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(self.current_state()["head_hash"], before)
        self.assertEqual(self.current_state()["identity"]["logical_chat_id"], "logical-chat-1")

    def test_first_prompt_creates_separate_ledger_for_each_new_chat(self) -> None:
        self.bootstrap()
        self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": "new-chat-2", "turn_id": "new-turn", "prompt": "second chat source"},
        )
        identities = [json.loads(path.read_text(encoding="utf-8")) for path in self.data.rglob("identity.json")]
        self.assertEqual({item["logical_chat_id"] for item in identities}, {"logical-chat-1", "new-chat-2"})
        self.assertEqual(self.current_state()["revision"], 1)

    def test_resume_and_compact_before_first_prompt_do_not_create_ledger(self) -> None:
        for event_name in ("SessionStart", "PreCompact", "PostCompact", "Stop"):
            result = self.cli(
                "hook",
                "--event",
                event_name,
                stdin={"session_id": "empty-chat", "turn_id": f"{event_name}-1", "source": "resume", "trigger": "auto"},
            )
            if event_name == "Stop":
                self.assertTrue(result["continue"])
        self.assertEqual(len(list(self.data.rglob("identity.json"))), 0)
        self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": "empty-chat", "turn_id": "first-turn", "prompt": "first source"},
        )
        self.assertEqual(len(list(self.data.rglob("identity.json"))), 1)
        journal = next(self.data.rglob("journal.jsonl"))
        before = journal.read_bytes()
        self.cli("hook", "--event", "SessionStart", stdin={"session_id": "empty-chat", "source": "resume"})
        self.cli("hook", "--event", "PostCompact", stdin={"session_id": "empty-chat", "trigger": "auto"})
        self.assertEqual(journal.read_bytes(), before)

    def test_subagent_prompt_writes_nothing_and_creates_no_second_ledger(self) -> None:
        self.bootstrap()
        before = self.current_state()["head_hash"]
        result = self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": "child-1", "turn_id": "child-turn", "prompt": "child text is not user authority"},
        )
        self.assertEqual(result, {})
        self.assertEqual(self.current_state()["head_hash"], before)
        self.assertEqual(len(list(self.data.rglob("identity.json"))), 1)

    def test_unknown_cancel_effect_holds_only_declared_dependent_mutation(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        start = self.root / "start.json"
        self.write_json(start, {"action_id": "A1", "description": "external write", "outcome_ids": ["O1"], "contract_id": "contract-v1", "source_clause_ids": ["C1"]})
        self.cli("action-start", "--input", str(start), "--event-id", "ACTION-START-1")
        outcome = self.root / "outcome.json"
        self.write_json(outcome, {"action_id": "A1", "status": "UNKNOWN_EFFECT", "effect_state": "UNKNOWN_EFFECT", "first_fault": "cancelled before result readback"})
        self.cli("action-outcome", "--input", str(outcome), "--event-id", "ACTION-OUTCOME-1")
        self.assertEqual(self.cli("preflight", "--tool-name", "read", "--depends-on-action", "A1")["decision"], "allow")
        self.assertEqual(self.cli("preflight", "--tool-name", "write")["decision"], "allow")
        dependent = self.cli("preflight", "--tool-name", "write", "--depends-on-action", "A1")
        self.assertEqual(dependent["decision"], "hold")
        self.assertEqual(dependent["blocked_action_ids"], ["A1"])

    def test_hook_pretooluse_emits_current_deny_shape_only_for_scoped_mutation(self) -> None:
        self.bootstrap()
        denied = self.cli(
            "hook",
            "--event",
            "PreToolUse",
            stdin={"session_id": ROOT_SESSION, "turn_id": "turn-tool", "tool_name": "write", "tool_use_id": "tool-1", "tool_input": {}},
        )
        self.assertEqual(denied["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        allowed = self.cli(
            "hook",
            "--event",
            "PreToolUse",
            stdin={"session_id": ROOT_SESSION, "turn_id": "turn-tool", "tool_name": "read", "tool_use_id": "tool-2", "tool_input": {}},
        )
        self.assertEqual(allowed, {})

    def test_action_terminal_statuses_are_retained(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        for index, terminal in enumerate(("SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN_EFFECT"), start=1):
            action_id = f"A{index}"
            start = self.root / f"start-{index}.json"
            self.write_json(start, {"action_id": action_id, "description": terminal, "outcome_ids": ["O1"], "contract_id": "contract-v1", "source_clause_ids": ["C1"]})
            self.cli("action-start", "--input", str(start), "--event-id", f"START-{index}")
            outcome = self.root / f"outcome-{index}.json"
            outcome_payload = {"action_id": action_id, "status": terminal, "evidence_refs": [f"test:{terminal}"]}
            outcome_payload["effect_state"] = {
                "SUCCEEDED": "EFFECT_CONFIRMED_SUCCEEDED",
                "FAILED": "EFFECT_CONFIRMED_FAILED",
                "CANCELLED": "NO_EFFECT_CONFIRMED",
                "UNKNOWN_EFFECT": "UNKNOWN_EFFECT",
            }[terminal]
            self.write_json(outcome, outcome_payload)
            self.cli("action-outcome", "--input", str(outcome), "--event-id", f"OUTCOME-{index}")
        state = self.current_state()
        self.assertEqual({item["status"] for item in state["actions"].values()}, {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN_EFFECT"})
        self.assertEqual(state["unknown_effect_action_ids"], ["A4"])

    def test_action_identity_terminal_result_retry_and_reconciliation_are_append_only(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        start = {"action_id": "A1", "description": "first attempt", "outcome_ids": ["O1"], "contract_id": "contract-v1", "source_clause_ids": ["C1"]}
        self.cli("action-start", "--input", "-", "--event-id", "START-A1", stdin=start)
        reused = self.cli("action-start", "--input", "-", "--event-id", "START-A1-REUSE", stdin=start, expected=2)
        self.assertEqual(reused["status"], "ERROR")
        unknown = {"action_id": "A1", "status": "UNKNOWN_EFFECT", "effect_state": "UNKNOWN_EFFECT", "first_fault": "transport lost after dispatch", "evidence_refs": ["dispatch:1"]}
        self.cli("action-outcome", "--input", "-", "--event-id", "OUTCOME-A1", stdin=unknown)
        overwritten = self.cli(
            "action-outcome",
            "--input",
            "-",
            "--event-id",
            "OUTCOME-A1-OVERWRITE",
            stdin={"action_id": "A1", "status": "SUCCEEDED", "effect_state": "EFFECT_CONFIRMED_SUCCEEDED", "description": "overwrite"},
            expected=2,
        )
        self.assertEqual(overwritten["status"], "ERROR")
        self.cli("action-start", "--input", "-", "--event-id", "START-A2", stdin={"action_id": "A2", "description": "bounded retry", "outcome_ids": ["O1"], "contract_id": "contract-v1", "source_clause_ids": ["C1"], "retry_of": "A1"})
        self.cli("action-outcome", "--input", "-", "--event-id", "OUTCOME-A2", stdin={"action_id": "A2", "status": "SUCCEEDED", "effect_state": "EFFECT_CONFIRMED_SUCCEEDED", "evidence_refs": ["retry:success"]})
        self.assertEqual(self.current_state()["unknown_effect_action_ids"], ["A1"])
        self.cli("action-reconcile", "--input", "-", "--event-id", "RECON-A1-1", stdin={"action_id": "A1", "resolution": "REMAINS_UNKNOWN", "evidence_refs": ["readback:first"]})
        self.assertEqual(self.current_state()["unknown_effect_action_ids"], ["A1"])
        self.cli("action-reconcile", "--input", "-", "--event-id", "RECON-A1-2", stdin={"action_id": "A1", "resolution": "NO_EFFECT_CONFIRMED", "prior_reconciliation_event_id": "RECON-A1-1", "evidence_refs": ["readback:final"]})
        state = self.current_state()
        self.assertEqual(state["unknown_effect_action_ids"], [])
        self.assertEqual(state["actions"]["A1"]["first_fault"], "transport lost after dispatch")
        self.assertEqual([item["event_id"] for item in state["action_reconciliations"]["A1"]], ["RECON-A1-1", "RECON-A1-2"])

    def test_missing_or_modified_contract_source_is_unproven_and_holds_only_dependent_mutation(self) -> None:
        boot = self.bootstrap()
        self.classify_initial(boot["source_event_id"])
        state = self.current_state()
        source = state["sources"][boot["source_event_id"]]
        ledger_root = next(self.data.rglob("identity.json")).parent
        source_path = ledger_root / source["source_ref"]
        original = source_path.read_bytes()
        source_path.unlink()
        missing = self.cli("verify")
        self.assertEqual(missing["status"], "STRUCTURE_PASS_SOURCE_UNPROVEN")
        self.assertIn(boot["source_event_id"], missing["unproven_source_ids"])
        self.assertEqual(self.cli("preflight", "--tool-name", "read")["decision"], "allow")
        self.assertEqual(self.cli("preflight", "--tool-name", "write")["decision"], "hold")
        rejected = self.cli("progress", "--input", "-", "--event-id", "PROGRESS-SOURCE-MISSING", stdin={"contract_id": "contract-v1", "outcome_updates": []}, expected=2)
        self.assertEqual(rejected["status"], "ERROR")
        source_path.write_bytes(b"modified")
        modified = self.cli("verify")
        self.assertEqual(modified["status"], "STRUCTURE_PASS_SOURCE_UNPROVEN")
        source_path.write_bytes(original)
        self.assertEqual(self.cli("verify")["status"], "STRUCTURE_PASS")

    def test_session_bound_prompt_without_agent_id_is_raw_only(self) -> None:
        result = self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": ROOT_SESSION, "turn_id": "turn-no-agent", "prompt": "raw host prompt"},
        )
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        state = self.current_state()
        self.assertEqual(state["revision"], 1)
        self.assertIsNone(state["current_contract_id"])
        source = state["sources"][state["unclassified_source_ids"][0]]
        self.assertEqual(source["semantic_state"], "UNCLASSIFIED_REQUIRES_ROOT_SOURCE_REVIEW")

    def test_utf8_prompt_and_stdin_are_lossless_while_stdout_is_ascii_json(self) -> None:
        prompt = "Keep the primary objective 🙂 café"
        result = self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "unicode-turn", "prompt": prompt})
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        state = self.current_state()
        source = state["sources"][state["unclassified_source_ids"][0]]
        root = next(self.data.rglob("identity.json")).parent
        self.assertEqual((root / source["source_ref"]).read_text(encoding="utf-8"), prompt)
        self.cli(
            "dispose-source",
            "--input",
            "-",
            "--event-id",
            "UNICODE-DISPOSE",
            stdin={"source_event_id": source["source_event_id"], "disposition": "IRRELEVANT", "review_ground": "Owner review evidence 🙂"},
        )

    def test_oversize_prompt_records_identity_bound_gap_without_claiming_continuity(self) -> None:
        self.config["max_prompt_bytes"] = 8
        self.write_json(self.config_path, self.config)
        result = self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "oversize-prompt", "prompt": "0123456789"})
        self.assertTrue(result["continue"])
        gaps = list(self.data.rglob("capture-gaps/*.json"))
        self.assertEqual(len(gaps), 1)
        gap = json.loads(gaps[0].read_text(encoding="ascii"))
        gap_id = gaps[0].stem
        self.assertFalse(gap["continuity_captured"])
        self.assertFalse(gap["semantic_authority"])
        self.assertNotIn("0123456789", gaps[0].read_text(encoding="ascii"))
        self.assertEqual(list(self.data.rglob("journal.jsonl")), [])
        self.assertEqual(self.cli("preflight", "--tool-name", "read")["decision"], "allow")
        held = self.cli("preflight", "--tool-name", "write")
        self.assertEqual(held["decision"], "hold")
        self.assertEqual(held["unresolved_capture_gap_ids"], [gap_id])
        resumed = self.cli("hook", "--event", "SessionStart", stdin={"session_id": ROOT_SESSION, "source": "resume"})
        context = resumed["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-8")), 600)
        self.assertIn(str(gaps[0].parent), context)
        self.assertNotIn("objective.txt", context)
        self.assertEqual(sorted(path.stem for path in gaps[0].parent.glob("*.json")), [gap_id])
        self.config["max_prompt_bytes"] = 262144
        self.write_json(self.config_path, self.config)
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "wrong-old-source", "prompt": "abcdefghij"})
        wrong_state = self.current_state()
        wrong_source_id = wrong_state["unclassified_source_ids"][0]
        wrong_recovery = self.cli(
            "resolve-capture-gap",
            "--input",
            "-",
            "--event-id",
            "RESOLVE-GAP-WRONG-SOURCE",
            stdin={"gap_id": gap_id, "resolution": "SOURCE_RECOVERED", "review_ground": "wrong old source must not close an exact gap", "source_event_id": wrong_source_id},
            expected=2,
        )
        self.assertEqual(wrong_recovery["status"], "ERROR")
        self.assertIn(gap_id, self.current_state()["unresolved_capture_gap_ids"])
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "recovered-prompt", "prompt": "0123456789"})
        state = self.current_state()
        source_id = next(
            source_id
            for source_id in state["unclassified_source_ids"]
            if state["sources"][source_id]["source_sha256"] == gap["detail"]["observed_sha256"]
        )
        self.assertIn(gap_id, state["unresolved_capture_gap_ids"])
        self.cli(
            "resolve-capture-gap",
            "--input",
            "-",
            "--event-id",
            "RESOLVE-GAP-SOURCE",
            stdin={"gap_id": gap_id, "resolution": "SOURCE_RECOVERED", "review_ground": "exact source bytes were recaptured for root review", "source_event_id": source_id},
        )
        resolved = self.current_state()
        self.assertEqual(resolved["unresolved_capture_gap_ids"], [])
        self.assertEqual(resolved["capture_gaps"][gap_id]["resolution"], "SOURCE_RECOVERED")
        root = next(self.data.rglob("identity.json")).parent
        recovered_path = root / resolved["sources"][source_id]["source_ref"]
        recovered_path.write_bytes(b"XXXXXXXXXX")
        invalidated = self.current_state()
        self.assertIn(gap_id, invalidated["unresolved_capture_gap_ids"])
        self.assertEqual(invalidated["capture_gaps"][gap_id]["status"], "INVALID_RECOVERY_RELATION")

    def test_identity_bound_capture_gap_allows_explicit_owner_disposition_without_global_stop(self) -> None:
        self.config["max_prompt_bytes"] = 4
        self.write_json(self.config_path, self.config)
        self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "gap-disposition", "prompt": "too long"})
        gap = next(self.data.rglob("capture-gaps/*.json"))
        self.cli(
            "resolve-capture-gap",
            "--input",
            "-",
            "--event-id",
            "RESOLVE-GAP-DISPOSITION",
            stdin={"gap_id": gap.stem, "resolution": "OWNER_DISPOSITION", "review_ground": "root determined this failed capture creates no user authority or objective change"},
        )
        state = self.current_state()
        self.assertEqual(state["unresolved_capture_gap_ids"], [])
        self.assertFalse(state["capture_gap_resolutions"][gap.stem]["semantic_authority"])
        self.assertFalse(state["capture_gap_resolutions"][gap.stem]["objective_changed"])

    def test_oversize_hook_input_uses_global_bounded_gap_when_identity_unavailable(self) -> None:
        self.config["max_hook_input_bytes"] = 32
        self.write_json(self.config_path, self.config)
        result = self.cli("hook", "--event", "UserPromptSubmit", stdin={"session_id": ROOT_SESSION, "turn_id": "oversize-input", "prompt": "x" * 100})
        self.assertTrue(result["continue"])
        gaps = list((self.data / "test-user" / "_unbound-capture-gaps").glob("*.json"))
        self.assertEqual(len(gaps), 1)
        gap = json.loads(gaps[0].read_text(encoding="ascii"))
        self.assertEqual(gap["identity"]["logical_chat_id"], "UNAVAILABLE")
        self.assertFalse(gap["continuity_captured"])
        resumed = self.cli("hook", "--event", "SessionStart", stdin={"session_id": ROOT_SESSION, "source": "resume"})
        self.assertNotIn(gaps[0].stem, resumed.get("hookSpecificOutput", {}).get("additionalContext", ""))
        self.assertEqual(list(self.data.rglob("identity.json")), [])

    def test_stop_structural_failure_never_creates_continuation(self) -> None:
        self.bootstrap()
        journal = next(self.data.rglob("journal.jsonl"))
        raw = journal.read_bytes()
        journal.write_bytes(raw.replace(b'"seq":1', b'"seq":9', 1))
        event = {"session_id": ROOT_SESSION, "event_id": "stop-1"}
        first = self.cli("hook", "--event", "Stop", stdin=event)
        second = self.cli("hook", "--event", "Stop", stdin={**event, "stop_hook_active": True})
        self.assertEqual(first["continue"], True)
        self.assertEqual(second["continue"], True)
        self.assertNotIn("decision", first)
        self.assertIn("systemMessage", first)

    def test_hook_event_name_mismatch_is_rejected(self) -> None:
        result = self.cli(
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": ROOT_SESSION, "turn_id": "turn-x", "prompt": "x", "hook_event_name": "Stop"},
        )
        self.assertTrue(result["continue"])
        self.assertIn("systemMessage", result)
        self.assertEqual(len(list(self.data.rglob("journal.jsonl"))), 0)

    def test_stdout_failure_after_commit_never_reports_executed_false(self) -> None:
        runtime = self.load_runtime()
        argv = [
            "bootstrap",
            "--config",
            str(self.config_path),
            "--logical-chat-id",
            "logical-chat-1",
            "--session-id",
            ROOT_SESSION,
            "--source-file",
            str(self.source_file),
            "--delivery-id",
            "transport-failure",
        ]
        with mock.patch.object(runtime, "output", side_effect=OSError("simulated stdout failure")):
            code = runtime.main(argv)
        self.assertEqual(code, 4)
        self.assertEqual(self.current_state()["revision"], 1)


if __name__ == "__main__":
    unittest.main()
