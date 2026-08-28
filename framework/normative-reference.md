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

## 16. Self-Improvement

Improve the framework through measured episodes, representative replay, independent challenge, rollback plans, and no-drop preservation. Do not claim empirical superiority from structural review alone.

## 17. Completion

Completion requires:

- Observable normal-path outcome for each primary objective.
- Mandatory acceptance criteria passed or explicitly excepted by the user.
- Candidate identity and evidence identity fixed.
- Required evidence routes within their proof ceilings.
- No reachable material finding left undispositioned.
- External action and completion eligibility when applicable.

Report residual empirical limits as measured status, not as hidden failure or exaggerated success.
