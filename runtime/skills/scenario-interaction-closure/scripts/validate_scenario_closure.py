#!/usr/bin/env python3
"""Validate only the identity/no-drop shape of a scenario closure record."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

DISPOSITIONS = {"preserved", "decomposed", "composition-congruent", "cut", "orphan", "unproven", "not_applicable"}

def nonempty(value): return isinstance(value, str) and bool(value.strip())

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    path = Path(args.input).resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    required = {"schema_version", "closure_id", "objective_id", "source_ref", "blind_inputs", "factors", "relations", "semantic_locks", "consumer_oracles", "recomposition_witnesses", "unmasking", "delta_lineage"}
    if set(data) != required:
        errors.append("exact top-level schema required")
    if data.get("schema_version") != "scenario-interaction-closure-v1":
        errors.append("unsupported schema")
    for field in ("closure_id", "objective_id", "source_ref"):
        if not nonempty(data.get(field)):
            errors.append(f"{field}: non-empty string required")
    if not isinstance(data.get("blind_inputs"), dict) or data.get("blind_inputs", {}).get("status") not in {"frozen", "reconciled"}:
        errors.append("blind_inputs: frozen or reconciled record required")
    for field in ("factors", "relations", "semantic_locks", "consumer_oracles", "recomposition_witnesses"):
        if not isinstance(data.get(field), list) or not data.get(field):
            errors.append(f"{field}: non-empty array required")
    factor_ids = {item.get("id") for item in data.get("factors", []) if isinstance(item, dict) and nonempty(item.get("id"))}
    if len(factor_ids) != len(data.get("factors", [])) or any(not nonempty(item.get("activation")) for item in data.get("factors", []) if isinstance(item, dict)):
        errors.append("factors: non-empty unique ids required")
    relation_ids = set()
    for relation in data.get("relations", []):
        if not isinstance(relation, dict) or not nonempty(relation.get("id")) or relation.get("disposition") not in DISPOSITIONS:
            errors.append("relations: id and valid disposition required")
            continue
        if relation["id"] in relation_ids:
            errors.append("relations: duplicate id")
        relation_ids.add(relation["id"])
        if not isinstance(relation.get("factor_ids"), list) or not relation["factor_ids"] or not set(relation["factor_ids"]).issubset(factor_ids) or not nonempty(relation.get("semantic_relation")):
            errors.append("relations: known factor_ids required")
    lock_ids = {item.get("id") for item in data.get("semantic_locks", []) if isinstance(item, dict) and nonempty(item.get("id"))}
    if len(lock_ids) != len(data.get("semantic_locks", [])) or any(not isinstance(item.get("relation_ids"), list) or not item["relation_ids"] or not set(item["relation_ids"]).issubset(relation_ids) for item in data.get("semantic_locks", []) if isinstance(item, dict)):
        errors.append("semantic_locks: non-empty unique ids required")
    for oracle in data.get("consumer_oracles", []):
        if not isinstance(oracle, dict) or not nonempty(oracle.get("id")) or oracle.get("lock_id") not in lock_ids or not nonempty(oracle.get("consumer_outcome")):
            errors.append("consumer_oracles: id and known lock_id required")
    for witness in data.get("recomposition_witnesses", []):
        if not isinstance(witness, dict) or not nonempty(witness.get("id")) or witness.get("lock_id") not in lock_ids or witness.get("relation_id") not in relation_ids or not nonempty(witness.get("countermodel")) or not nonempty(witness.get("method")):
            errors.append("recomposition_witnesses: id, known lock_id and relation_id required")
    if not isinstance(data.get("unmasking"), dict) or not nonempty(data.get("unmasking", {}).get("recovery")):
        errors.append("unmasking: recovery record required")
    if not isinstance(data.get("delta_lineage"), list):
        errors.append("delta_lineage: array required")
    orphan_ids = {r.get("id") for r in data.get("relations", []) if isinstance(r, dict) and r.get("disposition") in {"orphan", "unproven"}}
    witnessed = {w.get("relation_id") for w in data.get("recomposition_witnesses", []) if isinstance(w, dict)}
    unresolved_without_reason = orphan_ids & witnessed
    if unresolved_without_reason:
        errors.append("orphan/unproven relation cannot claim a recomposition witness")
    result = {
        "schema_version": "scenario-interaction-closure-result-v1",
        "decision": "PASS_STRUCTURAL" if not errors else "HOLD",
        "errors": sorted(set(errors)),
        "relation_ids": sorted(relation_ids),
        "orphan_relation_ids": sorted(orphan_ids),
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        "proof_ceiling": "schema, identity, disposition and no-drop relation shape only; scenario completeness, reachability, harm and consumer outcome are unproven"
    }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if not errors else 3

if __name__ == "__main__": raise SystemExit(main())
