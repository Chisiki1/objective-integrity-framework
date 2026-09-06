# Architecture

Objective Integrity Framework has one outcome path and three supporting planes. The outcome path begins at the user's source and ends at the final consumer; the supporting planes provide workflow rules, durable learning, and focused reusable guidance.

## Primary Outcome Path

```text
exact source
-> durable objective and open outcomes
-> objective-necessary action
-> evidence-bearing result
-> final consumer outcome
-> measured learning disposition
-> later exact match
```

One same-conversation primary owner normally carries this path. Independent review is read-only toward its target. Material results return to the exact next objective-bound step rather than ending at a receipt, review, or status update.

## Record Layer

- `Objective Contract`: maps the source request into primary objective, independent deliverables, acceptance criteria, constraints, means, preferences, and assumptions.
- `Open Deliverable Ledger`: keeps source-bound deliverables open until their observable outcomes are satisfied.
- `Evidence Map`: connects claims to evidence routes and proof ceilings.
- `Scenario and Interaction Ledger`: preserves outcome-threatening families and the interaction graph between them.
- `Audit Receipt`: records review scope, findings, proof ceiling, and unresolved claims.
- `Correction Authorization`: separates diagnosis and review findings from permission to change the candidate.
- `Effect Episode`: measures outcome, cost, regressions, and learning value after a transition.
- `Knowledge Masters`: store exact project evidence and sanitized reusable knowledge without becoming the objective.
- `Skill Selection Receipt`: records selected and rejected reusable skills, path and hash identity, blind-safety status, expected deltas, rollback, and proof ceiling.
- `Skill Lifecycle Record`: records whether a skill event becomes no-change, candidate, shadow, active-bounded, measured, revised, merged, superseded, or retired.
- `Exact-Action Preflight`: records bounded mechanical checks over the exact action representation before execution.
- `Objective Ledger`: preserves immutable source events, explicit source dispositions, open outcomes, progress, pending effects, recovery, and the current human projection.
- `Learning Queue`: owns actionable improvement candidates, immutable lifecycle events, due triggers, metrics, and later-consumer references without duplicating raw incidents.
- `Master Index`: binds source hashes and provides bounded retrieval across current controls and history.
- `Transition Admission`: joins source authority or refutation, scenario identity, planned impact, allocation, selected guidance, recomposition, consumer oracle, and later effect for one material transition.

## Transition Layer

1. Capture the exact source and classify objective, deliverables, conditions, authority, methods, and preferences.
2. Reuse or create one logical objective ledger; preserve all open outcomes and pending effects.
3. Freeze baseline, normal success, and semantic authority boundaries.
4. Generate blind scenarios and reachable interactions before anchoring on the first error or proposed patch.
5. Choose only work with a source-bound omission consequence, then allocate capability, reasoning, tools, and independent review from the actual job shape.
6. Resolve reusable guidance from provenance-bound source and finalized action facts when it will change the work.
7. Admit a material correction or action only after its causal, scenario, impact, recomposition, consumer, and recovery relations are bound.
8. Audit and test at the stage where each evidence route can change a decision, preserving the system claim across decomposed checks.
9. Project action eligibility before external writes or completion claims, then preserve the real outcome and any partial or unknown effect.
10. Turn a reusable result into a candidate, later matching action, observed effect, and revise/retire decision.

## Governance Planes

The framework keeps three governance planes independent:

- `WORKFLOW`: normative authority for objective, scope, evidence, scenarios, root cause, impact, verification, audit, action eligibility, and completion.
- `LEARNING`: project-local raw evidence and sanitized global reusable knowledge.
- `SKILL BOOK`: focused instructions, deterministic helper scripts, selected/rejected resolver receipts, and skill lifecycle evidence for the current work unit.

These planes cooperate through identity-bound references. None replaces another. A skill receipt does not prove semantic correctness. A learning entry does not prove the next action used it. A workflow checklist does not prove the final consumer result.

## Ownership and Review

- One primary owner classifies source events, performs authorized work, records action outcomes, and owns shared semantic writes for one objective tree.
- Independent reviewers derive their own challenge from the authorized source and fixed evidence, then remain read-only toward the reviewed target.
- Parallel jobs declare non-overlapping resources and return proof-carrying results to the primary owner.
- Legacy split-task status is used only to recover an explicitly existing lease; it does not define the default architecture.

## State and Boundary Continuity

The framework treats state capture, transfer, replay, rebuild, and first decision-bearing consumption as one relation. Each state family names its source, owner, lifecycle, clock or ordering basis, gap, consumer, equivalence oracle, and recovery. Exact bytes are identity evidence; semantic continuity depends on the consumer meaning.

The same discipline applies to long-running progress. Component health, empty error logs, and local queue checks do not replace the supported workload, fairness, convergence, recovery, and final progress obligation.

## Runtime and Evidence Identities

The framework keeps five identities separate:

- `PRODUCT`: source, configuration, artifacts, persistent candidate state, and runtime inputs.
- `VERIFIER`: tests, fixtures, oracles, scanners, validators, and review scripts.
- `EVIDENCE`: logs, captures, reports, timestamps, and manifests.
- `ENVIRONMENT`: toolchain, operating system, services, permissions, and external dependencies.
- `EXTERNAL`: public repositories, production systems, deployed resources, accounts, and consumer-visible effects.

Keeping these planes separate prevents a lower-level pass from being promoted beyond what it can prove.

## Package Layout

- `framework/`: portable normative rules and compact invariants.
- `profiles/`: proportional operating modes.
- `runtime/`: executable generic continuity, learning, selection, application, scenario, transition, and objective-control mechanisms.
- `.agents/skills/objective-integrity/`: progressive agent guidance.
- `templates/` and `schemas/`: portable records and structural contracts.
- `tools/`: adoption, demonstration, scans, validation, and compatibility entrypoints.
- `adapters/`: opt-in host integrations, including Codex and native PowerShell support.
