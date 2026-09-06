#!/usr/bin/env python3
"""Bind selected skills and their scripts to one finalized action and effect."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def process_output(action: subprocess.CompletedProcess) -> dict[str, Any]:
    """Capture raw bytes before display decoding; decoding cannot erase an effect."""
    stdout, stderr = action.stdout or b"", action.stderr or b""
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_sha256": sha_bytes(stdout), "stderr_sha256": sha_bytes(stderr),
        "display_encoding": "utf-8-replace; raw bytes authoritative",
    }


def work_lane(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("role") in {"WORK", "COORDINATED-WORK"}
        and isinstance(value.get("task_id"), str)
        and bool(value["task_id"])
        and isinstance(value.get("lease_id"), str)
        and bool(value["lease_id"])
        and (
            value["role"] == "WORK"
            or (
                value.get("chat_id") == value["task_id"]
                and re.fullmatch(r"[A-Fa-f0-9]{64}", str(value.get("source_sha256", ""))) is not None
            )
        )
    )


def deliver_selected_bytes(selected: dict[str, dict[str, Any]], required: dict[tuple[str, str], str]) -> tuple[dict[str, Any], list[str]]:
    """Open the selected files in this bridge invocation and emit their exact bytes."""
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    for (skill_id, relative_path), expected_sha in sorted(required.items()):
        skill = selected.get(skill_id)
        try:
            path = (Path(str(skill["canonical_path"])) / relative_path).resolve(strict=True) if skill else None
            content = path.read_bytes() if path else b""
            actual_sha = sha_bytes(content)
            if actual_sha != expected_sha:
                errors.append(f"selected byte hash mismatch:{skill_id}:{relative_path}")
                continue
            files.append({
                "skill_id": skill_id,
                "path": relative_path,
                "sha256": actual_sha,
                "byte_count": len(content),
                "encoding": "base64",
                "bytes": base64.b64encode(content).decode("ascii"),
            })
        except (KeyError, FileNotFoundError, OSError) as exc:
            errors.append(f"selected byte delivery unavailable:{skill_id}:{relative_path}:{exc}")
    receipt = {
        "schema_version": "bridge-byte-delivery-receipt-v1",
        "decision": "BYTES_DELIVERED" if not errors else "HOLD",
        "files": files,
        "delivery_sha256": sha_bytes(canonical([{key: item[key] for key in ("skill_id", "path", "sha256", "byte_count")} for item in files])),
        "comprehension": "UNPROVEN",
    }
    return receipt, errors


def load_bound(spec: dict[str, Any], field: str) -> tuple[Path, dict[str, Any]]:
    item = spec.get(field)
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        raise ValueError(f"{field} must contain path and sha256")
    path = Path(str(item["path"])).resolve(strict=True)
    if sha_file(path) != str(item["sha256"]).upper():
        raise ValueError(f"{field} sha256 mismatch")
    return path, json.loads(path.read_text(encoding="utf-8"))


def schema_errors(value: Any, schema: Any, where: str = "payload") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{where}: schema is not an object"]
    expected = schema.get("type")
    checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected not in checks or not checks[expected]:
        return [f"{where}: expected {expected}"]
    errors: list[str] = []
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above maximum")
    if expected == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: below minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: above maxLength")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: outside enum")
    if expected == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: below minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: above maxItems")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, schema["items"], f"{where}[{index}]"))
    if expected == "object":
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
            if field not in value:
                errors.append(f"{where}.{field}: required")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{where}: extra fields {extra}")
        for field in sorted(set(value) & set(properties)):
            errors.extend(schema_errors(value[field], properties[field], f"{where}.{field}"))
    return errors


def prepare(bundle_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = bundle_path.read_bytes()
    bundle = json.loads(raw.decode("utf-8"))
    errors: list[str] = []
    if bundle.get("schema_version") != "skill-application-bundle-v1":
        errors.append("unsupported bundle schema")
    envelope = bundle.get("envelope")
    if not isinstance(envelope, dict) or envelope.get("schema_version") != "final-action-envelope-v1":
        errors.append("invalid final action envelope")
        envelope = {}
    envelope_sha = sha_bytes(canonical(envelope))
    cut_spec = bundle.get("candidate_cut")
    if not isinstance(cut_spec, dict) or set(cut_spec) != {"root", "manifest", "manifest_sha256", "member_set_sha256"}:
        errors.append("candidate cut binding is incomplete")
        cut_spec = {}
    candidate_member_set_sha256 = str(cut_spec.get("member_set_sha256", "")).upper()
    try:
        candidate_root = Path(str(cut_spec["root"])).resolve(strict=True)
        manifest_path = Path(str(cut_spec["manifest"])).resolve(strict=True)
        if sha_file(manifest_path) != str(cut_spec["manifest_sha256"]).upper():
            errors.append("candidate cut manifest hash mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        members = manifest.get("files")
        snapshot_members = manifest.get("members")
        if isinstance(members, list):
            expected_cut = sha_bytes(canonical(members))
            expected_members = members
        elif isinstance(snapshot_members, list):
            expected_cut = str(manifest.get("candidate_member_set_sha256", "")).upper()
            expected_members = [
                {"path": str(item.get("path")), "sha256": str(item.get("sha256", "")).upper(), "size": item.get("bytes")}
                for item in snapshot_members if isinstance(item, dict)
            ]
        else:
            expected_cut = ""
            expected_members = []
        declared_cut = str(manifest.get("member_set_sha256", manifest.get("candidate_member_set_sha256", ""))).upper()
        if declared_cut != candidate_member_set_sha256 or expected_cut != candidate_member_set_sha256:
            errors.append("candidate member-set identity mismatch")
        managed_roots = manifest.get("managed_roots")
        if not isinstance(managed_roots, list) or not managed_roots or any(not isinstance(root, str) for root in managed_roots):
            errors.append("candidate managed roots are incomplete")
            managed_roots = []
        actual_paths: set[Path] = set()
        for root_name in managed_roots:
            root = (candidate_root / root_name).resolve(strict=True)
            try:
                root.relative_to(candidate_root)
            except ValueError:
                errors.append(f"candidate managed root escapes payload:{root_name}")
                continue
            if root.is_file():
                actual_paths.add(root)
            elif root.is_dir():
                actual_paths.update(item.resolve(strict=True) for item in root.rglob("*") if item.is_file())
            else:
                errors.append(f"candidate managed root is not file or directory:{root_name}")
        actual_members = [
            {"path": item.relative_to(candidate_root).as_posix(), "sha256": sha_file(item), "size": item.stat().st_size}
            for item in sorted(actual_paths)
        ]
        # Manifest order is hash-bound serialization, not a filesystem ordering
        # contract. Compare complete records without losing case or duplicates.
        if sorted(actual_members, key=canonical) != sorted(expected_members, key=canonical):
            errors.append("candidate member-set does not match current root")
    except (KeyError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        errors.append(f"candidate cut unreadable:{exc}")
    if envelope.get("candidate_member_set_sha256") != candidate_member_set_sha256:
        errors.append("envelope candidate member-set mismatch")
    payload = envelope.get("final_payload")
    if sha_bytes(canonical(payload)) != str(envelope.get("final_payload_sha256", "")).upper():
        errors.append("final payload hash mismatch")
    lane = envelope.get("lane")
    if not work_lane(lane):
        errors.append("owner lane must be legacy WORK or source-bound COORDINATED-WORK with task/chat and lease identity")

    compiler_path, compiler = load_bound(bundle, "compiler_receipt")
    input_path, resolver_input = load_bound(bundle, "resolver_input")
    selection_path, selection = load_bound(bundle, "selection_receipt")
    read_path, read_receipt = load_bound(bundle, "required_read_receipt")
    if compiler.get("valid") is not True or compiler.get("errors"):
        errors.append("fact compiler receipt is not valid")
    action_id = envelope.get("action_id")
    bindings_for_action = [item for item in compiler.get("action_bindings", []) if isinstance(item, dict) and item.get("action_id") == action_id]
    if not action_id or len(bindings_for_action) != 1:
        errors.append("exact compiler action_id binding is missing or ambiguous")
    else:
        compiled_action = bindings_for_action[0]
        if compiled_action.get("status") != "ready" or compiled_action.get("finality") != "finalized":
            errors.append("compiled action is not finalized and ready")
        if compiled_action.get("final_payload") != payload or compiled_action.get("final_payload_sha256") != sha_bytes(canonical(payload)):
            errors.append("compiler and execution final payload differ")
        if compiled_action.get("tool_schema_sha256") != sha_bytes(canonical(envelope.get("tool_schema"))):
            errors.append("compiler and execution tool schema differ")
    if compiler.get("resolver_input") != resolver_input:
        errors.append("compiler output differs from resolver input")
    if selection.get("errors") or selection.get("decision") != "selected":
        errors.append("selection receipt is not selected")
    if selection.get("input_sha256") != sha_file(input_path):
        errors.append("selection does not bind resolver input")
    if selection.get("selection_snapshot_sha256") != envelope.get("selection_snapshot_sha256"):
        errors.append("envelope selection snapshot mismatch")
    if resolver_input.get("objective_id") != envelope.get("objective_id"):
        errors.append("objective identity mismatch")
    if sorted(resolver_input.get("source_claims", [])) != sorted(envelope.get("source_claims", [])):
        errors.append("source clause mismatch")

    required_files: dict[tuple[str, str], str] = {}
    selected_by_id: dict[str, dict[str, Any]] = {}
    for skill in selection.get("selected", []):
        selected_by_id[str(skill.get("skill_id"))] = skill
        for item in skill.get("files", []):
            path = str(item.get("path"))
            if not (path.startswith("agents/") and path.endswith(".yaml")):
                required_files[(str(skill.get("skill_id")), path)] = str(item.get("sha256", "")).upper()
    if read_receipt.get("schema_version") != "required-read-receipt-v1":
        errors.append("invalid required-read receipt")
    elif read_receipt.get("status") == "FULL":
        errors.append("caller-authored FULL is not action eligibility evidence")
    elif read_receipt.get("status") == "UNAVAILABLE":
        errors.append("required read unavailable for dependent action")
    elif read_receipt.get("status") != "HASH_ONLY":
        errors.append("required read receipt must be HASH_ONLY or UNAVAILABLE")
    if read_receipt.get("selection_snapshot_sha256") != selection.get("selection_snapshot_sha256"):
        errors.append("read receipt selection mismatch")
    if read_receipt.get("comprehension") != "UNPROVEN":
        errors.append("read receipt must not claim model comprehension")

    byte_delivery, delivery_errors = deliver_selected_bytes(selected_by_id, required_files)
    errors.extend(delivery_errors)

    bindings: list[dict[str, Any]] = []
    for item in bundle.get("script_bindings", []):
        if not isinstance(item, dict):
            errors.append("invalid script binding")
            continue
        skill = selected_by_id.get(str(item.get("skill_id")))
        rel = str(item.get("relative_path"))
        expected = next((x for x in skill.get("files", []) if x.get("path") == rel), None) if skill else None
        if not skill or not expected:
            errors.append(f"script not selected:{item.get('skill_id')}:{rel}")
            continue
        script = (Path(str(skill["canonical_path"])) / rel).resolve(strict=True)
        if sha_file(script) != str(expected.get("sha256", "")).upper() or sha_file(script) != str(item.get("sha256", "")).upper():
            errors.append(f"script hash mismatch:{rel}")
        bindings.append({**item, "canonical_path": str(script)})

    tool_schema = envelope.get("tool_schema")
    if not isinstance(tool_schema, dict) or tool_schema.get("status") not in {"available", "unavailable", "not-applicable"}:
        errors.append("invalid tool schema state")
    elif tool_schema.get("status") == "available":
        if not tool_schema.get("identity"):
            errors.append("available tool schema lacks identity")
        errors.extend(schema_errors(payload, tool_schema.get("schema")))
    elif tool_schema.get("status") == "unavailable":
        errors.append("tool schema unavailable for dependent typed action")

    receipt = {
        "schema_version": "skill-application-prepare-receipt-v1",
        "decision": "GO" if not errors else "HOLD",
        "bundle_path": str(bundle_path),
        "bundle_sha256": sha_bytes(raw),
        "envelope_sha256": envelope_sha,
        "compiler_receipt_sha256": sha_file(compiler_path),
        "action_id": action_id,
        "selection_receipt_sha256": sha_file(selection_path),
        "required_read_receipt_sha256": sha_file(read_path),
        "selection_snapshot_sha256": selection.get("selection_snapshot_sha256"),
        "candidate_member_set_sha256": candidate_member_set_sha256,
        "owner_lane": lane,
        "byte_delivery": byte_delivery,
        "script_bindings": bindings,
        "errors": sorted(set(errors)),
        "proof_ceiling": "bridge-generated selected-byte delivery, identity, script-binding and typed-schema closure only; comprehension, semantic applicability, authority and outcome unproven",
    }
    return receipt, selection


def run_powershell(bundle_path: Path) -> tuple[dict[str, Any], int]:
    prepared, _ = prepare(bundle_path)
    lane = prepared.get("owner_lane")
    # Recheck the action owner here; a caller must not be able to forge a GO prepare receipt.
    if prepared["decision"] != "GO" or not work_lane(lane):
        return {"schema_version": "bound-powershell-action-v1", "decision": "HOLD", "prepare": prepared, "executed": False}, 2
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload = bundle["envelope"]["final_payload"]
    bindings = [x for x in prepared["script_bindings"] if x.get("role") == "powershell-preflight"]
    if len(bindings) != 1:
        return {"schema_version": "bound-powershell-action-v1", "decision": "ERROR", "error": "exactly one PowerShell preflight binding required", "executed": False}, 1
    binding = bindings[0]
    command = str(payload.get("command_text", ""))
    executable = str(payload.get("powershell_executable", "powershell"))
    preflight = subprocess.run(
        [executable, "-NoProfile", "-File", binding["canonical_path"], "-CommandText", command, "-IncludeSource"],
        capture_output=True, check=False,
    )
    try:
        preflight_body = json.loads(preflight.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        preflight_body = {"decision": "ERROR", "raw_stdout_sha256": sha_bytes(preflight.stdout), "raw_stderr_sha256": sha_bytes(preflight.stderr)}
    result: dict[str, Any] = {
        "schema_version": "bound-powershell-action-v1",
        "prepare_receipt_sha256": sha_bytes(canonical(prepared)),
        "candidate_member_set_sha256": prepared["candidate_member_set_sha256"],
        "envelope_sha256": prepared["envelope_sha256"],
        "owner_lane": lane,
        "byte_delivery_sha256": prepared["byte_delivery"]["delivery_sha256"],
        "script_binding_sha256": sha_bytes(canonical(binding)),
        "command_sha256": sha_bytes(command.encode("utf-8")),
        "preflight_exit_code": preflight.returncode,
        "preflight": preflight_body,
        "executed": False,
        "proof_ceiling": "selected-script preflight and bounded process result only; semantic correctness and consumer outcome unproven",
    }
    if preflight.returncode != 0 or preflight_body.get("decision") != "PASS":
        result["decision"] = "BLOCK" if preflight_body.get("decision") == "BLOCK" else "ERROR"
        return result, 2 if result["decision"] == "BLOCK" else 1
    if payload.get("execute") is not True:
        result["decision"] = "PREFLIGHT_PASS"
        return result, 0
    action = subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True, check=False)
    captured = process_output(action)
    result.update({
        "decision": "EXECUTED" if action.returncode == 0 else "EXECUTION_FAILED",
        "executed": True,
        "exit_code": action.returncode,
        **captured,
        "primary_fault": None if action.returncode == 0 else (captured["stderr"].strip() or f"exit {action.returncode}"),
    })
    return result, 0 if action.returncode == 0 else 1


def run_script(bundle_path: Path) -> tuple[dict[str, Any], int]:
    """Execute one selected Python script without a shell, after exact preparation.

    This is process evidence, not permission, semantic applicability or benefit.
    Other runtimes remain explicit unsupported routes; they are not guessed.
    """
    prepared, _ = prepare(bundle_path)
    if prepared["decision"] != "GO" or not work_lane(prepared.get("owner_lane")):
        return {"schema_version": "bound-script-action-v1", "decision": "HOLD", "prepare": prepared, "executed": False}, 2
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload = bundle["envelope"]["final_payload"]
    bindings = [x for x in prepared["script_bindings"] if x.get("role") == "action-script"]
    if len(bindings) != 1 or not isinstance(payload, dict):
        raise ValueError("exactly one selected action-script and object payload required")
    binding = bindings[0]
    script = Path(binding["canonical_path"])
    argv = payload.get("arguments")
    if script.suffix.lower() != ".py" or payload.get("script_path") != str(script):
        raise ValueError("finalized script_path must equal the selected Python script")
    if not isinstance(argv, list) or any(not isinstance(x, str) or "\x00" in x for x in argv):
        raise ValueError("arguments must be a finalized string array")
    if sha_file(script) != str(binding["sha256"]).upper():
        raise ValueError("selected script changed after preparation")
    result = {
        "schema_version": "bound-script-action-v1", "decision": "PREFLIGHT_PASS", "executed": False,
        "prepare_receipt_sha256": sha_bytes(canonical(prepared)),
        "candidate_member_set_sha256": prepared["candidate_member_set_sha256"],
        "envelope_sha256": prepared["envelope_sha256"], "owner_lane": prepared["owner_lane"],
        "byte_delivery_sha256": prepared["byte_delivery"]["delivery_sha256"],
        "script_binding_sha256": sha_bytes(canonical(binding)),
        "argv_sha256": sha_bytes(canonical([str(script), *argv])),
        "proof_ceiling": "selected Python script, exact argument identity and bounded process outcome only; authority, semantic correctness and consumer benefit unproven",
    }
    if payload.get("execute") is not True:
        return result, 0
    action = subprocess.run([sys.executable, "-B", str(script), *argv], capture_output=True, check=False)
    captured = process_output(action)
    result.update({"decision": "EXECUTED" if action.returncode == 0 else "EXECUTION_FAILED", "executed": True,
                   "exit_code": action.returncode, **captured,
                   "primary_fault": None if action.returncode == 0 else (captured["stderr"].strip() or f"exit {action.returncode}")})
    return result, 0 if action.returncode == 0 else 1


def validate_effect(effect_path: Path) -> tuple[dict[str, Any], int]:
    effect = json.loads(effect_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    receipts = effect.get("application_receipts")
    required = {"prepare", "preflight_execution", "action_result"}
    if not isinstance(receipts, dict) or set(receipts) != required:
        errors.append("application_receipts set mismatch")
        receipts = {}
    loaded: dict[str, tuple[Path, dict[str, Any]]] = {}
    for field in sorted(required & set(receipts)):
        try:
            loaded[field] = load_bound(receipts, field)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if "prepare" in loaded and loaded["prepare"][1].get("decision") != "GO":
        errors.append("prepare receipt is not GO")
    if "prepare" in loaded and not work_lane(loaded["prepare"][1].get("owner_lane")):
        errors.append("prepare owner lane is not a bound execution lane")
    if "preflight_execution" in loaded:
        run = loaded["preflight_execution"][1]
        outcome = effect.get("outcome")
        allowed = {
            "advanced": {"EXECUTED"},
            "applied": {"EXECUTED"},
            "no_effect": {"EXECUTED", "EXECUTION_FAILED"},
            "recurred": {"EXECUTED", "EXECUTION_FAILED", "ERROR"},
            "prevented": {"BLOCK"},
            "false_block": {"BLOCK"},
            "not_applied": {"ERROR", "EXECUTION_FAILED"},
            "outcome_unknown": {"ERROR", "EXECUTION_FAILED"},
            "not_observed": {"PREFLIGHT_PASS", "ERROR"},
            "unavailable": {"PREFLIGHT_PASS", "ERROR", "EXECUTION_FAILED"},
            "not_applicable": {"PREFLIGHT_PASS", "BLOCK"},
            "misselected": {"BLOCK", "ERROR", "EXECUTION_FAILED"},
        }.get(outcome, set())
        if run.get("decision") not in allowed:
            errors.append(f"execution decision {run.get('decision')} is inconsistent with effect outcome {outcome}")
        if "prepare" in loaded and run.get("owner_lane") != loaded["prepare"][1].get("owner_lane"):
            errors.append("execution owner lane mismatch")
        if run.get("decision") in {"BLOCK", "ERROR", "HOLD"}:
            if run.get("executed") is not False:
                errors.append("non-success action cannot be executed")
            if any(key in run for key in ("exit_code", "stdout", "stderr", "target_execution_result")):
                errors.append("non-success receipt cannot contain a target execution result")
    if "action_result" in loaded and "preflight_execution" in loaded:
        result = loaded["action_result"][1]
        if result.get("execution_receipt_sha256") != sha_file(loaded["preflight_execution"][0]):
            errors.append("action result does not bind execution receipt")
        if result.get("envelope_sha256") != loaded["preflight_execution"][1].get("envelope_sha256"):
            errors.append("action result envelope mismatch")
        expected_lane = loaded["prepare"][1].get("owner_lane") if "prepare" in loaded else None
        if result.get("owner_lane") != expected_lane or effect.get("owner_lane") != expected_lane:
            errors.append("effect graph owner lane mismatch")
        expected_cut = loaded["prepare"][1].get("candidate_member_set_sha256") if "prepare" in loaded else None
        if result.get("candidate_member_set_sha256") != expected_cut:
            errors.append("action result candidate member-set mismatch")
        if loaded["preflight_execution"][1].get("candidate_member_set_sha256") != expected_cut:
            errors.append("execution receipt candidate member-set mismatch")
        if effect.get("candidate_member_set_sha256") != expected_cut:
            errors.append("effect candidate member-set mismatch")
    for field in ("actual_objective_delta", "actual_evidence_delta"):
        value = effect.get(field)
        if not isinstance(value, dict) or value.get("status") not in {"observed", "unavailable", "not_applicable"}:
            errors.append(f"{field} must be typed")
        elif value.get("status") == "observed" and not value.get("evidence"):
            errors.append(f"{field} observed requires evidence")
        elif effect.get("outcome") in {"advanced", "no_effect"} and value.get("status") != "observed":
            errors.append(f"{field} is hollow for outcome {effect.get('outcome')}")
    lifecycle_result = None
    if not errors and "prepare" in loaded:
        lifecycle_bindings = [
            item for item in loaded["prepare"][1].get("script_bindings", [])
            if item.get("role") == "lifecycle-validator"
        ]
        if len(lifecycle_bindings) != 1:
            errors.append("exactly one selected lifecycle binding required")
        else:
            completed = subprocess.run(
                [sys.executable, "-B", lifecycle_bindings[0]["canonical_path"], "effect", "--input", str(effect_path)],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            try:
                lifecycle_result = json.loads(completed.stdout)
            except json.JSONDecodeError:
                errors.append("selected lifecycle script returned invalid JSON")
            if completed.returncode != 0:
                errors.append(f"selected lifecycle script exited {completed.returncode}")
    result = {
        "schema_version": "skill-application-effect-gate-v1",
        "decision": "GO" if not errors else "HOLD",
        "effect_sha256": sha_file(effect_path),
        "errors": sorted(set(errors)),
        "lifecycle_result": lifecycle_result,
        "proof_ceiling": "receipt-graph integrity and non-hollow delta shape only; lifecycle merit and consumer benefit unproven",
    }
    return result, 0 if not errors else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--bundle", required=True)
    run_parser = sub.add_parser("run-powershell")
    run_parser.add_argument("--bundle", required=True)
    script_parser = sub.add_parser("run-script")
    script_parser.add_argument("--bundle", required=True)
    effect_parser = sub.add_parser("effect")
    effect_parser.add_argument("--input", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result, _ = prepare(Path(args.bundle).resolve(strict=True))
            code = 0 if result["decision"] == "GO" else 2
        elif args.command == "run-powershell":
            result, code = run_powershell(Path(args.bundle).resolve(strict=True))
        elif args.command == "run-script":
            result, code = run_script(Path(args.bundle).resolve(strict=True))
        else:
            result, code = validate_effect(Path(args.input).resolve(strict=True))
    except Exception as exc:
        result, code = {
            "schema_version": "skill-application-bridge-error-v1",
            "decision": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "proof_ceiling": "bridge failure only; dependent action remains unexecuted or unproven",
        }, 1
    # ASCII JSON is lossless after decoding and survives legacy Windows stdout code pages.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
