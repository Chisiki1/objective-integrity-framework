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

Keep every source-bound deliverable open until its observable outcome is satisfied, withdrawn, superseded, or deferred by the user. Answering a subquestion, completing an audit, or passing a verifier does not satisfy an open parent deliverable by itself.

For work that can resume, compact, hand off, queue, retry, or outlive one turn, maintain one durable ledger per logical objective tree:

- Preserve exact source events as immutable content with identity and delivery lineage.
- Classify later events as `ADD`, `CLARIFY`, `CORRECT`, `REPLACE`, or `WITHDRAW` against every affected open outcome.
- Keep append-only journal history, a replayed machine projection, and a concise human projection separate.
- Bind writes to an expected current head so stale writers do not append semantic state.
- Preserve pending, partial, and unknown action effects until evidence-linked reconciliation.
- Recover partial tails, abandoned locks, source-integrity gaps, and output failures without rewriting committed history.

Raw event capture is evidence that input was received. The primary owner still determines its semantic relation to the active objective; a host hook, session identifier, child context, or copied prompt does not create authority by itself.

Reuse that ledger for additions, clarification, resume and compaction in the same logical conversation; forks receive separate identities. Read it at those boundaries, after material results and completed items, and before final delivery. Factor repeated source triples in the human view without dropping clause locators, open outcomes or immutable history. Keep one current completion-scope reference rather than competing definitions of done.

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

