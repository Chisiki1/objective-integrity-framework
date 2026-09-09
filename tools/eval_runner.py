#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def plugin_scenario_errors(data: dict) -> list[str]:
    """Validate known scenario specifications, never certify model behavior."""
    errors = []
    if data.get("schema") != "oif-plugin-scenarios-v1":
        errors.append("unsupported scenario schema")
    cases = data.get("cases", [])
    expected = {"P01", "P02", "P03", "P04", "P05", "N01", "N02", "N03"}
    if not isinstance(cases, list) or len(cases) != 8 or not all(isinstance(c, dict) for c in cases):
        return errors + ["eight scenario objects are required"]
    if {c.get("id") for c in cases} != expected:
        errors.append("missing or duplicate scenario id")
    if data.get("observation", {}).get("status") != "NOT-RUN":
        errors.append("keep model observations in separate result artifacts")
    for case in cases:
        if not isinstance(case.get("should_activate"), bool) or case["should_activate"] != str(case.get("id", "")).startswith("P"):
            errors.append("activation expectation disagrees with case type")
        for field in ("turns", "required", "forbidden"):
            if not isinstance(case.get(field), list) or not case[field]:
                errors.append("missing scenario " + field)
    return errors


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
    scenario_sets = 0
    fixtures = sorted((root / "evals").glob("*.json"))
    for fixture in fixtures:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        if data.get("schema") == "oif-plugin-scenarios-v1":
            scenario_sets += 1
            failures.extend(f"{fixture.name}: {error}" for error in plugin_scenario_errors(data))
            continue
        fid = data.get("id")
        if not fid or fid in seen:
            failures.append(f"{fixture.name}: missing or duplicate id")
        seen.add(fid)
        for mechanism in data.get("expected_mechanisms", []):
            if mechanism.lower() not in reference:
                failures.append(f"{fixture.name}: mechanism not covered in reference: {mechanism}")
    if not seen:
        failures.append("required evaluation fixtures are missing")
    if failures:
        print("eval_runner: FAIL")
        print("\n".join(failures))
        return 1
    print(f"eval_runner: PASS ({len(seen)} fixtures; fixture wiring and reference-term coverage only)")
    if scenario_sets:
        print(f"Scenario specifications: {scenario_sets} validated; model execution NOT-RUN.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
