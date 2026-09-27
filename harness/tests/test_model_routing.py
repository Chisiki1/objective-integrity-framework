"""Local configuration/routing checks; no provider capability or live API proof."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from policy_harness.model_routing import (
    ModelSelectionError, StaleModelLease, bind_selection, catalog, resolve_lease,
)
from policy_harness.models import ConfigurationRequired
from policy_harness.settings import SettingsManager


def configure(path, **overrides):
    settings = SettingsManager(path)
    settings.update({
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4.1-flash", "api_mode": "compatible",
        "model_context_tokens": 1_000_000, "max_output_tokens": 32768,
        "request_timeout_seconds": 1200, **overrides,
    })
    return settings


@pytest.fixture
def settings(tmp_path):
    return configure(tmp_path / "settings")


def context(job="child-one", role="worker"):
    return {
        "job_id": job, "role": role, "objective": "Implement and validate a pure routing adapter.",
        "source_hash": "A" * 64, "policy_hash": "B" * 64,
        "capability_requirements": ["Structured JSON decisions", "Python source comparison"],
        "quality_requirements": ["Reject stale routing before any model call"],
        "cost_considerations": "Reuse the configured contract and judge elapsed time and rework before token cost.",
        "parent_task_id": "parent-one", "parent_operation_id": "delegate-one",
    }


def selection(settings, role="worker"):
    options = catalog(settings)
    candidate = next(item for item in options["candidates"] if role in item["roles"])
    return {
        "candidate_id": candidate["candidate_id"],
        "configuration_hash": options["configuration_hash"],
        "reasoning": copy.deepcopy(candidate["reasoning_choices"][0]),
        "model_reason": "The configured contract model is the available candidate for this bounded Python/JSON job; preserve quality and measure rework.",
        "reasoning_reason": "Use the explicitly configured reasoning route for source comparison; do not infer effort support from the model name.",
    }


def rehash(lease):
    body = {k: v for k, v in lease.items() if k != "lease_hash"}
    lease["lease_hash"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest().upper()
    return lease


def test_catalog_describes_only_real_settings_shape_and_default_reasoning(settings):
    result = catalog(settings)
    assert result["missing_configuration"] == []
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["model"] == "deepseek-v4.1-flash"
    assert candidate["roles"] == ["parent", "worker", "reviewer", "web"]
    assert candidate["reasoning_choices"] == [{"mode": "provider_default", "effort": None}]
    assert candidate["availability_evidence"] == "explicit_current_configuration"
    assert candidate["live_capability_status"] == "not_observed_by_catalog"


def test_missing_configuration_has_no_candidate_and_cannot_bind(tmp_path):
    empty = SettingsManager(tmp_path / "empty")
    result = catalog(empty)
    assert result["candidates"] == []
    assert set(result["missing_configuration"]) == {"base_url", "model", "model_context_tokens", "max_output_tokens"}
    with pytest.raises(ConfigurationRequired):
        bind_selection(empty, {}, context())


def test_roundtrip_pins_exact_values_and_job(settings):
    job = context()
    lease = bind_selection(settings, selection(settings), job)
    restored = json.loads(json.dumps(lease))
    values = resolve_lease(settings, restored, role="worker", job_context=job)
    assert values["base_url"] == "https://opencode.ai/zen/go/v1"
    assert values["model"] == values["review_model"] == "deepseek-v4.1-flash"
    assert values["api_mode"] == "compatible"
    assert values["reasoning_effort"] is None
    assert values["max_output_tokens"] == 32768
    assert values["model_selection"]["lease_hash"] == lease["lease_hash"]
    assert values["model_selection"]["job_id"] == "child-one"


def test_multiline_objective_is_preserved_exactly(settings):
    job = context()
    job["objective"] = "Compare the routing sources.\r\n\tPreserve the configured values.\nReport the evidence."
    lease = bind_selection(settings, selection(settings), job)
    restored = json.loads(json.dumps(lease))
    assert restored["job_context"]["objective"] == job["objective"]
    values = resolve_lease(settings, restored, role="worker", job_context=job)
    assert values["model_selection"]["job_context_hash"] == lease["job_context_hash"]


@pytest.mark.parametrize("field,value", [
    ("objective", "Normal text\x00with a forbidden control"),
    ("objective", "Normal text\x1bwith a forbidden escape"),
    ("job_id", "child\nother"), ("parent_operation_id", "delegate\r\nother"),
    ("cost_considerations", "cost\nother"),
])
def test_multiline_objective_does_not_relax_other_control_restrictions(settings, field, value):
    job = context()
    job[field] = value
    with pytest.raises(ModelSelectionError, match="control characters"):
        bind_selection(settings, selection(settings), job)


def test_selection_reason_control_restriction_is_unchanged(settings):
    selected = selection(settings)
    selected["model_reason"] = "A reason\nwith a line break"
    with pytest.raises(ModelSelectionError, match="control characters"):
        bind_selection(settings, selected, context())


def test_two_configured_cuts_and_jobs_do_not_silently_switch(tmp_path):
    # Both are explicit local SettingsManager cuts, not claims about live models.
    first = configure(tmp_path / "first")
    second = configure(tmp_path / "second", api_mode="deepagents", max_output_tokens=16384)
    job_a, job_b = context("child-a"), context("child-b")
    lease_a = bind_selection(first, selection(first), job_a)
    lease_b = bind_selection(second, selection(second), job_b)
    assert lease_a["lease_hash"] != lease_b["lease_hash"]
    assert resolve_lease(first, lease_a, job_context=job_a)["api_mode"] == "compatible"
    assert resolve_lease(second, lease_b, job_context=job_b)["api_mode"] == "deepagents"
    with pytest.raises(StaleModelLease):
        resolve_lease(second, lease_a)
    with pytest.raises(StaleModelLease):
        resolve_lease(first, lease_b)


@pytest.mark.parametrize("delta", [
    {"base_url": "https://example.invalid/other-contract/v1"},
    {"model": "new-operator-configured-model-fixture"},
    {"review_model": "new-review-fixture"},
    {"api_mode": "deepagents"},
    {"reasoning_effort": "operator-configured-effort-fixture"},
    {"model_context_tokens": 900_000}, {"max_output_tokens": 16000},
    {"request_timeout_seconds": 600}, {"user_agent": "harness-fixture/2"},
])
def test_any_model_request_configuration_change_invalidates_old_lease(settings, delta):
    lease = bind_selection(settings, selection(settings), context())
    settings.update(delta)
    with pytest.raises(StaleModelLease, match="configuration changed"):
        resolve_lease(settings, lease)


def test_changed_catalog_rejects_binding_not_only_request(settings):
    selected = selection(settings)
    settings.update({"max_output_tokens": 8192})
    with pytest.raises(StaleModelLease, match="before selection"):
        bind_selection(settings, selected, context())


def test_unrelated_web_setup_does_not_change_model_configuration(settings):
    lease = bind_selection(settings, selection(settings), context())
    settings.update({"web_provider": "public_url", "web_max_response_bytes": 10000})
    assert resolve_lease(settings, lease)["model_selection"]["lease_hash"] == lease["lease_hash"]


def test_catalog_and_lease_never_read_secrets_or_return_secret_fields(settings, monkeypatch):
    original_get = settings.get

    def public_with_extra_fields():
        # Sentinel fields establish that only the allowlisted public cut leaves.
        return {**original_get(), "model_api_key": "EXCLUDED_TEST_SENTINEL", "unknown_field": "EXCLUDED_TEST_SENTINEL"}

    def forbidden_secret(*args):
        pytest.fail("Routing code must not access the credential method.")

    monkeypatch.setattr(settings, "secret", forbidden_secret)
    monkeypatch.setattr(settings, "get", public_with_extra_fields)
    lease = bind_selection(settings, selection(settings), context())
    output = [catalog(settings), lease, resolve_lease(settings, lease)]
    assert "EXCLUDED_TEST_SENTINEL" not in json.dumps(output)
    assert "model_api_key" not in json.dumps(output)


def test_input_and_resolved_value_mutation_do_not_mutate_lease(settings):
    selected, job = selection(settings), context()
    lease = bind_selection(settings, selected, job)
    identity = lease["lease_hash"]
    selected["reasoning"]["effort"] = "unconfigured"
    job["capability_requirements"].append("later task change")
    resolved = resolve_lease(settings, lease)
    resolved["model_selection"]["reasoning"]["effort"] = "changed-output-copy"
    assert lease["lease_hash"] == identity
    assert lease["reasoning"]["effort"] is None
    assert len(lease["job_context"]["capability_requirements"]) == 2
    assert resolve_lease(settings, lease)["reasoning_effort"] is None


@pytest.mark.parametrize("field,value", [
    ("model", "unconfigured-model"), ("provider", "https://example.invalid/v1"),
    ("api_mode", "deepagents"), ("model_reason", "rewritten reason"),
    ("reasoning", {"mode": "configured", "effort": "unconfigured"}),
])
def test_mutated_lease_cannot_resolve(settings, field, value):
    lease = bind_selection(settings, selection(settings), context())
    lease[field] = value
    with pytest.raises(ModelSelectionError, match="altered"):
        resolve_lease(settings, lease)


@pytest.mark.parametrize("field,value", [
    ("model", "unconfigured-model"), ("provider", "https://example.invalid/v1"),
    ("api_mode", "deepagents"), ("job_context_hash", "C" * 64),
])
def test_recomputed_hash_does_not_make_nonconfigured_routing_valid(settings, field, value):
    lease = bind_selection(settings, selection(settings), context())
    lease[field] = value
    rehash(lease)
    with pytest.raises(ModelSelectionError):
        resolve_lease(settings, lease)


def test_unavailable_candidate_and_reasoning_are_rejected(settings):
    selected = selection(settings)
    selected["candidate_id"] = "C" * 64
    with pytest.raises(ModelSelectionError, match="not in"):
        bind_selection(settings, selected, context())
    selected = selection(settings)
    selected["reasoning"] = {"mode": "configured", "effort": "unconfigured-effort"}
    with pytest.raises(ModelSelectionError, match="not explicitly configured"):
        bind_selection(settings, selected, context())


def test_explicit_configured_effort_is_not_an_invented_effort_catalog(tmp_path):
    settings = configure(tmp_path / "effort", reasoning_effort="operator-configured-effort-fixture")
    result = catalog(settings)
    assert result["candidates"][0]["reasoning_choices"] == [
        {"mode": "configured", "effort": "operator-configured-effort-fixture"},
    ]
    lease = bind_selection(settings, selection(settings), context())
    assert resolve_lease(settings, lease)["reasoning_effort"] == "operator-configured-effort-fixture"
    selected = selection(settings)
    selected["reasoning"] = {"mode": "provider_default", "effort": None}
    with pytest.raises(ModelSelectionError):
        bind_selection(settings, selected, context())


def test_review_model_requires_separate_role_lease(tmp_path):
    settings = configure(tmp_path / "review", review_model="operator-configured-review-fixture")
    candidates = catalog(settings)["candidates"]
    assert len(candidates) == 2
    assert candidates[1]["roles"] == ["reviewer"]
    with pytest.raises(ModelSelectionError, match="job role"):
        bind_selection(settings, selection(settings, "reviewer"), context())
    worker = bind_selection(settings, selection(settings), context())
    reviewer = bind_selection(settings, selection(settings, "reviewer"), context(role="reviewer"))
    assert resolve_lease(settings, worker, role="worker")["review_model"] == "deepseek-v4.1-flash"
    assert resolve_lease(settings, reviewer, role="reviewer")["model"] == "operator-configured-review-fixture"
    with pytest.raises(StaleModelLease, match="another request role"):
        resolve_lease(settings, worker, role="reviewer")


@pytest.mark.parametrize("field,value", [
    ("job_id", "child-two"), ("objective", "A materially changed objective"),
    ("source_hash", "D" * 64), ("policy_hash", "E" * 64),
    ("quality_requirements", ["Changed acceptance"]),
    ("cost_considerations", "Changed job size needs a new choice"),
])
def test_cross_job_or_changed_requirements_cannot_reuse_valid_lease(settings, field, value):
    job = context()
    lease = bind_selection(settings, selection(settings), job)
    job[field] = value
    with pytest.raises(StaleModelLease, match="job/source/requirements changed"):
        resolve_lease(settings, lease, job_context=job)


@pytest.mark.parametrize("field", ["model_reason", "reasoning_reason"])
def test_blank_separate_reasons_are_rejected(settings, field):
    selected = selection(settings)
    selected[field] = "   "
    with pytest.raises(ModelSelectionError):
        bind_selection(settings, selected, context())


def test_identical_reasons_are_rejected(settings):
    selected = selection(settings)
    selected["reasoning_reason"] = selected["model_reason"]
    with pytest.raises(ModelSelectionError, match="separate substantive reasons"):
        bind_selection(settings, selected, context())


@pytest.mark.parametrize("field", ["capability_requirements", "quality_requirements", "cost_considerations"])
def test_job_shape_grounding_is_required(settings, field):
    job = context()
    del job[field]
    with pytest.raises(ModelSelectionError):
        bind_selection(settings, selection(settings), job)


def test_extra_authority_or_secret_fields_are_not_accepted(settings):
    selected = selection(settings)
    selected["approved"] = True
    with pytest.raises(ModelSelectionError):
        bind_selection(settings, selected, context())
    job = context()
    job["model_api_key"] = "DISALLOWED_FIELD_SENTINEL"
    with pytest.raises(ModelSelectionError):
        bind_selection(settings, selection(settings), job)
    lease = bind_selection(settings, selection(settings), context())
    lease["approved"] = True
    with pytest.raises(ModelSelectionError):
        resolve_lease(settings, lease)
