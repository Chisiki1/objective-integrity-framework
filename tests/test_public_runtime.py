from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tools" / "demo.py"
BOOTSTRAP = ROOT / "tools" / "bootstrap.py"
LEDGER = ROOT / "runtime" / "objective_ledger.py"
LIFECYCLE = ROOT / "runtime" / "skills" / "master-guided-skill-lifecycle" / "scripts"
QUEUE = LIFECYCLE / "learning_queue.py"
CHAIN_TEST = ROOT / "runtime" / "skills" / "master-guided-skill-resolver" / "scripts" / "test_same_chat_bridge.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class PublicRuntimeConsumerTest(unittest.TestCase):
    def setUp(self) -> None:
        parent = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="oif-", dir=parent)
        self.root = Path(self.temporary.name)
        self.process_temp = self.root / "process-temp"
        self.process_temp.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "TEMP": str(self.process_temp),
                "TMP": str(self.process_temp),
                "TMPDIR": str(self.process_temp),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_process(
        self,
        script: Path,
        *arguments: str,
        stdin: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        self.assertTrue(script.is_file(), f"missing public runtime member: {script}")
        result = subprocess.run(
            [sys.executable, "-B", str(script), *arguments],
            input=json.dumps(stdin, ensure_ascii=False).encode("utf-8") if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=self.environment,
            check=False,
        )
        self.assertIn(result.returncode, expected, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))
        return result

    def run_json(
        self,
        script: Path,
        *arguments: str,
        stdin: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        result = self.run_process(script, *arguments, stdin=stdin, expected=expected)
        value = json.loads(result.stdout.decode("utf-8"))
        self.assertIsInstance(value, dict)
        return value

    def test_demo_runs_actual_runtime_and_rejects_existing_user_files(self) -> None:
        demo_root = self.root / "demo"
        result = self.run_process(DEMO, "--directory", str(demo_root))
        self.assertIn("Objective integrity demo completed.", result.stdout.decode("utf-8"))
        summary = json.loads((demo_root / "demo-result.json").read_text(encoding="utf-8"))
        self.assertEqual({key: value["status"] for key, value in summary["outcomes"].items()}, {"DEMO-O1": "SATISFIED", "DEMO-O2": "SATISFIED"})
        self.assertEqual(summary["improvement_candidate"]["next_use_match_count"], 1)
        self.assertEqual(summary["improvement_candidate"]["review_status"], "pending")
        self.assertFalse(summary["improvement_candidate"]["materialized"])
        self.assertTrue((demo_root / summary["improvement_candidate"]["artifact"]).is_file())
        preview = summary["improvement_candidate"]["materializer_preview"]
        self.assertEqual(preview["decision"], "INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE")
        self.assertEqual(preview["returncode"], 3)
        self.assertEqual(preview["writes_performed"], [])
        self.assertTrue((demo_root / preview["receipt"]).is_file())
        self.assertEqual(list((demo_root / "inactive-candidates").iterdir()), [])
        self.assertTrue((demo_root / "queue" / "learning.sqlite3").is_file())
        record = json.loads((demo_root / "run-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "COMPLETED")
        returncodes = {step["name"]: step["returncode"] for step in record["steps"]}
        self.assertEqual(returncodes.pop("candidate-materialization-preview"), 3)
        self.assertTrue(all(returncode == 0 for returncode in returncodes.values()))

        occupied = self.root / "occupied"
        occupied.mkdir()
        marker = occupied / "user-file.txt"
        marker.write_text("keep me", encoding="utf-8")
        rejected = self.run_process(DEMO, "--directory", str(occupied), expected=(2,))
        self.assertIn("new or empty", rejected.stderr.decode("utf-8"))
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(list(occupied.iterdir()), [marker])

    def ledger_call(
        self,
        config: Path,
        logical_chat: str,
        *arguments: str,
        stdin: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        return self.run_json(
            LEDGER,
            *arguments,
            "--config",
            str(config),
            "--logical-chat-id",
            logical_chat,
            stdin=stdin,
            expected=expected,
        )

    def test_same_chat_add_preserves_outcome_and_unknown_effect_is_append_only(self) -> None:
        data_root = self.root / "ledger-data"
        config = self.root / "ledger-config.json"
        source_path = self.root / "source.txt"
        source_path.write_text("Deliver the observable artifact and preserve it across later additions.\n", encoding="utf-8")
        logical_chat = "consumer-chat-one"
        config.write_text(
            json.dumps(
                {
                    "schema": "chat-objective-continuity-config-v1",
                    "namespace": "public-consumer-test",
                    "session_bindings": {logical_chat: {"role": "root", "logical_chat_id": logical_chat}},
                    "implicit_session_ledgers": True,
                    "data_root": str(data_root),
                    "lock_timeout_seconds": 5,
                    "max_hook_input_bytes": 1048576,
                    "max_prompt_bytes": 262144,
                    "tool_classes": {"read": "read_only", "write": "mutating"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bootstrap = self.ledger_call(
            config,
            logical_chat,
            "bootstrap",
            "--session-id",
            logical_chat,
            "--source-file",
            str(source_path),
            "--delivery-id",
            "initial-source",
        )
        state = self.ledger_call(config, logical_chat, "status")["state"]
        source_event_id = bootstrap["source_event_id"]
        source = state["sources"][source_event_id]
        clause = {
            "clause_id": "C1",
            "source_event_id": source_event_id,
            "source_ref": source["source_ref"],
            "source_sha256": source["source_sha256"],
        }
        self.ledger_call(
            config,
            logical_chat,
            "classify-source",
            "--input",
            "-",
            "--event-id",
            "CLASSIFY-INITIAL",
            "--expected-head",
            state["head_hash"],
            stdin={
                "source_event_id": source_event_id,
                "disposition": "INITIAL",
                "classification_note": "consumer test source review",
                "contract": {
                    "contract_id": "contract-v1",
                    "primary_objective": "Deliver the observable artifact.",
                    "mandatory_acceptance": ["The artifact exists."],
                    "clauses": [clause],
                    "open_outcomes": [{"outcome_id": "O1", "status": "OPEN", "description": "observable artifact"}],
                },
            },
        )
        self.ledger_call(
            config,
            logical_chat,
            "progress",
            "--input",
            "-",
            "--event-id",
            "PROGRESS-O1",
            stdin={"contract_id": "contract-v1", "outcome_updates": [{"outcome_id": "O1", "status": "SATISFIED", "evidence_refs": ["file:artifact.txt"]}]},
        )
        self.ledger_call(
            config,
            logical_chat,
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": logical_chat, "turn_id": "addition", "prompt": "Also deliver a concise next-use note."},
        )
        state = self.ledger_call(config, logical_chat, "status")["state"]
        addition_id = state["unclassified_source_ids"][0]
        addition = state["sources"][addition_id]
        self.ledger_call(
            config,
            logical_chat,
            "classify-source",
            "--input",
            "-",
            "--event-id",
            "CLASSIFY-ADD",
            "--expected-head",
            state["head_hash"],
            stdin={
                "source_event_id": addition_id,
                "disposition": "ADD",
                "contract": {
                    "contract_id": "contract-v2",
                    "parent_contract_id": "contract-v1",
                    "primary_objective": "Deliver the observable artifact.",
                    "clauses": [
                        clause,
                        {
                            "clause_id": "C2",
                            "source_event_id": addition_id,
                            "source_ref": addition["source_ref"],
                            "source_sha256": addition["source_sha256"],
                        },
                    ],
                    "open_outcomes": [
                        {"outcome_id": "O1", "status": "OPEN", "description": "observable artifact"},
                        {"outcome_id": "O2", "status": "OPEN", "description": "next-use note"},
                    ],
                },
            },
        )
        state = self.ledger_call(config, logical_chat, "status")["state"]
        self.assertEqual(state["open_outcomes"]["O1"]["status"], "SATISFIED")
        self.assertEqual(state["open_outcomes"]["O1"]["evidence_refs"], ["file:artifact.txt"])

        stale_head = state["head_hash"]
        self.ledger_call(
            config,
            logical_chat,
            "progress",
            "--input",
            "-",
            "--event-id",
            "PROGRESS-CURRENT",
            stdin={"contract_id": "contract-v2", "outcome_updates": [], "next_eligible_work": "record the action effect"},
        )
        stale = self.ledger_call(
            config,
            logical_chat,
            "progress",
            "--input",
            "-",
            "--event-id",
            "PROGRESS-STALE",
            "--expected-head",
            stale_head,
            stdin={"contract_id": "contract-v2", "outcome_updates": []},
            expected=(3,),
        )
        self.assertEqual(stale["status"], "CAS_MISMATCH")

        self.ledger_call(
            config,
            logical_chat,
            "action-start",
            "--input",
            "-",
            "--event-id",
            "UNKNOWN-START",
            stdin={"action_id": "A-UNKNOWN", "description": "bounded synthetic write", "outcome_ids": ["O2"], "contract_id": "contract-v2", "source_clause_ids": ["C2"]},
        )
        self.ledger_call(
            config,
            logical_chat,
            "action-outcome",
            "--input",
            "-",
            "--event-id",
            "UNKNOWN-OUTCOME",
            stdin={"action_id": "A-UNKNOWN", "status": "UNKNOWN_EFFECT", "effect_state": "UNKNOWN_EFFECT", "first_fault": "result channel closed before readback"},
        )
        read_decision = self.ledger_call(config, logical_chat, "preflight", "--tool-name", "read", "--depends-on-action", "A-UNKNOWN")
        write_decision = self.ledger_call(config, logical_chat, "preflight", "--tool-name", "write", "--depends-on-action", "A-UNKNOWN")
        self.assertEqual(read_decision["decision"], "allow")
        self.assertEqual(write_decision["decision"], "hold")
        state = self.ledger_call(config, logical_chat, "status")["state"]
        self.assertEqual(state["actions"]["A-UNKNOWN"]["first_fault"], "result channel closed before readback")

        self.ledger_call(
            config,
            logical_chat,
            "hook",
            "--event",
            "UserPromptSubmit",
            stdin={"session_id": "consumer-chat-two", "turn_id": "first", "prompt": "A separate synthetic objective."},
        )
        identities = [json.loads(path.read_text(encoding="utf-8")) for path in data_root.rglob("identity.json")]
        self.assertEqual({value["logical_chat_id"] for value in identities}, {logical_chat, "consumer-chat-two"})

    def test_learning_queue_lifecycle_returns_only_exact_next_use(self) -> None:
        queue_root = self.root / "queue"
        queue_root.mkdir()
        database = queue_root / "learning.sqlite3"
        artifact = self.root / "candidate.md"
        artifact.write_text("# Synthetic candidate\n", encoding="utf-8")
        self.run_json(QUEUE, "init", "--db", str(database))
        base = {
            "schema_version": "learning-queue-event-v1",
            "candidate_id": "synthetic-candidate",
            "owner_ref": "owner:consumer-test",
            "objective_ref": "objective:consumer-test",
            "source_refs": ["source:synthetic"],
            "project_event_ref": "event:consumer-test",
            "global_disposition_ref": "disposition:isolated",
            "family_keys": ["family:open-outcome-continuity"],
            "artifact_ref": {"path": str(artifact), "sha256": digest(artifact)},
            "next_consumer_ref": "consumer:next-addition",
            "next_use_trigger": "trigger:add-outcome",
            "evidence_refs": ["evidence:artifact-hash"],
            "rollback_refs": ["rollback:remove-test-artifact"],
        }
        for revision, state in ((0, "discovered"), (1, "prepared")):
            event = {**base, "event_id": f"QUEUE-{state.upper()}", "expected_revision": revision, "state": state}
            event_path = self.root / f"queue-{state}.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            self.run_json(QUEUE, "upsert", "--db", str(database), "--input", str(event_path))
        due = self.run_json(
            QUEUE,
            "due",
            "--db",
            str(database),
            "--objective-ref",
            "objective:consumer-test",
            "--family-key",
            "family:open-outcome-continuity",
            "--trigger",
            "trigger:add-outcome",
        )
        self.assertEqual(due["match_count"], 1)
        self.assertEqual(due["matches"][0]["state"], "prepared")
        self.assertFalse(due["authority_granted"])
        no_match = self.run_json(
            QUEUE,
            "due",
            "--db",
            str(database),
            "--objective-ref",
            "objective:consumer-test",
            "--family-key",
            "family:other",
            "--trigger",
            "trigger:add-outcome",
        )
        self.assertEqual(no_match["match_count"], 0)
        history = self.run_json(QUEUE, "history", "--db", str(database), "--candidate-id", "synthetic-candidate")
        self.assertEqual([event["state"] for event in history["events"]], ["discovered", "prepared"])

    def test_compiled_selection_reaches_only_the_receipt_bound_script(self) -> None:
        result = self.run_process(CHAIN_TEST, "-v")
        self.assertIn("OK", result.stderr.decode("utf-8"))

    def backup_path(self, output: bytes) -> Path:
        for line in output.decode("utf-8").splitlines():
            if line.startswith("backup: "):
                return Path(line.removeprefix("backup: ").strip())
        self.fail("bootstrap apply did not publish its backup path")

    def assert_installed_skill_reference_walk(self, skill_path: Path) -> None:
        text = skill_path.read_text(encoding="utf-8")
        references = sorted(set(re.findall(r"`(references/[^`]+\.md)`", text)))
        self.assertTrue(references, f"no local references declared by {skill_path}")
        for reference in references:
            self.assertTrue((skill_path.parent / reference).is_file(), f"missing installed Skill reference: {reference}")

    def test_complete_bootstrap_installed_demo_and_conflict_aware_rollback(self) -> None:
        minimal_project = self.root / "minimal-codex-project"
        minimal_apply = self.run_process(
            BOOTSTRAP,
            "--destination",
            str(minimal_project),
            "--adapter",
            "codex",
            "--apply",
        )
        minimal_backup = self.backup_path(minimal_apply.stdout)
        self.assert_installed_skill_reference_walk(
            minimal_project / ".agents" / "skills" / "objective-integrity" / "SKILL.md"
        )
        self.run_process(BOOTSTRAP, "--rollback", str(minimal_backup))

        preview_project = self.root / "preview-project"
        preview = self.run_process(BOOTSTRAP, "--destination", str(preview_project), "--mode", "complete")
        self.assertIn("mode: dry-run", preview.stdout.decode("utf-8"))
        self.assertFalse(preview_project.exists())

        project = self.root / "sample-project"
        applied = self.run_process(BOOTSTRAP, "--destination", str(project), "--adapter", "codex", "--mode", "complete", "--apply")
        backup = self.backup_path(applied.stdout)
        installed_demo = project / ".oif" / "tools" / "demo.py"
        installed_check = project / ".oif" / "tools" / "check.py"
        self.assert_installed_skill_reference_walk(
            project / ".agents" / "skills" / "objective-integrity" / "SKILL.md"
        )
        self.assert_installed_skill_reference_walk(
            project / ".oif" / ".agents" / "skills" / "objective-integrity" / "SKILL.md"
        )
        # A short owned sibling keeps nested hash-addressed fixtures within
        # ordinary Windows path limits without changing host configuration.
        with tempfile.TemporaryDirectory(prefix="", dir=self.root.parent) as check_parent:
            self.run_process(installed_check, "--installed-runtime", "--temp-root", check_parent)
        installed_demo_root = self.root / "d"
        self.run_process(installed_demo, "--directory", str(installed_demo_root))
        self.assertTrue((installed_demo_root / "demo-result.json").is_file())
        self.run_process(BOOTSTRAP, "--rollback", str(backup))
        self.assertFalse(installed_demo.exists())

        conflict_project = self.root / "conflict-project"
        conflict_apply = self.run_process(BOOTSTRAP, "--destination", str(conflict_project), "--mode", "complete", "--apply")
        conflict_backup = self.backup_path(conflict_apply.stdout)
        edited = conflict_project / ".oif" / "README.md"
        edited.write_text("later user edit\n", encoding="utf-8")
        rejected = self.run_process(BOOTSTRAP, "--rollback", str(conflict_backup), expected=(2,))
        self.assertIn("Rollback conflict", rejected.stderr.decode("utf-8"))
        self.assertEqual(edited.read_text(encoding="utf-8"), "later user edit\n")
        manifest = json.loads((conflict_backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "APPLIED")


if __name__ == "__main__":
    unittest.main()
