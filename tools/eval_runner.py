#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def main(path: str = ".") -> int:
    root = Path(path).resolve()
    reference = "\n".join(
        (root / rel).read_text(encoding="utf-8")
        for rel in [
            "framework/core-invariants.md",
            "framework/normative-reference.md",
            "docs/evaluation.md",
            "docs/skill-book.md",
        ]
    ).lower()
    failures = []
    seen = set()
    for fixture in sorted((root / "evals").glob("*.json")):
        data = json.loads(fixture.read_text(encoding="utf-8"))
        fid = data.get("id")
        if not fid or fid in seen:
            failures.append(f"{fixture.name}: missing or duplicate id")
        seen.add(fid)
        for mechanism in data.get("expected_mechanisms", []):
            if mechanism.lower() not in reference:
                failures.append(f"{fixture.name}: mechanism not covered in reference: {mechanism}")
    if failures:
        print("eval_runner: FAIL")
        print("\n".join(failures))
        return 1
    print(f"eval_runner: PASS ({len(seen)} fixtures; fixture wiring and reference-term coverage only)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
