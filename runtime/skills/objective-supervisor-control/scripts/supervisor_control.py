#!/usr/bin/env python3
"""Structural decision-event admission for a source-bound Supervisor."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

DECISIONS = {"WATCH", "CONSUME", "REFUTE", "CORRECT", "USER_DECISION", "WAIT_AT_DECISION_WINDOW"}
def yes(v): return isinstance(v, str) and bool(v.strip())
def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--input", required=True); args = ap.parse_args()
    p = Path(args.input).resolve(strict=True); d = json.loads(p.read_text(encoding="utf-8")); errors=[]
    required={"schema_version","event_id","objective_id","decision","objective_evidence_delta","omission_consequence","shorter_alternative","induced_rework","return_step","later_effect","fixed_polling","status_only","progress_status","decision_window"}
    if set(d) != required: errors.append("exact top-level schema required")
    if d.get("schema_version") != "objective-supervisor-control-v1": errors.append("unsupported schema")
    if d.get("decision") not in DECISIONS: errors.append("decision invalid")
    for k in ("event_id","objective_id","objective_evidence_delta","omission_consequence","shorter_alternative","induced_rework","return_step","later_effect"):
        if not yes(d.get(k)): errors.append(f"{k}: non-empty string required")
    if d.get("fixed_polling") is not False: errors.append("fixed polling is not a decision event")
    if d.get("status_only") is True: errors.append("status-only narration cannot advance a decision")
    if d.get("progress_status") not in {"progressing","stalled","unknown","not_applicable"}: errors.append("progress_status invalid")
    if d.get("decision") == "WAIT_AT_DECISION_WINDOW":
        if not isinstance(d.get("decision_window"), dict) or not yes(d["decision_window"].get("next_evidence")):
            errors.append("wait needs a decision window with next evidence")
        if d.get("progress_status") != "progressing": errors.append("wait requires progressing evidence")
    out={"schema_version":"objective-supervisor-control-result-v1","decision":"ADMIT" if not errors else "HOLD","requested_decision":d.get("decision"),"errors":sorted(set(errors)),"input_sha256":hashlib.sha256(p.read_bytes()).hexdigest().upper(),"proof_ceiling":"event shape and bounded decision fields only; supervisor authority, causal merit, tool interception and outcome remain unproven"}
    print(json.dumps(out,sort_keys=True,indent=2)); return 0 if not errors else 3
if __name__ == "__main__": raise SystemExit(main())
