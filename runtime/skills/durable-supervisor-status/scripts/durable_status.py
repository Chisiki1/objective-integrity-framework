#!/usr/bin/env python3
"""Atomic Worker-local status and append-only journal for Supervisor pull.

This tool records observability/correction state only. It does not inspect,
intercept, authorize, execute, or prevent tool calls or external actions.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STATUS_SCHEMA_V1 = "durable-supervisor-status-v1"
STATUS_SCHEMA = "durable-supervisor-status-v2"
REQUEST_SCHEMA_V1 = "durable-supervisor-status-write-request-v1"
REQUEST_SCHEMA = "durable-supervisor-status-write-request-v2"
MESSAGE_SCHEMA = "durable-supervisor-message-binding-v1"
SELECTION_SCHEMA = "durable-supervisor-projection-evidence-v1"
SAFE_INTEGER = 9_007_199_254_740_991
MAX_JSON_BYTES = 1_048_576
MAX_JOURNAL_RECORDS = 4096
MAX_EFFECT_ITEMS = 128
MAX_DELIVERABLES = 256
SHA256_RE = re.compile(r"^[A-F0-9]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,511}$")
STATES = {"CONTINUE", "READY", "WORK_NEEDS_ATTENTION"}
PENDING_STATES = {"NONE", "READY_FOR_SUPERVISOR", "NEEDS_SUPERVISOR"}
TRANSITION_EFFECTS = {"STATUS_ONLY", "READ_ONLY_UPDATE", "DEPENDENT_TRANSITION"}
ACTION_EFFECTS = {"STATUS_ONLY", "READ_ONLY", "NO_EXTERNAL", "MATERIAL_OR_EXTERNAL"}
DELIVERABLE_STATES = {"OPEN", "BLOCKED", "SATISFIED"}


class StatusError(ValueError):
    pass


def reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StatusError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_float(_: str) -> None:
    raise StatusError("floating-point values are forbidden")


def load_json(path: Path, *, canonical_required: bool = False) -> Any:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise StatusError(f"JSON exceeds {MAX_JSON_BYTES} bytes: {path}")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise StatusError(f"UTF-8 BOM is forbidden: {path}")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_pairs,
            parse_float=reject_float,
            parse_constant=reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatusError(f"invalid UTF-8 JSON at {path}: {exc}") from exc
    validate_safe_numbers(value)
    if canonical_required and raw != canonical_bytes(value):
        raise StatusError(f"noncanonical durable record bytes: {path}")
    return value


def validate_safe_numbers(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > SAFE_INTEGER:
            raise StatusError("integer outside canonical safe range")
        return
    if isinstance(value, list):
        for item in value:
            validate_safe_numbers(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StatusError("object key must be a string")
            validate_safe_numbers(item)
        return
    raise StatusError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    validate_safe_numbers(value)
    text = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def record_body(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "record_sha256"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_utc(value: Any, label: str) -> str:
    text = require_text(value, label, maximum=64)
    if not text.endswith("Z"):
        raise StatusError(f"{label} must be an explicit UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise StatusError(f"{label} must be a valid ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise StatusError(f"{label} must be UTC")
    return text


def require_id(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise StatusError(f"{label} must be a bounded identifier")
    return value


def require_sha(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise StatusError(f"{label} must be uppercase SHA-256")
    return value


def require_text(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StatusError(f"{label} must be nonempty and at most {maximum} characters")
    return value


def exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatusError(f"{label} must be an object")
    actual = set(value)
    if actual != required:
        raise StatusError(f"{label} fields differ: missing={sorted(required - actual)} extra={sorted(actual - required)}")
    return value


def normalize_string_set(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EFFECT_ITEMS:
        raise StatusError(f"{label} must be an array of at most {MAX_EFFECT_ITEMS} items")
    items = [require_text(item, f"{label} item", maximum=512) for item in value]
    if len(items) != len(set(items)):
        raise StatusError(f"{label} contains duplicates")
    return sorted(items)


def normalize_evidence_delta(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise StatusError("actual_evidence_delta must be an array of at most 64 items")
    return [require_text(item, "actual_evidence_delta item", maximum=1024) for item in value]


def normalize_deliverables(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_DELIVERABLES:
        raise StatusError(f"open_deliverables must be an array of at most {MAX_DELIVERABLES} items")
    result: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, raw in enumerate(value):
        item = exact_keys(raw, {"deliverable_id", "state", "summary"}, f"open_deliverables[{index}]")
        deliverable_id = require_id(item["deliverable_id"], "deliverable_id")
        state = item["state"]
        if state not in DELIVERABLE_STATES:
            raise StatusError(f"invalid deliverable state: {state}")
        if deliverable_id in ids:
            raise StatusError(f"duplicate deliverable: {deliverable_id}")
        ids.add(deliverable_id)
        result.append({
            "deliverable_id": deliverable_id,
            "state": state,
            "summary": require_text(item["summary"], "deliverable summary", maximum=1024),
        })
    return sorted(result, key=lambda item: item["deliverable_id"])


def validate_identity(value: Any, label: str) -> dict[str, Any]:
    item = exact_keys(value, {"identity_id", "sha256"}, label)
    return {
        "identity_id": require_id(item["identity_id"], f"{label}.identity_id"),
        "sha256": require_sha(item["sha256"], f"{label}.sha256", nullable=True),
    }


def validate_objective(value: Any) -> dict[str, Any]:
    item = exact_keys(value, {"objective_id", "source", "source_sha256", "supervisor_task_id"}, "objective")
    return {
        "objective_id": require_id(item["objective_id"], "objective.objective_id"),
        "source": require_text(item["source"], "objective.source", maximum=2048),
        "source_sha256": require_sha(item["source_sha256"], "objective.source_sha256"),
        "supervisor_task_id": require_id(item["supervisor_task_id"], "objective.supervisor_task_id", nullable=True),
    }


def validate_worker(value: Any) -> dict[str, Any]:
    item = exact_keys(value, {"task_id", "turn_id", "lease_id"}, "worker")
    return {
        "task_id": require_id(item["task_id"], "worker.task_id", nullable=True),
        "turn_id": require_id(item["turn_id"], "worker.turn_id", nullable=True),
        "lease_id": require_id(item["lease_id"], "worker.lease_id", nullable=True),
    }


def validate_pending(value: Any) -> dict[str, Any]:
    item = exact_keys(value, {"state", "decision_id", "reason", "requested_transition_id"}, "pending_decision")
    state = item["state"]
    if state not in PENDING_STATES:
        raise StatusError(f"invalid pending decision state: {state}")
    decision_id = require_id(item["decision_id"], "pending_decision.decision_id", nullable=state == "NONE")
    transition_id = require_id(item["requested_transition_id"], "pending_decision.requested_transition_id", nullable=state == "NONE")
    reason = require_text(item["reason"], "pending_decision.reason", maximum=2048)
    if state == "NONE" and (decision_id is not None or transition_id is not None):
        raise StatusError("NONE pending decision must have null IDs")
    if state != "NONE" and (decision_id is None or transition_id is None):
        raise StatusError("pending Supervisor decision requires decision and transition IDs")
    return {"state": state, "decision_id": decision_id, "reason": reason, "requested_transition_id": transition_id}


def validate_next_action(value: Any) -> dict[str, str]:
    item = exact_keys(value, {"transition_id", "description", "effect"}, "next_proposed_action")
    effect = item["effect"]
    if effect not in ACTION_EFFECTS:
        raise StatusError(f"invalid next action effect: {effect}")
    return {
        "transition_id": require_id(item["transition_id"], "next_proposed_action.transition_id"),
        "description": require_text(item["description"], "next_proposed_action.description", maximum=2048),
        "effect": effect,
    }


def validate_metric_availability(value: Any) -> dict[str, str]:
    item = exact_keys(value, {"elapsed", "tokens", "cost", "rework"}, "metric_availability")
    result: dict[str, str] = {}
    for name, state in item.items():
        if state not in {"AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}:
            raise StatusError(f"metric_availability.{name} has invalid state: {state}")
        result[name] = state
    return result


def validate_decision_window(value: Any) -> dict[str, str]:
    required = {
        "owner", "dependency", "last_decision_delta", "predicted_next_event",
        "predicted_window", "wait_cost", "replacement_integration_cost",
        "maximum_harm", "alternate_route",
    }
    item = exact_keys(value, required, "decision_window")
    return {name: require_text(item[name], f"decision_window.{name}", maximum=2048) for name in sorted(required)}


def validate_supervision_context(value: Any) -> dict[str, Any]:
    required = {
        "stage", "action_or_wait_owner", "last_decision_delta", "next_decision_condition",
        "held_transition", "expected_value", "elapsed", "metric_availability",
        "decision_window", "no_push",
    }
    item = exact_keys(value, required, "supervision_context")
    if item["no_push"] is not True:
        raise StatusError("supervision_context.no_push must be true")
    return {
        "stage": require_text(item["stage"], "supervision_context.stage", maximum=1024),
        "action_or_wait_owner": require_text(item["action_or_wait_owner"], "supervision_context.action_or_wait_owner", maximum=1024),
        "last_decision_delta": require_text(item["last_decision_delta"], "supervision_context.last_decision_delta", maximum=2048),
        "next_decision_condition": require_text(item["next_decision_condition"], "supervision_context.next_decision_condition", maximum=2048),
        "held_transition": require_text(item["held_transition"], "supervision_context.held_transition", maximum=1024),
        "expected_value": require_text(item["expected_value"], "supervision_context.expected_value", maximum=2048),
        "elapsed": require_text(item["elapsed"], "supervision_context.elapsed", maximum=1024),
        "metric_availability": validate_metric_availability(item["metric_availability"]),
        "decision_window": validate_decision_window(item["decision_window"]),
        "no_push": True,
    }


def validate_supervisor_message(value: Any, latest: dict[str, Any], worker: dict[str, Any]) -> dict[str, Any]:
    item = exact_keys(
        value,
        {
            "schema_version", "source_role", "source_thread_id", "message_id",
            "destination_worker_task_id", "destination_turn_id", "prompt",
            "prior_status_id", "prior_status_sha256", "requested_transition_id",
            "received_at_utc",
        },
        "supervisor_message",
    )
    if item["schema_version"] != MESSAGE_SCHEMA or item["source_role"] != "SUPERVISOR":
        raise StatusError("unsupported Supervisor message binding")
    if item["prior_status_id"] != latest["status_id"] or item["prior_status_sha256"] != latest["record_sha256"]:
        raise StatusError("Supervisor message does not name the exact prior status")
    held = latest["transition_gate"]["held_transition_id"]
    if item["requested_transition_id"] != held:
        raise StatusError("Supervisor message requested transition differs from held transition")
    supervisor_task = latest["objective"]["supervisor_task_id"]
    if supervisor_task is None or item["source_thread_id"] != supervisor_task:
        raise StatusError("Supervisor message source thread differs from objective binding")
    prior_worker = latest["worker"]
    if prior_worker["task_id"] is None or item["destination_worker_task_id"] != prior_worker["task_id"]:
        raise StatusError("Supervisor message destination differs from Worker task")
    if worker["task_id"] != prior_worker["task_id"] or worker["lease_id"] is None or worker["lease_id"] != prior_worker["lease_id"]:
        raise StatusError("dependent resume is not on the same Worker task and lease")
    if worker["turn_id"] is None or item["destination_turn_id"] != worker["turn_id"]:
        raise StatusError("Supervisor message is not bound to the current Worker turn")
    if prior_worker["turn_id"] is not None and worker["turn_id"] == prior_worker["turn_id"]:
        raise StatusError("dependent resume must occur in a later Worker turn")
    prompt = require_text(item["prompt"], "supervisor_message.prompt", maximum=16384)
    return {
        "source_thread_id": require_id(item["source_thread_id"], "supervisor_message.source_thread_id"),
        "message_id": require_id(item["message_id"], "supervisor_message.message_id"),
        "turn_id": require_id(item["destination_turn_id"], "supervisor_message.destination_turn_id"),
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "prior_status_id": latest["status_id"],
        "prior_status_sha256": latest["record_sha256"],
        "requested_transition_id": require_id(item["requested_transition_id"], "supervisor_message.requested_transition_id"),
        "received_at_utc": require_utc(item["received_at_utc"], "supervisor_message.received_at_utc"),
    }


def validate_message_binding(
    value: Any,
    previous: dict[str, Any] | None,
    worker: dict[str, Any],
    action: dict[str, str],
) -> dict[str, Any] | None:
    if value is None:
        return None
    item = exact_keys(
        value,
        {
            "source_thread_id", "message_id", "turn_id", "prompt_sha256",
            "prior_status_id", "prior_status_sha256", "requested_transition_id",
            "received_at_utc",
        },
        "supervisor_message_binding",
    )
    if previous is None or not previous["transition_gate"]["requires_supervisor_message"]:
        raise StatusError("Supervisor message binding lacks a prior held transition")
    source_thread = require_id(item["source_thread_id"], "supervisor_message_binding.source_thread_id")
    message_id = require_id(item["message_id"], "supervisor_message_binding.message_id")
    turn_id = require_id(item["turn_id"], "supervisor_message_binding.turn_id")
    prompt_sha = require_sha(item["prompt_sha256"], "supervisor_message_binding.prompt_sha256")
    prior_status = require_id(item["prior_status_id"], "supervisor_message_binding.prior_status_id")
    prior_sha = require_sha(item["prior_status_sha256"], "supervisor_message_binding.prior_status_sha256")
    transition = require_id(item["requested_transition_id"], "supervisor_message_binding.requested_transition_id")
    received = require_utc(item["received_at_utc"], "supervisor_message_binding.received_at_utc")
    if prior_status != previous["status_id"] or prior_sha != previous["record_sha256"]:
        raise StatusError("stored Supervisor binding differs from exact prior status")
    if transition != previous["transition_gate"]["held_transition_id"] or transition != action["transition_id"]:
        raise StatusError("stored Supervisor binding differs from held/current transition")
    if source_thread != previous["objective"]["supervisor_task_id"]:
        raise StatusError("stored Supervisor binding differs from objective Supervisor task")
    if worker["turn_id"] is None or turn_id != worker["turn_id"]:
        raise StatusError("stored Supervisor binding differs from current Worker turn")
    return {
        "source_thread_id": source_thread,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_sha256": prompt_sha,
        "prior_status_id": prior_status,
        "prior_status_sha256": prior_sha,
        "requested_transition_id": transition,
        "received_at_utc": received,
    }


def validate_record(record: Any, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    schema_version = record.get("schema_version") if isinstance(record, dict) else None
    required = {
        "schema_version", "status_id", "record_sha256", "sequence", "previous_record_sha256",
        "objective", "worker", "candidate_identity", "evidence_identity", "active_step",
        "actual_evidence_delta", "pending_decision", "next_proposed_action", "allowed_effects",
        "prohibited_effects", "open_deliverables", "state", "transition_gate",
        "supervisor_message_binding", "timestamp_utc", "proof_ceiling",
    }
    if schema_version == STATUS_SCHEMA:
        required.add("supervision_context")
    item = exact_keys(record, required, "status record")
    if item["schema_version"] not in {STATUS_SCHEMA_V1, STATUS_SCHEMA}:
        raise StatusError("unsupported status schema")
    sequence = item["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1 or sequence > SAFE_INTEGER:
        raise StatusError("invalid status sequence")
    require_id(item["status_id"], "status_id")
    require_sha(item["record_sha256"], "record_sha256")
    seed_body = {key: value for key, value in item.items() if key not in {"status_id", "record_sha256"}}
    expected_status_id = f"STATUS-{sequence:020d}-{object_sha256(seed_body)[:24]}"
    if item["status_id"] != expected_status_id:
        raise StatusError("status ID differs from canonical record seed")
    expected_record_hash = object_sha256(record_body(item))
    if item["record_sha256"] != expected_record_hash:
        raise StatusError("status record hash mismatch")
    if item["state"] not in STATES:
        raise StatusError("invalid status state")
    objective = validate_objective(item["objective"])
    worker = validate_worker(item["worker"])
    candidate = validate_identity(item["candidate_identity"], "candidate_identity")
    validate_identity(item["evidence_identity"], "evidence_identity")
    require_text(item["active_step"], "active_step", maximum=2048)
    normalize_evidence_delta(item["actual_evidence_delta"])
    pending = validate_pending(item["pending_decision"])
    if item["schema_version"] == STATUS_SCHEMA:
        validate_supervision_context(item["supervision_context"])
    action = validate_next_action(item["next_proposed_action"])
    normalize_string_set(item["allowed_effects"], "allowed_effects")
    normalize_string_set(item["prohibited_effects"], "prohibited_effects")
    normalize_deliverables(item["open_deliverables"])
    gate = exact_keys(item["transition_gate"], {"requires_supervisor_message", "held_transition_id", "scope"}, "transition_gate")
    expected_requires = pending["state"] != "NONE"
    if gate["requires_supervisor_message"] is not expected_requires:
        raise StatusError("transition gate disagrees with pending decision")
    expected_held = pending["requested_transition_id"] if expected_requires else None
    if gate["held_transition_id"] != expected_held or gate["scope"] != "DEPENDENT_TRANSITION_ONLY":
        raise StatusError("invalid transition gate binding")
    if item["state"] == "CONTINUE" and pending["state"] != "NONE":
        raise StatusError("CONTINUE may not retain a pending Supervisor decision")
    if item["state"] in {"READY", "WORK_NEEDS_ATTENTION"} and pending["state"] == "NONE":
        raise StatusError("READY/WORK_NEEDS_ATTENTION requires an exact pending decision")
    if action["transition_id"] != (pending["requested_transition_id"] or action["transition_id"]):
        raise StatusError("next action differs from pending transition")
    binding = validate_message_binding(item["supervisor_message_binding"], previous, worker, action)
    require_utc(item["timestamp_utc"], "timestamp_utc")
    require_text(item["proof_ceiling"], "proof_ceiling", maximum=2048)
    if previous is None:
        if sequence != 1 or item["previous_record_sha256"] is not None:
            raise StatusError("initial status must start at sequence 1 with null previous hash")
    else:
        if sequence != previous["sequence"] + 1 or item["previous_record_sha256"] != previous["record_sha256"]:
            raise StatusError("status chain sequence or previous hash mismatch")
        if objective != previous["objective"]:
            raise StatusError("objective identity/source changed within status lineage")
        if previous["worker"]["task_id"] is not None and worker["task_id"] != previous["worker"]["task_id"]:
            raise StatusError("Worker task changed within status lineage")
        if previous["worker"]["lease_id"] is not None and worker["lease_id"] != previous["worker"]["lease_id"]:
            raise StatusError("Worker lease changed within status lineage")
        preserve_deliverables(previous, item["open_deliverables"])
        previous_held = previous["transition_gate"]["requires_supervisor_message"]
        if binding is not None:
            if item["state"] != "CONTINUE" or pending["state"] != "NONE":
                raise StatusError("consumed dependent transition must continue with no pending decision")
            if candidate != previous["candidate_identity"]:
                raise StatusError("candidate identity changed while consuming held transition")
            if action != previous["next_proposed_action"]:
                raise StatusError("consumed dependent transition differs from held action")
            if item["allowed_effects"] != previous["allowed_effects"] or item["prohibited_effects"] != previous["prohibited_effects"]:
                raise StatusError("consumed dependent transition changed effect authority")
        elif previous_held:
            if item["state"] not in {"READY", "WORK_NEEDS_ATTENTION"}:
                raise StatusError("unconsumed held transition must remain locally terminal")
            if pending != previous["pending_decision"] or action != previous["next_proposed_action"]:
                raise StatusError("status-only update rewrote held decision or action")
            if item["allowed_effects"] != previous["allowed_effects"] or item["prohibited_effects"] != previous["prohibited_effects"]:
                raise StatusError("status-only update changed effect authority while held")
            if candidate != previous["candidate_identity"]:
                raise StatusError("candidate identity changed while transition remained held")
    return item


def status_paths(worker_root: Path, *, create: bool) -> tuple[Path, Path, Path, Path]:
    root = worker_root.resolve(strict=True)
    if not root.is_dir():
        raise StatusError("worker root must be an existing directory")
    status_dir = root / ".oif-supervision-status"
    if create:
        status_dir.mkdir(parents=False, exist_ok=True)
    if status_dir.exists():
        if not status_dir.is_dir():
            raise StatusError("status path exists but is not a directory")
        resolved_status = status_dir.resolve(strict=True)
    else:
        resolved_status = status_dir
    if os.path.commonpath([str(root), str(resolved_status)]) != str(root):
        raise StatusError("status directory escapes Worker root")
    journal = resolved_status / "journal"
    if create:
        journal.mkdir(parents=False, exist_ok=True)
    if journal.exists() and not journal.is_dir():
        raise StatusError("journal path exists but is not a directory")
    resolved_journal = journal.resolve(strict=True) if journal.exists() else journal
    if os.path.commonpath([str(root), str(resolved_journal)]) != str(root):
        raise StatusError("journal directory escapes Worker root")
    return root, resolved_status / "status.json", resolved_journal, resolved_status / "status.lock"


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_create(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_journal(journal: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    files = sorted(journal.glob("event-*.json"))
    if len(files) > MAX_JOURNAL_RECORDS:
        raise StatusError(f"journal exceeds the append-only limit of {MAX_JOURNAL_RECORDS} records")
    for file in files:
        record = load_json(file, canonical_required=True)
        validate_record(record, previous)
        expected_name = f"event-{record['sequence']:020d}-{record['status_id']}.json"
        if file.name != expected_name:
            raise StatusError(f"journal filename does not match record identity: {file}")
        records.append(record)
        previous = record
    return records


def reconcile_current(status_path: Path, records: list[dict[str, Any]], *, repair: bool) -> str:
    if not records:
        if status_path.exists():
            raise StatusError("current status exists without journal authority")
        return "EMPTY"
    latest = records[-1]
    latest_bytes = canonical_bytes(latest)
    if not status_path.exists():
        if not repair:
            return "CURRENT_MISSING"
        atomic_write(status_path, latest_bytes)
        return "CURRENT_REPAIRED_FROM_JOURNAL"
    try:
        current = load_json(status_path, canonical_required=True)
    except (OSError, StatusError):
        if not repair:
            return "CURRENT_INVALID"
        atomic_write(status_path, latest_bytes)
        return "CURRENT_REPAIRED_FROM_JOURNAL"
    sequence = current.get("sequence") if isinstance(current, dict) else None
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        if not repair:
            return "CURRENT_INVALID"
        atomic_write(status_path, latest_bytes)
        return "CURRENT_REPAIRED_FROM_JOURNAL"
    if sequence > latest["sequence"]:
        raise StatusError("current status is ahead of journal authority; refusing destructive repair")
    predecessor = records[sequence - 2] if sequence > 1 else None
    try:
        validate_record(current, predecessor)
    except StatusError:
        if not repair:
            return "CURRENT_INVALID"
        atomic_write(status_path, latest_bytes)
        return "CURRENT_REPAIRED_FROM_JOURNAL"
    authoritative_at_sequence = records[sequence - 1]
    if canonical_bytes(current) != canonical_bytes(authoritative_at_sequence):
        raise StatusError("current status conflicts with journal authority; refusing destructive repair")
    if sequence < latest["sequence"]:
        if not repair:
            return "CURRENT_STALE"
        atomic_write(status_path, latest_bytes)
        return "CURRENT_REPAIRED_FROM_JOURNAL"
    if canonical_bytes(current) != latest_bytes:
        raise StatusError("current status conflicts with latest journal authority")
    return "CURRENT_MATCHES_JOURNAL"


def preserve_deliverables(previous: dict[str, Any], current: list[dict[str, str]]) -> None:
    old = {item["deliverable_id"]: item for item in previous["open_deliverables"]}
    new = {item["deliverable_id"]: item for item in current}
    missing = sorted(set(old) - set(new))
    if missing:
        raise StatusError(f"open deliverable lineage dropped: {missing}")
    for deliverable_id, prior in old.items():
        if prior["state"] == "SATISFIED" and new[deliverable_id]["state"] != "SATISFIED":
            raise StatusError(f"satisfied deliverable reverted: {deliverable_id}")


def build_record(request: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    request_schema = request.get("schema_version") if isinstance(request, dict) else None
    required = {
        "schema_version", "expected_previous_status_id", "expected_previous_record_sha256",
        "objective", "worker", "candidate_identity", "evidence_identity", "active_step",
        "actual_evidence_delta", "pending_decision", "next_proposed_action", "allowed_effects",
        "prohibited_effects", "open_deliverables", "state", "transition_effect",
        "supervisor_message", "proof_ceiling",
    }
    if request_schema == REQUEST_SCHEMA:
        required.add("supervision_context")
    raw = exact_keys(request, required, "write request")
    if raw["schema_version"] not in {REQUEST_SCHEMA_V1, REQUEST_SCHEMA}:
        raise StatusError("unsupported write request schema")
    objective = validate_objective(raw["objective"])
    worker = validate_worker(raw["worker"])
    candidate = validate_identity(raw["candidate_identity"], "candidate_identity")
    evidence = validate_identity(raw["evidence_identity"], "evidence_identity")
    pending = validate_pending(raw["pending_decision"])
    next_action = validate_next_action(raw["next_proposed_action"])
    allowed = normalize_string_set(raw["allowed_effects"], "allowed_effects")
    prohibited = normalize_string_set(raw["prohibited_effects"], "prohibited_effects")
    deliverables = normalize_deliverables(raw["open_deliverables"])
    state = raw["state"]
    effect = raw["transition_effect"]
    if state not in STATES or effect not in TRANSITION_EFFECTS:
        raise StatusError("invalid state or transition effect")
    message_binding: dict[str, Any] | None = None
    if latest is None:
        if raw["expected_previous_status_id"] is not None or raw["expected_previous_record_sha256"] is not None:
            raise StatusError("initial request must expect no prior status")
        if effect == "DEPENDENT_TRANSITION" or raw["supervisor_message"] is not None:
            raise StatusError("initial status cannot consume a Supervisor transition")
        sequence = 1
        previous_hash = None
    else:
        if latest["sequence"] >= MAX_JOURNAL_RECORDS:
            raise StatusError(f"journal reached its append-only limit of {MAX_JOURNAL_RECORDS} records")
        if raw["expected_previous_status_id"] != latest["status_id"] or raw["expected_previous_record_sha256"] != latest["record_sha256"]:
            raise StatusError("write request does not CAS the exact latest status")
        if objective != latest["objective"]:
            raise StatusError("objective identity/source changed within status lineage")
        if latest["worker"]["task_id"] is not None and worker["task_id"] != latest["worker"]["task_id"]:
            raise StatusError("Worker task changed within status lineage")
        if latest["worker"]["lease_id"] is not None and worker["lease_id"] != latest["worker"]["lease_id"]:
            raise StatusError("Worker lease changed within status lineage")
        preserve_deliverables(latest, deliverables)
        needs_message = latest["transition_gate"]["requires_supervisor_message"]
        if effect == "DEPENDENT_TRANSITION":
            if not needs_message or raw["supervisor_message"] is None:
                raise StatusError("dependent transition lacks a pending exact Supervisor message")
            message_binding = validate_supervisor_message(raw["supervisor_message"], latest, worker)
            if next_action["transition_id"] != message_binding["requested_transition_id"]:
                raise StatusError("next action differs from Supervisor-requested transition")
            if pending["state"] != "NONE" or state != "CONTINUE":
                raise StatusError("consumed dependent transition must write CONTINUE with no pending decision")
            if candidate != latest["candidate_identity"] or next_action != latest["next_proposed_action"]:
                raise StatusError("consumed dependent transition differs from held candidate or action")
            if allowed != latest["allowed_effects"] or prohibited != latest["prohibited_effects"]:
                raise StatusError("consumed dependent transition may not change effect authority")
        else:
            if raw["supervisor_message"] is not None:
                raise StatusError("Supervisor message may be consumed only by DEPENDENT_TRANSITION")
            if needs_message:
                if state not in {"READY", "WORK_NEEDS_ATTENTION"}:
                    raise StatusError("unconsumed held transition must remain READY or WORK_NEEDS_ATTENTION")
                if pending != latest["pending_decision"] or next_action != latest["next_proposed_action"]:
                    raise StatusError("status-only update may not rewrite the held decision or action")
                if allowed != latest["allowed_effects"] or prohibited != latest["prohibited_effects"]:
                    raise StatusError("status-only update may not change effect authority while held")
                if candidate != latest["candidate_identity"]:
                    raise StatusError("status-only update may not change candidate identity while held")
        sequence = latest["sequence"] + 1
        previous_hash = latest["record_sha256"]
    requires_message = pending["state"] != "NONE"
    record: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA if raw["schema_version"] == REQUEST_SCHEMA else STATUS_SCHEMA_V1,
        "sequence": sequence,
        "previous_record_sha256": previous_hash,
        "objective": objective,
        "worker": worker,
        "candidate_identity": candidate,
        "evidence_identity": evidence,
        "active_step": require_text(raw["active_step"], "active_step", maximum=2048),
        "actual_evidence_delta": normalize_evidence_delta(raw["actual_evidence_delta"]),
        "pending_decision": pending,
        "next_proposed_action": next_action,
        "allowed_effects": allowed,
        "prohibited_effects": prohibited,
        "open_deliverables": deliverables,
        "state": state,
        "transition_gate": {
            "requires_supervisor_message": requires_message,
            "held_transition_id": pending["requested_transition_id"] if requires_message else None,
            "scope": "DEPENDENT_TRANSITION_ONLY",
        },
        "supervisor_message_binding": message_binding,
        "timestamp_utc": utc_now(),
        "proof_ceiling": require_text(raw["proof_ceiling"], "proof_ceiling", maximum=2048),
    }
    if raw["schema_version"] == REQUEST_SCHEMA:
        record["supervision_context"] = validate_supervision_context(raw["supervision_context"])
    seed = object_sha256(record)
    record["status_id"] = f"STATUS-{sequence:020d}-{seed[:24]}"
    record["record_sha256"] = object_sha256(record_body(record))
    validate_record(record, latest)
    return record


def preflight_write_request(worker_root: Path, request_path: Path) -> dict[str, Any]:
    """Validate a write against a read-only lineage before creating status artifacts."""
    _, status_path, journal, _ = status_paths(worker_root, create=False)
    request = load_json(request_path)
    records = read_journal(journal)
    reconcile_current(status_path, records, repair=False)
    latest = records[-1] if records else None
    build_record(request, latest)
    return request


def preflight_status(worker_root: Path, request_path: Path) -> dict[str, Any]:
    """Validate an exact write request without creating, locking, repairing, or writing."""
    request = preflight_write_request(worker_root, request_path)
    return {
        "state": "VALID",
        "expected_previous_status_id": request["expected_previous_status_id"],
        "expected_previous_record_sha256": request["expected_previous_record_sha256"],
        "requested_transition_id": request["next_proposed_action"]["transition_id"],
        "next_effect": request["next_proposed_action"]["effect"],
        "proof_ceiling": "read-only request/lineage validation only; no status write, action authority, or later write outcome",
    }


def read_status(worker_root: Path) -> dict[str, Any]:
    """Read the validated journal tail and projection state without repair."""
    _, status_path, journal, _ = status_paths(worker_root, create=False)
    records = read_journal(journal)
    projection = reconcile_current(status_path, records, repair=False)
    latest = records[-1] if records else None
    return {
        "state": "VALID" if projection in {"EMPTY", "CURRENT_MATCHES_JOURNAL"} else "NEEDS_PROJECTION_RECOVERY",
        "journal_records": len(records),
        "projection": projection,
        "record": latest,
        "proof_ceiling": "read-only validated local journal/current projection only; app projection and Supervisor provenance unproven",
    }


def write_status(worker_root: Path, request_path: Path) -> dict[str, Any]:
    request = preflight_write_request(worker_root, request_path)
    _, status_path, journal, lock_path = status_paths(worker_root, create=True)
    with file_lock(lock_path):
        records = read_journal(journal)
        latest = records[-1] if records else None
        record = build_record(request, latest)
        recovery = reconcile_current(status_path, records, repair=True)
        event_path = journal / f"event-{record['sequence']:020d}-{record['status_id']}.json"
        if event_path.exists():
            raise StatusError("journal event identity already exists")
        data = canonical_bytes(record)
        atomic_create(event_path, data)
        atomic_write(status_path, data)
        return {
            "state": record["state"],
            "status_id": record["status_id"],
            "record_sha256": record["record_sha256"],
            "sequence": record["sequence"],
            "status_path": str(status_path),
            "journal_event_path": str(event_path),
            "recovery": recovery,
            "next_effect": record["next_proposed_action"]["effect"],
            "requires_supervisor_message": record["transition_gate"]["requires_supervisor_message"],
            "proof_ceiling": "durable local status identity and chain update only; host message provenance and action authority unproven",
        }


def verify_status(worker_root: Path) -> dict[str, Any]:
    _, status_path, journal, _ = status_paths(worker_root, create=False)
    records = read_journal(journal)
    projection = reconcile_current(status_path, records, repair=False)
    latest = records[-1] if records else None
    return {
        "state": "VALID" if projection in {"EMPTY", "CURRENT_MATCHES_JOURNAL"} else "NEEDS_PROJECTION_RECOVERY",
        "journal_records": len(records),
        "projection": projection,
        "latest_status_id": latest["status_id"] if latest else None,
        "latest_record_sha256": latest["record_sha256"] if latest else None,
        "latest_sequence": latest["sequence"] if latest else 0,
        "latest_worker_task_id": latest["worker"]["task_id"] if latest else None,
        "latest_worker_turn_id": latest["worker"]["turn_id"] if latest else None,
        "latest_state": latest["state"] if latest else None,
        "held_transition_id": latest["transition_gate"]["held_transition_id"] if latest else None,
        "proof_ceiling": "read-only local journal/current consistency only; app projection and Supervisor provenance unproven",
    }


def select_status_fallback(evidence_path: Path) -> dict[str, Any]:
    raw = load_json(evidence_path)
    item = exact_keys(raw, {"schema_version", "projection", "decision_boundary", "proof_ceiling"}, "projection evidence")
    if item["schema_version"] != SELECTION_SCHEMA:
        raise StatusError("unsupported projection-evidence schema")
    projection = exact_keys(
        item["projection"],
        {"source_kind", "thread_id", "turn_id", "thread_status", "turn_status", "items_count", "updated_at_utc", "observed_at_utc", "last_decision_delta_at_utc", "contradiction_facts", "expected_result_markers", "raw_store_visibility", "cursor_state", "intentional_no_output"},
        "projection",
    )
    if projection["source_kind"] not in {"provider-read-task", "provider-wait-task", "local-runtime"}:
        raise StatusError("projection.source_kind is unsupported")
    require_id(projection["thread_id"], "projection.thread_id")
    require_id(projection["turn_id"], "projection.turn_id", nullable=True)
    for field in ("thread_status", "turn_status"):
        require_text(projection[field], f"projection.{field}", maximum=128)
    if not isinstance(projection["items_count"], int) or isinstance(projection["items_count"], bool) or projection["items_count"] < 0:
        raise StatusError("projection.items_count must be a nonnegative integer")
    updated_at = require_utc(projection["updated_at_utc"], "projection.updated_at_utc")
    require_utc(projection["observed_at_utc"], "projection.observed_at_utc")
    last_delta = projection["last_decision_delta_at_utc"]
    if last_delta is not None:
        require_utc(last_delta, "projection.last_decision_delta_at_utc")
    contradiction_facts = projection["contradiction_facts"]
    if not isinstance(contradiction_facts, list):
        raise StatusError("projection.contradiction_facts must be an array")
    for index, fact in enumerate(contradiction_facts):
        require_text(fact, f"projection.contradiction_facts[{index}]", maximum=1024)
    for field in ("expected_result_markers", "raw_store_visibility"):
        values = projection[field]
        if not isinstance(values, list):
            raise StatusError(f"projection.{field} must be an array")
        for index, fact in enumerate(values):
            require_text(fact, f"projection.{field}[{index}]", maximum=1024)
    if projection["cursor_state"] not in {"fresh", "stale", "advanced", "unknown"}:
        raise StatusError("projection.cursor_state is invalid")
    if not isinstance(projection["intentional_no_output"], bool):
        raise StatusError("projection.intentional_no_output must be boolean")
    boundary = exact_keys(
        item["decision_boundary"],
        {"decision_bearing", "owner", "next_decision_condition", "maximum_harm", "alternate_route", "decision_window_ms"},
        "decision_boundary",
    )
    if not isinstance(boundary["decision_bearing"], bool):
        raise StatusError("decision_boundary.decision_bearing must be boolean")
    for field in ("owner", "next_decision_condition", "maximum_harm", "alternate_route"):
        require_text(boundary[field], f"decision_boundary.{field}", maximum=2048)
    if not isinstance(boundary["decision_window_ms"], int) or isinstance(boundary["decision_window_ms"], bool) or boundary["decision_window_ms"] <= 0:
        raise StatusError("decision_boundary.decision_window_ms must be a positive integer")
    observed = datetime.fromisoformat(projection["observed_at_utc"][:-1] + "+00:00")
    latest = max(datetime.fromisoformat(updated_at[:-1] + "+00:00"), datetime.fromisoformat(last_delta[:-1] + "+00:00") if last_delta else datetime.min.replace(tzinfo=timezone.utc))
    elapsed_ms = int((observed - latest).total_seconds() * 1000)
    active_empty = projection["turn_status"].lower() in {"inprogress", "running", "active"} and projection["items_count"] == 0
    terminal_empty = projection["turn_status"].lower() in {"completed", "complete", "terminal", "failed", "cancelled"} and projection["items_count"] == 0 and bool(projection["expected_result_markers"] or projection["raw_store_visibility"])
    contradictory = bool(contradiction_facts or projection["raw_store_visibility"])
    stale = elapsed_ms > boundary["decision_window_ms"] or projection["cursor_state"] == "stale"
    anomaly = (active_empty and (contradictory or stale)) or terminal_empty or contradictory or stale
    if projection["intentional_no_output"] and not contradictory and not stale:
        anomaly = False
    decision = "SELECT" if anomaly and boundary["decision_bearing"] else "NO_SELECT"
    return {
        "schema_version": "durable-supervisor-selection-v1",
        "decision": decision,
        "trigger_families": [name.upper() for name, active in (("active-empty", active_empty and (contradictory or stale)), ("terminal-empty", terminal_empty), ("contradictory", contradictory), ("stale", stale)) if active],
        "derived_elapsed_ms": elapsed_ms,
        "owner": boundary["owner"],
        "next_decision_condition": boundary["next_decision_condition"],
        "proof_ceiling": "mechanical selection from caller-supplied projection/action evidence only; app truth, semantic need, action authority and outcome remain unproven",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight", help="validate one exact write request and lineage without creating, repairing, locking, or writing")
    preflight_parser.add_argument("--worker-root", required=True)
    preflight_parser.add_argument("--request", required=True)
    read_parser = subparsers.add_parser("read", help="read the validated journal tail and current projection state without repair")
    read_parser.add_argument("--worker-root", required=True)
    write_parser = subparsers.add_parser("write", help="append one decision-bearing status and replace current projection atomically")
    write_parser.add_argument("--worker-root", required=True)
    write_parser.add_argument("--request", required=True)
    verify_parser = subparsers.add_parser("verify", help="validate journal chain and current projection without repair")
    verify_parser.add_argument("--worker-root", required=True)
    select_parser = subparsers.add_parser("select", help="mechanically select the fallback from bounded projection/action evidence")
    select_parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = preflight_status(Path(args.worker_root), Path(args.request))
        elif args.command == "read":
            result = read_status(Path(args.worker_root))
        elif args.command == "write":
            result = write_status(Path(args.worker_root), Path(args.request))
        elif args.command == "verify":
            result = verify_status(Path(args.worker_root))
        else:
            result = select_status_fallback(Path(args.evidence))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        if result.get("state") == "NEEDS_PROJECTION_RECOVERY":
            return 3
        return 0
    except (OSError, StatusError) as exc:
        print(json.dumps({"state": "HELD", "error": str(exc)}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
