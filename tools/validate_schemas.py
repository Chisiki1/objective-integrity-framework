#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures = []
    for path in sorted((root / "schemas").glob("*.schema.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"{path.name}: invalid json: {exc}")
            continue
        for key in ["$schema", "type", "required", "properties"]:
            if key not in data:
                failures.append(f"{path.name}: missing {key}")
        if data.get("type") != "object":
            failures.append(f"{path.name}: root type must be object")
    if failures:
        print("\n".join(failures))
        return 1
    print("validate_schemas: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
