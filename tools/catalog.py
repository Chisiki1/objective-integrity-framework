#!/usr/bin/env python3
"""Build an exact, project-origin registry for this distribution's Skill Book.

Prints JSON by default; --output creates a new explicitly named file. Rebuild
after a reviewed change. This does not register or activate a host integration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from bootstrap import no_links, within

ROOT = Path(__file__).resolve().parents[1]
# Public behavior descriptors, not copies of private events or registry records.
SPECS = [
    ("master-guided-skill-resolver", "1.5.0", "blind_safe_mechanical", [{"job": ["skill-resolution", "workflow-skill-resolution"]}]),
    ("master-guided-skill-lifecycle", "1.5.0", "post-blind-semantic", [{"job": ["skill-effect-record", "skill-lifecycle-transition", "master-knowledge-retrieval", "learning-next-use", "workflow-condition-review"]}]),
    ("powershell-exact-action", "1.0.0", "blind_safe_mechanical", [
        {"action": ["execute-powershell"], "tool": ["powershell"]},
        {"job": ["command-preflight"], "tool": ["powershell"]},
        {"cause_families": ["powershell::foreach-pipe-parser", "powershell::literal-dollar-arg-expansion"]}]),
    ("durable-supervisor-status", "1.1.1", "post-blind-semantic", [{
        "cause_families": ["SUPERVISOR::EMPTY-TASK-PROJECTION", "WORKFLOW::WORKER-LOCAL-STATUS"],
        "environment": ["legacy-split-chat"], "job": ["supervisor-observation-fallback"]}]),
    ("workflow-transition-admission", "2.0.0", "post-blind-semantic", [
        {"job": ["workflow-transition-admission", "supervisor-correction-admission", "same-chat-transition-admission"]},
        {"action": ["worker-candidate-transition", "released-external-action", "same-chat-candidate-transition"]},
        {"cause_families": ["WORKFLOW::CORRECTION-ADMISSION-BYPASS", "WORKFLOW::SKILL-SELECTED-NOT-APPLIED", "WORKFLOW::STALE-ACTION-PROJECTION"]}]),
    ("scenario-interaction-closure", "1.0.0", "post-blind-semantic", [
        {"job": ["scenario-interaction-closure", "material-correction"]},
        {"cause_families": ["WORKFLOW::SCENARIO-OMISSION", "WORKFLOW::RECOMPOSITION-LOSS"]}]),
    ("objective-supervisor-control", "1.1.0", "post-blind-semantic", [
        {"job": ["supervisor-decision-control", "monitoring-decision"]},
        {"cause_families": ["OBJECTIVE::SUPERVISOR-STATUS-NARRATION", "WORKFLOW::FIXED-WAIT"]}]),
    ("chat-objective-continuity", "1.0.0", "post-blind-semantic", [
        {"job": ["chat-objective-continuity", "objective-checkpoint"]},
        {"action": ["update-chat-objective", "restore-chat-objective"]},
        {"cause_families": ["OBJECTIVE::COMPACTION-LOSS", "OBJECTIVE::CROSS-CHAT-CONTAMINATION"]}]),
]


def build() -> dict:
    entries = []
    for name, version, disclosure, clauses in SPECS:
        directory = ROOT / "runtime/skills" / name
        no_links(directory)
        if not (directory / "SKILL.md").is_file():
            raise ValueError(f"Incomplete Skill package: {name}")
        files = []
        for path in sorted(directory.rglob("*")):
            no_links(path)
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                raise ValueError(f"Remove generated cache before freezing the catalog: {name}")
            files.append({"path": path.relative_to(directory).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper()})
        entries.append({"skill_id": "OIF-" + name.upper(), "name": name, "version": version,
                        "origin": "project", "relative_path": name, "status": "active-bounded",
                        "disclosure_class": disclosure, "match_clauses": clauses, "files": files,
                        "master_links": ["OIF-DISTRIBUTION-CATALOG"], "expires_utc": None,
                        "required_authority": ["Read the current workflow and both explicitly supplied masters; execution requires separate action authority."],
                        "required_inputs": ["Source-bound objective, final action facts and the applicable versioned contract."],
                        "resource_claims": ["Read only during selection; implementing actions have explicit scoped destinations."],
                        "revalidation": ["source/action, member set, root, schema, status or registry change"],
                        "expected_delta": "Retrieve the exact applicable instructions and executable supporting evidence.",
                        "cost": "Proportional to matched members; measure benefit at the actual consumer.",
                        "rollback": "Discard this registry snapshot; restore a previously reviewed cut before dependent use.",
                        "proof_ceiling": "Distribution membership and selection identity; action, semantic judgment and measured effect are separate."})
    return {"schema_version": "mgskill-registry-v1", "registry_id": "OIF-PUBLIC-SKILL-BOOK", "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Create a new registry file; stdout is the default.")
    args = parser.parse_args()
    try:
        encoded = json.dumps(build(), indent=2) + "\n"
        if args.output:
            path = Path(args.output).absolute()
            no_links(path)
            if within(path, ROOT / "runtime"):
                raise ValueError("Keep generated registry outside the hash-bound runtime member tree.")
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
            print(f"catalog: created {path}")
        else:
            print(encoded, end="")
        return 0
    except (OSError, ValueError) as exc:
        print(f"catalog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
