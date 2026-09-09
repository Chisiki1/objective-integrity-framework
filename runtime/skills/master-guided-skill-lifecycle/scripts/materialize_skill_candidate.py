#!/usr/bin/env python3
"""Materialize one exact inactive Skill package; never install or execute it."""
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


KINDS = {"CREATE", "REVISE", "MERGE", "SUPERSEDE", "RETIRE"}
SAFE_CANDIDATE_ID = package.SAFE_NAME
SCHEMAS = {"mgskill-inactive-candidate-v1", "mgskill-inactive-candidate-v2"}


def digest(path: Path) -> str:
    return package.read_file(path).sha256


root_manifest = package.root_manifest
is_under = package.is_under


def receipt_sha(value: dict[str, Any]) -> str:
    return package.sha_value({key: item for key, item in value.items() if key != "materialization_receipt_sha256"})


def emit(value: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))
    return code


def error(messages: list[str]) -> int:
    return emit({"decision": "REJECTED_INACTIVE_CANDIDATE", "errors": sorted(set(messages)),
                 "effect_state": "none", "writes_performed": []}, 2)


def fault(point: str) -> None:
    if os.environ.get("MGSKILL_TEST_MATERIALIZATION_FAULT") == point:
        raise RuntimeError(f"test fault:{point}")


