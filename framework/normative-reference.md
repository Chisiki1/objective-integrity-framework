# Normative Reference

This reference is intentionally more complete than the quick-start path. Use it when work is material, risky, long-running, public, external, or correction-heavy.

## 1. Objective Contract

Create a versioned objective contract before planning material work.

Classify each source clause as one of:

- Primary objective.
- Independent deliverable.
- Acceptance criterion.
- Constraint or authority boundary.
- Method or work preference.
- Priority or ordering condition.
- Optional preference or example.

The primary objective is the observable result the user or downstream consumer should receive. Later tests, blockers, reviews, and helper tasks do not replace it unless the user explicitly changes the objective.

## 2. Open Deliverable Ledger

Keep every source-bound deliverable open until its observable outcome is satisfied, withdrawn, superseded, or deferred by the user. A answered subquestion, completed audit, or passing verifier does not satisfy an open parent deliverable by itself.

## 3. Active Objective Integrity View

Before and after material actions, compare:

- Current objective identity.
- Open deliverables.
- Active objective-necessity link.
- Expected evidence delta.
- Actual evidence delta.
- Prohibited substitutes.
- Blocker return step.
- Newly introduced mandatory conditions.

If drift is detected, hold only the dependent next action, preserve evidence, restore the last valid objective checkpoint, and resume the exact eligible objective work.

## 4. Baseline and Semantic Authority

Freeze enough baseline to distinguish existing state from change-induced state. Name which legacy behavior, document, test, or runtime is authoritative and for what semantics. A legacy implementation is not automatically authoritative for language, timing, locking, object layout, or process details unless those details are externally required.

## 5. Raw Evidence and Root Cause

For failures, preserve raw evidence before causal interpretation:

- Inputs, environment, version, state, command, and time window.
- Primary symptom and neighboring negative evidence.
- First fault, propagation symptoms, detector symptoms, terminal effects, and recovery failures.
- Affected and unaffected consumers.

Then build a provisional causal ledger. Confirm root cause through deterministic closure or independent challenge when meaning, alternatives, or material harm justify it.

## 6. Scenario and Interaction Closure

Generate scenario families from orthogonal lenses:

- Outcome failure.
- State, lifecycle, and identity.
- Control, data, contract, and consumer.
- Order, time, and concurrency.
- Dependency, resource, permission, and target.
- Detection, containment, and recovery.
- Boundary-state continuity.
- Workload progress and fairness.

Interaction closure means preserving the reachable combinations that can change objective harm, state transition, side effect, oracle, containment, recovery, or final consumer result. Factor material families by activation conditions, owner, consumer, state, order, resource, guard, side effect, retry, recovery, oracle, and harm. Represent interactions with a causal graph or hypergraph. Exhaust small finite reachable components where practical; otherwise use constraints, state machines, properties, representative partitions, and noninteraction proofs.

For boundary-state continuity, record the state family, source of truth, owner, lifecycle phase, clock or ordering basis, capture or transfer gap, first consumer decision, semantic-equivalence oracle, and retry or rebuild route.

For workload progress and fairness, record offered workload, admitted workload, progress obligation, quiescence, overload or backpressure behavior, scheduler and priority interactions, shared resources, and recovery under continued load.

## 7. Semantic Recomposition

When system behavior is verified through smaller checks, lock the original system claim first. Map it to local claims and relational claims for shared state, identity, ownership, data flow, order, clock, concurrency, resources, side effects, persistence, recovery, and final consumer result.

Each material factor is one of:

- `PRESERVED`: kept with the same meaning.
- `RECOMPOSED`: proven through local and relational evidence.
- `EQUIVALENT`: represented by a composition-congruent substitute.
- `CUT`: affirmatively nonreachable or noninteracting.
- `DEFERRED-UNPROVEN`: left unproven with its dependent claim held.
- `REJECTED`: out of scope, nonmaterial, or not worth current cost with reasons.

## 8. Intervention and Change Impact

Before changing a candidate, state:

- The causal or design link being changed.
- The supported normal-success envelope.
- Preservation contracts.
- Expected semantic delta.
- Potential new failures.
- Unmasking frontier after an earlier fault or guard is removed.

After the change, compare intended and actual delta. Do not let passing tests erase an unexpected impact.

