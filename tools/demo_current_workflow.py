"""A small end-to-end source-wide example used by the isolated public demo.

The example deliberately creates one shared formatting defect, observes both
consumers before repairing either, and keeps the original findings afterward.
All files are synthetic and owned by demo.py's validated new directory. Review
references demonstrate the data contract; they are not an independent reviewer.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Callable


OIF = Path(__file__).with_name("oif.py")
OWNER = "demo-workflow-owner"
OBJECTIVE = "demo-connected-status"
ITEMS = ("label", "index")


def reference(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper()}


def owner_event(binding: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "objective-supervisor-control-v2",
        "event_id": "DEMO-" + binding["action"], "objective_id": OBJECTIVE,
        "source_sha256": source["sha256"], "owner_chat_id": OWNER,
        "work_phase": binding,
        "decision": "CORRECT" if binding["action"] == "REPAIR_FINDINGS" else "ACT",
        "objective_evidence_delta": "Advance the label and its real index consumer together.",
        "omission_consequence": "One required consumer would remain inconsistent.",
        "shorter_alternative": "Reuse the exact scope and unaffected source evidence.",
        "induced_rework": "Collect both current-stage observations before grouped repair.",
        "return_step": "Deliver the two consistently named files.",
        "later_effect": "Inspect actual files separately from structural admission.",
        "fixed_polling": False, "status_only": False,
        "progress_status": "progressing", "decision_window": None,
    }


def advisory_transition(binding: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
    """A read-only transition query, not a correction or external-action permit."""
    return {
        "schema_version": "workflow-transition-admission-v3", "receipt_id": "DEMO-TRANSITION",
        "objective_id": OBJECTIVE, "source_sha256": source["sha256"],
        "owner_role": "COORDINATED-WORK", "action_class": "trivial_read_only",
        "requested_transition": "ADVISORY", "material_candidate_action": False,
        "released_external_action": False,
        "correction_admission": {
            "applicable": False, "authority_mode": "", "authority_ref": "",
            "fcr_status": "", "fcr_ref": "", "scenario_status": "", "scenario_ref": "",
            "interaction_ids": [], "impact_status": "", "impact_ref": "",
            "omission_consequence": "", "expected_objective_delta": "",
            "cost_rework_counterfactual": "", "return_step": "",
        },
        "known_cause": {
            "applicable": False, "global_master_search_ref": "", "project_master_search_ref": "",
            "signature": "", "disposition": "", "prior_family_ref": "", "changed_premise": "",
            "discriminating_prediction": "",
        },
        "skill_effect": {
            "applicable": False, "action_finalized": False, "selected_ref": "", "bytes_ref": "",
            "script_applicable": False, "script_ref": "", "application_ref": "", "effect_ref": "",
            "repeated_mechanical_family": False, "constrained_representation": False,
            "phase": "PRE_ACTION", "outcome_capture_ref": "",
        },
        "freshness": {"required": False, "observed_at": "", "state_ref": "",
                      "dependent_claim_ids": [], "baseline_status": "NOT_APPLICABLE"},
        "monitoring": {
            "applicable": False, "decision_window_ref": "", "cursor_status": "NOT_APPLICABLE",
            "objective_evidence_delta": "", "unchanged_reuse": True,
            "progress_status": "NOT_APPLICABLE", "wait_value_ref": "", "fixed_polling": False,
            "recovery_disposition": "",
        },
        "stage_allocation": {
            "job_shape_changed": False, "view_ref": "", "disposition": "", "view_sha256": "",
            "result_ref": "", "result_file_sha256": "", "selected_configuration_id": "",
            "allocator_script_ref": "", "allocator_script_sha256": "",
        },
        "value": {"auxiliary": False, "mandatory_outcome_unproven_if_omitted": "",
                  "expected_time_saved": "", "retirement_condition": ""},
        "topology": {
            "read_only_auditor": False, "implementing_owner_role": "COORDINATED-WORK",
            "owner_task_id": OWNER, "owner_lease_id": "demo-local-run", "chat_id": OWNER,
            "parent_lease_ref": "", "resource_claims_ref": "explicit synthetic demo directory",
            "recovery_mode": "NORMAL",
        },
        "scenario_recomposition": {"applicable": False, "semantic_lock_ref": "", "relation_ids": [],
                                   "witness_ref": "", "unresolved_relation_ids": []},
        "consumer": {"required": False, "outcome_claim_ids": [], "oracle_ref": "",
                     "evidence_delta": "", "status": "NOT_APPLICABLE"},
        "proof_ceiling": "Synthetic read-only transition query; no edit or external authority.",
        "job_id": "demo-connected-files", "job_shape_sha256": binding["completion_scope_ref"]["sha256"],
        "work_phase": binding,
    }


def run_current_workflow(
    runner: Any, *, write_json: Callable, write_bytes: Callable,
) -> dict[str, Any]:
    root = runner.destination / "current-workflow"
    root.mkdir()

    def file(name: str, value: Any) -> dict[str, str]:
        path = root / name
        if isinstance(value, (dict, list)):
            write_json(path, value)
        else:
            write_bytes(path, value.encode("utf-8") if isinstance(value, str) else value)
        return reference(path)

    def cli(name: str, command: str, *args: str) -> dict[str, Any]:
        return runner.run("current-" + name, OIF, command, *args)

    def require(condition: bool, message: str) -> None:
        if not condition:
            runner.first_fault = message
            runner.publish("FAILED")
            raise ValueError(message)

    source = file("source.txt", "Create a label and an index pointing to it. Both must use the exact name Demo Project.\n")
    owner_review = file("owner-review.txt", "Synthetic scope review: both the label and index are required.\n")
    independent_review = file("review-contract-fixture.txt", "Synthetic distinct review-reference fixture only; no independent reviewer ran.\n")
    label_path, index_path = root / "label.txt", root / "index.json"
    plan = file("planned-files.json", {"label": str(label_path), "index": str(index_path)})
    scope = {
        "schema": "source-wide-scope-v1", "objective_id": OBJECTIVE, "owner_chat_id": OWNER,
        "source_ref": source,
        "requirements": [
            {"id": "name", "source_clause": "Both must use the exact name Demo Project.",
             "description": "Consistent exact display name in both files."},
            {"id": "connection", "source_clause": "an index pointing to it",
             "description": "The index points to the actual label file."},
        ],
        "implementation_items": [
            {"id": "label", "requirement_ids": ["name"], "description": "Write the label."},
            {"id": "index", "requirement_ids": ["name", "connection"], "description": "Write the connected index."},
        ],
        "checks": [
            {"id": "label-check", "requirement_ids": ["name"], "stage": "files",
             "description": "Read the actual label."},
            {"id": "index-check", "requirement_ids": ["name", "connection"], "stage": "files",
             "description": "Read the index and follow its label reference."},
        ],
        "coverage_review": {"owner_ref": owner_review, "independent_ref": independent_review},
    }
    scope_ref = file("scope.json", scope)
    state = {
        "schema": "source-wide-state-v1", "objective_id": OBJECTIVE, "owner_chat_id": OWNER,
        "source_sha256": source["sha256"], "completion_scope_ref": scope_ref,
        "phase": "BUILD", "current_stage": "files", "candidate_ref": plan,
        "implementation": [{"id": item, "status": "pending", "evidence_ref": None} for item in ITEMS],
        "checks": [{"id": item + "-check", "status": "pending", "evidence_ref": None, "reason": ""} for item in ITEMS],
        "findings": [], "findings_closed": False,
    }
    phases: list[dict[str, Any]] = []

    def select(name: str, action: str, *, items=(), checks=(), findings=()) -> dict[str, Any]:
        state_ref = file(name + "-state.json", state)
        binding = {"state_ref": state_ref, "completion_scope_ref": scope_ref, "action": action,
                   "item_ids": list(items), "check_ids": list(checks),
                   "finding_ids": list(findings), "feedback": None}
        binding_ref = file(name + "-binding.json", binding)
        event_ref = file(name + "-owner-event.json", owner_event(binding, source))
        transition_ref = file(name + "-transition.json", advisory_transition(binding, source))
        control = cli(name + "-owner-control", "control", "--input", event_ref["path"])
        transition = cli(name + "-transition", "transition", "--input", transition_ref["path"])
        require(control["decision"] == "ADMIT" and transition["work_phase"]["admitted"],
                "The source-wide demo's current action was not structurally selected")
        phases.append({"phase": state["phase"], "action": action, "binding": binding_ref,
                       "owner_control": control["decision"], "transition": transition["decision"],
                       "permission_granted": transition["work_phase"]["permission_granted"]})
        return binding

    orchestrator = cli(
        "typed-state", "allocation-io", "state", "--selected-model", "synthetic-model",
        "--selected-reasoning", "synthetic-reasoning", "--requested-model", "synthetic-model",
        "--requested-reasoning", "synthetic-reasoning", "--injectable", "false",
        "--accepted", "unavailable", "--fallback", "Demonstration only; no model selected or dispatched.",
    )
    allocation = {
        "schema_version": "stage-allocation-v2", "objective_id": OBJECTIVE,
        "source_sha256": source["sha256"], "stage_id": "files", "job_id": "demo-connected-files",
        "job_shape_sha256": scope_ref["sha256"], "trigger": "JOB_SHAPE_CHANGED",
        "current_lease_id": "demo-local-run",
        "owner": {"lane": "COORDINATED-WORK", "task_id": OWNER, "lease_id": "demo-local-run"},
        "context_reuse": {"benefit_positive": True, "evidence": "Synthetic already-initialized runner fixture.",
                          "replacement_cost": "unavailable", "current_configuration_id": "demo-current"},
        "selection_authority": {"mode": "measured-comparison", "source_clause": "Demo source only.",
                                "configuration_id": None, "evidence": "Illustrative typed selection, no measured model comparison."},
        "candidates": [{"configuration_id": "demo-current", "capability_needs": [
            {"need": "Write the two owned local fixture files", "met": True, "evidence": "Synthetic capability fixture."}],
            "representative_eval_version": "demo-v1",
            "risk_adjusted_total_cost": {"status": "unavailable", "reason": "No model cost comparison is performed."},
            "lower_configuration_failure_prediction": "Not assessed by the demo.", "orchestrator_state": orchestrator}],
        "proof_ceiling": "Typed synthetic allocator input; not current-host model discovery or model quality evidence.",
    }
    allocation_ref = file("allocation.json", allocation)
    allocation_result = cli("allocation", "allocation-io", "run", "--input", allocation_ref["path"],
                            "--evidence", str(root / "allocation-evidence.jsonl"))
    require(allocation_result["selection_ready"] and not allocation_result["dispatch_performed"],
            "The typed allocation example did not retain a non-dispatching selection")

    select("build", "IMPLEMENT", items=ITEMS)
    # This planned educational defect reaches BOTH implemented consumers before
    # the first sweep. No user or production file is being deliberately broken.
    file("label.txt", "Demo project\n")
    file("index.json", {"label": "label.txt", "display_name": "Demo project"})
    built = file("candidate-built.json", {item: reference(path) for item, path in
                                          (("label", label_path), ("index", index_path))})
    state["candidate_ref"] = built
    for item in state["implementation"]:
        item.update(status="complete", evidence_ref=built)
    state["phase"] = "SWEEP"
    select("sweep", "FORMAL_CHECK", checks=[item + "-check" for item in ITEMS])

    def observe(name: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
        label = label_path.read_text(encoding="utf-8").rstrip("\n")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        connected = index.get("label") == "label.txt" and (root / index["label"]).is_file()
        rows = [
            {"id": "label-check", "status": "pass" if label == "Demo Project" else "fail",
             "actual": label, "expected": "Demo Project"},
            {"id": "index-check", "status": "pass" if connected and index.get("display_name") == "Demo Project" else "fail",
             "actual": index, "expected": {"label": "label.txt", "display_name": "Demo Project"},
             "connected_to_actual_label": connected},
        ]
        evidence_ref = file(name + "-observations.json", rows)
        for item in state["checks"]:
            observed = next(row for row in rows if row["id"] == item["id"])
            item.update(status=observed["status"], evidence_ref=evidence_ref, reason="")
        return evidence_ref, rows

    first_ref, first_rows = observe("first-sweep")
    require(all(row["status"] == "fail" for row in first_rows),
            "The planned shared-defect demonstration did not produce the expected observations")
    state["findings"] = [
        {"id": item + "-name", "check_id": item + "-check", "classification": "mandatory",
         "basis": "The synthetic source requires the exact name Demo Project.",
         "root_cause": "shared-display-name", "consumer_item_ids": [item], "status": "open",
         "evidence_ref": first_ref} for item in ITEMS
    ]
    state.update(phase="REPAIR", findings_closed=True)
    select("repair", "REPAIR_FINDINGS", items=ITEMS, findings=[item + "-name" for item in ITEMS])
    file("label.txt", "Demo Project\n")
    file("index.json", {"label": "label.txt", "display_name": "Demo Project"})
    state["candidate_ref"] = file("candidate-repaired.json", {item: reference(path) for item, path in
                                                               (("label", label_path), ("index", index_path))})
    for finding in state["findings"]:
        finding["status"] = "resolved"
    for check in state["checks"]:
        check.update(status="pending", evidence_ref=None)
    state.update(phase="SWEEP", findings_closed=False)
    select("resweep", "FORMAL_CHECK", checks=[item + "-check" for item in ITEMS])
    _, final_rows = observe("repaired-sweep")
    require(all(row["status"] == "pass" for row in final_rows), "The connected synthetic files are still inconsistent")
    state.update(phase="ACCEPT", findings_closed=True)
    select("accept", "ACCEPT")

    # Keep completed raw evidence reachable without accumulating it in the live
    # decision view. The exact old master is published before its new pointer.
    master = root / "PROJECT_MASTER.md"
    file("PROJECT_MASTER.md", "# Demo Project Master\n\n## CURRENT CONTROL\n"
         "Next work: use the connected status example; retain its first findings and results.\n\n"
         "## P-DEMO-001\nRAW_EPISODE shared-display-name\n"
         + json.dumps({"source": source, "first_observations": first_rows,
                       "first_observations_ref": first_ref, "final_observations": final_rows,
                       "finding_disposition": "Both consumers repaired together; old evidence retained."}, indent=2)
         + "\n\n## P-DEMO-002\nRAW_EPISODE next-use: compare actual consumer consistency at the next matching request.\n")
    before_raw = master.read_bytes()
    built_index = cli("index-before", "index", "build", "--master", str(master), "--cache", str(root / "cache-before"))
    index_data = json.loads(Path(built_index["index"]).read_bytes())
    section_ids = [section["section_id"] for section in index_data["masters"][0]["sections"]
                   if section["title"] == "P-DEMO-001"]
    require(len(section_ids) == 1, "The history example could not identify its complete owned episode")
    archive = root / "history" / "completed-episode.md"
    proposal = cli("organization-proposal", "index", "propose-organization", "--master", str(master),
                   "--history", str(archive), "--section-id", str(section_ids[0]),
                   "--expected-sha256", reference(master)["sha256"])
    backing = base64.b64decode(proposal["backing_base64"])
    replacement = base64.b64decode(proposal["replacement_base64"])
    require(master.read_bytes() == before_raw == backing and not archive.exists(),
            "The exact owned history publication precondition changed")
    write_bytes(archive, backing)
    require(archive.read_bytes() == before_raw, "The complete history backing did not read back")
    write_bytes(master, replacement)
    after_index = cli("index-after", "index", "build", "--master", str(master), "--cache", str(root / "cache-after"))
    retrieved = cli("history-query", "index", "query", "--index", after_index["index"], "--term", "shared-display-name", "--legacy-output")
    require(retrieved["complete_for_explicit_query"] and any(item["source_role"] == "history" for item in retrieved["items"]),
            "The completed episode was not retrievable through its actual current/history reader")
    return {
        "schema": "oif-current-workflow-demo-v1", "source": "current-workflow/source.txt",
        "phase_sequence": [item["phase"] for item in phases], "phase_observations": phases,
        "artifacts": ["current-workflow/label.txt", "current-workflow/index.json"],
        "initial_findings": first_rows, "final_observations": final_rows,
        "original_findings_preserved": reference(Path(first_ref["path"])) == first_ref,
        "allocation": {key: allocation_result[key] for key in
                       ("decision", "selection_ready", "permission_granted", "dispatch_performed")},
        "history": {"current": "current-workflow/PROJECT_MASTER.md", "backing": "current-workflow/history/completed-episode.md",
                    "before_bytes": len(before_raw), "current_bytes": len(replacement),
                    "complete_backing_preserved": archive.read_bytes() == before_raw,
                    "archived_episode_retrieved": True},
        "review_evidence": "Synthetic structural fixtures only; no independent reviewer was simulated as real.",
        "scope": "Actual local file/readback and structural tool flow inside the explicit synthetic demo directory.",
        "permission_granted": False, "source_outcome_completed": False,
    }