At a decision-bearing result, disposition the event as `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, or `WAIT`. A wait is valuable only at a decision window where new evidence can change the next action. Status narration without objective or evidence delta is not objective control.

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

Keep one source-wide scope for all required implementation items, connections and stage-appropriate checks. Bounded construction jobs do not narrow that scope.

- BUILD completes the required implementation and connections. Small construction checks may answer a concrete next-build question; they do not accept an incomplete scope.
- SWEEP freezes the complete candidate and gathers all safe independent findings for the current stage. Structural review can precede behavior-dependent checks when needed; it is not a per-component release cycle.
- REPAIR starts from the collected findings, distinguishes mandatory source-backed defects from optional improvements, challenges material root causes and repairs connected causes together.
- ACCEPT uses the original consumer outcome and required evidence, with final and pre-action review where they can change a decision.

Preserve first faults and blocked dependent checks. Continue safe independent partitions rather than fixing the first visible issue before discovering the rest. External-action prerequisites remain action-relative; describing an operation as construction feedback does not bypass them.

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

Choose model capability, reasoning depth, tools, subagents, job reuse, and parallelism from the actual deliverable, hardest cognitive operation, context and dependency shape, modality, ambiguity, evidence conflict, verification route, decomposability, latency, reversibility, and concrete error consequences. Domain and risk labels are inputs to assurance allocation, not fixed model tiers.

Record capability requirements, eligible configurations, requested and accepted configuration, effective metadata when observable, unavailable fields, expected total cost, and the concrete failure predicted from a weaker configuration. High consequence may require stronger independent verification without automatically selecting the most expensive executor.

One primary owner normally preserves the objective and performs authorized work in the same conversation. That owner classifies source events, consumes decision-bearing results, records action effects, and remains the single shared semantic writer for the objective tree.

Independent review remains separate from execution:

- The reviewer starts from the authorized source and fixed evidence rather than inheriting the owner's conclusion.
- The reviewer remains read-only toward the candidate, verifier, external state, and shared learning records it reviews.
- The owner consumes the result as `CONSUME`, `REFUTE`, `CORRECT`, `USER_DECISION`, or `WAIT`.
- Parallel implementation uses bounded jobs with non-overlapping resource claims and a positive total-value case.

A legacy coordinator/executor status channel is used only to recover an explicitly existing split-task lease. It does not create a second default topology, duplicate action owner, or automatic independent-review key.

Use typed allocation preparation and consume the actual fresh result before acting on it. A requested model is not observed effective metadata. Source-bound handoffs retain scope, resource claims, remaining work and proof ceiling; their dispatch-shaped output does not itself dispatch a job. Reuse genuinely read unchanged coverage, but one reader's receipt never certifies another reader's required reading or independent judgment.

## 15. Continual Learning

Use project and global learning records when the cost is justified:

- Project records preserve exact evidence, failures, commands, local constraints, and return steps.
- Global records promote sanitized reusable knowledge, applicability, proof ceiling, and action constraints.

Learning records do not replace current source inspection. A confirmed lesson has prevention value only when it is projected onto the exact matching action before execution.

An actionable learning queue binds each candidate to its source objective, causal family, owner, artifact, next-use trigger, graph references, expected effect, rollback, immutable events, and current disposition. Supported states include `discovered`, `prepared`, `evaluated`, `active-bounded`, `measured`, `deferred`, `retired`, and `superseded`. A due query retrieves possible next work; it grants neither authority nor semantic applicability.

A source-hash-bound index may make current controls and history retrievable. It preserves every occurrence and bounded cursor identity. It is a projection over source records, not a new semantic master.

Organize touched knowledge families after meaningful results, recurrence or avoidable retrieval cost. Reconcile every affected current event, control header, index and family at the same update; route volatile state to its existing objective/work record rather than duplicating it. Explicit whole-section keep/replace/archive dispositions preserve occurrences, ancestry, roles, unrelated ownership and unresolved obligations. Publish complete old backing first, then compare-and-swap the reviewed current replacement and verify actual history reachability. Retrieval includes declared archives, factors identical text without merging different meanings, and exposes every origin separately. A compact query or index is not semantic consolidation. Queue metadata corrections retain event history without claiming a new effect or lifecycle advancement.

Connect learning at the actual result owner during work. Internal jobs use result consumption and result-derived next requests; recurring native operations preserve exit status, raw streams and first fault before display. Compare reuse, revision, creation, merging, narrowing, retirement, no change and concrete deferral. Create only complete necessary Skill packages and carry an eligible improvement into the next required operation after authority, independent challenge, normal-path evidence and recovery close. Do not wait for task end or a new prompt. Every continuing actor personally reconciles affected methods at safe boundaries, reusing its own unchanged reads and retaining in-flight versions and partial or unknown effects. Hooks can assist on supported, trusted, observed surfaces; filenames, notifications, hashes, empty display and generated requests are not proof of reading, use, effect or permission. No duplicate invocation, trivial-read ticket, Skill quota, competing semantic ledger, daemon or Stop continuation is implied.

Metrics distinguish observed, unavailable, and not applicable values. Missing evidence is never encoded as numeric zero.

## 16. Skill Book Plane

Use a Skill Book when reusable instructions or deterministic helpers should be projected into the current work without turning the catalog into the objective.

The framework keeps three independent planes:

- The workflow remains the authority for objective, scope, scenarios, root cause, impact, verification, audit, action eligibility, and completion.
- Project and global learning records preserve exact local evidence and sanitized reusable families.
- The Skill Book resolves focused instructions and deterministic scripts for the current work unit.

No plane promotes its evidence into another plane without an explicit evidence route. A skill, registry, resolver, script, selection receipt, lifecycle status, or audit never replaces the user source, current artifact, runtime, external state, blind scenario work, independent review, or final consumer result.

### Resolution

Before resolution, compile provenance-bound facts from every source clause and the finalized job, action, tool payload, environment, permission, resource, and consumer. Each clause either emits evidence-backed selection facts or carries an explicit no-selection-fact disposition. Preserve source and payload hashes, action finality, tool-schema state, and a negative-selection challenge. Desired skill names or caller conclusions are not admissible facts.

A resolver derives exact, near, rejected, and no-match entries from the compiled facts, not from registry order, fuzzy description similarity, popularity, model preference, or first match. A receipt binds the objective contract, source claims, compiler result, job/action/tool/environment facts, registry hash, selected and rejected reasons, required inputs, resource claims, expected deltas, proof ceiling, expiry, rollback, canonical path, lexical path, linked file hashes, reparse state, negative countermodel, and selection snapshot hash.

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

### Application Bridge

Selection does not establish use. For a material application, bind one graph across exact source, objective owner, lease or work-unit identity, compiled finalized action, resolver snapshot, candidate member set, selected script, typed schema, resource claim, expected result, and effect route.

The application bridge opens and hash-verifies selected non-UI bytes, records bytes delivered, revalidates the same graph immediately before execution, executes only the selected bounded script with exact arguments, captures raw result bytes and exit status, and later attaches independent effect observation. It is not a general-purpose command runner.

Keep these states distinct: selected, bytes delivered, executed, action result, consumer effect observed, and lifecycle disposition. Hash-only or unavailable reading, stale identity, graph mismatch, missing action finality, or a hollow effect leaves only the dependent application claim unresolved.

### Single Writer and Retirement

A single semantic writer should own registry or shared-learning updates for a work unit. Parallel jobs may read the same immutable snapshot or clearly separated snapshots with resource claims. They should not directly write shared registries or learning ledgers in parallel.

Retirement requires more than a status change. Superseded and retired skills should leave active discovery roots, while immutable evidence and lineage remain preserved outside those roots. Replacement identity, reason, effective time, prior status, proof ceiling, and rollback path should be explicit.

Skill scripts may provide deterministic structural evidence for schema, set equality, paths, hashes, typed matchers, exact-action discriminators, and lifecycle transitions. They should not decide semantic applicability, root cause, scenario completeness, reachability or harm, intervention safety, audit sufficiency, runtime discovery, consumer outcome, or empirical superiority.

## 17. Self-Improvement

Improve the framework through measured episodes, representative replay, independent challenge, rollback plans, no-drop preservation, and later consumer effects. Every material candidate names its owner, source, reusable family, actual artifact, next eligible use, expected effect, and retirement trigger.

Prefer revise, narrow, merge, replace, or retire when an existing method can close the gap. Add a new mechanism only when existing generic invariants cannot express a reusable material need and the expected outcome or total-cost benefit exceeds added complexity. Registration, validation, selection, document count, and queue state are not improvement outcomes.

When existing authority, preservation, representative normal-path evidence, independent challenge, bounded adoption and rollback are satisfied, carry a worthwhile candidate into its next real use without waiting for another prompt. At stalled integration, recurrence, support-heavy results or capability changes, also compare structural alternatives: change the work unit, entrypoint, representation or ownership, or consolidate/remove mechanisms. Neither novelty nor a brainstorming quota is a criterion.

Use focused current primary-source research and counterevidence when they can change the decision. Distinguish available API features from the actual host's tools, configuration, trusted delivery and observed use. Evaluate objective progress and total delivery cost including induced rework; unavailable metrics are not zero.

## 18. Condition Stewardship

Classify conditions by authority:

- `HIGHER_PRIORITY`: platform, system, developer, or tool constraints outside project-level amendment.
- `USER_CONDITION`: source-authored objective, acceptance, constraint, permission, or requested method.
- `DELEGATED_METHOD`: an implementation method the owner may improve while preserving source meaning.
- `UNKNOWN`: authority that must be resolved for its dependent decision.

A delegated method may change through the normal improvement lifecycle. A user condition changes meaning only through an exact later user source bound to the current condition, proposed replacement, affected condition set, scope, dependencies, effective period, expected benefit and total cost, lost guarantees, independent challenge, rollback, and later effect route.

Approval-shaped text, a matching hash, silence, queue activation, or a machine validator does not grant authority. An admitted amendment supersedes the prior condition only in its exact scope, preserves old and new source lineage, retains every unaffected condition, and remains reversible through its declared recovery route.

Reuse a verified unchanged current-condition inventory for a delegated-method improvement. A pending amendment holds only the dependent change, not improvements that already preserve current conditions. An exact approved amendment uses the newly authorized criteria without circularly requiring the superseded text; unaffected obligations and downstream action authority remain binding.

## 19. Material Transition Admission

Before a material correction, selected-skill action, external action, or other candidate-bearing transition, bind one identity graph across:

- exact source authority or an independent refutation key;
- causal or design readiness;
- frozen scenario and interaction identity;
- planned semantic and evidence delta;
- omission consequence and return step;
- allocation result and selected guidance application;
- local and relational claims, recomposition witness, and final consumer oracle;
- candidate, action, result, and later effect identities.

The transition record checks that those references belong to the same work unit and current snapshot. It does not prove the semantic truth of the cause, scenarios, witness, safety, or consumer outcome. Missing structure changes only the dependent transition to diagnosis or advisory work; unrelated read-only work continues.

For source-wide work, v3 admission and owner control consume the same current phase/scope binding. Check that binding before classifying an action as lightweight: formal verification cannot pass as construction feedback, and a repair cannot bypass whole-stage findings collection. Legacy ordinary or split-lease records apply only to their explicit compatibility paths.

## 20. Completion

Completion requires:

- Observable normal-path outcome for each primary objective.
- Mandatory acceptance criteria passed or explicitly excepted by the user.
- Candidate identity and evidence identity fixed.
- Required evidence routes within their proof ceilings.
- No reachable material finding left undispositioned.
- External action and completion eligibility when applicable.

Report residual empirical limits as measured status, not as hidden failure or exaggerated success.
