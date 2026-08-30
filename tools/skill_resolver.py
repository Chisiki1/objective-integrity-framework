#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SELECTABLE_STATUSES = {"active-bounded", "measured"}
FACT_FIELDS = [
    "job",
    "action",
    "tool",
    "environment",
    "cause_families",
    "permissions",
    "resources",
    "consumers",
    "risks",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_hash(data: Any) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def normalize_tokens(values: list[str] | None) -> set[str]:
    return {str(value).strip().casefold() for value in values or [] if str(value).strip()}


def is_relative_safe(path_text: str) -> bool:
    path = Path(path_text)
    return bool(path_text) and not path.is_absolute() and ".." not in path.parts


def is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400)


def reparse_components(root: Path, candidate: Path) -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    current = root
    if is_reparse_or_symlink(current):
        try:
            target = current.resolve(strict=True)
        except OSError:
            target = current.absolute()
        components.append(
            {
                "lexical_path": str(current.absolute()),
                "resolved_target": str(target),
            }
        )
    for part in candidate.relative_to(root).parts:
        current = current / part
        if is_reparse_or_symlink(current):
            try:
                target = current.resolve(strict=True)
            except OSError:
                target = current.absolute()
            components.append(
                {
                    "lexical_path": str(current.absolute()),
                    "resolved_target": str(target),
                }
            )
    return components


def under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def manifest_hashes(skill_path: Path) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(skill_path.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(skill_path).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    return files


def entry_matches(entry: dict[str, Any], facts: dict[str, set[str]]) -> tuple[bool, str]:
    clauses = entry.get("match_clauses") or []
    if not clauses:
        return False, "no_match_clauses"
    for clause in clauses:
        matched = True
        missing: list[str] = []
        for field, needed_values in clause.items():
            needed = normalize_tokens(needed_values)
            if not needed.issubset(facts.get(field, set())):
                matched = False
                missing.append(field)
        if matched:
            return True, "matched_exact_clause"
    return False, "no_exact_clause_match"


def build_entry_view(entry: dict[str, Any], roots: dict[str, Path]) -> dict[str, Any]:
    origin = entry.get("origin", "")
    rel = entry.get("relative_path", "")
    view: dict[str, Any] = {
        "skill_id": entry.get("skill_id"),
        "name": entry.get("name"),
        "version": entry.get("version"),
        "origin": origin,
        "status": entry.get("status"),
        "disclosure_class": entry.get("disclosure_class"),
        "required_authority": entry.get("required_authority", []),
        "required_inputs": entry.get("required_inputs", []),
        "resource_claims": entry.get("resource_claims", []),
        "expected_delta": entry.get("expected_delta", ""),
        "rollback": entry.get("rollback", ""),
        "revalidation": entry.get("revalidation", []),
        "proof_ceiling": entry.get("proof_ceiling", "selection identity only"),
    }
    root = roots.get(origin)
    if root is None:
        view["path_status"] = "missing_origin_root"
        return view
    if not is_relative_safe(rel):
        view["path_status"] = "unsafe_relative_path"
        return view
    root_lexical = root.absolute()
    candidate_lexical = root_lexical / rel
    view["lexical_path"] = str(candidate_lexical)
    try:
        root_physical = root_lexical.resolve(strict=True)
        candidate_physical = candidate_lexical.resolve(strict=True)
    except OSError as exc:
        view["path_status"] = f"unreadable_path: {exc.__class__.__name__}"
        return view
    view["canonical_path"] = str(candidate_physical)
    view["reparse_components"] = reparse_components(root_lexical, candidate_lexical)
    view["reparse_present"] = bool(view["reparse_components"])
    if not under_root(candidate_physical, root_physical):
        view["path_status"] = "resolved_path_escapes_origin_root"
        return view
    if not (candidate_physical / "SKILL.md").is_file():
        view["path_status"] = "missing_skill_md"
        return view
    view["path_status"] = "ok"
    view["files"] = manifest_hashes(candidate_physical)
    view["content_sha256"] = canonical_json_hash(view["files"])
    return view


def detect_conflicts(entries: list[dict[str, Any]]) -> set[str]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("status") in SELECTABLE_STATUSES:
            key = str(entry.get("name"))
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def resolve(registry_path: Path, input_data: dict[str, Any], roots: dict[str, Path]) -> dict[str, Any]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_sha = sha256_file(registry_path)
    facts = {field: normalize_tokens(input_data.get(field)) for field in FACT_FIELDS}
    blind_phase = input_data.get("blind_phase", "none")
    conflicts = detect_conflicts(registry.get("entries", []))
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    withheld = 0

    for entry in registry.get("entries", []):
        entry_view = build_entry_view(entry, roots)
        matched, match_reason = entry_matches(entry, facts)
        conflict_key = str(entry.get("name"))
        selectable = entry.get("status") in SELECTABLE_STATUSES
        blind_safe = entry.get("disclosure_class") == "blind_safe_mechanical"

        if blind_phase == "initial" and not blind_safe:
            withheld += 1
            continue

        if not matched:
            entry_view["reason"] = match_reason
            rejected.append(entry_view)
        elif not selectable:
            entry_view["reason"] = "status_not_selectable"
            rejected.append(entry_view)
        elif conflict_key in conflicts:
            entry_view["reason"] = "duplicate_active_origin_name_conflict"
            rejected.append(entry_view)
        elif entry_view.get("path_status") != "ok":
            entry_view["reason"] = entry_view.get("path_status")
            rejected.append(entry_view)
        else:
            entry_view["reason"] = match_reason
            selected.append(entry_view)

    snapshot_material = {
        "registry_sha256": registry_sha,
        "objective_id": input_data.get("objective_id"),
        "source_claims": input_data.get("source_claims", []),
        "facts": {field: sorted(values) for field, values in facts.items()},
        "blind_phase": blind_phase,
        "selected": selected,
        "rejected": rejected,
        "withheld_non_blind_safe_count": withheld,
    }
    snapshot_hash = canonical_json_hash(snapshot_material)
    receipt = {
        "schema_version": "skill-selection-receipt-v1",
        "objective_id": input_data.get("objective_id"),
        "source_claims": input_data.get("source_claims", []),
        "registry_id": registry.get("registry_id"),
        "registry_sha256": registry_sha,
        "selection_snapshot_sha256": snapshot_hash,
        "selected": selected,
        "rejected": rejected,
        "withheld_non_blind_safe_count": withheld,
        "fallback_normal_workflow": not selected,
        "proof_ceiling": "deterministic selection, path, hash, and stale-snapshot evidence only",
    }
    return receipt


def parse_roots(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--root values must use origin=path")
        origin, path = value.split("=", 1)
        roots[origin.strip()] = Path(path).absolute()
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve Skill Book entries into a hash-bound receipt.")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--root", action="append", default=[], help="Origin root as origin=path.")
    parser.add_argument("--expect-snapshot")
    parser.add_argument("--receipt-out")
    args = parser.parse_args(argv)

    receipt = resolve(Path(args.registry).resolve(), json.loads(Path(args.input).read_text(encoding="utf-8")), parse_roots(args.root))
    if args.expect_snapshot and args.expect_snapshot.upper() != receipt["selection_snapshot_sha256"]:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        print("skill_resolver: STALE_SNAPSHOT")
        return 2
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.receipt_out:
        Path(args.receipt_out).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
