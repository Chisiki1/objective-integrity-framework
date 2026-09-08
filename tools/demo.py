#!/usr/bin/env python3
"""Run an isolated, synthetic Objective Integrity Framework demonstration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from path_identity import comparison_identity, within
from demo_current_workflow import run_current_workflow


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "runtime" / "objective_ledger.py"
LIFECYCLE = ROOT / "runtime" / "skills" / "master-guided-skill-lifecycle" / "scripts"
QUEUE = LIFECYCLE / "learning_queue.py"
MATERIALIZE = LIFECYCLE / "materialize_skill_candidate.py"
REPARSE_POINT = 0x400


class DemoFailure(RuntimeError):
    """A failure whose command evidence has already been preserved."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_text(path: Path, value: str) -> None:
    write_bytes(path, value.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def no_redirects(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
            raise ValueError(f"Redirected paths are not supported: {part}")


def inside(path: Path, root: Path) -> bool:
    return within(path, root)


def safe_destination(raw: str) -> Path:
    if not raw.strip():
        raise ValueError("--directory must name an explicit demo directory")
    destination = Path(os.path.abspath(os.path.expanduser(raw)))
    no_redirects(destination)
    destination = destination.resolve()
    home = Path.home().resolve()
    protected = (home, home / ".codex", home / ".agents", home / ".config", ROOT)
    if destination.parent == destination:
        raise ValueError("A filesystem root cannot be a demo directory")
    if comparison_identity(destination) == comparison_identity(home) or any(inside(destination, root) for root in protected[1:4]):
        raise ValueError("A live or global configuration root cannot be a demo directory")
    if destination == ROOT or inside(destination, ROOT) or inside(ROOT, destination):
        raise ValueError("The demo directory must be separate from the framework distribution")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("The demo destination exists and is not a directory")
        if next(destination.iterdir(), None) is not None:
            raise ValueError("The demo destination must be new or empty; existing files were not changed")
    return destination


def parse_json_output(stdout: bytes, step: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoFailure(f"{step} returned unreadable JSON; raw output is preserved") from exc
    if not isinstance(value, dict):
        raise DemoFailure(f"{step} returned a non-object JSON result")
    return value


class Runner:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.records = destination / "records"
        self.temp = destination / "tmp"
        self.record_path = destination / "run-record.json"
        self.steps: list[dict[str, Any]] = []
        self.first_fault: str | None = None

    def publish(self, state: str) -> None:
        value: dict[str, Any] = {
            "schema": "oif-runtime-demo-record-v1",
            "state": state,
            "steps": self.steps,
            "partial_effects_preserved": True,
        }
        if self.first_fault:
            value["first_fault"] = self.first_fault
        write_json(self.record_path, value)

    def run(
        self,
        name: str,
        script: Path,
        *arguments: str,
        stdin: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        if not script.is_file():
            self.first_fault = f"Required public runtime member is missing: {script.relative_to(ROOT)}"
            self.publish("FAILED")
            raise DemoFailure(self.first_fault)
        number = len(self.steps) + 1
        stdout_path = self.records / f"{number:02d}-{name}.stdout.json"
        stderr_path = self.records / f"{number:02d}-{name}.stderr.txt"
        command = [sys.executable, "-B", str(script), *arguments]
        environment = os.environ.copy()
        environment.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            "TMPDIR": str(self.temp),
        })
        result = subprocess.run(
            command,
            input=(json.dumps(stdin, ensure_ascii=False).encode("utf-8") if stdin is not None else None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=environment,
            check=False,
        )
        write_bytes(stdout_path, result.stdout)
        write_bytes(stderr_path, result.stderr)
        entry = {
            "name": name,
            "returncode": result.returncode,
            "stdout": stdout_path.relative_to(self.destination).as_posix(),
            "stderr": stderr_path.relative_to(self.destination).as_posix(),
        }
        self.steps.append(entry)
        self.publish("RUNNING")
        if result.returncode not in expected:
            self.first_fault = f"{name} exited with status {result.returncode}"
            self.publish("FAILED")
            raise DemoFailure(self.first_fault)
        try:
            return parse_json_output(result.stdout, name)
        except DemoFailure as exc:
            self.first_fault = str(exc)
            self.publish("FAILED")
            raise


def ledger_arguments(config: Path, logical_chat_id: str) -> tuple[str, ...]:
    return "--config", str(config), "--logical-chat-id", logical_chat_id


def contract_clause(source: dict[str, Any], clause_id: str, source_event_id: str) -> dict[str, str]:
    return {
        "clause_id": clause_id,
        "source_event_id": source_event_id,
        "source_ref": source["source_ref"],
        "source_sha256": source["source_sha256"],
    }


def run_demo(destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("inputs", "ledger-state", "outputs", "records", "tmp", "queue", "inactive-candidates", "simulated-discovery-root"):
        (destination / name).mkdir(parents=True, exist_ok=True)

    runner = Runner(destination)
    runner.publish("PREPARED")
    logical_chat = "demo-chat-one"
    config = destination / "inputs" / "ledger-config.json"
    source_path = destination / "inputs" / "initial-source.txt"
    write_text(
        source_path,
        "Create a concise project status card, keep its evidence visible, and do not replace the result with process activity.\n",
    )
    write_json(
        config,
        {
            "schema": "chat-objective-continuity-config-v1",
            "namespace": "oif-public-demo",
            "session_bindings": {logical_chat: {"role": "root", "logical_chat_id": logical_chat}},
            "implicit_session_ledgers": True,
            "data_root": str(destination / "ledger-state"),
            "lock_timeout_seconds": 5,
            "max_hook_input_bytes": 1048576,
            "max_prompt_bytes": 262144,
            "tool_classes": {"read": "read_only", "write": "mutating"},
        },
    )

    common = ledger_arguments(config, logical_chat)
    bootstrap = runner.run(
        "objective-capture",
        LEDGER,
        "bootstrap",
        "--session-id",
        logical_chat,
        "--source-file",
        str(source_path),
        "--delivery-id",
        "demo-initial-source",
        *common,
    )
    state = runner.run("objective-status-before-classification", LEDGER, "status", *common)["state"]
    source_event_id = bootstrap["source_event_id"]
    source = state["sources"][source_event_id]
    initial_contract = {
        "source_event_id": source_event_id,
        "disposition": "INITIAL",
        "classification_note": "Synthetic source reviewed by the demo owner.",
        "contract": {
            "contract_id": "demo-contract-v1",
            "primary_objective": "Create a concise project status card with visible evidence.",
            "mandatory_acceptance": ["The status card exists as an inspectable file."],
            "scope": {"included": ["the explicit demo directory"], "excluded": ["live projects and global configuration"]},
            "authority": {"semantic_writer": "the synthetic demo owner"},
            "constraints": ["Use synthetic English content only."],
            "prohibited_substitutes": ["A checklist without the requested status card."],
            "proof_ceiling": "Isolated synthetic demonstration.",
            "clauses": [contract_clause(source, "DEMO-C1", source_event_id)],
            "open_outcomes": [
                {"outcome_id": "DEMO-O1", "status": "OPEN", "description": "An inspectable status card exists."}
            ],
        },
    }
    classify_path = destination / "inputs" / "classify-initial.json"
    write_json(classify_path, initial_contract)
    runner.run(
        "objective-classification",
        LEDGER,
        "classify-source",
        "--input",
        str(classify_path),
        "--event-id",
        "DEMO-CLASSIFY-INITIAL",
        "--expected-head",
        state["head_hash"],
        *common,
    )

    status_card = destination / "outputs" / "status-card.txt"
    action_one = {
        "action_id": "DEMO-A1",
        "description": "Create the synthetic project status card.",
        "outcome_ids": ["DEMO-O1"],
        "contract_id": "demo-contract-v1",
        "source_clause_ids": ["DEMO-C1"],
    }
    action_one_path = destination / "inputs" / "action-one.json"
    write_json(action_one_path, action_one)
    runner.run("status-card-action-start", LEDGER, "action-start", "--input", str(action_one_path), "--event-id", "DEMO-A1-START", *common)
    write_text(status_card, "Project status\n\nOutcome: Publish a concise status card.\nResult: The synthetic status card is available for review.\n")
    status_card_ref = f"file:outputs/status-card.txt#sha256={sha256_file(status_card)}"
    runner.run(
        "status-card-action-outcome",
        LEDGER,
        "action-outcome",
        "--input",
        "-",
        "--event-id",
        "DEMO-A1-OUTCOME",
        *common,
        stdin={"action_id": "DEMO-A1", "status": "SUCCEEDED", "effect_state": "EFFECT_CONFIRMED_SUCCEEDED", "evidence_refs": [status_card_ref]},
    )
    progress_state = runner.run("status-card-input-state", LEDGER, "status", *common)["state"]
    progress_input = runner.run(
        "status-card-owner-input", ROOT / "tools" / "oif.py", "objective-input", "progress",
        "--runtime", str(LEDGER), "--runtime-sha256", sha256_file(LEDGER),
        "--expected-head", progress_state["head_hash"], *common,
        stdin={
            "outcome_updates": [{"outcome_id": "DEMO-O1", "status": "SATISFIED", "evidence_refs": [status_card_ref]}],
            "next_eligible_work": "Classify the additive source without dropping the completed outcome.",
            "proof_ceiling": "File creation and ledger evidence inside this demo only.",
        },
    )
    runner.run(
        "status-card-progress",
        LEDGER,
        "progress",
        "--input",
        "-",
        "--event-id",
        "DEMO-PROGRESS-ONE",
        "--expected-head",
        progress_state["head_hash"],
        *common,
        stdin=progress_input,
    )

    addition = "Also create a next-use note while preserving the completed status-card outcome."
    runner.run(
        "additive-source-capture",
        LEDGER,
        "hook",
        "--event",
        "UserPromptSubmit",
        *common,
        stdin={"session_id": logical_chat, "turn_id": "demo-addition", "prompt": addition},
    )
    state = runner.run("objective-status-before-addition", LEDGER, "status", *common)["state"]
    addition_event_id = state["unclassified_source_ids"][0]
    addition_source = state["sources"][addition_event_id]
    prior_clause = state["contracts"]["demo-contract-v1"]["clauses"][0]
    addition_contract = {
        "source_event_id": addition_event_id,
        "disposition": "ADD",
        "classification_note": "The second synthetic source adds one outcome and preserves the first.",
        "contract": {
            "contract_id": "demo-contract-v2",
            "parent_contract_id": "demo-contract-v1",
            "primary_objective": "Create a concise project status card with visible evidence.",
            "clauses": [prior_clause, contract_clause(addition_source, "DEMO-C2", addition_event_id)],
            "open_outcomes": [
                {"outcome_id": "DEMO-O1", "status": "OPEN", "description": "An inspectable status card exists."},
                {"outcome_id": "DEMO-O2", "status": "OPEN", "description": "An inspectable next-use note exists."},
            ],
        },
    }
    addition_path = destination / "inputs" / "classify-addition.json"
    write_json(addition_path, addition_contract)
    runner.run("addition-classification", LEDGER, "classify-source", "--input", str(addition_path), "--event-id", "DEMO-CLASSIFY-ADD", "--expected-head", state["head_hash"], *common)

    next_use_note = destination / "outputs" / "next-use-note.txt"
    runner.run(
        "next-use-action-start",
        LEDGER,
        "action-start",
        "--input",
        "-",
        "--event-id",
        "DEMO-A2-START",
        *common,
        stdin={
            "action_id": "DEMO-A2",
            "description": "Create the additive next-use note.",
            "outcome_ids": ["DEMO-O2"],
            "contract_id": "demo-contract-v2",
            "source_clause_ids": ["DEMO-C2"],
        },
    )
    write_text(next_use_note, "Next use\n\nWhen the same objective family appears, retrieve the prepared candidate by its exact objective, family, and trigger.\n")
    next_use_ref = f"file:outputs/next-use-note.txt#sha256={sha256_file(next_use_note)}"
    runner.run(
        "next-use-action-outcome",
        LEDGER,
        "action-outcome",
        "--input",
        "-",
        "--event-id",
        "DEMO-A2-OUTCOME",
        *common,
        stdin={"action_id": "DEMO-A2", "status": "SUCCEEDED", "effect_state": "EFFECT_CONFIRMED_SUCCEEDED", "evidence_refs": [next_use_ref]},
    )
    runner.run(
        "next-use-progress",
        LEDGER,
        "progress",
        "--input",
        "-",
        "--event-id",
        "DEMO-PROGRESS-TWO",
        *common,
        stdin={
            "contract_id": "demo-contract-v2",
            "outcome_updates": [{"outcome_id": "DEMO-O2", "status": "SATISFIED", "evidence_refs": [next_use_ref]}],
            "next_eligible_work": "Prepare an owned improvement candidate and query its exact next-use trigger.",
            "proof_ceiling": "Synthetic file and ledger effects inside this demo only.",
        },
    )
    final_state = runner.run("objective-final-status", LEDGER, "status", *common)["state"]
    if {key: item["status"] for key, item in final_state["open_outcomes"].items()} != {
        "DEMO-O1": "SATISFIED",
        "DEMO-O2": "SATISFIED",
    }:
        runner.first_fault = "The additive objective state did not preserve both completed outcomes"
        runner.publish("FAILED")
        raise DemoFailure(runner.first_fault)

    candidate_source = destination / "inputs" / "candidate-source" / "SKILL.md"
    write_text(
        candidate_source,
        "---\nname: evidence-linked-next-use\ndescription: Keep a completed outcome visible when adding the next eligible outcome.\n---\n\n# Evidence-linked next use\n\nBefore adding a new outcome, retain the prior outcome ID, status, and evidence references. Record the next exact retrieval trigger without claiming a measured benefit before later use.\n",
    )
    candidate_root = destination / "inactive-candidates"
    source_sha = sha256_file(source_path)
    proposal = {
        "schema_version": "mgskill-inactive-candidate-v1",
        "operation": "CREATE",
        "event_id": "DEMO-CANDIDATE-MATERIALIZE",
        "objective_id": "demo-objective-v1",
        "source_claims": [f"sha256:{source_sha}"],
        "owner": {"lane": "COORDINATED-WORK", "task_id": logical_chat, "lease_id": "demo-lease", "chat_id": logical_chat, "source_sha256": source_sha},
        "candidate": {
            "candidate_id": "evidence-linked-next-use",
            "artifact_path": str(candidate_source),
            "artifact_sha256": sha256_file(candidate_source),
            "prior_candidate_id": None,
            "equivalent_fingerprints": [],
        },
        "evidence": {"ledger_projection": "ledger-state/*/objective.txt", "completed_outcomes": ["DEMO-O1", "DEMO-O2"]},
        "rollback": {"method": "remove only the new inactive candidate directory"},
        "independent_challenge": {
            "status": "pending",
            "next_action": "Obtain a genuinely independent read-only challenge before materialization.",
            "evidence": "inputs/candidate-source/SKILL.md",
        },
        "proof_ceiling": "Owned unreviewed draft identity and pending materializer routing only.",
    }
    proposal_path = destination / "inputs" / "candidate-proposal.json"
    write_json(proposal_path, proposal)
    preview = runner.run(
        "candidate-materialization-preview",
        MATERIALIZE,
        "--input",
        str(proposal_path),
        expected=(3,),
    )
    expected_hold = "INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE"
    if (
        preview.get("decision") != expected_hold
        or preview.get("writes_performed") != []
        or next(candidate_root.iterdir(), None) is not None
    ):
        runner.first_fault = "The pending-review preview did not remain a non-writing independent-challenge hold"
        runner.publish("FAILED")
        raise DemoFailure(runner.first_fault)
    candidate_path = candidate_source
    candidate_sha = sha256_file(candidate_path)
    preview_record = runner.steps[-1]["stdout"]

    queue_db = destination / "queue" / "learning.sqlite3"
    runner.run("learning-queue-initialize", QUEUE, "init", "--db", str(queue_db))
    queue_base = {
        "schema_version": "learning-queue-event-v1",
        "candidate_id": "evidence-linked-next-use",
        "owner_ref": f"demo-owner:{logical_chat}",
        "objective_ref": "demo-objective-v1",
        "source_refs": [f"sha256:{source_sha}"],
        "project_event_ref": "demo:event:owned-unreviewed-draft",
        "global_disposition_ref": "demo:isolated-no-global-write",
        "family_keys": ["objective-outcome-continuity"],
        "artifact_ref": {"path": str(candidate_path), "sha256": candidate_sha},
        "next_consumer_ref": "demo:independent-review-before-next-use",
        "next_use_trigger": "independent-review-before-next-use",
        "evidence_refs": ["records:objective-final-status", f"file:{preview_record}"],
        "rollback_refs": ["demo-directory:remove-owned-draft"],
    }
    discovered = {**queue_base, "event_id": "DEMO-QUEUE-DISCOVERED", "expected_revision": 0, "state": "discovered"}
    discovered_path = destination / "inputs" / "queue-discovered.json"
    write_json(discovered_path, discovered)
    runner.run("learning-candidate-discovered", QUEUE, "upsert", "--db", str(queue_db), "--input", str(discovered_path))
    prepared = {**queue_base, "event_id": "DEMO-QUEUE-PREPARED", "expected_revision": 1, "state": "prepared"}
    prepared_path = destination / "inputs" / "queue-prepared.json"
    write_json(prepared_path, prepared)
    runner.run("learning-candidate-prepared", QUEUE, "upsert", "--db", str(queue_db), "--input", str(prepared_path))
    due = runner.run(
        "learning-next-use-query",
        QUEUE,
        "due",
        "--db",
        str(queue_db),
        "--objective-ref",
        "demo-objective-v1",
        "--family-key",
        "objective-outcome-continuity",
        "--trigger",
        "independent-review-before-next-use",
    )
    if due.get("match_count") != 1 or due["matches"][0]["candidate_id"] != "evidence-linked-next-use":
        runner.first_fault = "The exact next-use query did not return the prepared candidate"
        runner.publish("FAILED")
        raise DemoFailure(runner.first_fault)

    current_workflow = run_current_workflow(runner, write_json=write_json, write_bytes=write_bytes)
    result = {
        "schema": "oif-runtime-demo-result-v1",
        "objective": "Create a concise project status card with visible evidence.",
        "source": {"kind": "synthetic", "sha256": source_sha},
        "outcomes": {
            key: {"status": item["status"], "evidence_refs": item.get("evidence_refs", [])}
            for key, item in sorted(final_state["open_outcomes"].items())
        },
        "work_artifacts": ["outputs/status-card.txt", "outputs/next-use-note.txt"],
        "improvement_candidate": {
            "state": "prepared",
            "review_status": "pending",
            "materialized": False,
            "artifact": candidate_path.relative_to(destination).as_posix(),
            "sha256": candidate_sha,
            "next_use_match_count": due["match_count"],
            "materializer_preview": {
                "decision": preview["decision"],
                "returncode": 3,
                "writes_performed": preview["writes_performed"],
                "receipt": preview_record,
            },
            "effect_disposition": "The owned draft awaits genuine independent review before materialization or later matching use; broader benefit is not claimed.",
        },
        "evidence": {"run_record": "run-record.json", "command_records": "records/", "learning_queue": "queue/learning.sqlite3"},
        "scope": "All source, state, artifacts, and queue data are synthetic and contained in the explicit demo directory.",
        "current_workflow": current_workflow,
    }
    write_json(destination / "demo-result.json", result)
    runner.publish("COMPLETED")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create and run a self-contained synthetic objective-continuity and improvement demo."
    )
    parser.add_argument("--directory", required=True, help="Explicit new or empty directory for all demo effects.")
    args = parser.parse_args(argv)
    destination: Path | None = None
    try:
        destination = safe_destination(args.directory)
        result = run_demo(destination)
        print("Objective integrity demo completed.")
        print("Objective: two synthetic outcomes were preserved across an additive update.")
        print("Work evidence: outputs/status-card.txt and outputs/next-use-note.txt")
        print(f"Improvement: one owned draft is queued pending independent review; exact next-use matches: {result['improvement_candidate']['next_use_match_count']}")
        print("Current workflow: connected files built, findings collected, shared cause repaired, original history retained.")
        print("Details: demo-result.json and run-record.json")
        return 0
    except (OSError, ValueError, KeyError, TypeError, DemoFailure) as exc:
        print(f"demo: {exc}", file=sys.stderr)
        if destination is not None and destination.exists():
            print(f"Partial effects and command output remain in: {destination}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
