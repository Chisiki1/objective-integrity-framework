#!/usr/bin/env python3
"""Standalone synthetic regressions for the legacy durable-status adapter."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("durable_status.py")


def run(*args: str, expected: tuple[int, ...] = (0,)) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode not in expected:
        raise AssertionError((args, completed.returncode, completed.stdout, completed.stderr))
    payload = completed.stdout if completed.returncode == 0 else completed.stderr
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AssertionError((args, completed.returncode, completed.stdout, completed.stderr)) from exc


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def request() -> dict:
    return {
        "schema_version": "durable-supervisor-status-write-request-v2",
        "expected_previous_status_id": None,
        "expected_previous_record_sha256": None,
        "objective": {
            "objective_id": "OBJ-FIXTURE-001",
            "source": "synthetic-source.json",
            "source_sha256": "A" * 64,
            "supervisor_task_id": "REVIEW-TASK-001",
        },
        "worker": {
            "task_id": "IMPLEMENTATION-TASK-001",
            "turn_id": "TURN-001",
            "lease_id": "LEASE-001",
        },
        "candidate_identity": {"identity_id": "CANDIDATE-001", "sha256": "B" * 64},
        "evidence_identity": {"identity_id": "EVIDENCE-001", "sha256": "C" * 64},
        "active_step": "Preserve one decision-bearing compatibility status.",
        "actual_evidence_delta": ["The provider projection is stale at this decision boundary."],
        "pending_decision": {
            "state": "NEEDS_SUPERVISOR",
            "decision_id": "DECISION-001",
            "reason": "The named transition needs an exact later decision.",
            "requested_transition_id": "TRANSITION-001",
        },
        "next_proposed_action": {
            "transition_id": "TRANSITION-001",
            "description": "Perform only the later identity-bound transition.",
            "effect": "MATERIAL_OR_EXTERNAL",
        },
        "allowed_effects": ["READ_ONLY_EVIDENCE", "STATUS_WRITE"],
        "prohibited_effects": ["ACTIVE_WRITE", "EXTERNAL_ACTION", "IMPLEMENTER_TO_REVIEWER_PUSH"],
        "open_deliverables": [{
            "deliverable_id": "DELIVERABLE-001",
            "state": "BLOCKED",
            "summary": "Await the exact later transition decision.",
        }],
        "state": "WORK_NEEDS_ATTENTION",
        "transition_effect": "STATUS_ONLY",
        "supervisor_message": None,
        "supervision_context": {
            "stage": "Decision-bearing compatibility stage.",
            "action_or_wait_owner": "The implementation task owns the current status.",
            "last_decision_delta": "Projection freshness changed the next decision.",
            "next_decision_condition": "An exact later identity-bound message is observed.",
            "held_transition": "TRANSITION-001",
            "expected_value": "Keep the decision recoverable without a push channel.",
            "elapsed": "Unavailable in this fixture.",
            "metric_availability": {
                "elapsed": "UNAVAILABLE",
                "tokens": "UNAVAILABLE",
                "cost": "UNAVAILABLE",
                "rework": "UNAVAILABLE",
            },
            "decision_window": {
                "owner": "review pull owner",
                "dependency": "exact local status identity",
                "last_decision_delta": "provider projection became stale",
                "predicted_next_event": "the exact status is consumed once",
                "predicted_window": "event-driven",
                "wait_cost": "one bounded local status write and read",
                "replacement_integration_cost": "must be measured before replacement",
                "maximum_harm": "the dependent transition remains held",
                "alternate_route": "continue unrelated read-only work",
            },
            "no_push": True,
        },
        "proof_ceiling": "Local compatibility status structure only.",
    }


def main() -> int:
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="oif-durable-status-") as raw:
        root = Path(raw)
        initial_path = write(root / "initial.json", request())

        preflight = run("preflight", "--worker-root", str(root), "--request", str(initial_path))
        assert preflight["state"] == "VALID"
        assert not (root / ".oif-supervision-status").exists()
        checks.append("preflight-is-read-only")

        created = run("write", "--worker-root", str(root), "--request", str(initial_path))
        assert created["sequence"] == 1
        assert Path(created["status_path"]).parent.name == ".oif-supervision-status"
        checks.append("write-uses-neutral-status-root")

        readback = run("read", "--worker-root", str(root))
        verified = run("verify", "--worker-root", str(root))
        assert readback["journal_records"] == 1 and readback["projection"] == "CURRENT_MATCHES_JOURNAL"
        assert verified["latest_record_sha256"] == created["record_sha256"]
        checks.append("read-and-verify-do-not-repair")

        dropped = copy.deepcopy(request())
        dropped["expected_previous_status_id"] = created["status_id"]
        dropped["expected_previous_record_sha256"] = created["record_sha256"]
        dropped["open_deliverables"] = []
        dropped_path = write(root / "dropped.json", dropped)
        rejected = run(
            "preflight",
            "--worker-root",
            str(root),
            "--request",
            str(dropped_path),
            expected=(2,),
        )
        assert "open deliverable lineage dropped" in rejected["error"]
        assert run("verify", "--worker-root", str(root))["latest_sequence"] == 1
        checks.append("open-deliverable-drop-rejected-without-append")

        evidence = {
            "schema_version": "durable-supervisor-projection-evidence-v1",
            "projection": {
                "source_kind": "provider-read-task",
                "thread_id": "TASK-001",
                "turn_id": "TURN-001",
                "thread_status": "active",
                "turn_status": "running",
                "items_count": 0,
                "updated_at_utc": "2026-01-01T00:00:00Z",
                "observed_at_utc": "2026-01-01T00:00:10Z",
                "last_decision_delta_at_utc": "2026-01-01T00:00:00Z",
                "contradiction_facts": [],
                "expected_result_markers": [],
                "raw_store_visibility": [],
                "cursor_state": "stale",
                "intentional_no_output": False,
            },
            "decision_boundary": {
                "decision_bearing": True,
                "owner": "review pull owner",
                "next_decision_condition": "consume the durable status",
                "maximum_harm": "the dependent transition remains held",
                "alternate_route": "continue unrelated read-only work",
                "decision_window_ms": 1000,
            },
            "proof_ceiling": "Synthetic projection selection only.",
        }
        selected = run("select", "--evidence", str(write(root / "projection.json", evidence)))
        assert selected["decision"] == "SELECT"
        checks.append("generic-provider-token-selects-mechanical-fallback")

    print(json.dumps({"passed": len(checks), "failed": 0, "checks": checks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
