#!/usr/bin/env python3
"""Deterministic, hash-bound workflow-skill resolver.

Structural/mechanical evidence only. The workflow and current masters remain authoritative.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


INPUT_FIELDS = (
    "job",
    "action",
    "tool",
    "environment",
    "cause_families",
    "permissions",
    "resources",
    "consumers",
    "risks",
)
SELECTABLE = {"active-bounded", "measured"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def norm_tokens(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    return sorted({item.strip().casefold() for item in value if item.strip()})


def parse_time(value: str | None) -> tuple[dt.datetime, str]:
    if value:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        parsed = parsed.astimezone(dt.timezone.utc)
    else:
        parsed = dt.datetime.now(dt.timezone.utc)
    rendered = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    return parsed, rendered


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def get_reparse_components(path: Path, stop: Path) -> list[dict[str, str]]:
    lexical_path = lexical_absolute(path)
    lexical_stop = lexical_absolute(stop)
    if not is_under(lexical_path, lexical_stop):
        raise ValueError(f"lexical path escapes origin root:{lexical_path}")
    components = [lexical_stop]
    current = lexical_stop
    for part in lexical_path.relative_to(lexical_stop).parts:
        current = current / part
        components.append(current)
    result: list[dict[str, str]] = []
    for component in components:
        try:
            stat = component.lstat()
            if component.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400):
                result.append(
                    {
                        "lexical_path": str(component),
                        "resolved_target": str(component.resolve(strict=True)),
                    }
                )
        except FileNotFoundError:
            pass
    return result


def clause_matches(clause: dict[str, Any], facts: dict[str, set[str]]) -> tuple[bool, str]:
    checked: list[str] = []
    for field, expected in sorted(clause.items()):
        if field not in INPUT_FIELDS:
            return False, f"unsupported clause field:{field}"
        if not isinstance(expected, list) or not expected:
            return False, f"invalid empty clause field:{field}"
        expected_norm = {str(item).strip().casefold() for item in expected}
        overlap = expected_norm & facts[field]
        if not overlap:
            return False, f"{field} lacked any of {sorted(expected_norm)}"
        checked.append(f"{field}={sorted(overlap)}")
    return True, "; ".join(checked)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    input_path = Path(args.input).resolve(strict=True)
    registry_path = Path(args.registry).resolve(strict=True)
    request = load_json(input_path)
    registry_raw = registry_path.read_bytes()
    registry = json.loads(registry_raw.decode("utf-8"))

    errors: list[str] = []
    if request.get("schema_version") != "mgskill-resolve-input-v1":
        errors.append("unsupported input schema_version")
    if registry.get("schema_version") != "mgskill-registry-v1":
        errors.append("unsupported registry schema_version")
    if not isinstance(request.get("objective_id"), str) or not request["objective_id"].strip():
        errors.append("objective_id is required")
    if not isinstance(request.get("source_claims"), list) or not request["source_claims"]:
        errors.append("source_claims must be a non-empty array")
    blind_phase = request.get("blind_phase", "none")
    if blind_phase not in {"none", "initial", "reconciled"}:
        errors.append("blind_phase must be none, initial, or reconciled")

    facts: dict[str, set[str]] = {}
    for field in INPUT_FIELDS:
        try:
            facts[field] = set(norm_tokens(request.get(field, []), field))
        except ValueError as exc:
            errors.append(str(exc))
            facts[field] = set()

    try:
        now, now_text = parse_time(request.get("now_utc"))
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
        now, now_text = parse_time(None)

    user_root = lexical_absolute(Path(args.user_root))
    project_root = lexical_absolute(Path(args.project_root)) if args.project_root else None
    roots = {"user": user_root, "project": project_root}
    entries = registry.get("entries", [])
    if not isinstance(entries, list):
        errors.append("registry entries must be an array")
        entries = []

    duplicate_names: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") not in SELECTABLE:
            continue
        name = str(entry.get("name", "")).casefold()
        duplicate_names.setdefault(name, []).append(str(entry.get("skill_id", "")))
    duplicate_names = {name: ids for name, ids in duplicate_names.items() if name and len(ids) > 1}

    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for entry in sorted((e for e in entries if isinstance(e, dict)), key=lambda e: str(e.get("skill_id", ""))):
        skill_id = str(entry.get("skill_id", ""))
        name = str(entry.get("name", ""))
        if blind_phase == "initial" and entry.get("disclosure_class") != "blind_safe_mechanical":
            withheld.append(
                {
                    "withheld": True,
                    "reason_code": "non_blind_metadata_withheld_during_initial_derivation",
                    "release_condition": "blind derivation frozen; parent reruns with blind_phase=reconciled",
                }
            )
            continue
        reasons: list[str] = []
        if entry.get("status") not in SELECTABLE:
            reasons.append(f"status {entry.get('status')} is not selectable")
        if name.casefold() in duplicate_names:
            reasons.append(f"duplicate active name conflicts with {duplicate_names[name.casefold()]}")
        expiry = entry.get("expires_utc")
        if expiry:
            try:
                expires, _ = parse_time(str(expiry))
                if now >= expires:
                    reasons.append(f"expired at {expiry}")
            except ValueError:
                reasons.append("invalid expires_utc")

        clauses = entry.get("match_clauses", [])
        matched_reasons: list[str] = []
        if not isinstance(clauses, list) or not clauses:
            reasons.append("no match clauses")
        else:
            for clause in clauses:
                if not isinstance(clause, dict):
                    continue
                matched, why = clause_matches(clause, facts)
                if matched:
                    matched_reasons.append(why)
            if not matched_reasons:
                reasons.append("no trigger clause matched")

        origin = entry.get("origin")
        root = roots.get(str(origin))
        reparse_components: list[dict[str, str]] = []
        if root is None:
            reasons.append(f"origin root unavailable:{origin}")
            candidate = None
        else:
            relative = Path(str(entry.get("relative_path", "")))
            candidate = root / relative
            try:
                real_root = root.resolve(strict=True)
                reparse_components = get_reparse_components(candidate, root)
                real_candidate = candidate.resolve(strict=True)
                if not is_under(real_candidate, real_root):
                    reasons.append("canonical path escapes origin root")
            except FileNotFoundError:
                real_root = root.resolve()
                real_candidate = candidate.resolve()
                reasons.append("skill path missing")

        file_receipts: list[dict[str, str]] = []
        directory_set_sha256: str | None = None
        if candidate is not None and not any(reason.endswith("missing") or "escapes" in reason for reason in reasons):
            manifest = entry.get("files", [])
            if not isinstance(manifest, list) or not manifest:
                reasons.append("empty file manifest")
            else:
                expected = {
                    str(item.get("path")): str(item.get("sha256", "")).upper()
                    for item in manifest
                    if isinstance(item, dict)
                }
                if len(expected) != len(manifest):
                    reasons.append("invalid or duplicate file manifest member")
                actual_map: dict[str, str] = {}
                real_skill = candidate.resolve(strict=True)
                for file_path in sorted((path for path in real_skill.rglob("*") if path.is_file()), key=lambda path: path.as_posix().casefold()):
                    rel = file_path.relative_to(real_skill).as_posix()
                    if file_path.resolve(strict=True) == registry_path:
                        continue
                    real_file = file_path.resolve(strict=True)
                    if not is_under(real_file, real_skill):
                        reasons.append(f"directory file escapes skill root:{rel}")
                        continue
                    actual_map[rel] = sha256_file(real_file)
                missing = sorted(set(expected) - set(actual_map))
                extra = sorted(set(actual_map) - set(expected))
                mismatch = sorted(path for path in set(expected) & set(actual_map) if expected[path] != actual_map[path])
                if missing:
                    reasons.append(f"manifest files missing:{missing}")
                if extra:
                    reasons.append(f"unmanifested files present:{extra}")
                if mismatch:
                    reasons.append(f"manifest hash mismatch:{mismatch}")
                file_receipts = [{"path": path, "sha256": actual_map[path]} for path in sorted(actual_map)]
                directory_set_sha256 = sha256_bytes(canonical_bytes(file_receipts))

        if reasons:
            canonical_path = str(real_candidate) if candidate is not None else None
            rejected.append(
                {
                    "skill_id": skill_id,
                    "name": name,
                    "version": entry.get("version"),
                    "origin": origin,
                    "canonical_path": canonical_path,
                    "reparse_present": bool(reparse_components),
                    "reparse_components": reparse_components,
                    "status": entry.get("status"),
                    "disclosure_class": entry.get("disclosure_class"),
                    "reasons": sorted(set(reasons)),
                    "required_authority": entry.get("required_authority", []),
                    "required_inputs": entry.get("required_inputs", []),
                    "resource_claims": entry.get("resource_claims", []),
                    "expected_delta": entry.get("expected_delta"),
                    "proof_ceiling": entry.get("proof_ceiling"),
                    "cost": entry.get("cost"),
                    "revalidation": entry.get("revalidation"),
                    "rollback": entry.get("rollback"),
                    "master_links": entry.get("master_links", []),
                    "files": file_receipts,
                    "content_sha256": sha256_bytes(canonical_bytes(file_receipts)),
                    "directory_set_sha256": directory_set_sha256,
                }
            )
            continue

        assert candidate is not None
        real_candidate = candidate.resolve(strict=True)
        selected.append(
            {
                "skill_id": skill_id,
                "name": name,
                "version": entry.get("version"),
                "origin": origin,
                "canonical_path": str(real_candidate),
                "reparse_present": bool(reparse_components),
                "reparse_components": reparse_components,
                "status": entry.get("status"),
                "disclosure_class": entry.get("disclosure_class"),
                "match_reasons": sorted(matched_reasons),
                "required_authority": entry.get("required_authority", []),
                "required_inputs": entry.get("required_inputs", []),
                "resource_claims": entry.get("resource_claims", []),
                "expected_delta": entry.get("expected_delta"),
                "proof_ceiling": entry.get("proof_ceiling"),
                "cost": entry.get("cost"),
                "revalidation": entry.get("revalidation"),
                "rollback": entry.get("rollback"),
                "master_links": entry.get("master_links", []),
                "files": file_receipts,
                "content_sha256": sha256_bytes(canonical_bytes(file_receipts)),
                "directory_set_sha256": directory_set_sha256,
            }
        )

    snapshot_body = {
        "registry_sha256": sha256_bytes(registry_raw),
        "objective_id": request.get("objective_id"),
        "source_claims": sorted(str(item) for item in request.get("source_claims", [])),
        "facts": {field: sorted(values) for field, values in facts.items()},
        "blind_phase": blind_phase,
        "selected": selected,
        "rejected": rejected,
        "withheld": withheld,
    }
    snapshot_hash = sha256_bytes(canonical_bytes(snapshot_body))
    decision = "selected" if selected else "fallback_normal_workflow"
    if errors:
        decision = "fallback_normal_workflow"
    stale = bool(args.expect_snapshot and args.expect_snapshot.upper() != snapshot_hash)
    if stale:
        errors.append("selection snapshot mismatch")
        decision = "fallback_normal_workflow"

    receipt = {
        "schema_version": "mgskill-selection-receipt-v1",
        "registry_id": registry.get("registry_id"),
        "registry_path": str(registry_path),
        "registry_sha256": sha256_bytes(registry_raw),
        "input_path": str(input_path),
        "input_sha256": sha256_file(input_path),
        "evaluated_at_utc": now_text,
        "decision": decision,
        "selected": selected,
        "rejected": rejected,
        "withheld": withheld,
        "withheld_count": len(withheld),
        "selection_snapshot_sha256": snapshot_hash,
        "stale_snapshot": stale,
        "errors": sorted(set(errors)),
        "proof_ceiling": "deterministic registry match plus current path/hash/status identity; semantic applicability and consumer outcomes unproven",
    }
    return receipt, 2 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument(
        "--user-root",
        required=True,
        help="explicit root containing user-origin Skill directories",
    )
    parser.add_argument(
        "--project-root",
        help="explicit root containing project-origin Skill directories, when present",
    )
    parser.add_argument("--expect-snapshot")
    args = parser.parse_args()
    try:
        receipt, code = resolve(args)
    except Exception as exc:  # preserve a bounded machine-readable failure
        receipt = {
            "schema_version": "mgskill-selection-receipt-v1",
            "decision": "fallback_normal_workflow",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "proof_ceiling": "resolver failure only; normal workflow remains authoritative",
        }
        code = 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
