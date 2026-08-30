#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


REQUIRED_PHRASES = [
    "objective contract",
    "objective-necessity link",
    "open deliverable ledger",
    "semantic authority",
    "root cause",
    "raw evidence",
    "blind scenario",
    "interaction closure",
    "semantic recomposition",
    "boundary-state continuity",
    "workload progress",
    "unmasking frontier",
    "proof ceiling",
    "verification model",
    "guard and stop composition",
    "Supervisor and Worker",
    "two layers",
    "three governance planes",
    "Skill Book",
    "selected and rejected",
    "canonical path",
    "reparse",
    "stale snapshot",
    "blind-safe",
    "lifecycle",
    "single semantic writer",
    "exact-action preflight",
    "action constraints",
    "empirical superiority",
    "privacy",
    "external action",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that public docs retain core framework families.")
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    haystack = []
    for rel in [
        "README.md",
        "docs/architecture.md",
        "docs/skill-book.md",
        "framework/core-invariants.md",
        "framework/normative-reference.md",
        "docs/coverage-map.md",
        "templates/skill-selection-receipt.md",
        "templates/skill-lifecycle-record.md",
        "templates/exact-action-preflight.md",
        "templates/scenario-interaction-ledger.md",
        "templates/correction-authorization.md",
        "templates/audit-receipt.md",
        "templates/global-knowledge-master.md",
        "templates/project-master.md",
    ]:
        haystack.append((root / rel).read_text(encoding="utf-8"))
    text = "\n".join(haystack).lower()
    missing = [phrase for phrase in REQUIRED_PHRASES if phrase.lower() not in text]
    if missing:
        print("no_drop_check: FAIL")
        for item in missing:
            print(f"missing: {item}")
        return 1
    print("no_drop_check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
