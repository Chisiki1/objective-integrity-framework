#!/usr/bin/env python3
"""Stage one instruction-only candidate with explicit commit-state evidence."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sys, uuid
from pathlib import Path
from typing import Any

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
def sha_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest().upper()
def sha(path: Path) -> str: return sha_bytes(path.read_bytes())
def canonical(value: Any) -> bytes: return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
def is_under(path: Path, root: Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False
def manifest(root: Path) -> str:
    return sha_bytes("\n".join(f"{item.relative_to(root).as_posix()}\t{sha(item)}" for item in sorted(root.rglob("*")) if item.is_file()).encode())
def material_sha(data: dict[str, Any]) -> str: return sha_bytes(canonical({key:value for key,value in data.items() if key != "materialization_receipt_sha256"}))
def fault(point: str) -> None:
    if os.environ.get("MGSKILL_TEST_ADOPTION_FAULT") == point: raise RuntimeError(f"test fault:{point}")
def readback(destination: Path | None) -> dict[str, Any]:
    if destination is None or not destination.exists(): return {"destination_exists":False, "manifest_sha256":None}
    try: return {"destination_exists":True, "manifest_sha256":manifest(destination)}
    except OSError as exc: return {"destination_exists":True, "manifest_sha256":None, "readback_error":str(exc)}
def fail(primary: str, state: str, stage: Path | None, destination: Path | None, writes: list[str], cleanup: list[str] | None = None) -> int:
    cleanup = cleanup or []; rollback = "not-needed"
    if state in {"PREPARED", "STAGED", "COMMITTING"} and stage and stage.exists():
        try: shutil.rmtree(stage); rollback = "precommit-staging-removed"
        except OSError as exc: rollback = f"precommit-cleanup-failed:{exc}"; cleanup.append(rollback)
    elif state in {"COMMITTED", "VERIFIED"}:
        rollback = "committed-destination-preserved-pending-explicit-recovery"
    print(json.dumps({"decision":"OUTCOME_UNKNOWN" if state in {"COMMITTED", "VERIFIED"} else "ADOPTION_FAILED", "transaction_state":state, "primary_fault":primary, "cleanup_failures":cleanup, "rollback_result":rollback, "readback":readback(destination), "writes_performed":writes}, sort_keys=True, indent=2)); return 2

def main() -> int:
    p=argparse.ArgumentParser()
    for name in ("proposal","candidate-root","isolated-adoption-root","expected-destination-manifest-sha256","registry","expected-registry-sha256","registry-entry"): p.add_argument(f"--{name}",required=True)
    p.add_argument("--discovery-root",action="append",required=True); p.add_argument("--apply",action="store_true"); a=p.parse_args()
    receipt=json.loads(Path(a.proposal).resolve(strict=True).read_text(encoding="utf-8")); candidate_root=Path(a.candidate_root).resolve(strict=True); destination=Path(a.isolated_adoption_root).resolve(); registry=Path(a.registry).resolve(strict=True); discoveries=[Path(item).resolve(strict=True) for item in a.discovery_root]
    if not a.apply or receipt.get("decision")!="INACTIVE_CANDIDATE_MATERIALIZED": print(json.dumps({"decision":"HOLD_ADOPTION","writes_performed":[],"reason":"materialized receipt and explicit --apply required"},sort_keys=True)); return 3
    if receipt.get("materialization_receipt_sha256")!=material_sha(receipt): return fail("materialization receipt self-hash mismatch","PREPARED",None,None,[])
    candidate,owner=receipt.get("candidate",{}),receipt.get("owner",{})
    if not isinstance(candidate,dict) or not SAFE_NAME.fullmatch(str(candidate.get("candidate_id",""))) or not isinstance(owner,dict) or owner.get("lane") not in {"WORK","COORDINATED-WORK"}: return fail("candidate or owner binding invalid","PREPARED",None,None,[])
    try: skill=Path(str(receipt.get("candidate_path",""))).resolve(strict=True); members=[{"path":"SKILL.md","sha256":sha(skill)}]
    except OSError as exc: return fail(f"candidate readback unavailable:{exc}","PREPARED",None,None,[])
    if receipt.get("support_mode")!="instruction-only" or receipt.get("candidate_member_set")!=members or candidate_root not in skill.parents or sha(skill)!=receipt.get("candidate_artifact_sha256") or sha(skill)!=candidate.get("artifact_sha256"): return fail("candidate member-set/current bytes binding invalid","PREPARED",None,None,[])
    if any(destination==root or is_under(destination,root) or is_under(root,destination) for root in discoveries) or destination==candidate_root or is_under(destination,candidate_root): return fail("destination intersects candidate or active discovery root","PREPARED",None,None,[])
    if a.expected_destination_manifest_sha256.upper()!="ABSENT" or destination.exists(): return fail("destination CAS requires an absent isolated root","PREPARED",None,destination,[])
    try: registry_bytes=registry.read_bytes()
    except OSError as exc: return fail(f"registry read failed:{exc}","PREPARED",None,None,[])
    if sha_bytes(registry_bytes)!=a.expected_registry_sha256.upper(): return fail("source registry CAS mismatch","PREPARED",None,None,[])
    try: entry,source_registry=json.loads(a.registry_entry),json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError): return fail("registry or entry JSON invalid","PREPARED",None,None,[])
    required={"skill_id","name","version","origin","relative_path","status","match_clauses","files"}
    if not isinstance(entry,dict) or not required<=set(entry) or entry.get("status")!="candidate" or entry.get("name")!=candidate["candidate_id"] or entry.get("relative_path")!=candidate["candidate_id"] or entry.get("files")!=members: return fail("entry must exactly bind instruction-only candidate path/name/member-set","PREPARED",None,None,[])
    if any(item.get("skill_id")==entry["skill_id"] or item.get("name")==entry["name"] for item in source_registry.get("entries",[]) if isinstance(item,dict)): return fail("duplicate registry identity","PREPARED",None,None,[])
    if not destination.parent.exists(): return fail("destination parent missing","PREPARED",None,None,[])
    state="PREPARED"; stage=destination.parent/f".{destination.name}.stage-{uuid.uuid4().hex}"; writes=[]; cleanup=[]
    try:
        stage.mkdir(); staged_skill=stage/candidate["candidate_id"] / "SKILL.md"; staged_skill.parent.mkdir(); shutil.copy2(skill,staged_skill); writes.append(str(staged_skill)); fault("after-skill-copy")
        if sha(staged_skill)!=members[0]["sha256"]: raise RuntimeError("staged skill readback mismatch")
        backup=stage/(registry.name+".backup"); backup.write_bytes(registry_bytes); writes.append(str(backup)); fault("after-backup")
        source_registry["entries"].append(entry); staged_registry=stage/registry.name; staged_registry.write_text(json.dumps(source_registry,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8"); writes.append(str(staged_registry)); state="STAGED"; fault("after-registry-stage")
        if sha(backup)!=sha_bytes(registry_bytes) or not staged_registry.is_file() or not staged_skill.is_file(): raise RuntimeError("staged target incomplete")
        state="COMMITTING"; fault("before-rename"); stage.replace(destination); writes.append(str(destination)); state="COMMITTED"; fault("after-rename")
        current=readback(destination)
        if current.get("manifest_sha256") is None or sha(destination/candidate["candidate_id"] / "SKILL.md")!=members[0]["sha256"]: raise RuntimeError("committed readback mismatch")
        fault("after-readback"); state="VERIFIED"; fault("after-verified")
        result={"decision":"ISOLATED_ADOPTION_STAGED","transaction_state":state,"materialization_receipt_sha256":receipt["materialization_receipt_sha256"],"candidate_id":candidate["candidate_id"],"owner":owner,"source_claims":receipt.get("source_claims"),"destination_manifest_sha256":current["manifest_sha256"],"registry_backup_sha256":sha(destination/(registry.name+".backup")),"registry_sha256":sha(destination/registry.name),"skill_path":str(destination/candidate["candidate_id"] / "SKILL.md"),"skill_sha256":sha(destination/candidate["candidate_id"] / "SKILL.md"),"readback":current,"rollback":"remove only this destination root after explicit recovery decision; source registry/candidate unchanged","writes_performed":writes}
        result["receipt_sha256"]=sha_bytes(canonical(result)); print(json.dumps(result,sort_keys=True,indent=2)); return 0
    except (OSError,RuntimeError) as exc: return fail(str(exc),state,stage,destination,writes,cleanup)
if __name__=="__main__": sys.exit(main())
