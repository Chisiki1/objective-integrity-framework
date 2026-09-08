---
name: master-guided-skill-lifecycle
description: Carry worthwhile workflow learning from a real result to an owned candidate, later matching use, and measured disposition. Use for material results, recurrence, reusable success, avoidable cost, capability changes, or a concrete condition blocker; do not turn ordinary reads into extra phases or treat records as benefit.
---

# Master-Guided Skill Lifecycle

Use this skill to close a learning loop, not merely validate a record. Read [learning-loop.md](references/learning-loop.md) when recording, retrieving, or advancing a candidate. The repository's broader [governance and self-improvement guide](../../../docs/governance-self-improvement.md) is optional context, not a required Skill dependency. Index and queue scripts never write source masters or activate a policy or skill. The source-authorized primary owner executes eligible actions and remains the single semantic writer.

At a material job-shape change, read [stage-allocation-contract.md](references/stage-allocation-contract.md) and run `python -B scripts/stage_allocation.py --input <view.json>`. Allocation is event-driven, not a fixed model ranking or periodic reselection.

Use `scripts/allocation_io.py state` for typed orchestration facts and `run --input <view.json> --evidence <new-result.jsonl>` for real selection consumption. Preserve raw output, inspect `selection_ready`, identity and selected configuration, and do not insist that context reuse has one particular success label. Python callers with a current transition pass its independently obtained `expected_identity`. The CLI binds its explicit input, not an unknown later live decision; the helper neither grants permission nor dispatches a job.

At a missing consumer connection, recurrence or support-heavy result, apply [convergence.md](references/convergence.md). Compare unchanged reuse, a small repair and a useful structural alternative: change the work unit, representation, entrypoint or ownership, or remove/consolidate machinery. When outside evidence can change that choice, apply [external-calibration.md](references/external-calibration.md) inside the same decision, with contrary evidence and actual host applicability. Technical documentation alone is not method-effectiveness evidence. Put the selected change into the next real action; do not add an idea quota, extra ledger or standing research phase.

For repeated bounded request/result assembly, use [work-entrypoint.md](references/work-entrypoint.md) and `scripts/work_io.py` instead of handwritten duplicate packets when the cost is justified. The caller maps the prepared payload to its actual internal-job API and handles dispatch, source meaning, integration and shared writes. Direct work is appropriate when no reusable handoff is needed.

For implementation that is looping through small check/fix cycles before the whole request is built, use [source-wide-execution.md](references/source-wide-execution.md). One scope/state drives `work_phase.py`, owner control, transition admission and work_io v2. Complete all required parts and consumer connections before formal checking; freeze and collect safe current-stage findings, then repair shared causes together. Decision-essential construction feedback stays available. Do not impose product phases on ordinary read-only research or claim helper invocation forces host compliance.

For a real condition improvement or blocker, read [condition-stewardship.md](references/condition-stewardship.md). The read-only `workflow_governance.py` checks proposal and reference consistency; it never grants authority. Exact later user source can amend a user condition within scope. Delegated methods may improve without a new user prompt when source meaning, normal path, review, recovery, and rollback remain intact. No candidate means no extra governance procedure.

1. Preserve the raw project event. Classify its causal family, solution status, applicability, and whether an existing invariant or skill already handles it.
2. Create an `mgskill-effect-v3` record matching [lifecycle-contract.md](references/lifecycle-contract.md), then run `python -B scripts/skill_lifecycle.py effect --input <file>`. Record planned and actual objective and evidence deltas separately. Keep applied, not applied, unavailable, not applicable, and false block distinct.
3. Use `prevented` only for an observed pre-submission block with the rejected candidate preserved and exact applicable-set equality. A known constraint that did not screen the final action is not applied.
4. For create, revise, merge, supersede, or retire, run `python -B scripts/skill_lifecycle.py transition --input <file>`. Treat its output as a candidate for the primary owner's semantic write, not an automatic transition.
5. Materialize only into an explicit inactive root with `materialize_skill_candidate.py` and an expected root-manifest hash. Adopt only through `isolated_registry_adoption.py` into an explicit isolated discovery root with destination-registry comparison, backup, and rollback. Both routes preview by default.
6. An instruction-only skill is applied only when a real action result and independent observation show its instructions changed the action. Reading or byte delivery is not application.
7. Activation needs source-bound benefit, baseline, representative replay or shadow evidence, normal-path counterexamples, independent challenge, bounded scope, rollback, and later measurement. If meaning or effect remains materially ambiguous, return `USER_DECISION`.

Lifecycle and discoverability are different states. Retired or superseded versions leave active discovery roots while immutable evidence and lineage remain available. Changed registry, skill, resource, policy, or source record creates a new identity cut and invalidates only dependent evidence.
