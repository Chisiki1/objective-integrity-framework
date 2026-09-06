#!/usr/bin/env python3
"""Bounded deterministic tests for the Master-Guided Skill Book candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


OUTPUT_ENCODINGS: dict[str, str] = {}


def run_captured(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    resolved = shutil.which(command[0], path=(env if env is not None else os.environ).get("PATH"))
    if resolved is None:
        raise AssertionError(f"executable unavailable before execution: {command[0]}")
    executable = str(Path(resolved).resolve())
    exact_command = [executable, *command[1:]]
    encoding = "utf-8"
    if Path(command[0]).stem.casefold() in {"powershell", "pwsh"}:
        if executable not in OUTPUT_ENCODINGS:
            probe = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding.CodePage"], capture_output=True, check=False, env=env)
            if probe.returncode != 0:
                raise AssertionError(f"PowerShell encoding probe failed: {probe.stderr!r}")
            code_page = int(probe.stdout.decode("ascii").strip())
            OUTPUT_ENCODINGS[executable] = "utf-8" if code_page == 65001 else f"cp{code_page}"
        encoding = OUTPUT_ENCODINGS[executable]
    "".encode(encoding)  # Reject an unsupported codec before executing the tested member.
    raw = subprocess.run(exact_command, capture_output=True, check=False, env=env)
    try:
        stdout, stderr = raw.stdout.decode(encoding), raw.stderr.decode(encoding)
    except UnicodeDecodeError as exc:
        raise AssertionError(f"output decode failed after exit={raw.returncode}, encoding={encoding}: {exc}; stdout={raw.stdout!r}; stderr={raw.stderr!r}") from exc
    return subprocess.CompletedProcess(command, raw.returncode, stdout, stderr)


def run_json(command: list[str], expected_codes: set[int]) -> tuple[dict[str, Any], int, str]:
    completed = run_captured(command)
    if completed.returncode not in expected_codes:
        raise AssertionError(f"unexpected exit {completed.returncode}: {completed.stderr}\n{completed.stdout}")
    try:
        return json.loads(completed.stdout), completed.returncode, completed.stderr
    except json.JSONDecodeError as exc:
        raise AssertionError(f"invalid JSON output: {exc}\n{completed.stdout}\n{completed.stderr}") from exc


def write_json(directory: Path, name: str, value: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return path


def make_directory_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        command = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:MGSKILL_LINK -Target $env:MGSKILL_TARGET | Out-Null"
        environment = os.environ.copy()
        environment["MGSKILL_LINK"] = str(link)
        environment["MGSKILL_TARGET"] = str(target)
        completed = run_captured(
            ["powershell", "-NoProfile", "-Command", command],
            env=environment,
        )
        if completed.returncode != 0:
            raise AssertionError(f"could not create directory link: {completed.stderr}\n{completed.stdout}")


def remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


def resolver_input(**updates: Any) -> dict[str, Any]:
    value = {
        "schema_version": "mgskill-resolve-input-v1",
        "objective_id": "OBJ-TEST-001",
        "source_claims": ["SRC-TEST"],
        "job": [],
        "action": [],
        "tool": [],
        "environment": ["windows"],
        "cause_families": [],
        "permissions": ["read-local"],
        "resources": [],
        "consumers": [],
        "risks": [],
        "blind_phase": "none",
        "now_utc": "2026-08-30T00:00:00Z",
    }
    value.update(updates)
    return value


def effect_metrics() -> dict[str, Any]:
    names = (
        "objective_evidence", "mandatory_quality", "elapsed_ms", "token_usage", "tool_calls",
        "delegation", "integration", "rework", "consumer_outcome", "overhead",
        "false_positive", "late_miss", "recurrence",
    )
    return {name: {"status": "observed", "value": 0} for name in names}


def skill_files(skill_root: Path) -> list[dict[str, str]]:
    return [
        {
            "path": path.relative_to(skill_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        }
        for path in sorted(
            (item for item in skill_root.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        )
    ]


def registry_entry(
    *,
    skill_id: str,
    name: str,
    origin: str,
    relative_path: str,
    disclosure_class: str,
    match_clauses: list[dict[str, list[str]]],
    files: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "skill_id": skill_id,
        "name": name,
        "version": "1.0.0",
        "origin": origin,
        "relative_path": relative_path,
        "status": "active-bounded",
        "disclosure_class": disclosure_class,
        "match_clauses": match_clauses,
        "required_authority": ["explicit local fixture authority"],
        "required_inputs": ["finalized synthetic input"],
        "resource_claims": ["temporary fixture tree only"],
        "expected_delta": "exercise the selected bounded fixture",
        "proof_ceiling": "synthetic identity and behavior regression only",
        "cost": "one temporary local fixture",
        "revalidation": ["input, registry, root, or member change"],
        "rollback": "remove the temporary fixture tree",
        "master_links": ["G-EVT-TEST-SKILLBOOK-001", "P-EVT-TEST-SKILLBOOK-002"],
        "files": files,
    }


def create_synthetic_fixture(temp: Path, runtime_skills: Path) -> dict[str, str]:
    """Create domain-neutral roots, registry, and records for standalone tests."""
    user_root = temp / "user-skills"
    project_root = temp / "project-skills"
    user_root.mkdir()
    project_root.mkdir()

    powershell_root = user_root / "powershell-exact-action"
    lifecycle_root = user_root / "master-guided-skill-lifecycle"
    shutil.copytree(runtime_skills / "powershell-exact-action", powershell_root)
    shutil.copytree(runtime_skills / "master-guided-skill-lifecycle", lifecycle_root)

    project_skill = project_root / "sandbox-release-review"
    project_skill.mkdir()
    (project_skill / "SKILL.md").write_text(
        "---\nname: sandbox-release-review\n"
        "description: Review one synthetic release in an isolated sandbox.\n"
        "---\nReview the exact supplied fixture only.\n",
        encoding="utf-8",
    )

    entries = [
        registry_entry(
            skill_id="SKILL-USER-POWERSHELL-EXACT-ACTION-001",
            name="powershell-exact-action",
            origin="user",
            relative_path="powershell-exact-action",
            disclosure_class="blind_safe_mechanical",
            match_clauses=[{
                "job": ["command-preflight"],
                "action": ["execute-powershell"],
                "tool": ["powershell"],
                "cause_families": ["POWERSHELL::FOREACH-PIPE-PARSER"],
            }],
            files=skill_files(powershell_root),
        ),
        registry_entry(
            skill_id="SKILL-PROJECT-SANDBOX-REVIEW-001",
            name="sandbox-release-review",
            origin="project",
            relative_path="sandbox-release-review",
            disclosure_class="post-blind-semantic",
            match_clauses=[{
                "job": ["staged-review"],
                "action": ["review-sample-release"],
                "environment": ["sandbox"],
                "resources": ["sample-package"],
            }],
            files=skill_files(project_skill),
        ),
    ]
    registry_path = write_json(
        temp,
        "synthetic-registry.json",
        {
            "schema_version": "mgskill-registry-v1",
            "registry_id": "OIF-SYNTHETIC-TEST-REGISTRY",
            "entries": entries,
        },
    )
    global_master = temp / "global-record.md"
    project_master = temp / "project-record.md"
    global_master.write_text("# Global fixture\nG-EVT-TEST-SKILLBOOK-001\n", encoding="utf-8")
    project_master.write_text("# Project fixture\nP-EVT-TEST-SKILLBOOK-002\n", encoding="utf-8")
    return {
        "registry": str(registry_path),
        "user_root": str(user_root),
        "project_root": str(project_root),
        "global_master": str(global_master),
        "project_master": str(project_master),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry")
    parser.add_argument("--user-root")
    parser.add_argument("--project-root")
    parser.add_argument("--global-master")
    parser.add_argument("--project-master")
    args = parser.parse_args()

    supplied = [
        args.registry,
        args.user_root,
        args.project_root,
        args.global_master,
        args.project_master,
    ]
    if any(supplied) and not all(supplied):
        parser.error("provide all explicit fixture paths or none")

    here = Path(__file__).resolve().parent
    resolver = here / "resolve_skills.py"
    compiler = here / "compile_skill_facts.py"
    inventory_v2 = here / "inventory_live_masters_v2.py"
    validator = here / "validate_registry.py"
    tests: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: Any) -> None:
        if not condition:
            raise AssertionError(f"{name}: {detail}")
        tests.append({"name": name, "status": "PASS", "detail": detail})

    with tempfile.TemporaryDirectory(prefix="mgskill-tests-") as temp_name:
        temp = Path(temp_name)
        if not all(supplied):
            fixture = create_synthetic_fixture(temp, here.parents[1])
            args.registry = fixture["registry"]
            args.user_root = fixture["user_root"]
            args.project_root = fixture["project_root"]
            args.global_master = fixture["global_master"]
            args.project_master = fixture["project_master"]
        ps_script = Path(args.user_root) / "powershell-exact-action" / "scripts" / "Test-PowerShellExactAction.ps1"
        lifecycle = Path(args.user_root) / "master-guided-skill-lifecycle" / "scripts" / "skill_lifecycle.py"
        source_registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
        test_registry = json.loads(json.dumps(source_registry))
        for entry in test_registry["entries"]:
            entry["status"] = "active-bounded"
        test_registry_path = write_json(temp, "test-registry.json", test_registry)
        positive = write_json(temp, "positive.json", resolver_input(
            job=["command-preflight"], action=["execute-powershell"], tool=["powershell"],
            cause_families=["POWERSHELL::FOREACH-PIPE-PARSER"], resources=["final-command"]
        ))
        result, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
        ], {0})
        selected_ids = [item["skill_id"] for item in result["selected"]]
        check("resolver-positive-powershell", "SKILL-USER-POWERSHELL-EXACT-ACTION-001" in selected_ids, selected_ids)

        snapshot = result["selection_snapshot_sha256"]
        result2, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--expect-snapshot", snapshot,
        ], {0})
        check("resolver-stable-snapshot", result2["selection_snapshot_sha256"] == snapshot, snapshot)

        stale, code, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--expect-snapshot", "0" * 64,
        ], {2})
        check("resolver-stale-snapshot", code == 2 and stale["stale_snapshot"], stale["errors"])

        missing = write_json(temp, "missing.json", resolver_input(job=["unregistered-job"]))
        fallback, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(missing), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
        ], {0})
        check("resolver-normal-fallback", fallback["decision"] == "fallback_normal_workflow", fallback["decision"])
        required_rejected = {
            "skill_id", "name", "version", "origin", "canonical_path", "status", "disclosure_class",
            "reasons", "required_authority", "required_inputs", "resource_claims", "expected_delta",
            "proof_ceiling", "cost", "revalidation", "rollback", "master_links", "files",
            "content_sha256", "directory_set_sha256",
        }
        check(
            "resolver-rejected-identity-complete",
            bool(fallback["rejected"]) and all(required_rejected <= set(item) for item in fallback["rejected"]),
            [sorted(set(required_rejected) - set(item)) for item in fallback["rejected"]],
        )

        blind = write_json(temp, "blind.json", resolver_input(
            job=["staged-review"], action=["review-sample-release"],
            environment=["sandbox"], resources=["sample-package"], blind_phase="initial"
        ))
        blind_result, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(blind), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
        ], {0})
        blind_serialized = json.dumps(blind_result, ensure_ascii=False, sort_keys=True)
        check(
            "resolver-blind-metadata-withheld",
            blind_result["withheld_count"] >= 1
            and "SKILL-PROJECT-SANDBOX-REVIEW-001" not in blind_serialized
            and "sandbox-release-review" not in blind_serialized
            and all(set(item) == {"withheld", "reason_code", "release_condition"} for item in blind_result["withheld"]),
            blind_result["withheld"],
        )

        copied_user = temp / "copied-user"
        copied_project = temp / "copied-project"
        shutil.copytree(args.user_root, copied_user)
        shutil.copytree(args.project_root, copied_project)
        (copied_user / "powershell-exact-action" / "UNMANIFESTED.txt").write_text("unexpected", encoding="utf-8")
        extra_result, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", str(copied_user), "--project-root", str(copied_project),
        ], {0})
        ps_rejection = next(item for item in extra_result["rejected"] if item["skill_id"] == "SKILL-USER-POWERSHELL-EXACT-ACTION-001")
        check("resolver-unmanifested-file-rejected", any("unmanifested" in reason for reason in ps_rejection["reasons"]), ps_rejection)

        reparse_user = temp / "reparse-user"
        reparse_project = temp / "reparse-project"
        shutil.copytree(args.user_root, reparse_user)
        shutil.copytree(args.project_root, reparse_project)
        reparse_link = reparse_user / "powershell-exact-action"
        reparse_target = reparse_user / "powershell-exact-action-target-a"
        reparse_link.rename(reparse_target)
        make_directory_link(reparse_link, reparse_target)
        reparse_result, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", str(reparse_user), "--project-root", str(reparse_project),
        ], {0})
        reparse_selected = next(item for item in reparse_result["selected"] if item["skill_id"] == "SKILL-USER-POWERSHELL-EXACT-ACTION-001")
        check(
            "resolver-in-root-reparse-disclosed",
            reparse_selected["reparse_present"]
            and any(item["lexical_path"] == str(reparse_link) and item["resolved_target"] == str(reparse_target.resolve()) for item in reparse_selected["reparse_components"]),
            reparse_selected["reparse_components"],
        )

        first_reparse_snapshot = reparse_result["selection_snapshot_sha256"]
        reparse_target_b = reparse_user / "powershell-exact-action-target-b"
        shutil.copytree(reparse_target, reparse_target_b)
        remove_directory_link(reparse_link)
        make_directory_link(reparse_link, reparse_target_b)
        retarget_result, code, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", str(reparse_user), "--project-root", str(reparse_project),
            "--expect-snapshot", first_reparse_snapshot,
        ], {2})
        check(
            "resolver-reparse-retarget-stale",
            code == 2 and retarget_result["stale_snapshot"] and "selection snapshot mismatch" in retarget_result["errors"],
            retarget_result["errors"],
        )

        escape_user = temp / "escape-user"
        escape_project = temp / "escape-project"
        shutil.copytree(args.user_root, escape_user)
        shutil.copytree(args.project_root, escape_project)
        escape_link = escape_user / "powershell-exact-action"
        escape_target = temp / "outside-powershell-target"
        escape_link.rename(escape_target)
        make_directory_link(escape_link, escape_target)
        escape_result, _, _ = run_json([
            sys.executable, str(resolver), "--input", str(positive), "--registry", str(test_registry_path),
            "--user-root", str(escape_user), "--project-root", str(escape_project),
        ], {0})
        escape_rejection = next(item for item in escape_result["rejected"] if item["skill_id"] == "SKILL-USER-POWERSHELL-EXACT-ACTION-001")
        check(
            "resolver-reparse-root-escape-rejected",
            any("canonical path escapes origin root" in reason for reason in escape_rejection["reasons"])
            and escape_rejection["reparse_present"],
            escape_rejection,
        )

        valid_registry, _, _ = run_json([
            sys.executable, str(validator), "--registry", str(test_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {0})
        check("registry-structural", valid_registry["valid"], valid_registry["errors"])

        retired_registry = json.loads(json.dumps(test_registry))
        retired_registry["entries"].append({
            "skill_id": "SKILL-RETIRED-TEST-001", "name": "retired-test", "version": "1.0.0",
            "origin": "user", "relative_path": "retired-test", "status": "retired",
            "disclosure_class": "post-blind-semantic", "match_clauses": [{"job": ["never"]}],
            "files": [], "master_links": ["G-EVT-TEST-SKILLBOOK-001"]
        })
        retired_registry_path = write_json(temp, "retired-registry.json", retired_registry)
        retired_result, code, _ = run_json([
            sys.executable, str(validator), "--registry", str(retired_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {2})
        check("registry-retired-evidence-required", code == 2 and any("missing evidence_path" in item for item in retired_result["errors"]), retired_result["errors"])

        evidence_dir = temp / "retired-evidence"
        evidence_dir.mkdir()
        evidence_file = evidence_dir / "SKILL.md"
        evidence_file.write_text("retired evidence", encoding="utf-8")
        evidence_sha = hashlib.sha256(evidence_file.read_bytes()).hexdigest().upper()
        retired_entry = {
            "skill_id": "SKILL-RETIRED-TEST-002", "name": "retired-test-2", "version": "1.0.0",
            "origin": "user", "relative_path": "retired-test-2", "status": "retired",
            "disclosure_class": "post-blind-semantic", "match_clauses": [{"job": ["never"]}],
            "files": [], "master_links": ["G-EVT-TEST-SKILLBOOK-001"],
            "evidence_files": [{"path": "SKILL.md", "sha256": evidence_sha}],
            "lineage": {"reason": "test retirement", "effective_utc": "2026-08-30T00:00:00Z", "prior_status": "measured"}
        }
        relative_registry = json.loads(json.dumps(test_registry))
        relative_entry = json.loads(json.dumps(retired_entry))
        relative_entry["evidence_path"] = "retired-evidence"
        relative_registry["entries"].append(relative_entry)
        relative_registry_path = write_json(temp, "relative-retired-registry.json", relative_registry)
        relative_result, code, _ = run_json([
            sys.executable, str(validator), "--registry", str(relative_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {2})
        check("registry-retired-relative-path-rejected", code == 2 and any("must be absolute" in item for item in relative_result["errors"]), relative_result["errors"])

        valid_retired_registry = json.loads(json.dumps(test_registry))
        valid_retired_entry = json.loads(json.dumps(retired_entry))
        valid_retired_entry["evidence_path"] = str(evidence_dir.resolve())
        valid_retired_registry["entries"].append(valid_retired_entry)
        valid_retired_registry_path = write_json(temp, "valid-retired-registry.json", valid_retired_registry)
        valid_retired_result, code, _ = run_json([
            sys.executable, str(validator), "--registry", str(valid_retired_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {0})
        check("registry-retired-absolute-evidence-normal-path", code == 0 and valid_retired_result["valid"], valid_retired_result["errors"])

        superseded_registry = json.loads(json.dumps(test_registry))
        superseded_entry = json.loads(json.dumps(retired_entry))
        superseded_entry.update({
            "skill_id": "SKILL-SUPERSEDED-TEST-001", "name": "superseded-test",
            "relative_path": "superseded-test", "status": "superseded",
            "evidence_path": str(evidence_dir.resolve())
        })
        superseded_registry["entries"].append(superseded_entry)
        superseded_registry_path = write_json(temp, "superseded-missing-replacement.json", superseded_registry)
        superseded_result, code, _ = run_json([
            sys.executable, str(validator), "--registry", str(superseded_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {2})
        check("registry-superseded-replacement-required", code == 2 and any("missing replacement identity" in item for item in superseded_result["errors"]), superseded_result["errors"])

        valid_superseded_registry = json.loads(json.dumps(test_registry))
        valid_superseded_entry = json.loads(json.dumps(superseded_entry))
        valid_superseded_entry["lineage"]["replacement"] = {
            "skill_id": "SKILL-USER-POWERSHELL-EXACT-ACTION-001", "version": "1.0.0", "origin": "user"
        }
        valid_superseded_registry["entries"].append(valid_superseded_entry)
        valid_superseded_registry_path = write_json(temp, "valid-superseded-registry.json", valid_superseded_registry)
        valid_superseded_result, code, _ = run_json([
            sys.executable, str(validator), "--registry", str(valid_superseded_registry_path),
            "--user-root", args.user_root, "--project-root", args.project_root,
            "--master", args.global_master, "--master", args.project_master,
        ], {0})
        check("registry-superseded-replacement-normal-path", code == 0 and valid_superseded_result["valid"], valid_superseded_result["errors"])

        blocked, code, _ = run_json([
            "powershell", "-NoProfile", "-File", str(ps_script),
            "-CommandText", "foreach ($x in 1..2) { $x } | ConvertTo-Json", "-IncludeSource",
        ], {2})
        check("powershell-foreach-block", code == 2 and blocked["decision"] == "BLOCK", blocked)

        safe, code, _ = run_json([
            "powershell", "-NoProfile", "-File", str(ps_script),
            "-CommandText", "$items = foreach ($x in 1..2) { $x }; $items | ConvertTo-Json",
        ], {0})
        check("powershell-intermediate-safe", code == 0 and safe["decision"] == "PASS", safe)

        quoted, code, _ = run_json([
            "powershell", "-NoProfile", "-File", str(ps_script),
            "-CommandText", "Write-Output 'foreach ($x in 1..2) { $x } | ConvertTo-Json'",
        ], {0})
        check("powershell-quoted-false-positive", code == 0 and quoted["decision"] == "PASS", quoted)

        literal_batch = write_json(temp, "literal.json", {"members": [{
            "id": "literal", "command": 'python tool.py --interface "default_prompt=Use $skill-name now"',
            "literal_dollar_required": True, "literal_tokens": ["$skill-name"]
        }]})
        literal, code, _ = run_json([
            "powershell", "-NoProfile", "-File", str(ps_script), "-InputJsonPath", str(literal_batch), "-IncludeSource",
        ], {2})
        check("powershell-literal-dollar-block", code == 2 and literal["decision"] == "BLOCK", literal)

        invalid_transition = write_json(temp, "invalid-transition.json", {
            "schema_version": "mgskill-transition-v1", "event_id": "E1", "objective_id": "O1",
            "source_claims": ["S1"], "skill_id": "K1", "from_state": "raw-event", "to_state": "active-bounded",
            "reason": "skip", "baseline": "b", "normal_path_counterexamples": ["n"],
            "independent_challenge": "i", "rollback": "r", "identity_snapshot": "h",
            "proof_ceiling": "structural", "ambiguity": "none"
        })
        invalid, code, _ = run_json([sys.executable, str(lifecycle), "transition", "--input", str(invalid_transition)], {2})
        check("lifecycle-direct-activation-rejected", code == 2 and "illegal transition" in " ".join(invalid["errors"]), invalid)

        invalid_material_data = json.loads(invalid_transition.read_text(encoding="utf-8"))
        invalid_material_data["from_state"] = "shadow"
        invalid_material_data["to_state"] = "audited"
        invalid_material_data["ambiguity"] = "material"
        invalid_material = write_json(temp, "invalid-material-transition.json", invalid_material_data)
        invalid_material_result, code, _ = run_json([sys.executable, str(lifecycle), "transition", "--input", str(invalid_material)], {2})
        check(
            "lifecycle-material-ambiguity-validates-identity-first",
            code == 2
            and invalid_material_result["decision"] == "INELIGIBLE_TRANSITION_CANDIDATE"
            and any("identity_snapshot" in item for item in invalid_material_result["errors"]),
            invalid_material_result,
        )

        valid_material_data = json.loads(json.dumps(invalid_material_data))
        valid_material_data["baseline"] = {"identity": "base-v1", "metric_definition": "objective-quality-cost-v1", "comparison": "shadow versus baseline"}
        valid_material_data["independent_challenge"] = {"status": "completed", "reviewer": "independent", "result": "no unclosed countermodel", "evidence": "fixture"}
        valid_material_data["rollback"] = {"target_identity": "prior-cut", "trigger": "harm", "steps": ["restore prior cut", "read back identity"], "recovery_owner": "parent"}
        valid_material_data["identity_snapshot"] = {
            "registry_sha256": "A" * 64,
            "selection_snapshot_sha256": "B" * 64,
            "skill_content_sha256": "C" * 64
        }
        valid_material = write_json(temp, "valid-material-transition.json", valid_material_data)
        valid_material_result, code, _ = run_json([sys.executable, str(lifecycle), "transition", "--input", str(valid_material)], {3})
        check("lifecycle-valid-material-ambiguity-user-decision", code == 3 and valid_material_result["decision"] == "USER-DECISION" and not valid_material_result["errors"], valid_material_result)

        invalid_effect = write_json(temp, "invalid-effect.json", {
            "schema_version": "mgskill-effect-v2", "event_id": "E2", "objective_id": "O1", "source_claims": ["S1"],
            "skill_id": "K1", "skill_version": "1.0.0", "registry_sha256": "A", "selection_snapshot_sha256": "B",
            "planned_objective_delta": "p", "actual_objective_delta": "a", "planned_evidence_delta": "p",
            "actual_evidence_delta": "a", "owner": {"lane": "worker", "writer": "parent"},
            "trigger": {"event_id": "E2", "family": "test", "evidence": "fixture"},
            "candidate_identity": {"id": "C2", "version": "1", "content_sha256": "C" * 64},
            "independent_challenge": {"status": "completed", "reviewer": "r", "result": "x", "evidence": "e"},
            "retirement_trigger": {"condition": "harm", "evidence": "metric", "recovery_owner": "parent"},
            "unknown_case_disposition": {"generic_invariants": ["source"], "dependent_hold": "one", "fallback": "normal"},
            "outcome": "prevented", "consumer_result": "not observed", "side_effects": ["none"],
            "rollback_result": "n/a", "rework": "none", "proof_ceiling": "mechanical", "metrics": effect_metrics()
        })
        invalid_effect_result, code, _ = run_json([sys.executable, str(lifecycle), "effect", "--input", str(invalid_effect)], {2})
        check(
            "effect-prevented-integrity",
            code == 2
            and any("pre_submission" in item for item in invalid_effect_result["errors"])
            and any("registry_sha256" in item for item in invalid_effect_result["errors"]),
            invalid_effect_result,
        )

        valid_effect = write_json(temp, "valid-effect.json", {
            "schema_version": "mgskill-effect-v2", "event_id": "E3", "objective_id": "O1", "source_claims": ["S1"],
            "skill_id": "K1", "skill_version": "1.0.0", "registry_sha256": "A" * 64,
            "selection_snapshot_sha256": "B" * 64, "planned_objective_delta": "p", "actual_objective_delta": "a",
            "planned_evidence_delta": "p", "actual_evidence_delta": "a",
            "owner": {"lane": "WORK", "writer": "parent"},
            "trigger": {"event_id": "E3", "family": "test", "evidence": "fixture"},
            "candidate_identity": {"id": "C3", "version": "1", "content_sha256": "C" * 64},
            "independent_challenge": {"status": "completed", "reviewer": "r", "result": "x", "evidence": "e"},
            "retirement_trigger": {"condition": "harm", "evidence": "metric", "recovery_owner": "parent"},
            "unknown_case_disposition": {"generic_invariants": ["source"], "dependent_hold": "one", "fallback": "normal"},
            "outcome": "advanced", "consumer_result": "bounded", "side_effects": [], "rollback_result": "n/a",
            "rework": "none", "proof_ceiling": "mechanical", "metrics": effect_metrics()
        })
        valid_effect_result, code, _ = run_json([sys.executable, str(lifecycle), "effect", "--input", str(valid_effect)], {0})
        check("effect-typed-normal-path", code == 0 and valid_effect_result["decision"] == "ACCEPTED_EFFECT_CANDIDATE", valid_effect_result)

    output = {
        "schema_version": "mgskill-test-result-v1",
        "passed": len(tests),
        "failed": 0,
        "tests": tests,
        "proof_ceiling": "bounded deterministic fixtures; runtime universal discovery, semantics, consumer outcomes and empirical benefit unproven",
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
