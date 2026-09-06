---
name: master-guided-skill-resolver
description: Compile provenance-bound source and finalized-action facts, then resolve exact registered workflow skills with deterministic path- and hash-bound selected and rejected receipts. Use before material work when reusable guidance may change the action or when selection is missing, stale, conflicting, retired, or ambiguous.
---

# Master-Guided Skill Resolver

Use this skill for one job: compile current source and action facts without desired-skill naming, then resolve them into a deterministic selection snapshot.

1. Read the configured project and shared learning sources. Freeze the source-bound objective, every source-clause disposition, authority, scope, and exact finalized job, action, and tool facts.
2. Create an `mgskill-fact-source-v2` input matching [input-schema.md](references/input-schema.md). Every source clause is fact-bearing or no-selection-fact, and every emitted or excluded fact has evidence. Prefer `python -B scripts/build_skill_fact_input.py --plan <plan.json> --registry <registry.json> --output <source.json>`.
3. Run `python -B scripts/compile_skill_facts.py --source <source.json> --registry <registry.json> --output <receipt.json> --resolver-input-output <input.json>`. Consume exact, near, and no-match results, action finality, tool-schema state, and the completed negative-selection countermodel.
4. Run `python -B scripts/resolve_skills.py --input <input.json> --registry <registry.json> --project-root <project-skill-root> --user-root <shared-skill-root>`. Supply only roots intentionally in scope.
5. Consume every selected and rejected reason. Never use first-match priority. Missing, stale, conflicting, invalid, or unmatched entries hold only their dependent route; continue under the normal workflow when the receipt selects fallback.
6. Before first decision-bearing use, rerun compiler and resolver with unchanged source, action, registry, and root identities. Require the same compiler result, selection snapshot, canonical path, `SKILL.md` hash, linked member hashes, and reparse state.
7. For a bounded child job, pass exact source, action, registry, compiler, selection, skill, path, member, disclosure, resource, and proof-ceiling identities. The primary owner remains the single semantic writer.
8. For a material selected-skill use, read [application-chain-contract.md](references/application-chain-contract.md) and run `python -B scripts/skill_application_bridge.py prepare --bundle <bundle.json>`. Continue only on `GO`. The bridge opens selected files, checks path and hash, and returns delivered bytes; reading remains separate from application.
9. Execute supported bounded scripts through `run-script` or optional `run-powershell`, then bind the actual execution, action result, and later effect to the same graph. `BLOCK`, `ERROR`, and `HOLD` do not prove target execution. Independent reviewers remain read-only.

For blind-first work, initial resolution exposes only entries marked blind-safe mechanical and withholds other identity and reasons until the initial derivation is frozen. Reconcile in a later resolution; do not send both views together.

Legacy manually assembled v1 inputs may remain readable, but they do not close material provenance, negative-selection, action-finality, or typed-schema claims. Use `inventory_skillbook.py` and `validate_registry.py` for declared identity, set, path, hash, reference, and status structure only. Read [registry-contract.md](references/registry-contract.md) when maintaining a public registry or interpreting conflicts.
