#!/usr/bin/env python3
"""Structural decision-event admission for a source-bound Supervisor."""
from __future__ import annotations
import argparse, hashlib, json, types
from pathlib import Path

DECISIONS = {"WATCH", "CONSUME", "REFUTE", "CORRECT", "USER_DECISION", "WAIT_AT_DECISION_WINDOW", "ACT"}
def yes(v): return isinstance(v, str) and bool(v.strip())
def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--input", required=True); args = ap.parse_args()
    p = Path(args.input).resolve(strict=True); d = json.loads(p.read_text(encoding="utf-8")); errors=[]
    required={"schema_version","event_id","objective_id","decision","objective_evidence_delta","omission_consequence","shorter_alternative","induced_rework","return_step","later_effect","fixed_polling","status_only","progress_status","decision_window"}
    v2 = d.get('schema_version') == 'objective-supervisor-control-v2'
    if v2: required |= {'source_sha256', 'owner_chat_id', 'work_phase'}
    if set(d) != required: errors.append("exact top-level schema required")
    if d.get("schema_version") not in {"objective-supervisor-control-v1", "objective-supervisor-control-v2"}: errors.append("unsupported schema")
    if not v2 and d.get('decision') == 'ACT': errors.append('ACT requires a source-wide v2 binding')
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
    phase={'source_wide_evaluated': False, 'proof_ceiling': 'legacy event; no source-wide selection'}
    if v2 and not errors:
        try:
            q=Path(__file__).resolve().parents[2]/'master-guided-skill-lifecycle/scripts/work_phase.py'
            mod=types.ModuleType('owner_work_phase'); mod.__file__=str(q)
            exec(compile(q.read_bytes(),str(q),'exec'),mod.__dict__)
            phase=mod.evaluate(d['work_phase'],objective_id=d['objective_id'],source_sha256=d['source_sha256'],owner_chat_id=d['owner_chat_id'])
            if not phase['admitted']: errors.append('source-wide phase holds requested next work')
            if d['decision'] == 'CORRECT' and d['work_phase']['action'] != 'REPAIR_FINDINGS': errors.append('CORRECT must bind grouped findings repair')
        except (OSError, ValueError, KeyError, TypeError) as exc: errors.append('work_phase: '+str(exc))
    out={"schema_version":"objective-supervisor-control-result-v1","decision":"ADMIT" if not errors else "HOLD","requested_decision":d.get("decision"),"errors":sorted(set(errors)),"input_sha256":hashlib.sha256(p.read_bytes()).hexdigest().upper(),"work_phase":phase,"proof_ceiling":"event shape and bound scope/phase selection only; semantic coverage, authority, causal merit, tool interception and outcome remain unproven"}
    print(json.dumps(out,sort_keys=True,indent=2)); return 0 if not errors else 3
if __name__ == "__main__": raise SystemExit(main())
