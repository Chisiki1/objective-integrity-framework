#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def is_global_like(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    home = Path.home().resolve()
    try:
        relative_to_home = path.resolve().relative_to(home)
    except ValueError:
        relative_to_home = None
    if ".codex" in parts or ".config" in parts or ".agents" in parts:
        return True
    if relative_to_home is not None and len(relative_to_home.parts) <= 1:
        return True
    return False


def planned_files(adapter: str) -> dict[str, Path]:
    if adapter == "generic":
        return {
            "objective-integrity-adapter.md": ROOT / "adapters" / "generic" / "system-developer-adapter.md",
            "objective-integrity/objective-contract.md": ROOT / "templates" / "objective-contract.md",
        }
    if adapter == "codex":
        return {
            "AGENTS.md": ROOT / "adapters" / "codex" / "AGENTS.md",
            ".agents/skills/objective-integrity/SKILL.md": ROOT / ".agents" / "skills" / "objective-integrity" / "SKILL.md",
            ".agents/skills/objective-integrity/references/standard-workflow.md": ROOT / ".agents" / "skills" / "objective-integrity" / "references" / "standard-workflow.md",
            ".agents/skills/objective-integrity/references/high-assurance.md": ROOT / ".agents" / "skills" / "objective-integrity" / "references" / "high-assurance.md",
            ".agents/skills/objective-integrity/references/skill-book.md": ROOT / ".agents" / "skills" / "objective-integrity" / "references" / "skill-book.md",
        }
    if adapter == "skill-book":
        return {
            "objective-integrity/skill-selection-receipt.md": ROOT / "templates" / "skill-selection-receipt.md",
            "objective-integrity/skill-lifecycle-record.md": ROOT / "templates" / "skill-lifecycle-record.md",
            "objective-integrity/exact-action-preflight.md": ROOT / "templates" / "exact-action-preflight.md",
            "objective-integrity/skill-book.md": ROOT / "docs" / "skill-book.md",
        }
    raise ValueError(adapter)


def rollback(backup: Path) -> int:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    destination = Path(manifest["destination"]).resolve()
    for rel in manifest["created"]:
        target = destination / rel
        if target.exists():
            target.unlink()
    for rel in manifest["replaced"]:
        source = backup / "files" / rel
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"rollback: restored {backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install project-local Objective Integrity Framework files.")
    parser.add_argument("--destination", help="Explicit project destination.")
    parser.add_argument("--adapter", choices=["generic", "codex", "skill-book"], default="generic")
    parser.add_argument("--apply", action="store_true", help="Write files. Default is dry-run.")
    parser.add_argument("--allow-global", action="store_true", help="Acknowledge a global-looking destination.")
    parser.add_argument("--rollback", help="Rollback from a generated backup directory.")
    args = parser.parse_args(argv)

    if args.rollback:
        return rollback(Path(args.rollback).resolve())
    if not args.destination:
        parser.error("--destination is required unless --rollback is used")
    destination = Path(args.destination).resolve()
    if is_global_like(destination) and not args.allow_global:
        raise SystemExit("Refusing global-looking destination without --allow-global.")

    files = planned_files(args.adapter)
    created, replaced = [], []
    for rel in files:
        target = destination / rel
        (replaced if target.exists() else created).append(str(rel).replace("\\", "/"))

    print(f"adapter: {args.adapter}")
    print(f"destination: {destination}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")
    print(f"created: {created}")
    print(f"replaced: {replaced}")
    if not args.apply:
        return 0

    backup = destination / ".objective-integrity-backups" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (backup / "files").mkdir(parents=True, exist_ok=True)
    for rel in replaced:
        source = destination / rel
        target = backup / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for rel, source in files.items():
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (backup / "manifest.json").write_text(json.dumps({
        "destination": str(destination),
        "adapter": args.adapter,
        "created": created,
        "replaced": replaced,
    }, indent=2), encoding="utf-8")
    print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
