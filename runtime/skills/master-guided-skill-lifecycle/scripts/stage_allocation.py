#!/usr/bin/env python3
"""Select the lowest evidenced sufficient stage configuration without model rankings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")


def fail(message: str) -> int:
    print(json.dumps({"decision": "HELD", "error": message}, sort_keys=True), file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "objective_id", "source_sha256", "stage_id", "job_id", "job_shape_sha256",
        "trigger", "current_lease_id", "owner", "context_reuse", "selection_authority", "candidates", "proof_ceiling",
    }
    if not isinstance(data, dict) or set(data) != required:
        return fail("stage allocation fields differ")
    if data["schema_version"] != "stage-allocation-v2" or data["trigger"] != "JOB_SHAPE_CHANGED":
        return fail("unsupported schema or trigger")
    for field in ("source_sha256", "job_shape_sha256"):
        if not isinstance(data[field], str) or not SHA256.fullmatch(data[field]):
            return fail(f"{field} must be SHA-256")
    if not isinstance(data["job_id"], str) or not data["job_id"].strip():
        return fail("job_id must be non-empty")
    owner = data["owner"]
    required_owner = {"lane", "task_id", "lease_id"}
    if not isinstance(owner, dict) or set(owner) != required_owner or owner["lane"] not in {"WORK", "COORDINATED-WORK"} or any(not isinstance(owner[key], str) or not owner[key].strip() for key in required_owner):
        return fail("owner must bind lane/task_id/lease_id")
    context = data["context_reuse"]
    if not isinstance(context, dict) or set(context) not in ({"benefit_positive", "evidence", "replacement_cost"}, {"benefit_positive", "evidence", "replacement_cost", "current_configuration_id"}):
        return fail("context_reuse fields differ")
    if not isinstance(context["benefit_positive"], bool):
        return fail("context_reuse.benefit_positive must be boolean")
    authority = data["selection_authority"]
    if not isinstance(authority, dict) or set(authority) != {"mode", "source_clause", "configuration_id", "evidence"}:
        return fail("selection_authority fields differ")
    if authority["mode"] not in {"measured-comparison", "exact-source-selection"}:
        return fail("selection_authority.mode is invalid")
    candidates = data["candidates"]
    if not isinstance(candidates, list) or not candidates:
        return fail("candidates must be non-empty")
    admitted: list[dict[str, Any]] = []
    capable_candidates: list[dict[str, Any]] = []
    unavailable: list[str] = []
    cost_statuses: dict[str, str] = {}
    for candidate in candidates:
        fields = {"configuration_id", "capability_needs", "representative_eval_version", "risk_adjusted_total_cost", "lower_configuration_failure_prediction", "orchestrator_state"}
        if not isinstance(candidate, dict) or set(candidate) != fields:
            return fail("candidate fields differ")
        needs = candidate["capability_needs"]
        if not isinstance(needs, list) or not needs:
            return fail("capability_needs must be non-empty")
        capable = True
        for need in needs:
            if not isinstance(need, dict) or set(need) != {"need", "met", "evidence"} or not isinstance(need["met"], bool):
                return fail("invalid capability need")
            capable = capable and need["met"]
        if not capable:
            continue
        capable_candidates.append(candidate)
        state = candidate["orchestrator_state"]
        state_fields = {"selected_model", "selected_reasoning", "injectable", "requested_model", "requested_reasoning", "accepted", "effective_model", "effective_reasoning", "fallback"}
        if not isinstance(state, dict) or set(state) != state_fields:
            return fail("orchestrator_state fields differ")
        for field in ("selected_model", "selected_reasoning", "requested_model", "requested_reasoning", "fallback"):
            if not isinstance(state[field], str) or not state[field].strip():
                return fail(f"orchestrator_state.{field} must be non-empty")
        if not isinstance(state["injectable"], bool) or state["accepted"] not in {"accepted", "rejected", "unavailable"}:
            return fail("orchestrator_state injectable/accepted is invalid")
        for field in ("effective_model", "effective_reasoning"):
            if state[field] is not None and (not isinstance(state[field], str) or not state[field].strip()):
                return fail(f"orchestrator_state.{field} must be null or non-empty")
        cost = candidate["risk_adjusted_total_cost"]
        if not isinstance(cost, dict) or set(cost) not in ({"status", "value"}, {"status", "value", "evidence"}, {"status", "reason"}):
            return fail("invalid risk_adjusted_total_cost")
        if cost["status"] in {"observed", "estimated"} and isinstance(cost.get("value"), int) and cost["value"] >= 0:
            if cost["status"] == "estimated" and (not isinstance(cost.get("evidence"), str) or not cost["evidence"].strip()):
                return fail("estimated cost requires evidence")
            admitted.append(candidate)
            cost_statuses[candidate["configuration_id"]] = cost["status"]
        elif cost["status"] == "source_selected" and isinstance(cost.get("reason"), str) and cost["reason"].strip():
            if authority["mode"] != "exact-source-selection":
                return fail("source_selected cost requires exact-source-selection authority")
            cost_statuses[candidate["configuration_id"]] = "source_selected"
        elif cost["status"] == "unavailable" and isinstance(cost.get("reason"), str) and cost["reason"].strip():
            unavailable.append(candidate["configuration_id"])
            cost_statuses[candidate["configuration_id"]] = "unavailable"
        else:
            return fail("cost must be observed/estimated nonnegative integer or unavailable with reason")
    if authority["mode"] == "exact-source-selection":
        matches = [item for item in capable_candidates if item["configuration_id"] == authority["configuration_id"] and item["risk_adjusted_total_cost"]["status"] == "source_selected"]
        if len(matches) != 1 or not isinstance(authority["source_clause"], str) or not authority["source_clause"].strip() or not isinstance(authority["evidence"], str) or not authority["evidence"].strip():
            return fail("exact source selection does not bind one capable source-selected configuration")
        decision = "SOURCE_SELECTED"
        selected = matches[0]["configuration_id"]
        admitted = matches
    elif not admitted:
        current_capable = next((item for item in capable_candidates if item["configuration_id"] == context.get("current_configuration_id")), None)
        if current_capable is not None and context["benefit_positive"]:
            decision = "RETAIN_CURRENT"
            selected = current_capable["configuration_id"]
        else:
            decision = "USER-DECISION" if unavailable else "NO_CAPABLE_CONFIGURATION"
            selected = None
    elif unavailable:
        current = next((item for item in capable_candidates if item["configuration_id"] == context.get("current_configuration_id")), None)
        if current is not None and context["benefit_positive"]:
            decision = "RETAIN_CURRENT"
            selected = current["configuration_id"]
        else:
            decision = "USER-DECISION"
            selected = None
    else:
        admitted.sort(key=lambda item: (item["risk_adjusted_total_cost"]["value"], item["configuration_id"]))
        best_cost = admitted[0]["risk_adjusted_total_cost"]["value"]
        ties = [item for item in admitted if item["risk_adjusted_total_cost"]["value"] == best_cost]
        current = next((item for item in ties if item["configuration_id"] == context.get("current_configuration_id")), None)
        decision = "SELECTED" if len(ties) == 1 else ("RETAIN_CURRENT" if current is not None and context["benefit_positive"] else "USER-DECISION")
        selected = ties[0]["configuration_id"] if len(ties) == 1 else (current["configuration_id"] if decision == "RETAIN_CURRENT" else None)
    basis = {
        "SELECTED": "estimated-minimum" if selected and cost_statuses.get(selected) == "estimated" else "observed-minimum",
        "RETAIN_CURRENT": "retain-current-tie-or-unavailable",
        "SOURCE_SELECTED": "source-selection",
        "USER-DECISION": "hold",
        "NO_CAPABLE_CONFIGURATION": "hold",
    }[decision]
    result = {
        "schema_version": "stage-allocation-result-v2",
        "objective_id": data["objective_id"],
        "source_sha256": data["source_sha256"],
        "stage_id": data["stage_id"],
        "job_id": data["job_id"],
        "job_shape_sha256": data["job_shape_sha256"],
        "current_lease_id": data["current_lease_id"],
        "owner": owner,
        "decision": decision,
        "selected_configuration_id": selected,
        "selected_orchestrator_state": next((item["orchestrator_state"] for item in capable_candidates if item["configuration_id"] == selected), None),
        "reuse_current_conversation": context["benefit_positive"],
        "selection_basis": basis,
        "cost_statuses": dict(sorted(cost_statuses.items())),
        "unavailable_cost_candidates": sorted(unavailable),
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        "proof_ceiling": "typed capability admission and explicit observed/estimated cost ordering only; model quality, future behavior and consumer outcome remain empirically unproven",
    }
    result["result_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if decision in {"SELECTED", "RETAIN_CURRENT", "SOURCE_SELECTED"} else 3


if __name__ == "__main__":
    sys.exit(main())
