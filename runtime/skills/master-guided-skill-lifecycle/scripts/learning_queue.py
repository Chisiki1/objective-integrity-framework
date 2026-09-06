#!/usr/bin/env python3
"""References-only SQLite queue for bounded workflow learning candidates.

This module validates identities and reference-shaped lifecycle evidence.  It does
not validate semantic merit, grant action authority, execute a candidate, or
write policies, masters, registries, or active Skills.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
STATES = {
    "discovered",
    "prepared",
    "evaluated",
    "active-bounded",
    "measured",
    "deferred",
    "retired",
    "superseded",
}
ACTIONABLE_STATES = STATES - {"retired", "superseded"}
METRIC_FIELDS = {
    "objective_evidence",
    "mandatory_quality",
    "elapsed_ms",
    "token_usage",
    "tool_calls",
    "delegation",
    "integration",
    "rework",
    "consumer_outcome",
    "overhead",
    "false_positive",
    "late_miss",
    "recurrence",
}
METRIC_STATUSES = {"observed", "unavailable", "not_applicable"}
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
MAX_REFERENCE_LENGTH = 4096
MAX_REFERENCE_ITEMS = 256
DEFAULT_DUE_LIMIT = 100
MAX_DUE_LIMIT = 1000


class QueueError(Exception):
    """A decision-bearing queue failure with explicit mutation status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        commit_status: str = "not_committed",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.commit_status = commit_status
        self.details = dict(details or {})

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "first_fault": self.message,
            "commit_status": self.commit_status,
            "authority_granted": False,
        }
        if self.details:
            result["details"] = self.details
        return result


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QueueError("MALFORMED_JSON_VALUE", str(exc)) from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise QueueError(
            "INVALID_IDENTIFIER",
            f"{name} must match {ID_RE.pattern}",
            details={"field": name},
        )
    return value


