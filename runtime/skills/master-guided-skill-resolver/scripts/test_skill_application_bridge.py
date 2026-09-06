#!/usr/bin/env python3
"""Focused installed regression test for the selected-skill application bridge."""

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest().upper()


def write_json(root, name, value):
    path = root / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_script(script, *args, expected=(0,)):
    result = subprocess.run(
        [sys.executable, "-B", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode not in expected:
        raise AssertionError((script, args, result.returncode, result.stdout, result.stderr))
    output_path = None
    try:
        if result.stdout.strip():
            return json.loads(result.stdout)
        if result.returncode == 0 and "--output" in args:
            output_path = Path(args[args.index("--output") + 1])
            return json.loads(output_path.read_text(encoding="utf-8"))
        raise ValueError("No JSON result was delivered by the requested output route")
    except (OSError, ValueError, IndexError) as exc:
        raise AssertionError((script, args, result.returncode, result.stdout, result.stderr, output_path)) from exc


def build_synthetic_fixture(root, *, execute=True, powershell_executable=None):
    """Build a complete selected-PowerShell bundle without private fixtures."""
    here = Path(__file__).resolve().parent
    skill = root / "powershell-exact-action"
    shutil.copytree(here.parents[1] / "powershell-exact-action", skill)
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha_bytes(path.read_bytes()),
            "size": path.stat().st_size,
        }
        for path in sorted(
            (item for item in skill.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        )
    ]
    cut_hash = sha_bytes(canonical(files))
    manifest_path = write_json(
        root,
        "candidate-manifest.json",
        {"files": files, "member_set_sha256": cut_hash, "managed_roots": [skill.name]},
    )
    skill_files = [
        {
            "path": item.relative_to(skill).as_posix(),
            "sha256": sha_bytes(item.read_bytes()),
        }
        for item in sorted(
            (path for path in skill.rglob("*") if path.is_file()),
            key=lambda path: path.as_posix().casefold(),
        )
    ]
    entry = {
        "skill_id": "SKILL-TEST-POWERSHELL-EXACT-ACTION-001",
        "name": "powershell-exact-action",
        "version": "1.0.0",
        "origin": "user",
        "relative_path": skill.name,
        "status": "active-bounded",
        "disclosure_class": "blind_safe_mechanical",
        "match_clauses": [{
            "job": ["command-preflight"],
            "action": ["execute-powershell"],
            "tool": ["powershell"],
            "cause_families": ["POWERSHELL::FOREACH-PIPE-PARSER"],
        }],
        "required_authority": ["explicit temporary-fixture authority"],
        "required_inputs": ["finalized PowerShell command"],
        "resource_claims": ["temporary process only"],
        "expected_delta": "screen and optionally execute the exact fixture command",
        "proof_ceiling": "selected-script process evidence only",
        "cost": "one bounded local process",
        "revalidation": ["command, registry, root, or selected file change"],
        "rollback": "remove the temporary fixture root",
        "master_links": ["G-EVT-TEST-BRIDGE-001"],
        "files": skill_files,
    }
    registry_path = write_json(
        root,
        "registry.json",
        {
            "schema_version": "mgskill-registry-v1",
            "registry_id": "OIF-SYNTHETIC-BRIDGE-REGISTRY",
            "entries": [entry],
        },
    )
    source_path = write_json(
        root,
        "source.json",
        {"clauses": [{"id": "SRC-1", "text": "Run the exact bounded PowerShell fixture."}]},
    )
    facts = {
        "job": ["command-preflight"],
        "action": ["execute-powershell"],
        "tool": ["powershell"],
        "cause_families": ["POWERSHELL::FOREACH-PIPE-PARSER"],
    }
    payload = {
        "facts": facts,
        "command_text": "Write-Output 'bridge-ok'",
        "powershell_executable": powershell_executable or shutil.which("pwsh") or shutil.which("powershell") or "powershell",
        "execute": execute,
    }
    string_array = {"type": "array", "items": {"type": "string"}}
    schema = {
        "status": "available",
        "identity": "bound-powershell-fixture-v1",
        "schema": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "object",
                    "properties": {
                        "job": string_array,
                        "action": string_array,
                        "tool": string_array,
                        "cause_families": string_array,
                    },
                    "required": ["job", "action", "tool", "cause_families"],
                    "additionalProperties": False,
                },
                "command_text": {"type": "string"},
                "powershell_executable": {"type": "string"},
                "execute": {"type": "boolean"},
            },
            "required": ["facts", "command_text", "powershell_executable", "execute"],
            "additionalProperties": False,
        },
    }
    plan_path = write_json(
        root,
        "plan.json",
        {
            "schema_version": "mgskill-fact-builder-plan-v1",
            "objective_id": "objective",
            "current_authority": {"message": "Isolated verification of a local fixture."},
            "source_document": {"path": str(source_path)},
            "source_claims": ["SRC-1"],
            "blind_phase": "none",
            "source_records": [{
                "id": "SRC-1",
                "disposition": "no-selection-fact",
                "classification": "primary",
                "intent": "exercise the bounded fixture",
                "mechanism": "selected PowerShell preflight",
                "evidence": "exact synthetic source",
                "reason": "the source describes the outcome, not a preferred Skill",
                "facts": {},
                "excluded_facts": {},
            }],
            "action_records": [{
                "id": "ACT-1",
                "disposition": "fact-bearing",
                "evidence": "final synthetic payload",
                "facts": facts,
                "excluded_facts": {},
                "finality": "finalized",
                "final_payload": payload,
                "tool_schema": schema,
            }],
            "negative_selection_challenge": {
                "status": "completed",
                "countermodel": "An unrelated command must not select the fixture Skill.",
            },
        },
    )
    built = root / "built.json"
    compiler = root / "compiler.json"
    resolver_input = root / "resolver-input.json"
    run_script(here / "build_skill_fact_input.py", "--plan", str(plan_path), "--registry", str(registry_path), "--output", str(built))
    run_script(here / "compile_skill_facts.py", "--source", str(built), "--registry", str(registry_path), "--output", str(compiler), "--resolver-input-output", str(resolver_input))
    selection = run_script(here / "resolve_skills.py", "--input", str(resolver_input), "--registry", str(registry_path), "--user-root", str(root))
    if selection.get("decision") != "selected":
        raise AssertionError(selection)
    selection_path = write_json(root, "selection.json", selection)
    read_path = write_json(
        root,
        "read.json",
        {
            "schema_version": "required-read-receipt-v1",
            "status": "HASH_ONLY",
            "selection_snapshot_sha256": selection["selection_snapshot_sha256"],
            "comprehension": "UNPROVEN",
        },
    )

    def spec(path):
        return {"path": str(path), "sha256": sha_bytes(path.read_bytes())}

    bundle_path = write_json(
        root,
        "application-bundle.json",
        {
            "schema_version": "skill-application-bundle-v1",
            "candidate_cut": {
                "root": str(root),
                "manifest": str(manifest_path),
                "manifest_sha256": sha_bytes(manifest_path.read_bytes()),
                "member_set_sha256": cut_hash,
            },
            "envelope": {
                "schema_version": "final-action-envelope-v1",
                "objective_id": "objective",
                "source_claims": ["SRC-1"],
                "action_id": "ACT-1",
                "candidate_member_set_sha256": cut_hash,
                "lane": {
                    "role": "COORDINATED-WORK",
                    "task_id": "fixture-chat",
                    "chat_id": "fixture-chat",
                    "lease_id": "fixture-lease",
                    "source_sha256": sha_bytes(source_path.read_bytes()),
                },
                "final_payload": payload,
                "final_payload_sha256": sha_bytes(canonical(payload)),
                "selection_snapshot_sha256": selection["selection_snapshot_sha256"],
                "tool_schema": schema,
            },
            "compiler_receipt": spec(compiler),
            "resolver_input": spec(resolver_input),
            "selection_receipt": spec(selection_path),
            "required_read_receipt": spec(read_path),
            "script_bindings": [{
                "skill_id": entry["skill_id"],
                "relative_path": "scripts/Test-PowerShellExactAction.ps1",
                "sha256": sha_bytes((skill / "scripts" / "Test-PowerShellExactAction.ps1").read_bytes()),
                "role": "powershell-preflight",
            }],
        },
    )
    blocked_effect = write_json(root, "blocked-effect.json", {"schema_version": "skill-application-effect-v1"})
    return bundle_path, blocked_effect


