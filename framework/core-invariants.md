# Core Invariants

These invariants are the compact form of Objective Integrity Framework.

## 1. Preserve the Source-Bound Objective

Derive the objective from the user's source, not from the agent's plan, a test name, an audit finding, or a later convenience. Keep independent deliverables, acceptance criteria, constraints, means, and preferences distinct.

Preserve later source events and their classification in one durable logical ledger. `ADD`, `CLARIFY`, and `CORRECT` do not silently retire existing outcomes. `REPLACE` and `WITHDRAW` require explicit source lineage. Raw capture is not automatic semantic authority.

## 2. Keep Means Subordinate

Tests, audits, verifiers, blockers, safety checks, subagents, skills, and learning records exist to advance the requested outcome. They do not become the outcome unless the user explicitly made them deliverables.

## 3. Require Objective-Necessity Links

Every material action states which source-bound claim it advances, what would remain failed or unproven if omitted, its proof ceiling, and the next eligible objective-bound step.

## 4. Separate Evidence From Interpretation

Raw observations, causal hypotheses, findings, scenarios, intervention impact, and correction authorization are separate records. Do not patch from the first visible error.

## 5. Generate Scenarios Before Anchoring

For material changes and actions, derive outcome-threatening scenarios from the objective, baseline, architecture, state, boundaries, workload, and consumers before anchoring on one error, test, or patch.

## 6. Close Reachable Interactions

Cover reachable combinations that can change harm, state transition, side effect, oracle, containment, recovery, or final consumer result. Use equivalence classes and causal graphs instead of naive Cartesian expansion.

## 7. Recompose Decomposed Evidence

When a system claim is split into component checks, preserve the local and relational claims needed to prove the original claim. Component pass is not system pass without a recomposition witness.

## 8. Preserve Normal Success

Guards, stops, holds, retries, fallbacks, and fail-closed behavior must preserve the supported normal path or clearly expose the accepted tradeoff and recovery path.

## 9. Respect Proof Ceilings

Do not promote static, package, component, verifier, runtime, or external evidence beyond what it can prove. Claims crossing a consumer boundary need evidence at that boundary or a valid bridge.

## 10. Resolve Focused Guidance Deterministically

When reusable skills or deterministic scripts are used, compile facts from every source clause and the finalized action payload, including a negative-selection challenge. Resolve selected, near, rejected, and no-match entries with version, origin, canonical path, linked hashes, blind-safety rules, and stale-snapshot checks. Missing or conflicting skills fall back to the normal workflow for unrelated work; they do not bypass independently applicable action constraints.

## 11. Learn Into Action

Learning records matter when they constrain future matching actions, reduce repeated mistakes, or improve measured outcomes. Use two layers when the cost is justified: a project-local learning ledger for exact evidence and a sanitized shared ledger for reusable action knowledge. Keep reading, selection, bytes delivered, execution, action result, and observed effect distinct. Recording a lesson is not evidence that the next action used it.

## 12. Keep One Owner and Independent Refutation

One primary owner normally maintains the objective and performs authorized work in the same conversation. Independent reviewers remain read-only toward the target they review. Legacy split-task status is compatibility for an explicitly existing lease, not the default workflow.

## 13. Preserve Effects and First Faults

At every aggregation boundary, retain the initiating fault, contributing conditions, propagation, cleanup or recovery failures, affected and unaffected consumers, and unobserved scope. A retry does not erase a partial or unknown prior effect; reconcile it first.

## 14. Govern Conditions From Their Source

Improve delegated methods autonomously when the source meaning and supported normal path remain intact. Change a user-authored condition only through an exact later user source bound to the old and new condition set, scope, dependencies, lost guarantees, and rollback. Structural matching never creates authority.

## 15. Allocate From Work Shape

Choose model capability, reasoning, tools, delegation, and independent review from the actual deliverable, ambiguity, dependencies, evidence route, decomposability, reversibility, and error cost. Domain labels and maximum settings are not allocation rules.
