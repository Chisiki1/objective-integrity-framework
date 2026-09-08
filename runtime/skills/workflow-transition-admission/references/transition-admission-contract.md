# Workflow transition admission v3 with legacy read compatibility

Source-wide implementation uses `workflow-transition-admission-v3`: every v2 field plus `work_phase`, as defined by the sibling lifecycle [source-wide execution contract](../../master-guided-skill-lifecycle/references/source-wide-execution.md). The canonical sibling engine checks objective/source/owner identity and carries eligible next actions into the result, before any lightweight bypass. `EDIT` requires `IMPLEMENT` or `REPAIR_FINDINGS`. Construction feedback cannot admit external apply, push, CI or binding regression correction. All authority, causal, allocation, first-fault, scenario and effect obligations remain.

Use `workflow-transition-admission-v2` for ordinary same-conversation non-product boundaries. Read-only diagnosis, research and standalone documentation keep their original outcomes without manufacturing product phases. V1/v2 results explicitly state that source-wide selection was not evaluated. Reconcile the full original scope and current phase when adopting v3; a hash refresh or a child-only scope is not adoption.

The paragraphs under **Legacy v1** describe old receipts only and do not impose a separate Worker on v2/v3.

V2 keeps the shared blocks of v1 and adds root job_id and job_shape_sha256, with these exact replacements/additions:

- owner_role: COORDINATED-WORK, BOUNDED-SUBAGENT or DIRECT (DIRECT is non-candidate).
- topology exact fields: read_only_auditor (boolean), implementing_owner_role (COORDINATED-WORK / BOUNDED-SUBAGENT / DIRECT / NONE), owner_task_id, owner_lease_id, chat_id, parent_lease_ref, resource_claims_ref, recovery_mode. Material action requires role equality, nonempty task/lease/chat/resources; root task_id equals chat_id; a bounded child additionally binds parent_lease_ref. A read-only auditor cannot implement. Recovery adds JOURNAL_RECONCILED and LEGACY_LEASE_RECONCILED without inventing authority.
- skill_effect adds phase (PRE_ACTION / POST_ACTION) and outcome_capture_ref. PRE_ACTION requires selected/bytes/planned application/capture, an empty effect_ref and selected script if applicable. POST_ACTION requires actual effect_ref as well. No future PASS may authorize an action.
- stage_allocation adds view_sha256, result_ref, result_file_sha256, selected_configuration_id, allocator_script_ref and allocator_script_sha256. When job_shape_changed, view_ref is the actual typed stage-allocation-v2 input. The consumer verifies both file hashes, current objective/source/job/job-shape/owner/task/lease, result v2 self-hash and input hash; checks the canonical sibling lifecycle allocator script/hash; reruns that read-only allocator and requires exact result equality. Only SELECTED, RETAIN_CURRENT or SOURCE_SELECTED is admitted. A different task's valid result and a handwritten SELECTED label are not usable. These checks still cannot prove truthful cost/capability, semantic job identity or permission.

All original source/FCR/scenario/U0/known-family/freshness/consumer/recomposition/value obligations remain. Unresolved dependent relations are never promoted to PASS. Trivial read-only work stays lightweight. See scripts/test_same_chat_transition.py for a v2 builder and normal/counterexample receipts.

## Legacy v1 (existing explicit split-chat leases only)

The exact JSON fields are enforced by `transition_admission.py`. Every reference is a non-empty immutable identity or evidence locator, never a self-authored semantic PASS.

`action_class` is one of `trivial_read_only`, `material_diagnosis`, `binding_correction`, `finalized_dependent_action`, `monitoring_decision`, `commit_push`, `ci_dispatch`, or `external_apply`. `requested_transition` is one of `ADVISORY`, `DIAGNOSE`, `EDIT`, `EXECUTE`, `WAIT`, `REPLACE`, `USER_DECISION`.

A trivial read-only action with `material_candidate_action=false` may take `ADMIT_LIGHTWEIGHT`. Binding `EDIT` requires the complete correction block. `known_cause.applicable` requires current searches of both masters and a disposition; `RETEST` additionally requires a changed premise and discriminating prediction. A finalized applicable Skill-dependent action requires selected bytes, script when applicable, application and effect references. A repeated mechanical family requires `constrained_representation=true`. Released external action freshness must be `CURRENT`; otherwise only that action is `REPLAN`. Monitoring cannot use fixed polling; progressing work may `WAIT` at a decision window, while stalled/empty/stale projection requires an explicit recovery disposition.

Deterministic output proves structural admission and bounded cross-field consistency only. Semantic evidence remains under the normal workflow and independent review.

## Legacy v1 topology; shared recomposition and consumer fields

Only a legacy v1 receipt declares whether a Supervisor-internal implementation lane exists, the implementing owner, the formal Worker task reference and the recovery mode. In v1, a Supervisor-internal implementer is a dependent topology hold and material EDIT requires FORMAL_WORKER ownership. V2/v3 instead use the same-conversation topology above; independent reviewers remain read-only in every version.

Material candidate transitions, including a finalized dependent action, commit/push, CI dispatch, or external apply, also bind one semantic-lock reference, all relational-claim IDs, a recomposition witness, any unresolved relation IDs, consumer outcome claim IDs, an oracle and the expected evidence delta. Non-empty unresolved relations hold that dependent transition. These are identity/no-drop checks only and cannot validate scenario truth, witness sufficiency or consumer success.
