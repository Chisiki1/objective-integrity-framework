#!/usr/bin/env python3
import json, subprocess, sys, tempfile
from pathlib import Path
root = Path(__file__).resolve().parent
base = {"schema_version":"scenario-interaction-closure-v1","closure_id":"S0","objective_id":"O","source_ref":"SRC","blind_inputs":{"status":"frozen"},"factors":[{"id":"F","activation":"entry"}],"relations":[{"id":"R","factor_ids":["F"],"semantic_relation":"owner-to-consumer","disposition":"preserved"}],"semantic_locks":[{"id":"L","relation_ids":["R"]}],"consumer_oracles":[{"id":"C","lock_id":"L","consumer_outcome":"outcome"}],"recomposition_witnesses":[{"id":"W","lock_id":"L","relation_id":"R","method":"countermodel","countermodel":"local pass/system fail"}],"unmasking":{"recovery":"return"},"delta_lineage":[]}
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "ok.json"; p.write_text(json.dumps(base), encoding="utf-8")
    assert subprocess.run([sys.executable,"-B",str(root/"validate_scenario_closure.py"),"--input",str(p)]).returncode == 0
    base["relations"] = [{"id":"R","factor_ids":["F"],"semantic_relation":"owner-to-consumer","disposition":"orphan"}]; p.write_text(json.dumps(base), encoding="utf-8")
    assert subprocess.run([sys.executable,"-B",str(root/"validate_scenario_closure.py"),"--input",str(p)]).returncode != 0
    base["consumer_oracles"]=[{"id":"C","lock_id":"L","consumer_outcome":"outcome"}]; base["semantic_locks"]=[{"id":"L","relation_ids":["MISSING"]}]; p.write_text(json.dumps(base), encoding="utf-8")
    assert subprocess.run([sys.executable,"-B",str(root/"validate_scenario_closure.py"),"--input",str(p)]).returncode != 0
    base["relations"] = [{"id":"R","factor_ids":["F"],"semantic_relation":"owner-to-consumer","disposition":"preserved"}]; base["consumer_oracles"]=[{}]; p.write_text(json.dumps(base), encoding="utf-8")
    assert subprocess.run([sys.executable,"-B",str(root/"validate_scenario_closure.py"),"--input",str(p)]).returncode != 0
print("scenario closure countermodels passed")