## 9. Verification Model

Treat product root cause, escaped product failure, and faulty verification model as separate claims. A test can be wrong because of fixture state, identity, order, clock, observer effect, oracle, assertion dependency, or report handling. Repair the shared verification model when the same family recurs.

## 10. Audit Placement

Use audits where they can change a decision:

- Root-cause challenge after diagnosis and before correction.
- Early structural audit after implementation snapshot and before behavioral tests.
- Final audit on the tested release candidate.
- Pre-action audit before external writes or completion claims.

Do not repeat unchanged audits for wording, reviewer, model, or hash changes alone. Re-enter only for changed claims, new evidence, or invalidated dependency boundaries.

## 11. External Action Eligibility

Before public release, deployment, production mutation, or completion assertion, project eligibility from the objective contract, evidence map, audit verdicts, proof ceilings, identities, freshness, and exceptions.

Distinguish:

- Non-executing transfer.
- Bounded diagnostic or staging activation.
- Operational application.
- Completion assertion.

Lower-plane evidence does not prove higher-plane consumer outcomes.

## 12. Diagnostic Preservation

At aggregation boundaries, preserve the first decision-bearing fault, contributing conditions, cleanup or recovery failures, affected and unaffected consumers, and unobserved scope. Continue only safe independent diagnostics with objective-bound decision value. Avoid log spam and avoid stop guards that exist only to make reporting easier.

## 13. Guard and Stop Composition

For each stop, hold, reject, timeout, fallback, retry, or fail-closed rule, record authority, activation, effect, unique value, narrower alternative, recovery, and normal-path preservation. Evaluate the composition of all guards, not only each guard in isolation.

## 14. Allocation and Delegation

Choose model, reasoning depth, subagents, Worker reuse, and parallelism from task content and total cost. Domain labels are metadata, not automatic escalation. Delegate when independence, speed, or quality benefit exceeds instruction, startup, waiting, integration, and reverification cost.

Supervisor and Worker separation can help long tasks. In public-facing terms, this is a Coordinator and Executor topology.

- The Coordinator preserves the source-bound objective, monitors decision-bearing events, and consumes Executor results by pull.
- The Executor performs the objective-bound work and keeps progress, audit results, needs-attention states, and proof-carrying finals in its own work channel.
- There is normally one primary Executor for one objective tree. Parallel work needs non-overlapping resource claims and positive total value.
- Executor-to-Coordinator user-visible push is avoided unless the runtime provides an explicit non-contaminating mailbox.
- The Coordinator is read-only toward product, verifier, external state, and shared learning ledgers unless a separate authority grants a write.
- Result consumption is explicit: consume, refute, correct, ask for user decision, or wait. A status update without objective evidence delta is not supervision.

The Coordinator must not become a second Executor or invent new requirements.

## 15. Continual Learning

Use project and global learning records when the cost is justified:

- Project records preserve exact evidence, failures, commands, local constraints, and return steps.
- Global records promote sanitized reusable knowledge, applicability, proof ceiling, and action constraints.

Learning records do not replace current source inspection. A confirmed lesson has prevention value only when it is projected onto the exact matching action before execution.

## 16. Skill Book Plane

Use a Skill Book when reusable instructions or deterministic helpers should be projected into the current work without turning the catalog into the objective.

The framework keeps three independent planes:

- The workflow remains the authority for objective, scope, scenarios, root cause, impact, verification, audit, action eligibility, and completion.
- Project and global learning records preserve exact local evidence and sanitized reusable families.
- The Skill Book resolves focused instructions and deterministic scripts for the current work unit.

No plane promotes its evidence into another plane without an explicit evidence route. A skill, registry, resolver, script, selection receipt, lifecycle status, or audit never replaces the user source, current artifact, runtime, external state, blind scenario work, independent review, or final consumer result.

### Resolution

A resolver should derive selected and rejected entries from structured work facts, not from registry order, fuzzy description similarity, popularity, model preference, or first match. A receipt should bind the objective contract, source claims, job/action/tool/environment facts, registry hash, selected and rejected reasons, required inputs, resource claims, expected deltas, proof ceiling, expiry, rollback, canonical path, lexical path, linked file hashes, and a selection snapshot hash.

