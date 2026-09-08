#!/usr/bin/env python3
"""Build owner-authored ledger inputs with the exact runtime's read-only validators.

No journal append, lock, projection repair, retry, or source classification occurs.
The owner still supplies the explicit chat/head, real facts, and action authority;
the unchanged runtime CLI performs its own current-head validation when applying.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
from typing import Any


EVENT_TYPES = {"progress": "progress", "action-start": "action_started", "action-outcome": "action_outcome"}
HASH = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def _hash(value: str, name: str) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError(f"{name} must be an explicit SHA-256")
    return value.upper()


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError(f"duplicate JSON field: {name}")
            result[name] = value
        return result
    def constant(value):
        raise ValueError(f"non-finite JSON value: {value}")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def load_runtime(path: str | Path, expected_sha256: str) -> types.ModuleType:
    """Import only the bytes explicitly selected and hash-bound by the caller."""
    source = Path(path).resolve(strict=True)
    expected = _hash(expected_sha256, "runtime_sha256")
    raw = source.read_bytes()
    if _sha(raw) != expected:
        raise ValueError("runtime hash differs from the reviewed explicit runtime")
    module = types.ModuleType("ledger_input_runtime")
    module.__file__ = str(source)
    exec(compile(raw, str(source), "exec"), module.__dict__)
    return module


class LedgerInputs:
    """One explicit owner/head binding; a stale instance never silently refreshes.

    progress(), action_start(), and action_outcome() accept the unchanged runtime's
    supported keyword fields and return input dictionaries only. For progress and
    action-start, an omitted contract_id is filled from this exact replay. Explicit
    incompatible contract IDs are rejected, not replaced. No old progress fields
    or historical evidence list is copied into a new payload.
    """

    def __init__(self, *, runtime_path: str | Path, runtime_sha256: str,
                 config_path: str | Path, logical_chat_id: str, expected_head: str):
        self.runtime_path = Path(runtime_path).resolve(strict=True)
        self.runtime_sha256 = _hash(runtime_sha256, "runtime_sha256")
        self.runtime = load_runtime(self.runtime_path, self.runtime_sha256)
        self.config_path = Path(config_path).resolve(strict=True)
        self.config_sha256 = _sha(self.config_path.read_bytes())
        self.expected_head = _hash(expected_head, "expected_head")
        self.logical_chat_id = self.runtime.require_safe_id(logical_chat_id, "logical_chat_id")
        self.config = self.runtime.select_ledger_config(
            self.runtime.load_config(self.config_path), self.logical_chat_id)
        self.ledger_root = self.runtime.ledger_dir(self.config).resolve(strict=True)
        self.observations: dict[str, Any] = {}
        self._prepared: tuple[str, str] | None = None
        self._snapshot()  # Validate the explicit existing binding, without creating it.

    def _bindings_current(self) -> None:
        if _sha(self.runtime_path.read_bytes()) != self.runtime_sha256:
            raise ValueError("runtime changed after the explicit binding")
        if _sha(self.config_path.read_bytes()) != self.config_sha256:
            raise ValueError("config changed after the explicit binding")
        identity = self.runtime.load_json_file(self.ledger_root / "identity.json")
        if identity != self.runtime.identity_document(self.config):
            raise ValueError("existing ledger identity differs from the explicit chat/config")

    def _snapshot(self):
        self._bindings_current()
        journal = self.runtime.journal_path(self.config)
        before = _sha(journal.read_bytes())
        # read_state(repair=False) still writes identity/lock/projections. Its pure
        # components are used instead; partial tails are rejected and untouched.
        records, notes = self.runtime.scan_journal(self.config, repair_partial=False)
        state = self.runtime.attach_runtime_frontiers(self.config, self.runtime.replay(self.config, records))
        after = _sha(journal.read_bytes())
        if before != after:
            raise ValueError("journal changed during read-only input preparation")
        if state["head_hash"] != self.expected_head:
            raise ValueError("stale expected_head; no automatic refresh or retry")
        self._bindings_current()
        return records, state, after, notes

    def build(self, command: str, fields: dict[str, Any]) -> dict[str, Any]:
        if command not in EVENT_TYPES:
            raise ValueError("only progress, action-start and action-outcome are supported")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        # A JSON roundtrip detaches nested caller objects and rejects Python-only
        # values; it never rewrites enum aliases, booleans or effect states.
        payload = _json(json.dumps(fields, ensure_ascii=True, allow_nan=False).encode("ascii"))
        records, state, journal_sha, notes = self._snapshot()
        if command in {"progress", "action-start"} and "contract_id" not in payload:
            payload["contract_id"] = state["current_contract_id"]
        event_type = EVENT_TYPES[command]
        normalized = self.runtime.normalize_proposed_transition(self.config, state, event_type, payload)
        # The real append path also replays the proposed record. Normalization
        # alone does not validate progress statuses or terminal action effects.
        record = {"schema": self.runtime.SCHEMA, "seq": len(records) + 1,
                  "event_id": "INPUT-PREFLIGHT-" + self.runtime.sha256_json(normalized)[:24],
                  "event_type": event_type, "actor_role": "root", "timestamp_utc": self.runtime.utc_now(),
                  "prev_hash": self.expected_head, "payload": normalized}
        record["event_hash"] = self.runtime.event_hash(record)
        candidate = self.runtime.attach_runtime_frontiers(
            self.config, self.runtime.replay(self.config, [*records, record]))
        if event_type in {"progress", "action_started"}:
            self.runtime.require_sources_available(candidate, self.runtime.current_contract_source_ids(candidate))
        # action-outcome deliberately keeps the runtime's source-unavailable
        # conservation path: recording an observed/unknown effect is not blocked
        # by a new source-readiness rule introduced by this adapter.
        if candidate["current_contract_id"] != state["current_contract_id"]:
            raise ValueError("input unexpectedly changes the objective contract")
        if set(candidate["open_outcomes"]) != set(state["open_outcomes"]):
            raise ValueError("input unexpectedly changes the outcome inventory")
        if command != "progress" and candidate["open_outcomes"] != state["open_outcomes"]:
            raise ValueError("action metadata unexpectedly changes outcome state")
        current_records, current, current_sha, _ = self._snapshot()
        if current_sha != journal_sha or current_records != records:
            raise ValueError("journal changed before input output; no automatic retry")
        if event_type in {"progress", "action_started"}:
            self.runtime.require_sources_available(current, self.runtime.current_contract_source_ids(current))
        self.observations = {
            "runtime_sha256": self.runtime_sha256, "config_sha256": self.config_sha256,
            "logical_chat_id": self.logical_chat_id, "namespace": self.config["namespace"],
            "expected_head": self.expected_head, "contract_id": state["current_contract_id"],
            "journal_sha256": journal_sha, "unproven_source_ids": current["unproven_source_ids"],
            "unclassified_source_ids": current["unclassified_source_ids"],
            "unresolved_capture_gap_ids": current["unresolved_capture_gap_ids"],
            "unknown_effect_action_ids": current["unknown_effect_action_ids"],
            "notes": notes, "journal_applied": False, "permission_granted": False,
            "proof_ceiling": "read-only runtime normalization/replay at an explicit head; source meaning, real effects and later CLI application remain separate",
        }
        self._prepared = (command, self.runtime.sha256_json(normalized))
        return copy.deepcopy(normalized)

    def progress(self, **fields: Any) -> dict[str, Any]:
        return self.build("progress", fields)

    def action_start(self, **fields: Any) -> dict[str, Any]:
        return self.build("action-start", fields)

    def action_outcome(self, **fields: Any) -> dict[str, Any]:
        return self.build("action-outcome", fields)

    def write_input(self, output_path: str | Path, payload: dict[str, Any]) -> None:
        """Write the last prepared input outside the ledger; never overwrite."""
        if self._prepared is None or self.runtime.sha256_json(payload) != self._prepared[1]:
            raise ValueError("output must be the unchanged last validated input")
        payload = self.build(self._prepared[0], payload)
        path = Path(os.path.abspath(os.fspath(output_path)))
        for part in (*reversed(path.parents), path):
            try:
                info = part.lstat()
            except FileNotFoundError:
                if part != path:
                    raise
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("output path contains a link or reparse point")
        resolved = path.resolve(strict=False)
        if resolved == self.ledger_root or self.ledger_root in resolved.parents:
            raise ValueError("input output must not write inside the ledger")
        raw = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
        self._snapshot()  # The eventual runtime CLI still owns its apply-time CAS.
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(EVENT_TYPES))
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--logical-chat-id", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--fields", default="-", help="explicit owner field object as UTF-8 JSON file, or - for stdin")
    parser.add_argument("--output", help="new JSON input path outside the ledger; otherwise payload goes to stdout")
    args = parser.parse_args()
    try:
        context = LedgerInputs(runtime_path=args.runtime, runtime_sha256=args.runtime_sha256,
                               config_path=args.config, logical_chat_id=args.logical_chat_id,
                               expected_head=args.expected_head)
        raw = sys.stdin.buffer.read() if args.fields == "-" else Path(args.fields).read_bytes()
        payload = context.build(args.command, _json(raw))
        if args.output:
            context.write_input(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "INPUT_REJECTED", "journal_applied": False,
                          "error": f"{type(error).__name__}: {error}"}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
