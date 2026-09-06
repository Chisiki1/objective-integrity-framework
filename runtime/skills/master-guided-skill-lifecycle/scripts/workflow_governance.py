#!/usr/bin/env python3
"""Validate a bound workflow-governance proposal without granting authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "workflow-governance-v1"
RESULT_SCHEMA = "workflow-governance-result-v1"
TIERS = {"HIGHER_PRIORITY", "USER_CONDITION", "DELEGATED_METHOD", "UNKNOWN"}
EFFECT_STATES = {"NONE", "KNOWN", "UNKNOWN"}
DECISIONS = {"APPROVE", "REJECT"}
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
REQUEST_FIELDS = {
    "schema_version", "proposal", "current_conditions", "approval", "as_of", "effect_state"
}
CONDITION_FIELDS = {"id", "tier", "text_ref"}
PROPOSAL_FIELDS = {
    "id", "version", "owner", "scope", "changes", "dependency_ids", "source_refs",
    "rationale", "evaluation_ref", "independent_ref", "rollback_ref", "next_consumer", "return_step",
}
CHANGE_FIELDS = {"condition_id", "before_sha256", "after_text"}
REF_FIELDS = {"path", "sha256"}
APPROVAL_FIELDS = {
    "proposal_sha256", "scope", "condition_ids", "source_ref", "decision", "expires_at",
    "withdrawn", "semantic_review_ref",
}
PROOF_CEILING = (
    "T0 structure, bound-file identity, and routing only; source authority, approval meaning, "
    "semantic no-drop, action eligibility, activation, and consumer effect remain unproven."
)


class DuplicateKeyError(ValueError):
    pass


class BoundFileError(ValueError):
    def __init__(self, code: str, path: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.path = path
        self.detail = detail


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key:{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON number:{value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"nonfinite JSON number:{value}")
    return parsed


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"input is not UTF-8:{exc}") from exc
    return json.loads(
        text,
        object_pairs_hook=_pairs_no_duplicates,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _validate_json_scalars(value: Any, field: str, reasons: list[dict[str, str]]) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            reasons.append(_reason("INVALID_UNICODE_SCALAR", field, "contains an unpaired surrogate"))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_scalars(item, f"{field}[{index}]", reasons)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            try:
                key.encode("utf-8")
                child_field = f"{field}.{key}"
            except UnicodeEncodeError:
                reasons.append(_reason("INVALID_UNICODE_SCALAR", field, "object key contains an unpaired surrogate"))
                child_field = f"{field}.[invalid-key]"
            _validate_json_scalars(item, child_field, reasons)
        return
    if isinstance(value, float) and not math.isfinite(value):
        reasons.append(_reason("NONFINITE_NUMBER", field, "must be finite"))


def _is_reparse(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & marker)


def _components(path: Path) -> list[Path]:
    parts = path.parts
    if not parts:
        return []
    current = Path(parts[0])
    result = [current]
    for part in parts[1:]:
        current = current / part
        result.append(current)
    return result


def _validate_bound_path(path_text: str) -> tuple[Path, os.stat_result]:
    if not isinstance(path_text, str) or not path_text:
        raise BoundFileError("INVALID_PATH", str(path_text), "path must be a nonempty string")
    if "\x00" in path_text:
        raise BoundFileError("INVALID_PATH", path_text, "path contains NUL")
    try:
        path_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BoundFileError("INVALID_PATH", path_text, "path contains an unpaired surrogate") from exc
    path = Path(path_text)
    if not path.is_absolute():
        raise BoundFileError("PATH_NOT_ABSOLUTE", path_text, "bound path must be absolute")
    try:
        final_lstat: os.stat_result | None = None
        for component in _components(path):
            component_stat = os.lstat(component)
            if stat.S_ISLNK(component_stat.st_mode) or _is_reparse(component_stat):
                raise BoundFileError("REPARSE_PATH", path_text, f"reparse component:{component}")
            final_lstat = component_stat
        if final_lstat is None or not stat.S_ISREG(final_lstat.st_mode):
            raise BoundFileError("NOT_REGULAR_FILE", path_text, "bound path is not a regular file")
        return path, final_lstat
    except BoundFileError:
        raise
    except FileNotFoundError as exc:
        raise BoundFileError("FILE_NOT_FOUND", path_text, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise BoundFileError("INVALID_PATH", path_text, str(exc)) from exc


def _read_bound_file(
    path_text: str,
    validated: tuple[Path, os.stat_result] | None = None,
) -> bytes:
    try:
        path, final_lstat = validated if validated is not None else _validate_bound_path(path_text)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened_before = os.fstat(fd)
            if not stat.S_ISREG(opened_before.st_mode):
                raise BoundFileError("NOT_REGULAR_FILE", path_text, "opened target is not a regular file")
            if (final_lstat.st_dev, final_lstat.st_ino) != (opened_before.st_dev, opened_before.st_ino):
                raise BoundFileError("PATH_CHANGED", path_text, "path identity changed before open")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(fd)
            before_cut = (opened_before.st_dev, opened_before.st_ino, opened_before.st_size, opened_before.st_mtime_ns)
            after_cut = (opened_after.st_dev, opened_after.st_ino, opened_after.st_size, opened_after.st_mtime_ns)
            if before_cut != after_cut:
                raise BoundFileError("FILE_CHANGED_DURING_READ", path_text, "bound file changed during read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except BoundFileError:
        raise
    except FileNotFoundError as exc:
        raise BoundFileError("FILE_NOT_FOUND", path_text, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise BoundFileError("FILE_READ_ERROR", path_text, str(exc)) from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(raw)


def _reason(code: str, field: str, detail: str) -> dict[str, str]:
    return {"code": code, "field": field, "detail": detail}


def _exact_object(value: Any, field: str, allowed: set[str], reasons: list[dict[str, str]]) -> bool:
    if not isinstance(value, dict):
        reasons.append(_reason("MALFORMED_TYPE", field, "must be an object"))
        return False
    for name in sorted(allowed - set(value)):
        reasons.append(_reason("MISSING_FIELD", f"{field}.{name}", "required"))
    for name in sorted(set(value) - allowed):
        reasons.append(_reason("UNKNOWN_FIELD", f"{field}.{name}", "not allowed"))
    return allowed == set(value)


def _nonempty_string(value: Any, field: str, reasons: list[dict[str, str]]) -> bool:
    if not isinstance(value, str) or not value.strip():
        reasons.append(_reason("MALFORMED_TYPE", field, "must be a nonempty string"))
        return False
    return True


def _sha_string(value: Any, field: str, reasons: list[dict[str, str]]) -> bool:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        reasons.append(_reason("MALFORMED_SHA256", field, "must be 64 hexadecimal characters"))
        return False
    return True


def _aware_time(value: Any, field: str, nullable: bool, reasons: list[dict[str, str]]) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        reasons.append(_reason("MALFORMED_TIME", field, "must be a timezone-aware ISO timestamp"))
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone offset is absent")
        return parsed
    except (ValueError, OverflowError) as exc:
        reasons.append(_reason("MALFORMED_TIME", field, str(exc)))
        return None


def _string_list(
    value: Any, field: str, reasons: list[dict[str, str]], *, nonempty: bool = True, unique: bool = True
) -> list[str] | None:
    if not isinstance(value, list) or (nonempty and not value):
        reasons.append(_reason("MALFORMED_TYPE", field, "must be a nonempty string array"))
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        reasons.append(_reason("MALFORMED_TYPE", field, "must contain only nonempty strings"))
        return None
    if unique and len(value) != len(set(value)):
        reasons.append(_reason("DUPLICATE_ID", field, "values must be unique"))
        return None
    return value


def _validate_ref_shape(value: Any, field: str, reasons: list[dict[str, str]]) -> bool:
    if not _exact_object(value, field, REF_FIELDS, reasons):
        return False
    return _nonempty_string(value["path"], f"{field}.path", reasons) and _sha_string(
        value["sha256"], f"{field}.sha256", reasons
    )


def _validate_shape(data: Any) -> tuple[list[dict[str, str]], datetime | None]:
    reasons: list[dict[str, str]] = []
    _validate_json_scalars(data, "request", reasons)
    if reasons:
        return reasons, None
    if not _exact_object(data, "request", REQUEST_FIELDS, reasons):
        return reasons, None
    if data["schema_version"] != SCHEMA:
        reasons.append(_reason("UNSUPPORTED_SCHEMA", "schema_version", f"expected {SCHEMA}"))
    if not isinstance(data["effect_state"], str) or data["effect_state"] not in EFFECT_STATES:
        reasons.append(_reason("MALFORMED_ENUM", "effect_state", "must be NONE, KNOWN, or UNKNOWN"))
    as_of = _aware_time(data["as_of"], "as_of", False, reasons)

    conditions = data["current_conditions"]
    if not isinstance(conditions, list):
        reasons.append(_reason("MALFORMED_TYPE", "current_conditions", "must be an array"))
    else:
        ids: list[str] = []
        for index, condition in enumerate(conditions):
            prefix = f"current_conditions[{index}]"
            if not _exact_object(condition, prefix, CONDITION_FIELDS, reasons):
                continue
            if _nonempty_string(condition["id"], f"{prefix}.id", reasons):
                ids.append(condition["id"])
            if not isinstance(condition["tier"], str) or condition["tier"] not in TIERS:
                reasons.append(_reason("MALFORMED_ENUM", f"{prefix}.tier", "invalid authority tier"))
            _validate_ref_shape(condition["text_ref"], f"{prefix}.text_ref", reasons)
        if len(ids) != len(set(ids)):
            reasons.append(_reason("DUPLICATE_ID", "current_conditions", "condition ids must be unique"))

    proposal = data["proposal"]
    if proposal is not None:
        if _exact_object(proposal, "proposal", PROPOSAL_FIELDS, reasons):
            for name in ("id", "version", "owner", "scope", "rationale", "next_consumer", "return_step"):
                _nonempty_string(proposal[name], f"proposal.{name}", reasons)
            changes = proposal["changes"]
            change_ids: list[str] = []
            if not isinstance(changes, list) or not changes:
                reasons.append(_reason("MALFORMED_TYPE", "proposal.changes", "must be a nonempty array"))
            else:
                for index, change in enumerate(changes):
                    prefix = f"proposal.changes[{index}]"
                    if not _exact_object(change, prefix, CHANGE_FIELDS, reasons):
                        continue
                    if _nonempty_string(change["condition_id"], f"{prefix}.condition_id", reasons):
                        change_ids.append(change["condition_id"])
                    _sha_string(change["before_sha256"], f"{prefix}.before_sha256", reasons)
                    _nonempty_string(change["after_text"], f"{prefix}.after_text", reasons)
                if len(change_ids) != len(set(change_ids)):
                    reasons.append(_reason("DUPLICATE_ID", "proposal.changes", "changed condition ids must be unique"))
            dependencies = _string_list(proposal["dependency_ids"], "proposal.dependency_ids", reasons)
            if dependencies is not None and change_ids and not set(change_ids).issubset(dependencies):
                reasons.append(_reason("DEPENDENCY_OMISSION", "proposal.dependency_ids", "must include every changed id"))
            refs = proposal["source_refs"]
            if not isinstance(refs, list) or not refs:
                reasons.append(_reason("MALFORMED_TYPE", "proposal.source_refs", "must be a nonempty array"))
            else:
                for index, ref in enumerate(refs):
                    _validate_ref_shape(ref, f"proposal.source_refs[{index}]", reasons)
            for name in ("evaluation_ref", "independent_ref", "rollback_ref"):
                _validate_ref_shape(proposal[name], f"proposal.{name}", reasons)

    approval = data["approval"]
    if approval is not None:
        if _exact_object(approval, "approval", APPROVAL_FIELDS, reasons):
            _sha_string(approval["proposal_sha256"], "approval.proposal_sha256", reasons)
            _nonempty_string(approval["scope"], "approval.scope", reasons)
            ids = _string_list(approval["condition_ids"], "approval.condition_ids", reasons)
            if ids is not None and ids != sorted(ids):
                reasons.append(_reason("UNSORTED_IDS", "approval.condition_ids", "must be sorted"))
            if not isinstance(approval["decision"], str) or approval["decision"] not in DECISIONS:
                reasons.append(_reason("MALFORMED_ENUM", "approval.decision", "must be APPROVE or REJECT"))
            if not isinstance(approval["withdrawn"], bool):
                reasons.append(_reason("MALFORMED_TYPE", "approval.withdrawn", "must be boolean"))
            _aware_time(approval["expires_at"], "approval.expires_at", True, reasons)
            _validate_ref_shape(approval["source_ref"], "approval.source_ref", reasons)
            _validate_ref_shape(approval["semantic_review_ref"], "approval.semantic_review_ref", reasons)
    return reasons, as_of


def _collect_refs(data: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    refs: list[tuple[str, dict[str, str]]] = []
    for index, condition in enumerate(data["current_conditions"]):
        refs.append((f"current_conditions[{index}].text_ref", condition["text_ref"]))
    proposal = data["proposal"]
    if proposal is not None:
        refs.extend((f"proposal.source_refs[{index}]", ref) for index, ref in enumerate(proposal["source_refs"]))
        for name in ("evaluation_ref", "independent_ref", "rollback_ref"):
            refs.append((f"proposal.{name}", proposal[name]))
    approval = data["approval"]
    if approval is not None:
        refs.append(("approval.source_ref", approval["source_ref"]))
        refs.append(("approval.semantic_review_ref", approval["semantic_review_ref"]))
    return refs


def _check_refs(
    refs: list[tuple[str, dict[str, str]]],
) -> list[dict[str, Any]]:
    content_cache: dict[tuple[int, int], tuple[str | None, BoundFileError | None]] = {}
    normalized_identities: dict[str, tuple[int, int]] = {}
    results: list[dict[str, Any]] = []
    for role, ref in refs:
        path_text = ref["path"]
        cache_key = os.path.normcase(os.path.normpath(path_text))
        actual: str | None = None
        error: BoundFileError | None = None
        try:
            validated = _validate_bound_path(path_text)
            lexical_identity = (validated[1].st_dev, validated[1].st_ino)
        except BoundFileError as exc:
            validated = None
            lexical_identity = None
            error = exc
        if error is None and validated is not None:
            prior_identity = normalized_identities.get(cache_key)
            if prior_identity is not None and prior_identity != lexical_identity:
                error = BoundFileError(
                    "PATH_IDENTITY_CONFLICT",
                    path_text,
                    "normalized spellings do not identify the same file",
                )
            else:
                normalized_identities[cache_key] = lexical_identity
                cached_content = content_cache.get(lexical_identity)
                if cached_content is not None:
                    actual, error = cached_content
                else:
                    try:
                        actual = _sha256(_read_bound_file(path_text, validated))
                    except BoundFileError as exc:
                        error = exc
                    content_cache[lexical_identity] = (actual, error)
        item: dict[str, Any] = {
            "role": role,
            "path": path_text,
            "expected_sha256": ref["sha256"].upper(),
            "actual_sha256": actual,
        }
        if error is not None:
            item.update({"status": "ERROR", "error_code": error.code, "detail": error.detail})
        elif actual != ref["sha256"].upper():
            item.update({"status": "HASH_MISMATCH", "error_code": "HASH_MISMATCH"})
        else:
            item["status"] = "MATCH"
        results.append(item)
    return results


def _base_result(input_path: str | None, input_sha256: str | None) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "route": "INVALID_REQUEST",
        "authority_granted": False,
        "semantic_authority": "UNPROVEN",
        "dependent_scope": {
            "proposal_id": None,
            "scope": None,
            "condition_ids": [],
            "unrelated_work_eligible": True,
        },
        "input": {"path": input_path, "sha256": input_sha256},
        "proposal_sha256": None,
        "approval_sha256": None,
        "as_of": None,
        "effect_state": None,
        "source_hashes": [],
        "condition_dispositions": [],
        "reasons": [],
        "first_fault": None,
        "proof_ceiling": PROOF_CEILING,
    }


def _approval_reasons(
    data: dict[str, Any],
    proposal_sha256: str,
    change_ids: list[str],
    as_of: datetime | None,
    refs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    approval = data["approval"]
    reasons: list[dict[str, str]] = []
    if approval is None:
        return [_reason("APPROVAL_ABSENT", "approval", "exact approval is required")]
    if approval["decision"] != "APPROVE":
        reasons.append(_reason("APPROVAL_REJECTED", "approval.decision", approval["decision"]))
    if approval["withdrawn"]:
        reasons.append(_reason("APPROVAL_WITHDRAWN", "approval.withdrawn", "true"))
    if approval["expires_at"] is not None:
        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
        if as_of is not None and expires <= as_of:
            reasons.append(_reason("APPROVAL_EXPIRED", "approval.expires_at", approval["expires_at"]))
    if approval["proposal_sha256"].upper() != proposal_sha256:
        reasons.append(_reason("APPROVAL_PROPOSAL_MISMATCH", "approval.proposal_sha256", "does not match proposal"))
    if approval["scope"] != data["proposal"]["scope"]:
        reasons.append(_reason("APPROVAL_SCOPE_MISMATCH", "approval.scope", "does not match proposal scope"))
    if approval["condition_ids"] != sorted(change_ids):
        reasons.append(
            _reason(
                "APPROVAL_CONDITION_IDS_MISMATCH",
                "approval.condition_ids",
                "must exactly equal all changed condition ids",
            )
        )
    for item in refs:
        if item["role"].startswith("approval.") and item["status"] != "MATCH":
            reasons.append(_reason("APPROVAL_REFERENCE_MISMATCH", item["role"], item["status"]))
    return reasons


def _append_unique(reasons: list[dict[str, str]], additions: list[dict[str, str]]) -> None:
    seen = {(item["code"], item["field"], item["detail"]) for item in reasons}
    for item in additions:
        identity = (item["code"], item["field"], item["detail"])
        if identity not in seen:
            reasons.append(item)
            seen.add(identity)


def evaluate(input_path: str) -> dict[str, Any]:
    result = _base_result(input_path, None)
    try:
        raw = _read_bound_file(input_path)
        result["input"]["sha256"] = _sha256(raw)
        data = _decode_json(raw)
    except (BoundFileError, DuplicateKeyError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, BoundFileError):
            reason = _reason(exc.code, "input", exc.detail)
        elif isinstance(exc, DuplicateKeyError):
            reason = _reason("DUPLICATE_JSON_KEY", "input", str(exc))
        else:
            reason = _reason("INVALID_JSON", "input", str(exc))
        result["reasons"] = [reason]
        result["first_fault"] = reason
        return result

    shape_reasons, as_of = _validate_shape(data)
    if shape_reasons:
        result["reasons"] = shape_reasons
        result["first_fault"] = shape_reasons[0]
        return result

    proposal = data["proposal"]
    change_ids = [change["condition_id"] for change in proposal["changes"]] if proposal is not None else []
    result["as_of"] = data["as_of"]
    result["effect_state"] = data["effect_state"]
    result["dependent_scope"] = {
        "proposal_id": proposal["id"] if proposal is not None else None,
        "scope": proposal["scope"] if proposal is not None else None,
        "condition_ids": sorted(change_ids),
        "unrelated_work_eligible": True,
    }
    result["proposal_sha256"] = _canonical_sha256(proposal) if proposal is not None else None
    result["approval_sha256"] = _canonical_sha256(data["approval"]) if data["approval"] is not None else None

    refs = _check_refs(_collect_refs(data))
    result["source_hashes"] = refs
    condition_by_id = {condition["id"]: condition for condition in data["current_conditions"]}
    ref_by_role = {item["role"]: item for item in refs}
    changes = {change["condition_id"]: change for change in proposal["changes"]} if proposal is not None else {}
    dispositions: list[dict[str, Any]] = []
    for index, condition in enumerate(data["current_conditions"]):
        ref_status = ref_by_role[f"current_conditions[{index}].text_ref"]
        change = changes.get(condition["id"])
        baseline_match = (
            change is None
            or (ref_status["status"] == "MATCH" and change["before_sha256"].upper() == ref_status["actual_sha256"])
        )
        if ref_status["status"] != "MATCH":
            disposition = "REFERENCE_UNRESOLVED"
        elif change is not None:
            disposition = "CHANGED_CANDIDATE"
        else:
            disposition = "PRESERVED_UNCHANGED"
        dispositions.append({
            "id": condition["id"],
            "tier": condition["tier"],
            "changed": change is not None,
            "dependency": proposal is not None and condition["id"] in proposal["dependency_ids"],
            "text_ref_status": ref_status["status"],
            "text_sha256": ref_status["actual_sha256"],
            "baseline_match": baseline_match,
            "disposition": disposition,
        })
    for condition_id in sorted(set(change_ids) - set(condition_by_id)):
        dispositions.append({
            "id": condition_id,
            "tier": None,
            "changed": True,
            "dependency": proposal is not None and condition_id in proposal["dependency_ids"],
            "text_ref_status": "MISSING",
            "text_sha256": None,
            "baseline_match": False,
            "disposition": "MISSING_CURRENT_CONDITION",
        })
    result["condition_dispositions"] = dispositions

    invalid_refs = [item for item in refs if item["status"] != "MATCH"]
    non_condition_invalid = [item for item in invalid_refs if not item["role"].startswith("current_conditions[")]
    invalid_path_refs = [item for item in invalid_refs if item.get("error_code") == "INVALID_PATH"]
    changed_conditions_present = [condition_by_id[item] for item in change_ids if item in condition_by_id]
    has_user_condition = any(item["tier"] == "USER_CONDITION" for item in changed_conditions_present)
    approval_reasons = (
        _approval_reasons(data, result["proposal_sha256"], change_ids, as_of, refs)
        if proposal is not None and has_user_condition
        else []
    )
    if non_condition_invalid or invalid_path_refs:
        primary_refs = non_condition_invalid + [item for item in invalid_path_refs if item not in non_condition_invalid]
        reasons = [
            _reason(item.get("error_code", "BOUND_REFERENCE_ERROR"), item["role"], item.get("detail", "hash mismatch"))
            for item in primary_refs
        ]
        _append_unique(reasons, approval_reasons)
        if data["effect_state"] == "UNKNOWN":
            _append_unique(
                reasons,
                [_reason("UNKNOWN_EFFECT", "effect_state", "reconcile before dependent retry")],
            )
        result["reasons"] = reasons
        result["first_fault"] = reasons[0]
        return result

    if data["effect_state"] == "UNKNOWN":
        result["route"] = "RECONCILE_UNKNOWN_EFFECT"
        result["reasons"] = [_reason("UNKNOWN_EFFECT", "effect_state", "reconcile before dependent retry")]
        result["first_fault"] = result["reasons"][0]
        return result

    if proposal is None:
        result["route"] = "NO_CHANGE"
        return result

    baseline_reasons: list[dict[str, str]] = []
    for change in proposal["changes"]:
        condition = condition_by_id.get(change["condition_id"])
        if condition is None:
            baseline_reasons.append(_reason("MISSING_CURRENT_CONDITION", change["condition_id"], "not in current_conditions"))
            continue
        disposition = next(item for item in dispositions if item["id"] == change["condition_id"])
        if disposition["text_ref_status"] != "MATCH":
            baseline_reasons.append(_reason("CURRENT_CONDITION_REF_STALE", change["condition_id"], disposition["text_ref_status"]))
        elif not disposition["baseline_match"]:
            baseline_reasons.append(_reason("CURRENT_CONDITION_CHANGED", change["condition_id"], "before_sha256 mismatch"))
    dependency_missing = sorted(set(proposal["dependency_ids"]) - set(condition_by_id))
    for condition_id in dependency_missing:
        baseline_reasons.append(_reason("MISSING_DEPENDENCY_CONDITION", condition_id, "not in current_conditions"))
    for condition_id in sorted(set(proposal["dependency_ids"]) & set(condition_by_id)):
        disposition = next(item for item in dispositions if item["id"] == condition_id)
        if disposition["text_ref_status"] != "MATCH" and condition_id not in change_ids:
            baseline_reasons.append(
                _reason("DEPENDENCY_CONDITION_REF_STALE", condition_id, disposition["text_ref_status"])
            )
    if baseline_reasons:
        result["route"] = "REFRESH_DEPENDENT_BASELINE"
        result["reasons"] = baseline_reasons
        result["first_fault"] = baseline_reasons[0]
        return result

    changed_conditions = [condition_by_id[condition_id] for condition_id in change_ids]
    higher = [condition["id"] for condition in changed_conditions if condition["tier"] == "HIGHER_PRIORITY"]
    if higher:
        result["route"] = "OUTSIDE_USER_AMENDMENT"
        result["reasons"] = [_reason("HIGHER_PRIORITY_CONDITION", condition_id, "not user-waivable") for condition_id in higher]
        result["first_fault"] = result["reasons"][0]
        return result
    unknown = [condition["id"] for condition in changed_conditions if condition["tier"] == "UNKNOWN"]
    if unknown:
        result["route"] = "RESOLVE_AUTHORITY"
        result["reasons"] = [_reason("UNKNOWN_AUTHORITY_TIER", condition_id, "resolve source authority") for condition_id in unknown]
        result["first_fault"] = result["reasons"][0]
        return result

    user_ids = sorted(condition["id"] for condition in changed_conditions if condition["tier"] == "USER_CONDITION")
    if user_ids:
        if approval_reasons:
            result["route"] = "USER_DECISION"
            result["reasons"] = approval_reasons
            result["first_fault"] = approval_reasons[0]
            return result
        result["route"] = "MATCHED_APPROVAL_REQUIRES_SOURCE_REVIEW"
        result["reasons"] = [
            _reason("STRUCTURAL_MATCH_ONLY", "approval", "source identity and semantic approval remain unproven")
        ]
        return result

    result["route"] = "WITHIN_AUTHORITY_CANDIDATE"
    result["reasons"] = [
        _reason("DELEGATED_METHOD_STRUCTURE", "proposal.changes", "semantic no-drop and activation gates remain required")
    ]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="absolute path to workflow-governance-v1 JSON")
    args = parser.parse_args(argv)
    result = evaluate(args.input)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result["route"] != "INVALID_REQUEST" else 2


if __name__ == "__main__":
    sys.exit(main())
