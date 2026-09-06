#!/usr/bin/env python3
"""Validate an inactive Skill candidate proposal; this command never installs it."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any


KINDS = {"CREATE", "REVISE", "MERGE", "SUPERSEDE", "RETIRE"}
SAFE_CANDIDATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def root_manifest(root: Path) -> str:
    rows = []
    for item in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.as_posix()):
        rows.append(f"{item.relative_to(root).as_posix()}\t{digest(item)}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest().upper()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def receipt_sha(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({key: item for key, item in value.items() if key != "materialization_receipt_sha256"}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()


def error(messages: list[str]) -> int:
    print(json.dumps({"decision": "REJECTED_INACTIVE_CANDIDATE", "errors": sorted(set(messages)), "writes_performed": []}, sort_keys=True, indent=2))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--candidate-root")
    parser.add_argument("--expected-root-manifest-sha256")
    parser.add_argument("--discovery-root", action="append")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = Path(args.input).resolve(strict=True)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    required = {"schema_version", "operation", "event_id", "objective_id", "source_claims", "owner", "candidate", "evidence", "rollback", "independent_challenge", "proof_ceiling"}
    if set(data) != required or data.get("schema_version") != "mgskill-inactive-candidate-v1":
        errors.append("candidate fields/schema differ")
    if data.get("operation") not in KINDS:
        errors.append("invalid operation")
    if not isinstance(data.get("source_claims"), list) or not data["source_claims"] or not all(isinstance(x, str) and x.strip() for x in data["source_claims"]):
        errors.append("source_claims required")
    owner = data.get("owner")
    if not isinstance(owner, dict) or owner.get("lane") not in {"WORK", "COORDINATED-WORK"}:
        errors.append("owner lane must be WORK or COORDINATED-WORK")
    elif owner["lane"] == "COORDINATED-WORK" and any(not isinstance(owner.get(k), str) or not owner[k].strip() for k in ("task_id", "lease_id", "chat_id", "source_sha256")):
        errors.append("COORDINATED-WORK requires task_id/lease_id/chat_id/source_sha256")
    candidate = data.get("candidate")
    if not isinstance(candidate, dict) or set(candidate) != {"candidate_id", "artifact_path", "artifact_sha256", "prior_candidate_id", "equivalent_fingerprints"}:
        errors.append("candidate fields differ")
    else:
        try:
            artifact = Path(candidate["artifact_path"]).resolve(strict=True)
            if digest(artifact) != str(candidate["artifact_sha256"]).upper():
                errors.append("artifact_sha256 mismatch")
            if artifact.name.lower() != "skill.md":
                errors.append("candidate artifact must be SKILL.md")
        except (OSError, TypeError):
            errors.append("candidate artifact unreadable")
        if not isinstance(candidate.get("equivalent_fingerprints"), list) or any(not isinstance(item, str) or len(item) != 64 for item in candidate["equivalent_fingerprints"]):
            errors.append("equivalent_fingerprints must be SHA-256 strings")
        if not isinstance(candidate.get("candidate_id"), str) or not SAFE_CANDIDATE_ID.fullmatch(candidate["candidate_id"]):
            errors.append("candidate_id must be one safe lowercase basename")
    challenge = data.get("independent_challenge")
    if not isinstance(challenge, dict) or challenge.get("status") not in {"pending", "completed"}:
        errors.append("independent challenge required")
    if not isinstance(data.get("evidence"), dict) or not data["evidence"]:
        errors.append("evidence required")
    if not isinstance(data.get("rollback"), dict) or not data["rollback"]:
        errors.append("rollback required")
    if errors:
        return error(errors)
    proposal = {
        "schema_version": "mgskill-inactive-candidate-proposal-v1",
        "decision": "INACTIVE_CANDIDATE_READY" if challenge["status"] == "completed" else "INACTIVE_CANDIDATE_HOLD_INDEPENDENT_CHALLENGE",
        "operation": data["operation"], "event_id": data["event_id"], "objective_id": data["objective_id"],
        "source_claims": data["source_claims"], "owner": owner, "candidate": candidate,
        "adoption_required": {"explicit_scope": True, "frozen_evidence": True, "destination_cas": True, "backup": True, "rollback": True},
        "proof_ceiling": "inactive artifact identity and candidate routing only; adoption, discovery, use, effect and consumer benefit unproven",
        "writes_performed": [],
    }
    proposal["proposal_sha256"] = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()
    if not args.apply:
        print(json.dumps(proposal, sort_keys=True, indent=2))
        return 0 if proposal["decision"] == "INACTIVE_CANDIDATE_READY" else 3
    if proposal["decision"] != "INACTIVE_CANDIDATE_READY" or not args.candidate_root or not args.expected_root_manifest_sha256 or not args.discovery_root:
        return error(["--apply requires a ready proposal, --candidate-root, --expected-root-manifest-sha256 and --discovery-root"])
    root = Path(args.candidate_root).resolve(strict=True)
    discovery_roots = [Path(item).resolve(strict=True) for item in args.discovery_root]
    if any(root == discovery or is_under(root, discovery) or is_under(discovery, root) for discovery in discovery_roots):
        return error(["candidate root must be disjoint from every discovery root"])
    if root_manifest(root) != args.expected_root_manifest_sha256.upper():
        return error(["candidate root CAS manifest mismatch"])
    source = Path(candidate["artifact_path"]).resolve(strict=True)
    destination = root / candidate["candidate_id"]
    if not is_under(destination, root) or destination.parent != root or destination.exists() or source == destination / "SKILL.md":
        return error(["candidate destination already exists or aliases source"])
    try:
        destination.mkdir(parents=False, exist_ok=False)
        staged = destination / "SKILL.md"
        shutil.copy2(source, staged)
        readback = digest(staged)
        if readback != candidate["artifact_sha256"].upper():
            raise RuntimeError("post-copy artifact hash mismatch")
        receipt = {**proposal, "decision": "INACTIVE_CANDIDATE_MATERIALIZED", "candidate_root": str(root), "candidate_path": str(staged), "candidate_artifact_sha256": readback, "candidate_member_set": [{"path": "SKILL.md", "sha256": readback}], "support_mode": "instruction-only", "pre_root_manifest_sha256": args.expected_root_manifest_sha256.upper(), "post_root_manifest_sha256": root_manifest(root), "readback": {"status": "observed", "artifact_sha256": readback, "destination_exists": staged.is_file()}, "rollback": {"method": "remove only newly created candidate directory", "target": str(destination), "pre_root_manifest_sha256": args.expected_root_manifest_sha256.upper()}}
        receipt["materialization_receipt_sha256"] = receipt_sha(receipt)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"decision": "MATERIALIZATION_OUTCOME_UNKNOWN", "error": str(exc), "candidate_path": str(destination), "readback": {"exists": destination.exists()}, "writes_performed": [str(destination)] if destination.exists() else []}, sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
