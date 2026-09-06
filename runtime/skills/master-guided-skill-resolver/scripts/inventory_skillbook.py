#!/usr/bin/env python3
"""Create the non-historical mgskill-inventory-v1 identity inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


TOKEN = re.compile(r"`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`")
HEADING = re.compile(r"^#{1,6}\s+.*?`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`")
TABLE_FIRST = re.compile(r"^\|\s*`?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`?\s*\|")
CONTROL = re.compile(r"^-\s+(?:Master ID|Unit ID|Repeat-Key):\s+`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()


def scan_master(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        defined = set()
        for pattern in (HEADING, TABLE_FIRST, CONTROL):
            match = pattern.search(line)
            if match:
                defined.add(match.group(1))
        for token in sorted(set(TOKEN.findall(line))):
            record = {"id": token, "line": number}
            if token in defined:
                definitions.append(record)
            else:
                references.append(record)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "definitions": definitions,
        "references": references,
    }


def scan_skill_root(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for skill_file in sorted(root.glob("*/SKILL.md"), key=lambda p: p.as_posix().casefold()):
        skill_dir = skill_file.parent
        files = []
        for file_path in sorted((p for p in skill_dir.rglob("*") if p.is_file()), key=lambda p: p.as_posix().casefold()):
            files.append({"path": file_path.relative_to(skill_dir).as_posix(), "sha256": sha256_file(file_path)})
        frontmatter = skill_file.read_text(encoding="utf-8").split("---", 2)
        name_match = re.search(r"(?m)^name:\s*([^\r\n]+)$", frontmatter[1] if len(frontmatter) > 2 else "")
        records.append(
            {
                "name": name_match.group(1).strip() if name_match else None,
                "path": str(skill_dir.resolve()),
                "files": files,
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", action="append", required=True)
    parser.add_argument("--skill-root", action="append", default=[])
    parser.add_argument("--compare-to")
    parser.add_argument("--output")
    args = parser.parse_args()

    masters = [scan_master(Path(item).resolve(strict=True)) for item in args.master]
    skills = []
    for item in args.skill_root:
        root = Path(item).resolve()
        skills.extend(scan_skill_root(root))

    definition_sources: dict[str, list[str]] = {}
    reference_sources: dict[str, list[str]] = {}
    for master in masters:
        for record in master["definitions"]:
            definition_sources.setdefault(record["id"], []).append(f"{master['path']}:{record['line']}")
        for record in master["references"]:
            reference_sources.setdefault(record["id"], []).append(f"{master['path']}:{record['line']}")

    core = {
        "grammar": {
            "id": "mgskill-inventory-v1",
            "definition": "backticked uppercase hyphen token in a heading, first table cell, or Master ID/Unit ID/Repeat-Key control line",
            "reference": "other backticked uppercase hyphen token",
            "historical_kcm_comparable": False,
        },
        "masters": masters,
        "definition_ids": sorted(definition_sources),
        "reference_ids": sorted(reference_sources),
        "definition_sources": {key: sorted(value) for key, value in sorted(definition_sources.items())},
        "reference_sources": {key: sorted(value) for key, value in sorted(reference_sources.items())},
        "duplicate_definition_ids": {key: value for key, value in sorted(definition_sources.items()) if len(value) > 1},
        "skills": sorted(skills, key=lambda item: (str(item["name"]), item["path"])),
    }
    comparison = None
    code = 0
    if args.compare_to:
        before = json.loads(Path(args.compare_to).read_text(encoding="utf-8"))
        before_defs = set(before.get("definition_ids", []))
        after_defs = set(core["definition_ids"])
        before_skill_names = {item.get("name") for item in before.get("skills", [])}
        after_skill_names = {item.get("name") for item in core["skills"]}
        comparison = {
            "definition_ids_missing": sorted(before_defs - after_defs),
            "definition_ids_added": sorted(after_defs - before_defs),
            "skill_names_missing": sorted(before_skill_names - after_skill_names),
            "skill_names_added": sorted(after_skill_names - before_skill_names),
            "preserved": not (before_defs - after_defs) and not (before_skill_names - after_skill_names),
        }
        if not comparison["preserved"]:
            code = 2

    output = dict(core)
    output["inventory_sha256"] = canonical_hash(core)
    output["comparison"] = comparison
    output["proof_ceiling"] = "mgskill-inventory-v1 current set/path/hash evidence only; historical KCM and semantic completeness unproven"
    rendered = json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    return code


if __name__ == "__main__":
    sys.exit(main())