def validate_input(data: Any) -> tuple[dict[str, Any], package.PackageBytes, str]:
    required = {"schema_version", "operation", "event_id", "objective_id", "source_claims", "owner", "candidate", "evidence", "rollback", "independent_challenge", "proof_ceiling"}
    if not isinstance(data, dict) or set(data) != required or data.get("schema_version") not in SCHEMAS:
        raise package.PackageError("candidate fields/schema differ")
    if data["operation"] not in KINDS:
        raise package.PackageError("invalid operation")
    if any(not isinstance(data[key], str) or not data[key].strip() for key in ("event_id", "objective_id", "proof_ceiling")):
        raise package.PackageError("event_id, objective_id and proof_ceiling required")
    if (not isinstance(data["source_claims"], list) or not data["source_claims"]
            or not all(isinstance(item, str) and item.strip() for item in data["source_claims"])):
        raise package.PackageError("source_claims required")
    owner = data["owner"]
    if not isinstance(owner, dict) or owner.get("lane") not in {"WORK", "COORDINATED-WORK"}:
        raise package.PackageError("owner lane must be WORK or COORDINATED-WORK")
    if owner["lane"] == "COORDINATED-WORK" and any(not isinstance(owner.get(key), str) or not owner[key].strip() for key in ("task_id", "lease_id", "chat_id", "source_sha256")):
        raise package.PackageError("COORDINATED-WORK requires task_id/lease_id/chat_id/source_sha256")
    candidate = data["candidate"]
    fields = {"candidate_id", "artifact_path", "artifact_sha256", "prior_candidate_id", "equivalent_fingerprints"}
    is_v2 = data["schema_version"].endswith("-v2")
    if not isinstance(candidate, dict) or set(candidate) != fields | ({"package"} if is_v2 else set()):
        raise package.PackageError("candidate fields differ")
    if not isinstance(candidate["candidate_id"], str) or not SAFE_CANDIDATE_ID.fullmatch(candidate["candidate_id"]):
        raise package.PackageError("candidate_id must be one safe lowercase basename")
    if not isinstance(candidate["equivalent_fingerprints"], list):
        raise package.PackageError("equivalent_fingerprints must be SHA-256 strings")
    for item in candidate["equivalent_fingerprints"]:
        package.require_hash(item, "equivalent fingerprint")
    artifact = package.checked_path(candidate["artifact_path"], kind="file")
    artifact_sha = package.require_hash(candidate["artifact_sha256"], "artifact_sha256")
    if artifact.name.lower() != "skill.md":
        raise package.PackageError("candidate artifact must be SKILL.md")
    if is_v2:
        spec = candidate["package"]
        if not isinstance(spec, dict) or set(spec) != {"root", "members", "manifest_sha256"}:
            raise package.PackageError("package has exactly root, members and manifest_sha256")
        source = package.capture_package(spec["root"], spec["members"], expected_manifest=spec["manifest_sha256"])
        if artifact != source.root / "SKILL.md" or source.files["SKILL.md"].sha256 != artifact_sha:
            raise package.PackageError("artifact must exactly bind package-root SKILL.md")
        mode = "resource-package"
    else:
        source = package.capture_package(artifact.parent, [{"path": "SKILL.md", "sha256": artifact_sha}], legacy_artifact=artifact)
        mode = "instruction-only"
    challenge = data["independent_challenge"]
    if not isinstance(challenge, dict) or challenge.get("status") not in {"pending", "completed"}:
        raise package.PackageError("independent challenge required")
    for key in ("evidence", "rollback"):
        if not isinstance(data[key], dict) or not data[key]:
            raise package.PackageError(f"{key} required")
    proposal = {
        "schema_version": "mgskill-inactive-candidate-proposal-v2" if is_v2 else "mgskill-inactive-candidate-proposal-v1",
        "decision": "INACTIVE_CANDIDATE_READY" if challenge["status"] == "completed" else "INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE",
        "operation": data["operation"], "event_id": data["event_id"], "objective_id": data["objective_id"],
        "source_claims": data["source_claims"], "owner": owner, "candidate": candidate,
        "adoption_required": {"explicit_scope": True, "frozen_evidence": True, "destination_cas": True, "backup": True, "rollback": True},
        "proof_ceiling": "inactive exact file-package identity and candidate routing only; no activation, execution or benefit claim",
        "writes_performed": [],
    }
    proposal["proposal_sha256"] = package.sha_value(proposal)
    return proposal, source, mode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--candidate-root")
    parser.add_argument("--expected-root-manifest-sha256")
    parser.add_argument("--discovery-root", action="append")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        data, input_bytes = package.read_json_file(args.input)
        proposal, source, mode = validate_input(data)
        if not args.apply:
            return emit(proposal, 0 if proposal["decision"] == "INACTIVE_CANDIDATE_READY" else 3)
        if proposal["decision"] != "INACTIVE_CANDIDATE_READY" or not args.candidate_root or not args.expected_root_manifest_sha256 or not args.discovery_root:
            return error(["--apply requires a ready proposal, --candidate-root, --expected-root-manifest-sha256 and --discovery-root"])
        root = package.checked_path(args.candidate_root, kind="dir")
        discoveries = [package.checked_path(item, kind="dir") for item in args.discovery_root]
        if any(package.intersects(root, item) for item in discoveries):
            raise package.PackageError("candidate root must be disjoint from every discovery root")
        candidate_id = proposal["candidate"]["candidate_id"]
        destination = package.checked_path(root / candidate_id, kind="dir", absent=True)
        if package.intersects(destination, source.root) or package.is_under(input_bytes.path, destination):
            raise package.PackageError("candidate destination intersects source package/control input")
        before_members = package.tree_members(root)
        expected_root = package.require_hash(args.expected_root_manifest_sha256, "candidate root manifest")
        if package.root_manifest_from_members(before_members) != expected_root:
            raise package.PackageError("candidate root CAS manifest mismatch")
        publish_method = package.publish_capability()
        stage = package.checked_path(root / f".{candidate_id}.stage-{uuid.uuid4().hex}", kind="dir", absent=True)
        if package.intersects(stage, source.root):
            raise package.PackageError("staging directory intersects source package")
        package.assert_package_unchanged(source)
        package.assert_file_unchanged(input_bytes)
        if root_manifest(root) != expected_root:
            raise package.PackageError("candidate root CAS changed before first write")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return error([str(exc)])

    state = "PREPARED"
    writes: list[str] = []
    stage_id: tuple[int, int] | None = None
    try:
        writes.append(str(stage))
        stage.mkdir(exist_ok=False)
        stage_id = package.identity(stage.lstat())
        package.copy_package(source, stage, writes)
        state = "STAGED"
        fault("after-skill-copy")
        package.assert_package_unchanged(source)
        package.assert_file_unchanged(input_bytes)
        if root_manifest(root, exclude=stage) != expected_root:
            raise package.PackageError("candidate root CAS changed during staging")
        package.verify_tree(stage, source.members)
        state = "COMMITTING"
        fault("before-rename")
        package.publish_directory(stage, destination)
        state = "COMMITTED"
        writes.append(str(destination))
        fault("after-rename")
        members = package.verify_tree(destination, source.members)
        after_manifest = root_manifest(root)
        expected_after = package.root_manifest_from_members(before_members + [
            {"path": f"{candidate_id}/{row['path']}", "sha256": row["sha256"]} for row in members
        ])
        if after_manifest != expected_after:
            raise package.PackageError("post-commit candidate-root manifest contains an unowned change")
        fault("after-readback")
        state = "VERIFIED"
        fault("after-verified")
        skill_sha = source.files["SKILL.md"].sha256
        receipt = {
            **proposal, "decision": "INACTIVE_CANDIDATE_MATERIALIZED", "transaction_state": state,
            "effect_state": "confirmed", "candidate_root": str(root), "candidate_path": str(destination / "SKILL.md"),
            "candidate_package_root": str(destination), "candidate_artifact_sha256": skill_sha,
            "candidate_member_set": members, "candidate_package_manifest_sha256": source.manifest_sha256,
            "support_mode": mode, "publish_method": publish_method,
            "pre_root_manifest_sha256": expected_root, "post_root_manifest_sha256": after_manifest,
            "readback": {"status": "observed", "artifact_sha256": skill_sha, "destination_exists": True,
                         "package_manifest_sha256": source.manifest_sha256, "member_set": members},
            "rollback": {"method": "remove only newly created candidate directory after an explicit recovery decision",
                         "target": str(destination), "pre_root_manifest_sha256": expected_root},
            "writes_performed": writes,
        }
        receipt["materialization_receipt_sha256"] = receipt_sha(receipt)
        return emit(receipt)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        failure = package.failure_record(str(exc), state, stage, destination, writes, stage_id=stage_id)
        return emit({"decision": "MATERIALIZATION_OUTCOME_UNKNOWN", "error": str(exc),
                     "candidate_path": str(destination), **failure}, 2)


if __name__ == "__main__":
    sys.exit(main())
