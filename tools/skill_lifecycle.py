#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")
EFFECT_OUTCOMES = {
    "advanced",
    "no_effect",
    "recurred",
    "false_block",
    "misselected",
    "prevented",
    "outcome_unknown",
    "not_observed",
}
FORWARD_TRANSITIONS = {
    ("raw-event", "solution"),
    ("solution", "candidate"),
    ("candidate", "shadow"),
    ("shadow", "audited"),
    ("audited", "active-bounded"),
    ("active-bounded", "measured"),
    ("measured", "revise"),
    ("measured", "merge"),
    ("measured", "supersede"),
    ("measured", "retire"),
}


def require(data: dict[str, Any], fields: list[str], failures: list[str]) -> None:
    for field in fields:
        if field not in data:
            failures.append(f"missing {field}")


def require_hash(data: dict[str, Any], field: str, failures: list[str]) -> None:
    value = data.get(field, "")
    if not isinstance(value, str) or not HEX64.match(value):
        failures.append(f"{field} must be a 64-hex SHA-256")


def validate_effect(data: dict[str, Any]) -> tuple[str, list[str]]:
    failures: list[str] = []
    require(
        data,
        [
            "event_id",
            "objective_id",
            "source_claims",
            "skill_id",
            "skill_version",
            "registry_sha256",
            "selection_snapshot_sha256",
            "planned_objective_delta",
            "actual_objective_delta",
            "planned_evidence_delta",
            "actual_evidence_delta",
            "outcome",
            "consumer_result",
            "side_effects",
            "rollback_result",
            "elapsed_ms",
            "tool_calls",
            "rework",
            "proof_ceiling",
        ],
        failures,
    )
    require_hash(data, "registry_sha256", failures)
    require_hash(data, "selection_snapshot_sha256", failures)
    if data.get("outcome") not in EFFECT_OUTCOMES:
        failures.append("outcome is not recognized")
    if not data.get("source_claims"):
        failures.append("source_claims must be non-empty")
    if data.get("outcome") == "prevented":
        if data.get("pre_submission_block_observed") is not True:
            failures.append("prevented requires pre_submission_block_observed=true")
        if data.get("candidate_preserved") is not True:
            failures.append("prevented requires candidate_preserved=true")
        require_hash(data, "rejected_candidate_sha256", failures)
    decision = "PASS" if not failures else "FAIL"
    return decision, failures


def validate_transition(data: dict[str, Any]) -> tuple[str, list[str]]:
    failures: list[str] = []
    require(
        data,
        [
            "event_id",
            "objective_id",
            "source_claims",
            "skill_id",
            "from_state",
            "to_state",
            "reason",
            "baseline",
            "source_bound_benefit",
            "representative_replay_or_shadow",
            "normal_path_counterexamples",
            "independent_challenge",
            "bounded_activation_scope",
            "rollback",
            "retirement_trigger",
            "measurement",
            "identity_snapshot",
            "proof_ceiling",
            "ambiguity",
        ],
        failures,
    )
    if data.get("ambiguity") == "material":
        return "USER-DECISION", ["material ambiguity remains"]
    if (data.get("from_state"), data.get("to_state")) not in FORWARD_TRANSITIONS:
        failures.append("transition is not a legal forward lifecycle transition")
    if not data.get("source_claims"):
        failures.append("source_claims must be non-empty")
    if not data.get("normal_path_counterexamples"):
        failures.append("normal_path_counterexamples must be non-empty")
    for field in ["source_bound_benefit", "representative_replay_or_shadow", "bounded_activation_scope", "retirement_trigger", "measurement"]:
        if not data.get(field):
            failures.append(f"{field} must be present and non-empty")
    snapshot = data.get("identity_snapshot", {})
    if not isinstance(snapshot, dict):
        failures.append("identity_snapshot must be an object")
    else:
        for field in ["registry_sha256", "selection_snapshot_sha256", "skill_content_sha256"]:
            require_hash(snapshot, field, failures)
    decision = "PASS" if not failures else "FAIL"
    return decision, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Skill Book effect and lifecycle transition records.")
    parser.add_argument("mode", choices=["effect", "transition"])
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    decision, findings = validate_effect(data) if args.mode == "effect" else validate_transition(data)
    print(json.dumps({"decision": decision, "findings": findings}, indent=2, sort_keys=True))
    return 0 if decision == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
