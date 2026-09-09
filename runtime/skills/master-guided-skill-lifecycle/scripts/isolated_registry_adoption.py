#!/usr/bin/env python3
"""Stage an exact Skill package and candidate registry; never activate or run it."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import uuid
from pathlib import Path
from typing import Any

_load_source = runpy.run_path(str(Path(__file__).with_name('source_module.py')))['load_source']
package = _load_source(Path(__file__).with_name('skill_package.py'))


SAFE_NAME = package.SAFE_NAME
sha_bytes = package.sha_bytes
canonical = package.canonical
is_under = package.is_under
manifest = package.root_manifest
readback = package.destination_readback


def sha(path: Path) -> str:
    return package.read_file(path).sha256


def material_sha(data: dict[str, Any]) -> str:
    return package.sha_value({key: value for key, value in data.items() if key != "materialization_receipt_sha256"})


def fault(point: str) -> None:
    if os.environ.get("MGSKILL_TEST_ADOPTION_FAULT") == point:
        raise RuntimeError(f"test fault:{point}")


def emit(value: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))
    return code


def fail(primary: str, state: str, stage: Path | None, destination: Path | None, writes: list[str],
         cleanup: list[str] | None = None, *, stage_id: tuple[int, int] | None = None) -> int:
    result = package.failure_record(primary, state, stage, destination, writes, stage_id=stage_id)
    result["cleanup_failures"] = (cleanup or []) + result["cleanup_failures"]
    committed = result["transaction_state"] in {"COMMITTED", "VERIFIED", "COMMIT_UNCERTAIN"}
    return emit({"decision": "OUTCOME_UNKNOWN" if committed else "ADOPTION_FAILED", **result}, 2)


def receipt_package(receipt: Any, root: Path) -> tuple[package.PackageBytes, dict[str, Any], dict[str, Any]]:
    if not isinstance(receipt, dict) or receipt.get("materialization_receipt_sha256") != material_sha(receipt):
        raise package.PackageError("materialization receipt self-hash mismatch")
    candidate, owner = receipt.get("candidate"), receipt.get("owner")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str) or not SAFE_NAME.fullmatch(candidate["candidate_id"]):
        raise package.PackageError("candidate binding invalid")
    if not isinstance(owner, dict) or owner.get("lane") not in {"WORK", "COORDINATED-WORK"}:
        raise package.PackageError("owner binding invalid")
    if owner["lane"] == "COORDINATED-WORK" and any(not isinstance(owner.get(key), str) or not owner[key].strip() for key in ("task_id", "lease_id", "chat_id", "source_sha256")):
        raise package.PackageError("COORDINATED-WORK owner binding incomplete")
    if (not isinstance(receipt.get("source_claims"), list) or not receipt["source_claims"]
            or not all(isinstance(item, str) and item.strip() for item in receipt["source_claims"])):
        raise package.PackageError("source claims binding missing")
    if package.checked_path(receipt.get("candidate_root"), kind="dir") != root:
        raise package.PackageError("receipt candidate-root binding mismatch")
    directory = package.checked_path(root / candidate["candidate_id"], kind="dir")
    if package.checked_path(receipt.get("candidate_path"), kind="file") != directory / "SKILL.md":
        raise package.PackageError("candidate path must bind the exact candidate directory SKILL.md")
    members = package.normalize_members(receipt.get("candidate_member_set"))
    if receipt.get("candidate_member_set") != members:
        raise package.PackageError("receipt member-set must be canonical")
    schema = receipt.get("schema_version")
    mode = receipt.get("support_mode")
    if schema == "mgskill-inactive-candidate-proposal-v1" and mode == "instruction-only":
        if len(members) != 1 or members[0]["path"] != "SKILL.md":
            raise package.PackageError("v1 remains instruction-only")
    elif schema == "mgskill-inactive-candidate-proposal-v2" and mode == "resource-package":
        spec = candidate.get("package")
        if not isinstance(spec, dict) or set(spec) != {"root", "members", "manifest_sha256"}:
            raise package.PackageError("v2 candidate package binding missing")
        if package.normalize_members(spec["members"]) != members or package.require_hash(spec["manifest_sha256"], "package manifest") != package.package_manifest(members):
            raise package.PackageError("v2 source and materialized member-set identities differ")
        if package.checked_path(receipt.get("candidate_package_root"), kind="dir") != directory:
            raise package.PackageError("v2 candidate package-root binding mismatch")
        if receipt.get("candidate_package_manifest_sha256") != package.package_manifest(members):
            raise package.PackageError("v2 materialized package manifest mismatch")
    else:
        raise package.PackageError("unsupported materialized schema/support_mode")
    source = package.capture_package(directory, members)
    skill_sha = source.files["SKILL.md"].sha256
    if (skill_sha != package.require_hash(candidate.get("artifact_sha256"), "source artifact")
            or skill_sha != receipt.get("candidate_artifact_sha256")):
        raise package.PackageError("candidate artifact/current bytes binding invalid")
    return source, candidate, owner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("proposal", "candidate-root", "isolated-adoption-root", "expected-destination-manifest-sha256", "registry", "expected-registry-sha256", "registry-entry"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--discovery-root", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    destination: Path | None = None
    try:
        receipt, receipt_bytes = package.read_json_file(args.proposal)
        if not args.apply or not isinstance(receipt, dict) or receipt.get("decision") != "INACTIVE_CANDIDATE_MATERIALIZED":
            return emit({"decision": "HOLD_ADOPTION", "writes_performed": [],
                         "reason": "materialized receipt and explicit --apply required"}, 3)
        candidate_root = package.checked_path(args.candidate_root, kind="dir")
        source, candidate, owner = receipt_package(receipt, candidate_root)
        discoveries = [package.checked_path(item, kind="dir") for item in args.discovery_root]
        if any(package.intersects(candidate_root, item) for item in discoveries):
            raise package.PackageError("candidate source intersects active discovery root")
        if args.expected_destination_manifest_sha256.upper() != "ABSENT":
            raise package.PackageError("destination CAS requires ABSENT")
        destination = package.checked_path(args.isolated_adoption_root, kind="dir", absent=True)
        if package.intersects(destination, candidate_root) or any(package.intersects(destination, item) for item in discoveries):
            raise package.PackageError("destination intersects candidate or active discovery root")
        source_registry, registry_bytes = package.read_json_file(args.registry)
        registry = registry_bytes.path
        if registry_bytes.sha256 != package.require_hash(args.expected_registry_sha256, "registry SHA256"):
            raise package.PackageError("source registry CAS mismatch")
        if any(package.is_under(item, destination) for item in (registry, receipt_bytes.path)):
            raise package.PackageError("destination intersects a source/control file")
        if not isinstance(source_registry, dict) or source_registry.get("schema_version") != "mgskill-registry-v1" or not isinstance(source_registry.get("entries"), list) or not all(isinstance(item, dict) for item in source_registry["entries"]):
            raise package.PackageError("source registry object/schema/entries invalid")
        entry = package.strict_json(args.registry_entry)
        required = {"skill_id", "name", "version", "origin", "relative_path", "status", "match_clauses", "files"}
        if (not isinstance(entry, dict) or not required <= set(entry) or entry.get("status") != "candidate"
                or entry.get("name") != candidate["candidate_id"] or entry.get("relative_path") != candidate["candidate_id"]
                or entry.get("files") != source.members):
            raise package.PackageError("entry must exactly bind candidate path/name/canonical full member-set with status candidate")
        if any(not isinstance(entry[key], str) or not entry[key].strip() for key in ("skill_id", "version", "origin")) or not isinstance(entry["match_clauses"], list):
            raise package.PackageError("registry entry identity/match fields invalid")
        if any(item.get("skill_id") == entry["skill_id"] or item.get("name") == entry["name"] for item in source_registry["entries"]):
            raise package.PackageError("duplicate registry identity")
        registry_name = package.relative_name(registry.name)
        backup_name = package.relative_name(registry.name + ".backup")
        if candidate["candidate_id"].casefold() in {registry_name.casefold(), backup_name.casefold()}:
            raise package.PackageError("registry/backup filename collides with candidate directory")
        staged_registry_object = {**source_registry, "entries": [*source_registry["entries"], entry]}
        staged_registry_bytes = (json.dumps(staged_registry_object, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        expected_stage = package.normalize_members([
            *[{"path": f"{candidate['candidate_id']}/{row['path']}", "sha256": row["sha256"]} for row in source.members],
            {"path": registry_name, "sha256": sha_bytes(staged_registry_bytes)},
            {"path": backup_name, "sha256": registry_bytes.sha256},
        ], require_skill=False)
        publish_method = package.publish_capability()
        stage = package.checked_path(destination.parent / f".{destination.name}.stage-{uuid.uuid4().hex}", kind="dir", absent=True)
        if package.intersects(stage, candidate_root) or any(package.intersects(stage, item) for item in discoveries):
            raise package.PackageError("stage intersects source or active discovery root")
        package.assert_package_unchanged(source)
        package.assert_file_unchanged(registry_bytes)
        package.assert_file_unchanged(receipt_bytes)
        package.checked_path(destination, kind="dir", absent=True)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return fail(str(exc), "PREPARED", None, destination, [])

    state = "PREPARED"
    writes: list[str] = []
    stage_id: tuple[int, int] | None = None
    try:
        writes.append(str(stage))
        stage.mkdir(exist_ok=False)
        stage_id = package.identity(stage.lstat())
        staged_package = stage / candidate["candidate_id"]
        writes.append(str(staged_package))
        staged_package.mkdir(exist_ok=False)
        package.copy_package(source, staged_package, writes)
        fault("after-skill-copy")
        backup = stage / backup_name
        writes.append(str(backup))
        package.write_exclusive(backup, registry_bytes.data)
        fault("after-backup")
        staged_registry = stage / registry_name
        writes.append(str(staged_registry))
        package.write_exclusive(staged_registry, staged_registry_bytes)
        state = "STAGED"
        fault("after-registry-stage")
        package.verify_tree(stage, expected_stage, require_skill=False)
        package.assert_package_unchanged(source)
        package.assert_file_unchanged(registry_bytes)
        package.assert_file_unchanged(receipt_bytes)
        state = "COMMITTING"
        fault("before-rename")
        package.publish_directory(stage, destination)
        writes.append(str(destination))
        state = "COMMITTED"
        fault("after-rename")
        package.verify_tree(destination, expected_stage, require_skill=False)
        current = readback(destination)
        expected_manifest = package.root_manifest_from_members(expected_stage)
        if current.get("manifest_sha256") != expected_manifest:
            raise package.PackageError("committed readback mismatch")
        fault("after-readback")
        state = "VERIFIED"
        fault("after-verified")
        result = {
            "decision": "ISOLATED_ADOPTION_STAGED", "transaction_state": state, "effect_state": "confirmed",
            "materialization_receipt_sha256": receipt["materialization_receipt_sha256"],
            "candidate_id": candidate["candidate_id"], "owner": owner, "source_claims": receipt["source_claims"],
            "destination_manifest_sha256": current["manifest_sha256"], "registry_backup_sha256": sha(destination / backup_name),
            "registry_sha256": sha(destination / registry_name), "skill_path": str(destination / candidate["candidate_id"] / "SKILL.md"),
            "skill_sha256": sha(destination / candidate["candidate_id"] / "SKILL.md"),
            "package_root": str(destination / candidate["candidate_id"]), "package_member_set": source.members,
            "package_manifest_sha256": source.manifest_sha256, "support_mode": receipt["support_mode"],
            "publish_method": publish_method, "readback": current,
            "rollback": "remove only this destination root after explicit recovery decision; source registry/candidate unchanged",
            "proof_ceiling": "isolated exact package and candidate registry staging; no activation, selection, execution or benefit claim",
            "writes_performed": writes,
        }
        result["receipt_sha256"] = package.sha_value(result)
        return emit(result)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return fail(str(exc), state, stage, destination, writes, stage_id=stage_id)


if __name__ == "__main__":
    sys.exit(main())
