# Skill Book Reference

Use this reference only when the current work depends on reusable skills, deterministic helper scripts, exact-action constraints, or skill lifecycle decisions.

## Resolution

Compile the input from every source clause and the finalized job/action/tool payload. Each clause must emit evidence-backed selection facts or an explicit no-selection-fact disposition. Bind source and payload hashes, finality, tool-schema state, and a negative-selection challenge. Do not encode a desired skill name as a fact.

Create a skill selection receipt from the compiled facts. Include exact, near, rejected, and no-match candidates, not just the chosen skill. Bind the registry hash, version, origin, lexical path, canonical path, linked file hashes, reparse components, expected objective delta, expected evidence delta, resource claim, rollback, expiry, and proof ceiling.

Do not select by first match, description similarity, popularity, or model preference. Missing or conflicting skills hold only their dependent route; the normal workflow still applies.

Revalidate the same snapshot immediately before first decision-bearing use when source facts, registry status, selected bytes, roots, reparse targets, resource claims, or the action payload may have changed.

## Blind Safety

Before blind-first diagnosis, scenario generation, or audit, expose only mechanical guidance that does not reveal causal, patch, test, scenario, or reviewer conclusions. Withhold non-blind-safe identity and reasons until the initial derivation is frozen, then reconcile with the full receipt.

## Lifecycle

Treat skill changes as measured workflow changes:

```text
raw event -> causal family and solution -> disposition -> candidate -> shadow or replay -> independent audit -> active-bounded -> measured -> revise, merge, supersede, or retire
```

Activation needs source-bound benefit, baseline, representative replay or shadow evidence, normal-path counterexamples, independent challenge, bounded activation, rollback, and measurement. A single event, recurrence count, model suggestion, or structural pass is not enough.

The actionable learning queue binds each candidate to its source, owner, family, artifact, next-use trigger, graph references, evidence, rollback, immutable state history, and later consumer. Missing metrics remain unavailable rather than becoming zero. Deferral names a return trigger.

## Application

Selection is not application. Bind one graph across source, owner, work unit, compiled final action, resolver snapshot, selected path/member hashes, delivered bytes, execution receipt, action result, independent effect observation, and lifecycle disposition.

Use the selected bounded script only after revalidation. Capture stdout, stderr, exit status, and byte identities before display decoding. Hash-only or unavailable reading, stale identity, a missing action binding, or graph mismatch holds only the dependent application.

## Effect Records

Record planned and actual objective/evidence deltas, consumer result, side effects, rollback, elapsed time, tool or delegation cost, rework, counterevidence, and proof ceiling. `prevented` requires a pre-submission block with the rejected candidate preserved and applicable-set equality. Post-submission errors are containment evidence, not prevention evidence.

## Allocation and Conditions

Choose execution and review configurations from the actual job shape and capability floor. Preserve requested, accepted, effective, estimated, unavailable, and not-applicable values rather than inventing effective metadata.

Improve delegated methods through the normal lifecycle. A user-authored condition changes only through an exact later user source bound to the old and proposed condition set, scope, dependencies, lost guarantees, rollback, effective period, and later effect. Structural matching and queue state do not grant authority.
