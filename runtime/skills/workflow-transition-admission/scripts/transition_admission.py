#!/usr/bin/env python3
"""Deterministic structural admission for material workflow transitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
ACTION_CLASSES = {"trivial_read_only", "material_diagnosis", "binding_correction", "finalized_dependent_action", "monitoring_decision", "commit_push", "ci_dispatch", "external_apply"}
TRANSITIONS = {"ADVISORY", "DIAGNOSE", "EDIT", "EXECUTE", "WAIT", "REPLACE", "USER_DECISION"}
ROOT_FIELDS = {
    "schema_version", "receipt_id", "objective_id", "source_sha256", "owner_role",
    "action_class", "requested_transition", "material_candidate_action", "released_external_action",
    "correction_admission", "known_cause", "skill_effect", "freshness", "monitoring",
    "stage_allocation", "value", "topology", "scenario_recomposition", "consumer", "proof_ceiling",
}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def exact(obj: Any, fields: set[str], label: str, errors: list[str]) -> bool:
    if not isinstance(obj, dict) or set(obj) != fields:
        errors.append(f"{label}: fields differ")
        return False
    return True


def require_refs(obj: dict[str, Any], names: tuple[str, ...], prefix: str, errors: list[str]) -> None:
    for name in names:
        if not nonempty(obj.get(name)):
            errors.append(f"{prefix}.{name}: non-empty reference required")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    v3 = isinstance(data, dict) and data.get('schema_version') == 'workflow-transition-admission-v3'
    v2 = isinstance(data, dict) and data.get("schema_version") in {"workflow-transition-admission-v2", "workflow-transition-admission-v3"}
    root_fields = ROOT_FIELDS | ({"job_id", "job_shape_sha256"} if v2 else set())
    if v3: root_fields |= {'work_phase'}
    if not exact(data, root_fields, "root", errors):
        return emit(path, "HELD_INVALID", errors, [])
    if data["schema_version"] not in {"workflow-transition-admission-v1", "workflow-transition-admission-v2", "workflow-transition-admission-v3"}:
        errors.append("schema_version: unsupported")
    if not nonempty(data["receipt_id"]) or not nonempty(data["objective_id"]):
        errors.append("receipt_id/objective_id: non-empty required")
    if not isinstance(data["source_sha256"], str) or not SHA256.fullmatch(data["source_sha256"]):
        errors.append("source_sha256: SHA-256 required")
    if data["owner_role"] not in ({"COORDINATED-WORK", "BOUNDED-SUBAGENT", "DIRECT"} if v2 else {"SUPERVISOR", "WORKER", "DIRECT"}):
        errors.append("owner_role: invalid")
    if data["action_class"] not in ACTION_CLASSES or data["requested_transition"] not in TRANSITIONS:
        errors.append("action_class/requested_transition: invalid")
    for name in ("material_candidate_action", "released_external_action"):
        if not isinstance(data[name], bool):
            errors.append(f"{name}: boolean required")
    if not nonempty(data["proof_ceiling"]):
        errors.append("proof_ceiling: non-empty required")
    if v2 and (not nonempty(data["job_id"]) or not SHA256.fullmatch(str(data["job_shape_sha256"]))):
        errors.append("job_id/job_shape_sha256: bound job identity required")

    correction = data["correction_admission"]
    c_fields = {"applicable", "authority_mode", "authority_ref", "fcr_status", "fcr_ref", "scenario_status", "scenario_ref", "interaction_ids", "impact_status", "impact_ref", "omission_consequence", "expected_objective_delta", "cost_rework_counterfactual", "return_step"}
    if exact(correction, c_fields, "correction_admission", errors):
        if not isinstance(correction["applicable"], bool) or not isinstance(correction["interaction_ids"], list):
            errors.append("correction_admission: applicable boolean and interaction_ids list required")

    known = data["known_cause"]
    k_fields = {"applicable", "global_master_search_ref", "project_master_search_ref", "signature", "disposition", "prior_family_ref", "changed_premise", "discriminating_prediction"}
    if exact(known, k_fields, "known_cause", errors) and not isinstance(known["applicable"], bool):
        errors.append("known_cause.applicable: boolean required")

    skill = data["skill_effect"]
    s_fields = {"applicable", "action_finalized", "selected_ref", "bytes_ref", "script_applicable", "script_ref", "application_ref", "effect_ref", "repeated_mechanical_family", "constrained_representation"}
    if v2:
        s_fields |= {"phase", "outcome_capture_ref"}
    if exact(skill, s_fields, "skill_effect", errors):
        for name in ("applicable", "action_finalized", "script_applicable", "repeated_mechanical_family", "constrained_representation"):
            if not isinstance(skill[name], bool):
                errors.append(f"skill_effect.{name}: boolean required")

    freshness = data["freshness"]
    f_fields = {"required", "observed_at", "state_ref", "dependent_claim_ids", "baseline_status"}
    if exact(freshness, f_fields, "freshness", errors):
        if not isinstance(freshness["required"], bool) or not isinstance(freshness["dependent_claim_ids"], list):
            errors.append("freshness: required boolean and dependent_claim_ids list required")
        if freshness["baseline_status"] not in {"CURRENT", "STALE", "UNKNOWN", "NOT_APPLICABLE"}:
            errors.append("freshness.baseline_status: invalid")

    monitoring = data["monitoring"]
    m_fields = {"applicable", "decision_window_ref", "cursor_status", "objective_evidence_delta", "unchanged_reuse", "progress_status", "wait_value_ref", "fixed_polling", "recovery_disposition"}
    if exact(monitoring, m_fields, "monitoring", errors):
        for name in ("applicable", "unchanged_reuse", "fixed_polling"):
            if not isinstance(monitoring[name], bool):
                errors.append(f"monitoring.{name}: boolean required")
        if monitoring["cursor_status"] not in {"CHANGED", "UNCHANGED", "EMPTY", "STALE", "NOT_APPLICABLE"}:
            errors.append("monitoring.cursor_status: invalid")
        if monitoring["progress_status"] not in {"PROGRESSING", "STALLED", "UNKNOWN", "NOT_APPLICABLE"}:
            errors.append("monitoring.progress_status: invalid")

    stage = data["stage_allocation"]
    stage_fields = {"job_shape_changed", "view_ref", "disposition"}
    if v2:
        stage_fields |= {"view_sha256", "result_ref", "result_file_sha256", "selected_configuration_id", "allocator_script_ref", "allocator_script_sha256"}
    if exact(stage, stage_fields, "stage_allocation", errors) and not isinstance(stage["job_shape_changed"], bool):
        errors.append("stage_allocation.job_shape_changed: boolean required")
    value = data["value"]
    if exact(value, {"auxiliary", "mandatory_outcome_unproven_if_omitted", "expected_time_saved", "retirement_condition"}, "value", errors) and not isinstance(value["auxiliary"], bool):
        errors.append("value.auxiliary: boolean required")

    topology = data["topology"]
    t_fields = {"supervisor_internal_implementer", "read_only_auditor", "implementing_owner_role", "formal_worker_task_ref", "recovery_mode"}
    if v2:
        t_fields = {"read_only_auditor", "implementing_owner_role", "owner_task_id", "owner_lease_id", "chat_id", "parent_lease_ref", "resource_claims_ref", "recovery_mode"}
    if exact(topology, t_fields, "topology", errors):
        for name in (("read_only_auditor",) if v2 else ("supervisor_internal_implementer", "read_only_auditor")):
            if not isinstance(topology[name], bool):
                errors.append(f"topology.{name}: boolean required")
        if topology["implementing_owner_role"] not in ({"COORDINATED-WORK", "BOUNDED-SUBAGENT", "DIRECT", "NONE"} if v2 else {"FORMAL_WORKER", "DIRECT", "NONE", "SUPERVISOR", "INTERNAL_SUBAGENT"}):
            errors.append("topology.implementing_owner_role: invalid")
        if topology["recovery_mode"] not in {"NORMAL", "DURABLE_STATUS", "FORMAL_WORKER_RECOVERY", "FORMAL_WORKER_REPLACEMENT", "NOT_APPLICABLE", "JOURNAL_RECONCILED", "LEGACY_LEASE_RECONCILED"}:
            errors.append("topology.recovery_mode: invalid")

    recomposition = data["scenario_recomposition"]
    r_fields = {"applicable", "semantic_lock_ref", "relation_ids", "witness_ref", "unresolved_relation_ids"}
    if exact(recomposition, r_fields, "scenario_recomposition", errors):
        if not isinstance(recomposition["applicable"], bool) or not isinstance(recomposition["relation_ids"], list) or not isinstance(recomposition["unresolved_relation_ids"], list):
            errors.append("scenario_recomposition: applicable boolean and relation arrays required")

    consumer = data["consumer"]
    o_fields = {"required", "outcome_claim_ids", "oracle_ref", "evidence_delta", "status"}
    if exact(consumer, o_fields, "consumer", errors):
        if not isinstance(consumer["required"], bool) or not isinstance(consumer["outcome_claim_ids"], list):
            errors.append("consumer: required boolean and outcome_claim_ids list required")
        if consumer["status"] not in {"PASS", "UNPROVEN", "NOT_APPLICABLE"}:
            errors.append("consumer.status: invalid")

    holds: list[str] = []
    phase = {'source_wide_evaluated': False, 'proof_ceiling': 'legacy transition; no source-wide selection'}
    if v3 and not errors:
        try:
            module_path = Path(__file__).resolve().parents[2]/'master-guided-skill-lifecycle/scripts/work_phase.py'
            module = types.ModuleType('transition_work_phase'); module.__file__ = str(module_path)
            exec(compile(module_path.read_bytes(), str(module_path), 'exec'), module.__dict__)
            phase = module.evaluate(data['work_phase'], objective_id=data['objective_id'],
                source_sha256=data['source_sha256'], owner_chat_id=topology['chat_id'])
            if not phase['admitted']: holds.append('SOURCE_WIDE_PHASE_HOLD')
            action = data['work_phase']['action']
            if data['requested_transition'] == 'EDIT' and action not in {'IMPLEMENT', 'REPAIR_FINDINGS'}:
                holds.append('EDIT_REQUIRES_IMPLEMENTATION_OR_GROUPED_REPAIR')
            if action == 'CONSTRUCTION_FEEDBACK' and (data['released_external_action'] or data['action_class'] in {'external_apply','commit_push','ci_dispatch','binding_correction'}):
                holds.append('CONSTRUCTION_FEEDBACK_CANNOT_RECLASSIFY_EXTERNAL_OR_REGRESSION_ACTION')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append('work_phase: '+str(exc))
    # Include the actual decision in every later result, including a held action.
    emit.phase = phase
    if not errors:
        if data["action_class"] == "trivial_read_only" and not data["material_candidate_action"]:
            if not holds: return emit(path, "ADMIT_LIGHTWEIGHT", [], [])
        binding_classes = {"binding_correction", "finalized_dependent_action", "commit_push", "ci_dispatch", "external_apply"}
        binding_material = data["action_class"] in binding_classes
        if binding_material:
            if data["material_candidate_action"] is not True:
                holds.append("MATERIAL_TRANSITION_FLAG_FALSE")
            if not correction["applicable"]:
                holds.append("CORRECTION_ADMISSION_NOT_APPLICABLE")
            if correction["authority_mode"] not in {"EXACT_SOURCE", "INDEPENDENT_REFUTATION"}:
                holds.append("CORRECTION_AUTHORITY_MISSING")
            require_refs(correction, ("authority_ref", "fcr_ref", "scenario_ref", "impact_ref", "omission_consequence", "expected_objective_delta", "cost_rework_counterfactual", "return_step"), "correction_admission", holds)
            if correction["fcr_status"] != "CLOSED":
                holds.append("FCR_NOT_CLOSED")
            if correction["scenario_status"] != "FROZEN" or not correction["interaction_ids"] or not all(nonempty(v) for v in correction["interaction_ids"]):
                holds.append("SCENARIO_INTERACTION_NOT_FROZEN")
            if correction["impact_status"] != "PLANNED":
                holds.append("IMPACT_NOT_PLANNED")
            if v2:
                if topology["implementing_owner_role"] not in {"COORDINATED-WORK", "BOUNDED-SUBAGENT"} or data["owner_role"] != topology["implementing_owner_role"]:
                    holds.append("BOUND_EXECUTION_OWNER_MISSING")
                require_refs(topology, ("owner_task_id", "owner_lease_id", "chat_id", "resource_claims_ref"), "topology", holds)
                if topology["implementing_owner_role"] == "COORDINATED-WORK" and topology["owner_task_id"] != topology["chat_id"]:
                    holds.append("CHAT_OWNER_IDENTITY_MISMATCH")
                if topology["implementing_owner_role"] == "BOUNDED-SUBAGENT" and not nonempty(topology["parent_lease_ref"]):
                    holds.append("PARENT_LEASE_MISSING")
                if topology["read_only_auditor"]:
                    holds.append("AUDITOR_CANNOT_IMPLEMENT_REVIEWED_CANDIDATE")
            elif topology["implementing_owner_role"] != "FORMAL_WORKER" or not nonempty(topology["formal_worker_task_ref"]):
                holds.append("FORMAL_WORKER_EDIT_OWNER_MISSING")
            if not recomposition["applicable"]:
                holds.append("SCENARIO_RECOMPOSITION_NOT_BOUND")
            else:
                require_refs(recomposition, ("semantic_lock_ref", "witness_ref"), "scenario_recomposition", holds)
                if not recomposition["relation_ids"] or not all(nonempty(v) for v in recomposition["relation_ids"]):
                    holds.append("SCENARIO_RELATIONS_MISSING")
                if recomposition["unresolved_relation_ids"]:
                    holds.append("SCENARIO_RELATIONS_UNRESOLVED")
            if not consumer["required"]:
                holds.append("CONSUMER_TARGET_NOT_BOUND")
            else:
                require_refs(consumer, ("oracle_ref", "evidence_delta"), "consumer", holds)
                if not consumer["outcome_claim_ids"] or not all(nonempty(v) for v in consumer["outcome_claim_ids"]):
                    holds.append("CONSUMER_CLAIMS_MISSING")

        if not v2 and (topology["supervisor_internal_implementer"] or topology["implementing_owner_role"] in {"SUPERVISOR", "INTERNAL_SUBAGENT"}):
            holds.append("ROLE_TOPOLOGY_VIOLATION")

        if known["applicable"]:
            require_refs(known, ("global_master_search_ref", "project_master_search_ref", "signature", "disposition"), "known_cause", holds)
            if known["disposition"] not in {"APPLY_RECORDED_ROUTE", "RETEST", "NOT_SAME_FAMILY", "INVALIDATED"}:
                holds.append("KNOWN_CAUSE_UNDISPOSITIONED")
            if known["disposition"] == "RETEST" and (not nonempty(known["changed_premise"]) or not nonempty(known["discriminating_prediction"])):
                holds.append("KNOWN_CAUSE_RETEST_NOT_DISCRIMINATING")

        if skill["applicable"] and skill["action_finalized"]:
            if v2:
                if skill["phase"] not in {"PRE_ACTION", "POST_ACTION"}:
                    holds.append("SKILL_PHASE_INVALID")
                names = ("selected_ref", "bytes_ref", "application_ref", "outcome_capture_ref")
                if skill["phase"] == "POST_ACTION":
                    names += ("effect_ref",)
                require_refs(skill, names, "skill_effect", holds)
                if skill["phase"] == "PRE_ACTION" and nonempty(skill["effect_ref"]):
                    holds.append("FUTURE_EFFECT_CANNOT_AUTHORIZE_ACTION")
            else:
                require_refs(skill, ("selected_ref", "bytes_ref", "application_ref", "effect_ref"), "skill_effect", holds)
            if skill["script_applicable"] and not nonempty(skill["script_ref"]):
                holds.append("SKILL_SCRIPT_REF_MISSING")
            if skill["repeated_mechanical_family"] and not skill["constrained_representation"]:
                holds.append("REPEATED_MECHANICAL_FAMILY_RAW_ACTION")

        if data["released_external_action"] or data["action_class"] == "external_apply":
            if data["action_class"] == "external_apply" and data["released_external_action"] is not True:
                holds.append("EXTERNAL_APPLY_FLAG_FALSE")
            if not freshness["required"]:
                holds.append("JIT_FRESHNESS_NOT_REQUIRED")
            require_refs(freshness, ("observed_at", "state_ref"), "freshness", holds)
            if not freshness["dependent_claim_ids"] or not all(nonempty(v) for v in freshness["dependent_claim_ids"]):
                holds.append("JIT_DEPENDENT_CLAIMS_MISSING")
            if freshness["baseline_status"] != "CURRENT":
                return emit(path, "REPLAN", [], holds + ["ACTION_STATE_NOT_CURRENT"])

        if monitoring["applicable"]:
            require_refs(monitoring, ("decision_window_ref", "wait_value_ref"), "monitoring", holds)
            if monitoring["fixed_polling"]:
                holds.append("FIXED_POLLING_FORBIDDEN")
            if monitoring["cursor_status"] == "UNCHANGED" and not monitoring["unchanged_reuse"]:
                holds.append("UNCHANGED_EVIDENCE_NOT_REUSED")
            if monitoring["progress_status"] in {"STALLED", "UNKNOWN"} or monitoring["cursor_status"] in {"EMPTY", "STALE"}:
                if monitoring["recovery_disposition"] not in {"CORRECT", "REPLACE", "USER_DECISION", "WAIT_WITH_BOUND_DECISION_WINDOW"}:
                    holds.append("RECOVERY_DISPOSITION_MISSING")

        if stage["job_shape_changed"]:
            require_refs(stage, ("view_ref", "disposition"), "stage_allocation", holds)
            if v2:
                validate_stage_result(stage, data, holds)
        if value["auxiliary"]:
            require_refs(value, ("mandatory_outcome_unproven_if_omitted", "expected_time_saved", "retirement_condition"), "value", holds)

    if errors:
        return emit(path, "HELD_INVALID", errors, holds)
    if holds:
        decision = "WORK_NEEDS_ATTENTION" if data["owner_role"] == "WORKER" and data["material_candidate_action"] else "HELD_DEPENDENT_TRANSITION"
        return emit(path, decision, [], holds)
    if monitoring["applicable"] and data["requested_transition"] == "WAIT" and monitoring["progress_status"] == "PROGRESSING":
        return emit(path, "WAIT", [], [])
    return emit(path, "ADMIT", [], [])


def validate_stage_result(stage: dict[str, Any], transition: dict[str, Any], holds: list[str]) -> None:
    """Bind actual allocator input and result bytes; a self-authored label is not enough."""
    try:
        view = Path(stage["view_ref"]).read_bytes()
        raw = Path(stage["result_ref"]).read_bytes()
        view_hash = hashlib.sha256(view).hexdigest().upper()
        if view_hash != stage["view_sha256"].upper() or hashlib.sha256(raw).hexdigest().upper() != stage["result_file_sha256"].upper():
            raise ValueError("file hash mismatch")
        result = json.loads(raw)
        view_data = json.loads(view)
        if view_data.get("schema_version") != "stage-allocation-v2":
            raise ValueError("typed allocation v2 input required")
        expected_owner = {"lane": transition["owner_role"], "task_id": transition["topology"]["owner_task_id"], "lease_id": transition["topology"]["owner_lease_id"]}
        for field in ("objective_id", "source_sha256", "job_id", "job_shape_sha256"):
            if view_data.get(field) != transition[field] or result.get(field) != transition[field]:
                raise ValueError("allocation/current transition mismatch: " + field)
        if view_data.get("owner") != expected_owner or result.get("owner") != expected_owner:
            raise ValueError("allocation/current owner mismatch")
        allocator = Path(__file__).resolve().parents[2] / "master-guided-skill-lifecycle/scripts/stage_allocation.py"
        if Path(stage["allocator_script_ref"]).resolve(strict=True) != allocator.resolve(strict=True) or hashlib.sha256(allocator.read_bytes()).hexdigest().upper() != stage["allocator_script_sha256"].upper():
            raise ValueError("allocator identity mismatch")
        rerun = subprocess.run([sys.executable, "-B", str(allocator), "--input", stage["view_ref"]], capture_output=True, check=False)
        if rerun.returncode != 0 or json.loads(rerun.stdout) != result:
            raise ValueError("result differs from actual typed allocator decision")
        if Path(stage["view_ref"]).read_bytes() != view:
            raise ValueError("allocation input changed during consumption")
        declared = result.pop("result_sha256")
        expected = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()
        if declared != expected or result.get("schema_version") != "stage-allocation-result-v2" or result.get("input_sha256") != view_hash:
            raise ValueError("result/input identity mismatch")
        if result.get("decision") not in {"SELECTED", "RETAIN_CURRENT", "SOURCE_SELECTED"}:
            raise ValueError("allocation did not admit a configuration")
        if result["decision"] != stage["disposition"] or result.get("selected_configuration_id") != stage["selected_configuration_id"]:
            raise ValueError("selection projection differs from result")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        holds.append(f"STAGE_RESULT_NOT_BOUND:{exc}")


def emit(path: Path, decision: str, errors: list[str], holds: list[str]) -> int:
    result = {
        "schema_version": "workflow-transition-admission-result-v1",
        "decision": decision,
        "errors": sorted(set(errors)),
        "holds": sorted(set(holds)),
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        "work_phase": getattr(emit, 'phase', {'source_wide_evaluated': False}),
        "proof_ceiling": "structural presence, enum, identity-format and bounded cross-field consistency only; semantic source, root cause, scenario, impact, freshness, Skill effect and objective outcome remain unproven",
    }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if decision in {"ADMIT", "ADMIT_LIGHTWEIGHT", "WAIT"} else 3


if __name__ == "__main__":
    sys.exit(main())
