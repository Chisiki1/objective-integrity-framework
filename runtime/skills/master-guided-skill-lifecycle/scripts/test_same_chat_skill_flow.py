#!/usr/bin/env python3
"""Isolated forward-use/regression tests for the same-chat Skill flow.

Creates only a temporary fixture tree.  It never reads/writes live masters,
active discovery roots, or the candidate root outside the supplied scripts.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
LIFECYCLE = HERE / "skill_lifecycle.py"
MATERIALIZE = HERE / "materialize_skill_candidate.py"
ADOPT = HERE / "isolated_registry_adoption.py"
RESOLVER_DIR = HERE.parent.parent / "master-guided-skill-resolver" / "scripts"
BUILDER = RESOLVER_DIR / "build_skill_fact_input.py"
COMPILER = RESOLVER_DIR / "compile_skill_facts.py"
RESOLVER = RESOLVER_DIR / "resolve_skills.py"
METRICS = ("objective_evidence", "mandatory_quality", "elapsed_ms", "token_usage", "tool_calls", "delegation", "integration", "rework", "consumer_outcome", "overhead", "false_positive", "late_miss", "recurrence")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def write(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def run(command: list[str], codes: set[int] = {0}, output_file: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False, env=env)
    if completed.returncode not in codes:
        raise AssertionError(f"unexpected exit {completed.returncode}: {completed.stderr}\n{completed.stdout}")
    return json.loads(completed.stdout if completed.stdout.strip() else (output_file or Path("__missing_output__")).read_text(encoding="utf-8"))


def root_manifest(root: Path) -> str:
    rows = [f"{item.relative_to(root).as_posix()}\t{sha_file(item)}" for item in sorted(root.rglob("*")) if item.is_file()]
    return sha_bytes("\n".join(rows).encode())


def registry(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": "mgskill-registry-v1", "registry_id": "TEST-SAME-CHAT", "entries": [] if entry is None else [entry]}


def source_plan(document: Path) -> dict[str, Any]:
    return {
        "schema_version": "mgskill-fact-builder-plan-v1", "objective_id": "OBJ-SAME-CHAT-TEST",
        "current_authority": {"message": "isolated test authority"}, "source_document": {"path": str(document)},
        "source_claims": ["SRC-1"], "blind_phase": "none",
        "source_records": [{"id": "SRC-1", "disposition": "fact-bearing", "classification": "method", "intent": "exercise isolated forward-use", "mechanism": "exact fact match", "evidence": "fixture clause", "facts": {"job": ["same-chat-forward-use"]}, "excluded_facts": {}}],
        "action_records": [{"id": "ACT-1", "disposition": "fact-bearing", "evidence": "fixture action", "facts": {"job": ["same-chat-forward-use"]}, "excluded_facts": {}, "finality": "finalized", "final_payload": {"facts": {"job": ["same-chat-forward-use"]}}, "tool_schema": {"status": "not-applicable"}}],
        "negative_selection_challenge": {"status": "completed", "countermodel": "A nonmatching job must not select this skill."},
    }


def candidate_input(source: Path) -> dict[str, Any]:
    return {
        "schema_version": "mgskill-inactive-candidate-v1", "operation": "CREATE", "event_id": "EVT-1", "objective_id": "OBJ-SAME-CHAT-TEST", "source_claims": ["SRC-1"],
        "owner": {"lane": "COORDINATED-WORK", "writer": "fixture", "task_id": "task", "lease_id": "lease", "chat_id": "chat", "source_sha256": "A" * 64},
        "candidate": {"candidate_id": "same-chat-test-skill", "artifact_path": str(source), "artifact_sha256": sha_file(source), "prior_candidate_id": None, "equivalent_fingerprints": []},
        "evidence": {"source": "fixture", "selection": "future isolated resolver"}, "rollback": {"owner": "fixture", "method": "remove isolated directory"}, "independent_challenge": {"status": "completed"}, "proof_ceiling": "fixture identity only",
    }


def effect(selection: dict[str, Any], candidate_sha: str, actual_action: str, outcome: str, use_kind: str = "instruction", include_observation: bool = True) -> dict[str, Any]:
    value = {
        "schema_version": "mgskill-effect-v3", "event_id": "EVT-1", "objective_id": "OBJ-SAME-CHAT-TEST", "source_claims": ["SRC-1"], "skill_id": "SKILL-TEST-SAME-CHAT-001", "skill_version": "1.0.0",
        "registry_sha256": selection["registry_sha256"], "selection_snapshot_sha256": selection["selection_snapshot_sha256"], "planned_objective_delta": "use selected instruction", "actual_objective_delta": "artifact created", "planned_evidence_delta": "selection plus use", "actual_evidence_delta": "fixture output",
        "owner": {"lane": "COORDINATED-WORK", "writer": "fixture", "task_id": "task", "lease_id": "lease", "chat_id": "chat", "source_sha256": "A" * 64}, "trigger": {"event_id": "EVT-1", "family": "same-chat", "evidence": "fixture"},
        "candidate_identity": {"id": "same-chat-test-skill", "version": "1.0.0", "content_sha256": candidate_sha}, "independent_challenge": {"status": "completed", "reviewer": "fixture-observer", "result": "observed", "evidence": "separate fixture readback"},
        "retirement_trigger": {"condition": "negative outcome", "evidence": "future effect", "recovery_owner": "fixture"}, "unknown_case_disposition": {"generic_invariants": ["hold unknown"], "dependent_hold": "effect only", "fallback": "normal workflow"},
        "outcome": outcome, "consumer_result": "OBSERVED", "side_effects": [], "rollback_result": "not needed", "rework": "none", "proof_ceiling": "isolated fixture effect only", "metrics": {key: {"status": "observed", "value": 0} for key in METRICS},
        "materiality": {"material": True, "effect_candidate_generated": True, "reason": "fixture route"}, "equivalent_skill_fingerprints": [],
    }
    if include_observation:
        value["effect_observation"] = {"use_kind": use_kind, "actual_action": actual_action, "outcome_evidence": "isolated artifact readback", "independent_observer": {"identity": "fixture-reader", "observation": "artifact exists"}}
        if use_kind in {"script", "tool"}:
            value["effect_observation"].update({"bound_action_id": "ACT-1", "bound_outcome_sha256": sha_bytes(f"{use_kind}-outcome".encode())})
    return value


def stage_input(costs: list[dict[str, Any]], current: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "stage-allocation-v2", "objective_id": "OBJ-SAME-CHAT-TEST", "source_sha256": "A" * 64, "stage_id": "TEST", "job_id": "fixture-job", "job_shape_sha256": "B" * 64, "trigger": "JOB_SHAPE_CHANGED", "current_lease_id": "lease", "owner": {"lane": "COORDINATED-WORK", "task_id": "task", "lease_id": "lease"}, "proof_ceiling": "fixture", "selection_authority": {"mode": "measured-comparison", "source_clause": "fixture", "configuration_id": "", "evidence": "fixture"},
        "context_reuse": {"benefit_positive": True, "evidence": "fixture", "replacement_cost": "known", **({"current_configuration_id": current} if current else {})},
        "candidates": [{"configuration_id": f"cfg-{i}", "capability_needs": [{"need": "typed-script", "met": True, "evidence": "fixture"}], "representative_eval_version": "fixture", "risk_adjusted_total_cost": cost, "lower_configuration_failure_prediction": "none", "orchestrator_state": {"selected_model": "m", "selected_reasoning": "high", "injectable": True, "requested_model": "m", "requested_reasoning": "high", "accepted": "accepted", "effective_model": None, "effective_reasoning": None, "fallback": "none"}} for i, cost in enumerate(costs)],
    }


def main() -> int:
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="same-chat-skill-flow-") as raw:
        root = Path(raw)
        source_doc = write(root / "source.json", {"clauses": [{"id": "SRC-1", "text": "select a generic isolated instruction skill"}]})
        source_skill = root / "source-skill" / "SKILL.md"; source_skill.parent.mkdir()
        source_skill.write_text("---\nname: same-chat-test-skill\ndescription: Isolated test instruction.\n---\n\n# Test\n\nCreate an observed artifact.\n", encoding="utf-8")
        entry = {"skill_id": "SKILL-TEST-SAME-CHAT-001", "name": "same-chat-test-skill", "version": "1.0.0", "origin": "user", "relative_path": "same-chat-test-skill", "status": "candidate", "disclosure_class": "blind_safe_mechanical", "match_clauses": [{"job": ["same-chat-forward-use"]}], "files": [{"path": "SKILL.md", "sha256": sha_file(source_skill)}]}
        base_registry = write(root / "registry.json", registry())
        selection_registry = write(root / "selection-registry.json", registry(entry))
        plan = write(root / "plan.json", source_plan(source_doc))
        built = root / "built.json"
        run([sys.executable, str(BUILDER), "--plan", str(plan), "--registry", str(selection_registry), "--output", str(built)])
        compiled = root / "compiled.json"; resolver_input = root / "resolver-input.json"
        compiler = run([sys.executable, str(COMPILER), "--source", str(built), "--registry", str(selection_registry), "--output", str(compiled), "--resolver-input-output", str(resolver_input)], output_file=compiled)
        assert compiler["valid"] is True; results.append("canonical-builder-compiler")
        manual = write(root / "manual-resolver-input.json", {"schema_version": "mgskill-resolve-input-v1", "objective_id": "OBJ-SAME-CHAT-TEST"})
        run([sys.executable, str(COMPILER), "--source", str(manual), "--registry", str(selection_registry)], {2}); results.append("manual-input-rejected")
        malformed = json.loads(plan.read_text(encoding="utf-8")); malformed["source_records"] = []
        malformed_path = write(root / "malformed.json", malformed)
        run([sys.executable, str(BUILDER), "--plan", str(malformed_path), "--registry", str(selection_registry), "--output", str(root / "never.json")], {2}); results.append("malformed-plan-rejected")
        candidate_root = root / "inactive-candidates"; candidate_root.mkdir()
        material_input = write(root / "candidate.json", candidate_input(source_skill))
        discovery = root / "active-root"; discovery.mkdir()
        material = run([sys.executable, str(MATERIALIZE), "--input", str(material_input), "--candidate-root", str(candidate_root), "--expected-root-manifest-sha256", root_manifest(candidate_root), "--discovery-root", str(discovery), "--apply"])
        assert material["decision"] == "INACTIVE_CANDIDATE_MATERIALIZED" and Path(material["candidate_path"]).is_file(); results.append("candidate-created-readback")
        run([sys.executable, str(MATERIALIZE), "--input", str(material_input), "--candidate-root", str(candidate_root), "--expected-root-manifest-sha256", "0" * 64, "--discovery-root", str(discovery), "--apply"], {2}); results.append("candidate-stale-cas-rejected")
        adopted_root = root / "adopted"; entry_arg = json.dumps(entry)
        run([sys.executable, str(ADOPT), "--proposal", str(write(root / "material.json", material)), "--candidate-root", str(candidate_root), "--isolated-adoption-root", str(discovery), "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(base_registry), "--expected-registry-sha256", sha_file(base_registry), "--registry-entry", entry_arg, "--discovery-root", str(discovery), "--apply"], {2}); results.append("active-root-path-rejected")
        run([sys.executable, str(ADOPT), "--proposal", str(write(root / "material.json", material)), "--candidate-root", str(candidate_root), "--isolated-adoption-root", str(root / "duplicate"), "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(selection_registry), "--expected-registry-sha256", sha_file(selection_registry), "--registry-entry", entry_arg, "--discovery-root", str(discovery), "--apply"], {2}); results.append("duplicate-registry-rejected")
        original_registry_bytes = base_registry.read_bytes(); base_registry.write_text(json.dumps(registry({"skill_id": "other", "name": "other", "version": "1", "origin": "user", "relative_path": "other", "status": "candidate", "match_clauses": [{"job": ["other"]}], "files": [{"path": "SKILL.md", "sha256": "0" * 64}]})), encoding="utf-8")
        run([sys.executable, str(ADOPT), "--proposal", str(write(root / "material.json", material)), "--candidate-root", str(candidate_root), "--isolated-adoption-root", str(root / "stale-source"), "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(base_registry), "--expected-registry-sha256", sha_bytes(original_registry_bytes), "--registry-entry", entry_arg, "--discovery-root", str(discovery), "--apply"], {2}); results.append("source-registry-change-rejected")
        base_registry.write_bytes(original_registry_bytes)
        for point, committed in (("after-skill-copy", False), ("after-backup", False), ("after-registry-stage", False), ("before-rename", False), ("after-rename", True), ("after-readback", True), ("after-verified", True)):
            fault_destination = root / f"fault-{point}"; fault_env = {**os.environ, "MGSKILL_TEST_ADOPTION_FAULT": point}
            failed = run([sys.executable, str(ADOPT), "--proposal", str(write(root / "material.json", material)), "--candidate-root", str(candidate_root), "--isolated-adoption-root", str(fault_destination), "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(base_registry), "--expected-registry-sha256", sha_file(base_registry), "--registry-entry", entry_arg, "--discovery-root", str(discovery), "--apply"], {2}, env=fault_env)
            assert failed["transaction_state"] in ({"COMMITTED", "VERIFIED"} if committed else {"PREPARED", "STAGED", "COMMITTING"}) and fault_destination.exists() is committed
            if committed: assert failed["decision"] == "OUTCOME_UNKNOWN" and failed["readback"]["destination_exists"] is True
            results.append(f"adoption-fault-{point}")
        adoption = run([sys.executable, str(ADOPT), "--proposal", str(write(root / "material.json", material)), "--candidate-root", str(candidate_root), "--isolated-adoption-root", str(adopted_root), "--expected-destination-manifest-sha256", "ABSENT", "--registry", str(base_registry), "--expected-registry-sha256", sha_file(base_registry), "--registry-entry", entry_arg, "--discovery-root", str(discovery), "--apply"])
        assert adoption["decision"] == "ISOLATED_ADOPTION_STAGED"; results.append("isolated-adoption-cas-backup")
        adopted_registry = adopted_root / "registry.json"; adopted = json.loads(adopted_registry.read_text(encoding="utf-8")); adopted["entries"][0]["status"] = "active-bounded"; write(adopted_registry, adopted)  # simulated post-eligibility isolated discovery only
        selected = run([sys.executable, str(RESOLVER), "--input", str(resolver_input), "--registry", str(adopted_registry), "--user-root", str(adopted_root)])
        assert selected["decision"] == "selected" and selected["selected"][0]["canonical_path"] == str((adopted_root / "same-chat-test-skill").resolve()); results.append("fresh-natural-selection")
        artifact = root / "used-artifact.txt"; artifact.write_text("observed action outcome", encoding="utf-8")
        read_only = write(root / "read-only-effect.json", effect(selected, sha_file(source_skill), "read", "applied"))
        run([sys.executable, str(LIFECYCLE), "effect", "--input", str(read_only)], {2}); results.append("read-not-applied")
        honest_read = write(root / "honest-read-effect.json", effect(selected, sha_file(source_skill), "read", "not_applied"))
        assert run([sys.executable, str(LIFECYCLE), "effect", "--input", str(honest_read)])["decision"] == "ACCEPTED_EFFECT_CANDIDATE"; results.append("read-only-not-applied-recorded")
        used = write(root / "used-effect.json", effect(selected, sha_file(source_skill), "instruction-used", "applied"))
        effect_result = run([sys.executable, str(LIFECYCLE), "effect", "--input", str(used)])
        assert effect_result["decision"] == "ACCEPTED_EFFECT_CANDIDATE" and artifact.is_file(); results.append("used-artifact-effect")
        for kind, action in (("script", "script-executed"), ("tool", "tool-executed")):
            positive = write(root / f"{kind}-positive-effect.json", effect(selected, sha_file(source_skill), action, "advanced", use_kind=kind))
            assert run([sys.executable, str(LIFECYCLE), "effect", "--input", str(positive)])["decision"] == "ACCEPTED_EFFECT_CANDIDATE"
            results.append(f"{kind}-positive-bound-effect")
        mismatch = write(root / "script-mismatch-effect.json", effect(selected, sha_file(source_skill), "instruction-used", "advanced", use_kind="script"))
        run([sys.executable, str(LIFECYCLE), "effect", "--input", str(mismatch)], {2}); results.append("script-modality-mismatch-rejected")
        unavailable = write(root / "unavailable-effect.json", effect(selected, sha_file(source_skill), "read", "unavailable", include_observation=False))
        assert run([sys.executable, str(LIFECYCLE), "effect", "--input", str(unavailable)])["decision"] == "ACCEPTED_EFFECT_CANDIDATE"; results.append("unavailable-effect-recorded")
        for name, costs, current, expected in (("observed", [{"status": "observed", "value": 2}], None, "SELECTED"), ("estimated", [{"status": "estimated", "value": 2, "evidence": "forecast"}], None, "SELECTED"), ("tie", [{"status": "observed", "value": 2}, {"status": "observed", "value": 2}], "cfg-1", "RETAIN_CURRENT"), ("unavailable", [{"status": "observed", "value": 2}, {"status": "unavailable", "reason": "not measured"}], "cfg-0", "RETAIN_CURRENT")):
            view = write(root / f"stage-{name}.json", stage_input(costs, current))
            stage = run([sys.executable, str(HERE / "stage_allocation.py"), "--input", str(view)], {0})
            assert stage["decision"] == expected and len(stage["result_sha256"]) == 64 and "selection_basis" in stage and "cost_statuses" in stage
            results.append(f"allocation-{name}")
    print(json.dumps({"decision": "PASS", "tests": results, "proof_ceiling": "isolated fixture path only; no live installation, broad empirical improvement or user outcome claim"}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
