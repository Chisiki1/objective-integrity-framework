# Lifecycle and effect contract v3

New effects use `mgskill-effect-v3`. It retains the v2 fields and adds `materiality` with explicit `material`, `effect_candidate_generated`, and `reason` values, plus an `equivalent_skill_fingerprints` SHA-256 array. A material event is rejected unless candidate generation is true. Equivalent fingerprints recommend one merge candidate. Existing `mgskill-effect-v2` remains accepted for lineage compatibility but cannot establish the v3 obligation. Mechanical generation never authorizes semantic activation.

Only positive v3 outcomes (`applied` or `advanced`) require `effect_observation`, with actual outcome evidence and an independently identified observer. Its `use_kind` is `instruction`, `script`, or `tool`: instruction requires `instruction-used`; script/tool require `script-executed`/`tool-executed` plus bound action ID and outcome SHA-256. A read-only episode cannot claim a positive effect. Non-positive outcomes, including `not_applied`, `not_observed`, `unavailable`, and an unavailable observer, remain recordable with their typed reason/evidence and are never promoted to positive effect. `COORDINATED-WORK` owners carry `task_id`, `lease_id`, `chat_id`, and `source_sha256`; legacy `WORK` remains lineage-compatible.

## Inactive candidate and isolated adoption

`materialize_skill_candidate.py` accepts `mgskill-inactive-candidate-v1` for `CREATE`, `REVISE`, `MERGE`, `SUPERSEDE`, or `RETIRE`. With `--apply`, it requires a safe single-component candidate ID, candidate-root manifest CAS and every active discovery root; it rejects root intersections and creates only one instruction-only `SKILL.md`, a member-set, readback, and self-hashed receipt. `isolated_registry_adoption.py` reads registry bytes once, hashes those same bytes for CAS, JSON decoding and backup, then records `PREPARED`, `STAGED`, `COMMITTING`, `COMMITTED`, and `VERIFIED` transaction states. It verifies the receipt/current candidate bytes/member-set, owner/source, exact entry name/path/files and absent destination before staging; every fallible readback or receipt-assembly operation after rename, including after `VERIFIED`, preserves the exact committed destination and returns `OUTCOME_UNKNOWN` with readback rather than claiming no effect. Linked-resource candidates are explicitly unsupported. This proves only bounded staging; discovery, execution, effect and activation remain unproven.

## Effect candidate

`schema_version` is `mgskill-effect-v2`. Required fields are `event_id`, `objective_id`, `source_claims`, `skill_id`, `skill_version`, `registry_sha256`, `selection_snapshot_sha256`, `planned_objective_delta`, `actual_objective_delta`, `planned_evidence_delta`, `actual_evidence_delta`, `owner`, `trigger`, `candidate_identity`, `independent_challenge`, `retirement_trigger`, `unknown_case_disposition`, `outcome`, `consumer_result`, `side_effects`, `rollback_result`, `rework`, `proof_ceiling`, and `metrics`.

`outcome` is `advanced`, `applied`, `no_effect`, `recurred`, `false_block`, `misselected`, `prevented`, `outcome_unknown`, `not_observed`, `not_applied`, `unavailable`, or `not_applicable`. `prevented` additionally requires `pre_submission_block_observed=true`, a 64-hex `rejected_candidate_sha256`, `candidate_preserved=true`, and `applicable_set_equality=true`. It is a mechanical action result, not automatically inferred consumer benefit. `not_applied` means applicable knowledge or a selected constraint existed but did not screen the final action before submission; `unavailable` and `not_applicable` must not be encoded as zero-value benefit.

`metrics` has exactly these fields: `objective_evidence`, `mandatory_quality`, `elapsed_ms`, `token_usage`, `tool_calls`, `delegation`, `integration`, `rework`, `consumer_outcome`, `overhead`, `false_positive`, `late_miss`, and `recurrence`. Each is an object with `status` equal to `observed`, `unavailable`, or `not_applicable`. Observed metrics require `value`; unavailable/not-applicable metrics require `reason` and must not contain a placeholder value.

Accepted effects return a candidate-only recommended disposition. `recurred`, `misselected`, and `not_applied` recommend revise-or-merge; `false_block` recommends narrow-or-retire; `no_effect` recommends revise-or-retire; unknown/unobserved effects hold measurement. These recommendations never authorize semantic activation or a master write.

`independent_challenge.status` is `pending` or `completed`; completed requires a result. Pending challenge forces `hold-independent-challenge` regardless of the outcome recommendation. The active source-bound primary Worker owns event-driven candidate generation at the existing PENDING-EVENT/Stage Effect transition; no fixed polling or background daemon is implied.

## Transition candidate

`schema_version` remains `mgskill-transition-v1`. Required fields are `event_id`, `objective_id`, `source_claims`, `skill_id`, `from_state`, `to_state`, `reason`, `baseline`, `normal_path_counterexamples`, `independent_challenge`, `rollback`, `identity_snapshot`, `proof_ceiling`, and `ambiguity`. `identity_snapshot` contains exact registry, selection and skill content SHA-256 values.

Legal transitions remain `raw-event -> solution -> candidate -> shadow -> audited -> active-bounded -> measured`, followed by revise, merge, supersede, or retire. Direct `active-bounded -> retire` is allowed only with exact harm, containment and recovery owner. Material ambiguity returns `USER-DECISION`.

The validator proves schema and lifecycle/effect invariant closure only. Semantic merit, actual consumer outcome, empirical improvement and rollback success remain outside its proof ceiling.
