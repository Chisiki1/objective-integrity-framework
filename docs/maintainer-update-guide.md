# Maintainer Update Guide

Every public update should preserve seven release conditions. They keep the project useful, portable, welcoming, and faithful to its purpose.

## The Seven Conditions

### 1. Current capability, isolated work

Map the current generic behavior into the public package without copying private implementation context. Build and exercise the candidate only in an isolated workspace. Do not let bootstrap, tests, imports, caches, defaults, or helper scripts write into a live agent configuration or reference environment.

### 2. Public-release quality

Ship one coherent capability cut: documentation, runtime, adapters, examples, templates, schemas, validators, and installation members must agree. Remove unfinished scaffolds, stale paths, broken links, ambiguous errors, and generated residue from the release candidate.

### 3. English throughout

All tracked public prose, comments, examples, schema descriptions, command help, errors, and generated public outputs use English. Language review covers the whole deliverable, not only the landing page.

### 4. Easy, truthful adoption

Lead with a short successful path and concrete value. Keep destinations explicit, preview before writing, back up replacements, and explain rollback. Describe shipped behavior confidently. Put material evidence boundaries where they help a decision instead of repeating generic caveats throughout the introduction.

### 5. Privacy by construction

Use neutral synthetic identifiers and scenarios. Exclude personal paths, accounts, repository or task identifiers, credentials, sessions, private histories, real incidents, and source-environment defaults from tracked files and generated outputs.

### 6. Outcome and self-improvement first

Keep primary-objective achievement as the product story. Learning is complete only when a source-bound result becomes an owned candidate, changes a later matching action, receives an observed effect, and can be revised, narrowed, merged, superseded, or retired. Record and tool counts are not improvement outcomes.

### 7. Careful progressive explanation

Provide a concise landing page, a runnable synthetic demonstration, adoption and rollback instructions, focused concept guides, exact runtime reference, troubleshooting, and a normative reference. Explain advanced machinery where it becomes relevant rather than making every reader absorb it at the start.

## Update Workflow

1. Freeze the source conditions, public baseline identity, and intended semantic delta.
2. Build a capability map that marks every existing normative family and every new generic behavior as present, generalized, adapter-only, deliberately excluded with a semantic-equivalence reason, or unresolved.
3. Freeze blind scenario families and their material interactions before candidate-bearing edits.
4. Define the public member set and protected roots. Keep the private evidence source outside the public repository and release archive.
5. Implement the generic core first. Put host-specific behavior in an optional adapter.
6. Freeze one implementation snapshot. Review actual impact, privacy, English, source-to-path coverage, and documentation/runtime agreement before behavioral tests.
7. Exercise the documented clean-room path, recovery path, and representative counterexamples in an isolated sandbox.
8. Freeze the tested release candidate and perform the final public-quality, privacy, and provenance review.
9. Treat publication as its own action: confirm authority, remote freshness, exact candidate identity, and public readback.

## No-Drop Review

The [Public Coverage Map](coverage-map.md) is the maintainer's index. A row is useful only when its linked artifact carries the behavior. Documentation mention alone does not close a runtime capability; a runtime file alone does not close adoption, recovery, or consumer meaning.

When a family changes, preserve its source condition, supported normal path, state and owner, interaction edges, final consumer oracle, evidence route, rollback, and lineage. Re-enter only the affected families when that boundary is clear.

## Release Notes

Describe observable additions and migrations. Separate structural checks, runtime observations, and public readback. Avoid guarantees, universal prevention claims, and unsupported comparative language. A concise accurate boundary is stronger than either hype or a page of repeated disclaimers.
