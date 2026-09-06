#!/usr/bin/env python3
"""Build the canonical v2 fact-compiler input from an explicit source/action plan.

This is deliberately a builder, not a selector: it rejects skill names and
does not consult the registry beyond the compiler's later validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


FACT_FIELDS = ("job", "action", "tool", "environment", "cause_families", "permissions", "resources", "consumers", "risks")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest().upper()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def fail(errors: list[str]) -> int:
    print(json.dumps({"decision": "REJECTED_FACT_INPUT", "errors": sorted(set(errors))}, sort_keys=True, indent=2))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    registry = json.loads(Path(args.registry).resolve(strict=True).read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(plan, dict) or plan.get("schema_version") != "mgskill-fact-builder-plan-v1":
        return fail(["unsupported plan schema"])
    source_doc = plan.get("source_document")
    if not isinstance(source_doc, dict) or not isinstance(source_doc.get("path"), str):
        return fail(["source_document.path is required"])
    try:
        document_path = Path(source_doc["path"]).resolve(strict=True)
        document_bytes = document_path.read_bytes()
        document = json.loads(document_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return fail([f"source_document unreadable:{exc}"])
    clauses = {str(item.get("id")): str(item.get("text")) for item in document.get("clauses", []) if isinstance(item, dict)}
    claim_ids = plan.get("source_claims")
    if not isinstance(claim_ids, list) or not claim_ids or len(set(claim_ids)) != len(claim_ids) or any(item not in clauses for item in claim_ids):
        errors.append("source_claims must be unique authoritative clause IDs")
    records: list[dict[str, Any]] = []
    by_id = {item.get("id"): item for item in plan.get("source_records", []) if isinstance(item, dict)}
    if set(by_id) != set(claim_ids or []) or len(by_id) != len(plan.get("source_records", [])):
        errors.append("source_records must contain exactly one record per source_claim")
    for claim_id in claim_ids or []:
        item = by_id.get(claim_id, {})
        disposition = item.get("disposition")
        facts = item.get("facts", {})
        excluded = item.get("excluded_facts", {})
        if disposition not in {"fact-bearing", "no-selection-fact"}:
            errors.append(f"{claim_id}: invalid disposition")
        if not isinstance(item.get("classification"), str) or not item["classification"].strip():
            errors.append(f"{claim_id}: classification required")
        if not isinstance(item.get("intent"), str) or not item["intent"].strip() or not isinstance(item.get("mechanism"), str) or not item["mechanism"].strip():
            errors.append(f"{claim_id}: intent/mechanism required")
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            errors.append(f"{claim_id}: evidence required")
        if not isinstance(facts, dict) or not isinstance(excluded, dict) or set(facts) - set(FACT_FIELDS) or set(excluded) - set(FACT_FIELDS):
            errors.append(f"{claim_id}: invalid fact fields")
        if disposition == "fact-bearing" and not any(facts.values()):
            errors.append(f"{claim_id}: fact-bearing requires facts")
        if disposition == "no-selection-fact" and (any(facts.values()) or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            errors.append(f"{claim_id}: no-selection-fact requires reason and no facts")
        records.append({**item, "id": claim_id, "text": clauses.get(claim_id), "text_sha256": text_sha(clauses.get(claim_id, ""))})
    actions = plan.get("action_records")
    if not isinstance(actions, list) or not actions:
        errors.append("action_records required")
    normalized_actions: list[dict[str, Any]] = []
    for action in actions or []:
        if not isinstance(action, dict) or not isinstance(action.get("final_payload"), dict):
            errors.append("action final_payload required")
            continue
        payload = action["final_payload"]
        facts = payload.get("facts")
        if not isinstance(facts, dict) or set(facts) - set(FACT_FIELDS):
            errors.append(f"{action.get('id')}: final_payload.facts invalid")
        normalized_actions.append({**action, "facts": facts, "final_payload_sha256": sha(payload)})
    forbidden = {str(entry.get(key, "")).strip().casefold() for entry in registry.get("entries", []) if isinstance(entry, dict) for key in ("skill_id", "name")}
    for record in [*records, *normalized_actions]:
        for bag_name in ("facts", "excluded_facts"):
            bag = record.get(bag_name, {})
            if isinstance(bag, dict):
                for values in bag.values():
                    if isinstance(values, list) and any(isinstance(value, str) and value.strip().casefold() in forbidden for value in values):
                        errors.append(f"{record.get('id')}: desired skill identity cannot be a fact")
    authority_message = plan.get("current_authority", {}).get("message") if isinstance(plan.get("current_authority"), dict) else None
    if not isinstance(authority_message, str) or not authority_message.strip():
        errors.append("current_authority.message required")
    challenge = plan.get("negative_selection_challenge")
    if not isinstance(challenge, dict) or challenge.get("status") != "completed" or not isinstance(challenge.get("countermodel"), str) or not challenge["countermodel"].strip():
        errors.append("completed negative_selection_challenge required")
    if errors:
        return fail(errors)
    output = {
        "schema_version": "mgskill-fact-source-v2",
        "objective_id": plan.get("objective_id"),
        "current_authority": {"message": authority_message, "sha256": text_sha(authority_message)},
        "source_document": {"path": str(document_path), "sha256": hashlib.sha256(document_bytes).hexdigest().upper()},
        "source_claims": claim_ids,
        "source_records": records,
        "action_records": normalized_actions,
        "negative_selection_challenge": challenge,
        "blind_phase": plan.get("blind_phase", "none"),
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"decision": "BUILT_FACT_INPUT", "output_sha256": hashlib.sha256(Path(args.output).read_bytes()).hexdigest().upper()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
