#!/usr/bin/env python3
"""Compile provenance-bound source/action facts for the v1 skill resolver."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


FACT_FIELDS = (
    "job",
    "action",
    "tool",
    "environment",
    "cause_families",
    "permissions",
    "resources",
    "consumers",
    "risks",
)
FINALITY = {"finalized", "not-finalized"}
SCHEMA_STATUS = {"available", "unavailable", "not-applicable"}
DISPOSITIONS = {"fact-bearing", "no-selection-fact"}
CLASSIFICATIONS = {"primary", "acceptance", "constraint", "method", "evidence", "authority", "reporting"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def validate_schema_subset(value: Any, schema: Any, path: str = "payload") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    errors: list[str] = []
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)
    if not type_ok:
        return [f"{path}: expected {expected}"]
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if expected == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: not in enum")
    if expected == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema_subset(item, schema["items"], f"{path}[{index}]"))
    if expected == "object":
        required = schema.get("required", [])
        if not isinstance(required, list):
            errors.append(f"{path}: required must be an array")
            required = []
        for field in required:
            if field not in value:
                errors.append(f"{path}.{field}: required")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            errors.append(f"{path}: properties must be an object")
            properties = {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: additional properties {extra}")
        for field in sorted(set(value) & set(properties)):
            errors.extend(validate_schema_subset(value[field], properties[field], f"{path}.{field}"))
    return errors


def tokens(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{field} must be an array of strings")
        return []
    return sorted({item.strip().casefold() for item in value if item.strip()})


def identifiers(value: Any, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        errors.append(f"{field} must be a non-empty array of strings")
        return []
    return sorted({item.strip() for item in value})


def normalize_record(
    record: Any,
    kind: str,
    known_claims: set[str],
    errors: list[str],
) -> tuple[dict[str, list[str]], list[dict[str, str]], list[dict[str, str]], str | None]:
    if not isinstance(record, dict):
        errors.append(f"{kind} record must be an object")
        return {}, [], [], None
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        errors.append(f"{kind} record id is required")
        return {}, [], [], None
    record_id = record_id.strip()
    if kind == "source" and record_id not in known_claims:
        errors.append(f"unknown source record:{record_id}")
    disposition = record.get("disposition")
    if disposition not in DISPOSITIONS:
        errors.append(f"{record_id}: invalid disposition:{disposition}")
    evidence = record.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        errors.append(f"{record_id}: evidence is required")
        evidence = ""
    reason = record.get("reason")
    if disposition == "no-selection-fact" and (not isinstance(reason, str) or not reason.strip()):
        errors.append(f"{record_id}: no-selection-fact requires reason")
    if kind == "source":
        if record.get("classification") not in CLASSIFICATIONS:
            errors.append(f"{record_id}: invalid classification")
        for field in ("intent", "mechanism"):
            if not meaningful(record.get(field)):
                errors.append(f"{record_id}: {field} is required")

    facts_raw = record.get("facts", {})
    excluded_raw = record.get("excluded_facts", {})
    if not isinstance(facts_raw, dict):
        errors.append(f"{record_id}: facts must be an object")
        facts_raw = {}
    if not isinstance(excluded_raw, dict):
        errors.append(f"{record_id}: excluded_facts must be an object")
        excluded_raw = {}
    unknown = sorted((set(facts_raw) | set(excluded_raw)) - set(FACT_FIELDS))
    if unknown:
        errors.append(f"{record_id}: unsupported fact fields:{unknown}")

    normalized: dict[str, list[str]] = {}
    provenance: list[dict[str, str]] = []
    exclusions: list[dict[str, str]] = []
    for field in FACT_FIELDS:
        positive = tokens(facts_raw.get(field, []), f"{record_id}.facts.{field}", errors)
        negative = tokens(excluded_raw.get(field, []), f"{record_id}.excluded_facts.{field}", errors)
        overlap = sorted(set(positive) & set(negative))
        if overlap:
            errors.append(f"{record_id}: positive/excluded conflict {field}:{overlap}")
        if positive:
            normalized[field] = positive
            provenance.extend(
                {"field": field, "token": token, "record_id": record_id, "evidence": evidence.strip()}
                for token in positive
            )
        exclusions.extend(
            {"field": field, "token": token, "record_id": record_id, "reason": str(reason or "explicitly excluded")}
            for token in negative
        )
    if disposition == "fact-bearing" and not normalized:
        errors.append(f"{record_id}: fact-bearing record has no facts")
    if disposition == "no-selection-fact" and normalized:
        errors.append(f"{record_id}: no-selection-fact record cannot emit facts")
    return normalized, provenance, exclusions, record_id


def compile_facts(source: dict[str, Any], registry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if source.get("schema_version") != "mgskill-fact-source-v2":
        errors.append("unsupported schema_version")
    current_authority = source.get("current_authority")
    if not isinstance(current_authority, dict) or not meaningful(current_authority.get("message")):
        errors.append("current_authority.message is required")
    elif sha256_bytes(current_authority["message"].encode("utf-8")) != str(current_authority.get("sha256", "")).upper():
        errors.append("current_authority sha256 mismatch")
    source_document = source.get("source_document")
    authoritative_clauses: dict[str, str] = {}
    if not isinstance(source_document, dict):
        errors.append("source_document is required")
    else:
        source_path_value = source_document.get("path")
        expected_source_sha = source_document.get("sha256")
        try:
            source_path = Path(str(source_path_value)).resolve(strict=True)
            actual_source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest().upper()
            if actual_source_sha != str(expected_source_sha).upper():
                errors.append("source_document sha256 mismatch")
            authoritative = load_json(source_path)
            authoritative_clauses = {
                str(item.get("id")): str(item.get("text"))
                for item in authoritative.get("clauses", [])
                if isinstance(item, dict)
            }
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            errors.append(f"source_document unreadable:{exc}")
    objective_id = source.get("objective_id")
    if not isinstance(objective_id, str) or not objective_id.strip():
        errors.append("objective_id is required")
    source_claims = identifiers(source.get("source_claims"), "source_claims", errors)
    if not source_claims:
        errors.append("source_claims must be non-empty")
    blind_phase = source.get("blind_phase", "none")
    if blind_phase not in {"none", "initial", "reconciled"}:
        errors.append("blind_phase must be none, initial, or reconciled")

    challenge = source.get("negative_selection_challenge")
    if not isinstance(challenge, dict) or challenge.get("status") != "completed":
        errors.append("negative_selection_challenge.status must be completed")
    elif not isinstance(challenge.get("countermodel"), str) or not challenge["countermodel"].strip():
        errors.append("negative_selection_challenge.countermodel is required")

    aggregated = {field: set() for field in FACT_FIELDS}
    provenance: list[dict[str, str]] = []
    exclusions: list[dict[str, str]] = []
    seen_claims: list[str] = []
    known_claims = set(source_claims)
    for record in source.get("source_records", []):
        normalized, prov, excluded, record_id = normalize_record(record, "source", known_claims, errors)
        if record_id:
            seen_claims.append(record_id)
            record_text = record.get("text") if isinstance(record, dict) else None
            if record_text != authoritative_clauses.get(record_id):
                errors.append(f"{record_id}: authoritative text mismatch")
            expected_text_sha = record.get("text_sha256") if isinstance(record, dict) else None
            actual_text_sha = sha256_bytes(str(record_text).encode("utf-8"))
            if actual_text_sha != str(expected_text_sha).upper():
                errors.append(f"{record_id}: text_sha256 mismatch")
        for field, values in normalized.items():
            aggregated[field].update(values)
        provenance.extend(prov)
        exclusions.extend(excluded)
    missing_claims = sorted(known_claims - set(seen_claims))
    duplicate_claims = sorted({item for item in seen_claims if seen_claims.count(item) > 1})
    if missing_claims:
        errors.append(f"source claims without disposition:{missing_claims}")
    if duplicate_claims:
        errors.append(f"duplicate source dispositions:{duplicate_claims}")

    action_status: list[dict[str, str]] = []
    action_bindings: list[dict[str, Any]] = []
    action_records = source.get("action_records", [])
    if not isinstance(action_records, list) or not action_records:
        errors.append("action_records must be a non-empty array")
        action_records = []
    action_ids: list[str] = []
    for record in action_records:
        normalized, prov, excluded, record_id = normalize_record(record, "action", known_claims, errors)
        if record_id:
            action_ids.append(record_id)
        finality = record.get("finality") if isinstance(record, dict) else None
        if finality not in FINALITY:
            errors.append(f"{record_id}: invalid finality:{finality}")
        final_payload = record.get("final_payload") if isinstance(record, dict) else None
        payload_sha = sha256_bytes(canonical_bytes(final_payload)) if final_payload is not None else None
        if final_payload is None:
            errors.append(f"{record_id}: final_payload is required")
        if payload_sha != str(record.get("final_payload_sha256", "")).upper():
            errors.append(f"{record_id}: final_payload_sha256 mismatch")
        payload_facts = final_payload.get("facts") if isinstance(final_payload, dict) else None
        if not isinstance(payload_facts, dict):
            errors.append(f"{record_id}: final_payload.facts is required")
            payload_facts = {}
        declared_facts = record.get("facts", {}) if isinstance(record, dict) else {}
        normalized_payload_facts = {
            field: tokens(payload_facts.get(field, []), f"{record_id}.final_payload.facts.{field}", errors)
            for field in FACT_FIELDS
            if payload_facts.get(field, [])
        }
        normalized_declared_facts = {
            field: tokens(declared_facts.get(field, []), f"{record_id}.facts.{field}", errors)
            for field in FACT_FIELDS
            if declared_facts.get(field, [])
        }
        if normalized_payload_facts != normalized_declared_facts:
            errors.append(f"{record_id}: declared facts do not equal canonical final_payload.facts")
        schema = record.get("tool_schema") if isinstance(record, dict) else None
        if not isinstance(schema, dict) or schema.get("status") not in SCHEMA_STATUS:
            errors.append(f"{record_id}: invalid tool_schema status")
            schema_status = "unavailable"
        else:
            schema_status = schema["status"]
            if schema_status == "available" and not meaningful(schema.get("identity")):
                errors.append(f"{record_id}: available tool_schema requires identity")
            if schema_status == "unavailable" and not isinstance(schema.get("reason"), str):
                errors.append(f"{record_id}: unavailable tool_schema requires reason")
        schema_errors: list[str] = []
        if schema_status == "available":
            schema_errors = validate_schema_subset(final_payload, schema.get("schema"))
            errors.extend(f"{record_id}: {item}" for item in schema_errors)
        status = "ready" if finality == "finalized" and payload_sha and schema_status == "not-applicable" else "unproven"
        if finality == "finalized" and payload_sha and schema_status == "available" and not schema_errors:
            status = "ready"
        action_status.append({"record_id": str(record_id), "status": status, "finality": str(finality), "tool_schema": schema_status})
        action_bindings.append({
            "action_id": str(record_id), "finality": str(finality), "status": status,
            "final_payload_sha256": payload_sha,
            "final_payload": final_payload,
            "tool_schema_status": schema_status,
            "tool_schema_identity": schema.get("identity") if isinstance(schema, dict) else None,
            "tool_schema_sha256": sha256_bytes(canonical_bytes(schema)) if isinstance(schema, dict) else None,
        })
        for field, values in normalized.items():
            aggregated[field].update(values)
        provenance.extend(prov)
        exclusions.extend(excluded)
    duplicate_action_ids = sorted({item for item in action_ids if action_ids.count(item) > 1})
    if duplicate_action_ids:
        errors.append(f"duplicate action records:{duplicate_action_ids}")

    forbidden_names = {
        str(entry.get(key, "")).strip().casefold()
        for entry in registry.get("entries", [])
        if isinstance(entry, dict)
        for key in ("skill_id", "name")
        if str(entry.get(key, "")).strip()
    }
    desired_name_facts = sorted(
        f"{field}:{token}"
        for field, values in aggregated.items()
        for token in values
        if token in forbidden_names
    )
    if desired_name_facts:
        errors.append(f"desired skill identities cannot be facts:{desired_name_facts}")

    matched: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for entry in registry.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_matches: list[dict[str, list[str]]] = []
        best_missing: list[str] | None = None
        best_overlap: list[str] = []
        for clause in entry.get("match_clauses", []):
            if not isinstance(clause, dict):
                continue
            missing: list[str] = []
            overlap: list[str] = []
            for field, expected in clause.items():
                expected_norm = {str(item).strip().casefold() for item in expected}
                hit = sorted(expected_norm & aggregated.get(field, set()))
                if hit:
                    overlap.extend(f"{field}:{token}" for token in hit)
                else:
                    missing.append(field)
            if not missing:
                entry_matches.append({"matched": sorted(overlap)})
            if best_missing is None or len(missing) < len(best_missing):
                best_missing, best_overlap = sorted(missing), sorted(overlap)
        item = {"skill_id": entry.get("skill_id"), "name": entry.get("name")}
        if entry_matches:
            matched.append({**item, "clauses": entry_matches})
        else:
            rejected.append({**item, "reason": "no exact clause match"})
            if best_overlap:
                near.append({**item, "overlap": best_overlap, "missing_fields": best_missing or []})

    resolver_input = {
        "schema_version": "mgskill-resolve-input-v1",
        "objective_id": objective_id,
        "source_claims": source_claims,
        **{field: sorted(values) for field, values in aggregated.items()},
        "blind_phase": blind_phase,
    }
    result = {
        "schema_version": "mgskill-fact-compiler-result-v2",
        "source_identity": sha256_bytes(canonical_bytes(source)),
        "registry_canonical_json_sha256": sha256_bytes(canonical_bytes(registry)),
        "resolver_input": resolver_input,
        "fact_provenance": sorted(provenance, key=lambda item: (item["field"], item["token"], item["record_id"])),
        "excluded_facts": sorted(exclusions, key=lambda item: (item["field"], item["token"], item["record_id"])),
        "source_dispositions": sorted(seen_claims),
        "source_semantic_bindings": [
            {
                "id": record.get("id"),
                "text_sha256": record.get("text_sha256"),
                "classification": record.get("classification"),
                "intent": record.get("intent"),
                "mechanism": record.get("mechanism"),
            }
            for record in source.get("source_records", [])
            if isinstance(record, dict)
        ],
        "action_status": action_status,
        "action_bindings": sorted(action_bindings, key=lambda item: item["action_id"]),
        "matched_candidates": matched,
        "near_matches": near,
        "rejected_candidates": rejected,
        "negative_selection_challenge": challenge,
        "proof_ceiling": "typed source/action fact provenance and registry-clause projection only; semantic completeness and skill benefit unproven",
    }
    result["compiler_snapshot_sha256"] = sha256_bytes(canonical_bytes(result))
    return result, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output")
    parser.add_argument("--resolver-input-output")
    args = parser.parse_args()
    source_path = Path(args.source).resolve(strict=True)
    registry_path = Path(args.registry).resolve(strict=True)
    result, errors = compile_facts(load_json(source_path), load_json(registry_path))
    result["registry_identity"] = hashlib.sha256(registry_path.read_bytes()).hexdigest().upper()
    result["compiler_snapshot_sha256"] = sha256_bytes(canonical_bytes({key: value for key, value in result.items() if key != "compiler_snapshot_sha256"}))
    envelope = {**result, "errors": errors, "valid": not errors}
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    if args.resolver_input_output and not errors:
        Path(args.resolver_input_output).write_text(
            json.dumps(result["resolver_input"], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
