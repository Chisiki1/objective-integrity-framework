#!/usr/bin/env python3
"""Durable, inactive candidate for one logical chat's primary-objective ledger.

The journal is authoritative. ``objective.txt`` is the atomic, user-facing current projection.  Hooks
capture facts only; they never derive objective semantics from prompt words.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import tempfile
import time
from typing import Any, Iterable


SCHEMA = "chat-objective-continuity-journal-v1"
CONFIG_SCHEMA = "chat-objective-continuity-config-v1"
PROJECTION_SCHEMA = "chat-objective-continuity-projection-v1"
ZERO_HASH = "0" * 64
MAX_DEFAULT_INPUT = 1_048_576
MAX_DEFAULT_PROMPT = 262_144
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
OBJECTIVE_KEYS = {
    "primary_objective",
    "clauses",
    "contract",
    "mandatory_acceptance",
    "authority",
    "scope",
    "prohibited_substitutes",
}
PROGRESS_KEYS = {
    "contract_id",
    "outcome_updates",
    "next_eligible_work",
    "blockers",
    "return_step",
    "evidence_refs",
    "proof_ceiling",
    "notes",
    "objective_evidence_delta",
}
OUTCOME_UPDATE_KEYS = {
    "outcome_id",
    "status",
    "evidence_refs",
    "blocker",
    "return_step",
    "note",
    "proof_ceiling",
}
OUTCOME_TRANSITION_KEYS = {"outcome_id", "operation", "from_status", "source_clause_ids", "note"}
SOURCE_DISPOSITION_KEYS = {
    "source_event_id",
    "disposition",
    "review_ground",
    "incorporated_contract_id",
    "incorporated_clause_ids",
}
ACTION_START_KEYS = {
    "action_id",
    "description",
    "outcome_ids",
    "contract_id",
    "source_clause_ids",
    "action_input_sha256",
    "retry_of",
    "proof_ceiling",
    "notes",
}
ACTION_OUTCOME_KEYS = {
    "action_id",
    "status",
    "effect_state",
    "first_fault",
    "contributing_conditions",
    "propagation_symptoms",
    "cleanup_failures",
    "affected_consumers",
    "unaffected_consumers",
    "unobserved_scope",
    "evidence_refs",
    "result_ref",
    "notes",
}
ACTION_RECONCILE_KEYS = {
    "action_id",
    "resolution",
    "evidence_refs",
    "prior_reconciliation_event_id",
    "first_fault_recovery",
    "notes",
}
NORMATIVE_CONTRACT_FIELDS = {
    "primary_objective",
    "mandatory_acceptance",
    "acceptance_criteria",
    "authority",
    "scope",
    "constraints",
    "permissions",
    "prohibited_substitutes",
    "preservation_contracts",
    "evidence_requirements",
    "stopping_conditions",
    "independent_deliverables",
    "methods",
    "preferences",
    "proof_ceiling",
}
CONTRACT_KEYS = NORMATIVE_CONTRACT_FIELDS | {
    "contract_id",
    "parent_contract_id",
    "replaces_contract_id",
    "withdraws_contract_id",
    "clauses",
    "open_outcomes",
    "outcome_transitions",
}
CLASSIFICATION_KEYS = {
    "source_event_id",
    "disposition",
    "classification_note",
    "contract",
    "supersedes_clause_ids",
    "normative_transitions",
}
NORMATIVE_TRANSITION_KEYS = {"field", "operation", "source_clause_ids", "note"}
CAPTURE_GAP_RESOLUTION_KEYS = {
    "gap_id",
    "resolution",
    "review_ground",
    "source_event_id",
}


class LedgerError(RuntimeError):
    pass


class CasMismatch(LedgerError):
    pass


class InputLimitError(LedgerError):
    def __init__(self, message: str, observed_prefix: bytes, limit: int) -> None:
        super().__init__(message)
        self.observed_prefix = observed_prefix
        self.limit = limit


class PromptLimitError(LedgerError):
    def __init__(self, message: str, observed_sha256: str, observed_bytes: int, limit: int) -> None:
        super().__init__(message)
        self.observed_sha256 = observed_sha256
        self.observed_bytes = observed_bytes
        self.limit = limit


class PostCommitError(LedgerError):
    def __init__(
        self,
        message: str,
        event_id: str,
        head_hash: str,
        effect: str,
        cleanup_fault: str | None = None,
    ) -> None:
        super().__init__(message)
        self.primary_message = message
        self.event_id = event_id
        self.head_hash = head_hash
        self.effect = effect
        self.cleanup_fault = cleanup_fault

    def __str__(self) -> str:
        if self.cleanup_fault:
            return f"{self.primary_message}; cleanup fault: {self.cleanup_fault}"
        return self.primary_message


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    # Hook stdout can inherit a non-UTF-8 Windows code page. ASCII JSON keeps
    # transport lossless for every Unicode scalar while exact source bytes stay
    # in their immutable UTF-8 files.
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def human_json(value: Any) -> str:
    """Deterministic JSON for the UTF-8 human projection only.

    JSON escaping still preserves quotes, backslashes and controls.  Normal
    Unicode scalars remain readable, while surrogate code units are escaped so
    a previously accepted non-scalar value cannot make projection encoding
    fail.  Protocol, hashing and machine-state callers keep ``canonical_json``.
    """
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in rendered
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be a JSON object")
    return value


def require_safe_id(value: Any, label: str) -> str:
    text = str(value or "")
    if not SAFE_ID.fullmatch(text):
        raise LedgerError(f"{label} is missing or unsafe")
    return text


def load_json_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read JSON {path}: {exc}") from exc
    return require_object(value, str(path))


def expand_path(value: str, base: pathlib.Path) -> pathlib.Path:
    expanded = os.path.expanduser(os.path.expandvars(value))
    path = pathlib.Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def load_config(path: pathlib.Path) -> dict[str, Any]:
    config = load_json_file(path)
    if config.get("schema") != CONFIG_SCHEMA:
        raise LedgerError(f"config schema must be {CONFIG_SCHEMA}")
    namespace = require_safe_id(config.get("namespace"), "namespace")
    data_root = config.get("data_root")
    if not isinstance(data_root, str) or not data_root:
        raise LedgerError("data_root is required")
    config = dict(config)
    config["namespace"] = namespace
    config["data_root_path"] = expand_path(data_root, path.parent)
    bindings = config.get("session_bindings", {})
    if not isinstance(bindings, dict):
        raise LedgerError("session_bindings must be an object")
    for session_id, binding in bindings.items():
        require_safe_id(session_id, "session binding id")
        binding = require_object(binding, "session binding")
        if binding.get("role") not in {"root", "subagent"}:
            raise LedgerError(f"invalid role for session binding {session_id}")
        if binding.get("logical_chat_id") is not None:
            require_safe_id(binding["logical_chat_id"], "binding logical_chat_id")
    if config.get("implicit_session_ledgers", True) is not True:
        raise LedgerError("implicit_session_ledgers must be true for the common per-chat configuration")
    tool_classes = config.get("tool_classes", {})
    if not isinstance(tool_classes, dict):
        raise LedgerError("tool_classes must be an object")
    for name, classification in tool_classes.items():
        require_safe_id(name, "tool class name")
        if classification not in {"read_only", "mutating", "mixed", "unknown"}:
            raise LedgerError(f"invalid tool class for {name}")
    return config


def select_ledger_config(config: dict[str, Any], logical_chat_id: str) -> dict[str, Any]:
    selected = dict(config)
    selected["logical_chat_id"] = require_safe_id(logical_chat_id, "logical_chat_id")
    return selected


def ledger_key(config: dict[str, Any]) -> str:
    raw = f"{config['namespace']}\0{config['logical_chat_id']}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()[:40]


def ledger_dir(config: dict[str, Any]) -> pathlib.Path:
    return pathlib.Path(config["data_root_path"]) / config["namespace"] / ledger_key(config)


def identity_document(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "chat-objective-continuity-identity-v1",
        "namespace": config["namespace"],
        "logical_chat_id": config["logical_chat_id"],
        "ledger_key": ledger_key(config),
    }


def fsync_directory(path: pathlib.Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def create_exact(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".new", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, path)
            fsync_directory(path.parent)
        except FileExistsError:
            if path.read_bytes() != data:
                raise LedgerError(f"immutable file collision: {path}")
    except BaseException:
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            pathlib.Path(temp_name).unlink()


def owner_alive(pid: Any) -> bool | None:
    """Read-only PID liveness; Windows never uses os.kill, even signal zero."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


