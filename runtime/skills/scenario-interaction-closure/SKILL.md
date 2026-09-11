---
name: scenario-interaction-closure
description: Freeze a source-bound, blind-first harm-scenario and interaction closure before a material correction, then preserve its meaning through evidence decomposition and recomposition.
---

# Scenario interaction closure

Use for a material candidate edit, migration, or released action when normal success depends on state, owner, consumer, ordering, recovery, or cross-component evidence.

1. Derive initial scenarios from the source-bound objective, normal-success envelope, state/owner/consumer/dependency boundaries and change/fault boundary.  Do not seed initial generation with an observed error, patch, or test.
2. Record factors, reachable edges/hyperedges, semantic locks, local and relational claims, final consumer oracles, unmasking/recovery, and a disposition for every frozen relation.
3. Decompose only when the retained local and relational evidence has a recomposition witness.  A missing relation is an orphan, not a PASS.
4. Run `python -B scripts/validate_scenario_closure.py --input <record.json>`.  It checks schema, identity, no-drop sets, and dispositions only.  It cannot prove reachability, harm, causation, witness sufficiency, or consumer success.
5. On a material delta, append a delta record that names the changed boundary and the decision it can change; do not regenerate unrelated families.