def _require_reference(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueueError("INVALID_REFERENCE", f"{name} must be a non-empty string reference")
    if len(value) > MAX_REFERENCE_LENGTH or "\n" in value or "\r" in value or "\x00" in value:
        raise QueueError(
            "INVALID_REFERENCE",
            f"{name} must be a single bounded reference, not an embedded body",
        )
    if any(character.isspace() for character in value) and not Path(value).is_absolute():
        raise QueueError(
            "INVALID_REFERENCE",
            f"{name} must be a reference token or absolute path, not narrative text",
        )
    return value


def _require_reference_list(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = "an array" if allow_empty else "a non-empty array"
        raise QueueError("INVALID_REFERENCE_LIST", f"{name} must be {suffix} of references")
    if len(value) > MAX_REFERENCE_ITEMS:
        raise QueueError(
            "REFERENCE_LIST_TOO_LARGE",
            f"{name} exceeds the bounded maximum of {MAX_REFERENCE_ITEMS} references",
        )
    result = [_require_reference(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(set(result)) != len(result):
        raise QueueError("DUPLICATE_REFERENCE", f"{name} cannot contain duplicate references")
    return result


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise QueueError("INVALID_SHA256", f"{name} must be a 64-hex SHA-256")
    return value.upper()


def _verify_artifact(path_value: str, expected_sha256: str) -> tuple[str, str]:
    artifact_path = Path(path_value)
    if not artifact_path.is_absolute():
        raise QueueError("ARTIFACT_PATH_NOT_ABSOLUTE", "artifact_ref.path must be absolute")
    normalized = Path(os.path.abspath(os.fspath(artifact_path)))
    _check_no_reparse(normalized)
    if not normalized.is_file():
        raise QueueError("ARTIFACT_NOT_FOUND", f"artifact is not an existing regular file: {normalized}")
    digest = hashlib.sha256()
    try:
        with normalized.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QueueError("ARTIFACT_READ_FAILED", f"cannot read artifact {normalized}: {exc}") from exc
    actual = digest.hexdigest().upper()
    if actual != expected_sha256:
        raise QueueError(
            "ARTIFACT_HASH_MISMATCH",
            "artifact bytes do not match artifact_ref.sha256",
            details={"path": str(normalized), "expected": expected_sha256, "actual": actual},
        )
    return str(normalized), actual


def _check_no_reparse(path: Path) -> None:
    """Reject existing symlink/reparse components before SQLite follows them."""

    candidates = [path, *path.parents]
    for candidate in candidates:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise QueueError(
                "DB_PATH_INSPECTION_FAILED",
                f"cannot inspect database path component {candidate}: {exc}",
            ) from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
            raise QueueError(
                "DB_REPARSE_PATH_REJECTED",
                f"database path component is a symlink/reparse point: {candidate}",
            )


def validate_db_path(db_path: os.PathLike[str] | str) -> Path:
    raw = Path(db_path)
    if not raw.is_absolute():
        raise QueueError("DB_PATH_NOT_ABSOLUTE", "database path must be explicit and absolute")
    normalized = Path(os.path.abspath(os.fspath(raw)))
    _check_no_reparse(normalized)
    if not normalized.parent.exists():
        raise QueueError("DB_PARENT_MISSING", f"database parent does not exist: {normalized.parent}")
    if not normalized.parent.is_dir():
        raise QueueError("DB_PARENT_NOT_DIRECTORY", f"database parent is not a directory: {normalized.parent}")
    if normalized.exists() and not normalized.is_file():
        raise QueueError("DB_PATH_NOT_FILE", f"database path is not a regular file: {normalized}")
    return normalized


def _connect(db_path: os.PathLike[str] | str, *, access: str) -> sqlite3.Connection:
    path = validate_db_path(db_path)
    if access not in {"create", "rw", "ro"}:
        raise QueueError("INVALID_DB_ACCESS", f"unsupported database access mode: {access}")
    if access != "create" and not path.is_file():
        raise QueueError("DB_NOT_FOUND", f"queue database does not exist: {path}")
    try:
        if access == "create":
            connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        else:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode={access}",
                timeout=10.0,
                isolation_level=None,
                uri=True,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
    except sqlite3.Error as exc:
        raise QueueError("DB_OPEN_FAILED", f"cannot open queue database: {exc}") from exc


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise QueueError("DB_BEGIN_FAILED", f"cannot acquire queue writer transaction: {exc}") from exc


def _begin_read(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as exc:
        raise QueueError("DB_READ_BEGIN_FAILED", f"cannot acquire queue read snapshot: {exc}") from exc


def _rollback_status(connection: sqlite3.Connection) -> str:
    try:
        connection.rollback()
        return "not_committed"
    except sqlite3.Error:
        return "unknown"


def _require_initialized(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('queue_events','candidate_current')"
        )
    }
    if version != SCHEMA_VERSION or names != {"queue_events", "candidate_current"}:
        raise QueueError(
            "DB_NOT_INITIALIZED",
            "queue database is absent or has an unsupported schema; run init first",
            details={"user_version": version, "tables": sorted(names)},
        )


def init_db(db_path: os.PathLike[str] | str) -> dict[str, Any]:
    """Create or verify the two-table queue schema in one writer transaction."""

    connection = _connect(db_path, access="create")
    commit_status = "not_committed"
    transaction_started = False
    try:
        _begin_immediate(connection)
        transaction_started = True
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise QueueError(
                "UNSUPPORTED_SCHEMA_VERSION",
                f"database user_version {version} is not supported",
            )
        schema_statements = (
            """
            CREATE TABLE IF NOT EXISTS queue_events (
                event_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                prior_revision INTEGER NOT NULL,
                new_revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(candidate_id, new_revision),
                FOREIGN KEY(candidate_id) REFERENCES candidate_current(candidate_id)
                    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS candidate_current (
                candidate_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                state TEXT NOT NULL,
                owner_ref TEXT NOT NULL,
                objective_ref TEXT NOT NULL,
                project_event_ref TEXT NOT NULL,
                global_disposition_ref TEXT NOT NULL,
                family_keys_json TEXT NOT NULL,
                artifact_path TEXT,
                artifact_sha256 TEXT,
                next_consumer_ref TEXT NOT NULL,
                next_use_trigger TEXT NOT NULL,
                return_trigger TEXT,
                successor_candidate_id TEXT,
                payload_json TEXT NOT NULL,
                last_event_id TEXT NOT NULL UNIQUE,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                FOREIGN KEY(last_event_id) REFERENCES queue_events(event_id)
                    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(successor_candidate_id) REFERENCES candidate_current(candidate_id)
                    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS queue_events_no_update
            BEFORE UPDATE ON queue_events
            BEGIN
                SELECT RAISE(ABORT, 'queue_events is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS queue_events_no_delete
            BEFORE DELETE ON queue_events
            BEGIN
                SELECT RAISE(ABORT, 'queue_events is immutable');
            END
            """,
        )
        for statement in schema_statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _require_initialized(connection)
        connection.commit()
        commit_status = "committed"
        return {
            "ok": True,
            "operation": "init",
            "schema_version": SCHEMA_VERSION,
            "commit_status": commit_status,
            "authority_granted": False,
            "proof_ceiling": "storage schema and reference-integrity structure only",
        }
    except QueueError as exc:
        if connection.in_transaction:
            exc.commit_status = _rollback_status(connection)
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            status = _rollback_status(connection)
        elif transaction_started and commit_status != "committed":
            status = "unknown"
        else:
            status = commit_status
        raise QueueError("DB_INIT_FAILED", f"queue schema initialization failed: {exc}", commit_status=status) from exc
    finally:
        connection.close()


def _normalize_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QueueError("INVALID_METRICS", "measurement.metrics must be an object")
    missing = sorted(METRIC_FIELDS - set(value))
    unknown = sorted(set(value) - METRIC_FIELDS)
    if missing or unknown:
        raise QueueError(
            "INVALID_METRIC_SET",
            "measurement.metrics must contain exactly the registered metric fields",
            details={"missing": missing, "unknown": unknown},
        )
    normalized: dict[str, Any] = {}
    for name in sorted(METRIC_FIELDS):
        metric = value[name]
        if not isinstance(metric, dict):
            raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} must be an object")
        if set(metric) - {"status", "value", "reason"}:
            raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} has unsupported fields")
        status_value = metric.get("status")
        if status_value not in METRIC_STATUSES:
            raise QueueError(
                "INVALID_METRIC_STATUS",
                f"measurement.metrics.{name}.status is unknown: {status_value}",
            )
        if status_value == "observed":
            if "value" not in metric or metric["value"] is None:
                raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} observed requires value")
            if isinstance(metric["value"], str) and not metric["value"].strip():
                raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} observed value cannot be blank")
            if isinstance(metric["value"], (list, dict)):
                raise QueueError(
                    "INVALID_METRIC",
                    f"measurement.metrics.{name} observed value must be a bounded scalar",
                )
            if isinstance(metric["value"], str) and (
                len(metric["value"]) > MAX_REFERENCE_LENGTH
                or "\n" in metric["value"]
                or "\r" in metric["value"]
                or "\x00" in metric["value"]
            ):
                raise QueueError(
                    "INVALID_METRIC",
                    f"measurement.metrics.{name} observed string value must be bounded and single-line",
                )
            if isinstance(metric["value"], float) and not math.isfinite(metric["value"]):
                raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} observed value must be finite")
            if "reason" in metric:
                raise QueueError("INVALID_METRIC", f"measurement.metrics.{name} observed cannot include reason")
        else:
            if "value" in metric:
                raise QueueError(
                    "METRIC_PLACEHOLDER_REJECTED",
                    f"measurement.metrics.{name} {status_value} cannot include a placeholder value",
                )
            _require_reference(metric.get("reason"), f"measurement.metrics.{name}.reason")
        normalized[name] = dict(metric)
    return normalized


