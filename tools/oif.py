#!/usr/bin/env python3
"""Small command directory for the versioned, standalone runtime tools."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "runtime/skills"
COMMANDS = {
    "objective": ROOT / "runtime/objective_ledger.py",
    "objective-input": SKILLS / "chat-objective-continuity/scripts/ledger_input.py",
    "facts-build": SKILLS / "master-guided-skill-resolver/scripts/build_skill_fact_input.py",
    "facts-compile": SKILLS / "master-guided-skill-resolver/scripts/compile_skill_facts.py",
    "resolve": SKILLS / "master-guided-skill-resolver/scripts/resolve_skills.py",
    "registry-check": SKILLS / "master-guided-skill-resolver/scripts/validate_registry.py",
    "skill-inventory": SKILLS / "master-guided-skill-resolver/scripts/inventory_skillbook.py",
    "master-inventory": SKILLS / "master-guided-skill-resolver/scripts/inventory_live_masters_v2.py",
    "apply-skill": SKILLS / "master-guided-skill-resolver/scripts/skill_application_bridge.py",
    "learning": SKILLS / "master-guided-skill-lifecycle/scripts/learning_queue.py",
    "index": SKILLS / "master-guided-skill-lifecycle/scripts/master_index.py",
    "reconcile": SKILLS / "master-guided-skill-lifecycle/scripts/master_reconcile.py",
    "operation": SKILLS / "master-guided-skill-lifecycle/scripts/operation_io.py",
    "artifact": SKILLS / "artifact-access/scripts/artifact_access.py",
    "lifecycle": SKILLS / "master-guided-skill-lifecycle/scripts/skill_lifecycle.py",
    "allocate": SKILLS / "master-guided-skill-lifecycle/scripts/stage_allocation.py",
    "allocation-io": SKILLS / "master-guided-skill-lifecycle/scripts/allocation_io.py",
    "work": SKILLS / "master-guided-skill-lifecycle/scripts/work_io.py",
    "work-phase": SKILLS / "master-guided-skill-lifecycle/scripts/work_phase.py",
    "capabilities": SKILLS / "master-guided-skill-lifecycle/scripts/capability_snapshot.py",
    "candidate": SKILLS / "master-guided-skill-lifecycle/scripts/materialize_skill_candidate.py",
    "adopt": SKILLS / "master-guided-skill-lifecycle/scripts/isolated_registry_adoption.py",
    "governance": SKILLS / "master-guided-skill-lifecycle/scripts/workflow_governance.py",
    "transition": SKILLS / "workflow-transition-admission/scripts/transition_admission.py",
    "scenarios": SKILLS / "scenario-interaction-closure/scripts/validate_scenario_closure.py",
    "control": SKILLS / "objective-supervisor-control/scripts/supervisor_control.py",
    "legacy-status": SKILLS / "durable-supervisor-status/scripts/durable_status.py",
    "catalog": ROOT / "tools/catalog.py",
    "demo": ROOT / "tools/demo.py",
    "bootstrap": ROOT / "tools/bootstrap.py",
    "plugin": ROOT / "tools/plugin.py",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"list", "--help", "-h"}:
        print("Usage: python tools/oif.py COMMAND [original command arguments]\n")
        for name, path in COMMANDS.items():
            print(f"{name:18} {path.relative_to(ROOT).as_posix()}")
        print("\nUse COMMAND --help for the exact versioned input contract. No defaults activate a tool.")
        return 0
    command = args.pop(0)
    if command not in COMMANDS:
        print(f"Unknown command: {command}. Run with --help for the directory.", file=sys.stderr)
        return 2
    script = COMMANDS[command]
    if not script.is_file():
        print(f"Incomplete distribution: {script.relative_to(ROOT)}", file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, "-B", str(script), *args], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
