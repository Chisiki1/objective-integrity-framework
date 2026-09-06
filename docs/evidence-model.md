# Evidence Model

Evidence is useful only when its scope and ceiling are explicit.

## Claim-Level Mapping

Every material claim should name:

- The source clause or preservation contract it supports.
- The earliest stage where it can be decided.
- The evidence route.
- The proof ceiling.
- The identity and freshness requirements.
- The invalidation trigger.

## Evidence Routes

- `Deterministic`: parse, schema, hash, inventory, exact equality, or static contract checks.
- `Review`: independent human or agent review with stated scope and blind-first boundaries.
- `Behavioral`: tests, simulations, fixture runs, and fault-sensitivity checks.
- `Runtime`: observed behavior in the real runtime or an equivalence-proven environment.
- `External`: post-action readback from the external consumer or public target.

## Evidence Limits

A route can pass its own claim while leaving a higher claim unproven. The normative reference also calls this a proof ceiling. The framework encourages positive language such as "supports", "is designed to", "structural pass", and "measured status" so readers know what has been shown without being buried in caveats.

Keep producer, verifier, evidence, environment, and external-consumer identities separate. Hashes preserve identity and change detection. A consumer claim is closed by evidence at that consumer or a recorded equivalence bridge, not by correlated producer and verifier logic alone.

For selected guidance, distinguish compilation, selection, bytes delivered, execution, action result, and independently observed effect. For durable actions, distinguish planned, no-effect, partial, committed, and unknown effects. Those distinctions make evidence useful without forcing every page to repeat generic warnings.
