---
name: scenario-interaction-closure
description: Freeze source-bound blind-first harm scenarios and their reachable interactions before a material change, then preserve their meaning through evidence decomposition and recomposition. Use when state, ownership, consumers, ordering, recovery, workload, or cross-component evidence can change the outcome.
---

# Scenario Interaction Closure

1. Derive initial scenario families from the source-bound objective, normal-success envelope, state, owner, consumer, dependency, permission, resource, workload, and change or fault boundaries. Do not seed the initial generation with an observed error, proposed patch, or existing test.
2. Factor each material family by activation, lifecycle, control and data path, order and clock, concurrency, resource, side effect, persistence, retry, recovery, oracle, and harm. Record reachable edges or hyperedges rather than a blind Cartesian product.
3. Freeze semantic locks, local and relational claims, final consumer oracles, continuity, unmasking, recovery, and a disposition for every relation.
4. Decompose only when the retained local and relational evidence has a recomposition witness. A missing relation is an orphan, not a pass.
5. Run `python -B scripts/validate_scenario_closure.py --input <record.json>`. The validator checks schema, identity, set equality, lineage, references, and dispositions only. It cannot prove reachability, harm, causation, witness sufficiency, or consumer success.
6. After a material delta, append only the changed scenario, interaction, state, consumer, or recovery boundaries and the decision they can change. Regenerate broadly only when the objective, architecture, responsibility, supported scope, or state model materially changes.