Only `active-bounded` and `measured` entries are normally selectable. `candidate`, `shadow`, `audited`, `superseded`, and `retired` entries can remain for lineage but should not be normal resolution targets. Missing, stale, unreadable, conflicting, or expired entries hold only their dependent skill route. The normal workflow continues for unrelated work, and independently applicable exact-action constraints still apply.

Resolvers should preserve path identity. Walk the lexical root-to-skill path before resolving it, record symlinks, junctions, and other reparse components with resolved targets, require the final physical path to remain under the resolved origin root, and include that information in the snapshot. A retargeted link, changed file, changed registry status, or changed objective fact invalidates the old snapshot before decision-bearing use.

Duplicate and ancestor precedence must be explicit. If a user-level and project-level skill share a name, the resolver should either follow a declared precedence rule with lineage or return a conflict/user-decision result. It should not silently choose whichever entry is encountered first.

### Blind-Safe Projection

For blind-first diagnosis, scenario generation, or audit work, expose only non-conclusion mechanical entries before the initial derivation is frozen. Syntax, schema, path, permission, tool-contract, and execution-hygiene constraints can be blind-safe when they do not reveal causal, patch, test, scenario, or reviewer conclusions.

Non-blind-safe skill identity, names, descriptions, triggers, selected/rejected reasons, paths, and references are withheld by count and release condition until the initial derivation is frozen. After that, rerun resolution in a reconciled phase and consume the full identity-bound receipt.

### Lifecycle and Effects

Material skill events should receive a rule-bound disposition such as existing skill applicable, existing generic invariant sufficient, candidate, merge candidate, revise candidate, supersede candidate, retire candidate, project-local no-skill, global family candidate, unconfirmed, or rejected.

The ordinary lifecycle is:

```text
raw event -> causal family and solution -> disposition -> candidate -> shadow or replay -> independent audit -> active-bounded -> measured -> revise, merge, supersede, or retire
```

This is lineage, not a fixed phase quota. A single failure, a recurrence count, a model suggestion, registry validation, test pass, or self-authored documentation review is not enough to activate a semantic skill. Activation needs a source-bound benefit, baseline, representative replay or shadow evidence, normal-path counterexamples, independent challenge, bounded activation, rollback, measurement, and retirement trigger. Material ambiguity returns a user decision rather than an automatic transition.

Effect records should distinguish planned and actual objective deltas, planned and actual evidence deltas, consumer result, side effects, rollback, elapsed time, tool/delegation cost, rework, counterevidence, and proof ceiling. Public effect outcomes include `advanced`, `no_effect`, `recurred`, `false_block`, `misselected`, `prevented`, `outcome_unknown`, and `not_observed`.

`prevented` is limited to an observed pre-submission block with the rejected candidate preserved and the applicable constraint set matched to the final action representation. Post-submission parser rejection, exceptions, no-ops, and later repairs are containment evidence. They do not prove prevention or avoided consumer harm.

### Single Writer and Retirement

A single semantic writer should own registry or shared-learning updates for a work unit. Parallel jobs may read the same immutable snapshot or clearly separated snapshots with resource claims. They should not directly write shared registries or learning ledgers in parallel.

Retirement requires more than a status change. Superseded and retired skills should leave active discovery roots, while immutable evidence and lineage remain preserved outside those roots. Replacement identity, reason, effective time, prior status, proof ceiling, and rollback path should be explicit.

Skill scripts may provide deterministic structural evidence for schema, set equality, paths, hashes, typed matchers, exact-action discriminators, and lifecycle transitions. They should not decide semantic applicability, root cause, scenario completeness, reachability or harm, intervention safety, audit sufficiency, runtime discovery, consumer outcome, or empirical superiority.

## 17. Self-Improvement

Improve the framework through measured episodes, representative replay, independent challenge, rollback plans, and no-drop preservation. Do not claim empirical superiority from structural review alone.

## 18. Completion

Completion requires:

- Observable normal-path outcome for each primary objective.
- Mandatory acceptance criteria passed or explicitly excepted by the user.
- Candidate identity and evidence identity fixed.
- Required evidence routes within their proof ceilings.
- No reachable material finding left undispositioned.
- External action and completion eligibility when applicable.

Report residual empirical limits as measured status, not as hidden failure or exaggerated success.
