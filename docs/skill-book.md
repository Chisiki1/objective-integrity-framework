# Skill Book Plane

The Skill Book plane turns reusable operating knowledge into focused instructions and deterministic checks for one work unit. It is separate from the normative workflow and from learning records.

## Three Independent Planes

Objective Integrity Framework uses three cooperating planes:

- **Workflow authority:** Defines objective extraction, scope, evidence, scenarios, root cause, impact, audit placement, action eligibility, and completion.
- **Learning records:** Preserve exact project evidence locally and promote sanitized reusable families globally.
- **Skill Book:** Resolves focused skills and deterministic scripts for the current job, then records whether they actually helped.

No plane proves another. A selected skill does not prove semantic applicability. A registry pass does not prove the user objective. A learning entry does not prove the next action used it. A deterministic script can block or pass only the bounded mechanical claim it checks.

## Deterministic Resolution

A resolver should select every applicable active skill and return rejected candidates with reasons. It should not choose by registry order, fuzzy description similarity, popularity, model preference, or first match.

A skill selection receipt should bind:

- Objective contract and source-clause identifiers.
- Normalized job, action, tool, environment, permission, resource, consumer, and risk facts.
- Registry identity and registry hash.
- Selected and rejected skill identifiers, versions, origins, statuses, and reasons.
- Canonical path, lexical path, and linked file hashes.
- Reparse or symlink components, including resolved targets.
- Expected objective and evidence delta.
- Resource claim, rollback route, expiry, and revalidation triggers.
- Selection snapshot hash.
- Proof ceiling.

Only active, bounded entries should be selected by default. Candidate, shadow, audited, superseded, and retired entries can remain in the registry for lineage, but they should not be normal resolution targets.

## Path and Snapshot Integrity

Skill paths are mutable filesystem state. A safe resolver preserves both lexical and physical identity:

- Walk the lexical root-to-skill path before resolving it.
- Detect symlinks, junctions, or other reparse components.
- Record each component and resolved target in the receipt.
- Require the final physical path to remain under the resolved origin root.
- Include reparse information in the selection snapshot.

If a link is retargeted, a file changes, a registry status changes, or the objective facts change before first decision-bearing use, the old selection is a stale snapshot and must be resolved again.

## Blind-Safe Projection

Some skills leak prior causal conclusions, patch ideas, tests, or reviewer opinions. For blind-first diagnosis, scenario generation, or audit work, the initial projection may expose only blind-safe mechanical entries such as syntax, schema, path, permission, and tool-contract checks.

Non-blind-safe entries are withheld by count and release condition until the initial derivation is frozen. After that point, a reconciled receipt can expose full selected and rejected details.

## Skill Lifecycle

Skill lifecycle is evidence-gated, not count-gated:

```text
raw event -> causal family and solution -> disposition -> candidate -> shadow or replay -> independent audit -> active-bounded -> measured -> revise, merge, supersede, or retire
```

Disposition options include:

- Existing skill applicable.
- Existing generic invariant sufficient.
- New candidate.
- Merge candidate.
- Revise candidate.
- Supersede candidate.
- Retire candidate.
- Project-local, no reusable skill.
- Global family candidate.
- Unconfirmed.
- Rejected.

A single failure, repeated count, model suggestion, registry validation, or documentation review should not activate a semantic skill. Activation needs a source-bound benefit, a baseline, representative replay or shadow evidence, normal-path counterexamples, independent challenge, bounded activation, rollback, and measurement.

## Effect Records

Invocation effects belong first in the project-local record, where exact evidence can be preserved. A sanitized global record can summarize the reusable family, applicability, aggregate effect, invalidation trigger, and proof ceiling.

Effect outcomes should distinguish:

- Advanced the objective.
- No effect.
- Recurrence.
- False block.
- Misselection.
- Prevented before submission.
- Outcome unknown.
- Not observed.

An exact-action preflight is the bounded mechanical form of this check for commands, tool payloads, or parallel members. `prevented` is narrow. It requires an observed pre-submission block, the rejected candidate preserved, and applicable-set equality for the final action representation. Parser rejection after submission, exceptions, no-ops, or later repairs are containment evidence, not prevention evidence.

## Retirement and Parallel Safety

Only one semantic writer should update the shared registry or learning ledgers for a work unit. Parallel jobs may read the same immutable snapshot, but their resource claims and write scopes must be separated.

Retirement is not just a status label. Retired or superseded skills should leave active discovery roots, while immutable evidence and lineage remain available outside those roots. Replacement identity, reason, effective time, prior status, and rollback path should be explicit.

## Structural and Empirical Status

The Skill Book can provide strong structural evidence: selected and rejected reasons, hashes, path identity, stale-snapshot detection, lifecycle conformance, and mechanical preflight results. Empirical improvement remains measured status. Reduced drift, fewer repeated failures, lower rework, better consumer outcomes, and lower cost require representative task evidence in the adopting environment.