def _normalize_measurement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "action_ref",
        "outcome_ref",
        "independent_observation_ref",
        "metrics",
    }:
        raise QueueError(
            "MEASUREMENT_REQUIRED",
            "measurement requires action_ref, outcome_ref, independent_observation_ref, and metrics",
        )
    return {
        "action_ref": _require_reference(value.get("action_ref"), "measurement.action_ref"),
        "outcome_ref": _require_reference(value.get("outcome_ref"), "measurement.outcome_ref"),
        "independent_observation_ref": _require_reference(
            value.get("independent_observation_ref"),
            "measurement.independent_observation_ref",
        ),
        "metrics": _normalize_metrics(value.get("metrics")),
    }


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise QueueError("INVALID_EVENT", "event must be an object")
    allowed = {
        "schema_version",
        "event_id",
        "candidate_id",
        "expected_revision",
        "state",
        "owner_ref",
        "objective_ref",
        "source_refs",
        "project_event_ref",
        "global_disposition_ref",
        "family_keys",
        "artifact_ref",
        "next_consumer_ref",
        "next_use_trigger",
        "evidence_refs",
        "rollback_refs",
        "evaluation_ref",
        "independent_challenge_ref",
        "normal_counterexample_ref",
        "rollback_plan_ref",
        "activation_receipt_ref",
        "activation_source_authority_ref",
        "measurement",
        "reason",
        "return_trigger",
        "successor_candidate_id",
        "predecessor_candidate_ids",
    }
    unknown = sorted(set(event) - allowed)
    if unknown:
        raise QueueError("UNKNOWN_EVENT_FIELDS", "event has unsupported fields", details={"unknown": unknown})
    if event.get("schema_version") != "learning-queue-event-v1":
        raise QueueError("INVALID_EVENT_SCHEMA", "schema_version must be learning-queue-event-v1")

    normalized = dict(event)
    normalized["event_id"] = _require_identifier(event.get("event_id"), "event_id")
    normalized["candidate_id"] = _require_identifier(event.get("candidate_id"), "candidate_id")
    expected_revision = event.get("expected_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise QueueError("INVALID_EXPECTED_REVISION", "expected_revision must be an explicit non-negative integer")
    state_value = event.get("state")
    if state_value not in STATES:
        raise QueueError("INVALID_STATE", f"unknown state: {state_value}")

    for name in (
        "owner_ref",
        "objective_ref",
        "project_event_ref",
        "global_disposition_ref",
        "next_consumer_ref",
        "next_use_trigger",
    ):
        normalized[name] = _require_reference(event.get(name), name)
    normalized["source_refs"] = _require_reference_list(event.get("source_refs"), "source_refs")
    normalized["family_keys"] = _require_reference_list(event.get("family_keys"), "family_keys")
    normalized["evidence_refs"] = _require_reference_list(event.get("evidence_refs"), "evidence_refs")
    normalized["rollback_refs"] = _require_reference_list(
        event.get("rollback_refs"), "rollback_refs", allow_empty=True
    )
    predecessors = event.get("predecessor_candidate_ids", [])
    if not isinstance(predecessors, list):
        raise QueueError("INVALID_PREDECESSORS", "predecessor_candidate_ids must be an array")
    normalized["predecessor_candidate_ids"] = [
        _require_identifier(item, f"predecessor_candidate_ids[{index}]")
        for index, item in enumerate(predecessors)
    ]
    if len(set(normalized["predecessor_candidate_ids"])) != len(normalized["predecessor_candidate_ids"]):
        raise QueueError("DUPLICATE_PREDECESSOR", "predecessor_candidate_ids cannot contain duplicates")
    if normalized["candidate_id"] in normalized["predecessor_candidate_ids"]:
        raise QueueError("SELF_REFERENCE", "candidate cannot list itself as a predecessor")

    artifact = event.get("artifact_ref")
    artifact_required = state_value in {"prepared", "evaluated", "active-bounded", "measured"}
    if artifact_required:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise QueueError("ARTIFACT_REQUIRED", f"{state_value} requires artifact_ref path and sha256")
        artifact_path = Path(_require_reference(artifact.get("path"), "artifact_ref.path"))
        if not artifact_path.is_absolute():
            raise QueueError("ARTIFACT_PATH_NOT_ABSOLUTE", "artifact_ref.path must be absolute")
        normalized["artifact_ref"] = {
            "path": os.path.abspath(os.fspath(artifact_path)),
            "sha256": _require_sha256(artifact.get("sha256"), "artifact_ref.sha256"),
        }
        if not normalized["rollback_refs"]:
            raise QueueError("ROLLBACK_REFERENCE_REQUIRED", f"{state_value} requires rollback_refs")
    elif artifact is not None:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise QueueError("INVALID_ARTIFACT", "artifact_ref must contain exactly path and sha256")
        artifact_path = Path(_require_reference(artifact.get("path"), "artifact_ref.path"))
        if not artifact_path.is_absolute():
            raise QueueError("ARTIFACT_PATH_NOT_ABSOLUTE", "artifact_ref.path must be absolute")
        normalized["artifact_ref"] = {
            "path": os.path.abspath(os.fspath(artifact_path)),
            "sha256": _require_sha256(artifact.get("sha256"), "artifact_ref.sha256"),
        }

    evaluated_fields = (
        "evaluation_ref",
        "independent_challenge_ref",
        "normal_counterexample_ref",
        "rollback_plan_ref",
    )
    if state_value in {"evaluated", "active-bounded", "measured"}:
        for name in evaluated_fields:
            normalized[name] = _require_reference(event.get(name), name)
    elif any(name in event for name in evaluated_fields):
        for name in evaluated_fields:
            if name in event:
                normalized[name] = _require_reference(event[name], name)

    activation_fields = ("activation_receipt_ref", "activation_source_authority_ref")
    if state_value in {"active-bounded", "measured"}:
        for name in activation_fields:
            normalized[name] = _require_reference(event.get(name), name)
    elif any(name in event for name in activation_fields):
        for name in activation_fields:
            if name in event:
                normalized[name] = _require_reference(event[name], name)

    if state_value == "measured":
        normalized["measurement"] = _normalize_measurement(event.get("measurement"))
    elif "measurement" in event and state_value in {"deferred", "retired", "superseded"}:
        normalized["measurement"] = _normalize_measurement(event.get("measurement"))
    elif "measurement" in event:
        raise QueueError(
            "PREMATURE_MEASUREMENT",
            "measurement may be introduced only by measured state and then preserved on deferred/terminal exits",
        )

    if state_value in {"deferred", "retired", "superseded"}:
        normalized["reason"] = _require_reference(event.get("reason"), "reason")
    if state_value in {"deferred", "retired"}:
        normalized["return_trigger"] = _require_reference(event.get("return_trigger"), "return_trigger")
    if state_value == "superseded":
        normalized["successor_candidate_id"] = _require_identifier(
            event.get("successor_candidate_id"), "successor_candidate_id"
        )
        if normalized["successor_candidate_id"] == normalized["candidate_id"]:
            raise QueueError("SELF_REFERENCE", "candidate cannot supersede itself")
    elif "successor_candidate_id" in event:
        raise QueueError("INVALID_SUCCESSOR", "successor_candidate_id is accepted only for superseded state")

    return normalized


LEGAL_TRANSITIONS = {
    None: {"discovered"},
    "discovered": {"prepared", "deferred", "retired", "superseded"},
    "prepared": {"evaluated", "deferred", "retired", "superseded"},
    "evaluated": {"active-bounded", "deferred", "retired", "superseded"},
    "active-bounded": {"measured", "deferred", "retired", "superseded"},
    "measured": {"prepared", "deferred", "retired", "superseded"},
    "deferred": {"prepared", "evaluated", "active-bounded", "measured", "retired", "superseded"},
    "retired": set(),
    "superseded": set(),
}


def _validate_identity_continuity(current: sqlite3.Row, event: Mapping[str, Any]) -> None:
    prior = json.loads(current["payload_json"])
    immutable_fields = (
        "candidate_id",
        "owner_ref",
        "objective_ref",
        "family_keys",
    )
    changed = [name for name in immutable_fields if prior.get(name) != event.get(name)]
    if changed:
        raise QueueError(
            "IMMUTABLE_IDENTITY_CHANGED",
            "candidate identity/reference bindings cannot change across revisions",
            details={"changed": changed},
        )
    removed_sources = sorted(set(prior.get("source_refs", [])) - set(event.get("source_refs", [])))
    if removed_sources:
        raise QueueError(
            "SOURCE_REFERENCE_REMOVED",
            "source_refs may add later authority references but cannot remove prior bindings",
            details={"removed": removed_sources},
        )
    removed_predecessors = sorted(
        set(prior.get("predecessor_candidate_ids", [])) - set(event.get("predecessor_candidate_ids", []))
    )
    if removed_predecessors:
        raise QueueError(
            "PREDECESSOR_REFERENCE_REMOVED",
            "predecessor_candidate_ids cannot be removed from a later projection",
            details={"removed": removed_predecessors},
        )
    prior_state = prior.get("state")
    next_state = event.get("state")
    if "measurement" in event and "measurement" not in prior and next_state != "measured":
        raise QueueError(
            "PREMATURE_MEASUREMENT",
            "a deferred or terminal transition may preserve but cannot introduce measurement",
        )
    stage_reference_fields = (
        "artifact_ref",
        "evaluation_ref",
        "independent_challenge_ref",
        "normal_counterexample_ref",
        "rollback_plan_ref",
        "activation_receipt_ref",
        "activation_source_authority_ref",
        "measurement",
    )
    for name in stage_reference_fields:
        if name not in prior:
            continue
        must_preserve = next_state in {"deferred", "retired", "superseded"}
        if name == "artifact_ref" and next_state in {"evaluated", "active-bounded", "measured"}:
            must_preserve = True
        if name in {
            "evaluation_ref",
            "independent_challenge_ref",
            "normal_counterexample_ref",
            "rollback_plan_ref",
        } and next_state in {"active-bounded", "measured"}:
            must_preserve = True
        if name in {"activation_receipt_ref", "activation_source_authority_ref"} and next_state == "measured":
            must_preserve = True
        if must_preserve and event.get(name) != prior[name]:
            raise QueueError(
                "STAGE_REFERENCE_CHANGED",
                f"{name} cannot be removed or changed across this lifecycle transition",
                details={"field": name, "from_state": prior_state, "to_state": next_state},
            )


def _validate_graph_references(
    connection: sqlite3.Connection,
    event: Mapping[str, Any],
) -> None:
    candidate_id = str(event["candidate_id"])
    for predecessor in event.get("predecessor_candidate_ids", []):
        row = connection.execute(
            "SELECT candidate_id,objective_ref,family_keys_json FROM candidate_current WHERE candidate_id = ?",
            (predecessor,),
        ).fetchone()
        if row is None:
            raise QueueError(
                "MISSING_PREDECESSOR",
                f"predecessor candidate does not exist: {predecessor}",
            )
        if row["objective_ref"] != event["objective_ref"]:
            raise QueueError("PREDECESSOR_OBJECTIVE_MISMATCH", "predecessor must share the exact objective_ref")
        predecessor_families = set(json.loads(row["family_keys_json"]))
        if not predecessor_families.intersection(event["family_keys"]):
            raise QueueError("PREDECESSOR_FAMILY_MISMATCH", "predecessor must share at least one exact family key")
    successor = event.get("successor_candidate_id")
    if successor is None:
        return
    successor_row = connection.execute(
        "SELECT candidate_id, objective_ref, family_keys_json, successor_candidate_id "
        "FROM candidate_current WHERE candidate_id = ?",
        (successor,),
    ).fetchone()
    if successor_row is None:
        raise QueueError("MISSING_SUCCESSOR", f"successor candidate does not exist: {successor}")
    if successor_row["objective_ref"] != event["objective_ref"]:
        raise QueueError("SUCCESSOR_OBJECTIVE_MISMATCH", "successor must share the exact objective_ref")
    successor_families = set(json.loads(successor_row["family_keys_json"]))
    if not successor_families.intersection(event["family_keys"]):
        raise QueueError("SUCCESSOR_FAMILY_MISMATCH", "successor must share at least one exact family key")

    seen = {candidate_id}
    cursor: str | None = successor
    while cursor is not None:
        if cursor in seen:
            raise QueueError("SUCCESSOR_CYCLE", "successor relation would create a cycle")
        seen.add(cursor)
        row = connection.execute(
            "SELECT successor_candidate_id FROM candidate_current WHERE candidate_id = ?", (cursor,)
        ).fetchone()
        cursor = row[0] if row is not None else None


def _next_action(state_value: str) -> str:
    return {
        "discovered": "owner decides reuse disposition and prepares a hash-bound artifact or defers/retires",
        "prepared": "owner obtains evaluation, independent challenge, normal counterexample, and rollback-plan evidence",
        "evaluated": "owner independently determines whether a separately authorized bounded activation is eligible",
        "active-bounded": "owner records a later action outcome and independent observation with typed metrics",
        "measured": "owner consumes measured effect and decides no-change, revision, defer, supersession, or retirement",
        "deferred": "owner waits for the exact return trigger; due does not authorize action",
        "retired": "no automatic action; preserve history and use the explicit return trigger for reviewed reconsideration",
        "superseded": "use the reviewed successor reference; preserve this candidate history",
    }[state_value]


def upsert_candidate(
    db_path: os.PathLike[str] | str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically append one event and replace its current projection under CAS."""

    input_payload_json = _canonical_json(dict(event))
    normalized = _normalize_event(event)
    payload_json = _canonical_json(normalized)
    payload_sha256 = _sha256_text(input_payload_json)
    connection = _connect(db_path, access="rw")
    commit_status = "not_committed"
    transaction_started = False
    try:
        _require_initialized(connection)
        _begin_immediate(connection)
        transaction_started = True
        existing_event = connection.execute(
            "SELECT payload_sha256, payload_json, result_json FROM queue_events WHERE event_id = ?",
            (normalized["event_id"],),
        ).fetchone()
        if existing_event is not None:
            if (
                existing_event["payload_sha256"] != payload_sha256
                or existing_event["payload_json"] != input_payload_json
            ):
                raise QueueError(
                    "EVENT_ID_CONFLICT",
                    "event_id already exists with a different payload",
                    details={"event_id": normalized["event_id"]},
                )
            try:
                stored_result = json.loads(existing_event["result_json"])
            except json.JSONDecodeError as exc:
                raise QueueError(
                    "STORED_RESULT_CORRUPT",
                    f"stored idempotent result is not valid JSON: {exc}",
                ) from exc
            connection.commit()
            commit_status = "committed"
            return stored_result

        artifact_to_verify = normalized.get("artifact_ref")
        if normalized["state"] in {"prepared", "evaluated", "active-bounded", "measured"}:
            assert isinstance(artifact_to_verify, dict)
            verified_path, verified_sha256 = _verify_artifact(
                artifact_to_verify["path"], artifact_to_verify["sha256"]
            )
            artifact_to_verify["path"] = verified_path
            artifact_to_verify["sha256"] = verified_sha256
            payload_json = _canonical_json(normalized)

        current = connection.execute(
            "SELECT * FROM candidate_current WHERE candidate_id = ?",
            (normalized["candidate_id"],),
        ).fetchone()
        actual_revision = int(current["revision"]) if current is not None else 0
        if normalized["expected_revision"] != actual_revision:
            raise QueueError(
                "STALE_REVISION",
                "expected_revision does not match the current candidate revision",
                details={
                    "candidate_id": normalized["candidate_id"],
                    "expected_revision": normalized["expected_revision"],
                    "actual_revision": actual_revision,
                },
            )
        from_state = str(current["state"]) if current is not None else None
        if normalized["state"] not in LEGAL_TRANSITIONS[from_state]:
            raise QueueError(
                "ILLEGAL_TRANSITION",
                f"illegal transition: {from_state or '<new>'}->{normalized['state']}",
            )
        if current is not None:
            _validate_identity_continuity(current, normalized)
        _validate_graph_references(connection, normalized)

        new_revision = actual_revision + 1
        artifact = normalized.get("artifact_ref") or {}
        result = {
            "ok": True,
            "operation": "upsert",
            "event_id": normalized["event_id"],
            "candidate_id": normalized["candidate_id"],
            "prior_revision": actual_revision,
            "revision": new_revision,
            "from_state": from_state,
            "state": normalized["state"],
            "commit_status": "committed",
            "authority_granted": False,
            "semantic_validation": "not_performed",
            "next_action_for_owner": _next_action(normalized["state"]),
            "proof_ceiling": "transactional identity, reference, lifecycle, and projection integrity only",
        }
        result_json = _canonical_json(result)
        connection.execute(
            "INSERT INTO queue_events(event_id,candidate_id,payload_sha256,from_state,to_state,"
            "prior_revision,new_revision,payload_json,result_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                normalized["event_id"],
                normalized["candidate_id"],
                payload_sha256,
                from_state,
                normalized["state"],
                actual_revision,
                new_revision,
                input_payload_json,
                result_json,
            ),
        )
        projection_values = (
            new_revision,
            normalized["state"],
            normalized["owner_ref"],
            normalized["objective_ref"],
            normalized["project_event_ref"],
            normalized["global_disposition_ref"],
            _canonical_json(normalized["family_keys"]),
            artifact.get("path"),
            artifact.get("sha256"),
            normalized["next_consumer_ref"],
            normalized["next_use_trigger"],
            normalized.get("return_trigger"),
            normalized.get("successor_candidate_id"),
            payload_json,
            normalized["event_id"],
        )
        if current is None:
            connection.execute(
                "INSERT INTO candidate_current(candidate_id,revision,state,owner_ref,objective_ref,"
                "project_event_ref,global_disposition_ref,family_keys_json,artifact_path,artifact_sha256,"
                "next_consumer_ref,next_use_trigger,return_trigger,successor_candidate_id,payload_json,last_event_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (normalized["candidate_id"], *projection_values),
            )
        else:
            cursor = connection.execute(
                "UPDATE candidate_current SET revision=?,state=?,owner_ref=?,objective_ref=?,project_event_ref=?,"
                "global_disposition_ref=?,family_keys_json=?,artifact_path=?,artifact_sha256=?,next_consumer_ref=?,"
                "next_use_trigger=?,return_trigger=?,successor_candidate_id=?,payload_json=?,last_event_id=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE candidate_id=? AND revision=?",
                (*projection_values, normalized["candidate_id"], actual_revision),
            )
            if cursor.rowcount != 1:
                raise QueueError("CAS_LOST", "candidate projection changed before compare-and-swap update")
        connection.commit()
        commit_status = "committed"
        return result
    except QueueError as exc:
        if connection.in_transaction:
            exc.commit_status = _rollback_status(connection)
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            status = _rollback_status(connection)
        elif transaction_started and commit_status != "committed":
            status = "unknown"
        else:
            status = commit_status
        raise QueueError("DB_MUTATION_FAILED", f"queue transaction failed: {exc}", commit_status=status) from exc
    finally:
        connection.close()


def _status_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    predecessor_rows = connection.execute(
        "SELECT candidate_id FROM candidate_current WHERE successor_candidate_id = ? ORDER BY candidate_id",
        (row["candidate_id"],),
    ).fetchall()
    return {
        "ok": True,
        "operation": "status",
        "candidate_id": row["candidate_id"],
        "revision": row["revision"],
        "state": row["state"],
        "last_event_id": row["last_event_id"],
        "payload": payload,
        "reverse_predecessor_ids": [item[0] for item in predecessor_rows],
        "authority_granted": False,
        "semantic_validation": "not_performed",
        "next_action_for_owner": _next_action(row["state"]),
        "proof_ceiling": "stored references and graph integrity only",
    }


def _projection_snapshot_sha256(connection: sqlite3.Connection) -> str:
    frontier = [
        [row["candidate_id"], row["revision"], row["last_event_id"]]
        for row in connection.execute(
            "SELECT candidate_id,revision,last_event_id FROM candidate_current ORDER BY candidate_id"
        )
    ]
    return _sha256_text(_canonical_json(frontier))


def _encode_due_cursor(query_sha256: str, snapshot_sha256: str, after_candidate_id: str) -> str:
    payload = _canonical_json(
        {
            "version": 1,
            "query_sha256": query_sha256,
            "snapshot_sha256": snapshot_sha256,
            "after_candidate_id": after_candidate_id,
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_due_cursor(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise QueueError("INVALID_DUE_CURSOR", "cursor must be a bounded non-empty string")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueueError("INVALID_DUE_CURSOR", f"cursor is malformed: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "query_sha256",
        "snapshot_sha256",
        "after_candidate_id",
    }:
        raise QueueError("INVALID_DUE_CURSOR", "cursor has an unsupported shape")
    if payload.get("version") != 1:
        raise QueueError("INVALID_DUE_CURSOR", "cursor version is unsupported")
    _require_sha256(payload.get("query_sha256"), "cursor.query_sha256")
    _require_sha256(payload.get("snapshot_sha256"), "cursor.snapshot_sha256")
    _require_identifier(payload.get("after_candidate_id"), "cursor.after_candidate_id")
    return payload


def get_status(db_path: os.PathLike[str] | str, candidate_id: str) -> dict[str, Any]:
    candidate_id = _require_identifier(candidate_id, "candidate_id")
    connection = _connect(db_path, access="ro")
    try:
        _begin_read(connection)
        _require_initialized(connection)
        row = connection.execute(
            "SELECT * FROM candidate_current WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise QueueError("CANDIDATE_NOT_FOUND", f"candidate does not exist: {candidate_id}")
        result = _status_from_row(connection, row)
        result["snapshot_sha256"] = _projection_snapshot_sha256(connection)
        connection.commit()
        return result
    except QueueError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise QueueError("DB_READ_FAILED", f"cannot read candidate status: {exc}") from exc
    finally:
        connection.close()


def _read_candidate_events(
    connection: sqlite3.Connection, candidate_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT event_id,payload_sha256,from_state,to_state,prior_revision,new_revision,payload_json,committed_at "
        "FROM queue_events WHERE candidate_id = ? ORDER BY new_revision",
        (candidate_id,),
    ).fetchall()


def get_history(db_path: os.PathLike[str] | str, candidate_id: str) -> dict[str, Any]:
    candidate_id = _require_identifier(candidate_id, "candidate_id")
    connection = _connect(db_path, access="ro")
    try:
        _begin_read(connection)
        _require_initialized(connection)
        current = connection.execute(
            "SELECT revision FROM candidate_current WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if current is None:
            raise QueueError("CANDIDATE_NOT_FOUND", f"candidate does not exist: {candidate_id}")
        rows = _read_candidate_events(connection, candidate_id)
        result = {
            "ok": True,
            "operation": "history",
            "candidate_id": candidate_id,
            "revision": current["revision"],
            "events": [
                {
                    "event_id": row["event_id"],
                    "payload_sha256": row["payload_sha256"],
                    "from_state": row["from_state"],
                    "state": row["to_state"],
                    "prior_revision": row["prior_revision"],
                    "revision": row["new_revision"],
                    "payload": json.loads(row["payload_json"]),
                    "committed_at": row["committed_at"],
                }
                for row in rows
            ],
            "authority_granted": False,
            "semantic_validation": "not_performed",
            "proof_ceiling": "immutable stored event history only",
        }
        result["snapshot_sha256"] = _projection_snapshot_sha256(connection)
        connection.commit()
        return result
    except QueueError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise QueueError("DB_READ_FAILED", f"cannot read candidate history: {exc}") from exc
    finally:
        connection.close()


def find_due(
    db_path: os.PathLike[str] | str,
    *,
    objective_ref: str,
    family_key: str,
    trigger: str,
    limit: int = DEFAULT_DUE_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return exact-context matches without ranking, authorization, or execution."""

    objective_ref = _require_reference(objective_ref, "objective_ref")
    family_key = _require_reference(family_key, "family_key")
    trigger = _require_reference(trigger, "trigger")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_DUE_LIMIT:
        raise QueueError("INVALID_DUE_LIMIT", f"limit must be an integer from 1 to {MAX_DUE_LIMIT}")
    connection = _connect(db_path, access="ro")
    try:
        _begin_read(connection)
        _require_initialized(connection)
        query = {
            "objective_ref": objective_ref,
            "family_key": family_key,
            "trigger": trigger,
        }
        query_sha256 = _sha256_text(_canonical_json(query))
        snapshot_sha256 = _projection_snapshot_sha256(connection)
        after_candidate_id: str | None = None
        if cursor is not None:
            cursor_payload = _decode_due_cursor(cursor)
            if cursor_payload["query_sha256"].upper() != query_sha256:
                raise QueueError("CURSOR_QUERY_MISMATCH", "cursor is bound to a different due query")
            if cursor_payload["snapshot_sha256"].upper() != snapshot_sha256:
                raise QueueError(
                    "CURSOR_SNAPSHOT_CHANGED",
                    "candidate projection changed after the previous due page; restart pagination",
                    details={
                        "cursor_snapshot_sha256": cursor_payload["snapshot_sha256"].upper(),
                        "current_snapshot_sha256": snapshot_sha256,
                    },
                )
            after_candidate_id = cursor_payload["after_candidate_id"]
        rows = connection.execute(
            "SELECT * FROM candidate_current WHERE objective_ref = ? ORDER BY candidate_id",
            (objective_ref,),
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            if row["state"] not in ACTIONABLE_STATES:
                continue
            payload = json.loads(row["payload_json"])
            if family_key not in payload["family_keys"]:
                continue
            required_trigger = payload.get("return_trigger") if row["state"] == "deferred" else payload["next_use_trigger"]
            if required_trigger != trigger:
                continue
            matches.append(
                {
                    "candidate_id": row["candidate_id"],
                    "revision": row["revision"],
                    "state": row["state"],
                    "owner_ref": row["owner_ref"],
                    "next_consumer_ref": row["next_consumer_ref"],
                    "matched_trigger": required_trigger,
                    "evidence_refs": payload["evidence_refs"],
                    "rollback_refs": payload["rollback_refs"],
                    "next_action_for_owner": _next_action(row["state"]),
                    "authority_granted": False,
                }
            )
        total = len(matches)
        start = 0
        if after_candidate_id is not None:
            ids = [item["candidate_id"] for item in matches]
            if after_candidate_id not in ids:
                raise QueueError(
                    "CURSOR_POSITION_INVALID",
                    "cursor position is not a member of this exact due result",
                )
            start = ids.index(after_candidate_id) + 1
        returned = matches[start : start + limit]
        remaining = total - start - len(returned)
        next_cursor = None
        if remaining > 0 and returned:
            next_cursor = _encode_due_cursor(
                query_sha256,
                snapshot_sha256,
                returned[-1]["candidate_id"],
            )
        result = {
            "ok": True,
            "operation": "due",
            "query": query,
            "query_sha256": query_sha256,
            "snapshot_sha256": snapshot_sha256,
            "matches": returned,
            "match_count": total,
            "returned_count": len(returned),
            "prior_page_count": start,
            "remaining_count": remaining,
            "omitted_count": total - len(returned),
            "has_more": remaining > 0,
            "next_cursor": next_cursor,
            "selection_method": "exact objective_ref + exact family_key + exact state-relative trigger; candidate_id order",
            "authority_granted": False,
            "semantic_validation": "not_performed",
            "proof_ceiling": "reference match only; due is not permission or an action instruction",
        }
        connection.commit()
        return result
    except QueueError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise QueueError("DB_READ_FAILED", f"cannot select due candidates: {exc}") from exc
    finally:
        connection.close()


def _load_event(path_value: str) -> dict[str, Any]:
    path = Path(path_value).resolve(strict=True)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QueueError("INPUT_READ_FAILED", f"cannot read event JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueError("INVALID_EVENT", "event JSON root must be an object")
    return value


def _emit_json(result: Mapping[str, Any]) -> None:
    """Serialize only after commit; preserve status if publication itself fails."""

    status = str(result.get("commit_status", "unknown"))
    if status not in {"committed", "not_committed", "unknown"}:
        status = "unknown"
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
    except Exception as exc:  # stdout/encoding failures are outside SQLite's transaction
        message = json.encoder.encode_basestring_ascii(f"post-commit result publication failed: {exc}")
        fallback = (
            '{"ok":false,"error_code":"POST_COMMIT_PUBLICATION_FAILED",'
            f'"commit_status":"{status}","authority_granted":false,"first_fault":{message}}}\n'
        )
        try:
            sys.stderr.write(fallback)
            sys.stderr.flush()
        finally:
            raise QueueError(
                "POST_COMMIT_PUBLICATION_FAILED",
                f"post-commit result publication failed: {exc}",
                commit_status=status,
            ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--db", required=True)
    upsert_parser = subparsers.add_parser("upsert")
    upsert_parser.add_argument("--db", required=True)
    upsert_parser.add_argument("--input", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--db", required=True)
    status_parser.add_argument("--candidate-id", required=True)
    history_parser = subparsers.add_parser("history")
    history_parser.add_argument("--db", required=True)
    history_parser.add_argument("--candidate-id", required=True)
    due_parser = subparsers.add_parser("due")
    due_parser.add_argument("--db", required=True)
    due_parser.add_argument("--objective-ref", required=True)
    due_parser.add_argument("--family-key", required=True)
    due_parser.add_argument("--trigger", required=True)
    due_parser.add_argument("--limit", type=int, default=DEFAULT_DUE_LIMIT)
    due_parser.add_argument("--cursor")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_db(args.db)
        elif args.command == "upsert":
            result = upsert_candidate(args.db, _load_event(args.input))
        elif args.command == "status":
            result = get_status(args.db, args.candidate_id)
        elif args.command == "history":
            result = get_history(args.db, args.candidate_id)
        else:
            result = find_due(
                args.db,
                objective_ref=args.objective_ref,
                family_key=args.family_key,
                trigger=args.trigger,
                limit=args.limit,
                cursor=args.cursor,
            )
        _emit_json(result)
        return 0
    except QueueError as exc:
        if exc.code == "POST_COMMIT_PUBLICATION_FAILED":
            return 3
        try:
            _emit_json(exc.as_result())
        except QueueError:
            return 3
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
