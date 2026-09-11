#!/usr/bin/env python3
"""Build owner-authored ledger inputs with the exact runtime's read-only validators.

Preparation is read-only. Explicit --apply commits validated source/progress/action
inputs through the existing runtime and reads back; no implicit retry occurs.
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


EVENT_TYPES = {"progress": "progress", "action-start": "action_started", "action-outcome": "action_outcome", "classify-source": "source_classified"}
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
                 config_path: str | Path, logical_chat_id: str, expected_head: str | None = None):
        self.runtime_path = Path(runtime_path).resolve(strict=True)
        self.runtime_sha256 = _hash(runtime_sha256, "runtime_sha256")
        self.runtime = load_runtime(self.runtime_path, self.runtime_sha256)
        self.config_path = Path(config_path).resolve(strict=True)
        self.config_sha256 = _sha(self.config_path.read_bytes())
        self.expected_head = _hash(expected_head, "expected_head") if expected_head is not None else None
        self.logical_chat_id = self.runtime.require_safe_id(logical_chat_id, "logical_chat_id")
        self.config = self.runtime.select_ledger_config(
            self.runtime.load_config(self.config_path), self.logical_chat_id)
        self.ledger_root = self.runtime.ledger_dir(self.config).resolve(strict=True)
        self.observations: dict[str, Any] = {}
        self._prepared: tuple[str, str] | None = None
        self.apply_phase = "not_started"
        self.applied_receipt = None
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
        if self.expected_head is not None and state["head_hash"] != self.expected_head:
            raise ValueError("stale expected_head; no automatic refresh or retry")
        self._bindings_current()
        return records, state, after, notes

    def build(self, command: str, fields: dict[str, Any]) -> dict[str, Any]:
        if self.expected_head is None:
            raise ValueError("mutation preparation requires an explicit observed expected_head")
        if command not in EVENT_TYPES:
            raise ValueError("unsupported ledger input command")
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
        if event_type in {"progress", "action_started", "source_classified"}:
            self.runtime.require_sources_available(candidate, self.runtime.current_contract_source_ids(candidate))
        # action-outcome deliberately keeps the runtime's source-unavailable
        # conservation path: recording an observed/unknown effect is not blocked
        # by a new source-readiness rule introduced by this adapter.
        if command != "classify-source" and candidate["current_contract_id"] != state["current_contract_id"]:
            raise ValueError("input unexpectedly changes the objective contract")
        if command != "classify-source" and set(candidate["open_outcomes"]) != set(state["open_outcomes"]):
            raise ValueError("input unexpectedly changes the outcome inventory")
        if command not in {"progress", "classify-source"} and candidate["open_outcomes"] != state["open_outcomes"]:
            raise ValueError("action metadata unexpectedly changes outcome state")
        current_records, current, current_sha, _ = self._snapshot()
        if current_sha != journal_sha or current_records != records:
            raise ValueError("journal changed before input output; no automatic retry")
        if event_type in {"progress", "action_started", "source_classified"}:
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

    def _host_binding(self) -> dict[str, Any]:
        spec = self.config.get("host_binding") if isinstance(self.config, dict) else None
        if not isinstance(spec, dict):
            return {"status": "UNAVAILABLE", "reason": "no host_binding configured; prepare/reference reads only"}
        name, where = spec.get("session_env"), spec.get("bindings_dir")
        if not isinstance(name, str) or not name or not isinstance(where, str) or not where:
            return {"status": "UNAVAILABLE", "reason": "host_binding requires session_env and bindings_dir"}
        session = os.environ.get(name, "")
        if not session:
            return {"status": "UNAVAILABLE", "reason": "host session absent; reference read only"}
        root = Path(where)
        if not root.is_absolute():
            root = self.config_path.parent / root
        binding_file = root / (hashlib.sha256(str(session).encode()).hexdigest() + ".json")
        if not binding_file.exists():
            return {"status": "UNBOUND_OR_CHILD", "reason": "No host source-event binding"}
        binding = _json(binding_file.read_bytes())
        own = binding.get("role") == "root" and binding.get("session_id") == self.logical_chat_id == session
        return {"status": "OWN" if own else "REFERENCE_ONLY", "reason": "Host session binding; semantic source authority remains separate"}

    def owner_view(self) -> dict[str, Any]:
        """Complete current decision data once; exact identity comes from config, not cwd."""
        _, state, _, _ = self._snapshot()
        return self._owner_view(state)

    def _owner_view(self, state: dict[str, Any]) -> dict[str, Any]:
        binding = self._host_binding()
        own = binding["status"] == "OWN"
        contract = state["contracts"].get(state["current_contract_id"] or "")
        bindings: dict[str, Any] = {}
        clauses = []
        for clause in (contract or {}).get("clauses", []):
            sid = clause["source_event_id"]
            bindings[sid] = {k: state["sources"][sid][k] for k in ("source_ref", "source_sha256")}
            clauses.append({k: v for k, v in clause.items() if k not in {"source_ref", "source_sha256"}})
        return {
            "schema": "ledger-owner-view-v1",
            "host_binding": binding,
            "identity": copy.deepcopy(state["identity"]),
            "ledger_root": str(self.ledger_root),
            "target_projection": str(self.runtime.projection_path(self.config)),
            "projection_role": "OWN" if own else "REFERENCE_ONLY",
            "revision": state["revision"], "head_hash": state["head_hash"],
            "current_contract_id": state["current_contract_id"],
            "normative": {k: copy.deepcopy(v) for k, v in (contract or {}).items()
                          if k in self.runtime.NORMATIVE_CONTRACT_FIELDS},
            "source_dictionary": bindings, "clauses": clauses,
            "active_outcomes": [copy.deepcopy(v) for v in state["open_outcomes"].values()
                                if v.get("status") not in {"SATISFIED", "USER_WITHDRAWN", "SUPERSEDED"}],
            "pending_sources": [copy.deepcopy(state["sources"][k]) for k in state["unclassified_source_ids"]],
            "pending_actions": {k: copy.deepcopy(state["actions"][k]) for k in state["unknown_effect_action_ids"]},
            "capture_gaps": copy.deepcopy(state.get("capture_gaps", {})),
            "unresolved_capture_gap_ids": state["unresolved_capture_gap_ids"],
            "unproven_source_ids": state["unproven_source_ids"],
            "latest_progress": copy.deepcopy(state.get("latest_progress")),
            "ownership_rule": ("The inherited host routing identifies this as your ledger. Update it through its CLI; never edit the generated card. Other task objectives remain references, not project-local copies." if own else "This selected target is reference-only: own host routing is different or unproven. Do not adopt/copy its objective or update it as your own. Resolve your host-bound ledger separately."),
            "master_write_rule": "Under the existing workflow, the primary owner writes its own objective/Project and scoped sanitized Global updates with history, CAS and required backup. Shared does not mean read-only. Independent reviewers/children return candidates; they do not write shared semantics. Preserve other tasks' CURRENT CONTROL and explicit source restrictions.",
            "permission_granted": False,
            "execution_rule": "Record start before the operation, consume its actual result, then execute the next eligible work. Intermediate findings belong in commentary; a next-step promise does not continue an ended turn. Use response-check for an explicit current-request disposition, never as host scheduling or permission.",
            "proof_ceiling": "Bound current data and standing-rule reminder, not actor authentication, new authority, semantic compliance or all-tool enforcement.",
        }

    def source_update(self, **fields: Any) -> dict[str, Any]:
        """Build one explicit incremental source delta from this ledger only."""
        allowed = {"source_event_id", "contract_id", "clause_id", "disposition", "changes",
                   "new_outcomes", "outcome_transitions", "classification_note", "locator"}
        if set(fields) - allowed:
            raise ValueError("source-update accepts a delta, not copied contracts/source paths: " + str(sorted(set(fields) - allowed)))
        _, state, _, _ = self._snapshot()
        sid = self.runtime.require_safe_id(fields.get("source_event_id"), "source_event_id")
        if sid not in state["unclassified_source_ids"]:
            raise ValueError("source is not pending in this own ledger; foreign/already-consumed source refused")
        self.runtime.require_sources_available(state, [sid])
        disposition = fields.get("disposition")
        if disposition not in {"INITIAL", "ADD", "CLARIFY", "CORRECT"}:
            raise ValueError("incremental source-update supports INITIAL/ADD/CLARIFY/CORRECT; use explicit existing classify-source for REPLACE/WITHDRAW")
        changes = fields.get("changes", {})
        if not isinstance(changes, dict) or set(changes) - self.runtime.NORMATIVE_CONTRACT_FIELDS:
            raise ValueError("changes must contain only supported normative fields")
        additions = fields.get("new_outcomes", [])
        if not isinstance(additions, list):
            raise ValueError("new_outcomes must be a list")
        for item in additions:
            if not isinstance(item, dict) or item.get("outcome_id") in state["outcome_catalog"]:
                raise ValueError("new_outcomes cannot rewrite an existing outcome; use explicit progress/transition")
        parent = state["contracts"].get(state["current_contract_id"] or "")
        if (disposition == "INITIAL") != (parent is None):
            raise ValueError("INITIAL must have no active parent; no implicit replacement")
        src = state["sources"][sid]
        clause = dict(clause_id=fields.get("clause_id"), source_event_id=sid,
                      source_ref=src["source_ref"], source_sha256=src["source_sha256"])
        if fields.get("locator") is not None:
            clause["locator"] = fields["locator"]
        contract = dict(contract_id=fields.get("contract_id"),
                        clauses=copy.deepcopy((parent or {}).get("clauses", [])) + [clause],
                        open_outcomes=copy.deepcopy(list(state["open_outcomes"].values())) + copy.deepcopy(additions),
                        **copy.deepcopy(changes))
        if parent:
            contract["parent_contract_id"] = state["current_contract_id"]
        payload = dict(source_event_id=sid, disposition=disposition, contract=contract,
                       classification_note=fields.get("classification_note"))
        if fields.get("outcome_transitions") is not None:
            contract["outcome_transitions"] = copy.deepcopy(fields["outcome_transitions"])
        if parent:
            payload["normative_transitions"] = [dict(field=k, operation=disposition,
                source_clause_ids=[clause["clause_id"]]) for k, v in changes.items() if parent.get(k) != v]
        return self.build("classify-source", payload)

    def apply_source_update(self, payload: dict[str, Any], *, event_id: str) -> dict[str, Any]:
        """Commit only the exact validated source delta; no implicit head refresh/retry."""
        return self.apply_prepared("classify-source", payload, event_id=event_id)

    def apply_prepared(self, command: str, payload: dict[str, Any], *, event_id: str) -> dict[str, Any]:
        """One prepare/apply caller; rejected preparation never reaches append."""
        if command not in EVENT_TYPES or self._prepared != (command, self.runtime.sha256_json(payload)):
            raise ValueError("apply requires the unchanged prepared input")
        if self._host_binding()["status"] != "OWN":
            raise ValueError("apply requires the inherited host own-ledger binding; reference reads remain available")
        payload = self.build(command, payload)
        self.apply_phase = "append_entered"
        result = self.runtime.append_event(self.config, EVENT_TYPES[command], event_id, payload,
                                           "root", self.expected_head)
        self.apply_phase = "committed"
        self.applied_receipt = {"event_id": result["record"]["event_id"], "head_hash": result["state"]["head_hash"]}
        state = result["state"]
        # Appending may already have effects. Any readback failure must propagate
        # as committed/uncertain, never as a clean no-write preparation rejection.
        try:
            current = self.runtime.load_json_file(self.ledger_root / "current.json")
            if current != state:
                raise ValueError("current projection changed or differs after commit")
            if (self.runtime.projection_path(self.config)).read_text(encoding="utf-8") != self.runtime.render_projection(state):
                raise ValueError("own card readback differs")
            return {"status": result["status"], "journal_applied": True,
                    "event_id": result["record"]["event_id"], "owner_view": self._owner_view(state),
                    "readback_verified": True, "permission_granted": False}
        except Exception as error:
            raise self.runtime.PostCommitError(str(error), result["record"]["event_id"],
                state["head_hash"], "JOURNAL_COMMITTED_READBACK_UNPROVEN") from error

    def run_operation(self, *, start: dict[str, Any], event_id: str,
                      runner_path: str, runner_sha256: str, spec_path: str,
                      output_root: str) -> dict[str, Any]:
        """Connect a reviewed local operation to the existing native capture runner.

        The runner and its dependency resources retain their normal Skill review.
        Scope is declared, not sandboxed. Never infer action completion from exit0.
        """
        runner = load_runtime(runner_path, runner_sha256)
        spec_file = runner.plain(spec_path)
        spec_raw = spec_file.read_bytes()
        spec_ref = {"path": str(spec_file), "sha256": _sha(spec_raw)}
        spec = runner.validate(runner.parse(spec_raw))
        root = runner.plain(output_root, kind="dir", absent=True)
        if root.exists():
            raise ValueError("operation evidence exists; inspect the prior attempt, never replay")
        if spec["owner_chat_id"] != self.logical_chat_id:
            raise ValueError("operation owner differs from the bound ledger")
        _, state, _, _ = self._snapshot()
        contract = state["contracts"][state["current_contract_id"]]
        clauses = [v for v in contract["clauses"] if v["clause_id"] in start.get("source_clause_ids", [])]
        sources = [{"path": str(self.ledger_root / v["source_ref"]), "sha256": v["source_sha256"]} for v in clauses]
        if not any(Path(v["path"]).resolve() == Path(spec["source"]["path"]).resolve()
                   and v["sha256"].upper() == spec["source"]["sha256"].upper() for v in sources):
            raise ValueError("operation source must be an exact own action clause source")
        for p in [self.ledger_root, Path(spec_path), Path(spec["source"]["path"]),
                  *[Path(v["path"]) for v in spec["methods"]]]:
            if root == p or root in p.parents or p == self.ledger_root and p in root.parents:
                raise ValueError("operation evidence overlaps ledger or bound input")
        fields = copy.deepcopy(start)
        if fields.get("action_input_sha256", spec_ref["sha256"]) != spec_ref["sha256"]:
            raise ValueError("action input differs from exact operation spec")
        fields["action_input_sha256"] = spec_ref["sha256"]
        payload = self.action_start(**fields)
        # No subprocess is reachable before successful append AND own readback.
        started = self.apply_prepared("action-start", payload, event_id=event_id)
        if runner.ref(spec_path) != spec_ref or _sha(Path(runner_path).read_bytes()) != runner_sha256.upper():
            raise ValueError("operation binding changed after start; action remains pending, no replay")
        result = runner.execute(spec_path, output_root, expected_spec_sha256=spec_ref["sha256"])
        return {"schema": "ledger-operation-v1", "start": started, "operation": result,
                "action_id": payload["action_id"], "action_outcome_required": True,
                "permission_granted": False,
                "proof_ceiling": "Start committed before one supported native call; result/effects retained, semantic action completion and next decision remain owner work."}

    def response_check(self, *, request_outcome_ids: list[str], excluded_active: dict[str, str],
                       source_clause_ids: list[str], purpose: str, next_action: dict[str, Any],
                       boundary: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read-only current-request disposition, not a Stop hook or scheduler."""
        _, state, _, _ = self._snapshot()
        if self._host_binding()["status"] != "OWN":
            raise ValueError("response decision requires own inherited host binding")
        contract = state["contracts"][state["current_contract_id"]]
        def ids(values, allowed):
            if not isinstance(values, list) or not values or any(not isinstance(v,str) for v in values) or len(values)!=len(set(values)) or not set(values)<=set(allowed):
                raise ValueError("explicit unique known IDs required")
        ids(request_outcome_ids, state["open_outcomes"])
        ids(source_clause_ids, [c["clause_id"] for c in contract["clauses"]])
        active = {k for k,v in state["open_outcomes"].items() if v["status"] not in {"SATISFIED","USER_WITHDRAWN","SUPERSEDED"}}
        scope = set(request_outcome_ids)
        if not isinstance(excluded_active,dict) or set(excluded_active)!=active-scope or any(not isinstance(v,str) or not v.strip() for v in excluded_active.values()):
            raise ValueError("all unrelated active outcomes need explicit source-bound exclusion reasons")
        if purpose not in {"progress","completion"}:
            raise ValueError("purpose must be progress or completion")
        if not isinstance(next_action,dict) or set(next_action)!={"eligible","description","evidence_refs"} or type(next_action["eligible"]) is not bool:
            raise ValueError("explicit next_action eligibility, description and evidence required")
        def evidence(value):
            return isinstance(value,list) and bool(value) and all(isinstance(v,str) and bool(v.strip()) for v in value)
        if not isinstance(next_action["description"],str) or not next_action["description"].strip() or not evidence(next_action["evidence_refs"]):
            raise ValueError("next action disposition requires reason and evidence references")
        pending = [k for k in state["unknown_effect_action_ids"] if scope.intersection(state["actions"][k].get("outcome_ids", []))]
        incomplete = sorted(active & scope)
        source_frontier = bool(state["unclassified_source_ids"] or state["unproven_source_ids"] or state["unresolved_capture_gap_ids"])
        decision = "CONTINUE_WORK";allowed = False
        if boundary is not None:
            if not isinstance(boundary,dict) or set(boundary)!={"kind","reason","evidence_refs"} or boundary["kind"] not in {"user_pause","permission_required","blocked","host_limit"} or not isinstance(boundary["reason"],str) or not boundary["reason"].strip() or not evidence(boundary["evidence_refs"]):
                raise ValueError("boundary requires a supported observed kind, reason and evidence")
            if next_action["eligible"] and boundary["kind"]!="user_pause":
                raise ValueError("a dependent boundary cannot stop independent eligible work")
            decision="REPORT_BOUNDARY";allowed=True
        elif purpose=="completion" and not incomplete and not pending and not source_frontier and not next_action["eligible"]:
            decision="REPORT_COMPLETION";allowed=True
        disposition = {"schema":"ledger-response-disposition-v1","head_hash":state["head_hash"],
                "contract_id":state["current_contract_id"],"decision":decision,"final_allowed":allowed,
                "request_outcome_ids":request_outcome_ids,"excluded_active":excluded_active,
                "incomplete_outcomes":incomplete,"pending_actions":pending,"source_frontier":source_frontier,
                "next_action":next_action,"boundary":boundary,"permission_granted":False,
                "proof_ceiling":"Owner-declared request, evidence and eligibility checked against current ledger; no semantic verification, host interception, continuation scheduling or all-model obedience."}
        disposition["receipt_appended"] = False
        return disposition

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
    parser.add_argument("command", choices=sorted([*EVENT_TYPES, "owner-view", "source-update", "response-check", "run-operation"]))
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--logical-chat-id", required=True)
    parser.add_argument("--expected-head", help="observed head required for preparation/apply; optional for read-only owner-view/response-check")
    parser.add_argument("--fields", default="-", help="explicit owner field object as UTF-8 JSON file, or - for stdin")
    parser.add_argument("--output", help="new JSON input path outside the ledger; otherwise payload goes to stdout")
    parser.add_argument("--apply", action="store_true", help="commit source-update/progress/action-start/action-outcome; required for run-operation")
    parser.add_argument("--event-id", help="explicit unique event ID for source-update --apply")
    args = parser.parse_args()
    context = None
    try:
        context = LedgerInputs(runtime_path=args.runtime, runtime_sha256=args.runtime_sha256,
                               config_path=args.config, logical_chat_id=args.logical_chat_id,
                               expected_head=args.expected_head)
        if args.command == "owner-view":
            if args.apply or args.output:
                raise ValueError("owner-view is read-only and prints only")
            print(json.dumps(context.owner_view(), ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "response-check":
            if args.apply or args.output:
                raise ValueError("response-check is read-only and prints only")
            raw = sys.stdin.buffer.read() if args.fields == "-" else Path(args.fields).read_bytes()
            _rc = _json(raw)
            try: print(json.dumps(context.response_check(**_rc), ensure_ascii=True))
            except TypeError as exc:
                raise ValueError('response-check fields missing/invalid ('+str(exc)+'). Required keyword fields: request_outcome_ids, excluded_active, source_clause_ids, purpose, next_action.') from exc
            return 0
        if args.apply and (args.command not in {"source-update","progress","action-start","action-outcome","run-operation"} or not args.event_id or args.output):
            raise ValueError("--apply requires a supported explicit action and --event-id, without --output")
        raw = sys.stdin.buffer.read() if args.fields == "-" else Path(args.fields).read_bytes()
        if args.command == "run-operation":
            if not args.apply:
                raise ValueError("run-operation requires explicit --apply and --event-id")
            print(json.dumps(context.run_operation(**_json(raw),event_id=args.event_id),ensure_ascii=True))
            return 0
        payload = context.source_update(**_json(raw)) if args.command == "source-update" else context.build(args.command, _json(raw))
        if args.apply:
            receipt = context.apply_source_update(payload, event_id=args.event_id) if args.command == "source-update" else context.apply_prepared(args.command,payload,event_id=args.event_id)
            print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
            return 0
        if args.output:
            context.write_input(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        phase = context.apply_phase if context else "not_started"
        uncertain = phase != "not_started"
        details = dict(context.applied_receipt or {}) if context else {}
        if context and isinstance(error, context.runtime.PostCommitError):
            details.update(event_id=error.event_id, head_hash=error.head_hash,
                           effect=error.effect, cleanup_fault=error.cleanup_fault)
        print(json.dumps({"status": "APPLY_UNCERTAIN_REPLAY_REQUIRED" if uncertain else "INPUT_REJECTED",
                          "journal_applied": True if phase == "committed" else "unknown" if uncertain else False,
                          "apply_phase": phase, **details,
                          "error": f"{type(error).__name__}: {error}"}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
