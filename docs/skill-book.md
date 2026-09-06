# Skill Book Plane

The Skill Book turns reusable operating knowledge into focused instructions and bounded deterministic actions for one work unit. It is separate from the normative workflow and learning records, and it closes the path from source facts to actual application and measured effect.

## Three Independent Planes

Objective Integrity Framework uses three cooperating planes:

- **Workflow authority:** Defines objective extraction, scope, evidence, scenarios, root cause, impact, audit placement, action eligibility, and completion.
- **Learning records:** Preserve exact project evidence locally and promote sanitized reusable families globally.
- **Skill Book:** Resolves focused skills and deterministic scripts for the current job, then records whether they actually helped.

No plane proves another. Selection, delivered bytes, execution, action result, and observed effect are separate states.

## Compile Facts From the Source and Final Action

Before resolution, build a typed fact set from:

- every source clause, with either emitted selection facts or an explicit no-selection-fact disposition;
- the finalized job, action, tool payload, environment, permissions, resources, and consumer;
- tool-schema state as available, unavailable, or not applicable;
- the source and payload identities;
- a negative-selection challenge describing the strongest candidate that should remain unselected and why.

The compiler rejects missing or duplicate clause dispositions, overlap between emitted and excluded facts, desired skill names disguised as facts, incomplete finality, and malformed schema state. This prevents a caller from selecting the desired answer by writing the conclusion into the input.

## Deterministic Resolution

A resolver selects every applicable active skill and returns exact, near, rejected, and no-match candidates with reasons. It does not choose by registry order, fuzzy description similarity, popularity, model preference, or first match.

A skill selection receipt should bind:

- Objective contract, exact source identity, and source-clause identifiers.
- Compiled job, finalized action, canonical payload, tool, environment, permission, resource, consumer, and risk facts.
- Registry identity and registry hash.
- Selected and rejected skill identifiers, versions, origins, statuses, and reasons.
- Canonical path, lexical path, and linked file hashes.
- Reparse or symlink components, including resolved targets.
- Expected objective and evidence delta.
- Resource claim, rollback route, expiry, and revalidation triggers.
- Selection snapshot hash.
- Proof ceiling.
- Negative-selection countermodel and compiler result.

Only active, bounded entries should be selected by default. Candidate, shadow, audited, superseded, and retired entries can remain in the registry for lineage, but they should not be normal resolution targets.

The public distribution catalog is generated from the shipped eight generic skill entrypoints. `tools/catalog.py` writes to stdout by default or to an explicit new output path. Catalog generation records public package identity; it does not activate skills or import private learning history.

## Path and Snapshot Integrity

Skill paths are mutable filesystem state. A safe resolver preserves both lexical and physical identity:

- Walk the lexical root-to-skill path before resolving it.
- Detect symlinks, junctions, or other reparse components.
- Record each component and resolved target in the receipt.
- Require the final physical path to remain under the resolved origin root.
- Include reparse information in the selection snapshot.

If a link is retargeted, a file changes, a registry status changes, or the objective facts change before first decision-bearing use, the old selection is a stale snapshot and must be resolved again.

## Apply the Selected Guidance

Resolution is preparation, not application. A material application graph binds:

```text
source and owner
-> compiled final action
-> resolver snapshot
-> selected path and member hashes
-> bytes delivered
-> exact execution receipt
-> action result
-> independent effect observation
-> lifecycle disposition
```

The application bridge opens and hash-verifies selected non-UI files itself. It rechecks identity immediately before execution, passes exact arguments without a general-purpose shell for portable Python actions, captures stdout and stderr bytes and exit status before display decoding, and binds the result to the same owner, lease, candidate, action, schema, and member set.

Caller-authored readiness labels do not replace these relations. Hash-only or unavailable file reading, stale members, a missing final action, graph mismatch, or an unbound script holds only the dependent application route.

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

Effect outcomes distinguish:

- Advanced the objective.
- No effect.
- Recurrence.
- False block.
- Misselection.
- Prevented before submission.
- Outcome unknown.
- Not observed.

An exact-action preflight is the bounded mechanical form of this check for commands, tool payloads, or parallel members. `prevented` is narrow: it requires an observed pre-submission block, the rejected candidate preserved, and applicable-set equality for the final action representation. Parser rejection after submission, exceptions, no-ops, or later repairs are containment evidence.

## Retirement and Parallel Safety

Only one semantic writer updates the shared registry or learning ledgers for a work unit. Parallel jobs may read the same immutable snapshot, but their resource claims and write scopes remain separated.

Retirement is not just a status label. Retired or superseded skills should leave active discovery roots, while immutable evidence and lineage remain available outside those roots. Replacement identity, reason, effective time, prior status, and rollback path should be explicit.

## Lifecycle and Condition Changes

The learning queue keeps candidates actionable across tasks. It records due triggers, artifact and graph references, immutable events, typed metrics, later consumer results, and revise/narrow/merge/supersede/retire decisions.

Changing a delegated method follows the normal improvement route. Changing a user-authored condition requires an exact later user source bound to the old and proposed condition set, scope, meaning delta, dependent conditions, lost guarantees, rollback, and effective period. Structural proposal matching never creates that authority.

## Evidence Boundaries

The Skill Book provides identity and structural evidence: compiled clause coverage, selected and rejected reasons, hashes, path identity, stale-snapshot detection, application graph consistency, lifecycle conformance, and bounded mechanical preflight results. Final objective progress and improvement are measured at the actual consumer. See [Runtime Reference](runtime-reference.md) for the executable entrypoints.