class FileLock:
    def __init__(
        self,
        path: pathlib.Path,
        timeout: float = 10.0,
        owner_fields: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.timeout = timeout
        fields = dict(owner_fields or {})
        fields.update(
            {
                "schema": "chat-objective-continuity-lock-v1",
                "pid": os.getpid(),
                "token": secrets.token_hex(16),
                "created_utc": utc_now(),
            }
        )
        self.raw = (canonical_json(fields) + "\n").encode("ascii")
        self.effect_identity: dict[str, str] | None = None

    def note_effect(self, event_id: str, head_hash: str, effect: str) -> None:
        self.effect_identity = {
            "event_id": event_id,
            "head_hash": head_hash,
            "effect": effect,
        }

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            fd, temp_name = tempfile.mkstemp(prefix=".lock-owner-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temp_name, self.path)
                fsync_directory(self.path.parent)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise LedgerError(f"lock timeout: {self.path}")
                time.sleep(0.025)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    pathlib.Path(temp_name).unlink()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        cleanup_fault: str | None = None
        try:
            current = self.path.read_bytes()
        except FileNotFoundError:
            cleanup_fault = "owned ledger lock disappeared before release"
        except OSError as cleanup_exc:
            cleanup_fault = f"cannot read owned ledger lock before release: {cleanup_exc}"
        else:
            if current != self.raw:
                cleanup_fault = "ledger lock identity changed before release"
            else:
                try:
                    self.path.unlink()
                    fsync_directory(self.path.parent)
                except OSError as cleanup_exc:
                    cleanup_fault = f"cannot remove/fsync owned ledger lock: {cleanup_exc}"
        if cleanup_fault is None:
            return
        if isinstance(exc, PostCommitError):
            exc.cleanup_fault = cleanup_fault
            return
        if self.effect_identity is not None:
            raise PostCommitError(
                "journal effect completed or became uncertain before lock release",
                self.effect_identity["event_id"],
                self.effect_identity["head_hash"],
                self.effect_identity["effect"],
                cleanup_fault,
            ) from exc
        if exc is not None:
            raise LedgerError(f"primary fault: {exc}; cleanup fault: {cleanup_fault}") from exc
        raise LedgerError(cleanup_fault)


def ensure_identity(config: dict[str, Any]) -> pathlib.Path:
    root = ledger_dir(config)
    root.mkdir(parents=True, exist_ok=True)
    expected = (canonical_json(identity_document(config)) + "\n").encode("utf-8")
    path = root / "identity.json"
    create_exact(path, expected)
    actual = load_json_file(path)
    if actual != identity_document(config):
        raise LedgerError("ledger identity mismatch")
    return root


def journal_path(config: dict[str, Any]) -> pathlib.Path:
    return ledger_dir(config) / "journal.jsonl"


def event_hash(record_without_hash: dict[str, Any]) -> str:
    return sha256_json(record_without_hash)


def preserve_partial(root: pathlib.Path, tail: bytes) -> pathlib.Path:
    digest = sha256_bytes(tail)
    path = root / "recovery" / f"partial-{digest}.bin"
    create_exact(path, tail)
    return path


def scan_journal(config: dict[str, Any], repair_partial: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    path = journal_path(config)
    if not path.exists():
        return [], []
    raw = path.read_bytes()
    records: list[dict[str, Any]] = []
    notes: list[str] = []
    offset = 0
    valid_offset = 0
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        complete = line.endswith(b"\n")
        body = line[:-1] if complete else line
        try:
            if not complete:
                raise json.JSONDecodeError("partial final line", "", 0)
            record = json.loads(body.decode("utf-8"))
            require_object(record, f"journal line {index + 1}")
        except (UnicodeDecodeError, json.JSONDecodeError, LedgerError) as exc:
            if is_last and repair_partial and not complete:
                tail = raw[offset:]
                saved = preserve_partial(ledger_dir(config), tail)
                with path.open("r+b") as stream:
                    stream.truncate(valid_offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                notes.append(f"partial final bytes preserved at {saved}")
                break
            raise LedgerError(f"journal parse failure at line {index + 1}: {exc}") from exc
        if record.get("schema") != SCHEMA:
            raise LedgerError(f"journal schema failure at line {index + 1}")
        if record.get("seq") != index + 1:
            raise LedgerError(f"journal sequence failure at line {index + 1}")
        expected_prev = records[-1]["event_hash"] if records else ZERO_HASH
        if record.get("prev_hash") != expected_prev:
            raise LedgerError(f"journal previous-hash failure at line {index + 1}")
        without_hash = dict(record)
        recorded_hash = without_hash.pop("event_hash", None)
        if recorded_hash != event_hash(without_hash):
            raise LedgerError(f"journal event-hash failure at line {index + 1}")
        records.append(record)
        offset += len(line)
        valid_offset = offset
    return records, notes


def append_event(
    config: dict[str, Any],
    event_type: str,
    event_id: str,
    payload: dict[str, Any],
    actor_role: str,
    expected_head: str | None = None,
) -> dict[str, Any]:
    require_safe_id(event_type, "event_type")
    require_safe_id(event_id, "event_id")
    root = ensure_identity(config)
    attempt = {
        "event_type": event_type,
        "event_id": event_id,
        "actor_role": actor_role,
        "payload": payload,
        "expected_head": expected_head.upper() if expected_head else None,
    }
    owner_fields = {
        "namespace": config["namespace"],
        "logical_chat_id": config["logical_chat_id"],
        "operation": "append_event",
        "attempt_sha256": sha256_json(attempt),
    }
    with FileLock(
        root / "ledger.lock",
        float(config.get("lock_timeout_seconds", 10)),
        owner_fields,
    ) as ledger_lock:
        records, notes = scan_journal(config, repair_partial=True)
        head = records[-1]["event_hash"] if records else ZERO_HASH
        try:
            state = attach_runtime_frontiers(config, replay(config, records))
            if expected_head is not None and expected_head.upper() != head:
                raise CasMismatch(f"expected head {expected_head.upper()} but found {head}")
            normalized_payload = normalize_proposed_transition(config, state, event_type, payload)
            for existing in records:
                if existing["event_id"] == event_id:
                    comparable = {
                        "event_type": event_type,
                        "actor_role": actor_role,
                        "payload": normalized_payload,
                    }
                    prior = {key: existing[key] for key in comparable}
                    if prior != comparable:
                        raise LedgerError(f"event id collision: {event_id}")
                    write_projection(config, state)
                    return {"status": "duplicate", "record": existing, "notes": notes, "state": state}
            without_hash = {
                "schema": SCHEMA,
                "seq": len(records) + 1,
                "event_id": event_id,
                "event_type": event_type,
                "actor_role": actor_role,
                "timestamp_utc": utc_now(),
                "prev_hash": head,
                "payload": normalized_payload,
            }
            record = dict(without_hash)
            record["event_hash"] = event_hash(without_hash)
            # Full replay is the common transition oracle. No journal byte is
            # written until the proposed state validates completely.
            candidate_records = [*records, record]
            candidate_state = attach_runtime_frontiers(config, replay(config, candidate_records))
            if event_type in {"bootstrap_source", "user_prompt_source"}:
                dependent_source_ids = [event_id]
            elif event_type in {"source_classified", "source_disposed"}:
                dependent_source_ids = [normalized_payload["source_event_id"]]
                if event_type == "source_classified":
                    dependent_source_ids.extend(current_contract_source_ids(candidate_state))
            elif event_type in {"progress", "action_started"}:
                dependent_source_ids = current_contract_source_ids(candidate_state)
            elif event_type == "capture_gap_resolved" and normalized_payload.get("source_event_id"):
                dependent_source_ids = [normalized_payload["source_event_id"]]
            else:
                dependent_source_ids = []
            require_sources_available(candidate_state, dependent_source_ids)
        except (LedgerError, CasMismatch) as exc:
            try:
                record_rejection(config, attempt, head, exc)
            except (LedgerError, OSError) as evidence_exc:
                raise LedgerError(
                    f"{exc}; rejection evidence also failed: {evidence_exc}"
                ) from exc
            raise
        line = (canonical_json(record) + "\n").encode("ascii")
        path = journal_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("ab", buffering=0) as stream:
                ledger_lock.note_effect(
                    event_id,
                    record["event_hash"],
                    "JOURNAL_APPEND_UNCERTAIN_REPLAY_REQUIRED",
                )
                written = stream.write(line)
                if written != len(line):
                    raise OSError(f"short journal append: wrote {written} of {len(line)} bytes")
                os.fsync(stream.fileno())
            ledger_lock.note_effect(
                event_id,
                record["event_hash"],
                "JOURNAL_COMMITTED_PROJECTION_AND_LOCK_RELEASE_UNPROVEN",
            )
        except OSError as exc:
            if ledger_lock.effect_identity is None:
                raise
            # Once append begins, a short/failed write can have left either a
            # complete commit or a repairable partial tail. Never report the
            # operation as not executed; exact replay decides it later.
            raise PostCommitError(
                f"journal append effect is uncertain: {exc}",
                event_id,
                record["event_hash"],
                "JOURNAL_APPEND_UNCERTAIN_REPLAY_REQUIRED",
            ) from exc
        try:
            write_projection(config, candidate_state)
            ledger_lock.note_effect(
                event_id,
                record["event_hash"],
                "JOURNAL_AND_PROJECTION_COMMITTED_LOCK_RELEASE_UNPROVEN",
            )
        except (OSError, LedgerError) as exc:
            raise PostCommitError(
                f"journal committed but projection failed: {exc}",
                event_id,
                record["event_hash"],
                "JOURNAL_COMMITTED_PROJECTION_UNPROVEN",
            ) from exc
        return {"status": "appended", "record": record, "notes": notes, "state": candidate_state}


def record_rejection(
    config: dict[str, Any], attempt: dict[str, Any], observed_head: str, exc: BaseException
) -> None:
    body = {
        "schema": "chat-objective-continuity-rejection-v1",
        "attempt_sha256": sha256_json(attempt),
        "event_id": attempt["event_id"],
        "event_type": attempt["event_type"],
        "observed_head": observed_head,
        "reason_family": type(exc).__name__,
        "reason": str(exc),
        "semantic_journal_changed": False,
    }
    key = sha256_json(body)
    create_exact(
        ledger_dir(config) / "rejections" / f"{key}.json",
        (canonical_json(body) + "\n").encode("ascii"),
    )


def empty_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": PROJECTION_SCHEMA,
        "identity": identity_document(config),
        "revision": 0,
        "head_hash": ZERO_HASH,
        "sources": {},
        "unclassified_source_ids": [],
        "disposed_sources": {},
        "source_integrity": {},
        "unproven_source_ids": [],
        "capture_gaps": {},
        "capture_gap_resolutions": {},
        "unresolved_capture_gap_ids": [],
        "orphan_capture_gap_resolution_ids": [],
        "contracts": {},
        "current_contract_id": None,
        "open_outcomes": {},
        "outcome_catalog": {},
        "actions": {},
        "action_reconciliations": {},
        "unknown_effect_action_ids": [],
        "latest_progress": None,
        "latest_lifecycle": None,
        "proof_ceiling": "local journal/projection structure only; hook delivery, semantic fidelity, model obedience and empirical improvement are unproven",
    }


def validate_clause_refs(clauses: Any, sources: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(clauses, list) or not clauses:
        raise LedgerError("contract clauses must be a non-empty list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_clause in clauses:
        clause = require_object(raw_clause, "clause")
        clause_id = require_safe_id(clause.get("clause_id"), "clause_id")
        if clause_id in seen:
            raise LedgerError(f"duplicate clause id: {clause_id}")
        seen.add(clause_id)
        source_event_id = require_safe_id(clause.get("source_event_id"), "clause source_event_id")
        if source_event_id not in sources:
            raise LedgerError(f"clause {clause_id} source event is unknown")
        pair = (str(clause.get("source_ref", "")), str(clause.get("source_sha256", "")).upper())
        source = sources[source_event_id]
        if pair != (source["source_ref"], source["source_sha256"]):
            raise LedgerError(f"clause {clause_id} does not bind an immutable source")
        normalized = dict(clause)
        normalized["source_event_id"] = source_event_id
        normalized["source_sha256"] = pair[1]
        result.append(normalized)
    return result


def attach_source_integrity(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    integrity: dict[str, dict[str, Any]] = {}
    unproven: list[str] = []
    root = ledger_dir(config)
    for source_id, source in state["sources"].items():
        path = (root / source["source_ref"]).resolve(strict=False)
        try:
            path.relative_to(root.resolve(strict=False))
        except ValueError:
            item = {"status": "INVALID_PATH", "expected_sha256": source["source_sha256"]}
        else:
            try:
                if not path.is_file():
                    item = {"status": "MISSING", "expected_sha256": source["source_sha256"]}
                else:
                    source_bytes = path.read_bytes()
                    actual = sha256_bytes(source_bytes)
                    item = {
                        "status": "VERIFIED" if actual == source["source_sha256"] else "MODIFIED",
                        "expected_sha256": source["source_sha256"],
                        "actual_sha256": actual,
                        "actual_bytes": len(source_bytes),
                    }
            except OSError as exc:
                item = {
                    "status": "UNREADABLE",
                    "expected_sha256": source["source_sha256"],
                    "error_family": type(exc).__name__,
                }
        integrity[source_id] = item
        if item["status"] != "VERIFIED":
            unproven.append(source_id)
    state["source_integrity"] = integrity
    state["unproven_source_ids"] = sorted(unproven)
    if unproven:
        state["proof_ceiling"] = (
            "journal structure readable, but one or more immutable sources are missing or modified; "
            "dependent semantic/action transitions are unproven"
        )
    return state


def require_sources_available(state: dict[str, Any], source_ids: Iterable[str]) -> None:
    bad = sorted(
        source_id
        for source_id in set(source_ids)
        if state.get("source_integrity", {}).get(source_id, {}).get("status") != "VERIFIED"
    )
    if bad:
        raise LedgerError(f"dependent source bytes are unavailable or modified: {bad}")


def current_contract_source_ids(state: dict[str, Any]) -> list[str]:
    contract = state["contracts"].get(state.get("current_contract_id") or "")
    if not contract:
        return []
    return sorted({item["source_event_id"] for item in contract.get("clauses", [])})


def validate_exact_gap_recovery(
    state: dict[str, Any], gap: dict[str, Any], source_event_id: str
) -> str | None:
    expected_sha256 = gap.get("observed_sha256")
    expected_bytes = gap.get("observed_bytes")
    if not (
        isinstance(expected_sha256, str)
        and re.fullmatch(r"[A-F0-9]{64}", expected_sha256)
        and isinstance(expected_bytes, int)
        and not isinstance(expected_bytes, bool)
        and expected_bytes >= 0
    ):
        return "capture gap has no exact observed SHA-256/byte-count identity"
    source = state.get("sources", {}).get(source_event_id)
    if source is None:
        return "recovered source event is unknown"
    integrity = state.get("source_integrity", {}).get(source_event_id, {})
    if integrity.get("status") != "VERIFIED":
        return "recovered source bytes are unavailable or modified"
    if source.get("source_sha256") != expected_sha256:
        return "recovered source SHA-256 does not match the capture gap"
    if integrity.get("actual_bytes") != expected_bytes:
        return "recovered source byte count does not match the capture gap"
    return None


def validate_contract_event(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    unsupported_payload = set(payload) - CLASSIFICATION_KEYS
    if unsupported_payload:
        raise LedgerError(f"classification contains unsupported fields: {sorted(unsupported_payload)}")
    source_event_id = require_safe_id(payload.get("source_event_id"), "source_event_id")
    if source_event_id not in state["sources"]:
        raise LedgerError("classification source_event_id is unknown")
    if source_event_id not in state["unclassified_source_ids"]:
        raise LedgerError("classification source_event_id was already consumed")
    if state.get("source_integrity"):
        require_sources_available(state, [source_event_id])
    disposition = str(payload.get("disposition", ""))
    allowed = {"INITIAL", "ADD", "CLARIFY", "CORRECT", "REPLACE", "WITHDRAW"}
    if disposition not in allowed:
        raise LedgerError(f"disposition must be one of {sorted(allowed)}")
    current_id = state["current_contract_id"]
    contract = require_object(payload.get("contract", {}), "contract")
    unsupported_contract = set(contract) - CONTRACT_KEYS
    if unsupported_contract:
        raise LedgerError(f"contract contains unsupported fields: {sorted(unsupported_contract)}")
    contract_id = require_safe_id(contract.get("contract_id"), "contract_id")
    if contract_id in state["contracts"]:
        raise LedgerError(f"contract already exists: {contract_id}")
    if disposition == "INITIAL":
        if current_id is not None:
            raise LedgerError("INITIAL is valid only without a current contract")
    elif disposition in {"ADD", "CLARIFY", "CORRECT"}:
        if current_id is None or contract.get("parent_contract_id") != current_id:
            raise LedgerError(f"{disposition} must preserve the current contract lineage")
    elif disposition == "REPLACE":
        if current_id is None or contract.get("replaces_contract_id") != current_id:
            raise LedgerError("REPLACE requires exact replaces_contract_id lineage")
    elif disposition == "WITHDRAW":
        if current_id is None or contract.get("withdraws_contract_id") != current_id:
            raise LedgerError("WITHDRAW requires exact withdraws_contract_id lineage")
    if disposition != "INITIAL" and state.get("source_integrity"):
        require_sources_available(state, current_contract_source_ids(state))
    provided_normative_fields = set(contract).intersection(NORMATIVE_CONTRACT_FIELDS)
    normalized = dict(contract)
    if disposition in {"ADD", "CLARIFY", "CORRECT"}:
        parent_contract = state["contracts"][current_id]
        for field in NORMATIVE_CONTRACT_FIELDS:
            if field not in normalized and field in parent_contract:
                normalized[field] = copy.deepcopy(parent_contract[field])
    primary = normalized.get("primary_objective")
    if disposition != "WITHDRAW" and (not isinstance(primary, str) or not primary.strip()):
        raise LedgerError("active contract requires primary_objective")
    normalized["contract_id"] = contract_id
    normalized["clauses"] = validate_clause_refs(contract.get("clauses"), state["sources"])
    outcomes = contract.get("open_outcomes", [])
    if not isinstance(outcomes, list):
        raise LedgerError("open_outcomes must be a list")
    if disposition == "WITHDRAW" and outcomes:
        raise LedgerError("WITHDRAW cannot publish replacement open outcomes")
    supplied_outcomes: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for value in outcomes:
        item = require_object(value, "open outcome")
        outcome_id = require_safe_id(item.get("outcome_id"), "outcome_id")
        if outcome_id in seen:
            raise LedgerError(f"duplicate open outcome id: {outcome_id}")
        seen.add(outcome_id)
        status = item.get("status", "OPEN")
        if status not in {"OPEN", "SATISFIED", "USER_DEFERRED", "USER_WITHDRAWN", "SUPERSEDED"}:
            raise LedgerError(f"invalid outcome status: {status}")
        normalized_item = dict(item)
        normalized_item["outcome_id"] = outcome_id
        normalized_item["status"] = status
        description = normalized_item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise LedgerError(f"outcome {outcome_id} requires a non-empty description")
        known_description = state.get("outcome_catalog", {}).get(outcome_id)
        if known_description is not None and description != known_description:
            raise LedgerError(f"outcome semantic ID changed description: {outcome_id}")
        supplied_outcomes[outcome_id] = normalized_item
    if disposition in {"ADD", "CLARIFY", "CORRECT"}:
        parent = state["contracts"][current_id]
        parent_outcome_ids = set(state["open_outcomes"])
        if not parent_outcome_ids.issubset(seen):
            missing = sorted(parent_outcome_ids - seen)
            raise LedgerError(f"contract silently drops parent outcomes: {missing}")
        parent_clauses = {item["clause_id"]: item for item in parent.get("clauses", [])}
        new_clauses = {item["clause_id"]: item for item in normalized["clauses"]}
        superseded = payload.get("supersedes_clause_ids", [])
        if not isinstance(superseded, list):
            raise LedgerError("supersedes_clause_ids must be a list")
        superseded_ids = {require_safe_id(item, "superseded clause id") for item in superseded}
        if disposition != "CORRECT" and superseded_ids:
            raise LedgerError("only CORRECT may explicitly supersede prior clauses")
        unknown_superseded = superseded_ids - set(parent_clauses)
        if unknown_superseded:
            raise LedgerError(f"superseded clause ids are not in the parent: {sorted(unknown_superseded)}")
        missing_clauses = set(parent_clauses) - set(new_clauses) - superseded_ids
        if missing_clauses:
            raise LedgerError(f"contract silently drops parent clauses: {sorted(missing_clauses)}")
        for clause_id in set(parent_clauses).intersection(new_clauses):
            if parent_clauses[clause_id] != new_clauses[clause_id]:
                raise LedgerError(f"parent clause changed without a new clause id: {clause_id}")
        if disposition in {"ADD", "CLARIFY"} and normalized.get("primary_objective") != parent.get("primary_objective"):
            raise LedgerError(f"{disposition} cannot rewrite the primary objective")
        merged = {outcome_id: dict(item) for outcome_id, item in state["open_outcomes"].items()}
        for outcome_id, item in supplied_outcomes.items():
            if outcome_id not in merged:
                if item["status"] != "OPEN":
                    raise LedgerError(f"new outcome must begin OPEN: {outcome_id}")
                merged[outcome_id] = item
            # Existing status, evidence and description come from the current
            # progressed projection, never from a stale contract template.
        transitions = contract.get("outcome_transitions", [])
        if not isinstance(transitions, list):
            raise LedgerError("outcome_transitions must be a list")
        current_source = state["sources"][source_event_id]
        current_clause_ids = {
            clause["clause_id"]
            for clause in normalized["clauses"]
            if clause["source_event_id"] == source_event_id
            and clause["source_ref"] == current_source["source_ref"]
            and clause["source_sha256"] == current_source["source_sha256"]
        }
        if superseded_ids and not current_clause_ids:
            raise LedgerError("clause correction lacks an immutable clause bound to the current source")
        raw_normative_transitions = payload.get("normative_transitions", [])
        if not isinstance(raw_normative_transitions, list):
            raise LedgerError("normative_transitions must be a list")
        normative_transitions: list[dict[str, Any]] = []
        transitioned_fields: set[str] = set()
        for value in raw_normative_transitions:
            transition = require_object(value, "normative transition")
            unsupported = set(transition) - NORMATIVE_TRANSITION_KEYS
            if unsupported:
                raise LedgerError(f"normative transition contains unsupported fields: {sorted(unsupported)}")
            field = str(transition.get("field", ""))
            if field not in NORMATIVE_CONTRACT_FIELDS or field in transitioned_fields:
                raise LedgerError(f"invalid or duplicate normative transition field: {field}")
            if transition.get("operation") != disposition:
                raise LedgerError(f"normative transition operation must match {disposition}: {field}")
            clause_ids = transition.get("source_clause_ids")
            if not isinstance(clause_ids, list) or not clause_ids:
                raise LedgerError(f"normative transition lacks source clause authority: {field}")
            normalized_clause_ids = [require_safe_id(item, "normative transition source clause id") for item in clause_ids]
            if not set(normalized_clause_ids).issubset(current_clause_ids):
                raise LedgerError(f"normative transition is not bound to the current source: {field}")
            transitioned_fields.add(field)
            normalized_transition = dict(transition)
            normalized_transition["source_clause_ids"] = normalized_clause_ids
            normative_transitions.append(normalized_transition)
        changed_normative_fields = {
            field
            for field in NORMATIVE_CONTRACT_FIELDS
            if field in provided_normative_fields
            and normalized.get(field) != parent.get(field)
        }
        if changed_normative_fields != transitioned_fields:
            raise LedgerError(
                "normative field transition mismatch: "
                f"changed={sorted(changed_normative_fields)} transitioned={sorted(transitioned_fields)}"
            )
        transitioned: set[str] = set()
        prior_outcome_ids = set(state["open_outcomes"])
        target_by_operation = {
            "WITHDRAW": "USER_WITHDRAWN",
            "DEFER": "USER_DEFERRED",
            "SUPERSEDE": "SUPERSEDED",
            "REOPEN": "OPEN",
        }
        for value in transitions:
            transition = require_object(value, "outcome transition")
            unsupported = set(transition) - OUTCOME_TRANSITION_KEYS
            if unsupported:
                raise LedgerError(f"outcome transition contains unsupported fields: {sorted(unsupported)}")
            outcome_id = require_safe_id(transition.get("outcome_id"), "outcome transition id")
            if outcome_id in transitioned or outcome_id not in prior_outcome_ids:
                raise LedgerError(f"invalid or duplicate outcome transition: {outcome_id}")
            transitioned.add(outcome_id)
            operation = str(transition.get("operation", ""))
            if operation not in target_by_operation:
                raise LedgerError(f"invalid outcome transition operation: {operation}")
            if transition.get("from_status") != merged[outcome_id].get("status"):
                raise LedgerError(f"outcome transition stale from_status: {outcome_id}")
            clause_ids = transition.get("source_clause_ids", [])
            if not isinstance(clause_ids, list) or not clause_ids:
                raise LedgerError(f"outcome transition lacks source clause authority: {outcome_id}")
            normalized_clause_ids = [require_safe_id(value, "outcome transition source clause id") for value in clause_ids]
            if len(set(normalized_clause_ids)) != len(normalized_clause_ids):
                raise LedgerError(f"duplicate outcome transition source clause id: {outcome_id}")
            if not set(normalized_clause_ids).issubset(current_clause_ids):
                raise LedgerError(f"outcome transition is not bound to the current source: {outcome_id}")
            merged[outcome_id]["status"] = target_by_operation[operation]
            normalized_transition = dict(transition)
            normalized_transition["source_clause_ids"] = normalized_clause_ids
            merged[outcome_id]["semantic_transition"] = normalized_transition
        for outcome_id, item in supplied_outcomes.items():
            corrects = item.get("corrects_outcome_id")
            if corrects is not None and outcome_id not in prior_outcome_ids:
                prior_id = require_safe_id(corrects, "corrects_outcome_id")
                if (
                    disposition != "CORRECT"
                    or prior_id not in prior_outcome_ids
                    or outcome_id in prior_outcome_ids
                    or outcome_id == prior_id
                ):
                    raise LedgerError(f"corrected outcome lacks valid prior identity: {outcome_id}")
                prior_transition = next(
                    (value for value in transitions if value.get("outcome_id") == prior_id), None
                )
                if not prior_transition or prior_transition.get("operation") != "SUPERSEDE":
                    raise LedgerError(f"corrected outcome must explicitly supersede prior ID: {outcome_id}")
        normalized["open_outcomes"] = [merged[key] for key in sorted(merged)]
        normalized["outcome_transitions"] = [
            dict(state_item["semantic_transition"])
            for outcome_id, state_item in merged.items()
            if outcome_id in transitioned
        ]
    else:
        if payload.get("normative_transitions"):
            raise LedgerError(f"{disposition} cannot use incremental normative_transitions")
        normative_transitions = []
        normalized["open_outcomes"] = [supplied_outcomes[key] for key in sorted(supplied_outcomes)]
    return {
        "source_event_id": source_event_id,
        "disposition": disposition,
        "contract": normalized,
        "classification_note": payload.get("classification_note"),
        "supersedes_clause_ids": sorted(superseded_ids) if disposition in {"ADD", "CLARIFY", "CORRECT"} else [],
        "normative_transitions": normative_transitions,
    }


def validate_progress(payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = set(payload) - PROGRESS_KEYS
    if forbidden:
        raise LedgerError(f"progress payload contains unsupported fields: {sorted(forbidden)}")
    updates = payload.get("outcome_updates", [])
    if not isinstance(updates, list):
        raise LedgerError("outcome_updates must be a list")
    for value in updates:
        update = require_object(value, "outcome update")
        unsupported = set(update) - OUTCOME_UPDATE_KEYS
        if unsupported:
            raise LedgerError(f"outcome update contains semantic/unsupported fields: {sorted(unsupported)}")
    return dict(payload)


def normalize_proposed_transition(
    config: dict[str, Any], state: dict[str, Any], event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    normalized = dict(payload)
    if event_type in {"bootstrap_source", "user_prompt_source"}:
        source_ref = str(payload.get("source_ref", ""))
        expected = str(payload.get("source_sha256", "")).upper()
        path = ledger_dir(config) / source_ref
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            raise LedgerError("captured source file is missing or modified before journal commit")
    elif event_type == "source_classified":
        normalized = validate_contract_event(payload, state)
    elif event_type == "source_disposed":
        unsupported = set(payload) - SOURCE_DISPOSITION_KEYS
        if unsupported:
            raise LedgerError(f"non-authority disposition contains unsupported fields: {sorted(unsupported)}")
        source_event_id = require_safe_id(payload.get("source_event_id"), "source_event_id")
        if source_event_id not in state["unclassified_source_ids"]:
            raise LedgerError("disposed source is unknown or already consumed")
        require_sources_available(state, [source_event_id])
        if payload.get("disposition") not in {"INTERNAL_CONTINUATION", "SUBAGENT", "IRRELEVANT", "NON_USER", "ALREADY_INCORPORATED_DUPLICATE"}:
            raise LedgerError("invalid non-authority source disposition")
        ground = payload.get("review_ground")
        if not isinstance(ground, str) or not ground.strip():
            raise LedgerError("non-authority source disposition requires review_ground")
        if payload.get("disposition") == "ALREADY_INCORPORATED_DUPLICATE":
            contract_id = require_safe_id(payload.get("incorporated_contract_id"), "incorporated_contract_id")
            if contract_id != state["current_contract_id"]:
                raise LedgerError("already-incorporated source must bind the current contract")
            clause_ids = payload.get("incorporated_clause_ids")
            if not isinstance(clause_ids, list) or not clause_ids:
                raise LedgerError("already-incorporated source requires incorporated_clause_ids")
            normalized_clause_ids = [require_safe_id(value, "incorporated clause id") for value in clause_ids]
            current_ids = {
                item["clause_id"] for item in state["contracts"][contract_id].get("clauses", [])
            }
            if not set(normalized_clause_ids).issubset(current_ids):
                raise LedgerError("incorporated clause ids are not in the current contract")
            normalized["incorporated_clause_ids"] = normalized_clause_ids
        normalized["semantic_authority"] = False
        normalized["objective_changed"] = False
    elif event_type == "progress":
        normalized = validate_progress(payload)
        if state["current_contract_id"] is None:
            raise LedgerError("progress requires an active source-classified contract")
        contract_id = require_safe_id(payload.get("contract_id"), "progress contract_id")
        if contract_id != state["current_contract_id"]:
            raise LedgerError("progress must bind the exact current contract")
        normalized["contract_id"] = contract_id
        require_sources_available(state, current_contract_source_ids(state))
    elif event_type == "action_started":
        unsupported = set(payload) - ACTION_START_KEYS
        if unsupported:
            raise LedgerError(f"action start contains unsupported fields: {sorted(unsupported)}")
        if state["current_contract_id"] is None:
            raise LedgerError("action start requires an active source-classified contract")
        require_sources_available(state, current_contract_source_ids(state))
        contract_id = require_safe_id(payload.get("contract_id"), "action contract_id")
        if contract_id != state["current_contract_id"]:
            raise LedgerError("action start must bind the exact current contract")
        outcome_ids = payload.get("outcome_ids")
        if not isinstance(outcome_ids, list) or not outcome_ids:
            raise LedgerError("action start requires outcome_ids")
        normalized_outcome_ids = [require_safe_id(value, "action outcome id") for value in outcome_ids]
        if not set(normalized_outcome_ids).issubset(state["open_outcomes"]):
            raise LedgerError("action start references an outcome outside the current contract")
        clause_ids = payload.get("source_clause_ids")
        if not isinstance(clause_ids, list) or not clause_ids:
            raise LedgerError("action start requires source_clause_ids")
        normalized_clause_ids = [require_safe_id(value, "action source clause id") for value in clause_ids]
        contract_clause_ids = {
            item["clause_id"] for item in state["contracts"][contract_id].get("clauses", [])
        }
        if not set(normalized_clause_ids).issubset(contract_clause_ids):
            raise LedgerError("action start source clauses are not in the current contract")
        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            raise LedgerError("action start requires a non-empty description")
        normalized["contract_id"] = contract_id
        normalized["outcome_ids"] = normalized_outcome_ids
        normalized["source_clause_ids"] = normalized_clause_ids
    elif event_type == "action_outcome":
        unsupported = set(payload) - ACTION_OUTCOME_KEYS
        if unsupported:
            raise LedgerError(f"action outcome contains unsupported fields: {sorted(unsupported)}")
    elif event_type == "action_reconciled":
        unsupported = set(payload) - ACTION_RECONCILE_KEYS
        if unsupported:
            raise LedgerError(f"action reconciliation contains unsupported fields: {sorted(unsupported)}")
    elif event_type == "capture_gap_resolved":
        unsupported = set(payload) - CAPTURE_GAP_RESOLUTION_KEYS
        if unsupported:
            raise LedgerError(f"capture-gap resolution contains unsupported fields: {sorted(unsupported)}")
        gap_id = require_safe_id(payload.get("gap_id"), "gap_id")
        if gap_id not in state.get("unresolved_capture_gap_ids", []):
            raise LedgerError("capture gap is unknown or already resolved")
        resolution = str(payload.get("resolution", ""))
        if resolution not in {"SOURCE_RECOVERED", "OWNER_DISPOSITION"}:
            raise LedgerError("invalid capture-gap resolution")
        ground = payload.get("review_ground")
        if not isinstance(ground, str) or not ground.strip():
            raise LedgerError("capture-gap resolution requires review_ground")
        if resolution == "SOURCE_RECOVERED":
            source_id = require_safe_id(payload.get("source_event_id"), "recovered source_event_id")
            recovery_error = validate_exact_gap_recovery(
                state, state["capture_gaps"][gap_id], source_id
            )
            if recovery_error is not None:
                raise LedgerError(recovery_error)
            normalized["source_event_id"] = source_id
        elif payload.get("source_event_id") is not None:
            raise LedgerError("OWNER_DISPOSITION cannot claim a recovered source")
        normalized["semantic_authority"] = False
        normalized["objective_changed"] = False
    else:
        raise LedgerError(f"unknown journal event type: {event_type}")
    return normalized


def replay(config: dict[str, Any], records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    state = empty_state(config)
    for record in records:
        event_type = record["event_type"]
        payload = record["payload"]
        if event_type in {"bootstrap_source", "user_prompt_source"}:
            source_event_id = record["event_id"]
            source = dict(payload)
            source["source_event_id"] = source_event_id
            state["sources"][source_event_id] = source
            if source_event_id not in state["unclassified_source_ids"]:
                state["unclassified_source_ids"].append(source_event_id)
        elif event_type == "source_disposed":
            source_event_id = require_safe_id(payload.get("source_event_id"), "source_event_id")
            if source_event_id not in state["unclassified_source_ids"]:
                raise LedgerError("disposed source is unknown or already consumed")
            disposition = str(payload.get("disposition", ""))
            if disposition not in {"INTERNAL_CONTINUATION", "SUBAGENT", "IRRELEVANT", "NON_USER", "ALREADY_INCORPORATED_DUPLICATE"}:
                raise LedgerError("invalid non-authority source disposition")
            ground = payload.get("review_ground")
            if not isinstance(ground, str) or not ground.strip():
                raise LedgerError("non-authority source disposition requires review_ground")
            if disposition == "ALREADY_INCORPORATED_DUPLICATE":
                contract_id = require_safe_id(payload.get("incorporated_contract_id"), "incorporated_contract_id")
                if contract_id != state["current_contract_id"]:
                    raise LedgerError("already-incorporated source must bind the current contract")
                clause_ids = payload.get("incorporated_clause_ids")
                if not isinstance(clause_ids, list) or not clause_ids:
                    raise LedgerError("already-incorporated source requires incorporated_clause_ids")
                current_ids = {
                    item["clause_id"]
                    for item in state["contracts"][contract_id].get("clauses", [])
                }
                if not set(clause_ids).issubset(current_ids):
                    raise LedgerError("incorporated clause ids are not in the current contract")
            state["disposed_sources"][source_event_id] = dict(payload)
            state["unclassified_source_ids"].remove(source_event_id)
        elif event_type == "source_classified":
            normalized = validate_contract_event(payload, state)
            contract = dict(normalized["contract"])
            contract["source_event_id"] = normalized["source_event_id"]
            contract["disposition"] = normalized["disposition"]
            contract["normative_transitions"] = normalized.get("normative_transitions", [])
            contract["supersedes_clause_ids"] = normalized.get("supersedes_clause_ids", [])
            state["contracts"][contract["contract_id"]] = contract
            # Next-work advice is contract-relative. Never carry advice across
            # any source-classified contract transition without a new progress
            # event explicitly bound to the new contract.
            state["latest_progress"] = None
            if normalized["disposition"] == "WITHDRAW":
                state["current_contract_id"] = None
                state["open_outcomes"] = {}
            else:
                state["current_contract_id"] = contract["contract_id"]
                state["open_outcomes"] = {
                    item["outcome_id"]: dict(item) for item in contract["open_outcomes"]
                }
                for item in contract["open_outcomes"]:
                    prior = state["outcome_catalog"].get(item["outcome_id"])
                    if prior is not None and prior != item["description"]:
                        raise LedgerError(f"outcome semantic ID changed description: {item['outcome_id']}")
                    state["outcome_catalog"][item["outcome_id"]] = item["description"]
            with contextlib.suppress(ValueError):
                state["unclassified_source_ids"].remove(normalized["source_event_id"])
        elif event_type == "progress":
            payload = validate_progress(payload)
            contract_id = require_safe_id(payload.get("contract_id"), "progress contract_id")
            if contract_id != state["current_contract_id"]:
                raise LedgerError("progress must bind the exact current contract")
            updates = payload.get("outcome_updates", [])
            for value in updates:
                update = require_object(value, "outcome update")
                outcome_id = require_safe_id(update.get("outcome_id"), "outcome_id")
                if outcome_id not in state["open_outcomes"]:
                    raise LedgerError(f"progress references unknown outcome: {outcome_id}")
                status = update.get("status")
                if status not in {"OPEN", "SATISFIED"}:
                    raise LedgerError(f"invalid outcome status: {status}")
                current_status = state["open_outcomes"][outcome_id].get("status")
                if current_status != "OPEN" and status != current_status:
                    raise LedgerError(f"progress cannot reopen or semantically retire outcome: {outcome_id}")
                normalized_update = dict(update)
                if "evidence_refs" in normalized_update:
                    refs = normalized_update["evidence_refs"]
                    if not isinstance(refs, list):
                        raise LedgerError("evidence_refs must be a list")
                    normalized_update["evidence_refs"] = list(
                        dict.fromkeys([*state["open_outcomes"][outcome_id].get("evidence_refs", []), *refs])
                    )
                state["open_outcomes"][outcome_id].update(normalized_update)
            state["latest_progress"] = dict(payload)
        elif event_type == "action_started":
            action_id = require_safe_id(payload.get("action_id"), "action_id")
            if action_id in state["actions"]:
                raise LedgerError(f"action identity is immutable and already exists: {action_id}")
            contract_id = require_safe_id(payload.get("contract_id"), "action contract_id")
            if contract_id != state["current_contract_id"]:
                raise LedgerError("action start must bind the exact current contract")
            outcome_ids = payload.get("outcome_ids")
            if not isinstance(outcome_ids, list) or not outcome_ids or not set(outcome_ids).issubset(state["open_outcomes"]):
                raise LedgerError("action start references an outcome outside the current contract")
            clause_ids = payload.get("source_clause_ids")
            contract_clause_ids = {
                item["clause_id"] for item in state["contracts"][contract_id].get("clauses", [])
            }
            if not isinstance(clause_ids, list) or not clause_ids or not set(clause_ids).issubset(contract_clause_ids):
                raise LedgerError("action start source clauses are not in the current contract")
            retry_of = payload.get("retry_of")
            if retry_of is not None:
                retry_of = require_safe_id(retry_of, "retry_of")
                if retry_of not in state["actions"]:
                    raise LedgerError("retry_of references unknown action")
                if state["actions"][retry_of].get("status") == "PENDING":
                    raise LedgerError("retry_of action has no terminal result")
            item = dict(payload)
            item["status"] = "PENDING"
            item["event_id"] = record["event_id"]
            state["actions"][action_id] = item
        elif event_type == "action_outcome":
            action_id = require_safe_id(payload.get("action_id"), "action_id")
            if action_id not in state["actions"]:
                raise LedgerError(f"outcome references unknown action: {action_id}")
            if state["actions"][action_id].get("status") != "PENDING":
                raise LedgerError(f"action already has a terminal outcome: {action_id}")
            status = payload.get("status")
            if status not in {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN_EFFECT"}:
                raise LedgerError(f"invalid action outcome: {status}")
            if status == "CANCELLED" and payload.get("effect_state") not in {"NO_EFFECT_CONFIRMED", "UNKNOWN_EFFECT"}:
                raise LedgerError("CANCELLED requires explicit effect_state")
            expected_effects = {
                "SUCCEEDED": {"EFFECT_CONFIRMED_SUCCEEDED"},
                "FAILED": {"EFFECT_CONFIRMED_FAILED", "EFFECT_CONFIRMED_PARTIAL"},
                "UNKNOWN_EFFECT": {"UNKNOWN_EFFECT"},
                "CANCELLED": {"NO_EFFECT_CONFIRMED", "UNKNOWN_EFFECT"},
            }
            effect_state = payload.get("effect_state")
            if effect_state not in expected_effects[status]:
                raise LedgerError(f"{status} requires a matching explicit effect_state")
            terminal = dict(payload)
            terminal.pop("action_id", None)
            terminal["outcome_event_id"] = record["event_id"]
            state["actions"][action_id].update(terminal)
        elif event_type == "action_reconciled":
            action_id = require_safe_id(payload.get("action_id"), "action_id")
            if action_id not in state["actions"]:
                raise LedgerError("reconciliation references unknown action")
            action = state["actions"][action_id]
            if not (
                action.get("status") == "UNKNOWN_EFFECT"
                or (
                    action.get("status") == "CANCELLED"
                    and action.get("effect_state") == "UNKNOWN_EFFECT"
                )
            ):
                raise LedgerError("only unknown/cancelled effects can be reconciled")
            prior_reconciliations = state["action_reconciliations"].setdefault(action_id, [])
            if prior_reconciliations:
                prior = prior_reconciliations[-1]
                if prior.get("resolution") != "REMAINS_UNKNOWN":
                    raise LedgerError("action effect already has a terminal reconciliation")
                if payload.get("prior_reconciliation_event_id") != prior.get("event_id"):
                    raise LedgerError("reconciliation does not link the prior unknown reconciliation")
            elif payload.get("prior_reconciliation_event_id") is not None:
                raise LedgerError("first reconciliation cannot claim a prior reconciliation event")
            resolution = str(payload.get("resolution", ""))
            if resolution not in {"EFFECT_CONFIRMED_SUCCEEDED", "EFFECT_CONFIRMED_FAILED", "NO_EFFECT_CONFIRMED", "REMAINS_UNKNOWN"}:
                raise LedgerError("invalid reconciliation resolution")
            refs = payload.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                raise LedgerError("action reconciliation requires evidence_refs")
            item = dict(payload)
            item["event_id"] = record["event_id"]
            prior_reconciliations.append(item)
        elif event_type == "capture_gap_resolved":
            gap_id = require_safe_id(payload.get("gap_id"), "gap_id")
            if gap_id in state["capture_gap_resolutions"]:
                raise LedgerError("capture gap already has a resolution")
            resolution = str(payload.get("resolution", ""))
            if resolution not in {"SOURCE_RECOVERED", "OWNER_DISPOSITION"}:
                raise LedgerError("invalid capture-gap resolution")
            ground = payload.get("review_ground")
            if not isinstance(ground, str) or not ground.strip():
                raise LedgerError("capture-gap resolution requires review_ground")
            if resolution == "SOURCE_RECOVERED":
                source_id = require_safe_id(payload.get("source_event_id"), "recovered source_event_id")
                if source_id not in state["sources"]:
                    raise LedgerError("recovered source event is unknown")
            item = dict(payload)
            item["event_id"] = record["event_id"]
            state["capture_gap_resolutions"][gap_id] = item
        elif event_type in {"lifecycle", "tool_observation"}:
            state["latest_lifecycle"] = dict(payload)
        else:
            raise LedgerError(f"unknown journal event type: {event_type}")
        state["revision"] = record["seq"]
        state["head_hash"] = record["event_hash"]
    state["unknown_effect_action_ids"] = sorted(
        action_id
        for action_id, action in state["actions"].items()
        if (
            action.get("status") in {"PENDING", "UNKNOWN_EFFECT"}
            or (action.get("status") == "CANCELLED" and action.get("effect_state") == "UNKNOWN_EFFECT")
        )
        and (
            not state["action_reconciliations"].get(action_id)
            or state["action_reconciliations"][action_id][-1].get("resolution")
        )
        not in {"EFFECT_CONFIRMED_SUCCEEDED", "EFFECT_CONFIRMED_FAILED", "NO_EFFECT_CONFIRMED"}
    )
    return state


def render_projection(state: dict[str, Any]) -> str:
    contract = state["contracts"].get(state["current_contract_id"] or "")
    lines = [
        "CHAT OBJECTIVE CONTINUITY v1",
        f"namespace: {state['identity']['namespace']}",
        f"logical_chat_id: {state['identity']['logical_chat_id']}",
        f"revision: {state['revision']}",
        f"head_hash: {state['head_hash']}",
        f"current_contract_id: {state['current_contract_id'] or 'NONE'}",
        "",
        "COMPLETE MACHINE/HISTORY POINTERS",
        "- current machine state: current.json",
        "- append-only history: journal.jsonl",
        "- exact source bytes: sources/<SHA256>.txt",
        "",
        "PRIMARY OBJECTIVE",
        str(contract.get("primary_objective", "UNCLASSIFIED OR WITHDRAWN")) if contract else "UNCLASSIFIED OR WITHDRAWN",
        "",
        "CURRENT POLICY / ACCEPTANCE / AUTHORITY",
    ]
    policy_fields = sorted(NORMATIVE_CONTRACT_FIELDS - {"primary_objective"})
    if contract:
        present = False
        for field in policy_fields:
            if field in contract:
                lines.append(f"- {field}: {human_json(contract[field])}")
                present = True
        if not present:
            lines.append("- NONE RECORDED")
    else:
        lines.append("- NONE (NO ACTIVE CONTRACT)")
    lines.extend([
        "",
        "SOURCE CLAUSE REFERENCES",
    ])
    if contract:
        # Render each exact source binding once. Every clause ID and locator
        # remains visible; only byte-identical repeated source triples move to
        # a local dictionary. Contract/journal/machine schemas are unchanged.
        bindings = {}
        for clause in contract.get("clauses", []):
            key = (clause['source_event_id'], clause['source_ref'], clause['source_sha256'])
            if key not in bindings:
                bindings[key] = 'S' + str(len(bindings) + 1)
        for (event_id, source_ref, digest), alias in bindings.items():
            lines.append(f"- {alias} = {event_id} | {source_ref} | {digest}")
        lines.append("CLAUSE LOCATORS (S aliases bind the complete source triple above)")
        for clause in contract.get("clauses", []):
            key = (clause['source_event_id'], clause['source_ref'], clause['source_sha256'])
            lines.append(
                f"- {clause['clause_id']} | {bindings[key]}"
                + (f" | locator={clause['locator']}" if clause.get("locator") else "")
            )
    else:
        lines.append("- NONE")
    lines.extend(["", "ACTIVE UNRESOLVED OUTCOMES"])
    outcome_items = [
        item
        for item in state["open_outcomes"].values()
        if item.get("status") not in {"SATISFIED", "USER_WITHDRAWN", "SUPERSEDED"}
    ]
    if outcome_items:
        for item in sorted(outcome_items, key=lambda value: value["outcome_id"]):
            lines.append(f"- {item['outcome_id']} | {item.get('status')} | {item.get('description', '')}")
    else:
        lines.append("- NONE")
    resolved_count = len(state["open_outcomes"]) - len(outcome_items)
    lines.append(f"- resolved/retired outcome count (see current.json): {resolved_count}")
    lines.extend(["", "UNCLASSIFIED CAPTURED SOURCES"])
    if state["unclassified_source_ids"]:
        for source_id in state["unclassified_source_ids"]:
            source = state["sources"][source_id]
            lines.append(f"- {source_id} | {source['source_ref']} | {source['source_sha256']}")
    else:
        lines.append("- NONE")
    lines.extend(["", "UNRESOLVED CAPTURE GAPS"])
    if state["unresolved_capture_gap_ids"]:
        for gap_id in state["unresolved_capture_gap_ids"]:
            gap = state["capture_gaps"][gap_id]
            lines.append(
                f"- {gap_id} | {gap.get('status')} | {gap.get('reason_family', 'UNKNOWN')} | {gap.get('source_ref')}"
            )
        lines.append("- RECOVERY: recapture exact source, then resolve-capture-gap as SOURCE_RECOVERED; or use explicit owner disposition.")
    else:
        lines.append("- NONE")
    if state["orphan_capture_gap_resolution_ids"]:
        lines.append(
            "- ORPHAN RESOLUTION RECORDS (gap sidecar unavailable): "
            + human_json(state["orphan_capture_gap_resolution_ids"])
        )
    lines.extend(["", "UNKNOWN OR PENDING ACTION EFFECTS"])
    if state["unknown_effect_action_ids"]:
        for action_id in state["unknown_effect_action_ids"]:
            action = state["actions"][action_id]
            lines.append(f"- {action_id} | {action.get('status')} | {action.get('description', '')}")
    else:
        lines.append("- NONE")
    progress = state["latest_progress"]
    lines.extend(["", "CURRENT-CONTRACT NEXT ELIGIBLE WORK"])
    if progress and progress.get("contract_id") == state["current_contract_id"]:
        lines.append(f"- next_eligible_work: {human_json(progress.get('next_eligible_work'))}")
        lines.append(f"- blockers: {human_json(progress.get('blockers', []))}")
        lines.append(f"- return_step: {human_json(progress.get('return_step'))}")
        lines.append(f"- proof_ceiling: {human_json(progress.get('proof_ceiling'))}")
    else:
        lines.append("- NONE; record a new progress event bound to the exact current_contract_id before relying on next-work advice.")
    lines.extend(["", "UNPROVEN SOURCE INTEGRITY"])
    if state["unproven_source_ids"]:
        for source_id in state["unproven_source_ids"]:
            lines.append(
                f"- {source_id} | {state['source_integrity'][source_id]['status']} | {state['sources'][source_id]['source_ref']}"
            )
    else:
        lines.append("- NONE")
    lines.extend(["", "PROOF CEILING", state["proof_ceiling"], ""])
    return "\n".join(lines)


def write_projection(config: dict[str, Any], state: dict[str, Any]) -> None:
    root = ledger_dir(config)
    payload = (canonical_json(state) + "\n").encode("utf-8")
    atomic_write(root / "current.json", payload)
    text = render_projection(state).encode("utf-8")
    atomic_write(root / "objective.txt", text)


def read_state(config: dict[str, Any], repair: bool = True) -> tuple[dict[str, Any], list[str]]:
    root = ensure_identity(config)
    with FileLock(
        root / "ledger.lock",
        float(config.get("lock_timeout_seconds", 10)),
        {
            "namespace": config["namespace"],
            "logical_chat_id": config["logical_chat_id"],
            "operation": "read_repair_project",
        },
    ):
        records, notes = scan_journal(config, repair_partial=repair)
        state = attach_runtime_frontiers(config, replay(config, records))
        write_projection(config, state)
        return state, notes


def recover_dead_owner_lock(
    config: dict[str, Any], expected_lock_sha256: str
) -> dict[str, Any]:
    root = ledger_dir(config)
    lock = root / "ledger.lock"
    raw = lock.read_bytes()
    actual_hash = sha256_bytes(raw)
    if actual_hash != expected_lock_sha256.upper():
        raise LedgerError("lock recovery hash mismatch")
    try:
        owner = require_object(json.loads(raw.decode("ascii")), "lock owner")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"lock owner is not valid ASCII JSON: {exc}") from exc
    if owner.get("schema") != "chat-objective-continuity-lock-v1":
        raise LedgerError("unsupported lock owner schema")
    if owner.get("namespace") != config["namespace"] or owner.get("logical_chat_id") != config["logical_chat_id"]:
        raise LedgerError("lock owner identity does not match selected ledger")
    if owner_alive(owner.get("pid")) is not False:
        raise LedgerError("lock owner is alive or liveness is unknown; do not evict")
    if lock.read_bytes() != raw:
        raise LedgerError("lock changed during recovery")
    preserved = root / "recovery" / f"dead-lock-{actual_hash}.json"
    create_exact(preserved, raw)
    if lock.read_bytes() != raw:
        raise LedgerError("lock changed after recovery preservation")
    lock.unlink()
    fsync_directory(root)
    try:
        state, notes = read_state(config, repair=True)
        return {
            "status": "LOCK_RECOVERED",
            "prior_lock_sha256": actual_hash,
            "preserved_lock": str(preserved),
            "head_hash": state["head_hash"],
            "notes": notes,
            "proof_ceiling": "dead owner and exact lock recovery plus journal replay only",
        }
    except (LedgerError, OSError) as exc:
        return {
            "status": "LOCK_RECOVERED_LEDGER_UNPROVEN",
            "prior_lock_sha256": actual_hash,
            "preserved_lock": str(preserved),
            "error": str(exc),
            "proof_ceiling": "lock removed after confirmed dead owner; ledger structure remains unproven",
        }


def record_capture_gap(
    base_config: dict[str, Any],
    selected_config: dict[str, Any] | None,
    event_name: str,
    reason_family: str,
    detail: dict[str, Any],
) -> pathlib.Path:
    if selected_config is not None:
        root = ledger_dir(selected_config) / "capture-gaps"
        identity = identity_document(selected_config)
    else:
        root = pathlib.Path(base_config["data_root_path"]) / base_config["namespace"] / "_unbound-capture-gaps"
        identity = {"namespace": base_config["namespace"], "logical_chat_id": "UNAVAILABLE"}
    body = {
        "schema": "chat-objective-continuity-capture-gap-v1",
        "event_name": event_name,
        "identity": identity,
        "reason_family": reason_family,
        "detail": detail,
        "continuity_captured": False,
        "semantic_authority": False,
        "created_utc": utc_now(),
        "nonce": secrets.token_hex(8),
    }
    key = sha256_json(body)
    path = root / f"{key}.json"
    create_exact(path, (canonical_json(body) + "\n").encode("ascii"))
    return path


def attach_capture_gap_frontier(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    root = ledger_dir(config)
    expected_identity = identity_document(config)
    gaps: dict[str, dict[str, Any]] = {}
    directory = root / "capture-gaps"
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            gap_id = path.stem
            entry: dict[str, Any] = {
                "gap_id": gap_id,
                "source_ref": str(path.relative_to(root)).replace("\\", "/"),
                "status": "UNRESOLVED",
            }
            try:
                require_safe_id(gap_id, "capture gap id")
                raw = path.read_bytes()
                body = require_object(json.loads(raw.decode("ascii")), "capture gap")
                if body.get("schema") != "chat-objective-continuity-capture-gap-v1":
                    raise LedgerError("capture gap schema mismatch")
                if body.get("identity") != expected_identity:
                    raise LedgerError("capture gap identity mismatch")
                if sha256_json(body) != gap_id:
                    raise LedgerError("capture gap content hash does not match its identity")
                detail = require_object(body.get("detail"), "capture gap detail")
                entry.update(
                    {
                        "event_name": body.get("event_name"),
                        "reason_family": body.get("reason_family"),
                        "created_utc": body.get("created_utc"),
                        "observed_sha256": detail.get("observed_sha256"),
                        "observed_bytes": detail.get("observed_bytes"),
                    }
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, LedgerError) as exc:
                entry.update(
                    {
                        "status": "INVALID_GAP_RECORD",
                        "error_family": type(exc).__name__,
                    }
                )
            resolution = state["capture_gap_resolutions"].get(gap_id)
            if resolution is not None and entry["status"] == "UNRESOLVED":
                if resolution["resolution"] == "SOURCE_RECOVERED":
                    source_id = str(resolution.get("source_event_id", ""))
                    recovery_error = validate_exact_gap_recovery(state, entry, source_id)
                else:
                    recovery_error = None
                if recovery_error is None:
                    entry["status"] = "RESOLVED"
                    entry["resolution_event_id"] = resolution["event_id"]
                    entry["resolution"] = resolution["resolution"]
                else:
                    entry["status"] = "INVALID_RECOVERY_RELATION"
                    entry["resolution_event_id"] = resolution["event_id"]
                    entry["recovery_error"] = recovery_error
            gaps[gap_id] = entry
    state["capture_gaps"] = gaps
    state["unresolved_capture_gap_ids"] = sorted(
        gap_id for gap_id, item in gaps.items() if item["status"] != "RESOLVED"
    )
    state["orphan_capture_gap_resolution_ids"] = sorted(
        set(state["capture_gap_resolutions"]) - set(gaps)
    )
    if state["unresolved_capture_gap_ids"] or state["orphan_capture_gap_resolution_ids"]:
        gap_limit = (
            "identity-bound capture-gap frontier unresolved; dependent source continuity is unproven"
        )
        existing_limit = str(state.get("proof_ceiling", "")).strip()
        if gap_limit not in existing_limit:
            state["proof_ceiling"] = f"{existing_limit}; {gap_limit}" if existing_limit else gap_limit
    return state


def attach_runtime_frontiers(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return attach_capture_gap_frontier(config, attach_source_integrity(config, state))


def store_source(config: dict[str, Any], data: bytes) -> tuple[str, str]:
    limit = int(config.get("max_prompt_bytes", MAX_DEFAULT_PROMPT))
    if len(data) > limit:
        raise PromptLimitError(
            f"source exceeds configured limit of {limit} bytes",
            sha256_bytes(data),
            len(data),
            limit,
        )
    data.decode("utf-8")
    digest = sha256_bytes(data)
    relative = f"sources/{digest}.txt"
    create_exact(ledger_dir(config) / relative, data)
    return relative, digest


def source_event_id(session_id: str, delivery_id: str) -> str:
    # A trusted host delivery identity is the idempotency key. If the same key
    # is reused with different bytes, append_event exposes an event collision.
    digest = sha256_bytes(f"{session_id}\0{delivery_id}".encode("utf-8"))[:40]
    return f"SRC-{digest}"


def ingest_source(
    config: dict[str, Any],
    session_id: str,
    delivery_id: str,
    data: bytes,
    event_type: str,
    capture_route: str,
    actor_role: str = "root",
) -> dict[str, Any]:
    relative, digest = store_source(config, data)
    event_id = source_event_id(session_id, delivery_id)
    payload = {
        "session_id": session_id,
        "delivery_id": delivery_id,
        "source_ref": relative,
        "source_sha256": digest,
        "capture_route": capture_route,
        "semantic_state": "UNCLASSIFIED_REQUIRES_ROOT_SOURCE_REVIEW",
    }
    result = append_event(config, event_type, event_id, payload, actor_role)
    result["source_event_id"] = event_id
    return result


def resolve_hook_ledger(
    config: dict[str, Any], hook: dict[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve a session to one ledger, never to a semantic actor identity.

    The optional host hook input reuses the parent ``session_id`` for subagent hooks
    and does not expose ``agent_id`` on ordinary prompt/tool events.  This
    result can select one logical ledger, but cannot prove a root-agent caller.
    """
    session_id = str(hook.get("session_id", ""))
    if not session_id:
        return None, "unbound", "session_id absent"
    try:
        require_safe_id(session_id, "session_id")
    except LedgerError as exc:
        return None, "unbound", str(exc)
    binding = config.get("session_bindings", {}).get(session_id)
    if binding is not None:
        if not isinstance(binding, dict) or binding.get("role") not in {"root", "subagent"}:
            return None, "unbound", "invalid configured binding"
        role = str(binding["role"])
        if role == "subagent":
            return None, role, "explicit subagent binding"
        logical_chat_id = str(binding.get("logical_chat_id") or session_id)
    else:
        if not config.get("implicit_session_ledgers", True):
            return None, "unbound", "implicit session ledgers are disabled"
        role = "root"
        logical_chat_id = session_id
    return (
        select_ledger_config(config, logical_chat_id),
        role,
        "session-to-ledger routing only; actor identity is not proven",
    )


def delivery_identity(event_name: str, hook: dict[str, Any]) -> str:
    for field in ("hook_event_id", "event_id", "turn_id", "tool_use_id", "tool_call_id"):
        value = hook.get(field)
        if value:
            return require_safe_id(value, field)
    # Without a host delivery identity, equal content is not evidence of one
    # delivery. Assign a unique occurrence ID rather than payload-hash dedup.
    return f"OCC-{event_name}-{time.time_ns():X}-{os.getpid()}-{secrets.token_hex(8)}"


def extract_prompt(hook: dict[str, Any]) -> bytes:
    found: list[str] = []
    for field in ("prompt", "user_prompt", "message"):
        value = hook.get(field)
        if isinstance(value, str):
            found.append(value)
    if len(found) != 1:
        raise LedgerError("hook must expose exactly one supported string prompt field")
    return found[0].encode("utf-8")


def preflight(
    config: dict[str, Any],
    tool_name: str,
    action_class: str | None,
    dependent_action_ids: Iterable[str],
) -> dict[str, Any]:
    configured = config.get("tool_classes", {}).get(tool_name, "unknown")
    effective = action_class or configured
    if effective not in {"read_only", "mutating", "mixed", "unknown"}:
        raise LedgerError("invalid action_class")
    if not (ledger_dir(config) / "identity.json").exists():
        gap_state = attach_capture_gap_frontier(config, empty_state(config))
        if effective == "mutating" and gap_state["unresolved_capture_gap_ids"]:
            return {
                "decision": "hold",
                "reason": "identity-bound source capture failed and requires explicit recovery/disposition",
                "unresolved_capture_gap_ids": gap_state["unresolved_capture_gap_ids"],
                "tool_class": effective,
                "proof_ceiling": "only this chat's dependent mutation is held; unrelated read-only work remains allowed",
            }
        return {
            "decision": "allow",
            "reason": "no ledger exists before the first effective prompt",
            "tool_class": effective,
            "writes": 0,
            "proof_ceiling": "no prompt-source capture or mutation protection claim",
            "unresolved_capture_gap_ids": gap_state["unresolved_capture_gap_ids"],
        }
    try:
        state, notes = read_state(config, repair=True)
    except LedgerError as exc:
        if effective == "mutating":
            return {"decision": "hold", "reason": "ledger structural readback failed", "detail": str(exc), "tool_class": effective}
        return {"decision": "allow", "reason": "independent non-mutating/unknown route; ledger readback unavailable", "detail": str(exc), "tool_class": effective, "proof_ceiling": "no mutation protection claim"}
    if effective == "mutating" and state["unclassified_source_ids"]:
        return {
            "decision": "hold",
            "reason": "known user source is not yet semantically classified by the root chat",
            "unclassified_source_ids": state["unclassified_source_ids"],
            "tool_class": effective,
            "head_hash": state["head_hash"],
        }
    if effective == "mutating" and state["unresolved_capture_gap_ids"]:
        return {
            "decision": "hold",
            "reason": "identity-bound source capture failed and requires explicit recovery/disposition",
            "unresolved_capture_gap_ids": state["unresolved_capture_gap_ids"],
            "tool_class": effective,
            "head_hash": state["head_hash"],
            "proof_ceiling": "only this chat's dependent mutation is held; unrelated read-only work remains allowed",
        }
    dependent_unproven = sorted(set(current_contract_source_ids(state)).intersection(state["unproven_source_ids"]))
    if effective == "mutating" and dependent_unproven:
        return {
            "decision": "hold",
            "reason": "active contract source bytes are missing or modified",
            "unproven_source_ids": dependent_unproven,
            "tool_class": effective,
            "head_hash": state["head_hash"],
            "proof_ceiling": "source-dependent mutation is unproven; unrelated read-only work remains allowed",
        }
    dependencies = sorted(set(str(value) for value in dependent_action_ids))
    blocked = sorted(set(dependencies).intersection(state["unknown_effect_action_ids"]))
    if effective == "mutating" and blocked:
        return {
            "decision": "hold",
            "reason": "dependent action effect is pending or unknown",
            "blocked_action_ids": blocked,
            "tool_class": effective,
            "head_hash": state["head_hash"],
        }
    if effective == "read_only" and (
        state["unproven_source_ids"]
        or state["unresolved_capture_gap_ids"]
        or state["orphan_capture_gap_resolution_ids"]
    ):
        proof = "read-only allowed; listed source-integrity/capture-continuity claims remain UNPROVEN"
    else:
        proof = "exact configured tool class only" if effective in {"read_only", "mutating"} else "mixed/unknown tool is not claimed protected"
    return {
        "decision": "allow",
        "reason": "no scoped continuity hold",
        "tool_class": effective,
        "configured_tool_class": configured,
        "head_hash": state["head_hash"],
        "notes": notes,
        "proof_ceiling": proof,
    }


def stop_check(config: dict[str, Any], hook: dict[str, Any]) -> dict[str, Any]:
    if not (ledger_dir(config) / "identity.json").exists():
        gap_state = attach_capture_gap_frontier(config, empty_state(config))
        return {
            "decision": "allow",
            "reason": "no ledger exists; any identity-bound capture gap remains explicit",
            "detail": "source capture recovery/disposition is required" if gap_state["unresolved_capture_gap_ids"] else None,
            "unresolved_capture_gap_ids": gap_state["unresolved_capture_gap_ids"],
            "journal_events_appended": 0,
            "semantic_writes": 0,
            "metadata_repair_possible": False,
            "proof_ceiling": "no continuity claim",
        }
    try:
        state, notes = read_state(config, repair=True)
        return {
            "decision": "allow",
            "head_hash": state["head_hash"],
            "notes": notes,
            "journal_events_appended": 0,
            "semantic_writes": 0,
            "metadata_repair_possible": True,
        }
    except LedgerError as exc:
        # Blocking Stop would manufacture a continuation that acts as a new
        # user prompt. Surface the failure, but let the turn stop normally.
        return {
            "decision": "allow",
            "reason": "objective ledger structural readback failed; Stop was not continued",
            "detail": str(exc),
            "stop_hook_active": bool(hook.get("stop_hook_active", False)),
            "journal_events_appended": 0,
            "semantic_writes": 0,
            "metadata_repair_possible": True,
            "proof_ceiling": "ledger remains structurally invalid",
        }


def recovery_context(config: dict[str, Any]) -> dict[str, Any]:
    root = ledger_dir(config)
    if not (root / "identity.json").exists():
        gap_state = attach_capture_gap_frontier(config, empty_state(config))
        gap_ids = gap_state["unresolved_capture_gap_ids"]
        return {
            "status": "capture_gap_before_ledger" if gap_ids else "no_ledger_before_first_effective_prompt",
            "head_hash": None,
            "current_contract_id": None,
            "open_outcome_ids": [],
            "unclassified_source_ids": [],
            "unknown_effect_action_ids": [],
            "unresolved_capture_gap_ids": gap_ids,
            "projection": None,
            "capture_gap_location": str(root / "capture-gaps") if gap_ids else None,
            "notes": [],
        }
    state, notes = read_state(config, repair=True)
    return {
        "head_hash": state["head_hash"],
        "current_contract_id": state["current_contract_id"],
        "open_outcome_ids": sorted(state["open_outcomes"]),
        "unclassified_source_ids": state["unclassified_source_ids"],
        "unknown_effect_action_ids": state["unknown_effect_action_ids"],
        "unresolved_capture_gap_ids": state["unresolved_capture_gap_ids"],
        "projection": str(ledger_dir(config) / "objective.txt"),
        "notes": notes,
    }


def compact_context(result: dict[str, Any]) -> str:
    def bounded(values: Any, limit: int = 4) -> dict[str, Any]:
        items = list(values or [])
        return {"count": len(items), "ids": items[:limit], "truncated": len(items) > limit}

    fields = {
        "head_hash": result.get("head_hash"),
        "current_contract_id": result.get("current_contract_id"),
        "open_outcomes": bounded(result.get("open_outcome_ids", [])),
        "unclassified_sources": bounded(result.get("unclassified_source_ids", [])),
        "unknown_effect_actions": bounded(result.get("unknown_effect_action_ids", [])),
        "capture_gaps": bounded(result.get("unresolved_capture_gap_ids", [])),
    }
    if result.get("status") == "capture_gap_before_ledger":
        location = result.get("capture_gap_location")
        prefix = f"RECOVER FIRST: inspect every *.json in {location}. "
        suffix = (
            ". This existing directory is the complete pre-ledger unresolved-gap frontier. "
            "No objective projection exists yet; recover exact source or record explicit owner disposition."
        )
    elif result.get("status") == "no_ledger_before_first_effective_prompt":
        prefix = "NO OBJECTIVE LEDGER EXISTS BEFORE THE FIRST EFFECTIVE PROMPT. "
        suffix = ". No continuity claim is available."
    else:
        prefix = f"READ FIRST: {result.get('projection')}. "
        suffix = (
            ". Read all source refs and outcome IDs from objective.txt before material work. "
            "UNCLASSIFIED sources are raw captures, not authority; capture gaps require explicit source recovery/disposition."
        )
    rendered = (
        prefix
        + "Same-chat objective ledger readback (structural only): "
        + canonical_json(fields)
        + suffix
    )
    while len(rendered.encode("utf-8")) > 600:
        candidates = [value for value in fields.values() if isinstance(value, dict) and value.get("ids")]
        if not candidates:
            rendered = prefix + "Counts: " + canonical_json({key: value.get("count") if isinstance(value, dict) else value for key, value in fields.items()}) + suffix
            break
        max(candidates, key=lambda value: len(value["ids"]))["ids"].pop()
        rendered = prefix + "Same-chat objective ledger readback (structural only): " + canonical_json(fields) + suffix
    if len(rendered.encode("utf-8")) > 600:
        raise LedgerError("compact recovery context cannot fit the 600-byte hook bound")
    return rendered


def handle_hook(config: dict[str, Any], event_name: str, hook: dict[str, Any]) -> dict[str, Any]:
    reported_name = hook.get("hook_event_name")
    if reported_name is not None and reported_name != event_name:
        raise LedgerError("hook_event_name does not match the configured event")
    if event_name == "UserPromptSubmit":
        prompt = extract_prompt(hook)
        delivery = delivery_identity(event_name, hook)
        result = ingest_source(
            config,
            str(hook["session_id"]),
            delivery,
            prompt,
            "user_prompt_source",
            "HOST_USER_PROMPT_SESSION_BOUND_UNVERIFIED_ACTOR",
            "host_session_bound_unverified",
        )
        state = result["state"]
        return {
            "decision": "allow",
            "status": result["status"],
            "source_event_id": result["source_event_id"],
            "head_hash": state["head_hash"],
            "current_contract_id": state["current_contract_id"],
            "open_outcome_ids": sorted(state["open_outcomes"]),
            "unclassified_source_ids": state["unclassified_source_ids"],
            "unknown_effect_action_ids": state["unknown_effect_action_ids"],
            "unresolved_capture_gap_ids": state["unresolved_capture_gap_ids"],
            "projection": str(ledger_dir(config) / "objective.txt"),
            "semantic_authority": "UNCLASSIFIED_REQUIRES_ROOT_SOURCE_REVIEW",
        }
    if event_name in {"SessionStart", "PreCompact", "PostCompact"}:
        result = recovery_context(config)
        result.update(
            {
                "decision": "allow",
                "status": result.get("status", "readback"),
                "journal_events_appended": 0,
                "semantic_writes": 0,
                "metadata_repair_possible": result.get("head_hash") is not None,
            }
        )
        return result
    if event_name == "Stop":
        return stop_check(config, hook)
    if event_name == "PreToolUse":
        tool_name = str(hook.get("tool_name", hook.get("tool", "unknown")))
        requested_class = hook.get("workflow_action_class")
        dependencies = hook.get("workflow_dependency_action_ids", [])
        if not isinstance(dependencies, list):
            dependencies = []
        return preflight(config, tool_name, str(requested_class) if requested_class else None, dependencies)
    raise LedgerError(f"unsupported hook event: {event_name}")


def read_stdin_json(limit: int) -> dict[str, Any]:
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise InputLimitError(f"stdin exceeds configured limit of {limit} bytes", raw, limit)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"invalid hook JSON: {exc}") from exc
    return require_object(value, "hook input")


def read_input_arg(path: str, limit: int = MAX_DEFAULT_INPUT) -> dict[str, Any]:
    if path == "-":
        return read_stdin_json(limit)
    return load_json_file(pathlib.Path(path).resolve())


def output(value: Any) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def hook_warning_output(event_name: str, message: str) -> dict[str, Any]:
    if event_name == "PreToolUse":
        return {"systemMessage": message}
    return {"continue": True, "systemMessage": message}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--config", required=True)
        item.add_argument("--logical-chat-id")
        return item

    bootstrap = with_config("bootstrap", "create/reuse a logical chat ledger and ingest an exact source file")
    bootstrap.add_argument("--session-id", required=True)
    bootstrap.add_argument("--source-file", required=True)
    bootstrap.add_argument("--delivery-id", default="bootstrap-current-source-v1")

    classify = with_config("classify-source", "append a root-authored semantic classification")
    classify.add_argument("--input", required=True)
    classify.add_argument("--event-id", required=True)
    classify.add_argument("--expected-head")

    dispose = with_config("dispose-source", "append an owner-reviewed non-authority source disposition")
    dispose.add_argument("--input", required=True, help="JSON file, or - for UTF-8 stdin")
    dispose.add_argument("--event-id", required=True)
    dispose.add_argument("--expected-head")

    resolve_gap = with_config("resolve-capture-gap", "append an explicit source-recovery or owner-disposition for one identity-bound gap")
    resolve_gap.add_argument("--input", required=True, help="JSON file, or - for UTF-8 stdin")
    resolve_gap.add_argument("--event-id", required=True)
    resolve_gap.add_argument("--expected-head")

    progress = with_config("progress", "append progress without changing objective semantics")
    progress.add_argument("--input", required=True)
    progress.add_argument("--event-id", required=True)
    progress.add_argument("--expected-head")

    start = with_config("action-start", "record a pending action")
    start.add_argument("--input", required=True)
    start.add_argument("--event-id", required=True)
    start.add_argument("--expected-head")

    finish = with_config("action-outcome", "record a succeeded/failed/cancelled/unknown action outcome")
    finish.add_argument("--input", required=True)
    finish.add_argument("--event-id", required=True)
    finish.add_argument("--expected-head")

    reconcile = with_config("action-reconcile", "append distinct evidence-based reconciliation of an unknown effect")
    reconcile.add_argument("--input", required=True)
    reconcile.add_argument("--event-id", required=True)
    reconcile.add_argument("--expected-head")

    verify = with_config("verify", "read, optionally repair a partial final line, replay, and project")
    verify.add_argument("--no-repair", action="store_true")

    recover = with_config("recover-lock", "recover one exact hash-bound lock whose owner is confirmed dead")
    recover.add_argument("--expected-lock-sha256", required=True)

    status = with_config("status", "read the current journal-derived state")

    pre = with_config("preflight", "screen only an explicitly classified action")
    pre.add_argument("--tool-name", required=True)
    pre.add_argument("--action-class", choices=["read_only", "mutating", "mixed", "unknown"])
    pre.add_argument("--depends-on-action", action="append", default=[])

    hook = with_config("hook", "consume one optional host-hook JSON object from stdin")
    hook.add_argument("--event", required=True, choices=["UserPromptSubmit", "SessionStart", "PreCompact", "PostCompact", "PreToolUse", "Stop"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_config: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    hook_input: dict[str, Any] | None = None
    committed: dict[str, Any] | None = None
    try:
        base_config = load_config(pathlib.Path(args.config).resolve())
        if args.command == "hook":
            hook_input = read_stdin_json(int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            config, role, _ = resolve_hook_ledger(base_config, hook_input)
            if config is None:
                # Exit 0 with no output: the event belongs to an explicitly
                # excluded subagent session or has no usable session identity.
                return 0
        else:
            logical_chat_id = args.logical_chat_id
            if args.command == "bootstrap" and logical_chat_id is None:
                logical_chat_id = args.session_id
            if logical_chat_id is None:
                raise LedgerError("--logical-chat-id is required for non-hook commands")
            config = select_ledger_config(base_config, logical_chat_id)
        if args.command == "bootstrap":
            session_id = require_safe_id(args.session_id, "session_id")
            resolved, role, _ = resolve_hook_ledger(base_config, {"session_id": session_id})
            if resolved is None or role != "root":
                raise LedgerError("bootstrap session is explicitly excluded from root ownership")
            if resolved["logical_chat_id"] != config["logical_chat_id"]:
                raise LedgerError("bootstrap logical_chat_id conflicts with the configured session binding")
            data = pathlib.Path(args.source_file).resolve().read_bytes()
            result = ingest_source(config, session_id, require_safe_id(args.delivery_id, "delivery_id"), data, "bootstrap_source", "manual_bootstrap")
            committed = result
            output({"status": result["status"], "executed": True, "source_event_id": result["source_event_id"], "head_hash": result["state"]["head_hash"], "projection": str(ledger_dir(config) / "objective.txt")})
        elif args.command == "classify-source":
            payload = read_input_arg(args.input, int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            result = append_event(config, "source_classified", args.event_id, payload, "root", args.expected_head)
            committed = result
            output({"status": result["status"], "executed": True, "head_hash": result["state"]["head_hash"], "current_contract_id": result["state"]["current_contract_id"]})
        elif args.command == "dispose-source":
            payload = read_input_arg(args.input, int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            result = append_event(config, "source_disposed", args.event_id, payload, "root", args.expected_head)
            committed = result
            output({"status": result["status"], "executed": True, "head_hash": result["state"]["head_hash"], "unclassified_source_ids": result["state"]["unclassified_source_ids"]})
        elif args.command == "resolve-capture-gap":
            payload = read_input_arg(args.input, int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            result = append_event(config, "capture_gap_resolved", args.event_id, payload, "root", args.expected_head)
            committed = result
            output({"status": result["status"], "executed": True, "head_hash": result["state"]["head_hash"], "unresolved_capture_gap_ids": result["state"]["unresolved_capture_gap_ids"]})
        elif args.command == "progress":
            payload = read_input_arg(args.input, int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            result = append_event(config, "progress", args.event_id, payload, "root", args.expected_head)
            committed = result
            output({"status": result["status"], "executed": True, "head_hash": result["state"]["head_hash"]})
        elif args.command in {"action-start", "action-outcome", "action-reconcile"}:
            payload = read_input_arg(args.input, int(base_config.get("max_hook_input_bytes", MAX_DEFAULT_INPUT)))
            event_type = {
                "action-start": "action_started",
                "action-outcome": "action_outcome",
                "action-reconcile": "action_reconciled",
            }[args.command]
            result = append_event(config, event_type, args.event_id, payload, "root", args.expected_head)
            committed = result
            output({"status": result["status"], "executed": True, "head_hash": result["state"]["head_hash"], "unknown_effect_action_ids": result["state"]["unknown_effect_action_ids"]})
        elif args.command == "verify":
            state, notes = read_state(config, repair=not args.no_repair)
            if state["unproven_source_ids"]:
                status = "STRUCTURE_PASS_SOURCE_UNPROVEN"
            elif state["unresolved_capture_gap_ids"] or state["orphan_capture_gap_resolution_ids"]:
                status = "STRUCTURE_PASS_CAPTURE_CONTINUITY_UNPROVEN"
            else:
                status = "STRUCTURE_PASS"
            committed = {"state": state, "effect": "READ_REPAIR_PROJECTION_COMPLETED"}
            output({"status": status, "revision": state["revision"], "head_hash": state["head_hash"], "unproven_source_ids": state["unproven_source_ids"], "unresolved_capture_gap_ids": state["unresolved_capture_gap_ids"], "orphan_capture_gap_resolution_ids": state["orphan_capture_gap_resolution_ids"], "notes": notes, "proof_ceiling": state["proof_ceiling"]})
        elif args.command == "recover-lock":
            result = recover_dead_owner_lock(config, args.expected_lock_sha256)
            committed = {**result, "effect": "LOCK_RECOVERY_ATTEMPT_COMPLETED"}
            output(result)
        elif args.command == "status":
            state, notes = read_state(config, repair=True)
            committed = {"state": state, "effect": "READ_REPAIR_PROJECTION_COMPLETED"}
            output({"status": "OK", "state": state, "notes": notes, "projection": str(ledger_dir(config) / "objective.txt")})
        elif args.command == "preflight":
            result = preflight(config, args.tool_name, args.action_class, args.depends_on_action)
            committed = {**result, "effect": "PREFLIGHT_READBACK_COMPLETED_METADATA_REPAIR_POSSIBLE"}
            output(result)
        elif args.command == "hook":
            if hook_input is None:
                raise LedgerError("hook input unavailable")
            result = handle_hook(config, args.event, hook_input)
            committed = {**result, "effect": "HOOK_CONSUMER_COMPLETED_SEE_RESULT_FOR_WRITE_SCOPE"}
            if args.event == "PreToolUse" and result.get("decision") == "hold":
                output({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": str(result.get("reason", "objective continuity hold")),
                    }
                })
            elif args.event in {"SessionStart", "UserPromptSubmit"} and result.get("status") != "ignored_non_root":
                output({
                    "hookSpecificOutput": {
                        "hookEventName": args.event,
                        "additionalContext": compact_context(result),
                    }
                })
            elif args.event == "Stop":
                response: dict[str, Any] = {"continue": True}
                if result.get("detail"):
                    response["systemMessage"] = "Objective ledger readback failed; no automatic continuation was created. Manual verification is required."
                output(response)
            elif args.event in {"PreCompact", "PostCompact"}:
                output({"continue": True})
        else:
            raise LedgerError("unknown command")
        return 0
    except PostCommitError as exc:
        receipt = {
            "status": "COMMIT_EFFECT_REQUIRES_REPLAY",
            "error": str(exc),
            "event_id": exc.event_id,
            "proposed_head_hash": exc.head_hash,
            "executed": True,
            "effect": exc.effect,
            "primary_fault": exc.primary_message,
            "cleanup_fault": exc.cleanup_fault,
        }
        try:
            output(receipt)
        except OSError as output_exc:
            fallback = canonical_json({**receipt, "output_error": str(output_exc)}) + "\n"
            with contextlib.suppress(OSError):
                sys.stderr.buffer.write(fallback.encode("ascii"))
        return 4
    except PromptLimitError as exc:
        if base_config is not None:
            path = record_capture_gap(base_config, config, getattr(args, "event", "manual"), "PROMPT_LIMIT", {"observed_sha256": exc.observed_sha256, "observed_bytes": exc.observed_bytes, "limit": exc.limit})
        else:
            path = None
        if args.command == "hook":
            output(hook_warning_output(getattr(args, "event", "hook"), f"Objective source capture exceeded the configured bound; continuity was not captured. Gap: {path}"))
            return 0
        output({"status": "ERROR", "error": str(exc), "executed": False, "capture_gap": str(path) if path else None})
        return 2
    except InputLimitError as exc:
        path = None
        if base_config is not None:
            path = record_capture_gap(base_config, None, getattr(args, "event", "manual"), "INPUT_LIMIT_IDENTITY_UNAVAILABLE", {"observed_prefix_sha256": sha256_bytes(exc.observed_prefix), "observed_bytes_at_least": len(exc.observed_prefix), "limit": exc.limit})
        if args.command == "hook":
            output(hook_warning_output(getattr(args, "event", "hook"), f"Hook input exceeded the configured bound; chat identity and continuity capture are unavailable. Gap: {path}"))
            return 0
        output({"status": "ERROR", "error": str(exc), "executed": False, "capture_gap": str(path) if path else None})
        return 2
    except CasMismatch as exc:
        output({"status": "CAS_MISMATCH", "error": str(exc), "executed": False})
        return 3
    except OSError as exc:
        if committed is not None:
            state = committed.get("state") if isinstance(committed, dict) else None
            head_hash = state.get("head_hash") if isinstance(state, dict) else committed.get("head_hash")
            fallback = canonical_json(
                {
                    "status": "OUTPUT_FAILED_AFTER_EFFECT",
                    "error": str(exc),
                    "executed": True,
                    "head_hash": head_hash,
                    "effect": committed.get("effect", "OPERATION_COMPLETED_OUTPUT_UNDELIVERED"),
                }
            ) + "\n"
            with contextlib.suppress(OSError):
                sys.stderr.buffer.write(fallback.encode("ascii"))
            return 4
        output({"status": "ERROR", "error": str(exc), "executed": False})
        return 2
    except (LedgerError, UnicodeDecodeError) as exc:
        if args.command == "hook" and base_config is not None:
            path = record_capture_gap(base_config, config, getattr(args, "event", "hook"), type(exc).__name__, {"error": str(exc)[:1024]})
            output(hook_warning_output(getattr(args, "event", "hook"), f"Objective continuity hook could not capture or read its bounded state; dependent continuity is unproven. Gap: {path}"))
            return 0
        output({"status": "ERROR", "error": str(exc), "executed": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