def run(bridge, *args, expected=(0,)):
    result = subprocess.run(
        [sys.executable, "-B", str(bridge), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode not in expected:
        raise AssertionError((args, result.returncode, result.stdout, result.stderr))
    return json.loads(result.stdout), result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-root",
        help="optional directory containing application-bundle.json and blocked-effect.json",
    )
    args = parser.parse_args()
    bridge = Path(__file__).resolve().with_name("skill_application_bridge.py")
    checks = []
    skipped = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory() as raw_temp:
        temp = Path(raw_temp)
        if args.fixture_root:
            fixture_root = Path(args.fixture_root).resolve(strict=True)
            bundle_path = fixture_root / "application-bundle.json"
            blocked_effect_path = fixture_root / "blocked-effect.json"
        else:
            bundle_path, blocked_effect_path = build_synthetic_fixture(temp / "fixture")
        base = json.loads(bundle_path.read_text(encoding="utf-8"))

        def write(name, value):
            path = temp / name
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return path

        # R5 retires caller-authored FULL. The bridge itself delivers selected bytes.
        hash_only_read = json.loads(Path(base["required_read_receipt"]["path"]).read_text(encoding="utf-8"))
        hash_only_read["status"] = "HASH_ONLY"
        hash_only_read.pop("evidence", None)
        hash_only_path = write("hash-only-read.json", hash_only_read)
        working = copy.deepcopy(base)
        working["required_read_receipt"] = {
            "path": str(hash_only_path),
            "sha256": hashlib.sha256(hash_only_path.read_bytes()).hexdigest().upper(),
        }
        working_path = write("r5-working-bundle.json", working)

        prepared, _ = run(bridge, "prepare", "--bundle", str(working_path))
        delivery = prepared.get("byte_delivery", {})
        check("bridge-generated-bytes-delivered", prepared["decision"] == "GO" and delivery.get("decision") == "BYTES_DELIVERED")
        check("bytes-delivery-keeps-comprehension-unproven", delivery.get("comprehension") == "UNPROVEN")
        host = working["envelope"]["final_payload"]["powershell_executable"]
        native_available = shutil.which(host) is not None
        if native_available:
            executed, _ = run(bridge, "run-powershell", "--bundle", str(working_path))
            check("selected-script-executed", executed["decision"] == "EXECUTED" and executed["executed"] is True)
        else:
            skipped.append({"case": "selected-script-executed", "reason": "No native PowerShell host is available"})

        override = subprocess.run(
            [sys.executable, "-B", str(bridge), "run-powershell", "--bundle", str(working_path), "--execute"],
            capture_output=True, text=True, encoding="utf-8",
        )
        check("cli-intent-override-rejected", override.returncode == 2)

        def case(name, mutate):
            value = copy.deepcopy(working)
            mutate(value)
            return write(f"{name}.json", value)

        missing = case("missing-cut", lambda value: value["envelope"].pop("candidate_member_set_sha256"))
        result, _ = run(bridge, "prepare", "--bundle", str(missing), expected=(2,))
        check("missing-member-set-holds", result["decision"] == "HOLD")

        wrong = case("wrong-cut", lambda value: value["envelope"].__setitem__("candidate_member_set_sha256", "0" * 64))
        result, _ = run(bridge, "prepare", "--bundle", str(wrong), expected=(2,))
        check("wrong-member-set-holds", result["decision"] == "HOLD")

        no_schema = case(
            "schema-unavailable",
            lambda value: value["envelope"].__setitem__("tool_schema", {"status": "unavailable", "reason": "test"}),
        )
        result, _ = run(bridge, "prepare", "--bundle", str(no_schema), expected=(2,))
        check("schema-unavailable-dependent-hold", result["decision"] == "HOLD")

        supervisor = case("supervisor-lane", lambda value: value["envelope"]["lane"].__setitem__("role", "SUPERVISOR"))
        result, _ = run(bridge, "prepare", "--bundle", str(supervisor), expected=(2,))
        check("supervisor-prepare-rejected-before-script", result["decision"] == "HOLD")
        result, _ = run(bridge, "run-powershell", "--bundle", str(supervisor), expected=(2,))
        check("supervisor-run-rejected-before-script", result["decision"] == "HOLD" and result["executed"] is False)

        forged_full = copy.deepcopy(hash_only_read)
        forged_full["status"] = "FULL"
        forged_full["evidence"] = {"issuer": "external-tool-export", "event_ids": ["forged"]}
        forged_full_path = write("forged-full-read.json", forged_full)
        forged_bundle = copy.deepcopy(working)
        forged_bundle["required_read_receipt"] = {"path": str(forged_full_path), "sha256": hashlib.sha256(forged_full_path.read_bytes()).hexdigest().upper()}
        forged_bundle_path = write("forged-full-bundle.json", forged_bundle)
        result, _ = run(bridge, "prepare", "--bundle", str(forged_bundle_path), expected=(2,))
        check("forged-external-full-rejected", result["decision"] == "HOLD")

        synthetic_stale_digest = hashlib.sha256(b"synthetic-stale-selection").hexdigest().upper()
        stale = case("stale-r4-selection", lambda value: value["envelope"].__setitem__("selection_snapshot_sha256", synthetic_stale_digest))
        result, _ = run(bridge, "prepare", "--bundle", str(stale), expected=(2,))
        check("stale-r4-selection-rejected", result["decision"] == "HOLD")

        no_run = copy.deepcopy(working)
        no_run["envelope"]["final_payload"]["execute"] = False
        no_run["envelope"]["final_payload_sha256"] = hashlib.sha256(
            canonical(no_run["envelope"]["final_payload"])
        ).hexdigest().upper()
        no_run_path = temp / "no-run.json"
        no_run_path.write_text(json.dumps(no_run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result, _ = run(bridge, "run-powershell", "--bundle", str(no_run_path), expected=(2,))
        check("post-compilation-payload-mutation-holds", result["decision"] == "HOLD" and result["executed"] is False
              and "compiler and execution final payload differ" in result["prepare"]["errors"])
        # A legitimate no-execute action needs a new complete provenance graph,
        # not a rewritten execution envelope attached to an older compiler receipt.
        bound_no_run, _ = build_synthetic_fixture(temp / "no-execute", execute=False, powershell_executable=host)
        prepared_no_run, _ = run(bridge, "prepare", "--bundle", str(bound_no_run))
        check("no-execute-graph-prepared", prepared_no_run["decision"] == "GO")
        if native_available:
            result, _ = run(bridge, "run-powershell", "--bundle", str(bound_no_run))
            check("payload-no-execute", result["decision"] == "PREFLIGHT_PASS" and result["executed"] is False)
        else:
            skipped.append({"case": "payload-no-execute", "reason": "No native PowerShell host is available"})

        # R3 effect evidence has no R5 owner-lane graph and must not be promoted.
        effect, _ = run(bridge, "effect", "--input", str(blocked_effect_path), expected=(2,))
        check("hollow-or-mismatched-effect-graph-rejected", effect["decision"] == "HOLD")

    print(json.dumps({"passed": len(checks), "failed": 0, "checks": checks, "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
