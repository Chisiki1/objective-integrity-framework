#!/usr/bin/env python3
"""Inventory real master headings/current-control roles without claiming semantic completeness."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
STABLE_ID = re.compile(
    r"(?:(?:GPM|PM)-[A-Z0-9-]+|(?:G|P)-EVT-[A-Z0-9-]+|GK-[A-Z0-9-]+|GC-[A-Z0-9-]+|"
    r"U-[A-Z0-9-]+|KCM-[A-Z0-9-]+|POLICY-[A-Z0-9-]+|SKILL-[A-Z0-9-]+|MGSKILL-[A-Z0-9-]+)"
)
STATUS_WORDS = ("STALE", "REDUNDANT", "CONFLICT", "ORPHAN", "SUPERSEDED", "INVALIDATED", "UNPROVEN", "PENDING")
WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s`]+")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def normalized_body(value: str) -> str:
    value = STABLE_ID.sub("<ID>", value.upper())
    value = WINDOWS_PATH.sub("<PATH>", value)
    return " ".join(value.split())


def section_inventory(lines: list[str], headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        start = heading["line"] - 1
        end = headings[index + 1]["line"] - 1 if index + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end])
        normalized = normalized_body(body)
        tags = [tag for tag in ("RAW_EPISODE", "SANITIZED_FAMILY", "PROJECT_EVENT_LINK") if tag in body.upper()]
        sections.append({
            "line": heading["line"],
            "title": heading["title"],
            "role": heading["role"],
            "tags": tags,
            "project_event_links": sorted(set(re.findall(r"PROJECT_EVENT_LINK:\s*((?:P)-EVT-[A-Z0-9-]+)", body.upper()))),
            "normalized_body_sha256": sha256_bytes(normalized.encode("utf-8")),
            "normalized_body_length": len(normalized),
        })
    return sections


def inventory(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    in_current = False
    headings: list[dict[str, Any]] = []
    current_lines: list[str] = []
    status_hits: dict[str, list[dict[str, Any]]] = {word: [] for word in STATUS_WORDS}
    for number, line in enumerate(lines, 1):
        match = HEADING.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2)
            normalized = title.replace("[ALWAYS-READ]", "").strip().upper()
            if level == 2:
                in_current = normalized == "CURRENT CONTROL"
            ids = sorted(set(STABLE_ID.findall(title.upper())))
            headings.append({"line": number, "level": level, "title": title, "ids": ids, "role": "current-control" if in_current else "append-history"})
        if in_current:
            current_lines.append(line)
        upper = line.upper()
        for word in STATUS_WORDS:
            if word in upper:
                status_hits[word].append({"line": number, "ids": sorted(set(STABLE_ID.findall(upper)))})
    heading_ids = [identifier for item in headings for identifier in item["ids"]]
    duplicate_heading_ids = sorted({identifier for identifier in heading_ids if heading_ids.count(identifier) > 1})
    sections = section_inventory(lines, headings)
    control_ids = sorted(set(re.findall(r"CURRENT_CONTROL_ID:\s*([A-Za-z0-9_.:/-]+)", "\n".join(lines), re.IGNORECASE)))
    superseded_controls = sorted(set(re.findall(r"SUPERSEDES_CURRENT_CONTROL:\s*([A-Za-z0-9_.:/-]+)", "\n".join(lines), re.IGNORECASE)))
    return {
        "path": str(path.resolve()),
        "sha256": sha256_bytes(raw),
        "line_count": len(lines),
        "headings": headings,
        "heading_ids": sorted(set(heading_ids)),
        "heading_id_occurrences": len(heading_ids),
        "duplicate_heading_ids": duplicate_heading_ids,
        "sections": sections,
        "current_control_ids": control_ids,
        "later_superseded_control_ids": superseded_controls,
        "stale_current_control_candidates": sorted(set(control_ids) & set(superseded_controls)),
        "current_control_sha256": sha256_bytes("\n".join(current_lines).encode("utf-8")),
        "status_hits": status_hits,
        "proof_ceiling": "heading/current-control/status-token identity and no-drop comparison only; semantic completeness, KCM comparability and conflict correctness unproven",
    }


def cross_layer(global_master: dict[str, Any], project_master: dict[str, Any]) -> dict[str, Any]:
    global_sections = global_master["sections"]
    project_sections = project_master["sections"]
    global_by_fp = {item["normalized_body_sha256"]: item for item in global_sections}
    project_by_fp = {item["normalized_body_sha256"]: item for item in project_sections}
    duplicates = sorted(set(global_by_fp) & set(project_by_fp))
    tagged_duplicates = [
        fingerprint for fingerprint in duplicates
        if "SANITIZED_FAMILY" in global_by_fp[fingerprint]["tags"]
        or "RAW_EPISODE" in project_by_fp[fingerprint]["tags"]
    ]
    global_tag_errors = [
        item["line"] for item in global_sections
        if "SANITIZED_FAMILY" in item["tags"] and "PROJECT_EVENT_LINK" not in item["tags"]
    ]
    project_event_ids = {identifier for identifier in project_master["heading_ids"] if identifier.startswith("P-EVT-")}
    global_raw_errors = [item["line"] for item in global_sections if "RAW_EPISODE" in item["tags"]]
    invalid_project_links = [
        {"line": item["line"], "link": link}
        for item in global_sections for link in item["project_event_links"]
        if link not in project_event_ids
    ]
    project_tag_errors = [item["line"] for item in project_sections if "SANITIZED_FAMILY" in item["tags"]]
    blockers = bool(tagged_duplicates or global_tag_errors or global_raw_errors or invalid_project_links or project_tag_errors)
    return {
        "decision": "BLOCK_NEW_WRITE" if blockers else "ELIGIBLE_WITH_SEMANTIC_AUDIT",
        "normalized_cross_layer_duplicates": duplicates,
        "tagged_duplicate_blockers": tagged_duplicates,
        "global_sanitized_without_project_link_lines": global_tag_errors,
        "global_raw_episode_lines": global_raw_errors,
        "invalid_project_event_links": invalid_project_links,
        "project_contains_sanitized_family_lines": project_tag_errors,
        "legacy_untagged_sections": {
            "global": [item["line"] for item in global_sections if not item["tags"]],
            "project": [item["line"] for item in project_sections if not item["tags"]],
        },
        "proof_ceiling": "normalized exact-body/tag/link separation only; semantic equivalence, sanitization quality and historical backing remain independently audited",
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_ids = set(before["heading_ids"])
    after_ids = set(after["heading_ids"])
    return {
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
        "preserved_heading_ids": sorted(before_ids & after_ids),
        "missing_heading_ids": sorted(before_ids - after_ids),
        "added_heading_ids": sorted(after_ids - before_ids),
        "before_duplicate_heading_ids": before["duplicate_heading_ids"],
        "after_duplicate_heading_ids": after["duplicate_heading_ids"],
        "no_drop": not (before_ids - after_ids),
        "proof_ceiling": "stable heading-ID set comparison only; raw semantic no-drop remains separately audited",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", action="append", required=True)
    parser.add_argument("--compare-before", action="append")
    parser.add_argument("--output")
    args = parser.parse_args()
    masters = [inventory(Path(item).resolve(strict=True)) for item in args.master]
    result: dict[str, Any] = {
        "schema_version": "master-live-role-inventory-v2",
        "masters": masters,
        "proof_ceiling": "real-master heading/current-control/status identity; historical KCM and semantic completeness unproven",
    }
    if len(masters) == 2:
        global_candidates = [item for item in masters if Path(item["path"]).name.upper() == "GLOBAL_PROJECT_MASTER.MD"]
        project_candidates = [item for item in masters if Path(item["path"]).name.upper() == "PROJECT_MASTER.MD"]
        if len(global_candidates) == 1 and len(project_candidates) == 1:
            result["cross_layer"] = cross_layer(global_candidates[0], project_candidates[0])
    if args.compare_before:
        if len(args.compare_before) != len(masters):
            raise SystemExit("compare-before count must equal master count")
        before_values = []
        for before in args.compare_before:
            loaded = json.loads(Path(before).read_text(encoding="utf-8"))
            if loaded.get("schema_version") == "master-live-role-inventory-v2":
                if len(loaded.get("masters", [])) != 1:
                    raise SystemExit("each compare-before envelope must contain exactly one master")
                loaded = loaded["masters"][0]
            before_values.append(loaded)
        result["comparisons"] = [compare(before, after) for before, after in zip(before_values, masters)]
    text = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
