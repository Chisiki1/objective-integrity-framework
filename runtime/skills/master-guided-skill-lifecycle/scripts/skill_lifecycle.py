#!/usr/bin/env python3
"""Validate skill-effect and lifecycle-transition candidates without writing masters."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


EFFECT_OUTCOMES = {
    "advanced", "no_effect", "recurred", "false_block", "misselected",
    "prevented", "outcome_unknown", "not_observed", "not_applied", "applied", "unavailable", "not_applicable",
}
METRIC_STATUSES = {"observed", "unavailable", "not_applicable"}
METRIC_FIELDS = {
    "objective_evidence", "mandatory_quality", "elapsed_ms", "token_usage",
    "tool_calls", "delegation", "integration", "rework", "consumer_outcome",
    "overhead", "false_positive", "late_miss", "recurrence",
}
LEGAL = {
    ("raw-event", "solution"), ("solution", "candidate"), ("candidate", "shadow"),
    ("shadow", "audited"), ("audited", "active-bounded"), ("active-bounded", "measured"),
    ("measured", "revise"), ("measured", "merge"), ("measured", "supersede"),
    ("measured", "retire"), ("active-bounded", "retire"),
}
SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def nonempty(data: dict[str, Any], field: str) -> bool:
    value = data.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def require_object_fields(value: Any, name: str, fields: tuple[str, ...]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{name} must be an object"]
    return [f"{name}.{field} is required" for field in fields if not meaningful(value.get(field))]


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256.fullmatch(value))


def string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)


def validate_metrics(metrics: Any) -> list[str]:
    if not isinstance(metrics, dict):
        return ["metrics must be an object"]
    errors: list[str] = []
    missing = sorted(METRIC_FIELDS - set(metrics))
    extra = sorted(set(metrics) - METRIC_FIELDS)
    if missing:
        errors.append(f"metrics missing:{missing}")
    if extra:
        errors.append(f"unsupported metrics:{extra}")
    for name in sorted(METRIC_FIELDS & set(metrics)):
        metric = metrics[name]
        if not isinstance(metric, dict):
            errors.append(f"{name}: metric must be an object")
            continue
        status = metric.get("status")
        if status not in METRIC_STATUSES:
            errors.append(f"{name}: invalid status:{status}")
        if status == "observed" and not meaningful(metric.get("value")):
            errors.append(f"{name}: observed requires meaningful value")
        if status == "unavailable" and not meaningful(metric.get("reason")):
            errors.append(f"{name}: unavailable requires reason")
        if status == "not_applicable" and not meaningful(metric.get("reason")):
            errors.append(f"{name}: not_applicable requires reason")
        if status != "observed" and "value" in metric:
            errors.append(f"{name}: non-observed metric cannot use a placeholder value")
    return errors


def validate_effect(data: dict[str, Any]) -> tuple[list[str], str, str]:
    required = [
        "event_id", "objective_id", "source_claims", "skill_id", "skill_version",
        "registry_sha256", "selection_snapshot_sha256", "planned_objective_delta",
        "actual_objective_delta", "planned_evidence_delta", "actual_evidence_delta",
        "owner", "trigger", "candidate_identity", "independent_challenge",
        "retirement_trigger", "unknown_case_disposition", "outcome", "consumer_result",
        "side_effects", "rollback_result", "rework", "proof_ceiling",
    ]
    errors = [f"missing or empty:{field}" for field in required if field not in data or (field != "side_effects" and not nonempty(data, field))]
    outcome = data.get("outcome")
    if outcome not in EFFECT_OUTCOMES:
        errors.append(f"invalid outcome:{outcome}")
    if outcome == "prevented":
        if data.get("pre_submission_block_observed") is not True:
            errors.append("prevented requires pre_submission_block_observed=true")
        if data.get("candidate_preserved") is not True:
            errors.append("prevented requires candidate_preserved=true")
        if not is_sha256(data.get("rejected_candidate_sha256")):
            errors.append("prevented requires a 64-hex rejected_candidate_sha256")
        if data.get("applicable_set_equality") is not True:
            errors.append("prevented requires applicable_set_equality=true")
    for field in ("registry_sha256", "selection_snapshot_sha256"):
        if not is_sha256(data.get(field)):
            errors.append(f"{field} must be a 64-hex SHA-256")
    if not string_list(data.get("source_claims")):
        errors.append("source_claims must be a non-empty string array")
    if not isinstance(data.get("side_effects"), list):
        errors.append("side_effects must be an array")
    errors.extend(require_object_fields(data.get("owner"), "owner", ("lane", "writer")))
    owner = data.get("owner")
    if isinstance(owner, dict):
        if owner.get("lane") not in {"WORK", "COORDINATED-WORK"}:
            errors.append("owner.lane must be WORK or COORDINATED-WORK")
        if owner.get("lane") == "COORDINATED-WORK":
            errors.extend(require_object_fields(owner, "owner", ("task_id", "lease_id", "chat_id", "source_sha256")))
    errors.extend(require_object_fields(data.get("trigger"), "trigger", ("event_id", "family", "evidence")))
    errors.extend(require_object_fields(data.get("candidate_identity"), "candidate_identity", ("id", "version", "content_sha256")))
    candidate_identity = data.get("candidate_identity")
    if isinstance(candidate_identity, dict) and not is_sha256(candidate_identity.get("content_sha256")):
        errors.append("candidate_identity.content_sha256 must be a 64-hex SHA-256")
    challenge = data.get("independent_challenge")
    if not isinstance(challenge, dict) or challenge.get("status") not in {"completed", "pending"}:
        errors.append("independent_challenge requires status completed or pending")
    elif challenge.get("status") == "completed":
        errors.extend(require_object_fields(challenge, "independent_challenge", ("reviewer", "result", "evidence")))
    errors.extend(require_object_fields(data.get("retirement_trigger"), "retirement_trigger", ("condition", "evidence", "recovery_owner")))
    errors.extend(require_object_fields(data.get("unknown_case_disposition"), "unknown_case_disposition", ("generic_invariants", "dependent_hold", "fallback")))
    unknown = data.get("unknown_case_disposition")
    if isinstance(unknown, dict) and not string_list(unknown.get("generic_invariants")):
        errors.append("unknown_case_disposition.generic_invariants must be a non-empty string array")
    errors.extend(validate_metrics(data.get("metrics")))
    if data.get("schema_version") == "mgskill-effect-v3":
        materiality = data.get("materiality")
        errors.extend(require_object_fields(materiality, "materiality", ("material", "effect_candidate_generated", "reason")))
        if isinstance(materiality, dict):
            if not isinstance(materiality.get("material"), bool) or not isinstance(materiality.get("effect_candidate_generated"), bool):
                errors.append("materiality booleans must be explicit")
            if materiality.get("material") is True and materiality.get("effect_candidate_generated") is not True:
                errors.append("every material effect must generate a candidate disposition")
        equivalent = data.get("equivalent_skill_fingerprints")
        if not isinstance(equivalent, list) or any(not is_sha256(item) for item in equivalent):
            errors.append("equivalent_skill_fingerprints must be an array of SHA-256 values")
        observation = data.get("effect_observation")
        if outcome in {"applied", "advanced"}:
            errors.extend(require_object_fields(observation, "effect_observation", ("actual_action", "outcome_evidence", "independent_observer")))
            if isinstance(observation, dict):
                observer = observation.get("independent_observer")
                use_kind = observation.get("use_kind")
                expected_action = {"instruction": "instruction-used", "script": "script-executed", "tool": "tool-executed"}.get(use_kind)
                if expected_action is None or observation.get("actual_action") != expected_action:
                    errors.append("positive effect use_kind/action mismatch")
                if use_kind in {"script", "tool"}:
                    errors.extend(require_object_fields(observation, "effect_observation", ("bound_action_id", "bound_outcome_sha256")))
                    if not is_sha256(observation.get("bound_outcome_sha256")):
                        errors.append("script/tool positive effect requires bound_outcome_sha256")
                if not isinstance(observer, dict) or not meaningful(observer.get("identity")) or not meaningful(observer.get("observation")):
                    errors.append("positive effect requires independent observer identity/observation")
        elif observation is not None and not isinstance(observation, dict):
            errors.append("non-positive effect_observation must be an object when present")
    if outcome in {"not_observed", "outcome_unknown", "not_applied", "unavailable", "not_applicable"} and str(data.get("consumer_result", "")).upper() == "PASS":
        errors.append("unobserved/unknown/not-applied effect cannot claim consumer PASS")
    recommendation = {
        "recurred": "revise-or-merge-candidate",
        "misselected": "revise-or-merge-candidate",
        "not_applied": "revise-or-merge-candidate",
        "false_block": "narrow-or-retire-candidate",
        "no_effect": "revise-or-retire-candidate",
        "outcome_unknown": "hold-measurement",
        "not_observed": "hold-measurement",
        "unavailable": "hold-measurement",
        "not_applicable": "no-change-candidate",
    }.get(str(outcome), "measurement-pending")
    if data.get("schema_version") == "mgskill-effect-v3" and data.get("equivalent_skill_fingerprints"):
        recommendation = "merge-equivalent-skill-candidate"
    if isinstance(challenge, dict) and challenge.get("status") == "pending":
        recommendation = "hold-independent-challenge"
    return errors, "ACCEPTED_EFFECT_CANDIDATE" if not errors else "REJECTED_EFFECT_CANDIDATE", recommendation


def validate_transition(data: dict[str, Any]) -> tuple[list[str], str]:
    required = [
        "event_id", "objective_id", "source_claims", "skill_id", "from_state", "to_state",
        "reason", "baseline", "normal_path_counterexamples", "independent_challenge",
        "rollback", "identity_snapshot", "proof_ceiling", "ambiguity",
    ]
    errors = [f"missing or empty:{field}" for field in required if not nonempty(data, field)]
    transition = (data.get("from_state"), data.get("to_state"))
    if transition not in LEGAL:
        errors.append(f"illegal transition:{transition[0]}->{transition[1]}")
    if data.get("ambiguity") not in {"none", "material"}:
        errors.append("ambiguity must be none or material")
    if not string_list(data.get("source_claims")):
        errors.append("source_claims must be a non-empty string array")
    if not string_list(data.get("normal_path_counterexamples")):
        errors.append("normal_path_counterexamples must be a non-empty string array")
    errors.extend(require_object_fields(data.get("baseline"), "baseline", ("identity", "metric_definition", "comparison")))
    transition_challenge = data.get("independent_challenge")
    if not isinstance(transition_challenge, dict) or transition_challenge.get("status") not in {"completed", "pending"}:
        errors.append("independent_challenge requires status completed or pending")
    elif transition_challenge.get("status") == "completed":
        errors.extend(require_object_fields(transition_challenge, "independent_challenge", ("reviewer", "result", "evidence")))
    errors.extend(require_object_fields(data.get("rollback"), "rollback", ("target_identity", "trigger", "steps", "recovery_owner")))
    rollback = data.get("rollback")
    if isinstance(rollback, dict) and not string_list(rollback.get("steps")):
        errors.append("rollback.steps must be a non-empty string array")
    identity = data.get("identity_snapshot")
    if not isinstance(identity, dict):
        errors.append("identity_snapshot must be an object")
    else:
        for field in ("registry_sha256", "selection_snapshot_sha256", "skill_content_sha256"):
            if not is_sha256(identity.get(field)):
                errors.append(f"identity_snapshot.{field} must be a 64-hex SHA-256")
    if transition == ("active-bounded", "retire"):
        for field in ("exact_harm", "containment", "recovery_owner"):
            if not nonempty(data, field):
                errors.append(f"bounded emergency retirement missing:{field}")
    if isinstance(transition_challenge, dict) and transition_challenge.get("status") == "pending" and not errors:
        return errors, "HOLD-INDEPENDENT-CHALLENGE"
    if data.get("ambiguity") == "material" and not errors:
        return errors, "USER-DECISION"
    return errors, "ELIGIBLE_TRANSITION_CANDIDATE" if not errors else "INELIGIBLE_TRANSITION_CANDIDATE"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["effect", "transition"])
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_schemas = {"mgskill-effect-v2", "mgskill-effect-v3"} if args.kind == "effect" else {"mgskill-transition-v1"}
    errors = [] if data.get("schema_version") in expected_schemas else [f"expected one of:{sorted(expected_schemas)}"]
    recommendation = "none"
    if args.kind == "effect":
        specific, decision, recommendation = validate_effect(data)
    else:
        specific, decision = validate_transition(data)
    errors.extend(specific)
    if errors and decision != "USER-DECISION":
        decision = "REJECTED_EFFECT_CANDIDATE" if args.kind == "effect" else "INELIGIBLE_TRANSITION_CANDIDATE"
    result = {
        "schema_version": "mgskill-lifecycle-result-v2",
        "kind": args.kind,
        "input_path": str(path),
        "input_sha256": sha256_file(path),
        "decision": decision,
        "recommended_disposition": recommendation,
        "errors": sorted(set(errors)),
        "project_event_candidate": {
            "event_id": data.get("event_id"), "objective_id": data.get("objective_id"),
            "source_claims": data.get("source_claims"), "skill_id": data.get("skill_id"),
            "kind": args.kind, "decision": decision, "recommended_disposition": recommendation,
            "proof_ceiling": data.get("proof_ceiling"), "raw_input_sha256": sha256_file(path),
        },
        "global_disposition_candidate": "SANITIZED-AGGREGATE-PENDING-PARENT-WRITE",
        "writes_performed": [],
        "proof_ceiling": "schema and lifecycle/effect invariant validation only; semantic merit and consumer benefit unproven",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if not errors and decision != "USER-DECISION" else (3 if decision == "USER-DECISION" else 2)


if __name__ == "__main__":
    sys.exit(main())
