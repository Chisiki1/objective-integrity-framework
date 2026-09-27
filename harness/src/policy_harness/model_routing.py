"""Bind one reviewed job/role selection to the configured model request route.

Only the public SettingsManager adapter is read. Configuration is an operator
assertion of availability, not a live capability/quality observation. Leases are
JSON envelopes with immutable content identities: store the approved envelope
in controller-owned state, never accept it from a model's request payload. A
digest detects alteration; it is not a signature or independent authorization.

Gateway integration, before reading credentials or constructing either request
mode::

    values = resolve_lease(settings, controller_owned_lease,
                           role=role, job_context=current_job_context)
    # Read credentials through the existing protected gateway path separately.
    # Use these exact values in both _request and _deepagents, and copy
    # values['model_selection'] into request/response metadata.

The selected role's model is written to both model and review_model so the
gateway's existing reviewer override cannot silently substitute another model.
Bind worker and reviewer selections separately. A materially changed job or
configuration needs a new parent/reviewer disposition; it never mutates a lease.
Nonempty, separate rationales are structural requirements. Their semantic
adequacy, mandatory quality and actual model behavior still require review and
the real consumer's evidence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from typing import Any, Protocol

from .models import ConfigurationRequired, PolicyError
from .settings import DEFAULTS, SettingsManager


class PublicSettings(Protocol):
    def get(self) -> dict: ...


class ModelSelectionError(PolicyError):
    """Invalid/unavailable selection; no model request was made."""


class StaleModelLease(ModelSelectionError):
    """The selected configuration or job no longer matches the caller."""


CONFIGURATION_FIELDS = (
    "base_url", "model", "review_model", "api_mode", "reasoning_effort",
    "model_context_tokens", "max_output_tokens", "request_timeout_seconds",
    "user_agent",
)
ROLES = ("parent", "worker", "reviewer", "web")
CONTEXT_REQUIRED = frozenset({
    "job_id", "role", "objective", "source_hash", "policy_hash",
    "capability_requirements", "quality_requirements", "cost_considerations",
})
CONTEXT_OPTIONAL = frozenset({"parent_task_id", "parent_operation_id"})
SELECTION_FIELDS = frozenset({
    "candidate_id", "configuration_hash", "reasoning", "model_reason",
    "reasoning_reason",
})
LEASE_FIELDS = frozenset({
    "schema", "lease_hash", "configuration_hash", "configuration",
    "candidate_id", "provider", "model", "api_mode", "reasoning",
    "model_reason", "reasoning_reason", "job_context", "job_context_hash",
})


def _copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OverflowError):
        raise ModelSelectionError("Selection data must be finite JSON values.") from None


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()


def _text(value: Any, field: str, *, multiline: bool = False) -> str:
    permitted_whitespace = "\r\n\t" if multiline else ""
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 and c not in permitted_whitespace for c in value):
        raise ModelSelectionError(f"{field} must be nonempty text without unsupported control characters.")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise ModelSelectionError(f"{field} must be a SHA-256 identity.")
    return value.upper()


def _configuration(settings: PublicSettings) -> dict:
    # Do not copy arbitrary public/secret fields into hashes, metadata or leases.
    public = settings.get()
    if not isinstance(public, Mapping):
        raise ModelSelectionError("The trusted settings adapter returned an invalid public configuration.")
    values = SettingsManager._validate({
        **DEFAULTS, **{k: public[k] for k in CONFIGURATION_FIELDS if k in public},
    })
    if not math.isfinite(values["request_timeout_seconds"]):
        raise ModelSelectionError("request_timeout_seconds must be finite.")
    return {name: values[name] for name in CONFIGURATION_FIELDS}


def _reasoning(configuration: dict) -> dict:
    effort = configuration["reasoning_effort"]
    return {"mode": "provider_default" if effort is None else "configured", "effort": effort}


def _catalog(configuration: dict) -> dict:
    config_hash = _digest(configuration)
    missing = [name for name in ("base_url", "model", "model_context_tokens", "max_output_tokens") if not configuration[name]]
    candidates = []
    if not missing:
        primary, reviewer = configuration["model"], configuration["review_model"] or configuration["model"]
        routes = [(primary, [r for r in ROLES if r != "reviewer" or primary == reviewer])]
        if reviewer != primary:
            routes.append((reviewer, ["reviewer"]))
        for model, roles in routes:
            identity = {"configuration_hash": config_hash, "provider": configuration["base_url"],
                        "model": model, "api_mode": configuration["api_mode"], "roles": roles}
            candidates.append({
                "candidate_id": _digest(identity), **identity,
                "reasoning_choices": [_reasoning(configuration)],
                "availability_evidence": "explicit_current_configuration",
                "live_capability_status": "not_observed_by_catalog",
            })
    return {"schema": "model-catalog-v1", "configuration_hash": config_hash,
            "candidates": candidates, "missing_configuration": missing,
            "reasoning_support": "only_explicit_configuration_or_provider_default"}


def catalog(settings: PublicSettings) -> dict:
    """Describe currently configured routes only; no discovery or key access.

    An empty candidates list names missing setup. A distinct configured
    review_model is eligible only for the reviewer role. Named effort support is
    never inferred from the model/provider name.
    """
    return _catalog(_configuration(settings))


def _context(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - CONTEXT_REQUIRED - CONTEXT_OPTIONAL or CONTEXT_REQUIRED - set(value):
        raise ModelSelectionError("job_context must contain the defined job, role, source, quality, capability and cost fields only.")
    result = _copy(value)
    for name in (CONTEXT_REQUIRED | CONTEXT_OPTIONAL) - {"capability_requirements", "quality_requirements"}:
        if name in result:
            result[name] = _text(result[name], f"job_context.{name}", multiline=name == "objective")
    for name in ("source_hash", "policy_hash"):
        result[name] = _hash(result[name], f"job_context.{name}")
    if result["role"] not in ROLES:
        raise ModelSelectionError("The job role has no configured model route.")
    for name in ("capability_requirements", "quality_requirements"):
        if not isinstance(result[name], list) or not result[name]:
            raise ModelSelectionError(f"job_context.{name} requires explicit job-specific requirements.")
        for item in result[name]:
            _text(item, f"job_context.{name}")
    return result


def _selection(selected: Any) -> dict:
    if not isinstance(selected, dict) or set(selected) != SELECTION_FIELDS:
        raise ModelSelectionError("Selection requires only candidate_id, configuration_hash, explicit reasoning and separate model/reasoning reasons.")
    result = _copy(selected)
    for name in ("candidate_id", "configuration_hash"):
        result[name] = _hash(result[name], name)
    for name in ("model_reason", "reasoning_reason"):
        _text(result[name], name)
    if result["model_reason"].strip().casefold() == result["reasoning_reason"].strip().casefold():
        raise ModelSelectionError("Model and reasoning choices require separate substantive reasons.")
    reasoning = result["reasoning"]
    if not isinstance(reasoning, dict) or set(reasoning) != {"mode", "effort"}:
        raise ModelSelectionError("reasoning requires explicit mode and effort fields.")
    if reasoning["mode"] == "provider_default":
        if reasoning["effort"] is not None:
            raise ModelSelectionError("provider_default reasoning must explicitly use null effort.")
    elif reasoning["mode"] == "configured":
        _text(reasoning["effort"], "reasoning.effort")
    else:
        raise ModelSelectionError("Unknown reasoning selection mode.")
    return result


def bind_selection(settings: PublicSettings, selected: dict, job_context: dict) -> dict:
    """Create a content-addressed, JSON-safe lease for one exact job and role.

    selected = {candidate_id, configuration_hash, reasoning: {mode, effort},
                model_reason, reasoning_reason}
    job_context = {job_id, role, objective, source_hash, policy_hash,
                   capability_requirements: [text], quality_requirements: [text],
                   cost_considerations, [parent_task_id], [parent_operation_id]}

    The controller must persist this exact lease as reviewed state. Keep source
    hashes derived from the real task, never from a model-supplied claim.
    """
    configuration = _configuration(settings)
    options = _catalog(configuration)
    if options["missing_configuration"]:
        raise ConfigurationRequired("Configure the model endpoint, model, context and output limits before selecting a child model.")
    selection, context = _selection(selected), _context(job_context)
    if selection["configuration_hash"] != options["configuration_hash"]:
        raise StaleModelLease("The catalog configuration changed before selection was bound.")
    candidate = next((item for item in options["candidates"] if item["candidate_id"] == selection["candidate_id"]), None)
    if candidate is None:
        raise ModelSelectionError("The selected candidate is not in the current configured catalog.")
    if context["role"] not in candidate["roles"]:
        raise ModelSelectionError("The selected model is not configured for this job role.")
    if selection["reasoning"] not in candidate["reasoning_choices"]:
        raise ModelSelectionError("The selected reasoning is not explicitly configured for this candidate.")
    lease = {
        "schema": "model-selection-lease-v1", "configuration": configuration,
        "configuration_hash": options["configuration_hash"], "candidate_id": candidate["candidate_id"],
        "provider": candidate["provider"], "model": candidate["model"], "api_mode": candidate["api_mode"],
        "reasoning": selection["reasoning"], "model_reason": selection["model_reason"],
        "reasoning_reason": selection["reasoning_reason"], "job_context": context,
        "job_context_hash": _digest(context),
    }
    return {**lease, "lease_hash": _digest(lease)}


def resolve_lease(settings: PublicSettings, lease: dict, *, role: str | None = None,
                  job_context: dict | None = None) -> dict:
    """Return exact nonsecret request values, rejecting changed or stale leases.

    Supply role and current controller-derived job_context at the gateway to
    prevent reusing a valid lease for another job. The two-argument adapter can
    validate persisted state but cannot observe which task is calling it.
    """
    if not isinstance(lease, dict) or set(lease) != LEASE_FIELDS:
        raise ModelSelectionError("Invalid model-selection lease envelope.")
    safe = _copy(lease)
    given_hash = _hash(safe.pop("lease_hash"), "lease_hash")
    if safe["schema"] != "model-selection-lease-v1" or not hmac.compare_digest(given_hash, _digest(safe)):
        raise ModelSelectionError("The immutable model-selection lease was altered or is invalid.")
    configuration = _configuration(settings)
    current_hash = _digest(configuration)
    if safe["configuration_hash"] != current_hash or safe["configuration"] != configuration:
        raise StaleModelLease("Model request configuration changed; preserve the old lease and explicitly reassess the dependent job.")
    context = _context(safe["job_context"])
    if safe["job_context_hash"] != _digest(context):
        raise ModelSelectionError("The lease job-context identity is invalid.")
    if role is not None and role != context["role"]:
        raise StaleModelLease("The lease belongs to another request role.")
    if job_context is not None and _context(job_context) != context:
        raise StaleModelLease("The job/source/requirements changed; explicitly reassess this selection.")
    # Revalidate all denormalized routing fields, even if a caller recomputed a
    # digest. Only controller ownership supplies approval, not this comparison.
    selected = {name: safe[name] for name in SELECTION_FIELDS}
    rebuilt = bind_selection(_SnapshotSettings(configuration), selected, context)
    if not hmac.compare_digest(rebuilt["lease_hash"], given_hash):
        raise ModelSelectionError("The lease fields do not describe the configured selection.")
    return {
        **configuration, "model": safe["model"], "review_model": safe["model"],
        "reasoning_effort": safe["reasoning"]["effort"],
        "model_selection": {"lease_hash": given_hash, "configuration_hash": current_hash,
                            "candidate_id": safe["candidate_id"], "job_context_hash": safe["job_context_hash"],
                            "job_id": context["job_id"], "role": context["role"],
                            "requested_model": safe["model"], "reasoning": safe["reasoning"]},
    }


class _SnapshotSettings:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get(self) -> dict:
        return dict(self.values)
