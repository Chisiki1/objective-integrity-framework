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

## Transition Layer

1. Extract the objective.
2. Freeze baseline and semantic authority boundaries.
3. Generate blind scenarios before anchoring on the first error or proposed patch.
4. Design or change only the causal link that advances the objective.
5. Recompose decomposed evidence back to the system claim.
6. Audit the exact candidate at the stage where the audit can change a decision.
7. Test partitioned evidence routes without losing the system-level meaning.
8. Project action eligibility before external writes or completion claims.
9. Measure the effect and feed reusable knowledge back into action constraints.

## Planes

The framework keeps five identities separate:

- `PRODUCT`: source, configuration, artifacts, persistent candidate state, and runtime inputs.
- `VERIFIER`: tests, fixtures, oracles, scanners, validators, and review scripts.
- `EVIDENCE`: logs, captures, reports, timestamps, and manifests.
- `ENVIRONMENT`: toolchain, operating system, services, permissions, and external dependencies.
- `EXTERNAL`: public repositories, production systems, deployed resources, accounts, and consumer-visible effects.

Keeping these planes separate prevents a lower-level pass from being promoted beyond what it can prove.
