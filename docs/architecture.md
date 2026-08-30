# Architecture

The framework is organized as a set of records and transitions.

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

## Transition Layer

1. Extract the objective.
2. Freeze baseline and semantic authority boundaries.
3. Generate blind scenarios before anchoring on the first error or proposed patch.
4. Design or change only the causal link that advances the objective.
5. Recompose decomposed evidence back to the system claim.
6. Audit the exact candidate at the stage where the audit can change a decision.
7. Test partitioned evidence routes without losing the system-level meaning.
8. Project action eligibility before external writes or completion claims.
9. Resolve focused Skill Book guidance when it can materially improve correctness, recurrence prevention, privacy, or external-action safety.
10. Measure the effect and feed reusable knowledge back into action constraints.

## Governance Planes

The framework keeps three governance planes independent:

- `WORKFLOW`: normative authority for objective, scope, evidence, scenarios, root cause, impact, verification, audit, action eligibility, and completion.
- `LEARNING`: project-local raw evidence and sanitized global reusable knowledge.
- `SKILL BOOK`: focused instructions, deterministic helper scripts, selected/rejected resolver receipts, and skill lifecycle evidence for the current work unit.

These planes cooperate, but none replaces another. A skill receipt does not prove semantic correctness. A learning entry does not prove the next action used it. A workflow checklist does not prove empirical improvement.

## Runtime and Evidence Identities

The framework keeps five identities separate:

- `PRODUCT`: source, configuration, artifacts, persistent candidate state, and runtime inputs.
- `VERIFIER`: tests, fixtures, oracles, scanners, validators, and review scripts.
- `EVIDENCE`: logs, captures, reports, timestamps, and manifests.
- `ENVIRONMENT`: toolchain, operating system, services, permissions, and external dependencies.
- `EXTERNAL`: public repositories, production systems, deployed resources, accounts, and consumer-visible effects.

Keeping these planes separate prevents a lower-level pass from being promoted beyond what it can prove.
