#!/usr/bin/env python3
"""Build typed allocation state and consume a fresh, identity-bound allocator result.

This adapter does not rank models, submit a job, or grant action permission. Evidence
is an exclusive-created JSONL file: raw process output is flushed before validation.
The caller still supplies current source/job facts and rechecks them before dispatch.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any


ALLOCATOR = Path(__file__).with_name("stage_allocation.py")
IDENTITY_FIELDS = (
    "objective_id", "source_sha256", "stage_id", "job_id", "job_shape_sha256",
    "current_lease_id", "owner",
)
SUCCESS_DECISIONS = {"SELECTED", "RETAIN_CURRENT", "SOURCE_SELECTED"}
ACCEPTED_CHOICES = ("accepted", "rejected", "unavailable")
STATE_FIELDS = {
    "selected_model", "selected_reasoning", "injectable", "requested_model",
    "requested_reasoning", "accepted", "effective_model", "effective_reasoning", "fallback",
}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def build_orchestrator_state(
    *, selected_model: str, selected_reasoning: str, injectable: bool,
    requested_model: str, requested_reasoning: str, accepted: str,
    fallback: str, effective_model: str | None = None,
    effective_reasoning: str | None = None,
) -> dict[str, Any]:
    """Validate explicit episode facts; never infer effective settings or enum aliases."""
    if type(injectable) is not bool:
        raise ValueError("injectable must be a boolean")
    if not isinstance(accepted, str) or accepted not in ACCEPTED_CHOICES:
        raise ValueError(f"accepted must be one of {ACCEPTED_CHOICES}")
    state = {
        "selected_model": selected_model, "selected_reasoning": selected_reasoning,
        "injectable": injectable, "requested_model": requested_model,
        "requested_reasoning": requested_reasoning, "accepted": accepted,
        "effective_model": effective_model, "effective_reasoning": effective_reasoning,
        "fallback": fallback,
    }
    for field in ("selected_model", "selected_reasoning", "requested_model", "requested_reasoning", "fallback"):
        _text(state[field], field)
    for field in ("effective_model", "effective_reasoning"):
        if state[field] is not None:
            _text(state[field], field)
    return state


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=reject_constant)


def _plain_path(value: str | Path, *, existing: bool) -> Path:
    """Reject link/reparse components rather than silently resolve their targets."""
    path = Path(os.path.abspath(os.fspath(value)))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component != path or existing:
                raise
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"link or reparse point is not allowed: {component}")
    if existing and not path.is_file():
        raise ValueError(f"expected a regular file: {path}")
    return path


def _stamp(path: Path) -> dict[str, Any]:
    _plain_path(path, existing=True)
    info = path.stat()
    return {"sha256": _sha(path.read_bytes()), "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "device": info.st_dev, "inode": info.st_ino}


def _append(stream: Any, record: dict[str, Any]) -> None:
    stream.write(_canonical(record) + b"\n")
    stream.flush()
    os.fsync(stream.fileno())


def _validate_input_identity(data: Any, expected: dict[str, Any] | None) -> None:
    if not isinstance(data, dict):
        raise ValueError("allocation input must be an object")
    for field in IDENTITY_FIELDS[:-1]:
        _text(data.get(field), field)
    owner = data.get("owner")
    if not isinstance(owner, dict) or set(owner) != {"lane", "task_id", "lease_id"}:
        raise ValueError("owner must bind lane/task_id/lease_id")
    for field, value in owner.items():
        _text(value, f"owner.{field}")
    if expected is not None:
        if not isinstance(expected, dict) or set(expected) != set(IDENTITY_FIELDS):
            raise ValueError("expected_identity must contain exactly the identity fields")
        if any(data[field] != expected[field] for field in IDENTITY_FIELDS):
            raise ValueError("input differs from the caller's current identity")


def _selection_projection(data: dict[str, Any]) -> dict[str, Any]:
    """Recompute v2 selection relations, not model quality or action eligibility.

    Keep this checked projection aligned with stage_allocation.py. It deliberately
    uses that allocator's existing cost ordering and context tie rules, with no
    model-name interpretation. A changed allocator requires affected adapter tests.
    """
    capable = [item for item in data["candidates"]
               if all(need["met"] for need in item["capability_needs"])]
    statuses = {item["configuration_id"]: item["risk_adjusted_total_cost"]["status"] for item in capable}
    if len(statuses) != len(capable):
        raise ValueError("capable configuration identities are ambiguous")
    measured = [item for item in capable if statuses[item["configuration_id"]] in {"observed", "estimated"}]
    unavailable = [item["configuration_id"] for item in capable if statuses[item["configuration_id"]] == "unavailable"]
    context = data["context_reuse"]
    current = next((item for item in capable if item["configuration_id"] == context.get("current_configuration_id")), None)
    authority = data["selection_authority"]
    chosen = None
    if authority["mode"] == "exact-source-selection":
        matches = [item for item in capable if item["configuration_id"] == authority["configuration_id"]
                   and statuses[item["configuration_id"]] == "source_selected"]
        if len(matches) != 1 or not authority["source_clause"] or not authority["evidence"]:
            raise ValueError("source selection is not bound to one capable candidate")
        chosen = matches[0]
        decision = "SOURCE_SELECTED"
    elif not measured:
        if current is not None and context["benefit_positive"]:
            chosen, decision = current, "RETAIN_CURRENT"
        else:
            decision = "USER-DECISION" if unavailable else "NO_CAPABLE_CONFIGURATION"
    elif unavailable:
        if current is not None and context["benefit_positive"]:
            chosen, decision = current, "RETAIN_CURRENT"
        else:
            decision = "USER-DECISION"
    else:
        measured.sort(key=lambda item: (item["risk_adjusted_total_cost"]["value"], item["configuration_id"]))
        best = measured[0]["risk_adjusted_total_cost"]["value"]
        ties = [item for item in measured if item["risk_adjusted_total_cost"]["value"] == best]
        tied_current = next((item for item in ties if item["configuration_id"] == context.get("current_configuration_id")), None)
        if len(ties) == 1:
            chosen, decision = ties[0], "SELECTED"
        elif tied_current is not None and context["benefit_positive"]:
            chosen, decision = tied_current, "RETAIN_CURRENT"
        else:
            decision = "USER-DECISION"
    selected = chosen["configuration_id"] if chosen is not None else None
    basis = {
        "SELECTED": "estimated-minimum" if selected and statuses[selected] == "estimated" else "observed-minimum",
        "RETAIN_CURRENT": "retain-current-tie-or-unavailable", "SOURCE_SELECTED": "source-selection",
        "USER-DECISION": "hold", "NO_CAPABLE_CONFIGURATION": "hold",
    }[decision]
    return {"decision": decision, "selected_configuration_id": selected,
            "selected_orchestrator_state": chosen["orchestrator_state"] if chosen is not None else None,
            "reuse_current_conversation": context["benefit_positive"], "selection_basis": basis,
            "cost_statuses": dict(sorted(statuses.items())), "unavailable_cost_candidates": sorted(unavailable)}


def _consume_result(raw: bytes, data: dict[str, Any], input_sha256: str) -> dict[str, Any]:
    result = _json(raw)
    if not isinstance(result, dict) or result.get("schema_version") != "stage-allocation-result-v2":
        raise ValueError("unsupported allocator result")
    payload = dict(result)
    digest = payload.pop("result_sha256", None)
    if not isinstance(digest, str) or digest != _sha(_canonical(payload)):
        raise ValueError("allocator result self-hash mismatch")
    if result.get("input_sha256") != input_sha256:
        raise ValueError("allocator result is not bound to this input")
    if any(result.get(field) != data[field] for field in IDENTITY_FIELDS):
        raise ValueError("allocator result identity mismatch")
    expected_selection = _selection_projection(data)
    expected_fields = set(IDENTITY_FIELDS) | set(expected_selection) | {
        "schema_version", "input_sha256", "result_sha256", "proof_ceiling",
    }
    if set(result) != expected_fields or any(result[key] != value for key, value in expected_selection.items()):
        raise ValueError("allocator result does not match the input's deterministic v2 selection")
    if result.get("decision") not in SUCCESS_DECISIONS:
        raise ValueError("allocator did not return a successful selection")
    selected_id = _text(result.get("selected_configuration_id"), "selected_configuration_id")
    matches = [item for item in data.get("candidates", [])
               if isinstance(item, dict) and item.get("configuration_id") == selected_id]
    if len(matches) != 1:
        raise ValueError("selected configuration must match exactly one input candidate")
    selected = matches[0]
    needs = selected.get("capability_needs")
    if not isinstance(needs, list) or not needs or any(
        not isinstance(need, dict) or need.get("met") is not True for need in needs
    ):
        raise ValueError("selected configuration is not capable in this input")
    state = selected.get("orchestrator_state")
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise ValueError("selected orchestrator state fields differ")
    build_orchestrator_state(**state)
    if result.get("selected_orchestrator_state") != state:
        raise ValueError("selected orchestrator state mismatch")
    context = data.get("context_reuse", {})
    reuse = result.get("reuse_current_conversation")
    if type(reuse) is not bool or reuse != context.get("benefit_positive"):
        raise ValueError("context reuse is not bound to the input")
    return {
        "selection_ready": True, "decision": result["decision"],
        "selected_configuration_id": selected_id, "selected_orchestrator_state": state,
        "reuse_current_conversation": reuse,
        "selected_matches_current_configuration": selected_id == context.get("current_configuration_id"),
        "selection_basis": result.get("selection_basis"),
        "result_sha256": digest,
    }


def run_allocation(
    input_path: str | Path, evidence_path: str | Path,
    *, expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the sibling allocator once, preserving raw evidence before interpretation.

    Existing or linked evidence destinations raise before starting the allocator.
    Other validation/process failures return selection_ready=False with evidence.
    expected_identity should be supplied when the caller already has a current
    transition. Without it, freshness is only relative to the explicit input file.
    """
    source = _plain_path(input_path, existing=True)
    allocator = _plain_path(ALLOCATOR, existing=True)
    destination = _plain_path(evidence_path, existing=False)
    if destination in {source, allocator} or destination.exists():
        raise FileExistsError(f"evidence destination must be new: {destination}")
    before = {"input": _stamp(source), "allocator": _stamp(allocator)}
    input_bytes = source.read_bytes()
    if _sha(input_bytes) != before["input"]["sha256"]:
        raise ValueError("input changed during acquisition")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(destination, flags, 0o600)
    started = time.monotonic()
    normalized: dict[str, Any] = {
        "schema_version": "allocation-io-result-v1", "selection_ready": False,
        "permission_granted": False, "dispatch_performed": False,
        "evidence_path": str(destination), "input_sha256": before["input"]["sha256"],
        "allocator_sha256": before["allocator"]["sha256"],
        "proof_ceiling": "fresh allocation selection only; not dispatch, effective model, permission, or outcome proof",
    }
    with os.fdopen(fd, "wb") as evidence:
        raw: dict[str, Any] = {
            "record_type": "raw_process", "schema_version": "allocation-io-evidence-v1",
            "input_path": str(source), "allocator_path": str(allocator), "before": before,
            "input_base64": base64.b64encode(input_bytes).decode("ascii"),
            "expected_identity": expected_identity,
            "argv": [sys.executable, "-B", str(allocator), "--input", str(source)],
            "process_started": False, "returncode": None,
            "stdout_base64": "", "stderr_base64": "", "error": None,
        }
        completed = None
        data = None
        try:
            data = _json(input_bytes)
            _validate_input_identity(data, expected_identity)
            # Check the same identities immediately before the sole process launch.
            if _stamp(source) != before["input"] or _stamp(allocator) != before["allocator"]:
                raise ValueError("input or allocator changed before execution")
            process = subprocess.Popen(raw["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            raw["process_started"] = True
            try:
                stdout, stderr = process.communicate()
            except BaseException:
                process.kill()
                stdout, stderr = process.communicate()
                raw.update(returncode=process.returncode,
                           stdout_base64=base64.b64encode(stdout).decode("ascii"),
                           stderr_base64=base64.b64encode(stderr).decode("ascii"))
                raise
            completed = (process.returncode, stdout, stderr)
            raw.update(returncode=process.returncode,
                       stdout_base64=base64.b64encode(stdout).decode("ascii"),
                       stderr_base64=base64.b64encode(stderr).decode("ascii"))
        except (Exception, KeyboardInterrupt) as error:
            raw["error"] = f"{type(error).__name__}: {error}"
        raw["elapsed_seconds"] = time.monotonic() - started
        _append(evidence, raw)  # Never decode the process result before this durable record.
        try:
            after = {"input": _stamp(source), "allocator": _stamp(allocator)}
            normalized["after"] = after
            if after != before:
                raise ValueError("input or allocator changed during execution; result unconsumed")
            if raw["error"]:
                raise ValueError(raw["error"])
            if completed is None or completed[0] != 0:
                raise ValueError(f"allocator returned nonzero or no result: {raw['returncode']}")
            normalized.update(_consume_result(completed[1], data, before["input"]["sha256"]))
        except Exception as error:
            normalized["error"] = f"{type(error).__name__}: {error}"
        normalized["elapsed_seconds"] = time.monotonic() - started
        normalized["process_started"] = raw["process_started"]
        normalized["returncode"] = raw["returncode"]
        normalized["process_effect_status"] = (
            "NOT-STARTED" if not raw["process_started"] else
            "EXIT-OBSERVED" if raw["returncode"] is not None else "UNKNOWN"
        )
        _append(evidence, {"record_type": "consumption", **normalized})
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    state = commands.add_parser("state", help="write one explicitly typed orchestrator state to stdout")
    for field in ("selected-model", "selected-reasoning", "requested-model", "requested-reasoning", "fallback"):
        state.add_argument(f"--{field}", required=True)
    state.add_argument("--injectable", choices=("true", "false"), required=True)
    state.add_argument("--accepted", choices=ACCEPTED_CHOICES, required=True)
    state.add_argument("--effective-model")
    state.add_argument("--effective-reasoning")
    run = commands.add_parser("run", help="run allocation once and write new JSONL evidence")
    run.add_argument("--input", required=True)
    run.add_argument("--evidence", required=True)
    args = parser.parse_args()
    try:
        if args.command == "state":
            fields = vars(args).copy()
            fields.pop("command")
            fields["injectable"] = fields["injectable"] == "true"
            result = build_orchestrator_state(**fields)
            exit_code = 0
        else:
            result = run_allocation(args.input, args.evidence)
            exit_code = 0 if result["selection_ready"] else 3
        print(json.dumps(result, sort_keys=True, indent=2))
        return exit_code
    except (ValueError, OSError) as error:
        print(json.dumps({"selection_ready": False, "permission_granted": False,
                          "error": f"{type(error).__name__}: {error}"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
