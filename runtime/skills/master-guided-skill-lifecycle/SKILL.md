---
name: master-guided-skill-lifecycle
description: Carry worthwhile workflow learning from a real result to an owned candidate, later matching use, and measured disposition. Use for material results, recurrence, reusable success, avoidable cost, capability changes, or a concrete condition blocker; do not turn ordinary reads into extra phases or treat records as benefit.
---

# Master-Guided Skill Lifecycle

Use this skill to close a learning loop, not merely validate a record. Read [learning-loop.md](references/learning-loop.md) when recording, retrieving, or advancing a candidate. The repository's broader [governance and self-improvement guide](../../../docs/governance-self-improvement.md) is optional context, not a required Skill dependency. Index and queue scripts never write source masters or activate a policy or skill. The source-authorized primary owner executes eligible actions and remains the single semantic writer.

At a material job-shape change, read [stage-allocation-contract.md](references/stage-allocation-contract.md) and run `python -B scripts/stage_allocation.py --input <view.json>`. Allocation is event-driven, not a fixed model ranking or periodic reselection.

For a real condition improvement or blocker, read [condition-stewardship.md](references/condition-stewardship.md). The read-only `workflow_governance.py` checks proposal and reference consistency; it never grants authority. Exact later user source can amend a user condition within scope. Delegated methods may improve without a new user prompt when source meaning, normal path, review, recovery, and rollback remain intact. No candidate means no extra governance procedure.

1. Preserve the raw project event. Classify its causal family, solution status, applicability, and whether an existing invariant or skill already handles it.
2. Create an `mgskill-effect-v3` record matching [lifecycle-contract.md](references/lifecycle-contract.md), then run `python -B scripts/skill_lifecycle.py effect --input <file>`. Record planned and actual objective and evidence deltas separately. Keep applied, not applied, unavailable, not applicable, and false block distinct.
3. Use `prevented` only for an observed pre-submission block with the rejected candidate preserved and exact applicable-set equality. A known constraint that did not screen the final action is not applied.
4. For create, revise, merge, supersede, or retire, run `python -B scripts/skill_lifecycle.py transition --input <file>`. Treat its output as a candidate for the primary owner's semantic write, not an automatic transition.
5. Materialize only into an explicit inactive root with `materialize_skill_candidate.py` and an expected root-manifest hash. Adopt only through `isolated_registry_adoption.py` into an explicit isolated discovery root with destination-registry comparison, backup, and rollback. Both routes preview by default.
6. An instruction-only skill is applied only when a real action result and independent observation show its instructions changed the action. Reading or byte delivery is not application.
7. Activation needs source-bound benefit, baseline, representative replay or shadow evidence, normal-path counterexamples, independent challenge, bounded scope, rollback, and later measurement. If meaning or effect remains materially ambiguous, return `USER_DECISION`.

Lifecycle and discoverability are different states. Retired or superseded versions leave active discovery roots while immutable evidence and lineage remain available. Changed registry, skill, resource, policy, or source record creates a new identity cut and invalidates only dependent evidence.
