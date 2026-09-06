#!/usr/bin/env python3
"""Validate mgskill registry identities, paths, manifests, and master links."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


ALLOWED_STATES = {"candidate", "shadow", "audited", "active-bounded", "measured", "superseded", "retired"}
SELECTABLE = {"active-bounded", "measured"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--user-root", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--master", action="append", required=True)
    args = parser.parse_args()

    registry_path = Path(args.registry).resolve(strict=True)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    roots = {
        "user": Path(args.user_root).resolve(strict=True),
        "project": Path(args.project_root).resolve(strict=True),
    }
    master_text = "\n".join(Path(item).read_text(encoding="utf-8") for item in args.master)
    entries = registry.get("entries", []) if isinstance(registry, dict) else []
    errors: list[str] = []
    warnings: list[str] = []
    if registry.get("schema_version") != "mgskill-registry-v1":
        errors.append("unsupported registry schema")
    if not isinstance(entries, list):
        errors.append("entries must be an array")
        entries = []

    id_counts = Counter(str(entry.get("skill_id")) for entry in entries if isinstance(entry, dict))
    for value, count in sorted(id_counts.items()):
        if not value or value == "None" or count != 1:
            errors.append(f"skill_id count invalid:{value}:{count}")
    active_name_counts = Counter(
        str(entry.get("name", "")).casefold()
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") in SELECTABLE
    )
    for value, count in sorted(active_name_counts.items()):
        if not value or count != 1:
            errors.append(f"active name conflict:{value}:{count}")

    receipts = []
    for entry in sorted((item for item in entries if isinstance(item, dict)), key=lambda item: str(item.get("skill_id", ""))):
        skill_id = str(entry.get("skill_id", ""))
        status = entry.get("status")
        if status not in ALLOWED_STATES:
            errors.append(f"{skill_id}:invalid status:{status}")
        origin = entry.get("origin")
        root = roots.get(str(origin))
        if root is None:
            errors.append(f"{skill_id}:invalid origin:{origin}")
            continue
        skill_path = root / str(entry.get("relative_path", ""))
        if status in {"retired", "superseded"}:
            if skill_path.exists():
                errors.append(f"{skill_id}:{status} copy remains in discovery root")
            evidence_value = entry.get("evidence_path")
            if not isinstance(evidence_value, str) or not evidence_value.strip():
                errors.append(f"{skill_id}:{status} missing evidence_path")
                continue
            if not Path(evidence_value).is_absolute():
                errors.append(f"{skill_id}:{status} evidence_path must be absolute")
                continue
            try:
                evidence_path = Path(evidence_value).resolve(strict=True)
            except FileNotFoundError:
                errors.append(f"{skill_id}:{status} evidence_path missing")
                continue
            if any(is_under(evidence_path, active_root) for active_root in roots.values()):
                errors.append(f"{skill_id}:{status} evidence_path remains under a discovery root")
            evidence_items = entry.get("evidence_files")
            if not isinstance(evidence_items, list) or not evidence_items:
                errors.append(f"{skill_id}:{status} missing evidence_files")
                evidence_items = []
            expected_evidence = {
                str(item.get("path")): str(item.get("sha256", "")).upper()
                for item in evidence_items if isinstance(item, dict)
            }
            actual_evidence = {
                path.relative_to(evidence_path).as_posix(): sha256_file(path)
                for path in sorted((item for item in evidence_path.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold())
            }
            if set(expected_evidence) != set(actual_evidence):
                errors.append(f"{skill_id}:{status} evidence file set mismatch")
            if any(expected_evidence[path] != actual_evidence[path] for path in set(expected_evidence) & set(actual_evidence)):
                errors.append(f"{skill_id}:{status} evidence hash mismatch")
            lineage = entry.get("lineage")
            if not isinstance(lineage, dict) or not all(lineage.get(field) for field in ("reason", "effective_utc", "prior_status")):
                errors.append(f"{skill_id}:{status} incomplete lineage")
            if status == "superseded":
                replacement = lineage.get("replacement") if isinstance(lineage, dict) else None
                if not isinstance(replacement, dict) or not all(replacement.get(field) for field in ("skill_id", "version", "origin")):
                    errors.append(f"{skill_id}:superseded missing replacement identity")
                else:
                    matches = [
                        candidate
                        for candidate in entries
                        if isinstance(candidate, dict)
                        and candidate.get("skill_id") == replacement.get("skill_id")
                        and candidate.get("version") == replacement.get("version")
                        and candidate.get("origin") == replacement.get("origin")
                    ]
                    if len(matches) != 1:
                        errors.append(f"{skill_id}:superseded replacement identity does not resolve uniquely")
            receipts.append({"skill_id": skill_id, "status": status, "evidence_path": str(evidence_path), "evidence_files": actual_evidence, "lineage": lineage})
            continue
        try:
            real_path = skill_path.resolve(strict=True)
        except FileNotFoundError:
            errors.append(f"{skill_id}:skill path missing")
            continue
        if not is_under(real_path, root):
            errors.append(f"{skill_id}:skill path escapes origin root")
            continue

        expected_items = entry.get("files", [])
        if not isinstance(expected_items, list) or not expected_items:
            errors.append(f"{skill_id}:empty file manifest")
            expected_items = []
        expected = {str(item.get("path")): str(item.get("sha256", "")).upper() for item in expected_items if isinstance(item, dict)}
        actual = {}
        for file_path in sorted((path for path in real_path.rglob("*") if path.is_file()), key=lambda path: path.as_posix().casefold()):
            rel = file_path.relative_to(real_path).as_posix()
            if file_path.resolve() == registry_path:
                continue
            actual[rel] = sha256_file(file_path)
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        mismatch = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
        if missing:
            errors.append(f"{skill_id}:manifest missing files:{missing}")
        if extra:
            errors.append(f"{skill_id}:manifest extra files:{extra}")
        if mismatch:
            errors.append(f"{skill_id}:manifest hash mismatch:{mismatch}")

        links = entry.get("master_links", [])
        if not isinstance(links, list) or not links:
            errors.append(f"{skill_id}:no master links")
            links = []
        absent_links = sorted(str(link) for link in links if str(link) not in master_text)
        if absent_links:
            errors.append(f"{skill_id}:unresolved master links:{absent_links}")

        receipts.append({"skill_id": skill_id, "path": str(real_path), "files": actual, "master_links": links})

    result = {
        "schema_version": "mgskill-registry-validation-v1",
        "registry_path": str(registry_path),
        "registry_sha256": sha256_file(registry_path),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "receipts": receipts,
        "proof_ceiling": "structural identity/path/hash/status/master-reference closure only",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
