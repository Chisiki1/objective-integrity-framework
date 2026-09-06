#!/usr/bin/env python3
import json, subprocess, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parent
base={"schema_version":"objective-supervisor-control-v1","event_id":"E","objective_id":"O","decision":"WAIT_AT_DECISION_WINDOW","objective_evidence_delta":"new artifact","omission_consequence":"objective remains unproven","shorter_alternative":"reuse cursor","induced_rework":"none observed","return_step":"consume result","later_effect":"measure wait","fixed_polling":False,"status_only":False,"progress_status":"progressing","decision_window":{"next_evidence":"worker completion"}}
with tempfile.TemporaryDirectory() as d:
 p=Path(d)/"e.json";p.write_text(json.dumps(base),encoding="utf-8");assert subprocess.run([sys.executable,"-B",str(root/"supervisor_control.py"),"--input",str(p)]).returncode==0
 base["fixed_polling"]=True;p.write_text(json.dumps(base),encoding="utf-8");assert subprocess.run([sys.executable,"-B",str(root/"supervisor_control.py"),"--input",str(p)]).returncode!=0
print("supervisor control countermodels passed")
